"""
# PROMPT: Generate WebSocket manager for real-time event broadcasting to dashboard clients
# CHANGES MADE: Added connection manager with broadcast, echo keepalive, graceful disconnect handling.
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import List
import json

router = APIRouter()

class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, message: dict):
        data = json.dumps(message)
        for ws in list(self.active):
            try:
                await ws.send_text(data)
            except Exception:
                self.disconnect(ws)

manager = ConnectionManager()

@router.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            msg = await websocket.receive_text()
            # Echo back for keepalive/control. Clients should generally only receive.
            await websocket.send_text(json.dumps({"echo": msg}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Helper function for pipeline to call when events are produced
async def publish_event(event: dict):
    await manager.broadcast(event)
