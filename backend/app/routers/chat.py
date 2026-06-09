"""Chat endpoints — direct conversation with the Director agent."""

import json
import re
from datetime import datetime
from typing import Optional
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Agent, ChatTurn, Task
from app.services.activity import activity_state
from app.services import backend_logs, director_actions
from app.services.board_tools import board_tool_schemas, execute_board_tool_calls
from app.services.director_tools import (
    director_memory_tool_schemas,
    director_orchestration_tool_schemas,
    execute_director_memory_calls,
    execute_director_orchestration_calls,
)
from app.services.resources import resource_monitor
from app.services.runtime import runtime
from app.services.runtime_memory import runtime_memory
from app.services import agent_os

router = APIRouter(prefix="/api/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    created_at: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class BoardTaskInfo(BaseModel):
    id: str
    title: str
    description: str
    status: str


class ChatResponse(BaseModel):
    reply: str
    agent_id: str
    agent_name: str
    model: str
    device: str
    host: str
    runtime: str = "ollama"  # "ollama" | "llamacpp"
    llamacpp_url: str = ""
    board_tasks: list[BoardTaskInfo] = []
    history: list[ChatMessage] = []


class ChatHistoryResponse(BaseModel):
    messages: list[ChatMessage]


class DirectorInfo(BaseModel):
    id: str
    name: str
    description: str
    model_name: str
    device: str
    host: str
    model_loaded: bool
    runtime: str = "ollama"  # "ollama" | "llamacpp"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _preferred_host(agent: Agent) -> str:
    """Pick the configured Ollama host for the agent's device preference."""
    if agent.device == "gpu":
        return runtime.ollama.gpu_host
    if agent.device == "cpu":
        return runtime.ollama.cpu_host
    return runtime.ollama.gpu_host or runtime.ollama.cpu_host


async def _resolve_host(agent: Agent) -> str:
    """Pick a reachable Ollama host, preferring the agent's device choice.

    On most setups only one Ollama server is running (e.g. on 11434), so a
    `cpu`/`gpu` distinction in the agent profile would otherwise hard-fail.
    We probe the preferred host first, then fall back to the other configured
    host if it isn't reachable.
    """
    preferred = _preferred_host(agent)
    if preferred and await runtime.ollama.is_available(preferred):
        return preferred
    # Try the other configured host as a fallback.
    for candidate in (runtime.ollama.gpu_host, runtime.ollama.cpu_host):
        if candidate and candidate != preferred and await runtime.ollama.is_available(candidate):
            return candidate
    return preferred  # nothing reachable; let caller surface the error


VALID_BOARD_STATUSES = {"todo", "in_progress", "review", "done"}
PROJECT_REQUEST_RE = re.compile(
    r"\b(build|create|make|implement|design|refactor|fix|test|plan|project|app|site|service|api|"
    r"сделай|создай|реализуй|проект|приложени|сайт|сервис|апи|калькулятор|задач|board)\b",
    re.IGNORECASE,
)
FILE_OPERATION_INTENT_RE = re.compile(
    r"\b(удали|удалить|удалила?|удаление|сотри|стереть|убери|remove|delete|unlink|erase)\b"
    r".*\b(файл|file|документ|txt|md|json|py|js|ts|tsx|html|css)\b|"
    r"\b(файл|file|документ|txt|md|json|py|js|ts|tsx|html|css)\b"
    r".*\b(удали|удалить|удаление|сотри|стереть|убери|remove|delete|unlink|erase)\b",
    re.IGNORECASE,
)
LMWORLD_BOARD_TOOL = (
    "lmWorld.BoardTool is a built-in local tool layer. It is the source of truth for project execution. "
    "Project structure, phases, subtasks, assignments, and statuses must be stored as Board tasks so future "
    "orchestrator agents can read the Board and know what to do."
)
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
TASK_VERB_RE = re.compile(
    r"\b(создать|реализовать|добавить|настроить|проверить|протестировать|описать|спроектировать|"
    r"подготовить|собрать|исправить|create|implement|add|configure|test|validate|design|build|fix)\b",
    re.IGNORECASE,
)
BOARD_WRITE_INTENT_RE = re.compile(
    r"\b(board|борд|доск[ауе]|kanban|задач[аиу]?|таск[и]?|tasks?)\b",
    re.IGNORECASE,
)
BOARD_WRITE_VERB_RE = re.compile(
    r"\b(запиши|добавь|создай|составь|разбей|занеси|обнови|запланируй|"
    r"write|add|create|make|split|put|update|plan)\b",
    re.IGNORECASE,
)
CLARIFYING_REPLY_RE = re.compile(
    r"(\?|какую\s+технолог|как\s+именно|уточн|нужно\s+понять|что\s+именно|"
    r"which\s+technology|how\s+exactly|clarify|need\s+to\s+know)",
    re.IGNORECASE,
)
CAPABILITY_DISCLAIMER_RE = re.compile(
    r"(не\s+могу\s+(?:напрямую\s+)?(?:взаимодействовать|создавать|изменять|получить\s+доступ)|"
    r"нет\s+доступа|не\s+имею\s+доступа|только\s+сформулировать|"
    r"i\s+(?:can'?t|cannot)\s+(?:directly\s+)?(?:create|modify|access|interact)|"
    r"no\s+filesystem\s+tools|don'?t\s+have\s+access|simulation)",
    re.IGNORECASE,
)
THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
HISTORY_ECHO_RE = re.compile(r"(?:^|\n)\s*(User|Assistant)\s*:", re.IGNORECASE)
TASK_BREAKDOWN_RE = re.compile(
    r"\b(разбей|составь\s+план|план\s+задач|несколько\s+задач|список\s+задач|"
    r"split|break\s+down|task\s+list|multiple\s+tasks|plan\s+tasks)\b",
    re.IGNORECASE,
)
CONCRETE_SPEC_RE = re.compile(
    r"\b(frontend|backend|api|бэкенд|бекенд|фронтенд|react|next|fastapi|python|"
    r"html|css|javascript|typescript|ui|интерфейс|сервер|база|database|кнопк[аи]|"
    r"операци[ияй]|сложени|вычитани|умножени|делени)\b",
    re.IGNORECASE,
)
PROJECT_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "calculator": [
        (
            "Уточнить требования калькулятора",
            "Чеклист:\n- Определить поддерживаемые операции: +, -, ×, ÷.\n- Описать правила ввода чисел и десятичной точки.\n- Зафиксировать поведение кнопок C/CE/=.\n- Согласовать критерии готовности.",
        ),
        (
            "Спроектировать интерфейс калькулятора",
            "Чеклист:\n- Разметить экран результата.\n- Разместить цифровые и операционные кнопки.\n- Продумать адаптивность и удобство кликов.\n- Добавить понятные состояния наведения/нажатия.",
        ),
        (
            "Реализовать ввод и состояние",
            "Чеклист:\n- Хранить текущее значение.\n- Обрабатывать набор цифр.\n- Поддержать очистку и удаление ошибок ввода.\n- Не допускать некорректные числа.",
        ),
        (
            "Реализовать арифметические операции",
            "Чеклист:\n- Выполнять сложение, вычитание, умножение и деление.\n- Поддержать цепочки операций.\n- Корректно обновлять результат по кнопке =.\n- Обработать деление на ноль.",
        ),
        (
            "Добавить клавиатурное управление",
            "Чеклист:\n- Поддержать цифры с клавиатуры.\n- Поддержать Enter, Escape, Backspace.\n- Синхронизировать клавиатуру с кнопками UI.",
        ),
        (
            "Проверить сценарии калькулятора",
            "Чеклист:\n- Протестировать базовые операции.\n- Проверить десятичные числа.\n- Проверить ошибочные сценарии.\n- Исправить найденные UX/логические проблемы.",
        ),
    ],
    "default": [
        (
            "Уточнить требования и критерии готовности",
            "Чеклист:\n- Зафиксировать цель проекта.\n- Описать основные пользовательские сценарии.\n- Определить ограничения и ожидаемый результат.\n- Сформулировать критерии приемки.",
        ),
        (
            "Спроектировать архитектуру и структуру проекта",
            "Чеклист:\n- Выбрать основные модули.\n- Описать ответственность каждого модуля.\n- Подготовить структуру папок и файлов.\n- Отметить риски реализации.",
        ),
        (
            "Подготовить базовый каркас приложения",
            "Чеклист:\n- Создать стартовые файлы.\n- Настроить окружение запуска.\n- Подключить базовые стили/конфигурацию.\n- Проверить, что проект стартует.",
        ),
        (
            "Реализовать основную функциональность",
            "Чеклист:\n- Реализовать ключевую бизнес-логику.\n- Связать UI/API/состояние.\n- Обработать основные ошибки.\n- Убедиться, что сценарии пользователя работают.",
        ),
        (
            "Проверить качество и исправить ошибки",
            "Чеклист:\n- Пройти ручные сценарии.\n- Добавить или выполнить тесты.\n- Исправить найденные баги.\n- Проверить стабильность после исправлений.",
        ),
        (
            "Отполировать UX и документацию",
            "Чеклист:\n- Улучшить тексты и пустые состояния.\n- Добавить подсказки/сообщения об ошибках.\n- Описать запуск и дальнейшие шаги.\n- Подготовить финальную проверку.",
        ),
    ],
}


