# -*- coding: utf-8 -*-
"""Счета-фактуры, нумерация, книга продаж и вычет НДС с выданных авансов.

Проверяется то, из-за чего книга продаж и декларация расходятся:

1. нумерация сквозная с 1 января, номер не переиспользуется, занятый номер — отказ;
2. дата счёта-фактуры не раньше основания; срок 5 дней считается числом, а не «на глаз»;
3. НДС берётся из базы и ставки; нет ставки — нет документа (`INVOICE_NO_RATE`);
4. книга продаж строится из документов и сходится с начислением в журнале;
5. акт без счёта-фактуры — это `SALES_BOOK_MISMATCH` с суммой;
6. вычет по авансам выданным попадает в декларацию (`vat_return`), 76.ВА: вычет — кредит;
7. при зачёте аванса вычет восстанавливается, и тоже попадает в декларацию.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pto_agent import invoices, ledger, tax, treasury

REPO = Path(__file__).resolve().parents[1]


def acts_journal(rate: float = 22.0) -> dict:
    payload = json.loads((REPO / "data/demo/accounting/acts_signed_scenario.json")
                         .read_text(encoding="utf-8"))
    payload["acts"][0]["vat"] = {"rate": rate, "included": False}
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    ledger.post_acts(state, payload, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    return state


# ─────────────────────────── нумерация ───────────────────────────
def test_numbering_restarts_each_year_and_never_reuses():
    rules = invoices.load_rules()
    state = invoices.new_state()
    assert invoices.next_number(state, "2026-10-29", rules=rules) == "1"
    invoices.register(state, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                      base_number="КС-2 №38", counterparty="ООО «Заказчик-Инвест»",
                      net=1000, rate=22, rules=rules)
    assert invoices.next_number(state, "2026-11-01", rules=rules) == "2"
    assert invoices.next_number(state, "2027-01-01", rules=rules) == "1", \
        "с 1 января нумерация должна начинаться заново"
    assert invoices.next_number(state, "2026-11-01", kind="correction", rules=rules) == "К1"


def test_taken_number_is_rejected():
    rules = invoices.load_rules()
    state = invoices.new_state()
    invoices.register(state, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                      base_number="КС-2 №38", counterparty="Покупатель", net=1000, rate=22,
                      rules=rules)
    second = invoices.register(state, kind="shipped", date="2026-10-30", base_date="2026-10-30",
                               base_number="КС-2 №39", counterparty="Покупатель", net=1000,
                               rate=22, number="1", rules=rules)
    assert second["status"] == invoices.STATUS_REJECTED
    assert "INVOICE_NUMBER_TAKEN" in [item["code"] for item in second["errors"]]


def test_duplicate_base_does_not_create_second_invoice():
    rules = invoices.load_rules()
    state = invoices.new_state()
    first = invoices.register(state, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                              base_number="КС-2 №38", counterparty="Покупатель", net=1000,
                              rate=22, rules=rules)
    again = invoices.register(state, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                              base_number="КС-2 №38", counterparty="Покупатель", net=1000,
                              rate=22, rules=rules)
    assert first["status"] == invoices.STATUS_OK
    assert again["status"] == invoices.STATUS_DUPLICATE
    assert len(state["issued"]) == 1


# ─────────────────────────── даты и ставка ───────────────────────────
def test_invoice_cannot_predate_its_base():
    rules = invoices.load_rules()
    state = invoices.new_state()
    result = invoices.register(state, kind="shipped", date="2026-10-20",
                              base_date="2026-10-29", base_number="КС-2 №38",
                              counterparty="Покупатель", net=1000, rate=22, rules=rules)
    assert result["status"] == invoices.STATUS_REJECTED
    assert "INVOICE_DATE_BEFORE_BASE" in [item["code"] for item in result["errors"]]


def test_late_invoice_is_registered_but_late_is_a_number():
    rules = invoices.load_rules()
    state = invoices.new_state()
    result = invoices.register(state, kind="shipped", date="2026-11-10",
                              base_date="2026-11-01", base_number="КС-2 №41",
                              counterparty="Покупатель", net=1000, rate=22, rules=rules)
    assert result["status"] == invoices.STATUS_OK
    assert result["invoice"]["late"] is True
    assert result["invoice"]["late_days"] == 9
    assert any("168" in text for text in result["warnings"])


def test_invoice_without_rate_is_not_registered():
    rules = invoices.load_rules()
    state = invoices.new_state()
    result = invoices.register(state, kind="shipped", date="2026-11-11",
                              base_date="2026-11-11", base_number="КС-2 №42",
                              counterparty="Покупатель", net=1000, rules=rules)
    assert result["status"] == invoices.STATUS_REJECTED
    assert any("ставка" in gap["what"] for gap in result["gaps"])
    assert not state["issued"]


def test_zero_rate_invoice_warns_about_grounds():
    rules = invoices.load_rules()
    state = invoices.new_state()
    result = invoices.register(state, kind="shipped", date="2026-11-11",
                              base_date="2026-11-11", base_number="КС-2 №43",
                              counterparty="Покупатель", net=1000, rate=0, rules=rules)
    assert result["status"] == invoices.STATUS_OK
    assert result["invoice"]["vat"] == 0.0
    assert any("ст. 149" in text or "ст. 164" in text for text in result["warnings"])


# ─────────────────────────── книга продаж ───────────────────────────
def test_sales_book_matches_journal_on_signed_acts():
    rules = invoices.load_rules()
    journal = acts_journal(22.0)
    registry = invoices.new_state()
    issued = invoices.auto_issue(registry, journal, "2026-10", rules=rules)
    assert issued["posted"] == 1
    book = invoices.sales_book(registry, journal, "2026-10", rules=rules)
    assert book["totals"]["invoices"] == 1
    assert book["totals"]["difference"] == 0
    assert book["totals"]["vat"] == pytest.approx(7_503_452.0)
    assert not any(item["code"] == "SALES_BOOK_MISMATCH" for item in book["problems"])


def test_empty_book_is_a_mismatch_with_sum():
    rules = invoices.load_rules()
    journal = acts_journal(22.0)
    book = invoices.sales_book(invoices.new_state(), journal, "2026-10", rules=rules)
    mismatch = [item for item in book["problems"] if item["code"] == "SALES_BOOK_MISMATCH"]
    assert mismatch, "пустая книга должна давать расхождение, а не молчание"
    assert "0,00 ₽" in mismatch[0]["why"]
    assert book["ready"] is False


def test_preview_does_not_take_numbers():
    rules = invoices.load_rules()
    registry = invoices.new_state()
    preview = invoices.auto_issue(registry, acts_journal(22.0), "2026-10", rules=rules,
                                  mode="propose")
    assert preview["posted"] == 0 and preview["planned"] == 1
    assert registry["issued"] == []


def test_sales_book_keeps_stale_rate_problem_from_journal():
    """Книга не «исправляет» ошибку акта: расхождение ставки идёт из vat_return."""
    rules = invoices.load_rules()
    journal = acts_journal(20.0)
    registry = invoices.new_state()
    invoices.auto_issue(registry, journal, "2026-10", rules=rules)
    book = invoices.sales_book(registry, journal, "2026-10", rules=rules)
    codes = [item["code"] for item in book["problems"]]
    assert "VAT_RATE_STALE" in codes
    assert book["rows"][0]["rate"] == 20.0, "счёт-фактура повторяет акт, а не законную ставку"


def test_sales_book_markdown_names_limits():
    rules = invoices.load_rules()
    journal = acts_journal(22.0)
    registry = invoices.new_state()
    invoices.auto_issue(registry, journal, "2026-10", rules=rules)
    text = invoices.to_markdown(invoices.sales_book(registry, journal, "2026-10", rules=rules))
    assert "Книга продаж" in text
    assert "на проверку" in text, "графы книги не помечены «на проверку»"
    assert "книга покупок" in text, "не сделано должно быть в тексте, а не только в отчёте"


# ─────────────────────────── 76.ВА в декларации ───────────────────────────
def issued_advance_journal() -> dict:
    state = ledger.new_state()
    cash = treasury.new_state(opening=50_000_000)
    treasury.add_request(cash, {"number": "З-501", "date": "2026-10-01", "article": "материалы",
                                "amount": 12_200_000, "counterparty": "ООО «Бетон-Сервис»",
                                "requested_by": "snab", "vat": {"rate": 22},
                                "basis": {"number": "СЧ-908", "date": "2026-09-30",
                                          "kind": "счёт", "contract": "Д-11/2025"}},
                          rules=treasury.load_rules())
    for role, person in (("снабжение", "Прохоров"), ("ПТО", "Кузнецов"),
                         ("финдиректор", "Смирнов")):
        treasury.approve(cash, "З-501", role=role, person=person, date="2026-10-02")
    treasury.pay(cash, "З-501", date="2026-10-05", advance=True)
    treasury.post_to_ledger(cash, state)
    return state


def test_issued_advance_deduction_reaches_declaration():
    state = issued_advance_journal()
    tax.post_advance_vat(state, "2026-10")
    report = tax.vat_return(state, "2026-10")
    assert report["totals"]["vat_on_issued_advances"] == pytest.approx(2_200_000.0)
    assert report["totals"]["payable"] == pytest.approx(-2_200_000.0), \
        "вычет должен уменьшить налог, иначе журнал знает больше декларации"
    assert any("счёт-фактура" in text.lower() for text in report["warnings"])


def test_issued_block_sides_are_not_swapped():
    """76.ВА — активный счёт: вычет идёт в кредит. В круге 32 стороны были перепутаны."""
    state = issued_advance_journal()
    tax.post_advance_vat(state, "2026-10")
    block = tax.advances_vat(state, "2026-10")["issued"]
    assert block["in_ledger"] == pytest.approx(2_200_000.0)
    assert block["restored"] == 0.0


def test_issued_restore_on_offset_returns_to_declaration():
    state = issued_advance_journal()
    tax.post_advance_vat(state, "2026-10")
    ledger.post_entry(state, event="advance_offset", date="2026-11-20",
                      doc={"number": "зачёт аванса Д-11/2025", "date": "2026-11-20"},
                      lines=[{"account": "60.01", "side": ledger.DEBIT, "amount": 6_100_000,
                              "analytics": {"контрагент": "ООО «Бетон-Сервис»",
                                            "договор": "Д-11/2025"}},
                             {"account": "60.02", "side": ledger.CREDIT, "amount": 6_100_000,
                              "analytics": {"контрагент": "ООО «Бетон-Сервис»",
                                            "договор": "Д-11/2025"}}],
                      memo="зачёт", rule="treasury.advance_offset",
                      chart=ledger.load_chart(), rules=ledger.load_rules())
    restored = tax.post_advance_vat(state, "2026-11")
    assert restored["issued_restored"] == pytest.approx(1_100_000.0)
    november = tax.vat_return(state, "2026-11")
    assert november["totals"]["vat_on_issued_restored"] == pytest.approx(1_100_000.0)
    assert ledger.verify(state)["ok"] is True


def test_rules_have_sources_and_limits():
    rules = invoices.load_rules()
    assert rules["issue"]["deadline_days"] == 5
    assert "168" in rules["issue"]["source"]
    assert "1137" in rules["numbering"]["source"]
    assert rules["sales_book"]["verify"] is True, "графы книги должны быть «на проверку»"
    assert any("книга покупок" in text for text in rules["not_implemented"])
