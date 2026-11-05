"""Tests for keyword extraction and keyword matching helpers in queries.py."""

from datetime import date, datetime, timezone

import pytest

from backend.storage.queries import (
    _contains_word,
    _extract_keywords_static,
    _keyword_hit_ratio,
    day_bounds,
    extract_search_terms,
)
from tests.helpers import StubLLM, make_msg

TODAY = date(2026, 11, 5)  # a Thursday


async def extract_search_keywords(query, llm):
    """Keyword half of extract_search_terms, for the keyword-only tests below."""
    return (await extract_search_terms(query, llm, TODAY)).keywords


# --- _extract_keywords_static ------------------------------------------------

def test_static_extraction_drops_stop_words_and_lowercases():
    assert _extract_keywords_static("Find emails from Jane about the Q3 budget") == [
        "jane", "q3", "budget",
    ]


def test_static_extraction_drops_single_characters():
    assert _extract_keywords_static("x y jane") == ["jane"]


def test_static_extraction_splits_on_punctuation():
    # Tokenising on [a-zA-Z0-9]+ means a domain becomes separate words.
    assert _extract_keywords_static("acme.com invoices") == ["acme", "com", "invoices"]


def test_static_extraction_all_stop_words_gives_empty_list():
    assert _extract_keywords_static("show me all the emails") == []


# --- _contains_word ----------------------------------------------------------

@pytest.mark.parametrize("haystack, needle, expected", [
    ("met jane today", "jane", True),
    ("jane", "jane", True),                      # whole string
    ("jane said hi", "jane", True),              # at start
    ("hello jane", "jane", True),                # at end
    ("hi jane, bye", "jane", True),              # punctuation boundary
    ("jane.doe@x.com", "jane", True),            # '.' is not alphanumeric
    ("janeway", "jane", False),                  # prefix of longer word
    ("mary-jane", "jane", True),                 # '-' is a boundary
    ("maryjane", "jane", False),                 # suffix of longer word
    ("sent from my email client", "ai", False),  # the "ai" in "email" case
    ("see the ai report", "ai", True),
    ("janeway met jane", "jane", True),          # first hit rejected, later one accepted
    ("jane_doe", "jane", True),                  # '_' is not alphanumeric
    ("", "jane", False),
])
def test_contains_word(haystack, needle, expected):
    assert _contains_word(haystack, needle) is expected


# --- _keyword_hit_ratio ------------------------------------------------------

def test_hit_ratio_no_keywords_is_zero():
    assert _keyword_hit_ratio(make_msg(subject="anything"), []) == 0.0


def test_hit_ratio_headers_use_substring_matching():
    # "jan" is a substring of "Jane" in the sender; headers mirror ILIKE semantics.
    msg = make_msg(sender="Jane Doe <jd@x.com>")
    assert _keyword_hit_ratio(msg, ["jan"]) == 1.0


def test_hit_ratio_counts_each_header_field():
    msg = make_msg(sender="jane@x.com", subject="budget", recipients_to="bob@y.com")
    assert _keyword_hit_ratio(msg, ["jane", "budget", "bob", "missing"]) == 0.75


def test_hit_ratio_ignores_body_by_default():
    msg = make_msg(body_clean="jane wrote this")
    assert _keyword_hit_ratio(msg, ["jane"]) == 0.0


def test_hit_ratio_includes_body_when_asked():
    msg = make_msg(body_clean="Jane wrote this")
    assert _keyword_hit_ratio(msg, ["jane"], include_body=True) == 1.0


def test_hit_ratio_body_uses_whole_word_matching():
    msg = make_msg(body_clean="captain janeway")
    assert _keyword_hit_ratio(msg, ["jane"], include_body=True) == 0.0


def test_hit_ratio_body_falls_back_to_body_text():
    msg = make_msg(body_clean=None, body_text="jane wrote this")
    assert _keyword_hit_ratio(msg, ["jane"], include_body=True) == 1.0


def test_hit_ratio_combines_header_and_body_without_double_counting():
    msg = make_msg(subject="budget", body_clean="budget talk with jane")
    # budget: header hit; jane: body hit; "budget" in the body is not counted again.
    assert _keyword_hit_ratio(msg, ["budget", "jane"], include_body=True) == 1.0


