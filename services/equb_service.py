"""Business logic for the Equb rotating-savings cycle. Transport-agnostic
(no aiogram types here except where we need `bot` to send DMs) so it can be
reused from a future REST layer / Mini App the same way the router layer
uses it today.

Every Equb is a lottery: rounds have no predefined order, the winner is
drawn at the scheduled draw time among members who VERIFIED their payment
for that round (and haven't received their pool yet this cycle), and the
outcome is secret to everyone — including admins — until the draw fires.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core import timeutils
from db import repository as repo
from services import draw_service

RESTART_MODES = ("once", "auto")


class EqubError(Exception):
    """Raised for expected, user-facing validation failures."""


def parse_newequb_args(raw: str) -> dict:
    """Parse '/newequb Name | amount | frequency | [currency] | [restart]'.
    Pure function; raises EqubError with an i18n error-code suffix."""
    parts = [p.strip() for p in (raw or "").split("|")]
    if len(parts) < 3 or not parts[0]:
        raise EqubError("usage_newequb")

    try:
        amount = int(parts[1])
    except ValueError:
        raise EqubError("usage_newequb")
    if amount <= 0:
        raise EqubError("invalid_amount")

    frequency = parts[2].lower()
    if any(p.lower() in ("sequential", "random") for p in parts[2:]):
        # Pre-lottery syntax ('... | random' / '... | sequential' in any
        # later slot) — fail with a clear message instead of silently
        # treating it as a currency.
        raise EqubError("order_mode_removed")

    try:
        interval_days = timeutils.parse_frequency_interval(frequency)
    except ValueError:
        raise EqubError("invalid_frequency")

    currency = parts[3] if len(parts) > 3 and parts[3] else "ETB"
    restart_mode = parts[4].lower() if len(parts) > 4 and parts[4] else "once"
    if restart_mode not in RESTART_MODES:
        raise EqubError("invalid_restart_mode")

    return {
        "name": parts[0],
        "amount": amount,
        "frequency": frequency,
        "interval_days": interval_days,
        "currency": currency,
        "restart_mode": restart_mode,
    }


async def create_group(chat_id: int, name: str, amount: int, currency: str, frequency: str, interval_days: int, restart_mode: str, creator: dict) -> dict:
    existing = await repo.get_active_or_open_group(chat_id)
    if existing:
        raise EqubError("group_already_exists")

    group = await repo.create_group(
        chat_id=chat_id,
        name=name,
        contribution_amount=amount,
        currency=currency,
        frequency=frequency,
        interval_days=interval_days,
        restart_mode=restart_mode,
        created_by=creator["telegram_id"],
    )
    # The organizer is auto-enrolled as the first member.
    await repo.add_member(group["_id"], creator["telegram_id"], creator.get("username"), creator.get("display_name"))
    return group


async def join_group(group: dict, member: dict) -> dict:
    """Self-join. Open groups: they're in immediately. Active cycles: they
    join mid-cycle and must back-pay every round since the cycle started
    before they can win one (see _add_member_to_cycle)."""
    if group["status"] not in ("open", "active"):
        raise EqubError("group_not_open")
    existing = await repo.get_member(group["_id"], member["telegram_id"])
    if existing and existing["status"] == "active":
        raise EqubError("already_member")
    if group["status"] == "open":
        new_doc = await repo.add_member(group["_id"], member["telegram_id"], member.get("username"), member.get("display_name"))
        return {"member": new_doc, "backpay_periods": []}
    return await _add_member_to_cycle(group, member)


async def admin_add_member(group: dict, member: dict) -> dict:
    """Admin adds a specific person — same rules as self-join: immediate on
    an open group, back-pay mid-cycle join on an active one."""
    if group["status"] not in ("open", "active"):
        raise EqubError("group_not_open")
    if group["status"] == "open":
        existing = await repo.get_member(group["_id"], member["telegram_id"])
        if existing and existing["status"] == "active":
            raise EqubError("already_member")
        if existing:  # previously removed — reactivate
            await repo.reactivate_member(group["_id"], member["telegram_id"], None)
            new_doc = await repo.get_member(group["_id"], member["telegram_id"])
        else:
            new_doc = await repo.add_member(
                group["_id"], member["telegram_id"], member.get("username"), member.get("display_name")
            )
        return {"member": new_doc, "backpay_periods": []}
    return await _add_member_to_cycle(group, member)


async def _add_member_to_cycle(group: dict, member: dict) -> dict:
    """Add someone to an ALREADY-RUNNING cycle. They contribute every round
    from now on, and — to keep the lottery fair — must back-pay every round
    the cycle has already run before they're eligible for any draw. Without
    this, someone could join after everyone else had received their pool
    and win a full pool having paid only one contribution."""
    # A 'once' cycle whose current round is already drawn has no future
    # round left for a joiner — they'd back-pay into a cycle that's about
    # to complete. Auto-restart groups are fine: a fresh cycle follows.
    if group.get("draw_state") == "drawn" and group.get("restart_mode") != "auto":
        raise EqubError("join_cycle_ending")

    telegram_id = member["telegram_id"]
    existing = await repo.get_member(group["_id"], telegram_id)
    if existing and existing["status"] == "active":
        raise EqubError("already_member")

    joined_period = group["current_period"]
    if existing:  # previously removed — reactivate
        await repo.reactivate_member(group["_id"], telegram_id, joined_period)
        new_doc = await repo.get_member(group["_id"], telegram_id)
    else:
        new_doc = await repo.add_member(
            group["_id"], telegram_id, member.get("username"), member.get("display_name"),
            joined_period=joined_period,
        )

    # Back-pay: pending contributions for rounds 1..joined_period of this
    # cycle. (Round docs for settled rounds were only created for members
    # who were in the group at the time, so the joiner gets fresh ones —
    # skipping rounds they already have a doc for, e.g. after a re-join.)
    cycle = group.get("cycle_number", 1)
    backpay_periods = []
    for period in range(1, joined_period + 1):
        already = await repo.get_contribution(group["_id"], period, telegram_id, cycle_number=cycle)
        if already:
            continue
        await repo.create_contribution(group["_id"], cycle, period, member, group["contribution_amount"])
        backpay_periods.append(period)

    return {"member": new_doc, "backpay_periods": backpay_periods}


async def leave_or_remove_member(group: dict, telegram_id: int) -> None:
    """Self-leave (open groups only) or admin removal. Mid-cycle removal is
    allowed unless the member already received their pool or is the current
    round's drawn-but-unpaid winner (they're owed / owe the pool)."""
    if group["status"] == "open":
        removed = await repo.remove_member(group["_id"], telegram_id)
        if not removed:
            raise EqubError("not_a_member")
        return

    if group["status"] != "active":
        raise EqubError("group_not_open")

    member = await repo.get_member(group["_id"], telegram_id)
    if not member or member["status"] != "active":
        raise EqubError("not_a_member")
    if member.get("received_payout"):
        raise EqubError("member_already_paid_out")

    payout = await repo.get_payout(group["_id"], group["current_period"], group.get("cycle_number", 1))
    if payout and payout["telegram_id"] == telegram_id and payout["status"] != "paid":
        raise EqubError("member_is_current_winner")

    await repo.remove_member(group["_id"], telegram_id)


def _next_draw_at(interval_days: int) -> datetime:
    return timeutils.next_draw_at(datetime.now(timezone.utc), interval_days)


async def start_cycle(group: dict):
    """Locks membership, opens period 1 and schedules the first draw one
    interval out. No winner or order exists at this point — only the
    commit hash of the round's seed, published for later verification."""
    if group["status"] != "open":
        raise EqubError("group_not_open")

    members = await repo.list_members(group["_id"], active_only=True)
    if len(members) < 2:
        raise EqubError("not_enough_members")

    seed, seed_hash = draw_service.new_period_seed()
    draw_at = _next_draw_at(group["interval_days"])
    await repo.start_group_cycle(group["_id"], seed, seed_hash, draw_at)
    await repo.create_period_contributions(group["_id"], 1, members, group["contribution_amount"], cycle_number=1)

    return {
        "seed_hash": seed_hash,
        "draw_at": draw_at,
        "members": members,
        "members_by_id": {m["telegram_id"]: m for m in members},
    }


