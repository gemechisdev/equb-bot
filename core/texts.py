"""HTML-safe text builders for every announcement the bot posts."""

import html

from core.i18n import t
from core.timeutils import format_countdown, format_draw_time

FREQUENCY_KEYS = {
    "weekly": "frequency_weekly",
    "biweekly": "frequency_biweekly",
    "monthly": "frequency_monthly",
}

STATUS_KEYS = {
    "open": "group_status_open",
    "active": "group_status_active",
    "completed": "group_status_completed",
    "cancelled": "group_status_cancelled",
}

CONTRIB_EMOJI = {
    "pending": "⚪",
    "awaiting_review": "🟡",
    "verified": "🟢",
    "rejected": "🔴",
}


def format_user_identity(display_name: str | None, username: str | None, telegram_id: int | None) -> str:
    """Return a standardized string: Display Name - @username - id
    Use '-' for a missing display name and 'None' for a missing username/id.
    HTML-escaped: every message is sent with HTML parse mode."""
    disp = html.escape(display_name) if display_name else "-"
    userpart = f"@{html.escape(username)}" if username else "None"
    idpart = str(telegram_id) if telegram_id else "None"
    return f"{disp} - {userpart} - {idpart}"


def winner_display(winner: dict | None, telegram_id: int) -> str:
    """HTML-safe, clickable mention of a drawn winner."""
    if winner and (winner.get("display_name") or winner.get("username")):
        name = html.escape(winner.get("display_name") or f"@{winner.get('username')}")
    else:
        name = str(telegram_id)
    return f'<a href="tg://user?id={telegram_id}">{name}</a>'


def frequency_display(lang: str, frequency: str | None, interval_days: int | None) -> str:
    key = FREQUENCY_KEYS.get(frequency or "")
    if key:
        return t(lang, key)
    return t(lang, "frequency_every_n_days", n=interval_days if interval_days else "?")


def build_group_summary_line(group: dict, lang: str) -> str:
    return t(
        lang,
        "group_summary_line",
        number=group["group_number"],
        name=html.escape(group["name"]),
        status=t(lang, STATUS_KEYS.get(group["status"], "group_status_open")),
        amount=group["contribution_amount"],
        currency=group["currency"],
        frequency=frequency_display(lang, group.get("frequency"), group.get("interval_days")),
    )


def build_members_text(group: dict, members: list[dict], lang: str) -> str:
    lines = [t(lang, "members_title", name=html.escape(group["name"]), count=len(members))]
    active_cycle = group["status"] == "active"
    for i, m in enumerate(members, start=1):
        who = format_user_identity(m.get("display_name"), m.get("username"), m["telegram_id"])
        extra = ""
        if m.get("received_payout"):
            extra = t(lang, "members_line_received", period=m.get("payout_period"))
        elif active_cycle and m.get("joined_period"):
            # Joined mid-cycle: must back-pay rounds 1..joined_period before
            # they can win a draw.
            extra = t(lang, "members_line_backpay", period=m.get("joined_period"))
        lines.append(t(lang, "members_line", index=i, who=who, extra=extra))
    return "\n".join(lines)


def _received_so_far_lines(group: dict, members: list[dict], payouts: list[dict], lang: str) -> list[str]:
    if not payouts:
        return []
    members_by_id = {m["telegram_id"]: m for m in members}
    lines = ["", t(lang, "status_received_header")]
    for p in sorted(payouts, key=lambda x: (x.get("cycle_number") or 1, x["period"])):
        m = members_by_id.get(p["telegram_id"])
        who = format_user_identity(
            m.get("display_name") if m else None,
            m.get("username") if m else None,
            p["telegram_id"],
        )
        cycle = p.get("cycle_number") or 1
        period_label = f"{cycle}-{p['period']}" if cycle > 1 else str(p["period"])
        lines.append(t(lang, "status_received_line", period=period_label, who=who))
    return lines


