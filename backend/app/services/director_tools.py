"""Native tools surfaced to the Director (and only the Director).

Director tools are split into two groups:
  * memory tools — load/unload/list local model residency;
  * orchestration tools — delegate work to isolated Worker sessions and read
    task context explicitly.

The important product rule is backend-first execution: the Director may decide,
but only backend services mutate Board state, start Workers, or expose context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent
from app.services.runtime_memory import MemoryBudgetError, runtime_memory
from app.services.worker_factory import worker_factory


DIRECTOR_MEMORY_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "load_model",
            "description": (
                "Pin a local model into RAM/VRAM so a Worker can use it. "
                "Use only when a Worker is about to be assigned a task that "
                "needs this model warm."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_name": {
                        "type": "string",
                        "description": "Exact model id from the Office, e.g. llama3.1:8b.",
                    },
                    "device": {
                        "type": "string",
                        "enum": ["gpu", "cpu", "auto"],
                        "description": "Where to place the model. Defaults to gpu.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional id of the Worker this load is for.",
                    },
                    "estimated_gb": {
                        "type": "number",
                        "description": "Optional size estimate in GB; used for the RAM budget check.",
                    },
                    "keep_alive_seconds": {
                        "type": "integer",
                        "description": "How long to keep it warm with no activity. Default 600.",
                    },
                    "note": {
                        "type": "string",
                        "description": "Short reason this model is being pinned.",
                    },
                },
                "required": ["model_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unload_model",
            "description": (
                "Evict a model from RAM/VRAM. Use when no current or near-term "
                "task needs it and you want to free resources."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_name": {
                        "type": "string",
                        "description": "Exact model id currently pinned.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short reason, for audit logs.",
                    },
                },
                "required": ["model_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_loaded_models",
            "description": "Return all currently-pinned models with metadata.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


DIRECTOR_ORCHESTRATION_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": (
                "Create a Board card and launch an isolated Worker session for dirty file/code work. "
                "Use this instead of narrating plans when the user asks to create, edit, run, or build something. "
                "The Director receives only a compact status summary; Worker scratchpad/code context stays isolated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_model": {
                        "type": "string",
                        "description": "Optional exact Worker model to prefer, e.g. qwen2.5-coder:7b. Empty means backend chooses.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Concrete Worker instruction with paths, files, commands, and acceptance criteria.",
                    },
                    "task_title": {
                        "type": "string",
                        "description": "Short Board card title for the delegated task.",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Board priority; lower number is higher priority. Default 50.",
                    },
                    "wait": {
                        "type": "boolean",
                        "description": "If true, wait for Worker completion like Claude Code Task(). Default true.",
                    },
                },
                "required": ["instruction", "task_title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_context",
            "description": (
                "Explicitly read compact task context/result for a Board task. "
                "Use only when Director needs worker context; never automatic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Board task id to inspect."},
                    "limit": {"type": "integer", "description": "Max context entries to summarize. Default 20."},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
]


def director_memory_tool_schemas() -> list[dict[str, Any]]:
    """Return memory-management tool schemas for the Director."""
    return DIRECTOR_MEMORY_TOOL_SCHEMAS


def director_orchestration_tool_schemas() -> list[dict[str, Any]]:
    """Return Worker orchestration tool schemas for the Director."""
    return DIRECTOR_ORCHESTRATION_TOOL_SCHEMAS


@dataclass
class MemoryToolExecution:
    """Result of executing director memory tool calls."""

    actions: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


@dataclass
class OrchestrationToolExecution:
    """Result of executing Director orchestration tool calls."""

    actions: list[dict[str, Any]] = field(default_factory=list)
    changed_task_ids: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def _parse_args(raw_args: object) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if not isinstance(raw_args, str):
        return {}
    try:
        parsed = json.loads(raw_args or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _normalize_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    fn = call.get("function") if isinstance(call, dict) else None
    if isinstance(fn, dict):
        return str(fn.get("name") or ""), _parse_args(fn.get("arguments"))
    return str(call.get("name") or ""), _parse_args(call.get("arguments"))


async def execute_director_memory_calls(
    *,
    db: AsyncSession,
    director: Agent,
    tool_calls: list[dict[str, Any]],
) -> MemoryToolExecution:
    """Run validated memory operations on behalf of the Director."""
    execution = MemoryToolExecution()

    if not director or not director.is_director:
        execution.rejected.append("memory tools require a Director caller")
        return execution

    for call in tool_calls or []:
        name, args = _normalize_call(call)
        if name == "load_model":
            outcome = await _do_load(db=db, director=director, args=args)
        elif name == "unload_model":
            outcome = await _do_unload(args=args)
        elif name == "list_loaded_models":
            loaded = await runtime_memory.list_loaded()
            outcome = {"action": "list_loaded_models", "loaded": loaded}
        else:
            execution.rejected.append(f"unknown memory tool: {name}")
            continue

        if outcome.get("error"):
            execution.rejected.append(outcome["error"])
        else:
            execution.actions.append(outcome)

    return execution


async def execute_director_orchestration_calls(
    *,
    db: AsyncSession,
    director: Agent,
    tool_calls: list[dict[str, Any]],
) -> OrchestrationToolExecution:
    """Run Worker Factory operations on behalf of the Director."""
    execution = OrchestrationToolExecution()

    if not director or not director.is_director:
        execution.rejected.append("orchestration tools require a Director caller")
        return execution

    for call in tool_calls or []:
        name, args = _normalize_call(call)
        try:
            if name == "delegate_task":
                outcome = await _do_delegate_task(db=db, director=director, args=args)
                task_id = str(outcome.get("task_id") or "")
                if task_id:
                    execution.changed_task_ids.append(task_id)
            elif name == "read_file_context":
                outcome = await _do_read_file_context(db=db, director=director, args=args)
            else:
                execution.rejected.append(f"unknown orchestration tool: {name}")
                continue
        except Exception as exc:  # noqa: BLE001 - tool failures must be returned to Director
            execution.rejected.append(f"{name}: {type(exc).__name__}: {exc}")
            continue

        if outcome.get("error"):
            execution.rejected.append(outcome["error"])
        else:
            execution.actions.append(outcome)

    return execution


async def _do_delegate_task(*, db: AsyncSession, director: Agent, args: dict[str, Any]) -> dict[str, Any]:
    instruction = str(args.get("instruction") or "").strip()
    task_title = str(args.get("task_title") or "").strip()
    worker_model = str(args.get("worker_model") or "").strip()
    wait = bool(args.get("wait", True))
    try:
        priority = int(args.get("priority", 50))
    except (TypeError, ValueError):
        priority = 50

    result = await worker_factory.delegate_task(
        db=db,
        director=director,
        instruction=instruction,
        task_title=task_title,
        worker_model=worker_model,
        priority=priority,
        wait=wait,
        max_depth=1,
    )
    return {"action": "delegate_task", **result.to_director_summary()}


async def _do_read_file_context(*, db: AsyncSession, director: Agent, args: dict[str, Any]) -> dict[str, Any]:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"error": "read_file_context: missing task_id"}
    try:
        limit = int(args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    result = await worker_factory.read_file_context(db=db, director=director, task_id=task_id, limit=limit)
    return {"action": "read_file_context", **result}


async def _do_load(
    *, db: AsyncSession, director: Agent, args: dict[str, Any]
) -> dict[str, Any]:
    model_name = str(args.get("model_name") or "").strip()
    if not model_name:
        return {"error": "load_model: missing model_name"}

    device = str(args.get("device") or "gpu").lower()
    if device not in {"gpu", "cpu", "auto"}:
        device = "gpu"

    requested_agent_id: Optional[str] = None
    raw_agent_id = str(args.get("agent_id") or "").strip()
    if raw_agent_id:
        agent = await db.get(Agent, raw_agent_id)
        if agent is None:
            return {"error": f"load_model: agent {raw_agent_id!r} not found"}
        requested_agent_id = agent.id

    keep_alive = args.get("keep_alive_seconds")
    try:
        keep_alive_seconds = int(keep_alive) if keep_alive is not None else None
    except (TypeError, ValueError):
        keep_alive_seconds = None

    estimated = args.get("estimated_gb")
    try:
        estimated_gb = float(estimated) if estimated is not None else 0.0
    except (TypeError, ValueError):
        estimated_gb = 0.0

    note = str(args.get("note") or "").strip()

    try:
        entry = await runtime_memory.load_model(
            model_name=model_name,
            device=device,
            agent_id=requested_agent_id or director.id,
            keep_alive_seconds=keep_alive_seconds,
            estimated_gb=estimated_gb,
            note=note,
        )
    except MemoryBudgetError as e:
        return {"error": f"load_model: {e}"}
    except Exception as e:  # noqa: BLE001 — surface to Director for retry
        return {"error": f"load_model: {type(e).__name__}: {e}"}

    return {"action": "load_model", "entry": entry}


async def _do_unload(*, args: dict[str, Any]) -> dict[str, Any]:
    model_name = str(args.get("model_name") or "").strip()
    if not model_name:
        return {"error": "unload_model: missing model_name"}
    try:
        result = await runtime_memory.unload_model(model_name=model_name)
    except Exception as e:  # noqa: BLE001
        return {"error": f"unload_model: {type(e).__name__}: {e}"}
    return {"action": "unload_model", **result}


__all__ = [
    "DIRECTOR_MEMORY_TOOL_SCHEMAS",
    "DIRECTOR_ORCHESTRATION_TOOL_SCHEMAS",
    "director_memory_tool_schemas",
    "director_orchestration_tool_schemas",
    "MemoryToolExecution",
    "OrchestrationToolExecution",
    "execute_director_memory_calls",
    "execute_director_orchestration_calls",
]
