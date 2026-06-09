"""Per-task / per-subtask context routing service.

Stores temporary context entries that the Director and Workers exchange
while executing a Board task. Each entry carries:
  * `task_id`         — which Board task / subtask it belongs to
  * `author_agent_id` — who wrote it (Worker or Director)
  * `kind`            — note | plan | result | question | observation | director
  * `content`         — text payload

The Director reads compact summaries to adjust plans without overflowing
its chat context. Workers append their progress so the Director sees
intermediate state.

Public API:
  * append(...)        — write a new context entry
  * for_task(...)      — read entries for one task in chronological order
  * thread(...)        — task entries + parent task entries (for subtasks)
  * worker_briefing(...) — build a system+prompt pair for a Worker run
  * director_summary(...) — compact text snapshot the Director can read
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Task, TaskContext, TaskContextKind, TaskStatus
from app.services import agent_os


_MAX_TEXT = 2000  # per-entry hard cap to keep prompts bounded


def _clip(text: str, limit: int = _MAX_TEXT) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _normalize_kind(kind: str) -> str:
    try:
        return TaskContextKind(kind).value
    except ValueError:
        return TaskContextKind.note.value


async def append(
    db: AsyncSession,
    *,
    task_id: str,
    content: str,
    kind: str = TaskContextKind.note.value,
    author_agent_id: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> TaskContext:
    """Create a new context entry for `task_id`."""
    entry = TaskContext(
        task_id=task_id,
        author_agent_id=author_agent_id,
        kind=_normalize_kind(kind),
        content=_clip(content),
        extra=extra or {},
    )
    db.add(entry)
    await db.flush()
    return entry


async def for_task(
    db: AsyncSession, task_id: str, *, limit: int = 50
) -> list[TaskContext]:
    """Return the most recent `limit` entries for a task, oldest-first."""
    rows = (
        await db.execute(
            select(TaskContext)
            .where(TaskContext.task_id == task_id)
            .order_by(TaskContext.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


async def thread(
    db: AsyncSession, task: Task, *, limit_per_task: int = 30
) -> dict[str, list[TaskContext]]:
    """Return the context thread relevant to `task`.

    Returns `{"parent": [...], "self": [...]}` so callers can render
    parent context first when building Worker prompts.
    """
    own = await for_task(db, task.id, limit=limit_per_task)
    parent: list[TaskContext] = []
    if task.parent_task_id:
        parent = await for_task(db, task.parent_task_id, limit=limit_per_task)
    return {"parent": parent, "self": own}


def _format_entry(entry: TaskContext) -> str:
    when = entry.created_at.strftime("%Y-%m-%d %H:%M") if entry.created_at else "?"
    label = entry.kind.upper()
    return f"[{when} | {label}] {entry.content}"


def render(entries: Iterable[TaskContext]) -> str:
    return "\n".join(_format_entry(e) for e in entries)


async def recent_completed_task_context(
    db: AsyncSession, *, current_task: Task, worker: Agent, limit: int = 6
) -> str:
    """Recent completed Board results so a Worker can resolve follow-up requests."""
    rows = (
        await db.execute(
            select(Task)
            .where(Task.id != current_task.id)
            .where(Task.status == TaskStatus.done.value)
            .order_by(Task.updated_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    if not rows:
        return ""

    lines: list[str] = []
    for previous in rows:
        assignee_note = "same worker" if previous.assigned_agent_id == worker.id else "other worker"
        result = _clip(previous.result or "", 700)
        description = _clip(previous.description or "", 300)
        lines.append(
            f"- [{previous.updated_at:%Y-%m-%d %H:%M} | {assignee_note}] "
            f"{previous.title}: {description}\n  Result: {result or '(no result text)'}"
        )
    return "\n".join(lines)


async def director_summary(
    db: AsyncSession, *, task_id: str, limit: int = 20
) -> str:
    """Compact text snapshot suitable for inclusion in Director prompts."""
    entries = await for_task(db, task_id, limit=limit)
    if not entries:
        return f"(no context yet for task {task_id})"
    return render(entries)


async def worker_briefing(
    db: AsyncSession, *, task: Task, worker: Agent
) -> dict[str, str]:
    """Build a `{system, prompt}` pair the Worker will execute on.

    The system block declares the Worker's permission set so it can
    self-restrict. The prompt block carries the (sub)task description plus
    the relevant context thread.
    """
    perms = []
    for label, attr in (
        ("read", "perm_read"),
        ("write", "perm_write"),
        ("execute", "perm_execute"),
        ("mcp", "perm_mcp"),
        ("mode", "perm_mode"),
        ("subtasks", "perm_subtasks"),
        ("question", "perm_question"),
    ):
        if getattr(worker, attr, False):
            perms.append(label)
    perm_line = ", ".join(perms) if perms else "none"

    os_context = await agent_os.agent_os_prompt_context()

    system_lines = [
        f"You are {worker.name}, a Worker agent in lmWorld.",
        os_context,
        f"Allowed tools: {perm_line}.",
        "Stay strictly within your permission set; if a step requires a "
        "permission you don't have, log a `question` context entry instead "
        "of attempting it.",
        "Append a `result` context entry when you finish.",
    ]

    th = await thread(db, task)
    parent_block = render(th["parent"])
    own_block = render(th["self"])
    recent_done_block = await recent_completed_task_context(db, current_task=task, worker=worker)
    parent_section = (
        f"\nParent task context:\n{parent_block}\n" if parent_block else ""
    )
    own_section = (
        f"\nThis task's prior context:\n{own_block}\n" if own_block else ""
    )
    recent_done_section = (
        "\nRecent completed Board results (use this to resolve phrases like "
        f"'the file you just created'):\n{recent_done_block}\n"
        if recent_done_block
        else ""
    )

    prompt = (
        f"Task: {task.title}\n"
        f"Description: {task.description or '(no description)'}\n"
        f"{parent_section}{own_section}{recent_done_section}\n"
        "If this task refers to something created/changed earlier, use the exact path/details "
        "from Recent completed Board results instead of guessing. "
        "Produce a concise plan, then execute it within your permissions. "
        "End with a single `result` context entry summarizing the outcome."
    )

    return {"system": "\n".join(system_lines), "prompt": prompt}


def has_permission(worker: Agent, permission: str) -> bool:
    """Boolean check for a Worker permission by short name."""
    attr = f"perm_{permission}"
    return bool(getattr(worker, attr, False))


__all__ = [
    "append",
    "for_task",
    "thread",
    "render",
    "director_summary",
    "recent_completed_task_context",
    "worker_briefing",
    "has_permission",
]
