# gglads

Autonomous Google Ads manager. Knows your Shopify catalog, watches search terms 24/7, proposes (then executes) changes within configurable guardrails, and learns from your feedback.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Render                                                       │
│                                                               │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐ │
│  │ Web (FastAPI)│   │ Worker (loop)│   │ Cron (daily sweep)│ │
│  └──────┬───────┘   └──────┬───────┘   └────────┬─────────┘ │
│         │                  │                     │            │
│         └──────────────────┼─────────────────────┘            │
│                            │                                  │
└────────────────────────────┼──────────────────────────────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
       ┌──────▼─────┐ ┌──────▼─────┐ ┌─────▼──────┐
       │   Neon     │ │  Shopify   │ │ Google Ads │
       │ (Postgres) │ │   Admin    │ │    API     │
       └────────────┘ └────────────┘ └────────────┘
                             │
                      ┌──────▼─────┐
                      │ Anthropic  │
                      │   (Claude) │
                      └────────────┘
```

- **Web** serves the portal: training, approval queue, dashboards, settings.
- **Worker** runs the 10-minute loop: pull deltas, ask Claude about interesting ones, queue or execute actions.
- **Cron** runs heavier daily sweeps (campaign restructuring, opportunity mining).

## Phases

1. **Read-only sync.** Mirror Shopify catalog + Google Ads structure + search terms into Neon. No writes.
2. **Recommendations w/ approval gate.** Claude proposes actions; you approve in the portal.
3. **Policy-gated autonomy.** You set guardrails (max budget, target CPA, allowed action types). Inside the rails: auto-execute. Outside: queue for approval.
4. **MCP server (optional).** Chat with the system from Claude.app / Claude Code.

## Stack

- Python 3.11+, FastAPI, SQLAlchemy 2.0, Alembic
- Neon (Postgres) for state
- Render for hosting (web + worker + cron)
- `google-ads` (official), `anthropic`, Shopify GraphQL Admin API

## Local setup

```bash
# 1. Install uv (fast Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Sync dependencies
uv sync

# 3. Copy env template and fill in
cp .env.example .env

# 4. Run migrations
uv run alembic upgrade head

# 5. Start the web app
uv run uvicorn gglads.web.app:app --reload

# 6. In another terminal, start the worker
uv run python -m gglads.worker.loop
```

## Required environment variables

See `.env.example` for the full list. You'll need:

- `DATABASE_URL` — Neon Postgres connection string
- `ANTHROPIC_API_KEY` — Anthropic API key
- `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_API_TOKEN` — Shopify Admin API
- `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_CUSTOMER_ID` — Google Ads API

## Project layout

```
gglads/
├── web/             # FastAPI app: portal, approval queue, training UI
├── worker/          # Background loop (10-min cadence)
├── cron/            # Scheduled heavy sweeps
├── services/
│   ├── shopify/     # Catalog sync
│   ├── google_ads/  # Ads API client (read + write)
│   ├── claude/      # Anthropic client, prompt assembly, caching
│   └── policy/      # Guardrail engine
├── models/          # SQLAlchemy models
├── db/              # Session, migrations
└── config.py        # Pydantic settings

alembic/             # DB migrations
tests/
render.yaml          # Render deployment config
```