def test_hit_ratio_partial_body_match():
    msg = make_msg(body_clean="jane only")
    assert _keyword_hit_ratio(msg, ["jane", "budget"], include_body=True) == 0.5


# --- extract_search_terms: keywords (LLM path) --------------------------------------

async def test_llm_keywords_are_cleaned_and_lowercased():
    llm = StubLLM("- Jane Smith\n* Budget\n")
    assert await extract_search_keywords("q", llm) == ["jane", "smith", "budget"]


async def test_llm_keywords_drop_stop_words_and_duplicates():
    llm = StubLLM("find\njane\nJane\nthe")
    assert await extract_search_keywords("q", llm) == ["jane"]


async def test_llm_keywords_drop_terms_contained_in_longer_terms():
    # "jane" is dropped because it is a substring of "janet".
    llm = StubLLM("jane\njanet")
    assert await extract_search_keywords("q", llm) == ["janet"]


@pytest.mark.parametrize("reply, expected", [
    ("acme.com", ["acme.com"]),                        # domain keeps its dot
    ("jane.doe@acme.com", ["jane.doe@acme.com"]),      # full address
    ("mary-jane", ["mary-jane"]),                      # hyphenated name
    ("(acme.com).", ["acme.com"]),                     # edge punctuation stripped
    ("acme.com.", ["acme.com"]),                       # trailing sentence period
    ("o'brien", ["obrien"]),                           # other punctuation removed
    ("jane_doe", ["janedoe"]),                         # "_" is a LIKE wildcard
    ("50%", ["50"]),                                   # "%" is a LIKE wildcard
    ("- .", "static"),                                 # nothing survives
])
async def test_llm_keyword_interior_punctuation(reply, expected):
    llm = StubLLM(reply)
    if expected == "static":
        expected = ["budget"]
    assert await extract_search_keywords("budget", llm) == expected


async def test_llm_domain_supersedes_its_bare_name():
    # "acme" is a substring of "acme.com", so substring elimination drops it.
    llm = StubLLM("acme\nacme.com")
    assert await extract_search_keywords("q", llm) == ["acme.com"]


@pytest.mark.parametrize("reply", ["NONE", "none", "", "   \n  "])
async def test_llm_no_keywords_falls_back_to_static(reply):
    llm = StubLLM(reply)
    assert await extract_search_keywords("budget from jane", llm) == ["budget", "jane"]


async def test_llm_only_stop_words_falls_back_to_static():
    llm = StubLLM("find\nemails")
    assert await extract_search_keywords("budget from jane", llm) == ["budget", "jane"]


async def test_llm_failure_falls_back_to_static():
    llm = StubLLM(RuntimeError("provider down"))
    assert await extract_search_keywords("budget from jane", llm) == ["budget", "jane"]


def test_static_extraction_drops_time_words():
    assert _extract_keywords_static("all of today's emails from last week") == []


async def test_llm_time_words_are_not_keywords():
    llm = StubLLM("grafana\nlast month")
    assert await extract_search_keywords("q", llm) == ["grafana"]


# --- extract_search_terms: date range -----------------------------------------

async def test_prompt_includes_todays_date_and_weekday():
    llm = StubLLM("NONE")
    await extract_search_terms("budget", llm, TODAY)
    assert "2026-11-05" in llm.calls[0]
    assert "Thursday" in llm.calls[0]
    assert "budget" in llm.calls[0]


async def test_date_lines_are_parsed_and_kept_out_of_keywords():
    llm = StubLLM("grafana\nDATE_FROM: 2026-10-05\nDATE_TO: 2026-11-05")
    terms = await extract_search_terms("q", llm, TODAY)
    assert terms.keywords == ["grafana"]
    assert (terms.date_from, terms.date_to) == (date(2026, 10, 5), date(2026, 11, 5))


async def test_single_day_range():
    llm = StubLLM("NONE\nDATE_FROM: 2026-11-05\nDATE_TO: 2026-11-05")
    terms = await extract_search_terms("all today's emails", llm, TODAY)
    assert (terms.date_from, terms.date_to) == (TODAY, TODAY)
    # NONE + static fallback: every word of the query is a stop word.
    assert terms.keywords == []


