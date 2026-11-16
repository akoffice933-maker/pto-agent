# -*- coding: utf-8 -*-
"""Раздельный учёт НДС и разделы декларации, которых до круга 36 не было.

Проверяется то, из-за чего декларацию по НДС возвращают:

1. пропорция считается по **отгруженной** выручке (абз. 4 п. 4 ст. 170 НК РФ), а правило 5 % —
   по **совокупным расходам** (абз. 9): это разные базы, и подменять одну другой нельзя;
2. нет расходов — правило 5 % «не проверено», а не посчитано от выручки;
3. необлагаемая операция без основания или без документа в расчёт не идёт — с кодом причины;
4. код раздела 7 не выдумывается; код, противоречащий основанию (1010800 против ст. 149), —
   это `EXEMPT_CODE_BASIS_MISMATCH`, а не «мелкое предупреждение»;
5. невычитаемый НДС списывается проводкой Дт 91.02 Кт 19.03 ровно на сумму раздела 7 и
   повторно не проводится;
6. разделы 8 и 9 — книги, перенесённые в декларацию, и контрольные соотношения между ними и
   разделом 3 считаются числами, а не «на глаз»;
7. номера строк помечены «на проверку», чего нет — написано.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pto_agent import invoices, ledger, vat_sections

REPO = Path(__file__).resolve().parents[1]


# ─────────────────────────── сценарии ───────────────────────────
def demo_states(tmp_path: Path) -> tuple[dict, dict]:
    """Журнал и реестр на демо-первичке: приходы с НДС на 19.03 и акты за октябрь 2026.

    Берётся тот же демо-контур, что у экранов, иначе НДС на 19.03 неоткуда взять: приход
    с НДС появляется в журнале из накладных, а не из реестра счетов-фактур.
    """
    from pto_agent.web import build_demo

    build_demo(tmp_path)
    state = ledger.load(tmp_path / "ledger" / "state.json")
    acts = json.loads((REPO / "data/demo/accounting/acts_signed_scenario.json")
                      .read_text(encoding="utf-8"))
    ledger.post_acts(state, acts, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    return state, registry(state)


def journal_with_purchases() -> dict:
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        state, _ = demo_states(Path(folder))
    return state


def registry(state: dict) -> dict:
    reg = invoices.new_state(object_name="ЖК «Северный», корпус 2")
    invoices.auto_issue(reg, state, "2026-10")
    incoming = json.loads((REPO / "data/demo/purchases/incoming_2026_10.json")
                          .read_text(encoding="utf-8"))
    invoices.auto_register_purchases(reg, incoming, "2026-10", ledger_state=state)
    return reg


EXEMPT_OK = [{"document": "ДКП №14/2026", "date": "2026-10-15", "amount": 6_200_000,
              "basis": "пп. 22 п. 3 ст. 149 НК РФ"}]


def separate(**kwargs):
    state = journal_with_purchases()
    return vat_sections.separate_accounting(state, "2026-10", **kwargs)


# ─────────────────────────── пропорция ───────────────────────────
def test_proportion_uses_shipped_revenue():
    report = separate(exempt=EXEMPT_OK)
    totals = report["totals"]
    assert totals["taxable_revenue"] > 0
    expected = totals["exempt_revenue"] / (totals["taxable_revenue"] + totals["exempt_revenue"])
    assert totals["share_exempt"] == pytest.approx(round(float(expected) * 100, 2), abs=0.01)


def test_rule5_uses_expenses_not_revenue():
    """Доля 5 % — по расходам. Те же данные, разные расходы — разный итог."""
    cheap = separate(exempt=EXEMPT_OK, expenses_not_taxable=380_000, expenses_total=12_400_000)
    dear = separate(exempt=EXEMPT_OK, expenses_not_taxable=1_000_000, expenses_total=12_400_000)
    assert cheap["rule5"]["checked"] and cheap["rule5"]["applies"] is True
    assert cheap["totals"]["vat_not_deductible"] == 0, "при доле ≤ 5 % весь НДС к вычету"
    assert dear["rule5"]["applies"] is False
    assert dear["totals"]["vat_not_deductible"] > 0


def test_without_expenses_rule5_is_not_checked():
    """Нет расходов — «не проверено». Считать 5 % от выручки запрещено."""
    report = separate(exempt=EXEMPT_OK)
    rule5 = report["rule5"]
    assert rule5["checked"] is False
    assert rule5["share"] is None
    assert any("расход" in gap["what"] for gap in report["gaps"])
    # без правила 5 % действует пропорция: часть НДС невычитаема
    assert report["totals"]["vat_not_deductible"] > 0


def test_zero_taxable_revenue_is_flagged_not_silent():
    """Если облагаемой выручки в журнале нет, 100 % — это может быть ошибка данных."""
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    report = vat_sections.separate_accounting(state, "2026-10", exempt=EXEMPT_OK)
    assert any("облагаемой выручки" in text for text in report["warnings"])


# ─────────────────────────── раздел 7 ───────────────────────────
def test_exempt_without_basis_is_not_counted():
    rows = [{"document": "Акт №1", "date": "2026-10-10", "amount": 100_000}]
    report = separate(exempt=rows)
    codes = [problem["code"] for problem in report["problems"]]
    assert "EXEMPT_NO_BASIS" in codes
    assert report["totals"]["exempt_revenue"] == 0, "без основания в пропорцию не идёт"


def test_exempt_without_document_is_a_gap():
    rows = [{"amount": 100_000, "basis": "пп. 22 п. 3 ст. 149 НК РФ"}]
    report = separate(exempt=rows)
    assert report["totals"]["exempt_revenue"] == 0
    assert any("документ и дата" in gap["what"] for gap in report["gaps"])


def test_code_contradicting_basis_is_a_problem():
    """1010800 — «не объект» по п. 2 ст. 146; освобождение по ст. 149 кодируется иначе."""
    rows = [{"document": "ДКП №14/2026", "date": "2026-10-15", "amount": 6_200_000,
             "basis": "пп. 22 п. 3 ст. 149 НК РФ", "code": "1010800"}]
    report = separate(exempt=rows)
    assert [problem["code"] for problem in report["problems"]] == ["EXEMPT_CODE_BASIS_MISMATCH"]
    assert report["rows"][0]["verify"] is True


def test_known_code_passes_and_unknown_code_asks():
    rules = vat_sections.load_rules()
    known = (rules["section7_codes"] or [{}])[0]["code"]
    good = separate(exempt=[{"document": "ДКП №15/2026", "date": "2026-10-16",
                             "amount": 1_000_000, "basis": "п. 2 ст. 146 НК РФ",
                             "code": known}])
    assert [problem["code"] for problem in good["problems"]] == []
    unknown = separate(exempt=[{"document": "ДКП №16/2026", "date": "2026-10-17",
                                "amount": 1_000_000, "basis": "п. 2 ст. 146 НК РФ",
                                "code": "1099999"}])
    assert [problem["code"] for problem in unknown["problems"]] == []
    assert any("код операции раздела 7" in gap["what"] for gap in unknown["gaps"])
    assert unknown["rows"][0]["state"] == "код на проверку"


# ─────────────────────────── проводка ───────────────────────────
def test_non_deductible_posting_matches_section7_sum():
    state = journal_with_purchases()
    report = vat_sections.separate_accounting(state, "2026-10", exempt=EXEMPT_OK,
                                              expenses_not_taxable=1_000_000,
                                              expenses_total=12_400_000)
    target = report["totals"]["vat_not_deductible"]
    assert target > 0
    entry = vat_sections.post_non_deductible(state, "2026-10", separate=report)
    assert entry["posted"] is True
    assert entry["amount"] == pytest.approx(target, abs=0.01)
    debit = sum(line["amount"] for line in entry["entry"]["lines"] if line["side"] == "Дт")
    credit = sum(line["amount"] for line in entry["entry"]["lines"] if line["side"] == "Кт")
    assert debit == pytest.approx(credit) == pytest.approx(target, abs=0.01)
    assert any(line["account"] == "91.02" for line in entry["entry"]["lines"])
    assert all(line["account"] == "19.03" for line in entry["entry"]["lines"]
               if line["side"] == "Кт")


def test_posting_is_idempotent():
    state = journal_with_purchases()
    report = vat_sections.separate_accounting(state, "2026-10", exempt=EXEMPT_OK,
                                              expenses_not_taxable=1_000_000,
                                              expenses_total=12_400_000)
    first = vat_sections.post_non_deductible(state, "2026-10", separate=report)
    second = vat_sections.post_non_deductible(state, "2026-10", separate=report)
    assert first["posted"] is True
    assert second["posted"] is False
    keys = [entry.get("key") for entry in state["entries"]]
    assert keys.count("vat.non_deductible|2026-10") == 1


def test_nothing_to_post_when_rule5_applies():
    state = journal_with_purchases()
    report = vat_sections.separate_accounting(state, "2026-10", exempt=EXEMPT_OK,
                                              expenses_not_taxable=380_000,
                                              expenses_total=12_400_000)
    result = vat_sections.post_non_deductible(state, "2026-10", separate=report)
    assert result["entry"] is None
    assert result["warnings"]


# ─────────────────────────── разделы ───────────────────────────
def test_sections_use_books_and_agree_with_each_other():
    state = journal_with_purchases()
    reg = registry(state)
    report = vat_sections.vat_sections(reg, state, "2026-10", exempt=EXEMPT_OK,
                                       expenses_not_taxable=1_000_000,
                                       expenses_total=12_400_000)
    s3, s7, s8, s9 = (report["section3"], report["section7"], report["section8"],
                      report["section9"])
    assert s8["totals"]["vat"] > 0 and s9["totals"]["vat"] > 0
    statuses = {item["what"]: item["status"] for item in report["checks"]}
    assert statuses["итог раздела 9 = начислено по журналу"] == vat_sections.OK
    assert statuses["итог раздела 8 = вычет + НДС не к вычету"] == vat_sections.OK
    # раздел 3 считается по книгам, а не по оборотам 68.02
    assert s3["totals"]["book_deducted"] == pytest.approx(s8["totals"]["vat"], abs=0.01)
    assert s3["totals"]["vat_not_deductible"] == pytest.approx(s7["totals"]["vat_not_deducted"]
                                                               if "vat_not_deducted" in s7["totals"]
                                                               else 0, abs=0.01)
    assert report["problems"] == []
    assert report["ready"] is True


def test_empty_period_is_honest():
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    reg = invoices.new_state(object_name="ЖК «Северный», корпус 2")
    report = vat_sections.vat_sections(reg, state, "2026-10")
    texts = [gap["what"] for gap in report["gaps"]]
    assert any("книга продаж" in text for text in texts)
    assert report["checks"] == [] or all(item["left"] == 0 for item in report["checks"])


def test_line_numbers_are_marked_to_verify():
    state = journal_with_purchases()
    reg = registry(state)
    report = vat_sections.vat_sections(reg, state, "2026-10", exempt=EXEMPT_OK)
    assert report["layout_verify"]["verify"] is True
    assert all(row["verify_line"] for row in report["section3"]["rows"])
    assert report["not_implemented"], "список «не сделано» не должен быть пустым"


def test_last_day_is_a_full_date():
    """Раньше функция отдавала «MM-DD», и дата проводки склеивалась в «2026-10-10-31»."""
    assert vat_sections._last_day("2026-10") == "2026-10-31"
    assert vat_sections._last_day("2026-02") == "2026-02-28"


def test_markdown_says_what_is_missing():
    state = journal_with_purchases()
    reg = registry(state)
    report = vat_sections.vat_sections(reg, state, "2026-10", exempt=EXEMPT_OK,
                                       expenses_not_taxable=1_000_000,
                                       expenses_total=12_400_000)
    text = vat_sections.to_markdown(report)
    for fragment in ("Раздел 3", "Раздел 7", "Раздел 8", "Раздел 9",
                     "Контрольные соотношения", "Не сделано", "Проводка (предложение)"):
        assert fragment in text
