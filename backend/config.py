"""
Configuration loader for MailLens.

Loads config.yaml, then overlays environment variable overrides for sensitive
values. Env vars use the convention MAILLENS_<SECTION>_<KEY> with underscores
replacing dots and all uppercase.
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class MailConfig(BaseModel):
    directory: str = "/mail"
    format: str = "auto"


class DatabaseConfig(BaseModel):
    host: str = "db"
    port: int = 5432
    name: str = "maillens"
    user: str = "maillens"
    password: str = "maillens"

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def sync_url(self) -> str:
        return f"postgresql+psycopg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class EmbeddingsConfig(BaseModel):
    provider: str = "ollama"
    model: str = "nomic-embed-text"
    ollama_url: str = "http://ollama:11434"
    chunk_size: int = 512
    batch_size: int = 64


class RetrievalConfig(BaseModel):
    top_k: int = 15
    similarity_threshold: float = 0.3
    include_thread_context: bool = True


class AnthropicConfig(BaseModel):
    api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    max_context_tokens: int = 180000
    temperature: float = 0.2


class OpenAIConfig(BaseModel):
    api_key: str = ""
    model: str = "gpt-4o"
    max_tokens: int = 4096
    max_context_tokens: int = 120000
    temperature: float = 0.2


class GeminiConfig(BaseModel):
    api_key: str = ""
    model: str = "gemini-2.5-flash"
    max_tokens: int = 8192
    max_context_tokens: int = 900000
    temperature: float = 0.2


class OllamaLLMConfig(BaseModel):
    url: str = "http://ollama:11434"
    model: str = "llama3.2:3b"
    max_tokens: int = 4096
    max_context_tokens: int = 8000
    temperature: float = 0.2


class LLMConfig(BaseModel):
    active_provider: str = "anthropic"
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    ollama: OllamaLLMConfig = Field(default_factory=OllamaLLMConfig)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    cors_allow_all: bool = True


class AppConfig(BaseModel):
    mail: MailConfig = Field(default_factory=MailConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)


# Environment variable override mappings.
# Maps env var names to dotted config paths.
ENV_OVERRIDES = {
    "MAILLENS_DB_PASSWORD": "database.password",
    "MAILLENS_DB_HOST": "database.host",
    "MAILLENS_DB_PORT": "database.port",
    "MAILLENS_DB_NAME": "database.name",
    "MAILLENS_DB_USER": "database.user",
    "MAILLENS_ANTHROPIC_API_KEY": "llm.anthropic.api_key",
    "MAILLENS_LLM_ANTHROPIC_API_KEY": "llm.anthropic.api_key",
    "MAILLENS_OPENAI_API_KEY": "llm.openai.api_key",
    "MAILLENS_LLM_OPENAI_API_KEY": "llm.openai.api_key",
    "MAILLENS_GEMINI_API_KEY": "llm.gemini.api_key",
    "MAILLENS_LLM_GEMINI_API_KEY": "llm.gemini.api_key",
    "MAILLENS_LLM_ACTIVE_PROVIDER": "llm.active_provider",
    "MAILLENS_MAIL_DIRECTORY": "mail.directory",
    "MAILLENS_MAIL_FORMAT": "mail.format",
}


def _set_nested(data: dict, dotted_path: str, value: str) -> None:
    """Set a value in a nested dict using a dotted path."""
    keys = dotted_path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    # Attempt type coercion for ints and floats
    final_key = keys[-1]
    current[final_key] = value


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """
    Load configuration from YAML file, then apply environment variable overrides.
    """
    if config_path is None:
        # Look for config.yaml in several places
        candidates = [
            Path("/app/config.yaml"),
            Path("config.yaml"),
            Path(__file__).parent.parent / "config.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                config_path = str(candidate)
                break

    raw: dict = {}
    if config_path and Path(config_path).exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

    # Apply environment variable overrides
    for env_var, dotted_path in ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is not None:
            _set_nested(raw, dotted_path, value)

    return AppConfig(**raw)


# Singleton config instance, loaded on first import
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config(config_path: Optional[str] = None) -> AppConfig:
    global _config
    _config = load_config(config_path)
    return _config
