"""
Hybrid retrieval: two-path search combining pgvector cosine similarity
with ILIKE keyword matching, then merged and re-ranked.

Path 1 — Vector: searches chunk-level embeddings, deduplicates to parent
message, keeping the best chunk similarity.
Path 2 — Keyword: extracts non-trivial words from the query and finds
messages whose sender, subject, or recipients contain those words via ILIKE.

Results from both paths are merged, scored, and returned.  Vector candidates
that no keyword hit in the header fields are given a second chance against
their body text in Python (see BODY_HIT_WEIGHT), since the ILIKE path cannot
search bodies without a sequential scan.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
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
    "Never output time expressions ('today', 'last month', 'in March') as keywords.\n\n"
    "Output one keyword per line, lowercase. If there are no search-worthy terms, "
    "output the single word: NONE\n\n"
    "Date range: the message gives today's date. ONLY if the query explicitly limits "
    "WHEN the emails were sent (e.g. 'today', 'yesterday', 'last week', 'in the past "
    "3 months', 'since March', 'in 2023', 'before June 2024'), also output one or both "
    "of these lines, with inclusive calendar dates:\n"
    "DATE_FROM: YYYY-MM-DD\n"
    "DATE_TO: YYYY-MM-DD\n"
    "For a single day, DATE_FROM and DATE_TO are the same date. "
    "Open-ended ranges use only one line ('since March' -> DATE_FROM only). "
    "Do NOT output date lines for phrases that are not a time span, such as "
    "'the last time we talked', 'our first meeting', or 'the latest invoice'."
)

# Lines the keyword extractor may add to declare a date range.
_DATE_LINE_RE = re.compile(r"^\W*date_(from|to)\s*[:=]\s*(\S+)", re.IGNORECASE)

_QUERY_REWRITE_PROMPT = (
    "You are a search query rewriter for a conversational email search system. "
    "Given a conversation history and a follow-up question, rewrite the follow-up "
    "as a fully self-contained search query that incorporates all relevant context "
    "from the conversation (names, companies, topics, dates, etc.).\n\n"
    "Rules:\n"
    "- The rewritten query must be self-contained — someone reading it without "
    "the conversation should understand exactly what emails to search for.\n"
    "- Preserve all specific details from the follow-up (names, dates, keywords).\n"
    "- Add context from the conversation that the follow-up implicitly refers to "
    "(e.g., if the conversation was about 'Grafana interviews' and the follow-up "
    "asks about 'three parts', include 'Grafana' in the rewrite).\n"
    "- If the follow-up is already self-contained or introduces a completely new "
    "topic unrelated to the conversation, return it unchanged.\n"
    "- Output ONLY the rewritten query text, nothing else. No preamble, no quotes."
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
    # Time expressions: handled as a date range, never as header keywords.
    # Month names are deliberately absent ("june" may be a person).
    "today", "todays", "yesterday", "tomorrow", "tonight", "now", "ago", "since",
    "past", "previous", "prior", "next", "current", "until",
    "day", "days", "week", "weeks", "weekend", "month", "months",
    "year", "years", "quarter", "daily", "weekly", "monthly", "yearly",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})

VECTOR_WEIGHT = 0.4
KEYWORD_WEIGHT = 0.6

# Credit for a keyword found only in the body, relative to a header hit.
# Full credit would let body matches outrank subject matches; the 0.1 tier
# used for recipients-only matches buries them under the relative cutoff.
# Set to 0.0 to restore header-only keyword gating.
BODY_HIT_WEIGHT = 0.5


def _extract_keywords_static(text: str) -> list[str]:
    """Fallback: regex tokenization with static stop-word removal."""
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 1]


@dataclass
class SearchTerms:
    """What the extractor pulled out of a query.

    ``date_from`` / ``date_to`` are inclusive calendar dates in the user's
    timezone; either may be None for an open-ended or absent range.
    """

    keywords: list[str] = field(default_factory=list)
    date_from: Optional[date] = None
    date_to: Optional[date] = None


def _validate_date_range(
    date_from: Optional[date], date_to: Optional[date], today: date
) -> tuple[Optional[date], Optional[date]]:
    """Sanity-check an LLM-inferred range; return (None, None) if it is unusable.

    Emails cannot come from the future, so ``date_to`` is clamped to today and
    a range that starts after today is discarded, as is an inverted range.
    """
    if date_to is not None and date_to > today:
        date_to = today
    if date_from is not None and date_from > today:
        return None, None
    if date_from is not None and date_to is not None and date_from > date_to:
        return None, None
    return date_from, date_to


def day_bounds(
    date_from: Optional[date],
    date_to: Optional[date],
    utc_offset_minutes: int = 0,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Turn inclusive calendar dates into a half-open datetime range.

    Returns ``(start, end)`` for filtering ``start <= Message.date < end``:
    ``start`` is midnight at the beginning of ``date_from`` and ``end`` is
    midnight at the beginning of the day *after* ``date_to``, both in the
    timezone ``utc_offset_minutes`` east of UTC.  So ``date_to`` covers its
    whole day, and ``date_from == date_to`` selects exactly that day.
    """
    tz = timezone(timedelta(minutes=utc_offset_minutes))
    start = datetime.combine(date_from, time.min, tzinfo=tz) if date_from else None
    end = (
        datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz)
        if date_to else None
    )
    return start, end


