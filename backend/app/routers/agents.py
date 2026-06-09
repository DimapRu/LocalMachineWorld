"""Agent CRUD endpoints."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Agent
from app.schemas import AgentCreate, AgentResponse, AgentUpdate
from app.services.huggingface import HuggingFaceService
from app.services.llamacpp import get_llamacpp
from app.services.ollama import OllamaError, ollama_service
from app.services.websocket import ws_manager

router = APIRouter(prefix="/api/agents", tags=["agents"])


_UNSUPPORTED_GGUF_MARKERS = (
    "repository is not gguf",
    "is not gguf or is not compatible",
    "unsupported model",
    "unsupported architecture",
    "unknown model architecture",
    "no gguf",
    "not a gguf",
)


def _is_hf_like(model_name: str) -> bool:
    """Return True for HuggingFace-style model ids."""
    clean = _hf_repo_id(model_name)
    return "/" in clean


def _hf_repo_id(model_name: str) -> str:
    """Normalize hf.co/huggingface.co URLs or ids to `org/repo`.

    Quant tags like `:q4_k_m` are stripped because the direct GGUF fallback
    needs the HuggingFace repository id, not Ollama's hf.co tag syntax.
    """
    clean = (model_name or "").strip()
    for prefix in (
        "https://huggingface.co/",
        "http://huggingface.co/",
        "huggingface.co/",
        "https://hf.co/",
        "http://hf.co/",
        "hf.co/",
    ):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            break
    if ":" in clean:
        clean = clean.split(":", 1)[0]
    return clean.strip("/")


def _is_ollama_unsupported_error(exc: Exception) -> bool:
    """Detect Ollama errors that should fall back to direct GGUF download."""
    text = str(exc).lower()
    return any(marker in text for marker in _UNSUPPORTED_GGUF_MARKERS)


async def _broadcast_pull_progress(model: str, percent: int, info: str) -> None:
    """Broadcast pull progress to the office WebSocket."""
    try:
        await ws_manager.broadcast_office(
            {
                "type": "pull_progress",
                "model": model,
                "percent": max(0, min(100, percent)),
                "info": info,
            }
        )
    except Exception:
        pass


async def _pick_gguf_repo_and_file(repo_id: str) -> Optional[tuple[str, str, int]]:
    """Find a GGUF file in the repo or common `-GGUF` sibling repo.

    Returns `(repo_id, filename, size_bytes)` or None.
    """
    llamacpp = get_llamacpp()
    candidates = [repo_id]
    if not repo_id.lower().endswith("-gguf"):
        candidates.append(f"{repo_id}-GGUF")

    # Common community convention: TheBloke publishes GGUF mirrors for many
    # base repositories. Try it only after the official repo/sibling name.
    owner, _, name = repo_id.partition("/")
    if owner and name and not name.lower().endswith("-gguf"):
        candidates.append(f"TheBloke/{name}-GGUF")

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        pick = await llamacpp.pick_best_gguf(candidate)
        if pick:
            filename, size = pick
            return candidate, filename, size
    return None


async def _pull_via_llamacpp(model_name: str) -> str:
    """Download a HF GGUF directly for llama-server fallback.

    Returns the normalized HuggingFace GGUF repo id to store on the agent. If
    the original repo has no GGUF files, common `-GGUF` sibling repos are tried
    before returning a clean HTTP error.
    """
    repo_id = _hf_repo_id(model_name)
    if not repo_id or "/" not in repo_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Model '{model_name}' is not a HuggingFace repo id. "
                "Choose a model like 'Qwen/Qwen2.5-7B-Instruct-GGUF' "
                "or an Ollama-native tag like 'qwen2.5:7b'."
            ),
        )

    await _broadcast_pull_progress(repo_id, 0, "searching for GGUF")
    pick = await _pick_gguf_repo_and_file(repo_id)
    if not pick:
        await _broadcast_pull_progress(repo_id, 0, "no GGUF found")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Ollama cannot pull '{model_name}' because that repository is "
                "not GGUF-compatible, and the direct llama.cpp fallback could "
                f"not find a GGUF file in '{repo_id}' or '{repo_id}-GGUF'. "
                "Pick a GGUF repository explicitly, for example a repo ending "
                "with '-GGUF', or choose an Ollama-native model tag."
            ),
        )

    gguf_repo, filename, size = pick
    size_gb = size / (1024 ** 3) if size else 0
    info = f"downloading {filename}"
    if size_gb:
        info += f" ({size_gb:.1f} GB)"
    print(f"[agents] Direct GGUF fallback selected {gguf_repo}/{filename}")
    await _broadcast_pull_progress(gguf_repo, 0, info)

    last_pct = -1

    def progress(downloaded: int, total: int) -> None:
        nonlocal last_pct
        if total <= 0:
            return
        pct = int(downloaded * 100 / total)
        if pct == last_pct or pct < last_pct + 2:
            return
        last_pct = pct
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_broadcast_pull_progress(gguf_repo, pct, info))
        except RuntimeError:
            pass

    try:
        gguf_path = await get_llamacpp().download_gguf(
            repo_id=gguf_repo,
            filename=filename,
            progress_callback=progress,
            output_dir=Path(settings.models_path),
        )
    except Exception as exc:
        await _broadcast_pull_progress(gguf_repo, 0, "GGUF download failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to download GGUF '{gguf_repo}/{filename}': {exc}",
        ) from exc

    await _broadcast_pull_progress(gguf_repo, 100, "downloaded")
    print(f"[agents] Direct GGUF downloaded to {gguf_path}")
    return gguf_repo


async def _normalize_pull_target(agent_data: AgentCreate) -> str:
    """Normalize selected model into an Ollama pull target when applicable."""
    raw_model_name = agent_data.model_name.strip()
    pull_target = raw_model_name

    if agent_data.model_provider != "ollama" or "/" not in raw_model_name:
        return pull_target

    if raw_model_name.startswith(("hf.co/", "huggingface.co/", "https://hf.co/", "https://huggingface.co/")):
        return raw_model_name

    base = raw_model_name
    tag = ""
    if ":" in raw_model_name:
        base, tag = raw_model_name.split(":", 1)

    if tag:
        pull_target = f"hf.co/{raw_model_name}"
        print(f"[agents] HF id with explicit tag, pulling '{pull_target}'")
        return pull_target

    try:
        hf = HuggingFaceService()
        picked = await hf.pick_gguf_quant(base)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[agents] pick_gguf_quant failed: {exc}")
        picked = None

    if picked:
        picked_tag = picked.lower()
        pull_target = f"hf.co/{base}:{picked_tag}"
        print(
            f"[agents] HF id '{raw_model_name}' has GGUFs, "
            f"auto-picked quant '{picked_tag}' -> '{pull_target}'"
        )
    else:
        pull_target = f"hf.co/{raw_model_name}"
        print(
            f"[agents] HF id '{raw_model_name}' had no GGUF or "
            f"no known quant tag, pulling default: '{pull_target}'"
        )

    return pull_target


async def _ensure_ollama_installed() -> None:
    """Install bundled Ollama if the project-local binary is missing."""
    from app.routers.system import _download_and_install_ollama

    ollama_exe = Path(settings.root_dir) / "ollama" / "ollama.exe"
    if ollama_exe.exists():
        return

    print("[agents] Ollama not found, auto-installing...")
    result = await _download_and_install_ollama(
        Path(settings.root_dir),
        Path(settings.root_dir) / "ollama",
    )
    if result.get("status") not in ("installed", "already_installed"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to install Ollama: {result.get('error', 'unknown error')}",
        )


async def _ensure_model_available(agent_data: AgentCreate) -> str:
    """Download or verify the selected model and return the stored model id."""
    pull_target = await _normalize_pull_target(agent_data)

    if agent_data.model_provider != "ollama":
        return pull_target

    await _ensure_ollama_installed()

    try:
        already = await ollama_service.has_model(pull_target)
    except OllamaError as exc:
        # If Ollama is not reachable but the model is HF-like, try the direct
        # llama-server path instead of failing the whole hire flow.
        if _is_hf_like(pull_target):
            print(
                f"[agents] Ollama is not reachable for '{pull_target}'. "
                "Trying direct GGUF download via llama-server..."
            )
            return await _pull_via_llamacpp(pull_target)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cannot reach Ollama: {exc}",
        ) from exc

    if already:
        print(f"[agents] Model '{pull_target}' already installed, skipping pull")
        return pull_target

    print(
        f"[agents] Model '{pull_target}' not installed, "
        "pulling from Ollama registry..."
    )
    try:
        await ollama_service.pull_model_blocking(pull_target)
        return pull_target
    except OllamaError as exc:
        if _is_ollama_unsupported_error(exc) and _is_hf_like(pull_target):
            print(
                f"[agents] Ollama can't serve '{pull_target}' "
                "(not GGUF). Falling back to direct GGUF download via llama-server..."
            )
            return await _pull_via_llamacpp(pull_target)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to download '{pull_target}': {exc}",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - safety net
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unexpected error while downloading '{pull_target}': {exc}",
        ) from exc


async def _ensure_some_director(db: AsyncSession, exclude_agent_id: str | None = None) -> Agent | None:
    """Promote the best available agent if no Director remains."""
    current_result = await db.execute(select(Agent).where(Agent.is_director == True))  # noqa: E712
    current = current_result.scalar_one_or_none()
    if current and current.id != exclude_agent_id:
        return current

    candidates_result = await db.execute(
        select(Agent)
        .where(Agent.id != exclude_agent_id if exclude_agent_id else True)
        .order_by(Agent.tasks_completed.desc(), Agent.created_at.asc())
    )
    replacement = candidates_result.scalars().first()
    if not replacement:
        return None
    replacement.is_director = True
    replacement.updated_at = datetime.utcnow()
    return replacement


@router.get("", response_model=list[AgentResponse])
async def list_agents(db: AsyncSession = Depends(get_db)):
    """Get all agents."""
    result = await db.execute(select(Agent))
    return result.scalars().all()


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(agent_data: AgentCreate, db: AsyncSession = Depends(get_db)):
    """Create a new agent and ensure its model is downloadable first."""
    model_name = await _ensure_model_available(agent_data)

    agent = Agent(
        name=agent_data.name,
        description=agent_data.description,
        model_name=model_name,
        model_provider=agent_data.model_provider,
        port=agent_data.port,
        device=agent_data.device,
        is_director=agent_data.is_director,
        mcp_tools=agent_data.mcp_tools,
        perm_read=agent_data.perm_read,
        perm_write=agent_data.perm_write,
        perm_execute=agent_data.perm_execute,
        perm_mcp=agent_data.perm_mcp,
        perm_mode=agent_data.perm_mode,
        perm_subtasks=agent_data.perm_subtasks,
        perm_question=agent_data.perm_question,
    )

    # If this agent is a director, demote the current director.
    if agent_data.is_director:
        result = await db.execute(select(Agent).where(Agent.is_director == True))  # noqa: E712
        current_director = result.scalar_one_or_none()
        if current_director:
            current_director.is_director = False

    db.add(agent)
    await db.flush()
    await db.refresh(agent)
    return agent


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    """Get agent by ID."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str, agent_data: AgentUpdate, db: AsyncSession = Depends(get_db)
):
    """Update agent profile."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    update_data = agent_data.model_dump(exclude_unset=True)

    # Handle director promotion.
    if update_data.get("is_director") is True and not agent.is_director:
        dir_result = await db.execute(select(Agent).where(Agent.is_director == True))  # noqa: E712
        current_director = dir_result.scalar_one_or_none()
        if current_director:
            current_director.is_director = False

    for field, value in update_data.items():
        setattr(agent, field, value)

    agent.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    """Delete (fire) an agent and keep a Director if any agent remains."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    was_director = bool(agent.is_director)
    await db.delete(agent)
    if was_director:
        await _ensure_some_director(db, exclude_agent_id=agent_id)


@router.post("/{agent_id}/hire", response_model=AgentResponse)
async def hire_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    """Hire an agent (set status to idle, ready to work)."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    agent.status = "idle"
    agent.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(agent)
    return agent


@router.post("/{agent_id}/fire", response_model=AgentResponse)
async def fire_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    """Fire an agent (release resources, set status to idle) and keep Director role filled."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    was_director = bool(agent.is_director)
    agent.status = "idle"
    agent.is_director = False
    agent.updated_at = datetime.utcnow()
    if was_director:
        await _ensure_some_director(db, exclude_agent_id=agent.id)
    await db.flush()
    await db.refresh(agent)
    return agent


@router.post("/{agent_id}/director", response_model=AgentResponse)
async def set_director(agent_id: str, db: AsyncSession = Depends(get_db)):
    """Promote an agent to director."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Demote current director.
    dir_result = await db.execute(select(Agent).where(Agent.is_director == True))  # noqa: E712
    current_director = dir_result.scalar_one_or_none()
    if current_director and current_director.id != agent_id:
        current_director.is_director = False

    agent.is_director = True
    agent.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(agent)
    return agent
