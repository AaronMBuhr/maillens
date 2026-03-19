# MailLens

Self-hosted email intelligence powered by LLMs. Ingest your Thunderbird email archive, search it with natural language, and get AI-powered answers with full citations.

## Features

- **Natural language email search** -- Ask questions like "find all emails from Jane about the Q3 budget" or "summarize my conversations with the recruiter last month"
- **Hybrid retrieval** -- Combines vector similarity search (pgvector) with keyword matching (ILIKE) across sender, subject, and recipients, then merges and re-ranks results
- **Context-budget aware** -- Dynamically estimates how many emails fit in the active LLM's context window, caps per-message body length to distribute space evenly, and applies a relative score cutoff so noise doesn't crowd out relevant results
- **Multi-account support** -- Discovers all email accounts in your Thunderbird profile and exposes them as filterable checkboxes in the UI
- **Thread-aware** -- Reconstructs conversation threads and includes thread context for top results
- **Conversation memory** -- Each query is part of a persistent chat session; previous context carries forward until you start a new conversation
- **Four LLM backends** -- Anthropic Claude, OpenAI GPT, Google Gemini, or local models via Ollama
- **Local embeddings** -- All embeddings generated locally via Ollama (nomic-embed-text) -- your email content never leaves your machine unless you choose a cloud LLM
- **Multi-chunk embeddings** -- Long messages are split into chunks; each chunk is embedded individually and the best chunk match is used for retrieval
- **Web UI** -- Chat interface with streaming responses, inbox browser with sorting and pagination, ingestion controls, and settings dashboard

## Screenshots

The **Query** page lets you chat with your email archive:

- Type natural language questions
- See status updates as the system searches, retrieves, and sends context to the LLM
- Source citations appear as cards below each response
- Filter by sender, date range, folder, or email account
- Conversation history persists across queries

The **Inbox** page provides a traditional email browser:

- Sortable columns (date, sender, subject) with ascending/descending toggle
- Full pagination with page jumps and fast-forward/reverse (10 pages at a time)
- Search and filter by sender, subject, folder, or account

## Quick Start

### Prerequisites

- Docker and Docker Compose
- A Thunderbird mail profile directory
- At least one LLM API key (Anthropic, OpenAI, or Google Gemini) -- or use Ollama for a fully local setup

### Setup

1. Clone the repo:

```bash
git clone https://github.com/youruser/maillens.git
cd maillens
```

2. Copy the config templates:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

3. Edit `.env` with your settings:

```bash
# Point to your Thunderbird profile's mail directory
MAILLENS_MAIL_DIR=/home/youruser/.thunderbird/xxxxxxxx.default-release

# Add at least one LLM API key
MAILLENS_GEMINI_API_KEY=AIza...
# and/or
MAILLENS_ANTHROPIC_API_KEY=sk-ant-...
# and/or
MAILLENS_OPENAI_API_KEY=sk-...
```

4. Optionally edit `config.yaml` to choose your LLM provider, adjust models, or tune retrieval settings.

5. Start the stack:

```bash
docker compose up --build
```

First startup takes a few minutes while it pulls the embedding model.

6. Open **http://localhost:8000** in your browser.

7. Go to the **Ingestion** tab and click **Incremental Ingest** to index your mail.

8. Once ingestion completes, go to the **Query** tab and start asking questions.

### Finding Your Thunderbird Profile Directory

