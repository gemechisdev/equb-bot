"""Remember every user the bot sees (group members messaging around it,
people DMing it) in a lightweight `users` collection.

The Telegram Bot API has no username -> id lookup, so this cache is what
lets an admin run /addmember @someuser later — provided that user has sent
at least one message in a chat the bot is in. Numeric ids and replying to
one of their messages always work regardless of the cache.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from db import repository as repo


class UserCacheMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ):
        user = data.get("event_from_user")
        if user and not user.is_bot:
            try:
                await repo.upsert_user(user.id, user.username, user.first_name, user.full_name)
            except Exception:
                pass  # identity caching must never break message handling
        return await handler(event, data)
