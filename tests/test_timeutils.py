from datetime import datetime, timedelta, timezone

import pytest

from core import timeutils


class TestParseFrequencyInterval:
    def test_named_frequencies(self):
        assert timeutils.parse_frequency_interval("weekly") == 7
        assert timeutils.parse_frequency_interval("biweekly") == 14
        assert timeutils.parse_frequency_interval("monthly") == 30

    def test_named_frequencies_case_insensitive(self):
        assert timeutils.parse_frequency_interval("WEEKLY") == 7
        assert timeutils.parse_frequency_interval(" Monthly ") == 30

    def test_n_days(self):
        assert timeutils.parse_frequency_interval("3d") == 3
        assert timeutils.parse_frequency_interval("30D") == 30
        assert timeutils.parse_frequency_interval(" 7d ") == 7

    @pytest.mark.parametrize("bad", ["0d", "-3d", "d3", "3", "yearly", "", "weeklyd", "3dd"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            timeutils.parse_frequency_interval(bad)


class TestNextDrawAt:
    def test_adds_interval(self):
        start = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
        assert timeutils.next_draw_at(start, 7) == start + timedelta(days=7)
        assert timeutils.next_draw_at(start, 3) == start + timedelta(days=3)


class TestFormatDrawTime:
    def test_converts_to_addis_ababa(self):
        # 15:00 UTC == 18:00 EAT (UTC+3)
        dt = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)
        rendered = timeutils.format_draw_time(dt)
        assert "06:00 PM" in rendered
        assert "2026" in rendered
        assert "EAT" in rendered

    def test_is_deterministic(self):
        dt = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        assert timeutils.format_draw_time(dt) == timeutils.format_draw_time(dt)


class TestFormatCountdown:
    def test_past_is_now(self):
        now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
        past = now - timedelta(minutes=5)
        assert timeutils.format_countdown(past, now=now) == "now"

    def test_days_and_hours(self):
        now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
        future = now + timedelta(days=2, hours=5)
        assert timeutils.format_countdown(future, now=now) == "in 2d 5h"

    def test_hours_and_minutes(self):
        now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
        future = now + timedelta(hours=3, minutes=20)
        assert timeutils.format_countdown(future, now=now) == "in 3h 20m"
