"""Per-round lottery draws with commit/reveal fairness.

Every round works like a provably-fair lottery:

1. When a round opens, the bot generates a random `seed` and publishes (and
   pins) only its SHA-256 hash — the commitment.
2. During the round, members pay and admins verify. Nobody can predict the
   winner, not only because the seed is secret, but because the eligible
   set isn't final until the draw time: unpaid members can still pay in.
3. At the draw time, the winner is `sorted(eligible_ids)[Random(seed).randrange(n)]`
   among members who (a) are still active, (b) have a VERIFIED contribution
   for the round, and (c) haven't received their pool yet this cycle.
   The seed is revealed in the result so anyone can reproduce the pick and
   check it against the commitment hash published earlier.

If nobody has a verified payment when the draw fires, the round is marked
overdue and admins decide what happens next (/drawnow or /postpone).
"""

from __future__ import annotations

import hashlib
import random
import secrets

from db import repository as repo
from core.i18n import t
from core.texts import build_draw_result_text
from core.pinning import announce_and_pin


def new_period_seed() -> tuple[str, str]:
    """Returns (seed, seed_hash). The hash is safe to publish immediately;
    the seed must stay secret until the draw."""
    seed = secrets.token_hex(16)
    return seed, hashlib.sha256(seed.encode()).hexdigest()


def pick_winner(seed: str, eligible_ids: list[int]) -> int:
    """Deterministic given (seed, eligible list): anyone can re-run this
    with the revealed seed to verify the outcome. Sorting the ids first
    makes the mapping independent of DB ordering."""
    ids = sorted(eligible_ids)
    if not ids:
        raise ValueError("no eligible members to draw among")
    rng = random.Random(seed)
    return ids[rng.randrange(len(ids))]


async def compute_eligible_ids(group: dict, period: int) -> list[int]:
    """Active members who verified their payment this round (in this cycle)
    and haven't received their pool yet — the only people in the draw.

    Mid-cycle joiners (joined_period set for this cycle) additionally need
    EVERY contribution since the cycle started verified — their back-pay
    must be complete, otherwise someone could join after everyone else had
    received their pool and win a full pool having paid once."""
    members = await repo.list_members(group["_id"], active_only=True)
    verified = set(await repo.get_verified_member_ids(group["_id"], period, group.get("cycle_number")))
    cycle = group.get("cycle_number", 1)

    eligible = []
    for m in members:
        if m.get("received_payout") or m["telegram_id"] not in verified:
            continue
        if m.get("joined_period") and await repo.member_has_unverified_contributions(
            group["_id"], cycle, m["telegram_id"]
        ):
            continue
        eligible.append(m["telegram_id"])
    return sorted(eligible)


async def run_draw(bot, group: dict) -> dict:
    """Run the draw for a group that has been CLAIMED (draw_state=drawing).

    Returns a dict: {"drawn": bool, "overdue": bool, "winner_id": ...}.
    - 0 eligible members -> mark overdue, notify group + admins (once).
    - >=1 eligible       -> pick winner, create the payout record, announce
                            and pin the result, DM the winner.
    """
    group_id = group["_id"]
    chat_id = group["chat_id"]
    lang = await repo.get_chat_language(chat_id)
    period = group["current_period"]

    # Crash-recovery idempotency: if a previous attempt already created this
    # period's payout, don't create a second one.
    existing_payout = await repo.get_payout(group_id, period, group.get("cycle_number"))
    if existing_payout:
        await repo.set_group_drawn(group_id)
        return {"drawn": True, "overdue": False, "winner_id": existing_payout["telegram_id"]}

    eligible_ids = await compute_eligible_ids(group, period)

    if not eligible_ids:
        already_notified = bool(group.get("overdue_notified"))
        await repo.set_draw_overdue(group_id, notified=True)
        if not already_notified:
            await _notify_overdue(bot, group, lang, period)
        return {"drawn": False, "overdue": True, "winner_id": None}

    seed = group.get("seed") or ""
    seed_hash = group.get("seed_hash") or ""
    winner_id = pick_winner(seed, eligible_ids)

    members = await repo.list_members(group_id, active_only=True)
    pool = group["contribution_amount"] * len(members)
    winner = next((m for m in members if m["telegram_id"] == winner_id), None)

    await repo.create_payout_record(
        group_id, period, winner_id, pool,
        seed=seed, seed_hash=seed_hash, eligible_ids=eligible_ids,
        cycle_number=group.get("cycle_number", 1),
    )
    await repo.set_group_drawn(group_id)

    text = build_draw_result_text(group, period, winner, winner_id, pool, lang, seed, seed_hash, eligible_ids)
    await announce_and_pin(bot, chat_id, group_id, text)

    try:
        await bot.send_message(
            winner_id,
            t(lang, "dm_you_won", name=group["name"], period=period, amount=pool, currency=group["currency"]),
        )
    except Exception:
        pass

    return {"drawn": True, "overdue": False, "winner_id": winner_id, "pool": pool}


async def _notify_overdue(bot, group: dict, lang: str, period: int) -> None:
    """Draw time passed with zero verified payments: tell the group publicly
    and DM the bot admins, who decide via /drawnow or /postpone."""
    text = t(lang, "draw_overdue_group", name=group["name"], period=period)
    try:
        await bot.send_message(group["chat_id"], text)
    except Exception:
        pass

    admin_text = t(lang, "draw_overdue_admin_dm", name=group["name"], period=period)
    for admin_id in await repo.get_admins():
        try:
            await bot.send_message(admin_id, admin_text)
        except Exception:
            pass


async def notify_overdue_progress(bot, group: dict) -> None:
    """An overdue round now has at least one verified payment (someone paid
    after the deadline). Nudge the admins once — they decide whether to
    /drawnow or /postpone further."""
    group_id = group["_id"]
    lang = await repo.get_chat_language(group["chat_id"])
    eligible = await compute_eligible_ids(group, group["current_period"])
    if not eligible:
        return
    await repo.set_overdue_eligible_notified(group_id)
    text = t(
        lang,
        "overdue_eligible_admin_dm",
        name=group["name"],
        period=group["current_period"],
        count=len(eligible),
    )
    for admin_id in await repo.get_admins():
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass
