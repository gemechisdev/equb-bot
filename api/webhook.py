import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler

from aiogram.types import Update

from core.config import WEBHOOK_SECRET
from core.dispatcher import build_dispatcher
from services import scheduler as scheduler_service

# Serverless deployments have no background scheduler, so draws are run
# lazily: every incoming update (throttled) checks for due draws. Pair this
# with an external cron pinging the endpoint for timely draws on quiet
# groups. The atomic draw claim in the DB prevents double draws.
_DUE_CHECK_MIN_INTERVAL_SECONDS = 60
_last_due_check = 0.0


async def process_update(payload: dict):
    global _last_due_check
    dp, bot = build_dispatcher()
    update = Update.model_validate(payload)
    await dp.feed_update(bot, update)

    now = time.monotonic()
    if now - _last_due_check >= _DUE_CHECK_MIN_INTERVAL_SECONDS:
        _last_due_check = now
        try:
            await scheduler_service.run_due_draws(bot)
        except Exception as e:  # noqa: BLE001 - never fail the webhook ack
            print(f"Due-draw check failed: {e}")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
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
        # simple health check, e.g. GET https://your-app.vercel.app/api/webhook
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "alive"}')