async def test_only_date_lines_falls_back_to_static_keywords():
    llm = StubLLM("DATE_FROM: 2026-10-01")
    terms = await extract_search_terms("budget since october 1", llm, TODAY)
    assert terms.date_from == date(2026, 10, 1)
    assert terms.date_to is None
    assert "budget" in terms.keywords


@pytest.mark.parametrize("reply", [
    "- DATE_FROM: 2026-10-05",      # bullet prefix
    "date_from: 2026-10-05",        # lowercase
    "DATE_FROM = 2026-10-05",       # '=' separator
    "DATE_FROM: 2026-10-05.",       # trailing period
])
async def test_date_line_format_variants(reply):
    terms = await extract_search_terms("q", StubLLM(reply), TODAY)
    assert terms.date_from == date(2026, 10, 5)


@pytest.mark.parametrize("reply", [
    "jane\nDATE_FROM: NONE",
    "jane\nDATE_FROM: last week",
    "jane\nDATE_TO: 2026-13-40",
])
async def test_unparseable_dates_are_ignored(reply):
    terms = await extract_search_terms("q", StubLLM(reply), TODAY)
    assert (terms.date_from, terms.date_to) == (None, None)
    assert terms.keywords == ["jane"]


async def test_no_date_lines_means_no_range():
    terms = await extract_search_terms("q", StubLLM("jane"), TODAY)
    assert (terms.date_from, terms.date_to) == (None, None)


async def test_future_end_is_clamped_to_today():
    llm = StubLLM("DATE_FROM: 2026-11-01\nDATE_TO: 2026-11-30")
    terms = await extract_search_terms("q", llm, TODAY)
    assert (terms.date_from, terms.date_to) == (date(2026, 11, 1), TODAY)


async def test_future_start_discards_range():
    llm = StubLLM("jane\nDATE_FROM: 2026-12-01")
    terms = await extract_search_terms("q", llm, TODAY)
    assert (terms.date_from, terms.date_to) == (None, None)


async def test_inverted_range_is_discarded():
    llm = StubLLM("DATE_FROM: 2026-10-05\nDATE_TO: 2026-09-01")
    terms = await extract_search_terms("q", llm, TODAY)
    assert (terms.date_from, terms.date_to) == (None, None)


async def test_llm_failure_gives_static_keywords_and_no_range():
    terms = await extract_search_terms("budget from jane", StubLLM(RuntimeError("down")), TODAY)
    assert terms.keywords == ["budget", "jane"]
    assert (terms.date_from, terms.date_to) == (None, None)


# --- day_bounds ----------------------------------------------------------------

def test_day_bounds_end_covers_whole_to_day():
    start, end = day_bounds(date(2024, 3, 15), date(2024, 3, 15))
    assert start == datetime(2024, 3, 15, tzinfo=timezone.utc)
    assert end == datetime(2024, 3, 16, tzinfo=timezone.utc)
    late_on_the_day = datetime(2024, 3, 15, 23, 59, tzinfo=timezone.utc)
    assert start <= late_on_the_day < end


def test_day_bounds_uses_user_offset():
    # UTC-5: the user's March 15 runs from 05:00 UTC Mar 15 to 05:00 UTC Mar 16.
    start, end = day_bounds(date(2024, 3, 15), date(2024, 3, 15), utc_offset_minutes=-300)
    assert start == datetime(2024, 3, 15, 5, tzinfo=timezone.utc)
    assert end == datetime(2024, 3, 16, 5, tzinfo=timezone.utc)
    evening_local = datetime(2024, 3, 16, 1, tzinfo=timezone.utc)  # 8pm Mar 15 local
    assert start <= evening_local < end


def test_day_bounds_open_ended():
    assert day_bounds(None, None) == (None, None)
    start, end = day_bounds(date(2024, 3, 15), None)
    assert end is None and start is not None
    start, end = day_bounds(None, date(2024, 3, 15))
    assert start is None and end == datetime(2024, 3, 16, tzinfo=timezone.utc)