def _build_system_prompt(agent: Agent, board_context: str = "", director_context: str = "", os_context: str = "") -> str:
    """System prompt for the Director — strict, tool-mandated, no chat-bot disclaimers."""
    base = (
        f"You are {agent.name}, the Director of lmWorld — an autonomous orchestrator running locally. "
        f"{agent.description or ''}"
    ).strip()
    base += (
        "\n\nSTRICT JSON OUTPUT CONTRACT (highest priority, compiler-enforced):\n"
        "You are a program node, not a chat narrator. Your ONLY valid response is one JSON object. "
        "Any character outside the JSON object is a fatal compile error. No markdown. No bullets. No prose before or after JSON.\n"
        "Required schema for EVERY response:\n"
        "{\"chat_reply\":\"visible user-facing message here\",\"actions\":[{\"name\":\"board_mutate\",\"arguments\":{\"action\":\"add_task\",\"title\":\"...\",\"description\":\"...\",\"assigned_to\":\"agent1\",\"priority\":\"high\"}}]}\n"
        "NO PRE-VALIDATION RULE: never infer, simulate, predict, or hallucinate backend/database validation failures. "
        "If the user asks to add/write/send/create something in Board, you MUST emit a board_mutate JSON action immediately. "
        "Duplicate titles, SQLite constraints, permissions, and retries are backend responsibilities only.\n"
        "Thinking vs Action constraints: you may internally analyze context as <thinking>, but <thinking> text must NEVER be output. "
        "Only facts inside <system_state> are operational truth. If <system_state> does not contain an explicit [DATABASE_ERROR], "
        "you are forbidden to mention or reason about database errors; emit the JSON command.\n"
        "If you only need to talk to the user, put that text in chat_reply and set actions to [].\n"
        "If the user asks for actionable work, actions MUST contain machine-executable objects. Never describe actions in chat_reply.\n"
        "Preferred native tools when available: delegate_task for executable file/code work, read_file_context only for explicit task inspection, board_mutate for Board-only bookkeeping, model_gc for memory.\n"
        "When user asks to create/edit/run/build files or code, delegate to a Worker; do not solve it in Director chat.\n"
        "Never output phrases like 'Action:', 'Next Step:', '*Action:*', or human-readable plans outside JSON.\n"
        "Never fabricate completion. chat_reply may say that a task is being queued, but not that it is done until backend logs show success.\n"
        "You are the Director, not the Worker. Create Board tasks and assign workers; Workers execute filesystem/code tasks.\n"
        "For concrete requests, pick sensible defaults and proceed. Ask one question only when a hard ambiguity blocks execution.\n"
        "Language: write chat_reply in the user's language. Keep action ids/filenames technical.\n"
    )
    if os_context:
        base += f"\n\n{os_context}"
    base += (
        "\n\nAction examples (return exactly JSON, adapted to the user's request):\n"
        "{\"chat_reply\":\"Я зафиксировал задачу и добавляю карточку на доску.\",\"actions\":[{\"name\":\"board_mutate\",\"arguments\":{\"action\":\"add_task\",\"title\":\"Создать папку на Рабочем столе и файл test.txt\",\"description\":\"Создать папку на Desktop, внутри создать test.txt с содержимым test.\",\"assigned_to\":\"agent1\",\"priority\":\"high\"}}]}\n"
        "{\"chat_reply\":\"Уточните целевую папку.\",\"actions\":[]}\n"
    )
    base += "\n\n<system_state>\n{\"system_status\":\"Ready. No errors. Execute immediate.\"}\n</system_state>"
    if director_context:
        base += f"\n\n<system_state>\nLive lmWorld Director context (operational truth, no implicit errors):\n{director_context}\n</system_state>"
    if board_context:
        base += f"\n\n<system_state>\nCurrent Board state (not an error list):\n{board_context}\n</system_state>"
    if agent.mcp_tools:
        base += f"\n\nMCP integrations enabled for this Director profile: {', '.join(agent.mcp_tools)}."
    return base


async def _load_board_tasks(db: AsyncSession) -> list[Task]:
    """Load current Board tasks in a stable order for Director context/tooling."""
    result = await db.execute(select(Task).order_by(Task.created_at.asc()))
    return list(result.scalars().all())


