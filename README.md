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

## License

MIT