def _split_date_lines(response: str) -> tuple[list[str], Optional[date], Optional[date]]:
    """Separate DATE_FROM/DATE_TO lines from keyword lines in an LLM reply.

    Unparseable dates (``DATE_TO: NONE``, ``DATE_FROM: last week``) are ignored.
    """
    keyword_lines: list[str] = []
    found: dict[str, date] = {}
    for line in response.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = _DATE_LINE_RE.match(line)
        if m:
            try:
                found[m.group(1).lower()] = date.fromisoformat(m.group(2).strip(".,"))
            except ValueError:
                pass
            continue
        keyword_lines.append(line.lower())
    return keyword_lines, found.get("from"), found.get("to")


async def extract_search_terms(query: str, llm_provider, today: date) -> SearchTerms:
    """Use the active LLM to extract keywords and any explicit date range.

    One LLM call yields both.  ``today`` is the user's current date, which the
    model needs to resolve relative phrases such as "last month".  Keywords
    fall back to static stop-word extraction on any failure; the date range
    simply comes back empty.
    """
    try:
        response = await llm_provider.complete(
            system_prompt=_KEYWORD_EXTRACT_PROMPT,
            user_message=f"Today's date: {today.isoformat()} ({today:%A})\nQuery: {query}",
            context_messages=[],
        )
    except Exception as e:
        print(f"[keywords] LLM extraction failed ({type(e).__name__}: {e}), using static fallback")
        return SearchTerms(keywords=_extract_keywords_static(query))

    lines, raw_from, raw_to = _split_date_lines(response)
    date_from, date_to = _validate_date_range(raw_from, raw_to, today)
    if (raw_from, raw_to) != (date_from, date_to):
        print(f"[keywords] adjusted LLM date range {raw_from}..{raw_to} -> {date_from}..{date_to}")
    if date_from or date_to:
        print(f"[keywords] LLM date range: {date_from}..{date_to}")

    if lines == ["none"] or not lines:
        print(f"[keywords] LLM returned no keywords, falling back to static")
        return SearchTerms(_extract_keywords_static(query), date_from, date_to)
    raw = [re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", ln) for ln in lines]
    words: list[str] = []
    for phrase in raw:
        for w in phrase.split():
            # Keep interior ".", "@" and "-" so domains and addresses
            # ("acme.com") survive for ILIKE; "_" and "%" are LIKE wildcards.
            cleaned = re.sub(r"[^a-z0-9.@-]", "", w).strip(".@-")
            if cleaned and len(cleaned) > 1 and cleaned not in _STOP_WORDS:
                words.append(cleaned)
    unique = [w for w in dict.fromkeys(words) if w != "none"]
    keywords = [
        kw for kw in unique
        if not any(kw != other and kw in other for other in unique)
    ]
    print(f"[keywords] LLM extracted: {keywords}")
    return SearchTerms(keywords or _extract_keywords_static(query), date_from, date_to)


async def rewrite_follow_up_query(
    question: str,
    conversation_history: list[dict],
    llm_provider,
) -> str:
    """Rewrite a follow-up question as a standalone query using conversation context.

    When a user asks a follow-up like "did they mention three parts?" after
    discussing Grafana interviews, this rewrites it to include "Grafana" so
    the retrieval step finds the right emails instead of doing a context-free search.

    Returns the original question unchanged if there's no history or on failure.
    """
    if not conversation_history:
        return question

    history_lines = []
    for turn in conversation_history:
        role = turn.get("role", "user").capitalize()
        content = turn.get("content", "")
        if role == "Assistant" and len(content) > 500:
            content = content[:500] + "..."
        history_lines.append(f"{role}: {content}")

    user_message = (
        "Conversation so far:\n"
        + "\n\n".join(history_lines)
        + f"\n\nFollow-up question: {question}"
    )

    try:
        rewritten = await llm_provider.complete(
            system_prompt=_QUERY_REWRITE_PROMPT,
            user_message=user_message,
            context_messages=[],
        )
        rewritten = rewritten.strip().strip('"').strip("'")
        if not rewritten:
            return question
        print(f"[rewrite] '{question}' → '{rewritten}'")
        return rewritten
    except Exception as e:
        print(f"[rewrite] Failed ({type(e).__name__}: {e}), using original query")
        return question


def _contains_word(haystack: str, needle: str) -> bool:
    """True if ``needle`` occurs in ``haystack`` as a whole word.

    Both arguments must already be lowercase.  Used for body matching, where
    plain substring semantics would be far too loose: over kilobytes of prose
    a short keyword becomes near-universal ("ai" occurs inside "email" in
    almost every signature), which would turn the keyword gate into a no-op.

    Implemented with ``str.find`` and two character tests rather than a regex
    or a tokenisation pass -- this runs once per keyword for every candidate
    the vector path returned, so it must not allocate per body.
    """
    n = len(needle)
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return False
        before_ok = i == 0 or not haystack[i - 1].isalnum()
        end = i + n
        after_ok = end == len(haystack) or not haystack[end].isalnum()
        if before_ok and after_ok:
            return True
        start = i + 1


def _keyword_hit_ratio(
    msg: Message, keywords: list[str], include_body: bool = False
) -> float:
    """Fraction of keywords found in sender + subject + recipients.

    With ``include_body``, the cleaned body is searched as well.  Header
    fields stay in the haystack either way: a candidate can reach the body
    check because it fell outside the ILIKE path's LIMIT rather than because
    it missed on headers, and those keywords should still count.

    Headers are matched on substring, mirroring the ILIKE path.  The body is
    matched on whole words instead -- see _contains_word.  This is close to,
    but not the same as, what a tsvector body index would do: tsvector also
    stems ("lease" would match "leases") and keeps "foo_bar" as one lexeme.
    """
    if not keywords:
        return 0.0
    header = " ".join([
        msg.sender or "",
        msg.subject or "",
        msg.recipients_to or "",
    ]).lower()
    hits = sum(1 for kw in keywords if kw in header)
    if include_body and hits < len(keywords):
        body = (msg.body_clean or msg.body_text or "").lower()
        if body:
            hits += sum(
                1 for kw in keywords
                if kw not in header and _contains_word(body, kw)
            )
    return hits / len(keywords)


def _build_metadata_filters(
    sender: Optional[str],
    date_from: Optional[datetime],
    date_before: Optional[datetime],
    folder: Optional[str],
    has_attachments: Optional[bool],
    accounts: Optional[list[str]],
) -> list:
    filters = []
    if sender:
        filters.append(Message.sender.ilike(f"%{sender}%"))
    if date_from:
        filters.append(Message.date >= date_from)
    if date_before:
        filters.append(Message.date < date_before)
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


CONTINUITY_BOOST = 0.15


async def hybrid_search(
    session: AsyncSession,
    query_embedding: list[float],
    query_text: str = "",
    top_k: int = 15,
    similarity_threshold: float = 0.05,
    sender: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_before: Optional[datetime] = None,
    folder: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    accounts: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
    previous_source_ids: Optional[list[int]] = None,
) -> list[dict]:
    """
    Two-path hybrid search: vector similarity + keyword ILIKE matching.

    Results from both paths are merged so that messages containing literal
    query terms (names, domains) surface even when their embeddings aren't
    the closest match.

    If ``keywords`` is provided, those are used directly for the ILIKE path.
    Otherwise falls back to static stop-word extraction from ``query_text``.

    The date filter is half-open: ``date_from <= Message.date < date_before``
    (see ``day_bounds`` for building it from inclusive calendar dates).
    """
    meta_filters = _build_metadata_filters(sender, date_from, date_before, folder, has_attachments, accounts)

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
    prev_ids = set(previous_source_ids) if previous_source_ids else set()
    print(
        f"[search] vector={len(vec_rows)}, keyword={len(kw_rows) if keywords else 0}, "
        f"unique={len(candidates)}, keywords={keywords}, threshold={similarity_threshold:.3f}"
        f"{f', prev_source_ids={len(prev_ids)}' if prev_ids else ''}"
    )

    ranked = []
    rescued_ids: set[int] = set()
    for msg, vscore, kscore in candidates.values():
        is_previous = msg.id in prev_ids
        if keywords:
            # The ILIKE path only sees headers, so a message whose sole match
            # is in the body arrives here with kscore == 0 and would be dropped
            # regardless of similarity.  Check its body before the gate.
            # Carried-over sources are skipped: they clear the gate anyway, and
            # rescoring them would make a follow-up turn rank its own previous
            # sources differently than the turn that produced them.
            if kscore <= 0 and BODY_HIT_WEIGHT > 0 and not is_previous:
                body_ratio = _keyword_hit_ratio(msg, keywords, include_body=True)
                if body_ratio > 0:
                    kscore = body_ratio * BODY_HIT_WEIGHT
                    rescued_ids.add(msg.id)
            if kscore <= 0 and not is_previous:
                continue
            combined = VECTOR_WEIGHT * vscore + KEYWORD_WEIGHT * kscore
        else:
            combined = vscore
        if is_previous:
            combined += CONTINUITY_BOOST
        if combined >= similarity_threshold:
            ranked.append((msg, combined))

    ranked.sort(key=lambda x: x[1], reverse=True)
    print(
        f"[search] after threshold: {len(ranked)} results (returning top {top_k})"
        f"{f', body_rescued={len(rescued_ids)}' if rescued_ids else ''}"
    )
    for msg, score in ranked[:10]:
        print(f"  [{score:.3f}] {(msg.sender or '')[:40]} | {(msg.subject or '')[:50]}")

    if ranked:
        best = ranked[0][1]
        cutoff = best * 0.4
        ranked = [(m, s) for m, s in ranked if s >= cutoff]
        kept_rescues = sum(1 for m, _ in ranked[:top_k] if m.id in rescued_ids)
        print(
            f"[search] after relative cutoff ({cutoff:.3f}): {len(ranked)} results"
            f"{f', {kept_rescues} of {len(rescued_ids)} body rescues kept' if rescued_ids else ''}"
        )

    return [_msg_to_dict(msg, score) for msg, score in ranked[:top_k]]


async def get_thread_context(
    session: AsyncSession,
    thread_id: int,
    limit: int = 10,
    date_from: Optional[datetime] = None,
    date_before: Optional[datetime] = None,
) -> list[dict]:
    """Get messages in a thread, ordered by date.

    The optional half-open ``[date_from, date_before)`` range keeps thread
    expansion inside the search's date filter; it is applied in SQL so the
    ``limit`` counts only in-range messages.
    """
    stmt = select(Message).where(Message.thread_id == thread_id)
    if date_from:
        stmt = stmt.where(Message.date >= date_from)
    if date_before:
        stmt = stmt.where(Message.date < date_before)
    stmt = stmt.order_by(Message.date).limit(limit)
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