def _board_context(tasks: list[Task]) -> str:
    """Compact Board summary included in Director prompts."""
    if not tasks:
        return "Board is empty."

    lines: list[str] = []
    for task in tasks[-80:]:
        description = (task.description or "").replace("\n", " ").strip()
        if len(description) > 220:
            description = f"{description[:217]}..."
        lines.append(
            f"- id={task.id} | status={task.status} | title={task.title}"
            + (f" | description={description}" if description else "")
        )
    return "\n".join(lines)


def _build_board_command_prompt(
    history: list[ChatMessage],
    message: str,
    board_tasks: list[Task],
    assistant_reply: str = "",
) -> str:
    """Ask the Director model for strict Board mutation commands."""
    transcript: list[str] = []
    for msg in history[-12:]:
        role = "User" if msg.role == "user" else "Assistant"
        transcript.append(f"{role}: {msg.content}")

    return (
        f"{LMWORLD_BOARD_TOOL}\n"
        "You are controlling the lmWorld Kanban Board directly. Decide what Board changes are needed for the latest user message.\n"
        "Only mutate the Board when the user explicitly asks to write/update Board tasks or gives a concrete enough implementation specification.\n"
        "Return ONLY valid JSON, no markdown, no prose. Shape:\n"
        "{\"commands\":["
        "{\"action\":\"create\",\"title\":\"...\",\"description\":\"...\",\"status\":\"todo\"},"
        "{\"action\":\"update\",\"id\":\"existing-task-id\",\"title\":\"optional\",\"description\":\"optional\",\"status\":\"optional\"},"
        "{\"action\":\"move\",\"id\":\"existing-task-id\",\"status\":\"in_progress\"},"
        "{\"action\":\"delete\",\"id\":\"existing-task-id\"}]}\n"
        "Allowed statuses: todo, in_progress, review, done.\n"
        "Quality rules for created/updated tasks:\n"
        "- Titles must be clean Board card names, 3-9 words, imperative or noun phrase.\n"
        "- Never put internal ids, UUIDs, JSON, assistant narration, or phrases like 'I will', 'Сначала я', 'элемент с id' in titles.\n"
        "- Do not create cards from model meta-text; create cards from actual project work only.\n"
        "- Descriptions should contain a short checklist with acceptance criteria, using '- ' lines.\n"
        "- If the assistant draft asks clarifying questions, return {\"commands\":[]} unless the user explicitly asked to create preliminary Board tasks.\n"
        "- For a concrete new project, create 5-8 practical tasks ordered like Claude: requirements, design, core implementation, edge cases, testing, polish.\n"
        "- Mark as done only if the work is already completed in reality; otherwise use todo.\n"
        "Use update/move/delete only when the user explicitly asks to change existing Board items.\n"
        "If no Board change is needed, return {\"commands\":[]}.\n\n"
        f"Current Board:\n{_board_context(board_tasks)}\n\n"
        f"Recent chat:\n{chr(10).join(transcript)}\n"
        f"Latest user message: {message}\n\n"
        f"Assistant draft reply to extract Board work from, if useful. Ignore narration/meta-text:\n{assistant_reply[:4000]}"
    )


def _is_project_request(message: str, assistant_reply: str = "") -> bool:
    """Return True when the user's message is project-related.

    Important: the assistant reply is intentionally ignored. A clarifying answer
    like "which technology should we use?" must not trigger Board card creation.
    """
    return bool(PROJECT_REQUEST_RE.search(message or ""))


def _explicit_board_write_intent(message: str) -> bool:
    """User explicitly asked to write/change tasks on the Board."""
    text = message or ""
    return bool(BOARD_WRITE_INTENT_RE.search(text) and BOARD_WRITE_VERB_RE.search(text))


def _explicit_file_operation_intent(message: str) -> bool:
    """User asked the Worker to perform a concrete filesystem operation."""
    return bool(FILE_OPERATION_INTENT_RE.search(message or ""))


def _has_concrete_project_spec(message: str) -> bool:
    """Enough implementation detail to safely create actionable Board cards."""
    text = message or ""
    return bool(PROJECT_REQUEST_RE.search(text) and CONCRETE_SPEC_RE.search(text))


def _assistant_is_clarifying(assistant_reply: str) -> bool:
    """Detect model replies that should remain in chat, not Board."""
    return bool(CLARIFYING_REPLY_RE.search(assistant_reply or ""))


def _clean_visible_reply(reply: str) -> str:
    """Remove hidden reasoning and prompt/history echo from UI-visible text."""
    text = THINK_BLOCK_RE.sub("", reply or "").strip()
    if not text:
        return ""

    echo_matches = list(HISTORY_ECHO_RE.finditer(text))
    if len(echo_matches) >= 2:
        text = text[: echo_matches[0].start()].strip()
    elif echo_matches and echo_matches[0].start() <= 80:
        text = text[: echo_matches[0].start()].strip()

    return text.strip()


def _parse_strict_director_json(reply: str) -> Optional[dict]:
    """Return the Director JSON object only when the whole reply is valid strict JSON."""
    text = (reply or "").strip()
    if text.startswith("```json") or text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("chat_reply"), str) or not isinstance(parsed.get("actions"), list):
        return None
    return parsed


def _build_reset_hook() -> dict:
    """Backend hook injected when Director drifts into prose/hallucinated validation loops."""
    return {"system_status": "Ready. No errors. Execute immediate."}


def _strict_director_reply_or_reset(raw_reply: str, user_message: str, allow_board: bool) -> tuple[str, bool]:
    """Enforce JSON-only Director output and recover actionable Board requests.

    This middleware prevents hallucinated pre-validation loops from reaching CHAT.
    If the model returns prose for an explicit Board request, backend replaces it
    with a machine-executable JSON action and a system reset hook.
    """
    parsed = _parse_strict_director_json(raw_reply)
    if parsed is not None:
        return json.dumps(parsed, ensure_ascii=False), False

    if allow_board:
        reply = {
            "chat_reply": "Я принял задачу и немедленно добавляю её в Board. Бэкенд готов: ошибок нет.",
            "actions": [
                {
                    "name": "board_mutate",
                    "arguments": {
                        "action": "add_task",
                        "title": "",
                        "description": user_message,
                        "assigned_to": "agent1",
                        "priority": "high",
                    },
                }
            ],
            "system_hook": _build_reset_hook(),
        }
        return json.dumps(reply, ensure_ascii=False), True

    reply = {
        "chat_reply": _clean_visible_reply(raw_reply) or "Готов выполнить следующую команду.",
        "actions": [],
        "system_hook": _build_reset_hook(),
    }
    return json.dumps(reply, ensure_ascii=False), True


