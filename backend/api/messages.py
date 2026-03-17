"""
Messages API: browse and view indexed email messages.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.storage.db import get_session
from backend.storage.models import Message
from backend.storage.queries import get_accounts as _get_accounts

router = APIRouter()


SORT_COLUMNS = {
    "date": Message.date,
    "sender": Message.sender,
    "subject": Message.subject,
    "account": Message.account,
    "folder": Message.folder,
}


@router.get("/")
async def list_messages(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sender: Optional[str] = None,
    folder: Optional[str] = None,
    subject: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    accounts: Optional[list[str]] = Query(None),
    sort_by: str = Query("date"),
    sort_dir: str = Query("desc"),
    session: AsyncSession = Depends(get_session),
):
    """List indexed messages with optional filters, sorting, and pagination."""
    stmt = select(Message)

    if sender:
        stmt = stmt.where(Message.sender.ilike(f"%{sender}%"))
    if folder:
        stmt = stmt.where(Message.folder == folder)
    if subject:
        stmt = stmt.where(Message.subject.ilike(f"%{subject}%"))
    if date_from:
        stmt = stmt.where(Message.date >= date_from)
    if date_to:
        stmt = stmt.where(Message.date <= date_to)
    if accounts:
        stmt = stmt.where(Message.account.in_(accounts))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    direction = desc if sort_dir == "desc" else asc
    col = SORT_COLUMNS.get(sort_by, Message.date)
    ordering = [direction(col)]
    if col is not Message.date:
        ordering.append(direction(Message.date))
    stmt = stmt.order_by(*ordering).offset((page - 1) * per_page).limit(per_page)
    result = await session.execute(stmt)
    messages = result.scalars().all()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "messages": [
            {
                "id": msg.id,
                "message_id": msg.message_id,
                "subject": msg.subject,
                "sender": msg.sender,
                "recipients_to": msg.recipients_to,
                "date": msg.date.isoformat() if msg.date else None,
                "account": msg.account,
                "folder": msg.folder,
                "has_attachments": msg.has_attachments,
                "snippet": (msg.body_clean or msg.body_text or "")[:200],
            }
            for msg in messages
        ],
    }


@router.get("/accounts")
async def list_accounts(session: AsyncSession = Depends(get_session)):
    """Get distinct email accounts with message counts."""
    return await _get_accounts(session)


@router.get("/{message_id}")
async def get_message(
    message_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Get full message content by database ID."""
    stmt = select(Message).where(Message.id == message_id)
    result = await session.execute(stmt)
    msg = result.scalar_one_or_none()

    if not msg:
        return {"error": "Message not found"}

    return {
        "id": msg.id,
        "message_id": msg.message_id,
        "in_reply_to": msg.in_reply_to,
        "subject": msg.subject,
        "sender": msg.sender,
        "recipients_to": msg.recipients_to,
        "recipients_cc": msg.recipients_cc,
        "date": msg.date.isoformat() if msg.date else None,
        "account": msg.account,
        "folder": msg.folder,
        "source_file": msg.source_file,
        "body_text": msg.body_text,
        "body_clean": msg.body_clean,
        "has_attachments": msg.has_attachments,
        "thread_id": msg.thread_id,
    }
