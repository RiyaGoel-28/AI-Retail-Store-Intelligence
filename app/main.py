"""
# PROMPT: Generate a FastAPI application with structured logging,
# CORS middleware, and endpoints for store intelligence analytics.
# CHANGES MADE: Added dashboard serving, CORS for all origins,
# structured logging middleware, graceful error handling.
"""

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import time
import logging
import uuid
import json
import os

from app import ingestion, metrics, funnel, anomalies, health, ws, heatmap
from app.models import init_db

# Structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("store_intelligence")

app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="AI-powered retail analytics from CCTV footage"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Structured logging middleware with graceful error handling
@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id = str(uuid.uuid4())[:8]
    start = time.time()

    # Extract store_id from path (e.g., /stores/STORE_BLR_002/metrics)
    store_id = None
    path_parts = request.url.path.strip("/").split("/")
    if "stores" in path_parts:
        idx = path_parts.index("stores")
        if idx + 1 < len(path_parts):
            store_id = path_parts[idx + 1]

    # Extract event_count for ingest endpoint
    event_count = None
    if request.url.path.endswith("/ingest") and request.method == "POST":
        try:
            body = await request.body()
            payload = json.loads(body)
            event_count = len(payload.get("events", []))
            request._body = body
        except Exception:
            event_count = None

    try:
        response = await call_next(request)
    except Exception as exc:
        latency_ms = round((time.time() - start) * 1000, 2)
        logger.exception(
            f"trace_id={trace_id} | method={request.method} | path={request.url.path} | "
            f"store_id={store_id} | event_count={event_count} | status=500 | "
            f"latency_ms={latency_ms} | error={exc}"
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": "service_unavailable",
                "detail": "temporary failure",
                "trace_id": trace_id
            }
        )

    latency_ms = round((time.time() - start) * 1000, 2)
    logger.info(
        f"trace_id={trace_id} | "
        f"method={request.method} | "
        f"path={request.url.path} | "
        f"store_id={store_id} | "
        f"event_count={event_count} | "
        f"status={response.status_code} | "
        f"latency_ms={latency_ms}"
    )
    response.headers["X-Trace-ID"] = trace_id
    return response


# Include routers
app.include_router(ingestion.router, prefix="/events", tags=["Events"])
app.include_router(metrics.router, tags=["Analytics"])
app.include_router(funnel.router, tags=["Analytics"])
app.include_router(anomalies.router, tags=["Analytics"])
app.include_router(heatmap.router, tags=["Analytics"])
app.include_router(health.router, tags=["System"])
app.include_router(ws.router, tags=["Realtime"])


# ── Dashboard routes ─────────

@app.get("/dashboard", tags=["Dashboard"])
def serve_dashboard():
    """Serve the analytics dashboard HTML."""
    dashboard_path = os.path.join(os.path.dirname(__file__), "..", "dashboard", "index.html")
    if os.path.exists(dashboard_path):
        return FileResponse(dashboard_path, media_type="text/html")
    return {"error": "Dashboard not found", "path": dashboard_path}


# Mount static files and root route
os.makedirs("dashboard", exist_ok=True)

if os.path.exists("dashboard/index.html"):
    app.mount("/static", StaticFiles(directory="dashboard"), name="dashboard")

    @app.get("/", tags=["Dashboard"])
    def root():
        """Serve the dashboard index."""
        return FileResponse("dashboard/index.html")
else:
    @app.get("/", tags=["Dashboard"])
    def root():
        """API root — returns basic info when dashboard is not built."""
        return {
            "message": "Store Intelligence API",
            "docs": "/docs",
            "health": "/health"
        }


# Initialize database
init_db()