| OS | Typical path |
|---|---|
| Linux | `~/.thunderbird/<profile>/` |
| macOS | `~/Library/Thunderbird/Profiles/<profile>/` |
| Windows | `%APPDATA%\Thunderbird\Profiles\<profile>\` |

To find your profile name, check Thunderbird's **Help > Troubleshooting Information > Profile Directory**, or look in `~/.thunderbird/profiles.ini`.

Point `MAILLENS_MAIL_DIR` to the profile directory itself -- MailLens will discover all `Mail/` and `ImapMail/` subdirectories automatically and derive account names from the folder structure.

> **Windows + WSL note**: If Docker runs in WSL, use the WSL mount path (e.g., `/mnt/c/Users/you/AppData/Roaming/Thunderbird/Profiles/...`).

## Configuration

### config.yaml

The main configuration file. See `config.example.yaml` for all options with inline documentation.

| Section | Key settings |
|---|---|
| `mail` | Directory path, format (auto / mbox / maildir) |
| `embeddings` | Model (`nomic-embed-text`), chunk size, batch size |
| `retrieval` | Similarity threshold, thread context toggle |
| `llm` | Active provider, per-provider model, `max_tokens` (output), `max_context_tokens` (input budget), temperature |
| `server` | Host, port, CORS |

### Environment Variables

Sensitive values should be set as env vars rather than in the YAML file:

| Variable | Overrides |
|---|---|
| `MAILLENS_MAIL_DIR` | Mail directory mount path |
| `MAILLENS_GEMINI_API_KEY` | `llm.gemini.api_key` |
| `MAILLENS_ANTHROPIC_API_KEY` | `llm.anthropic.api_key` |
| `MAILLENS_OPENAI_API_KEY` | `llm.openai.api_key` |
| `MAILLENS_DB_PASSWORD` | `database.password` |
| `MAILLENS_PORT` | Host port for the web UI (default 8000) |

### Switching LLM Providers

Set `llm.active_provider` in `config.yaml` to one of: `anthropic`, `openai`, `gemini`, or `ollama`. Since `config.yaml` is volume-mounted, changes take effect after `docker compose restart app` -- no rebuild needed.

To use a fully local setup with Ollama, set the provider to `ollama` and specify the model name. The model must be pulled into the Ollama container first (`docker compose exec ollama ollama pull llama3`).

### Context Budget

Each LLM provider has a `max_context_tokens` setting that controls how many tokens of email content are sent per query. The system:

1. Estimates how many messages can fit in the budget
2. Retrieves candidates via hybrid search (vector + keyword)
3. Caps each message body to distribute the budget evenly
4. Applies a relative score cutoff to filter noise
5. Fills the context greedily in rank order until the budget is exhausted

Default context budgets reflect each provider's capabilities:

| Provider | Default context budget |
|---|---|
| Gemini 2.5 Flash | 900,000 tokens |
| Anthropic Claude | 180,000 tokens |
| OpenAI GPT | 120,000 tokens |
| Ollama (local) | 6,000 tokens |

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│  React SPA  │────▶│  FastAPI     │────▶│  PostgreSQL    │
│  (Vite)     │     │  Backend     │     │  + pgvector    │
└─────────────┘     └──────┬───────┘     └────────────────┘
                           │
                    ┌──────┴───────┐
                    │    Ollama    │
                    │ (embeddings)│
                    └──────┬──────┘
                           │
              ┌────────────┼─────────────┐────────────┐
              ▼            ▼             ▼            ▼
         Claude API   OpenAI API   Gemini API   Ollama LLM
```

### Ingestion Pipeline

1. **Discovery** -- Scans the mounted Thunderbird profile for mbox and Maildir sources, deriving email account names from directory structure
2. **Parsing** -- Reads each message, decodes MIME parts, extracts headers, handles charset edge cases and null bytes
3. **Cleaning** -- Strips quoted reply chains, HTML tags, and signatures via `email-reply-parser`
4. **Chunking** -- Splits long messages into overlapping chunks (default 512 tokens)
5. **Embedding** -- Generates embeddings via Ollama in batches; each chunk gets its own embedding, and the message-level embedding is the average
6. **Storage** -- Inserts messages and chunks into PostgreSQL with pgvector, with deduplication and resilient batch error handling
7. **Threading** -- Reconstructs conversation threads using `In-Reply-To` and `References` headers

### Hybrid Search

Retrieval uses two parallel paths that are merged and re-ranked:

- **Vector path** -- Cosine similarity search on chunk-level embeddings via pgvector, deduplicated to the parent message
- **Keyword path** -- ILIKE matching on sender, subject, and recipients for each extracted query keyword; results are ranked by how many keywords each message matches

Results are blended with configurable weights (40% vector, 60% keyword by default), filtered by a minimum score threshold, and then trimmed by a relative score cutoff (results scoring below 40% of the best match are dropped).

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 18, Vite, Lucide icons |
| Backend | Python 3.12, FastAPI, SQLAlchemy (async), Pydantic |
| Database | PostgreSQL 16 with pgvector |
| Embeddings | Ollama + nomic-embed-text |
| LLM providers | Anthropic, OpenAI, Google Gemini, Ollama |
| Deployment | Docker Compose (multi-stage build) |

## Technical Details

### Database Schema

