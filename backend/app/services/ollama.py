"""Ollama integration service."""

import asyncio
import json
from typing import AsyncIterator, Optional

import httpx

from app.config import settings


class OllamaError(RuntimeError):
    """Raised when an Ollama API call fails in a way the user needs to know about."""


class OllamaService:
    """Client for interacting with Ollama API."""

    def __init__(self, host: str = None):
        self.gpu_host = host or settings.ollama_gpu_host
        self.cpu_host = settings.ollama_cpu_host

    @staticmethod
    def _normalize_host(host: Optional[str], fallback: str) -> str:
        target = host or fallback
        if target and not target.startswith("http"):
            target = f"http://{target}"
        return target

    async def generate(
        self,
        model: str,
        prompt: str,
        host: str = None,
        system: str = "",
        stream: bool = False,
    ) -> dict:
        """Generate a response from a model."""
        target_host = host or self.gpu_host
        # Ensure host has protocol
        if target_host and not target_host.startswith("http"):
            target_host = f"http://{target_host}"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
        }
        if system:
            payload["system"] = system

        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.post(
                f"{target_host}/api/generate", json=payload
            )
            response.raise_for_status()
            return response.json()

    async def chat_with_tools(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        host: str = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> dict:
        """Call Ollama's OpenAI-compatible chat endpoint with native tools.

        This is intentionally separate from /api/generate because tool calling
        requires chat-completions style messages and returns message.tool_calls.
        """
        target_host = self._normalize_host(host, self.gpu_host)
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=180.0, trust_env=False) as client:
            response = await client.post(f"{target_host}/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return {
            "response": message.get("content") or "",
            "message": message,
            "tool_calls": message.get("tool_calls") or [],
            "model": model,
            "runtime": "ollama-openai-tools",
            "raw": data,
        }

    async def list_models(self, host: str = None) -> list[dict]:
        """List models available on an Ollama instance.

        Returns [] on any transport/HTTP error (including Ollama not running),
        so callers that just want a snapshot don't have to handle exceptions.
        Use `is_available()` if you need to distinguish "down" from "empty".
        """
        target_host = self._normalize_host(host, self.gpu_host)
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            try:
                response = await client.get(f"{target_host}/api/tags")
                response.raise_for_status()
                data = response.json()
                return data.get("models", [])
            except (httpx.RequestError, httpx.HTTPStatusError):
                return []

    async def list_models_with_tool_support(self, host: str = None) -> list[dict]:
        """List installed Ollama models and annotate native tool support.

        Ollama exposes a model prompt template through /api/show. Models whose
        template contains the Tools placeholder can receive native tool schemas;
        other models will require lmWorld's text-tool fallback.
        """
        models = await self.list_models(host)
        if not models:
            return []

        async def annotate(model: dict) -> dict:
            name = str(model.get("name") or model.get("model") or "").strip()
            if not name:
                return {**model, "supports_tools": False}
            supports_tools = await self.supports_tools(name, host)
            return {**model, "supports_tools": supports_tools}

        return await asyncio.gather(*(annotate(model) for model in models))

    async def supports_tools(self, model: str, host: str = None) -> bool:
        """Return True when an Ollama model template advertises tool support."""
        info = await self.model_info(model, host)
        template = str((info or {}).get("template") or "")
        return "{{ .Tools }}" in template or "{{.Tools}}" in template

    async def pull_model(self, model: str, host: str = None) -> dict:
        """Pull a model from Ollama registry (non-streaming, legacy)."""
        target_host = self._normalize_host(host, self.gpu_host)
        async with httpx.AsyncClient(timeout=600.0, trust_env=False) as client:
            response = await client.post(
                f"{target_host}/api/pull",
                json={"name": model, "stream": False},
            )
            response.raise_for_status()
            return response.json()

    async def stream_pull(self, model: str, host: str = None) -> AsyncIterator[dict]:
        """Stream pull events from Ollama as parsed dicts.

        Each event looks like:
            {"status": "pulling manifest"}
            {"status": "pulling deadbeef...", "digest": "...", "total": 1234, "completed": 567}
            {"status": "success"}
            {"error": "model 'foo' not found"}

        Raises `OllamaError` if the underlying connection fails or Ollama
        emits an `error` event.
        """
        target_host = self._normalize_host(host, self.gpu_host)
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=None)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    f"{target_host}/api/pull",
                    json={"name": model, "stream": True},
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise OllamaError(
                            f"Ollama returned HTTP {response.status_code}: "
                            f"{body.decode('utf-8', errors='replace').strip() or '<empty>'}"
                        )
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if evt.get("error"):
                            raise OllamaError(str(evt["error"]))
                        yield evt
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            raise OllamaError(
                f"Ollama is not reachable at {target_host}. "
                f"Start Ollama (run 'ollama serve') and retry."
            ) from exc
        except httpx.HTTPError as exc:
            msg = str(exc) or exc.__class__.__name__
            raise OllamaError(f"Ollama transport error: {msg}") from exc

    async def pull_model_blocking(self, model: str, host: str = None) -> dict:
        """Pull a model and wait for it to finish. Logs progress to stdout
        (and therefore backend.log) so long pulls aren't silent. Also
        broadcasts `pull_progress` events over the office WebSocket so the
        UI can show a live progress bar. Raises `OllamaError` with a clear
        message on any failure path.
        """
        from app.services.websocket import ws_manager

        async def _broadcast(pct: int, info: str):
            try:
                await ws_manager.broadcast_office({
                    "type": "pull_progress",
                    "model": model,
                    "percent": pct,
                    "info": info,
                })
            except Exception:
                pass

        last_status = ""
        last_pct = -10
        async for evt in self.stream_pull(model, host):
            status = evt.get("status", "") or ""
            total = evt.get("total")
            completed = evt.get("completed")
            if isinstance(total, int) and isinstance(completed, int) and total > 0:
                pct = int(completed * 100 / total)
                if pct >= last_pct + 5 or status != last_status:
                    mb_done = completed / (1024 * 1024)
                    mb_total = total / (1024 * 1024)
                    info = f"{status} {pct}% ({mb_done:.0f}/{mb_total:.0f} MB)"
                    print(f"[ollama:pull {model}] {info}", flush=True)
                    await _broadcast(pct, info)
                    last_pct = pct
                    last_status = status
            elif status and status != last_status:
                print(f"[ollama:pull {model}] {status}", flush=True)
                await _broadcast(max(last_pct, 0), status)
                last_status = status
        print(f"[ollama:pull {model}] done", flush=True)
        await _broadcast(100, "downloaded")
        return {"status": "success", "model": model}

    async def has_model(self, model: str, host: str = None) -> bool:
        """Check if a tag is already installed on the target Ollama host.
        Match is case-insensitive on the full tag (e.g. 'llama3.1:8b')."""
        installed = await self.list_models(host)
        target = model.strip().lower()
        for m in installed:
            name = (m.get("name") or m.get("model") or "").lower()
            if name == target:
                return True
        return False

    async def model_info(self, model: str, host: str = None) -> Optional[dict]:
        """Get model information."""
        target_host = self._normalize_host(host, self.gpu_host)
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            try:
                response = await client.post(
                    f"{target_host}/api/show", json={"name": model}
                )
                response.raise_for_status()
                return response.json()
            except (httpx.RequestError, httpx.HTTPStatusError):
                return None

    async def is_available(self, host: str = None) -> bool:
        """Check if Ollama server is reachable."""
        target_host = self._normalize_host(host, self.gpu_host)
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            try:
                response = await client.get(target_host)
                return response.status_code == 200
            except (httpx.RequestError, httpx.HTTPStatusError):
                return False


# Singleton instance
ollama_service = OllamaService()
