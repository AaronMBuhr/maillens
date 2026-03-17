"""
Anthropic Claude LLM provider.
"""

from typing import AsyncGenerator, Optional

import anthropic

from backend.config import get_config
from backend.llm.base import LLMProvider


class AnthropicProvider(LLMProvider):
    def __init__(self):
        config = get_config()
        self.client = anthropic.AsyncAnthropic(
            api_key=config.llm.anthropic.api_key,
        )
        self.model = config.llm.anthropic.model
        self.max_tokens = config.llm.anthropic.max_tokens
        self.max_context_tokens = config.llm.anthropic.max_context_tokens
        self.temperature = config.llm.anthropic.temperature

    def _build_messages(
        self,
        context: str,
        user_message: str,
        conversation_history: Optional[list[dict]] = None,
    ) -> list[dict]:
        msgs: list[dict] = []

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
        msgs = self._build_messages(context, user_message, conversation_history)

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=msgs,
        )

        return response.content[0].text

    async def stream(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict],
        conversation_history: Optional[list[dict]] = None,
    ) -> AsyncGenerator[str, None]:
        budget = self._context_char_budget(system_prompt, user_message, conversation_history)
        context = self._format_context(context_messages, max_context_chars=budget)
        msgs = self._build_messages(context, user_message, conversation_history)

        async with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=msgs,
        ) as stream:
            async for text in stream.text_stream:
                yield text
