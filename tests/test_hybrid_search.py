"""Tests for the Python-side merge, gating and ranking in hybrid_search.

The session is faked: the first execute() returns vector-path rows as
(Message, similarity), the second returns keyword-path rows as (Message,
kw_hits).  See tests/helpers.py for what this does and doesn't cover.
"""

import pytest

import backend.storage.queries as queries
from backend.storage.queries import (
    BODY_HIT_WEIGHT,
    CONTINUITY_BOOST,
    KEYWORD_WEIGHT,
    VECTOR_WEIGHT,
    hybrid_search,
)
from tests.helpers import FakeSession, make_msg

EMB = [0.0] * 4  # never inspected; the fake session ignores the SQL


def scores(results):
    return {r["id"]: r["similarity"] for r in results}


async def run(vec_rows, kw_rows=None, **kwargs):
    batches = [vec_rows] if kw_rows is None else [vec_rows, kw_rows]
    session = FakeSession(*batches)
    results = await hybrid_search(session, EMB, **kwargs)
    return results, session


def test_weights_match_documented_values():
    # The spec and README quote these; fail loudly if they change.
    assert (VECTOR_WEIGHT, KEYWORD_WEIGHT) == (0.4, 0.6)
    assert BODY_HIT_WEIGHT == 0.5
    assert CONTINUITY_BOOST == 0.15


# --- merging the two paths ---------------------------------------------------

async def test_merges_both_paths_with_weighting():
    both = make_msg(sender="Jane <j@x.com>")
    kw_only = make_msg(subject="lunch with jane")
    results, _ = await run(
        [(both, 0.8)],
        [(both, 1), (kw_only, 1)],
        keywords=["jane"],
    )
    assert [r["id"] for r in results] == [both.id, kw_only.id]
    assert scores(results)[both.id] == pytest.approx(0.4 * 0.8 + 0.6 * 1.0)
    assert scores(results)[kw_only.id] == pytest.approx(0.6 * 1.0)


async def test_keyword_score_is_fraction_of_keywords_hit():
    msg = make_msg(subject="jane budget")
    results, _ = await run([], [(msg, 1)], keywords=["jane", "budget"])
    # hits come from SQL (1 of 2), not recomputed in Python.
    assert scores(results)[msg.id] == pytest.approx(0.6 * 0.5)


async def test_keyword_hit_outside_sender_and_subject_is_demoted():
    # Matched via recipients only: sender/subject don't contain the keyword.
    msg = make_msg(sender="bob@x.com", subject="hi", recipients_to="jane@x.com")
    results, _ = await run([], [(msg, 1)], keywords=["jane"])
    assert scores(results)[msg.id] == pytest.approx(0.6 * 1.0 * 0.1)


async def test_vector_rows_with_null_similarity_are_ignored():
    msg = make_msg(body_clean="jane")
    results, _ = await run([(msg, None)], [], keywords=["jane"])
    assert results == []


async def test_static_keywords_used_when_none_given():
    msg = make_msg(subject="budget")
    results, session = await run([], [(msg, 1)], query_text="find the budget")
    assert len(session.statements) == 2
    assert [r["id"] for r in results] == [msg.id]


# --- keyword gate and body rescue -------------------------------------------

async def test_vector_only_candidate_without_any_keyword_is_dropped():
    msg = make_msg(subject="unrelated", body_clean="nothing relevant")
    results, _ = await run([(msg, 0.95)], [], keywords=["jane"])
    assert results == []


async def test_body_only_match_is_rescued_at_body_weight():
    msg = make_msg(subject="catch up", body_clean="I met Jane yesterday.")
    results, _ = await run([(msg, 0.7)], [], keywords=["jane"])
    assert scores(results)[msg.id] == pytest.approx(
        VECTOR_WEIGHT * 0.7 + KEYWORD_WEIGHT * 1.0 * BODY_HIT_WEIGHT
    )


async def test_body_rescue_requires_whole_word():
    msg = make_msg(subject="trek", body_clean="captain janeway")
    results, _ = await run([(msg, 0.7)], [], keywords=["jane"])
    assert results == []


async def test_body_rescue_partial_keyword_coverage():
    msg = make_msg(body_clean="jane was here")
    results, _ = await run([(msg, 0.7)], [], keywords=["jane", "budget"])
    assert scores(results)[msg.id] == pytest.approx(0.4 * 0.7 + 0.6 * 0.5 * 0.5)


async def test_body_rescue_also_credits_header_hits_at_body_weight():
    # A candidate whose header matches but which the keyword query didn't
    # return (e.g. it fell past that query's LIMIT) is scored through the
    # rescue path, so its header hit is credited at BODY_HIT_WEIGHT too.
    msg = make_msg(subject="budget", body_clean="from jane")
    results, _ = await run([(msg, 0.7)], [], keywords=["jane", "budget"])
    assert scores(results)[msg.id] == pytest.approx(0.4 * 0.7 + 0.6 * 1.0 * 0.5)


