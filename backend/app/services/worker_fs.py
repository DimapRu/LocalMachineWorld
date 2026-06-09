"""Worker filesystem layer.

Workers operate with broader filesystem reach than the MCP HTTP routes:
they may read/write/list inside the user's home directory (Desktop, Documents,
project folders) and the project workspace itself. System-critical paths are
blocked. All resolution goes through `resolve_path` which:

- Expands ~ and environment variables
- Rejects relative paths (Workers must always pass absolute paths)
- Resolves symlinks via Path.resolve()
- Verifies the result lives inside one of the allow-listed roots
- Verifies the result does NOT live inside any of the deny-listed roots

This file is consumed by services/worker_tools.py — Workers cannot bypass it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# Maximum byte size we will read or write in a single tool call.
# Keeps runaway models from filling RAM with multi-GB files.
MAX_READ_BYTES: int = 2 * 1024 * 1024      # 2 MiB
MAX_WRITE_BYTES: int = 2 * 1024 * 1024     # 2 MiB
MAX_LIST_ENTRIES: int = 500


class WorkerFsError(Exception):
    """Raised when a Worker filesystem operation is rejected."""


@dataclass(frozen=True)
class ResolvedPath:
    """A path that has passed all safety checks."""

    path: Path
    root: Path  # which allow-listed root authorized this path

    @property
    def display(self) -> str:
        return str(self.path)


def _project_root() -> Path:
    """Best-effort project workspace root.

    The backend usually runs from `<project>/backend`, so two levels up is
    the project root. Falls back to cwd if that path does not exist.
    """
    here = Path(__file__).resolve()
    candidate = here.parents[3] if len(here.parents) >= 4 else Path.cwd()
    return candidate.resolve()


def _user_home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def allow_roots() -> list[Path]:
    """Filesystem roots that Workers are allowed to touch."""
    home = _user_home()
    project = _project_root()
    roots: list[Path] = [home, project]
    # Add common subfolders explicitly so they always resolve even if symlinked.
    for sub in ("Desktop", "Documents", "Downloads"):
        candidate = home / sub
        try:
            roots.append(candidate.resolve())
        except OSError:
            continue
    # De-duplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def deny_roots() -> list[Path]:
    """Paths Workers must never touch even if they live inside an allow root."""
    denied: list[Path] = []
    home = _user_home()
    # Hidden user-secret folders.
    for sub in (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config/gh"):
        denied.append(home / sub)
    # Windows-specific system-critical roots.
    if os.name == "nt":
        for env_var in ("WINDIR", "SYSTEMROOT", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            value = os.environ.get(env_var)
            if value:
                denied.append(Path(value))
    else:
        denied.extend(Path(p) for p in ("/etc", "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/boot", "/sys", "/proc"))
    # Project secrets.
    project = _project_root()
    denied.append(project / "backend" / ".env")
    denied.append(project / ".env")
    out: list[Path] = []
    seen: set[Path] = set()
    for p in denied:
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_model_path(raw: str) -> str:
    """Normalize common model-generated home paths for the current OS."""
    text = raw.strip().replace("\\", "/")
    home = _user_home()
    lowered = text.lower()

    if lowered in ("desktop", "~/desktop"):
        return str(home / "Desktop")
    if lowered.startswith("~/"):
        return str(home / text[2:])

    # Local models sometimes copy prompt examples literally. Replace common
    # placeholder homes with the real current user's home before safety checks.
    placeholder_prefixes = (
        "c:/users/<user>",
        "c:/users/%3cuser%3e",
        "/users/<user>",
        "/home/<user>",
    )
    for prefix in placeholder_prefixes:
        if lowered == prefix:
            return str(home)
        if lowered.startswith(prefix + "/"):
            suffix = text[len(prefix):].lstrip("/")
            return str(home / suffix) if suffix else str(home)

    # Local models often hallucinate Linux/macOS home paths while running on
    # Windows. Map them to the actual current user's home so write_file works.
    for prefix in ("/users/", "/home/"):
        if lowered.startswith(prefix):
            parts = text.strip("/").split("/", 2)
            if len(parts) >= 2:
                suffix = parts[2] if len(parts) > 2 else ""
                return str(home / suffix) if suffix else str(home)

    return raw.strip()


def resolve_path(raw: str) -> ResolvedPath:
    """Resolve a user-provided path string and verify it is safe.

    Raises WorkerFsError on any violation.
    """
    if not raw or not isinstance(raw, str):
        raise WorkerFsError("path must be a non-empty string")
    normalized = _normalize_model_path(raw)
    expanded = os.path.expandvars(os.path.expanduser(normalized))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        raise WorkerFsError(f"path must be absolute, got: {raw}")
    try:
        # strict=False so resolve works even when writing a brand-new file.
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WorkerFsError(f"could not resolve path: {exc}") from exc

    # Deny list takes precedence.
    for denied in deny_roots():
        if resolved == denied or _is_within(resolved, denied):
            raise WorkerFsError(f"path is in a protected location: {resolved}")

    # Must live inside one of the allow roots.
    for root in allow_roots():
        if resolved == root or _is_within(resolved, root):
            return ResolvedPath(path=resolved, root=root)

    raise WorkerFsError(
        f"path is outside the allowed Worker scope (home + project): {resolved}"
    )


def safe_read_text(raw: str, *, encoding: str = "utf-8") -> tuple[ResolvedPath, str]:
    """Read a text file with size guardrails."""
    rp = resolve_path(raw)
    if not rp.path.exists():
        raise WorkerFsError(f"file does not exist: {rp.path}")
    if not rp.path.is_file():
        raise WorkerFsError(f"path is not a regular file: {rp.path}")
    size = rp.path.stat().st_size
    if size > MAX_READ_BYTES:
        raise WorkerFsError(
            f"file is too large to read ({size} bytes > {MAX_READ_BYTES})"
        )
    try:
        return rp, rp.path.read_text(encoding=encoding, errors="replace")
    except OSError as exc:
        raise WorkerFsError(f"read failed: {exc}") from exc


def safe_write_text(
    raw: str,
    content: str,
    *,
    encoding: str = "utf-8",
    create_parents: bool = True,
    overwrite: bool = True,
) -> ResolvedPath:
    """Write a text file with size guardrails. Returns the resolved path."""
    if not isinstance(content, str):
        raise WorkerFsError("content must be a string")
    if len(content.encode(encoding, errors="replace")) > MAX_WRITE_BYTES:
        raise WorkerFsError(
            f"content is too large to write (> {MAX_WRITE_BYTES} bytes)"
        )
    rp = resolve_path(raw)
    if rp.path.exists():
        if rp.path.is_dir():
            raise WorkerFsError(f"target is a directory, not a file: {rp.path}")
        if not overwrite:
            raise WorkerFsError(f"file already exists: {rp.path}")
    if create_parents:
        try:
            rp.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkerFsError(f"could not create parent directory: {exc}") from exc
    try:
        rp.path.write_text(content, encoding=encoding)
    except OSError as exc:
        raise WorkerFsError(f"write failed: {exc}") from exc
    return rp


def safe_list_dir(raw: str) -> tuple[ResolvedPath, list[dict]]:
    """List a directory's immediate children with metadata."""
    rp = resolve_path(raw)
    if not rp.path.exists():
        raise WorkerFsError(f"directory does not exist: {rp.path}")
    if not rp.path.is_dir():
        raise WorkerFsError(f"path is not a directory: {rp.path}")
    entries: list[dict] = []
    try:
        iterator: Iterable = rp.path.iterdir()
    except OSError as exc:
        raise WorkerFsError(f"list failed: {exc}") from exc
    for child in iterator:
        if len(entries) >= MAX_LIST_ENTRIES:
            break
        try:
            stat = child.stat()
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "size": stat.st_size if child.is_file() else None,
                }
            )
        except OSError:
            # Unreadable entry — skip it but keep going.
            continue
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return rp, entries


def safe_delete(raw: str) -> ResolvedPath:
    """Delete a file (not a directory) with safety checks."""
    rp = resolve_path(raw)
    if not rp.path.exists():
        raise WorkerFsError(f"path does not exist: {rp.path}")
    if rp.path.is_dir():
        raise WorkerFsError(
            f"refusing to delete a directory (Workers may only delete files): {rp.path}"
        )
    try:
        rp.path.unlink()
    except OSError as exc:
        raise WorkerFsError(f"delete failed: {exc}") from exc
    return rp
