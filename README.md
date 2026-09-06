# Digital Equb Bot

A Telegram bot that runs a traditional **Equb (እቁብ)** — a rotating
savings and credit association — digitally inside a Telegram group,
as a **provably-fair lottery**.

A fixed group of trusted members each contribute the same amount every
round (weekly, bi-weekly, monthly, or every N days). At each round's
scheduled draw time, the whole pool goes to ONE member drawn **at random
among those who verified their payment before the draw** — nobody, not
even the admins, knows who will win until the draw actually happens.
Once you've received your pool you keep contributing every round like
everyone else; you're just no longer in the draw. The cycle ends once
every member has received the pool exactly once (and can auto-restart a
fresh cycle if the group was created with `auto`).

Every draw is **provably fair**: when a round opens the bot publishes a
SHA-256 commitment of a secret seed; at draw time the winner is
`sorted(eligible_ids)[Random(seed).randrange(n)]` and the seed is
revealed in the result, so anyone can re-run the pick and match it
against the published commitment. It couldn't have been predicted
earlier even in principle, because the eligible set isn't final until
the deadline — unpaid members can still pay in.

## Stack

- aiogram 3.x (async, webhook-first)
- MongoDB (Motor async driver)
- aiohttp for the built-in web server (health/root/ping/wake + webhook endpoint)
- Built-in asyncio scheduler that fires draws at their exact scheduled time
- Deployable as: a long-running polling process, a standard web service
  (Docker/Procfile/gunicorn), or Vercel serverless functions

## 1. Setup

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill in the real values
```

You need:

- A bot token from **@BotFather**.
- A MongoDB connection string (Atlas free tier, or local `mongod`).
- Your own Telegram numeric user ID (and any co-organizers) for `ADMIN_IDS`
  — get it from **@userinfobot**.

`.env` is loaded automatically whenever you run `main.py`, `app.py`,
`bot.py`, or `run_polling.py` locally.

> **Group admin rights:** add the bot as a group admin with the
> **"Pin Messages"** right. It posts the exact draw date & time for every
> round and keeps it pinned (replacing the previous pin). Without the
> right it still posts everything, just unpinned.

## 2. Try it locally

```bash
python run_polling.py
```

Add the bot to a group, then:

1. `/newequb Family Equb | 500 | weekly | ETB | auto` — creates a group:
   500 ETB per member per round, drawn weekly, and a fresh cycle starts
   automatically when the current one completes. Frequency can be
   `weekly`, `biweekly`, `monthly`, or every N days (`3d`, `14d`, …).
2. Everyone who wants in runs `/joinequb` — or an admin adds specific
   people with `/addmember @username` (or a numeric id, or by replying
   to one of their messages). The pinned announcement also carries
   **💰 Contribute** and **➕ Join** buttons.
3. An admin runs `/startcycle` — membership is locked, the exact first
   draw date & time is announced **and pinned** (with the buttons), and
   the round's fairness commitment (seed hash) is published. No order and
   no pre-known winner exists.
4. Each member DMs the bot their payment proof (a screenshot or a typed
   transaction ID) — either after running `/contribute` for instructions,
   by tapping the **💰 Contribute** button on the pinned message (it opens
   the bot's DM for you, and starts the bot first if necessary), or just
   by sending the proof directly.
5. An admin runs `/pending` in the group to review proofs with
   Approve/Reject buttons. **Only verified payments are in the draw.**
   The bot DMs admins ~1 hour before each draw if proofs are still
   unreviewed.
6. At the scheduled draw time the bot automatically announces (and pins)
   the winner among verified payers, with the seed revealed for
   verification. If NOBODY verified a payment by then, the bot posts
   that the draw is overdue and admins decide: `/drawnow` once payments
   are verified, or `/postpone 24` to move the draw.
7. After the winner actually receives the pool (off-platform), an admin
   runs `/payout` — the next round opens with a new pinned draw time, or
   the cycle completes / auto-restarts.
8. `/status` at any time shows the next draw time, who has/hasn't paid,
   and who already won — never the upcoming winner, because nobody knows
   it. `/history` shows the full payout log.

## 3. How draws are scheduled

- **Polling / webhook server / gunicorn:** a background scheduler task
  (default: every 30s, `SCHEDULER_INTERVAL_SECONDS`) claims and runs due
  draws. Every draw is claimed atomically in MongoDB, so multiple
  workers or an app restart can never double-draw or skip one — a draw
  missed during downtime runs on the next startup/tick.
- **Vercel serverless:** no background task is possible, so every
  incoming update (throttled to once per minute) and every `/health` /
  `/wake` hit also checks for due draws. For timely draws on quiet
  groups, point a free cron service (e.g. cron-job.org, UptimeRobot) at
  `https://your-app.vercel.app/api/webhook` every minute — a GET returns
  `200` and doubles as the wake ping.

