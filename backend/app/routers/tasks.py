"""Task CRUD endpoints."""

from datetime import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Task
from app.schemas import TaskCreate, TaskResponse, TaskUpdate

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _title_key(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").lower()).strip()


async def _unique_task_title(db: AsyncSession, requested_title: str) -> str:
    """Return a non-conflicting Board title; never reject duplicates at API edge."""
    base = re.sub(r"\s+", " ", requested_title or "").strip()[:160] or "Board task"
    result = await db.execute(select(Task.title))
    existing = {_title_key(title) for title in result.scalars().all()}
    if _title_key(base) not in existing:
        return base

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    candidate = f"{base} [{stamp}]"[:200]
    if _title_key(candidate) not in existing:
        return candidate

    suffix = 2
    while True:
        tail = f" [{stamp}-{suffix}]"
        candidate = f"{base[:200 - len(tail)]}{tail}"
        if _title_key(candidate) not in existing:
            return candidate
        suffix += 1


@router.get("", response_model=list[TaskResponse])
async def list_tasks(db: AsyncSession = Depends(get_db)):
    """Get all tasks."""
    result = await db.execute(select(Task))
    tasks = result.scalars().all()
    return tasks


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(task_data: TaskCreate, db: AsyncSession = Depends(get_db)):
    """Create a new task idempotently; duplicate titles get a backend suffix."""
    task = Task(
        title=await _unique_task_title(db, task_data.title),
        description=task_data.description,
        assigned_agent_id=task_data.assigned_agent_id,
    )
    db.add(task)
    await db.flush()
    await db.refresh(task)
    return task


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    """Get task by ID."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: str, task_data: TaskUpdate, db: AsyncSession = Depends(get_db)
):
    """Update task status or assignment."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = task_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value if not hasattr(value, "value") else value.value)

    task.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(task)
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a task."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.delete(task)
