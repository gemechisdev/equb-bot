# Digital Equb (Quqube)

A **Telegram-first digital Equb (እቁብ)** — rotating savings inside a group chat — run as a **provably fair lottery**.

Members contribute the same amount each round. At the scheduled draw time, the pool goes to **one** eligible member drawn at random among those whose payment was **admin-verified before the draw**. Nobody (including admins) can know the winner in advance. After you receive the pool you keep paying; you only leave the draw pool. The cycle ends when every active member has received once (or **auto-restarts** if the Equb was created with `auto`).

Payments are **manual and offline** (bank transfer, Telebirr, cash, etc.). Members submit **proof** in a private chat with the bot; **admins approve or reject**. The bot does not hold or move funds.

## Features

| Area | What it does |
|------|----------------|
| Lifecycle | `/newequb` → join → `/startcycle` → rounds → complete or cancel |
| Fair draws | SHA-256 commit (`seed_hash`) when a round opens; seed revealed at draw; `/verify` re-runs the math |
| Contributions | Proof photo/text → `awaiting_review` → admin Approve/Reject; resubmit after reject |
| Payouts | Draw creates a `pending` payout record; admin `/payout` (with confirm) marks paid and advances |
| Transparency | `/status`, `/statement`, `/statement csv`, `/history`, `/auditlog`, `/verify` |
| Corrections | `/unverify` (before draw), `/reconcile` (stuck after paid), dual confirm for payout/cancel |
| i18n | English, Amharic, Afaan Oromoo (`/language`) |
| Ops | Indexes on startup, atomic claims for draws/reviews/payouts, serverless cron `/run-draws` |

**Telegram command menu** is registered on startup (member commands + `[admin]` commands). Full guide: **`/help`**.

## Stack

- **Python 3.11+**, aiogram 3.x, Motor (MongoDB), aiohttp, PyYAML, tzdata  
- Long-running: polling or webhook + built-in draw scheduler  
- Serverless: Vercel `api/webhook.py` + external cron for draws  

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # fill BOT_TOKEN, MONGO_URI, ADMIN_IDS, …
```

Requirements:

- Bot token from [@BotFather](https://t.me/BotFather)
- MongoDB (Atlas or local)
- Your Telegram numeric user id(s) in `ADMIN_IDS` (e.g. [@userinfobot](https://t.me/userinfobot))
- In groups: bot admin with **Pin messages** (draw times are pinned)

See **`.env.example`** for every variable (`WEBHOOK_SECRET`, `TIMEZONE`, `PAYOUT_MESSAGE_EFFECT_ID`, scheduler/cron, etc.).

## Run locally (polling)

```bash
python run_polling.py
# or: python main.py   # respects RUN_MODE
```

## Deploy

| Target | How |
|--------|-----|
| Polling process | `python run_polling.py` |
| Webhook service | set `PUBLIC_URL`, `WEBHOOK_SECRET`, `RUN_MODE=webhook` → `python main.py` / Docker / Procfile |
| Vercel | `api/webhook.py`; once: `WEBHOOK_URL=… python set_webhook.py` |
| Due draws (serverless) | Cron every 1–5 min → `GET /api/webhook?run_draws=1&secret=$WEBHOOK_SECRET` or long-running `GET /run-draws` + `X-Webhook-Secret` |

## Typical group flow

1. Admin: `/newequb Family | 500 | weekly | ETB | auto`  
2. Members: `/joinequb` (or ➕ Join on the pin)  
3. Admin: `/startcycle` — round 1 opens, `seed_hash` + draw time pinned  
4. Members: private `/contribute` (or 💰 Contribute) → send proof  
5. Admin: `/pending` → Approve / Reject  
6. At `draw_at` (or `/drawnow`): winner announced; seed revealed; winner gets a private DM (optional message effect)  
7. Admin settles **offline**, then `/payout` → Confirm → next round (or complete / auto new cycle)  

Fairness check anytime after a draw: `/verify` or `/verify <period>`.

## Architecture (short)

```
Telegram updates
    → core/routers/* (aiogram)
        → services/equb_service, draw_service, ledger_service, scheduler
            → db/repository (Motor / MongoDB)
```

- **Atomic** transitions: contribution review, draw claim, payout paid, period advance  
- **Append-only** `audit_events` for reviews, draws, payouts, cancel, reconcile  
- **Immutable payout rows** store `seed`, `seed_hash`, `eligible_ids`, winner  

Collections: `equb_groups`, `equb_members`, `equb_contributions`, `equb_payouts`, `audit_events`, `users`, `admins`, `chat_settings`, payment methods (admin).

## Tests

```bash
pip install pytest
pytest tests/ -q
```

Unit tests cover arg parsing, draw math, verify, and in-memory cycle flows. No Mongo required for the default suite.

## Client app (optional)

Telegram remains the first interface. A separate **Mini App / PWA / native app** can sit on the same data model once you expose an API.

→ Full product + data contract for that UI: **[`docs/CLIENT_UX_PROMPT.md`](docs/CLIENT_UX_PROMPT.md)**

## License / ops notes

- The bot never custodies money; organizers remain responsible for offline settlement.  
- Review Git history before public release; keep secrets out of commits.  
- Prefer excluding `.git` and `__pycache__` from source zips.