Timezone for all displayed draw times defaults to **Africa/Addis_Ababa**
(EAT); override globally with the `TIMEZONE` env var.

## 4. Deployment mode selection

Every entrypoint (`main.py`, `app.py`, `bot.py`) shares the same logic,
controlled by env vars — **not** by which file you run:

| RUN_MODE  | Behavior                                                                                                                     | Needs                                                                                                                    |
| --------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `polling` | Long-lived polling loop                                                                                                      | Just `BOT_TOKEN` + `MONGO_URI` — works anywhere, even with no public URL at all |
| `webhook` | Starts an aiohttp server exposing `/` `/health` `/ping` `/wake` and `/webhook` (Telegram updates) | A public URL platforms can reach — set `PUBLIC_URL` so it self-registers the webhook on startup |

If `RUN_MODE` isn't set explicitly, it's auto-detected: **webhook** if
`PORT` is present (most PaaS web services inject this automatically),
**polling** otherwise.

## 5. Deployment options

### A) Docker (any host that runs containers)

```bash
docker build -t digital-equb-bot .
docker run -d --env-file .env -p 8080:8080 \
  -e PUBLIC_URL=https://your-domain.example \
  digital-equb-bot
```

Or as a background worker (no public URL needed):

```bash
docker run -d --env-file .env -e RUN_MODE=polling digital-equb-bot
```

### B) `docker-compose` (local dev with a real Mongo, no Atlas needed)

```bash
docker compose up
```

### C) Railway / Render / Fly.io / Heroku-style platforms

These understand the included `Procfile`:

```
web: python main.py
worker: RUN_MODE=polling python main.py
```

For Render specifically, set the health check path to `/health`.

### D) Vercel (serverless)

```bash
vercel
vercel env add BOT_TOKEN
vercel env add MONGO_URI
vercel env add MONGO_DB_NAME
vercel env add ADMIN_IDS
vercel env add WEBHOOK_SECRET
vercel --prod
```

Then register the webhook once (serverless has no startup hook):

```bash
export BOT_TOKEN=... WEBHOOK_URL=https://your-app.vercel.app/api/webhook WEBHOOK_SECRET=...
python set_webhook.py
```

See section 3 for scheduling draws on serverless (cron pings).

### E) gunicorn / plain VPS / systemd

```bash
gunicorn -k aiohttp.GunicornWebWorker -b 0.0.0.0:8080 app:app
```

or a `systemd` unit running `python main.py` with `RUN_MODE=polling`.

## 6. Commands

### Setting up (group chat, admin only)

| Command | Purpose |
| --- | --- |
| `/newequb Name \| amount \| frequency \| [currency] \| [restart]` | Create a group. `frequency`: `weekly`, `biweekly`, `monthly`, or every N days (`3d`, `14d`, …). `restart` (optional): `once` (default — ends when everyone got the pool) or `auto` (a fresh cycle starts immediately with the same members). |
| `/startcycle` (`/sc`) | Lock membership, open round 1, schedule + pin the first draw time |
| `/addmember @user\|user_id\|reply` | Add a specific person — while open they join immediately; mid-cycle they follow the same back-pay rule as `/joinequb` |
| `/removemember @user\|user_id\|reply` | Remove someone; mid-cycle removal is blocked if they already received a pool or are the current round's drawn winner |
| `/cancelequb` | Cancel the group entirely |

### Joining (group chat, anyone)

| Command | Purpose |
| --- | --- |
| `/joinequb` (`/join`) | Join the Equb — while it's open, or MID-CYCLE (see below) |
| `/leaveequb` (`/leave`) | Leave before the cycle starts |
| `/members` (`/listmembers`) | List current members (with received-pool and back-pay badges) |

