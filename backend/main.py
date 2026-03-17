"""
MailLens FastAPI application.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import get_config
from backend.api.query import router as query_router
from backend.api.ingest import router as ingest_router
from backend.api.settings import router as settings_router
from backend.api.messages import router as messages_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    config = get_config()
    print("MailLens build 0.05")
    print(f"LLM provider: {config.llm.active_provider}")
    yield
    print("MailLens shutting down.")


app = FastAPI(
    title="MailLens",
    description="Email analysis powered by LLMs",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
config = get_config()
if config.server.cors_allow_all:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# API routes
app.include_router(query_router, prefix="/api/query", tags=["query"])
app.include_router(ingest_router, prefix="/api/ingest", tags=["ingest"])
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(messages_router, prefix="/api/messages", tags=["messages"])

# Serve frontend static files (built React app)
static_dir = Path("/app/static")
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
