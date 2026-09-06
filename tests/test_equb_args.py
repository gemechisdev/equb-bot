import pytest

from services.equb_service import EqubError, parse_newequb_args


class TestParseNewequbArgs:
    def test_full_syntax(self):
        args = parse_newequb_args("Family Equb | 500 | weekly | ETB | auto")
        assert args == {
            "name": "Family Equb",
            "amount": 500,
            "frequency": "weekly",
            "interval_days": 7,
            "currency": "ETB",
            "restart_mode": "auto",
        }

    def test_minimal_syntax_uses_defaults(self):
        args = parse_newequb_args("Family Equb | 500 | monthly")
        assert args["currency"] == "ETB"
        assert args["restart_mode"] == "once"
        assert args["interval_days"] == 30

    def test_every_n_days_frequency(self):
        args = parse_newequb_args("School | 100 | 3d")
        assert args["frequency"] == "3d"
        assert args["interval_days"] == 3

    def test_extra_whitespace_is_tolerated(self):
        args = parse_newequb_args("  Family  |  500  |  biweekly  |  USD  |  once  ")
        assert args["name"] == "Family"
        assert args["currency"] == "USD"

    def test_old_order_mode_is_rejected_clearly(self):
        with pytest.raises(EqubError) as e:
            parse_newequb_args("Family | 500 | weekly | random")
        assert e.value.args[0] == "order_mode_removed"

    @pytest.mark.parametrize(
        "raw, code",
        [
            ("", "usage_newequb"),
            ("Family | 500", "usage_newequb"),
            ("Family | abc | weekly", "usage_newequb"),
            ("Family | 0 | weekly", "invalid_amount"),
            ("Family | -5 | weekly", "invalid_amount"),
            ("Family | 500 | yearly", "invalid_frequency"),
            ("Family | 500 | 0d", "invalid_frequency"),
            ("Family | 500 | weekly | ETB | sometimes", "invalid_restart_mode"),
        ],
    )
    def test_validation_errors(self, raw, code):
        with pytest.raises(EqubError) as e:
            parse_newequb_args(raw)
        assert e.value.args[0] == code
