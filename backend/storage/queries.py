"""
Hybrid retrieval: two-path search combining pgvector cosine similarity
with ILIKE keyword matching, then merged and re-ranked.

Path 1 — Vector: searches chunk-level embeddings, deduplicates to parent
message, keeping the best chunk similarity.
Path 2 — Keyword: extracts non-trivial words from the query and finds
messages whose sender, subject, or recipients contain those words via ILIKE.

Results from both paths are merged, scored, and returned.
"""

import re
from datetime import datetime
from typing import Optional

from sqlalchemy import case, literal, select, and_, or_, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from backend.storage.models import Message, MessageChunk, Thread

_KEYWORD_EXTRACT_PROMPT = (
    "You are a keyword extractor for an email search engine. "
    "Given a user's query, output ONLY the search-worthy terms that should be "
    "matched against email senders, recipients, and subjects. This means proper "
    "nouns (people's names, company names), domain names, and specific phrases. "
    "Omit generic/instruction words like 'summarize', 'find', 'common', 'topic', etc. "
    "Output one keyword per line, lowercase. If there are no search-worthy terms, "
    "output the single word: NONE"
)

_STOP_WORDS = frozenset({
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it", "its",
    "they", "them", "their", "this", "that", "these", "those",
    "is", "am", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall", "should",
    "can", "could", "may", "might", "must",
    "a", "an", "the", "and", "but", "or", "nor", "not", "no",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "if", "when", "where", "how", "what", "which", "who", "whom",
    "all", "any", "both", "each", "few", "more", "most", "some",
    "about", "between", "through", "during", "before", "after",
    "above", "below", "up", "down", "out", "off", "over", "under",
    "again", "then", "once", "here", "there", "why",
    "also", "just", "so", "than", "too", "very",
    "find", "show", "tell", "give", "get", "search", "look", "list",
    "email", "emails", "message", "messages", "mail", "mails",
    "summarize", "summary", "describe", "explain", "overview",
    "conversations", "conversation", "discuss", "discussed", "talking",
    "recent", "recently", "everything", "anything", "something",
    "many", "much", "lot", "lots", "every", "always", "never",
    "want", "need", "know", "think", "like", "please", "thanks",
    "been", "going", "went", "come", "came", "talk", "talked",
    "common", "topic", "topics", "frequent", "frequently", "often",
    "usual", "usually", "typical", "typically", "mainly", "mostly",
    "related", "regarding", "concerning", "involved", "involve",
    "subject", "subjects", "theme", "themes", "content", "contents",
    "important", "interesting", "specific", "general", "overall",
    "first", "last", "latest", "earliest", "oldest", "newest",
    "sent", "received", "wrote", "written", "replied", "reply",
})

VECTOR_WEIGHT = 0.4
KEYWORD_WEIGHT = 0.6


def _extract_keywords_static(text: str) -> list[str]:
    """Fallback: regex tokenization with static stop-word removal."""
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 1]


