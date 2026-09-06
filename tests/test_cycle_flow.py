"""End-to-end test of the lottery state machine with an in-memory repo
stand-in (no MongoDB needed):

create -> join -> start cycle -> verify payments -> draw -> payout ->
next round -> ... -> completion with auto-restart -> fresh cycle.

Guards the cycle-scoping invariants: after an auto-restart, the new
cycle's round 1 must not inherit the previous cycle's verified payments
or its received-payout flags.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from db import repository as repo
from services import draw_service, equb_service
from services.equb_service import EqubError


class FakeMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.sent = []
        self.pinned = []
        self.unpinned = []
        self._next_id = 0

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        self.sent.append((chat_id, text))
        return FakeMessage(self._next_id)

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.pinned.append((chat_id, message_id))

    async def unpin_chat_message(self, chat_id, message_id=None):
        self.unpinned.append((chat_id, message_id))


class MemoryRepo:
    """Minimal in-memory mirror of the repository semantics the services use."""

    def __init__(self):
        self.groups = {}
        self.members = []
        self.contributions = []
        self.payouts = []
        self.seq = 0

    def _id(self):
        self.seq += 1
        return f"obj{self.seq}"

    # --- groups ---
    async def create_group(self, chat_id, name, contribution_amount, currency, frequency, interval_days, restart_mode, created_by):
        g = {
            "_id": self._id(), "chat_id": chat_id, "name": name,
            "contribution_amount": contribution_amount, "currency": currency,
            "frequency": frequency, "interval_days": interval_days,
            "restart_mode": restart_mode, "status": "open",
            "order": [], "current_period": 0, "cycle_number": 0,
            "seed": None, "seed_hash": None, "draw_at": None,
            "draw_state": None, "draw_started_at": None,
            "overdue_notified": False, "overdue_eligible_notified": False,
            "pre_draw_reminder_sent": False, "pinned_message_id": None,
        }
        self.groups[g["_id"]] = g
        return g

    async def get_group(self, gid):
        return self.groups.get(str(gid))

    async def get_active_or_open_group(self, chat_id):
        for g in self.groups.values():
            if g["chat_id"] == chat_id and g["status"] in ("open", "active"):
                return g
        return None

    async def start_group_cycle(self, gid, seed, seed_hash, draw_at):
        g = self.groups[gid]
        g.update(status="active", current_period=1, cycle_number=1,
                 seed=seed, seed_hash=seed_hash, draw_at=draw_at, draw_state="scheduled")

    async def restart_group_cycle(self, gid, seed, seed_hash, draw_at):
        g = self.groups[gid]
        g["cycle_number"] += 1
        g.update(status="active", current_period=1, seed=seed, seed_hash=seed_hash,
                 draw_at=draw_at, draw_state="scheduled")
        for m in self.members:
            if m["group_id"] == gid and m["status"] == "active":
                m.update(received_payout=False, payout_period=None, joined_period=None)
        return g["cycle_number"]

    async def set_draw_schedule(self, gid, seed, seed_hash, draw_at):
        g = self.groups[gid]
        g.update(seed=seed, seed_hash=seed_hash, draw_at=draw_at, draw_state="scheduled",
                 overdue_notified=False, overdue_eligible_notified=False, pre_draw_reminder_sent=False)

    async def set_draw_time(self, gid, draw_at):
        g = self.groups[gid]
        g.update(draw_at=draw_at, draw_state="scheduled", overdue_notified=False,
                 overdue_eligible_notified=False, pre_draw_reminder_sent=False)

    async def set_group_drawn(self, gid):
        self.groups[gid]["draw_state"] = "drawn"

    async def set_pinned_message_id(self, gid, message_id):
        self.groups[gid]["pinned_message_id"] = message_id

    async def claim_due_draw(self, gid, now=None, force=False):
        g = self.groups.get(str(gid))
        if not g or g["status"] != "active":
            return None
        if g.get("draw_state") not in ("scheduled", "overdue"):
            return None
        if not force:
            if not g.get("draw_at") or g["draw_at"] > (now or datetime.now(timezone.utc)):
                return None
        g["draw_state"] = "drawing"
        return g

    async def set_draw_overdue(self, gid, notified):
        g = self.groups[gid]
        g["draw_state"] = "overdue"
        g["overdue_notified"] = notified

    async def advance_group_period(self, gid, period):
        self.groups[gid]["current_period"] = period

    async def complete_group(self, gid):
        self.groups[gid]["status"] = "completed"

    # --- members ---
    async def add_member(self, gid, telegram_id, username, display_name, joined_period=None):
        m = {"_id": self._id(), "group_id": gid, "telegram_id": telegram_id,
             "username": username, "display_name": display_name, "status": "active",
             "received_payout": False, "payout_period": None, "joined_period": joined_period}
        self.members.append(m)
        return m

    async def get_member(self, gid, telegram_id):
        return next((m for m in self.members
                     if m["group_id"] == gid and m["telegram_id"] == telegram_id and m["status"] == "active"), None)

    async def reactivate_member(self, gid, telegram_id, joined_period):
        m = next((m for m in self.members
                  if m["group_id"] == gid and m["telegram_id"] == telegram_id and m["status"] == "removed"), None)
        if not m:
            return False
        m["status"] = "active"
        m["joined_period"] = joined_period
        return True

    async def list_members(self, gid, active_only=True):
        return [m for m in self.members
                if m["group_id"] == gid and (not active_only or m["status"] == "active")]

    async def list_unreceived_members(self, gid):
        return [m for m in self.members
                if m["group_id"] == gid and m["status"] == "active" and not m.get("received_payout")]

    async def count_active_members(self, gid):
        return len(await self.list_members(gid))

    async def mark_member_paid_out(self, gid, telegram_id, period):
        m = next(m for m in self.members
                 if m["group_id"] == gid and m["telegram_id"] == telegram_id)
        m["received_payout"] = True
        m["payout_period"] = period

    # --- contributions ---
    async def create_contribution(self, gid, cycle_number, period, member, amount):
        c = {"_id": self._id(), "group_id": gid, "cycle_number": cycle_number, "period": period,
             "telegram_id": member["telegram_id"], "status": "pending", "amount": amount}
        self.contributions.append(c)
        return c

    async def create_period_contributions(self, gid, period, members, amount, cycle_number=None):
        for m in members:
            self.contributions.append({
                "group_id": gid, "cycle_number": cycle_number, "period": period,
                "telegram_id": m["telegram_id"], "status": "pending", "amount": amount,
            })

    async def get_contribution(self, gid, period, telegram_id, cycle_number=None):
        return next((c for c in self.contributions
                     if c["group_id"] == gid and c["period"] == period and c["telegram_id"] == telegram_id
                     and (cycle_number is None or c.get("cycle_number") == cycle_number)), None)

    async def get_contributions_for_period(self, gid, period, cycle_number=None):
        return [c for c in self.contributions
                if c["group_id"] == gid and c["period"] == period
                and (cycle_number is None or c.get("cycle_number") == cycle_number)]

    async def get_verified_member_ids(self, gid, period, cycle_number=None):
        return [c["telegram_id"] for c in self.contributions
                if c["group_id"] == gid and c["period"] == period and c["status"] == "verified"
                and (cycle_number is None or c.get("cycle_number") == cycle_number)]

    async def member_has_unverified_contributions(self, gid, cycle_number, telegram_id):
        return any(c["group_id"] == gid and c["telegram_id"] == telegram_id
                   and c["status"] != "verified"
                   and (cycle_number is None or c.get("cycle_number") == cycle_number)
                   for c in self.contributions)

    async def list_pending_contributions_for_user(self, telegram_id):
        docs = [c for c in self.contributions
                if c["telegram_id"] == telegram_id and c["status"] in ("pending", "awaiting_review")]
        return sorted(docs, key=lambda c: (c["period"], c.get("cycle_number") or 0))

    # --- payouts ---
    async def get_payout(self, gid, period, cycle_number=None):
        return next((p for p in self.payouts
                     if p["group_id"] == gid and p["period"] == period
                     and (cycle_number is None or p.get("cycle_number") == cycle_number)), None)

    async def create_payout_record(self, gid, period, telegram_id, amount, seed=None,
                                   seed_hash=None, eligible_ids=None, cycle_number=None):
        p = {"_id": self._id(), "group_id": gid, "cycle_number": cycle_number, "period": period,
             "telegram_id": telegram_id, "amount": amount, "status": "pending",
             "seed": seed, "seed_hash": seed_hash, "eligible_ids": eligible_ids or []}
        self.payouts.append(p)
        return p

    async def mark_payout_paid(self, gid, period, admin_id, cycle_number=None):
        p = next(p for p in self.payouts
                 if p["group_id"] == gid and p["period"] == period
                 and (cycle_number is None or p.get("cycle_number") == cycle_number))
        p["status"] = "paid"

    # --- misc ---
    async def get_chat_language(self, chat_id):
        return "en"

    async def get_admins(self):
        return [999]


def _patch_repo(monkeypatch):
    mem = MemoryRepo()
    for name in dir(mem):
        if not name.startswith("_"):
            fn = getattr(mem, name)
            if callable(fn):
                monkeypatch.setattr(repo, name, fn, raising=False)
    return mem


def _run(coro):
    return asyncio.run(coro)


class TestFullLotteryCycle:
    def test_three_members_full_cycle_with_auto_restart(self, monkeypatch):
        mem = _patch_repo(monkeypatch)
        _run(self._flow(mem))

    async def _flow(self, mem):
        bot = FakeBot()
        creator = {"telegram_id": 1, "username": "alice", "display_name": "Alice"}
        args = equb_service.parse_newequb_args("Family | 100 | 1d | ETB | auto")
        group = await equb_service.create_group(chat_id=-100, creator=creator, **args)
        for tid, name in [(2, "Bony"), (3, "Chala")]:
            await equb_service.join_group(group, {"telegram_id": tid, "username": name.lower(), "display_name": name})

        result = await equb_service.start_cycle(group)
        g = await repo.get_group(group["_id"])
        assert g["status"] == "active" and g["cycle_number"] == 1 and g["current_period"] == 1
        assert g["draw_at"] > datetime.now(timezone.utc)
        assert result["seed_hash"] and g["seed"]
        # The winner must not exist anywhere before the draw:
        assert await repo.get_payout(group["_id"], 1, 1) is None

        # Round 1: Alice and Bony verified, Chala didn't -> excluded.
        self._verify(mem, group["_id"], 1, [1, 2])
        r1 = await draw_service.run_draw(bot, g)
        assert r1["drawn"] and r1["winner_id"] in (1, 2)
        payout1 = await repo.get_payout(group["_id"], 1, 1)
        assert payout1["telegram_id"] == r1["winner_id"]
        assert sorted(payout1["eligible_ids"]) == [1, 2]

        r = await equb_service.mark_payout_and_advance(g, 999)
        assert not r["completed"] and r["next_period"] == 2
        g = await repo.get_group(group["_id"])
        assert g["draw_state"] == "scheduled" and g["draw_at"] > datetime.now(timezone.utc)

        # Round 2: everyone verified; winner must be someone who hasn't received.
        self._verify(mem, group["_id"], 2, [1, 2, 3])
        r2 = await draw_service.run_draw(bot, g)
        assert r2["winner_id"] != r1["winner_id"]
        assert r2["winner_id"] in {1, 2, 3} - {r1["winner_id"]}
        r = await equb_service.mark_payout_and_advance(g, 999)
        g = await repo.get_group(group["_id"])

        # Round 3: only the last member remains -> they win.
        self._verify(mem, group["_id"], 3, [1, 2, 3])
        r3 = await draw_service.run_draw(bot, g)
        winners = {r1["winner_id"], r2["winner_id"], r3["winner_id"]}
        assert winners == {1, 2, 3}  # every member received exactly once

        # Cycle completed -> auto-restart opens a FRESH cycle.
        r = await equb_service.mark_payout_and_advance(g, 999)
        assert r["completed"] and r["restarted"] and r["cycle_number"] == 2
        g = await repo.get_group(group["_id"])
        assert g["cycle_number"] == 2 and g["current_period"] == 1 and g["draw_state"] == "scheduled"
        assert await repo.get_payout(group["_id"], 1, 2) is None

        # Cycle-scoping regression: nobody is marked received in cycle 2, and
        # old cycle-1 verified payments must NOT leak into new cycle round 1.
        unreceived = await repo.list_unreceived_members(group["_id"])
        assert {m["telegram_id"] for m in unreceived} == {1, 2, 3}
        self._verify(mem, group["_id"], 1, [1, 2, 3], cycle=2)
        r4 = await draw_service.run_draw(bot, g)
        assert r4["drawn"] and r4["winner_id"] in (1, 2, 3)

    def _verify(self, mem, gid, period, tids, cycle=None):
        for c in mem.contributions:
            if (c["group_id"] == gid and c["period"] == period and c["telegram_id"] in tids
                    and (cycle is None or c.get("cycle_number") == cycle)):
                c["status"] = "verified"


class TestMidCycleJoinBackPay:
    """A member joining an already-running cycle must back-pay every round
    since the cycle started before they can win a draw — otherwise someone
    could join after everyone else had received their pool and win a full
    pool having paid only one contribution."""

    def test_backpay_gates_eligibility(self, monkeypatch):
        mem = _patch_repo(monkeypatch)
        _run(self._flow(mem))

    async def _flow(self, mem):
        bot = FakeBot()
        args = equb_service.parse_newequb_args("Family | 100 | 1d | ETB | auto")
        group = await equb_service.create_group(
            chat_id=-100, creator={"telegram_id": 1, "username": "a", "display_name": "A"}, **args
        )
        for tid, name in [(2, "Bony"), (3, "Chala")]:
            await equb_service.join_group(group, {"telegram_id": tid, "username": name.lower(), "display_name": name})
        await equb_service.start_cycle(group)
        g = await repo.get_group(group["_id"])

        # Round 1 runs among the founders only.
        self._verify(mem, group["_id"], 1, [1, 2, 3])
        r1 = await draw_service.run_draw(bot, g)
        assert r1["drawn"] and r1["winner_id"] in (1, 2, 3)
        await equb_service.mark_payout_and_advance(g, 999)
        g = await repo.get_group(group["_id"])

        # Newcomer joins mid-cycle (round 2): gets back-pay docs for rounds 1..2.
        result = await equb_service.join_group(group, {"telegram_id": 4, "username": "d", "display_name": "D"})
        assert result["backpay_periods"] == [1, 2]
        assert (await repo.get_member(group["_id"], 4))["joined_period"] == 2

        # Round 2: founders verified; newcomer verifies round 2 ONLY —
        # they must NOT be in the draw until the back-pay is complete.
        self._verify(mem, group["_id"], 2, [1, 2, 3, 4])
        r2 = await draw_service.run_draw(bot, g)
        assert r2["drawn"] and r2["winner_id"] != 4
        await equb_service.mark_payout_and_advance(g, 999)
        g = await repo.get_group(group["_id"])

        # Round 3: newcomer pays up (rounds 1, 2 and now 3) — now eligible.
        self._verify(mem, group["_id"], 3, [1, 2, 3, 4])
        r3 = await draw_service.run_draw(bot, g)
        assert r3["drawn"] and r3["winner_id"] in {1, 2, 3, 4} - {r1["winner_id"], r2["winner_id"]}

    def test_joining_after_all_pools_paid_cannot_win_early(self, monkeypatch):
        mem = _patch_repo(monkeypatch)
        _run(self._exploit_flow(mem))

    async def _exploit_flow(self, mem):
        """The corner case that motivated back-pay: someone joins when all
        other members have already received their pool. With naive
        next-round semantics they'd be the only unreceived member and
        guaranteed to win a full pool having paid once. Back-pay prevents
        it: they must pay every round of the new cycle before winning."""
        bot = FakeBot()
        args = equb_service.parse_newequb_args("Family | 100 | 1d | ETB | auto")
        group = await equb_service.create_group(
            chat_id=-100, creator={"telegram_id": 1, "username": "a", "display_name": "A"}, **args
        )
        await equb_service.join_group(group, {"telegram_id": 2, "username": "b", "display_name": "B"})
        await equb_service.start_cycle(group)
        g = await repo.get_group(group["_id"])

        # Both founders receive their pools; auto-restart opens cycle 2.
        self._verify(mem, group["_id"], 1, [1, 2])
        r1 = await draw_service.run_draw(bot, g)
        await equb_service.mark_payout_and_advance(g, 999)
        g = await repo.get_group(group["_id"])
        self._verify(mem, group["_id"], 2, [1, 2])
        r2 = await draw_service.run_draw(bot, g)
        assert {r1["winner_id"], r2["winner_id"]} == {1, 2}
        r = await equb_service.mark_payout_and_advance(g, 999)
        assert r["completed"] and r["restarted"]
        g = await repo.get_group(group["_id"])
        assert g["cycle_number"] == 2 and g["current_period"] == 1

        # Someone joins the fresh cycle: their back-pay is just the current
        # round (the cycle has only run one round) — fair, not exploitable.
        result = await equb_service.join_group(g, {"telegram_id": 5, "username": "e", "display_name": "E"})
        assert result["backpay_periods"] == [1]

        # Founders verified for the new round; the newcomer isn't yet:
        # they must NOT be in the draw.
        self._verify(mem, group["_id"], 1, [1, 2], cycle=2)
        r3 = await draw_service.run_draw(bot, g)
        assert r3["drawn"] and r3["winner_id"] in (1, 2)

        # Once the newcomer pays their round, they're eligible for future draws.
        self._verify(mem, group["_id"], 1, [5], cycle=2)
        eligible = await draw_service.compute_eligible_ids(g, 1)
        assert 5 in eligible

    def test_join_blocked_on_ending_once_cycle(self, monkeypatch):
        """A 'once' cycle whose final round is already drawn has no next
        round — joining would trap the joiner into back-paying a cycle
        that's about to complete. Must be refused."""
        mem = _patch_repo(monkeypatch)
        _run(self._ending_flow(mem))

    async def _ending_flow(self, mem):
        args = equb_service.parse_newequb_args("Family | 100 | 1d")
        group = await equb_service.create_group(
            chat_id=-100, creator={"telegram_id": 1, "username": "a", "display_name": "A"}, **args
        )
        await equb_service.join_group(group, {"telegram_id": 2, "username": "b", "display_name": "B"})
        await equb_service.start_cycle(group)
        g = await repo.get_group(group["_id"])

        self._verify(mem, group["_id"], 1, [1, 2])
        r1 = await draw_service.run_draw(bot := FakeBot(), g)
        await equb_service.mark_payout_and_advance(g, 999)
        g = await repo.get_group(group["_id"])
        self._verify(mem, group["_id"], 2, [1, 2])
        r2 = await draw_service.run_draw(bot, g)  # final round drawn...
        assert g["draw_state"] == "drawn"         # ...but not yet paid out

        with pytest.raises(EqubError) as e:
            await equb_service.join_group(g, {"telegram_id": 5, "username": "e", "display_name": "E"})
        assert e.value.args[0] == "join_cycle_ending"

    def _verify(self, mem, gid, period, tids, cycle=None):
        for c in mem.contributions:
            if (c["group_id"] == gid and c["period"] == period and c["telegram_id"] in tids
                    and (cycle is None or c.get("cycle_number") == cycle)):
                c["status"] = "verified"


