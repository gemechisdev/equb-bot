import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from core import config
from core import timeutils
from core.i18n import t
from core.keyboards import build_confirm_kb, build_participation_kb
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


async def _safe_dm(bot, telegram_id: int, text: str, *, celebrate: bool = False) -> bool:
    """Send a DM; log failures instead of swallowing silently.
    When celebrate=True, attach the win message effect (private chats only)."""
    try:
        if celebrate:
            from core.effects import send_with_optional_effect
            await send_with_optional_effect(bot, telegram_id, text)
        else:
            await bot.send_message(telegram_id, text)
        return True
    except Exception:
        logger.warning("DM failed to user %s", telegram_id, exc_info=True)
        return False


async def _finish_payout_side_effects(bot, group: dict, period: int, payout: dict | None, result: dict, lang: str, answer):
    """Shared announcements after a successful mark_payout_and_advance / reconcile."""
    amount = payout["amount"] if payout else 0
    recipient = await repo.get_member(group["_id"], result["winner_id"]) if result.get("winner_id") else None
    text = build_payout_announcement_text(group, period, recipient, result["winner_id"], amount, lang)
    # Group chats often reject message_effect_id — post plain text here.
    await answer(text)

    # Celebrate the winner in private (effects work in DMs).
    await _safe_dm(
        bot,
        result["winner_id"],
        t(lang, "dm_payout_received", name=group["name"], amount=amount, currency=group["currency"]),
        celebrate=True,
    )

    if result["completed"] and result.get("restarted"):
        text = build_next_round_text(
            group, 1, lang, result["seed_hash"], result["draw_at"], cycle_number=result["cycle_number"]
        )
        kb = await build_participation_kb(bot, lang, group_id=group["_id"])
        await announce_and_pin(bot, group["chat_id"], group["_id"], text, reply_markup=kb)
        when = timeutils.format_draw_time(result["draw_at"])
        members = await repo.list_members(group["_id"], active_only=True)
        for m in members:
            await _safe_dm(
                bot,
                m["telegram_id"],
                t(lang, "dm_new_cycle", name=group["name"], cycle=result["cycle_number"],
                  amount=group["contribution_amount"], currency=group["currency"], when=when),
            )
        return

    if result["completed"]:
        await answer(t(lang, "cycle_completed", name=group["name"]))
        return

    if result.get("legacy"):
        await answer(t(lang, "period_advanced", period=result["next_period"], total=len(group.get("order") or [])))
        return

    text = build_next_round_text(group, result["next_period"], lang, result["seed_hash"], result["draw_at"])
    kb = await build_participation_kb(bot, lang, group_id=group["_id"])
    await announce_and_pin(bot, group["chat_id"], group["_id"], text, reply_markup=kb)

    when = timeutils.format_draw_time(result["draw_at"])
    members = await repo.list_members(group["_id"], active_only=True)
    for m in members:
        await _safe_dm(
            bot,
            m["telegram_id"],
            t(lang, "dm_new_period", name=group["name"], period=result["next_period"],
              amount=group["contribution_amount"], currency=group["currency"], when=when),
        )


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
    """Admin: ask for confirmation, then mark the drawn winner paid and advance."""
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group or group["status"] != "active":
        await message.answer(t(lang, "no_active_group"))
        return

    period = group["current_period"]
    payout = await repo.get_payout(group["_id"], period, group.get("cycle_number"))
    if not payout:
        await message.answer(t(lang, "error_no_payout_record"))
        return
    if payout.get("status") == "paid":
        await message.answer(t(lang, "error_already_paid"))
        return

    member = await repo.get_member(group["_id"], payout["telegram_id"])
    who = format_user_identity(
        member.get("display_name") if member else None,
        member.get("username") if member else None,
        payout["telegram_id"],
    )
    await message.answer(
        t(
            lang, "confirm_payout_prompt",
            name=group["name"], period=period, who=who,
            amount=payout.get("amount", 0), currency=group.get("currency", ""),
        ),
        reply_markup=build_confirm_kb("payout", group["_id"], lang),
    )


