"""
Settings API: read and update runtime configuration.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_config
from backend.llm.factory import clear_provider_cache
from backend.storage.db import get_session
from backend.storage.queries import get_folders, get_message_count, get_senders

router = APIRouter()


class ProviderInfo(BaseModel):
    model: str
    has_key: bool = True
    max_tokens: int = 0
    max_context_tokens: int = 0
    url: Optional[str] = None


class SettingsResponse(BaseModel):
    active_provider: str
    available_providers: list[str]
    providers: dict[str, ProviderInfo]
    embedding_model: str
    retrieval_top_k: int
    retrieval_similarity_threshold: float
    mail_directory: str


class UpdateProviderRequest(BaseModel):
    provider: str  # anthropic, openai, ollama


@router.get("/", response_model=SettingsResponse)
async def get_settings():
    """Get current settings (sensitive values redacted)."""
    config = get_config()
    providers = {
        "anthropic": ProviderInfo(
            model=config.llm.anthropic.model,
            has_key=bool(config.llm.anthropic.api_key),
            max_tokens=config.llm.anthropic.max_tokens,
            max_context_tokens=config.llm.anthropic.max_context_tokens,
        ),
        "openai": ProviderInfo(
            model=config.llm.openai.model,
            has_key=bool(config.llm.openai.api_key),
            max_tokens=config.llm.openai.max_tokens,
            max_context_tokens=config.llm.openai.max_context_tokens,
        ),
        "gemini": ProviderInfo(
            model=config.llm.gemini.model,
            has_key=bool(config.llm.gemini.api_key),
            max_tokens=config.llm.gemini.max_tokens,
            max_context_tokens=config.llm.gemini.max_context_tokens,
        ),
        "ollama": ProviderInfo(
            model=config.llm.ollama.model,
            max_tokens=config.llm.ollama.max_tokens,
            max_context_tokens=config.llm.ollama.max_context_tokens,
            url=config.llm.ollama.url,
        ),
    }
    return SettingsResponse(
        active_provider=config.llm.active_provider,
        available_providers=list(providers.keys()),
        providers=providers,
        embedding_model=config.embeddings.model,
        retrieval_top_k=config.retrieval.top_k,
        retrieval_similarity_threshold=config.retrieval.similarity_threshold,
        mail_directory=config.mail.directory,
    )


@router.post("/provider")
async def update_provider(request: UpdateProviderRequest):
    """Switch the active LLM provider at runtime."""
    config = get_config()
    valid = ["anthropic", "openai", "gemini", "ollama"]
    if request.provider not in valid:
        return {"error": f"Invalid provider. Choose from: {valid}"}

    config.llm.active_provider = request.provider
    clear_provider_cache()
    return {"status": "ok", "active_provider": request.provider}


@router.get("/stats")
async def get_stats(session: AsyncSession = Depends(get_session)):
    """Get database statistics."""
    count = await get_message_count(session)
    folders = await get_folders(session)
    senders = await get_senders(session, limit=20)
    return {
        "message_count": count,
        "folder_count": len(folders),
        "folders": folders,
        "top_senders": senders,
    }
