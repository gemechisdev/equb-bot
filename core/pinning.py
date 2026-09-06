"""Announce-and-pin helper.

The bot keeps ONE pinned message per group: the current round's key
information (next draw time, or the latest draw result). Pinning a new
message unpins the previous tracked one, so the group's pin bar never
gets cluttered.

Requires the bot to have the "Pin Messages" admin right in the group; if
it doesn't, the message is still sent (unpinned) and we surface a short
note instead of failing silently.
"""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from core.i18n import t
from db import repository as repo

logger = logging.getLogger("digital-equb.pinning")


async def announce_and_pin(bot, chat_id: int, group_id, text: str, *, pin: bool = True, reply_markup=None):
    """Send `text` to the chat and pin it, unpinning the previously tracked
    pin for this group. Returns the sent Message (or None if sending failed)."""
    lang = await repo.get_chat_language(chat_id)
    group = await repo.get_group(group_id)

    old_pinned = (group or {}).get("pinned_message_id")
    if old_pinned and pin:
        try:
            await bot.unpin_chat_message(chat_id, message_id=old_pinned)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logger.warning("Could not unpin previous message in chat %s: %s", chat_id, e)
        except Exception as e:
            logger.warning("Unexpected unpin failure in chat %s: %s", chat_id, e)

    try:
        msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        logger.error("Could not send announcement to chat %s: %s", chat_id, e)
        return None

    if pin:
        try:
            await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
            await repo.set_pinned_message_id(group_id, msg.message_id)
        except TelegramForbiddenError:
            try:
                await bot.send_message(chat_id, t(lang, "pin_permission_missing"))
            except Exception:
                pass
        except (TelegramBadRequest, Exception) as e:
            logger.warning("Could not pin message in chat %s: %s", chat_id, e)

    return msg
