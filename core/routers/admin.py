from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from core.i18n import t
from db import repository as repo

router = Router(name="admin")


async def is_admin(user_id: int) -> bool:
    return await repo.is_user_admin(user_id)


def _split_pipe_args(text: str, expected_parts: int) -> list[str] | None:
    """Splits 'Name | Details' (or 'id | Name | Details') style arguments on
    '|', trimming whitespace. Returns None if the piece count doesn't match."""
    pieces = [p.strip() for p in text.split("|")]
    if len(pieces) != expected_parts or any(not p for p in pieces):
        return None
    return pieces


@router.message(Command("addadmin", "aadmin"))
async def cmd_addadmin(message: Message):
    if not await is_admin(message.from_user.id):
        return
    lang = await repo.get_chat_language(message.chat.id)

    parts = message.text.split()
    try:
        aid = int(parts[1])
    except (IndexError, ValueError):
        await message.answer(t(lang, "usage_addadmin"))
        return

    await repo.add_admin(aid)
    await message.answer(t(lang, "admin_added", admin_id=aid))


@router.message(Command("deladmin", "dadmin"))
async def cmd_deladmin(message: Message):
    if not await is_admin(message.from_user.id):
        return
    lang = await repo.get_chat_language(message.chat.id)

    parts = message.text.split()
    try:
        aid = int(parts[1])
    except (IndexError, ValueError):
        await message.answer(t(lang, "usage_deladmin"))
        return

    await repo.remove_admin(aid)
    await message.answer(t(lang, "admin_removed", admin_id=aid))


@router.message(Command("listadmins", "admins"))
async def cmd_listadmins(message: Message):
    if not await is_admin(message.from_user.id):
        return
    lang = await repo.get_chat_language(message.chat.id)

    admins = await repo.get_admins()
    lines = [str(a) for a in admins]
    await message.answer(t(lang, "admins_list_header", list="\n".join(lines)))


@router.message(Command("chat", "msg"))
async def cmd_chat(message: Message, bot: Bot):
    if not await is_admin(message.from_user.id):
        return
    lang = await repo.get_chat_language(message.chat.id)

    parts = message.text.split(maxsplit=2)
    try:
        target = int(parts[1])
    except (IndexError, ValueError):
        await message.answer(t(lang, "usage_chat"))
        return

    if len(parts) > 2 and parts[2].strip():
        txt = parts[2].strip()
        try:
            await bot.send_message(target, txt)
            await message.answer(t(lang, "chat_sent"))
        except Exception:
            await message.answer(t(lang, "chat_send_failed"))
        return

    if not message.reply_to_message:
        await message.answer(t(lang, "chat_need_reply_or_text"))
        return

    rm = message.reply_to_message
    try:
        await bot.copy_message(chat_id=target, from_chat_id=rm.chat.id, message_id=rm.message_id)
        await message.answer(t(lang, "chat_forwarded"))
    except Exception:
        try:
            await bot.forward_message(chat_id=target, from_chat_id=rm.chat.id, message_id=rm.message_id)
            await message.answer(t(lang, "chat_forwarded"))
        except Exception:
            await message.answer(t(lang, "chat_forward_failed"))


# ---------------------------------------------------------------------------
# Payment method management (bot-wide, not scoped to a single Equb group —
# the organizer's receiving accounts are the same regardless of which group
# a contribution belongs to).
# ---------------------------------------------------------------------------

@router.message(Command("addpayment"))
async def cmd_addpayment(message: Message):
    if not await is_admin(message.from_user.id):
        return
    lang = await repo.get_chat_language(message.chat.id)

    _, _, rest = message.text.partition(" ")
    parts = _split_pipe_args(rest, 2)
    if not parts:
        await message.answer(t(lang, "usage_addpayment"))
        return

    name, details = parts
    doc = await repo.add_payment_method(name, details)
    await message.answer(t(lang, "payment_added", payment_id=str(doc["_id"]), name=name))


@router.message(Command("editpayment"))
async def cmd_editpayment(message: Message):
    if not await is_admin(message.from_user.id):
        return
    lang = await repo.get_chat_language(message.chat.id)

    _, _, rest = message.text.partition(" ")
    parts = _split_pipe_args(rest, 3)
    if not parts:
        await message.answer(t(lang, "usage_editpayment"))
        return

    payment_id, name, details = parts
    ok = await repo.update_payment_method(payment_id, name=name, details=details)
    if not ok:
        await message.answer(t(lang, "payment_not_found"))
        return
    await message.answer(t(lang, "payment_updated", name=name))


@router.message(Command("delpayment"))
async def cmd_delpayment(message: Message):
    if not await is_admin(message.from_user.id):
        return
    lang = await repo.get_chat_language(message.chat.id)

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(t(lang, "usage_delpayment"))
        return

    ok = await repo.delete_payment_method(parts[1].strip())
    if not ok:
        await message.answer(t(lang, "payment_not_found"))
        return
    await message.answer(t(lang, "payment_removed"))


@router.message(Command("togglepayment"))
async def cmd_togglepayment(message: Message):
    if not await is_admin(message.from_user.id):
        return
    lang = await repo.get_chat_language(message.chat.id)

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(t(lang, "usage_togglepayment"))
        return

    method = await repo.get_payment_method(parts[1].strip())
    if not method:
        await message.answer(t(lang, "payment_not_found"))
        return

    new_active = not method.get("active", True)
    await repo.set_payment_method_active(method["_id"], new_active)
    state = t(lang, "state_active") if new_active else t(lang, "state_inactive")
    await message.answer(t(lang, "payment_toggled", name=method["name"], state=state))


@router.message(Command("listpayments", "payments"))
async def cmd_listpayments(message: Message):
    if not await is_admin(message.from_user.id):
        return
    lang = await repo.get_chat_language(message.chat.id)

    methods = await repo.list_payment_methods(active_only=False)
    if not methods:
        await message.answer(t(lang, "payment_list_empty"))
        return

    lines = [t(lang, "payment_list_header")]
    for m in methods:
        state = t(lang, "state_active") if m.get("active", True) else t(lang, "state_inactive")
        lines.append(t(lang, "payment_list_line", payment_id=str(m["_id"]), name=m["name"], details=m["details"], state=state))
    await message.answer("\n".join(lines))
