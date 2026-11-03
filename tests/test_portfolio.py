# -*- coding: utf-8 -*-
"""Портфель актов: деньги под риском по всем актам объекта и по месяцам.

Проверяется главное свойство: непроверенный акт — это «не оценено», а не ноль;
сумма по объекту = сумма по актам; одна позиция в двух актах — два спора,
а повторы внутри акта — один.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.portfolio import (assess_portfolio, check_act_vs_estimate,  # noqa: E402
                                 load_acts, month_label, portfolio_to_markdown,
                                 write_portfolio_reports)

SUMMARY = ROOT / "out" / "summary.json"
ACTS = ROOT / "data" / "demo" / "acts" / "acts.json"
RELEASES = [ROOT / "out" / "cad" / "batch_ks35" / "batch_summary.json",
            ROOT / "out" / "cad" / "batch_ks37" / "batch_summary.json",
            ROOT / "out" / "cad" / "batch" / "batch_summary.json"]
needs_releases = pytest.mark.skipif(
    not all(p.exists() for p in RELEASES),
    reason="нет собранных выпусков актов: make_demo_retro.py + cad scan-batch")


@pytest.fixture(scope="module")
def pto():
    if not SUMMARY.exists():
        pytest.skip("нет сводки ПТО")
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


def _conflict(**kw):
    base = {"type": "CONFLICT_QUANTITY", "code": "ФЕР 06-01-001-01",
            "sheet_id": "sheet-1", "drawing": "sheet_01.dxf", "mark_id": "m1",
            "mark_qty": 1300, "act_qty": 1240, "unit": "м3"}
    base.update(kw)
    return base


def _acts(*items):
    return {"object": "Тестовый объект", "contract": "Д-1", "acts": list(items)}


# ── реестр и месяцы ─────────────────────────────────────────────────────
def test_month_label():
    assert month_label("2026-09") == "сентябрь 2026"
    assert month_label("2026-12") == "декабрь 2026"


def test_load_acts_rejects_empty(tmp_path):
    path = tmp_path / "acts.json"
    path.write_text(json.dumps({"acts": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_acts(path)


# ── покрытие проверкой ──────────────────────────────────────────────────
def test_unchecked_act_is_not_zero(pto):
    acts = _acts(
        {"act_id": "КС-2 №35", "amount_rub": 4_000_000, "month": "2026-09",
         "status": "не проверялся", "note": "сканов нет"},
        {"act_id": "КС-2 №38", "amount_rub": 10_000_000, "month": "2026-10",
         "conflicts": [_conflict()]},
    )
    pf = assess_portfolio(acts, pto)
    t = pf["totals"]
    assert t["acts_total"] == 2 and t["acts_checked"] == 1
    assert t["amount_rub"] == pytest.approx(14_000_000)
    assert t["amount_checked_rub"] == pytest.approx(10_000_000)
    assert t["amount_unchecked_rub"] == pytest.approx(4_000_000)
    assert t["coverage_pct"] == pytest.approx(71.43, abs=0.01)
    unchecked = next(a for a in pf["acts"] if a["act_id"] == "КС-2 №35")
    assert unchecked["status"] == "не проверялся"
    assert unchecked["at_risk_rub"] == 0
    assert unchecked["share_pct"] is None       # не «0 %», а «не считалось»
    assert "сканов нет" in unchecked["note"]


def test_totals_equal_sum_of_acts(pto):
    acts = _acts(
        {"act_id": "A", "amount_rub": 5_000_000, "month": "2026-09",
         "conflicts": [_conflict(mark_qty=1300, act_qty=1240)]},
        {"act_id": "B", "amount_rub": 3_000_000, "month": "2026-10",
         "conflicts": [_conflict(mark_qty=1200, act_qty=1240)]},
    )
    pf = assess_portfolio(acts, pto)
    assert pf["totals"]["at_risk_rub"] == pytest.approx(
        sum(a["at_risk_rub"] for a in pf["acts"]))
    assert pf["totals"]["amount_rub"] == pytest.approx(
        sum(a["amount_rub"] for a in pf["acts"]))
    assert pf["totals"]["markup_rub"] == pytest.approx(1_110_000)
    assert pf["totals"]["underclaim_rub"] == pytest.approx(740_000)


def test_month_without_checked_acts_is_not_zero(pto):
    acts = _acts(
        {"act_id": "A", "amount_rub": 2_000_000, "month": "2026-08"},
        {"act_id": "B", "amount_rub": 8_000_000, "month": "2026-10",
         "conflicts": [_conflict()]},
    )
    pf = assess_portfolio(acts, pto)
    august, october = pf["months"]
    assert august["risk_assessed"] is False
    assert august["share_pct"] is None
    assert october["risk_assessed"] is True
    assert october["at_risk_rub"] == pytest.approx(1_110_000)
    assert october["share_pct"] == pytest.approx(13.88, abs=0.01)


def test_same_month_acts_are_summed(pto):
    acts = _acts(
        {"act_id": "A", "amount_rub": 4_000_000, "month": "2026-09"},
        {"act_id": "B", "amount_rub": 1_000_000, "month": "2026-09"},
    )
    pf = assess_portfolio(acts, pto)
    assert len(pf["months"]) == 1
    assert pf["months"][0]["amount_rub"] == pytest.approx(5_000_000)
    assert pf["months"][0]["acts"] == ["A", "B"]


# ── позиции и дедупликация ──────────────────────────────────────────────
def test_position_in_two_acts_is_two_disputes(pto):
    acts = _acts(
        {"act_id": "A", "amount_rub": 5_000_000, "month": "2026-09",
         "conflicts": [_conflict()]},
        {"act_id": "B", "amount_rub": 5_000_000, "month": "2026-10",
         "conflicts": [_conflict()]},
    )
    pf = assess_portfolio(acts, pto)
    pos = pf["positions"][0]
    assert pos["money_release_rub"] == pytest.approx(2_220_000)   # 2 × 1 110 000
    assert pos["acts"] == ["A", "B"]
    assert len(pos["risks"]) == 2                                  # по одному на акт
    assert {r["act_id"] for r in pos["risks"]} == {"A", "B"}


def test_iterations_inside_act_counted_once(pto):
    conflicts = [_conflict(sheet_id=f"v{i}", mark_id=f"m{i}") for i in range(10)]
    pf = assess_portfolio(_acts({"act_id": "A", "amount_rub": 10_000_000,
                                 "month": "2026-10", "conflicts": conflicts}), pto)
    assert pf["totals"]["at_risk_rub"] == pytest.approx(1_110_000)
    assert pf["acts"][0]["occurrences_total"] == 10
    assert pf["acts"][0]["sheets_total"] == 10


def test_background_goes_only_to_marked_act(pto):
    acts = _acts(
        {"act_id": "A", "amount_rub": 5_000_000, "month": "2026-09",
         "conflicts": [_conflict()]},
        {"act_id": "B", "amount_rub": 5_000_000, "month": "2026-10",
         "conflicts": [_conflict()], "background": True},
    )
    pf = assess_portfolio(acts, pto)
    by_id = {a["act_id"]: a for a in pf["acts"]}
    assert by_id["A"]["background_risk_rub"] == 0
    assert by_id["B"]["background_risk_rub"] > 8_000_000
    assert pf["totals"]["at_risk_rub"] == pytest.approx(2_220_000)      # фон не входит


# ── сверка акта со сметой ───────────────────────────────────────────────
def test_act_over_estimate_is_money():
    from pto_agent.impact import PriceBook
    book = PriceBook.from_summary({"lines": [
        {"code": "ФЕР 06-01-001-01", "unit": "м3", "price": 18500, "estimate_qty": 1200}]})
    check = check_act_vs_estimate(
        {"positions": [{"code": "ФЕР 06-01-001-01", "qty": 1300, "unit": "м3"}]}, book)
    assert check["overrun_rub"] == pytest.approx(1_850_000)     # 100 м3 × 18 500
    assert "больше сметного объёма" in check["rows"][0]["note"]


def test_act_overrun_counts_in_portfolio(pto):
    acts = _acts(
        {"act_id": "A", "amount_rub": 5_000_000, "month": "2026-09",
         "positions": [{"code": "ФЕР 06-01-001-01", "qty": 1300, "unit": "м3",
                        "price": 18500}]},
        {"act_id": "B", "amount_rub": 5_000_000, "month": "2026-10",
         "positions": [{"code": "ФЕР 06-01-001-01", "qty": 200, "unit": "м3",
                        "price": 18500}]},
    )
    pf = assess_portfolio(acts, pto)
    assert pf["totals"]["estimate_overrun_rub"] == pytest.approx(1_850_000)
    assert pf["acts"][0]["estimate_overrun_rub"] == pytest.approx(1_850_000)
    assert pf["acts"][1]["estimate_overrun_rub"] == 0


# ── отчёты, CLI, MCP ────────────────────────────────────────────────────
def test_reports_render(pto, tmp_path):
    acts = _acts(
        {"act_id": "A", "amount_rub": 5_000_000, "month": "2026-09", "note": "нет сканов"},
        {"act_id": "B", "amount_rub": 5_000_000, "month": "2026-10",
         "conflicts": [_conflict()]},
    )
    pf = assess_portfolio(acts, pto)
    md = portfolio_to_markdown(pf)
    assert "Деньги под риском по актам объекта" in md
    assert "не проверялся" in md and "| Месяц |" in md

    from openpyxl import load_workbook
    paths = write_portfolio_reports(pf, tmp_path)
    wb = load_workbook(paths["xlsx"])
    assert {"Портфель — итог", "Акты", "Месяцы", "Позиции по актам",
            "Расчёт по актам", "Не проверено", "Не оценено", "Методика"} <= set(wb.sheetnames)
    assert paths["chart"].exists() and paths["chart"].stat().st_size > 3000
    assert json.loads(paths["json"].read_text(encoding="utf-8"))["totals"]["acts_total"] == 2


@needs_releases
def test_demo_portfolio_is_coherent():
    """Демо-объект: три акта, все проверены (после ретро-выпуска по №35 и №37)."""
    pto = json.loads(SUMMARY.read_text(encoding="utf-8"))
    pf = assess_portfolio(load_acts(ACTS), pto, root=ROOT)
    t = pf["totals"]
    assert t["acts_total"] == 3
    assert t["acts_checked"] == 3
    assert t["acts_unchecked"] == 0
    assert t["amount_rub"] == pytest.approx(38_522_600)
    assert t["coverage_pct"] == pytest.approx(100.0)
    assert t["at_risk_rub"] == pytest.approx(2_091_000)
    assert t["at_risk_rub"] == pytest.approx(sum(a["at_risk_rub"] for a in pf["acts"]))
    assert t["markup_rub"] == pytest.approx(1_326_200)
    assert t["underclaim_rub"] == pytest.approx(24_800)       # акт №37: «180 м» против 200 м
    months = {m["month"]: m for m in pf["months"]}
    assert months["2026-09"]["risk_assessed"] is True
    assert months["2026-09"]["at_risk_rub"] == pytest.approx(241_000)
    assert months["2026-10"]["at_risk_rub"] == pytest.approx(1_850_000)


@needs_releases
def test_cli_portfolio(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "cad", "impact",
                          "--acts", str(ACTS), "--summary", str(SUMMARY),
                          "--out", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "покрытие 100,0 %" in res.stdout
    assert (tmp_path / "portfolio.xlsx").exists()
    data = json.loads((tmp_path / "portfolio.json").read_text(encoding="utf-8"))
    assert data["totals"]["at_risk_rub"] == pytest.approx(2_091_000)


@needs_releases
def test_mcp_portfolio(tmp_path):
    from pto_agent.cad.mcp_server import TOOLS, handle_rpc, make_ctx
    assert len(TOOLS) == 14                                  # портфель — тот же инструмент
    ctx = make_ctx(ROOT, ROOT / "out" / "cad" / "audit.jsonl")
    req = {"jsonrpc": "2.0", "id": 31, "method": "tools/call",
           "params": {"name": "assess_release_impact", "arguments": {
               "acts_path": str(ACTS), "summary_path": str(SUMMARY),
               "out_dir": str(tmp_path)}}}
    pf = json.loads(handle_rpc(req, ctx)["result"]["content"][0]["text"])
    assert pf["totals"]["coverage_pct"] == pytest.approx(100.0)
    assert pf["reports"]["xlsx"].endswith("portfolio.xlsx")