**Joining mid-cycle (back-pay).** The ➕ Join button on the pinned message
and `/joinequb` both work while a cycle is running — important for
`restart=auto` groups, which are effectively always active. To keep the
lottery fair, a mid-cycle joiner must **back-pay every round the cycle
has already run** (they get a pending contribution for each missed round,
paid privately round by round) and stay paid-up every round after. Until
their back-pay is complete they contribute but can't win a draw — this
closes the loophole where someone joins after everyone else had received
their pool and wins a full pool having paid once. A `once` cycle in its
final drawn round refuses joins (there's no next round left for them).

### Running the cycle (group chat, admin unless noted)

| Command | Purpose |
| --- | --- |
| `/status` (`/board`) | Anyone — next draw time (exact, with countdown), who's paid, who already received a pool. **Never shows a future winner — nobody knows it.** |
| `/recipient` (`/whoisnext`) | Anyone — this round's drawn winner, or when the draw is coming if it hasn't fired yet |
| `/drawnow` | Draw the winner immediately among currently-verified payers |
| `/postpone <hours>` | Move the pending draw (default 24h). The committed seed is kept, so the round's fairness commitment still holds |
| `/pending` (`/pd`) | Contributions awaiting review, with Approve/Reject buttons |
| `/payout` (`/markpaid`) | Confirm the winner received the pool; opens the next round with a new pinned draw time, or completes / auto-restarts the cycle |
| `/remind` | DM everyone who hasn't paid yet this round |
| `/history` | Anyone — full payout log for this group |
| `/listequbs` (`/equbs`) | Anyone — every Equb group ever run in this chat |

### Contributing (private chat with the bot, anyone)

| Command | Purpose |
| --- | --- |
| `/contribute` (`/pay`) | See everything you owe — round by round, including back-pay — and how to pay |

The **💰 Contribute** button on the group's pinned message is the fastest
path: it deep-links into the bot's DM (`t.me/<bot>?start=contribute`) and
immediately shows what you owe — Telegram makes users press START first if
they've never started the bot, which is exactly what proof submission
requires. After that, just send your payment proof — a photo or a typed
transaction ID — as a normal message; it's matched to your **oldest
unpaid round** automatically. **Get verified before the draw: unverified
members are excluded from that round's draw** (they stay eligible for
later rounds).

### Admin & payment method management

| Command | Purpose |
| --- | --- |
| `/addadmin` / `/deladmin` / `/listadmins` | DB-backed bot admins (in addition to `ADMIN_IDS` in env) |
| `/addpayment` `Name \| Details` | Add a payment method (e.g. `Telebirr \| 09xxxxxxxx`) |
| `/editpayment <id> \| Name \| Details` | Edit one |
| `/delpayment <id>` / `/togglepayment <id>` | Remove / activate-deactivate one |
| `/listpayments` (`/payments`) | List all payment methods |
| `/chat <telegram_id> <text>` | DM a user directly (or reply to a message with `/chat <telegram_id>` to forward it) |

### Languages

Every chat — a group **or** a private DM — has its own language,
independent of every other chat. Anyone can run `/language` (alias
`/lang`) in their own private chat; in a group, only admins can change it.
Supported: English (`en`), Amharic (`am`), Afaan Oromoo (`om`), default
English. Strings live in `langs/en.yml`, `langs/am.yml`, `langs/om.yml` —
`am.yml`/`om.yml` are intentionally partial (the essentials are
translated); any key missing from a translation file falls back to
English automatically, so it's safe to extend them gradually. To add a
language: copy `langs/en.yml` to `langs/<code>.yml`, translate values
(keep `{placeholders}`), and add `<code>` to `SUPPORTED_LANGS` in
`core/i18n.py`.

## 7. Project layout

