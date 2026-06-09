"""Strict backend action log and Director feedback service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import BackendLogEntry

VALID_LEVELS = {"INFO", "WARNING", "ERROR"}
MAX_FIELD = 4000
MAX_CONTEXT_LINES = 20


@dataclass
class LoggedActionResult:
    """Normalized execution result surfaced back to the Director."""

    success: bool
    message: str
    error_details: str = ""
    action_type: str = ""
    payload: dict[str, Any] | None = None


def _clip(value: object, limit: int = MAX_FIELD) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 20]}... [truncated]"


def _safe_payload(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    try:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded) <= 12000:
            return payload
        return {"_truncated": True, "preview": encoded[:12000]}
    except (TypeError, ValueError):
        return {"_unserializable": repr(payload)[:12000]}


async def append(
    db: Optional[AsyncSession] = None,
    *,
    source: str,
    level: str = "INFO",
    message: str,
    error_details: str = "",
    action_type: str = "",
    status: str = "",
    correlation_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    task_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> BackendLogEntry:
    """Append one backend/system feedback log entry.

    If `db` is omitted, this function opens a short-lived session and commits
    immediately. If `db` is supplied, the caller owns commit/rollback.
    """

    normalized_level = str(level or "INFO").upper()
    if normalized_level not in VALID_LEVELS:
        normalized_level = "INFO"

    entry = BackendLogEntry(
        source=_clip(source or "Бэкенд", 80),
        level=normalized_level,
        message=_clip(message),
        error_details=_clip(error_details, 8000),
        action_type=_clip(action_type, 80),
        status=_clip(status, 20),
        correlation_id=correlation_id,
        agent_id=agent_id,
        task_id=task_id,
        payload=_safe_payload(payload),
    )

    if db is not None:
        db.add(entry)
        await db.flush()
        return entry

    async with async_session() as session:
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        return entry


async def clear_startup_logs(db: AsyncSession) -> int:
    """Clear all operational logs shown in the Agent Logs tab on app startup."""

    result = await db.execute(delete(BackendLogEntry))
    await db.commit()
    return int(result.rowcount or 0)


async def recent(
    db: AsyncSession,
    *,
    limit: int = MAX_CONTEXT_LINES,
    level: Optional[str] = None,
) -> list[BackendLogEntry]:
    """Return recent backend logs newest-last for prompt injection."""

    stmt = select(BackendLogEntry).order_by(BackendLogEntry.timestamp.desc()).limit(max(1, min(limit, 100)))
    if level:
        stmt = (
            select(BackendLogEntry)
            .where(BackendLogEntry.level == level.upper())
            .order_by(BackendLogEntry.timestamp.desc())
            .limit(max(1, min(limit, 100)))
        )
    result = await db.execute(stmt)
    return list(reversed(result.scalars().all()))


async def director_feedback_context(db: AsyncSession, *, limit: int = MAX_CONTEXT_LINES) -> str:
    """Build hidden feedback context for the Director prompt."""

    entries = await recent(db, limit=limit)
    if not entries:
        return "Backend feedback log: no entries yet."

    lines = [
        "Backend feedback log (hidden operational truth; newest entries at bottom):",
        "- The Director must treat ERROR entries as blocking until recovered.",
        "- The Director must not claim success unless the backend log has status=success for the requested action.",
    ]
    lines.extend(_format_entries(entries))
    return "\n".join(lines)


async def board_feedback_context(db: AsyncSession, *, limit: int = 12) -> str:
    """Build separate Board execution feedback for Director prompt checks."""

    stmt = (
        select(BackendLogEntry)
        .where(
            (BackendLogEntry.source == "Board")
            | (BackendLogEntry.action_type.in_(["board_mutate", "native_board_tools", "board_fallback"]))
        )
        .order_by(BackendLogEntry.timestamp.desc())
        .limit(max(1, min(limit, 50)))
    )
    result = await db.execute(stmt)
    entries = list(reversed(result.scalars().all()))
    if not entries:
        return "Board execution log: no Board actions have been executed yet."

    lines = [
        "Board execution log (separate Board truth; newest entries at bottom):",
        "- status=success means a Board mutation was actually committed.",
        "- status=failed means the Director must repair the Board action before claiming progress.",
    ]
    lines.extend(_format_entries(entries))
    return "\n".join(lines)


def _format_entries(entries: list[BackendLogEntry]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        details = f" | details={_clip(entry.error_details, 500)}" if entry.error_details else ""
        status = f" | status={entry.status}" if entry.status else ""
        action = f" | action={entry.action_type}" if entry.action_type else ""
        task = f" | task_id={entry.task_id}" if entry.task_id else ""
        lines.append(
            f"[{_format_ts(entry.timestamp)}] {entry.level} source={entry.source}{action}{status}{task}: {entry.message}{details}"
        )
    return lines


def critical_error_prompt(entry: BackendLogEntry) -> str:
    """Return the strict system prompt fragment sent back to the Director on backend failure."""

    details = entry.error_details or entry.message
    return (
        f"Критическая ошибка бэкенда при вызове {entry.action_type or entry.source}: {details}. "
        "Скорректируй свои действия или выгрузи лишние модели из ОЗУ! "
        "Не назначай новые задачи и не пиши Done, пока backend_logs/board_logs не покажут status=success."
    )


def _format_ts(value: datetime | None) -> str:
    if not value:
        return "unknown-time"
    return value.isoformat()
