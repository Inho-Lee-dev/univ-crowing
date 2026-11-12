from datetime import date

import pytest

from src.models import (TITLE_MAX, clean_title, months_ago, normalize_range,
                        parse_period, utc_to_kst_date)


class TestParsePeriod:
    def test_range(self):
        assert parse_period("2026.11.03(화) ~ 2026.11.18(수)") == ("2026-11-03", "2026-11-18")

    def test_same_day_drops_end(self):
        assert parse_period("2026.11.20(금) ~ 2026.11.20(금)") == ("2026-11-20", None)

    def test_single_date(self):
        assert parse_period("2026.11.20(금)") == ("2026-11-20", None)

    def test_single_digit_month_day(self):
        assert parse_period("2026.1.5(월) ~ 2026.2.3(화)") == ("2026-01-05", "2026-02-03")

    @pytest.mark.parametrize("text", [None, "", "   "])
    def test_empty(self, text):
        assert parse_period(text) == (None, None)

    @pytest.mark.parametrize("text", ["상시", "2026.13.01(?)", "2026.11.01 ~ 2026.11.02 ~ 2026.11.03"])
    def test_invalid(self, text):
        with pytest.raises(ValueError):
            parse_period(text)


class TestUtcToKst:
    def test_crosses_midnight(self):
        assert utc_to_kst_date("2026-10-07T17:32:56Z") == "2026-10-08"

    def test_same_day(self):
        assert utc_to_kst_date("2026-10-07T05:32:56Z") == "2026-10-07"

    def test_boundary(self):
        assert utc_to_kst_date("2026-11-10T14:59:59Z") == "2026-11-10"
        assert utc_to_kst_date("2026-11-10T15:00:00Z") == "2026-11-11"

    def test_naive_rejected(self):
        with pytest.raises(ValueError):
            utc_to_kst_date("2026-10-07T05:32:56")


class TestMonthsAgo:
    @pytest.mark.parametrize("today, expected", [
        (date(2026, 11, 11), date(2026, 8, 11)),
        (date(2026, 11, 30), date(2026, 8, 30)),
        (date(2026, 5, 31), date(2026, 2, 28)),
        (date(2028, 5, 31), date(2028, 2, 29)),   # 윤년
        (date(2027, 1, 15), date(2026, 10, 15)),  # 연도 넘김
        (date(2026, 3, 31), date(2025, 12, 31)),
    ])
    def test_cases(self, today, expected):
        assert months_ago(today, 3) == expected


class TestNormalize:
    def test_range_rules(self):
        assert normalize_range(None, "2026-11-01") == (None, None)
        assert normalize_range("2026-11-01", "2026-11-01") == ("2026-11-01", None)
        assert normalize_range("2026-11-01", None) == ("2026-11-01", None)
        assert normalize_range("2026-11-01", "2026-11-02") == ("2026-11-01", "2026-11-02")

    def test_clean_title(self):
        assert clean_title("  공지  \n") == "공지"
        assert len(clean_title("가" * 3000)) == TITLE_MAX
