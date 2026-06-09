"""Operational log APIs.

Frontend/Tauri can poll these endpoints to render live Director/Worker/backend
activity as a chat-like stream, plus detailed Worker audit events for a Board
`task_id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import BackendLogEntry
from app.services import worker_audit

router = APIRouter(prefix="/api/logs", tags=["logs"])


class WorkerLogResponse(BaseModel):
    task_id: str
    session_id: str = ""
    events: list[dict[str, Any]]


class OpsLogEntry(BaseModel):
    id: str
    timestamp: str
    source: str
    level: str
    message: str
    error_details: str = ""
    action_type: str = ""
    status: str = ""
    correlation_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class OpsLogResponse(BaseModel):
    entries: list[OpsLogEntry]


def _ts(value: datetime | None) -> str:
    if not value:
        return ""
    return value.isoformat() + "Z"


def _serialize_log(entry: BackendLogEntry) -> OpsLogEntry:
    return OpsLogEntry(
        id=entry.id,
        timestamp=_ts(entry.timestamp),
        source=entry.source,
        level=entry.level,
        message=entry.message,
        error_details=entry.error_details or "",
        action_type=entry.action_type or "",
        status=entry.status or "",
        correlation_id=entry.correlation_id,
        agent_id=entry.agent_id,
        task_id=entry.task_id,
        payload=entry.payload if isinstance(entry.payload, dict) else {},
    )


@router.get("/ops", response_model=OpsLogResponse)
async def ops_logs(
    limit: int = Query(default=300, ge=1, le=1000),
    level: Optional[str] = Query(default=None, description="Optional level filter: INFO, WARNING, ERROR."),
    source: Optional[str] = Query(default=None, description="Optional exact source filter."),
    q: Optional[str] = Query(default=None, description="Optional case-insensitive text search."),
    db: AsyncSession = Depends(get_db),
):
    """Return recent Director/Worker/backend operational logs newest-last."""
    stmt = select(BackendLogEntry).order_by(BackendLogEntry.timestamp.desc()).limit(limit)
    if level:
        stmt = stmt.where(BackendLogEntry.level == level.upper())
    if source:
        stmt = stmt.where(BackendLogEntry.source == source)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.reverse()

    query = (q or "").strip().lower()
    if query:
        rows = [
            row
            for row in rows
            if query in (row.message or "").lower()
            or query in (row.error_details or "").lower()
            or query in (row.action_type or "").lower()
            or query in (row.source or "").lower()
        ]

    return OpsLogResponse(entries=[_serialize_log(row) for row in rows])


@router.get("/worker/{task_id}", response_model=WorkerLogResponse)
async def worker_logs(
    task_id: str,
    session_id: Optional[str] = Query(default="", description="Optional isolated Worker session id."),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """Return structured Worker audit events for one Board task."""
    events = await worker_audit.recent_for_task(db, task_id=task_id, session_id=session_id or "", limit=limit)
    return WorkerLogResponse(task_id=task_id, session_id=session_id or "", events=events)
