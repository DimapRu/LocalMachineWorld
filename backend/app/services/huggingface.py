"""HuggingFace API service for model catalog."""

import httpx
from pathlib import Path
from typing import Optional

from app.config import settings


class HuggingFaceService:
    """Client for HuggingFace Hub API."""

    BASE_URL = "https://huggingface.co/api"
    WEB_URL = "https://huggingface.co/models"

    def __init__(self):
        self.cache: dict = {}
        self.cache_ttl = 300  # 5 minutes
        # Token is loaded from SQLite at app startup (see lifespan in main.py).
        # `settings.hf_token` is only used as a one-time migration source on
        # first run and may be empty.
        self._token: str = (settings.hf_token or "").strip()

    @property
    def headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def set_token(self, token: str):
        """Update HF token at runtime."""
        self._token = token.strip()

    def has_token(self) -> bool:
        return bool(self._token)

    async def search_models(
        self,
        query: str = "",
        filter_vram_gb: Optional[float] = None,
        filter_ram_gb: Optional[float] = None,
        sort_by: str = "downloads",
        direction: str = "-",
        limit: int = 20,
        offset: int = 0,
        gguf_only: bool = False,
    ) -> dict:
        """Search models from HuggingFace with optional GGUF/hardware filters."""
        # HF API: sort=field (downloads sorts descending by default).
        # When used by Hire Agent, force GGUF in the search phrase so HF ranks
        # downloadable llama.cpp repos instead of base safetensors repos like
        # `Qwen/Qwen3.5-9B`, which Ollama/llama.cpp cannot pull directly.
        effective_query = (query or "").strip()
        if gguf_only and "gguf" not in effective_query.lower():
            effective_query = f"{effective_query} GGUF".strip()

        params: dict = {
            "search": effective_query,
            "sort": sort_by,
            "limit": min(max(limit * 3, limit), 100),
            "offset": offset,
        }
        if gguf_only:
            params["filter"] = "gguf"

        # Retry up to 3 times on transient network/HF errors. The HF Hub
        # is occasionally flaky and a single immediate retry usually fixes it.
        last_error: str = ""
        models: list = []
        ok = False
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            for attempt in range(3):
                try:
                    response = await client.get(
                        f"{self.BASE_URL}/models",
                        params=params,
                        headers=self.headers,
                    )
                    response.raise_for_status()
                    models = response.json()
                    ok = True
                    break
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    detail = ""
                    try:
                        body = exc.response.json()
                        if isinstance(body, dict):
                            detail = body.get("error") or body.get("message") or ""
                    except Exception:
                        detail = (exc.response.text or "").strip()[:200]
                    if not detail:
                        detail = f"HTTP {status} from HuggingFace"
                    else:
                        detail = f"HTTP {status}: {detail}"
                    last_error = detail
                    print(f"[hf] search_models attempt {attempt + 1} HTTP error: {detail}")
                    # Don't retry client errors (4xx except 429)
                    if 400 <= status < 500 and status != 429:
                        break
                except httpx.RequestError as exc:
                    last_error = f"Network error: {type(exc).__name__}: {exc}"
                    print(f"[hf] search_models attempt {attempt + 1} {last_error}")
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    print(f"[hf] search_models attempt {attempt + 1} unexpected: {last_error}")
                # brief backoff before retry
                if attempt < 2:
                    import asyncio as _asyncio
                    await _asyncio.sleep(0.5 * (attempt + 1))

        if not ok:
            return {"models": [], "total": 0, "error": last_error or "Unknown HuggingFace error"}

        # Filter to repos that are actually likely to contain GGUF artifacts.
        # HF search still sometimes returns base repos even when `GGUF` is in
        # the query, so verify by id/tags first and by repo files if needed.
        if gguf_only:
            models = await self._filter_gguf_models(models, limit=limit)

        # Filter models by memory requirements
        if filter_vram_gb is not None:
            models = [
                m for m in models if self._check_vram_compatible(m, filter_vram_gb)
            ]

        if filter_ram_gb is not None:
            models = [
                m for m in models if self._check_ram_compatible(m, filter_ram_gb)
            ]

        return {"models": models[:limit], "total": len(models[:limit])}

    async def _filter_gguf_models(self, models: list, limit: int) -> list:
        """Keep only models that look GGUF-compatible.

        Fast path checks id/tags. Slow path inspects repo files for `.gguf` so
        repos without a `gguf` tag can still appear, while plain safetensors
        base repos are excluded from Hire search.
        """
        filtered: list = []
        for model in models:
            model_id = model.get("id", "")
            tags = model.get("tags", []) or []
            haystack = f"{model_id} {' '.join(map(str, tags))}".lower()
            if "gguf" in haystack:
                filtered.append(model)
            elif model_id:
                files = await self.get_model_files(model_id)
                if any(
                    (f.get("path") or f.get("rfilename") or "").lower().endswith(".gguf")
                    for f in files
                ):
                    filtered.append(model)
            if len(filtered) >= limit:
                break
        return filtered

    def _check_vram_compatible(self, model: dict, available_vram_gb: float) -> bool:
        """Check if model can fit in available VRAM."""
        # Try to get memory requirements from model tags
        model_id = model.get("id", "")
        memory = model.get("memory", {})

        # Check if model has GGUF or quantized tag
        tags = model.get("tags", [])
        if "gguf" in str(tags).lower() or "gguf" in model_id.lower():
            # Check model parameters size as estimate
            params = self._estimate_params(model)
            # Rough estimate: 0.5 GB per billion parameters for 4-bit quantization
            estimated_vram = params * 0.5  # GB
            return estimated_vram <= available_vram_gb

        # For non-quantized models, check shaffer model
        params = self._estimate_params(model)
        # Full precision estimate: 4 GB per billion parameters
        estimated_vram = params * 4
        return estimated_vram <= available_vram_gb

    def _check_ram_compatible(self, model: dict, available_ram_gb: float) -> bool:
        """Check if model can fit in available RAM."""
        params = self._estimate_params(model)
        # CPU inference: ~8 GB per billion parameters (fp32)
        estimated_ram = params * 8
        return estimated_ram <= available_ram_gb

    def _estimate_params(self, model: dict) -> float:
        """Estimate model parameters in billions."""
        model_id = model.get("id", "").lower()
        tags = [t.lower() for t in model.get("tags", [])]
        config = model.get("config", {}) or {}

        # 1. Try to read from HF config if available
        try:
            num_params = config.get("num_parameters")
            if num_params:
                return float(num_params) / 1e9
        except Exception:
            pass

        # 2. Parse model size from ID (e.g., llama-3.1-8b, 1.5b, 70b-instruct)
        import re

        size_match = re.search(r"(\d+\.?\d*)\s?b", model_id)
        if size_match:
            return float(size_match.group(1))

        # 3. Check tags for size hints
        for tag in tags:
            tag_match = re.search(r"(\d+\.?\d*)\s?b", tag)
            if tag_match:
                return float(tag_match.group(1))

        # 4. Known model families fallback
        known_models = {
            "tinyllama": 1.1,
            "llama3.2": 3.0,
            "llama3.1": 8.0,
            "llama3": 8.0,
            "llama2": 7.0,
            "llama": 7.0,
            "mistral": 7.0,
            "mixtral": 46.7,
            "phi3": 3.8,
            "phi4": 14.0,
            "phi": 2.7,
            "gemma2": 9.0,
            "gemma": 2.0,
            "qwen2.5": 7.0,
            "qwen": 7.0,
            "codellama": 7.0,
            "deepseek": 7.0,
            "falcon": 7.0,
            "mpt": 7.0,
            "stablelm": 7.0,
        }
        for name, size in known_models.items():
            if name in model_id:
                return size

        # Default to 7B as fallback
        return 7.0

    async def get_model_info(self, model_id: str) -> Optional[dict]:
        """Get detailed information about a specific model."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    f"{self.BASE_URL}/models/{model_id}",
                    headers=self.headers,
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError:
                return None

    async def get_model_files(self, model_id: str) -> list[dict]:
        """Get list of files in a model repository."""
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            try:
                response = await client.get(
                    f"{self.BASE_URL}/models/{model_id}/tree/main",
                    headers=self.headers,
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError:
                return []
            except httpx.RequestError:
                return []

    # Preference order for GGUF quants when auto-picking the smallest
    # reasonable quality variant. Lower index = preferred.
    _QUANT_PREFERENCE = [
        "Q4_K_M",
        "Q4_K_S",
        "Q4_0",
        "Q5_K_M",
        "Q5_K_S",
        "Q5_0",
        "Q3_K_M",
        "Q3_K_L",
        "Q3_K_S",
        "Q6_K",
        "Q8_0",
        "Q2_K",
        "F16",
        "BF16",
        "F32",
    ]

    async def pick_gguf_quant(self, model_id: str) -> Optional[str]:
        """Inspect HF repo files and return the best GGUF quant tag.

        Returns a lowercase tag suitable for `ollama pull hf.co/<repo>:<tag>`,
        or None if the repo has no GGUF files (Ollama will then fall back
        to its default behavior). Tag matching is case-insensitive in
        Ollama's hf.co gateway.
        """
        files = await self.get_model_files(model_id)
        if not files:
            return None
        gguf_names: list[str] = []
        for f in files:
            path = f.get("path") or f.get("rfilename") or ""
            if path.lower().endswith(".gguf"):
                gguf_names.append(path)
        if not gguf_names:
            return None
        # Find the first preferred quant that exists in this repo.
        upper_names = [n.upper() for n in gguf_names]
        for quant in self._QUANT_PREFERENCE:
            for name in upper_names:
                # Match as a token surrounded by ., -, _ or boundaries
                # to avoid e.g. "Q4_0" matching inside "Q4_K_M".
                if (
                    f".{quant}." in name
                    or f"-{quant}." in name
                    or f"_{quant}." in name
                    or f".{quant}-" in name
                    or name.endswith(f".{quant}.GGUF")
                ):
                    return quant
        # No known quant matched — return the first GGUF stem so Ollama
        # has something specific to pull instead of the largest file.
        return None

    def estimate_model_size(self, model: dict, quantization: str = "int4") -> dict:
        """Estimate model size in memory for different quantization levels."""
        params = self._estimate_params(model)

        # params is in billions, so params * bytes_per_param = GB
        estimates = {
            "fp32": params * 4,        # GB
            "fp16": params * 2,
            "int8": params * 1,
            "int4": params * 0.5,
            "int2": params * 0.25,
        }

        return {
            "params_billions": params,
            "sizes_gb": estimates,
            "recommended": "int4" if params >= 7 else "int8",
        }

    def get_models_dir(self) -> Path:
        """Return the directory where models are stored."""
        path = settings.models_path
        path.mkdir(parents=True, exist_ok=True)
        return path


# Singleton instance
hf_service = HuggingFaceService()
