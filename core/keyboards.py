from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.i18n import SUPPORTED_LANGS, language_display_name


def build_review_kb(contribution_id) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approve", callback_data=f"crev:approve:{contribution_id}")
    builder.button(text="❌ Reject", callback_data=f"crev:reject:{contribution_id}")
    builder.adjust(2)
    return builder.as_markup()


def build_join_kb(group_number: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🙋 Join this Equb", callback_data=f"joinequb:{group_number}")
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
