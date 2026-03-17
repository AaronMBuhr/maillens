"""
Ingestion pipeline: orchestrates mail parsing, cleaning, threading,
embedding, and database storage.
"""

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_config
from backend.ingestion.attachments import extract_text_from_attachment
from backend.ingestion.cleaner import clean_message_body
from backend.ingestion.embedder import chunk_text, embed_texts
from backend.ingestion.parser import (
    ParsedMessage,
    discover_mail_sources,
    parse_maildir,
    parse_mbox,
)
from backend.ingestion.threading import build_threads
from backend.storage.db import get_session_factory
from backend.storage.models import Attachment, IngestionRun, Message, MessageChunk, Thread


class IngestionProgress:
    """Tracks ingestion progress for status reporting."""

    def __init__(self):
        self.status: str = "idle"
        self.total_sources: int = 0
        self.current_source: int = 0
        self.current_source_name: str = ""
        self.messages_processed: int = 0
        self.messages_new: int = 0
        self.messages_skipped: int = 0
        self.errors: list[str] = []
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "total_sources": self.total_sources,
            "current_source": self.current_source,
            "current_source_name": self.current_source_name,
            "messages_processed": self.messages_processed,
            "messages_new": self.messages_new,
            "messages_skipped": self.messages_skipped,
            "error_count": len(self.errors),
            "errors": self.errors[-10:],  # last 10 errors
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


# Global progress tracker
_progress = IngestionProgress()


def get_progress() -> IngestionProgress:
    return _progress


async def _message_exists(session: AsyncSession, message_id: str) -> bool:
    """Check if a message has already been ingested."""
    stmt = select(Message.id).where(Message.message_id == message_id).limit(1)
    result = await session.execute(stmt)
    return result.scalar() is not None


def _average_embeddings(embeddings: list[list[float]]) -> list[float]:
    """Compute the element-wise average of a list of embedding vectors."""
    if not embeddings:
        return []
    dim = len(embeddings[0])
    avg = [0.0] * dim
    for emb in embeddings:
        for i, v in enumerate(emb):
            avg[i] += v
    n = len(embeddings)
    return [v / n for v in avg]


async def _store_message(
    session: AsyncSession,
    parsed: ParsedMessage,
    body_clean: str,
    chunk_texts: list[str],
    chunk_embeddings: list[list[float]],
) -> Message:
    """Store a parsed message and its chunk embeddings in the database."""
    non_zero = [e for e in chunk_embeddings if any(v != 0.0 for v in e)]
    summary_embedding = _average_embeddings(non_zero) if non_zero else None

    msg = Message(
        message_id=parsed.message_id,
        in_reply_to=parsed.in_reply_to,
        references=parsed.references,
        subject=parsed.subject,
        sender=parsed.sender,
        recipients_to=parsed.recipients_to,
        recipients_cc=parsed.recipients_cc,
        date=parsed.date,
        account=parsed.account,
        folder=parsed.folder,
        source_file=parsed.source_file,
        body_text=parsed.body_text,
        body_html=parsed.body_html,
        body_clean=body_clean,
        embedding=summary_embedding,
        has_attachments=parsed.has_attachments,
    )
    session.add(msg)

    for idx, (ct, ce) in enumerate(zip(chunk_texts, chunk_embeddings)):
        has_signal = any(v != 0.0 for v in ce)
        chunk = MessageChunk(
            message=msg,
            chunk_index=idx,
            chunk_text=ct,
            embedding=ce if has_signal else None,
        )
        session.add(chunk)

    for att in parsed.attachments:
        extracted = extract_text_from_attachment(att)
        db_att = Attachment(
            message=msg,
            filename=att.filename,
            content_type=att.content_type,
            size_bytes=att.size_bytes,
            extracted_text=extracted,
        )
        session.add(db_att)

    return msg


