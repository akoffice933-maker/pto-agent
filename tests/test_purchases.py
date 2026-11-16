# -*- coding: utf-8 -*-
"""Книга покупок, корректировочные СФ, журнал посредников и выгрузка в ФНС.

Тесты держат три вещи, на которых декларация расходится с учётом:

* книга покупок сверяется с проводками (19.03) и с закрытием месяца;
* уменьшение в корректировке уменьшает книгу, увеличение — увеличивает, причина обязательна;
* выгрузка в ФНС честно помечена как несверенная со схемой.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pto_agent import closing, invoices, ledger


def demo_ledger() -> dict:
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    chart, rules = ledger.load_chart(), ledger.load_rules()
    for number, name, net in (("УПД №118", "ООО «БетонТранс»", 1_426_000),
                              ("УПД №119", "ООО «МеталлСнаб»", 1_496_000),
                              ("УПД №120", "ООО «СтройБаза»", 98_000)):
        vat = net * 0.22
        result = ledger.post_entry(
            state, event="receipt", date="2026-10-01",
            doc={"number": number, "date": "2026-10-01", "kind": "upd"},
            lines=[{"account": "20.01", "side": ledger.DEBIT, "amount": net,
                    "analytics": {"объект": "ЖК «Северный», корпус 2"}},
                   {"account": "19.03", "side": ledger.DEBIT, "amount": vat,
                    "analytics": {"контрагент": name}},
                   {"account": "60.01", "side": ledger.CREDIT, "amount": net + vat,
                    "analytics": {"контрагент": name}}],
            memo="приход", rule="test_purchases", chart=chart, rules=rules)
        assert result["status"] == ledger.STATUS_OK, result["errors"]
    return state


def registry_state(ledger_state: dict) -> dict:
    state = invoices.new_state(object_name="ЖК «Северный», корпус 2")
    payload = json.loads((Path("data/demo/purchases/incoming_2026_10.json"))
                         .read_text(encoding="utf-8"))
    result = invoices.auto_register_purchases(state, payload, "2026-10",
                                              ledger_state=ledger_state)
    assert result["posted"] == 2
    return state


@pytest.fixture(scope="module")
def ledger_state() -> dict:
    return demo_ledger()


@pytest.fixture(scope="module")
def book(ledger_state) -> dict:
    return invoices.purchase_book(registry_state(ledger_state), ledger_state, "2026-10")


# ── книга покупок ──────────────────────────────────────────────────────────
def test_purchase_book_totals(book):
    assert book["totals"]["invoices"] == 2
    assert book["totals"]["vat"] == pytest.approx(335_280)
    assert book["totals"]["journal_vat"] == pytest.approx(664_400)
    assert book["totals"]["difference"] == pytest.approx(329_120)


def test_purchase_book_names_missing_invoice(book):
    codes = [item["code"] for item in book["problems"]]
    assert "PURCHASE_BOOK_MISMATCH" in codes
    assert any("329 120" in str(gap.get("ask")) for gap in book["gaps"])


def test_purchase_book_flags_rate_mismatch(ledger_state):
    """Демо приведено к 22 % (круг 35), поэтому расхождение проверяется отдельным документом."""
    state = registry_state(ledger_state)
    invoices.register_purchase(state, number="СФ №900", date="2026-10-08",
                               base_number="УПД №118", base_date="2026-10-01",
                               counterparty="ООО «БетонТранс»", net=1000, rate=20)
    report = invoices.purchase_book(state, ledger_state, "2026-10")
    assert report["rate_notes"], "ставка 20 % при законной 22 % должна быть названа"
    assert all("22" in note for note in report["rate_notes"])


def test_invoice_without_posting_is_flagged(ledger_state):
    state = invoices.new_state()
    invoices.register_purchase(state, number="СФ №900", date="2026-10-05",
                               base_number="УПД №999", base_date="2026-10-05",
                               counterparty="ООО «Нет-Прихода»", net=100_000, rate=22)
    report = invoices.purchase_book(state, ledger_state, "2026-10")
    assert "PURCHASE_NO_BASE_DOC" in [item["code"] for item in report["problems"]]


def test_invoice_without_number_not_registered():
    state = invoices.new_state()
    result = invoices.register_purchase(state, number="", date="2026-10-05",
                                        base_number="УПД №118",
                                        counterparty="ООО «БетонТранс»",
                                        net=1000, rate=22)
    assert result["status"] == invoices.STATUS_REJECTED
    assert result["gaps"] and "номер" in result["gaps"][0]["what"]
    assert state["received"] == []


def test_invoice_without_base_not_registered():
    state = invoices.new_state()
    result = invoices.register_purchase(state, number="СФ №1", date="2026-10-05",
                                        base_number="", counterparty="ООО «БетонТранс»",
                                        net=1000, rate=22)
    assert result["status"] == invoices.STATUS_REJECTED
    assert "PURCHASE_NO_BASE" in [item["code"] for item in result["errors"]]
    assert state["received"] == []


def test_invoice_without_rate_asks_instead_of_guessing():
    state = invoices.new_state()
    result = invoices.register_purchase(state, number="СФ №2", date="2026-10-05",
                                        base_number="УПД №118",
                                        counterparty="ООО «БетонТранс»", net=1000)
    assert result["status"] == invoices.STATUS_REJECTED
    assert any("ставк" in gap["what"].lower() for gap in result["gaps"])


def test_duplicate_purchase_not_counted_twice(ledger_state):
    state = registry_state(ledger_state)
    payload = json.loads((Path("data/demo/purchases/incoming_2026_10.json"))
                         .read_text(encoding="utf-8"))
    again = invoices.auto_register_purchases(state, payload, "2026-10",
                                             ledger_state=ledger_state)
    assert again["posted"] == 0 and again["duplicates"] == 2
    assert len(state["received"]) == 2


def test_for_closing_contract(ledger_state):
    contract = invoices.for_closing(registry_state(ledger_state), "2026-10")
    assert set(contract) == {"invoices"}
    first = contract["invoices"][0]
    assert first["document"] == "УПД №118"
    assert first["vat"] == pytest.approx(313_720)


def test_closing_deducts_only_documented_vat(ledger_state):
    state = demo_ledger()
    report = closing.close_month(
        state, "2026-10",
        invoices=invoices.for_closing(registry_state(state), "2026-10"), dry_run=True)
    step = next(item for item in report["steps"] if "19.03" in str(item.get("title")))
    assert float(step["amount"]) == pytest.approx(335_280)
    assert step["status"] == "выполнено частично"


# ── корректировочные счета-фактуры ─────────────────────────────────────────
def test_decrease_reduces_book(ledger_state):
    state = registry_state(ledger_state)
    result = invoices.register_correction(state, for_kind="received", base_number="СФ №341",
                                          base_date="2026-10-01", number="К/512",
                                          date="2026-10-20", direction="decrease",
                                          delta_net=26_000,
                                          reason="сторнирование объёма бетона")
    assert result["status"] == invoices.STATUS_OK
    assert result["correction"]["vat"] == pytest.approx(-5_720)  # 26 000 × 22 %
    report = invoices.purchase_book(state, ledger_state, "2026-10")
    assert report["totals"]["vat"] == pytest.approx(329_560)


def test_increase_grows_book(ledger_state):
    state = registry_state(ledger_state)
    invoices.register_correction(state, for_kind="received", base_number="СФ №512",
                                 base_date="2026-10-03", number="К/513", date="2026-10-21",
                                 direction="increase", delta_net=50_000, rate=22,
                                 reason="допоставка")
    report = invoices.purchase_book(state, ledger_state, "2026-10")
    assert report["totals"]["vat"] == pytest.approx(346_280)  # 335 280 + 50 000 × 22 %


def test_correction_requires_reason():
    state = registry_state(demo_ledger())
    result = invoices.register_correction(state, for_kind="received", base_number="СФ №341",
                                          base_date="2026-10-01", number="К/514",
                                          date="2026-10-22", direction="decrease",
                                          delta_net=1000, reason="   ")
    assert result["status"] == invoices.STATUS_REJECTED
    assert state["corrections"] == []


def test_correction_requires_original():
    state = registry_state(demo_ledger())
    result = invoices.register_correction(state, for_kind="received", base_number="СФ №000",
                                          base_date="2026-10-01", number="К/515",
                                          date="2026-10-22", direction="decrease",
                                          delta_net=1000, reason="проба")
    assert result["status"] == invoices.STATUS_REJECTED
    assert result["errors"][0]["code"] == "CORRECTION_NO_ORIGINAL"


def test_received_correction_number_not_invented():
    state = registry_state(demo_ledger())
    result = invoices.register_correction(state, for_kind="received", base_number="СФ №341",
                                          base_date="2026-10-01", date="2026-10-23",
                                          direction="decrease", delta_net=1000,
                                          reason="проба без номера")
    assert result["status"] == invoices.STATUS_REJECTED
    assert any("поставщик" in gap["why"] for gap in result["gaps"])


def test_issued_correction_gets_k_prefix():
    state = invoices.new_state()
    first = invoices.register(state, kind="shipped", date="2026-10-29",
                              base_date="2026-10-29", base_number="КС-2 №38",
                              counterparty="ООО «Заказчик-Инвест»", net=1_000_000,
                              rate=22, rules=invoices.load_rules())
    assert first["status"] == invoices.STATUS_OK
    issued = first["invoice"]
    result = invoices.register_correction(state, for_kind="issued",
                                          base_number=issued["number"],
                                          base_date=issued["date"], date="2026-10-30",
                                          direction="decrease", delta_net=100_000,
                                          reason="уменьшение объёма КС-2")
    assert result["status"] == invoices.STATUS_OK
    assert result["correction"]["number"].startswith("К")


# ── журнал посредников ─────────────────────────────────────────────────────
def test_agency_invoice_goes_to_journal_not_to_book():
    state = invoices.new_state()
    invoices.register_purchase(state, number="СФ №77", date="2026-10-06",
                               base_number="УПД №130", base_date="2026-10-06",
                               counterparty="ООО «Заказчик-Инвест»", net=1_000_000,
                               rate=22, agency=True)
    invoices.register_purchase(state, number="СФ №78", date="2026-10-07",
                               base_number="УПД №131", base_date="2026-10-07",
                               counterparty="ООО «СтройБаза»", net=100_000, rate=22)
    book = invoices.purchase_book(state, {"entries": []}, "2026-10")
    assert book["totals"]["invoices"] == 1
    journal = invoices.intermediary_journal(state, "2026-10")
    assert len(journal["part2"]) == 1
    assert journal["totals"]["vat2"] == pytest.approx(220_000)


def test_journal_empty_is_a_gap_not_a_zero():
    state = registry_state(demo_ledger())
    journal = invoices.intermediary_journal(state, "2026-10")
    assert journal["part1"] == [] and journal["part2"] == []
    assert journal["gaps"] and journal["gaps"][0]["what"].startswith("посреднические")


def test_journal_gets_operation_code_from_reference():
    """Круг 35: коды заведены — журнал готов, код выведен правилом и назван."""
    state = invoices.new_state()
    invoices.register_purchase(state, number="СФ №77", date="2026-10-06",
                               base_number="УПД №130", base_date="2026-10-06",
                               counterparty="ООО «Заказчик-Инвест»", net=1_000_000,
                               rate=22, agency=True)
    journal = invoices.intermediary_journal(state, "2026-10")
    assert journal["ready"] is True
    assert journal["part2"][0]["operation_code"] == "01"
    assert "агентск" in journal["part2"][0]["operation_code_why"]


def test_operation_code_rules_come_from_order():
    codes = invoices.load_operation_codes()
    assert "ММВ-7-3/136" in codes["source"]
    assert len(codes["codes"]) == 24
    assert codes["loaded_from_primary_source"] is True
    assert invoices.operation_code_for("shipped")["code"] == "01"
    assert invoices.operation_code_for("advance")["code"] == "02"
    assert invoices.operation_code_for("purchase")["code"] == "01"
    assert invoices.operation_code_for("correction", direction="decrease")["code"] == "18"
    assert invoices.operation_code_for("correction", direction="increase")["code"] == "01"
    assert invoices.operation_code_for("purchase", agent=True)["code"] == "06"


def test_operation_code_unknown_kind_is_not_invented():
    answer = invoices.operation_code_for("something-else")
    assert answer["code"] is None and answer["resolved"] is False


def test_operation_code_shows_up_in_xml(book):
    xml = invoices.to_fns_xml(book)
    assert "<КодВидОпер>01</КодВидОпер>" in xml
    assert "КодВидОпер не определён" not in xml


# ── исправленные счета-фактуры ─────────────────────────────────────────────
def test_fix_keeps_number_and_date_of_document(ledger_state):
    state = registry_state(ledger_state)
    result = invoices.fix_invoice(state, for_kind="received", number="СФ №341",
                                  date="2026-10-01", fix_number="И-1",
                                  fix_date="2026-10-10", reason="неверные реквизиты")
    assert result["status"] == invoices.STATUS_OK
    document = result["invoice"]
    assert document["number"] == "СФ №341" and document["date"] == "2026-10-01"
    assert document["fix_number"] == "И-1" and document["fix_date"] == "2026-10-10"
    report = invoices.purchase_book(state, ledger_state, "2026-10")
    row = next(item for item in report["rows"] if item["number"] == "СФ №341")
    assert "исправление № И-1 от 2026-10-10" == row["fix_note"]
    xml = invoices.to_fns_xml(report)
    assert 'НомерИспр="И-1"' in xml and 'ДатаИспр="10.10.2026"' in xml


def test_fix_requires_reason_and_original(ledger_state):
    state = registry_state(ledger_state)
    no_original = invoices.fix_invoice(state, for_kind="received", number="СФ №000",
                                       date="2026-10-01", fix_number="И-9",
                                       fix_date="2026-10-10", reason="проба")
    assert no_original["status"] == invoices.STATUS_REJECTED
    assert no_original["errors"][0]["code"] == "FIX_NO_ORIGINAL"
    no_reason = invoices.fix_invoice(state, for_kind="received", number="СФ №341",
                                     date="2026-10-01", fix_number="И-2",
                                     fix_date="2026-10-10", reason="  ")
    assert no_reason["status"] == invoices.STATUS_REJECTED
    assert state["received"][0].get("fix_number") is None


def test_fix_date_cannot_be_earlier_than_document(ledger_state):
    state = registry_state(ledger_state)
    result = invoices.fix_invoice(state, for_kind="received", number="СФ №341",
                                  date="2026-10-01", fix_number="И-3",
                                  fix_date="2026-09-30", reason="проба")
    assert result["status"] == invoices.STATUS_REJECTED
    assert result["errors"][0]["code"] == "FIX_DATE_BEFORE_DOC"


def test_received_fix_number_is_not_invented(ledger_state):
    state = registry_state(ledger_state)
    result = invoices.fix_invoice(state, for_kind="received", number="СФ №512",
                                  date="2026-10-03", fix_date="2026-10-11",
                                  reason="проба без номера")
    assert result["status"] == invoices.STATUS_REJECTED
    assert any("поставщик" in gap["why"] for gap in result["gaps"])


def test_own_fix_number_is_sequential():
    state = invoices.new_state()
    first = invoices.register(state, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                              base_number="КС-2 №38", counterparty="ООО «Заказчик-Инвест»",
                              net=1_000_000, rate=22, rules=invoices.load_rules())
    one = invoices.fix_invoice(state, for_kind="issued", number=first["invoice"]["number"],
                               date=first["invoice"]["date"], fix_date="2026-10-30",
                               reason="неверный адрес покупателя")
    two = invoices.fix_invoice(state, for_kind="issued", number=first["invoice"]["number"],
                               date=first["invoice"]["date"], fix_date="2026-10-31",
                               reason="повторное исправление")
    assert one["invoice"]["fix_number"] == "1"
    assert two["invoice"]["fix_number"] == "2"
    assert len(two["invoice"]["fixes"]) == 2


def test_fix_in_another_period_warns_about_extra_sheet(ledger_state):
    state = registry_state(ledger_state)
    result = invoices.fix_invoice(state, for_kind="received", number="СФ №341",
                                  date="2026-10-01", fix_number="И-4",
                                  fix_date="2026-11-05", reason="исправление в ноябре")
    assert result["status"] == invoices.STATUS_OK
    assert any("дополнительный лист" in warning for warning in result["warnings"])


def test_journal_has_deadline_and_knd():
    journal = invoices.intermediary_journal(invoices.new_state(), "2026-10")
    assert journal["knd"] == "1115104"
    assert "20-го числа" in journal["deadline"]


# ── выгрузка в ФНС ─────────────────────────────────────────────────────────
def test_sales_book_xml_marked_unverified():
    state = invoices.new_state()
    invoices.register(state, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                      base_number="КС-2 №38", counterparty="ООО «Заказчик-Инвест»",
                      net=34_106_600, rate=22, rules=invoices.load_rules())
    xml = invoices.to_fns_xml(invoices.sales_book(state, {"entries": []}, "2026-10"))
    assert 'КНД="1115103"' in xml
    assert 'СвереноСоСхемой="нет"' in xml
    assert "<КодВидОпер>01</КодВидОпер>" in xml, "код вида операции должен быть в выгрузке"
    assert "не сверялся со схемой XSD" in xml


def test_purchase_book_xml(book):
    xml = invoices.to_fns_xml(book)
    assert 'КНД="1115102"' in xml
    assert "313720.00" in xml and "21560.00" in xml
    assert "ПроверкаФайла" in xml


def test_journal_xml_has_inn_from_reference():
    xml = invoices.to_fns_xml(invoices.intermediary_journal(invoices.new_state(), "2026-10"))
    assert 'КНД="1115104"' in xml
    assert "7810447576" in xml


def test_xml_is_well_formed(book):
    from xml.etree import ElementTree as ET
    payload = invoices.to_fns_xml(book)
    ET.fromstring(payload.split("\n", 1)[1])  # первая строка — объявление XML


def test_unknown_report_kind_rejected():
    with pytest.raises(ValueError):
        invoices.to_fns_xml({"kind": "something", "period": "2026-10"})


# ── печать ─────────────────────────────────────────────────────────────────
def test_markdown_purchase_book(book):
    text = invoices.to_markdown(book)
    assert text.startswith("# Книга покупок")
    assert "Чего не хватает" in text
    assert "Не сделано" in text


def test_markdown_journal():
    text = invoices.to_markdown(invoices.intermediary_journal(invoices.new_state(), "2026-10"))
    assert "Журнал учёта" in text
    assert "Часть 1" in text and "Часть 2" in text
