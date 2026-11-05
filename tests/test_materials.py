# -*- coding: utf-8 -*-
"""Материалы: заявка (решение) → приход (накладные) → КС-2."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.materials import (ACT_NOT_DELIVERED, ACT_WITHOUT_RECEIPT, DELIVERED_NOT_ORDERED,
                                 ORDERED_NOT_DELIVERED, PRICE_DOWN, PRICE_UP, SUPPLIER_MISMATCH,
                                 UNIT_MISMATCH, export_materials_xlsx, materials_to_markdown,
                                 orders_from_decision, read_act_materials, read_receipts,
                                 reconcile, write)
from pto_agent.procurement import build_request, compare_quotes, decide

RECEIPTS = ROOT / "data" / "demo" / "materials" / "receipts_1166n.csv"
ACT = ROOT / "data" / "demo" / "materials" / "act_1166n_materials.csv"
CABLE = "21.1.06.09-0151"
AUTOMAT = "62.1.01.09-1101"
BOX = "20.5.02.07-0003"


def order(code, qty, price, supplier="ООО «А»", unit="шт", name="Материал"):
    return {"code": code, "name": name, "unit": unit, "qty": qty, "price_vat": price,
            "supplier": supplier}


def receipt(code, qty, price, supplier="ООО «А»", unit="шт", document="УПД-1"):
    return {"code": code, "name": "Материал", "unit": unit, "qty": qty, "price": price,
            "vat_rate": 20, "price_vat": round(price * 1.2, 2), "supplier": supplier,
            "document": document, "doc_date": "2026-11-10"}


def act(code, qty, price, unit="шт", document="КС-2 № 4"):
    return {"code": code, "name": "Материал", "unit": unit, "qty": qty, "price": price,
            "vat_rate": 20, "price_vat": round(price * 1.2, 2), "act_no": document}


def kinds(report):
    return [d["kind"] for d in report["discrepancies"]]


# ─────────────────────────────── чтение файлов ───────────────────────────────
def test_receipts_read_numbers_and_supplier():
    receipts, problems = read_receipts(RECEIPTS)
    assert problems == []
    cable = next(r for r in receipts if r["code"] == CABLE)
    assert cable["qty"] == pytest.approx(0.30)
    assert cable["price"] == 64800 and cable["supplier"].startswith("ООО")
    assert cable["document"] == "УПД-4412"


def test_receipt_without_price_is_read_but_flagged(tmp_path):
    path = tmp_path / "receipts.csv"
    path.write_text("Накладная;Код;Кол-во;Ед.;Цена\nУПД-9;01-1;5;шт;\n", encoding="utf-8")
    receipts, problems = read_receipts(path)
    assert receipts[0]["qty"] == 5 and receipts[0]["price"] is None
    assert "цена" in receipts[0]["missing"]
    assert any("цена" in p for p in problems)


def test_act_without_material_rows_is_not_an_empty_act(tmp_path):
    path = tmp_path / "act.csv"
    path.write_text("Акт;Код;Наименование;Ед.;Кол-во\n", encoding="utf-8")
    materials, problems = read_act_materials(path)
    assert materials == []
    assert any("построчных материалов" in p for p in problems)


# ─────────────────────────────── сверка ───────────────────────────────
def test_ordered_not_delivered_freezes_money():
    report = reconcile([order(CABLE, 1.0, 1000)], [])
    item = report["discrepancies"][0]
    assert item["kind"] == ORDERED_NOT_DELIVERED
    assert item["money"] == pytest.approx(1000.0)
    assert item["money_kind"] == "frozen"
    assert item["formula"].startswith("1 × 1000")
    assert "поставщику" in item["action"]


def test_partial_delivery_splits_frozen_and_claim_risk():
    """0,35 в акте при 0,30 прихода из 0,40 заказа: 0,05 — риск акта, 0,05 — заморожено."""
    report = reconcile([order(CABLE, 0.40, 1000)], [receipt(CABLE, 0.30, 1000)],
                       [act(CABLE, 0.35, 1000)])
    money = {d["kind"]: d["money"] for d in report["discrepancies"]}
    assert money[ACT_WITHOUT_RECEIPT] == pytest.approx(60.0)     # 0,05 × 1200 (цена акта с НДС)
    assert money[ORDERED_NOT_DELIVERED] == pytest.approx(50.0)   # 0,40 − 0,30 − 0,05, цена решения
    assert report["totals"]["frozen_rub"] == pytest.approx(50.0)
    assert report["totals"]["claim_risk_rub"] == pytest.approx(60.0)


def test_delivered_not_ordered_counts_as_risk():
    report = reconcile([], [receipt("20.5.02.04-0001", 200, 45, unit="м")])
    item = report["discrepancies"][0]
    assert item["kind"] == DELIVERED_NOT_ORDERED
    assert item["money"] == pytest.approx(200 * 54.0)      # цена с НДС 20 %
    assert report["totals"]["at_risk_rub"] == pytest.approx(10800.0)


def test_price_change_against_decision():
    up = reconcile([order(AUTOMAT, 10, 400)], [receipt(AUTOMAT, 10, 400 + 10 / 1.2)])
    assert PRICE_UP in kinds(up) and up["totals"]["overpay_rub"] > 0

    down = reconcile([order(AUTOMAT, 20, 516)], [receipt(AUTOMAT, 20, 395)])
    item = next(d for d in down["discrepancies"] if d["kind"] == PRICE_DOWN)
    assert item["money"] == pytest.approx((474 - 516) * 20 * -1)
    assert down["totals"]["saving_rub"] == pytest.approx(840.0)


def test_supplier_mismatch_is_named_but_not_money():
    report = reconcile([order(AUTOMAT, 20, 516, supplier="ООО «Кабель-СПб»")],
                       [receipt(AUTOMAT, 20, 395, supplier="ООО «ЭлектроТорг»")])
    item = next(d for d in report["discrepancies"] if d["kind"] == SUPPLIER_MISMATCH)
    assert item["money"] is None
    assert "Кабель-СПб" in item["detail"] and "«»" not in item["detail"]
    assert "согласование замены" in item["action"]


def test_different_units_stop_quantity_comparison():
    report = reconcile([order(CABLE, 0.3754, 64800, unit="1000 м")],
                       [receipt(CABLE, 375, 64.8, unit="м")],
                       [act(CABLE, 0.35, 64800, unit="1000 м")])
    assert UNIT_MISMATCH in kinds(report)
    assert all(d["money"] is None for d in report["discrepancies"])
    assert report["lines"][0]["status"] == "unit_mismatch"


def test_act_material_without_receipt_is_claim_risk():
    report = reconcile([order(BOX, 16, 68.4)], [], [act(BOX, 16, 57)])
    item = next(d for d in report["discrepancies"] if d["kind"] == ACT_NOT_DELIVERED)
    assert item["money"] == pytest.approx(16 * 68.4)
    assert item["money_kind"] == "claim_risk"
    assert "убрать из акта" in item["action"]


def test_no_act_means_unknown_not_zero():
    report = reconcile([order(CABLE, 1.0, 1000)], [receipt(CABLE, 0.5, 1000)])
    assert report["act_materials"] == "unknown"
    assert "ACT_MATERIALS_UNKNOWN" in kinds(report)
    assert report["totals"]["claim_risk_rub"] == 0
    assert report["status"] == "partial"


def test_missing_price_is_unpriced_not_zero():
    report = reconcile([order(CABLE, 1.0, None)], [])
    assert report["unpriced"] and "нет цены" in report["unpriced"][0]["reason"]
    assert report["discrepancies"][0]["money"] is None


def test_clean_reconcile_is_complete():
    report = reconcile([order(AUTOMAT, 20, 516)], [receipt(AUTOMAT, 20, 430)],
                       [act(AUTOMAT, 20, 430)])
    assert report["status"] == "complete"
    assert report["totals"]["frozen_rub"] == 0 and report["totals"]["at_risk_rub"] == 0


def test_no_sources_means_no_data():
    report = reconcile([], [])
    assert report["status"] == "no_data"
    assert report["totals"]["discrepancies"] == 0


def test_demo_files_give_the_expected_story():
    decision = {"request_no": "ЗМ-ДЕМО", "by": "Иванов И.И.", "decisions": [
        {**order(CABLE, 0.3754, 64800, supplier="ООО «ЭлектроТорг»", unit="1000 м")},
        {**order(AUTOMAT, 20, 516, supplier="ООО «Кабель-СПб»")},
        {**order(BOX, 16, 68.4, supplier="ООО «Кабель-СПб»")},
    ]}
    receipts, _ = read_receipts(RECEIPTS)
    act_materials, _ = read_act_materials(ACT)
    report = reconcile(orders_from_decision(decision), receipts, act_materials,
                       object_name="Дошкольное отделение", request_no="ЗМ-ДЕМО")
    totals = report["totals"]
    assert report["status"] == "partial"
    assert totals["frozen_rub"] == pytest.approx(1645.92)          # кабель 0,0254 × 64 800
    assert totals["claim_risk_rub"] == pytest.approx(3240 + 1094.4)
    assert totals["at_risk_rub"] == pytest.approx(15134.4)
    assert totals["saving_rub"] == pytest.approx(840.0)


# ───────────────────────── источники и выгрузки ─────────────────────────
def test_decision_carries_qty_and_unit():
    request = build_request([{"kind": "material", "code": CABLE, "name": "Кабель",
                              "qty": 0.5, "unit": "1000 м"}], request_no="ЗМ-1")
    quotes = [{"supplier": "ООО «А»", "code": CABLE, "price": 1000, "unit": "1000 м",
               "vat_rate": 20, "delivery_days": 5, "valid_until": "2027-01-01",
               "quote_date": "2026-11-01", "quote_no": "КП-1"}]
    comparison = compare_quotes(request, quotes)
    decision = decide(comparison, {CABLE: "ООО «А»"}, by="Петров П.П.", reason="тест")
    orders = orders_from_decision(decision)
    assert orders[0]["qty"] == pytest.approx(0.5) and orders[0]["unit"] == "1000 м"
    assert orders[0]["price_vat"] == pytest.approx(1200.0)


def test_markdown_explains_money_and_addressee():
    report = reconcile([order(CABLE, 1.0, 1000)], [])
    text = materials_to_markdown(report)
    assert "Сверка материалов" in text and "заморожено" in text
    assert "кому:" in text and "1\u00a0000,00" in text


def test_xlsx_has_three_sheets(tmp_path):
    report = reconcile([order(CABLE, 1.0, 1000)], [], [act(CABLE, 1.0, 1000)])
    path = export_materials_xlsx(report, tmp_path / "materials.xlsx")
    from openpyxl import load_workbook
    wb = load_workbook(path)
    assert wb.sheetnames == ["Сверка", "Расхождения", "Итоги"]


# ─────────────────────────────── CLI и MCP ───────────────────────────────
def _decision_file(tmp_path) -> Path:
    decision = {"schema": "pto-agent/procurement-decision@1", "request_no": "ЗМ-ТЕСТ",
                "by": "Иванов И.И.", "role": "инженер ПТО", "reason": "тест",
                "decisions": [order(CABLE, 0.3754, 64800, supplier="ООО «ЭлектроТорг»",
                                    unit="1000 м"),
                              order(BOX, 16, 68.4, supplier="ООО «Кабель-СПб»")]}
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_materials_reconcile(tmp_path):
    out = tmp_path / "materials"
    res = subprocess.run([sys.executable, "-m", "pto_agent", "materials", "--out", str(out),
                          "reconcile", "--decision", str(_decision_file(tmp_path)),
                          "--receipts", str(RECEIPTS), "--act", str(ACT)],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    assert "Сверка материалов" in res.stdout
    report = json.loads((out / "materials.json").read_text(encoding="utf-8"))
    assert report["totals"]["discrepancies"] >= 3
    assert (out / "materials.xlsx").exists() and (out / "materials.md").exists()


def test_cli_materials_without_decision_items_fails(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    res = subprocess.run([sys.executable, "-m", "pto_agent", "materials", "--out",
                          str(tmp_path / "out"), "reconcile", "--decision", str(path)],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 1 and "сверять нечего" in res.stderr


def test_mcp_materials_reconcile(tmp_path):
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx
    ctx = make_ctx(str(ROOT))
    path = _decision_file(tmp_path)
    res = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "materials_reconcile",
                                 "arguments": {"decision_path": str(path),
                                               "receipts_paths": [str(RECEIPTS)],
                                               "act_paths": [str(ACT)],
                                               "out_dir": str(tmp_path / "out")}}}, ctx)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["report"]["status"] == "partial"
    assert (tmp_path / "out" / "materials.xlsx").exists()

    res = handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "materials_reconcile",
                                 "arguments": {"decision_path": str(path),
                                               "format": "markdown",
                                               "out_dir": str(tmp_path / "out2")}}}, ctx)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["format"] == "markdown" and "Сверка материалов" in payload["markdown"]


# ─────────────────────────────── сводка проверки ───────────────────────────────
def test_review_marks_materials_skipped_when_no_report():
    from pto_agent.review import build_review
    review = build_review()
    check = next(c for c in review["checked"] if c["id"] == "materials")
    assert check["status"] == "skipped"
    assert "не сверялись" in check["detail"]


def test_review_adds_material_money_and_questions():
    from pto_agent.review import build_review
    report = reconcile([order(CABLE, 0.40, 1000)], [receipt(CABLE, 0.30, 1000)],
                       [act(CABLE, 0.35, 1000)])
    review = build_review(materials=report)
    check = next(c for c in review["checked"] if c["id"] == "materials")
    assert check["status"] == "warning"
    assert check["money"]["blocked_rub"] == pytest.approx(50.0)
    assert review["money"]["at_risk_rub"] == pytest.approx(60.0)  # риск по акту
    assert review["money"]["blocked_rub"] == pytest.approx(50.0)  # заморожено
    assert any(d["priority"] == "important" and "материал" in d["question"].lower()
               or "акте" in d["question"] for d in review["decisions"])


def test_review_flags_unknown_act_materials_as_open_item():
    from pto_agent.review import build_review
    report = reconcile([order(CABLE, 1.0, 1000)], [receipt(CABLE, 1.0, 1000)])
    review = build_review(materials=report)
    assert any(item["kind"] == "materials_in_act_unknown" for item in review["open_items"])


def test_write_report_roundtrip(tmp_path):
    report = reconcile([order(CABLE, 1.0, 1000)], [])
    path = write(tmp_path / "m.json", report)
    assert json.loads(path.read_text(encoding="utf-8"))["schema"].startswith("pto-agent/materials")
