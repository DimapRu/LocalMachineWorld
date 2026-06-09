# lmWorld — Project Status Report

## 1. What Was Done

### Phase 1: Core Foundation
- FastAPI backend with SQLAlchemy 2.0 + aiosqlite async database
- Agent, Task, MCPTool ORM models
- Pydantic schemas for validation
- Seed data for default MCP tools

### Phase 2: HuggingFace Model Catalog + Hardware Filters
- `HuggingFaceService` — search models, check VRAM/RAM compatibility, estimate params
- `ResourceMonitor` — detect GPU (NVIDIA) and RAM usage
- Endpoints:
  - `GET /api/hf-models/recommended` — hardware-aware suggestions
  - `GET /api/hf-models/gpu-compatible`
  - `GET /api/hf-models/cpu-compatible`
  - `GET /api/hf-models/filter-options` — returns hardware info + model suggestions by VRAM bucket
  - `GET /api/hf-models/estimate` — size estimation

### Phase 3: MCP (Model Context Protocol)
- **MCP Tools CRUD** (`/api/tools`) — create, list, delete, execute registered tools
- **MCP File-System Router** (`/api/mcp/*`) — secure file operations:
  - `POST /api/mcp/read` — read file contents
  - `POST /api/mcp/write` — write/create files
  - `POST /api/mcp/edit` — string replacement in files
  - `POST /api/mcp/list` — list directory entries
  - `POST /api/mcp/delete` — delete files/directories
  - `POST /api/mcp/terminal` — execute shell commands with optional `cwd`
  - Security: `_resolve_path()` prevents directory traversal outside workspace
- **MCP Tool Execution** (`POST /api/tools/{tool_id}/execute`) — runs registered tool commands

### Phase 4: Frontend (Next.js 14 + TailwindCSS)
- **OfficePage** — agent cards with status, role, device indicator, MCP tool tags
- **HirePage** — agent creation form with:
  - Hardware detection display (RAM, VRAM, GPU available)
  - Model selection dropdown (filtered by hardware)
  - Provider selection (Ollama / HuggingFace / OpenAI)
  - **Device selection** (`auto` / `gpu` / `cpu`) with dynamic hints
  - System prompt, auto-accept toggle
- **AgentProfile** — view/edit agent details, device editing, task history, MCP tools, delete
- **TasksPage** — Kanban board (todo / in_progress / done), create tasks, move between columns
- **ToolsPage** — MCP tool registry, execute tools, view results
- **Sidebar** — navigation between pages
- **ModelDownload component** — native Tauri download for HuggingFace models

### Phase 5: Desktop Application (Tauri v2)
- Tauri + Next.js integration compiled successfully
- Native commands:
  - `download_model` — download with progress tracking
  - `cancel_download`
  - `get_models_dir`
  - `list_downloaded_models`
- Build artifacts:
  - `lmworld.exe` (portable)
  - `lmWorld_0.1.0_x64_en-US.msi` (Windows installer)
  - `lmWorld_0.1.0_x64-setup.exe` (NSIS installer)

### Additional Features
- **Ollama integration** — generate, list models, pull models, check availability
- **Device selection per agent** — `gpu` | `cpu` | `auto`, stored in DB, displayed in UI
- **Model pull on hire** — `ollama_service.pull_model()` called during agent creation
- **Start.bat launcher** — attempts to start backend then desktop app (has path issues)

---

## 2. Current Architecture

```
lmWorld/
├── backend/           # FastAPI + SQLAlchemy + aiosqlite
│   ├── app/
│   │   ├── main.py              # App entry, router registration
│   │   ├── models.py            # Agent, Task, MCPTool ORM
│   │   ├── schemas.py           # Pydantic schemas
│   │   ├── database.py          # Async engine + session
│   │   ├── seed.py              # Default MCP tools seed
│   │   ├── config.py            # Settings
│   │   ├── routers/
│   │   │   ├── agents.py        # Agent CRUD + hire/fire/director
│   │   │   ├── tasks.py         # Task CRUD
│   │   │   ├── tools.py         # MCP tools CRUD + execute
│   │   │   ├── mcp.py           # File-system + terminal MCP
│   │   │   ├── huggingface.py   # HF model catalog + hardware filter
│   │   │   ├── models.py        # Ollama model list/status
│   │   │   └── system.py        # System resources
│   │   └── services/
│   │       ├── ollama.py        # Ollama API client
│   │       ├── huggingface.py   # HF Hub API client
│   │       ├── resources.py     # RAM/VRAM monitoring
│   │       └── websocket.py     # WebSocket manager (placeholder)
│   └── requirements.txt
├── frontend/          # Next.js 14 + TailwindCSS
│   ├── src/
│   │   ├── app/
│   │   │   ├── page.tsx         # Main layout with sidebar
│   │   │   ├── layout.tsx
│   │   │   └── globals.css
│   │   ├── components/
│   │   │   ├── OfficePage.tsx
│   │   │   ├── HirePage.tsx
│   │   │   ├── AgentProfile.tsx
│   │   │   ├── TasksPage.tsx
│   │   │   ├── ToolsPage.tsx
│   │   │   ├── Sidebar.tsx
│   │   │   └── ModelDownload.tsx
│   │   └── lib/
│   │       └── tauri.ts         # Tauri API wrappers
│   └── src-tauri/               # Rust Tauri app
│       ├── src/
│       │   ├── main.rs
│       │   ├── lib.rs
│       │   └── commands.rs      # Native download commands
│       ├── Cargo.toml
│       └── tauri.conf.json
├── docs/
│   └── ARCHITECTURE.md
├── start.bat            # Launcher (has issues)
└── PROJECT_STATUS.md    # This file
```

