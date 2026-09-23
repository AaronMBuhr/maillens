"""
Shared test helpers for MailLens tests.

These are unit tests: no Postgres is involved.  ``hybrid_search`` issues its
SQL through ``session.execute()`` and does all merging and ranking in Python,
so a fake session that returns canned rows is enough to exercise the ranking
logic.  The SQL itself (ILIKE semantics, pgvector distance, metadata filters,
LIMITs) is NOT covered here.
"""

from datetime import datetime

from backend.storage.models import Message


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeSession:
    """Stand-in for AsyncSession that returns queued row lists in order.

    ``hybrid_search`` executes the vector query first, then (only when there
    are keywords) the keyword query.
    """

    def __init__(self, *row_batches):
        self._batches = list(row_batches)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if not self._batches:
            raise AssertionError("unexpected extra session.execute() call")
        return _FakeResult(self._batches.pop(0))


class StubLLM:
    """Minimal LLM provider: returns a fixed reply, or raises it if an exception."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def complete(self, system_prompt, user_message, context_messages):
        self.calls.append(user_message)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


_next_id = iter(range(1, 10_000))


def make_msg(
    sender: str = "someone@example.com",
    subject: str = "",
    recipients_to: str = "",
    body_clean: str | None = None,
    body_text: str | None = None,
    id: int | None = None,
) -> Message:
    if id is None:
        id = next(_next_id)
    return Message(
        id=id,
        message_id=f"<m{id}@test>",
        sender=sender,
        subject=subject,
        recipients_to=recipients_to,
        body_clean=body_clean,
        body_text=body_text,
        date=datetime(2024, 1, 1),
        account="acct",
        folder="INBOX",
        has_attachments=False,
        thread_id=None,
    )