async def run_ingestion(
    mail_directory: Optional[str] = None,
    incremental: bool = True,
) -> dict:
    """
    Run the full ingestion pipeline.

    Args:
        mail_directory: Override mail directory path. Uses config default if None.
        incremental: If True, skip messages already in the database.

    Returns:
        Summary dict of ingestion results.
    """
    global _progress
    config = get_config()
    mail_dir = mail_directory or config.mail.directory
    _progress = IngestionProgress()
    _progress.status = "running"
    _progress.started_at = datetime.now(timezone.utc)

    # Discover mail sources
    sources = discover_mail_sources(mail_dir)
    _progress.total_sources = len(sources)

    if not sources:
        _progress.status = "completed"
        _progress.completed_at = datetime.now(timezone.utc)
        _progress.errors.append(f"No mail sources found in {mail_dir}")
        return _progress.to_dict()

    session_factory = get_session_factory()

    # Phase 1: Parse and store messages
    all_message_ids = []
    seen_in_run: set[str] = set()

    for idx, (path, fmt, folder_name, account) in enumerate(sources):
        _progress.current_source = idx + 1
        _progress.current_source_name = folder_name

        parser = parse_mbox if fmt == "mbox" else parse_maildir

        async with session_factory() as session:
            batch_messages: list[tuple[ParsedMessage, str, list[str]]] = []
            batch_chunk_texts: list[str] = []
            batch_chunk_counts: list[int] = []

            for parsed_msg in parser(path, folder_name, account=account):
                _progress.messages_processed += 1

                # Always skip duplicates: same message can appear in
                # multiple Thunderbird folders (e.g. folder + All Mail).
                if parsed_msg.message_id in seen_in_run:
                    _progress.messages_skipped += 1
                    continue
                if await _message_exists(session, parsed_msg.message_id):
                    _progress.messages_skipped += 1
                    seen_in_run.add(parsed_msg.message_id)
                    continue

                try:
                    body_clean = (clean_message_body(
                        parsed_msg.body_text, parsed_msg.body_html
                    ) or "").replace("\x00", "") or None

                    embed_parts = []
                    if parsed_msg.subject:
                        embed_parts.append(f"Subject: {parsed_msg.subject}")
                    if body_clean:
                        embed_parts.append(body_clean)

                    embed_text = "\n\n".join(embed_parts)

                    chunks = chunk_text(embed_text, config.embeddings.chunk_size)
                    if not chunks:
                        chunks = [embed_text]

                    batch_messages.append((parsed_msg, body_clean, chunks))
                    batch_chunk_texts.extend(chunks)
                    batch_chunk_counts.append(len(chunks))
                    seen_in_run.add(parsed_msg.message_id)

                    if len(batch_chunk_texts) >= config.embeddings.batch_size:
                        embeddings = await embed_texts(batch_chunk_texts)
                        offset = 0
                        for (pm, bc, ch_texts), n_chunks in zip(batch_messages, batch_chunk_counts):
                            ch_embs = embeddings[offset : offset + n_chunks]
                            offset += n_chunks
                            await _store_message(session, pm, bc, ch_texts, ch_embs)
                            all_message_ids.append(pm.message_id)
                            _progress.messages_new += 1
                        await session.commit()
                        batch_messages = []
                        batch_chunk_texts = []
                        batch_chunk_counts = []

                except Exception as e:
                    _progress.errors.append(
                        f"Error processing {parsed_msg.message_id}: {str(e)}"
                    )
                    await session.rollback()
                    batch_messages = []
                    batch_chunk_texts = []
                    batch_chunk_counts = []
                    continue

            if batch_messages:
                try:
                    embeddings = await embed_texts(batch_chunk_texts)
                    offset = 0
                    for (pm, bc, ch_texts), n_chunks in zip(batch_messages, batch_chunk_counts):
                        ch_embs = embeddings[offset : offset + n_chunks]
                        offset += n_chunks
                        await _store_message(session, pm, bc, ch_texts, ch_embs)
                        all_message_ids.append(pm.message_id)
                        _progress.messages_new += 1
                    await session.commit()
                except Exception as e:
                    await session.rollback()
                    _progress.errors.append(f"Error flushing batch for {folder_name}: {str(e)}")

    # Phase 2: Build threads
    # asyncpg caps at 32767 query args, so fetch in batches
    BATCH_LIMIT = 30000
    if all_message_ids:
        try:
            async with session_factory() as session:
                messages = []
                for i in range(0, len(all_message_ids), BATCH_LIMIT):
                    batch_ids = all_message_ids[i : i + BATCH_LIMIT]
                    stmt = select(Message).where(
                        Message.message_id.in_(batch_ids)
                    )
                    result = await session.execute(stmt)
                    messages.extend(result.scalars().all())

                msg_dicts = [
                    {
                        "message_id": m.message_id,
                        "in_reply_to": m.in_reply_to,
                        "references": m.references,
                    }
                    for m in messages
                ]

                thread_map = build_threads(msg_dicts)

                thread_id_to_db_thread: dict[int, Thread] = {}
                for msg in messages:
                    thread_group = thread_map.get(msg.message_id)
                    if thread_group is None:
                        continue

                    if thread_group not in thread_id_to_db_thread:
                        thread = Thread(
                            subject=msg.subject,
                            first_date=msg.date,
                            last_date=msg.date,
                            message_count=0,
                        )
                        session.add(thread)
                        await session.flush()
                        thread_id_to_db_thread[thread_group] = thread

                    db_thread = thread_id_to_db_thread[thread_group]
                    msg.thread_id = db_thread.id
                    db_thread.message_count += 1
                    if msg.date:
                        if db_thread.first_date is None or msg.date < db_thread.first_date:
                            db_thread.first_date = msg.date
                        if db_thread.last_date is None or msg.date > db_thread.last_date:
                            db_thread.last_date = msg.date

                await session.commit()
        except Exception as e:
            _progress.errors.append(f"Error building threads: {str(e)}")

    _progress.status = "completed"
    _progress.completed_at = datetime.now(timezone.utc)
    return _progress.to_dict()
