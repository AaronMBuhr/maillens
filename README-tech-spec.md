# MailLens — Technical Specification

**Status:** describes the code on `main` as of the commit that last modified this file.
**Scope:** implementation reference. For installation, GPU setup, and model selection guidance, see [README.md](README.md).

Every claim below is anchored to a source location in `file:line` form. Where `README.md` and the code disagree, the code is authoritative and the discrepancy is recorded in [§16 Documentation drift](#16-documentation-drift).

---

## Table of contents

1. [What the system does](#1-what-the-system-does)
2. [Architecture](#2-architecture)
3. [Technology stack](#3-technology-stack)
4. [Repository layout](#4-repository-layout)
5. [Data model](#5-data-model)
6. [Ingestion pipeline](#6-ingestion-pipeline)
7. [Retrieval subsystem](#7-retrieval-subsystem)
8. [Query request lifecycle](#8-query-request-lifecycle)
9. [Context budget management](#9-context-budget-management)
10. [LLM provider abstraction](#10-llm-provider-abstraction)
11. [HTTP API surface](#11-http-api-surface)
12. [SSE streaming protocol](#12-sse-streaming-protocol)
13. [Frontend](#13-frontend)
14. [Configuration system](#14-configuration-system)
15. [Deployment and runtime topology](#15-deployment-and-runtime-topology)
16. [Documentation drift](#16-documentation-drift)
17. [Known gaps, dead code, and limitations](#17-known-gaps-dead-code-and-limitations)

---

## 1. What the system does

MailLens is a self-hosted retrieval-augmented generation (RAG) system over a local Thunderbird mail archive. It has three functional modes:

| Mode | Entry point | Purpose |
|---|---|---|
| **Ingest** | `POST /api/ingest/start` | Walk a mounted Thunderbird profile, parse every mbox/Maildir message, clean and chunk bodies, embed each chunk locally, and persist to PostgreSQL. |
| **Query** | `POST /api/query/` | Answer a natural-language question by retrieving relevant messages via hybrid search and passing them as context to an LLM, streamed back with citations. |
| **Browse** | `GET /api/messages/` | Conventional paginated, sortable, filterable inbox view over the indexed corpus. |

The distinguishing design choices, relative to a generic RAG pipeline:

- **Embeddings are always local.** Vector generation runs against a containerised Ollama instance (`nomic-embed-text`, 768-dim). Message content reaches a third party only if the user selects a cloud LLM provider for the *answer* step.
- **Retrieval is hybrid, keyword-dominant.** A vector path and an SQL `ILIKE` path run independently and are merged with a fixed 40/60 weighting favouring keyword hits (`backend/storage/queries.py:82-83`). This exists because email queries are heavily proper-noun driven ("what did Jane say about the Q3 budget") and pure embedding similarity buries exact-name matches.
- **Keyword extraction and follow-up rewriting are themselves LLM calls.** Before retrieval runs, the active provider is invoked twice on conversational turns — once to rewrite the follow-up into a standalone query, once to extract search terms. Both degrade to deterministic fallbacks on failure.
- **Context sizing is computed, not fixed.** The number of messages retrieved and the per-message body cap are derived at request time from the active provider's declared context window, rather than a constant `top_k`.

---

## 2. Architecture

### 2.1 Service topology

Three containers, defined in `docker-compose.yml`:

```
                              host :8000
                                   │
   ┌───────────────────────────────▼──────────────────────────────┐
   │  app  (python:3.12-slim)                                     │
   │  ┌────────────────────────────────────────────────────────┐  │
   │  │  uvicorn --workers 1                                   │  │
   │  │  ├── StaticFiles  /            → built React SPA       │  │
   │  │  ├── /api/query      query.py                          │  │
   │  │  ├── /api/ingest     ingest.py                         │  │
   │  │  ├── /api/messages   messages.py                       │  │
   │  │  └── /api/settings   settings.py                       │  │
   │  └────────────────────────────────────────────────────────┘  │
   │  mounts:  /mail  (ro, host Thunderbird profile)              │
   │           /app/config.yaml (ro)                              │
   └────────┬──────────────────────────────────┬──────────────────┘
            │ asyncpg                          │ httpx
            ▼                                  ▼
   ┌──────────────────────┐        ┌──────────────────────────────┐
   │ db                   │        │ ollama                       │
   │ pgvector/pgvector:   │        │ ollama/ollama:latest         │
   │   pg16               │        │ • nomic-embed-text (embed)   │
   │ vol: pgdata          │        │ • optional local chat model  │
   │ :5432                │        │ vol: ollama_models  :11434   │
   └──────────────────────┘        └──────────────────────────────┘
                                                │
                        (only when active_provider != ollama)
            ┌───────────────────┬───────────────┴───┐
            ▼                   ▼                   ▼
      Anthropic API        OpenAI API          Gemini API
```

Startup ordering is enforced by compose healthchecks: `app` declares `depends_on: {db: service_healthy, ollama: service_healthy}` (`docker-compose.yml:33-37`), with `pg_isready` gating the database and `ollama list` gating Ollama.

### 2.2 Process model

The application runs as a **single uvicorn worker** (`docker/entrypoint.sh:16`). This is load-bearing, not incidental: ingestion progress is held in a module-level global (`_progress`, `backend/ingestion/pipeline.py:60`) and the LLM provider instance cache is likewise per-process (`_provider_cache`, `backend/llm/factory.py:9`). Scaling to multiple workers would fragment both — the `/api/ingest/status` endpoint would report whichever worker's global the request happened to land on.

Ingestion runs as a FastAPI `BackgroundTasks` coroutine on the same event loop as the API (`backend/api/ingest.py:31-35`). It is I/O-bound (mailbox reads, HTTP calls to Ollama, database writes), so it yields regularly, but a long ingest and a concurrent query share one loop.

### 2.3 Layering

```
api/          HTTP boundary — Pydantic request/response models, SSE framing
  ├── query.py       orchestrates the full RAG pipeline
  ├── ingest.py      fire-and-forget trigger + progress polling
  ├── messages.py    inbox browse/detail
  └── settings.py    config introspection + runtime provider switch
llm/          provider abstraction — one class per backend, uniform interface
ingestion/    parse → clean → chunk → embed → thread
storage/      SQLAlchemy models, async session factory, retrieval queries
config.py     YAML + environment overlay, singleton
```

Dependencies flow strictly downward. `storage/queries.py` is the one exception: `extract_search_keywords()` and `rewrite_follow_up_query()` accept an `llm_provider` argument (duck-typed, not imported), so the storage layer invokes an LLM without depending on the `llm` package.

---

## 3. Technology stack

### 3.1 Backend

| Component | Package | Constraint (`backend/requirements.txt`) | Role |
|---|---|---|---|
| Runtime | CPython | 3.12 (`python:3.12-slim`) | — |
| Web framework | `fastapi` | `>=0.115.0` | Routing, validation, SSE via `StreamingResponse` |
| ASGI server | `uvicorn[standard]` | `>=0.30.0` | HTTP/1.1 server, single worker |
| ORM | `sqlalchemy[asyncio]` | `>=2.0.0` | 2.0-style declarative models, async sessions |
| DB driver (async) | `asyncpg` | `>=0.30.0` | Runtime query path |
| DB driver (sync) | `psycopg[binary]` | `>=3.2.0` | Schema creation only (`init_db.py`) |
| Vector types | `pgvector` | `>=0.3.0` | `Vector` column type, `cosine_distance()` operator |
| LLM SDKs | `anthropic` / `openai` / `google-genai` | `>=0.40.0` / `>=1.50.0` / `>=1.0.0` | Cloud providers |
| HTTP client | `httpx` | `>=0.27.0` | Ollama embed + chat (no SDK) |
| Config | `pyyaml`, `pydantic` | `>=6.0`, `>=2.0` | Typed config with defaults |
| Reply stripping | `email-reply-parser` | `>=0.5.0` | Quoted-chain and signature removal |
| PDF extraction | `pymupdf` | `>=1.24.0` | Attachment text extraction |

Email parsing itself uses the **standard library** — `email`, `mailbox`, `email.header`, `email.utils` — with no third-party MIME layer.

### 3.2 Frontend

| Component | Version (`frontend/package.json`) | Role |
|---|---|---|
| `react` / `react-dom` | `^18.3.0` | UI |
| `react-router-dom` | `^6.26.0` | Client-side routing, 4 routes |
| `lucide-react` | `^0.400.0` | Icon set |
| `vite` | `^5.4.0` | Dev server + production bundler |
| `@vitejs/plugin-react` | `^4.3.0` | JSX/Fast Refresh |

No state management library, no data-fetching library, no CSS framework. State is `useState`/`useMemo` local to each page; requests use bare `fetch`; styling is a single hand-written stylesheet (`frontend/src/styles/global.css`) driven by CSS custom properties.

### 3.3 Infrastructure

| Component | Image / tool | Role |
|---|---|---|
| Database | `pgvector/pgvector:pg16` | PostgreSQL 16 + `vector` extension |
| Inference | `ollama/ollama:latest` | Embeddings, optionally chat |
| Build | Multi-stage Dockerfile (`node:20-alpine` → `python:3.12-slim`) | Frontend bundled into backend image |
| Orchestration | Docker Compose, with an optional NVIDIA override | `docker-compose.gpu.yml` |

---

## 4. Repository layout

```
maillens/
├── backend/
│   ├── main.py                    FastAPI app, lifespan, CORS, router mount, static serve
│   ├── config.py                  Pydantic config models, env overlay, singleton
│   ├── setup_models.py            Startup: pull required Ollama models
│   ├── requirements.txt
│   ├── requirements-dev.txt       requirements.txt + pytest, pytest-asyncio
│   ├── api/
│   │   ├── query.py               RAG orchestration + SSE (286 lines)
│   │   ├── ingest.py              Ingestion trigger + status
│   │   ├── messages.py            Inbox list/detail/accounts
│   │   └── settings.py            Settings read, provider switch, corpus stats
│   ├── ingestion/
│   │   ├── parser.py              mbox/Maildir discovery + MIME parsing (300 lines)
│   │   ├── cleaner.py             Reply stripping, HTML→text, whitespace normalisation
│   │   ├── embedder.py            Ollama embed client + chunker
│   │   ├── threading.py           Thread reconstruction from headers
│   │   ├── attachments.py         PDF/plain-text extraction
│   │   └── pipeline.py            Two-phase orchestration (322 lines)
│   ├── llm/
│   │   ├── base.py                LLMProvider ABC + shared context formatting
│   │   ├── factory.py             Provider construction/caching + SYSTEM_PROMPT
│   │   └── {anthropic,openai,gemini,ollama}_provider.py
│   └── storage/
│       ├── models.py              5 SQLAlchemy models
│       ├── db.py                  Async engine + session factory
│       ├── init_db.py             CREATE EXTENSION + create_all
│       └── queries.py             Hybrid search, keyword extraction, rewriting (511 lines)
├── frontend/
│   ├── src/
│   │   ├── App.jsx                Router + sidebar shell
│   │   ├── main.jsx
│   │   ├── pages/{Chat,Inbox,Ingestion,Settings}Page.jsx
│   │   └── styles/global.css
│   ├── vite.config.js             /api → localhost:8000 dev proxy
│   └── package.json
├── docker/
│   ├── Dockerfile                 Two-stage build
│   └── entrypoint.sh              setup_models → init_db → uvicorn
├── tests/                         pytest unit tests for retrieval (§17.5)
├── pytest.ini
├── docker-compose.yml
├── docker-compose.gpu.yml         NVIDIA device reservation override
├── config.example.yaml            Tracked template
└── .env.example                   Tracked template
```

`config.yaml` and `.env` are gitignored (`.gitignore:13-14`) — they are local working copies. All defaults quoted in this document come from `backend/config.py` (Pydantic) and `config.example.yaml` (tracked template).

---

## 5. Data model

### 5.1 Schema creation

There is **no migration framework**. `backend/storage/init_db.py` runs on every container start (`docker/entrypoint.sh:12`) and does exactly two things:

```python
conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))   # init_db.py:17
Base.metadata.create_all(engine)                              # init_db.py:20
```

`create_all` is additive-only: it creates missing tables and indexes but never alters existing ones. Any change to a column type, constraint, or index definition requires dropping the `pgdata` volume and re-ingesting.

### 5.2 `messages`

`backend/storage/models.py:28-81`. One row per unique email.

| Column | Type | Nullable | Index | Notes |
|---|---|---|---|---|
| `id` | `Integer` | no | PK | Autoincrement. Used as the citation `id` surfaced to the LLM and frontend. |
| `message_id` | `String` | no | unique, btree | `Message-ID` header, or a synthesised fallback (§6.3). |
| `in_reply_to` | `String` | yes | btree | Direct parent for threading. |
| `references` | `Text` | yes | — | Space-separated reference chain. |
| `thread_id` | `Integer` | yes | btree, FK→`threads.id` | Assigned in ingestion phase 2. |
| `subject` | `Text` | yes | — | Decoded, null-byte stripped. |
| `sender` | `String` | yes | btree | Raw `From` including display name — critical for the keyword path. |
| `recipients_to` | `Text` | yes | — | Not indexed; `ILIKE` scans. |
| `recipients_cc` | `Text` | yes | — | Stored, never searched. |
| `date` | `DateTime(timezone=True)` | yes | btree + `ix_messages_date_desc` | Normalised to UTC at parse time. |
| `account` | `String` | yes | btree | Derived from Thunderbird directory structure. |
| `folder` | `String` | yes | btree | Dotted relative path, e.g. `ImapMail.imap.gmail.com.INBOX`. |
| `source_file` | `String` | yes | — | Absolute container path of the mbox/Maildir. |
| `body_text` | `Text` | yes | — | Decoded `text/plain` part. |
| `body_html` | `Text` | yes | — | Decoded `text/html` part, unprocessed. |
| `body_clean` | `Text` | yes | — | Post-cleaning. **This is what is embedded and what is sent to the LLM.** |
| `embedding` | `Vector(768)` | yes | HNSW cosine | Mean of non-zero chunk embeddings. |
| `has_attachments` | `Boolean` | — | — | Default `False`. |
| `ingested_at` | `DateTime(timezone=True)` | — | — | `func.now()` server default. |

Two explicit indexes beyond the column-level ones (`models.py:72-81`):

```sql
CREATE INDEX ix_messages_date_desc ON messages (date DESC);
CREATE INDEX ix_messages_embedding_cosine ON messages
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

Relationships: `thread` (many-to-one), `attachments` and `chunks` (one-to-many, `cascade="all, delete-orphan"`, chunks ordered by `chunk_index`).

### 5.3 `message_chunks`

`models.py:98-120`. One row per embedding window of a message body. This is the **primary vector search target** — message-level embeddings are only a fallback.

| Column | Type | Notes |
|---|---|---|
| `id` | `Integer` PK | |
| `message_id` | `Integer` FK→`messages.id` | `ondelete="CASCADE"` at the database level *and* `delete-orphan` at the ORM level. |
| `chunk_index` | `Integer` | Position within parent. |
| `chunk_text` | `Text` | Not null. The exact text embedded. |
| `embedding` | `Vector(768)` | **Nullable** — set to `NULL` when embedding produced an all-zero vector (§6.5). |

```sql
CREATE UNIQUE INDEX ix_message_chunks_msg_idx ON message_chunks (message_id, chunk_index);
CREATE INDEX ix_message_chunks_embedding_cosine ON message_chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

### 5.4 `threads`

`models.py:84-95`. Aggregate row per reconstructed conversation.

| Column | Type | Maintenance |
|---|---|---|
| `id` | `Integer` PK | |
| `subject` | `Text` | Subject of whichever member message created the thread row. |
| `first_date` / `last_date` | `DateTime(timezone=True)` | Min/max computed incrementally during phase 2. |
| `message_count` | `Integer` | Incremented per assignment. |

Counters are only updated by the ingestion phase-2 pass over messages in *that run*. A reply ingested in a later run creates its own thread grouping rather than joining and updating the existing row (§17).

### 5.5 `attachments`

`models.py:123-135`. Written during ingestion, read by nothing.

| Column | Type | Notes |
|---|---|---|
| `id` | `Integer` PK | |
| `message_id` | `Integer` FK→`messages.id` | Note: no `ondelete` clause, unlike `message_chunks`. |
| `filename`, `content_type`, `size_bytes` | `String`/`String`/`Integer` | From MIME headers. |
| `extracted_text` | `Text` | PDF and plain-text content, extracted at ingest. |

`extracted_text` is populated (`pipeline.py:129-137`) but never embedded and never queried — verified: no reference outside `models.py`, `pipeline.py`, and `attachments.py`. See §17.

### 5.6 `ingestion_runs`

`models.py:138-150`. Defined with columns for run history (`started_at`, `completed_at`, `status`, counts, `errors`), created by `create_all`, and **never instantiated**. The class is imported in `pipeline.py:25` but never constructed. Run state lives entirely in the in-process `IngestionProgress` global.

---

## 6. Ingestion pipeline

Orchestrated by `run_ingestion()` (`backend/ingestion/pipeline.py:142-322`). Two phases: per-source parse/embed/store, then a global threading pass.

### 6.1 Source discovery

`discover_mail_sources()` (`parser.py:264-300`) returns `list[tuple[path, format, folder_name, account]]`.

If the root itself is a single mbox file or Maildir, it is returned as the only source. Otherwise `root.rglob("*")` is walked in sorted order with these rules:

| Condition | Classification |
|---|---|
| Name starts with `.` | Skipped |
| File, **no suffix**, not `*.msf`, size > 0 | `mbox` |
| Directory containing `cur/` or `new/` | `maildir` |
| Anything else | Ignored |

The extensionless heuristic is what makes Thunderbird work: Thunderbird stores mbox files without extension and their index sidecars as `<name>.msf`. `folder_name` is the path relative to the root with separators replaced by `.`.

Account attribution (`_extract_account`, `parser.py:244-261`) reads the Thunderbird convention:

| Path shape | Account |
|---|---|
| `ImapMail/<server>/…` | `<server>` (e.g. `imap.gmail.com`) |
| `Mail/<account>/…` | `<account>` (e.g. `Local Folders`) |
| anything else | first path component, or `"default"` |

### 6.2 MIME parsing

`parse_mbox` / `parse_maildir` (`parser.py:197-228`) are generators wrapping `mailbox.mbox` / `mailbox.Maildir`. Each message is parsed inside a `try/except` that logs and continues — one corrupt message cannot abort a folder.

`_extract_body_and_attachments()` (`parser.py:98-157`) walks MIME parts and applies **first-wins** selection: the first `text/plain` part becomes `body_text`, the first `text/html` becomes `body_html`, and any part with `Content-Disposition: attachment` is collected regardless of type.

### 6.3 Parsing robustness

Four hardening measures, each addressing a specific failure observed against real archives:

| Helper | Location | Problem solved |
|---|---|---|
| `_sanitize()` | `parser.py:59-63` | Strips `\x00`. PostgreSQL rejects null bytes in `text` columns; they appear in malformed messages. |
| `_safe_charset()` | `parser.py:86-95` | `codecs.lookup()` probe with UTF-8 fallback. Garbage `charset=` values otherwise raise `LookupError`. |
| `_parse_date()` | `parser.py:66-83` | Normalises every date to tz-aware UTC. Mixing naive and aware datetimes in one asyncpg batch insert raises. |
| Synthetic `Message-ID` | `parser.py:167-174` | For messages lacking the header: `sha256(subject + date + sender + source_file + first 1024 bytes)[:24]`, formatted `<generated-{digest}@maillens>`. Deterministic, so re-ingestion produces the same ID and dedupes correctly. |

All decoded values pass through `_decode_header()` (`parser.py:45-56`), which resolves RFC 2047 encoded-words with `errors="replace"`.

### 6.4 Cleaning

`clean_message_body()` (`cleaner.py:62-83`) — three ordered steps:

1. **Source selection.** Prefer `body_text`; fall back to `strip_html_tags(body_html)`; return `""` if neither exists.
2. **Reply stripping.** `strip_quoted_replies()` (`cleaner.py:11-27`) runs `EmailReplyParser.read()` and keeps only fragments where `not fragment.quoted and not fragment.hidden`. **If that yields an empty string, the full original text is returned instead** — a deliberate guard against the parser over-stripping short messages into nothing.
3. **Normalisation.** CRLF→LF, tabs→spaces, runs of 2+ spaces collapsed, 3+ newlines collapsed to 2.

`strip_html_tags()` (`cleaner.py:30-50`) is regex-based, not a parser: it removes `<style>`/`<script>` blocks, converts `<br>` and closing `</p></div></tr></li>` to newlines, strips remaining tags, and decodes five entities (`&amp; &lt; &gt; &nbsp; &quot;`). Numeric character references and less common named entities pass through literally.

### 6.5 Chunking and embedding

`chunk_text()` (`embedder.py:55-84`) splits on whitespace with a fixed word-to-token ratio:

```
words_per_chunk = int(chunk_size * 0.75)   # 512 → 384 words
overlap_words   = int(overlap    * 0.75)   #  64 →  48 words
stride          = words_per_chunk - overlap_words   # 336 words
```

Text at or below `words_per_chunk` returns as a single unsplit chunk.

The text submitted for embedding is not the raw body — `pipeline.py:208-214` prepends the subject:

```
Subject: {subject}

{body_clean}
```

`embed_texts()` (`embedder.py:11-52`) posts to Ollama's `POST /api/embed`, **one text per HTTP request** — the `batch_size` loop partitions the list but still issues sequential requests inside each partition, so `batch_size` controls database commit granularity, not embedding concurrency. Timeout is 120s per client (`embedder.py:28`).

Two failure paths both produce a zero vector `[0.0] * 768`: empty/whitespace input (`embedder.py:34-36`) and any exception during the HTTP call (`embedder.py:48-50`). Downstream, `_store_message()` (`pipeline.py:95, 119-125`) treats zero vectors as sentinel values:

- Chunks with a zero embedding are stored with `embedding=NULL`.
- The message-level embedding is the element-wise mean of **non-zero** chunk embeddings only, or `NULL` if none survive.

`_average_embeddings()` (`pipeline.py:74-84`) is a plain Python double loop — no NumPy.

### 6.6 Deduplication

Two independent checks, both applied unconditionally (`pipeline.py:195-201`):

1. `seen_in_run: set[str]` — in-memory, catches the same message appearing in multiple Thunderbird folders within one run (Gmail's `All Mail` duplicating every other folder is the motivating case).
2. `_message_exists()` — a `SELECT id … LIMIT 1` per candidate against the unique `message_id` index, catching messages from prior runs.

This is one round-trip per candidate message. On a 66k-message archive that is 66k point lookups against a unique btree — individually cheap, collectively the dominant cost of a no-op incremental run.

### 6.7 Batching and error isolation

The inner loop accumulates parsed messages until `len(batch_chunk_texts) >= config.embeddings.batch_size` (default 64), then embeds the whole batch, stores each message with its slice of the results, and commits (`pipeline.py:225-237`). A trailing flush handles the remainder per source (`pipeline.py:249-262`).

On any exception during accumulation (`pipeline.py:239-247`): the error is appended to `_progress.errors`, the session is rolled back, **all three batch accumulators are cleared**, and the loop continues to the next message. The consequence is that a single bad message discards up to 63 already-parsed siblings from that batch — they are neither stored nor retried within the run. They will be picked up by a subsequent incremental run.

Sessions are scoped per source directory (`async with session_factory()`, `pipeline.py:185`), so a failure cannot poison a later folder.

### 6.8 Phase 2: thread reconstruction

Runs once after all sources are processed (`pipeline.py:264-318`).

Messages ingested in this run are re-fetched by `message_id`, **batched at 30,000 per query** (`BATCH_LIMIT`, `pipeline.py:266`) because asyncpg enforces PostgreSQL's 32,767 bind-parameter limit and `IN (...)` binds one parameter per element.

`build_threads()` (`threading.py:18-74`) is a union-find-by-root-walk:

1. Build `parent_map`: `in_reply_to` when present, else the **last** entry of `references` (the direct parent — the first entry is the thread root).
2. `find_root()` walks the parent chain to a message with no known parent, carrying a `visited` set so header cycles terminate rather than recursing infinitely (`threading.py:45-54`).
3. Each distinct root is assigned a sequential integer thread group.

Back in the pipeline, each group id materialises one `Thread` row on first encounter (with `session.flush()` to obtain its PK), then every member message gets `thread_id` set and the thread's `message_count`/`first_date`/`last_date` updated.

Because `find_root` resolves against `parent_map` built only from *this run's* messages, a parent referenced but not present in the run is treated as a root — meaning a reply ingested separately from its parent starts a new thread.

### 6.9 Progress reporting

`IngestionProgress` (`pipeline.py:28-56`) is a module-level singleton, replaced wholesale at the start of each run (`pipeline.py:159`). `to_dict()` exposes counters plus the **last 10** errors (`pipeline.py:53`), while `error_count` reports the true total. State is in-memory only — a container restart loses it, and `/api/ingest/status` reports `idle` afterwards regardless of what happened.

---

## 7. Retrieval subsystem

`hybrid_search()` (`backend/storage/queries.py:283-439`). Two independent SQL queries merged into one ranked list, with a Python-side body check for vector candidates that the keyword query missed.

### 7.1 Keyword extraction

The keyword list is the more heavily weighted signal, so it is produced by the **active LLM**, not by tokenisation.

`extract_search_keywords()` (`queries.py:98-131`) calls `llm_provider.complete()` with `_KEYWORD_EXTRACT_PROMPT` (`queries.py:24-32`) and an empty `context_messages`. The prompt instructs the model to emit only search-worthy terms — proper nouns, company names, domains, specific phrases — one per line, lowercase, or the literal `NONE`.

Post-processing (`queries.py:109-128`):

1. Split into lines; if the result is `["none"]` or empty, fall back to static extraction.
2. Strip leading/trailing non-alphanumerics from each line.
3. Split multi-word phrases into individual words. Within each word keep only `a-z`, `0-9`, `.`, `@` and `-`, then trim `.`/`@`/`-` from the ends (`queries.py:117-119`), so domains and addresses the prompt asks for (`acme.com`, `jane.doe@acme.com`) survive intact for `ILIKE`. `_` and `%` are removed because they are `LIKE` wildcards. Drop words of length ≤ 1 and anything in `_STOP_WORDS`.
4. Deduplicate preserving order (`dict.fromkeys`).
5. **Substring elimination** (`queries.py:123-126`): drop any keyword that is a proper substring of another surviving keyword — `["jane", "janedoe"]` collapses to `["janedoe"]`, preventing the shorter, noisier term from inflating hit counts.
6. If nothing survives, fall back to static extraction.

The fallback `_extract_keywords_static()` (`queries.py:92-95`) is regex tokenisation (`[a-zA-Z0-9]+`), lowercased, filtered against `_STOP_WORDS` and length > 1.

`_STOP_WORDS` (`queries.py:51-80`) is a 191-entry frozenset (192 literals; `"been"` is listed twice) spanning five categories: standard English function words; query verbs (`find`, `show`, `summarize`, `describe`); email-domain nouns (`email`, `message`, `mail`, `subject`, `conversation`); vague quantifiers and qualifiers (`common`, `topic`, `frequent`, `important`, `overall`); and mail verbs (`sent`, `received`, `wrote`, `replied`).

Every failure mode — API error, empty response, all-stopword output — degrades to the static path rather than propagating. Retrieval never fails because keyword extraction failed.

### 7.2 Follow-up query rewriting

`rewrite_follow_up_query()` (`queries.py:134-177`) runs **only when `conversation_history` is non-empty** and only in the query path (`query.py:132-134`).

The problem it addresses: a follow-up like *"did they mention three parts?"* embedded and keyword-extracted in isolation retrieves nothing useful, because the subject of the conversation is absent from the string.

Mechanism: the conversation is serialised as `Role: content` lines with **assistant turns truncated to 500 characters** (`queries.py:154-155`) to bound the rewrite prompt, appended with `Follow-up question: {question}`, and sent with `_QUERY_REWRITE_PROMPT` (`queries.py:34-49`). The prompt requires a self-contained rewrite, preservation of all specifics from the follow-up, and pass-through unchanged if the follow-up is already standalone or opens a new topic.

The result is stripped of surrounding quotes. On empty output or any exception, the original question is returned.

**The rewritten query drives retrieval only.** The LLM's final answer receives `request.question` verbatim (`query.py:242`), so the user's actual phrasing is never replaced in the conversation.

**Cost:** a conversational turn issues two extra LLM round-trips (rewrite, then keyword extract) *before* retrieval begins, both blocking. On a local Ollama model this dominates perceived latency.

### 7.3 Path 1 — vector similarity

`queries.py:310-340`. A grouped subquery finds each message's best-matching chunk:

```sql
SELECT message_id, MAX(1 - (embedding <=> :q)) AS best_sim
FROM message_chunks
WHERE embedding IS NOT NULL
GROUP BY message_id
```

The outer query left-joins that and coalesces to the message-level embedding when no chunk matched:

```sql
SELECT messages.*, COALESCE(sub.best_sim, 1 - (messages.embedding <=> :q)) AS similarity
FROM messages LEFT JOIN sub ON messages.id = sub.message_id
WHERE (sub.best_sim IS NOT NULL OR messages.embedding IS NOT NULL)
  AND <metadata filters>
ORDER BY similarity DESC
LIMIT :top_k * 3
```

Best-chunk-wins (rather than mean-of-chunks) is the right choice for email: a long thread whose relevant content is one paragraph should score on that paragraph, not have it diluted.

> **Index note.** pgvector's HNSW index accelerates `ORDER BY <distance> LIMIT n`. As written, the chunk subquery is an unbounded `GROUP BY` aggregate over every non-null chunk embedding, which the HNSW index cannot serve — it requires a full scan of `message_chunks` per query. The indexes are correctly declared; the query shape is what prevents their use on this path.

### 7.4 Path 2 — keyword ILIKE

`queries.py:342-385`. Runs only when the keyword list is non-empty.

Per keyword, the match condition is (`queries.py:352-358`):

```python
or_(
    Message.sender.ilike(f"%{kw}%"),
    Message.subject.ilike(f"%{kw}%"),
    and_(all_kw_in_recip, Message.recipients_to.ilike(f"%{kw}%")),
)
```

`all_kw_in_recip` (`queries.py:346-348`) is the conjunction of `recipients_to ILIKE '%kw%'` over **every** keyword. So the recipients field only contributes to a match when *all* keywords appear in it — a deliberate asymmetry. Matching recipients on any single keyword would return every message addressed to a common mailing list; requiring all of them means recipients only fires on genuinely specific queries.

Hit counting is done in SQL via summed `CASE` expressions:

```sql
SELECT messages.*, (
    CASE WHEN (<kw1 match>) THEN 1 ELSE 0 END +
    CASE WHEN (<kw2 match>) THEN 1 ELSE 0 END + …
) AS kw_hits
FROM messages
WHERE (<kw1 match> OR <kw2 match> OR …)
  AND <metadata filters>
ORDER BY kw_hits DESC, date DESC
LIMIT :top_k * 5
```

A second demotion is applied in Python (`queries.py:376-380`): if none of the keywords appears in `sender + subject`, the hit ratio is multiplied by **0.1**. Combined with the `all_kw_in_recip` gate, this means recipients-only matches survive but are pushed far down the ranking.

These are unanchored `ILIKE '%…%'` patterns, which no btree index can serve — the keyword path is a sequential scan of `messages` per query.

### 7.5 Merge and scoring

`queries.py:336-439`. Both result sets are folded into `candidates: dict[int, tuple[Message, vscore, kscore]]` keyed by primary key, so a message found by both paths carries both scores.

```
combined = 0.4 · vector_similarity + 0.6 · keyword_hit_ratio     (keywords present)
combined = vector_similarity                                      (no keywords)
```

Weights are module constants `VECTOR_WEIGHT = 0.4` / `KEYWORD_WEIGHT = 0.6` (`queries.py:82-83`) — not configurable via YAML. For a body-rescued candidate (step 1), `keyword_hit_ratio` is replaced by `body_ratio · BODY_HIT_WEIGHT`.

Then, in order:

1. **Body rescue, then keyword gate** (`queries.py:399-412`). When keywords exist, a candidate with `kscore <= 0` that is not in `previous_source_ids` first gets a second chance: `_keyword_hit_ratio(msg, keywords, include_body=True)` (`queries.py:206-236`) re-counts the keywords against the header fields (substring, as `ILIKE` does) plus `body_clean`, falling back to `body_text` (whole-word, via `_contains_word`, `queries.py:180-203`). A non-zero ratio becomes `kscore = ratio · BODY_HIT_WEIGHT` (`0.5`, `queries.py:89`). Any candidate still at `kscore <= 0` is then **discarded outright**, unless it is in `previous_source_ids`. So a vector-only match survives a keyword-bearing query only if a keyword appears in its body, as a whole word, or in a header field the keyword query did not credit. Previous sources skip the rescue so a follow-up turn scores them the same way the turn that produced them did. Setting `BODY_HIT_WEIGHT = 0.0` restores the old header-only gate.
2. **Continuity boost** (`queries.py:416-417`). Messages whose `id` appears in `previous_source_ids` — the sources cited in earlier turns of this conversation, sent up by the frontend — receive a flat `+0.15` (`CONTINUITY_BOOST`, `queries.py:280`). This both keeps prior context stable across turns and exempts those messages from the keyword gate.
3. **Absolute threshold.** `combined >= similarity_threshold` (from config).
4. **Sort** descending by combined score.
5. **Relative cutoff** (`queries.py:429-432`). `cutoff = best_score * 0.4`; everything below is dropped. This is the mechanism that prevents a large adaptive `top_k` from padding the LLM context with weak matches when only a handful of emails are genuinely relevant — an absolute threshold alone cannot distinguish "few good results" from "many mediocre ones".
6. **Truncate** to `top_k` and serialise via `_msg_to_dict()` (`queries.py:263-277`), which attaches the combined score as `similarity`.

Diagnostics for each stage are printed to stdout (`queries.py:389-393, 422-427, 434-437`), including the top 10 scored results with sender and subject and, when any occurred, how many candidates were body-rescued and how many of those survived the relative cutoff — the primary debugging surface for retrieval quality.

### 7.6 Metadata filters

`_build_metadata_filters()` (`queries.py:239-260`) builds a conjunctive filter list applied identically to **both** paths:

| Parameter | Predicate |
|---|---|
| `sender` | `sender ILIKE '%value%'` |
| `date_from` / `date_to` | `date >= …` / `date <= …` |
| `folder` | `folder = value` (exact) |
| `has_attachments` | `has_attachments = value` |
| `accounts` | `account IN (…)` |

---

## 8. Query request lifecycle

`POST /api/query/` → `query_email()` (`query.py:206-286`). The shared work is in `_run_search_pipeline()` (`query.py:117-203`); streaming and non-streaming modes differ only in how the provider is invoked.

```
1.  Resolve provider + provider config          factory.get_llm_provider / get_active_provider_config
2.  Compute char budget                         _compute_context_budget          query.py:127
3.  Derive adaptive top_k                       _estimate_top_k                  query.py:128
                                                (request.top_k overrides)
4.  Rewrite follow-up  ── LLM call #1 ──        rewrite_follow_up_query          query.py:132-134
                                                (only when history is present)
5.  Extract keywords   ── LLM call #2 ──        extract_search_keywords          query.py:137
6.  Embed search query ── Ollama call ──        embed_texts                      query.py:138
    ├─ emit `rewritten_query` (if changed)
7.  Hybrid search                               hybrid_search                    query.py:143-157
8.  Thread expansion                            get_thread_context               query.py:159-171
9.  Budget trim                                 _trim_to_budget                  query.py:175
10. Build source cards                          query.py:179-192
    ├─ emit `sources`
    ├─ emit `meta`
11. Stream LLM answer                           provider.stream()                query.py:240-246
    └─ emit `text` × N, then `done`
```

Steps 4–6 are sequential awaits; on a conversational turn against a local model they can exceed the retrieval and generation time combined.

**Thread expansion** (step 8, gated on `retrieval.include_thread_context`) iterates the **top 10** ranked results, and for each distinct `thread_id` not yet seen, fetches up to 10 sibling messages ordered by date (`get_thread_context`, `queries.py:442-472`). Siblings not already present are appended to the candidate list. Two consequences:

- Expansion happens *after* `top_k` truncation but *before* budget trimming, so thread siblings compete for budget with — and can displace — lower-ranked genuine hits.
- `get_thread_context()` returns dicts without a `similarity` key (`queries.py:457-472`), so thread-expanded entries surface in the SSE `sources` payload with `similarity: null`.

Timing is measured in two spans: `embed_time` covers steps 4–6 (both LLM calls plus embedding, despite the name), and `retrieval_time` covers steps 7–9.

---

## 9. Context budget management

The system sizes context from the active provider's declared window rather than a fixed message count. Token estimation throughout is `CHARS_PER_TOKEN = 4` (`query.py:22`) — a crude approximation, deliberately conservative.

### 9.1 Budget computation

`_compute_context_budget()` (`query.py:53-70`):

```
used_tokens      = max_output_tokens
                 + len(system_prompt) / 4
                 + len(question) / 4
                 + Σ len(history_turn.content) / 4
available_tokens = max(0, max_context_tokens - used_tokens)
char_budget      = available_tokens * 4
```

When the provider declares no `max_context_tokens`, the budget falls back to `15 * 8000 = 120,000` chars (`query.py:63`).

### 9.2 Adaptive `top_k`

`_estimate_top_k()` (`query.py:73-80`):

```
k = char_budget / 2000, clamped to [10, 500]
```

The 2,000-char divisor is an assumed average per retrieved message (headers plus a truncated body). An explicit `request.top_k` bypasses this entirely.

Worked example for Gemini at 900,000 context tokens / 32,768 output tokens:
`available ≈ 867,000 tokens → ~3.47M chars → k = 500` (clamped from 1,735).

### 9.3 Budget trimming

`_trim_to_budget()` (`query.py:86-114`) — two stages. First a per-message body cap that would let every candidate fit:

```
per_msg_cap = clamp(500, 8000, (char_budget - n·200) / n)
```

with `HEADER_CHARS_PER_MSG = 200` (`query.py:23`) and `MAX_BODY_PER_MSG = 8000` (`query.py:83`). Then a greedy fill in rank order: each body truncated to the cap with a `\n... [truncated]` marker, accumulating until the budget is exhausted or fewer than 400 chars remain.

The effect is that a handful of very long threads cannot monopolise the window — the cap is computed from candidate count *before* any message is admitted.

### 9.4 Second truncation in the provider

Each provider independently recomputes a budget via `LLMProvider._context_char_budget()` (`base.py:55-74`) and passes it to `_format_context()` (`base.py:76-125`), which applies its **own** per-message cap:

```
usable  = max_context_chars - n·180 - n·4
per_msg = max(200, usable / n)
```

So message bodies are capped twice, by two functions using different header-overhead constants (200 vs 180). In practice the second cap is the looser of the two — `_trim_to_budget` has already reduced `n` — so it rarely binds. It is nonetheless duplicated logic in two places, and the two would need to change together.

---

## 10. LLM provider abstraction

### 10.1 Interface

`LLMProvider` (`backend/llm/base.py:9-125`) — an ABC with two abstract coroutines, identical signatures:

```python
async def complete(system_prompt, user_message, context_messages, conversation_history=None) -> str
async def stream (system_prompt, user_message, context_messages, conversation_history=None) -> AsyncGenerator[str, None]
```

Two concrete helpers are shared by all four implementations:

- `_context_char_budget()` — token accounting (§9.4).
- `_format_context()` — renders retrieved messages into the block the model actually reads.

### 10.2 Context rendering

`_format_context()` (`base.py:76-125`) emits one block per message:

```
--- [Email 3] (id=48211) ---
From: "Jane Doe" <jane@example.com>
To: me@example.com
Date: 2024-03-15T14:22:00+00:00
Subject: Q3 Budget Review
Folder: ImapMail.imap.gmail.com.INBOX

<body, truncated to per-message cap>
```

The `[Email N]` label is the citation handle the system prompt instructs the model to use; the `id=` is the database primary key, letting a response be traced back to a specific row. Empty context renders the literal string `"No relevant emails found."` (`base.py:90`).

### 10.3 Provider implementations

| | Anthropic (`anthropic_provider.py`) | OpenAI (`openai_provider.py`) | Gemini (`gemini_provider.py`) | Ollama (`ollama_provider.py`) |
|---|---|---|---|---|
| Client | `anthropic.AsyncAnthropic` | `openai.AsyncOpenAI` | `genai.Client(...).aio` | raw `httpx.AsyncClient` |
| System prompt | `system=` parameter | `{"role":"system"}` message | `system_instruction` in config | `{"role":"system"}` message |
| History | alternating user/assistant dicts | interleaved message dicts | `types.Content(role="user"\|"model")` | interleaved message dicts |
| Context placement | prepended to final user message | prepended to final user message | prepended to final user `Content` | prepended to final user message |
| Output cap | `max_tokens` | `max_completion_tokens` | `max_output_tokens` | `num_ctx` option |
| Streaming | `messages.stream()` → `text_stream` | `stream=True` → `chunk.choices[0].delta` | `generate_content_stream()` | NDJSON lines over `POST /api/chat` |
| Timeout | SDK default | SDK default | SDK default | 300 s (`OLLAMA_TIMEOUT`) |

All four wrap the retrieved emails identically:

```
Here are the relevant emails from my mailbox:

{context}

My question: {user_message}
```

Provider-specific handling:

- **OpenAI** suppresses the `temperature` parameter for models whose name starts with `o1`, `o3`, or `gpt-5` (`_NO_TEMPERATURE_PREFIXES`, `openai_provider.py:12, 30-32`), since reasoning models reject it. It also uses `max_completion_tokens`, not the deprecated `max_tokens`.
- **Ollama** passes `num_ctx: max_context_tokens` to size the model's KV cache at request time, and parses the streaming response as newline-delimited JSON with malformed lines silently skipped (`ollama_provider.py:102-110`).
- **Gemini and Ollama** catch streaming exceptions internally and yield the error as text into the stream (`gemini_provider.py:91-93`, `ollama_provider.py:111-113`). Anthropic and OpenAI do not — their exceptions propagate to the handler in `query.py:247-249`, which yields `[LLM Error: …]`. Both routes surface errors to the user; they differ in where the message is composed.

### 10.4 Factory and system prompt

`get_llm_provider()` (`factory.py:12-44`) imports the provider module lazily inside the dispatch branch, so a missing or misconfigured SDK for an unused provider cannot break startup. Instances are cached in `_provider_cache` and invalidated by `clear_provider_cache()`, which `POST /api/settings/provider` calls after mutating `config.llm.active_provider` in memory (`settings.py:90-91`).

`SYSTEM_PROMPT` (`factory.py:60-72`) is a single module constant enforcing seven behaviours: answer only from provided emails; always cite `[Email N]` with sender, date, and subject; state explicitly when the context is insufficient; be concise but complete; organise pattern/timeline answers; flag contradictions and cite both sides; preserve dates, names, amounts, and commitments exactly.

---

## 11. HTTP API surface

All routers mount under `/api` (`main.py:48-51`). The built SPA is served from `/` via `StaticFiles(html=True)` when `/app/static` exists (`main.py:54-56`) — mounted last so it does not shadow API routes.

### 11.1 `POST /api/query/`

Request (`QueryRequest`, `query.py:31-42`):

| Field | Type | Default | Effect |
|---|---|---|---|
| `question` | `str` | required | The user's question. |
| `sender` | `str?` | — | `ILIKE` filter. |
| `date_from` / `date_to` | `datetime?` | — | Inclusive bounds. |
| `folder` | `str?` | — | Exact match. |
| `has_attachments` | `bool?` | — | Exact match. |
| `accounts` | `list[str]?` | — | `IN` filter. |
| `top_k` | `int?` | — | Overrides adaptive sizing. |
| `stream` | `bool` | `true` | SSE vs. buffered JSON. |
| `conversation_history` | `list[{role, content}]?` | — | Triggers query rewriting; consumed as token budget. |
| `previous_source_ids` | `list[int]?` | — | Grants `+0.15` and keyword-gate exemption. |

Response when `stream=false` (`QueryResponse`, `query.py:45-50`): `answer`, `sources`, `query_embedding_time_ms`, `retrieval_time_ms`, `llm_time_ms`.
Response when `stream=true`: `text/event-stream`, see §12.

### 11.2 `POST /api/ingest/start`

Body `{mail_directory?: str, incremental?: bool}`. Returns `{"status": "started"}`, or `{"status": "already_running", "progress": {…}}` if a run is in flight (`ingest.py:28-29`). Non-blocking — the run is queued via `BackgroundTasks`.

The `incremental` flag is accepted, threaded through to `run_ingestion()`, documented in its docstring — and **never read in the function body**. Deduplication is unconditional. "Full Re-ingest" in the UI is behaviourally identical to "Incremental Ingest" (§17).

### 11.3 `GET /api/ingest/status`

Returns `IngestionProgress.to_dict()`: `status` (`idle|running|completed|failed`), `total_sources`, `current_source`, `current_source_name`, `messages_processed`, `messages_new`, `messages_skipped`, `error_count`, `errors` (last 10), `started_at`, `completed_at`.

### 11.4 `GET /api/messages/`

| Param | Type | Default | Constraint |
|---|---|---|---|
| `page` | `int` | 1 | `≥ 1` |
| `per_page` | `int` | 50 | `1–200` |
| `sender`, `subject` | `str?` | — | `ILIKE` |
| `folder` | `str?` | — | exact |
| `date_from`, `date_to` | `datetime?` | — | bounds |
| `accounts` | `list[str]?` | — | `IN` |
| `sort_by` | `str` | `date` | `date\|sender\|subject\|account\|folder` |
| `sort_dir` | `str` | `desc` | `asc\|desc` |

Sorting resolves through `SORT_COLUMNS` (`messages.py:19-25`); an unrecognised value silently falls back to `date`. Non-date sorts append `date` as a secondary key in the same direction (`messages.py:63-65`) for stable ordering within ties. Total count is computed with a separate `COUNT(*)` over the filtered subquery before pagination is applied (`messages.py:58-59`). Each row carries a 200-char snippet from `body_clean or body_text`.

### 11.5 `GET /api/messages/accounts`

Distinct `account` values with message counts, ordered by account name.

### 11.6 `GET /api/messages/{id}`

Full message by primary key, including `body_text`, `body_clean`, `source_file`, `in_reply_to`, and `thread_id`. Returns `{"error": "Message not found"}` with **HTTP 200** on a miss — not a 404.

### 11.7 `GET /api/settings/`

Active provider, list of available providers, and per-provider `{model, has_key, max_tokens, max_context_tokens, url?}`. API keys are never returned — only the boolean `has_key` (`settings.py:48, 55, 61`). Also returns `embedding_model`, `retrieval_top_k`, `retrieval_similarity_threshold`, `mail_directory`.

### 11.8 `POST /api/settings/provider`

Body `{provider: str}`. Validates against `["anthropic", "openai", "gemini", "ollama"]`, mutates the in-memory singleton, and clears the provider cache. **Not persisted** — `config.yaml` is mounted read-only, so the change reverts on restart. An invalid value returns `{"error": …}` with HTTP 200.

### 11.9 `GET /api/settings/stats`

`message_count`, `folder_count`, full `folders` list, and `top_senders` (top 20 by message count).

---

## 12. SSE streaming protocol

`text/event-stream` with `Cache-Control: no-cache`, `Connection: keep-alive` (`query.py:253-260`). Each event is a single `data: <json>\n\n` frame. There are **six** event types:

| # | `type` | Payload | Emitted |
|---|---|---|---|
| 1 | `status` | `{message}` | Immediately on connect — `"Extracting keywords..."` (`query.py:223`). Flushes headers so the UI can render before the pipeline blocks. |
| 2 | `rewritten_query` | `{query}` | Only when the rewrite changed the question (`query.py:234-235`). |
| 3 | `sources` | `{sources: [{id, subject, sender, recipients_to, date, account, folder, similarity, snippet}]}` | After retrieval, before the LLM call. `snippet` is 200 chars of `body_clean`. |
| 4 | `meta` | `{embed_time_ms, retrieval_time_ms, context_messages, context_budget_tokens}` | Immediately after `sources`. |
| 5 | `text` | `{content}` | Once per token/chunk from the provider. |
| 6 | `done` | `{}` | Terminal frame — always emitted, including after an error. |

Ordering is fixed: `status` → [`rewritten_query`] → `sources` → `meta` → `text`* → `done`. LLM errors are delivered as a `text` frame containing `[LLM Error: …]` (`query.py:247-249`), so the stream always terminates cleanly rather than aborting the connection.

Client parsing (`ChatPage.jsx:171-188`) buffers the byte stream, splits on `\n\n` and retains the trailing partial frame across reads, then processes any residual buffer after `done` — correct handling for frames split across TCP segments.

---

## 13. Frontend

A four-route SPA (`App.jsx:34-39`) inside a fixed sidebar shell. No global state; each page owns its data.

### 13.1 ChatPage (`ChatPage.jsx`, 344 lines)

The only non-trivial page.

**State shape.** `messages: [{role, content, sources, status, rewrittenQuery}]`. The assistant entry is pushed optimistically with empty content and a `status` string before the request is sent (`ChatPage.jsx:72`); each SSE frame patches the last array element in place.

**Conversation history.** On submit, all prior turns with non-empty content are serialised, **excluding the just-submitted question** via `.slice(0, -1)` (`ChatPage.jsx:75-78`) — the question travels in its own field.

**Source continuity.** Every source id cited by any previous assistant turn is collected, deduplicated through a `Set`, and sent as `previous_source_ids` (`ChatPage.jsx:80-85`). This is the client half of the `CONTINUITY_BOOST` mechanism (§7.5) and is what makes multi-turn conversation about the same emails stable.

**Account filter tri-state** (`ChatPage.jsx:37-56`). `selectedAccounts === null` means "all" — and is what gets sent (i.e. omitted) rather than an explicit list, so newly-ingested accounts are automatically included. Selecting every account individually normalises back to `null` (`ChatPage.jsx:47-49`). An empty array is a distinct state meaning "none".

**Sources pane.** `latestSources` (`ChatPage.jsx:28-35`) scans backwards for the most recent assistant turn with sources, so the pane persists across turns that return nothing.

### 13.2 InboxPage (`InboxPage.jsx`, 271 lines)

Server-side sorting and pagination — `sort_by`/`sort_dir`/`page`/`per_page` are query parameters, not client-side operations, so the 66k-row corpus is never shipped to the browser. Page size is fixed at 50. Row click fetches the full message via `GET /api/messages/{id}`. Navigation offers first/last, ±1, ±10, and direct page entry.

### 13.3 IngestionPage (`IngestionPage.jsx`, 167 lines)

Two trigger buttons and a **2-second polling loop** (`IngestionPage.jsx:42`) that self-terminates when `status !== 'running'` (`IngestionPage.jsx:16-19`), with cleanup on unmount. Displays a progress bar computed from `current_source / total_sources` — source-granular, not message-granular, so it advances unevenly across folders of different sizes. Shows the four counters and the last 10 errors.

### 13.4 SettingsPage (`SettingsPage.jsx`, 171 lines)

Renders provider cards from `GET /api/settings/`, flags providers without a configured key, and posts a provider switch. Also displays corpus statistics from `GET /api/settings/stats`.

### 13.5 Styling

`global.css` — a single stylesheet using CSS custom properties (`--text-secondary`, `--text-muted`, `--success`, `--error`, `--border`, `--font-mono`). Layout-critical rules live in the stylesheet; a handful of one-off spacing and colour values are inline `style` props.

---

## 14. Configuration system

### 14.1 Load sequence

`load_config()` (`config.py:140-167`):

1. Locate `config.yaml` — first hit among `/app/config.yaml`, `./config.yaml`, `<repo root>/config.yaml` (`config.py:146-150`).
2. Parse with `yaml.safe_load`, defaulting to `{}` if absent or empty.
3. Overlay environment variables per the `ENV_OVERRIDES` table, writing into the raw dict at dotted paths via `_set_nested()` (`config.py:127-137`).
4. Construct `AppConfig(**raw)` — Pydantic applies defaults for anything unset and coerces types.

The result is memoised in a module global (`config.py:171-178`). `reload_config()` exists but is not wired to any endpoint. A missing `config.yaml` is not an error — the system runs entirely on Pydantic defaults.

### 14.2 Defaults by source

Three layers can define a value. This table gives all three where they differ:

| Setting | `config.py` default | `config.example.yaml` | Call-site default |
|---|---|---|---|
| `retrieval.similarity_threshold` | `0.3` (`config.py:48`) | `0.08` | `0.05` (`hybrid_search`, `queries.py:288`) |
| `retrieval.top_k` | `15` | `15` | unused in query path (§17) |
| `retrieval.include_thread_context` | `true` | `true` | — |
| `embeddings.chunk_size` | `512` | `512` | `512` (`chunk_text`) |
| `embeddings.batch_size` | `64` | `64` | — |
| chunk overlap | *not configurable* | — | `64` (`chunk_text`, `embedder.py:55`) |
| `llm.active_provider` | `anthropic` | `anthropic` | — |
| `anthropic.model` | `claude-sonnet-4-20250514` | same | — |
| `anthropic.max_tokens` | `4096` | `8192` | — |
| `anthropic.max_context_tokens` | `180000` | `180000` | — |
| `openai.model` | `gpt-4o` | `gpt-4o` | — |
| `openai.max_tokens` | `4096` | `32768` | — |
| `openai.max_context_tokens` | `120000` | `120000` | — |
| `gemini.model` | `gemini-2.5-flash` | same | — |
| `gemini.max_tokens` | `8192` | `32768` | — |
| `gemini.max_context_tokens` | `900000` | `900000` | — |
| `ollama.model` | `llama3.2:3b` | `llama3.2:3b` | — |
| `ollama.max_tokens` | `4096` | `4096` | — |
| `ollama.max_context_tokens` | `8000` | `8000` | — |
| all `temperature` | `0.2` | `0.2` | — |

Since `config.example.yaml` is the file users copy, its values are the effective defaults in practice. `similarity_threshold` is the one setting where all three layers disagree — the YAML value (0.08) wins under normal operation, and the low value is intentional: budget trimming and the relative cutoff are the real constraints, not the absolute threshold.

Hardcoded constants with no configuration path: `VECTOR_WEIGHT`/`KEYWORD_WEIGHT` (0.4/0.6), `CONTINUITY_BOOST` (0.15), `BODY_HIT_WEIGHT` (0.5), the 0.4 relative cutoff, the 0.1 recipients-only demotion, `CHARS_PER_TOKEN` (4), `HEADER_CHARS_PER_MSG` (200), `MAX_BODY_PER_MSG` (8000), the 2,000-char `top_k` divisor, `[10, 500]` clamp, `BATCH_LIMIT` (30000), `OLLAMA_TIMEOUT` (300s), thread expansion's top-10 × 10-sibling limits.

### 14.3 Environment variables — two distinct sets

These are frequently conflated. They are read by different processes.

**Set A — consumed by Docker Compose only** (never reaches `config.py`):

| Variable | Default | Effect |
|---|---|---|
| `MAILLENS_MAIL_DIR` | `./_mail` | Host path bind-mounted to `/mail:ro` |
| `MAILLENS_PORT` | `8000` | Host port published for the web UI |
| `MAILLENS_DB_NAME` / `MAILLENS_DB_USER` | `maillens` | `POSTGRES_*` for the `db` container |

**Set B — `ENV_OVERRIDES` in `config.py:109-124`**, applied to the parsed YAML:

| Variable | Config path |
|---|---|
| `MAILLENS_DB_PASSWORD` | `database.password` |
| `MAILLENS_DB_HOST` | `database.host` |
| `MAILLENS_DB_PORT` | `database.port` |
| `MAILLENS_DB_NAME` | `database.name` |
| `MAILLENS_DB_USER` | `database.user` |
| `MAILLENS_ANTHROPIC_API_KEY` *or* `MAILLENS_LLM_ANTHROPIC_API_KEY` | `llm.anthropic.api_key` |
| `MAILLENS_OPENAI_API_KEY` *or* `MAILLENS_LLM_OPENAI_API_KEY` | `llm.openai.api_key` |
| `MAILLENS_GEMINI_API_KEY` *or* `MAILLENS_LLM_GEMINI_API_KEY` | `llm.gemini.api_key` |
| `MAILLENS_LLM_ACTIVE_PROVIDER` | `llm.active_provider` |
| `MAILLENS_MAIL_DIRECTORY` | `mail.directory` |
| `MAILLENS_MAIL_FORMAT` | `mail.format` |

Note the near-collision: **`MAILLENS_MAIL_DIR`** (Set A, the host path to mount) is a different variable from **`MAILLENS_MAIL_DIRECTORY`** (Set B, the in-container path). `MAILLENS_DB_NAME` and `MAILLENS_DB_USER` appear in both sets and must agree.

Only four of Set B are forwarded into the `app` container by `docker-compose.yml:42-46`: the DB password and the three API keys. The others require editing the compose file.

`_set_nested()` writes raw strings; its comment claims type coercion it does not perform (`config.py:135-137`). Pydantic handles coercion at model construction, so integer-valued env vars still work — the comment is stale, not the behaviour.

### 14.4 Connection strings

`DatabaseConfig` derives two URLs (`config.py:29-35`): `postgresql+asyncpg://…` for the async runtime path, and `postgresql+psycopg://…` for synchronous schema creation, which is why both drivers are in `requirements.txt`. The engine uses `pool_size=5, max_overflow=10` (`db.py:17-22`) and `expire_on_commit=False` (`db.py:29-33`) — the latter necessary because `hybrid_search` reads ORM attributes after the session's work is done.

---

## 15. Deployment and runtime topology

### 15.1 Image build

`docker/Dockerfile` — two stages:

**Stage 1 (`node:20-alpine`)** copies `package.json` and any lockfile first so `npm install` layer-caches independently of source changes, then copies the frontend and runs `vite build` → `/frontend/dist`.

**Stage 2 (`python:3.12-slim`)** installs `libpq-dev`, `gcc`, `curl`; installs Python dependencies (again, requirements copied before source for caching); copies `backend/`; then copies the stage-1 `dist` into `/app/static`, which is exactly where `main.py:54` looks for it. Node and the frontend toolchain are absent from the final image.

### 15.2 Startup sequence

`docker/entrypoint.sh` runs three steps in order, with `set -e` so any failure aborts:

1. `python -m backend.setup_models` — queries `GET /api/tags` on Ollama, pulls `embeddings.model` if absent, and additionally pulls `llm.ollama.model` when `active_provider == "ollama"` (`setup_models.py:37-38`). Pull timeout 600s. **A connection failure here is caught and downgraded to a warning** (`setup_models.py:40-42`) — the app starts without embeddings rather than crash-looping.
2. `python -m backend.storage.init_db` — `CREATE EXTENSION IF NOT EXISTS vector`, then `create_all`.
3. `exec uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1`.

`exec` replaces the shell so uvicorn is PID 1 and receives signals directly.

### 15.3 Volumes

| Mount | Type | Mode | Purpose |
|---|---|---|---|
| `${MAILLENS_MAIL_DIR} → /mail` | bind | **ro** | Thunderbird profile. Read-only guarantees the mail store is never mutated. |
| `./config.yaml → /app/config.yaml` | bind | ro | Config changes need only `docker compose restart app`, not a rebuild. |
| `pgdata → /var/lib/postgresql/data` | named | rw | Database persistence. |
| `ollama_models → /root/.ollama` | named | rw | Model weight cache across restarts. |

### 15.4 GPU

`docker-compose.gpu.yml` is an override adding an NVIDIA device reservation (`driver: nvidia, count: all, capabilities: [gpu]`) to the `ollama` service only. It requires the NVIDIA Container Toolkit on the host and is applied by passing both compose files.

### 15.5 Ports

| Service | Container | Host | Note |
|---|---|---|---|
| `app` | 8000 | `${MAILLENS_PORT:-8000}` | Web UI + API |
| `db` | 5432 | — | Not published; reachable only on the compose network as `db:5432` |
| `ollama` | 11434 | 11434 | Published unconditionally |

`app` sets `restart: unless-stopped`; `db` and `ollama` do not.

### 15.6 Security posture

The design assumes a single-user machine on a trusted network:

- **No authentication or authorisation** anywhere in the API. Any client that can reach port 8000 can query the entire archive.
- **CORS defaults to `allow_origins=["*"]` with `allow_credentials=True`** (`main.py:38-45`), gated on `server.cors_allow_all`, which defaults to `true` (`config.py:95`).
- `db` is not published to the host. `ollama` publishes 11434 to the host without authentication.
- API keys never appear in an API response — `/api/settings/` reports only `has_key: bool`.
- Mail is mounted read-only.
- Embeddings are always local; message bodies leave the machine only via the chosen cloud LLM provider.

---

## 16. Documentation drift

Points where `README.md` describes behaviour the code no longer has. The code is authoritative.

| # | README claim | Actual behaviour |
|---|---|---|
| 1 | Keywords are extracted by "tokenizing with `[a-zA-Z0-9]+` … and filtering against a ~90-word stop list" (README:325) | The active LLM extracts keywords via `_KEYWORD_EXTRACT_PROMPT`; regex+stoplist is the *fallback*. The stop list holds 191 unique entries. Substring elimination is also applied. |
| 2 | *(unmentioned)* | Follow-up queries are rewritten by an LLM call before retrieval (`rewrite_follow_up_query`). A conversational turn costs two extra pre-retrieval LLM round-trips. |
| 3 | Keyword condition is `or_(sender.ilike, subject.ilike, recipients_to.ilike)` (README:325) | Recipients only participate when **all** keywords appear in `recipients_to`. A further ×0.1 demotion applies when no keyword hits sender or subject. |
| 4 | *(unmentioned)* | `previous_source_ids` grants `+0.15` (`CONTINUITY_BOOST`) and exempts prior sources from the keyword gate. |
| 5 | "four event types" (README:379) | Six: `status`, `rewritten_query`, `sources`, `meta`, `text`, `done`. |
| 6 | Ollama default context budget 32,000 (README:215) | `8000` in both `config.py:80` and `config.example.yaml`. |
| 7 | Env var table lists `MAILLENS_MAIL_DIR` as overriding the mail directory (README:164) | `MAILLENS_MAIL_DIR` is compose-only (bind-mount source). The config override is `MAILLENS_MAIL_DIRECTORY`. Likewise `MAILLENS_PORT` is compose-only. |
| 8 | `max_tokens` values | README's provider table omits that `config.py` and `config.example.yaml` disagree for Anthropic (4096/8192), OpenAI (4096/32768), and Gemini (8192/32768). |
| 9 | "Results are blended … filtered by a minimum score threshold" (README:252) | Understates the keyword gate: with keywords present, candidates with zero keyword score are dropped *before* the threshold, regardless of vector similarity. |
| 10 | Thread expansion described as appending context (README:357) | Correct, but expansion runs after `top_k` truncation and its results compete in budget trimming, so siblings can displace genuine tail-ranked hits. Thread-expanded sources also carry `similarity: null`. |

---

## 17. Known gaps, dead code, and limitations

### 17.1 Retrieval

**Body text is not keyword-searchable in SQL.** The `ILIKE` path covers `sender`, `subject`, and (conditionally) `recipients_to` only. The keyword gate in the merge loop drops any candidate with zero keyword score when keywords exist, so a body-only match was previously excluded outright regardless of vector similarity. That is now mitigated in Python: before the gate, a candidate with `kscore == 0` is re-scored with `_keyword_hit_ratio(..., include_body=True)`, and a whole-word body hit yields `kscore = ratio * BODY_HIT_WEIGHT` (0.5).

Two limits remain. The rescue only sees candidates the **vector path already returned** (`LIMIT top_k * 3`), so a body keyword in a message with mediocre similarity is still unreachable. And body matching is whole-word (`_contains_word`) while the header `ILIKE` path is substring, so the two paths do not have identical semantics — deliberate, since substring matching over kilobytes of prose makes short keywords near-universal. A `tsvector` GIN index on `body_clean` would remove the first limit by giving the body its own SQL path, with whole-word semantics matching what the Python rescue already does.

**HNSW indexes are not exercised by the vector path.** The chunk subquery is an unbounded `GROUP BY MAX(...)` aggregate, a shape pgvector's HNSW index cannot serve (it accelerates `ORDER BY <distance> LIMIT n`). The index definitions are correct; the query shape prevents their use, so vector search scans `message_chunks` in full.

**The keyword path cannot use an index either.** Unanchored `ILIKE '%term%'` is not btree-servable. A `pg_trgm` GIN index on `sender` and `subject` would be the direct fix.

**Chunk-level results are discarded.** The subquery computes which chunk matched best but returns only the score. The LLM receives the truncated *message* body, which may not include the matching chunk — the first N characters are kept, not the relevant span.

### 17.2 Ingestion

**`incremental` is inert.** Accepted by the API, threaded to `run_ingestion()`, documented in its docstring, never read. Deduplication is unconditional (`pipeline.py:195-201`), so "Full Re-ingest" cannot re-ingest anything. Re-ingesting requires manually clearing the tables or the `pgdata` volume.

**Threading does not span runs.** `build_threads()` sees only messages from the current run, so a reply ingested after its parent starts a new thread rather than joining the existing one. Thread counters are likewise never recomputed.

**Batch failures discard siblings.** A single failing message clears the whole accumulating batch (up to 63 already-parsed messages), which are neither stored nor retried in that run.

**Embedding failures fail silently.** A network blip or Ollama restart yields `[0.0]*768` → stored as `NULL` → that chunk is invisible to vector search permanently, with no record beyond a stdout warning. Nothing detects or retries null-embedded chunks.

**Deduplication is one query per message.** 66k point lookups on a no-op incremental run. A single `SELECT message_id FROM messages` into a set would collapse this to one query.

**Progress is process-local and non-durable.** Restarting during a run loses all progress state; `ingestion_runs` is never written, so there is no history.

**Ingestion shares the API event loop.** No isolation between a long ingest and concurrent queries.

### 17.3 Unused code

| Item | Location | Status |
|---|---|---|
| `IngestionRun` model | `models.py:138-150` | Table created, never instantiated. Imported in `pipeline.py:25` and unused. |
| `Attachment.extracted_text` | `models.py:133` | Populated at ingest; never embedded, never queried. Attachment content is unsearchable. |
| `mail.format` config | `config.py:19`, `ENV_OVERRIDES` | Defined and env-mappable; no reader exists. `discover_mail_sources()` always auto-detects. |
| `retrieval.top_k` config | `config.py:47` | Only surfaced by `/api/settings/` and displayed in the UI. The query path uses `request.top_k or _estimate_top_k(...)` — the configured value never affects retrieval. |
| `ThreadNode` dataclass | `threading.py:10-15` | Never instantiated. |
| `reload_config()` | `config.py:181-184` | Not wired to any endpoint. |
| `EmbeddingsConfig.provider` | `config.py:39` | Only `ollama` is implemented; the field is never branched on. |

### 17.4 Correctness and consistency

**Version strings disagree.** `main.py:23` prints `"MailLens build 0.14"`; `FastAPI(version="0.1.0")` at `main.py:32`. Neither is derived from a single source.

**Provider switching does not persist.** `POST /api/settings/provider` mutates the in-memory singleton only. `config.yaml` is mounted read-only, so the change is lost on restart.

**Not-found returns HTTP 200.** `GET /api/messages/{id}` on a miss returns `{"error": …}` with a 200 status (`messages.py:108-109`), as does an invalid provider on `POST /api/settings/provider`.

**Double body truncation.** `_trim_to_budget` (`query.py`) and `_format_context` (`base.py`) both cap bodies with independently-derived per-message budgets using different header-overhead constants (200 vs 180). The two must be kept in sync manually.

**Token estimation is 4 chars/token throughout.** Reasonable for English prose, poor for base64 fragments, long URLs, quoted headers, and CJK — all common in email. The estimate has no provider-specific calibration and no tokenizer.

**Stale comment.** `_set_nested()` (`config.py:135-137`) comments an int/float coercion it does not perform. Pydantic does it downstream, so behaviour is correct.

**Unused imports.** `asyncio` in `pipeline.py:6` and `ingest.py:5`; `Float` in `models.py:12`.

**`get_session` annotation.** Declared `-> AsyncSession` (`db.py:37`) but is an async generator. FastAPI handles it correctly as a dependency; the annotation is wrong.

### 17.5 Scale and operations

- **Unit tests cover retrieval ranking only.** `tests/` holds pytest unit tests for keyword extraction, `_contains_word`, `_keyword_hit_ratio`, and the Python-side merge, body rescue, gating, continuity boost, thresholds, and cutoff in `hybrid_search`, driven by a fake session that returns canned rows. Nothing runs against Postgres, so the SQL itself (ILIKE matching, the pgvector query, metadata filters, LIMITs) is untested, as are ingestion, the API, the providers, and the frontend. No CI. Run with `pytest` from the repo root after `pip install -r backend/requirements-dev.txt`.
- **No schema migrations.** Any model change requires dropping `pgdata`.
- **Single worker by necessity** (§2.2) — the in-process globals prevent horizontal scaling.
- **No structured logging.** Diagnostics are `print()` to stdout, visible only via `docker compose logs`. No levels, no correlation ids, no metrics.
- **No rate limiting or timeouts on the API.** A large `top_k` on a big archive can occupy the single worker for a long time.
- **No auth** (§15.6).

### 17.6 Validated scale

Developed and tested against a Thunderbird profile with 4 accounts and ~66,000 messages spanning 10+ years (README:437). Both retrieval paths are sequential scans at that size, which is tractable; the design has not been validated an order of magnitude higher.
