"""
Google Gemini LLM provider.
"""

from typing import AsyncGenerator, Optional

from google import genai
from google.genai import types

from backend.config import get_config
from backend.llm.base import LLMProvider


class GeminiProvider(LLMProvider):
    def __init__(self):
        config = get_config()
        self.client = genai.Client(api_key=config.llm.gemini.api_key)
        self.model = config.llm.gemini.model
        self.max_tokens = config.llm.gemini.max_tokens
        self.max_context_tokens = config.llm.gemini.max_context_tokens
        self.temperature = config.llm.gemini.temperature

    def _build_contents(
        self,
        context: str,
        user_message: str,
        conversation_history: Optional[list[dict]] = None,
    ) -> list[types.Content]:
        contents: list[types.Content] = []

        if conversation_history:
            for turn in conversation_history:
                role = "user" if turn["role"] == "user" else "model"
                contents.append(types.Content(role=role, parts=[types.Part(text=turn["content"])]))

        full_message = (
            f"Here are the relevant emails from my mailbox:\n\n"
            f"{context}\n\n"
            f"My question: {user_message}"
        )
        contents.append(types.Content(role="user", parts=[types.Part(text=full_message)]))
        return contents

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict],
        conversation_history: Optional[list[dict]] = None,
    ) -> str:
        budget = self._context_char_budget(system_prompt, user_message, conversation_history)
        context = self._format_context(context_messages, max_context_chars=budget)
        contents = self._build_contents(context, user_message, conversation_history)

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=self.max_tokens,
                temperature=self.temperature,
            ),
        )
        return response.text

    async def stream(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict],
        conversation_history: Optional[list[dict]] = None,
    ) -> AsyncGenerator[str, None]:
        budget = self._context_char_budget(system_prompt, user_message, conversation_history)
        context = self._format_context(context_messages, max_context_chars=budget)
        contents = self._build_contents(context, user_message, conversation_history)

        print(f"[gemini] sending {len(context):,} chars context ({len(context)//4:,} est. tokens) to {self.model}")

        try:
            async for chunk in await self.client.aio.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=self.max_tokens,
                    temperature=self.temperature,
                ),
            ):
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            print(f"[gemini] stream error: {type(e).__name__}: {e}")
            yield f"\n\n[Error from Gemini: {e}]"
