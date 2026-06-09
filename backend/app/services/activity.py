"""Shared runtime activity state for Board dispatcher UI.

This module keeps a lightweight in-memory snapshot of what lmWorld is doing
right now: worker state, active model/task, recent tool/file actions, and a
safe Director/Boss reasoning summary. It intentionally stores summaries and
observable actions instead of hidden model chain-of-thought.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Deque, Optional

from app.services.websocket import ws_manager

MAX_EVENTS = 80


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


class ActivityState:
    """In-memory activity tracker used by the worker, runtime, and MCP tools."""

    def __init__(self) -> None:
        self._events: Deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._snapshot: dict[str, Any] = {
            "running": False,
            "action": "idle",
            "status": "Worker is idle",
            "active_agent": None,
            "active_model": None,
            "active_runtime": "unknown",
            "active_task": None,
            "current_files": [],
            "boss_summary": "Boss is monitoring the Board and waiting for actionable tasks.",
            "last_update": _now(),
            "readit_integrations": {
                "llama_cpp": "GGUF fallback via llama-server with CPU/GPU layer split support.",
                "vllm": "Planned high-VRAM runtime option; current app exposes runtime status hooks first.",
                "llama_swap": "Implemented as unload controls for llama.cpp instances and runtime reset hooks.",
                "langgraph": "Mapped to worker monitor cycle: scan Board, delegate, run inference, review status.",
                "letta_memory": "Represented as persistent Board tasks/logs plus Director summary state.",
                "mcp": "Built-in local MCP-like file and terminal tools are instrumented for activity logs.",
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable copy of the current activity snapshot."""
        return {**self._snapshot, "events": list(self._events)}

    def set_worker_running(self, running: bool) -> dict[str, Any]:
        status = "Worker loop is running" if running else "Worker loop is stopped"
        return self.update(
            running=running,
            action="idle" if running else "stopped",
            status=status,
            boss_summary=(
                "Boss is monitoring Board tasks and model capacity."
                if running
                else "Boss is paused; no Board tasks will be dispatched until the worker starts."
            ),
            event_type="worker",
            event_title=status,
        )

    def update(
        self,
        *,
        action: Optional[str] = None,
        status: Optional[str] = None,
        active_agent: Optional[dict[str, Any]] = None,
        active_model: Optional[str] = None,
        active_runtime: Optional[str] = None,
        active_task: Optional[dict[str, Any]] = None,
        current_files: Optional[list[dict[str, Any]]] = None,
        boss_summary: Optional[str] = None,
        running: Optional[bool] = None,
        event_type: str = "activity",
        event_title: Optional[str] = None,
        event_meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if action is not None:
            self._snapshot["action"] = action
        if status is not None:
            self._snapshot["status"] = status
        if active_agent is not None:
            self._snapshot["active_agent"] = active_agent
        if active_model is not None:
            self._snapshot["active_model"] = active_model
        if active_runtime is not None:
            self._snapshot["active_runtime"] = active_runtime
        if active_task is not None:
            self._snapshot["active_task"] = active_task
        if current_files is not None:
            self._snapshot["current_files"] = current_files[-12:]
        if boss_summary is not None:
            self._snapshot["boss_summary"] = boss_summary
        if running is not None:
            self._snapshot["running"] = running

        self._snapshot["last_update"] = _now()

        event = {
            "time": self._snapshot["last_update"],
            "type": event_type,
            "title": event_title or status or action or "Activity update",
            "action": self._snapshot["action"],
            "meta": event_meta or {},
        }
        self._events.appendleft(event)
        return self.snapshot()

    async def broadcast(self, snapshot: Optional[dict[str, Any]] = None) -> None:
        """Broadcast activity state to office WebSocket clients."""
        await ws_manager.broadcast_office(
            {"type": "dispatcher_activity", "activity": snapshot or self.snapshot()}
        )

    async def update_and_broadcast(self, **kwargs: Any) -> dict[str, Any]:
        snapshot = self.update(**kwargs)
        await self.broadcast(snapshot)
        return snapshot

    async def track_tool_action(
        self,
        *,
        action: str,
        path: Optional[str] = None,
        command: Optional[str] = None,
        success: Optional[bool] = None,
    ) -> None:
        item = {
            "action": action,
            "path": path,
            "command": command,
            "success": success,
            "time": _now(),
        }
        current_files = list(self._snapshot.get("current_files") or [])
        current_files.append(item)
        title_target = path or command or "workspace"
        await self.update_and_broadcast(
            action=f"tool_{action}",
            status=f"Tool action: {action} {title_target}",
            current_files=current_files,
            boss_summary="Boss is observing tool activity and updating the operational context.",
            event_type="tool",
            event_title=f"{action}: {title_target}",
            event_meta={"path": path, "command": command, "success": success},
        )


activity_state = ActivityState()