async def mark_payout_and_advance(group: dict, admin_id: int) -> dict:
    """Admin confirmed the drawn winner received the pool. Marks the payout
    paid, then either opens the next round (new contributions + a freshly
    scheduled draw), completes the cycle, or — when the group was created
    with restart=auto — immediately starts a brand-new cycle for the same
    members.

    Legacy support: groups that started a fixed-order cycle before the
    lottery upgrade (they have an `order` and no `draw_at`) still advance
    sequentially so an in-flight Equb isn't broken by the upgrade."""
    if group["status"] != "active":
        raise EqubError("group_not_active")

    period = group["current_period"]
    cycle = group.get("cycle_number", 1)
    payout = await repo.get_payout(group["_id"], period, cycle)
    if not payout:
        raise EqubError("no_payout_record")
    if payout["status"] == "paid":
        raise EqubError("already_paid")

    await repo.mark_payout_paid(group["_id"], period, admin_id, cycle)
    await repo.mark_member_paid_out(group["_id"], payout["telegram_id"], period)

    legacy = bool(group.get("order")) and not group.get("draw_at")
    if legacy:
        return await _advance_legacy(group, period, payout)

    unreceived = await repo.list_unreceived_members(group["_id"])
    members = await repo.list_members(group["_id"], active_only=True)

    if unreceived:
        next_period = period + 1
        seed, seed_hash = draw_service.new_period_seed()
        draw_at = _next_draw_at(group["interval_days"])
        await repo.advance_group_period(group["_id"], next_period)
        await repo.set_draw_schedule(group["_id"], seed, seed_hash, draw_at)
        await repo.create_period_contributions(
            group["_id"], next_period, members, group["contribution_amount"],
            cycle_number=group.get("cycle_number", 1),
        )
        return {
            "completed": False,
            "legacy": False,
            "paid_period": period,
            "winner_id": payout["telegram_id"],
            "next_period": next_period,
            "draw_at": draw_at,
            "seed_hash": seed_hash,
            "member_count": len(members),
        }

    if group.get("restart_mode") == "auto" and len(members) >= 2:
        seed, seed_hash = draw_service.new_period_seed()
        draw_at = _next_draw_at(group["interval_days"])
        cycle_number = await repo.restart_group_cycle(group["_id"], seed, seed_hash, draw_at)
        await repo.create_period_contributions(group["_id"], 1, members, group["contribution_amount"], cycle_number=cycle_number)
        return {
            "completed": True,
            "restarted": True,
            "legacy": False,
            "paid_period": period,
            "winner_id": payout["telegram_id"],
            "cycle_number": cycle_number,
            "draw_at": draw_at,
            "seed_hash": seed_hash,
            "member_count": len(members),
        }

    await repo.complete_group(group["_id"])
    return {
        "completed": True,
        "restarted": False,
        "legacy": False,
        "paid_period": period,
        "winner_id": payout["telegram_id"],
    }


