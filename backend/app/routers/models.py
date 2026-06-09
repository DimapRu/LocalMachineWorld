"""Models catalog endpoint (Ollama models)."""

import asyncio

import httpx
from fastapi import APIRouter

from app.services.ollama import ollama_service
from app.config import settings

router = APIRouter(prefix="/api/models", tags=["models"])


# Hosts we attempt to reach to determine if the user is online.
# We only need a tiny HEAD/GET to verify connectivity.
_ONLINE_CHECK_URLS = [
    "https://registry.ollama.ai/",
    "https://huggingface.co/",
    "https://github.com/",
]
_ONLINE_CHECK_TIMEOUT = 3.0


async def _check_online() -> bool:
    """Return True if at least one known host is reachable.

    Tries the Ollama registry first (most relevant for our app), then falls
    back to HuggingFace and GitHub. Any successful response counts as online.
    """
    async with httpx.AsyncClient(timeout=_ONLINE_CHECK_TIMEOUT, trust_env=False) as client:
        for url in _ONLINE_CHECK_URLS:
            try:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code < 500:
                    return True
            except (httpx.RequestError, httpx.HTTPStatusError, asyncio.TimeoutError):
                continue
    return False


@router.get("/internet")
async def internet_status():
    """Return whether the user has working internet access."""
    online = await _check_online()
    return {"online": online}


@router.get("")
async def list_models():
    """List all available models from Ollama instances + online status."""
    gpu_models = await ollama_service.list_models_with_tool_support(settings.ollama_gpu_host)
    cpu_models = await ollama_service.list_models_with_tool_support(settings.ollama_cpu_host)
    online = await _check_online()

    return {
        "online": online,
        "gpu": {
            "host": settings.ollama_gpu_host,
            "models": gpu_models,
        },
        "cpu": {
            "host": settings.ollama_cpu_host,
            "models": cpu_models,
        },
    }


@router.get("/status")
async def ollama_status():
    """Check Ollama server availability."""
    gpu_available = await ollama_service.is_available(settings.ollama_gpu_host)
    cpu_available = await ollama_service.is_available(settings.ollama_cpu_host)

    return {
        "gpu": {
            "host": settings.ollama_gpu_host,
            "available": gpu_available,
        },
        "cpu": {
            "host": settings.ollama_cpu_host,
            "available": cpu_available,
        },
    }
