"""
Query API: natural language email search and analysis.
"""

import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_config
from backend.ingestion.embedder import embed_texts
from backend.llm.factory import SYSTEM_PROMPT, get_active_provider_config, get_llm_provider
from backend.storage.db import get_session
from backend.storage.queries import extract_search_keywords, get_thread_context, hybrid_search, rewrite_follow_up_query

router = APIRouter()

CHARS_PER_TOKEN = 4
HEADER_CHARS_PER_MSG = 200


class ConversationTurn(BaseModel):
    role: str
    content: str


class QueryRequest(BaseModel):
    question: str
    sender: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    folder: Optional[str] = None
    has_attachments: Optional[bool] = None
    accounts: Optional[list[str]] = None
    top_k: Optional[int] = None
    stream: bool = True
    conversation_history: Optional[list[ConversationTurn]] = None
    previous_source_ids: Optional[list[int]] = None


class QueryResponse(BaseModel):
    answer: str
    sources: list[dict]
    query_embedding_time_ms: float
    retrieval_time_ms: float
    llm_time_ms: float


def _compute_context_budget(
    provider_config,
    system_prompt: str,
    question: str,
    conversation_history: Optional[list[dict]],
) -> int:
    """Return the char budget available for email context."""
    max_ctx = getattr(provider_config, "max_context_tokens", 0)
    max_output = getattr(provider_config, "max_tokens", 4096)
    if not max_ctx:
        return 15 * 8000

    used = max_output + len(system_prompt) // CHARS_PER_TOKEN + len(question) // CHARS_PER_TOKEN
    if conversation_history:
        for turn in conversation_history:
            used += len(turn.get("content", "")) // CHARS_PER_TOKEN
    available_tokens = max(0, max_ctx - used)
    return available_tokens * CHARS_PER_TOKEN


def _estimate_top_k(context_char_budget: int, min_k: int = 10, max_k: int = 500) -> int:
    """Estimate how many messages we can fit given the char budget.

    Assumes ~2000 chars avg per message (header + truncated body).
    """
    avg_msg_chars = 2000
    k = context_char_budget // avg_msg_chars
    return max(min_k, min(k, max_k))


MAX_BODY_PER_MSG = 8000


