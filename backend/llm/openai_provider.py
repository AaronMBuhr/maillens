"""
OpenAI GPT LLM provider.
"""

from typing import AsyncGenerator, Optional

from openai import AsyncOpenAI

from backend.config import get_config
from backend.llm.base import LLMProvider

_NO_TEMPERATURE_PREFIXES = ("o1", "o3", "gpt-5")


class OpenAIProvider(LLMProvider):
    def __init__(self):
        config = get_config()
        self.client = AsyncOpenAI(api_key=config.llm.openai.api_key)
        self.model = config.llm.openai.model
        self.max_tokens = config.llm.openai.max_tokens
        self.max_context_tokens = config.llm.openai.max_context_tokens
        self.temperature = config.llm.openai.temperature

    def _base_kwargs(self, messages: list[dict]) -> dict:
        kwargs: dict = {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "messages": messages,
        }
        if not any(self.model.startswith(p) for p in _NO_TEMPERATURE_PREFIXES):
            kwargs["temperature"] = self.temperature
        return kwargs

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

        response = await self.client.chat.completions.create(**self._base_kwargs(msgs))
        return response.choices[0].message.content

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

        stream = await self.client.chat.completions.create(
            **self._base_kwargs(msgs), stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content