async def extract_search_keywords(query: str, llm_provider) -> list[str]:
    """Use the active LLM to extract search-worthy keywords from the query.

    Falls back to static stop-word extraction on any failure.
    """
    try:
        response = await llm_provider.complete(
            system_prompt=_KEYWORD_EXTRACT_PROMPT,
            user_message=query,
            context_messages=[],
        )
        lines = [ln.strip().lower() for ln in response.strip().splitlines() if ln.strip()]
        if lines == ["none"] or not lines:
            print(f"[keywords] LLM returned no keywords, falling back to static")
            return _extract_keywords_static(query)
        raw = [re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", ln) for ln in lines]
        words: list[str] = []
        for phrase in raw:
            for w in phrase.split():
                cleaned = re.sub(r"[^a-z0-9]", "", w)
                if cleaned and len(cleaned) > 1 and cleaned not in _STOP_WORDS:
                    words.append(cleaned)
        unique = [w for w in dict.fromkeys(words) if w != "none"]
        keywords = [
            kw for kw in unique
            if not any(kw != other and kw in other for other in unique)
        ]
        print(f"[keywords] LLM extracted: {keywords}")
        return keywords if keywords else _extract_keywords_static(query)
    except Exception as e:
        print(f"[keywords] LLM extraction failed ({type(e).__name__}: {e}), using static fallback")
        return _extract_keywords_static(query)


def _keyword_hit_ratio(msg: Message, keywords: list[str]) -> float:
    """Fraction of keywords found in sender + subject + recipients."""
    if not keywords:
        return 0.0
    searchable = " ".join([
        msg.sender or "",
        msg.subject or "",
        msg.recipients_to or "",
    ]).lower()
    hits = sum(1 for kw in keywords if kw in searchable)
    return hits / len(keywords)


def _build_metadata_filters(
    sender: Optional[str],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
    folder: Optional[str],
    has_attachments: Optional[bool],
    accounts: Optional[list[str]],
) -> list:
    filters = []
    if sender:
        filters.append(Message.sender.ilike(f"%{sender}%"))
    if date_from:
        filters.append(Message.date >= date_from)
    if date_to:
        filters.append(Message.date <= date_to)
    if folder:
        filters.append(Message.folder == folder)
    if has_attachments is not None:
        filters.append(Message.has_attachments == has_attachments)
    if accounts:
        filters.append(Message.account.in_(accounts))
    return filters


def _msg_to_dict(msg: Message, score: float) -> dict:
    return {
        "id": msg.id,
        "message_id": msg.message_id,
        "subject": msg.subject,
        "sender": msg.sender,
        "recipients_to": msg.recipients_to,
        "date": msg.date.isoformat() if msg.date else None,
        "account": msg.account,
        "folder": msg.folder,
        "body_clean": msg.body_clean or msg.body_text,
        "has_attachments": msg.has_attachments,
        "thread_id": msg.thread_id,
        "similarity": score,
    }


async def hybrid_search(
    session: AsyncSession,
    query_embedding: list[float],
    query_text: str = "",
    top_k: int = 15,
    similarity_threshold: float = 0.05,
    sender: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    folder: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    accounts: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
) -> list[dict]:
    """
    Two-path hybrid search: vector similarity + keyword ILIKE matching.

    Results from both paths are merged so that messages containing literal
    query terms (names, domains) surface even when their embeddings aren't
    the closest match.

    If ``keywords`` is provided, those are used directly for the ILIKE path.
    Otherwise falls back to static stop-word extraction from ``query_text``.
    """
    meta_filters = _build_metadata_filters(sender, date_from, date_to, folder, has_attachments, accounts)

    # --- Path 1: Vector similarity search ---
    chunk_sub = (
        select(
            MessageChunk.message_id,
            func.max(1 - MessageChunk.embedding.cosine_distance(query_embedding)).label("best_sim"),
        )
        .where(MessageChunk.embedding.isnot(None))
        .group_by(MessageChunk.message_id)
        .subquery()
    )

    msg_similarity = 1 - Message.embedding.cosine_distance(query_embedding)
    vector_sim = func.coalesce(chunk_sub.c.best_sim, msg_similarity).label("similarity")

    vec_stmt = (
        select(Message, vector_sim)
        .outerjoin(chunk_sub, Message.id == chunk_sub.c.message_id)
        .where((chunk_sub.c.best_sim.isnot(None)) | (Message.embedding.isnot(None)))
    )
    if meta_filters:
        vec_stmt = vec_stmt.where(and_(*meta_filters))
    vec_stmt = vec_stmt.order_by(vector_sim.desc()).limit(top_k * 3)

    vec_result = await session.execute(vec_stmt)
    vec_rows = vec_result.all()

    # Collect into a dict keyed by message id: {msg.id: (msg, vector_score)}
    candidates: dict[int, tuple[Message, float, float]] = {}
    for msg, vscore in vec_rows:
        if vscore is not None:
            candidates[msg.id] = (msg, float(vscore), 0.0)

    # --- Path 2: Keyword ILIKE search ---
    if keywords is None:
        keywords = _extract_keywords_static(query_text)
    if keywords:
        all_kw_in_recip = and_(*[
            Message.recipients_to.ilike(f"%{kw}%") for kw in keywords
        ]) if len(keywords) > 1 else Message.recipients_to.ilike(f"%{keywords[0]}%")

        kw_conditions = []
        hit_expr = literal(0)
        for kw in keywords:
            pat = f"%{kw}%"
            kw_match = or_(
                Message.sender.ilike(pat),
                Message.subject.ilike(pat),
                and_(all_kw_in_recip, Message.recipients_to.ilike(pat)),
            )
            kw_conditions.append(kw_match)
            hit_expr = hit_expr + case((kw_match, 1), else_=0)

        kw_hits = hit_expr.label("kw_hits")

        kw_stmt = (
            select(Message, kw_hits)
            .where(or_(*kw_conditions))
            .order_by(kw_hits.desc(), Message.date.desc())
            .limit(top_k * 5)
        )
        if meta_filters:
            kw_stmt = kw_stmt.where(and_(*meta_filters))

        kw_result = await session.execute(kw_stmt)
        kw_rows = kw_result.all()

        for msg, hits in kw_rows:
            hit_ratio = hits / len(keywords)
            sender_subj = ((msg.sender or "") + " " + (msg.subject or "")).lower()
            if not any(kw in sender_subj for kw in keywords):
                hit_ratio *= 0.1
            if msg.id in candidates:
                existing = candidates[msg.id]
                candidates[msg.id] = (existing[0], existing[1], hit_ratio)
            else:
                candidates[msg.id] = (msg, 0.0, hit_ratio)

    # --- Merge and rank ---
    print(
        f"[search] vector={len(vec_rows)}, keyword={len(kw_rows) if keywords else 0}, "
        f"unique={len(candidates)}, keywords={keywords}, threshold={similarity_threshold:.3f}"
    )

    ranked = []
    for msg, vscore, kscore in candidates.values():
        if keywords:
            if kscore <= 0:
                continue
            combined = VECTOR_WEIGHT * vscore + KEYWORD_WEIGHT * kscore
        else:
            combined = vscore
        if combined >= similarity_threshold:
            ranked.append((msg, combined))

    ranked.sort(key=lambda x: x[1], reverse=True)
    print(f"[search] after threshold: {len(ranked)} results (returning top {top_k})")
    for msg, score in ranked[:10]:
        print(f"  [{score:.3f}] {(msg.sender or '')[:40]} | {(msg.subject or '')[:50]}")

    if ranked:
        best = ranked[0][1]
        cutoff = best * 0.4
        ranked = [(m, s) for m, s in ranked if s >= cutoff]
        print(f"[search] after relative cutoff ({cutoff:.3f}): {len(ranked)} results")

    return [_msg_to_dict(msg, score) for msg, score in ranked[:top_k]]


async def get_thread_context(
    session: AsyncSession,
    thread_id: int,
    limit: int = 10,
) -> list[dict]:
    """Get messages in a thread, ordered by date."""
    stmt = (
        select(Message)
        .where(Message.thread_id == thread_id)
        .order_by(Message.date)
        .limit(limit)
    )
    result = await session.execute(stmt)
    messages = result.scalars().all()

    return [
        {
            "id": msg.id,
            "message_id": msg.message_id,
            "subject": msg.subject,
            "sender": msg.sender,
            "recipients_to": msg.recipients_to,
            "date": msg.date.isoformat() if msg.date else None,
            "account": msg.account,
            "folder": msg.folder,
            "body_clean": msg.body_clean or msg.body_text,
            "has_attachments": msg.has_attachments,
            "thread_id": msg.thread_id,
        }
        for msg in messages
    ]


async def get_folders(session: AsyncSession) -> list[str]:
    """Get list of all indexed folders."""
    stmt = select(Message.folder).distinct().where(Message.folder.isnot(None)).order_by(Message.folder)
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]


async def get_senders(session: AsyncSession, limit: int = 100) -> list[dict]:
    """Get top senders by message count."""
    stmt = (
        select(Message.sender, func.count(Message.id).label("count"))
        .where(Message.sender.isnot(None))
        .group_by(Message.sender)
        .order_by(desc("count"))
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [{"sender": row[0], "count": row[1]} for row in result.all()]


async def get_accounts(session: AsyncSession) -> list[dict]:
    """Get distinct email accounts with message counts."""
    stmt = (
        select(Message.account, func.count(Message.id).label("count"))
        .where(Message.account.isnot(None))
        .group_by(Message.account)
        .order_by(Message.account)
    )
    result = await session.execute(stmt)
    return [{"account": row[0], "count": row[1]} for row in result.all()]


async def get_message_count(session: AsyncSession) -> int:
    """Total number of indexed messages."""
    stmt = select(func.count(Message.id))
    result = await session.execute(stmt)
    return result.scalar() or 0
