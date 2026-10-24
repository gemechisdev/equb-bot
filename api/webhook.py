import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from aiogram.types import Update

from core.config import WEBHOOK_SECRET
from core.dispatcher import build_dispatcher
from services import scheduler as scheduler_service

# Serverless deployments have no background scheduler, so draws are run
# lazily: every incoming update (throttled) checks for due draws. Pair this
# with an external cron hitting GET/POST ?run_draws=1&secret=... (or the
# X-Webhook-Secret header) for timely draws on quiet groups. The atomic
# draw claim in the DB prevents double draws.
_DUE_CHECK_MIN_INTERVAL_SECONDS = 60
_last_due_check = 0.0
_last_due_draws_ok_at: float | None = None
_commands_registered = False


async def process_update(payload: dict):
    global _last_due_check, _last_due_draws_ok_at, _commands_registered
    dp, bot = build_dispatcher()

    # Serverless has no process startup hook — register the "/" menu once
    # per warm instance (and again after cold starts).
    if not _commands_registered:
        try:
            from core.bot_commands import register_bot_commands
            await register_bot_commands(bot)
            _commands_registered = True
        except Exception as e:  # noqa: BLE001
            print(f"Bot command registration failed: {e}")

    update = Update.model_validate(payload)
    await dp.feed_update(bot, update)

    now = time.monotonic()
    if now - _last_due_check >= _DUE_CHECK_MIN_INTERVAL_SECONDS:
        _last_due_check = now
        try:
            await scheduler_service.run_due_draws(bot)
            _last_due_draws_ok_at = time.time()
        except Exception as e:  # noqa: BLE001 - never fail the webhook ack
            print(f"Due-draw check failed: {e}")


async def run_due_draws_now() -> dict:
    """Unthrottled due-draw sweep for external cron on serverless."""
    global _last_due_draws_ok_at
    dp, bot = build_dispatcher()
    executed = await scheduler_service.run_due_draws(bot)
    _last_due_draws_ok_at = time.time()
    return {"executed": executed, "last_ok_at": _last_due_draws_ok_at}


def _secret_ok(headers, query: dict) -> bool:
    if not WEBHOOK_SECRET:
        return True
    provided = (
        headers.get("X-Telegram-Bot-Api-Secret-Token")
        or headers.get("X-Webhook-Secret")
        or (query.get("secret") or [None])[0]
    )
    return provided == WEBHOOK_SECRET


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        # Explicit cron trigger: POST /api/webhook?run_draws=1 with secret.
        if query.get("run_draws", ["0"])[0] in ("1", "true", "yes"):
            if not _secret_ok(self.headers, query):
                self.send_response(401)
                self.end_headers()
                return
            try:
                result = asyncio.run(run_due_draws_now())
                body = json.dumps({"ok": True, **result}).encode()
            except Exception as e:  # noqa: BLE001
                print(f"run_draws failed: {e}")
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return

        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
            self.send_response(401)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"

        try:
            payload = json.loads(body)
            asyncio.run(process_update(payload))
        except Exception as e:  # noqa: BLE001 - log and still ack Telegram
            print(f"Error processing update: {e}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        # External cron: GET /api/webhook?run_draws=1&secret=...
        if query.get("run_draws", ["0"])[0] in ("1", "true", "yes"):
            if not _secret_ok(self.headers, query):
                self.send_response(401)
                self.end_headers()
                return
            try:
                result = asyncio.run(run_due_draws_now())
                body = json.dumps({"ok": True, **result}).encode()
                status = 200
            except Exception as e:  # noqa: BLE001
                print(f"run_draws failed: {e}")
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                status = 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return

        # Simple health check.
        body = json.dumps({
            "status": "alive",
            "last_due_draws_ok_at": _last_due_draws_ok_at,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
