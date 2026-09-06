"""
Run this once after deploying to Vercel (and again if the URL changes):

    python set_webhook.py

Requires BOT_TOKEN, WEBHOOK_URL and (optionally) WEBHOOK_SECRET in your
environment — see .env.example.
"""

import asyncio
import os

from aiogram import Bot


async def main():
    token = os.environ["BOT_TOKEN"]
    url = os.environ["WEBHOOK_URL"]  # e.g. https://your-app.vercel.app/api/webhook
    secret = os.environ.get("WEBHOOK_SECRET") or None

    bot = Bot(token=token)
    await bot.set_webhook(url=url, secret_token=secret, drop_pending_updates=True)
    info = await bot.get_webhook_info()
    print(info)
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
