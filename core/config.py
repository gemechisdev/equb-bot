import os

try:
    # Makes `python main.py` / `python run_polling.py` pick up a local .env
    # automatically. No-ops safely if python-dotenv isn't installed or
    # there's no .env file (e.g. on a PaaS where env vars are injected).
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "digital_equb")

# How long to wait for MongoDB to respond before giving up.
MONGO_TIMEOUT_MS = int(os.environ.get("MONGO_TIMEOUT_MS", "20000"))

# Comma separated telegram user ids, e.g. "111111111,222222222"
# These are bot-level (super-admin) ids, seeded into the DB-backed admins
# collection on startup. Group organizers manage their own Equb groups once
# they've been granted admin (see /addadmin).
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
}

# Used to validate that webhook calls really come from Telegram.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Path the webhook server listens on (only used in webhook mode).
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")

# Public base URL of this deployment, e.g. https://your-app.onrender.com
# When set, the webhook server auto-registers itself with Telegram on
# startup. Not needed in polling mode.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip()

# Port the webhook server binds to. Most PaaS platforms inject this
# automatically for web services.
PORT = int(os.environ.get("PORT", 8080))

# Default currency label shown next to amounts when a group doesn't
# override it.
DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "ETB")

# Optional Telegram message effect id applied to payout announcements.
# Defaults to Telegram's built-in celebratory effect when not set.
PAYOUT_MESSAGE_EFFECT_ID = os.environ.get("PAYOUT_MESSAGE_EFFECT_ID", "5298766204649872471").strip()

# IANA timezone used to DISPLAY draw dates/times (default: Ethiopian time).
# All datetimes are stored and computed in UTC internally; this only affects
# what members see, e.g. "Fri, 12 Sep 2026, 06:00 PM (EAT)".
TIMEZONE = os.environ.get("TIMEZONE", "Africa/Addis_Ababa").strip() or "Africa/Addis_Ababa"

# How often (seconds) the background scheduler checks for due draws. Only
# relevant in long-running deployments (polling / webhook server); serverless
# targets (Vercel) run draws lazily on incoming traffic instead.
SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "30"))

# How many minutes before each draw the bot reminds admins about payment
# proofs that are still awaiting review. Sent once per round, and only if
# there actually are unreviewed proofs.
ADMIN_PRE_DRAW_REMIND_MINUTES = int(os.environ.get("ADMIN_PRE_DRAW_REMIND_MINUTES", "60"))

# Default number of hours /postpone moves a pending draw when called
# without an explicit amount.
DRAW_POSTPONE_HOURS = int(os.environ.get("DRAW_POSTPONE_HOURS", "24"))

# RUN_MODE controls how main.py / app.py / bot.py behave:
#   "polling"  -> long-lived polling loop, no public URL/port needed
#   "webhook"  -> starts an HTTP server (aiohttp) with health/ping routes
#                 and an aiogram webhook endpoint
_explicit_mode = os.environ.get("RUN_MODE", "").strip().lower()
if _explicit_mode in ("polling", "webhook"):
    RUN_MODE = _explicit_mode
else:
    RUN_MODE = "webhook" if os.environ.get("PORT") else "polling"
