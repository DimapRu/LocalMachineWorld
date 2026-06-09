"""Intercept and execute JSON actions emitted as plain Director text.

Some local models cannot use native OpenAI tool calls. They may still emit a
JSON block such as `{ "action_type": "board_mutate", ... }` in the assistant
text. This module treats that JSON as a backend action request, suppresses it
from UI-visible text, executes it through the same safe executors as native tool
calls, and writes a strict backend log entry with success/failure feedback.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, BackendLogEntry, Task
from app.services import backend_logs
from app.services.board_tools import execute_board_tool_calls
from app.services.director_tools import execute_director_memory_calls
from app.services.runtime_memory import runtime_memory

FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)
ACTION_KEY_RE = re.compile(r'"action_type"\s*:')
STRICT_ACTIONS_RE = re.compile(r'"actions"\s*:', re.IGNORECASE)
BOARD_MUTATE_RE = re.compile(r'"board_mutate"', re.IGNORECASE)
BOARD_SHAPE_RE = re.compile(r'"(?:description|status|assignee|assigned_to|title)"\s*:', re.IGNORECASE)


@dataclass
class InterceptedActions:
    """Result of parsing and executing text-emitted Director actions."""

    visible_text: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    changed_tasks: list[Task] = field(default_factory=list)
    logs: list[BackendLogEntry] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for action in self.actions if action.get("success"))

    @property
    def failure_count(self) -> int:
        return sum(1 for action in self.actions if not action.get("success"))

    @property
    def has_actions(self) -> bool:
        return bool(self.actions)

    @property
    def had_failure(self) -> bool:
        return self.failure_count > 0


def contains_action_json(text: str) -> bool:
    """Cheap pre-check for model text that likely contains action JSON."""

    return bool(text and (ACTION_KEY_RE.search(text) or STRICT_ACTIONS_RE.search(text) or BOARD_MUTATE_RE.search(text) or BOARD_SHAPE_RE.search(text)))


def extract_action_json(text: str) -> tuple[list[dict[str, Any]], str]:
    """Extract JSON objects with `action_type` and remove them from visible text."""

    if not text:
        return [], ""

    actions: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []

    whole_payloads = _load_actions(_strip_json_fence(text))
    if whole_payloads:
        return whole_payloads, ""

    for match in FENCED_JSON_RE.finditer(text):
        payloads = _load_actions(match.group(1))
        if payloads:
            actions.extend(payloads)
            spans.append(match.span())

    occupied = set()
    for start, end in spans:
        occupied.update(range(start, end))

    for start, candidate in _scan_json_objects(text):
        if start in occupied:
            continue
        payloads = _load_actions(candidate)
        if payloads:
            actions.extend(payloads)
            spans.append((start, start + len(candidate)))

    visible = _remove_spans(text, spans)
    return actions, visible


async def intercept_and_execute(
    *,
    db: AsyncSession,
    director: Agent,
    text: str,
    current_board: list[Task],
    allow_delete: bool = False,
    max_create_cards: int = 20,
    correlation_id: Optional[str] = None,
    user_message: str = "",
) -> InterceptedActions:
    """Execute text-emitted Director actions and return UI-safe visible text."""

    actions, visible_text = extract_action_json(text)
    result = InterceptedActions(visible_text=visible_text)

    if not actions:
        return result

    await backend_logs.append(
        db,
        source="Директор",
        level="WARNING",
        message=f"Director emitted {len(actions)} JSON action block(s) in plain text; backend intercepted them before UI display.",
        action_type="json_intercept",
        status="intercepted",
        correlation_id=correlation_id,
        agent_id=director.id,
        payload={"actions": actions},
    )

    board_snapshot = list(current_board)
    create_budget = max_create_cards
    for action in actions:
        _repair_action_from_user_message(action, user_message)
        action_type = str(action.get("action_type") or "").strip()
        if not action_type:
            continue

        try:
            if action_type == "board_mutate":
                changed, log_entry, success = await _execute_board_action(
                    db=db,
                    director=director,
                    action=action,
                    current_board=board_snapshot,
                    allow_delete=allow_delete,
                    max_create_cards=create_budget,
                    correlation_id=correlation_id,
                )
                result.changed_tasks.extend(changed)
                board_snapshot.extend(t for t in changed if t.id not in {existing.id for existing in board_snapshot})
                create_budget = max(0, create_budget - len([t for t in changed if t.id]))
                result.logs.append(log_entry)
                result.actions.append({"action_type": action_type, "success": success, "log_id": log_entry.id})
                continue

            if action_type == "model_gc":
                log_entry, success = await _execute_model_gc_action(
                    db=db,
                    director=director,
                    action=action,
                    correlation_id=correlation_id,
                )
                result.logs.append(log_entry)
                result.actions.append({"action_type": action_type, "success": success, "log_id": log_entry.id})
                continue

            if action_type in {"load_model", "unload_model", "list_loaded_models", "memory_mutate"}:
                log_entry, success = await _execute_memory_action(
                    db=db,
                    director=director,
                    action=action,
                    correlation_id=correlation_id,
                )
                result.logs.append(log_entry)
                result.actions.append({"action_type": action_type, "success": success, "log_id": log_entry.id})
                continue

            log_entry = await backend_logs.append(
                db,
                source="Бэкенд",
                level="ERROR",
                message=f"Unsupported Director text action_type: {action_type}",
                error_details="Only board_mutate, model_gc, load_model, unload_model, list_loaded_models, and memory_mutate are supported.",
                action_type=action_type,
                status="failed",
                correlation_id=correlation_id,
                agent_id=director.id,
                payload=action,
            )
            result.logs.append(log_entry)
            result.actions.append({"action_type": action_type, "success": False, "log_id": log_entry.id})
        except Exception as exc:  # noqa: BLE001 - errors must become Director feedback
            log_entry = await backend_logs.append(
                db,
                source="Бэкенд",
                level="ERROR",
                message=f"Backend failed while executing Director text action {action_type}",
                error_details=f"{type(exc).__name__}: {exc}",
                action_type=action_type,
                status="failed",
                correlation_id=correlation_id,
                agent_id=director.id,
                payload=action,
            )
            result.logs.append(log_entry)
            result.actions.append({"action_type": action_type, "success": False, "log_id": log_entry.id})

    return result


async def _execute_board_action(
    *,
    db: AsyncSession,
    director: Agent,
    action: dict[str, Any],
    current_board: list[Task],
    allow_delete: bool,
    max_create_cards: int,
    correlation_id: Optional[str],
) -> tuple[list[Task], BackendLogEntry, bool]:
    mutation = action.get("mutation")
    calls = action.get("tool_calls")
    if not isinstance(calls, list):
        calls = [_mutation_to_board_tool_call(mutation if isinstance(mutation, dict) else action)]

    calls = [call for call in calls if call]
    if not calls:
        log_entry = await backend_logs.append(
            db,
            source="Board",
            level="ERROR",
            message="board_mutate action did not contain an executable mutation.",
            error_details="Expected mutation.type add_task/update_task/assign_task/delete_task or OpenAI-style tool_calls.",
            action_type="board_mutate",
            status="failed",
            correlation_id=correlation_id,
            agent_id=director.id,
            payload=action,
        )
        return [], log_entry, False

    execution = await execute_board_tool_calls(
        db=db,
        director=director,
        current_board=current_board,
        tool_calls=calls,
        max_create_cards=max_create_cards,
        allow_delete=allow_delete,
    )

    success = bool(execution.changed_tasks) and not execution.rejected
    level = "INFO" if success else "ERROR"
    status = "success" if success else "failed"
    message = (
        f"Executed board_mutate: changed {len(execution.changed_tasks)} Board task(s)."
        if success
        else "board_mutate failed or was partially rejected by backend validation."
    )
    log_entry = await backend_logs.append(
        db,
        source="Board",
        level=level,
        message=message,
        error_details="; ".join(execution.rejected),
        action_type="board_mutate",
        status=status,
        correlation_id=correlation_id,
        agent_id=director.id,
        payload={"action": action, "changed_task_ids": [task.id for task in execution.changed_tasks], "rejected": execution.rejected},
    )
    return execution.changed_tasks, log_entry, success


async def _execute_memory_action(
    *,
    db: AsyncSession,
    director: Agent,
    action: dict[str, Any],
    correlation_id: Optional[str],
) -> tuple[BackendLogEntry, bool]:
    action_type = str(action.get("action_type") or "").strip()
    if action_type == "memory_mutate":
        tool_name = str(action.get("tool") or action.get("name") or "").strip()
        args = action.get("arguments") or action.get("args") or action.get("mutation") or {}
    else:
        tool_name = action_type
        args = action.get("arguments") or action.get("args") or action.get("mutation") or {
            key: value for key, value in action.items() if key != "action_type"
        }

    if not tool_name:
        tool_name = "list_loaded_models"
    tool_call = {"function": {"name": tool_name, "arguments": args if isinstance(args, dict) else {}}}
    execution = await execute_director_memory_calls(db=db, director=director, tool_calls=[tool_call])
    success = bool(execution.actions) and not execution.rejected
    log_entry = await backend_logs.append(
        db,
        source="Бэкенд",
        level="INFO" if success else "ERROR",
        message=f"Executed memory action {tool_name}." if success else f"Memory action {tool_name} failed.",
        error_details="; ".join(execution.rejected),
        action_type=tool_name,
        status="success" if success else "failed",
        correlation_id=correlation_id,
        agent_id=director.id,
        payload={"action": action, "results": execution.actions, "rejected": execution.rejected},
    )
    return log_entry, success


async def _execute_model_gc_action(
    *,
    db: AsyncSession,
    director: Agent,
    action: dict[str, Any],
    correlation_id: Optional[str],
) -> tuple[BackendLogEntry, bool]:
    """Execute a Director-emitted lightweight model garbage-collection action."""

    loaded = await runtime_memory.list_loaded()
    keep = str(action.get("keep_model") or action.get("keep") or "").strip()
    unloaded: list[dict[str, Any]] = []
    rejected: list[str] = []

    for entry in loaded:
        model_name = str(entry.get("model_name") or "").strip()
        if not model_name or (keep and model_name == keep):
            continue
        try:
            unloaded.append(await runtime_memory.unload_model(model_name=model_name))
        except Exception as exc:  # noqa: BLE001 - surface as backend feedback
            rejected.append(f"{model_name}: {type(exc).__name__}: {exc}")

    success = not rejected
    if not loaded:
        message = "model_gc completed: no pinned models were loaded."
    else:
        message = f"model_gc completed: unloaded {len(unloaded)} model(s), kept {keep or 'none explicitly'}."
    log_entry = await backend_logs.append(
        db,
        source="Бэкенд",
        level="INFO" if success else "ERROR",
        message=message,
        error_details="; ".join(rejected),
        action_type="model_gc",
        status="success" if success else "failed",
        correlation_id=correlation_id,
        agent_id=director.id,
        payload={"action": action, "loaded_before": loaded, "unloaded": unloaded, "rejected": rejected},
    )
    return log_entry, success


def _repair_action_from_user_message(action: dict[str, Any], user_message: str) -> None:
    """Fill safe missing fields in underspecified text-emitted actions.

    Local chat-only models often emit only `{type:add_task, assigned_to:...}` and
    rely on surrounding user text for the actual work. Backend validation should
    not execute unsafe empty titles, but it can safely recover the title and
    description from the real user request that triggered the action.
    """

    if not isinstance(action, dict) or action.get("action_type") != "board_mutate":
        return
    mutation = action.get("mutation")
    if not isinstance(mutation, dict):
        return
    kind = str(mutation.get("type") or mutation.get("action") or "").strip().lower()
    if kind not in {"add_task", "create", "create_task"}:
        return

    if not str(mutation.get("title") or "").strip():
        mutation["title"] = _title_from_user_message(user_message)
    if not str(mutation.get("description") or "").strip():
        mutation["description"] = _description_from_user_message(user_message)


def _title_from_user_message(user_message: str) -> str:
    text = (user_message or "").lower()
    delete_words = ("удали", "удалить", "удаление", "сотри", "стереть", "убери", "remove", "delete", "unlink", "erase")
    file_words = ("файл", "file", "документ", "document", ".txt", ".md", ".json", ".py", ".js", ".ts", ".tsx")
    if any(word in text for word in delete_words) and any(word in text for word in file_words):
        return "Удалить запрошенный файл"
    if any(word in text for word in ("рабоч", "desktop")) and any(word in text for word in ("папк", "folder")):
        return "Создать тестовый файл на рабочем столе"
    if any(word in text for word in file_words):
        return "Создать запрошенный текстовый файл"
    cleaned = re.sub(r"\s+", " ", user_message or "").strip(" .,!?:;\n\t")
    if not cleaned:
        return "Выполнить пользовательскую задачу"
    return cleaned[:80]


def _description_from_user_message(user_message: str) -> str:
    text = re.sub(r"\s+", " ", user_message or "").strip()
    if not text:
        return "- Выполнить действие, запрошенное пользователем.\n- Подтвердить результат через submit_result."
    lower = text.lower()
    delete_words = ("удали", "удалить", "удаление", "сотри", "стереть", "убери", "remove", "delete", "unlink", "erase")
    file_words = ("файл", "file", "документ", "document", ".txt", ".md", ".json", ".py", ".js", ".ts", ".tsx")
    if any(word in lower for word in delete_words) and any(word in lower for word in file_words):
        return (
            "- Удалить именно пользовательский файл, указанный в запросе или понятный из последнего контекста.\n"
            f"- Запрос: {text[:1000]}\n"
            "- Использовать Worker filesystem tool delete_file, а не удалять Board-задачу.\n"
            "- Завершить только после фактической проверки, что файла больше нет."
        )
    return (
        "- Выполнить исходный запрос пользователя.\n"
        f"- Запрос: {text[:1000]}\n"
        "- Если нужно создать/изменить/удалить файл, использовать Worker filesystem tools.\n"
        "- Завершить только после фактической проверки результата."
    )


def _mutation_to_board_tool_call(mutation: dict[str, Any]) -> dict[str, Any]:
    kind = str(mutation.get("type") or mutation.get("action") or "").strip().lower()
    args = dict(mutation)
    args.pop("type", None)
    args.pop("action", None)

    if kind in {"add_task", "create", "create_task"}:
        tool = "add_board_task"
        if "assigned_to" in args and "assigned_agent_name" not in args and "assigned_agent_id" not in args:
            args["assigned_agent_name"] = str(args.pop("assigned_to"))
    elif kind in {"update_task", "update", "move"}:
        tool = "update_board_task"
    elif kind in {"assign_task", "assign"}:
        tool = "assign_task"
        if "assigned_to" in args and "assigned_agent_name" not in args and "assigned_agent_id" not in args:
            args["assigned_agent_name"] = str(args.pop("assigned_to"))
    elif kind in {"delete_task", "delete", "remove"}:
        tool = "delete_board_task"
    else:
        return {}

    return {"function": {"name": tool, "arguments": args}}


def _strip_json_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```json") or text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    return text


def _load_actions(raw: str) -> list[dict[str, Any]]:
    """Load explicit action JSON or repair list-style Board JSON into actions."""

    try:
        parsed = json.loads(_strip_json_fence(raw))
    except (TypeError, json.JSONDecodeError):
        return []

    if isinstance(parsed, dict):
        action = _coerce_action(parsed)
        if not action:
            return []
        if action.get("action_type") == "__batch__" and isinstance(action.get("actions"), list):
            return [item for item in action["actions"] if isinstance(item, dict)]
        return [action]

    if isinstance(parsed, list):
        actions: list[dict[str, Any]] = []
        for item in parsed[:20]:
            if isinstance(item, dict):
                action = _coerce_action(item)
                if action:
                    actions.append(action)
        return actions

    return []


def _coerce_action(parsed: dict[str, Any]) -> Optional[dict[str, Any]]:
    board_keys = {
        "id",
        "title",
        "description",
        "status",
        "assignee",
        "assigned_to",
        "assigned_agent_id",
        "assigned_agent_name",
    }
    if isinstance(parsed.get("actions"), list):
        actions: list[dict[str, Any]] = []
        for item in parsed.get("actions", [])[:20]:
            if isinstance(item, dict):
                action = _coerce_action(item)
                if action:
                    actions.append(action)
        if actions:
            return {"action_type": "__batch__", "actions": actions}

    name = parsed.get("name")
    if name == "board_mutate" and isinstance(parsed.get("arguments"), dict):
        return {"action_type": "board_mutate", "mutation": _normalize_board_mutation(dict(parsed["arguments"]))}

    action_type = parsed.get("action_type")
    mutation = parsed.get("mutation")

    # Chat-only models also emit list items like {"mutation": {...}} without
    # action_type. Accept executable add/update/delete Board mutations directly,
    # but drop invalid assignment placeholders such as task_id=null.
    if isinstance(mutation, dict):
        kind = str(mutation.get("type") or mutation.get("action") or "").strip().lower()
        if kind in {"add_task", "create", "create_task"}:
            return {"action_type": "board_mutate", "mutation": _normalize_board_mutation(dict(mutation))}
        if kind in {"update_task", "update", "move", "assign_task", "assign", "delete_task", "delete", "remove"}:
            task_id = str(mutation.get("task_id") or mutation.get("id") or "").strip()
            if task_id:
                return {"action_type": "board_mutate", "mutation": dict(mutation)}

    # Chat-only models sometimes emit an explicit board_mutate object but put
    # card fields at the top level instead of inside `mutation`. Repair that
    # into the same safe mutation shape that the Board executor already checks.
    if action_type == "board_mutate":
        if isinstance(parsed.get("mutation"), dict) or isinstance(parsed.get("tool_calls"), list):
            return parsed
        if board_keys & set(parsed.keys()):
            mutation = {key: value for key, value in parsed.items() if key != "action_type"}
            return {"action_type": "board_mutate", "mutation": _normalize_board_mutation(mutation)}

    if isinstance(action_type, str):
        return parsed

    # Chat-only models often emit a bare list/object of Board cards instead of
    # the required {action_type: board_mutate, mutation: ...}. Treat that as a
    # Board create request and let the normal safe Board validator execute it.
    if board_keys & set(parsed.keys()):
        return {"action_type": "board_mutate", "mutation": _normalize_board_mutation(dict(parsed))}

    return None


def _normalize_board_mutation(mutation: dict[str, Any]) -> dict[str, Any]:
    mutation["type"] = mutation.get("type") or mutation.get("action") or "add_task"
    if "assignee" in mutation and "assigned_to" not in mutation:
        mutation["assigned_to"] = mutation.pop("assignee")
    if "id" in mutation and "title" not in mutation:
        mutation["title"] = str(mutation.pop("id") or "")
    return mutation


def _scan_json_objects(text: str) -> list[tuple[int, str]]:
    objects: list[tuple[int, str]] = []
    for match in ACTION_KEY_RE.finditer(text):
        start = text.rfind("{", 0, match.start())
        if start < 0:
            continue
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
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
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    objects.append((start, text[start : idx + 1]))
                    break
    return objects


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text.strip()
    normalized: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not normalized or start > normalized[-1][1]:
            normalized.append((start, end))
        else:
            prev_start, prev_end = normalized[-1]
            normalized[-1] = (prev_start, max(prev_end, end))

    parts: list[str] = []
    cursor = 0
    for start, end in normalized:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    cleaned = "".join(parts)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned
