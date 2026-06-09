"""WebSocket, resource, and worker control endpoints."""

import os
import subprocess
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.config import settings
from app.services.resources import resource_monitor
from app.services.websocket import ws_manager
from app.services.agent_worker import agent_worker
from app.services.activity import activity_state
from app.services.runtime import runtime
from app.services import agent_os
from app.schemas import ResourceStatus

router = APIRouter(tags=["system"])


class AgentOsUpdate(BaseModel):
    mode: Literal["auto", "windows", "linux", "macos"]


# ─── Ollama Installation Detection ──────────────────────────────────────


def get_ollama_info() -> dict:
    """Detect Ollama installation in project folder."""
    project_root = Path(settings.root_dir)
    ollama_dir = project_root / "ollama"
    ollama_exe = ollama_dir / "ollama.exe"
    
    result = {
        "installed": ollama_exe.exists(),
        "path": str(ollama_exe) if ollama_exe.exists() else None,
        "version": None,
        "running": False,
    }
    
    if ollama_exe.exists():
        try:
            ver = subprocess.run(
                [str(ollama_exe), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            result["version"] = ver.stdout.strip() or ver.stderr.strip()
        except Exception:
            pass
    
    # Check if Ollama is running on port 11434
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(("localhost", 11434))
        result["running"] = True
    except Exception:
        result["running"] = False
    finally:
        sock.close()
    
    return result


@router.get("/api/ollama-info")
async def get_ollama_info_endpoint():
    """Get Ollama installation status and version."""
    return get_ollama_info()


@router.post("/api/ollama-install")
async def install_ollama():
    """Download and install Ollama to the project folder."""
    import asyncio
    
    project_root = Path(settings.root_dir)
    ollama_dir = project_root / "ollama"
    ollama_exe = ollama_dir / "ollama.exe"
    
    if ollama_exe.exists():
        return {"status": "already_installed", "path": str(ollama_exe)}
    
    return await _download_and_install_ollama(project_root, ollama_dir)


async def _download_and_install_ollama(project_root: Path, ollama_dir: Path) -> dict:
    """Download and extract Ollama from GitHub portable zip with retry + integrity check."""
    import httpx
    import shutil
    import zipfile
    
    zip_path = ollama_dir / "ollama.zip"
    url = "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"
    MIN_SIZE = 30_000_000  # bytes; zip below this is treated as corrupt
    
    # Clean any corrupt state
    shutil.rmtree(ollama_dir, ignore_errors=True)
    ollama_dir.mkdir(parents=True, exist_ok=True)
    
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                zip_path.write_bytes(response.content)
            
            # Integrity check
            size = zip_path.stat().st_size
            if size < MIN_SIZE:
                print(f"[system] Downloaded zip only {size} bytes — too small, corrupt. Retrying...")
                zip_path.unlink(missing_ok=True)
                continue
            
            # Extract
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(ollama_dir)
            
            # Find ollama.exe (may be nested in subfolder)
            exe_path = None
            for item in ollama_dir.rglob("ollama.exe"):
                if item.parent != ollama_dir:
                    exe_path = item
                    break
                exe_path = item  # already in root, take first match
            
            if exe_path and exe_path.parent != ollama_dir:
                shutil.move(str(exe_path), str(ollama_dir / "ollama.exe"))
            
            zip_path.unlink(missing_ok=True)
            
            final_exe = ollama_dir / "ollama.exe"
            if final_exe.exists():
                return {"status": "installed", "path": str(final_exe)}
            else:
                raise FileNotFoundError("ollama.exe not found after extraction")
        
        except Exception as e:
            print(f"[system] Install attempt {attempt+1} failed: {e}")
            if zip_path.exists():
                zip_path.unlink(missing_ok=True)
            if attempt == 2:
                return {"status": "error", "error": str(e)}
    
    return {"status": "error", "error": "All download attempts failed"}


# ─── Agent OS Context ────────────────────────────────────────────────────────


@router.get("/api/system/os")
async def get_agent_os_context():
    """Return detected/current OS context used in Director and Worker prompts."""
    return await agent_os.get_agent_os_config()


@router.put("/api/system/os")
async def update_agent_os_context(req: AgentOsUpdate):
    """Persist the OS mode used in Director and Worker prompts."""
    try:
        return await agent_os.set_agent_os_mode(req.mode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save OS setting: {exc}") from exc


# ─── Resource Monitor ────────────────────────────────────────────────────────


@router.get("/api/resources", response_model=ResourceStatus)
async def get_resources():
    """Get current system resource usage."""
    return resource_monitor.get_status()


# ─── Agent Worker Control ────────────────────────────────────────────────────


@router.post("/api/worker/start")
async def start_worker():
    """Start the agent worker loop."""
    await agent_worker.start()
    return {"status": "started", "activity": agent_worker.status_snapshot()}


@router.post("/api/worker/stop")
async def stop_worker():
    """Stop the agent worker loop."""
    await agent_worker.stop()
    return {"status": "stopped", "activity": agent_worker.status_snapshot()}


@router.get("/api/worker/status")
async def worker_status():
    """Get agent worker loop status plus live dispatcher activity."""
    return agent_worker.status_snapshot()


@router.get("/api/dispatcher/status")
async def dispatcher_status():
    """Board dispatcher snapshot for active model, task, tools, and Boss summary."""
    return agent_worker.status_snapshot()


@router.post("/api/runtime/unload")
async def unload_runtime_models():
    """Unload running llama.cpp model servers and clear runtime resolution cache."""
    await runtime.shutdown()
    runtime._resolved.clear()
    await activity_state.update_and_broadcast(
        action="runtime_unload",
        status="llama.cpp runtime models unloaded",
        active_runtime="unknown",
        boss_summary="Boss summary: idle llama.cpp model servers were unloaded to free VRAM/RAM.",
        event_type="runtime",
        event_title="Runtime unloaded",
    )
    return {"status": "unloaded", "activity": agent_worker.status_snapshot()}


# ─── WebSocket Endpoints ─────────────────────────────────────────────────────


@router.websocket("/ws/office")
async def ws_office(websocket: WebSocket):
    """WebSocket for real-time office updates (all agents)."""
    await ws_manager.connect_office(websocket)
    try:
        while True:
            # Keep connection alive, receive any client messages
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect_office(websocket)


@router.websocket("/ws/agent/{agent_id}")
async def ws_agent(websocket: WebSocket, agent_id: str):
    """WebSocket for real-time logs of a specific agent."""
    await ws_manager.connect_agent(websocket, agent_id)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect_agent(websocket, agent_id)
