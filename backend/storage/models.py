"""
SQLAlchemy models for MailLens.
"""

from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Boolean,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Message(Base):
    """A single email message."""

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Core identity
    message_id = Column(String, unique=True, nullable=False, index=True)
    in_reply_to = Column(String, nullable=True, index=True)
    references = Column(Text, nullable=True)  # space-separated message IDs

    # Thread grouping (computed during ingestion)
    thread_id = Column(Integer, ForeignKey("threads.id"), nullable=True, index=True)

    # Headers
    subject = Column(Text, nullable=True)
    sender = Column(String, nullable=True, index=True)
    recipients_to = Column(Text, nullable=True)
    recipients_cc = Column(Text, nullable=True)
    date = Column(DateTime(timezone=True), nullable=True, index=True)

    # Source info
    account = Column(String, nullable=True, index=True)
    folder = Column(String, nullable=True, index=True)
    source_file = Column(String, nullable=True)

    # Content
    body_text = Column(Text, nullable=True)  # cleaned plain text
    body_html = Column(Text, nullable=True)  # original HTML if present
    body_clean = Column(Text, nullable=True)  # after reply-chain stripping

    # Embedding (768 dimensions for nomic-embed-text)
    embedding = Column(Vector(768), nullable=True)

    # Metadata
    has_attachments = Column(Boolean, default=False)
    ingested_at = Column(DateTime(timezone=True), default=func.now())

    # Relationships
    thread = relationship("Thread", back_populates="messages")
    attachments = relationship("Attachment", back_populates="message", cascade="all, delete-orphan")
    chunks = relationship("MessageChunk", back_populates="message", cascade="all, delete-orphan", order_by="MessageChunk.chunk_index")

    __table_args__ = (
        Index("ix_messages_date_desc", date.desc()),
        Index(
            "ix_messages_embedding_cosine",
            embedding,
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Thread(Base):
    """A conversation thread grouping related messages."""

    __tablename__ = "threads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subject = Column(Text, nullable=True)
    first_date = Column(DateTime(timezone=True), nullable=True)
    last_date = Column(DateTime(timezone=True), nullable=True)
    message_count = Column(Integer, default=0)

    messages = relationship("Message", back_populates="thread", order_by="Message.date")


class MessageChunk(Base):
    """A single embedding chunk of a message body."""

    __tablename__ = "message_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    chunk_text = Column(Text, nullable=False)
    embedding = Column(Vector(768), nullable=True)

    message = relationship("Message", back_populates="chunks")

    __table_args__ = (
        Index("ix_message_chunks_msg_idx", message_id, chunk_index, unique=True),
        Index(
            "ix_message_chunks_embedding_cosine",
            embedding,
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Attachment(Base):
    """An email attachment with optional extracted text."""

    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False, index=True)
    filename = Column(String, nullable=True)
    content_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    extracted_text = Column(Text, nullable=True)

    message = relationship("Message", back_populates="attachments")


class IngestionRun(Base):
    """Tracks ingestion runs for incremental updates."""

    __tablename__ = "ingestion_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime(timezone=True), default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, default="running")  # running, completed, failed
    messages_processed = Column(Integer, default=0)
    messages_new = Column(Integer, default=0)
    messages_skipped = Column(Integer, default=0)
    errors = Column(Text, nullable=True)
