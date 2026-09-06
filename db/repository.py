"""Every Mongo query lives here, nowhere else.

Collections
-----------
equb_groups         one document per rotating-savings group (an "Equb"),
                     scoped to the Telegram chat it lives in.
equb_members         one document per (group, telegram user).
equb_contributions   one document per (group, period, telegram user) —
                     that member's contribution for that period.
equb_payouts         one document per (group, period) — who received the
                     pooled amount that period and whether it's been paid.
                     Created at DRAW time (when the winner is chosen), not
                     before, so the winner stays secret until the draw.
payment_methods      shared payout/contribution instructions (Telebirr,
                     CBE, etc.) — not scoped to a group, since the admin's
                     receiving accounts are the same everywhere.
admins               DB-backed bot admins (in addition to ADMIN_IDS in env).
chat_settings        per-chat language preference.
users                lightweight identity cache (telegram_id <-> username)
                     populated by middleware, so admins can add members by
                     @username — the Bot API has no username->id lookup.

Lottery draw state lives on the group document:
    seed / seed_hash     commit/reveal pair for the CURRENT period. The
                          hash is published (and pinned) when the round
                          opens; the seed is revealed only in the draw
                          result, letting anyone reproduce the pick.
    draw_at              when the current period's draw fires (UTC).
    draw_state           scheduled -> drawing -> drawn, or overdue when the
                          draw time passed with zero verified payments.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bson import ObjectId

from core.config import ADMIN_IDS
from db.client import get_db

# If a draw is claimed ("drawing") but not finished within this window, the
# scheduler is allowed to reclaim and retry it (crash recovery).
DRAW_CLAIM_STALE_MINUTES = 10


def utcnow():
    return datetime.now(timezone.utc)


def _oid(value):
    return value if isinstance(value, ObjectId) else ObjectId(str(value))


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

async def get_next_group_number(chat_id: int) -> int:
    db = get_db()
    last = await db.equb_groups.find_one({"chat_id": chat_id}, sort=[("group_number", -1)])
    return (last["group_number"] + 1) if last else 1


async def create_group(
    chat_id: int,
    name: str,
    contribution_amount: int,
    currency: str,
    frequency: str,
    interval_days: int,
    restart_mode: str,
    created_by: int,
) -> dict:
    db = get_db()
    group_number = await get_next_group_number(chat_id)
    doc = {
        "chat_id": chat_id,
        "group_number": group_number,
        "name": name,
        "contribution_amount": contribution_amount,
        "currency": currency,
        "frequency": frequency,  # weekly | biweekly | monthly | <N>d
        "interval_days": interval_days,
        "restart_mode": restart_mode,  # once | auto
        "status": "open",  # open -> active -> completed | cancelled
        # --- lottery draw state (current period) ---
        "cycle_number": 0,  # incremented on every cycle start (first start -> 1)
        "seed": None,  # committed seed for the current period, revealed at draw
        "seed_hash": None,  # published when the round opens
        "draw_at": None,  # current period's draw time (UTC)
        "draw_state": None,  # scheduled | drawing | drawn | overdue
        "draw_started_at": None,
        "overdue_notified": False,
        "overdue_eligible_notified": False,
        "pre_draw_reminder_sent": False,
        "pinned_message_id": None,
        # --- legacy fields (pre-lottery groups still mid-cycle) ---
        "order": [],  # legacy: fixed payout order; unused by new groups
        "current_period": 0,
        "draw": {},  # legacy commit/reveal metadata
        # ---
        "created_by": created_by,
        "created_at": utcnow(),
        "started_at": None,
        "current_cycle_started_at": None,
        "completed_at": None,
        "board_message_id": None,
    }
    result = await db.equb_groups.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_group(group_id) -> Optional[dict]:
    db = get_db()
    return await db.equb_groups.find_one({"_id": _oid(group_id)})


async def get_group_by_number(chat_id: int, group_number: int) -> Optional[dict]:
    db = get_db()
    return await db.equb_groups.find_one({"chat_id": chat_id, "group_number": group_number})


async def get_active_or_open_group(chat_id: int) -> Optional[dict]:
    """The single non-terminal (open or active) group for this chat, if any.
    Mirrors the original bot's "one active round per group" simplification."""
    db = get_db()
    return await db.equb_groups.find_one({"chat_id": chat_id, "status": {"$in": ["open", "active"]}})


