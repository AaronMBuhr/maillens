"""
Ensure required Ollama models are available.
Run during container startup — pulls both the embedding model
and the LLM model (when active_provider is ollama).
"""

import httpx
from backend.config import get_config


def _ensure_model(ollama_url: str, model: str, existing: list[str], label: str) -> None:
    if any(model in name for name in existing):
        print(f"  {label} model '{model}' already available.")
    else:
        print(f"  Pulling {label} model '{model}'... (this may take a few minutes on first run)")
        resp = httpx.post(
            f"{ollama_url}/api/pull",
            json={"name": model, "stream": False},
            timeout=600,
        )
        resp.raise_for_status()
        print(f"  {label} model '{model}' pulled successfully.")


def main():
    config = get_config()
    ollama_url = config.embeddings.ollama_url

    try:
        resp = httpx.get(f"{ollama_url}/api/tags", timeout=30)
        resp.raise_for_status()
        existing = [m["name"] for m in resp.json().get("models", [])]

        print(f"Checking models on Ollama at {ollama_url}...")
        _ensure_model(ollama_url, config.embeddings.model, existing, "Embedding")

        if config.llm.active_provider == "ollama":
            _ensure_model(ollama_url, config.llm.ollama.model, existing, "LLM")

    except httpx.ConnectError:
        print(f"WARNING: Could not connect to Ollama at {ollama_url}.")
        print("Embeddings will not be available until Ollama is running.")
    except Exception as e:
        print(f"WARNING: Error checking/pulling model: {e}")


if __name__ == "__main__":
    main()
