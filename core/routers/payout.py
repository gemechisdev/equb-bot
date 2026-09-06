import logging
from datetime import timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from core import config
from core import timeutils
from core.i18n import t
from core.keyboards import build_participation_kb
from core.pinning import announce_and_pin
from core.texts import (
    build_next_round_text,
    build_payout_announcement_text,
    format_user_identity,
)
from db import repository as repo
from services import draw_service, equb_service
from services.equb_service import EqubError

logger = logging.getLogger("digital-equb.payout")

router = Router(name="payout")


async def _is_admin(user_id: int) -> bool:
    return await repo.is_user_admin(user_id)


def _error_text(lang: str, error: EqubError) -> str:
    code = error.args[0]
    if code.startswith("usage_"):
        return t(lang, code)
    return t(lang, f"error_{code}")


@router.message(Command("recipient", "whoisnext"))
async def cmd_recipient(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    group = await repo.get_active_or_open_group(message.chat.id)
    if not group or group["status"] != "active":
        await message.answer(t(lang, "no_active_group"))
        return

    payout = await repo.get_payout(group["_id"], group["current_period"], group.get("cycle_number"))
    if payout:
        member = await repo.get_member(group["_id"], payout["telegram_id"])
        who = format_user_identity(
            member.get("display_name") if member else None,
            member.get("username") if member else None,
            payout["telegram_id"],
        )
        state = t(lang, "payout_paid" if payout["status"] == "paid" else "payout_pending")
        await message.answer(
            t(lang, "recipient_winner_info", period=group["current_period"], who=who,
              amount=payout["amount"], currency=group["currency"], state=state)
        )
        return

    if group.get("draw_at"):
        await message.answer(
            t(lang, "recipient_pending_info",
              when=timeutils.format_draw_time(group["draw_at"]),
              countdown=timeutils.format_countdown(group["draw_at"]))
        )
    else:
        await message.answer(t(lang, "no_active_group"))


@router.message(Command("drawnow", "draw"))
async def cmd_drawnow(message: Message):
    """Admin: run the round's draw right now among members whose payment is
    already verified — don't wait for the scheduled draw time."""
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group or group["status"] != "active":
        await message.answer(t(lang, "no_active_group"))
        return

    claimed = await repo.claim_due_draw(group["_id"], force=True)
    if not claimed:
        await message.answer(t(lang, "error_nothing_to_draw"))
        return

    try:
        result = await draw_service.run_draw(message.bot, claimed)
    except Exception:
        logger.exception("Manual draw failed for group %s", claimed["_id"])
        await repo.reset_draw_state(claimed["_id"])
        await message.answer(t(lang, "error_nothing_to_draw"))
        return

    if result.get("overdue"):
        await message.answer(t(lang, "error_no_eligible_members"))


@router.message(Command("postpone",))
async def cmd_postpone(message: Message):
    """Admin: move the pending draw by N hours (default DRAW_POSTPONE_HOURS).
    The committed seed stays the same, so the fairness commitment holds."""
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group or group["status"] != "active":
        await message.answer(t(lang, "no_active_group"))
        return

    if group.get("draw_state") not in ("scheduled", "overdue") or not group.get("draw_at"):
        await message.answer(t(lang, "error_nothing_to_draw"))
        return

    parts = message.text.split()
    hours = config.DRAW_POSTPONE_HOURS
    if len(parts) > 1:
        try:
            hours = int(parts[1])
        except ValueError:
            await message.answer(t(lang, "usage_postpone", default=config.DRAW_POSTPONE_HOURS))
            return
    if hours < 1:
        await message.answer(t(lang, "usage_postpone", default=config.DRAW_POSTPONE_HOURS))
        return

    now = repo.utcnow()
    base = group["draw_at"] if group["draw_at"] > now else now
    new_draw_at = base + timedelta(hours=hours)
    await repo.set_draw_time(group["_id"], new_draw_at)

    await message.answer(
        t(lang, "draw_postponed",
          when=timeutils.format_draw_time(new_draw_at),
          countdown=timeutils.format_countdown(new_draw_at))
    )
    # Refresh the pinned message so the group always sees the exact time.
    text = build_next_round_text(group, group["current_period"], lang, group.get("seed_hash") or "", new_draw_at)
    kb = await build_participation_kb(message.bot, lang, group_id=group["_id"])
    await announce_and_pin(message.bot, group["chat_id"], group["_id"], text, reply_markup=kb)


@router.message(Command("payout", "markpaid"))
async def cmd_payout(message: Message):
    """Admin: confirm the drawn winner actually received the pool, then open
    the next round (auto-restarting a fresh cycle if the group is set to)."""
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group or group["status"] != "active":
        await message.answer(t(lang, "no_active_group"))
        return

    period = group["current_period"]
    payout = await repo.get_payout(group["_id"], period, group.get("cycle_number"))
    recipient = await repo.get_member(group["_id"], payout["telegram_id"]) if payout else None

    try:
        result = await equb_service.mark_payout_and_advance(group, message.from_user.id)
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    amount = payout["amount"] if payout else 0
    text = build_payout_announcement_text(group, period, recipient, result["winner_id"], amount, lang)
    send_kwargs = {}
    if config.PAYOUT_MESSAGE_EFFECT_ID:
        send_kwargs["message_effect_id"] = config.PAYOUT_MESSAGE_EFFECT_ID
    try:
        await message.answer(text, **send_kwargs)
    except Exception:
        await message.answer(text)

    try:
        await message.bot.send_message(
            result["winner_id"],
            t(lang, "dm_payout_received", name=group["name"], amount=amount, currency=group["currency"]),
        )
    except Exception:
        pass

    if result["completed"] and result.get("restarted"):
        # Auto-restart: a brand-new cycle for the same members starts now.
        text = build_next_round_text(
            group, 1, lang, result["seed_hash"], result["draw_at"], cycle_number=result["cycle_number"]
        )
        kb = await build_participation_kb(message.bot, lang, group_id=group["_id"])
        await announce_and_pin(message.bot, group["chat_id"], group["_id"], text, reply_markup=kb)
        when = timeutils.format_draw_time(result["draw_at"])
        members = await repo.list_members(group["_id"], active_only=True)
        for m in members:
            try:
                await message.bot.send_message(
                    m["telegram_id"],
                    t(lang, "dm_new_cycle", name=group["name"], cycle=result["cycle_number"],
                      amount=group["contribution_amount"], currency=group["currency"], when=when),
                )
            except Exception:
                pass
        return

    if result["completed"]:
        await message.answer(t(lang, "cycle_completed", name=group["name"]))
        return

    if result.get("legacy"):
        await message.answer(t(lang, "period_advanced", period=result["next_period"], total=len(group["order"])))
        return

    # Next round opens: announce it and pin the exact next draw time.
    text = build_next_round_text(group, result["next_period"], lang, result["seed_hash"], result["draw_at"])
    kb = await build_participation_kb(message.bot, lang, group_id=group["_id"])
    await announce_and_pin(message.bot, group["chat_id"], group["_id"], text, reply_markup=kb)

    when = timeutils.format_draw_time(result["draw_at"])
    members = await repo.list_members(group["_id"], active_only=True)
    for m in members:
        try:
            await message.bot.send_message(
                m["telegram_id"],
                t(lang, "dm_new_period", name=group["name"], period=result["next_period"],
                  amount=group["contribution_amount"], currency=group["currency"], when=when),
            )
        except Exception:
            pass


@router.message(Command("remind",))
async def cmd_remind(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group or group["status"] != "active":
        await message.answer(t(lang, "no_active_group"))
        return

    contributions = await repo.get_contributions_for_period(
        group["_id"], group["current_period"], group.get("cycle_number")
    )
    unpaid = [c for c in contributions if c["status"] not in ("verified",)]

    if not unpaid:
        await message.answer(t(lang, "everyone_contributed"))
        return

    sent = 0
    for c in unpaid:
        member_lang = await repo.get_chat_language(c["telegram_id"])
        try:
            await message.bot.send_message(
                c["telegram_id"],
                t(member_lang, "dm_reminder", name=group["name"], period=group["current_period"],
                  amount=c["amount"], currency=group["currency"]),
            )
            sent += 1
        except Exception:
            pass

    await message.answer(t(lang, "reminders_sent", count=sent))


@router.message(Command("history",))
async def cmd_history(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    payouts = await repo.list_payouts(group["_id"])
    if not payouts:
        await message.answer(t(lang, "no_payout_history"))
        return

    lines = [t(lang, "history_title", name=group["name"])]
    for p in payouts:
        member = await repo.get_member(group["_id"], p["telegram_id"])
        who = format_user_identity(
            member.get("display_name") if member else None,
            member.get("username") if member else None,
            p["telegram_id"],
        )
        state = t(lang, "payout_paid" if p["status"] == "paid" else "payout_pending")
        cycle = p.get("cycle_number") or 1
        period_label = f"{cycle}-{p['period']}" if cycle > 1 else str(p["period"])
        lines.append(t(lang, "history_line", period=period_label, who=who, amount=p["amount"],
                       currency=group["currency"], state=state))

    await message.answer("\n".join(lines))