async def list_groups(chat_id: int) -> List[dict]:
    db = get_db()
    cursor = db.equb_groups.find({"chat_id": chat_id}).sort("group_number", 1)
    return await cursor.to_list(length=200)


async def get_active_groups() -> List[dict]:
    """Every active group across all chats (scheduler input)."""
    db = get_db()
    return await db.equb_groups.find({"status": "active"}).to_list(length=1000)


async def set_group_status(group_id, status: str, **extra_fields) -> None:
    db = get_db()
    fields = {"status": status, **extra_fields}
    await db.equb_groups.update_one({"_id": _oid(group_id)}, {"$set": fields})


async def set_board_message_id(group_id, message_id: int) -> None:
    db = get_db()
    await db.equb_groups.update_one({"_id": _oid(group_id)}, {"$set": {"board_message_id": message_id}})


async def set_pinned_message_id(group_id, message_id: int) -> None:
    db = get_db()
    await db.equb_groups.update_one({"_id": _oid(group_id)}, {"$set": {"pinned_message_id": message_id}})


def _draw_fields(seed: Optional[str], seed_hash: Optional[str], draw_at: Optional[datetime]) -> dict:
    return {
        "seed": seed,
        "seed_hash": seed_hash,
        "draw_at": draw_at,
        "draw_state": "scheduled" if draw_at else None,
        "draw_started_at": None,
        "overdue_notified": False,
        "overdue_eligible_notified": False,
        "pre_draw_reminder_sent": False,
    }


async def start_group_cycle(group_id, seed: str, seed_hash: str, draw_at: datetime) -> None:
    """First cycle start: lock membership, open period 1, schedule round 1."""
    db = get_db()
    now = utcnow()
    await db.equb_groups.update_one(
        {"_id": _oid(group_id)},
        {
            "$set": {
                "status": "active",
                "order": [],
                "current_period": 1,
                "cycle_number": 1,
                "started_at": now,
                "current_cycle_started_at": now,
                **_draw_fields(seed, seed_hash, draw_at),
            }
        },
    )


async def restart_group_cycle(group_id, seed: str, seed_hash: str, draw_at: datetime) -> int:
    """Auto-restart: a brand-new cycle for the same members/settings.
    Resets per-cycle member state (received_payout, joined_period) so
    everyone is back in the lottery, and returns the new cycle number."""
    db = get_db()
    result = await db.equb_groups.find_one_and_update(
        {"_id": _oid(group_id)},
        {
            "$inc": {"cycle_number": 1},
            "$set": {
                "status": "active",
                "current_period": 1,
                "current_cycle_started_at": utcnow(),
                **_draw_fields(seed, seed_hash, draw_at),
            },
        },
        return_document=True,
    )
    await db.equb_members.update_many(
        {"group_id": _oid(group_id), "status": "active"},
        {"$set": {"received_payout": False, "payout_period": None, "joined_period": None}},
    )
    return (result or {}).get("cycle_number", 1)


async def set_draw_schedule(group_id, seed: str, seed_hash: str, draw_at: datetime) -> None:
    """Schedule a new round's draw (used when a period advances)."""
    db = get_db()
    await db.equb_groups.update_one(
        {"_id": _oid(group_id)}, {"$set": _draw_fields(seed, seed_hash, draw_at)}
    )


async def set_draw_time(group_id, draw_at: datetime) -> None:
    """Move the pending draw (postpone). The committed seed is kept, so the
    fairness commitment made when the round opened still holds."""
    db = get_db()
    await db.equb_groups.update_one(
        {"_id": _oid(group_id)},
        {
            "$set": {
                "draw_at": draw_at,
                "draw_state": "scheduled",
                "draw_started_at": None,
                "overdue_notified": False,
                "overdue_eligible_notified": False,
                "pre_draw_reminder_sent": False,
            }
        },
    )


async def reset_draw_state(group_id) -> None:
    """Return a claimed-but-unfinished draw to the scheduled state so the
    scheduler retries it."""
    db = get_db()
    await db.equb_groups.update_one(
        {"_id": _oid(group_id)}, {"$set": {"draw_state": "scheduled", "draw_started_at": None}}
    )


