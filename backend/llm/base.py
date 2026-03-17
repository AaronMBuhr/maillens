"""
Abstract base class for LLM providers.
"""

from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional


class LLMProvider(ABC):
    """
    Base interface for LLM providers.

    All providers implement the same interface so they can be swapped
    via configuration.
    """

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict],
        conversation_history: Optional[list[dict]] = None,
    ) -> str:
        """
        Generate a completion given a system prompt, user message,
        and retrieved email context.

        Args:
            system_prompt: Instructions for how the LLM should behave.
            user_message: The user's query.
            context_messages: List of retrieved email dicts to reason over.
            conversation_history: Previous turns as [{"role": "user"|"assistant", "content": "..."}].

        Returns:
            The LLM's response text.
        """
        ...

    @abstractmethod
    async def stream(
        self,
        system_prompt: str,
        user_message: str,
        context_messages: list[dict],
        conversation_history: Optional[list[dict]] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream a completion token by token.

        Same interface as complete() but yields text chunks.
        """
        ...

    def _context_char_budget(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: Optional[list[dict]] = None,
    ) -> int:
        """Estimate remaining char budget for email context.

        Uses ~4 chars/token as a rough approximation.
        """
        max_ctx = getattr(self, "max_context_tokens", 0)
        if not max_ctx:
            return 0
        max_output = getattr(self, "max_tokens", 4096)
        used_tokens = max_output + len(system_prompt) // 4 + len(user_message) // 4
        if conversation_history:
            for turn in conversation_history:
                used_tokens += len(turn.get("content", "")) // 4
        available_tokens = max(0, max_ctx - used_tokens)
        return available_tokens * 4

    def _format_context(
        self, context_messages: list[dict], max_context_chars: int = 0,
    ) -> str:
        """
        Format retrieved email messages into a context string for the LLM.

        Each email gets a stable [Email N] reference and full header metadata
        so the LLM can cite specific messages back to the user.

        When max_context_chars > 0 the formatter distributes the character
        budget across messages, giving higher-ranked (earlier) messages more
        space while ensuring we never exceed the limit.
        """
        if not context_messages:
            return "No relevant emails found."

        n = len(context_messages)
        header_overhead = 180

        if max_context_chars > 0:
            usable = max_context_chars - n * header_overhead - n * 4
            per_msg = max(200, usable // max(n, 1))
        else:
            per_msg = 8000

        parts = []
        remaining = max_context_chars if max_context_chars > 0 else float("inf")

        for i, msg in enumerate(context_messages, 1):
            header = (
                f"--- [Email {i}] (id={msg.get('id', '?')}) ---\n"
                f"From: {msg.get('sender', 'Unknown')}\n"
                f"To: {msg.get('recipients_to', 'Unknown')}\n"
                f"Date: {msg.get('date', 'Unknown')}\n"
                f"Subject: {msg.get('subject', '(no subject)')}\n"
                f"Folder: {msg.get('folder', 'Unknown')}\n"
            )
            body = msg.get("body_clean", msg.get("body_text", "(no content)"))
            body_limit = per_msg
            if body and len(body) > body_limit:
                body = body[:body_limit] + "\n... [truncated]"

            entry = f"{header}\n{body}"

            if remaining < len(entry) + 4:
                break
            parts.append(entry)
            remaining -= len(entry) + 4

        return "\n\n".join(parts)
