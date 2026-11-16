# -*- coding: utf-8 -*-
"""Удержания из зарплаты: пределы ст. 138 ТК РФ, алименты, очерёдность, проводки.

Проверки на числа: каждая граница (20 / 50 / 70 %, база после НДФЛ, перенос остатка)
проверяется отдельно, потому что ошибка здесь — это или долг перед взыскателем,
или нарушение трудового права.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from pto_agent import ledger, payroll, withholdings

REPO = Path(__file__).resolve().parents[1]
RULES = payroll.load_rules()
WH = withholdings.load_rules()
STAFF = payroll.load_json(REPO / "data/demo/payroll/staff.json")


def timesheet(period: str = "2026-10") -> dict:
    calendar = payroll.work_calendar(period, RULES)
    return {"schema": "pto-agent/timesheet@1", "period": period, "object": STAFF["object"],
            "norm": {"days": calendar["days"], "hours": calendar["hours"]},
            "rows": [{"tab": employee["tab"], "worked_days": calendar["days"]}
                     for employee in STAFF["employees"]]}


def period(rows: list[dict], period_name: str = "2026-10") -> dict:
    """Минимальный «расчёт месяца» для проверки удержаний — без НДФЛ и взносов."""
    return {"period": period_name, "rows": rows}


def row(tab: str, net: float, name: str = "Тестов Тест") -> dict:
    return {"tab": tab, "name": name, "net": net}


# ─────────────────────────── пределы ───────────────────────────
def test_limits_come_from_reference_not_code():
    limits = WH["limits"]
    assert limits["employer"]["percent"] == 20      # ч. 1 ст. 138 ТК РФ
    assert limits["enforcement"]["percent"] == 50   # ч. 2 ст. 138 ТК РФ
    assert limits["priority"]["percent"] == 70      # ч. 3 ст. 138 ТК РФ
    for item in limits.values():
        assert item["basis"], "у предела нет нормы-источника"


def test_limit_is_counted_from_amount_after_ndfl_not_from_accrual():
    """Главная ошибка: считать 50 % от начисления. База — сумма после НДФЛ."""
    result = withholdings.apply(period([row("001", 10000.0)]),
                                [{"tab": "001", "kind": "writ", "number": "ИЛ-1",
                                  "amount": 9000, "payee": "Банк"}])
    slot = result["rows"][0]
    assert slot["limit_amount"] == 5000.0
    assert slot["withheld"] == 5000.0
    assert slot["carried"] == 4000.0
    assert slot["documents"][0]["state"] == withholdings.LIMITED


def test_employer_order_has_twenty_percent_limit_not_fifty():
    result = withholdings.apply(period([row("001", 10000.0)]),
                                [{"tab": "001", "kind": "employer_order", "number": "РП-1",
                                  "amount": 8000, "payee": "касса",
                                  "basis": "счётная ошибка"}])
    slot = result["rows"][0]
    assert slot["withheld"] == 2000.0
    assert slot["carried"] == 6000.0


def test_alimony_keeps_seventy_percent_headroom():
    result = withholdings.apply(period([row("001", 10000.0)]),
                                [{"tab": "001", "kind": "alimony_fixed", "number": "ИЛ-2",
                                  "amount": 6800, "payee": "Алименты"}])
    slot = result["rows"][0]
    assert slot["withheld"] == 6800.0, "алименты не должны упираться в 50 %"
    assert slot["carried"] == 0.0


# ─────────────────────────── алименты в долях ───────────────────────────
@pytest.mark.parametrize("children,share", [(1, Decimal("0.25")), (2, Decimal("1") / 3),
                                            (3, Decimal("0.5")), (5, Decimal("0.5"))])
def test_alimony_share_by_children(children, share):
    assert withholdings.share_of(children, WH)[0] == share


def test_alimony_share_counts_from_net():
    result = withholdings.apply(period([row("002", 90000.0)]),
                                [{"tab": "002", "kind": "alimony_share", "number": "ИЛ ВС-1",
                                  "children": 2, "payee": "ОСП"}])
    assert result["rows"][0]["withheld"] == 30000.0


def test_alimony_without_children_is_refused():
    result = withholdings.apply(period([row("002", 90000.0)]),
                                [{"tab": "002", "kind": "alimony_share", "number": "ИЛ ВС-2",
                                  "children": 0, "payee": "ОСП"}])
    codes = [item["code"] for item in result["errors"]]
    assert "ALIMONY_NO_CHILDREN" in codes
    assert result["rows"][0]["withdrawn" if False else "withheld"] == 0.0


# ─────────────────────────── документы ───────────────────────────
@pytest.mark.parametrize("document,code", [
    ({"tab": "001", "kind": "writ", "number": "", "amount": 5000, "payee": "Банк"},
     "WITHHOLDING_NO_NUMBER"),
    ({"tab": "001", "kind": "writ", "number": "ИЛ-3", "amount": 5000, "payee": ""},
     "WITHHOLDING_NO_PAYEE"),
    ({"tab": "001", "kind": "writ", "number": "ИЛ-4", "payee": "Банк"},
     "WITHHOLDING_NO_AMOUNT"),
    ({"tab": "001", "kind": "магия", "number": "ИЛ-5", "amount": 100, "payee": "Банк"},
     "WITHHOLDING_KIND"),
    ({"tab": "001", "kind": "employer_order", "number": "РП-2", "amount": 100,
      "payee": "касса"}, "EMPLOYER_ORDER_NO_BASIS"),
])
def test_broken_document_is_named_not_silently_zero(document, code):
    result = withholdings.apply(period([row("001", 10000.0)]), [document])
    assert code in [item["code"] for item in result["errors"]]
    assert result["rows"][0]["withheld"] == 0.0
    assert result["rows"][0]["documents"][0]["state"] == withholdings.SKIPPED
    assert not result["ready"]


def test_percent_over_hundred_is_refused():
    result = withholdings.apply(period([row("001", 10000.0)]),
                                [{"tab": "001", "kind": "writ", "number": "ИЛ-6",
                                  "percent": 120, "payee": "Банк"}])
    assert "WITHHOLDING_PERCENT" in [item["code"] for item in result["errors"]]


def test_document_of_unknown_employee_is_a_problem():
    result = withholdings.apply(period([row("001", 10000.0)]),
                                [{"tab": "999", "kind": "writ", "number": "ИЛ-7",
                                  "amount": 100, "payee": "Банк"}])
    assert "WITHHOLDING_NO_EMPLOYEE" in [item["code"] for item in result["problems"]]


# ─────────────────────────── очерёдность ───────────────────────────
def test_queue_one_is_paid_before_credit():
    result = withholdings.apply(
        period([row("001", 10000.0)]),
        [{"tab": "001", "kind": "writ", "number": "Кредит", "amount": 4000, "payee": "Банк"},
         {"tab": "001", "kind": "alimony_fixed", "number": "Алименты", "amount": 7000,
          "payee": "Бывшая супруга"}])
    by_number = {item["number"]: item for item in result["rows"][0]["documents"]}
    assert by_number["Алименты"]["withheld"] == 7000.0
    assert by_number["Кредит"]["withheld"] == 0.0
    assert by_number["Кредит"]["carried"] == 4000.0


def test_same_queue_is_split_proportionally():
    result = withholdings.apply(
        period([row("001", 10000.0)]),
        [{"tab": "001", "kind": "writ", "number": "А", "amount": 6000, "payee": "Банк А"},
         {"tab": "001", "kind": "writ", "number": "Б", "amount": 6000, "payee": "Банк Б"}])
    amounts = [item["withheld"] for item in result["rows"][0]["documents"]]
    assert amounts == [2500.0, 2500.0]


# ─────────────────────────── интеграция с начислением ───────────────────────────
def test_accrual_net_is_after_ndfl_and_withholdings():
    report = payroll.accrue(payroll.new_state(object_name=STAFF["object"]), timesheet(), STAFF,
                            RULES, withholdings=[{"tab": "001", "kind": "writ",
                                                  "number": "ИЛ-8", "amount": 1000,
                                                  "payee": "Банк"}])
    row_001 = next(item for item in report["rows"] if item["tab"] == "001")
    assert row_001["net"] == pytest.approx(row_001["accrued_net"] - 1000.0, abs=0.01)
    assert report["totals"]["net"] == pytest.approx(
        report["totals"]["net_accrued"] - report["totals"]["withheld"], abs=0.01)


def test_repeated_calculation_does_not_withhold_twice():
    state = payroll.new_state(object_name=STAFF["object"])
    documents = [{"tab": "002", "kind": "alimony_share", "number": "ИЛ ВС-9", "children": 2,
                  "payee": "ОСП"}]
    report = payroll.accrue(state, timesheet(), STAFF, RULES, withholdings=documents)
    row_002 = next(item for item in report["rows"] if item["tab"] == "002")
    again = withholdings.apply({"period": "2026-10", "rows": [row_002]}, documents)
    assert again["rows"][0]["withheld"] == pytest.approx(row_002["withheld"], abs=0.01)


def test_withholdings_do_not_change_ndfl_or_contributions():
    plain = payroll.accrue(payroll.new_state(object_name=STAFF["object"]), timesheet(), STAFF,
                           RULES)
    with_wh = payroll.accrue(payroll.new_state(object_name=STAFF["object"]), timesheet(), STAFF,
                             RULES, withholdings=[{"tab": "001", "kind": "writ",
                                                   "number": "ИЛ-10", "amount": 1000,
                                                   "payee": "Банк"}])
    assert plain["totals"]["ndfl"] == with_wh["totals"]["ndfl"]
    assert plain["totals"]["contrib"] == with_wh["totals"]["contrib"]
    assert plain["totals"]["accrued"] == with_wh["totals"]["accrued"]


def test_absent_registry_is_named_not_assumed_empty():
    report = payroll.accrue(payroll.new_state(object_name=STAFF["object"]), timesheet(), STAFF,
                            RULES)
    assert any("удержания" in (item["what"] or "") for item in report["not_included"])
    assert "withheld" not in report["totals"]


def test_broken_document_does_not_block_the_month():
    report = payroll.accrue(payroll.new_state(object_name=STAFF["object"]), timesheet(), STAFF,
                            RULES, withholdings=[{"tab": "001", "kind": "writ", "number": "",
                                                  "amount": 1000, "payee": "Банк"}])
    assert report["rows"], "оклад должен посчитаться даже при пустом листе"
    assert report["withholding_errors"], "ошибка документа не показана"


# ─────────────────────────── проводки и заявки ───────────────────────────
def _accrued_with_withholdings() -> tuple[dict, dict, dict]:
    state = payroll.new_state(object_name=STAFF["object"])
    documents = [{"tab": "001", "kind": "writ", "number": "ИЛ-11", "amount": 1000,
                  "payee": "Банк А"},
                 {"tab": "007", "kind": "alimony_fixed", "number": "ИЛ-12", "amount": 5000,
                  "payee": "Бывшая супруга"}]
    report = payroll.accrue(state, timesheet(), STAFF, RULES, withholdings=documents)
    payroll.remember(state, report, rules=RULES)
    return state, report, report["periods"][0] if "periods" in report else state["periods"][0]


def test_postings_close_seventy_and_credit_seventy_six_per_payee():
    state = payroll.new_state(object_name=STAFF["object"])
    report = payroll.accrue(state, timesheet(), STAFF, RULES,
                            withholdings=[{"tab": "001", "kind": "writ", "number": "ИЛ-13",
                                           "amount": 1000, "payee": "Банк А"},
                                          {"tab": "007", "kind": "alimony_fixed",
                                           "number": "ИЛ-14", "amount": 5000,
                                           "payee": "Бывшая супруга"}])
    payroll.remember(state, report, rules=RULES)
    ledger_state = ledger.new_state(object_name=STAFF["object"])
    posted = payroll.post_ledger(ledger_state, state["periods"][0])
    assert posted["rejected"] == 0, posted["errors"]
    withheld = [entry for entry in ledger_state["entries"]
                if entry.get("event") == "payroll_withheld"]
    assert len(withheld) == 2, "проводка должна идти по каждому взыскателю отдельно"
    payees = {(line.get("analytics") or {}).get("контрагент")
              for entry in withheld for line in entry["lines"] if line["account"] == "76"}
    assert payees == {"Банк А", "Бывшая супруга"}
    seventy = 0.0
    for entry in ledger_state["entries"]:
        for line in entry["lines"]:
            if line["account"] == "70":
                seventy += line["amount"] if line["side"] == ledger.CREDIT else -line["amount"]
    assert seventy == pytest.approx(report["totals"]["net"], abs=0.01)
    assert ledger.verify(ledger_state)["ok"]


def test_payout_request_uses_amount_after_withholdings():
    state = payroll.new_state(object_name=STAFF["object"])
    report = payroll.accrue(state, timesheet(), STAFF, RULES,
                            withholdings=[{"tab": "001", "kind": "writ", "number": "ИЛ-15",
                                           "amount": 1000, "payee": "Банк"}])
    payroll.remember(state, report, rules=RULES)
    requests = payroll.payout_requests(state["periods"][0], date="2026-11-05")
    row_001 = next(item for item in report["rows"] if item["tab"] == "001")
    request = next(item for item in requests if item["counterparty"] == row_001["name"])
    assert request["amount"] == pytest.approx(row_001["net"], abs=0.01)
    assert "за вычетом удержаний" in request["purpose"]


def test_transfer_requests_go_to_payees_not_employees():
    state = payroll.new_state(object_name=STAFF["object"])
    report = payroll.accrue(state, timesheet(), STAFF, RULES,
                            withholdings=[{"tab": "001", "kind": "writ", "number": "ИЛ-16",
                                           "amount": 1000, "payee": "Банк А"}])
    payroll.remember(state, report, rules=RULES)
    transfer = payroll.withholding_requests(state["periods"][0], date="2026-11-05")
    assert len(transfer) == 1
    assert transfer[0]["counterparty"] == "Банк А"
    assert transfer[0]["amount"] == pytest.approx(report["totals"]["withheld"], abs=0.01)


def test_verify_accepts_withholdings_and_catches_broken_net():
    state = payroll.new_state(object_name=STAFF["object"])
    report = payroll.accrue(state, timesheet(), STAFF, RULES,
                            withholdings=[{"tab": "001", "kind": "writ", "number": "ИЛ-17",
                                           "amount": 1000, "payee": "Банк"}])
    payroll.remember(state, report, rules=RULES)
    assert payroll.verify(state)["ok"]
    state["periods"][0]["rows"][0]["net"] -= 100
    broken = payroll.verify(state)
    assert not broken["ok"]
    codes = [item["code"] for item in broken["problems"]]
    assert "NET_MISMATCH" in codes
    assert "TOTAL_MISMATCH" in codes


# ─────────────────────────── отчёт ───────────────────────────
def test_markdown_names_limits_and_not_implemented():
    result = withholdings.apply(period([row("001", 10000.0)]),
                                [{"tab": "001", "kind": "writ", "number": "ИЛ-18",
                                  "amount": 9000, "payee": "Банк"}])
    text = withholdings.to_markdown(result)
    assert "после НДФЛ" in text
    assert "остаток" in text.lower()
    assert "Не сделано" in text


def test_demo_registry_is_honest_about_broken_document():
    documents = json.loads((REPO / "data/demo/payroll/withholdings_2026_10.json")
                           .read_text(encoding="utf-8"))["documents"]
    assert any(not (item.get("number") or "").strip() for item in documents), \
        "в демо нет примера документа без номера — отказ нечем показать"
