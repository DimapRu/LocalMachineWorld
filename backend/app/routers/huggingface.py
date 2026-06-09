"""HuggingFace model catalog and hardware filtering endpoints."""

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.huggingface import hf_service
from app.services.resources import resource_monitor
from app.services.settings_store import set_setting

router = APIRouter(prefix="/api/hf-models", tags=["hf-models"])

HF_TOKEN_KEY = "hf_token"


class HFTokenUpdate(BaseModel):
    token: str


@router.get("/auth-status")
async def get_auth_status():
    """Check if HF token is configured."""
    return {
        "authenticated": hf_service.has_token(),
        "models_dir": str(hf_service.get_models_dir()),
    }


@router.post("/auth")
async def set_hf_token(data: HFTokenUpdate):
    """Set or update HuggingFace API token.

    The token is persisted to the local SQLite database (`app_settings` table)
    rather than `.env`. This avoids Windows codepage issues and survives
    uvicorn `--reload`. An empty token clears the stored value.
    """
    token = (data.token or "").strip()
    hf_service.set_token(token)
    await set_setting(HF_TOKEN_KEY, token)
    return {"authenticated": hf_service.has_token()}


@router.get("/recommended")
async def get_recommended_models(
    query: str = Query("", description="Search query"),
    limit: int = Query(100, ge=1, le=100),
):
    """Search HuggingFace GGUF models.

    Hire Agent can only download HuggingFace repos that contain GGUF files
    because Ollama's hf.co gateway and the llama.cpp fallback both need GGUF
    weights. Base safetensors repos are intentionally filtered out here.
    """
    return await hf_service.search_models(
        query=query or "llama",
        limit=limit,
        gguf_only=True,
    )


@router.get("/gpu-compatible")
async def get_gpu_compatible_models(
    query: str = Query("", description="Search query"),
    limit: int = Query(10, ge=1, le=50),
):
    """Get models that fit in current GPU VRAM."""
    resources = resource_monitor.get_status()

    if not resources.gpu_available:
        return {
            "models": [],
            "total": 0,
            "error": "No GPU detected",
            "available_vram_gb": 0,
        }

    available_vram = (resources.vram_total_gb or 0) - (resources.vram_used_gb or 0)

    return await hf_service.search_models(
        query=query,
        filter_vram_gb=available_vram,
        limit=limit,
    )


@router.get("/cpu-compatible")
async def get_cpu_compatible_models(
    query: str = Query("", description="Search query"),
    limit: int = Query(10, ge=1, le=50),
):
    """Get models that fit in available RAM."""
    resources = resource_monitor.get_status()
    available_ram = resources.ram_total_gb - resources.ram_used_gb

    return await hf_service.search_models(
        query=query,
        filter_ram_gb=available_ram,
        limit=limit,
    )


# Curated Ollama catalog with realistic Q4_K_M sizes (the default Ollama
# quantization). `params_b` is in billions; `size_gb` is the on-disk weight
# size in Q4_K_M and is also a good proxy for required VRAM/RAM at runtime.
# Sources: ollama.com/library tags + measured pulls.
OLLAMA_CATALOG: list[dict] = [
    # Tiny / always fits
    {"id": "qwen2.5:0.5b",     "params_b": 0.5,  "size_gb": 0.4},
    {"id": "tinyllama:latest", "params_b": 1.1,  "size_gb": 0.7},
    {"id": "llama3.2:1b",      "params_b": 1.2,  "size_gb": 0.8},
    {"id": "qwen2.5:1.5b",     "params_b": 1.5,  "size_gb": 1.0},
    {"id": "gemma2:2b",        "params_b": 2.6,  "size_gb": 1.6},
    {"id": "phi3:mini",        "params_b": 3.8,  "size_gb": 2.3},
    {"id": "llama3.2:3b",      "params_b": 3.2,  "size_gb": 2.0},
    # 7B class
    {"id": "mistral:7b",       "params_b": 7.2,  "size_gb": 4.1},
    {"id": "codellama:7b",     "params_b": 6.7,  "size_gb": 3.8},
    {"id": "qwen2.5:7b",       "params_b": 7.6,  "size_gb": 4.7},
    {"id": "llama3.1:8b",      "params_b": 8.0,  "size_gb": 4.7},
    {"id": "llama3:8b",        "params_b": 8.0,  "size_gb": 4.7},
    {"id": "gemma2:9b",        "params_b": 9.2,  "size_gb": 5.4},
    {"id": "phi3:medium",      "params_b": 14.0, "size_gb": 7.9},
    # Mid
    {"id": "qwen2.5:14b",      "params_b": 14.7, "size_gb": 9.0},
    {"id": "qwen2.5:32b",      "params_b": 32.0, "size_gb": 19.9},
    {"id": "mixtral:8x7b",     "params_b": 46.7, "size_gb": 26.4},
    # Heavy
    {"id": "llama3.1:70b",     "params_b": 70.0, "size_gb": 40.0},
    {"id": "qwen2.5:72b",      "params_b": 72.7, "size_gb": 47.0},
    {"id": "mixtral:8x22b",    "params_b": 141.0, "size_gb": 80.0},
]

