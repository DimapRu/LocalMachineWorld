"""Runtime dispatcher: Ollama first, llama.cpp fallback on 400.

Provides a single `run_inference(...)` entry point that the chat and worker
loops call without caring which backend actually serves the request.

Strategy:
  1. Try Ollama. If the model responds (any 2xx, even slow), use it.
  2. If Ollama returns 400 (unsupported model), inspect the error to see
     whether it's a model-shape problem, then:
        a. Find a GGUF in the same HF repo (if known).
        b. Download via HF Hub.
        c. Start a llama-server with that GGUF.
        d. Re-issue the request against llama-server's OpenAI-compatible
           /v1/chat/completions endpoint.
  3. If no GGUF is available anywhere, raise a typed error so callers can
     surface a clear "unsupported architecture" message.
"""

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

from app.config import settings
from app.services.llamacpp import get_llamacpp, LlamaCppService
from app.services.ollama import OllamaService, ollama_service


class ModelUnsupportedError(RuntimeError):
    """Raised when neither Ollama nor llama-server can serve a model."""


class RuntimeDispatcher:
    """Routes inference requests to Ollama or llama-server as needed."""

    def __init__(self):
        self.ollama: OllamaService = ollama_service
        self.llamacpp: LlamaCppService = get_llamacpp()
        # Cache: model_id -> "ollama" | "llamacpp"
        self._resolved: dict[str, str] = {}
        # Models whose Ollama chat template can't auto-generate a tool parser.
        # Once a model lands here we stop sending `tools=[...]` to it.
        self._tools_unsupported: set[str] = set()
        self._ollama_started_by_us: bool = False

    def _start_ollama_if_needed(self) -> bool:
        """Start Ollama server from project folder if not already running.
        
        Returns True if Ollama is now running, False otherwise.
        """
        # Check if already reachable
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("localhost", 11434))
            sock.close()
            if result == 0:
                return True  # Already running
        except Exception:
            pass

        # Find ollama.exe
        ollama_exe = settings.root_dir / "ollama" / "ollama.exe"
        if not ollama_exe.exists():
            return False

        models_dir = settings.models_path
        env = {
            "OLLAMA_MODELS": str(models_dir),
            "OLLAMA_HOST": "127.0.0.1:11434",
        }
        
        try:
            # Start Ollama in background
            subprocess.Popen(
                [str(ollama_exe), "serve"],
                cwd=str(ollama_exe.parent),
                env={**os.environ, **env},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            self._ollama_started_by_us = True
            # Wait up to 10 seconds for Ollama to be ready
            for _ in range(20):
                time.sleep(0.5)
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    result = sock.connect_ex(("localhost", 11434))
                    sock.close()
                    if result == 0:
                        return True
                except Exception:
                    pass
            return True  # Started, even if not fully ready yet
        except Exception as e:
            print(f"[runtime] Failed to start Ollama: {e}")
            return False

    # ------------------------------------------------------------------
    # HF repo inference
    # ------------------------------------------------------------------
    @staticmethod
    def looks_like_hf_id(model_name: str) -> bool:
        """True if the name looks like `org/repo` (HF model id)."""
        if not model_name:
            return False
        # Strip "hf.co/" prefix if present
        clean = model_name.replace("hf.co/", "").replace("huggingface.co/", "")
        return bool(re.match(r"^[\w.-]+/[\w.-]+$", clean))

    @staticmethod
    def hf_repo_id(model_name: str) -> str:
        """Extract `org/repo` from a model name."""
        clean = model_name.replace("hf.co/", "").replace("huggingface.co/", "")
        return clean

    # ------------------------------------------------------------------
    # Main dispatch entry
    # ------------------------------------------------------------------
    async def run_inference(
        self,
        *,
        model_name: str,
        prompt: str,
        system: Optional[str] = None,
        host: Optional[str] = None,
        device: str = "gpu",
        use_gpu: bool = True,
        stream: bool = False,
    ) -> dict:
        """Run inference, transparently falling back to llama-server.

        Returns a dict with at least a `response` key. Raises
        ModelUnsupportedError if no runtime can serve the request.
        """
        cached = self._resolved.get(model_name)
        if cached == "llamacpp":
            return await self._run_llamacpp(
                model_name=model_name, prompt=prompt, system=system, use_gpu=use_gpu
            )
    
            # Auto-start Ollama if not already running
            self._start_ollama_if_needed()
    
            # Try Ollama first
        try:
            result = await self.ollama.generate(
                model=model_name,
                prompt=prompt,
                host=host or self.ollama.gpu_host,
                system=system,
                stream=stream,
            )
            self._resolved[model_name] = "ollama"
            return result
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            body = e.response.text or ""
            # 400 / 404 from Ollama = "model not supported by this build"
            if status_code not in (400, 404) or not self._is_unsupported(body):
                raise
            # Fall through to llama-server
        except httpx.RequestError:
            # Network error to Ollama; if model is HF-style, try llama-server
            if not self.looks_like_hf_id(model_name):
                raise
            # else fall through

        # Ollama rejected / unreachable AND model looks like HF id
        return await self._run_llamacpp_fallback(
            model_name=model_name, prompt=prompt, system=system, use_gpu=use_gpu
        )

    async def run_chat_tools(
        self,
        *,
        model_name: str,
        messages: list[dict],
        tools: list[dict],
        host: Optional[str] = None,
        device: str = "gpu",
        use_gpu: bool = True,
    ) -> dict:
        """Run OpenAI-compatible chat completion with native tool definitions.

        If the selected local runtime does not support /v1/chat/completions tools,
        return a safe chat-only marker instead of falling back to prompt-parsed JSON.
        """
        cached = self._resolved.get(model_name)
        if cached == "llamacpp" or model_name in self._tools_unsupported:
            # Either llama-server (no /v1 tools) or a model whose chat template
            # rejected `tools=[...]` on a previous call.
            return {
                "response": "",
                "tool_calls": [],
                "runtime": "chat-only-no-native-tools",
                "tool_call_supported": False,
            }

        self._start_ollama_if_needed()
        try:
            result = await self.ollama.chat_with_tools(
                model=model_name,
                messages=messages,
                tools=tools,
                host=host or self.ollama.gpu_host,
            )
            result["tool_call_supported"] = True
            self._resolved[model_name] = "ollama"
            return result
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            body = e.response.text or ""
            if status_code in (400, 404, 422) and any(
                marker in body.lower()
                for marker in (
                    "tools",
                    "tool",
                    "chat",
                    "unsupported",
                    "not found",
                    "unknown",
                    # Ollama jinja chat-template failures (no built-in tool parser)
                    "parser",
                    "template",
                    "generate parser",
                    "callexpression",
                )
            ):
                print(f"[runtime-tools] native tools unavailable: {body[:200]}", flush=True)
                # Remember this model can't do native tools so we don't keep
                # retrying and hitting 400s on every iteration of the loop.
                self._tools_unsupported.add(model_name)
                return {
                    "response": "",
                    "tool_calls": [],
                    "runtime": "chat-only-no-native-tools",
                    "tool_call_supported": False,
                    "error": body[:500],
                }
            raise
        except httpx.RequestError as exc:
            print(f"[runtime-tools] native tools endpoint unreachable: {exc}", flush=True)
            return {
                "response": "",
                "tool_calls": [],
                "runtime": "chat-only-no-native-tools",
                "tool_call_supported": False,
                "error": str(exc),
            }

    @staticmethod
    def _is_unsupported(body: str) -> bool:
        """Heuristic: does Ollama's error look like 'unsupported model'?"""
        body_l = body.lower()
        return any(
            marker in body_l
            for marker in (
                "unsupported model",
                "model not found",
                "no such model",
                "not a gguf",
                "unknown model architecture",
                "this version of ollama",
                "missing task",
                "invalid model",
            )
        )

    # ------------------------------------------------------------------
    # llama-server dispatch
    # ------------------------------------------------------------------
    async def _run_llamacpp_fallback(
        self,
        *,
        model_name: str,
        prompt: str,
        system: Optional[str],
        use_gpu: bool,
    ) -> dict:
        """Download GGUF (if needed) and start a llama-server, then query it."""
        repo_id = self.hf_repo_id(model_name)

        pick = await self.llamacpp.pick_best_gguf(repo_id)
        if not pick:
            raise ModelUnsupportedError(
                f"No GGUF quant found in HF repo '{repo_id}'. "
                "Architecture may be too new for llama.cpp."
            )
        filename, _size = pick

        # Download (no progress callback in this sync path)
        gguf_path = await self.llamacpp.download_gguf(
            repo_id=repo_id,
            filename=filename,
            progress_callback=None,
        )

        inst = await self.llamacpp.start_server(
            model_id=repo_id,
            gguf_path=str(gguf_path),
            use_gpu=use_gpu,
        )

        # Build a simple messages list
        messages = [{"role": "user", "content": prompt}]
        result = await self.llamacpp.generate(
            model_id=repo_id,
            messages=messages,
            system_prompt=system,
            temperature=0.7,
            max_tokens=2048,
            stream=False,
        )

        self._resolved[model_name] = "llamacpp"
        # Normalize to Ollama-style response dict
        return {
            "response": (result.get("choices") or [{}])[0].get("message", {}).get("content", ""),
            "model": model_name,
            "runtime": "llamacpp",
            "llamacpp_url": f"http://{inst.host}:{inst.port}",
        }

    async def _run_llamacpp(
        self,
        *,
        model_name: str,
        prompt: str,
        system: Optional[str],
        use_gpu: bool,
    ) -> dict:
        """Re-issue against an already-running llama-server instance."""
        repo_id = self.hf_repo_id(model_name)
        inst = self.llamacpp.instance_for(repo_id)
        if inst is None:
            # Lost the instance; rebuild it
            return await self._run_llamacpp_fallback(
                model_name=model_name, prompt=prompt, system=system, use_gpu=use_gpu
            )
        messages = [{"role": "user", "content": prompt}]
        result = await self.llamacpp.generate(
            model_id=repo_id,
            messages=messages,
            system_prompt=system,
            temperature=0.7,
            max_tokens=2048,
            stream=False,
        )
        return {
            "response": (result.get("choices") or [{}])[0].get("message", {}).get("content", ""),
            "model": model_name,
            "runtime": "llamacpp",
            "llamacpp_url": f"http://{inst.host}:{inst.port}",
        }

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def runtime_for(self, model_name: str) -> str:
        """Return 'ollama', 'llamacpp', or 'unknown' for a given model."""
        return self._resolved.get(model_name, "unknown")

    def reset_resolution(self, model_name: str) -> None:
        self._resolved.pop(model_name, None)

    async def shutdown(self) -> None:
        """Stop all background llama-server processes."""
        await self.llamacpp.stop_all()


# Singleton
runtime = RuntimeDispatcher()
