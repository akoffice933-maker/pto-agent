# -*- coding: utf-8 -*-
"""Декретные и выходное пособие: два вида приказов, где «как больничный» не работает.

Декретные: 100 % среднего заработка, делитель 730 − исключённые дни, замена лет только по
заявлению и только если выгодна, НДФЛ и взносов нет, при стаже меньше 6 месяцев — потолок
МРОТ за месяц (255-ФЗ).

Выходное пособие: средний месячный заработок = средний дневной × (рабочие дни года / 12),
а не дни конкретного месяца после увольнения (п. 9 Положения № 540); НДФЛ и взносы — только
с превышения трёхкратного размера.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pto_agent import orders, payroll

REPO = Path(__file__).resolve().parents[1]
RULES = payroll.load_rules()
STAFF = payroll.load_json(REPO / "data/demo/payroll/staff.json")
EMPLOYEE = {"tab": "900", "name": "Тестова Анна", "position": "инженер"}


def maternity(**extra) -> dict:
    payload = {"number": "П-1", "kind": "maternity", "tab": "900", "leave": "обычные роды",
               "days": 140, "service_years": 5, "earnings_2y": 1_460_000, "excluded_days": 0}
    payload.update(extra)
    return orders.maternity(payload, EMPLOYEE, RULES, period="2026-11")


def severance(**extra) -> dict:
    payload = {"number": "П-2", "kind": "severance", "tab": "900", "multiple": 1,
               "base_12m": 1_020_000, "worked_days_12m": 200,
               "basis": "сокращение численности, ч. 1 ст. 178 ТК РФ"}
    payload.update(extra)
    return orders.severance(payload, EMPLOYEE, RULES, period="2026-11")


# ─────────────────────────── декретные ───────────────────────────
def test_maternity_is_one_hundred_percent_not_by_seniority():
    row = maternity(service_years=1)
    assert row["sdz"] == pytest.approx(1_460_000 / 730, abs=0.01)
    assert row["amount"] == 0.0, "декретные платит СФР: это не расход организации"
    assert row["fund_amount"] == pytest.approx(row["sdz"] * 140, abs=0.01)


def test_maternity_has_no_ndfl_and_no_contributions():
    row = maternity()
    assert row["taxable"] == 0.0, "п. 1 ст. 217 НК РФ"
    assert row["contrib_base"] == 0.0, "п. 1 ст. 422 НК РФ"


@pytest.mark.parametrize("leave,days", [("обычные роды", 140), ("осложнённые роды", 156),
                                        ("многоплодная беременность", 194)])
def test_maternity_days_by_kind(leave, days):
    row = maternity(leave=leave, days=None)
    assert row["days"] == days
    assert row["fund_amount"] == pytest.approx(row["sdz"] * days, abs=0.01)


def test_excluded_days_shrink_the_divisor():
    plain = maternity(excluded_days=0)
    shorter = maternity(excluded_days=30)
    assert shorter["sdz"] > plain["sdz"]
    assert plain["sdz"] == pytest.approx(1_460_000 / 730, abs=0.01)
    assert shorter["sdz"] == pytest.approx(1_460_000 / 700, abs=0.01)


def test_year_replacement_taken_only_when_bigger():
    row = maternity(replaced={"from_years": [2024, 2025], "to_years": [2022, 2023],
                              "earnings_2y": 1_900_000, "statement": True})
    assert row["sdz"] == pytest.approx(1_900_000 / 730, abs=0.01)
    assert "замена" in row["sdz_source"]
    assert any("замена лет выгоднее" in text for text in row["warnings"])


def test_year_replacement_ignored_when_smaller():
    row = maternity(replaced={"from_years": [2024, 2025], "to_years": [2022, 2023],
                              "earnings_2y": 800_000, "statement": True})
    assert row["sdz"] == pytest.approx(1_460_000 / 730, abs=0.01)
    assert any("не растёт" in text for text in row["warnings"])


def test_year_replacement_without_statement_is_refused():
    row = maternity(replaced={"from_years": [2024, 2025], "to_years": [2022, 2023],
                              "earnings_2y": 1_900_000, "statement": False})
    assert any(item["code"] == "MATERNITY_NO_STATEMENT" for item in row["errors"])


def test_year_replacement_only_backwards():
    row = maternity(replaced={"from_years": [2022, 2023], "to_years": [2024, 2025],
                              "earnings_2y": 1_900_000, "statement": True})
    assert any(item["code"] == "MATERNITY_REPLACE_ORDER" for item in row["errors"])


def test_maternity_capped_at_insurance_base_limit():
    row = maternity(earnings_2y=5_000_000)
    assert row["sdz"] == pytest.approx(6827.40, abs=0.01)
    assert row["capped"]


def test_maternity_raised_to_mrot_floor():
    row = maternity(earnings_2y=300_000)
    assert row["sdz"] == pytest.approx(890.73, abs=0.01)
    assert row["raised_to_min"]


def test_maternity_short_service_capped_by_month():
    row = maternity(service_years=0.25, earnings_2y=3_000_000)
    assert row["capped_by_month"]
    assert row["sdz"] == pytest.approx(27_093 / 30, abs=0.01)   # ноябрь: 30 дней
    assert row["verify"], "цифра при стаже меньше 6 месяцев должна быть помечена на проверку"


def test_maternity_without_base_is_refused():
    row = maternity(earnings_2y=None)
    assert any(item["code"] == "MATERNITY_NO_BASE" for item in row["errors"])
    assert row.get("not_calculated")


def test_maternity_without_days_is_refused():
    row = maternity(days=0, leave="неизвестный вид")
    assert any(item["code"] == "MATERNITY_NO_DAYS" for item in row["errors"])


# ─────────────────────────── выходное пособие ───────────────────────────
def test_severance_uses_average_working_days_of_year():
    row = severance()
    assert row["sdz"] == pytest.approx(1020000 / 200, abs=0.01)
    assert row["working_days_year"] == 247
    assert row["working_days_per_month"] == pytest.approx(247 / 12, abs=0.0001)
    assert row["monthly"] == pytest.approx(5100 * 247 / 12, abs=0.02)
    assert row["amount"] == pytest.approx(row["monthly"], abs=0.02)


def test_severance_not_counted_by_days_of_next_month():
    """Старая ошибка: взять рабочие дни конкретного месяца после увольнения."""
    row = severance()
    by_next_month = round(5100 * 20, 2)          # в ноябре 2026 — 20 рабочих дней
    assert row["amount"] != pytest.approx(by_next_month, abs=0.01)
    assert row["amount"] == pytest.approx(104_975.0, abs=0.02)


def test_severance_is_tax_free_within_three_monthly_amounts():
    row = severance()
    assert row["tax_free_limit"] == pytest.approx(row["monthly"] * 3, abs=0.02)
    assert row["taxable"] == 0.0
    assert row["contrib_base"] == 0.0


def test_severance_above_three_times_is_taxable():
    row = severance(multiple=4)
    assert row["taxable"] == pytest.approx(row["amount"] - row["monthly"] * 3, abs=0.02)
    assert row["verify"], "кратность выше трёх должна быть помечена на проверку"


def test_two_week_severance_is_a_share_and_flagged():
    row = severance(multiple=0.4615)
    assert row["verify"]
    assert any("двухнедельн" in text for text in row["warnings"])


def test_severance_floor_is_mrot():
    row = severance(base_12m=100_000, worked_days_12m=247)
    assert row["monthly"] < RULES["mrot"]
    assert row["amount"] == pytest.approx(RULES["mrot"], abs=0.01)
    assert row["raised_to_min"]


def test_severance_without_days_is_refused():
    row = severance(worked_days_12m=0)
    assert any(item["code"] == "SEVERANCE_NO_DAYS" for item in row["errors"])
    assert row["amount"] == 0.0


def test_severance_without_base_is_refused():
    row = severance(base_12m=None)
    assert any(item["code"] == "SEVERANCE_NO_BASE" for item in row["errors"])


def test_kinds_are_registered():
    assert "maternity" in orders.KINDS and "severance" in orders.KINDS


# ─────────────────────────── в составе месяца ───────────────────────────
def test_maternity_does_not_enter_accrual_but_is_reported():
    parsed = [{"number": "П-3", "date": "2026-11-03", "kind": "maternity", "tab": "006",
               "leave": "обычные роды", "days": 140, "service_years": 4,
               "earnings_2y": 1_460_000}]
    result = orders.report(parsed, STAFF, RULES, period="2026-11")
    assert result["totals"]["maternity_fund"] > 0
    assert result["totals"]["gross"] == 0.0, "декретные не начисляются как доход организации"
    assert result["by_tab"]["006"]["taxable"] == 0
    assert result["by_tab"]["006"]["sick_fund"] == 0.0, "декрет не должен попадать в больничные"


def test_severance_enters_accrual_but_not_tax_base():
    parsed = [{"number": "П-4", "date": "2026-11-20", "kind": "severance", "tab": "003",
               "multiple": 1, "base_12m": 1_020_000, "worked_days_12m": 200}]
    result = orders.report(parsed, STAFF, RULES, period="2026-11")
    assert result["totals"]["gross"] == pytest.approx(104_975.0, abs=0.02)
    assert result["totals"]["taxable"] == 0.0
    assert result["by_tab"]["003"]["gross"] == pytest.approx(104_975.0, abs=0.02)


def test_full_november_month_pays_severance_and_keeps_it_out_of_ndfl():
    timesheet = payroll.load_json(REPO / "data/demo/payroll/timesheet_2026_11.json")
    parsed = orders.load_orders(REPO / "data/demo/payroll/orders_2026_11.json")
    report = payroll.accrue(payroll.new_state(object_name=STAFF["object"]), timesheet, STAFF,
                            RULES, period="2026-11", orders=parsed)
    aslanov = next(row for row in report["rows"] if row["tab"] == "003")
    assert aslanov["tax_free"] == pytest.approx(104_975.0, abs=0.02)
    assert aslanov["net"] == pytest.approx(aslanov["accrued"] - aslanov["ndfl"], abs=0.02)
    assert report["totals"]["tax_free"] == pytest.approx(104_975.0, abs=0.02)
    assert payroll.verify(payroll.new_state() if False else
                          {"periods": [{"period": "2026-11", "rows": report["rows"],
                                        "totals": report["totals"]}]})["ok"]


def test_payout_requests_include_tax_free_severance():
    timesheet = payroll.load_json(REPO / "data/demo/payroll/timesheet_2026_11.json")
    parsed = orders.load_orders(REPO / "data/demo/payroll/orders_2026_11.json")
    state = payroll.new_state(object_name=STAFF["object"])
    report = payroll.accrue(state, timesheet, STAFF, RULES, period="2026-11", orders=parsed)
    payroll.remember(state, report, rules=RULES)
    requests = payroll.payout_requests(state["periods"][0], date="2026-12-05")
    aslanov = next(row for row in report["rows"] if row["tab"] == "003")
    request = next(item for item in requests if item["counterparty"] == aslanov["name"])
    assert request["amount"] == pytest.approx(aslanov["net"], abs=0.02)
    assert request["amount"] > 104_975, "выходное пособие должно попасть в выплату"


def test_month_without_income_does_not_use_deduction():
    """Сотрудница в декрете: дохода нет — вычет не применяется, НДФЛ не отрицательный."""
    timesheet = payroll.load_json(REPO / "data/demo/payroll/timesheet_2026_11.json")
    parsed = orders.load_orders(REPO / "data/demo/payroll/orders_2026_11.json")
    report = payroll.accrue(payroll.new_state(object_name=STAFF["object"]), timesheet, STAFF,
                            RULES, period="2026-11", orders=parsed)
    smetchikova = next(row for row in report["rows"] if row["tab"] == "006")
    assert smetchikova["ndfl"] == 0.0
    assert smetchikova["net"] == 0.0
    assert smetchikova["maternity_fund"] > 0


def test_demo_november_numbers():
    timesheet = payroll.load_json(REPO / "data/demo/payroll/timesheet_2026_11.json")
    parsed = orders.load_orders(REPO / "data/demo/payroll/orders_2026_11.json")
    report = payroll.accrue(payroll.new_state(object_name=STAFF["object"]), timesheet, STAFF,
                            RULES, period="2026-11", orders=parsed)
    assert not report["errors"]
    assert report["totals"]["dismissal"] == pytest.approx(101_535.70, abs=0.02)
    assert report["totals"]["severance"] == pytest.approx(104_975.0, abs=0.02)
    assert report["totals"]["maternity_fund"] == pytest.approx(390_422.20, abs=0.05)


def test_rules_calendar_has_working_days_year_with_source():
    year = RULES["calendar"]["working_days_year"]
    assert year["days"] == 247 and year["source"]
    assert year["verify"], "число рабочих дней помечено «сверить»: переносы в контуре не заложены"


def test_stale_not_implemented_lines_are_gone():
    texts = " ".join(RULES.get("not_implemented") or []).lower()
    assert "компенсация за неиспользованный отпуск" not in texts
    assert "декретные" not in texts
