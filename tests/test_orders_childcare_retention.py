# -*- coding: utf-8 -*-
"""Пособие по уходу до 1,5 лет и средний заработок на период трудоустройства.

Что должно быть верно:

1. пособие по уходу — 40 % среднего заработка, а не «как декрет»: месячная сумма, 30,4 дня,
   потолок и минимум, за неполный месяц — пропорционально календарным дням;
2. минимум и максимум — не выдуманные числа, а 40 % МРОТ и 40 % предельного среднего дневного;
3. НДФЛ и взносов с пособия нет, платит СФР — то есть в расходы организации сумма не идёт;
4. второй месяц трудоустройства идёт **зачётом** выходного пособия, а не второй полной суммой;
5. третий месяц без решения службы занятости и без обращения в две недели не считается;
6. необлагаемый лимит трёхкратного среднего месячного заработка — общий на все выплаты,
   а не по три раза на каждую.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pto_agent import orders, payroll                                  # noqa: E402

STAFF = REPO / "data" / "demo" / "payroll" / "staff.json"
LIFECYCLE = REPO / "data" / "demo" / "payroll" / "orders_lifecycle_2026.json"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def staff() -> dict:
    return read(STAFF)


def rules() -> dict:
    return payroll.load_rules()


def employee(tab: str) -> dict:
    return next(row for row in staff()["employees"] if row["tab"] == tab)


def childcare_order(**extra) -> dict:
    order = {"number": "П-40/2026", "date": "2026-11-05", "kind": "childcare", "tab": "002",
             "start": "2026-11-05", "end": "2028-05-04", "earnings_2y": 1_980_000,
             "excluded_days": 0, "days_in_month": 30}
    order.update(extra)
    return order


def retention_order(**extra) -> dict:
    order = {"number": "П-41/2026", "date": "2026-12-20", "kind": "retention", "tab": "003",
             "month": 2, "dismissal_date": "2026-11-20", "base_12m": 1_020_000,
             "worked_days_12m": 200}
    order.update(extra)
    return order


def codes(row: dict) -> list[str]:
    return [item["code"] for item in row.get("errors") or []]


# ─────────────────────────── пособие до 1,5 лет ───────────────────────────
def test_childcare_is_forty_percent_of_monthly_average():
    row = orders.childcare(childcare_order(), employee("002"), rules(), period="2026-11")
    # 1 980 000 / 730 = 2 712,33; × 30,4 = 82 454,83; × 40 % = 32 981,93
    assert row["sdz"] == pytest.approx(2712.33)
    assert row["monthly_raw"] == pytest.approx(32_981.93)
    assert row["monthly"] == pytest.approx(32_981.93)
    assert not row["capped_by_max"] and not row["raised_to_min"]


def test_partial_month_is_paid_by_calendar_days():
    row = orders.childcare(childcare_order(days_of_leave=26), employee("002"), rules(),
                           period="2026-11")
    assert row["days"] == 26
    assert row["fund_amount"] == pytest.approx(round(32_981.93 * 26 / 30, 2))
    assert row["verify"] is True


def test_full_month_needs_no_partial_flag():
    row = orders.childcare(childcare_order(days_of_leave=30), employee("002"), rules(),
                           period="2026-11")
    assert row["fund_amount"] == pytest.approx(32_981.93)


def test_childcare_is_capped_by_the_limit_not_by_wish():
    row = orders.childcare(childcare_order(earnings_2y=5_600_000), employee("002"), rules(),
                           period="2026-11")
    assert row["monthly_raw"] > 83_021.18
    assert row["monthly"] == pytest.approx(83_021.18)
    assert row["capped_by_max"] is True


def test_childcare_minimum_is_forty_percent_of_mrot():
    row = orders.childcare(childcare_order(earnings_2y=600_000), employee("002"), rules(),
                           period="2026-11")
    assert row["monthly"] == pytest.approx(10_837.20)
    assert row["raised_to_min"] is True


def test_childcare_has_no_ndfl_and_no_contributions():
    row = orders.childcare(childcare_order(), employee("002"), rules(), period="2026-11")
    assert row["amount"] == 0.0            # не расход организации: платит СФР
    assert row["taxable"] == 0.0
    assert row["contrib_base"] == 0.0
    assert row["fund_amount"] > 0


def test_childcare_without_earnings_is_not_calculated_from_salary():
    row = orders.childcare(childcare_order(earnings_2y=None), employee("002"), rules(),
                           period="2026-11")
    assert "CHILDCARE_NO_BASE" in codes(row)
    assert row["fund_amount"] == 0.0
    assert row.get("not_calculated") is True


def test_childcare_bad_days_are_refused():
    row = orders.childcare(childcare_order(days_of_leave=40), employee("002"), rules(),
                           period="2026-11")
    assert "CHILDCARE_BAD_DAYS" in codes(row)


# ─────────────────── средний заработок на период трудоустройства ───────────────────
def test_second_month_offsets_severance_already_paid():
    paid = orders.severance({**retention_order(multiple=1), "kind": "severance"},
                            employee("003"), rules(), period="2026-11")
    assert paid["amount"] == pytest.approx(104_975.0)
    row = orders.retention(retention_order(severance_paid=paid["amount"]), employee("003"),
                           rules(), period="2026-12")
    assert row["monthly"] == pytest.approx(104_975.0)
    assert row["offset"] == pytest.approx(104_975.0)
    assert row["amount"] == 0.0          # зачёт: второй раз та же сумма не платится
    assert "зачёт" in row["offset_note"]


def test_second_month_without_offset_amount_is_flagged():
    row = orders.retention(retention_order(severance_paid=None), employee("003"), rules(),
                           period="2026-12")
    assert row["amount"] == pytest.approx(104_975.0)
    assert row["verify"] is True


def test_third_month_requires_employment_service_decision():
    row = orders.retention(retention_order(month=3, applied_within_2_weeks=True),
                           employee("003"), rules(), period="2027-01")
    assert "RETENTION_NO_DECISION" in codes(row)
    assert row["amount"] == 0.0


def test_third_month_requires_two_week_application():
    row = orders.retention(retention_order(month=3, employment_decision={"number": "№214",
                                                                        "date": "2027-01-15"}),
                           employee("003"), rules(), period="2027-01")
    assert "RETENTION_LATE_APPLICATION" in codes(row)


def test_third_month_with_decision_is_tax_free_within_remaining_limit():
    row = orders.retention(retention_order(month=3, applied_within_2_weeks=True,
                                           employment_decision={"number": "№214",
                                                                "date": "2027-01-15"},
                                           tax_free_used=104_975),
                           employee("003"), rules(), period="2027-01")
    assert not row["errors"]
    assert row["amount"] == pytest.approx(104_975.0)
    assert row["tax_free_limit"] == pytest.approx(314_925.0)
    assert row["tax_free_remaining"] == pytest.approx(209_950.0)
    assert row["taxable"] == 0.0          # вся сумма в пределах трёхкратного
    assert row["verify"] is True


def test_retention_taxable_appears_only_above_three_monthly_salaries():
    row = orders.retention(retention_order(month=3, applied_within_2_weeks=True,
                                           employment_decision={"number": "№214",
                                                                "date": "2027-01-15"},
                                           tax_free_used=314_925),
                           employee("003"), rules(), period="2027-01")
    assert row["tax_free_remaining"] == 0.0
    assert row["taxable"] == pytest.approx(104_975.0)
    assert row["contrib_base"] == pytest.approx(104_975.0)


def test_bad_month_is_refused():
    row = orders.retention(retention_order(month=4), employee("003"), rules(), period="2027-03")
    assert "RETENTION_BAD_MONTH" in codes(row)


def test_retention_without_base_is_not_calculated():
    row = orders.retention(retention_order(base_12m=None), employee("003"), rules(),
                           period="2026-12")
    assert "RETENTION_NO_BASE" in codes(row)
    assert row.get("not_calculated") is True


# ─────────────────────────── общий отчёт приказов ───────────────────────────
def test_report_puts_childcare_into_fund_column_not_into_expense():
    report = orders.report(orders.load_orders(LIFECYCLE), staff(), rules(), period="2026-11")
    assert not report["errors"], report["errors"]
    line = next(row for row in report["lines"] if row["kind"] == "childcare")
    assert line["fund_amount"] > 0 and line["amount"] == 0
    assert report["totals"]["childcare_fund"] == pytest.approx(28_584.34)
    assert report["totals"]["gross"] == 0.0     # в расходы организации не попало ничего


def test_report_counts_retention_as_payable_but_tax_free():
    report = orders.report(orders.load_orders(LIFECYCLE), staff(), rules(), period="2027-01")
    assert not report["errors"], report["errors"]
    assert report["totals"]["retention"] == pytest.approx(104_975.0)
    assert report["totals"]["taxable"] == 0.0
    assert report["totals"]["gross"] == pytest.approx(104_975.0)


def test_report_separates_periods_and_does_not_mix_months():
    args = (orders.load_orders(LIFECYCLE), staff(), rules())
    december = orders.report(*args, period="2026-12")
    assert [row["number"] for row in december["lines"]] == ["П-41/2026"]
    february = orders.report(*args, period="2026-02")
    assert february["lines"] == []
    assert february["gaps"]          # «приказов нет» — это запрос, а не тишина
