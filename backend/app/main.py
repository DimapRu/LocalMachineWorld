"""lmWorld - FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import async_session, init_db
from app.routers import (
    agents,
    chat,
    huggingface,
    mcp,
    logs,
    models,
    runtime_memory as runtime_memory_router,
    system,
    task_context as task_context_router,
    tasks,
    tools,
)
from app.seed import seed_tools
from app.services import backend_logs, worker_audit
from app.services.agent_worker import agent_worker
from app.services.huggingface import hf_service
from app.services.runtime import runtime
from app.services.settings_store import get_setting, set_setting

HF_TOKEN_KEY = "hf_token"


async def _load_hf_token_from_db():
    """Load HF token from SQLite into the in-memory hf_service.

    On first run, migrate any token still sitting in `.env` (settings.hf_token)
    into the DB so we have a single source of truth going forward.
    """
    stored = await get_setting(HF_TOKEN_KEY)
    if stored is None:
        # First run after upgrade: migrate from .env if present.
        env_token = (settings.hf_token or "").strip()
        if env_token:
            await set_setting(HF_TOKEN_KEY, env_token)
            hf_service.set_token(env_token)
            print("[lmWorld] Migrated HF token from .env to SQLite")
        else:
            await set_setting(HF_TOKEN_KEY, "")
    else:
        hf_service.set_token(stored)
        if stored:
            print("[lmWorld] HF token loaded from SQLite")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    # Startup
    await init_db()
    async with async_session() as session:
        deleted_ops_logs = await backend_logs.clear_startup_logs(session)
        await worker_audit.clear_startup_logs(session)
    await seed_tools()
    await _load_hf_token_from_db()
    await agent_worker.start()
    print(f"[lmWorld] {settings.app_name} started")
    print(f"[lmWorld] Database initialized")
    print(f"[lmWorld] Operational logs cleared ({deleted_ops_logs} DB rows)")
    print(f"[lmWorld] Worker audit JSONL cleared")
    print(f"[lmWorld] Default MCP tools seeded")
    print(f"[lmWorld] Agent worker started")
    yield
    # Shutdown
    await runtime.shutdown()
    await agent_worker.stop()
    print(f"[lmWorld] {settings.app_name} shutting down")


app = FastAPI(
    title=settings.app_name,
    description="Interactive Local AI Agent Orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware for frontend
# Allow all origins since this is a local desktop app (Tauri WebView uses various origins)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(agents.router)
app.include_router(tasks.router)
app.include_router(tools.router)
app.include_router(models.router)
app.include_router(huggingface.router)
app.include_router(system.router)
app.include_router(mcp.router)
app.include_router(chat.router)
app.include_router(logs.router)
app.include_router(runtime_memory_router.router)
app.include_router(task_context_router.router)


@app.get("/")
async def root():
    """Health check and landing page."""
    return {
        "name": settings.app_name,
        "version": "0.1.0",
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc",
    }
