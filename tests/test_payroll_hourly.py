# -*- coding: utf-8 -*-
"""Суммированный учёт рабочего времени: часы, сверхурочные, ночные, праздничные.

Что должно быть верно:

1. при суммированном учёте табель по дням не принимается — нужны часы;
2. часовая ставка = оклад / норма часов месяца, и это помечено «на проверку»: закон способа
   расчёта не устанавливает;
3. сверхурочные считаются от нормы учётного периода: первые 2 часа — в полуторном, дальше —
   в двойном; «со слов табеля» переработка не принимается;
4. предел 120 часов в год (ч. 6 ст. 99 ТК РФ) не превышается молча;
5. ночные — 20 % часовой ставки за каждый час; праздничные — в двойном размере, причём час
   праздника не оплачивается трижды;
6. часы, посчитанные дважды (ночные + праздничные больше отработанных), — ошибка табеля.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pto_agent import payroll                                          # noqa: E402

STAFF = REPO / "data" / "demo" / "payroll" / "staff.json"
HOURS = REPO / "data" / "demo" / "payroll" / "timesheet_hours_2026_11.json"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def staff() -> dict:
    return read(STAFF)


def rules() -> dict:
    return payroll.load_rules()


def timesheet(**rows) -> dict:
    base = {"schema": "pto-agent/timesheet@1", "period": "2026-11", "regime": "суммированный",
            "norm": {"days": 20, "hours": 160,
                     "source": "производственный календарь: пн–пт минус праздники"},
            "rows": []}
    base.update(rows)
    return base


def codes(report: dict) -> list[str]:
    return [item["code"] for item in report.get("errors") or []]


def accrue(sheet: dict) -> dict:
    state = payroll.new_state(object_name="ЖК «Северный», корпус 2")
    return payroll.accrue(state, sheet, staff(), rules())


def row_of(report: dict, tab: str) -> dict:
    return next(row for row in report["rows"] if row["tab"] == tab)


# ─────────────────────────── табель ───────────────────────────
def test_hour_mode_needs_hours_not_days():
    report = accrue(timesheet(rows=[{"tab": "001", "worked_days": 20}]))
    assert "BAD_HOURS" in codes(report)
    assert report["rows"] == []


def test_hour_mode_needs_hours_norm():
    sheet = timesheet(rows=[{"tab": "001", "worked_hours": 160}])
    sheet["norm"] = {"days": 20}
    report = accrue(sheet)
    assert "NO_HOURS_NORM" in codes(report)


def test_double_counted_hours_are_an_error():
    report = accrue(timesheet(rows=[{"tab": "001", "worked_hours": 160,
                                     "night_hours": 100, "holiday_hours": 80}]))
    assert "HOURS_OVERLAP" in codes(report)


def test_declared_overtime_must_match_the_norm():
    report = accrue(timesheet(rows=[{"tab": "001", "worked_hours": 176, "overtime_hours": 8}]))
    assert "OVERTIME_MISMATCH" in codes(report)


def test_overtime_limit_per_year_is_enforced():
    report = accrue(timesheet(rows=[{"tab": "001", "worked_hours": 160 + 121}]))
    assert "OVERTIME_OVER_LIMIT" in codes(report)


# ─────────────────────────── начисление ───────────────────────────
def test_full_norm_pays_just_the_salary():
    report = accrue(timesheet(rows=[{"tab": "001", "worked_hours": 160}]))
    assert not report["errors"], report["errors"]
    row = row_of(report, "001")
    assert row["hourly_rate"] == pytest.approx(906.25)      # 145 000 / 160
    assert row["accrued"] == pytest.approx(145_000.0)
    assert row["overtime_pay"] == 0.0 and row["night_pay"] == 0.0


def test_overtime_first_two_hours_one_and_a_half_then_double():
    report = accrue(timesheet(rows=[{"tab": "002", "worked_hours": 168}]))
    row = row_of(report, "002")
    # 120 000 / 160 = 750; 2 × 0,5 × 750 + 6 × 1 × 750 = 5 250
    assert row["hourly_rate"] == pytest.approx(750.0)
    assert row["overtime_hours"] == pytest.approx(8.0)
    assert row["overtime_pay"] == pytest.approx(5_250.0)
    assert row["accrued"] == pytest.approx(126_000 + 5_250)


def test_night_hours_are_twenty_percent():
    report = accrue(timesheet(rows=[{"tab": "002", "worked_hours": 160, "night_hours": 24}]))
    row = row_of(report, "002")
    assert row["night_pay"] == pytest.approx(3_600.0)       # 24 × 750 × 20 %


def test_holiday_hour_is_not_paid_three_times():
    report = accrue(timesheet(rows=[{"tab": "004", "worked_hours": 160, "holiday_hours": 8}]))
    row = row_of(report, "004")
    # 8 ч. праздника уже оплачены в составе 160 ч., доплата — ещё одна ставка (итого двойной)
    assert row["hours_base_pay"] == pytest.approx(88_000.0)
    assert row["holiday_pay"] == pytest.approx(4_400.0)     # 8 × 550
    assert row["accrued"] == pytest.approx(92_400.0)


def test_combined_case_adds_up_exactly():
    report = accrue(read(HOURS))
    assert not report["errors"], report["errors"]
    row = row_of(report, "004")
    # 176 × 550 = 96 800 + сверхурочные 8 250 + ночные 3 520 + праздник 4 400
    assert row["accrued"] == pytest.approx(112_970.0)
    assert row["hourly_verify"] is True        # способ расчёта ставки — решение организации


def test_demo_hour_timesheet_is_consistent_with_staff_and_norm():
    sheet = read(HOURS)
    norm, errors, gaps = payroll.check_timesheet(sheet, staff(), rules())
    assert not errors
    assert norm["hours"] == 160
    assert all(row.get("worked_hours", 0) >= 0 for row in sheet["rows"])


def test_ndfl_and_contributions_are_counted_on_hour_pay():
    report = accrue(read(HOURS))
    row = row_of(report, "004")
    assert row["ndfl"] > 0
    assert row["contrib"] > 0
    assert row["net"] == pytest.approx(row["accrued"] - row["ndfl"])