@router.callback_query(F.data.startswith("conf:"))
async def cb_confirm_action(callback: CallbackQuery):
    """Handles confirm/abort for payout and group cancel."""
    lang = await repo.get_chat_language(callback.message.chat.id)
    if not await _is_admin(callback.from_user.id):
        await callback.answer(t(lang, "admins_only"), show_alert=True)
        return

    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    _, action, group_id = parts

    if action == "abort":
        try:
            await callback.message.edit_text(t(lang, "confirm_aborted"))
        except Exception:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await callback.answer()
        return

    group = await repo.get_group(group_id)
    if not group:
        await callback.answer(t(lang, "no_active_group"), show_alert=True)
        return

    if action == "payout":
        if group.get("status") != "active":
            await callback.answer(t(lang, "no_active_group"), show_alert=True)
            return
        period = group["current_period"]
        payout = await repo.get_payout(group["_id"], period, group.get("cycle_number"))
        try:
            result = await equb_service.mark_payout_and_advance(group, callback.from_user.id)
        except EqubError as e:
            try:
                await callback.message.edit_text(_error_text(lang, e))
            except Exception:
                pass
            await callback.answer()
            return

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer()

        async def answer(text, **kwargs):
            try:
                await callback.message.answer(text, **kwargs)
            except Exception:
                await callback.message.answer(text)

        await _finish_payout_side_effects(
            callback.bot, group, period, payout, result, lang, answer,
        )
        return

    if action == "cancel":
        if group.get("status") in ("completed", "cancelled"):
            await callback.answer(t(lang, "error_group_already_terminal"), show_alert=True)
            return
        try:
            summary = await equb_service.cancel_group(group, actor_id=callback.from_user.id)
        except EqubError as e:
            try:
                await callback.message.edit_text(_error_text(lang, e))
            except Exception:
                pass
            await callback.answer()
            return

        lines = [t(lang, "group_cancelled", name=summary["name"])]
        lines.append(t(
            lang, "group_cancelled_summary",
            open_count=summary["open_contributions_cancelled"],
            verified_count=summary["verified_left_intact"],
        ))
        if summary.get("unpaid_winner"):
            uw = summary["unpaid_winner"]
            member = await repo.get_member(group["_id"], uw["telegram_id"])
            who = format_user_identity(
                member.get("display_name") if member else None,
                member.get("username") if member else None,
                uw["telegram_id"],
            )
            lines.append(t(
                lang, "group_cancelled_unpaid_winner",
                who=who, amount=uw.get("amount") or 0, currency=group.get("currency", ""),
                period=uw.get("period"),
            ))
        try:
            await callback.message.edit_text("\n".join(lines))
        except Exception:
            await callback.message.answer("\n".join(lines))
        await callback.answer()
        return

    await callback.answer()


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


@router.message(Command("verify",))
async def cmd_verify(message: Message):
    """Re-run the lottery math for a drawn round so anyone can check fairness.

    Usage:
      /verify              — latest drawn round of the active group
      /verify 3            — period 3 of the current cycle
      /verify 2 3          — cycle 2, period 3
    """
    lang = await repo.get_chat_language(message.chat.id)
    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        # Fall back to the most recent group in this chat (completed cycles).
        groups = await repo.list_groups(message.chat.id)
        group = groups[-1] if groups else None
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    parts = (message.text or "").split()
    cycle = group.get("cycle_number", 1)
    period = None
    if len(parts) == 2:
        try:
            period = int(parts[1])
        except ValueError:
            await message.answer(t(lang, "usage_verify"))
            return
    elif len(parts) >= 3:
        try:
            cycle = int(parts[1])
            period = int(parts[2])
        except ValueError:
            await message.answer(t(lang, "usage_verify"))
            return

    if period is None:
        # Latest drawn payout for this group.
        payouts = await repo.list_payouts(group["_id"])
        if not payouts:
            await message.answer(t(lang, "verify_no_draw"))
            return
        payout = payouts[-1]
        cycle = payout.get("cycle_number") or 1
        period = payout["period"]
    else:
        payout = await repo.get_payout(group["_id"], period, cycle)
        if not payout:
            await message.answer(t(lang, "verify_no_draw_period", period=period, cycle=cycle))
            return

    result = draw_service.verify_payout_record(payout)
    member = await repo.get_member(group["_id"], payout["telegram_id"])
    who = format_user_identity(
        member.get("display_name") if member else None,
        member.get("username") if member else None,
        payout["telegram_id"],
    )
    ids = ", ".join(str(i) for i in result["eligible_ids"])
    if result["ok"]:
        await message.answer(t(
            lang, "verify_ok",
            name=group["name"], cycle=cycle, period=period,
            who=who, seed=payout.get("seed") or "",
            hash=result["stored_hash"], ids=ids,
        ))
    else:
        await message.answer(t(
            lang, "verify_fail",
            name=group["name"], cycle=cycle, period=period,
            who=who, errors=", ".join(result["errors"]),
            seed=payout.get("seed") or "?",
            hash=result["stored_hash"] or "?",
            expected_hash=result["expected_hash"] or "?",
            recomputed=result["recomputed_winner"] if result["recomputed_winner"] is not None else "?",
            ids=ids,
        ))


