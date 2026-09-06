from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.i18n import SUPPORTED_LANGS, language_display_name, t

_bot_username_cache: dict[int, str | None] = {}


async def get_bot_username(bot) -> str | None:
    """The bot's @username (memoized per process — it never changes mid-run).
    Needed to build deep links that open the bot's DM."""
    key = getattr(bot, "id", 0) or 0
    if key not in _bot_username_cache:
        try:
            me = await bot.get_me()
            _bot_username_cache[key] = me.username
        except Exception:
            _bot_username_cache[key] = None
    return _bot_username_cache[key]


async def build_participation_kb(bot, lang: str, group_id=None) -> InlineKeyboardMarkup | None:
    """Buttons pinned under every round announcement:

    - 💰 Contribute — deep link (t.me/<bot>?start=contribute) that opens the
      bot's DM and asks for the payment proof. For users who never started
      the bot, Telegram shows the START button first, which is exactly what
      we want: they must start the bot before they can submit proofs.
    - ➕ Join — lets a group member join (or queue up with back-pay on an
      active cycle) without typing a command.

    Returns None if the bot username can't be resolved (no URL button)."""
    builder = InlineKeyboardBuilder()
    username = await get_bot_username(bot)
    if username:
        builder.button(text=t(lang, "contribute_button"), url=f"https://t.me/{username}?start=contribute")
    if group_id is not None:
        builder.button(text=t(lang, "join_button"), callback_data=f"joinequb:{group_id}")
    if not builder.buttons:
        return None
    return builder.as_markup()


def build_review_kb(contribution_id) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approve", callback_data=f"crev:approve:{contribution_id}")
    builder.button(text="❌ Reject", callback_data=f"crev:reject:{contribution_id}")
    builder.adjust(2)
    return builder.as_markup()


def build_language_kb() -> InlineKeyboardMarkup:
    """Language names are shown in their own language regardless of the
    chat's current language, so a user can recognize their language even if
    the bot is currently set to one they don't read."""
    builder = InlineKeyboardBuilder()
    for code in SUPPORTED_LANGS:
        builder.button(text=language_display_name(code), callback_data=f"setlang:{code}")
    builder.adjust(1)
    return builder.as_markup()
