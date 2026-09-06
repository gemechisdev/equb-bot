from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from core.i18n import t
from core.keyboards import build_review_kb
from core.texts import format_user_identity
from core.timeutils import format_draw_time
from db import repository as repo

router = Router(name="contribution")


async def _is_admin(user_id: int) -> bool:
    return await repo.is_user_admin(user_id)


@router.message(Command("contribute", "pay"))
async def cmd_contribute(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if message.chat.type != "private":
        await message.answer(t(lang, "contribute_in_private"))
        return

    contribution = await repo.find_awaiting_contribution_for_user(message.from_user.id)
    if not contribution:
        await message.answer(t(lang, "no_pending_contribution"))
        return

    group = await repo.get_group(contribution["group_id"])
    if contribution["status"] == "awaiting_review":
        await message.answer(t(lang, "contribution_already_submitted", name=group["name"] if group else "?"))
        return

    payment_methods = await repo.list_payment_methods(active_only=True)
    when = ""
    if group and group.get("draw_at"):
        when = format_draw_time(group["draw_at"])
    lines = [
        t(
            lang,
            "contribute_instructions",
            name=group["name"] if group else "?",
            period=contribution["period"],
            amount=contribution["amount"],
            currency=group["currency"] if group else "",
            when=when,
        )
    ]
    if payment_methods:
        lines.append("")
        lines.append(t(lang, "payment_methods_header"))
        for pm in payment_methods:
            lines.append(f"• {pm['name']}: {pm['details']}")
    lines.append("")
    lines.append(t(lang, "contribute_send_proof"))
    await message.answer("\n".join(lines))


@router.message(F.chat.type == "private", F.text | F.photo)
async def catch_all_private_proof(message: Message):
    """Any non-command text or photo in a private chat is treated as
    payment proof for whichever contribution the user still owes, if any."""
    if message.text and message.text.startswith("/"):
        return

    lang = await repo.get_chat_language(message.chat.id)
    contribution = await repo.find_awaiting_contribution_for_user(message.from_user.id)
    if not contribution:
        await message.answer(t(lang, "no_pending_contribution"))
        return

    group = await repo.get_group(contribution["group_id"])

    if contribution["status"] == "awaiting_review":
        await message.answer(t(lang, "contribution_already_submitted", name=group["name"] if group else "?"))
        return

    if message.photo:
        proof = {"type": "photo", "content": message.photo[-1].file_id}
    else:
        proof = {"type": "text", "content": message.text}

    await repo.submit_contribution_proof(contribution["_id"], proof)
    await message.answer(t(lang, "proof_received", name=group["name"] if group else "?"))


@router.message(Command("pending", "pd"))
async def cmd_pending(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group or group["status"] != "active":
        await message.answer(t(lang, "no_active_group"))
        return

    pending = await repo.get_awaiting_review_contributions(group["_id"])
    if not pending:
        await message.answer(t(lang, "no_pending_reviews"))
        return

    for c in pending:
        who = format_user_identity(c.get("display_name"), c.get("username"), c["telegram_id"])
        text = t(lang, "pending_review_line", who=who, period=c["period"], amount=c["amount"], currency=group["currency"])
        if c["proof"]["type"] == "photo":
            await message.answer_photo(c["proof"]["content"], caption=text, reply_markup=build_review_kb(str(c["_id"])))
        else:
            text += "\n\n" + t(lang, "pending_review_proof_text", proof=c["proof"]["content"])
            await message.answer(text, reply_markup=build_review_kb(str(c["_id"])))


@router.callback_query(F.data.startswith("crev:"))
async def cb_review_contribution(callback: CallbackQuery):
    chat_lang = await repo.get_chat_language(callback.message.chat.id)
    if not await _is_admin(callback.from_user.id):
        await callback.answer(t(chat_lang, "admins_only"), show_alert=True)
        return

    _, action, contribution_id = callback.data.split(":", 2)
    status = "verified" if action == "approve" else "rejected"

    contribution = await repo.review_contribution(contribution_id, status, callback.from_user.id)
    if not contribution:
        await callback.answer()
        return

    group = await repo.get_group(contribution["group_id"])
    member_lang = await repo.get_chat_language(contribution["telegram_id"])

    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=callback.message.caption + "\n\n" + t(chat_lang, f"review_result_{status}")
            )
        else:
            await callback.message.edit_text(callback.message.text + "\n\n" + t(chat_lang, f"review_result_{status}"))
    except Exception:
        pass

    try:
        await callback.bot.send_message(
            contribution["telegram_id"],
            t(
                member_lang,
                f"dm_contribution_{status}",
                name=group["name"] if group else "?",
                period=contribution["period"],
                amount=contribution["amount"],
                currency=group["currency"] if group else "",
            ),
        )
    except Exception:
        pass

    await callback.answer()
