import hashlib

import pytest

from services.draw_service import new_period_seed, pick_winner, verify_payout_record


class TestNewPeriodSeed:
    def test_hash_is_sha256_of_seed(self):
        seed, seed_hash = new_period_seed()
        assert seed_hash == hashlib.sha256(seed.encode()).hexdigest()

    def test_seeds_are_unique(self):
        pairs = {new_period_seed()[0] for _ in range(20)}
        assert len(pairs) == 20


class TestPickWinner:
    ELIGIBLE = [111111, 222222, 333333, 444444, 555555]

    def test_deterministic_given_seed(self):
        a = pick_winner("fixed-seed", self.ELIGIBLE)
        b = pick_winner("fixed-seed", self.ELIGIBLE)
        assert a == b

    def test_independent_of_input_order(self):
        # The eligible list arrives from the DB in arbitrary order; the pick
        # must be stable because ids are sorted first.
        a = pick_winner("fixed-seed", self.ELIGIBLE)
        b = pick_winner("fixed-seed", list(reversed(self.ELIGIBLE)))
        assert a == b

    def test_winner_is_from_eligible(self):
        for seed in ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"):
            assert pick_winner(seed, self.ELIGIBLE) in self.ELIGIBLE

    def test_varies_across_seeds(self):
        winners = {pick_winner(f"seed-{i}", self.ELIGIBLE) for i in range(30)}
        assert len(winners) > 1  # not stuck on one member

    def test_single_eligible_always_wins(self):
        assert pick_winner("any-seed", [42]) == 42

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            pick_winner("any-seed", [])


class TestVerifyPayoutRecord:
    def test_ok_when_consistent(self):
        seed, seed_hash = new_period_seed()
        eligible = [10, 20, 30]
        winner = pick_winner(seed, eligible)
        result = verify_payout_record({
            "seed": seed,
            "seed_hash": seed_hash,
            "eligible_ids": eligible,
            "telegram_id": winner,
        })
        assert result["ok"] is True
        assert result["seed_hash_match"] is True
        assert result["winner_match"] is True
        assert result["recomputed_winner"] == winner

    def test_detects_hash_tamper(self):
        seed, _ = new_period_seed()
        eligible = [1, 2, 3]
        winner = pick_winner(seed, eligible)
        result = verify_payout_record({
            "seed": seed,
            "seed_hash": "0" * 64,
            "eligible_ids": eligible,
            "telegram_id": winner,
        })
        assert result["ok"] is False
        assert "seed_hash_mismatch" in result["errors"]

    def test_detects_winner_tamper(self):
        seed, seed_hash = new_period_seed()
        eligible = [1, 2, 3]
        winner = pick_winner(seed, eligible)
        other = next(i for i in eligible if i != winner)
        result = verify_payout_record({
            "seed": seed,
            "seed_hash": seed_hash,
            "eligible_ids": eligible,
            "telegram_id": other,
        })
        assert result["ok"] is False
        assert "winner_mismatch" in result["errors"]