def build_status_text(
    group: dict,
    members: list[dict],
    contributions: list[dict],
    payout: dict | None,
    lang: str,
    payouts: list[dict] | None = None,
) -> str:
    members_by_id = {m["telegram_id"]: m for m in members}
    currency = group["currency"]

    title_number = group["group_number"]
    if group.get("cycle_number", 0) > 1:
        title_number = f"{group['group_number']} — cycle {group['cycle_number']}"

    lines = [
        t(lang, "status_title", name=html.escape(group["name"]), number=title_number),
        t(lang, "status_state", status=t(lang, STATUS_KEYS.get(group["status"], "group_status_open"))),
        t(lang, "status_amount", amount=group["contribution_amount"], currency=currency),
        t(lang, "status_frequency", frequency=frequency_display(lang, group.get("frequency"), group.get("interval_days"))),
    ]

    if group["status"] == "open":
        lines.append(t(lang, "status_members_open", count=len(members)))
        lines.append("")
        lines.append(t(lang, "status_waiting_to_start"))
        return "\n".join(lines)

    # Legacy: pre-lottery group that started a fixed-order cycle before the
    # upgrade. It has an `order` and no draw schedule — show the recipient
    # like the old bot did.
    legacy = bool(group.get("order")) and not group.get("draw_at")

    if group["status"] == "active":
        pool = group["contribution_amount"] * len(members)
        lines.append(t(lang, "status_pool", amount=pool, currency=currency, count=len(members)))
        lines.append("")

        if legacy:
            total_periods = len(group["order"])
            recipient_id = payout["telegram_id"] if payout else None
            recipient = members_by_id.get(recipient_id)
            who = format_user_identity(
                recipient.get("display_name") if recipient else None,
                recipient.get("username") if recipient else None,
                recipient_id,
            )
            lines.append(t(lang, "status_period", period=group["current_period"], total=total_periods))
            lines.append(t(lang, "status_recipient", who=who, amount=(payout["amount"] if payout else 0), currency=currency))
            if payout:
                lines.append(t(lang, "status_payout_state", state=t(lang, "payout_paid" if payout["status"] == "paid" else "payout_pending")))
        else:
            lines.append(t(lang, "status_round", period=group["current_period"], received=len(payouts or []), total=len(members)))
            lines.append("")
            if group.get("draw_state") == "overdue":
                lines.append(t(lang, "status_draw_overdue"))
            elif group.get("draw_at"):
                lines.append(t(lang, "status_next_draw", when=format_draw_time(group["draw_at"]), countdown=format_countdown(group["draw_at"])))
            lines.append(t(lang, "status_secret_note"))

        lines.append("")
        lines.append(t(lang, "status_contributions_header"))
        for c in sorted(contributions, key=lambda x: x.get("display_name") or ""):
            emoji = CONTRIB_EMOJI.get(c["status"], "⚪")
            who = format_user_identity(c.get("display_name"), c.get("username"), c["telegram_id"])
            member = members_by_id.get(c["telegram_id"]) or {}
            note = t(lang, "status_backpay_note") if member.get("joined_period") else ""
            lines.append(f"{emoji} {who}{note}")

        lines.extend(_received_so_far_lines(group, members, payouts or [], lang))
    elif group["status"] == "completed":
        lines.append("")
        lines.append(t(lang, "status_completed_msg", total=len(payouts or [])))

    return "\n".join(lines)


def build_cycle_started_text(group: dict, member_count: int, lang: str, seed_hash: str, draw_at) -> str:
    """Posted (and pinned) when the cycle starts. Note there is NO order
    and NO pre-known recipient — that's the whole point of the lottery."""
    pool = group["contribution_amount"] * member_count
    lines = [
        t(lang, "cycle_started_title", name=html.escape(group["name"]), cycle=group.get("cycle_number", 1)),
        t(lang, "cycle_started_rules", amount=group["contribution_amount"], currency=group["currency"], pool=pool),
        "",
        t(lang, "cycle_first_draw", when=format_draw_time(draw_at), countdown=format_countdown(draw_at)),
        t(lang, "cycle_pay_instructions"),
        "",
        t(lang, "seed_commit_line", hash=seed_hash),
        t(lang, "cycle_secret_note"),
    ]
    return "\n".join(lines)


def build_next_round_text(group: dict, period: int, lang: str, seed_hash: str, draw_at, cycle_number: int = 0) -> str:
    """Posted (and pinned) when a round opens — after /payout or an
    auto-restart. Shows the exact next draw time."""
    lines = []
    if cycle_number > 1:
        lines.append(t(lang, "cycle_new_cycle_line", cycle=cycle_number, name=html.escape(group["name"])))
    lines.append(t(lang, "next_round_title", name=html.escape(group["name"]), period=period))
    lines.append(t(lang, "next_round_amount", amount=group["contribution_amount"], currency=group["currency"]))
    lines.append("")
    lines.append(t(lang, "next_round_draw", when=format_draw_time(draw_at), countdown=format_countdown(draw_at)))
    lines.append(t(lang, "seed_commit_line", hash=seed_hash))
    return "\n".join(lines)


def build_draw_result_text(
    group: dict,
    period: int,
    winner: dict | None,
    winner_id: int,
    pool: int,
    lang: str,
    seed: str,
    seed_hash: str,
    eligible_ids: list[int],
) -> str:
    who = winner_display(winner, winner_id)
    lines = [
        t(lang, "draw_result_title", name=html.escape(group["name"]), period=period),
        t(lang, "draw_result_winner", who=who, amount=pool, currency=group["currency"]),
        t(lang, "draw_result_eligible_count", count=len(eligible_ids)),
        "",
        t(lang, "payout_admin_note"),
        "",
        t(
            lang,
            "draw_verification",
            seed=html.escape(seed),
            hash=seed_hash,
            ids=html.escape(", ".join(str(i) for i in sorted(eligible_ids))),
        ),
    ]
    return "\n".join(lines)


def build_payout_announcement_text(group: dict, period: int, recipient: dict | None, telegram_id: int, amount: int, lang: str) -> str:
    who = format_user_identity(
        recipient.get("display_name") if recipient else None,
        recipient.get("username") if recipient else None,
        telegram_id,
    )
    return t(
        lang,
        "payout_announcement",
        name=html.escape(group["name"]),
        period=period,
        who=who,
        amount=amount,
        currency=group["currency"],
    )