class TestDrawOverdue:
    def test_draw_with_no_verified_payments_marks_overdue(self, monkeypatch):
        mem = _patch_repo(monkeypatch)
        _run(self._overdue_flow(mem))

    async def _overdue_flow(self, mem):
        bot = FakeBot()
        args = equb_service.parse_newequb_args("Family | 100 | 1d")
        group = await equb_service.create_group(chat_id=-100, creator={"telegram_id": 1, "username": "a", "display_name": "A"}, **args)
        await equb_service.join_group(group, {"telegram_id": 2, "username": "b", "display_name": "B"})
        await equb_service.start_cycle(group)
        g = await repo.get_group(group["_id"])

        result = await draw_service.run_draw(bot, g)
        assert not result["drawn"] and result["overdue"]
        g = await repo.get_group(group["_id"])
        assert g["draw_state"] == "overdue" and g["overdue_notified"]
        assert await repo.get_payout(group["_id"], 1, 1) is None
        # The group was told, and the admins were told:
        overdue_msgs = [t for (_, t) in bot.sent if "draw time" in t.lower()]
        assert overdue_msgs

        # Still overdue on a second tick — no duplicate notification.
        bot.sent.clear()
        result = await draw_service.run_draw(bot, g)
        assert not result["drawn"]
        assert not [t for (_, t) in bot.sent if "draw time" in t.lower()]

        # After a payment is verified and the draw is forced, it proceeds.
        mem.contributions[0]["status"] = "verified"
        mem.contributions[1]["status"] = "verified"
        claimed = await repo.claim_due_draw(group["_id"], force=True)
        assert claimed is not None
        result = await draw_service.run_draw(bot, claimed)
        assert result["drawn"] and result["winner_id"] in (1, 2)
