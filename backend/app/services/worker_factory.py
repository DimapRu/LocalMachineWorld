"""Director-triggered Worker Factory for isolated delegated task sessions.

This is the backend-first orchestration path inspired by Claude Code/Roo Code:
Director calls `delegate_task`, backend creates a Board card, picks/warms a Worker,
runs an isolated Worker tool loop, then returns only a compact status summary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import re
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Agent, AgentStatus, Task, TaskContextKind, TaskStatus
from app.services import backend_logs
from app.services import task_context as task_context_service
from app.services.activity import activity_state
from app.services.runtime_memory import MemoryBudgetError, runtime_memory


@dataclass
class WorkerSessionResult:
    """Public result returned to the Director after a delegated session starts/finishes."""

    status: str
    task_id: str
    task_title: str
    worker_agent_id: Optional[str] = None
    worker_name: str = ""
    worker_model: str = ""
    result: str = ""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_director_summary(self) -> dict[str, Any]:
        """Return a context-safe payload: no worker scratchpad, no source dumps."""
        return {
            "status": self.status,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "worker_agent_id": self.worker_agent_id,
            "worker_name": self.worker_name,
            "worker_model": self.worker_model,
            "result": (self.result or "")[:1200],
            "session_id": self.session_id,
        }


def _title_key(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").lower()).strip()


async def create_unique_task_title(db: AsyncSession, requested_title: str) -> str:
    """Return a duplicate-safe Board title without rejecting Director actions."""
    base = re.sub(r"\s+", " ", requested_title or "").strip()[:160] or "Delegated worker task"
    result = await db.execute(select(Task.title))
    existing = {_title_key(title) for title in result.scalars().all()}
    if _title_key(base) not in existing:
        return base

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    suffix = f" [{stamp}]"
    candidate = f"{base[:200 - len(suffix)]}{suffix}"
    if _title_key(candidate) not in existing:
        return candidate

    counter = 2
    while True:
        suffix = f" [{stamp}-{counter}]"
        candidate = f"{base[:200 - len(suffix)]}{suffix}"
        if _title_key(candidate) not in existing:
            return candidate
        counter += 1


class WorkerFactory:
    """Creates Board tasks and runs isolated Worker sessions on Director demand."""

    def __init__(self) -> None:
        self._active_sessions: set[str] = set()

    async def delegate_task(
        self,
        *,
        db: AsyncSession,
        director: Agent,
        instruction: str,
        task_title: str,
        worker_model: str = "",
        priority: int = 50,
        wait: bool = True,
        max_depth: int = 1,
    ) -> WorkerSessionResult:
        """Create a Board task, select a Worker, and run an isolated session.

        `wait=True` makes the Director tool call behave like Claude Code's `Task`:
        the call returns when the isolated subagent finishes or becomes blocked.
        """
        if not director or not director.is_director:
            raise PermissionError("delegate_task requires a Director caller")
        if max_depth < 0:
            raise ValueError("delegate_task depth cap exceeded")

        instruction = (instruction or "").strip()
        if not instruction:
            raise ValueError("delegate_task requires instruction")

        task = Task(
            title=await create_unique_task_title(db, task_title or instruction[:80]),
            description=instruction,
            status=TaskStatus.todo.value,
            priority=int(priority or 50),
        )
        db.add(task)
        await db.flush()

        worker = await self._select_worker(db, worker_model=worker_model)
        if worker is None:
            worker = director

        task.assigned_agent_id = worker.id
        task.status = TaskStatus.in_progress.value
        worker.status = AgentStatus.working.value
        worker.last_active = datetime.utcnow()

        session_id = str(uuid.uuid4())
        self._active_sessions.add(session_id)
        await task_context_service.append(
            db,
            task_id=task.id,
            author_agent_id=director.id,
            kind=TaskContextKind.director.value,
            content=instruction,
            extra={
                "session_id": session_id,
                "delegated_by": director.id,
                "depth": max_depth,
                "context_boundary": "Director receives only Board status and result summary by default.",
            },
        )
        await backend_logs.append(
            db,
            source="WorkerFactory",
            level="INFO",
            message=f"Director delegated task to isolated Worker session: {task.title}",
            action_type="delegate_task",
            status="started",
            agent_id=worker.id,
            task_id=task.id,
            payload={"session_id": session_id, "worker_model": worker.model_name, "wait": wait},
        )
        await activity_state.update_and_broadcast(
            action="delegate_task",
            status=f"Worker Factory started isolated session for {task.title}",
            active_agent={"id": worker.id, "name": worker.name, "role": "director" if worker.is_director else "worker"},
            active_model=worker.model_name,
            active_task={"id": task.id, "title": task.title, "status": task.status},
            boss_summary="Director delegated dirty file/code work to an isolated Worker session; Director context receives only summary status.",
            event_type="worker_factory",
            event_title=f"Delegated: {task.title}",
            event_meta={"session_id": session_id},
        )
        await db.commit()

        if not wait:
            asyncio.create_task(self._run_background(worker.id, task.id, session_id))
            return WorkerSessionResult(
                status="queued",
                task_id=task.id,
                task_title=task.title,
                worker_agent_id=worker.id,
                worker_name=worker.name,
                worker_model=worker.model_name,
                result="Worker session queued; watch Board status for completion.",
                session_id=session_id,
            )

        return await self._run_and_collect(worker.id, task.id, session_id)

    async def read_file_context(self, *, db: AsyncSession, director: Agent, task_id: str, limit: int = 20) -> dict[str, Any]:
        """Explicit Director context read for a task; never automatic."""
        if not director or not director.is_director:
            raise PermissionError("read_file_context requires a Director caller")
        task = await db.get(Task, task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        summary = await task_context_service.director_summary(db, task_id=task.id, limit=limit)
        return {
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "result": (task.result or "")[:1200],
            "context_summary": summary[:4000],
        }

    async def _select_worker(self, db: AsyncSession, *, worker_model: str = "") -> Optional[Agent]:
        stmt = select(Agent).where(Agent.is_director == False).where(Agent.status == AgentStatus.idle.value)
        if worker_model:
            stmt = stmt.where(Agent.model_name == worker_model)
        stmt = stmt.order_by(Agent.tasks_completed.desc())
        workers = list((await db.execute(stmt)).scalars().all())
        if not workers and worker_model:
            workers = list(
                (await db.execute(
                    select(Agent)
                    .where(Agent.is_director == False)
                    .where(Agent.status == AgentStatus.idle.value)
                    .order_by(Agent.tasks_completed.desc())
                )).scalars().all()
            )
        if not workers:
            return None
        return workers[0]

    async def _run_background(self, worker_id: str, task_id: str, session_id: str) -> None:
        try:
            await self._run_and_collect(worker_id, task_id, session_id)
        finally:
            self._active_sessions.discard(session_id)

    async def _run_and_collect(self, worker_id: str, task_id: str, session_id: str) -> WorkerSessionResult:
        from app.services.agent_worker import agent_worker

        try:
            await agent_worker.run_isolated_task_session(worker_id, task_id, session_id=session_id)
        finally:
            self._active_sessions.discard(session_id)

        async with async_session() as session:
            task = await session.get(Task, task_id)
            worker = await session.get(Agent, worker_id)
            status_value = task.status if task else "unknown"
            result_text = task.result if task else "Task disappeared before completion."
            outcome = WorkerSessionResult(
                status="success" if status_value == TaskStatus.done.value else status_value,
                task_id=task_id,
                task_title=task.title if task else task_id,
                worker_agent_id=worker.id if worker else worker_id,
                worker_name=worker.name if worker else "",
                worker_model=worker.model_name if worker else "",
                result=result_text or "",
                session_id=session_id,
            )
            await backend_logs.append(
                session,
                source="WorkerFactory",
                level="INFO" if outcome.status == "success" else "WARNING",
                message=f"Isolated Worker session finished with status={outcome.status}",
                action_type="delegate_task",
                status=outcome.status,
                agent_id=worker_id,
                task_id=task_id,
                payload=outcome.to_director_summary(),
            )
            await session.commit()
            return outcome


worker_factory = WorkerFactory()


__all__ = ["WorkerFactory", "WorkerSessionResult", "worker_factory", "create_unique_task_title"]