All models use PostgreSQL with the pgvector extension. Embeddings are 768-dimensional vectors (matching nomic-embed-text output).

**Message** -- one row per email:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | Auto-increment |
| `message_id` | String | Unique, indexed. From the `Message-ID` header, or a SHA-256 hash of `(subject, date, sender, source_file, raw_snippet)` for messages without one |
| `in_reply_to` | String | Indexed, used for thread reconstruction |
| `references` | Text | Space-separated reference chain |
| `thread_id` | Integer FK | Links to `threads.id` |
| `subject`, `sender`, `recipients_to`, `recipients_cc` | Text/String | `sender` and `folder` are indexed |
| `date` | DateTime(tz) | Timezone-aware UTC; descending index for sort performance |
| `account` | String | Derived from Thunderbird directory structure (e.g., `imap.gmail.com`) |
| `body_text`, `body_html`, `body_clean` | Text | Raw text, raw HTML, and cleaned (reply-stripped) versions |
| `embedding` | Vector(768) | HNSW index with `m=16`, `ef_construction=64`, cosine ops |
| `has_attachments` | Boolean | |
| `ingested_at` | DateTime(tz) | Server-side default |

**MessageChunk** -- one row per chunk of a long message:

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `message_id` | Integer FK | Cascading delete; part of unique index with `chunk_index` |
| `chunk_index` | Integer | Ordering within the parent message |
| `chunk_text` | Text | The chunk content |
| `embedding` | Vector(768) | HNSW index, same params as Message |

**Thread** -- groups related messages:

| Column | Type |
|---|---|
| `id` | Integer PK |
| `subject` | Text |
| `first_date`, `last_date` | DateTime |
| `message_count` | Integer |

### Ingestion Pipeline Details

**Source discovery** (`discover_mail_sources`): Recursively scans the mounted profile directory. Files without extensions (and not `.msf` summary files) are treated as mbox. Directories containing `cur/` or `new/` subdirectories are treated as Maildir. Account names are derived from the Thunderbird folder hierarchy -- `ImapMail/<server>/...` yields the server name, `Mail/<account>/...` yields the account name.

**Parsing robustness**: A `_sanitize` helper strips null bytes (`\x00`) that cause PostgreSQL encoding errors. A `_safe_charset` helper falls back to UTF-8 for unrecognized charset headers. Dates are always normalized to timezone-aware UTC.

**Deduplication**: A `seen_in_run` set tracks message IDs within a single ingestion run. Combined with a database existence check, this prevents duplicates when the same email appears in multiple Thunderbird folders. On flush errors, the session is rolled back, the batch is cleared, and processing continues.

**Thread building**: After all messages are ingested, threads are reconstructed by following `In-Reply-To` and `References` headers. The `message_id IN (...)` query is batched into groups of 30,000 to stay within asyncpg's 32,767 parameter limit.

**Chunking**: Long messages are split with `chunk_text(text, chunk_size=512, overlap=64)`. Token counts are approximated at 0.75 words per token. Overlap ensures context isn't lost at chunk boundaries. Each chunk is embedded independently; the message-level embedding is the element-wise mean of all non-zero chunk embeddings.

### Hybrid Search Details

The search runs two independent SQL queries and merges results:

**Vector path** -- A subquery finds the best chunk-level cosine similarity per message (`func.max(1 - cosine_distance)`), falling back to the message-level embedding via `coalesce`. Fetches `top_k * 3` candidates ordered by similarity.

**Keyword path** -- Extracts keywords from the query by tokenizing with `[a-zA-Z0-9]+`, lowercasing, and filtering against a ~90-word stop list (pronouns, articles, common query verbs like "find"/"show"/"summarize", and email-related terms). For each keyword, builds an `or_(sender.ilike, subject.ilike, recipients_to.ilike)` condition. A SQL `case` expression counts how many keywords each message matches:

```sql
-- Pseudocode for the generated SQL
SELECT messages.*, (
    CASE WHEN (sender ILIKE '%kw1%' OR subject ILIKE '%kw1%' OR ...) THEN 1 ELSE 0 END +
    CASE WHEN (sender ILIKE '%kw2%' OR subject ILIKE '%kw2%' OR ...) THEN 1 ELSE 0 END
) AS kw_hits
FROM messages
WHERE (any keyword matches)
ORDER BY kw_hits DESC, date DESC
LIMIT top_k * 5
```

