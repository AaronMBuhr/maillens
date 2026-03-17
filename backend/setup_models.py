"""
Ensure required Ollama models are available.
Run during container startup.
"""

import httpx
from backend.config import get_config


def main():
    config = get_config()
    ollama_url = config.embeddings.ollama_url
    model = config.embeddings.model

    print(f"Checking for embedding model '{model}' on Ollama at {ollama_url}...")

    try:
        # Check if model exists
        resp = httpx.get(f"{ollama_url}/api/tags", timeout=30)
        resp.raise_for_status()
        tags = resp.json()
        existing = [m["name"] for m in tags.get("models", [])]

        if any(model in name for name in existing):
            print(f"Model '{model}' already available.")
        else:
            print(f"Pulling model '{model}'... (this may take a few minutes on first run)")
            resp = httpx.post(
                f"{ollama_url}/api/pull",
                json={"name": model, "stream": False},
                timeout=600,
            )
            resp.raise_for_status()
            print(f"Model '{model}' pulled successfully.")

    except httpx.ConnectError:
        print(f"WARNING: Could not connect to Ollama at {ollama_url}.")
        print("Embeddings will not be available until Ollama is running.")
    except Exception as e:
        print(f"WARNING: Error checking/pulling model: {e}")


if __name__ == "__main__":
    main()
