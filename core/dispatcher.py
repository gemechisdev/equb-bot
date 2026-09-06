from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from core.config import BOT_TOKEN
from core.middleware import UserCacheMiddleware
from core.routers import admin, common, contribution, group, payout

_bot = None
_dp = None


def build_dispatcher():
    """Memoized so warm serverless invocations reuse the same Bot/Dispatcher
    instead of rebuilding routers and reconnecting on every request."""
    global _bot, _dp

    if _bot is None:
        _bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    if _dp is None:
        _dp = Dispatcher()
        # Remember every user the bot sees so admins can add members by
        # @username later (the Bot API has no username->id lookup).
        _dp.update.outer_middleware(UserCacheMiddleware())
        # Order matters: specific/command routers first, the private-chat
        # proof-submission catch-all (in contribution.py) last.
        _dp.include_router(common.router)
        _dp.include_router(admin.router)
        _dp.include_router(group.router)
        _dp.include_router(payout.router)
        _dp.include_router(contribution.router)

    return _dp, _bot
