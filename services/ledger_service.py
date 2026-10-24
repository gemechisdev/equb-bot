"""Financial transparency helpers: cycle statements and contribution ledgers.

Transport-agnostic so Telegram, a future Mini App, or a dashboard can share
the same numbers.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from db import repository as repo


async def build_cycle_statement(group: dict, cycle_number: Optional[int] = None) -> dict:
    """Aggregate contributions and payouts for one cycle into a statement.

    Returns a structured dict (not formatted text) so callers can render
    Telegram messages, CSV, or a web view from the same source.
    """
    cycle = cycle_number if cycle_number is not None else group.get("cycle_number", 1)
    amount = group["contribution_amount"]
    currency = group.get("currency", "")

    contributions = await repo.list_contributions_for_cycle(group["_id"], cycle)
    payouts = await repo.list_payouts_for_cycle(group["_id"], cycle)
    members = await repo.list_members(group["_id"], active_only=False)

    members_by_id = {m["telegram_id"]: m for m in members}

    # Per-member rollup
    by_member: dict[int, dict] = defaultdict(lambda: {
        "telegram_id": 0,
        "expected": 0,
        "verified": 0,
        "pending": 0,
        "awaiting_review": 0,
        "rejected": 0,
        "verified_amount": 0,
        "periods_verified": [],
        "periods_open": [],
    })

    periods = set()
    status_counts = defaultdict(int)

    for c in contributions:
        tid = c["telegram_id"]
        st = c.get("status", "pending")
        period = c["period"]
        periods.add(period)
        status_counts[st] += 1

        row = by_member[tid]
        row["telegram_id"] = tid
        row["expected"] += 1
        if st == "verified":
            row["verified"] += 1
            row["verified_amount"] += c.get("amount", amount)
            row["periods_verified"].append(period)
        elif st == "pending":
            row["pending"] += 1
            row["periods_open"].append(period)
        elif st == "awaiting_review":
            row["awaiting_review"] += 1
            row["periods_open"].append(period)
        elif st == "rejected":
            row["rejected"] += 1
            row["periods_open"].append(period)

    member_rows = []
    for tid, row in sorted(by_member.items(), key=lambda x: x[0]):
        m = members_by_id.get(tid) or {}
        row["display_name"] = m.get("display_name")
        row["username"] = m.get("username")
        row["received_payout"] = bool(m.get("received_payout"))
        row["payout_period"] = m.get("payout_period")
        member_rows.append(row)

    payout_rows = []
    total_disbursed = 0
    total_pending_disbursement = 0
    for p in payouts:
        m = members_by_id.get(p["telegram_id"]) or {}
        paid = p.get("status") == "paid"
        if paid:
            total_disbursed += p.get("amount", 0)
        else:
            total_pending_disbursement += p.get("amount", 0)
        payout_rows.append({
            "period": p["period"],
            "telegram_id": p["telegram_id"],
            "display_name": m.get("display_name"),
            "username": m.get("username"),
            "amount": p.get("amount", 0),
            "status": p.get("status"),
            "seed_hash": p.get("seed_hash"),
            "eligible_count": len(p.get("eligible_ids") or []),
        })

    verified_count = status_counts.get("verified", 0)
    expected_count = len(contributions)

    return {
        "group_id": str(group["_id"]),
        "group_name": group.get("name"),
        "chat_id": group.get("chat_id"),
        "cycle_number": cycle,
        "currency": currency,
        "contribution_amount": amount,
        "periods": sorted(periods),
        "status_counts": dict(status_counts),
        "expected_contributions": expected_count,
        "verified_contributions": verified_count,
        "verified_amount_total": verified_count * amount,
        "members": member_rows,
        "payouts": payout_rows,
        "total_disbursed": total_disbursed,
        "total_pending_disbursement": total_pending_disbursement,
        "promised_vs_received": {
            "expected_in": expected_count * amount,
            "verified_in": verified_count * amount,
            "disbursed_out": total_disbursed,
            "pending_out": total_pending_disbursement,
        },
    }
