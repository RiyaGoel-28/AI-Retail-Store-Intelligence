"""
# PROMPT: Generate a pytest test for the WebSocket /ws/live endpoint. Verify
#         that sending a 'ping' message returns a response containing 'echo'
#         to confirm the connection is active and bidirectional communication
#         works through FastAPI's TestClient.
# CHANGES MADE: Kept the test minimal — only verifies echo functionality since
#               full live event streaming requires a running detection pipeline.
#               The echo response confirms the WebSocket upgrade and
#               bidirectional communication are working correctly. Did NOT add
#               assertions on broadcast fan-out because that is covered
#               implicitly by the `/events/ingest` flow tested elsewhere, and
#               testing it here would require a fixture for two concurrent
#               clients which is brittle in TestClient's sync context.

Tests for WebSocket live event feed.

Verifies that the /ws/live WebSocket endpoint is functional and responds to
client messages with an echo — confirming the connection is alive and the
server is ready to push real-time events.
"""
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_ws_echo():
    with client.websocket_connect("/ws/live") as websocket:
        websocket.send_text("ping")
        data = websocket.receive_text()
        assert "echo" in data