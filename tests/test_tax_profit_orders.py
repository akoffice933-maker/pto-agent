# -*- coding: utf-8 -*-
"""Налог на прибыль, НДС с авансов и приказы (больничные, отпускные, премии).

Проверяется то, из-за чего цифры в декларации становятся выдуманными:

1. прибыль считается от реализации: 90.01 − 90.03 − 90.02, и только после закрытия месяца;
2. незакрытый месяц — это `PROFIT_NOT_CLOSED`, а не завышенная прибыль;
3. ставки 8 % и 17 % дают ровно 25 %, убыток не превращается в ноль;
4. НДС с авансов — отдельные строки, вычет только по зачтённой части;
5. авансовый НДС, не осевший в проводках, назван расхождением, а не забыт;
6. больничный: 3 дня работодателя, остальное СФР, процент по стажу, предел 6 827,40 ₽;
7. без заработка за два года пособие **не считается** (ноль и код), а не берётся из МРОТ;
8. отпускные — база/12/29,3; премия — сумма из приказа;
9. пособие входит в доход для НДФЛ, но не в базу по взносам.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from pto_agent import closing, ledger, orders, payroll, stock, tax

REPO = Path(__file__).resolve().parents[1]
OBJECT = "ЖК «Северный», корпус 2"


def _acts() -> dict:
    payload = json.loads((REPO / "data/demo/accounting/acts_signed_scenario.json")
                         .read_text(encoding="utf-8"))
    payload["acts"][0]["vat"] = {"rate": 22.0, "included": False}
    return payload


@pytest.fixture(scope="module")
def closed_journal() -> dict:
    state = ledger.new_state(object_name=OBJECT)
    ledger.post_acts(state, _acts(), contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    stock_state = stock.demo_state()
    ledger.post_stock(state, stock_state,
                      documents=json.loads((REPO / "data/demo/stock/documents_2026_10.json")
                                           .read_text(encoding="utf-8")))
    for account, amount, analytics in (
            ("25", 150_000, {"объект": OBJECT, "статья затрат": "механизмы"}),
            ("26", 50_000, {"статья затрат": "прочие"})):
        ledger.post_entry(state, event="overhead", date="2026-10-15",
                          doc={"number": f"счёт {account}"},
                          lines=[{"account": account, "side": ledger.DEBIT, "amount": amount,
                                  "analytics": analytics},
                                 {"account": "60.01", "side": ledger.CREDIT, "amount": amount,
                                  "analytics": {"контрагент": "ООО «Механизация»"}}])
    report = closing.close_month(state, "2026-10", stock_state=stock_state)
    assert report["closed"], [step for step in report["steps"] if step["status"] != "выполнено"]
    return state


@pytest.fixture(scope="module")
def open_journal() -> dict:
    state = ledger.new_state(object_name=OBJECT)
    ledger.post_acts(state, _acts(), contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    ledger.post_stock(state, stock.demo_state(),
                      documents=json.loads((REPO / "data/demo/stock/documents_2026_10.json")
                                           .read_text(encoding="utf-8")))
    return state


@pytest.fixture(scope="module")
def rules() -> dict:
    return payroll.load_rules()


@pytest.fixture(scope="module")
def staff() -> dict:
    return json.loads((REPO / "data/demo/payroll/staff.json").read_text(encoding="utf-8"))


# ─────────────────────────── прибыль ───────────────────────────
def test_profit_from_sales_only(closed_journal):
    report = tax.profit_return(closed_journal, "2026-10")
    assert report["totals"]["revenue"] == pytest.approx(34_106_600.00)
    # цифры сверены с docs/47 и README: тот же сценарий, 25 — 150 000, 26 — 50 000
    assert report["totals"]["cost"] == pytest.approx(2_826_500.00)
    assert report["totals"]["profit"] == pytest.approx(31_280_100.00)
    assert not report["problems"]
    assert report["ready"]


def test_profit_rates_split_eight_and_seventeen(closed_journal):
    totals = tax.profit_return(closed_journal, "2026-10")["totals"]
    assert totals["federal_rate"] + totals["regional_rate"] == 25
    assert totals["federal_tax"] == pytest.approx(round(totals["profit"] * 0.08, 2))
    assert totals["regional_tax"] == pytest.approx(round(totals["profit"] * 0.17, 2))
    assert totals["tax"] == pytest.approx(totals["federal_tax"] + totals["regional_tax"])
    assert totals["tax"] == pytest.approx(7_820_025.00)


def test_profit_names_what_it_does_not_count(closed_journal):
    report = tax.profit_return(closed_journal, "2026-10")
    names = " ".join(item["what"] for item in report["not_included"])
    for expected in ("внереализационные", "убыток прошлых лет", "ПБУ 18/02", "авансовые платежи"):
        assert expected in names
    assert "<НеСчитается>" in tax.to_xml(report)


def test_profit_refuses_before_closing(open_journal):
    report = tax.profit_return(open_journal, "2026-10")
    codes = [item["code"] for item in report["problems"]]
    assert "PROFIT_NOT_CLOSED" in codes
    assert "месяц не закрыт" in next(item["why"] for item in report["problems"]
                                     if item["code"] == "PROFIT_NOT_CLOSED")
    assert not report["ready"]


def test_profit_loss_is_named_loss_not_zero():
    state = ledger.new_state(object_name=OBJECT)
    ledger.post_entry(state, event="overhead", date="2026-10-15", doc={"number": "счёт №77"},
                      lines=[{"account": "90.02", "side": ledger.DEBIT, "amount": 100_000,
                              "analytics": {"объект": OBJECT}},
                             {"account": "60.01", "side": ledger.CREDIT, "amount": 100_000,
                              "analytics": {"контрагент": "ООО «Механизация»"}}])
    report = tax.profit_return(state, "2026-10")
    assert "PROFIT_LOSS" in [item["code"] for item in report["problems"]]
    assert report["totals"]["profit"] == pytest.approx(-100_000.00)
    assert report["totals"]["tax"] == 0


# ─────────────────────────── НДС с авансов ───────────────────────────
@pytest.fixture(scope="module")
def journal_with_advance() -> dict:
    state = ledger.new_state(object_name=OBJECT)
    ledger.post_acts(state, _acts(), contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    analytics = {"контрагент": "ООО «Заказчик-Инвест»", "договор": "Д-142/2026"}
    ledger.post_entry(state, event="receipt_payment", date="2026-10-05",
                      doc={"number": "п/п аванс 55"},
                      lines=[{"account": "51", "side": ledger.DEBIT, "amount": 12_200_000,
                              "analytics": {}},
                             {"account": "62.02", "side": ledger.CREDIT, "amount": 12_200_000,
                              "analytics": analytics}])
    outcome = ledger.post_entry(state, event="advance_offset", date="2026-10-29",
                                doc={"number": "зачёт аванса"},
                                lines=[{"account": "62.02", "side": ledger.DEBIT,
                                        "amount": 6_100_000, "analytics": analytics},
                                       {"account": "62.01", "side": ledger.CREDIT,
                                        "amount": 6_100_000, "analytics": analytics}])
    assert outcome["status"] == ledger.STATUS_OK, outcome["errors"]
    return state


def test_advance_vat_by_rate(journal_with_advance):
    report = tax.vat_return(journal_with_advance, "2026-10")
    advances = report["advances"]
    assert advances["rate"] == 22.0
    assert advances["received"] == pytest.approx(12_200_000.00)
    assert advances["vat_on_received"] == pytest.approx(2_200_000.00)
    assert advances["offset"] == pytest.approx(6_100_000.00)
    assert advances["vat_on_offset"] == pytest.approx(1_100_000.00)
    assert advances["to_pay"] == pytest.approx(1_100_000.00)


def test_advance_vat_goes_to_payable(journal_with_advance):
    report = tax.vat_return(journal_with_advance, "2026-10")
    totals = report["totals"]
    expected = totals["vat_charged"] - totals["vat_deducted"] + totals["vat_on_advances"] \
        - totals["vat_on_advances_offset"]
    assert totals["payable"] == pytest.approx(round(expected, 2))
    assert "VAT_TOTALS" not in [item["code"] for item in tax.validate(report)]


def test_advance_vat_not_posted_is_named(journal_with_advance):
    report = tax.vat_return(journal_with_advance, "2026-10")
    problem = next((item for item in report["problems"]
                    if item["code"] == "VAT_ADVANCE_NOT_POSTED"), None)
    assert problem is not None
    assert "76.АВ" in problem["why"]
    assert all(row["verify"] for row in report["advances"]["lines"])
    assert "АвансыНДС" in tax.to_xml(report)


def test_advance_vat_postings_close_the_gap(journal_with_advance):
    """Проводки на 76.АВ снимают VAT_ADVANCE_NOT_POSTED: декларация и журнал сходятся."""
    state = journal_with_advance
    before = tax.vat_return(state, "2026-10")
    assert "VAT_ADVANCE_NOT_POSTED" in [item["code"] for item in before["problems"]]
    result = tax.post_advance_vat(state, "2026-10")
    assert result["posted"] == 2 and not result["errors"]
    assert result["accrued"] == pytest.approx(2_200_000.00)
    assert result["deducted"] == pytest.approx(1_100_000.00)
    after = tax.vat_return(state, "2026-10")
    assert "VAT_ADVANCE_NOT_POSTED" not in [item["code"] for item in after["problems"]]
    assert after["advances"]["in_ledger"] == {"accrued": 2_200_000.0, "deducted": 1_100_000.0,
                                              "account": "76.АВ"}
    # на 76.АВ остаётся незакрытый аванс: 2 200 000 − 1 100 000
    balance = sum(ledger.balance_as_of(state, "76.АВ", "2026-10-31").values(), Decimal("0"))
    assert balance == Decimal("1100000.00")
    assert ledger.verify(state)["ok"]


def test_advance_vat_postings_are_idempotent(journal_with_advance):
    state = journal_with_advance
    first = tax.post_advance_vat(state, "2026-10")
    assert first["duplicates"] == 2 and first["posted"] == 0
    assert ledger.verify(state)["ok"]


def test_advance_vat_warns_about_missing_invoice(journal_with_advance):
    report = tax.vat_return(journal_with_advance, "2026-10")
    assert any("счёт-фактура" in text for text in report["warnings"])


def test_advance_invoice_draft_has_no_number(journal_with_advance):
    from pto_agent import forms

    form = forms.build_advance_invoice("2026-10", ledger_state=journal_with_advance)
    assert form["code"] == "СФ-аванс"
    assert len(form["rows"]) == 1
    assert form["rows"][0]["cells"][2] == "12 200 000,00"
    assert form["rows"][0]["cells"][4] == "2 200 000,00"
    assert form["totals"]["НДС"] == "2 200 000,00"
    asked = " ".join(gap["what"] for gap in form["gaps"])
    assert "номер и дата счёта-фактуры" in asked and "книга продаж" in asked
    assert "ЧЕРНОВИК" in form["draft_notice"]


# ─────────────────────────── больничный ───────────────────────────
def _sick(**overrides) -> dict:
    order = {"number": "П-31/2026", "kind": "sick", "tab": "004", "cause": "своя болезнь",
             "start": "2026-10-06", "end": "2026-10-10", "days": 5, "service_years": 9,
             "earnings_2y": 1_980_000}
    order.update(overrides)
    return order


def test_sick_three_days_by_employer(staff, rules):
    row = orders.sick(_sick(), staff["employees"][3], rules, period="2026-10")
    assert row["percent"] == 100
    assert row["daily"] == pytest.approx(2712.33)
    assert row["employer_days"] == 3 and row["fund_days"] == 2
    assert row["amount"] == pytest.approx(8136.99)
    assert row["fund_amount"] == pytest.approx(5424.66)
    assert row["contrib_base"] == 0, "с пособия взносы не начисляются (п. 1 ст. 422 НК РФ)"
    assert not row["errors"]


def test_sick_family_care_paid_by_fund_only(staff, rules):
    row = orders.sick(_sick(number="П-32/2026", cause="уход за членом семьи",
                            service_years=6, earnings_2y=1_500_000),
                      staff["employees"][5], rules, period="2026-10")
    assert row["employer_days"] == 0
    assert row["amount"] == 0
    assert row["fund_amount"] == pytest.approx(8219.15)
    assert not row["errors"]


def test_sick_seniority_percent(staff, rules):
    row = orders.sick(_sick(service_years=6), staff["employees"][3], rules, period="2026-10")
    assert row["percent"] == 80
    assert row["daily"] == pytest.approx(2169.86, abs=0.02)


def test_sick_capped_by_limit(staff, rules):
    row = orders.sick(_sick(earnings_2y=9_000_000), staff["employees"][3], rules,
                      period="2026-10")
    assert row["capped"] is True
    assert row["daily"] == pytest.approx(6827.40)
    assert any("6 827.40" in text for text in row["warnings"])


def test_sick_without_base_is_not_calculated(staff, rules):
    order = _sick()
    order.pop("earnings_2y")
    row = orders.sick(order, staff["employees"][2], rules, period="2026-10")
    assert row["not_calculated"] is True
    assert row["amount"] == 0 and row["fund_amount"] == 0
    assert [item["code"] for item in row["errors"]] == ["SICK_NO_BASE"]


def test_sick_days_must_match_dates(staff, rules):
    row = orders.sick(_sick(days=7), staff["employees"][3], rules, period="2026-10")
    assert "ORDER_DAYS_MISMATCH" in [item["code"] for item in row["errors"]]


def test_sick_unknown_cause_refused(staff, rules):
    row = orders.sick(_sick(cause="не хочу на работу"), staff["employees"][3], rules,
                      period="2026-10")
    assert "ORDER_UNKNOWN_CAUSE" in [item["code"] for item in row["errors"]]


# ─────────────────────────── отпускные и премии ───────────────────────────
def test_vacation_average_from_twelve_months(staff, rules):
    row = orders.vacation({"number": "П-33/2026", "kind": "vacation", "tab": "002",
                           "days": 14, "base_12m": 1_425_600},
                          staff["employees"][1], rules, period="2026-10")
    assert row["sdz"] == pytest.approx(4054.61)
    assert row["amount"] == pytest.approx(56_764.54)
    assert row["contrib_base"] == row["amount"], "отпускные входят в базу по взносам"
    assert "29.3" in row["sdz_source"]


def test_vacation_without_base_refused(staff, rules):
    row = orders.vacation({"number": "П-36/2026", "kind": "vacation", "tab": "002", "days": 14},
                          staff["employees"][1], rules, period="2026-10")
    assert "VACATION_NO_BASE" in [item["code"] for item in row["errors"]]


def test_bonus_requires_order_number(staff, rules):
    ok = orders.bonus({"number": "П-34/2026", "kind": "bonus", "tab": "001", "amount": 45_000,
                       "basis": "сдача корпуса 2"}, staff["employees"][0], rules,
                      period="2026-10")
    assert ok["amount"] == pytest.approx(45_000.00) and not ok["errors"]
    bad = orders.bonus({"kind": "bonus", "tab": "001", "amount": 45_000},
                       staff["employees"][0], rules, period="2026-10")
    assert "BONUS_NO_ORDER" in [item["code"] for item in bad["errors"]]


# ─────────────────────────── приказы в начислении ───────────────────────────
def test_orders_shift_tax_base_but_not_contribution_base(staff, rules):
    parsed = orders.load_orders(REPO / "data/demo/payroll/orders_2026_10.json")
    timesheet = json.loads((REPO / "data/demo/payroll/timesheet_2026_10.json")
                           .read_text(encoding="utf-8"))
    state = payroll.new_state(object_name=staff["object"])
    report = payroll.accrue(state, timesheet, staff, rules, orders=parsed)
    rows = {row["tab"]: row for row in report["rows"]}
    sick_man = rows["004"]
    assert sick_man["sick_employer"] == pytest.approx(8136.99)
    assert sick_man["accrued"] == pytest.approx(88_000 + 8136.99)
    assert sick_man["contrib_base"] == pytest.approx(88_000.00)
    bonus_man = rows["001"]
    assert bonus_man["bonus"] == pytest.approx(45_000.00)
    assert bonus_man["contrib_base"] == pytest.approx(145_000 + 45_000)
    assert report["totals"]["sick_fund"] == pytest.approx(13_643.81)
    assert any("П-35/2026" in item["what"] for item in report["order_errors"])


def test_sick_in_ndfl_but_not_in_rsv(staff, rules):
    parsed = orders.load_orders(REPO / "data/demo/payroll/orders_2026_10.json")
    timesheet = json.loads((REPO / "data/demo/payroll/timesheet_2026_10.json")
                           .read_text(encoding="utf-8"))
    state = payroll.new_state(object_name=staff["object"])
    report = payroll.accrue(state, timesheet, staff, rules, orders=parsed)
    payroll.remember(state, report, rules=rules)
    ndfl = tax.ndfl_return(state, "2026-10")
    rsv = tax.rsv(state, "2026-10")
    assert ndfl["totals"]["income"] - rsv["totals"]["base"] == pytest.approx(8136.99)
    assert ndfl["totals"]["tax_withheld"] > rsv["totals"]["contrib"] / 3


def test_accrue_without_orders_says_what_is_missing(staff, rules):
    timesheet = json.loads((REPO / "data/demo/payroll/timesheet_2026_10.json")
                           .read_text(encoding="utf-8"))
    state = payroll.new_state(object_name=staff["object"])
    report = payroll.accrue(state, timesheet, staff, rules)
    names = " ".join(item["what"] for item in report["not_included"])
    assert "больничные, отпускные и премии" in names


def test_reference_rules_have_sources(rules):
    benefits = rules["benefits"]
    assert benefits["sick"]["employer_days"] == 3
    assert benefits["sick"]["max_daily"] == pytest.approx(6827.40)
    assert benefits["vacation"]["avg_month_days"] == pytest.approx(29.3)
    assert benefits["sick"]["source"] and benefits["vacation"]["source"]
    assert Decimal(str(rules["mrot"])) == Decimal("27093")
