"""Native tool schemas surfaced to Worker agents, plus the safe executor.

Worker tools are the executor side of lmWorld's Director/Worker split. The
Director decomposes user requests into Board subtasks and assigns each to a
Worker. The Worker then runs an OpenAI-compatible chat-with-tools loop using
THESE schemas so the model can read/write files, list directories, run
commands, ask questions, log results, and (if permitted) delete files.

Each tool is gated by a `perm_*` flag on the Worker's Agent row. Tools the
Worker doesn't have permission for are simply not exposed to the model.

The executor:
  * parses tool-call JSON arguments safely
  * dispatches each call to the matching backend handler
  * routes filesystem ops through services.worker_fs (sandbox + size caps)
  * logs results back to the task's TaskContext for Director visibility
  * returns one OpenAI `role="tool"` message per call so the loop can continue
"""
from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Task, TaskContextKind
from app.services import task_context as task_context_service
from app.services import worker_fs


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI / Ollama-native function-calling format).
# ---------------------------------------------------------------------------

WORKER_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file from disk. Path must be absolute and "
                "inside the user's home directory or the project workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to read.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a UTF-8 text file. Parent directories "
                "are created automatically. Path must be absolute and inside "
                "the user's home directory or the project workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to write to.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file contents to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    "list_dir": {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "List the immediate children of an absolute directory path. "
                "Returns entries with name, path, is_dir, and size."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute directory path.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    "delete_file": {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Delete a single regular file. Directories cannot be deleted "
                "via this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the file to delete.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    "run_command": {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command. Output is captured (stdout, stderr, "
                "exit code). Long-running commands are killed by a timeout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Full command line to execute.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional absolute working directory.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Hard timeout in seconds (default 60, max 300).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    "log_progress": {
        "type": "function",
        "function": {
            "name": "log_progress",
            "description": (
                "Append a short progress note to this task's context so the "
                "Director can see what you're doing without interrupting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "Short progress text (one or two sentences).",
                    },
                },
                "required": ["note"],
            },
        },
    },
    "ask_director": {
        "type": "function",
        "function": {
            "name": "ask_director",
            "description": (
                "Ask the Director a clarifying question. Use sparingly — only "
                "when blocked by missing information you cannot infer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to surface to the Director.",
                    },
                },
                "required": ["question"],
            },
        },
    },
    "submit_result": {
        "type": "function",
        "function": {
            "name": "submit_result",
            "description": (
                "Submit the final result for this task and stop. Call this "
                "exactly once when the task is complete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Concise outcome summary for the Director.",
                    },
                },
                "required": ["summary"],
            },
        },
    },
}


# Map each tool name to the permission flag that gates it.
TOOL_PERMISSION_MAP: dict[str, str] = {
    "read_file": "perm_read",
    "list_dir": "perm_read",
    "write_file": "perm_write",
    "delete_file": "perm_write",
    "run_command": "perm_execute",
    "log_progress": "perm_question",   # always-on conceptually; gated by question perm
    "ask_director": "perm_question",
    "submit_result": "perm_question",  # always allowed; permission used as a soft gate
}


# Tools every Worker should always have, regardless of perm flags, because the
# loop cannot terminate cleanly without them.
ALWAYS_ON_TOOLS: tuple[str, ...] = ("log_progress", "submit_result")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

@dataclass
class ToolCallResult:
    """Outcome of executing a single tool call."""

    tool_call_id: str
    name: str
    content: str          # JSON-encoded payload returned to the model
    submitted: bool = False  # True iff the call was `submit_result`
    asked: bool = False      # True iff the call was `ask_director`
    args: Optional[dict[str, Any]] = None  # normalized arguments, for audit logs only


