"""Privacy guarantee of the status board: while a round is pending, /status
must never reveal — or even hint at — who is about to receive the pool,
because nobody knows (the winner doesn't exist until the draw fires).
Past winners, payment states and the exact draw time ARE public.
"""

from datetime import datetime, timedelta, timezone

from core.texts import build_status_text


def _group(**overrides):
    group = {
        "_id": "g" * 24,
        "chat_id": -100123,
        "group_number": 1,
        "name": "Family Equb",
        "contribution_amount": 500,
        "currency": "ETB",
        "frequency": "weekly",
        "interval_days": 7,
        "restart_mode": "auto",
        "status": "active",
        "cycle_number": 1,
        "order": [],
        "current_period": 3,
        "seed_hash": "ab" * 32,
        "draw_at": datetime.now(timezone.utc) + timedelta(days=2),
        "draw_state": "scheduled",
    }
    group.update(overrides)
    return group


def _members():
    return [
        {"telegram_id": 1, "username": "alice", "display_name": "Alice", "received_payout": True, "payout_period": 1, "joined_period": None},
        {"telegram_id": 2, "username": "bony", "display_name": "Bony", "received_payout": True, "payout_period": 2, "joined_period": None},
        {"telegram_id": 3, "username": "chala", "display_name": "Chala", "received_payout": False, "payout_period": None, "joined_period": None},
    ]


def _contributions():
    return [
        {"telegram_id": 1, "username": "alice", "display_name": "Alice", "status": "verified"},
        {"telegram_id": 2, "username": "bony", "display_name": "Bony", "status": "awaiting_review"},
        {"telegram_id": 3, "username": "chala", "display_name": "Chala", "status": "pending"},
    ]


class TestActiveLotteryStatus:
    def test_shows_exact_draw_time(self):
        text = build_status_text(_group(), _members(), _contributions(), None, "en", [])
        assert "Next draw" in text

    def test_shows_payment_states(self):
        text = build_status_text(_group(), _members(), _contributions(), None, "en", [])
        assert "🟢" in text and "🟡" in text and "⚪" in text

    def test_shows_past_winners(self):
        payouts = [
            {"period": 1, "telegram_id": 1, "amount": 1500, "status": "paid"},
            {"period": 2, "telegram_id": 2, "amount": 1500, "status": "paid"},
        ]
        text = build_status_text(_group(), _members(), _contributions(), None, "en", payouts)
        assert "Alice" in text and "Bony" in text
        assert "Pools received so far" in text

    def test_never_shows_an_upcoming_recipient(self):
        text = build_status_text(_group(), _members(), _contributions(), None, "en", [])
        # The old sequential wording must be gone entirely in lottery mode.
        assert "recipient" not in text.lower()

    def test_shows_secret_note(self):
        text = build_status_text(_group(), _members(), _contributions(), None, "en", [])
        assert "nobody knows" in text.lower()

    def test_overdue_state_is_visible(self):
        group = _group(draw_state="overdue")
        text = build_status_text(group, _members(), _contributions(), None, "en", [])
        assert "drawnow" in text


class TestLegacyGroupStatus:
    def test_legacy_group_still_shows_recipient(self):
        group = _group(
            order=[1, 2, 3],
            draw_at=None,
            draw_state=None,
            current_period=2,
        )
        payout = {"period": 2, "telegram_id": 2, "amount": 1500, "status": "pending"}
        text = build_status_text(group, _members(), _contributions(), payout, "en", [])
        # Pre-upgrade in-flight groups keep the old fixed-order display.
        assert "recipient" in text.lower()


class TestCompletedStatus:
    def test_completed_board(self):
        group = _group(status="completed", draw_at=None, draw_state="drawn")
        payouts = [{"period": 1, "telegram_id": 1, "amount": 1500, "status": "paid"},
                   {"period": 2, "telegram_id": 2, "amount": 1500, "status": "paid"},
                   {"period": 3, "telegram_id": 3, "amount": 1500, "status": "paid"}]
        text = build_status_text(group, _members(), [], None, "en", payouts)
        assert "complete" in text.lower()
