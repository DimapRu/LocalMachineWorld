"""Pydantic schemas for API request/response validation."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models import AgentStatus, TaskStatus


# ─── Agent Schemas ───────────────────────────────────────────────────────────


class AgentCreate(BaseModel):
    """Schema for creating a new agent."""
    model_config = {"protected_namespaces": ()}

    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    model_name: str = Field(..., min_length=1, max_length=200)
    model_provider: str = "ollama"
    port: int = 11434
    device: str = "auto"   # gpu | cpu | auto
    is_director: bool = False
    mcp_tools: list[str] = []
    perm_read: bool = True
    perm_write: bool = False
    perm_execute: bool = False
    perm_mcp: bool = False
    perm_mode: bool = False
    perm_subtasks: bool = False
    perm_question: bool = True


class AgentUpdate(BaseModel):
    """Schema for updating an agent."""
    model_config = {"protected_namespaces": ()}

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    model_name: Optional[str] = Field(None, min_length=1, max_length=200)
    model_provider: Optional[str] = None
    port: Optional[int] = None
    device: Optional[str] = None
    is_director: Optional[bool] = None
    mcp_tools: Optional[list[str]] = None
    perm_read: Optional[bool] = None
    perm_write: Optional[bool] = None
    perm_execute: Optional[bool] = None
    perm_mcp: Optional[bool] = None
    perm_mode: Optional[bool] = None
    perm_subtasks: Optional[bool] = None
    perm_question: Optional[bool] = None


class AgentResponse(BaseModel):
    """Schema for agent response."""
    model_config = {"protected_namespaces": (), "from_attributes": True}

    id: str
    name: str
    description: str
    model_name: str
    model_provider: str
    port: int
    device: str
    status: str
    is_director: bool
    mcp_tools: list[str]
    perm_read: bool
    perm_write: bool
    perm_execute: bool
    perm_mcp: bool
    perm_mode: bool
    perm_subtasks: bool
    perm_question: bool
    tasks_completed: int
    last_active: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime



# ─── Task Schemas ────────────────────────────────────────────────────────────


class TaskCreate(BaseModel):
    """Schema for creating a new task."""
    title: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    assigned_agent_id: Optional[str] = None


class TaskUpdate(BaseModel):
    """Schema for updating a task."""
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    assigned_agent_id: Optional[str] = None
    result: Optional[str] = None


class TaskResponse(BaseModel):
    """Schema for task response."""
    model_config = {"from_attributes": True}

    id: str
    title: str
    description: str
    status: str
    assigned_agent_id: Optional[str] = None
    result: str
    logs: list[str]
    created_at: datetime
    updated_at: datetime


# ─── MCPTool Schemas ─────────────────────────────────────────────────────────


class MCPToolCreate(BaseModel):
    """Schema for creating an MCP tool."""
    id: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    command: str = Field(..., min_length=1, max_length=500)
    args_schema: dict = {}
    category: str = "general"
    enabled: bool = True


class MCPToolResponse(BaseModel):
    """Schema for MCP tool response."""
    model_config = {"from_attributes": True}

    id: str
    name: str
    description: str
    command: str
    args_schema: dict
    category: str
    enabled: bool


class MCPToolExecute(BaseModel):
    """Schema for executing an MCP tool."""
    args: dict = {}


class MCPToolExecuteResponse(BaseModel):
    """Schema for MCP tool execution response."""
    tool_id: str
    command: str
    stdout: str
    stderr: str
    returncode: int
    success: bool


# ─── Resource Schemas ────────────────────────────────────────────────────────


class ResourceStatus(BaseModel):
    """System resource status."""
    ram_total_gb: float
    ram_used_gb: float
    ram_percent: float
    vram_total_gb: Optional[float] = None
    vram_used_gb: Optional[float] = None
    vram_percent: Optional[float] = None
    gpu_available: bool = False