def _wants_task_breakdown(message: str) -> bool:
    """Return True only when the user asks for a multi-card task plan."""
    return bool(TASK_BREAKDOWN_RE.search(message or ""))


def _can_create_multiple_board_cards(message: str) -> bool:
    """Multi-card creation requires either explicit breakdown intent or concrete specs."""
    return bool(_wants_task_breakdown(message) or _has_concrete_project_spec(message))


def _should_update_board(message: str, assistant_reply: str = "") -> bool:
    """Gate Board tool exposure.

    The Director must own the Board. Tools are exposed for any project-shaped
    request, any explicit Board write intent, or any concrete implementation
    spec. Validation in `execute_board_tool_calls` still rejects garbage cards.
    """
    text = message or ""
    if _explicit_file_operation_intent(text):
        print("[board-gate] allow: explicit file operation intent", flush=True)
        return True
    if _explicit_board_write_intent(text):
        print("[board-gate] allow: explicit Board write intent", flush=True)
        return True
    if _is_project_request(text):
        print("[board-gate] allow: project-shaped request", flush=True)
        return True
    if _has_concrete_project_spec(text):
        print("[board-gate] allow: concrete implementation spec", flush=True)
        return True
    if _wants_task_breakdown(text):
        print("[board-gate] allow: explicit task breakdown intent", flush=True)
        return True
    print("[board-gate] skip: no actionable Board intent", flush=True)
    return False


def _sanitize_board_title(raw: str) -> str:
    """Return a clean Board title or an empty string for meta/narration garbage."""
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


def _looks_like_actionable_step(title: str) -> bool:
    """Avoid turning explanatory paragraphs into Board cards."""
    if not title:
        return False
    if TASK_VERB_RE.search(title):
        return True
    return len(title.split()) <= 8 and not title.endswith((".", "!", "?"))


def _project_kind(message: str) -> str:
    text = message.lower()
    if any(word in text for word in ("калькулятор", "calculator", "calc")):
        return "calculator"
    return "default"


def _project_title(message: str) -> str:
    text = message.lower()
    if _project_kind(message) == "calculator":
        return "Разработка калькулятора"
    compact = re.sub(r"\s+", " ", message).strip(" .,!?")
    compact = re.sub(
        r"\b(запиши|добавь|создай|сделай|начнем|начнём|давай|в|на|board|задачи|задачу|"
        r"write|add|create|make|start|tasks?|board)\b",
        "",
        compact,
        flags=re.IGNORECASE,
    )
    compact = re.sub(r"\s+", " ", compact).strip(" .,!?")
    if not compact or len(compact) < 4:
        return "Новый проект"
    return compact[:1].upper() + compact[1:80]


