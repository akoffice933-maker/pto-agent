# -*- coding: utf-8 -*-
"""Ретро-выпуск по ранее подписанным актам: КС-2 №35 и №37.

Задача шага: 4,4 млн ₽, подписанные без проверки, перестают быть «не оценено».
Проверяется, что для каждого акта своя сводка ПТО (сверка идёт с количествами
этого акта, а не с текущим журналом), что направление спора сохранено и что
портфель от этого закрывает пробел в покрытии.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.portfolio import assess_portfolio, load_acts                # noqa: E402

RETRO = ROOT / "data" / "demo" / "retro"
ACTS = ROOT / "data" / "demo" / "acts" / "acts.json"
SUMMARY = ROOT / "out" / "summary.json"


def _summary(act: str) -> dict:
    path = ROOT / "out" / "acts" / f"summary_{act}.json"
    if not path.exists():
        pytest.skip(f"нет сводки по акту {act}")
    return json.loads(path.read_text(encoding="utf-8"))


def _batch(act: str) -> dict:
    path = ROOT / "out" / "cad" / f"batch_{act}" / "batch_summary.json"
    if not path.exists():
        pytest.skip(f"нет выпуска по акту {act}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pto():
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


# ── сводка ПТО периода акта ─────────────────────────────────────────────
def test_act_summary_matches_the_act_not_the_journal():
    """В сводке акта №35 количества — те, что приняты актом (200 м3, 600 м2)."""
    s35 = _summary("ks35")
    assert s35["act"] == "КС-2 №35"
    assert s35["amount_act"] == pytest.approx(4_168_000)
    qty = {line["code"]: line["qty_journal"] for line in s35["lines"]}
    assert qty["ФЕР 06-01-001-01"] == 200          # в октябрьском журнале 1240
    assert qty["ФЕР 08-02-001-01"] == 600
    assert s35["discrepancies"] == []              # сверка идёт по пометкам, не по ПТО


def test_act37_summary():
    s37 = _summary("ks37")
    assert s37["amount_act"] == pytest.approx(248_000)
    assert s37["lines"][0]["qty_journal"] == 200   # трубопровод, 200 м


# ── что нашёл ретро-выпуск ──────────────────────────────────────────────
def test_release_35_found_markup_on_two_positions():
    batch = _batch("ks35")
    assert len(batch["conflicts"]) == 10           # 10 листов, по одной пометке
    impact = batch["impact"]["totals"]
    assert impact["at_risk_rub"] == pytest.approx(216_200)
    assert impact["markup_rub"] == pytest.approx(216_200)
    assert impact["underclaim_rub"] == 0
    codes = {c["code"] for c in batch["conflicts"]}
    assert codes == {"ФЕР 06-01-001-01", "ФЕР 08-02-001-01"}
    # 10 м3 × 18 500 + 40 м2 × 780
    assert impact["at_risk_rub"] == pytest.approx(10 * 18_500 + 40 * 780)


def test_release_37_found_underclaim():
    batch = _batch("ks37")
    assert len(batch["conflicts"]) == 5
    impact = batch["impact"]["totals"]
    assert impact["at_risk_rub"] == pytest.approx(24_800)      # 20 м × 1 240
    assert impact["underclaim_rub"] == pytest.approx(24_800)
    assert impact["markup_rub"] == 0                            # занижение, не завышение
    conflict = batch["conflicts"][0]
    assert conflict["delta_qty"] == pytest.approx(-20)          # знак сохранён


def test_retro_conflicts_have_numbers_and_formula():
    for act in ("ks35", "ks37"):
        for conflict in _batch(act)["conflicts"]:
            assert conflict["mark_qty"] and conflict["act_qty"]
            assert conflict["unit"]
    formula = _batch("ks37")["impact"]["positions"][0]["risks"][0]["formula"]
    assert "200" in formula and "180" in formula and "1 240" in formula.replace("\u00a0", " ")


# ── реестр и портфель ───────────────────────────────────────────────────
def test_acts_registry_marks_retro_acts_as_checked():
    acts = {a["act_id"]: a for a in json.loads(ACTS.read_text(encoding="utf-8"))["acts"]}
    for act_id, release in (("КС-2 №35", "batch_ks35"), ("КС-2 №37", "batch_ks37"),
                            ("КС-2 №38", "batch")):
        assert acts[act_id]["status"] == "проверен"
        assert release in acts[act_id]["release"]
    assert acts["КС-2 №35"]["amount_rub"] == pytest.approx(4_168_000)


def test_portfolio_coverage_is_complete_and_still_honest(pto):
    pf = assess_portfolio(load_acts(ACTS), pto, root=ROOT)
    t = pf["totals"]
    assert t["acts_checked"] == t["acts_total"] == 3
    assert t["coverage_pct"] == pytest.approx(100.0)
    assert t["amount_unchecked_rub"] == 0
    assert t["at_risk_rub"] == pytest.approx(2_091_000)        # 216 200 + 24 800 + 1 850 000
    by_id = {a["act_id"]: a for a in pf["acts"]}
    assert by_id["КС-2 №35"]["at_risk_rub"] == pytest.approx(216_200)
    assert by_id["КС-2 №37"]["at_risk_rub"] == pytest.approx(24_800)
    assert by_id["КС-2 №38"]["at_risk_rub"] == pytest.approx(1_850_000)


def test_retro_positions_are_separate_rows(pto):
    pf = assess_portfolio(load_acts(ACTS), pto, root=ROOT)
    positions = {p["code"]: p for p in pf["positions"]}
    assert positions["ФЕР 08-02-001-01"]["money_release_rub"] == pytest.approx(31_200)
    assert positions["ФЕР 08-02-001-01"]["acts"] == ["КС-2 №35"]
    # по этой позиции есть и ретро-спор (№37), и фон ПТО (№38) — они не смешиваются
    assert positions["ФЕР 16-02-003-01"]["money_release_rub"] == pytest.approx(24_800)
    assert positions["ФЕР 16-02-003-01"]["money_background_rub"] == pytest.approx(49_600)
    assert positions["ФЕР 16-02-003-01"]["acts"] == ["КС-2 №37", "КС-2 №38"]


def test_months_now_both_assessed(pto):
    pf = assess_portfolio(load_acts(ACTS), pto, root=ROOT)
    months = {m["month"]: m for m in pf["months"]}
    assert months["2026-09"]["risk_assessed"] is True
    assert months["2026-09"]["at_risk_rub"] == pytest.approx(241_000)
    assert months["2026-09"]["share_pct"] == pytest.approx(5.46, abs=0.01)
