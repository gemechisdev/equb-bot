def _display_name_from_member(member) -> str | None:
    user = getattr(member, "user", None)
    if not user:
        return None
    parts = [p for p in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if p]
    return " ".join(parts) if parts else None


async def resolve_user_identity(bot, chat_id, telegram_id=None, username=None, display_name=None):
    """Resolve a user's identity as fully as possible from a chat context,
    filling in whatever the caller didn't already have from the live
    Telegram chat-member record (username, display name)."""
    resolved = {
        "telegram_id": int(telegram_id) if telegram_id is not None else None,
        "username": username,
        "display_name": display_name,
    }

    if resolved["telegram_id"] is not None and (not resolved["username"] or not resolved["display_name"]):
        try:
            member = await bot.get_chat_member(chat_id, resolved["telegram_id"])
            user = member.user
            resolved["username"] = resolved["username"] or user.username
            resolved["display_name"] = resolved["display_name"] or _display_name_from_member(member) or user.full_name
        except Exception:
            pass

    return resolved
