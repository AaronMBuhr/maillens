"""
Embedding generation via Ollama.
"""

import httpx
from typing import Optional

from backend.config import get_config


async def embed_texts(
    texts: list[str],
    model: Optional[str] = None,
    ollama_url: Optional[str] = None,
) -> list[list[float]]:
    """
    Generate embeddings for a list of texts using Ollama.

    Returns a list of embedding vectors (list of floats).
    """
    config = get_config()
    model = model or config.embeddings.model
    ollama_url = ollama_url or config.embeddings.ollama_url

    embeddings = []
    batch_size = config.embeddings.batch_size

    async with httpx.AsyncClient(timeout=120) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            # Ollama embedding endpoint handles one text at a time as of current API
            # but we can pipeline requests
            for text in batch:
                if not text or not text.strip():
                    embeddings.append([0.0] * 768)  # zero vector for empty texts
                    continue

                try:
                    resp = await client.post(
                        f"{ollama_url}/api/embed",
                        json={"model": model, "input": text},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    # Ollama returns {"embeddings": [[...]]} for /api/embed
                    emb = data.get("embeddings", [[]])[0]
                    embeddings.append(emb)
                except Exception as e:
                    print(f"Warning: Embedding failed for text chunk: {e}")
                    embeddings.append([0.0] * 768)

    return embeddings


def chunk_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """
    Split text into chunks for embedding.

    Uses a simple word-boundary approach. chunk_size is approximate
    token count (estimated as words * 1.3).
    """
    if not text:
        return []

    words = text.split()
    if not words:
        return []

    # Rough estimate: 1 token ~= 0.75 words, so chunk_size tokens ~= chunk_size * 0.75 words
    words_per_chunk = int(chunk_size * 0.75)
    overlap_words = int(overlap * 0.75)

    if len(words) <= words_per_chunk:
        return [text]

    chunks = []
    start = 0
    while start < len(words):
        end = start + words_per_chunk
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        start = end - overlap_words

    return chunks
