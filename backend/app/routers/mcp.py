"""MCP (Model Context Protocol) file system and terminal tools."""

import asyncio
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services.activity import activity_state

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# ─── Schemas ─────────────────────────────────────────────────────────────────


class ReadFileRequest(BaseModel):
    path: str = Field(..., min_length=1)
    offset: int = 0
    limit: Optional[int] = None


class ReadFileResponse(BaseModel):
    path: str
    content: str
    size: int


class WriteFileRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str


class WriteFileResponse(BaseModel):
    path: str
    size: int


class EditFileRequest(BaseModel):
    path: str = Field(..., min_length=1)
    old_string: str
    new_string: str


class EditFileResponse(BaseModel):
    path: str
    replaced: bool


class ListDirRequest(BaseModel):
    path: str = Field(..., min_length=1)


class DirEntry(BaseModel):
    name: str
    type: str  # "file" | "directory"
    size: Optional[int] = None


class ListDirResponse(BaseModel):
    path: str
    entries: list[DirEntry]


class DeleteRequest(BaseModel):
    path: str = Field(..., min_length=1)
    recursive: bool = False


class DeleteResponse(BaseModel):
    path: str
    deleted: bool


class TerminalRequest(BaseModel):
    command: str = Field(..., min_length=1)
    cwd: Optional[str] = None
    timeout: int = 30


class TerminalResponse(BaseModel):
    command: str
    cwd: Optional[str]
    stdout: str
    stderr: str
    returncode: int
    success: bool


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_path(path: str) -> Path:
    """Resolve path relative to workspace root, prevent directory traversal."""
    base = Path(os.getcwd()).resolve()
    target = (base / path).resolve()
    # Ensure target is within base directory
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path escapes workspace directory",
        )
    return target


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/read", response_model=ReadFileResponse)
async def read_file(req: ReadFileRequest):
    """Read file contents."""
    target = _resolve_path(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    content = target.read_text(encoding="utf-8", errors="replace")
    if req.limit:
        lines = content.splitlines()
        content = "\n".join(lines[req.offset : req.offset + req.limit])
    elif req.offset:
        lines = content.splitlines()
        content = "\n".join(lines[req.offset :])

    await activity_state.track_tool_action(action="read", path=req.path, success=True)
    return ReadFileResponse(path=req.path, content=content, size=target.stat().st_size)


@router.post("/write", response_model=WriteFileResponse)
async def write_file(req: WriteFileRequest):
    """Write or overwrite a file."""
    target = _resolve_path(req.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(req.content, encoding="utf-8")
    await activity_state.track_tool_action(action="write", path=req.path, success=True)
    return WriteFileResponse(path=req.path, size=target.stat().st_size)


@router.post("/edit", response_model=EditFileResponse)
async def edit_file(req: EditFileRequest):
    """Replace old_string with new_string in a file."""
    target = _resolve_path(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    content = target.read_text(encoding="utf-8", errors="replace")
    if req.old_string not in content:
        await activity_state.track_tool_action(action="edit", path=req.path, success=False)
        return EditFileResponse(path=req.path, replaced=False)

    new_content = content.replace(req.old_string, req.new_string, 1)
    target.write_text(new_content, encoding="utf-8")
    await activity_state.track_tool_action(action="edit", path=req.path, success=True)
    return EditFileResponse(path=req.path, replaced=True)


@router.post("/list", response_model=ListDirResponse)
async def list_directory(req: ListDirRequest):
    """List directory contents."""
    target = _resolve_path(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Directory not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    entries: list[DirEntry] = []
    for entry in target.iterdir():
        stat = entry.stat()
        entries.append(
            DirEntry(
                name=entry.name,
                type="directory" if entry.is_dir() else "file",
                size=stat.st_size if entry.is_file() else None,
            )
        )
    entries.sort(key=lambda e: (e.type != "directory", e.name.lower()))
    await activity_state.track_tool_action(action="list", path=req.path, success=True)
    return ListDirResponse(path=req.path, entries=entries)


@router.post("/delete", response_model=DeleteResponse)
async def delete_path(req: DeleteRequest):
    """Delete a file or directory."""
    target = _resolve_path(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    if target.is_dir():
        if req.recursive:
            shutil.rmtree(target)
        else:
            try:
                target.rmdir()
            except OSError:
                raise HTTPException(
                    status_code=400, detail="Directory not empty, use recursive=True"
                )
    else:
        target.unlink()

    await activity_state.track_tool_action(action="delete", path=req.path, success=True)
    return DeleteResponse(path=req.path, deleted=True)


@router.post("/terminal", response_model=TerminalResponse)
async def terminal_execute(req: TerminalRequest):
    """Execute a terminal command with optional working directory."""
    cwd = None
    if req.cwd:
        cwd_path = _resolve_path(req.cwd)
        if not cwd_path.exists():
            raise HTTPException(status_code=404, detail="Working directory not found")
        if not cwd_path.is_dir():
            raise HTTPException(status_code=400, detail="cwd is not a directory")
        cwd = str(cwd_path)

    try:
        proc = await asyncio.create_subprocess_shell(
            req.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=req.timeout
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        success = proc.returncode == 0
        await activity_state.track_tool_action(
            action="terminal",
            command=req.command,
            success=success,
        )
        return TerminalResponse(
            command=req.command,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode or 0,
            success=success,
        )
    except asyncio.TimeoutError:
        await activity_state.track_tool_action(
            action="terminal_timeout",
            command=req.command,
            success=False,
        )
        return TerminalResponse(
            command=req.command,
            cwd=cwd,
            stdout="",
            stderr=f"Command timed out after {req.timeout} seconds",
            returncode=-1,
            success=False,
        )
    except Exception as e:
        await activity_state.track_tool_action(
            action="terminal_error",
            command=req.command,
            success=False,
        )
        return TerminalResponse(
            command=req.command,
            cwd=cwd,
            stdout="",
            stderr=str(e),
            returncode=-1,
            success=False,
        )
