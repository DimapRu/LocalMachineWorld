"""Native Board tool schemas and safe backend executor.

The model may propose OpenAI-compatible tool calls, but this module is the only
place that mutates Board tasks. Every proposed call is validated against current
DB state, allowed statuses, duplicate titles, existing agents, and user intent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Task


VALID_BOARD_STATUSES = {"todo", "in_progress", "review", "done"}
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
BAD_BOARD_TITLE_RE = re.compile(
    r"(id\s*=|existing-task-id|элемент\s+с\s+id|сначала\s+я|я\s+обновлю|я\s+создам|"
    r"окей|попроси|попросить|сотрудник[ау]?|agent\s*\d+|"
    r"i\s+will|i'll|first\s+i|assistant\s+draft|board\s+tool|json|commands|markdown)",
    re.IGNORECASE,
)


@dataclass
class BoardToolExecution:
    """Result of executing model-proposed Board tool calls."""

    changed_tasks: list[Task]
    rejected: list[str]
    runtime_tier: str = "native_tool_calls"


BOARD_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "add_board_task",
            "description": (
                "Create a Board task. Use this for the project ROOT task (no parent_task_id) "
                "and for every subtask (with parent_task_id set to the root task id). "
                "Always emit one of these per logical step the user asked for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Clean Board card title, 3-9 words, imperative, no narration.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short checklist with acceptance criteria, '- step' lines.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["todo", "in_progress", "review", "done"],
                        "description": "Initial status. Use todo for new work.",
                    },
                    "parent_task_id": {
                        "type": "string",
                        "description": "Optional parent Board task id; set when this card is a subtask of a root project task.",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Lower = earlier. Use 10/20/30/... so subtasks have a clear order.",
                    },
                    "assigned_agent_id": {
                        "type": "string",
                        "description": "Optional existing agent id to assign this task to.",
                    },
                    "assigned_agent_name": {
                        "type": "string",
                        "description": "Optional existing agent name if id is unknown, e.g. agent1.",
                    },
                },
                "required": ["title", "description"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_board_task",
            "description": "Update an existing Board task by exact task id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Existing Board task id."},
                    "title": {"type": "string", "description": "Optional safe new title."},
                    "description": {"type": "string", "description": "Optional new description."},
                    "status": {"type": "string", "enum": ["todo", "in_progress", "review", "done"]},
                    "assigned_agent_id": {"type": "string", "description": "Optional existing agent id."},
                    "assigned_agent_name": {"type": "string", "description": "Optional existing agent name."},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_task",
            "description": "Assign an existing Board task to an existing agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Existing Board task id."},
                    "assigned_agent_id": {"type": "string", "description": "Existing agent id."},
                    "assigned_agent_name": {"type": "string", "description": "Existing agent name if id is unknown."},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_board_task",
            "description": "Delete an existing Board task. Use only for explicit user delete/remove requests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Existing Board task id."},
                    "reason": {"type": "string", "description": "Short reason for audit logs."},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
]


def board_tool_schemas(*, allow_delete: bool = False) -> list[dict[str, Any]]:
    """Return tool schemas exposed to the model for this turn."""
    if allow_delete:
        return BOARD_TOOL_SCHEMAS
    return [tool for tool in BOARD_TOOL_SCHEMAS if tool["function"]["name"] != "delete_board_task"]


def sanitize_board_title(raw: str) -> str:
    """Return a clean Board title or an empty string for unsafe model output."""
    title = re.sub(r"\s+", " ", raw or "").strip()
    title = re.sub(r"^[\s\-•*#>]*(?:\d+[.)]|[IVXLC]+[.)])\s*", "", title, flags=re.IGNORECASE)
    title = UUID_RE.sub("", title)
    title = re.sub(r"\bid\s*=\s*\S+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" -—:;.,")

    if not title or len(title) < 6 or len(title) > 120:
        return ""
    if BAD_BOARD_TITLE_RE.search(title):
        return ""
    if title.count("{") or title.count("}") or title.count('"') > 2:
        return ""
    return title[:1].upper() + title[1:]


def _title_key(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").lower()).strip()


def _parse_tool_arguments(raw_args: object) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if not isinstance(raw_args, str):
        return {}
    try:
        parsed = json.loads(raw_args or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _normalize_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalize OpenAI/Ollama-style tool call shapes to (name, args)."""
    function = call.get("function") if isinstance(call, dict) else None
    if isinstance(function, dict):
        return str(function.get("name") or ""), _parse_tool_arguments(function.get("arguments"))
    return str(call.get("name") or ""), _parse_tool_arguments(call.get("arguments"))


async def _load_agents(db: AsyncSession) -> list[Agent]:
    result = await db.execute(select(Agent).order_by(Agent.created_at.asc()))
    return list(result.scalars().all())


def _resolve_agent_id(args: dict[str, Any], agents: list[Agent]) -> Optional[str]:
    raw_id = str(
        args.get("assigned_agent_id")
        or args.get("assignee_id")
        or args.get("worker_id")
        or args.get("agent_id")
        or ""
    ).strip()
    if raw_id and any(agent.id == raw_id for agent in agents):
        return raw_id

    raw_name = str(
        args.get("assigned_agent_name")
        or args.get("assigned_to")
        or args.get("assignee")
        or args.get("worker")
        or ""
    ).strip().lower()
    if raw_name:
        for agent in agents:
            aliases = {agent.name.lower(), agent.id.lower()}
            if raw_name in aliases:
                return agent.id
    return None


def _default_worker_id(agents: list[Agent], director: Agent) -> str:
    """Prefer assigning new executable Board tasks to a Worker, not the Director."""
    for agent in agents:
        if not agent.is_director and agent.status != "fired":
            return agent.id
    for agent in agents:
        if not agent.is_director:
            return agent.id
    return director.id