```
main.py / app.py / bot.py     entrypoints (polling or webhook, env-driven)
run_polling.py                 forces polling mode, for local testing
set_webhook.py                  one-off webhook registration (Vercel)
api/webhook.py                   Vercel serverless entrypoint (runs due
                                  draws lazily on incoming traffic)

core/config.py                 env var loading + RUN_MODE detection
core/timeutils.py              frequency parsing ("3d", "weekly", …) +
                                draw-time formatting in the display TZ
core/dispatcher.py              aiogram Bot/Dispatcher + router wiring
core/middleware.py              users identity cache (enables /addmember
                                @username lookups)
core/webserver.py               aiohttp app: webhook + health/root/ping/wake,
                                starts the draw scheduler
core/pinning.py                 announce-and-pin (one tracked pin per group)
core/keyboards.py               inline keyboards (approve/reject, language)
core/texts.py                   status board / draw announcements / payouts
core/i18n.py                    language loader/lookup (t()), reads langs/*.yml
core/routers/
  common.py                       /start /help /language
  group.py                        group lifecycle: create, join, add member,
                                   members, start cycle, status, cancel
  contribution.py                 private-chat proof submission + /pending
                                   review (Approve/Reject)
  payout.py                       /recipient /drawnow /postpone /payout
                                   /remind /history
  admin.py                        admin mgmt, payment method CRUD, /chat

langs/en.yml, am.yml, om.yml   user-facing strings, one flat key:text
                                mapping per language

db/client.py                   Mongo connection singleton (tz-aware)
db/repository.py               every Mongo query — the only file that
                                talks to MongoDB directly (includes the
                                atomic draw claim)

services/equb_service.py       core domain logic: create/join/add, start a
                                cycle, advance rounds, auto-restart,
                                complete the cycle
services/draw_service.py       the lottery: seed commit/reveal, eligibility,
                                winner pick, draw announcements
services/scheduler.py          background scheduler: due draws, pre-draw
                                admin reminders, overdue notifications
services/user_identity.py      resolve a Telegram user's username/display
                                name from the live chat-member record

tests/                         pytest suite for the pure logic
                                (frequency parsing, draw math, status
                                board privacy, /newequb parsing)
```

## 8. Data model

- **equb_groups** — one Equb per document, scoped to the Telegram chat it
  lives in. `status`: `open` → `active` → `completed` | `cancelled`.
  Lottery state lives here: `interval_days` (from the frequency),
  `restart_mode`, `cycle_number`, and for the current round: `seed` /
  `seed_hash` (commit/reveal pair), `draw_at` (UTC), `draw_state`
  (`scheduled` → `drawing` → `drawn`, or `overdue`), plus the pinned
  message tracker. Legacy fields (`order`) are kept only for groups that
  were mid-cycle before the lottery upgrade — they still complete the old
  sequential way.
- **equb_members** — one doc per (group, user). Tracks whether they've
  received their pool yet and in which round; `joined_period` records
  mid-cycle additions (they must back-pay rounds 1..joined_period before
  they can win a draw; reset when a new cycle starts).
- **equb_contributions** — one doc per (group, round, user):
  `pending` → `awaiting_review` → `verified` | `rejected`.
- **equb_payouts** — one doc per (group, round), **created at draw time**
  (the winner doesn't exist anywhere before that): winner, pool amount,
  plus `seed`, `seed_hash` and the exact `eligible_ids` list the draw ran
  on for public verification.
- **users** — lightweight identity cache (telegram_id ↔ username) built
  from every message the bot sees; powers `/addmember @username`.
- **payment_methods** / **admins** / **chat_settings** — shared,
  bot-wide (not scoped to a single group).

## 9. Known limitations

- **One active group per chat at a time** — start a new one only after
  the current one completes or is cancelled.
- **No automated late-payment penalties.** Unverified members are
  excluded from that round's draw and `/remind` + the pre-draw admin
  nudge chase them, but nothing penalizes automatically.
- **Payment verification is manual** — an admin eyeballs each proof and
  taps Approve/Reject. Only verified payments are in the draw.
- **Payout disbursement is off-platform.** `/payout` records that the
  organizer has already paid the winner outside the bot (bank transfer,
  cash, Telebirr) — it doesn't move money itself.
- **Serverless draw timing** drifts until the next request arrives
  (section 3); long-running deployments fire draws exactly on time.

## 10. Next steps (future)

- Automatic payment verification (Telebirr/bank API or SMS parsing).
- A Telegram Mini App for a nicer setup/status UI (the service layer is
  already transport-agnostic, so a future REST layer can reuse it as-is).
- Multi-currency / multi-group dashboards, `/export` for a full ledger.
- Per-group timezone overrides via a `/settimezone` command.