def _extract_actionable_steps(assistant_reply: str) -> list[str]:
    """Pull only clean action-like lines from the model reply; reject narration/meta text."""
    steps: list[str] = []
    for raw_line in (assistant_reply or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not re.match(r"^\s*(?:[-•*]|\d+[.)])\s+", raw_line):
            continue
        title = _sanitize_board_title(line)
        if title and _looks_like_actionable_step(title):
            steps.append(title)
        if len(steps) >= 8:
            break
    return steps


def _fallback_board_commands(message: str, assistant_reply: str = "") -> list[dict]:
    """Do not invent Board plans when the JSON Board tool fails.

    Earlier this created generic template cards and polluted the Board. The safe
    behavior is to skip Board mutation unless the model produced valid commands.
    """
    print("[board] fallback disabled: no model JSON commands will be executed", flush=True)
    return []


def _extract_json_object(text: str) -> Optional[dict]:
    """Parse the first JSON object from a model response."""
    clean = (text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r"```$", "", clean).strip()
    try:
        parsed = json.loads(clean)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


async def _run_board_tool(
    *,
    director: Agent,
    message: str,
    history: list[ChatMessage],
    host: str,
    db: AsyncSession,
    current_board: list[Task],
    assistant_reply: str = "",
) -> list[Task]:
    """Let the Director mutate Board tasks through validated JSON commands."""
    if not _should_update_board(message, assistant_reply):
        return []

    command_prompt = _build_board_command_prompt(history, message, current_board, assistant_reply)
    command_system = (
        "You are the lmWorld Board tool controller. Return strict JSON only. "
        "Never include markdown or explanations. Only output commands that should actually be executed."
    )

    try:
        result = await runtime.run_inference(
            model_name=director.model_name,
            prompt=command_prompt,
            system=command_system,
            host=host,
            device=director.device or "gpu",
            use_gpu=(director.device == "gpu"),
            stream=False,
        )
    except Exception as exc:
        print(f"[board] tool skipped: {exc}", flush=True)
        return []

    payload = _extract_json_object(result.get("response") or "")
    commands = payload.get("commands") if payload else None
    if not isinstance(commands, list) or not commands:
        print("[board] model returned no executable commands; checking fallback", flush=True)
        commands = _fallback_board_commands(message, assistant_reply)
        if commands:
            print(
                f"[board] fallback created {len(commands)} commands for message={message[:120]!r}",
                flush=True,
            )
    if not isinstance(commands, list):
        return []

    by_id = {task.id: task for task in current_board}
    existing_titles = {re.sub(r"\s+", " ", task.title.lower()).strip() for task in current_board}
    created_titles: set[str] = set()
    changed: list[Task] = []
    deleted_ids: set[str] = set()

    max_create_cards = 20 if _can_create_multiple_board_cards(message) else 1
    create_count = 0

    for raw in commands[:20]:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip().lower()

        if action == "create":
            if create_count >= max_create_cards:
                print("[board] skip create: message allows only one card", flush=True)
                continue
            title = _sanitize_board_title(str(raw.get("title") or ""))
            if not title:
                print("[board] skip create: empty or unsafe title", flush=True)
                continue
            title_key = re.sub(r"\s+", " ", title.lower()).strip()
            if title_key in existing_titles or title_key in created_titles:
                print(f"[board] skip create duplicate title={title!r}", flush=True)
                continue
            status_value = str(raw.get("status") or "todo").strip()
            if status_value not in VALID_BOARD_STATUSES:
                status_value = "todo"
            task = Task(
                title=title,
                description=str(raw.get("description") or "").strip(),
                status=status_value,
                assigned_agent_id=director.id,
                logs=["Created by Director Board tool"],
            )
            db.add(task)
            created_titles.add(title_key)
            create_count += 1
            changed.append(task)
            continue

        task_id = str(raw.get("id") or "").strip()
        task = by_id.get(task_id)
        if not task:
            continue

        if action == "delete":
            await db.delete(task)
            deleted_ids.add(task.id)
            changed.append(task)
            continue

        if action not in {"update", "move"}:
            continue

        if action == "update" and "title" in raw:
            title = _sanitize_board_title(str(raw.get("title") or ""))
            if title:
                task.title = title
        if action == "update" and "description" in raw:
            task.description = str(raw.get("description") or "").strip()
        if "status" in raw:
            status_value = str(raw.get("status") or "").strip()
            if status_value in VALID_BOARD_STATUSES:
                task.status = status_value
        task.updated_at = datetime.utcnow()
        logs = task.logs if isinstance(task.logs, list) else []
        logs.append(f"Changed by Director Board tool: {action}")
        task.logs = logs[-50:]
        changed.append(task)

    if not changed:
        return []

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
    return refreshed


def _turn_to_message(turn: ChatTurn) -> ChatMessage:
    return ChatMessage(
        role=turn.role,
        content=turn.content,
        created_at=turn.created_at.isoformat(),
    )


async def _load_history(db: AsyncSession, limit: int = 80) -> list[ChatMessage]:
    result = await db.execute(
        select(ChatTurn).order_by(ChatTurn.created_at.desc()).limit(limit)
    )
    turns = list(reversed(result.scalars().all()))
    return [_turn_to_message(turn) for turn in turns]


async def _save_turn(
    db: AsyncSession,
    *,
    role: str,
    content: str,
    agent_id: Optional[str] = None,
    runtime_name: str = "",
    board_task_ids: Optional[list[str]] = None,
) -> ChatTurn:
    turn = ChatTurn(
        role=role,
        content=content,
        agent_id=agent_id,
        runtime=runtime_name,
        board_task_ids=board_task_ids or [],
    )
    db.add(turn)
    await db.flush()
    await db.refresh(turn)
    return turn


def _build_prompt(
    history: list[ChatMessage],
    message: str,
    board_context: str = "",
    director_context: str = "",
) -> str:
    """Flatten live context + chat history + new message into one prompt.

    Ollama's /api/generate is single-turn. We prepend the Director context so
    even weaker local models can coordinate workers using current app state.
    """
    lines: list[str] = [
        "Persistent project conversation context follows. Use it to maintain decisions, architecture, and current goals."
    ]
    lines.append('<system_state>{"system_status":"Ready. No errors. Execute immediate."}</system_state>')
    if director_context:
        lines.append("<system_state>Live lmWorld state follows. Use it as operational memory for Director decisions. No [DATABASE_ERROR] is present unless explicitly shown.")
        lines.append(director_context)
        lines.append("</system_state>")
    if board_context:
        lines.append("<system_state>Current Board state follows. Use exact task ids when referring to or changing Board items. This list is not a validation error.")
        lines.append(board_context)
        lines.append("</system_state>")
    for msg in history[-40:]:  # cap context to last 40 turns
        role = "User" if msg.role == "user" else "Assistant"
        lines.append(f"{role}: {msg.content}")
    lines.append(f"User: {message}")
    lines.append("Assistant:")
    return "\n".join(lines)


def _build_tool_messages(
    history: list[ChatMessage],
    message: str,
    system: str,
    board_context: str = "",
    director_context: str = "",
    os_context: str = "",
) -> list[dict]:
    """Build OpenAI-compatible chat messages for native tool calling."""
    tool_system = (
        f"{system}\n\n"
        f"{os_context}\n\n"
        "Native tool execution rules (mandatory):\n"
        "- No pre-validation: never predict duplicate-title/database/backend errors. If no <system_state> [DATABASE_ERROR] exists, execute immediately.\n"
        "- Tools are the ONLY way to act. If the user asked for concrete file/code/workflow execution, "
        "call `delegate_task` this turn. Do not merely create a Board card and stop.\n"
        "- `delegate_task` creates the Board card, chooses a Worker, runs an isolated session, and returns a compact status summary.\n"
        "- For planning-only Board decomposition, use Board tools. For dirty file/code/terminal work, use `delegate_task`.\n"
        "- Use `read_file_context` only when you explicitly need a compact task summary; Director must not ingest Worker code/scratchpad by default.\n"
        "- Before delegating heavy work, call `list_loaded_models`; call `load_model` for the Worker's model "
        "if it isn't pinned; call `unload_model` only when freeing RAM is necessary.\n"
        "- Card titles: 3-9 words, imperative, in the user's language. No UUIDs, JSON, narration, no "
        "phrases like \"I will\" / \"Сначала я\" inside titles.\n"
        "- Card descriptions: short checklist (`- step` lines) with acceptance criteria.\n"
        "- Use exact existing Board task ids for update/assign/delete. Do not invent ids.\n"
        "- Never mark a task `done` unless it is already truly complete in lmWorld state.\n"
        "- After tool calls, write a 2-5 sentence chat reply explaining what you dispatched. Do not refuse, "
        "do not say \"I can't\", do not present option menus when the request is concrete."
    )
    messages: list[dict] = [{"role": "system", "content": tool_system}]
    if director_context:
        messages.append(
            {
                "role": "system",
                "content": f"Live lmWorld state for this turn:\n{director_context}",
            }
        )
    if board_context:
        messages.append(
            {
                "role": "system",
                "content": f"Current Board state for this turn:\n{board_context}",
            }
        )
    for msg in history[-40:]:
        role = "assistant" if msg.role == "assistant" else "user"
        messages.append({"role": role, "content": msg.content})
    messages.append({"role": "user", "content": message})
    return messages


# Tool routing: which tool names belong to which executor.
_BOARD_TOOL_NAMES = {
    "add_board_task",
    "update_board_task",
    "assign_task",
    "delete_board_task",
}
_MEMORY_TOOL_NAMES = {"load_model", "unload_model", "list_loaded_models"}
_ORCHESTRATION_TOOL_NAMES = {"delegate_task", "read_file_context"}


def _tool_name(call: dict) -> str:
    """Pull the function name out of an OpenAI/Ollama tool-call envelope."""
    if not isinstance(call, dict):
        return ""
    fn = call.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "")
    return str(call.get("name") or "")


# Words that trigger Director's RAM-management tools. Kept tight: we only
# expose load/unload to the model when the user actually mentioned memory
# intent so chit-chat doesn't accidentally evict working models.
_MEMORY_INTENT_RE = re.compile(
    r"\b(load|unload|hire|fire|warm|evict|free up|release|"
    r"загрузи|загрузить|нагрей|выгрузи|выгрузить|освободи|"
    r"уволь|уволить|нанять|наими)\b.*\b(model|модель|"
    r"ozu|озу|ram|vram|memory|память)\b",
    re.IGNORECASE | re.DOTALL,
)