async def _advance_legacy(group: dict, period: int, payout: dict) -> dict:
    """Pre-lottery fixed-order groups: keep the old sequential behavior so
    cycles that were already running when the bot was upgraded complete."""
    order = group["order"]
    next_period = period + 1
    if next_period > len(order):
        await repo.complete_group(group["_id"])
        return {"completed": True, "legacy": True, "paid_period": period, "winner_id": payout["telegram_id"]}

    members = await repo.list_members(group["_id"], active_only=True)
    await repo.create_period_contributions(group["_id"], next_period, members, group["contribution_amount"])
    next_recipient = order[next_period - 1]
    await repo.create_payout_record(group["_id"], next_period, next_recipient, group["contribution_amount"] * len(members))
    await repo.advance_group_period(group["_id"], next_period)
    return {
        "completed": False,
        "legacy": True,
        "paid_period": period,
        "winner_id": payout["telegram_id"],
        "next_period": next_period,
        "next_recipient_id": next_recipient,
    }


async def cancel_group(group: dict) -> None:
    if group["status"] in ("completed", "cancelled"):
        raise EqubError("group_already_terminal")
    await repo.set_group_status(group["_id"], "cancelled")


def period_progress(group: dict, contributions: list[dict]) -> tuple[int, int]:
    verified = sum(1 for c in contributions if c["status"] == "verified")
    return verified, len(contributions)