@router.message(Command("statement",))
async def cmd_statement(message: Message):
    """Cycle financial statement: expected vs verified vs disbursed."""
    from services import ledger_service

    lang = await repo.get_chat_language(message.chat.id)
    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        groups = await repo.list_groups(message.chat.id)
        group = groups[-1] if groups else None
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    parts = (message.text or "").split()
    cycle = group.get("cycle_number", 1)
    as_csv = False
    for token in parts[1:]:
        low = token.lower()
        if low in ("csv", "export"):
            as_csv = True
        else:
            try:
                cycle = int(token)
            except ValueError:
                await message.answer(t(lang, "usage_statement"))
                return

    stmt = await ledger_service.build_cycle_statement(group, cycle)
    pv = stmt["promised_vs_received"]

    if as_csv:
        import html as _html
        # Plain CSV-ish text for copy/paste into sheets (no file upload).
        gname = (stmt["group_name"] or "").replace(",", " ")
        rows = [
            "section,key,value",
            f"meta,group,{gname}",
            f"meta,cycle,{cycle}",
            f"meta,currency,{stmt['currency']}",
            f"totals,expected_in,{pv['expected_in']}",
            f"totals,verified_in,{pv['verified_in']}",
            f"totals,disbursed_out,{pv['disbursed_out']}",
            f"totals,pending_out,{pv['pending_out']}",
            "member,telegram_id,verified,expected,open,received_pool",
        ]
        for m in stmt["members"]:
            open_n = m["pending"] + m["awaiting_review"] + m["rejected"]
            rows.append(
                f"member,{m['telegram_id']},{m['verified']},{m['expected']},{open_n},"
                f"{'yes' if m.get('received_payout') else 'no'}"
            )
        rows.append("payout,period,telegram_id,amount,status")
        for p in stmt["payouts"]:
            rows.append(f"payout,{p['period']},{p['telegram_id']},{p['amount']},{p['status']}")
        body = _html.escape("\n".join(rows))
        await message.answer(f"<pre>{body}</pre>")
        return

    lines = [
        t(lang, "statement_title", name=stmt["group_name"], cycle=cycle),
        t(
            lang, "statement_totals",
            expected=pv["expected_in"],
            verified=pv["verified_in"],
            disbursed=pv["disbursed_out"],
            pending_out=pv["pending_out"],
            currency=stmt["currency"],
        ),
        "",
        t(lang, "statement_members_header"),
    ]
    for m in stmt["members"]:
        who = format_user_identity(m.get("display_name"), m.get("username"), m["telegram_id"])
        lines.append(t(
            lang, "statement_member_line",
            who=who,
            verified=m["verified"],
            expected=m["expected"],
            pending=m["pending"] + m["awaiting_review"] + m["rejected"],
            received="✓" if m.get("received_payout") else "—",
        ))

    if stmt["payouts"]:
        lines.append("")
        lines.append(t(lang, "statement_payouts_header"))
        for p in stmt["payouts"]:
            who = format_user_identity(p.get("display_name"), p.get("username"), p["telegram_id"])
            state = t(lang, "payout_paid" if p["status"] == "paid" else "payout_pending")
            lines.append(t(
                lang, "statement_payout_line",
                period=p["period"], who=who, amount=p["amount"],
                currency=stmt["currency"], state=state,
            ))

    await message.answer("\n".join(lines))