async def test_body_rescue_uses_body_text_when_no_clean_body():
    msg = make_msg(body_clean=None, body_text="jane here")
    results, _ = await run([(msg, 0.7)], [], keywords=["jane"])
    assert msg.id in scores(results)


async def test_keyword_path_hits_are_not_rescored_from_body():
    # Already scored by the keyword path (demoted to 0.1); the body mention
    # must not replace that with a body-weight score.
    msg = make_msg(sender="bob@x.com", recipients_to="jane@x.com", body_clean="jane")
    results, _ = await run([(msg, 0.5)], [(msg, 1)], keywords=["jane"])
    assert scores(results)[msg.id] == pytest.approx(0.4 * 0.5 + 0.6 * 0.1)


async def test_body_hit_weight_zero_restores_header_only_gating(monkeypatch):
    monkeypatch.setattr(queries, "BODY_HIT_WEIGHT", 0.0)
    msg = make_msg(body_clean="I met jane yesterday")
    results, _ = await run([(msg, 0.7)], [], keywords=["jane"])
    assert results == []


# --- previous sources (conversation continuity) -----------------------------

async def test_previous_source_passes_gate_without_keyword_and_gets_boost():
    msg = make_msg(subject="unrelated")
    results, _ = await run(
        [(msg, 0.3)], [], keywords=["jane"], previous_source_ids=[msg.id]
    )
    assert scores(results)[msg.id] == pytest.approx(0.4 * 0.3 + CONTINUITY_BOOST)


async def test_previous_source_is_not_body_rescored():
    msg = make_msg(body_clean="jane was here")
    results, _ = await run(
        [(msg, 0.3)], [], keywords=["jane"], previous_source_ids=[msg.id]
    )
    # No body credit: same score as a previous source with no keyword at all.
    assert scores(results)[msg.id] == pytest.approx(0.4 * 0.3 + CONTINUITY_BOOST)


async def test_previous_source_keyword_hit_gets_boost_on_top():
    msg = make_msg(subject="jane")
    results, _ = await run(
        [], [(msg, 1)], keywords=["jane"], previous_source_ids=[msg.id]
    )
    assert scores(results)[msg.id] == pytest.approx(0.6 + CONTINUITY_BOOST)


# --- no-keyword path ---------------------------------------------------------

async def test_no_keywords_ranks_on_vector_only_and_skips_keyword_query():
    a, b = make_msg(), make_msg()
    results, session = await run([(a, 0.5), (b, 0.9)], keywords=[])
    assert len(session.statements) == 1
    assert [r["id"] for r in results] == [b.id, a.id]
    assert scores(results) == {a.id: 0.5, b.id: 0.9}


async def test_no_keywords_previous_source_boost():
    a = make_msg()
    results, _ = await run([(a, 0.5)], keywords=[], previous_source_ids=[a.id])
    assert scores(results)[a.id] == pytest.approx(0.5 + CONTINUITY_BOOST)


# --- thresholds and truncation ----------------------------------------------

async def test_absolute_threshold_drops_low_scores():
    a, b = make_msg(), make_msg()
    results, _ = await run([(a, 0.04), (b, 0.06)], keywords=[])
    assert [r["id"] for r in results] == [b.id]


async def test_custom_similarity_threshold():
    a = make_msg()
    results, _ = await run([(a, 0.3)], keywords=[], similarity_threshold=0.5)
    assert results == []


async def test_relative_cutoff_drops_results_below_40_percent_of_best():
    best, keep, drop = make_msg(), make_msg(), make_msg()
    results, _ = await run(
        [(best, 0.9), (keep, 0.37), (drop, 0.35)], keywords=[]
    )
    # cutoff = 0.9 * 0.4 = 0.36
    assert [r["id"] for r in results] == [best.id, keep.id]


async def test_top_k_truncates_sorted_results():
    msgs = [make_msg() for _ in range(5)]
    rows = [(m, 0.5 + i * 0.1) for i, m in enumerate(msgs)]
    results, _ = await run(rows, keywords=[], top_k=2)
    assert [r["id"] for r in results] == [msgs[4].id, msgs[3].id]


async def test_empty_results():
    results, _ = await run([], [], keywords=["jane"])
    assert results == []


# --- result shape ------------------------------------------------------------

async def test_result_dict_shape_and_body_fallback():
    msg = make_msg(sender="s", subject="subj", body_clean=None, body_text="raw body")
    results, _ = await run([(msg, 0.9)], keywords=[])
    r = results[0]
    assert set(r) == {
        "id", "message_id", "subject", "sender", "recipients_to", "date",
        "account", "folder", "body_clean", "has_attachments", "thread_id",
        "similarity",
    }
    assert r["body_clean"] == "raw body"
    assert r["date"] == "2024-01-01T00:00:00"
