# -*- coding: utf-8 -*-
"""Печатные формы КС-3, КС-6а, М-29: цифры, которые форма не имеет права скрывать.

Проверяем не «файл создался», а правила:
1. акт «на проверку» не попадает в денежный итог и не исчезает из формы;
2. НДС считается построчно, итог с НДС = сумма проверенных + НДС;
3. отсутствующий остаток на начало не превращается в ноль — остаток на конец «не определён»;
4. норматив и расход в разных единицах не сравниваются;
5. расход без прихода — отдельное состояние, а не «минус»;
6. пустой реквизит — «не задано» + запрос, а не прочерк;
7. на каждом бланке стоит «ЧЕРНОВИК» (в DOCX — в колонтитуле);
8. суммы формы сходятся с актами, а расхождение печатается, а не подгоняется.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import forms


def act(num, amount, *, month="2026-09", date="2026-09-30", status="проверен", positions=None):
    return {"act_id": f"КС-2 №{num}", "month": month, "act_date": date, "amount_rub": amount,
            "status": status, "positions": positions or []}


def position(code, qty, unit, price, amount=None, name=""):
    return {"code": code, "qty": qty, "unit": unit, "price": price,
            "amount_rub": amount if amount is not None else qty * price, "name": name}


@pytest.fixture
def demo_like_summary():
    """Мини-аналог out/summary.json: те же поля, что пишет `pto_agent run`."""
    return {"object": "ЖК «Северный», корпус 2", "period": ["2026-10-01", "2026-10-31"],
            "vat_rate": 0.2, "lines": [
                {"code": "ФЕР 06-01-001-01", "name": "Плита", "unit": "м3",
                 "qty_journal": 1240, "qty_claim": 1000, "price": 18500,
                 "restrictions": ["объём ограничен остатком сметы: 1000 м3"]},
                {"code": "ФЕР 06-01-001-02", "name": "Балки", "unit": "м3",
                 "qty_journal": 390, "qty_claim": 0, "price": 21400,
                 "restrictions": ["нет подтверждающих документов (АОСР/исп. схема)"]}]}


ACTS = [
    act(35, 4168000, positions=[position("ФЕР 06-01-001-01", 200, "м3", 18500),
                                position("ФЕР 08-02-001-01", 600, "м2", 780)]),
    act(37, 248000, positions=[position("ФЕР 16-02-003-01", 200, "м", 1240)]),
    act(38, 34106600, month="2026-10", date="2026-10-31"),
]


# ─────────────────────────── КС-3 ───────────────────────────
def test_ks3_sums_only_checked_acts():
    form = forms.build_ks3([act(1, 1000, status="проверен"), act(2, 500, status="на проверку")],
                           period="2026-09", vat_rate=20)
    assert form["totals"]["amount_checked"] == 1000.0
    assert form["totals"]["amount_review"] == 500.0
    # в итог с НДС попадает только проверенный акт
    assert form["totals"]["total_checked_with_vat"] == 1200.0
    assert any("на проверку" in row["cells"][6] for row in form["rows"])


def test_ks3_review_amount_is_excluded_and_named():
    form = forms.build_ks3([act(1, 1000), act(2, 500, status="на проверку")], period="2026-09",
                           vat_rate=20)
    asks = " ".join(gap["what"] for gap in form["gaps"])
    assert "500,00" in asks  # сумма непроверенного акта названа, а не спрятана


def test_ks3_vat_is_per_row_and_totals_match():
    form = forms.build_ks3(ACTS, period="2026-09", vat_rate=20)
    vat_sum = sum(float(row["cells"][4].replace(" ", "").replace(",", ".")) for row in form["rows"])
    assert round(vat_sum, 2) == form["totals"]["vat_checked"] == 883200.0


def test_ks3_period_filter_keeps_october_out():
    form = forms.build_ks3(ACTS, period="2026-09", vat_rate=20)
    assert {row["cells"][0] for row in form["rows"]} == {"КС-2 №35", "КС-2 №37"}


def test_ks3_missing_requisites_are_gaps_not_dashes():
    form = forms.build_ks3(ACTS, period="2026-09", vat_rate=20)
    assert form["requisites"]["Заказчик"]["missing"] is True
    assert any(gap["what"] == "Заказчик" for gap in form["gaps"])


def test_ks3_missing_vat_rate_is_a_gap_not_silent_20():
    form = forms.build_ks3(ACTS, period="2026-09")
    assert any("НДС" in gap["what"] for gap in form["gaps"])
    assert any("20" in warning for warning in form["warnings"])


def test_ks3_act_without_positions_is_reported():
    form = forms.build_ks3([act(99, 34106600, month="2026-10", date="2026-10-31")],
                           period="2026-10", vat_rate=20)
    assert any("расшифровка" in gap["what"] for gap in form["gaps"])


def test_ks3_position_sum_mismatch_is_printed_not_hidden():
    bad = act(40, 1000, positions=[position("ФЕР X", 10, "м2", 90)])  # 900 против 1000
    form = forms.build_ks3([bad], period="2026-09", vat_rate=20)
    assert any("≠" in warning for warning in form["warnings"])
    assert "≠" in form["rows"][0]["note"]


def test_ks3_empty_period_gives_gap_not_empty_silence():
    form = forms.build_ks3([], period="2026-12", vat_rate=20)
    assert form["rows"] == []
    assert any("акты за период" in gap["what"] for gap in form["gaps"])


# ─────────────────────────── КС-6а ───────────────────────────
def test_ks6a_accumulates_by_code_across_acts():
    acts = [act(1, 1000, positions=[position("ФЕР 1", 10, "м2", 100)]),
            act(2, 500, date="2026-09-15", positions=[position("ФЕР 1", 5, "м2", 100)])]
    form = forms.build_ks6a(acts, period="2026-09")
    assert len(form["rows"]) == 1
    assert form["rows"][0]["cells"][4] == "15"
    assert form["totals"]["amount_period_net"] == 1500.0


def test_ks6a_takes_earlier_acts_into_since_start():
    acts = [act(1, 1000, month="2026-08", date="2026-08-31",
                positions=[position("ФЕР 1", 10, "м2", 100)]),
            act(2, 500, positions=[position("ФЕР 1", 5, "м2", 100)])]
    form = forms.build_ks6a(acts, period="2026-09")
    assert form["rows"][0]["cells"][4] == "5"   # за период
    assert form["rows"][0]["cells"][5] == "15"  # с начала договора


def test_ks6a_names_resolved_from_dictionary_or_asked_for():
    acts = [act(1, 1000, positions=[position("ФЕР 1", 10, "м2", 100)])]
    with_names = forms.build_ks6a(acts, period="2026-09", names={"ФЕР 1": "Кладка перегородок"})
    assert with_names["rows"][0]["cells"][1] == "Кладка перегородок"
    without = forms.build_ks6a(acts, period="2026-09")
    assert any("наименование работ" in gap["what"] for gap in without["gaps"])


def test_ks6a_price_times_qty_mismatch_is_printed():
    acts = [act(1, 1000, positions=[position("ФЕР 1", 10, "м2", 95, amount=1000)])]
    form = forms.build_ks6a(acts, period="2026-09")
    assert any("≠" in warning for warning in form["warnings"])


def test_ks6a_october_act_without_positions_explains_itself():
    form = forms.build_ks6a(ACTS, period="2026-10")
    assert form["rows"] == []  # работ с движением в периоде нет
    assert any("расшифровка актов КС-2 №38" in gap["what"] for gap in form["gaps"])


def test_ks6a_shows_only_works_with_movement_in_period():
    form = forms.build_ks6a(ACTS, period="2026-09")
    assert all(row["cells"][4] != "0" for row in form["rows"])
    assert len(form["rows"]) == 3


# ─────────────────────────── М-29 ───────────────────────────
PURCHASES = [{"код": "Бетон B25", "наименование": "Бетон товарный B25", "ед.": "м3",
              "количество": "230", "цена": "6200", "ндс": "20", "документ": "УПД №118"},
             {"код": "Арматура A500", "наименование": "Арматура", "ед.": "т",
              "количество": "16", "цена": "68000", "ндс": "20", "документ": "УПД №119"}]
WRITEOFFS = [{"код": "Бетон B25", "наименование": "Бетон товарный B25", "ед.": "м3",
              "количество": "210", "документ": "М-29 №4", "причина": ""},
             {"код": "Арматура A500", "наименование": "Арматура", "ед.": "т",
              "количество": "19,5", "документ": "М-29 №4", "причина": ""},
             {"код": "Керамзит", "наименование": "Керамзит", "ед.": "м3",
              "количество": "12", "документ": "М-29 №4", "причина": ""}]
NORMS = [{"work_code": "ФЕР 06", "material_code": "Бетон B25", "unit": "м3", "norm": 1.02}]
WORKS = [{"code": "ФЕР 06", "qty": 200}]


def test_m29_without_opening_does_not_invent_zero_balance():
    form = forms.build_m29(PURCHASES, WRITEOFFS, period="2026-10", norms=NORMS, works=WORKS)
    assert form["totals"]["balance_defined"] is False
    assert all(row["cells"][7] == "—" for row in form["rows"])
    assert any("остаток на начало" in gap["what"] for gap in form["gaps"])


def test_m29_with_opening_balances():
    form = forms.build_m29(PURCHASES, WRITEOFFS, period="2026-10", opening={"Бетон B25": 10})
    row = next(r for r in form["rows"] if r["cells"][0].startswith("Бетон"))
    assert row["cells"][7] == "30"  # 10 + 230 − 210
    assert form["totals"]["balance_defined"] is True


def test_m29_norm_and_overrun():
    form = forms.build_m29(PURCHASES, WRITEOFFS, period="2026-10", norms=NORMS, works=WORKS)
    row = next(r for r in form["rows"] if r["cells"][0].startswith("Бетон"))
    assert row["cells"][5] == "204"   # норма 1,02 × 200 м3
    assert row["cells"][6] == "6"     # перерасход
    assert "перерасход" in row["note"]


def test_m29_unit_mismatch_blocks_comparison():
    norms = [{"work_code": "ФЕР 06", "material_code": "Бетон B25", "unit": "литр", "norm": 1.02}]
    form = forms.build_m29(PURCHASES, WRITEOFFS, period="2026-10", norms=norms, works=WORKS)
    row = next(r for r in form["rows"] if r["cells"][0].startswith("Бетон"))
    assert row["state"] == "unit_mismatch"
    assert row["cells"][6] == "—"
    assert form["totals"]["unit_mismatch"] == 1


def test_m29_expense_without_receipt_is_its_own_state():
    form = forms.build_m29(PURCHASES, WRITEOFFS, period="2026-10")
    row = next(r for r in form["rows"] if r["cells"][0].startswith("Керамзит"))
    assert row["state"] == "expense_without_receipt"
    assert row["cells"][3] == "—"  # прихода нет, а не «0»
    assert form["totals"]["expense_without_receipt"] == 1


def test_m29_materials_without_norm_are_listed():
    form = forms.build_m29(PURCHASES, WRITEOFFS, period="2026-10", norms=NORMS, works=WORKS)
    assert form["totals"]["without_norm"] >= 1
    assert any("норма расхода" in gap["what"] for gap in form["gaps"])


def test_m29_broken_quantity_is_reported_not_dropped_silently():
    broken = [{"код": "Бетон B25", "наименование": "Бетон", "ед.": "м3", "количество": "много"}]
    form = forms.build_m29(broken, [], period="2026-10")
    assert any("не разобрано" in warning for warning in form["warnings"])


# ─────────────────────────── бланк целиком ───────────────────────────
def test_every_form_is_marked_draft():
    assert forms.STATUS_DRAFT == "черновик"
    form = forms.build_ks3(ACTS, period="2026-09", vat_rate=20)
    assert "ЧЕРНОВИК" in form["draft_notice"]
    assert "не подписано" in form["draft_notice"].lower()


def test_docx_has_draft_in_header(tmp_path):
    from docx import Document

    form = forms.build_ks3(ACTS, period="2026-09", vat_rate=20)
    path = forms.to_docx(form, tmp_path / "ks3.docx")
    document = Document(str(path))
    assert "ЧЕРНОВИК" in document.sections[0].header.paragraphs[0].text
    table = document.tables[0]
    assert len(table.rows) == len(form["rows"]) + 1  # шапка + строки
    assert table.rows[1].cells[0].text == "КС-2 №35"


def test_xlsx_has_draft_and_gaps_sheet(tmp_path):
    from openpyxl import load_workbook

    form = forms.build_m29(PURCHASES, WRITEOFFS, period="2026-10", norms=NORMS, works=WORKS)
    path = forms.to_xlsx(form, tmp_path / "m29.xlsx")
    workbook = load_workbook(path)
    assert "ЧЕРНОВИК" in workbook[form["code"]].cell(row=1, column=1).value
    assert "требует данных" in workbook.sheetnames
    assert workbook["требует данных"].max_row > 1


def test_markdown_totals_are_russian_not_machine_keys():
    form = forms.build_ks3(ACTS, period="2026-09", vat_rate=20)
    text = forms.to_markdown(form)
    assert "сумма проверенных актов" in text
    assert "amount_checked" not in text


def test_write_produces_four_files(tmp_path):
    form = forms.build_ks6a(ACTS, period="2026-09")
    paths = forms.write(form, tmp_path / "forms")
    for key in ("json", "docx", "xlsx", "md"):
        assert Path(paths[key]).exists(), key
    payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert payload["schema"] == "pto-agent/print-form@1"


def test_unknown_form_raises():
    with pytest.raises(ValueError):
        forms.build_from_files("ks4", period="2026-09")


# ─────────────────────────── демо-артефакты ───────────────────────────
DEMO_ACTS = ROOT / "data" / "demo" / "acts" / "acts.json"
DEMO_CONS = ROOT / "data" / "demo" / "consumption"


@pytest.mark.skipif(not DEMO_ACTS.exists(), reason="демо-акты отсутствуют")
def test_demo_ks3_matches_acts_sum():
    form = forms.build_from_files("ks3", period="2026-09", acts_path=str(DEMO_ACTS), vat_rate=20)
    payload = json.loads(DEMO_ACTS.read_text(encoding="utf-8"))
    expected = sum(a["amount_rub"] for a in payload["acts"] if a["month"] == "2026-09")
    assert form["totals"]["amount_checked"] == expected == 4416000.0
    assert form["requisites"]["Объект"]["value"] == payload["object"]


@pytest.mark.skipif(not (DEMO_CONS / "purchases.csv").exists(), reason="демо-склад отсутствует")
def test_demo_m29_balances_receipts_and_expenses():
    form = forms.build_from_files(
        "m29", period="2026-10", purchases_path=str(DEMO_CONS / "purchases.csv"),
        writeoffs_path=str(DEMO_CONS / "writeoffs.csv"),
        norms_path=str(DEMO_CONS / "norms.json"), works_path=str(DEMO_CONS / "works.json"))
    assert form["totals"]["materials"] == 4
    assert form["totals"]["expense_without_receipt"] == 1  # керамзит: расход без прихода
    assert form["totals"]["rows_with_overrun"] == 2


# ─────────────────────────── КС-2: один механизм с остальными формами ───────────────────────────
def ks2_position(code, journal, claim, price, *, restrictions=None, unit="м3", name=""):
    return {"code": code, "name": name or f"Работа {code}", "unit": unit,
            "qty_journal": journal, "qty_claim": claim, "price": price,
            "restrictions": restrictions or []}


def test_ks2_claims_less_than_journal_and_shows_both():
    form = forms.build_ks2([ks2_position("ФЕР 1", 1240, 1000, 18500)], period_from="2026-10-01",
                           period_to="2026-10-31", vat_rate=20)
    row = form["rows"][0]
    assert row["cells"][4] == "1240"      # по журналу
    assert row["cells"][5] == "1000"      # к предъявлению
    assert form["totals"]["amount_claim"] == 18500000.0
    assert form["totals"]["amount_blocked"] == 4440000.0


def test_ks2_blocked_position_stays_in_act_with_reason():
    form = forms.build_ks2(
        [ks2_position("ФЕР 2", 390, 0, 21400, restrictions=["нет подтверждающих документов"])],
        period_from="2026-10-01", period_to="2026-10-31", vat_rate=20)
    row = form["rows"][0]
    assert row["state"] == "blocked"
    assert "нет подтверждающих документов" in row["cells"][8]
    assert row["cells"][7] == "0,00"
    assert form["totals"]["positions_fully_blocked"] == 1
    assert any("ФЕР 2" in gap["what"] for gap in form["gaps"])


def test_ks2_vat_only_from_claimable_amount():
    form = forms.build_ks2([ks2_position("ФЕР 1", 100, 60, 1000)], period_from="2026-10-01",
                           period_to="2026-10-31", vat_rate=20)
    assert form["totals"]["vat_claim"] == 12000.0          # 20 % от 60 000, не от 100 000
    assert form["totals"]["total_claim_with_vat"] == 72000.0


def test_ks2_position_without_price_is_not_priced():
    form = forms.build_ks2([{"code": "ФЕР 3", "name": "X", "unit": "м2",
                             "qty_journal": 10, "qty_claim": 10, "price": None}],
                           period_from="2026-10-01", period_to="2026-10-31", vat_rate=20)
    assert form["rows"][0]["state"] == "no_price"
    assert form["rows"][0]["cells"][7] == "—"
    assert any("нет цены" in warning for warning in form["warnings"])


def test_ks2_empty_positions_is_a_gap_not_a_zero_act():
    form = forms.build_ks2([], period_from="2026-10-01", period_to="2026-10-31", vat_rate=20)
    assert form["rows"] == []
    assert any("позиции акта" in gap["what"] for gap in form["gaps"])
    assert form["totals"]["amount_claim"] == 0.0


def test_ks2_from_summary_equals_reconciliation_path(demo_like_summary):
    """Один механизм: акт из сверки и акт из summary.json дают одни и те же суммы."""
    from pto_agent import forms as f
    from_summary = f.ks2_from_summary(demo_like_summary, vat_rate=20)
    by_hand = f.build_ks2(
        [ks2_position("ФЕР 06-01-001-01", 1240, 1000, 18500,
                      restrictions=["объём ограничен остатком сметы: 1000 м3"]),
         ks2_position("ФЕР 06-01-001-02", 390, 0, 21400,
                      restrictions=["нет подтверждающих документов (АОСР/исп. схема)"])],
        period_from="2026-10-01", period_to="2026-10-31", vat_rate=20)
    assert from_summary["totals"]["amount_claim"] == by_hand["totals"]["amount_claim"]
    assert from_summary["totals"]["amount_blocked"] == by_hand["totals"]["amount_blocked"]
    assert from_summary["totals"]["total_claim_with_vat"] == by_hand["totals"]["total_claim_with_vat"]


def test_ks2_from_summary_skips_unit_mismatch_lines():
    payload = {"object": "X", "period": ["2026-10-01", "2026-10-31"], "lines": [
        {"code": "A", "name": "a", "unit": "м3", "qty_journal": 10, "qty_claim": 10,
         "price": 100, "restrictions": []},
        {"code": "B", "name": "b", "unit": "м3", "qty_journal": 999, "qty_claim": 999,
         "price": 100, "restrictions": [], "unit_mismatch": True}]}
    form = forms.ks2_from_summary(payload, vat_rate=20)
    assert [row["cells"][1] for row in form["rows"]] == ["A"]
    assert form["totals"]["amount_claim"] == 1000.0


def test_ks2_numbers_are_numbers_in_excel(tmp_path):
    """В бланке Excel суммы — числа, а не тексты: иначе бухгалтерия их не сложит."""
    from openpyxl import load_workbook

    form = forms.build_ks2([ks2_position("ФЕР 1", 100, 60, 1000)], period_from="2026-10-01",
                           period_to="2026-10-31", vat_rate=20)
    ws = load_workbook(forms.to_xlsx(form, tmp_path / "ks2.xlsx"), data_only=True)["КС-2"]
    row = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 2).value == "ФЕР 1")
    assert ws.cell(row, 6).value == 60          # к предъявлению — число
    assert ws.cell(row, 8).value == 60000       # стоимость — число
    totals = [ws.cell(r, 2).value for r in range(1, ws.max_row + 1)]
    assert 60000 in totals and 40000 in totals  # предъявляется / не предъявляется


# ─────────────────────────── складские бланки М-4 / М-11 / М-8 / М-15 / ИНВ-3 ───────────────────────────
from pto_agent import stock as ledger                              # noqa: E402

STOCK_DOCS = ROOT / "data" / "demo" / "stock" / "documents_2026_10.json"
STOCK_LIMITS = ROOT / "data" / "demo" / "stock" / "limits_2026_10.json"


@pytest.fixture(scope="module")
def posted_state():
    return ledger.demo_state()


def test_m4_receipt_form_has_amounts_and_batch(posted_state):
    form = forms.build_stock_form(
        "m4", {"kind": "receipt", "number": "УПД №9", "date": "2026-10-01", "warehouse": "СК-1",
               "lines": [{"item": "Бетон B25", "qty": 10, "price": 6200}]}, state=posted_state)
    assert form["code"] == "М-4"
    assert forms._dec(form["rows"][0]["values"][5]) == 62000   # сумма в бланке
    assert form["rows"][0]["cells"][6].startswith("УПД №9")  # партия ссылается на документ
    assert form["totals"]["amount_total"] == 62000.0
    assert form["totals"]["mol"] == "Ким А.П."


def test_m4_receipt_without_price_is_flagged(posted_state):
    form = forms.build_stock_form(
        "m4", {"kind": "receipt", "number": "УПД №10", "date": "2026-10-01", "warehouse": "СК-1",
               "lines": [{"item": "Бетон B25", "qty": 10}]}, state=posted_state)
    assert form["rows"][0]["state"] == "no_price"
    assert any("без цены" in warning for warning in form["warnings"])


def test_m11_requirement_keeps_object_and_flags_missing(posted_state):
    form = forms.build_stock_form(
        "m11", {"kind": "issue", "number": "М-11 №77", "date": "2026-10-05", "warehouse": "СК-1",
                "lines": [{"item": "Бетон B25", "qty": 5, "object": "плита, захватка 2"},
                          {"item": "Керамзит", "qty": 1}]}, state=posted_state)
    assert form["rows"][0]["cells"][5] == "плита, захватка 2"
    assert form["rows"][1]["cells"][5] == "не задано"
    assert any("объект/назначение" in gap["what"] for gap in form["gaps"])
    assert "amount_total" not in form["totals"]  # деньги живут не в требовании


def test_m11_over_issue_is_flagged(posted_state):
    form = forms.build_stock_form(
        "m11", {"kind": "issue", "number": "М-11 №78", "date": "2026-10-05", "warehouse": "СК-1",
                "lines": [{"item": "Бетон B25", "qty": 5, "issued": 7, "object": "плита"}]},
        state=posted_state)
    assert form["rows"][0]["state"] == "over_issue"
    assert any("больше затребованного" in w for w in form["warnings"])


@pytest.mark.skipif(not STOCK_LIMITS.exists(), reason="нет демо-лимитов")
def test_m8_counts_used_from_moves_and_flags_overrun(posted_state):
    doc = json.loads(STOCK_LIMITS.read_text(encoding="utf-8"))
    form = forms.build_stock_form("m8", doc, state=posted_state)
    row = next(r for r in form["rows"] if r["cells"][1].startswith("Бетон"))
    assert forms._dec(row["values"][3]) == forms._dec("208.8")   # лимит
    assert forms._dec(row["values"][4]) == 210                   # отпущено из движений
    assert forms._dec(row["values"][5]) == forms._dec("-1.2")    # остаток лимита отрицательный
    assert form["totals"]["over_limit"] >= 1
    assert any("сверх лимита" in w for w in form["warnings"])


def test_m8_without_limit_is_a_gap(posted_state):
    form = forms.build_stock_form(
        "m8", {"kind": "limit", "number": "М-8 №1", "date": "2026-10-31", "warehouse": "СК-1",
               "lines": [{"item": "Бетон B25"}]}, state=posted_state)
    assert form["rows"][0]["state"] == "no_limit"
    assert any("лимит" in gap["what"].lower() for gap in form["gaps"])


def test_m15_shows_both_warehouses(posted_state):
    form = forms.build_stock_form(
        "m15", {"kind": "transfer", "number": "М-15 №9", "date": "2026-10-06",
                "warehouse": "СК-1", "warehouse_to": "ПР-2",
                "lines": [{"item": "Труба ПВХ 110", "qty": 10}]}, state=posted_state)
    row = form["rows"][0]
    assert row["cells"][6] == "СК-1" and row["cells"][7] == "ПР-2"
    assert any("цена берётся из партии" in w for w in form["warnings"])


def test_m15_without_target_is_a_gap(posted_state):
    form = forms.build_stock_form(
        "m15", {"kind": "transfer", "number": "М-15 №10", "date": "2026-10-06",
                "warehouse": "СК-1", "lines": [{"item": "Труба ПВХ 110", "qty": 10}]},
        state=posted_state)
    assert form["rows"][0]["state"] == "no_target"
    assert any("склад-получатель" in gap["what"] for gap in form["gaps"])


def test_inv3_shows_fact_book_and_delta(posted_state):
    form = forms.build_stock_form(
        "inv3", {"kind": "inventory", "number": "ИНВ-3 №9", "date": "2026-10-31",
                 "warehouse": "СК-1",
                 "lines": [{"item": "Бетон B25", "qty": 36, "price": 6200},
                           {"item": "Керамзит", "qty": 20}]}, state=posted_state)
    beton = form["rows"][0]
    assert forms._dec(beton["values"][3]) == 36   # факт по описи
    assert forms._dec(beton["values"][4]) == 35   # учёт — из движений склада
    assert forms._dec(beton["values"][5]) == 1    # излишек
    assert form["totals"]["surplus_amount"] > 0
    assert form["totals"]["charge"] > 0                              # недостача керамзита


def test_inv3_without_state_asks_for_book_balance():
    form = forms.build_stock_form(
        "inv3", {"kind": "inventory", "number": "ИНВ-3 №9", "date": "2026-10-31",
                 "warehouse": "СК-1", "lines": [{"item": "Бетон", "qty": 36}]})
    assert form["rows"][0]["state"] == "no_book"
    assert any("учётный остаток" in gap["what"] for gap in form["gaps"])


def test_writeoff_without_reason_is_flagged(posted_state):
    form = forms.build_stock_form(
        "writeoff", {"kind": "writeoff", "number": "акт №9", "date": "2026-10-20",
                     "warehouse": "СК-1", "lines": [{"item": "Бетон B25", "qty": 1}]},
        state=posted_state)
    assert form["rows"][0]["state"] == "no_reason"
    assert any("причина списания" in gap["what"] for gap in form["gaps"])


def test_daval_form_is_free_form_and_not_zero_priced(posted_state):
    form = forms.build_stock_form(
        "daval", {"kind": "daval_receipt", "number": "М-35 №9", "date": "2026-10-07",
                  "warehouse": "ПР-2", "lines": [{"item": "Труба ПВХ 110", "qty": 100}]},
        state=posted_state)
    assert "типовой бланк не установлен" in form["approved_by"]
    assert form["rows"][0]["cells"][4] == "по данным заказчика"  # не 0 и не «—»
    assert any("стоимость по данным заказчика" in w.lower() for w in form["warnings"])


def test_stock_form_refuses_wrong_document_kind(posted_state):
    with pytest.raises(ValueError):
        forms.build_stock_form("m4", {"kind": "issue", "number": "X", "date": "2026-10-01",
                                      "warehouse": "СК-1", "lines": []}, state=posted_state)


def test_unposted_document_is_named_in_form(posted_state):
    form = forms.build_stock_form(
        "m11", {"kind": "issue", "number": "М-11 №999", "date": "2026-10-09",
                "warehouse": "СК-1", "lines": [{"item": "Бетон B25", "qty": 1,
                                                "object": "плита"}]}, state=posted_state)
    assert any("не проведён" in w for w in form["warnings"])
    assert any("проведение" in gap["what"] for gap in form["gaps"])


def test_warehouse_mol_missing_is_a_gap():
    state = ledger.new_state(warehouses=[{"code": "СК-9"}],
                             items=[{"code": "Бетон", "unit": "м3"}])
    form = forms.build_stock_form(
        "m4", {"kind": "receipt", "number": "УПД №1", "date": "2026-10-01", "warehouse": "СК-9",
               "lines": [{"item": "Бетон", "qty": 1, "price": 100}]}, state=state)
    assert any("МОЛ" in gap["what"] for gap in form["gaps"])


def test_stock_form_files_do_not_overwrite_each_other(tmp_path):
    state = ledger.demo_state()
    docs = [{"kind": "receipt", "number": "УПД №1", "date": "2026-10-01", "warehouse": "СК-1",
             "lines": [{"item": "Бетон B25", "qty": 1, "price": 6200}]},
            {"kind": "receipt", "number": "УПД №2", "date": "2026-10-01", "warehouse": "СК-1",
             "lines": [{"item": "Бетон B25", "qty": 2, "price": 6200}]}]
    paths = [forms.write(forms.build_stock_form("m4", doc, state=state), tmp_path) for doc in docs]
    assert paths[0]["docx"] != paths[1]["docx"]
    assert len(list(tmp_path.glob("m4_*.docx"))) == 2


@pytest.mark.skipif(not STOCK_DOCS.exists(), reason="нет демо-документов склада")
def test_every_demo_document_gets_its_form(tmp_path):
    state = ledger.demo_state()
    documents = json.loads(STOCK_DOCS.read_text(encoding="utf-8"))["documents"]
    mapping = {"receipt": "m4", "issue": "m11", "transfer": "m15", "inventory": "inv3",
               "writeoff": "writeoff", "daval_receipt": "daval", "daval_return": "daval"}
    made = 0
    for doc in documents:
        form = forms.build_stock_form(mapping[doc["kind"]], doc, state=state)
        forms.write(form, tmp_path)
        made += 1
    assert made == len(documents)
    assert len(list(tmp_path.glob("*.docx"))) == made