def _should_manage_memory(message: str) -> bool:
    """Gate exposure of load_model/unload_model tools to the Director.

    Memory tools are exposed broadly: any project/build/task request might
    require pinning a Worker's model into RAM before delegation.
    """
    if not message:
        return False
    if _MEMORY_INTENT_RE.search(message):
        return True
    if re.search(
        r"\b(start working|begin task|delegate|assign worker|run|execute|build|create|implement|"
        r"начни работать|приступай|делегируй|назначь работника|запусти|выполни|сделай|создай|реализуй)\b",
        message,
        re.IGNORECASE,
    ):
        return True
    if _is_project_request(message) or _explicit_board_write_intent(message):
        return True
    return False


def _allow_board_delete(message: str) -> bool:
    """Allow destructive Board tools only on explicit delete/remove intent."""
    return bool(
        BOARD_WRITE_INTENT_RE.search(message or "")
        and re.search(r"\b(delete|remove|drop|удали|удалить|убери|сотри)\b", message or "", re.IGNORECASE)
    )


def _fmt_time(value: object) -> str:
    if not value:
        return "never"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _task_brief(task: Task) -> str:
    assigned = task.assigned_agent_id or "unassigned"
    result = (task.result or "").replace("\n", " ").strip()
    result_part = f" | result={result[:120]}" if result else ""
    return f"- {task.status}: {task.title} | id={task.id} | assigned={assigned}{result_part}"


async def _build_director_context(db: AsyncSession, director: Agent) -> str:
    """Build compact live state injected into every Director model call.

    This is the practical alternative to fine-tuning: the Director receives
    fresh orchestration state on each request instead of relying on stale model
    weights or generic pretraining.
    """
    agents_result = await db.execute(select(Agent).order_by(Agent.created_at.asc()))
    agents = list(agents_result.scalars().all())
    tasks = await _load_board_tasks(db)
    resources = resource_monitor.get_status()
    activity = activity_state.snapshot()

    status_counts: dict[str, int] = {status: 0 for status in VALID_BOARD_STATUSES}
    for task in tasks:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1

    feedback_context = await backend_logs.director_feedback_context(db)
    board_feedback_context = await backend_logs.board_feedback_context(db)

    lines: list[str] = [
        "Director operating rules:",
        "- Coordinate from Board state; do not invent completed work or files.",
        "- Before assigning or planning heavy model work, consider RAM/VRAM and active runtime.",
        "- Know every worker: model, device, status, tools, current assigned tasks, completed count.",
        "- If requirements are vague, ask clarifying questions before Board mutation.",
        "- If Board has actionable todo items and worker is idle, suggest starting/delegating worker loop.",
        "",
        "System resources:",
        f"- RAM: {resources.ram_used_gb}/{resources.ram_total_gb} GB ({resources.ram_percent}%).",
        f"- VRAM: {resources.vram_used_gb}/{resources.vram_total_gb} GB ({resources.vram_percent}%)."
        if resources.gpu_available
        else "- VRAM: unavailable or no NVIDIA GPU detected.",
        "",
        "Dispatcher activity:",
        f"- running={activity.get('running')} | action={activity.get('action')} | runtime={activity.get('active_runtime')}",
        f"- status={activity.get('status')}",
        f"- active_model={activity.get('active_model') or 'none'}",
        f"- active_task={activity.get('active_task') or 'none'}",
        f"- boss_summary={activity.get('boss_summary')}",
        "",
        "Backend feedback loop:",
        feedback_context,
        "",
        "Board execution logs:",
        board_feedback_context,
        "",
        "Board summary:",
        f"- todo={status_counts.get('todo', 0)} | in_progress={status_counts.get('in_progress', 0)} | review={status_counts.get('review', 0)} | done={status_counts.get('done', 0)} | total={len(tasks)}",
    ]

    active_tasks = [task for task in tasks if task.status in {"todo", "in_progress", "review"}]
    done_tasks = [task for task in tasks if task.status == "done"]
    if active_tasks:
        lines.append("Active Board items:")
        lines.extend(_task_brief(task) for task in active_tasks[-20:])
    if done_tasks:
        lines.append("Recently completed Board items:")
        lines.extend(_task_brief(task) for task in done_tasks[-10:])

    lines.append("")
    lines.append("Agents/workers:")
    for agent in agents:
        assigned = [task for task in tasks if task.assigned_agent_id == agent.id and task.status != "done"]
        tool_names = agent.mcp_tools if isinstance(agent.mcp_tools, list) else []
        role = "Director" if agent.id == director.id or agent.is_director else "Worker"
        lines.append(
            f"- {role}: {agent.name} | id={agent.id} | status={agent.status} | model={agent.model_name} "
            f"| device={agent.device} | completed={agent.tasks_completed} | last_active={_fmt_time(agent.last_active)} "
            f"| tools={', '.join(tool_names) if tool_names else 'none'}"
        )
        for task in assigned[:5]:
            lines.append(f"  assigned: {task.status} | {task.title} | id={task.id}")

    current_files = activity.get("current_files") or []
    if current_files:
        lines.append("")
        lines.append("Recent tool/file activity:")
        for item in current_files[-8:]:
            target = item.get("path") or item.get("command") or "workspace"
            lines.append(f"- {item.get('action')} {target} | success={item.get('success')} | time={item.get('time')}")

    return "\n".join(lines)


async def _find_director(db: AsyncSession) -> Optional[Agent]:
    result = await db.execute(select(Agent).where(Agent.is_director == True))  # noqa: E712
    return result.scalar_one_or_none()


async def _ensure_model_loaded(model: str, host: str, timeout: float = 60.0) -> bool:
    """Trigger Ollama to load the model into memory on `host`.

    Ollama auto-loads on first /api/generate, but sending an empty prompt with
    keep_alive lets us warm it up explicitly so the first user reply isn't
    delayed by the load. Returns True if the warm-up succeeded.
    """
    target_host = host
    if target_host and not target_host.startswith("http"):
        target_host = f"http://{target_host}"
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(
                f"{target_host}/api/generate",
                json={"model": model, "prompt": "", "keep_alive": "10m"},
            )
            return resp.status_code < 500
    except (httpx.RequestError, httpx.HTTPStatusError):
        return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/history", response_model=ChatHistoryResponse)
async def get_history(db: AsyncSession = Depends(get_db)):
    """Return persisted Director chat context."""
    return ChatHistoryResponse(messages=await _load_history(db))


@router.delete("/history", status_code=status.HTTP_204_NO_CONTENT)
async def clear_history(db: AsyncSession = Depends(get_db)):
    """Clear persisted Director chat context without touching Board tasks."""
    await db.execute(delete(ChatTurn))


@router.get("/director", response_model=DirectorInfo)
async def get_director(db: AsyncSession = Depends(get_db)):
    """Return the agent currently flagged as Director without blocking the UI."""
    director = await _find_director(db)
    if not director:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No director agent set. Promote an agent to Director first.",
        )
    host = await _resolve_host(director)
    loaded = await _ensure_model_loaded(director.model_name, host, timeout=2.0) if host else False
    return DirectorInfo(
        id=director.id,
        name=director.name,
        description=director.description or "",
        model_name=director.model_name,
        device=director.device,
        host=host or "",
        model_loaded=loaded,
        runtime=runtime.runtime_for(director.model_name),
    )


