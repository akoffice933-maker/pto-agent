# -*- coding: utf-8 -*-
"""Разделы 4–6 (ставка 0 %), раздел 12 и дополнительные листы книг.

Проверяется то, из-за чего декларацию по НДС считают неполной или неверной:

1. раздел 4 — только подтверждённые в этом периоде операции; подтверждение в будущем — запрос,
   а не строка декларации;
2. пока 180 дней не истекли, операция не попадает ни в раздел 4, ни в раздел 6;
3. после истечения срока налог начисляется по ставке на дату отгрузки — числом, а не «22 %»;
4. вычет по ранее подтверждённой операции заявляется разделом 5 один раз;
5. код операции не выдумывается: строка помечается «код на проверку» и попадает в запросы;
6. раздел 12 требует ИНН с верной контрольной суммой и сходящийся НДС;
7. дополнительный лист книги — две записи «до» и «после», причина обязательна, книга — только
   прошлого периода, повтор не создаёт второй лист;
8. приложение № 1 к разделу 8/9 переносит итог дополнительного листа, а не всей книги.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pto_agent import invoices, tax, vat_declaration

REPO = Path(__file__).resolve().parents[1]


def demo() -> dict:
    payload = json.loads((REPO / "data/demo/accounting/zero_rate_2026_10.json")
                         .read_text(encoding="utf-8"))
    return payload


def export(**kwargs) -> dict:
    base = {"document": "ГТД 10005030/150726/0012345", "date": "2026-07-15",
            "amount": 4_800_000, "code": "1010401", "vat_input": 620_000,
            "confirmed": True, "confirmed_date": "2026-10-20"}
    base.update(kwargs)
    return base


# ─────────────────────────── разделы 4–6 ───────────────────────────
def test_confirmed_in_period_goes_to_section4():
    report = vat_declaration.zero_rate("2026-10", exports=[export()])
    rows = report["sections"]["4"]["rows"]
    assert len(rows) == 1 and rows[0]["state"] == "ок"
    assert report["totals"]["base4"] == 4_800_000
    assert report["totals"]["deductions4"] == 620_000
    assert report["sections"]["6"]["rows"] == []


def test_deadline_not_expired_is_not_section6():
    report = vat_declaration.zero_rate(
        "2026-10", exports=[export(confirmed=False, confirmed_date="",
                                   date="2026-09-01")])
    assert report["sections"]["6"]["rows"][0].get("accrued") is None
    assert report["totals"]["accrued6"] == 0
    assert any("истекает" in gap["why"] for gap in report["gaps"])


def test_expired_deadline_accrues_tax_at_rate_on_ship_date():
    report = vat_declaration.zero_rate(
        "2026-10", exports=[export(confirmed=False, confirmed_date="", date="2026-03-10",
                                   amount=1_200_000, vat_input=180_000)])
    row = report["sections"]["6"]["rows"][0]
    lawful = tax.vat_rate_on("2026-03-10", tax.load_rates())
    expected = round(1_200_000 * float(lawful["rate"]) / 100, 2)
    assert row["rate"] == float(lawful["rate"])
    assert row["accrued"] == pytest.approx(expected, abs=0.01)
    assert report["totals"]["accrued6"] == pytest.approx(expected, abs=0.01)


def test_confirmed_earlier_goes_to_section5_once():
    first = vat_declaration.zero_rate(
        "2026-10", exports=[export(confirmed_date="2026-08-20")])
    assert first["sections"]["5"]["rows"][0]["state"] == "ок"
    assert first["totals"]["base5"] == 4_800_000
    again = vat_declaration.zero_rate(
        "2026-10", exports=[export(confirmed_date="2026-08-20", claimed=True)])
    assert again["sections"]["5"]["rows"][0]["state"] == "вычет уже заявлен"
    assert again["totals"]["base5"] == 0, "заявленный вычет не должен попасть второй раз"


def test_confirmation_in_future_is_a_gap_not_a_row():
    report = vat_declaration.zero_rate(
        "2026-10", exports=[export(confirmed_date="2026-12-15")])
    assert report["totals"]["base4"] == 0
    assert any("позже периода" in gap["why"] for gap in report["gaps"])


def test_missing_code_is_marked_not_invented():
    report = vat_declaration.zero_rate("2026-10", exports=[export(code="")])
    row = report["sections"]["4"]["rows"][0]
    assert row["verify"] is True and row["state"] == "код на проверку"
    assert any("код операции" in gap["what"] for gap in report["gaps"])
    assert report["totals"]["base4"] == 4_800_000, "сумма считается, не хватает только кода"


def test_without_document_nothing_is_counted():
    report = vat_declaration.zero_rate("2026-10",
                                       exports=[{"amount": 1_000, "code": "1010401"}])
    assert report["totals"]["base4"] == 0
    assert any("документ и дата" in gap["what"] for gap in report["gaps"])


def test_checks_and_markdown():
    report = vat_declaration.zero_rate("2026-10", exports=demo()["exports"])
    assert all(item["status"] == vat_declaration.OK for item in report["checks"])
    text = vat_declaration.to_markdown(report)
    assert "Раздел 4" in text and "Раздел 6" in text and "Не сделано" in text
    assert report["problems"] == []


def test_empty_registry_says_why_sections_are_empty():
    report = vat_declaration.zero_rate("2026-10", exports=[])
    assert report["totals"]["base4"] == 0
    assert any("разделы 4–6 не заполняются" in gap["why"] for gap in report["gaps"])


# ─────────────────────────── раздел 12 ───────────────────────────
ROW12 = {"number": "1", "date": "2026-10-12", "buyer": "ООО «Ромашка»",
         "buyer_inn": "7810447576", "buyer_kpp": "781001001",
         "net": 500_000, "vat": 110_000, "code": "01"}


def test_section12_row_ok():
    report = vat_declaration.section12("2026-10", invoices=[dict(ROW12)])
    assert report["rows"][0]["state"] == "ок"
    assert report["totals"]["vat"] == 110_000
    assert report["totals"]["to_line_030"] == 110_000
    assert report["ready"] is True


def test_section12_bad_inn_is_a_problem():
    report = vat_declaration.section12("2026-10", invoices=[dict(ROW12, buyer_inn="1234567890")])
    assert [p["code"] for p in report["problems"]] == ["SECTION12_BAD_INN"]
    assert report["ready"] is False


def test_section12_vat_must_match_rate():
    report = vat_declaration.section12("2026-10", invoices=[dict(ROW12, vat=100_000)])
    assert [p["code"] for p in report["problems"]] == ["SECTION12_VAT"]


def test_section12_empty_is_explained():
    report = vat_declaration.section12("2026-10")
    assert report["rows"] == []
    assert any("п. 5 ст. 173" in gap["why"] for gap in report["gaps"])
    assert "не заполняется" in vat_declaration.to_markdown(report)


# ─────────────────── дополнительные листы книг (A2) ───────────────────
def registry_with_previous_invoice() -> dict:
    payload = json.loads((REPO / "data/demo/accounting/book_fix_2026_10.json")
                         .read_text(encoding="utf-8"))
    doc = payload["invoice"]
    state = invoices.new_state(object_name="ЖК «Северный», корпус 2")
    invoices.register_purchase(state, number=doc["number"], date=doc["date"],
                               base_number=doc["base_number"],
                               counterparty=doc["counterparty"], inn=doc["inn"],
                               net=doc["net"], rate=doc["rate"])
    return state


def fix(state: dict, **kwargs) -> dict:
    base = {"sheet": "purchases", "base_period": "2026-09", "number": "СФ №298",
            "date": "2026-09-18", "net_before": 900_000, "vat_before": 198_000,
            "net_after": 820_000, "vat_after": 180_400,
            "reason": "неверная стоимость по акту сверки", "entry_date": "2026-10-25"}
    base.update(kwargs)
    return vat_declaration.register_book_fix(state, **base)


def test_book_fix_requires_reason():
    state = registry_with_previous_invoice()
    result = fix(state, reason="   ")
    assert result["status"] == "отклонён"
    assert result["gaps"], "без основания должна быть просьба, а не запись"
    assert not state.get("book_fixes")


def test_book_fix_only_for_past_period():
    state = registry_with_previous_invoice()
    result = fix(state, base_period="2026-10")
    assert [e["code"] for e in result["errors"]] == ["BOOK_FIX_NOT_PAST"]


def test_book_fix_needs_original_document():
    state = registry_with_previous_invoice()
    result = fix(state, number="СФ №999")
    assert [e["code"] for e in result["errors"]] == ["BOOK_FIX_NO_DOCUMENT"]


def test_book_fix_with_same_amounts_is_refused():
    state = registry_with_previous_invoice()
    result = fix(state, net_after=900_000, vat_after=198_000)
    assert [e["code"] for e in result["errors"]] == ["BOOK_FIX_SAME"]


def test_additional_sheet_has_before_and_after_rows():
    state = registry_with_previous_invoice()
    assert fix(state)["status"] == "записано"
    sheet = vat_declaration.additional_sheets(state, "2026-10", sheet="purchases",
                                              book_total_vat=335_280)
    kinds = [row["kind"] for row in sheet["rows"]]
    assert kinds == ["аннулируемая", "новая"]
    assert sheet["totals"]["delta_vat"] == pytest.approx(-17_600, abs=0.01)
    assert sheet["totals"]["delta_net"] == pytest.approx(-80_000, abs=0.01)
    lines = {row["line"]: row["value"] for row in sheet["appendix"]["rows"]}
    assert lines["190"] == pytest.approx(-17_600, abs=0.01)
    assert lines["005"] == pytest.approx(335_280, abs=0.01)


def test_book_fix_is_idempotent():
    state = registry_with_previous_invoice()
    first = fix(state)
    second = fix(state)
    assert first["status"] == "записано"
    assert second["status"] == "уже записано"
    assert len(state["book_fixes"]) == 1


def test_sheet_without_fixes_says_why():
    state = registry_with_previous_invoice()
    sheet = vat_declaration.additional_sheets(state, "2026-10", sheet="purchases")
    assert sheet["rows"] == []
    assert any("лист не формируется" in gap["why"] for gap in sheet["gaps"])
    assert sheet["ready"] is False


def test_sheet_markdown_names_verify_knd_and_appendix():
    state = registry_with_previous_invoice()
    fix(state)
    sheet = vat_declaration.additional_sheets(state, "2026-10", sheet="purchases",
                                              book_total_vat=335_280)
    text = vat_declaration.to_markdown(sheet)
    assert "Приложение № 1 к разделу 8" in text
    assert "на проверку" in text, "номер КНД не подтверждён — он должен быть помечен"
    assert "Дополнительный лист книги покупок" in text