@router.message(Command("unverify",))
async def cmd_unverify(message: Message):
    """Admin correction: reverse a verified contribution before the round is drawn.

    Usage: /unverify <telegram_id> [period] [reason...]
    Or reply to a member's message: /unverify [period] [reason...]
    """
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group or group["status"] != "active":
        await message.answer(t(lang, "no_active_group"))
        return

    parts = (message.text or "").split()
    target_id = None
    period = group["current_period"]
    reason_parts = []

    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        # /unverify [period] [reason...]
        idx = 1
        if len(parts) > 1 and parts[1].isdigit():
            period = int(parts[1])
            idx = 2
        reason_parts = parts[idx:]
    else:
        if len(parts) < 2:
            await message.answer(t(lang, "usage_unverify"))
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer(t(lang, "usage_unverify"))
            return
        idx = 2
        if len(parts) > 2 and parts[2].isdigit():
            period = int(parts[2])
            idx = 3
        reason_parts = parts[idx:]

    reason = " ".join(reason_parts).strip()
    cycle = group.get("cycle_number", 1)

    # Block if this period already has a draw (payout record exists).
    payout = await repo.get_payout(group["_id"], period, cycle)
    if payout:
        await message.answer(t(lang, "unverify_already_drawn", period=period))
        return

    contribution = await repo.get_contribution(group["_id"], period, target_id, cycle_number=cycle)
    if not contribution:
        await message.answer(t(lang, "unverify_not_found"))
        return
    if contribution.get("status") != "verified":
        await message.answer(t(lang, "unverify_not_verified", status=contribution.get("status", "?")))
        return

    updated = await repo.reverse_contribution_verification(
        contribution["_id"], message.from_user.id, reason=reason,
    )
    if not updated:
        await message.answer(t(lang, "unverify_failed"))
        return

    try:
        await repo.log_audit(
            "contribution_reversed",
            actor_id=message.from_user.id,
            group_id=group["_id"],
            chat_id=group["chat_id"],
            target_telegram_id=target_id,
            contribution_id=contribution["_id"],
            period=period,
            cycle_number=cycle,
            details={"reason": reason},
        )
    except Exception:
        pass

    member = await repo.get_member(group["_id"], target_id)
    who = format_user_identity(
        member.get("display_name") if member else None,
        member.get("username") if member else None,
        target_id,
    )
    await message.answer(t(lang, "unverify_ok", who=who, period=period))

    try:
        member_lang = await repo.get_chat_language(target_id)
        await message.bot.send_message(
            target_id,
            t(member_lang, "dm_contribution_reversed",
              name=group["name"], period=period, reason=reason or "—"),
        )
    except Exception:
        pass


@router.message(Command("auditlog", "audit"))
async def cmd_auditlog(message: Message):
    """Recent audit events for this group's Equb (admin)."""
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        groups = await repo.list_groups(message.chat.id)
        group = groups[-1] if groups else None
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    events = await repo.list_audit_events(group["_id"], limit=20)
    if not events:
        await message.answer(t(lang, "audit_empty"))
        return

    lines = [t(lang, "audit_title", name=group["name"])]
    for e in events:
        when = e.get("created_at")
        when_s = when.strftime("%Y-%m-%d %H:%M") if when else "?"
        lines.append(t(
            lang, "audit_line",
            when=when_s,
            action=e.get("action", "?"),
            actor=e.get("actor_id") or "—",
            target=e.get("target_telegram_id") or "—",
            period=e.get("period") if e.get("period") is not None else "—",
        ))
    await message.answer("\n".join(lines))


@router.message(Command("reconcile",))
async def cmd_reconcile(message: Message):
    """Admin recovery: payout already marked paid but cycle did not advance."""
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group or group["status"] != "active":
        await message.answer(t(lang, "no_active_group"))
        return

    period = group["current_period"]
    payout = await repo.get_payout(group["_id"], period, group.get("cycle_number"))
    try:
        result = await equb_service.reconcile_stuck_payout(group, message.from_user.id)
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    await message.answer(t(lang, "reconcile_ok", period=result.get("paid_period")))
    await _finish_payout_side_effects(
        message.bot, group, period, payout, result, lang, message.answer,
    )
