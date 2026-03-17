"""
Ingestion API: trigger and monitor mail ingestion.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from backend.ingestion.pipeline import get_progress, run_ingestion

router = APIRouter()


class IngestRequest(BaseModel):
    mail_directory: Optional[str] = None
    incremental: bool = True


@router.post("/start")
async def start_ingestion(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
):
    """Start an ingestion run in the background."""
    progress = get_progress()
    if progress.status == "running":
        return {"status": "already_running", "progress": progress.to_dict()}

    background_tasks.add_task(
        _run_ingestion_async,
        request.mail_directory,
        request.incremental,
    )
    return {"status": "started"}


async def _run_ingestion_async(
    mail_directory: Optional[str],
    incremental: bool,
):
    """Wrapper to run ingestion as a background task."""
    await run_ingestion(mail_directory, incremental)


@router.get("/status")
async def ingestion_status():
    """Get current ingestion progress."""
    return get_progress().to_dict()