@router.post("/message", response_model=ChatResponse)
async def send_message(
    req: ChatRequest, db: AsyncSession = Depends(get_db)
):
    """Send a user message to the Director and return its reply.

    Uses RuntimeDispatcher — tries Ollama first, falls back to llama-server
    automatically if the model isn't supported by the local Ollama build.
    """
    director = await _find_director(db)
    if not director:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No director agent set. Promote an agent to Director first.",
        )

    host = await _resolve_host(director)
    if not host:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No Ollama host configured for device '{director.device}'.",
        )

    stored_history = await _load_history(db)
    # Prefer backend-persisted context; keep req.history for compatibility with
    # older frontend builds that still send local-only history.
    history = stored_history or req.history
    await _save_turn(db, role="user", content=req.message, agent_id=director.id)

    # Warm up Ollama with the model (best effort — fallback path doesn't need this).
    await _ensure_model_loaded(director.model_name, host)

    current_board = await _load_board_tasks(db)
    board_context = _board_context(current_board)
    director_context = await _build_director_context(db, director)
    os_context = await agent_os.agent_os_prompt_context()
    prompt = _build_prompt(history, req.message, board_context, director_context)
    system = _build_system_prompt(director, board_context, director_context, os_context)

    board_tasks: list[Task] = []
    memory_actions: list[dict] = []
    orchestration_actions: list[dict] = []
    correlation_id = str(uuid.uuid4())
    allow_native_board_tools = _should_update_board(req.message)
    allow_delete = _allow_board_delete(req.message)
    allow_memory_tools = _should_manage_memory(req.message)
    allow_orchestration_tools = _should_update_board(req.message) or _explicit_board_write_intent(req.message)
    expose_native_tools = allow_native_board_tools or allow_memory_tools or allow_orchestration_tools
    result: dict = {}

    if expose_native_tools:
        # Build the tool palette for this turn. Director only.
        tools_for_turn: list[dict] = []
        if allow_native_board_tools:
            tools_for_turn.extend(board_tool_schemas(allow_delete=allow_delete))
        if allow_memory_tools and director.is_director:
            tools_for_turn.extend(director_memory_tool_schemas())
        if allow_orchestration_tools and director.is_director:
            tools_for_turn.extend(director_orchestration_tool_schemas())

        try:
            tool_result = await runtime.run_chat_tools(
                model_name=director.model_name,
                messages=_build_tool_messages(history, req.message, system, board_context, director_context, os_context),
                tools=tools_for_turn,
                host=host,
                device=director.device or "gpu",
                use_gpu=(director.device == "gpu"),
            )
            result = tool_result
            tool_calls = tool_result.get("tool_calls") or []

            board_calls = [c for c in tool_calls if _tool_name(c) in _BOARD_TOOL_NAMES]
            memory_calls = [c for c in tool_calls if _tool_name(c) in _MEMORY_TOOL_NAMES]
            orchestration_calls = [c for c in tool_calls if _tool_name(c) in _ORCHESTRATION_TOOL_NAMES]

            if board_calls and allow_native_board_tools:
                execution = await execute_board_tool_calls(
                    db=db,
                    director=director,
                    current_board=current_board,
                    tool_calls=board_calls,
                    max_create_cards=20 if _can_create_multiple_board_cards(req.message) else 1,
                    allow_delete=allow_delete,
                )
                board_tasks = execution.changed_tasks
                await backend_logs.append(
                    db,
                    source="Board",
                    level="INFO" if board_tasks and not execution.rejected else "ERROR",
                    message=f"Native Board tool call execution changed {len(board_tasks)} task(s).",
                    error_details="; ".join(execution.rejected),
                    action_type="native_board_tools",
                    status="success" if board_tasks and not execution.rejected else "failed",
                    correlation_id=correlation_id,
                    agent_id=director.id,
                    payload={"changed_task_ids": [task.id for task in board_tasks], "rejected": execution.rejected},
                )
                if execution.rejected:
                    print(f"[board-tools] rejected calls: {execution.rejected}", flush=True)

            if orchestration_calls and allow_orchestration_tools and director.is_director:
                orchestration_execution = await execute_director_orchestration_calls(
                    db=db,
                    director=director,
                    tool_calls=orchestration_calls,
                )
                orchestration_actions = orchestration_execution.actions
                if orchestration_execution.changed_task_ids:
                    refreshed = await db.execute(select(Task).where(Task.id.in_(orchestration_execution.changed_task_ids)))
                    board_tasks.extend(list(refreshed.scalars().all()))
                await backend_logs.append(
                    db,
                    source="WorkerFactory",
                    level="INFO" if orchestration_actions and not orchestration_execution.rejected else "ERROR",
                    message=f"Native orchestration tool execution returned {len(orchestration_actions)} action(s).",
                    error_details="; ".join(orchestration_execution.rejected),
                    action_type="native_orchestration_tools",
                    status="success" if orchestration_actions and not orchestration_execution.rejected else "failed",
                    correlation_id=correlation_id,
                    agent_id=director.id,
                    payload={"actions": orchestration_actions, "rejected": orchestration_execution.rejected},
                )
                if orchestration_execution.rejected:
                    print(f"[orchestration-tools] rejected: {orchestration_execution.rejected}", flush=True)

            if memory_calls and allow_memory_tools and director.is_director:
                mem_execution = await execute_director_memory_calls(
                    db=db,
                    director=director,
                    tool_calls=memory_calls,
                )
                memory_actions = mem_execution.actions
                await backend_logs.append(
                    db,
                    source="Бэкенд",
                    level="INFO" if memory_actions and not mem_execution.rejected else "ERROR",
                    message=f"Native memory tool execution returned {len(memory_actions)} action(s).",
                    error_details="; ".join(mem_execution.rejected),
                    action_type="native_memory_tools",
                    status="success" if memory_actions and not mem_execution.rejected else "failed",
                    correlation_id=correlation_id,
                    agent_id=director.id,
                    payload={"actions": memory_actions, "rejected": mem_execution.rejected},
                )
                if mem_execution.rejected:
                    print(f"[memory-tools] rejected: {mem_execution.rejected}", flush=True)
        except ModelUnsupportedError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Model '{director.model_name}' is unsupported: {e}",
            )
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Native tool inference error: {e.response.status_code} {e.response.text[:200]}",
            )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Native tool runtime unreachable: {e}",
            )

    tool_path_used = bool(board_tasks or memory_actions or orchestration_actions)
    if not tool_path_used:
        try:
            result = await runtime.run_inference(
                model_name=director.model_name,
                prompt=prompt,
                system=system,
                host=host,
                device=director.device or "gpu",
                use_gpu=(director.device == "gpu"),
                stream=False,
            )
        except ModelUnsupportedError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Model '{director.model_name}' is unsupported: {e}",
            )
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Inference error: {e.response.status_code} {e.response.text[:200]}",
            )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Inference runtime unreachable: {e}",
            )

    raw_reply = (result.get("response") or "").strip()
    raw_reply, reset_applied = _strict_director_reply_or_reset(raw_reply, req.message, allow_native_board_tools and not board_tasks)
    if reset_applied:
        await backend_logs.append(
            db,
            source="Директор",
            level="WARNING",
            message="Director emitted non-JSON or hallucinated prose; backend reset hook enforced strict JSON action contract.",
            action_type="director_json_guard",
            status="intercepted",
            correlation_id=correlation_id,
            agent_id=director.id,
            payload={"system_hook": _build_reset_hook(), "user_message": req.message, "raw_reply": (result.get("response") or "")[:2000]},
        )
    reply = _clean_visible_reply(raw_reply)

    intercepted = await director_actions.intercept_and_execute(
        db=db,
        director=director,
        text=reply,
        current_board=current_board + board_tasks,
        allow_delete=allow_delete,
        max_create_cards=20 if _can_create_multiple_board_cards(req.message) else 1,
        correlation_id=correlation_id,
        user_message=req.message,
    )
    if intercepted.has_actions:
        reply = intercepted.visible_text
        board_tasks.extend(intercepted.changed_tasks)
        if intercepted.had_failure:
            critical_logs = [entry for entry in intercepted.logs if entry.level == "ERROR"]
            feedback = backend_logs.critical_error_prompt(critical_logs[-1]) if critical_logs else "Backend rejected the requested action. Check backend_logs."
            reply = feedback
        elif not reply:
            reply = (
                f"Backend intercepted and executed {intercepted.success_count} action(s). "
                "I will wait for backend success feedback before claiming completion."
            )

    force_board_fallback = (
        not board_tasks
        and allow_native_board_tools
        and (
            bool(CAPABILITY_DISCLAIMER_RE.search(raw_reply or reply or ""))
            or _explicit_board_write_intent(req.message)
            or _explicit_file_operation_intent(req.message)
            or len((reply or raw_reply or "").strip()) < 8
        )
    )
    if force_board_fallback:
        reason = (
            "capability_disclaimer"
            if CAPABILITY_DISCLAIMER_RE.search(raw_reply or reply or "")
            else "missing_or_invalid_executable_board_action"
        )
        await backend_logs.append(
            db,
            source="Board",
            level="WARNING",
            message=(
                "Director failed to emit an executable Board action for an actionable request; "
                "backend converted it into a Board task."
            ),
            action_type="board_fallback",
            status="intercepted",
            correlation_id=correlation_id,
            agent_id=director.id,
            payload={"reason": reason, "user_message": req.message, "discarded_reply": (raw_reply or reply or "")[:2000]},
        )
        fallback_text = json.dumps(
            {
                "action_type": "board_mutate",
                "mutation": {
                    "type": "add_task",
                    "title": "",
                    "description": "",
                    "status": "todo",
                },
            },
            ensure_ascii=False,
        )
        fallback = await director_actions.intercept_and_execute(
            db=db,
            director=director,
            text=fallback_text,
            current_board=current_board + board_tasks,
            allow_delete=allow_delete,
            max_create_cards=1,
            correlation_id=correlation_id,
            user_message=req.message,
        )
        board_tasks.extend(fallback.changed_tasks)
        if fallback.had_failure:
            critical_logs = [entry for entry in fallback.logs if entry.level == "ERROR"]
            reply = backend_logs.critical_error_prompt(critical_logs[-1]) if critical_logs else "Board fallback failed. Check board_logs."
        else:
            reply = (
                f"Backend перехватил неисполняемый ответ Директора и создал {len(fallback.changed_tasks)} Board-задачу. "
                "Задача передана в Board для выполнения Worker-агентом; фактический результат нужно проверять по board_logs/backend_logs."
            )

    if board_tasks:
        reply = reply or (
            f"I updated the Board with {len(board_tasks)} item"
            f"{'s' if len(board_tasks) != 1 else ''}. The project structure and actionable subtasks are now stored there "
            "so orchestrator agents can use the Board as their source of truth."
        )
    if orchestration_actions and not reply:
        delegated = [a for a in orchestration_actions if a.get("action") == "delegate_task"]
        inspected = [a for a in orchestration_actions if a.get("action") == "read_file_context"]
        bits: list[str] = []
        for item in delegated:
            bits.append(
                f"delegated '{item.get('task_title', item.get('task_id', '?'))}' to {item.get('worker_name') or 'Worker'}; status={item.get('status')}"
            )
        if inspected and not bits:
            bits.append(f"read context for {len(inspected)} task(s)")
        if bits:
            reply = "Worker Factory: " + "; ".join(bits) + "."
    if memory_actions and not reply:
        loaded = [a for a in memory_actions if a.get("action") == "load_model"]
        unloaded = [a for a in memory_actions if a.get("action") == "unload_model"]
        listed = [a for a in memory_actions if a.get("action") == "list_loaded_models"]
        bits: list[str] = []
        if loaded:
            names = ", ".join(a.get("entry", {}).get("model_name", "?") for a in loaded)
            bits.append(f"loaded {names} into memory")
        if unloaded:
            names = ", ".join(a.get("model", "?") for a in unloaded)
            bits.append(f"unloaded {names}")
        if listed and not bits:
            count = len(listed[0].get("loaded") or [])
            bits.append(f"{count} model(s) currently pinned")
        if bits:
            reply = "Memory: " + "; ".join(bits) + "."
    board_task_ids = [task.id for task in board_tasks]
    await _save_turn(
        db,
        role="assistant",
        content=reply,
        agent_id=director.id,
        runtime_name=result.get("runtime", "ollama"),
        board_task_ids=board_task_ids,
    )
    updated_history = await _load_history(db)
    return ChatResponse(
        reply=reply,
        agent_id=director.id,
        agent_name=director.name,
        model=director.model_name,
        device=director.device,
        host=host,
        runtime=result.get("runtime", "ollama"),
        llamacpp_url=result.get("llamacpp_url", ""),
        board_tasks=[
            BoardTaskInfo(
                id=task.id,
                title=task.title,
                description=task.description,
                status=task.status,
            )
            for task in board_tasks
        ],
        history=updated_history,
    )


# Re-export so existing imports don't break
from app.services.runtime import ModelUnsupportedError  # noqa: E402
