"""Per-task context endpoints for Workers and Director.

Workers append progress entries while executing a Board task; the Director
reads back compact summaries to adjust the plan or create follow-up
subtasks. This is the persistence layer behind context routing.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Task, TaskContextKind
from app.services import task_context as ctx


router = APIRouter(prefix="/api/tasks", tags=["task-context"])


class TaskContextEntryResponse(BaseModel):
    id: str
    task_id: str
    author_agent_id: Optional[str]
    kind: str
    content: str
    extra: dict
    created_at: Optional[str]
    updated_at: Optional[str]

    @classmethod
    def from_model(cls, entry) -> "TaskContextEntryResponse":
        return cls(
            id=entry.id,
            task_id=entry.task_id,
            author_agent_id=entry.author_agent_id,
            kind=entry.kind,
            content=entry.content,
            extra=entry.extra or {},
            created_at=entry.created_at.isoformat() if entry.created_at else None,
            updated_at=entry.updated_at.isoformat() if entry.updated_at else None,
        )


class TaskContextAppendRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)
    kind: str = Field(TaskContextKind.note.value)
    author_agent_id: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


@router.get("/{task_id}/context", response_model=list[TaskContextEntryResponse])
async def list_task_context(
    task_id: str,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Return context entries for a Board task in chronological order."""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    entries = await ctx.for_task(db, task_id, limit=limit)
    return [TaskContextEntryResponse.from_model(e) for e in entries]


@router.post(
    "/{task_id}/context",
    response_model=TaskContextEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def append_task_context(
    task_id: str,
    body: TaskContextAppendRequest,
    db: AsyncSession = Depends(get_db),
):
    """Worker- or Director-side append of a context entry."""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    entry = await ctx.append(
        db,
        task_id=task_id,
        content=body.content,
        kind=body.kind,
        author_agent_id=body.author_agent_id,
        extra=body.extra,
    )
    await db.commit()
    await db.refresh(entry)
    return TaskContextEntryResponse.from_model(entry)


@router.get("/{task_id}/context/summary")
async def get_task_context_summary(
    task_id: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Compact text summary suitable for the Director's prompt context."""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    text = await ctx.director_summary(db, task_id=task_id, limit=limit)
    return {"task_id": task_id, "summary": text}
