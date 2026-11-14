# -*- coding: utf-8 -*-
"""Зарплата: НДФЛ по прогрессии, вычеты, взносы, предельная база, проводки.

Проверки на числа, а не на «код не упал»: каждая ставка и порог из НК РФ проверяются
на границе, потому что именно на границе такие расчёты и ломаются.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from pto_agent import ledger, payroll

REPO = Path(__file__).resolve().parents[1]
RULES = payroll.load_rules()
STAFF = payroll.load_json(REPO / "data/demo/payroll/staff.json")


def timesheet(period: str, days: dict[str, int] | None = None, *, norm: int | None = None) -> dict:
    calendar = payroll.work_calendar(period, RULES)
    rows = []
    for employee in STAFF["employees"]:
        rows.append({"tab": employee["tab"],
                     "worked_days": (days or {}).get(employee["tab"], calendar["days"])})
    return {"schema": "pto-agent/timesheet@1", "period": period,
            "object": STAFF["object"],
            "norm": {"days": norm if norm is not None else calendar["days"],
                     "hours": calendar["hours"]},
            "rows": rows}


def employee(salary: float, children: int = 0, **extra) -> dict:
    base = {"tab": "900", "name": "Тестов Тест Тестович", "position": "тест",
            "object": "Объект", "salary": salary, "coefficient": 1.0, "children": children,
            "resident": True, "status": "работает"}
    base.update(extra)
    return base


# ─────────────────────────── прогрессия НДФЛ ───────────────────────────
@pytest.mark.parametrize("base, tax", [
    (0, 0.0),
    (140800, 18304.0),
    (2_400_000, 312_000.0),
    (2_600_000, 342_000.0),
    (5_000_000, 702_000.0),
    (6_000_000, 882_000.0),
    (20_000_000, 3_402_000.0),
    (50_000_000, 9_402_000.0),
    (51_000_000, 9_622_000.0),
])
def test_ndfl_scale_matches_law(base, tax):
    assert float(payroll.ndfl_scale(RULES, Decimal(str(base)))["tax"]) == pytest.approx(tax)


def test_progressive_step_applies_only_to_excess():
    """15 % — только к сумме сверх 2,4 млн, а не ко всему доходу (иначе +48 000 ₽)."""
    scale = payroll.ndfl_scale(RULES, Decimal("2600000"))
    steps = scale["steps"]
    assert [step["rate"] for step in steps] == [13.0, 15.0]
    assert steps[0]["base"] == 2_400_000
    assert steps[1]["base"] == 200_000
    wrong = Decimal("2600000") * Decimal("0.15")
    assert wrong - scale["tax"] == pytest.approx(Decimal("48000"))


def test_month_tax_is_year_to_date_difference():
    """Налог за месяц = налог с начала года − удержанный ранее."""
    person = employee(200_000)
    first = payroll.ndfl_for_month(RULES, employee=person, income_month=Decimal("200000"),
                                   income_ytd_before=Decimal("0"),
                                   withheld_ytd_before=Decimal("0"),
                                   deductions_ytd_before=Decimal("0"))
    assert float(first["tax"]) == pytest.approx(26_000)
    # в году уже 2 300 000 дохода и 299 000 удержано; добавляем 200 000 — переход порога
    second = payroll.ndfl_for_month(RULES, employee=person, income_month=Decimal("200000"),
                                    income_ytd_before=Decimal("2300000"),
                                    withheld_ytd_before=Decimal("299000"),
                                    deductions_ytd_before=Decimal("0"))
    assert second["base_ytd"] == pytest.approx(2_500_000)
    assert float(second["tax"]) == pytest.approx(327_000 - 299_000)


# ─────────────────────────── вычеты ───────────────────────────
def test_child_deductions_by_order():
    assert float(payroll.deductions_of(RULES, {"children": 1}, Decimal("0"))["amount"]) == 1400
    assert float(payroll.deductions_of(RULES, {"children": 2}, Decimal("0"))["amount"]) == 4200
    assert float(payroll.deductions_of(RULES, {"children": 3}, Decimal("0"))["amount"]) == 10200
    assert float(payroll.deductions_of(RULES, {"children": 4}, Decimal("0"))["amount"]) == 16200


def test_sole_parent_gets_double_deduction():
    plain = payroll.deductions_of(RULES, {"children": 1}, Decimal("0"))
    single = payroll.deductions_of(RULES, {"children": 1, "sole_parent": True}, Decimal("0"))
    assert float(single["amount"]) == float(plain["amount"]) * 2


def test_deduction_stops_above_450000():
    below = payroll.deductions_of(RULES, {"children": 2}, Decimal("440000"))
    above = payroll.deductions_of(RULES, {"children": 2}, Decimal("450000"))
    assert float(below["amount"]) == 4200
    assert float(above["amount"]) == 0
    assert "450" in above["why"]


def test_deduction_limit_applies_in_the_crossing_month():
    """Месяц, в котором порог пройден, вычета уже не даёт."""
    person = employee(150_000, children=1)
    result = payroll.ndfl_for_month(RULES, employee=person, income_month=Decimal("150000"),
                                    income_ytd_before=Decimal("450000"),
                                    withheld_ytd_before=Decimal("0"),
                                    deductions_ytd_before=Decimal("0"))
    assert result["deduction"] == 0
    assert result["base_ytd"] == pytest.approx(600_000)


def test_non_resident_is_flat_30_without_deductions():
    person = employee(100_000, children=2, resident=False)
    result = payroll.ndfl_for_month(RULES, employee=person, income_month=Decimal("100000"),
                                    income_ytd_before=Decimal("0"),
                                    withheld_ytd_before=Decimal("0"),
                                    deductions_ytd_before=Decimal("0"))
    assert result["resident"] is False
    assert result["deduction"] == 0
    assert float(result["tax"]) == pytest.approx(30_000)


# ─────────────────────────── взносы ───────────────────────────
def test_contributions_30_percent_below_limit():
    result = payroll.contributions_for_month(RULES, income_month=Decimal("100000"),
                                             base_ytd_before=Decimal("0"))
    assert float(result["total"]) == pytest.approx(30_000)
    assert float(result["injury"]) == pytest.approx(2_100)


def test_contributions_switch_to_15_1_above_limit():
    """На границе предельной базы взносы не должны считаться «по 30 % на всё»."""
    before = Decimal("2_950_000")
    result = payroll.contributions_for_month(RULES, income_month=Decimal("100000"),
                                             base_ytd_before=before)
    expected = (Decimal("29000") * Decimal("0.30")
                + Decimal("71000") * Decimal("0.151"))
    assert float(result["total"]) == pytest.approx(float(expected)) == pytest.approx(19_421)
    assert result["limit_reached"] is True
    assert "15,1" in result["note"]


def test_injury_has_no_base_limit():
    result = payroll.contributions_for_month(RULES, income_month=Decimal("500000"),
                                             base_ytd_before=Decimal("2_979_000"))
    assert float(result["injury"]) == pytest.approx(500_000 * 0.021)


# ─────────────────────────── табель ───────────────────────────
def test_calendar_october_2026():
    calendar = payroll.work_calendar("2026-10", RULES)
    assert calendar["days"] == 22 and calendar["hours"] == 176


def test_timesheet_without_norm_is_refused():
    sheet = timesheet("2026-10")
    sheet["norm"] = {}
    norm, errors, gaps = payroll.check_timesheet(sheet, STAFF, RULES)
    assert any(item["code"] == "NO_NORM" for item in errors)


def test_timesheet_norm_mismatch_is_refused():
    sheet = timesheet("2026-10", norm=21)
    _, errors, _ = payroll.check_timesheet(sheet, STAFF, RULES)
    assert any(item["code"] == "NORM_MISMATCH" for item in errors)


def test_overtime_is_not_paid_silently():
    sheet = timesheet("2026-10", {"001": 25})
    _, errors, _ = payroll.check_timesheet(sheet, STAFF, RULES)
    assert any(item["code"] == "OVER_NORM" and item["what"] == "001" for item in errors)


def test_missing_employee_row_is_a_gap_not_zero():
    sheet = timesheet("2026-10")
    sheet["rows"] = [row for row in sheet["rows"] if row["tab"] != "003"]
    _, errors, gaps = payroll.check_timesheet(sheet, STAFF, RULES)
    assert not errors
    assert any("Асланов" in gap["what"] for gap in gaps)


# ─────────────────────────── начисление ───────────────────────────
def test_partial_month_proportional_to_days():
    sheet = timesheet("2026-10", {"005": 20})
    report = payroll.accrue(payroll.new_state(), sheet, STAFF, RULES)
    row = next(item for item in report["rows"] if item["tab"] == "005")
    assert row["accrued"] == pytest.approx(92_000 / 22 * 20, abs=0.01)


def test_accrual_totals_match_rows():
    report = payroll.accrue(payroll.new_state(), timesheet("2026-10"), STAFF, RULES)
    totals = report["totals"]
    assert totals["accrued"] == pytest.approx(sum(row["accrued"] for row in report["rows"]),
                                              abs=0.01)
    assert totals["net"] == pytest.approx(
        sum(row["accrued"] - row["ndfl"] for row in report["rows"]), abs=0.01)
    assert totals["cost"] == pytest.approx(
        totals["accrued"] + totals["contrib"] + totals["injury"], abs=0.01)


def test_second_month_counts_year_to_date():
    state = payroll.new_state()
    first = payroll.accrue(state, timesheet("2026-09"), STAFF, RULES)
    payroll.remember(state, first, rules=RULES)
    second = payroll.accrue(state, timesheet("2026-10"), STAFF, RULES)
    row = next(item for item in second["rows"] if item["tab"] == "007")
    assert row["income_ytd"] == pytest.approx(520_000)
    # начисление за месяц у директора не менялось, налог тоже: порог 2,4 млн не пройден
    assert row["ndfl"] == pytest.approx(33_800)


def test_same_month_is_not_accrued_twice():
    state = payroll.new_state()
    first = payroll.accrue(state, timesheet("2026-10"), STAFF, RULES)
    payroll.remember(state, first, rules=RULES)
    again = payroll.accrue(state, timesheet("2026-10"), STAFF, RULES)
    assert again["duplicate"] is True
    assert len(state["periods"]) == 1


def test_verify_catches_broken_total():
    state = payroll.new_state()
    report = payroll.accrue(state, timesheet("2026-10"), STAFF, RULES)
    payroll.remember(state, report, rules=RULES)
    assert payroll.verify(state)["ok"] is True
    state["periods"][0]["totals"]["ndfl"] += 1
    problems = payroll.verify(state)["problems"]
    assert any(item["code"] == "TOTAL_MISMATCH" for item in problems)


# ─────────────────────────── проводки ───────────────────────────
def test_ledger_postings_balance_and_land_on_right_accounts():
    state = payroll.new_state()
    report = payroll.accrue(state, timesheet("2026-10"), STAFF, RULES)
    ledger_state = ledger.new_state(object_name=STAFF["object"])
    result = payroll.post_ledger(ledger_state, report)
    assert result["posted"] == 4 and result["rejected"] == 0
    assert ledger.verify(ledger_state)["ok"] is True
    events = [entry["event"] for entry in ledger_state["entries"]]
    assert events == ["payroll_accrual", "payroll_contributions", "payroll_injury",
                      "payroll_ndfl"]

    def saldo(account: str) -> Decimal:
        return sum(ledger.balance_as_of(ledger_state, account, "2026-10-31").values(),
                   Decimal("0"))

    assert saldo("70") == pytest.approx(-Decimal(str(report["totals"]["net"])))
    assert saldo("68.01") == pytest.approx(-Decimal(str(report["totals"]["ndfl"])))
    assert saldo("69.01") == pytest.approx(-Decimal(str(report["totals"]["contrib"])))
    assert saldo("69.11") == pytest.approx(-Decimal(str(report["totals"]["injury"])))
    assert saldo("20.01") == pytest.approx(Decimal(str(report["totals"]["cost"])))


def test_repeat_posting_is_duplicate_not_second_accrual():
    state = payroll.new_state()
    report = payroll.accrue(state, timesheet("2026-10"), STAFF, RULES)
    ledger_state = ledger.new_state(object_name=STAFF["object"])
    payroll.post_ledger(ledger_state, report)
    again = payroll.post_ledger(ledger_state, report)
    assert again["posted"] == 0 and again["duplicates"] == 4
    assert len(ledger_state["entries"]) == 4


def test_payout_requests_carry_basis_and_article():
    state = payroll.new_state()
    report = payroll.accrue(state, timesheet("2026-10"), STAFF, RULES)
    requests = payroll.payout_requests(report, date="2026-11-05")
    assert len(requests) == len(report["rows"])
    for request in requests:
        assert request["article"] == "зарплата"
        assert request["basis"]["number"] == "Т-51 за 2026-10"
        assert request["amount"] > 0
    assert sum(item["amount"] for item in requests) == pytest.approx(
        report["totals"]["net"], abs=0.02)


def test_payroll_rates_come_from_data_file_not_code():
    payload = json.loads((REPO / "data/reference/payroll_rules.json").read_text(
        encoding="utf-8"))
    assert payload["schema"] == "pto-agent/payroll-rules@1"
    assert payload["contributions"]["base_limit"] == 2_979_000
    assert payload["deductions"]["income_limit"] == 450_000
    assert payload["ndfl"]["resident_brackets"][-1]["rate"] == 22
    assert payload["source"]["items"], "у каждой ставки должен быть источник"


# ─────────────────────────── выплата через казначейство ───────────────────────────
def payout_setup():
    from pto_agent import treasury

    report = payroll.accrue(payroll.new_state(), timesheet("2026-10"), STAFF, RULES)
    books = ledger.new_state(object_name=STAFF["object"])
    payroll.post_ledger(books, report)
    cash = treasury.new_state(object_name=STAFF["object"],
                              opening={"date": "2026-11-01", "amount": 2_000_000,
                                       "basis": "остаток"})
    requests = payroll.payout_requests(report, date="2026-11-05")
    for request in requests:
        treasury.add_request(cash, request)
    return treasury, cash, books, report, requests


def test_salary_route_goes_by_article_not_by_amount():
    treasury, cash, _, _, requests = payout_setup()
    route = treasury.find(cash, requests[0]["number"])["route"]
    assert route == ["руководитель"], "зарплату не согласует снабжение, даже если сумма мелкая"


def test_salary_payment_closes_seventy():
    treasury, cash, books, report, requests = payout_setup()
    for request in requests:
        treasury.approve(cash, request["number"], role="руководитель",
                         person="Директоров Д.Д.", date="2026-11-05")
        result = treasury.pay(cash, request["number"], date="2026-11-05",
                              person="Бухгалтерова М.П.")
        assert not result["errors"], result["errors"]
    posted = treasury.post_to_ledger(cash, books)
    assert posted["rejected"] == 0, posted["errors"]
    seventy = sum(ledger.balance_as_of(books, "70", "2026-11-30").values(), Decimal("0"))
    cash_left = sum(ledger.balance_as_of(books, "51", "2026-11-30").values(), Decimal("0"))
    assert seventy == pytest.approx(Decimal("0"), abs=Decimal("0.01"))
    assert cash_left == pytest.approx(Decimal(str(2_000_000 - report["totals"]["net"])),
                                      abs=Decimal("0.01"))
    assert ledger.verify(books)["ok"] is True


def test_payment_before_accrual_is_refused():
    from pto_agent import treasury

    report = payroll.accrue(payroll.new_state(), timesheet("2026-10"), STAFF, RULES)
    cash = treasury.new_state(object_name=STAFF["object"],
                              opening={"date": "2026-11-01", "amount": 1_000_000,
                                       "basis": "остаток"})
    requests = payroll.payout_requests(report, date="2026-11-05")
    treasury.add_request(cash, requests[0])
    treasury.approve(cash, requests[0]["number"], role="руководитель", person="Директоров Д.Д.",
                     date="2026-11-05")
    treasury.pay(cash, requests[0]["number"], date="2026-11-05", person="Бухгалтерова М.П.")
    books = ledger.new_state(object_name=STAFF["object"])   # начисления не проведены
    posted = treasury.post_to_ledger(cash, books)
    codes = [item["code"] for item in posted["errors"]]
    assert "PAYMENT_OVER_WAGES" in codes, "выплата вперёд начисления прошла молча"


def test_bdr_shows_payroll_as_articles():
    from pto_agent import treasury

    treasury_mod, cash, books, report, requests = payout_setup()
    bdr = treasury.bdr(cash, books, "2026-10")
    rows = {row["article"]: row for row in bdr["rows"]}
    assert rows["зарплата"]["fact"] == pytest.approx(report["totals"]["accrued"])
    assert rows["взносы"]["fact"] == pytest.approx(report["totals"]["contrib"]
                                                   + report["totals"]["injury"])
    assert bdr["unallocated_total"] == 0, "зарплата должна иметь статью затрат, а не «прочее»"
    assert bdr["expense_fact"] == pytest.approx(report["totals"]["cost"])