def _claimable_query(group_id, now: datetime, force: bool) -> dict:
    stale_cutoff = now - timedelta(minutes=DRAW_CLAIM_STALE_MINUTES)
    return {
        "_id": _oid(group_id),
        "status": "active",
        "$or": [
            {"draw_state": {"$in": ["scheduled", "overdue"]}, "draw_at": {"$ne": None, "$lte": now}},
            {"draw_state": "drawing", "draw_started_at": {"$ne": None, "$lt": stale_cutoff}},
        ]
        if not force
        else [
            {"draw_state": {"$in": ["scheduled", "overdue"]}},
            {"draw_state": "drawing", "draw_started_at": {"$ne": None, "$lt": stale_cutoff}},
        ],
    }


async def claim_due_draw(group_id, now: Optional[datetime] = None, force: bool = False) -> Optional[dict]:
    """Atomically claim a group's pending draw so only one worker (scheduler
    tick, /drawnow, gunicorn process) can ever run it. Returns the updated
    group document, or None if another worker got there first / nothing is
    claimable. `force` skips the draw_at check (used by /drawnow)."""
    db = get_db()
    now = now or utcnow()
    return await db.equb_groups.find_one_and_update(
        _claimable_query(group_id, now, force),
        {"$set": {"draw_state": "drawing", "draw_started_at": now}},
        return_document=True,
    )


async def get_due_draw_groups(now: Optional[datetime] = None) -> List[dict]:
    db = get_db()
    now = now or utcnow()
    stale_cutoff = now - timedelta(minutes=DRAW_CLAIM_STALE_MINUTES)
    query = {
        "status": "active",
        "$or": [
            {"draw_state": {"$in": ["scheduled", "overdue"]}, "draw_at": {"$ne": None, "$lte": now}},
            {"draw_state": "drawing", "draw_started_at": {"$ne": None, "$lt": stale_cutoff}},
        ],
    }
    return await db.equb_groups.find(query).to_list(length=200)


async def set_group_drawn(group_id) -> None:
    db = get_db()
    await db.equb_groups.update_one(
        {"_id": _oid(group_id)},
        {"$set": {"draw_state": "drawn", "draw_started_at": None}},
    )


async def set_draw_overdue(group_id, notified: bool) -> None:
    db = get_db()
    await db.equb_groups.update_one(
        {"_id": _oid(group_id)},
        {"$set": {"draw_state": "overdue", "draw_started_at": None, "overdue_notified": notified}},
    )


async def set_overdue_eligible_notified(group_id) -> None:
    db = get_db()
    await db.equb_groups.update_one(
        {"_id": _oid(group_id)}, {"$set": {"overdue_eligible_notified": True}}
    )


async def get_overdue_groups() -> List[dict]:
    db = get_db()
    return await db.equb_groups.find({"status": "active", "draw_state": "overdue"}).to_list(length=200)


async def get_pre_draw_reminder_groups(now: datetime, window_end: datetime) -> List[dict]:
    db = get_db()
    query = {
        "status": "active",
        "draw_state": "scheduled",
        "pre_draw_reminder_sent": {"$ne": True},
        "draw_at": {"$ne": None, "$gt": now, "$lte": window_end},
    }
    return await db.equb_groups.find(query).to_list(length=200)


async def mark_pre_draw_reminder_sent(group_id) -> None:
    db = get_db()
    await db.equb_groups.update_one(
        {"_id": _oid(group_id)}, {"$set": {"pre_draw_reminder_sent": True}}
    )


async def advance_group_period(group_id, new_period: int) -> None:
    db = get_db()
    await db.equb_groups.update_one({"_id": _oid(group_id)}, {"$set": {"current_period": new_period}})