def tools_for_worker(worker: Agent) -> list[dict[str, Any]]:
    """Return the OpenAI-format tool schemas the Worker is allowed to use."""
    allowed: list[dict[str, Any]] = []
    for name, schema in WORKER_TOOL_SCHEMAS.items():
        if name in ALWAYS_ON_TOOLS:
            allowed.append(schema)
            continue
        perm = TOOL_PERMISSION_MAP.get(name)
        if perm and bool(getattr(worker, perm, False)):
            allowed.append(schema)
    return allowed


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_call(call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Return (name, args, tool_call_id) from a raw tool-call dict."""
    fn = call.get("function") or {}
    name = str(fn.get("name") or call.get("name") or "").strip()
    args = _parse_args(fn.get("arguments") if "arguments" in fn else call.get("arguments"))
    tool_call_id = str(call.get("id") or call.get("tool_call_id") or "")
    return name, args, tool_call_id


def _ok(payload: dict[str, Any]) -> str:
    payload.setdefault("ok", True)
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str, **extra: Any) -> str:
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Individual tool implementations
# ---------------------------------------------------------------------------

async def _do_read_file(args: dict[str, Any]) -> str:
    path = str(args.get("path") or "").strip()
    if not path:
        return _err("path is required")
    try:
        rp, text = worker_fs.safe_read_text(path)
    except worker_fs.WorkerFsError as exc:
        return _err(str(exc))
    return _ok({"path": rp.display, "content": text, "bytes": len(text.encode("utf-8"))})


async def _do_write_file(args: dict[str, Any]) -> str:
    path = str(args.get("path") or "").strip()
    content = args.get("content")
    if not path:
        return _err("path is required")
    if content is None:
        return _err("content is required")
    if not isinstance(content, str):
        content = str(content)
    try:
        rp = worker_fs.safe_write_text(path, content)
        written = rp.path.read_text(encoding="utf-8")
    except worker_fs.WorkerFsError as exc:
        return _err(str(exc))
    except OSError as exc:
        return _err(f"write verification failed: {exc}")
    if written != content:
        return _err(
            "write verification failed: file content on disk does not match requested content",
            path=rp.display,
            bytes_written=len(written.encode("utf-8")),
        )
    return _ok({"path": rp.display, "bytes_written": len(content.encode("utf-8")), "verified": True})


async def _do_list_dir(args: dict[str, Any]) -> str:
    path = str(args.get("path") or "").strip()
    if not path:
        return _err("path is required")
    try:
        rp, entries = worker_fs.safe_list_dir(path)
    except worker_fs.WorkerFsError as exc:
        return _err(str(exc))
    return _ok({"path": rp.display, "entries": entries, "count": len(entries)})


async def _do_delete_file(args: dict[str, Any]) -> str:
    path = str(args.get("path") or "").strip()
    if not path:
        return _err("path is required")
    try:
        rp = worker_fs.safe_delete(path)
    except worker_fs.WorkerFsError as exc:
        return _err(str(exc))
    if rp.path.exists():
        return _err("delete verification failed: file still exists after delete", path=rp.display)
    return _ok({"path": rp.display, "deleted": True, "verified": True})


def _normalize_model_command(command: str) -> str:
    """Translate common Unix shell snippets emitted by local models on Windows."""
    if not command:
        return command
    if worker_fs.os.name != "nt":
        return command

    normalized = command
    home = worker_fs._user_home()
    desktop = home / "Desktop"
    normalized = normalized.replace("~/Desktop", str(desktop))
    normalized = normalized.replace("~/desktop", str(desktop))

    # Handle the frequent pattern:
    # mkdir -p <dir> && touch <file> && echo 'text' > <file>
    import re

    match = re.fullmatch(
        r"\s*mkdir\s+-p\s+(.+?)\s*&&\s*touch\s+(.+?)\s*&&\s*echo\s+(['\"])(.*?)\3\s*>\s*(.+?)\s*",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        dir_path, touch_path, _quote, content, out_path = match.groups()
        dir_path = worker_fs._normalize_model_path(dir_path.strip())
        touch_path = worker_fs._normalize_model_path(touch_path.strip())
        out_path = worker_fs._normalize_model_path(out_path.strip())

        def cmd_quote(value: str) -> str:
            return '"' + value.replace('"', '""') + '"'

        safe_content = content.replace("^", "^^").replace("&", "^&").replace("<", "^<").replace(">", "^>")
        return (
            f"mkdir {cmd_quote(dir_path)} 2>nul & "
            f"type nul > {cmd_quote(touch_path)} & "
            f"> {cmd_quote(out_path)} echo {safe_content}"
        )

    return normalized


async def _do_run_command(args: dict[str, Any]) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        return _err("command is required")
    command = _normalize_model_command(command)
    cwd_raw = args.get("cwd")
    cwd: Optional[str] = None
    if cwd_raw:
        try:
            rp = worker_fs.resolve_path(str(cwd_raw))
            cwd = rp.display
        except worker_fs.WorkerFsError as exc:
            return _err(f"cwd rejected: {exc}")
    try:
        timeout = int(args.get("timeout_seconds") or 60)
    except (TypeError, ValueError):
        timeout = 60
    timeout = max(1, min(timeout, 300))

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        return _err(f"failed to start command: {exc}")

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return _err(f"command timed out after {timeout}s", command=command)

    def _decode(b: bytes) -> str:
        text = b.decode("utf-8", errors="replace")
        # Cap each stream so a chatty command does not blow up the prompt.
        if len(text) > 4000:
            text = text[:4000] + "\n…[truncated]"
        return text

    payload = {
        "command": command,
        "cwd": cwd,
        "exit_code": proc.returncode,
        "stdout": _decode(stdout_b or b""),
        "stderr": _decode(stderr_b or b""),
    }
    if proc.returncode != 0:
        return _err(f"command failed with exit code {proc.returncode}", **payload)
    return _ok(payload)


async def _do_log_progress(
    args: dict[str, Any], *, db: AsyncSession, task: Task, worker: Agent
) -> str:
    note = str(args.get("note") or "").strip()
    if not note:
        return _err("note is required")
    await task_context_service.append(
        db,
        task_id=task.id,
        author_agent_id=worker.id,
        kind=TaskContextKind.note.value,
        content=note,
    )
    return _ok({"logged": True})


async def _do_ask_director(
    args: dict[str, Any], *, db: AsyncSession, task: Task, worker: Agent
) -> str:
    question = str(args.get("question") or "").strip()
    if not question:
        return _err("question is required")
    await task_context_service.append(
        db,
        task_id=task.id,
        author_agent_id=worker.id,
        kind=TaskContextKind.question.value,
        content=question,
    )
    return _ok({"asked": True, "question": question})


async def _do_submit_result(
    args: dict[str, Any], *, db: AsyncSession, task: Task, worker: Agent
) -> str:
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return _err("summary is required")
    await task_context_service.append(
        db,
        task_id=task.id,
        author_agent_id=worker.id,
        kind=TaskContextKind.result.value,
        content=summary,
    )
    return _ok({"submitted": True, "summary": summary})


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def execute_worker_tool_calls(
    *,
    db: AsyncSession,
    task: Task,
    worker: Agent,
    tool_calls: list[dict[str, Any]],
) -> list[ToolCallResult]:
    """Execute every call in `tool_calls` and return one result per call.

    Permission violations are surfaced as `ok: false` payloads — the model
    sees the rejection and can adapt rather than the loop crashing.
    """
    results: list[ToolCallResult] = []
    for raw_call in tool_calls or []:
        name, args, call_id = _normalize_call(raw_call)
        if not name:
            results.append(ToolCallResult(call_id, "", _err("malformed tool call"), args=args))
            continue

        if name == "question":
            # Text-tool fallback models often follow the system prompt wording
            # (`question`) instead of the real tool name (`ask_director`).
            name = "ask_director"

        if name not in WORKER_TOOL_SCHEMAS:
            results.append(ToolCallResult(call_id, name, _err(f"unknown tool: {name}"), args=args))
            continue

        # Permission gate (always-on tools bypass).
        if name not in ALWAYS_ON_TOOLS:
            perm = TOOL_PERMISSION_MAP.get(name)
            if perm and not bool(getattr(worker, perm, False)):
                results.append(
                    ToolCallResult(
                        call_id,
                        name,
                        _err(f"permission denied: {perm} required for {name}"),
                        args=args,
                    )
                )
                continue

        try:
            if name == "read_file":
                content = await _do_read_file(args)
            elif name == "write_file":
                content = await _do_write_file(args)
            elif name == "list_dir":
                content = await _do_list_dir(args)
            elif name == "delete_file":
                content = await _do_delete_file(args)
            elif name == "run_command":
                content = await _do_run_command(args)
            elif name == "log_progress":
                content = await _do_log_progress(args, db=db, task=task, worker=worker)
            elif name == "ask_director":
                content = await _do_ask_director(args, db=db, task=task, worker=worker)
            elif name == "submit_result":
                content = await _do_submit_result(args, db=db, task=task, worker=worker)
            else:
                content = _err(f"unhandled tool: {name}")
        except Exception as exc:  # noqa: BLE001 — surface to the model
            content = _err(f"tool crashed: {exc}")

        results.append(
            ToolCallResult(
                tool_call_id=call_id,
                name=name,
                content=content,
                submitted=(name == "submit_result"),
                asked=(name == "ask_director"),
                args=args,
            )
        )
    return results


def to_tool_messages(results: list[ToolCallResult]) -> list[dict[str, Any]]:
    """Convert ToolCallResult list into OpenAI-format `role=tool` messages."""
    msgs: list[dict[str, Any]] = []
    for r in results:
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": r.tool_call_id,
                "name": r.name,
                "content": r.content,
            }
        )
    return msgs


__all__ = [
    "WORKER_TOOL_SCHEMAS",
    "TOOL_PERMISSION_MAP",
    "ALWAYS_ON_TOOLS",
    "ToolCallResult",
    "tools_for_worker",
    "execute_worker_tool_calls",
    "to_tool_messages",
]