---

## 3. What Works Well

| Feature | Status |
|---------|--------|
| Backend API | Stable, all endpoints functional |
| Database | SQLite with async operations, schema migrations manual |
| Agent CRUD | Create, read, update, delete, hire, fire, director toggle |
| Task Kanban | Create tasks, move between statuses |
| MCP Tools registry | CRUD + execute registered commands |
| MCP File-system | Read/write/edit/list/delete/terminal with path security |
| Hardware detection | GPU (NVIDIA) and RAM detection works |
| Model suggestions | Backend returns correct hardware-filtered model lists |
| Device selection | Stored per-agent, editable, UI hints work |
| Ollama integration | Pull model on hire, generate, list, status check |
| Tauri build | MSI + NSIS + portable exe generated successfully |
| Frontend UI | Responsive dark theme, all pages navigable |

---

## 4. What Works Poorly / Known Bugs

### Critical
| Issue | Details |
|-------|---------|
| **HirePage model dropdown empty** | Backend `/api/hf-models/filter-options` returns correct data, but frontend shows "No models found". `buildRecommendedModels()` logic may fail silently or JSX conditional renders incorrectly. Needs debugging. |
| **start.bat path issues** | Launcher fails with "The system cannot find the path specified" when run from wrong directory. Needs absolute paths or working-directory check. |

### Medium
| Issue | Details |
|-------|---------|
| **No actual AI inference loop** | Agents are DB records only. No background worker processes models, runs prompts, or coordinates tasks. |
| **Director → Worker coordination not implemented** | Phase 3 (Coordination) is pending. No task assignment, no agent-to-agent communication. |
| **WebSocket placeholder** | `websocket.py` exists but is not used for real-time updates. |
| **MCP tool execution is shell-only** | No structured tool calling (function calling schema). Tools just run shell commands. |
| **No agent memory / context** | Agents have `system_prompt` but no conversation history, no RAG, no persistent context between sessions. |
| **Hardcoded localhost URLs** | Frontend fetches `http://127.0.0.1:8001/...` — will break in production/Tauri. Should use relative URLs or Tauri API. |

### Minor
| Issue | Details |
|-------|---------|
| **No model download progress in UI** | `ModelDownload` component exists but integration is basic. |
| **No error boundaries** | Frontend crashes on unhandled errors. |
| **No input validation on MCP terminal** | Dangerous commands can be executed (e.g., `rm -rf /`). Sandbox is path-only, not command-sandboxed. |
| **Database is SQLite** | Will not scale. Fine for local desktop app but limits concurrent agents. |
| **No tests** | Zero unit/integration tests. |

---

## 5. What Needs to Be Done (Priority Order)

### Phase 3: Coordination (High Priority)
1. **Agent Worker Loop** — Background async task processor that:
   - Polls for pending tasks assigned to an agent
   - Loads the agent's model (Ollama/HF)
   - Runs inference with system prompt + task context
   - Updates task status and stores result
2. **Director Assignment** — When a task is created without assignee:
   - Director agent analyzes task
   - Chooses best worker based on role/model/availability
   - Assigns task via API
3. **Agent Communication** — Optional: agents can message each other, delegate sub-tasks.

### Bug Fixes (High Priority)
4. **Fix HirePage model dropdown** — Debug why `recommendedModels` is empty despite valid backend data. Check `buildRecommendedModels()` return, check JSX conditional rendering.
5. **Fix start.bat** — Use `%~dp0` for script directory, validate paths, show clear error messages.

### Production Hardening (Medium Priority)
6. **Replace hardcoded localhost** — Use environment variables or Tauri config for API base URL.
7. **Add error boundaries** — Wrap pages in React error boundaries.
8. **Command sandboxing** — Whitelist/blacklist dangerous commands in MCP terminal.
9. **Add tests** — Backend pytest suite, frontend basic component tests.

### Features (Low Priority)
10. **Agent memory / RAG** — Vector DB for document storage, retrieval-augmented generation.
11. **Real-time updates** — WebSocket for live task status, agent activity.
12. **Multi-model orchestration** — Agents with different models collaborating.
13. **Plugin system** — Allow users to register custom MCP tools via UI.
14. **Auto-update** — Tauri updater for desktop app.

---

## 6. Quick Start (Current)

```bash
# Terminal 1 — Backend
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload

# Terminal 2 — Frontend (dev)
cd frontend
npm run dev

# Or build desktop app
cd frontend
npm run tauri-build
```

---

## 7. Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2.0, aiosqlite, httpx, Pydantic |
| Frontend | Next.js 14, React 18, TypeScript, TailwindCSS |
| Desktop | Tauri v2, Rust |
| AI Runtime | Ollama (local LLM) |
| Model Hub | HuggingFace Hub |
| DB | SQLite (async via aiosqlite) |

---

*Last updated: 2026-05-23*