# Headroom for KV-cache, context, OS overhead. Q4_K_M weights + ~1 GB is the
# rule of thumb that matches what Ollama actually needs to run inference.
RUNTIME_OVERHEAD_GB = 1.0


def _filter_catalog_by_budget(budget_gb: float) -> list[str]:
    """Return Ollama model ids that fit within `budget_gb`, biggest first."""
    if budget_gb <= 0:
        return []
    fitting = [
        m for m in OLLAMA_CATALOG
        if m["size_gb"] + RUNTIME_OVERHEAD_GB <= budget_gb
    ]
    fitting.sort(key=lambda m: m["params_b"], reverse=True)
    return [m["id"] for m in fitting]


def _tier_key_for_vram(vram_gb: float) -> str:
    """Map a VRAM amount to the legacy `for_gpu_*` bucket the frontend uses."""
    if vram_gb >= 16:
        return "for_gpu_16gb"
    if vram_gb >= 12:
        return "for_gpu_12gb"
    if vram_gb >= 8:
        return "for_gpu_8gb"
    if vram_gb >= 6:
        return "for_gpu_6gb"
    return "for_gpu_4gb"


@router.get("/filter-options")
async def get_filter_options():
    """Hardware info + Ollama models filtered to what actually fits.

    The "biggest" memory pool wins: if the GPU has more usable VRAM than the
    CPU has free RAM, we filter against VRAM; otherwise against RAM. Models
    are sorted most-powerful-first so the dropdown leads with the strongest
    model the user can actually run.
    """
    resources = resource_monitor.get_status()

    free_vram = (resources.vram_total_gb or 0) - (resources.vram_used_gb or 0)
    free_ram = resources.ram_total_gb - resources.ram_used_gb

    # Pick the bigger pool. GPU strictly preferred when available and at
    # least as large, since inference is dramatically faster on VRAM.
    use_gpu = resources.gpu_available and free_vram >= max(free_ram, 0.0)
    budget = free_vram if use_gpu else free_ram

    fitting_ids = _filter_catalog_by_budget(budget)

    # Always offer a CPU-only safety net (smallest models) so the UI never
    # ends up with a completely empty dropdown on extremely constrained
    # machines.
    cpu_safety = _filter_catalog_by_budget(min(free_ram, 4.0)) or [
        "qwen2.5:0.5b",
        "tinyllama:latest",
        "llama3.2:1b",
    ]

    # Populate every legacy `for_gpu_*` tier with the same filtered list so
    # the existing frontend `buildRecommendedModels` picks it up regardless
    # of which tier it computes from the resource snapshot.
    suggestions = {
        "for_gpu_4gb":  fitting_ids,
        "for_gpu_6gb":  fitting_ids,
        "for_gpu_8gb":  fitting_ids,
        "for_gpu_12gb": fitting_ids,
        "for_gpu_16gb": fitting_ids,
        "for_cpu_only": cpu_safety,
    }

    return {
        "hardware": {
            "gpu": {
                "available": resources.gpu_available,
                "total_vram_gb": resources.vram_total_gb,
                "used_vram_gb": resources.vram_used_gb,
                "free_vram_gb": free_vram,
            },
            "cpu": {
                "total_ram_gb": resources.ram_total_gb,
                "used_ram_gb": resources.ram_used_gb,
                "free_ram_gb": free_ram,
            },
            "filter": {
                "source": "vram" if use_gpu else "ram",
                "budget_gb": round(budget, 2),
                "tier": _tier_key_for_vram(budget) if use_gpu else "for_cpu_only",
            },
        },
        "suggestions": suggestions,
    }


@router.get("/estimate")
async def estimate_model_size(
    model_id: str = Query(..., description="Model ID like 'meta-llama/Llama-3.1-8B-Instruct'")
):
    """Get size estimates for a model."""
    model_info = await hf_service.get_model_info(model_id)
    if not model_info:
        return {"error": "Model not found"}

    estimates = hf_service.estimate_model_size(model_info)

    return {
        "model_id": model_id,
        "params_billions": estimates["params_billions"],
        "size_estimates_gb": estimates["sizes_gb"],
        "recommended_quantization": estimates["recommended"],
    }