"""SQLAlchemy ORM models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, Integer, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
import enum

from app.database import Base


class AgentStatus(str, enum.Enum):
    """Agent operational status."""
    idle = "idle"
    thinking = "thinking"
    working = "working"
    error = "error"


class TaskStatus(str, enum.Enum):
    """Kanban task status."""
    todo = "todo"
    in_progress = "in_progress"
    review = "review"
    done = "done"


class Agent(Base):
    """Agent (employee) in the virtual office."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    # Model configuration
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(50), default="ollama")
    port: Mapped[int] = mapped_column(Integer, default=11434)

    # Hardware preference: gpu | cpu | auto
    device: Mapped[str] = mapped_column(String(10), default="auto")

    # Status
    status: Mapped[str] = mapped_column(
        String(20), default=AgentStatus.idle.value
    )

    # Role
    is_director: Mapped[bool] = mapped_column(Boolean, default=False)

    # Skills (MCP tools) - stored as JSON list
    mcp_tools: Mapped[dict] = mapped_column(JSON, default=list)

    # Worker permission flags. Director ignores these (it has full control).
    # For Workers these gate which native tools / capabilities they can use
    # when executing Board tasks. Defaults are conservative: read-only.
    perm_read: Mapped[bool] = mapped_column(Boolean, default=True)
    perm_write: Mapped[bool] = mapped_column(Boolean, default=False)
    perm_execute: Mapped[bool] = mapped_column(Boolean, default=False)
    perm_mcp: Mapped[bool] = mapped_column(Boolean, default=False)
    perm_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    perm_subtasks: Mapped[bool] = mapped_column(Boolean, default=False)
    perm_question: Mapped[bool] = mapped_column(Boolean, default=True)

    # Metrics
    tasks_completed: Mapped[int] = mapped_column(Integer, default=0)
    last_active: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class MCPTool(Base):
    """MCP tool/instrument available for agents."""

    __tablename__ = "mcp_tools"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    command: Mapped[str] = mapped_column(String(500), nullable=False)
    args_schema: Mapped[dict] = mapped_column(JSON, default=dict)

    category: Mapped[str] = mapped_column(String(50), default="general")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AppSetting(Base):
    """Key-value store for app-wide settings (e.g., HF token).

    Used as a small, async-friendly persistence layer for things that don't
    deserve their own table and shouldn't live in `.env` (which is fragile on
    Windows due to encoding and gets clobbered by uvicorn `--reload`).
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Task(Base):
    """Task for the Kanban board."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    # Kanban state
    status: Mapped[str] = mapped_column(
        String(20), default=TaskStatus.todo.value
    )

    # Assignment
    assigned_agent_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )

    # Subtask hierarchy: NULL for top-level tasks, set to parent's id for subtasks.
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Lower number = higher priority. Used by Director when sequencing subtasks.
    priority: Mapped[int] = mapped_column(Integer, default=100)

    # Result
    result: Mapped[str] = mapped_column(Text, default="")
    logs: Mapped[dict] = mapped_column(JSON, default=list)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ChatTurn(Base):
    """Persisted Director chat message for long-lived project context."""

    __tablename__ = "chat_turns"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    runtime: Mapped[str] = mapped_column(String(20), default="")
    board_task_ids: Mapped[dict] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaskContextKind(str, enum.Enum):
    """Type of context entry attached to a Board task / subtask."""
    note = "note"             # free-form Worker note while executing
    plan = "plan"             # Director plan or sub-plan
    result = "result"         # Worker partial / final result
    question = "question"     # Worker question back to Director
    observation = "observation"  # tool output / state snapshot
    director = "director"     # message from Director to the Worker


class TaskContext(Base):
    """Per-task temporary context shared between Director and Workers.

    Workers append entries while executing a (sub)task; the Director reads
    them to adjust plans and create follow-up subtasks. Keeping context in
    a dedicated table prevents the Director's chat history from blowing
    up its context window with every Worker scratchpad note.
    """

    __tablename__ = "task_contexts"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    author_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), default=TaskContextKind.note.value)
    content: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class BackendLogEntry(Base):
    """Strict backend/system feedback log visible to the Director as hidden context.

    This is the audit trail for actions that the model requested and the backend
    actually executed (or rejected). The Director must use this table as the
    source of truth instead of trusting its own natural-language claims.
    """

    __tablename__ = "backend_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(20), default="INFO", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    error_details: Mapped[str] = mapped_column(Text, default="")
    action_type: Mapped[str] = mapped_column(String(80), default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="")
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class RuntimeMemoryEntry(Base):
    """Registry of currently-loaded local model instances.

    Owned by the Director's RAM-management service. One row per model that
    is intentionally pinned in memory. Lets the Director see which models
    are warm, who pinned them, and when they were last used, so it can make
    informed hire/fire decisions when budgeting RAM/VRAM.
    """

    __tablename__ = "runtime_memory"

    model_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    runtime: Mapped[str] = mapped_column(String(20), default="ollama")  # ollama | llamacpp
    host: Mapped[str] = mapped_column(String(200), default="")
    device: Mapped[str] = mapped_column(String(10), default="gpu")  # gpu | cpu | auto
    loaded_by_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    estimated_gb: Mapped[float] = mapped_column(Float, default=0.0)
    keep_alive_seconds: Mapped[int] = mapped_column(Integer, default=600)
    note: Mapped[str] = mapped_column(Text, default="")
    loaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
