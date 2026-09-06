"""
Canonical entrypoint for every non-Vercel deployment target: Docker,
Railway, Render, Fly.io, a plain VPS, systemd, whatever.

    python main.py

Behavior is controlled by env vars (see .env.example), not by which
filename you run — main.py, app.py and bot.py all do the same thing:

    RUN_MODE=polling   -> long-lived polling loop (no public URL needed)
    RUN_MODE=webhook   -> starts an aiohttp server with /, /health, /ping
                          and an aiogram webhook endpoint

If RUN_MODE isn't set, we default to "webhook" when PORT is present
(typical of PaaS web services) and "polling" otherwise.
"""

import asyncio
import logging
import sys

from aiohttp import web

from core.config import PORT, RUN_MODE
from core.dispatcher import build_dispatcher
from core.webserver import build_web_app
from db.client import ping_db
from db import repository as repo

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("digital-equb.main")


async def _check_db_or_exit():
    """Fails fast with a clear message if Mongo isn't reachable, instead of
    letting the first /newequb blow up with a 30s timeout buried in a
    random handler's traceback."""
    try:
        await ping_db()
        logger.info("MongoDB connection OK.")
        try:
            await repo.ensure_admins_from_env()
        except Exception:
            logger.warning("Could not seed admins from env into DB.")
    except Exception as e:
        logger.error(
            "Could not connect to MongoDB. Double-check MONGO_URI, and if "
            "you're on MongoDB Atlas, make sure your current IP is allowed "
            "in Atlas -> Network Access. Raw error: %s",
            e,
        )
        sys.exit(1)


def run_polling():
    async def _run():
        await _check_db_or_exit()
        dp, bot = build_dispatcher()
        # Start a lightweight aiohttp site alongside polling so PaaS health
        # checks and wake pings reach the process and keep it warm.
        app = build_web_app(dp=dp, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
        await site.start()
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Starting in POLLING mode with embedded web server...")
        await dp.start_polling(bot)

    asyncio.run(_run())


def run_webhook():
    asyncio.run(_check_db_or_exit())
    app = build_web_app()
    logger.info(f"Starting in WEBHOOK mode on 0.0.0.0:{PORT} ...")
    web.run_app(app, host="0.0.0.0", port=PORT)


def main():
    logger.info(f"RUN_MODE={RUN_MODE}")
    if RUN_MODE == "polling":
        run_polling()
    else:
        run_webhook()


if __name__ == "__main__":
    main()
