"""WebSocket manager for real-time updates."""

import json
from typing import Dict, Set

from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self):
        # Office-wide connections
        self.office_connections: Set[WebSocket] = set()
        # Per-agent connections: agent_id -> set of websockets
        self.agent_connections: Dict[str, Set[WebSocket]] = {}

    async def connect_office(self, websocket: WebSocket):
        """Connect a client to the office feed."""
        await websocket.accept()
        self.office_connections.add(websocket)

    async def disconnect_office(self, websocket: WebSocket):
        """Disconnect a client from the office feed."""
        self.office_connections.discard(websocket)

    async def connect_agent(self, websocket: WebSocket, agent_id: str):
        """Connect a client to a specific agent's feed."""
        await websocket.accept()
        if agent_id not in self.agent_connections:
            self.agent_connections[agent_id] = set()
        self.agent_connections[agent_id].add(websocket)

    async def disconnect_agent(self, websocket: WebSocket, agent_id: str):
        """Disconnect a client from an agent's feed."""
        if agent_id in self.agent_connections:
            self.agent_connections[agent_id].discard(websocket)
            if not self.agent_connections[agent_id]:
                del self.agent_connections[agent_id]

    async def broadcast_office(self, message: dict):
        """Broadcast a message to all office connections."""
        dead_connections = set()
        for connection in self.office_connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead_connections.add(connection)
        self.office_connections -= dead_connections

    async def send_to_agent(self, agent_id: str, message: dict):
        """Send a message to all connections watching a specific agent."""
        if agent_id not in self.agent_connections:
            return
        dead_connections = set()
        for connection in self.agent_connections[agent_id]:
            try:
                await connection.send_json(message)
            except Exception:
                dead_connections.add(connection)
        self.agent_connections[agent_id] -= dead_connections

    async def broadcast_agent_status(self, agent_id: str, status: str, data: dict = None):
        """Broadcast agent status change to office and agent-specific feeds."""
        message = {
            "type": "agent_status",
            "agent_id": agent_id,
            "status": status,
            **(data or {}),
        }
        await self.broadcast_office(message)
        await self.send_to_agent(agent_id, message)


# Singleton instance
ws_manager = ConnectionManager()
