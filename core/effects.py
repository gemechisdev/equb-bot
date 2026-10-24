"""Telegram message effects (celebrations, etc.).

Bot API: message_effect_id is an Integer and is documented for private
chats. Group/supergroup sends often reject the parameter — callers should
use send_with_optional_effect which retries without the effect on failure.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.config import PAYOUT_MESSAGE_EFFECT_ID

logger = logging.getLogger("digital-equb.effects")


def win_effect_id() -> Optional[int]:
    """Effect used when notifying a round winner (private DM preferred)."""
    return PAYOUT_MESSAGE_EFFECT_ID


async def send_with_optional_effect(bot, chat_id: int, text: str, **kwargs: Any):
    """send_message with optional message_effect_id; fall back if Telegram rejects it."""
    effect = kwargs.pop("message_effect_id", None)
    if effect is None:
        effect = win_effect_id()

    send_kwargs = dict(kwargs)
    if effect is not None:
        try:
            # Bot API expects an integer id.
            send_kwargs["message_effect_id"] = int(effect)
            return await bot.send_message(chat_id, text, **send_kwargs)
        except Exception as e:
            logger.info(
                "message_effect_id=%s rejected for chat %s (%s); retrying without effect",
                effect, chat_id, e,
            )
            send_kwargs.pop("message_effect_id", None)

    return await bot.send_message(chat_id, text, **send_kwargs)