def _trim_to_budget(results: list[dict], char_budget: int) -> list[dict]:
    """Keep messages in rank order, capping each body to share the budget.

    First computes a per-message body cap so that all candidates could
    theoretically fit.  Then fills greedily, truncating bodies to the
    cap and stopping once the budget is exhausted.
    """
    if not results:
        return []

    per_msg_cap = max(
        500,
        min(MAX_BODY_PER_MSG, (char_budget - len(results) * HEADER_CHARS_PER_MSG) // len(results)),
    )

    kept = []
    remaining = char_budget
    for msg in results:
        if remaining < HEADER_CHARS_PER_MSG + 200:
            break
        body = msg.get("body_clean") or msg.get("body_text") or ""
        if len(body) > per_msg_cap:
            body = body[:per_msg_cap] + "\n... [truncated]"
        cost = HEADER_CHARS_PER_MSG + len(body)
        if cost > remaining:
            break
        kept.append({**msg, "body_clean": body})
        remaining -= cost
    return kept


async def _run_search_pipeline(
    request: QueryRequest,
    session: AsyncSession,
):
    """Run keyword extraction, embedding, hybrid search, thread expansion, and trim."""
    config = get_config()
    provider_config = get_active_provider_config()
    history = [t.model_dump() for t in request.conversation_history] if request.conversation_history else None
    provider = get_llm_provider()

    char_budget = _compute_context_budget(provider_config, SYSTEM_PROMPT, request.question, history)
    adaptive_k = _estimate_top_k(char_budget)
    top_k = request.top_k or adaptive_k
    print(f"[query] budget={char_budget} chars ({char_budget // CHARS_PER_TOKEN} tokens), top_k={top_k}")

    search_query = request.question
    if history:
        search_query = await rewrite_follow_up_query(request.question, history, provider)

    t0 = time.time()
    keywords = await extract_search_keywords(search_query, provider)
    query_embeddings = await embed_texts([search_query])
    query_embedding = query_embeddings[0]
    embed_time = (time.time() - t0) * 1000

    t0 = time.time()
    candidates = await hybrid_search(
        session=session,
        query_embedding=query_embedding,
        query_text=search_query,
        top_k=top_k,
        similarity_threshold=config.retrieval.similarity_threshold,
        sender=request.sender,
        date_from=request.date_from,
        date_to=request.date_to,
        folder=request.folder,
        has_attachments=request.has_attachments,
        accounts=request.accounts,
        keywords=keywords,
        previous_source_ids=request.previous_source_ids,
    )

    if config.retrieval.include_thread_context:
        seen_ids = {m["message_id"] for m in candidates}
        extra = []
        seen_thread_ids = set()
        for msg in candidates[:10]:
            if msg.get("thread_id") and msg["thread_id"] not in seen_thread_ids:
                seen_thread_ids.add(msg["thread_id"])
                thread_msgs = await get_thread_context(session, msg["thread_id"])
                for tm in thread_msgs:
                    if tm["message_id"] not in seen_ids:
                        seen_ids.add(tm["message_id"])
                        extra.append(tm)
        candidates = candidates + extra

    print(f"[query] hybrid search returned {len(candidates)} candidates")

    results = _trim_to_budget(candidates, char_budget)
    print(f"[query] after budget trim: {len(results)} messages (budget={char_budget} chars)")
    retrieval_time = (time.time() - t0) * 1000

    sources = [
        {
            "id": msg.get("id"),
            "subject": msg.get("subject"),
            "sender": msg.get("sender"),
            "recipients_to": msg.get("recipients_to"),
            "date": msg.get("date"),
            "account": msg.get("account"),
            "folder": msg.get("folder"),
            "similarity": msg.get("similarity"),
            "snippet": (msg.get("body_clean") or "")[:200],
        }
        for msg in results
    ]

    return {
        "provider": provider,
        "history": history,
        "results": results,
        "sources": sources,
        "embed_time": embed_time,
        "retrieval_time": retrieval_time,
        "char_budget": char_budget,
        "search_query": search_query,
    }


@router.post("/")
async def query_email(
    request: QueryRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Query the email archive with natural language.

    Embeds the question, retrieves relevant messages via hybrid search,
    fills context greedily by rank until the provider's token budget
    is reached, and sends them to the configured LLM for analysis.
    """

    if request.stream:
        async def stream_response():
            import json

            yield f"data: {json.dumps({'type': 'status', 'message': 'Extracting keywords...'})}\n\n"

            pipeline = await _run_search_pipeline(request, session)
            provider = pipeline["provider"]
            history = pipeline["history"]
            results = pipeline["results"]
            sources = pipeline["sources"]
            embed_time = pipeline["embed_time"]
            retrieval_time = pipeline["retrieval_time"]
            char_budget = pipeline["char_budget"]

            if pipeline["search_query"] != request.question:
                yield f"data: {json.dumps({'type': 'rewritten_query', 'query': pipeline['search_query']})}\n\n"
            yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"
            yield f"data: {json.dumps({'type': 'meta', 'embed_time_ms': embed_time, 'retrieval_time_ms': retrieval_time, 'context_messages': len(results), 'context_budget_tokens': char_budget // CHARS_PER_TOKEN})}\n\n"

            try:
                async for chunk in provider.stream(
                    system_prompt=SYSTEM_PROMPT,
                    user_message=request.question,
                    context_messages=results,
                    conversation_history=history,
                ):
                    yield f"data: {json.dumps({'type': 'text', 'content': chunk})}\n\n"
            except Exception as e:
                print(f"[query] LLM stream error: {type(e).__name__}: {e}")
                yield f"data: {json.dumps({'type': 'text', 'content': f'[LLM Error: {e}]'})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    # Non-streaming response
    pipeline = await _run_search_pipeline(request, session)
    provider = pipeline["provider"]
    history = pipeline["history"]
    results = pipeline["results"]
    sources = pipeline["sources"]
    embed_time = pipeline["embed_time"]
    retrieval_time = pipeline["retrieval_time"]

    t0 = time.time()
    answer = await provider.complete(
        system_prompt=SYSTEM_PROMPT,
        user_message=request.question,
        context_messages=results,
        conversation_history=history,
    )
    llm_time = (time.time() - t0) * 1000

    return QueryResponse(
        answer=answer,
        sources=sources,
        query_embedding_time_ms=embed_time,
        retrieval_time_ms=retrieval_time,
        llm_time_ms=llm_time,
    )
