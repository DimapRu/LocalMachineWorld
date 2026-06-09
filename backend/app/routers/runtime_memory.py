"""Runtime memory (RAM/VRAM) management endpoints.

Surface the Director-controlled load/unload service so frontends and
external orchestrators can introspect and control which local models
are pinned in memory.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services.runtime_memory import MemoryBudgetError, runtime_memory


router = APIRouter(prefix="/api/runtime/memory", tags=["runtime-memory"])


class LoadModelRequest(BaseModel):
    model_name: str = Field(..., description="Exact model id to pin in memory.")
    device: str = Field("gpu", description="gpu | cpu | auto")
    agent_id: Optional[str] = Field(None, description="Worker this load is for.")
    estimated_gb: float = Field(0.0, description="Optional size estimate.")
    keep_alive_seconds: Optional[int] = Field(
        None, description="How long to keep warm with no activity."
    )
    note: str = Field("", description="Short reason this model is pinned.")


class UnloadModelRequest(BaseModel):
    model_name: str = Field(..., description="Exact model id to evict.")
    reason: str = Field("", description="Optional audit reason.")


@router.get("")
async def list_loaded_models():
    """Return all currently pinned models with metadata."""
    return {"loaded": await runtime_memory.list_loaded()}


@router.post("/load")
async def load_model(req: LoadModelRequest):
    """Pin `model_name` into memory ('hire')."""
    try:
        entry = await runtime_memory.load_model(
            model_name=req.model_name,
            device=req.device,
            agent_id=req.agent_id,
            keep_alive_seconds=req.keep_alive_seconds,
            estimated_gb=req.estimated_gb,
            note=req.note,
        )
    except MemoryBudgetError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"load_model failed: {type(e).__name__}: {e}",
        )
    return {"loaded": True, "entry": entry}


@router.post("/unload")
async def unload_model(req: UnloadModelRequest):
    """Evict `model_name` from memory ('fire'). Idempotent."""
    try:
        result = await runtime_memory.unload_model(model_name=req.model_name)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"unload_model failed: {type(e).__name__}: {e}",
        )
    return result
