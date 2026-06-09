# lmWorld

Local desktop orchestrator for AI agents: Director + Workers, Board tasks, local model runtimes, tool execution, and runtime memory controls.

## What This Project Does

lmWorld runs a local AI-agent office:

- Director agent coordinates work and delegates executable tasks to Workers.
- Worker agents run isolated task sessions with safe backend-owned tools.
- Board tracks tasks, progress, dispatcher state, tool activity, and recent events.
- Ollama is the primary runtime for local models.
- llama.cpp / llama-server is used as a GGUF fallback for models Ollama cannot serve.
- Models without native tool support can still use the text-tool fallback protocol.
- The Hire Agent UI warns when a model does not support native tools.

Director/Worker behavior is not baked into model weights. A downloaded model stays a normal model; lmWorld injects Director instructions, Board context, memory summaries, and tool schemas at runtime from the backend code.

## Current Stack

- Backend: Python 3.11+, FastAPI, async SQLAlchemy, SQLite, Uvicorn.
- Frontend: Next.js 14, React 18, TypeScript, TailwindCSS.
- Desktop shell: Tauri v2 / Rust.
- Local runtimes: Ollama first, llama.cpp / llama-server fallback.
- Model catalog/downloads: Hugging Face API, Ollama pulls, GGUF downloads.
- Monitoring: psutil, GPUtil/pynvml.
- Local tools: built-in MCP-like REST tools and Worker tool schemas.

See `READIT.txt` for the implementation board/status map.

## Repository Hygiene

Do not commit local runtime state, secrets, downloaded models, generated dependencies, or build output:

- `backend/.env`
- `backend/lmworld.db`
- `backend/*.log`
- `logs/`
- `models/`
- `ollama/`
- `backend/.venv/`
- `frontend/node_modules/`
- `frontend/.next/`
- `frontend/out/`
- `frontend/src-tauri/target/`
- `frontend/src-tauri/gen/schemas/`

Use `backend/.env.example` as the safe template for local settings. Keep `HF_TOKEN` empty in committed files; add private tokens only through your local `.env` or the app Settings UI.

## Requirements

For development from a fresh clone:

- Windows 10/11 recommended for `start.bat`.
- Python 3.11+ available as `python`.
- Node.js 18+ and npm.
- Rust toolchain only when building the Tauri desktop app with `start.bat /build`.
- Internet access for npm/pip packages and Ollama/model downloads.
- Optional: NVIDIA GPU drivers/CUDA for GPU acceleration.

## Quick Start On Windows

From the project root:

```bat
start.bat
```

The launcher will:

1. Create `models/` if missing.
2. Download portable Ollama into `ollama/` if missing.
3. Start Ollama on `127.0.0.1:11434` when needed.
4. Create `backend/.venv` if missing.
5. Install backend dependencies from `backend/requirements.txt`.
6. Start FastAPI on `127.0.0.1:8001`.
7. Reuse an existing desktop build when `lmworld.exe` is present.
8. Install frontend dependencies if the dev fallback needs them.
9. Start the Next.js dev server at `http://localhost:3000` when no desktop build exists.

To build or rebuild the Tauri desktop app manually, run:

```bat
start.bat /build
```

## Manual Development Start

Backend:

```bat
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

Frontend:

```bat
cd frontend
npm install
npm run dev
```

Tauri dev shell:

```bat
cd frontend
npm run tauri-dev
```

## First App Setup

1. Open Settings and confirm OS mode if needed.
2. Add a Hugging Face token only if you need private/gated models.
3. Open Hire Agent.
4. Pick or search a model.
5. Watch for the tool-support warning icon near models without native tools.
6. Create one Director agent and at least one Worker agent.
7. Use Chat to talk to the Director.
8. Use Board to inspect delegated Worker tasks and tool activity.

## Models And Director Instructions

Model downloads do not include lmWorld's Director logic. The app adds that behavior at runtime:

- Director prompt and orchestration rules live in `backend/app/routers/chat.py`.
- Director tools live in `backend/app/services/director_tools.py`.
- Worker tools live in `backend/app/services/worker_tools.py`.
- Per-task memory/context lives in `backend/app/services/task_context.py`.
- Runtime memory control lives in `backend/app/services/runtime_memory.py`.

So a GitHub user only needs the project code plus a compatible local model. The model itself is not fine-tuned or modified.

## Important Release Notes

- This repository should be committed without local `.env`, databases, logs, model files, Ollama binaries, dependency folders, or build folders.
- A clean database is created automatically on backend startup.
- `start.bat` bootstraps the backend virtual environment and downloads portable Ollama on first launch.
- `start.bat` does not rebuild Tauri on every run; use `start.bat /build` after frontend or desktop-shell changes.
- `backend/app/seed.py` intentionally does not seed external placeholder MCP tools.
- The current MCP implementation is local MCP-like tooling; real external MCP server integration is planned.
- `PROJECT_STATUS.md` may contain older historical notes; `READIT.txt` is the current stack board.

## Useful Commands

Backend syntax check:

```bat
cd backend
.venv\Scripts\python -m py_compile app\main.py app\services\llamacpp.py app\services\agent_worker.py
```

Frontend lint/build:

```bat
cd frontend
npm run lint
npm run build
```

Tauri build:

```bat
cd frontend
npm run tauri-build
```
