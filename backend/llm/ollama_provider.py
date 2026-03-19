"""
Ollama local LLM provider.
"""

import json
from typing import AsyncGenerator, Optional

import httpx

from backend.config import get_config
from backend.llm.base import LLMProvider

OLLAMA_TIMEOUT = 300


class OllamaProvider(LLMProvider):
    def __init__(self):
        config = get_config()
        self.url = config.llm.ollama.url
        self.model = config.llm.ollama.model
        self.max_tokens = config.llm.ollama.max_tokens
        self.max_context_tokens = config.llm.ollama.max_context_tokens
        self.temperature = config.llm.ollama.temperature

    def _build_messages(
        self,
        system_prompt: str,
        context: str,
        user_message: str,
        conversation_history: Optional[list[dict]] = None,
    ) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": system_prompt}]

        if conversation_history:
            for turn in conversation_history:
                msgs.append({"role": turn["role"], "content": turn["content"]})

        full_message = (
            f"Here are the relevant emails from my mailbox:\n\n"
            f"{context}\n\n"
            f"My question: {user_message}"
        )
        msgs.append({"role": "user", "content": full_message})
        return msgs

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict],
        conversation_history: Optional[list[dict]] = None,
    ) -> str:
        budget = self._context_char_budget(system_prompt, user_message, conversation_history)
        context = self._format_context(context_messages, max_context_chars=budget)
        msgs = self._build_messages(system_prompt, context, user_message, conversation_history)

        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "options": {
                        "temperature": self.temperature,
                        "num_ctx": self.max_context_tokens,
                    },
                    "messages": msgs,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]

    async def stream(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict],
        conversation_history: Optional[list[dict]] = None,
    ) -> AsyncGenerator[str, None]:
        budget = self._context_char_budget(system_prompt, user_message, conversation_history)
        context = self._format_context(context_messages, max_context_chars=budget)
        msgs = self._build_messages(system_prompt, context, user_message, conversation_history)

        print(f"[ollama] sending {len(context):,} chars context to {self.model}")

        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    f"{self.url}/api/chat",
                    json={
                        "model": self.model,
                        "stream": True,
                        "options": {
                            "temperature": self.temperature,
                            "num_ctx": self.max_context_tokens,
                        },
                        "messages": msgs,
                    },
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line:
                            try:
                                data = json.loads(line)
                                content = data.get("message", {}).get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
        except Exception as e:
            print(f"[ollama] stream error: {type(e).__name__}: {e}")
            yield f"\n\n[Error from Ollama: {e}]"
