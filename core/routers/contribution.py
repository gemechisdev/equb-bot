from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from core.i18n import t
from core.keyboards import build_participation_kb, build_review_kb
from core.texts import format_user_identity
from core.timeutils import format_draw_time
from db import repository as repo

router = Router(name="contribution")


async def _is_admin(user_id: int) -> bool:
    return await repo.is_user_admin(user_id)


async def send_contribute_instructions(bot, chat_id: int, lang: str, telegram_id: int) -> None:
    """List everything the user still owes (including mid-cycle back-pay,
    oldest round first) plus payment methods and proof instructions. Used by
    /contribute and by the ?start=contribute deep link from the group's
    pinned Contribute button."""
    pendings = await repo.list_pending_contributions_for_user(telegram_id)
    if not pendings:
        await bot.send_message(chat_id, t(lang, "no_pending_contribution"))
        return

    groups_by_id = {}
    lines = []
    for c in pendings:
        g = groups_by_id.get(str(c["group_id"]))
        if g is None:
            g = await repo.get_group(c["group_id"])
            groups_by_id[str(c["group_id"])] = g
        if c["status"] == "awaiting_review":
            state = t(lang, "state_awaiting_review")
        elif c["status"] == "rejected":
            state = t(lang, "state_rejected")
        else:
            state = t(lang, "state_to_pay")
        when = format_draw_time(g["draw_at"]) if g and g.get("draw_at") else ""
        lines.append(t(
            lang, "contribute_pending_line",
            name=g["name"] if g else "?", period=c["period"],
            amount=c["amount"], currency=g["currency"] if g else "", state=state, when=when,
        ))

    text = t(lang, "contribute_header", count=len(pendings)) + "\n" + "\n".join(lines)

    payment_methods = await repo.list_payment_methods(active_only=True)
    if payment_methods:
        text += "\n\n" + t(lang, "payment_methods_header")
        for pm in payment_methods:
            text += f"\n• {pm['name']}: {pm['details']}"

    text += "\n\n" + t(lang, "contribute_send_proof")
    await bot.send_message(chat_id, text)


@router.message(Command("contribute", "pay"))
async def cmd_contribute(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if message.chat.type != "private":
        kb = await build_participation_kb(message.bot, lang)
        await message.answer(t(lang, "contribute_in_private"), reply_markup=kb)
        return

    await send_contribute_instructions(message.bot, message.chat.id, lang, message.from_user.id)


@router.message(F.chat.type == "private", F.text | F.photo)
async def catch_all_private_proof(message: Message):
    """Any non-command text or photo in a private chat is treated as
    payment proof for the user's OLDEST outstanding round (back-pay is
    consumed in round order), if any."""
    if message.text and message.text.startswith("/"):
        return

    lang = await repo.get_chat_language(message.chat.id)
    pendings = await repo.list_pending_contributions_for_user(message.from_user.id)
    if not pendings:
        await message.answer(t(lang, "no_pending_contribution"))
        return

    # Oldest open debt first: unpaid (`pending`) or previously rejected.
    contribution = next(
        (c for c in pendings if c["status"] in ("pending", "rejected")),
        None,
    )
    if contribution is None:  # everything is awaiting review already
        group = await repo.get_group(pendings[0]["group_id"])
        await message.answer(t(lang, "contribution_already_submitted", name=group["name"] if group else "?"))
        return

    group = await repo.get_group(contribution["group_id"])

    if message.photo:
        proof = {"type": "photo", "content": message.photo[-1].file_id}
    else:
        proof = {"type": "text", "content": message.text}

    updated = await repo.submit_contribution_proof(contribution["_id"], proof)
    if not updated:
        # Race: another concurrent submission, or status changed under us.
        await message.answer(t(lang, "contribution_already_submitted", name=group["name"] if group else "?"))
        return

    remaining = sum(1 for c in pendings if c["status"] in ("pending", "rejected")) - 1
    if remaining > 0:
        await message.answer(t(lang, "proof_received_with_remaining", name=group["name"] if group else "?", remaining=remaining))
    else:
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

    # Scope reviews to the active cycle so old-cycle submissions never reappear.
    pending = await repo.get_awaiting_review_contributions(
        group["_id"], cycle_number=group.get("cycle_number", 1)
    )
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

    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in ("approve", "reject"):
        await callback.answer(t(chat_lang, "review_invalid_action"), show_alert=True)
        return

    _, action, contribution_id = parts
    status = "verified" if action == "approve" else "rejected"

    contribution = await repo.review_contribution(contribution_id, status, callback.from_user.id)
    if not contribution:
        # Stale button or double-click: already reviewed (or missing).
        # Strip the keyboard so the admin cannot click again.
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer(t(chat_lang, "review_already_processed"), show_alert=True)
        return

    group = await repo.get_group(contribution["group_id"])
    try:
        await repo.log_audit(
            f"contribution_{status}",
            actor_id=callback.from_user.id,
            group_id=contribution["group_id"],
            chat_id=group["chat_id"] if group else None,
            target_telegram_id=contribution["telegram_id"],
            contribution_id=contribution["_id"],
            period=contribution.get("period"),
            cycle_number=contribution.get("cycle_number"),
            details={"amount": contribution.get("amount")},
        )
    except Exception:
        pass

    member_lang = await repo.get_chat_language(contribution["telegram_id"])
    result_line = t(chat_lang, f"review_result_{status}")

    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + "\n\n" + result_line,
                reply_markup=None,
            )
        else:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n" + result_line,
                reply_markup=None,
            )
    except Exception:
        # Message may already have been edited; still try to drop the buttons.
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
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
