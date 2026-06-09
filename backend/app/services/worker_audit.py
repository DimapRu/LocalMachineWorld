"""Structured Worker execution audit logs.

The Worker audit stream is intentionally separate from normal backend logs:
it captures every LLM iteration, provider failure, tool call, tool result, and
OS/tool error in a frontend-friendly JSONL format while also mirroring entries
to the existing backend_logs table for persistence/querying.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BackendLogEntry
from app.services import backend_logs

LOG_DIR = Path(__file__).resolve().parents[3] / "logs"
LOG_FILE = LOG_DIR / "worker_execution.log"
MAX_FIELD = 4000
MAX_LIST_ITEMS = 25


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _clip(value: Any, limit: int = MAX_FIELD) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        clipped: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text == "entries" and isinstance(item, list):
                clipped[key_text] = [_clip(entry, max(500, limit // 4)) for entry in item[:MAX_LIST_ITEMS]]
                if len(item) > MAX_LIST_ITEMS:
                    clipped["entries_omitted"] = len(item) - MAX_LIST_ITEMS
                continue
            clipped[key_text] = _clip(item, max(500, limit // 2))
        return clipped
    if isinstance(value, list):
        clipped_items = [_clip(item, max(500, limit // 2)) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            clipped_items.append({"omitted_items": len(value) - MAX_LIST_ITEMS})
        return clipped_items
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class WorkerAuditEvent:
    """Frontend-friendly worker execution log event."""

    timestamp: str
    level: str
    event: str
    message: str
    task_id: str
    agent_id: Optional[str] = None
    session_id: str = ""
    iteration: Optional[int] = None
    tool_name: str = ""
    tool_call_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["payload"] = _clip(data.get("payload") or {})
        return data


async def append(
    db: AsyncSession,
    *,
    task_id: str,
    level: str,
    event: str,
    message: str,
    agent_id: Optional[str] = None,
    session_id: str = "",
    iteration: Optional[int] = None,
    tool_name: str = "",
    tool_call_id: str = "",
    payload: Optional[dict[str, Any]] = None,
) -> WorkerAuditEvent:
    """Append one Worker audit event to JSONL and backend_logs.

    Worker LLM/tool loops can spend many seconds waiting for model output. If
    audit rows are only flushed and the surrounding transaction stays open for
    the whole loop, SQLite keeps a writer lock and concurrent chat requests can
    fail with ``database is locked``. Commit audit writes immediately when the
    caller supplied a session; this keeps Agent Logs live without holding a
    long-running write transaction across model inference.
    """
    item = WorkerAuditEvent(
        timestamp=_now(),
        level=(level or "INFO").upper(),
        event=event,
        message=message,
        task_id=task_id,
        agent_id=agent_id,
        session_id=session_id,
        iteration=iteration,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        payload=_clip(payload or {}) or {},
    )
    serialized = item.to_json()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(serialized, ensure_ascii=False) + "\n")

    await backend_logs.append(
        db,
        source="WorkerAudit",
        level=item.level,
        message=message,
        action_type=f"worker_audit.{event}",
        status="failed" if item.level == "ERROR" else "success",
        correlation_id=session_id or None,
        agent_id=agent_id,
        task_id=task_id,
        payload=serialized,
    )
    await db.commit()
    return item


async def clear_startup_logs(db: AsyncSession) -> int:
    """Clear previous Worker audit logs at backend startup.

    Returns the number of SQLite backend log rows deleted. The JSONL file is
    truncated separately so every app run starts with a clean Worker log stream.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("", encoding="utf-8")

    result = await db.execute(
        delete(BackendLogEntry).where(BackendLogEntry.action_type.like("worker_audit.%"))
    )
    await db.commit()
    return int(result.rowcount or 0)


async def recent_for_task(
    db: AsyncSession,
    *,
    task_id: str,
    session_id: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return recent Worker audit events for a task from backend_logs."""
    limit = max(1, min(int(limit or 200), 1000))
    stmt = (
        select(BackendLogEntry)
        .where(BackendLogEntry.task_id == task_id)
        .where(BackendLogEntry.action_type.like("worker_audit.%"))
        .order_by(BackendLogEntry.timestamp.desc())
        .limit(limit)
    )
    if session_id:
        stmt = stmt.where(BackendLogEntry.correlation_id == session_id)
    rows = list((await db.execute(stmt)).scalars().all())
    rows.reverse()
    events: list[dict[str, Any]] = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if payload:
            events.append(payload)
            continue
        events.append(
            {
                "timestamp": row.timestamp.isoformat() + "Z" if row.timestamp else "",
                "level": row.level,
                "event": row.action_type.replace("worker_audit.", "", 1),
                "message": row.message,
                "task_id": row.task_id,
                "agent_id": row.agent_id,
                "session_id": row.correlation_id or "",
                "payload": {},
            }
        )
    return events


__all__ = ["WorkerAuditEvent", "append", "clear_startup_logs", "recent_for_task", "LOG_FILE"]