async def execute_board_tool_calls(
    *,
    db: AsyncSession,
    director: Agent,
    current_board: list[Task],
    tool_calls: list[dict[str, Any]],
    max_create_cards: int = 1,
    allow_delete: bool = False,
) -> BoardToolExecution:
    """Validate and execute native Board tool calls proposed by a model."""
    if not tool_calls:
        return BoardToolExecution(changed_tasks=[], rejected=[])

    agents = await _load_agents(db)
    by_id = {task.id: task for task in current_board}
    existing_by_title = {_title_key(task.title): task for task in current_board}
    existing_titles = set(existing_by_title)
    created_titles: set[str] = set()
    changed: list[Task] = []
    deleted_ids: set[str] = set()
    rejected: list[str] = []
    create_count = 0

    for raw_call in tool_calls[:20]:
        name, args = _normalize_tool_call(raw_call)
        if not name:
            rejected.append("tool call without function name")
            continue

        if name == "add_board_task":
            if create_count >= max_create_cards:
                rejected.append("create limit reached for this user message")
                continue
            title = sanitize_board_title(str(args.get("title") or ""))
            if not title:
                rejected.append("unsafe or empty create title")
                continue
            title_key = _title_key(title)
            if title_key in existing_by_title:
                task = existing_by_title[title_key]
                assigned_agent_id = _resolve_agent_id(args, agents) or task.assigned_agent_id or _default_worker_id(agents, director)
                task.assigned_agent_id = assigned_agent_id
                if task.status != "in_progress":
                    task.status = "todo"
                    task.result = ""
                description = str(args.get("description") or "").strip()[:4000]
                if description:
                    task.description = description
                try:
                    priority = int(args.get("priority")) if args.get("priority") is not None else task.priority
                except (TypeError, ValueError):
                    priority = task.priority
                task.priority = max(0, min(int(priority or 100), 9999))
                task.updated_at = datetime.utcnow()
                logs = task.logs if isinstance(task.logs, list) else []
                logs.append("Reused existing Board task for duplicate create request; reassigned/reopened idempotently")
                task.logs = logs[-50:]
                created_titles.add(title_key)
                create_count += 1
                changed.append(task)
                continue
            if title_key in created_titles:
                continue
            status_value = str(args.get("status") or "todo").strip()
            if status_value not in VALID_BOARD_STATUSES or status_value == "done":
                status_value = "todo"
            assigned_agent_id = _resolve_agent_id(args, agents) or _default_worker_id(agents, director)

            parent_task_id: Optional[str] = None
            raw_parent = str(args.get("parent_task_id") or "").strip()
            if raw_parent:
                if raw_parent in by_id:
                    parent_task_id = raw_parent
                else:
                    rejected.append(f"unknown parent_task_id: {raw_parent}")

            try:
                priority = int(args.get("priority")) if args.get("priority") is not None else 100
            except (TypeError, ValueError):
                priority = 100
            priority = max(0, min(priority, 9999))

            task = Task(
                title=title,
                description=str(args.get("description") or "").strip()[:4000],
                status=status_value,
                assigned_agent_id=assigned_agent_id,
                parent_task_id=parent_task_id,
                priority=priority,
                logs=["Created by native Director Board tool call"],
            )
            db.add(task)
            created_titles.add(title_key)
            create_count += 1
            changed.append(task)
            continue

        task_id = str(args.get("task_id") or args.get("id") or "").strip()
        task = by_id.get(task_id)
        if not task:
            rejected.append(f"unknown task id for {name}: {task_id or '<empty>'}")
            continue

        if name == "delete_board_task":
            if not allow_delete:
                rejected.append("delete tool is not allowed for this message")
                continue
            await db.delete(task)
            deleted_ids.add(task.id)
            changed.append(task)
            continue

        if name == "assign_task":
            assigned_agent_id = _resolve_agent_id(args, agents)
            if not assigned_agent_id:
                rejected.append("assign_task without existing agent")
                continue
            task.assigned_agent_id = assigned_agent_id
            task.updated_at = datetime.utcnow()
            logs = task.logs if isinstance(task.logs, list) else []
            logs.append("Assigned by native Director Board tool call")
            task.logs = logs[-50:]
            changed.append(task)
            continue

        if name == "update_board_task":
            if "title" in args:
                title = sanitize_board_title(str(args.get("title") or ""))
                if title:
                    title_key = _title_key(title)
                    if title_key not in existing_titles or _title_key(task.title) == title_key:
                        task.title = title
            if "description" in args:
                task.description = str(args.get("description") or "").strip()[:4000]
            if "status" in args:
                status_value = str(args.get("status") or "").strip()
                if status_value in VALID_BOARD_STATUSES:
                    task.status = status_value
            assigned_agent_id = _resolve_agent_id(args, agents)
            if assigned_agent_id:
                task.assigned_agent_id = assigned_agent_id
            task.updated_at = datetime.utcnow()
            logs = task.logs if isinstance(task.logs, list) else []
            logs.append("Updated by native Director Board tool call")
            task.logs = logs[-50:]
            changed.append(task)
            continue

        rejected.append(f"unsupported tool: {name}")

    if not changed:
        return BoardToolExecution(changed_tasks=[], rejected=rejected)

    await db.flush()
    refreshed: list[Task] = []
    seen: set[str] = set()
    for task in changed:
        if task.id in seen:
            continue
        seen.add(task.id)
        if task.id not in deleted_ids:
            await db.refresh(task)
        refreshed.append(task)

    return BoardToolExecution(changed_tasks=refreshed, rejected=rejected)
