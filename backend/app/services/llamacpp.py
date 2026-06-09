"""Llama.cpp llama-server integration for models not covered by Ollama.

This service provides a fallback when Ollama returns 400 (unsupported model).
It downloads GGUF files directly from HuggingFace and serves them via
llama-server's OpenAI-compatible API.
"""

import asyncio
import json
import re
import subprocess
import httpx
from pathlib import Path
from typing import AsyncIterator, Optional
from dataclasses import dataclass
from datetime import datetime

from app.config import settings


@dataclass
class LlamaServerInstance:
    """Represents a running llama-server process."""
    model_id: str          # e.g. "meta-llama/Llama-3.2-3B-Instruct-Q4_K_M"
    gguf_path: str         # local path to GGUF file
    host: str              # "127.0.0.1"
    port: int              # e.g. 8080
    process: asyncio.subprocess.Process
    started_at: datetime


class LlamaCppService:
    """Manages llama-server instances for direct GGUF serving."""

    # Base port for llama-server instances. Each concurrent model gets its own port.
    _BASE_PORT = 8080
    _MAX_PORTS = 10

    def __init__(self, models_dir: Optional[Path] = None):
        self.models_dir = models_dir or Path(settings.models_path)
        self.llama_server_exe = self._find_llama_server()
        # Maps model_id -> LlamaServerInstance
        self._instances: dict[str, LlamaServerInstance] = {}
        self._port_used: set[int] = set()
        self._lock = asyncio.Lock()

    def _find_llama_server(self) -> Optional[Path]:
        """Locate llama-server.exe in the project Ollama installation.
        
        Project layout:
          c:/python4ik/lmWorld/
            backend/
              app/services/llamacpp.py    <- this file
            ollama/lib/ollama/llama-server.exe
        
        Need to go up 4 levels: services -> app -> app -> lmWorld -> project
        """
        here = Path(__file__).resolve()
        # backend/app/services/ -> backend/ (parents[2])
        # Then go up to project root: backend/../
        root = here.parents[2].parent  # backend/ -> lmWorld/
        candidates = [
            root / "ollama" / "lib" / "ollama" / "llama-server.exe",
            root / "ollama" / "lib" / "ollama" / "llama-server",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    async def _allocate_port(self) -> int:
        """Find an unused port for a new llama-server instance."""
        for port in range(self._BASE_PORT, self._BASE_PORT + self._MAX_PORTS):
            if port not in self._port_used:
                return port
        raise RuntimeError(f"All llama-server ports ({self._BASE_PORT}-{self._BASE_PORT + self._MAX_PORTS - 1}) are in use")

    async def _find_free_port(self) -> int:
        """Find a genuinely free port by probing."""
        for port in range(self._BASE_PORT, self._BASE_PORT + self._MAX_PORTS):
            if port in self._port_used:
                continue
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                continue  # port is in use
            except (ConnectionRefusedError, OSError):
                return port  # free
        raise RuntimeError("No free ports available for llama-server")

    async def _build_args(
        self,
        gguf_path: str,
        host: str,
        port: int,
        use_gpu: bool = True,
    ) -> list[str]:
        """Build llama-server command-line arguments."""
        cmd = [
            str(self.llama_server_exe),
            "-m", gguf_path,
            "--host", host,
            "--port", str(port),
            "--ctx-size", "4096",
            "--parallel", "4",
            "--numa", "off",
        ]
        # GPU offloading hint
        if not use_gpu:
            cmd += ["--n-gpu-layers", "0"]
        return cmd

    async def start_server(
        self,
        model_id: str,
        gguf_path: str,
        use_gpu: bool = True,
    ) -> LlamaServerInstance:
        """Start a llama-server instance for the given GGUF file.
        
        Returns immediately after server starts listening. Does NOT wait for
        model to fully load into memory.
        """
        async with self._lock:
            # Reuse existing instance if the same model is already served
            if model_id in self._instances:
                inst = self._instances[model_id]
                if Path(inst.gguf_path).exists() and inst.process.returncode is None:
                    return inst
                # Clean up stale entry
                await self._cleanup_instance(model_id)

            port = await self._find_free_port()
            args = await self._build_args(gguf_path, "127.0.0.1", port, use_gpu)

            self._port_used.add(port)
            env = {
                "OLLAMA_MODELS": str(self.models_dir),
                "PATH": str(self.models_dir.parent / "ollama" / "lib"),
            }

            process = await asyncio.create_subprocess_exec(
                *args,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            inst = LlamaServerInstance(
                model_id=model_id,
                gguf_path=gguf_path,
                host="127.0.0.1",
                port=port,
                process=process,
                started_at=datetime.utcnow(),
            )
            self._instances[model_id] = inst

        # Wait for server to be ready
        await self._wait_for_server_ready(inst, timeout=60.0)
        return inst

    async def _wait_for_server_ready(
        self,
        inst: LlamaServerInstance,
        timeout: float = 60.0,
    ) -> None:
        """Poll the llama-server /health endpoint until it responds."""
        deadline = asyncio.get_event_loop().time() + timeout
        base_url = f"http://{inst.host}:{inst.port}"

        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(
                    f"llama-server for {inst.model_id} did not become ready within {timeout}s"
                )
            # Check if process died
            if inst.process.returncode is not None:
                stdout, stderr = await inst.process.communicate()
                raise RuntimeError(
                    f"llama-server process died with code {inst.process.returncode}.\n"
                    f"stdout: {stdout.decode(errors='replace')}\nstderr: {stderr.decode(errors='replace')}"
                )
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{base_url}/health")
                    if resp.status_code == 200:
                        return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout):
                pass
            await asyncio.sleep(1.0)

    async def stop_server(self, model_id: str) -> bool:
        """Stop a llama-server instance for the given model."""
        async with self._lock:
            return await self._cleanup_instance(model_id)

    async def _cleanup_instance(self, model_id: str) -> bool:
        """Internal cleanup without lock (caller must hold the lock)."""
        inst = self._instances.pop(model_id, None)
        if inst is None:
            return False
        self._port_used.discard(inst.port)
        try:
            inst.process.terminate()
            try:
                await asyncio.wait_for(inst.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                inst.process.kill()
                await inst.process.wait()
        except Exception:
            pass
        return True

    async def stop_all(self) -> None:
        """Stop all running llama-server instances."""
        async with self._lock:
            for model_id in list(self._instances.keys()):
                await self._cleanup_instance(model_id)

    @property
    def base_url(self) -> str:
        """Base URL for llama-server OpenAI-compatible API."""
        return "http://127.0.0.1:8080"

    def instance_for(self, model_id: str) -> Optional[LlamaServerInstance]:
        """Return the active instance for a model, or None."""
        return self._instances.get(model_id)

    async def generate(
        self,
        model_id: str,
        messages: list[dict],
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stream: bool = False,
    ) -> dict | AsyncIterator[dict]:
        """Send a chat completion request to the llama-server instance.

        Falls back to a new server if no instance exists for the model.
        """
        inst = self._instances.get(model_id)
        if inst is None:
            raise RuntimeError(
                f"No llama-server instance for {model_id}. "
                "Call start_server(model_id, gguf_path) first."
            )

        base_url = f"http://{inst.host}:{inst.port}/v1"

        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if system_prompt:
            # Prepend system message
            payload["messages"] = [{"role": "system", "content": system_prompt}] + messages

        if stream:
            return self._stream_chat(base_url, payload)
        else:
            return await self._sync_chat(base_url, payload)

    async def _sync_chat(self, base_url: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{base_url}/chat/completions", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def _stream_chat(self, base_url: str, payload: dict) -> AsyncIterator[dict]:
        """Yield parsed SSE chunks from the /chat/completions stream."""
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream("POST", f"{base_url}/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    yield json.loads(data)

    # --- GGUF Download helpers ---

    async def download_gguf(
        self,
        repo_id: str,
        filename: str,
        progress_callback=None,  # (downloaded_bytes, total_bytes) -> None
        output_dir: Optional[Path] = None,
    ) -> Path:
        """Download a GGUF file from HuggingFace Hub.

        Saves to output_dir / repo_id_safe / filename.
        """
        dest_dir = (output_dir or self.models_dir) / repo_id.replace("/", "--")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename

        if dest_path.exists():
            return dest_path

        url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
        headers = {}
        hf_token = (settings.hf_token or "").strip()
        if hf_token:
            headers["Authorization"] = f"Bearer {hf_token}"

        # Get file size for progress reporting
        async with httpx.AsyncClient(timeout=30.0) as client:
            head_resp = await client.head(url, headers=headers, follow_redirects=True)
            head_resp.raise_for_status()
            total_size = int(head_resp.headers.get("content-length", 0))

        downloaded = 0
        with open(dest_path, "wb") as f:
            async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size:
                            progress_callback(downloaded, total_size)

        return dest_path

    async def pick_best_gguf(self, repo_id: str) -> Optional[tuple[str, int]]:
        """Scan a HF repo for the best (smallest reasonable) GGUF quant.

        Returns (filename, size_bytes) of the selected file, or None if no GGUF found.

        Selection strategy:
        1. Prefer Q4_K_M or Q4_K_S (good quality/speed balance)
        2. Fall back to Q5_K_S, Q5_K_M, Q8_0
        3. Never pick F16, BF16, F32 (too large)
        4. Pick smallest available quant if none of the above
        """
        preferred_quants = ["Q4_K_M", "Q4_K_S", "Q5_K_S", "Q5_K_M", "Q8_0"]
        url = f"https://huggingface.co/api/models/{repo_id}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return None

        siblings = data.get("siblings", [])
        gguf_files = [
            f for f in siblings
            if f.get("rfilename", "").lower().endswith(".gguf")
            and not re.search(r"(?i)(f16|bf16|f32|q3|q6)", f.get("rfilename", ""))
        ]

        if not gguf_files:
            # Second pass: allow any GGUF not F16/BF16
            gguf_files = [
                f for f in siblings
                if f.get("rfilename", "").lower().endswith(".gguf")
                and not re.search(r"(?i)(f16|bf16|f32)", f.get("rfilename", ""))
            ]

        if not gguf_files:
            return None

        # Pick by preferred quant first, then by size
        best = None
        best_score = 999
        for f in gguf_files:
            fname = f["rfilename"]
            size = f.get("size", 0)
            score = 999
            for i, q in enumerate(preferred_quants):
                if q in fname.upper():
                    score = i
                    break
            if score < best_score:
                best_score = score
                best = (fname, size)

        return best

    def get_instance_base_url(self, model_id: str) -> Optional[str]:
        """Get the base URL for a running llama-server instance."""
        inst = self._instances.get(model_id)
        if inst:
            return f"http://{inst.host}:{inst.port}"
        return None

    def is_server_ready(self, model_id: str) -> bool:
        """Check if a llama-server is running for the given model."""
        inst = self._instances.get(model_id)
        if inst is None:
            return False
        return inst.process.returncode is None


# Module-level singleton
_llamacpp: Optional[LlamaCppService] = None


def get_llamacpp() -> LlamaCppService:
    global _llamacpp
    if _llamacpp is None:
        _llamacpp = LlamaCppService()
    return _llamacpp
