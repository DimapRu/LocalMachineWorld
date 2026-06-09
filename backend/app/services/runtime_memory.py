"""Director-controlled RAM/VRAM management for local models.

The Director "hires" a model into RAM (`load_model`) when it needs a Worker
to perform a task and "fires" it (`unload_model`) when it's no longer needed.
This service persists a registry of currently-loaded models in
`runtime_memory` so the Director can introspect what is warm and make
informed decisions when budgeting RAM/VRAM.

Backends:
  * Ollama: a no-prompt /api/generate call with `keep_alive` warms the model
    in RAM. Sending `keep_alive: 0` evicts it. This is the lightest path for
    Ollama-served models and matches Ollama's documented behavior.
  * llama.cpp: pinning a model means starting a `llama-server` instance via
    `LlamaCppService.start_server`; unloading means `stop_server`.

The Director can pin/unpin without going through the chat surface by calling
the dedicated REST endpoints, and through native tools wired into chat.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

import httpx
from sqlalchemy import select, delete

from app.database import async_session
from app.models import RuntimeMemoryEntry
from app.services.activity import activity_state
from app.services.llamacpp import get_llamacpp
from app.services.ollama import ollama_service
from app.services.resources import resource_monitor
from app.services.runtime import runtime


class MemoryBudgetError(RuntimeError):
    """Raised when the requested model would not fit in available RAM/VRAM."""


class RuntimeMemoryService:
    """Singleton controlling model residency in RAM/VRAM.

    Stateless wrt. process restarts: the registry is rebuilt opportunistically
    as the Director re-pins models. We do not try to introspect Ollama's
    internal model cache because doing so reliably across versions is brittle;
    we instead maintain our own intent-level record.
    """

    _DEFAULT_KEEP_ALIVE = 600  # seconds; Director can override per pin

    def __init__(self) -> None:
        # Per-model lock prevents racing load/unload of the same model.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, model_name: str) -> asyncio.Lock:
        lock = self._locks.get(model_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[model_name] = lock
        return lock

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    async def list_loaded(self) -> list[dict[str, Any]]:
        """Return all currently-pinned models with metadata."""
        async with async_session() as session:
            rows = (
                await session.execute(
                    select(RuntimeMemoryEntry).order_by(RuntimeMemoryEntry.loaded_at.desc())
                )
            ).scalars().all()
            return [self._serialize(entry) for entry in rows]

    async def is_loaded(self, model_name: str) -> bool:
        async with async_session() as session:
            entry = await session.get(RuntimeMemoryEntry, model_name)
            return entry is not None

    async def touch(self, model_name: str) -> None:
        """Mark a pinned model as just-used (extends LRU semantics)."""
        async with async_session() as session:
            entry = await session.get(RuntimeMemoryEntry, model_name)
            if entry is None:
                return
            entry.last_used_at = datetime.utcnow()
            await session.commit()

    # ------------------------------------------------------------------
    # Hire / fire
    # ------------------------------------------------------------------
    async def load_model(
        self,
        *,
        model_name: str,
        device: str = "gpu",
        agent_id: Optional[str] = None,
        keep_alive_seconds: Optional[int] = None,
        estimated_gb: float = 0.0,
        host: Optional[str] = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Pin `model_name` into memory.

        Refuses the request if `resource_monitor.can_load_model` says the
        machine can't fit it. Returns the registry row as a dict.
        """
        if not model_name:
            raise ValueError("model_name is required")

        keep_alive = (
            keep_alive_seconds
            if keep_alive_seconds is not None
            else self._DEFAULT_KEEP_ALIVE
        )
        use_gpu = device == "gpu"

        if estimated_gb > 0 and not resource_monitor.can_load_model(
            estimated_gb, use_gpu=use_gpu
        ):
            raise MemoryBudgetError(
                f"Not enough {'VRAM' if use_gpu else 'RAM'} to load "
                f"{model_name} (~{estimated_gb:.1f} GB)"
            )

        lock = self._lock_for(model_name)
        async with lock:
            # Decide which runtime serves this model. Reuse dispatcher cache
            # if we've seen it before; otherwise default to Ollama and let
            # the warm-up call confirm.
            chosen_runtime = runtime._resolved.get(model_name, "ollama")
            target_host: str = ""
            if chosen_runtime == "ollama":
                target_host = host or ollama_service.gpu_host
                if target_host and not target_host.startswith("http"):
                    target_host = f"http://{target_host}"
                await self._ollama_warmup(
                    model=model_name, host=target_host, keep_alive=keep_alive
                )
            else:
                # llama.cpp pinning means ensuring a server instance exists.
                # We don't auto-download here; the agent-creation path already
                # downloads GGUFs. Just rely on dispatcher's lazy bring-up.
                base = get_llamacpp().get_instance_base_url(model_name)
                target_host = base or ""

            entry_dict = await self._upsert_entry(
                model_name=model_name,
                runtime_name=chosen_runtime,
                host=target_host,
                device=device,
                agent_id=agent_id,
                estimated_gb=estimated_gb,
                keep_alive_seconds=keep_alive,
                note=note,
            )

        await activity_state.update_and_broadcast(
            current_action=f"loaded model {model_name}",
            tool_call=None,
        )
        return entry_dict

    async def unload_model(self, *, model_name: str) -> dict[str, Any]:
        """Evict `model_name` from memory if pinned.

        Returns `{"unloaded": bool, "model": str, "runtime": str|None}`.
        Idempotent: unloading something not pinned is a no-op success.
        """
        if not model_name:
            raise ValueError("model_name is required")

        lock = self._lock_for(model_name)
        async with lock:
            async with async_session() as session:
                entry = await session.get(RuntimeMemoryEntry, model_name)
                if entry is None:
                    return {"unloaded": False, "model": model_name, "runtime": None}
                runtime_name = entry.runtime
                target_host = entry.host

                if runtime_name == "ollama":
                    try:
                        await self._ollama_warmup(
                            model=model_name, host=target_host or None, keep_alive=0
                        )
                    except httpx.HTTPError:
                        # If Ollama is down, the model is already not in RAM.
                        pass
                else:
                    try:
                        await get_llamacpp().stop_server(model_name)
                    except Exception:
                        pass

                await session.execute(
                    delete(RuntimeMemoryEntry).where(
                        RuntimeMemoryEntry.model_name == model_name
                    )
                )
                await session.commit()

        await activity_state.update_and_broadcast(
            current_action=f"unloaded model {model_name}",
            tool_call=None,
        )
        return {"unloaded": True, "model": model_name, "runtime": runtime_name}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    async def _ollama_warmup(*, model: str, host: Optional[str], keep_alive: int) -> None:
        """Warm-load (keep_alive>0) or evict (keep_alive=0) via /api/generate."""
        target_host = host or ollama_service.gpu_host
        if target_host and not target_host.startswith("http"):
            target_host = f"http://{target_host}"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": "",
            "stream": False,
            "keep_alive": keep_alive,
        }
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.post(f"{target_host}/api/generate", json=payload)
            response.raise_for_status()

    async def _upsert_entry(
        self,
        *,
        model_name: str,
        runtime_name: str,
        host: str,
        device: str,
        agent_id: Optional[str],
        estimated_gb: float,
        keep_alive_seconds: int,
        note: str,
    ) -> dict[str, Any]:
        async with async_session() as session:
            entry = await session.get(RuntimeMemoryEntry, model_name)
            now = datetime.utcnow()
            if entry is None:
                entry = RuntimeMemoryEntry(
                    model_name=model_name,
                    runtime=runtime_name,
                    host=host,
                    device=device,
                    loaded_by_agent_id=agent_id,
                    estimated_gb=estimated_gb,
                    keep_alive_seconds=keep_alive_seconds,
                    note=note,
                    loaded_at=now,
                    last_used_at=now,
                )
                session.add(entry)
            else:
                entry.runtime = runtime_name
                entry.host = host or entry.host
                entry.device = device
                if agent_id:
                    entry.loaded_by_agent_id = agent_id
                if estimated_gb:
                    entry.estimated_gb = estimated_gb
                entry.keep_alive_seconds = keep_alive_seconds
                if note:
                    entry.note = note
                entry.last_used_at = now
            await session.commit()
            await session.refresh(entry)
            return self._serialize(entry)

    @staticmethod
    def _serialize(entry: RuntimeMemoryEntry) -> dict[str, Any]:
        return {
            "model_name": entry.model_name,
            "runtime": entry.runtime,
            "host": entry.host,
            "device": entry.device,
            "loaded_by_agent_id": entry.loaded_by_agent_id,
            "estimated_gb": entry.estimated_gb,
            "keep_alive_seconds": entry.keep_alive_seconds,
            "note": entry.note,
            "loaded_at": entry.loaded_at.isoformat() if entry.loaded_at else None,
            "last_used_at": entry.last_used_at.isoformat() if entry.last_used_at else None,
        }


runtime_memory = RuntimeMemoryService()
