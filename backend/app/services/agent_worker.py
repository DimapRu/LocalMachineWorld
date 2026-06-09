"""Agent Worker Loop — background task processor for AI agents."""

import asyncio
from datetime import datetime, timedelta
import json
import re
from typing import Any, Optional
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Agent, AgentStatus, Task, TaskContextKind, TaskStatus
from app.services import backend_logs
from app.services import task_context as task_context_service
from app.services import worker_audit
from app.services import worker_tools
from app.services.activity import activity_state
from app.services.resources import resource_monitor
from app.services.runtime import runtime


# Hard cap on the OpenAI tool loop so a chatty model can never spin forever.
MAX_TOOL_ITERATIONS: int = 8

# Only recover in-progress Board tasks that have not been touched for a while.
# Without this guard the poller can reset a live Worker task on every cycle,
# spawning duplicate Worker runs and flooding Agent Logs.
STALE_IN_PROGRESS_AFTER = timedelta(minutes=10)


class AgentWorker:
    """Background worker that polls for tasks and runs agent inference."""

    def __init__(self, poll_interval: float = 5.0):
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._active_runs: set[str] = set()

    async def start(self):
        """Start the worker loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        snapshot = activity_state.set_worker_running(True)
        await activity_state.broadcast(snapshot)
        print("[AgentWorker] Started")

    async def stop(self):
        """Stop the worker loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        snapshot = activity_state.set_worker_running(False)
        await activity_state.broadcast(snapshot)
        self._active_runs.clear()
        print("[AgentWorker] Stopped")

    def _schedule_agent_task(self, agent_id: str, task_id: str, *, session_id: str = "", isolated: bool = False) -> bool:
        """Start one in-memory Worker run unless the same task is already running."""
        key = f"{task_id}:{session_id or 'default'}"
        if key in self._active_runs:
            return False
        self._active_runs.add(key)
        task = asyncio.create_task(self._run_agent_task(agent_id, task_id, session_id=session_id, isolated=isolated))
        task.add_done_callback(lambda _finished: self._active_runs.discard(key))
        return True

    async def _loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._process_cycle()
            except Exception as e:
                await backend_logs.append(
                    source="Системный монитор",
                    level="ERROR",
                    message="AgentWorker cycle crashed.",
                    error_details=f"{type(e).__name__}: {e}",
                    action_type="worker_cycle",
                    status="failed",
                )
                print(f"[AgentWorker] Cycle error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _process_cycle(self):
        """One processing cycle: find pending tasks and assign to agents."""
        await activity_state.update_and_broadcast(
            running=self._running,
            action="scanning_board",
            status="Boss is scanning the Board for unassigned todo tasks",
            boss_summary="Boss is checking Board state, available agents, and whether a task should be delegated.",
            event_type="worker",
            event_title="Board scan started",
        )
        async with async_session() as session:
            # 0. Recover genuinely stale tasks/agents left in-progress by an old
            # backend process. Do not touch fresh in_progress tasks: they may be
            # actively running in an asyncio task created by a previous poll.
            stale_cutoff = datetime.utcnow() - STALE_IN_PROGRESS_AFTER
            orphaned_tasks = await session.execute(
                select(Task)
                .where(Task.status == TaskStatus.in_progress.value)
                .where(Task.updated_at < stale_cutoff)
            )
            for orphaned_task in orphaned_tasks.scalars().all():
                previous_agent_id = orphaned_task.assigned_agent_id
                orphaned_task.status = TaskStatus.todo.value
                orphaned_task.assigned_agent_id = None
                orphaned_task.logs = orphaned_task.logs or []
                orphaned_task.logs.append(
                    f"[{datetime.utcnow().isoformat()}] Recovered from stale in_progress state after {STALE_IN_PROGRESS_AFTER} without updates."
                )
                if previous_agent_id:
                    stale_assignee = await session.get(Agent, previous_agent_id)
                    if stale_assignee and stale_assignee.status in (AgentStatus.working.value, AgentStatus.thinking.value):
                        stale_assignee.status = AgentStatus.idle.value
                        stale_assignee.last_active = datetime.utcnow()
                await backend_logs.append(
                    session,
                    source="AgentWorker",
                    level="WARNING",
                    message=f"Recovered stale in-progress task '{orphaned_task.title}' and returned it to todo.",
                    action_type="task_status_recovery",
                    status="success",
                    task_id=orphaned_task.id,
                    agent_id=previous_agent_id,
                    payload={"stale_after_seconds": int(STALE_IN_PROGRESS_AFTER.total_seconds())},
                )

            # 1. Recover agents stuck in working/thinking when no in-progress task references them.
            stale_agents = await session.execute(
                select(Agent).where(Agent.status.in_([AgentStatus.working.value, AgentStatus.thinking.value]))
            )
            for stale_agent in stale_agents.scalars().all():
                active_task = await session.execute(
                    select(Task)
                    .where(Task.assigned_agent_id == stale_agent.id)
                    .where(Task.status == TaskStatus.in_progress.value)
                )
                if active_task.scalar_one_or_none():
                    continue
                previous_status = stale_agent.status
                stale_agent.status = AgentStatus.idle.value
                stale_agent.last_active = datetime.utcnow()
                await backend_logs.append(
                    session,
                    source="AgentWorker",
                    level="WARNING",
                    message=f"Recovered stale agent status for {stale_agent.name}: {previous_status} -> idle.",
                    action_type="agent_status_recovery",
                    status="success",
                    agent_id=stale_agent.id,
                )

            # 2. Find tasks that are already assigned by the Board but not yet started.
            assigned_pending = await session.execute(
                select(Task)
                .where(Task.status == TaskStatus.todo.value)
                .where(Task.assigned_agent_id.is_not(None))
            )
            assigned_tasks = assigned_pending.scalars().all()

            for task in assigned_tasks:
                agent_result = await session.execute(select(Agent).where(Agent.id == task.assigned_agent_id))
                agent = agent_result.scalar_one_or_none()
                if not agent:
                    continue
                if agent.status != AgentStatus.idle.value:
                    active_for_agent = await session.execute(
                        select(Task)
                        .where(Task.assigned_agent_id == agent.id)
                        .where(Task.status == TaskStatus.in_progress.value)
                    )
                    if active_for_agent.scalar_one_or_none():
                        continue
                    await backend_logs.append(
                        session,
                        source="AgentWorker",
                        level="WARNING",
                        message=f"Recovered stale Worker status for {agent.name}; starting assigned todo task.",
                        action_type="worker_status_recovery",
                        status="success",
                        agent_id=agent.id,
                        task_id=task.id,
                    )
                task.status = TaskStatus.in_progress.value
                agent.status = AgentStatus.working.value
                agent.last_active = datetime.utcnow()
                await activity_state.update_and_broadcast(
                    action="assigned_task_starting",
                    status=f"Starting assigned task for {agent.name}: {task.title}",
                    active_agent={"id": agent.id, "name": agent.name, "role": "director" if agent.is_director else "worker"},
                    active_model=agent.model_name,
                    active_runtime=runtime.runtime_for(agent.model_name),
                    active_task={"id": task.id, "title": task.title, "status": task.status},
                    boss_summary="Board already assigned this task; worker loop is starting execution.",
                    event_type="worker",
                    event_title=f"Starting: {task.title}",
                )
                self._schedule_agent_task(agent.id, task.id)

            # 3. Find unassigned tasks in todo status
            unassigned = await session.execute(
                select(Task)
                .where(Task.status == TaskStatus.todo.value)
                .where(Task.assigned_agent_id.is_(None))
            )
            tasks = unassigned.scalars().all()

            if not tasks:
                await session.commit()
                await activity_state.update_and_broadcast(
                    running=self._running,
                    action="idle",
                    status="No unassigned Board tasks found",
                    active_task={},
                    boss_summary="Boss is waiting for the next actionable Board item.",
                    event_type="worker",
                    event_title="Board idle",
                )
                return

            # 4. Find the director agent
            director_result = await session.execute(
                select(Agent).where(Agent.is_director == True)
            )
            director = director_result.scalar_one_or_none()

            for task in tasks:
                if director and director.status == AgentStatus.idle.value:
                    # Director analyzes and delegates
                    await self._delegate_task(session, director, task)
                else:
                    # No director — assign to first available worker
                    await self._assign_to_any_worker(session, task)

            await session.commit()

    async def _delegate_task(self, session: AsyncSession, director: Agent, task: Task):
        """Director analyzes task and picks best worker."""
        await activity_state.update_and_broadcast(
            action="delegating",
            status=f"Boss is choosing an agent for: {task.title}",
            active_agent={"id": director.id, "name": director.name, "role": "director"},
            active_model=director.model_name,
            active_runtime=runtime.runtime_for(director.model_name),
            active_task={"id": task.id, "title": task.title, "status": task.status},
            boss_summary="Boss summary: analyze task requirements, check idle workers, then assign the best available model.",
            event_type="boss",
            event_title=f"Delegating: {task.title}",
        )
        # Mark director as thinking
        director.status = AgentStatus.thinking.value
        director.last_active = datetime.utcnow()
        await session.flush()

        # Find best available worker using current RAM/VRAM pressure.
        worker_result = await session.execute(
            select(Agent)
            .where(Agent.is_director == False)
            .where(Agent.status == AgentStatus.idle.value)
            .order_by(Agent.tasks_completed.desc())
        )
        workers = list(worker_result.scalars().all())
        worker = self._select_worker_by_memory(workers)

        if not worker:
            await backend_logs.append(
                session,
                source="Бэкенд",
                level="WARNING",
                message=f"No idle Worker available for task {task.title}; Director will handle it as fallback.",
                action_type="assign_worker",
                status="fallback",
                agent_id=director.id,
                task_id=task.id,
            )
            # No worker available — director handles it
            worker = director

        # Assign task
        task.assigned_agent_id = worker.id
        task.status = TaskStatus.in_progress.value
        worker.status = AgentStatus.working.value
        worker.last_active = datetime.utcnow()
        await activity_state.update_and_broadcast(
            action="assigned",
            status=f"Task assigned to {worker.name}",
            active_agent={"id": worker.id, "name": worker.name, "role": "director" if worker.is_director else "worker"},
            active_model=worker.model_name,
            active_runtime=runtime.runtime_for(worker.model_name),
            active_task={"id": task.id, "title": task.title, "status": task.status},
            boss_summary=f"Boss summary: selected {worker.name} based on current RAM/VRAM pressure and model size fit.",
            event_type="worker",
            event_title=f"Assigned to {worker.name}",
        )

        # Run inference asynchronously (don't block the loop)
        self._schedule_agent_task(worker.id, task.id)

    async def _assign_to_any_worker(self, session: AsyncSession, task: Task):
        """Fallback: assign to any idle agent."""
        agent_result = await session.execute(
            select(Agent)
            .where(Agent.status == AgentStatus.idle.value)
            .order_by(Agent.tasks_completed.desc())
        )
        agents = list(agent_result.scalars().all())
        agent = self._select_worker_by_memory(agents)

        if agent:
            task.assigned_agent_id = agent.id
            task.status = TaskStatus.in_progress.value
            agent.status = AgentStatus.working.value
            agent.last_active = datetime.utcnow()
            await activity_state.update_and_broadcast(
                action="assigned",
                status=f"Task assigned to {agent.name}",
                active_agent={"id": agent.id, "name": agent.name, "role": "director" if agent.is_director else "worker"},
                active_model=agent.model_name,
                active_runtime=runtime.runtime_for(agent.model_name),
                active_task={"id": task.id, "title": task.title, "status": task.status},
                boss_summary="Boss summary: no Director delegation was available, so the first idle agent was selected.",
                event_type="worker",
                event_title=f"Assigned to {agent.name}",
            )
            self._schedule_agent_task(agent.id, task.id)

    async def run_isolated_task_session(self, agent_id: str, task_id: str, *, session_id: str = ""):
        """Public Worker Factory entrypoint for one isolated delegated session."""
        await self._run_agent_task(agent_id, task_id, session_id=session_id, isolated=True)

    async def _run_agent_task(self, agent_id: str, task_id: str, *, session_id: str = "", isolated: bool = False):
        """Run an agent's tool-loop on a Board task.

        The Worker (or fallback Director) is given the Worker tool schemas
        gated by its `perm_*` flags and runs an OpenAI-compatible
        chat-with-tools loop until it calls `submit_result`, asks a question,
        runs out of iterations, or the runtime cannot do native tools.

        `isolated=True` is the Claude/Roo-style subagent path: the caller gets
        only task status/result via Board/context summaries, never the worker's
        full scratchpad or code context.
        """
        async with async_session() as session:
            agent_result = await session.execute(
                select(Agent).where(Agent.id == agent_id)
            )
            agent = agent_result.scalar_one_or_none()

            task_result = await session.execute(
                select(Task).where(Task.id == task_id)
            )
            task = task_result.scalar_one_or_none()

            if not agent or not task:
                return

            try:
                await activity_state.update_and_broadcast(
                    action="preparing_prompt",
                    status=f"Preparing prompt for {agent.name}",
                    active_agent={"id": agent.id, "name": agent.name, "role": "director" if agent.is_director else "worker"},
                    active_model=agent.model_name,
                    active_runtime=runtime.runtime_for(agent.model_name),
                    active_task={"id": task.id, "title": task.title, "status": task.status},
                    boss_summary="Boss summary: convert the Board card into a concrete execution prompt with available tool context.",
                    event_type="worker",
                    event_title=f"Preparing: {task.title}",
                )

                if agent.model_provider not in ("ollama", "huggingface"):
                    raise RuntimeError(
                        f"provider '{agent.model_provider}' not supported in worker loop"
                    )

                if isolated:
                    await task_context_service.append(
                        session,
                        task_id=task.id,
                        author_agent_id=agent.id,
                        kind=TaskContextKind.observation.value,
                        content="Isolated Worker session started. Director context boundary is summary-only.",
                        extra={"session_id": session_id, "isolated": True},
                    )

                host = self._resolve_host(agent)
                result_text, asked_question, blocked = await self._run_tool_loop(
                    session=session, agent=agent, task=task, host=host, session_id=session_id
                )

                # Decide final task state.
                if blocked:
                    task.status = TaskStatus.todo.value
                    task.assigned_agent_id = None
                    task.logs = task.logs or []
                    task.logs.append(
                        f"[{datetime.utcnow().isoformat()}] Blocked: {result_text}"
                    )
                    await backend_logs.append(
                        session,
                        source=agent.name or "Воркер",
                        level="ERROR",
                        message=f"Worker blocked and did not complete task: {task.title}",
                        error_details=result_text,
                        action_type="worker_task",
                        status="blocked",
                        agent_id=agent.id,
                        task_id=task.id,
                    )
                    await activity_state.update_and_broadcast(
                        action="blocked",
                        status=f"{agent.name} is blocked: {result_text[:160]}",
                        active_task={"id": task.id, "title": task.title, "status": task.status},
                        boss_summary="Boss summary: Worker could not execute the Board card; inspect Worker audit logs and model/tool support.",
                        event_type="error",
                        event_title=f"Blocked: {task.title}",
                        event_meta={"reason": result_text},
                    )
                elif asked_question:
                    # Worker is blocked — bounce back to todo for Director attention.
                    task.status = TaskStatus.todo.value
                    task.assigned_agent_id = None
                    task.logs = task.logs or []
                    task.logs.append(
                        f"[{datetime.utcnow().isoformat()}] {agent.name} asked the Director and is waiting"
                    )
                    await activity_state.update_and_broadcast(
                        action="awaiting_director",
                        status=f"{agent.name} is blocked and asked the Director",
                        active_task={"id": task.id, "title": task.title, "status": task.status},
                        boss_summary="Boss summary: Worker raised a question; Director must answer before the task can resume.",
                        event_type="task",
                        event_title=f"Question raised: {task.title}",
                    )
                else:
                    await backend_logs.append(
                        session,
                        source=agent.name or "Воркер",
                        level="INFO",
                        message=f"Worker completed task: {task.title}",
                        action_type="worker_task",
                        status="success",
                        agent_id=agent.id,
                        task_id=task.id,
                        payload={"result_preview": (result_text or "")[:1000]},
                    )
                    task.result = result_text or "(no result text)"
                    task.status = TaskStatus.done.value
                    task.logs = task.logs or []
                    task.logs.append(
                        f"[{datetime.utcnow().isoformat()}] Completed by {agent.name}"
                    )
                    await activity_state.update_and_broadcast(
                        action="completed",
                        status=f"Task completed by {agent.name}",
                        active_task={"id": task.id, "title": task.title, "status": task.status},
                        boss_summary="Boss summary: Board card is complete and ready for user review or downstream agents.",
                        event_type="task",
                        event_title=f"Completed: {task.title}",
                    )

                agent.status = AgentStatus.idle.value
                agent.tasks_completed += 1
                agent.last_active = datetime.utcnow()

            except Exception as e:
                await backend_logs.append(
                    session,
                    source=agent.name or "Воркер",
                    level="ERROR",
                    message=f"Worker failed while executing task: {task.title}",
                    error_details=f"{type(e).__name__}: {e}",
                    action_type="worker_task",
                    status="failed",
                    agent_id=agent.id,
                    task_id=task.id,
                )
                task.status = TaskStatus.todo.value
                task.logs = task.logs or []
                task.logs.append(f"[{datetime.utcnow().isoformat()}] Error: {str(e)}")
                agent.status = AgentStatus.error.value
                await activity_state.update_and_broadcast(
                    action="error",
                    status=f"Task failed: {str(e)}",
                    active_task={"id": task.id, "title": task.title, "status": task.status},
                    boss_summary="Boss summary: execution failed, task was returned to backlog, and the error is logged for review.",
                    event_type="error",
                    event_title=f"Failed: {task.title}",
                    event_meta={"error": str(e)},
                )
                print(f"[AgentWorker] Task {task_id} failed: {e}")

            await session.commit()

    async def _run_tool_loop(
        self,
        *,
        session: AsyncSession,
        agent: Agent,
        task: Task,
        host: str,
        session_id: str = "",
    ) -> tuple[str, bool, bool]:
        """Drive the OpenAI-style tool loop for one Worker run.

        Returns `(final_text, asked_question, blocked)`. `final_text` is the assistant's
        last natural-language reply (or the most recent `submit_result` summary
        if available). `blocked=True` means no task-changing work happened and
        the Board card must not be marked done.
        """
        briefing = await task_context_service.worker_briefing(
            session, task=task, worker=agent
        )
        tools = worker_tools.tools_for_worker(agent)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": briefing["system"]},
            {"role": "user", "content": briefing["prompt"]},
        ]

        await worker_audit.append(
            session,
            task_id=task.id,
            agent_id=agent.id,
            session_id=session_id,
            level="INFO",
            event="worker_init",
            message=f"Worker initialized for task '{task.title}' with model {agent.model_name}",
            payload={
                "agent_name": agent.name,
                "model_name": agent.model_name,
                "model_provider": agent.model_provider,
                "host": host,
                "device": agent.device,
                "tool_count": len(tools),
                "tools": [schema.get("function", {}).get("name", "") for schema in tools],
                "system_prompt_chars": len(briefing["system"]),
                "user_prompt_chars": len(briefing["prompt"]),
                "user_prompt_preview": briefing["prompt"][:500],
            },
        )

        await activity_state.update_and_broadcast(
            action="running_inference",
            status=f"{agent.name} is running {agent.model_name}",
            active_agent={"id": agent.id, "name": agent.name, "role": "director" if agent.is_director else "worker"},
            active_model=agent.model_name,
            active_runtime=runtime.runtime_for(agent.model_name),
            active_task={"id": task.id, "title": task.title, "status": task.status},
            boss_summary="Boss summary: model is executing the Board task; monitor runtime, memory pressure, and result quality.",
            event_type="model",
            event_title=f"Inference started: {agent.model_name}",
            event_meta={"host": host, "device": agent.device, "tools": len(tools)},
        )

        final_text = ""
        last_submit_summary: Optional[str] = None
        asked_question = False
        blocked = False
        use_gpu = (agent.device != "cpu")

        for iteration in range(MAX_TOOL_ITERATIONS):
            await worker_audit.append(
                session,
                task_id=task.id,
                agent_id=agent.id,
                session_id=session_id,
                level="INFO",
                event="llm_request",
                message=f"Worker LLM request iteration {iteration + 1}/{MAX_TOOL_ITERATIONS}",
                iteration=iteration + 1,
                payload={
                    "model_name": agent.model_name,
                    "host": host,
                    "device": agent.device,
                    "use_gpu": use_gpu,
                    "message_count": len(messages),
                    "tool_count": len(tools),
                },
            )
            try:
                resp = await runtime.run_chat_tools(
                    model_name=agent.model_name,
                    messages=messages,
                    tools=tools,
                    host=host,
                    device=agent.device or "gpu",
                    use_gpu=use_gpu,
                )
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="INFO",
                    event="llm_response",
                    message=f"Worker LLM response iteration {iteration + 1}: {len(resp.get('tool_calls') or [])} tool call(s)",
                    iteration=iteration + 1,
                    payload={
                        "runtime": resp.get("runtime", "ollama"),
                        "tool_call_supported": resp.get("tool_call_supported", True),
                        "response_preview": (resp.get("response") or "")[:2000],
                        "tool_calls": resp.get("tool_calls") or [],
                    },
                )
            except Exception as exc:  # pragma: no cover — network/runtime errors
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="ERROR",
                    event="provider_error",
                    message=f"Native Worker tool loop provider error: {type(exc).__name__}: {exc}",
                    iteration=iteration + 1,
                    payload={
                        "exception_type": type(exc).__name__,
                        "exception": str(exc),
                        "model_name": agent.model_name,
                        "host": host,
                    },
                )
                await backend_logs.append(
                    session,
                    source=agent.name or "Воркер",
                    level="WARNING",
                    message="Native Worker tool loop failed; falling back to plain inference.",
                    error_details=f"{type(exc).__name__}: {exc}",
                    action_type="worker_tool_loop",
                    status="fallback",
                    agent_id=agent.id,
                    task_id=task.id,
                )
                # Fall back to plain inference so the task is not lost.
                try:
                    fallback = await runtime.run_inference(
                        model_name=agent.model_name,
                        prompt=briefing["prompt"],
                        system=briefing["system"],
                        host=host,
                        device=agent.device or "gpu",
                        use_gpu=use_gpu,
                        stream=False,
                    )
                    final_text = fallback.get("response", "") or f"[runtime error: {exc}]"
                    await worker_audit.append(
                        session,
                        task_id=task.id,
                        agent_id=agent.id,
                        session_id=session_id,
                        level="INFO",
                        event="fallback_response",
                        message="Plain inference fallback returned a response.",
                        iteration=iteration + 1,
                        payload={"response_preview": final_text[:2000], "runtime": fallback.get("runtime", "ollama")},
                    )
                except Exception as fallback_exc:  # noqa: BLE001
                    final_text = f"[runtime error: {type(fallback_exc).__name__}: {fallback_exc}]"
                    await worker_audit.append(
                        session,
                        task_id=task.id,
                        agent_id=agent.id,
                        session_id=session_id,
                        level="ERROR",
                        event="fallback_provider_error",
                        message=f"Plain inference fallback failed: {type(fallback_exc).__name__}: {fallback_exc}",
                        iteration=iteration + 1,
                        payload={"exception_type": type(fallback_exc).__name__, "exception": str(fallback_exc)},
                    )
                break

            assistant_text = (resp.get("response") or "").strip()
            tool_calls = resp.get("tool_calls") or []

            if not resp.get("tool_call_supported", True) and not tool_calls:
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="ERROR",
                    event="native_tools_unsupported",
                    message="Selected provider/model returned no native tool support.",
                    iteration=iteration + 1,
                    payload={
                        "assistant_text": assistant_text,
                        "runtime": resp.get("runtime", "chat-only-no-native-tools"),
                        "provider_error": resp.get("error") or "Native tool calls are unavailable for this model/runtime.",
                        "model_name": agent.model_name,
                        "host": host,
                        "available_tools": [schema.get("function", {}).get("name", "") for schema in tools],
                        "recommendation": "Use a model/runtime that supports OpenAI-compatible tool calls, or delegate to a Worker model with native tool support.",
                    },
                )
                final_text, asked_question, blocked = await self._run_text_tool_loop(
                    session=session,
                    agent=agent,
                    task=task,
                    host=host,
                    briefing=briefing,
                    tools=tools,
                    session_id=session_id,
                    start_iteration=iteration + 1,
                    provider_error=resp.get("error") or "Native tool calls are unavailable for this model/runtime.",
                )
                break

            if not tool_calls:
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="INFO",
                    event="no_tool_calls",
                    message="Worker returned assistant text without tool calls.",
                    iteration=iteration + 1,
                    payload={"assistant_text": assistant_text},
                )
                final_text = assistant_text
                break

            # Record the assistant turn that emitted the calls so the model
            # can see its own intent on the next iteration.
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_text or None,
                    "tool_calls": tool_calls,
                }
            )

            for raw_call in tool_calls:
                call_name = self._tool_name(raw_call)
                call_args = self._tool_args(raw_call)
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="INFO",
                    event="tool_call",
                    message=f"Worker calls tool {call_name}",
                    iteration=iteration + 1,
                    tool_name=call_name,
                    tool_call_id=str(raw_call.get("id") or raw_call.get("tool_call_id") or ""),
                    payload={"arguments": call_args},
                )
            results = await worker_tools.execute_worker_tool_calls(
                db=session, task=task, worker=agent, tool_calls=tool_calls
            )
            messages.extend(worker_tools.to_tool_messages(results))

            for r in results:
                parsed_result = self._parse_tool_result(r.content)
                ok = bool(parsed_result.get("ok", False)) if isinstance(parsed_result, dict) else False
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="INFO" if ok else "ERROR",
                    event="tool_result" if ok else "tool_error",
                    message=f"Tool {r.name} returned {'ok' if ok else 'error'}",
                    iteration=iteration + 1,
                    tool_name=r.name,
                    tool_call_id=r.tool_call_id,
                    payload={"arguments": r.args or {}, "result": parsed_result if parsed_result else r.content},
                )
                if r.submitted:
                    # Pull the summary back out of the JSON payload.
                    try:
                        import json as _json
                        parsed = _json.loads(r.content)
                        if isinstance(parsed, dict) and parsed.get("ok"):
                            last_submit_summary = str(parsed.get("summary") or "")
                    except (ValueError, TypeError):
                        pass
                if r.asked:
                    asked_question = True

            if last_submit_summary is not None:
                if any(not (self._parse_tool_result(item.content).get("ok", False)) for item in results):
                    last_error = next(
                        (self._parse_tool_result(item.content).get("error", item.content) for item in reversed(results) if not self._parse_tool_result(item.content).get("ok", False)),
                        "tool error before submit_result",
                    )
                    final_text = f"[blocked: submit_result after tool error] {last_error}"
                    blocked = True
                else:
                    final_text = last_submit_summary or assistant_text
                break
            if asked_question:
                final_text = assistant_text
                break

        else:
            await worker_audit.append(
                session,
                task_id=task.id,
                agent_id=agent.id,
                session_id=session_id,
                level="ERROR",
                event="iteration_cap",
                message=f"Worker tool loop reached {MAX_TOOL_ITERATIONS}-iteration cap without submit_result.",
                payload={"max_iterations": MAX_TOOL_ITERATIONS},
            )
            await backend_logs.append(
                session,
                source=agent.name or "Воркер",
                level="WARNING",
                message=f"Worker tool loop reached iteration cap for task: {task.title}",
                action_type="worker_tool_loop",
                status="iteration_cap",
                agent_id=agent.id,
                task_id=task.id,
            )
            # Loop exhausted without a submit_result — log a timeout note.
            await task_context_service.append(
                session,
                task_id=task.id,
                author_agent_id=agent.id,
                kind=TaskContextKind.observation.value,
                content=f"Tool loop reached the {MAX_TOOL_ITERATIONS}-iteration cap without submit_result.",
            )
            final_text = final_text or "(tool loop iteration cap reached)"

        await worker_audit.append(
            session,
            task_id=task.id,
            agent_id=agent.id,
            session_id=session_id,
            level="INFO",
            event="worker_finish",
            message=f"Worker tool loop finished for task '{task.title}'",
            payload={"final_text": final_text, "asked_question": asked_question, "blocked": blocked},
        )

        await activity_state.update_and_broadcast(
            action="inference_complete",
            status=f"{agent.name} finished model output",
            active_runtime=runtime.runtime_for(agent.model_name),
            boss_summary="Boss summary: model output returned; save result to the Board and free the agent for more work.",
            event_type="model",
            event_title=f"Inference completed: {agent.model_name}",
        )
        return final_text, asked_question, blocked

    async def _run_text_tool_loop(
        self,
        *,
        session: AsyncSession,
        agent: Agent,
        task: Task,
        host: str,
        briefing: dict[str, str],
        tools: list[dict[str, Any]],
        session_id: str,
        start_iteration: int,
        provider_error: str = "",
    ) -> tuple[str, bool, bool]:
        """Universal text-based tool protocol for models without native tools.

        The model emits plain JSON such as
        `{\"tool\": \"write_file\", \"arguments\": {...}}`; the backend
        converts it into the same safe Worker tool executor path used by native
        tool calls. This makes local models useful even when their runtime/chat
        template cannot produce OpenAI-compatible tool calls.
        """
        allowed_tool_names = [schema.get("function", {}).get("name", "") for schema in tools]
        tool_docs = []
        for schema in tools:
            fn = schema.get("function", {}) if isinstance(schema, dict) else {}
            name = str(fn.get("name") or "")
            desc = str(fn.get("description") or "")
            params = fn.get("parameters") or {}
            tool_docs.append(f"- {name}: {desc}\n  schema: {json.dumps(params, ensure_ascii=False)}")

        protocol_system = (
            f"{briefing['system']}\n\n"
            "NATIVE TOOL CALLING IS NOT AVAILABLE FOR THIS MODEL.\n"
            "You must use the backend text-tool protocol.\n"
            "CRITICAL: Execute ONLY ONE tool per response. Do not output multiple JSON blocks. "
            "Wait for the tool execution result before deciding the next step. "
            "For deleting files, prefer delete_file with the exact absolute path from the task context; do not use shell commands. "
            "Never call submit_result after any tool error; first fix the error with another tool call. "
            "If you need to create a file inside a new folder, you MUST use run_command (mkdir) "
            "or a folder creation tool FIRST, wait for success, and only then use write_file.\n"
            "Respond with exactly one JSON object and no Markdown.\n"
            "Allowed formats:\n"
            "{\"tool\":\"read_file\",\"arguments\":{\"path\":\"...\"}}\n"
            "{\"tool\":\"write_file\",\"arguments\":{\"path\":\"...\",\"content\":\"...\"}}\n"
            "{\"tool\":\"delete_file\",\"arguments\":{\"path\":\"...\"}}\n"
            "{\"tool\":\"run_command\",\"arguments\":{\"command\":\"...\",\"cwd\":\"...\",\"timeout_seconds\":60}}\n"
            "{\"tool\":\"submit_result\",\"arguments\":{\"summary\":\"...\"}}\n"
            "Never describe what you would do. Call a tool with JSON. "
            "After each tool result, continue with the next JSON tool call until finished."
        )
        transcript = briefing["prompt"]
        blocked = False
        asked_question = False
        final_text = ""
        parse_failures = 0
        prior_tool_error = ""
        use_gpu = (agent.device != "cpu")

        await worker_audit.append(
            session,
            task_id=task.id,
            agent_id=agent.id,
            session_id=session_id,
            level="INFO",
            event="text_tool_fallback_start",
            message="Native tools unavailable; switching Worker to universal text-tool protocol.",
            iteration=start_iteration,
            payload={
                "provider_error": provider_error,
                "model_name": agent.model_name,
                "host": host,
                "available_tools": allowed_tool_names,
                "tool_protocol": "json_text_tool_call_v1",
            },
        )

        for offset in range(MAX_TOOL_ITERATIONS):
            iteration = start_iteration + offset
            prompt = (
                f"{transcript}\n\n"
                "Available backend tools:\n"
                + "\n".join(tool_docs)
                + "\n\nCRITICAL: return exactly one JSON tool call now. Do not output multiple JSON blocks. "
                "Wait for the backend result before choosing the next tool. If this is a follow-up like 'delete the file you just created', "
                "use the exact path from Recent completed Board results. Prefer delete_file for file deletion. "
                "Never call submit_result after a tool error; fix the error first. If writing inside a new folder, "
                "create the folder first with run_command/mkdir, wait for success, then call write_file."
            )
            await worker_audit.append(
                session,
                task_id=task.id,
                agent_id=agent.id,
                session_id=session_id,
                level="INFO",
                event="text_tool_llm_request",
                message=f"Text-tool fallback LLM request iteration {iteration}",
                iteration=iteration,
                payload={"model_name": agent.model_name, "host": host, "prompt_chars": len(prompt)},
            )
            try:
                resp = await runtime.run_inference(
                    model_name=agent.model_name,
                    prompt=prompt,
                    system=protocol_system,
                    host=host,
                    device=agent.device or "gpu",
                    use_gpu=use_gpu,
                    stream=False,
                )
            except Exception as exc:  # noqa: BLE001
                final_text = f"[blocked: text-tool provider error] {type(exc).__name__}: {exc}"
                blocked = True
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="ERROR",
                    event="text_tool_provider_error",
                    message=f"Text-tool fallback provider error: {type(exc).__name__}: {exc}",
                    iteration=iteration,
                    payload={"exception_type": type(exc).__name__, "exception": str(exc)},
                )
                break

            assistant_text = str(resp.get("response") or "").strip()
            await worker_audit.append(
                session,
                task_id=task.id,
                agent_id=agent.id,
                session_id=session_id,
                level="INFO",
                event="text_tool_llm_response",
                message=f"Text-tool fallback response iteration {iteration}",
                iteration=iteration,
                payload={"response_preview": assistant_text[:4000], "runtime": resp.get("runtime", "ollama")},
            )

            parsed_calls = self._parse_text_tool_calls(assistant_text)
            if not parsed_calls:
                parse_failures += 1
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="ERROR",
                    event="text_tool_parse_error",
                    message="Model did not return a valid JSON text-tool call.",
                    iteration=iteration,
                    payload={"parse_failures": parse_failures, "raw_response": assistant_text[:4000]},
                )
                transcript += (
                    "\n\nBackend parser error: your previous response was not a valid JSON tool call. "
                    "Return exactly one valid JSON object and no Markdown. "
                    "Use one of the available backend tools. Do not ask for Desktop/Home paths: they are already provided "
                    "in the Runtime operating system context above as exact Home= and Desktop= values. "
                    "Never output placeholder paths containing <user> or vague names like <last created file>. "
                    "For follow-up deletion, copy the exact path from Recent completed Board results. "
                    "Example delete shape: {\"tool\":\"delete_file\",\"arguments\":{\"path\":\"EXACT_DESKTOP_PATH\\\\hello dimap.txt\"}}."
                )
                if parse_failures >= 3:
                    final_text = "[blocked: text-tool parse failure] Model failed to produce valid JSON tool calls after 3 attempts."
                    blocked = True
                    break
                continue

            parse_failures = 0
            for call in parsed_calls:
                call_name = self._tool_name(call)
                call_args = self._tool_args(call)
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="INFO",
                    event="text_tool_call",
                    message=f"Text-tool fallback calls tool {call_name}",
                    iteration=iteration,
                    tool_name=call_name,
                    tool_call_id=str(call.get("id") or call.get("tool_call_id") or ""),
                    payload={"arguments": call_args},
                )

            results = await worker_tools.execute_worker_tool_calls(
                db=session, task=task, worker=agent, tool_calls=parsed_calls
            )
            result_lines: list[str] = []
            had_tool_error = bool(prior_tool_error)
            last_tool_error = prior_tool_error
            successful_terminal_actions: list[tuple[str, dict[str, Any]]] = []
            for r in results:
                parsed_result = self._parse_tool_result(r.content)
                ok = bool(parsed_result.get("ok", False)) if isinstance(parsed_result, dict) else False
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="INFO" if ok else "ERROR",
                    event="tool_result" if ok else "tool_error",
                    message=f"Tool {r.name} returned {'ok' if ok else 'error'}",
                    iteration=iteration,
                    tool_name=r.name,
                    tool_call_id=r.tool_call_id,
                    payload={"arguments": r.args or {}, "result": parsed_result if parsed_result else r.content},
                )
                result_lines.append(f"Tool {r.name} result: {r.content}")
                if ok and r.name in {"write_file", "delete_file"} and isinstance(parsed_result, dict):
                    successful_terminal_actions.append((r.name, parsed_result))
                if not ok:
                    had_tool_error = True
                    last_tool_error = str(parsed_result.get("error") if isinstance(parsed_result, dict) else r.content)
                    prior_tool_error = last_tool_error
                if r.submitted:
                    if had_tool_error:
                        final_text = f"[blocked: submit_result after tool error] {last_tool_error or 'tool error before submit_result'}"
                        return final_text, asked_question, True
                    summary = parsed_result.get("summary") if isinstance(parsed_result, dict) else ""
                    final_text = str(summary or parsed_result.get("message") or r.content)
                    return final_text, asked_question, False
                if r.asked:
                    asked_question = True
                    final_text = str(parsed_result.get("question") if isinstance(parsed_result, dict) else r.content)
                    return final_text, True, False

            if successful_terminal_actions and not had_tool_error:
                action_name, action_result = successful_terminal_actions[-1]
                target_path = str(action_result.get("path") or "").strip()
                if action_name == "write_file":
                    bytes_written = action_result.get("bytes_written")
                    detail = f" ({bytes_written} bytes)" if bytes_written is not None else ""
                    final_text = f"Created/updated file successfully: {target_path}{detail}" if target_path else "File was written successfully."
                elif action_name == "delete_file":
                    final_text = f"Deleted file successfully: {target_path}" if target_path else "File was deleted successfully."
                await worker_audit.append(
                    session,
                    task_id=task.id,
                    agent_id=agent.id,
                    session_id=session_id,
                    level="INFO",
                    event="text_tool_auto_submit",
                    message="Backend auto-completed text-tool task after a successful terminal file action.",
                    iteration=iteration,
                    payload={"tool": action_name, "result": action_result, "summary": final_text},
                )
                return final_text, asked_question, False

            transcript += "\n\n" + assistant_text + "\n" + "\n".join(result_lines)

        if not final_text:
            final_text = "[blocked: text-tool iteration cap] Text-tool fallback reached iteration cap without submit_result."
            blocked = True
        return final_text, asked_question, blocked

    @staticmethod
    def _parse_text_tool_calls(text: str) -> list[dict[str, Any]]:
        payloads = AgentWorker._extract_json_payloads(text)
        calls: list[dict[str, Any]] = []
        for parsed in payloads:
            raw_items = parsed if isinstance(parsed, list) else [parsed]
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("tool") or item.get("name") or "").strip()
                args = item.get("arguments") or item.get("args") or {}
                if not isinstance(args, dict):
                    args = {}
                if not name:
                    continue
                calls.append(
                    {
                        "id": str(item.get("id") or f"text-tool-{uuid.uuid4()}"),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                    }
                )
        return calls

    @staticmethod
    def _extract_json_payloads(text: str) -> list[Any]:
        """Extract every valid JSON object/array from a text-tool response.

        Fallback models sometimes emit several JSON blocks in one answer, e.g.
        `write_file` followed by `submit_result`. The backend still discourages
        that in the prompt, but the parser must be robust and execute all valid
        tool blocks in order so terminal calls like `submit_result` are not lost.
        """
        raw = (text or "").strip()
        if not raw:
            return []

        try:
            return [json.loads(raw)]
        except json.JSONDecodeError:
            pass

        candidates: list[tuple[int, int, str]] = []
        seen_spans: set[tuple[int, int]] = set()

        for fenced in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE):
            body = fenced.group(1).strip()
            candidates.append((fenced.start(1), fenced.end(1), body))
            seen_spans.add((fenced.start(1), fenced.end(1)))

        for start, end in AgentWorker._json_spans(raw):
            if any(start >= seen_start and end <= seen_end for seen_start, seen_end in seen_spans):
                continue
            candidates.append((start, end, raw[start:end]))

        payloads: list[Any] = []
        for _start, _end, candidate in sorted(candidates, key=lambda item: item[0]):
            try:
                payloads.append(json.loads(candidate.strip()))
            except (json.JSONDecodeError, TypeError):
                continue

        return payloads

    @staticmethod
    def _extract_json_payload(text: str) -> Any:
        payloads = AgentWorker._extract_json_payloads(text)
        return payloads[0] if payloads else None

    @staticmethod
    def _json_spans(raw: str) -> list[tuple[int, int]]:
        """Return balanced JSON object/array spans, preserving source order."""
        spans: list[tuple[int, int]] = []
        stack: list[str] = []
        start: Optional[int] = None
        in_string = False
        escape = False

        for idx, ch in enumerate(raw):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue
            if ch in "[{":
                if not stack:
                    start = idx
                stack.append("}" if ch == "{" else "]")
                continue
            if ch in "}]" and stack:
                expected = stack.pop()
                if ch != expected:
                    stack.clear()
                    start = None
                    continue
                if not stack and start is not None:
                    spans.append((start, idx + 1))
                    start = None

        return spans

    @staticmethod
    def _tool_name(call: dict[str, Any]) -> str:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(call.get("name") or "") if isinstance(call, dict) else ""

    @staticmethod
    def _tool_args(call: dict[str, Any]) -> dict[str, Any]:
        import json as _json

        raw = None
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict):
            raw = fn.get("arguments")
        elif isinstance(call, dict):
            raw = call.get("arguments")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = _json.loads(raw or "{}")
                return parsed if isinstance(parsed, dict) else {}
            except _json.JSONDecodeError:
                return {"_raw": raw}
        return {}

    @staticmethod
    def _parse_tool_result(content: str) -> dict[str, Any]:
        import json as _json

        try:
            parsed = _json.loads(content or "{}")
            return parsed if isinstance(parsed, dict) else {"raw": parsed}
        except (_json.JSONDecodeError, TypeError):
            return {"raw": content}

    def _select_worker_by_memory(self, agents: list[Agent]) -> Optional[Agent]:
        """Pick an idle agent whose model size best fits current free RAM/VRAM.

        If memory is tight, prefer small models (e.g. 0.5B/1B/3B). If there is
        plenty of memory, allow larger models. This prevents the dispatcher from
        blindly selecting the first idle agent.
        """

        if not agents:
            return None

        ram = resource_monitor.get_ram_info()
        vram = resource_monitor.get_vram_info()
        free_ram = float(ram.get("available_gb") or 0.0)
        free_vram = float(vram.get("free_gb") or 0.0) if vram else 0.0

        def score(agent: Agent) -> tuple[int, float, int]:
            estimate = self._estimate_model_gb(agent.model_name)
            free = free_vram if agent.device == "gpu" and free_vram > 0 else free_ram
            fits = estimate <= max(0.1, free - 0.5)
            # Tight memory: smallest fitting model wins. Plenty memory: largest fitting model wins.
            tight = free < 4.0 or (agent.device == "gpu" and free_vram and free_vram < 4.0)
            fit_rank = 0 if fits else 1
            size_rank = estimate if tight else -estimate
            completed_rank = -int(agent.tasks_completed or 0)
            return (fit_rank, size_rank, completed_rank)

        selected = sorted(agents, key=score)[0]
        return selected

    @staticmethod
    def _estimate_model_gb(model_name: str) -> float:
        """Rough local-model memory estimate from common model id patterns."""

        text = (model_name or "").lower()
        match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", text)
        params_b = float(match.group(1)) if match else 3.0
        quant_factor = 0.65
        if any(q in text for q in ("q2", "iq2")):
            quant_factor = 0.35
        elif any(q in text for q in ("q3", "iq3")):
            quant_factor = 0.45
        elif any(q in text for q in ("q4", "iq4", "int4")):
            quant_factor = 0.60
        elif any(q in text for q in ("q5", "iq5")):
            quant_factor = 0.75
        elif any(q in text for q in ("q8", "int8")):
            quant_factor = 1.05
        elif any(q in text for q in ("fp16", "f16")):
            quant_factor = 2.10
        return max(0.35, round(params_b * quant_factor + 0.4, 2))

    def _build_system_prompt(self, agent: Agent) -> str:
        """Build system prompt from agent profile."""
        base = f"You are {agent.name}, an AI agent. {agent.description or ''}"
        if agent.mcp_tools:
            base += f"\nYour available tools: {', '.join(agent.mcp_tools)}."
        if agent.is_director:
            base += "\nYou are the Director. You coordinate the team and delegate tasks."
        return base

    def _build_task_prompt(self, task: Task) -> str:
        """Build prompt for a task."""
        return f"Task: {task.title}\n\nDescription:\n{task.description}\n\nPlease complete this task and provide a detailed response."

    def status_snapshot(self) -> dict:
        """Return current worker and dispatcher activity for the API/UI."""
        snapshot = activity_state.snapshot()
        snapshot["running"] = self._running
        snapshot["poll_interval"] = self.poll_interval
        return snapshot

    def _resolve_host(self, agent: Agent) -> str:
        """Resolve Ollama host based on agent device preference."""
        if agent.device == "gpu":
            return runtime.ollama.gpu_host
        elif agent.device == "cpu":
            return runtime.ollama.cpu_host
        else:
            # auto: use GPU if available, else CPU
            return runtime.ollama.gpu_host


# Singleton instance
agent_worker = AgentWorker()
