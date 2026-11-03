# -*- coding: utf-8 -*-
"""Тесты стоимости споров: пометки выпуска и расхождения ПТО → рубли.

Главное, что проверяется: каждая сумма получается по явной формуле из цены сметы
и объёма; спор считается один раз на позицию, а не по числу итераций листов;
где данных нет — суммы нет, есть причина; выпуск и «фон ПТО» не смешиваются.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.impact import (DOCS_RISK, MARKUP_RISK, OVERRUN_RISK,         # noqa: E402
                              PRICE_UNKNOWN, SIGNATURE_RISK, UNDERCLAIM_RISK,
                              assess_impact, conflict_money, discrepancy_money,
                              impact_chart, impact_to_markdown, write_impact_xlsx)

SUMMARY = ROOT / "out" / "summary.json"
BATCH = ROOT / "out" / "cad" / "batch" / "batch_summary.json"


@pytest.fixture(scope="module")
def pto():
    if not SUMMARY.exists():
        pytest.skip("нет сводки ПТО: python3 -m pto_agent run --data data/demo --out out")
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


def _conflict(ctype="CONFLICT_QUANTITY", code="ФЕР 06-01-001-01", **kw):
    base = {"type": ctype, "code": code, "sheet_id": "sheet-1", "drawing": "sheet_01.dxf",
            "mark_id": "m1"}
    base.update(kw)
    return base


# ── расхождения ПТО ──────────────────────────────────────────────────────
def test_volume_over_estimate_money(pto):
    d = next(x for x in pto["discrepancies"]
             if x["type"] == "VOLUME_OVER_ESTIMATE" and x["code"] == "ФЕР 06-01-001-01")
    money = discrepancy_money(d, __import__("pto_agent.impact", fromlist=["PriceBook"])
                              .PriceBook.from_summary(pto))
    assert money["money_rub"] == pytest.approx(740_000)      # 40 м3 × 18 500 ₽
    assert money["risk"] == SIGNATURE_RISK
    # русский формат: неразрывный пробел между тысячами
    assert "40 м³ × 18\u00a0500" in money["formula"]


def test_already_closed_money(pto):
    from pto_agent.impact import PriceBook
    d = next(x for x in pto["discrepancies"] if x["type"] == "ALREADY_CLOSED")
    money = discrepancy_money(d, PriceBook.from_summary(pto))
    assert money["money_rub"] == pytest.approx(49_600)       # 40 м × 1 240 ₽
    assert money["risk"] == OVERRUN_RISK


def test_missing_docs_money(pto):
    from pto_agent.impact import PriceBook
    d = next(x for x in pto["discrepancies"] if x["type"] == "MISSING_BACKING_DOCS")
    money = discrepancy_money(d, PriceBook.from_summary(pto))
    assert money["money_rub"] == pytest.approx(8_346_000)
    assert money["risk"] == DOCS_RISK


def test_unit_mismatch_is_not_priced(pto):
    from pto_agent.impact import PriceBook
    d = next(x for x in pto["discrepancies"] if x["type"] == "UNIT_MISMATCH")
    money = discrepancy_money(d, PriceBook.from_summary(pto))
    assert money["money_rub"] is None
    assert money["risk"] == PRICE_UNKNOWN
    assert "пересчёт единиц" in money["note"]


def test_not_in_estimate_is_not_priced(pto):
    from pto_agent.impact import PriceBook
    d = next(x for x in pto["discrepancies"] if x["type"] == "NOT_IN_ESTIMATE")
    money = discrepancy_money(d, PriceBook.from_summary(pto))
    assert money["money_rub"] is None
    assert "дополнительному соглашению" in money["note"]


def test_duplicate_costs_nothing(pto):
    from pto_agent.impact import PriceBook
    d = next(x for x in pto["discrepancies"] if x["type"] == "DUPLICATE")
    money = discrepancy_money(d, PriceBook.from_summary(pto))
    assert money["money_rub"] == 0.0


# ── конфликты пометок ───────────────────────────────────────────────────
def test_quantity_markup_money(pto):
    from pto_agent.impact import PriceBook
    c = _conflict(mark_qty=1300, act_qty=1240, unit="м3")
    money = conflict_money(c, PriceBook.from_summary(pto))
    assert money["money_rub"] == pytest.approx(1_110_000)     # 60 м3 × 18 500 ₽
    assert money["risk"] == MARKUP_RISK
    assert money["delta_qty"] == pytest.approx(60)


def test_quantity_underclaim_money(pto):
    from pto_agent.impact import PriceBook
    c = _conflict(mark_qty=1200, act_qty=1240, unit="м3")
    money = conflict_money(c, PriceBook.from_summary(pto))
    assert money["money_rub"] == pytest.approx(740_000)
    assert money["risk"] == UNDERCLAIM_RISK


def test_quantity_from_text_values(pto):
    """Если чисел нет, объём вытаскивается из подписи пометки."""
    from pto_agent.impact import PriceBook
    c = _conflict(mark_value="1300 м3", act_value="1240 м3")
    money = conflict_money(c, PriceBook.from_summary(pto))
    assert money["money_rub"] == pytest.approx(1_110_000)


def test_approval_conflict_prices_critical_discrepancies(pto):
    from pto_agent.impact import PriceBook
    c = _conflict("CONFLICT_APPROVAL", mark_text="СОГЛАСОВАНО")
    money = conflict_money(c, PriceBook.from_summary(pto))
    assert money["money_rub"] == pytest.approx(740_000)
    assert money["risk"] == SIGNATURE_RISK
    assert "VOLUME_OVER_ESTIMATE" in money["formula"]


def test_unknown_code_is_not_priced():
    c = _conflict(code="ФЕР 99-99-999-99", mark_qty=100, act_qty=90)
    money = conflict_money(c, __import__("pto_agent.impact", fromlist=["PriceBook"])
                           .PriceBook.from_summary(None))
    assert money["money_rub"] is None and money["risk"] == PRICE_UNKNOWN


# ── сводка по выпуску ───────────────────────────────────────────────────
def test_impact_counts_dispute_once_across_iterations(pto):
    """Одна и та же пометка на 10 итерациях листа — один спор, а не десять."""
    conflicts = [_conflict(sheet_id=f"К2-АР-01-v{i:02d}", drawing="sheet_01.dxf",
                           mark_qty=1300, act_qty=1240, unit="м3") for i in range(1, 11)]
    impact = assess_impact(conflicts, pto)
    assert impact["totals"]["at_risk_rub"] == pytest.approx(1_110_000)
    assert impact["totals"]["occurrences_total"] == 10
    assert impact["totals"]["sheets_total"] == 10
    pos = impact["positions"][0]
    assert pos["money_rub"] == pytest.approx(1_110_000)
    assert pos["risks"][0]["occurrences"] == 10


def test_impact_counts_dispute_once_across_drawings(pto):
    """Один спор на разных листах комплекта — тоже один раз."""
    conflicts = [_conflict(sheet_id="a", drawing=d, mark_qty=1300, act_qty=1240, unit="м3")
                 for d in ("sheet_01.dxf", "sheet_02.dxf")]
    impact = assess_impact(conflicts, pto)
    assert impact["totals"]["at_risk_rub"] == pytest.approx(1_110_000)


def test_impact_splits_release_and_background(pto):
    impact = assess_impact([_conflict(mark_qty=1300, act_qty=1240, unit="м3")], pto,
                           discrepancies=pto["discrepancies"])
    t = impact["totals"]
    assert t["at_risk_rub"] == pytest.approx(1_110_000)          # только пометки
    assert t["background_risk_rub"] > 8_000_000                  # фон ПТО отдельно
    release_positions = [p for p in impact["positions"] if p["money_release_rub"]]
    assert len(release_positions) == 1
    assert release_positions[0]["money_release_rub"] == pytest.approx(1_110_000)


def test_impact_share_of_act(pto):
    impact = assess_impact([_conflict(mark_qty=1300, act_qty=1240, unit="м3")], pto)
    assert impact["totals"]["share_of_act_pct"] == pytest.approx(3.25, abs=0.01)
    assert impact["totals"]["act_amount_rub"] == pytest.approx(34_106_600)


def test_impact_unpriced_is_grouped(pto):
    conflicts = [_conflict(code="ФЕР 99-99-999-99", mark_qty=5, act_qty=3) for _ in range(4)]
    impact = assess_impact(conflicts, pto)
    assert impact["totals"]["at_risk_rub"] == 0
    assert len(impact["unpriced"]) == 1
    assert impact["unpriced"][0]["occurrences"] == 4
    assert impact["totals"]["items_unpriced_raw"] == 4


def test_reports_render(pto, tmp_path):
    impact = assess_impact([_conflict(mark_qty=1300, act_qty=1240, unit="м3")], pto,
                           discrepancies=pto["discrepancies"])
    md = impact_to_markdown(impact)
    assert "Под риском по пометкам выпуска" in md
    assert "Не оценено в деньгах" in md

    from openpyxl import load_workbook
    xlsx = write_impact_xlsx(impact, tmp_path / "impact.xlsx")
    wb = load_workbook(xlsx)
    assert {"Деньги — итог", "Позиции под риском", "Расчёт по строкам",
            "Не оценено в деньгах", "Методика"} == set(wb.sheetnames)

    chart = impact_chart(impact, tmp_path / "impact.png")
    assert chart.exists() and chart.stat().st_size > 5000


# ── CLI и MCP ───────────────────────────────────────────────────────────
@pytest.mark.skipif(not BATCH.exists(), reason="нет сводки выпуска")
def test_demo_batch_speaks_in_money():
    batch = json.loads(BATCH.read_text(encoding="utf-8"))
    impact = batch.get("impact")
    assert impact, "в сводке выпуска должен быть раздел денег"
    assert impact["totals"]["at_risk_rub"] == pytest.approx(1_850_000)
    assert impact["totals"]["markup_rub"] == pytest.approx(1_110_000)
    assert impact["positions"][0]["code"] == "ФЕР 06-01-001-01"


@pytest.mark.skipif(not BATCH.exists(), reason="нет сводки выпуска")
def test_cli_impact(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "cad", "impact",
                          "--batch", str(BATCH), "--summary", str(SUMMARY),
                          "--out", str(tmp_path), "--with-background"],
                         cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "Под риском по пометкам выпуска" in res.stdout
    assert (tmp_path / "impact.xlsx").exists()
    assert (tmp_path / "impact.json").exists()


def test_mcp_assess_release_impact(tmp_path):
    from pto_agent.cad.mcp_server import TOOLS, handle_rpc, make_ctx
    assert len(TOOLS) == 14
    assert "assess_release_impact" in {t["name"] for t in TOOLS}
    if not BATCH.exists():
        pytest.skip("нет сводки выпуска")
    ctx = make_ctx(ROOT, ROOT / "out" / "cad" / "audit.jsonl")
    req = {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
           "params": {"name": "assess_release_impact", "arguments": {
               "batch_path": str(BATCH), "summary_path": str(SUMMARY),
               "out_dir": str(tmp_path)}}}
    res = handle_rpc(req, ctx)
    impact = json.loads(res["result"]["content"][0]["text"])
    assert impact["totals"]["at_risk_rub"] == pytest.approx(1_850_000)
    assert impact["reports"]["xlsx"].endswith("impact.xlsx")
