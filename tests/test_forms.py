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
