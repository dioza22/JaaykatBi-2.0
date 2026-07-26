# JaaykatBi 2.0

WhatsApp-only AI commerce assistant for a Dakar grocery/boutique ("Alimentation")
merchant — v1 MVP. No dashboard, no payment gateway integration: everything
(catalog Q&A, order-taking, merchant admin) happens over WhatsApp.

See `docs/` for the Charte Conversationnelle this bot's tone is built from,
and the kickoff brief for the v1/v2 scope split. This is a from-scratch Python
rebuild of the earlier C#/.NET JaaykatBi, deliberately pared back to the
leanest version that tests the "chat-only commerce + chat-only merchant
admin" hypothesis.

## Architecture

- **FastAPI** app (`app/`), **SQLAlchemy 2.0 (async) + Alembic** against **Postgres**.
- **WhatsApp Cloud API** (Meta, direct) for inbound/outbound messages — `app/services/whatsapp/`.
- **Deterministic conversation FSM** for order-taking and merchant admin — `app/services/conversation/`.
  `Conversation.state` (a JSONB column) tracks which flow/step/slots are active, so
  multi-step things like "take an order" or "add a product" don't depend on an LLM
  remembering where it left off.
- **Gemini 2.5 Flash-Lite** (free tier) is only consulted for open-ended catalog
  Q&A / general chat fallback — `app/services/ai/`. Swappable behind
  `LLMClient` if a paid model is needed later.

```
app/
  models/            SQLAlchemy entities (Business, Product, Contact, Conversation,
                      Message, Order/OrderItem, FAQ, Promotion)
  api/routes/         webhook.py (Meta webhook), health.py
  services/
    whatsapp/         inbound parsing, outbound client, message_handler orchestration
    conversation/      engine.py (router), intents.py, state.py (FSM),
                       customer_flows.py, merchant_flows.py
    ai/                llm_client.py (Gemini), prompts.py
    orders/            order creation/confirm/fulfill/cancel, promotion pricing
    faq/               keyword FAQ matching
  seed.py             seeds one demo business (Boutique Teranga) for local dev
alembic/              migrations
tests/                pytest suite (43 tests: webhook parsing, intents, FAQ,
                      promotions, full order-flow + merchant-flow simulations)
docker/               docker-compose.yml (Postgres + API), Dockerfile
```

## Local setup

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt      # venv/bin/pip on macOS/Linux
cp .env.example .env                              # then fill in the values below
docker compose -f docker/docker-compose.yml up -d db
venv/Scripts/alembic upgrade head                 # venv/bin/alembic on macOS/Linux
venv/Scripts/python -m app.seed                    # seeds Boutique Teranga demo data
venv/Scripts/uvicorn app.main:app --reload
```

Health check: `GET http://localhost:8000/api/health`.

### Environment variables (`.env`)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string (async, `postgresql+asyncpg://...`) |
| `WHATSAPP_PHONE_NUMBER_ID` | From Meta App Dashboard → WhatsApp → API Setup |
| `WHATSAPP_ACCESS_TOKEN` | Same page — use a permanent token for anything beyond quick testing |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | A string you choose; must match what you enter in the Meta webhook config |
| `GEMINI_API_KEY` | From [Google AI Studio](https://aistudio.google.com/apikey) (free tier) |
| `GEMINI_MODEL` | Defaults to `gemini-2.5-flash-lite` |

### Connecting a real WhatsApp number

1. In the Meta App Dashboard, set the webhook callback URL to
   `https://<your-public-url>/api/webhook/whatsapp` and the verify token to
   whatever you set as `WHATSAPP_WEBHOOK_VERIFY_TOKEN`. For local development,
   expose your machine with a tunnel (ngrok or similar) first.
2. Subscribe the webhook to the `messages` field.
3. In `app/seed.py` (or directly in the `businesses` table), set `whatsapp_number`
   to the Cloud API test/production number, and `owner_whatsapp_number` to the
   merchant's own personal WhatsApp number — that's how the bot tells a
   customer message apart from a merchant-admin command (the bot's own number
   can't message itself on WhatsApp).

## Merchant admin (WhatsApp commands, no dashboard)

Sent from the number configured as `owner_whatsapp_number`:

- `ajouter un produit` / `modifier un produit` / `supprimer un produit` — guided,
  multi-step (name → price → stock, etc.)
- `lancer une promotion` — guided (product → discount % → duration)
- `mes ventes` — today's + all-time order count and revenue
- `mes commandes` — lists pending/confirmed orders; then reply
  `<référence> confirmer` or `<référence> livrer` as a one-line command
- `messages en attente` — lists customer conversations flagged for manual
  follow-up (the brief's "manual fallback path" — the bot hands off rather
  than guessing after a few failed exchanges)
- `annuler` at any point aborts whatever multi-step flow is in progress

## Tests

```bash
docker compose -f docker/docker-compose.yml up -d db   # tests run against real Postgres —
venv/Scripts/alembic upgrade head                       # models use Postgres-specific UUID/JSONB
venv/Scripts/python -m pytest
```

Each test runs inside its own connection + outer transaction that's rolled
back afterward (see `tests/conftest.py`), so the suite never touches the
seeded dev data.

## Deployment (Oracle Cloud Free Tier ARM VM)

Postgres was chosen over the old build's SQL Server specifically because it
has an official ARM64 Linux build — SQL Server doesn't, so it can't run on
Oracle's Ampere (ARM) free-tier shape. To deploy:

1. Provision the Ampere A1 free-tier VM (Ubuntu), install Docker + Docker Compose.
2. Copy the repo (or just `docker/`, `app/`, `alembic/`, `alembic.ini`,
   `requirements.txt`) to the VM, with a production `.env` (real WhatsApp
   token, Gemini key, a non-default `DATABASE_URL` password).
3. `docker compose -f docker/docker-compose.yml up -d --build` — the API
   container runs `alembic upgrade head` on startup before starting uvicorn.
4. Put the VM behind a reverse proxy with TLS (Meta requires HTTPS for the
   webhook URL) — Caddy or nginx + Let's Encrypt both work fine on the free tier.
5. Point the Meta webhook at `https://<your-domain>/api/webhook/whatsapp`.

No Hangfire/cron-equivalent is needed for v1 — there are no scheduled jobs,
matching the brief's "nothing more than the MVP" scope.