async def complete_group(group_id) -> None:
    db = get_db()
    await db.equb_groups.update_one(
        {"_id": _oid(group_id)}, {"$set": {"status": "completed", "completed_at": utcnow()}}
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

async def add_member(
    group_id, telegram_id: int, username: Optional[str], display_name: Optional[str],
    joined_period: Optional[int] = None,
) -> dict:
    db = get_db()
    doc = {
        "group_id": _oid(group_id),
        "telegram_id": telegram_id,
        "username": username,
        "display_name": display_name,
        "status": "active",
        "joined_at": utcnow(),
        "joined_period": joined_period,  # mid-cycle joiners start next round
        "received_payout": False,
        "payout_period": None,
    }
    result = await db.equb_members.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_member(group_id, telegram_id: int) -> Optional[dict]:
    db = get_db()
    return await db.equb_members.find_one({"group_id": _oid(group_id), "telegram_id": telegram_id})


async def reactivate_member(group_id, telegram_id: int, joined_period: Optional[int]) -> bool:
    """A previously-removed member re-added to the group."""
    db = get_db()
    result = await db.equb_members.update_one(
        {"group_id": _oid(group_id), "telegram_id": telegram_id, "status": "removed"},
        {"$set": {"status": "active", "joined_period": joined_period, "rejoined_at": utcnow()}},
    )
    return result.modified_count > 0


async def list_members(group_id, active_only: bool = True) -> List[dict]:
    db = get_db()
    query = {"group_id": _oid(group_id)}
    if active_only:
        query["status"] = "active"
    cursor = db.equb_members.find(query).sort("joined_at", 1)
    return await cursor.to_list(length=500)


async def list_unreceived_members(group_id) -> List[dict]:
    """Active members who haven't received their pool yet — these are the
    people still in the lottery for future rounds."""
    db = get_db()
    return await db.equb_members.find(
        {"group_id": _oid(group_id), "status": "active", "received_payout": {"$ne": True}}
    ).to_list(length=500)


async def count_active_members(group_id) -> int:
    db = get_db()
    return await db.equb_members.count_documents({"group_id": _oid(group_id), "status": "active"})


async def remove_member(group_id, telegram_id: int) -> bool:
    result = await get_db().equb_members.update_one(
        {"group_id": _oid(group_id), "telegram_id": telegram_id, "status": "active"},
        {"$set": {"status": "removed", "removed_at": utcnow()}},
    )
    return result.modified_count > 0


async def mark_member_paid_out(group_id, telegram_id: int, period: int) -> None:
    db = get_db()
    await db.equb_members.update_one(
        {"group_id": _oid(group_id), "telegram_id": telegram_id},
        {"$set": {"received_payout": True, "payout_period": period}},
    )


async def list_member_groups(telegram_id: int, statuses: Optional[List[str]] = None) -> List[dict]:
    """All (member, group) pairs a user belongs to, newest first — used to
    resolve which group/period a private-chat proof submission belongs to."""
    db = get_db()
    member_docs = await db.equb_members.find({"telegram_id": telegram_id, "status": "active"}).to_list(length=200)
    if not member_docs:
        return []
    group_ids = [m["group_id"] for m in member_docs]
    query = {"_id": {"$in": group_ids}}
    if statuses:
        query["status"] = {"$in": statuses}
    groups = await db.equb_groups.find(query).to_list(length=200)
    groups_by_id = {g["_id"]: g for g in groups}
    pairs = []
    for m in member_docs:
        g = groups_by_id.get(m["group_id"])
        if g:
            pairs.append((m, g))
    return pairs


# ---------------------------------------------------------------------------
# Contributions
# ---------------------------------------------------------------------------

async def create_period_contributions(
    group_id, period: int, members: List[dict], amount: int, cycle_number: Optional[int] = None
) -> None:
    """Contributions are cycle-scoped: reusing period numbers across cycles
    (after an auto-restart) must never leak old payments into a new cycle."""
    db = get_db()
    docs = [
        {
            "group_id": _oid(group_id),
            "cycle_number": cycle_number,
            "period": period,
            "telegram_id": m["telegram_id"],
            "username": m.get("username"),
            "display_name": m.get("display_name"),
            "amount": amount,
            "status": "pending",  # pending -> awaiting_review -> verified | rejected
            "proof": None,
            "created_at": utcnow(),
            "submitted_at": None,
            "reviewed_at": None,
            "reviewed_by": None,
        }
        for m in members
    ]
    if docs:
        await db.equb_contributions.insert_many(docs)


async def get_contribution(group_id, period: int, telegram_id: int) -> Optional[dict]:
    db = get_db()
    return await db.equb_contributions.find_one(
        {"group_id": _oid(group_id), "period": period, "telegram_id": telegram_id}
    )


async def get_contributions_for_period(group_id, period: int, cycle_number: Optional[int] = None) -> List[dict]:
    db = get_db()
    query: dict = {"group_id": _oid(group_id), "period": period}
    if cycle_number is not None:
        query["cycle_number"] = cycle_number
    cursor = db.equb_contributions.find(query)
    return await cursor.to_list(length=500)


async def get_verified_member_ids(group_id, period: int, cycle_number: Optional[int] = None) -> List[int]:
    db = get_db()
    query: dict = {"group_id": _oid(group_id), "period": period, "status": "verified"}
    if cycle_number is not None:
        query["cycle_number"] = cycle_number
    return await db.equb_contributions.distinct("telegram_id", query)


async def submit_contribution_proof(contribution_id, proof: dict) -> None:
    db = get_db()
    await db.equb_contributions.update_one(
        {"_id": _oid(contribution_id)},
        {"$set": {"status": "awaiting_review", "proof": proof, "submitted_at": utcnow()}},
    )


async def find_awaiting_contribution_for_user(telegram_id: int) -> Optional[dict]:
    """The most recent contribution this user still needs to act on (pending
    submission or awaiting review), across every active group they're in and
    the CURRENT cycle of each — used to match a private-chat message/photo
    to the right record."""
    db = get_db()
    active_groups = await db.equb_groups.find(
        {"status": "active"}, {"_id": 1, "cycle_number": 1}
    ).to_list(length=1000)
    if not active_groups:
        return None
    or_clause = [
        {"group_id": g["_id"], "cycle_number": g.get("cycle_number", 1)}
        for g in active_groups
    ]
    return await db.equb_contributions.find_one(
        {
            "telegram_id": telegram_id,
            "$or": or_clause,
            "status": {"$in": ["pending", "awaiting_review"]},
        },
        sort=[("created_at", -1)],
    )


async def get_awaiting_review_contributions(group_id, cycle_number: Optional[int] = None) -> List[dict]:
    db = get_db()
    query: dict = {"group_id": _oid(group_id), "status": "awaiting_review"}
    if cycle_number is not None:
        query["cycle_number"] = cycle_number
    cursor = db.equb_contributions.find(query)
    return await cursor.to_list(length=500)


async def review_contribution(contribution_id, status: str, admin_id: int) -> Optional[dict]:
    db = get_db()
    await db.equb_contributions.update_one(
        {"_id": _oid(contribution_id)},
        {"$set": {"status": status, "reviewed_at": utcnow(), "reviewed_by": admin_id}},
    )
    return await db.equb_contributions.find_one({"_id": _oid(contribution_id)})


async def count_verified_for_period(group_id, period: int) -> int:
    db = get_db()
    return await db.equb_contributions.count_documents(
        {"group_id": _oid(group_id), "period": period, "status": "verified"}
    )


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------

async def create_payout_record(
    group_id,
    period: int,
    telegram_id: int,
    amount: int,
    seed: Optional[str] = None,
    seed_hash: Optional[str] = None,
    eligible_ids: Optional[List[int]] = None,
    cycle_number: Optional[int] = None,
) -> dict:
    """Called when a round's winner is DRAWN (not before — the winner must
    not exist anywhere until the draw fires)."""
    db = get_db()
    doc = {
        "group_id": _oid(group_id),
        "cycle_number": cycle_number,
        "period": period,
        "telegram_id": telegram_id,
        "amount": amount,
        "status": "pending",  # pending -> paid (admin confirms disbursement)
        "seed": seed,
        "seed_hash": seed_hash,
        "eligible_ids": eligible_ids or [],
        "drawn_at": utcnow() if seed else None,
        "created_at": utcnow(),
        "paid_at": None,
        "paid_by": None,
    }
    result = await db.equb_payouts.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_payout(group_id, period: int, cycle_number: Optional[int] = None) -> Optional[dict]:
    db = get_db()
    query: dict = {"group_id": _oid(group_id), "period": period}
    if cycle_number is not None:
        query["cycle_number"] = cycle_number
    return await db.equb_payouts.find_one(query)


async def mark_payout_paid(group_id, period: int, admin_id: int, cycle_number: Optional[int] = None) -> None:
    db = get_db()
    query: dict = {"group_id": _oid(group_id), "period": period}
    if cycle_number is not None:
        query["cycle_number"] = cycle_number
    await db.equb_payouts.update_one(
        query,
        {"$set": {"status": "paid", "paid_at": utcnow(), "paid_by": admin_id}},
    )


async def list_payouts(group_id) -> List[dict]:
    db = get_db()
    cursor = db.equb_payouts.find({"group_id": _oid(group_id)}).sort("period", 1)
    return await cursor.to_list(length=500)


# ---------------------------------------------------------------------------
# Users (identity cache for @username lookups)
# ---------------------------------------------------------------------------

async def upsert_user(telegram_id: int, username: Optional[str], first_name: Optional[str], full_name: Optional[str]) -> None:
    db = get_db()
    username = username or None
    await db.users.update_one(
        {"telegram_id": telegram_id},
        {
            "$set": {
                "username": username,
                "username_lower": username.lower() if username else None,
                "first_name": first_name,
                "full_name": full_name,
                "updated_at": utcnow(),
            },
            "$setOnInsert": {"created_at": utcnow()},
        },
        upsert=True,
    )


async def find_user_by_username(username: str) -> Optional[dict]:
    db = get_db()
    return await db.users.find_one(
        {"username_lower": username.lower().lstrip("@")}, sort=[("updated_at", -1)]
    )


# ---------------------------------------------------------------------------
# Admins (DB-backed, in addition to ADMIN_IDS in env)
# ---------------------------------------------------------------------------

async def add_admin(telegram_id: int) -> None:
    db = get_db()
    await db.admins.update_one({"telegram_id": telegram_id}, {"$set": {"telegram_id": telegram_id}}, upsert=True)


async def remove_admin(telegram_id: int) -> None:
    db = get_db()
    await db.admins.delete_one({"telegram_id": telegram_id})


async def get_admins() -> List[int]:
    db = get_db()
    db_admins = await db.admins.find({}).to_list(length=1000)
    ids = {a["telegram_id"] for a in db_admins}
    ids |= ADMIN_IDS
    return sorted(ids)


async def is_user_admin(telegram_id: int) -> bool:
    if telegram_id in ADMIN_IDS:
        return True
    db = get_db()
    found = await db.admins.find_one({"telegram_id": telegram_id})
    return found is not None


async def ensure_admins_from_env() -> None:
    for tid in ADMIN_IDS:
        await add_admin(tid)


# ---------------------------------------------------------------------------
# Chat language settings
# ---------------------------------------------------------------------------

_lang_cache: dict[int, str] = {}


async def get_chat_language(chat_id: int) -> str:
    if chat_id in _lang_cache:
        return _lang_cache[chat_id]
    db = get_db()
    doc = await db.chat_settings.find_one({"chat_id": chat_id})
    lang = (doc or {}).get("language", "en")
    _lang_cache[chat_id] = lang
    return lang


async def set_chat_language(chat_id: int, lang: str) -> None:
    db = get_db()
    await db.chat_settings.update_one({"chat_id": chat_id}, {"$set": {"language": lang}}, upsert=True)
    _lang_cache[chat_id] = lang


# ---------------------------------------------------------------------------
# Payment methods (shared across every group)
# ---------------------------------------------------------------------------

async def add_payment_method(name: str, details: str) -> dict:
    db = get_db()
    doc = {"name": name, "details": details, "active": True, "created_at": utcnow()}
    result = await db.payment_methods.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def list_payment_methods(active_only: bool = False) -> List[dict]:
    db = get_db()
    query = {"active": True} if active_only else {}
    cursor = db.payment_methods.find(query).sort("created_at", 1)
    return await cursor.to_list(length=200)


async def get_payment_method(payment_method_id) -> Optional[dict]:
    db = get_db()
    return await db.payment_methods.find_one({"_id": _oid(payment_method_id)})


async def update_payment_method(payment_method_id, name: Optional[str] = None, details: Optional[str] = None) -> bool:
    db = get_db()
    fields = {}
    if name is not None:
        fields["name"] = name
    if details is not None:
        fields["details"] = details
    if not fields:
        return False
    result = await db.payment_methods.update_one({"_id": _oid(payment_method_id)}, {"$set": fields})
    return result.modified_count > 0


async def set_payment_method_active(payment_method_id, active: bool) -> bool:
    result = await get_db().payment_methods.update_one(
        {"_id": _oid(payment_method_id)}, {"$set": {"active": active}}
    )
    return result.modified_count > 0


async def delete_payment_method(payment_method_id) -> bool:
    db = get_db()
    result = await db.payment_methods.delete_one({"_id": _oid(payment_method_id)})
    return result.deleted_count > 0
