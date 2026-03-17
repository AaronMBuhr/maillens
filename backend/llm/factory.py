"""
LLM provider factory. Returns the active provider based on configuration.
"""

from backend.config import get_config
from backend.llm.base import LLMProvider


_provider_cache: dict[str, LLMProvider] = {}


def get_llm_provider(provider_name: str | None = None) -> LLMProvider:
    """
    Get an LLM provider instance.

    Uses the active_provider from config if provider_name is not specified.
    Caches instances so repeated calls return the same provider.
    """
    config = get_config()
    name = provider_name or config.llm.active_provider

    if name in _provider_cache:
        return _provider_cache[name]

    if name == "anthropic":
        from backend.llm.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()
    elif name == "openai":
        from backend.llm.openai_provider import OpenAIProvider
        provider = OpenAIProvider()
    elif name == "gemini":
        from backend.llm.gemini_provider import GeminiProvider
        provider = GeminiProvider()
    elif name == "ollama":
        from backend.llm.ollama_provider import OllamaProvider
        provider = OllamaProvider()
    else:
        raise ValueError(
            f"Unknown LLM provider: '{name}'. "
            f"Valid options: anthropic, openai, gemini, ollama"
        )

    _provider_cache[name] = provider
    return provider


def clear_provider_cache():
    """Clear cached providers (useful after config reload)."""
    _provider_cache.clear()


def get_active_provider_config():
    """Return the config object for the currently active LLM provider."""
    config = get_config()
    name = config.llm.active_provider
    return getattr(config.llm, name, None)


# System prompt for email analysis
SYSTEM_PROMPT = """You are MailLens, an AI assistant that helps users search and analyze their email archive.

You will be given a set of relevant emails retrieved from the user's mailbox, along with a question or request from the user. Each email is labeled [Email N] with its From, To, Date, and Subject.

Guidelines:
- Answer based ONLY on the provided email content. Do not make up information.
- ALWAYS cite the specific emails that support your answer. Use the format: [Email N] followed by the sender, date, and subject so the user can locate the original. For example: "According to [Email 3] (from Jane Doe, 2024-03-15, 'Q3 Budget Review')..."
- If the retrieved emails don't contain enough information to answer the question, say so clearly.
- Be concise but thorough. If the user asks for a summary, provide one. If they ask for specifics, give details.
- When asked to find patterns, timelines, or trends, organize your response clearly.
- If emails contain contradictory information, point that out and cite both sources.
- Preserve important details like dates, names, amounts, and commitments accurately.
"""
