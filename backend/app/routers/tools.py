"""MCP Tools endpoints."""

import asyncio
import shlex

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import MCPTool
from app.schemas import MCPToolCreate, MCPToolResponse, MCPToolExecute, MCPToolExecuteResponse

router = APIRouter(prefix="/api/mcp-tools", tags=["mcp-tools"])


@router.get("", response_model=list[MCPToolResponse])
async def list_tools(db: AsyncSession = Depends(get_db)):
    """Get all available MCP tools."""
    result = await db.execute(select(MCPTool))
    tools = result.scalars().all()
    return tools


@router.post("", response_model=MCPToolResponse, status_code=status.HTTP_201_CREATED)
async def create_tool(tool_data: MCPToolCreate, db: AsyncSession = Depends(get_db)):
    """Register a new MCP tool."""
    existing = await db.execute(select(MCPTool).where(MCPTool.id == tool_data.id))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail=f"Tool with id '{tool_data.id}' already exists"
        )

    tool = MCPTool(
        id=tool_data.id,
        name=tool_data.name,
        description=tool_data.description,
        command=tool_data.command,
        args_schema=tool_data.args_schema,
        category=tool_data.category,
        enabled=tool_data.enabled,
    )
    db.add(tool)
    await db.flush()
    await db.refresh(tool)
    return tool


@router.get("/{tool_id}", response_model=MCPToolResponse)
async def get_tool(tool_id: str, db: AsyncSession = Depends(get_db)):
    """Get tool by ID."""
    result = await db.execute(select(MCPTool).where(MCPTool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool


@router.delete("/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(tool_id: str, db: AsyncSession = Depends(get_db)):
    """Remove an MCP tool."""
    result = await db.execute(select(MCPTool).where(MCPTool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    await db.delete(tool)


@router.post("/{tool_id}/execute", response_model=MCPToolExecuteResponse)
async def execute_tool(
    tool_id: str,
    execute_data: MCPToolExecute,
    db: AsyncSession = Depends(get_db),
):
    """Execute an MCP tool command."""
    result = await db.execute(select(MCPTool).where(MCPTool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    if not tool.enabled:
        raise HTTPException(status_code=400, detail="Tool is disabled")

    # Build command by substituting args
    command = tool.command
    for key, value in execute_data.args.items():
        command = command.replace(f"{{{key}}}", str(value))

    # Run command with timeout
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=30.0
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return MCPToolExecuteResponse(
            tool_id=tool.id,
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode or 0,
            success=proc.returncode == 0,
        )
    except asyncio.TimeoutError:
        return MCPToolExecuteResponse(
            tool_id=tool.id,
            command=command,
            stdout="",
            stderr="Command timed out after 30 seconds",
            returncode=-1,
            success=False,
        )
    except Exception as e:
        return MCPToolExecuteResponse(
            tool_id=tool.id,
            command=command,
            stdout="",
            stderr=str(e),
            returncode=-1,
            success=False,
        )
