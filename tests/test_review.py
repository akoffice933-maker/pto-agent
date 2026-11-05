# -*- coding: utf-8 -*-
"""Сводка проверки (`review`): что проверено, что осталось, где нужно решение человека.

Тесты держат два принципа, на которых держится доверие к сводке:
  * отсутствующий артефакт — «не выполнялось», а не «ок»;
  * «не оценено» не превращается в ноль, и это видно в деньгах и в открытых пунктах.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.review import (PRIORITY_BLOCKING, PRIORITY_IMPORTANT, STATUS_FAIL, STATUS_OK,
                             STATUS_SKIPPED, STATUS_WARNING, VERDICT_BLOCKED, VERDICT_NO_DATA,
                             VERDICT_READY, VERDICT_REVIEW, build_review, collect, load_json,
                             review_to_markdown, write_review)

SUMMARY = ROOT / "out" / "summary.json"


# ─────────────────────────── конструкторы примеров ───────────────────────
def summary_ok():
    return {"object": "ЖК «Северный»", "amount_act": 1000.0, "amount_act_safe": 1000.0,
            "amount_blocked": 0.0, "by_severity": {},
            "lines": [{"code": "A1", "name": "Работа", "unit": "м3", "price": 100.0,
                       "qty_journal": 10.0, "qty_claim": 10.0}],
            "discrepancies": []}


def summary_with_critical():
    s = summary_ok()
    s["discrepancies"] = [{"type": "ALREADY_CLOSED", "severity": "critical", "code": "A1",
                           "name": "Работа", "message": "объём уже закрыт актом КС-2 №35",
                           "suggested_action": "снять строку из акта"}]
    s["by_severity"] = {"critical": 1}
    s["amount_blocked"] = 100.0
    return s


def batch_ok():
    return {"totals": {"sheets": 4, "ok": 4, "failed": 0, "marks": 5, "conflicts": 0,
                       "decisions": 0, "needs_human": 0, "unconfirmed_calibration": 0,
                       "sheets_from_cache": 0},
            "decisions": [], "failed_sheets": []}


def batch_with_problems():
    return {"totals": {"sheets": 4, "ok": 3, "failed": 1, "marks": 5, "conflicts": 2,
                       "decisions": 2, "needs_human": 2, "unconfirmed_calibration": 1,
                       "sheets_from_cache": 2},
            "failed_sheets": [{"sheet_id": "К2-АР-04", "error": "не читается скан"}],
            "decisions": [{"sheet_id": "К2-АР-02", "mark_id": "m3", "text": "отмостку исключить",
                           "reason": "нет текста пометки", "action": "уточнить у автора"}]}


def impact_with_risk():
    return {"totals": {"at_risk_rub": 1500.0, "background_risk_rub": 500.0,
                       "act_amount_rub": 20000.0, "items_unpriced": 2},
            "unpriced": [{}, {}]}


def acts_all_checked():
    return {"object": "ЖК «Северный»",
            "acts": [{"act_id": "КС-2 №35", "status": "проверен"},
                     {"act_id": "КС-2 №37", "status": "проверен"}],
            "totals": {"coverage_pct": 100.0, "amount_rub": 4000000.0}}


def acts_with_unchecked():
    a = acts_all_checked()
    a["acts"].append({"act_id": "КС-2 №38", "status": "не проверен"})
    a["totals"]["coverage_pct"] = 66.7
    return a


# ───────────────────────────── вердикты ─────────────────────────────────
def test_no_artifacts_is_no_data_not_ok():
    review = build_review()
    assert review["verdict"]["status"] == VERDICT_NO_DATA
    assert review["verdict"]["can_sign"] is False
    assert all(c["status"] == STATUS_SKIPPED for c in review["checked"])
    assert "проверять нечего" in review["verdict"]["headline"].lower()


def zero_impact():
    return {"totals": {"at_risk_rub": 0.0, "background_risk_rub": 0.0,
                       "act_amount_rub": 1000.0, "items_unpriced": 0}, "unpriced": []}


def plan_clean():
    return {"summary": {"operations": 2}, "validation": {"blocking": 0, "issues": []}}


def test_clean_artifacts_are_ready():
    review = build_review(summary=summary_ok(), batch=batch_ok(), acts=acts_all_checked(),
                          impact=zero_impact(), plan=plan_clean())
    assert not [i for i in review["open_items"] if i["kind"] == "not_estimated"]
    assert review["verdict"]["status"] == VERDICT_READY
    assert review["verdict"]["can_sign"] is True
    assert review["decisions"] == []


def test_critical_discrepancy_blocks_signature():
    review = build_review(summary=summary_with_critical())
    assert review["verdict"]["status"] == VERDICT_BLOCKED
    assert review["verdict"]["can_sign"] is False
    decision = review["decisions"][0]
    assert decision["priority"] == PRIORITY_BLOCKING
    assert "ALREADY_CLOSED" in decision["question"]
    assert decision["action"] == "снять строку из акта"


def test_failed_sheet_blocks_release():
    review = build_review(batch=batch_with_problems())
    assert review["verdict"]["status"] == VERDICT_BLOCKED
    assert any("К2-АР-04" in d["question"] for d in review["decisions"])


def test_warnings_give_needs_review_but_allow_signature():
    review = build_review(summary=summary_ok(), batch=batch_ok(), acts=acts_with_unchecked())
    assert review["verdict"]["status"] == VERDICT_REVIEW
    assert review["verdict"]["can_sign"] is True          # подпись возможна, но с оговорками
    assert review["verdict"]["important"] == 1


# ─────────────────────── что проверено / что осталось ────────────────────
def test_missing_artifact_is_skipped_with_reason():
    review = build_review(summary=summary_ok())
    checks = {c["id"]: c for c in review["checked"]}
    assert checks["release"]["status"] == STATUS_SKIPPED
    assert "сканы не обрабатывались" in checks["release"]["detail"]
    assert checks["ks2"]["status"] == STATUS_OK
    assert any("дособрать артефакты" in s for s in review["next_steps"])


def test_unpriced_positions_are_open_items_not_zero():
    review = build_review(summary=summary_ok(), impact=impact_with_risk())
    money = next(c for c in review["checked"] if c["id"] == "money")
    assert money["money"]["at_risk_rub"] == 2000.0           # 1500 марок + 500 фон
    assert money["money"]["unpriced_positions"] == 2
    assert money["money"]["share_pct"] == 10.0
    items = [i for i in review["open_items"] if i["kind"] == "not_estimated"]
    assert items and "«не оценено» ≠ 0 ₽" in items[0]["detail"]


def test_money_totals_are_aggregated():
    review = build_review(summary=summary_with_critical(), impact=impact_with_risk())
    assert review["money"]["blocked_rub"] == 100.0           # из сводки КС-2
    assert review["money"]["at_risk_rub"] == 2000.0          # из расчёта денег
    assert review["money"]["claim_rub"] == 1000.0


def test_cad_plan_blocking_violations():
    plan = {"summary": {"operations": 4},
            "validation": {"blocking": 1, "issues": [
                {"type": "STAMP_FIELD_EMPTY", "severity": "blocking",
                 "message": "в штампе не заполнено поле «Стадия»"}]}}
    review = build_review(plan=plan)
    assert review["verdict"]["status"] == VERDICT_BLOCKED
    assert review["decisions"][0]["priority"] == PRIORITY_BLOCKING
    assert "Стадия" in review["decisions"][0]["question"]


def test_decisions_sorted_blocking_first():
    review = build_review(summary=summary_with_critical(), batch=batch_with_problems(),
                          acts=acts_with_unchecked())
    priorities = [d["priority"] for d in review["decisions"]]
    assert priorities[0] == PRIORITY_BLOCKING
    assert priorities == sorted(priorities, key=lambda p: {"blocking": 0, "important": 1,
                                                           "optional": 2}[p])


def test_optional_items_do_not_block():
    review = build_review(summary=summary_ok(), batch=batch_with_problems())
    kinds = {i["kind"] for i in review["open_items"]}
    assert {"calibration", "cache"} <= kinds
    assert all(i["priority"] != PRIORITY_BLOCKING for i in review["open_items"])


# ─────────────────────────── вывод для человека ─────────────────────────
def test_markdown_has_all_sections():
    review = build_review(summary=summary_with_critical(), impact=impact_with_risk(),
                          batch=batch_with_problems())
    md = review_to_markdown(review)
    for section in ("# Сводка проверки", "## Деньги", "## Что проверено",
                    "## Нужно решение человека", "## Осталось открытым", "## Что дальше"):
        assert section in md, section
    assert "не оценено" in md
    assert md.count("| ") >= 5


def test_write_review_creates_both_files(tmp_path):
    review = build_review(summary=summary_ok())
    paths = write_review(review, tmp_path / "review")
    assert Path(paths["json"]).exists() and Path(paths["markdown"]).exists()
    saved = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert saved["verdict"]["status"] == VERDICT_REVIEW       # не всё проверено → не ready
    assert "не выполнялось" in saved["verdict"]["headline"]


def test_load_json_tolerates_missing_and_broken(tmp_path):
    assert load_json(tmp_path / "нет.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{не json", encoding="utf-8")
    assert load_json(bad) is None


# ─────────────────── на реальных артефактах и в CLI/MCP ─────────────────
@pytest.mark.skipif(not SUMMARY.exists(), reason="нет out/summary.json")
def test_real_summary_review_blocks_on_critical():
    review = collect(summary_path=SUMMARY)
    assert review["verdict"]["status"] == VERDICT_BLOCKED
    assert review["verdict"]["blocking"] >= 1
    assert all("source" in d and "action" in d for d in review["decisions"])


def test_cli_review_writes_report(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "review",
                          "--summary", str(SUMMARY), "--out", str(tmp_path / "review")],
                         capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Вердикт:" in res.stdout and "Решения человека" in res.stdout
    assert (tmp_path / "review" / "review.json").exists()


def test_mcp_tool_review_summary(tmp_path):
    from pto_agent.cad.mcp_server import TOOLS, handle_rpc, make_ctx
    assert len(TOOLS) == 20
    ctx = make_ctx(ROOT, ROOT / "out" / "cad" / "audit.jsonl")
    req = {"jsonrpc": "2.0", "id": 31, "method": "tools/call",
           "params": {"name": "review_summary", "arguments": {}}}
    res = handle_rpc(req, ctx)
    out = json.loads(res["result"]["content"][0]["text"])
    assert out["ok"] is True
    assert out["verdict"]["status"] in (VERDICT_BLOCKED, VERDICT_REVIEW, VERDICT_READY,
                                        VERDICT_NO_DATA)
    assert isinstance(out["checked"], list) and len(out["checked"]) == 5


def test_mcp_tool_review_summary_markdown_format():
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx
    ctx = make_ctx(ROOT, ROOT / "out" / "cad" / "audit.jsonl")
    req = {"jsonrpc": "2.0", "id": 32, "method": "tools/call",
           "params": {"name": "review_summary", "arguments": {"format": "markdown"}}}
    res = handle_rpc(req, ctx)
    out = json.loads(res["result"]["content"][0]["text"])
    assert out["format"] == "markdown"
    assert "Сводка проверки" in out["markdown"]