**Merge and rank** -- Candidates from both paths are combined into a single dict keyed by message ID. Each gets a blended score:

```
combined = 0.4 * vector_similarity + 0.6 * keyword_hit_ratio
```

where `keyword_hit_ratio = matched_keywords / total_keywords`. Results below the absolute threshold (default 0.08) are dropped. Then a **relative cutoff** removes results scoring below 40% of the best match -- this prevents low-relevance results from filling the context when only a few emails are truly relevant.

### Context Budget Pipeline

The query endpoint manages context size through a multi-stage pipeline:

1. **Budget calculation** (`_compute_context_budget`): `available_tokens = max_context_tokens - max_output_tokens - system_prompt_tokens - question_tokens - history_tokens`. Tokens are estimated at ~4 characters each. The result is converted to a character budget.

2. **Adaptive top_k** (`_estimate_top_k`): `k = char_budget / 2000` (estimated average chars per truncated message), clamped to `[10, 500]`.

3. **Hybrid search**: Fetches up to `top_k` ranked results.

4. **Thread expansion**: For the top 10 results that have a `thread_id`, sibling messages are fetched and appended (without displacing existing results).

5. **Budget trimming** (`_trim_to_budget`): Computes a per-message body cap: `min(8000, (budget - n * 200) / n)` with a floor of 500 chars. Then greedily fills the budget in rank order, truncating each body to the cap. This ensures that a few long email threads don't monopolize the budget -- with Gemini's 900K context, this typically fits 400-500 messages at ~7K chars each.

### LLM Provider Interface

All providers extend `LLMProvider` and implement `complete()` and `stream()`. The base class provides:

- `_format_context(messages, max_context_chars)` -- Formats emails as numbered `[Email N]` blocks with headers (From, To, Date, Subject, Folder) and body text, distributing the character budget across messages. This is what the LLM sees.
- `_context_char_budget(system_prompt, user_message, history)` -- Estimates remaining char budget after accounting for non-context tokens.

Each provider builds its message array differently:

| Provider | System prompt | History format | Context location |
|---|---|---|---|
| **Anthropic** | `system=` parameter | Alternating user/assistant messages | Prepended to the final user message |
| **OpenAI** | `{"role": "system"}` message | Interleaved in messages array | Prepended to the final user message |
| **Gemini** | `system_instruction` in config | `types.Content(role="user"\|"model")` | Prepended to the final user Content |
| **Ollama** | `{"role": "system"}` message | Interleaved in messages array | Prepended to the final user message |

### Streaming Response Protocol

The query endpoint uses Server-Sent Events (SSE) with four event types:

| Event | Payload | When |
|---|---|---|
| `sources` | `{type: "sources", sources: [...]}` | After retrieval, before LLM call. Each source includes sender, date, subject, account, similarity score, and a 200-char snippet. |
| `meta` | `{type: "meta", embed_time_ms, retrieval_time_ms, context_messages, context_budget_tokens}` | After retrieval. Reports timing and how many emails were sent to the LLM. |
| `text` | `{type: "text", content: "..."}` | As LLM tokens stream in. Each chunk is a partial text fragment. |
| `done` | `{type: "done"}` | Stream complete. |

The frontend parses these via `ReadableStream` and updates the UI progressively -- source cards and a status message ("Searching emails...", "Sending N emails to LLM...") appear before the LLM's response starts streaming in.

### Frontend State Management

**ChatPage**: Maintains a `messages` array of `{role, content, sources, status}` objects. On submit, the full conversation history (minus the current question) is serialized as `conversation_history` in the API request, enabling multi-turn conversations. A "New conversation" button clears the array.

**InboxPage**: Server-side sorting (`sort_by`, `sort_dir` query params) and pagination (`page`, `per_page`). The backend applies `ORDER BY` with a secondary sort on date. The frontend provides page input, first/last page, and +/-10 page jump buttons.

## Development

### Backend

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload --port 8000
```

Requires PostgreSQL with pgvector and Ollama running separately.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api` requests to `localhost:8000`.

### Rebuilding

After code changes:

```bash
docker compose build --no-cache app && docker compose up
```

Config-only changes (`config.yaml`) are volume-mounted and take effect with just:

```bash
docker compose restart app
```

## Tested With

Developed and tested against a Thunderbird profile with 4 email accounts, ~66,000 messages spanning 10+ years.

## License

MIT
