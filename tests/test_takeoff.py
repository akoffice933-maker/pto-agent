# -*- coding: utf-8 -*-
"""Стык takeoff → ведомость объёмов с ценами: чужие объёмы, наши цены, решение человека."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.audit import AuditLog
from pto_agent.prices import import_prices
from pto_agent.takeoff import (STATUS_EXCLUDED, STATUS_NO_PRICE, STATUS_PRICED, STATUS_REVIEW,
                               build_draft, confirm, draft_to_markdown, export_draft_xlsx,
                               load_map, match_resource, read_boq, write)

BOQ = ROOT / "data" / "demo" / "takeoff" / "boq_kr_1166n.csv"
PRICES_KR = ROOT / "data" / "demo" / "prices" / "fgis_cs_2025q3_kr.csv"
CABLE_PRICES = ROOT / "data" / "demo" / "prices" / "fgis_cs_2025q3.csv"


@pytest.fixture()
def prices_dir(tmp_path):
    out = tmp_path / "prices"
    import_prices(PRICES_KR, source="ФГИС ЦС (КР)", price_date="2025-09-01", region="47",
                  out_dir=out)
    return out


def _line(description, qty, unit="m3", confidence="HIGH", source="КР-1"):
    return {"code": "", "description": description, "unit": unit, "unit_native": unit,
            "qty": qty, "qty_native": qty, "confidence": confidence, "source": source,
            "trade": "", "file": "test.csv"}


# ─────────────────────────────── чтение BOQ ───────────────────────────────
def test_read_boq_understands_autoconst_format():
    lines, problems = read_boq(BOQ)
    assert problems == [] and len(lines) == 8
    assert lines[0]["code"] == "03 30 00" and lines[0]["confidence"] == "high"
    assert lines[0]["qty"] == pytest.approx(42.5)      # «42,5» → 42.5
    assert lines[0]["source"] == "КР-1"


def test_read_boq_converts_kg_to_tonnes():
    lines, _ = read_boq(BOQ)
    rebar = next(l for l in lines if l["unit_native"] == "kg")
    assert rebar["unit"] == "т" and rebar["qty"] == pytest.approx(5.48)   # 5480 кг
    assert rebar["qty_native"] == pytest.approx(5480)


def test_read_boq_flags_row_without_quantity(tmp_path):
    path = tmp_path / "boq.csv"
    path.write_text("description,unit,qty,confidence\nБетон В25,m3,,HIGH\n", encoding="utf-8")
    lines, problems = read_boq(path)
    assert lines[0]["qty"] is None
    assert any("нет количества" in p for p in problems)


def test_unknown_unit_is_kept_as_is():
    lines, _ = read_boq(BOQ)
    assert all(line["unit"] for line in lines)


# ─────────────────────────────── сопоставление ───────────────────────────────
def test_mapping_matches_only_by_explicit_rules():
    mapping = load_map()
    assert match_resource(_line("Бетон В25 — плита", 10), mapping)["code"] == "01.1.02.01-0001"
    assert match_resource(_line("Бетон В15 — балки", 10), mapping)["code"] == "01.1.02.01-0002"
    missed = match_resource(_line("Керамогранит — полы", 10, unit="m2"), mapping)
    assert missed["matched"] is False and missed["code"] == ""


def test_unknown_element_goes_to_no_price(prices_dir):
    draft = build_draft([_line("Керамогранит — полы", 10, unit="m2")], mapping=load_map(),
                        prices_dir=prices_dir)
    assert draft["lines"][0]["status"] == STATUS_NO_PRICE
    assert "нет правила сопоставления" in draft["lines"][0]["reason"]


# ─────────────────────────────── уверенность и цены ───────────────────────────────
def test_high_confidence_is_priced(prices_dir):
    draft = build_draft([_line("Бетон В25 — плита", 10, confidence="HIGH")],
                        mapping=load_map(), prices_dir=prices_dir, as_of="2025-10-01")
    line = draft["lines"][0]
    assert line["status"] == STATUS_PRICED
    assert line["price"] == pytest.approx(6200) and line["amount"] == pytest.approx(62000)
    assert line["price_version"] == "2025-09-01-фгис-цс-кр-47"
    assert line["price_date"] == "2025-09-01"


def test_medium_confidence_counted_but_flagged(prices_dir):
    draft = build_draft([_line("Опалубка щитовая", 100, unit="m2", confidence="MEDIUM")],
                        mapping=load_map(), prices_dir=prices_dir, as_of="2025-10-01")
    line = draft["lines"][0]
    assert line["status"] == STATUS_REVIEW and line["amount"] == pytest.approx(125000)
    assert draft["totals"]["review_lines"] == 1
    assert "требует проверки" in line["reason"]


def test_low_confidence_never_enters_total(prices_dir):
    draft = build_draft([_line("Бетон В25 — плита", 10, confidence="HIGH"),
                         _line("Кирпич керамический", 12, unit="1000 шт", confidence="LOW")],
                        mapping=load_map(), prices_dir=prices_dir, as_of="2025-10-01")
    assert [e["element"] for e in draft["excluded"]] == ["Кирпич керамический"]
    assert draft["totals"]["amount_rub"] == pytest.approx(62000)     # только HIGH
    assert draft["totals"]["excluded_lines"] == 1


def test_rate_not_found_is_excluded_too(prices_dir):
    draft = build_draft([_line("Опалубка щитовая", 96, unit="m2", confidence="RATE_NOT_FOUND")],
                        mapping=load_map(), prices_dir=prices_dir)
    assert draft["lines"] == [] and len(draft["excluded"]) == 1


def test_missing_price_in_base_is_no_price_not_zero(prices_dir):
    draft = build_draft([_line("Бетон В15 — фундаментные балки", 9.4)], mapping=load_map(),
                        prices_dir=prices_dir, as_of="2025-10-01")
    line = draft["lines"][0]
    assert line["status"] == STATUS_NO_PRICE and line["price"] is None
    assert line["amount"] is None
    assert "не найден" in line["reason"]


def test_price_on_date_not_newer(prices_dir, tmp_path):
    """Цены КР от 01.09 действуют на 01.10; запрос на август их не берёт."""
    before = build_draft([_line("Бетон В25 — плита", 1)], mapping=load_map(),
                         prices_dir=prices_dir, as_of="2025-08-01")
    after = build_draft([_line("Бетон В25 — плита", 1)], mapping=load_map(),
                        prices_dir=prices_dir, as_of="2025-10-01")
    assert before["lines"][0]["status"] == STATUS_NO_PRICE
    assert after["lines"][0]["status"] == STATUS_PRICED


def test_high_and_review_totals_are_separate(prices_dir):
    draft = build_draft([_line("Бетон В25 — плита", 10, confidence="HIGH"),
                         _line("Опалубка щитовая", 100, unit="m2", confidence="MEDIUM")],
                        mapping=load_map(), prices_dir=prices_dir, as_of="2025-10-01")
    assert draft["totals"]["amount_rub"] == pytest.approx(187000)
    assert draft["totals"]["amount_high_rub"] == pytest.approx(62000)


def test_demo_boq_gives_expected_totals(prices_dir):
    lines, _ = read_boq(BOQ)
    draft = build_draft(lines, mapping=load_map(), prices_dir=prices_dir, as_of="2025-10-01",
                        object_name="Дошкольное отделение")
    assert draft["totals"]["lines"] == 6
    assert draft["totals"]["priced_lines"] == 5
    assert draft["totals"]["excluded_lines"] == 2
    assert draft["totals"]["amount_rub"] == pytest.approx(2503660.0)
    assert draft["totals"]["amount_high_rub"] == pytest.approx(1763900.0)


def test_empty_price_base_means_no_prices(tmp_path):
    draft = build_draft([_line("Бетон В25 — плита", 10)], mapping=load_map(),
                        prices_dir=tmp_path / "empty")
    assert draft["lines"][0]["status"] == STATUS_NO_PRICE
    assert draft["totals"]["amount_rub"] is None


# ─────────────────────────────── подтверждение ───────────────────────────────
def test_confirm_records_change_in_audit(prices_dir, tmp_path):
    # единица — как её отдаёт read_boq после пересчёта кг→т (цена в базе за тонну)
    draft = build_draft([_line("Арматура А500С — каркасы", 5.48, unit="т", confidence="MEDIUM")],
                        mapping=load_map(), prices_dir=prices_dir, as_of="2025-10-01")
    log = AuditLog(tmp_path / "audit.jsonl")
    draft = confirm(draft, {"Арматура А500С — каркасы": {"qty": 5.20, "norm": "ФЕР 06-01-001-01"}},
                    by="Иванов И.И.", reason="пересчёт по ведомости каркасов КР-3", audit_log=log)
    line = draft["lines"][0]
    assert line["qty"] == pytest.approx(5.2) and line["norm"] == "ФЕР 06-01-001-01"
    assert line["confirmed"] is True and line["confirmed_by"] == "Иванов И.И."
    assert draft["totals"]["amount_rub"] == pytest.approx(62000 * 5.2)
    record = log.history()[0]
    assert record["actor"] == "Иванов И.И." and record["action"] == "takeoff.confirmed"
    assert record["payload"]["delta"]["qty"] == {"from": 5.48, "to": 5.2}
    assert log.verify()[0] is True


def test_confirm_without_reason_is_flagged(prices_dir, tmp_path):
    draft = build_draft([_line("Бетон В25", 10)], mapping=load_map(), prices_dir=prices_dir,
                        as_of="2025-10-01")
    log = AuditLog(tmp_path / "audit.jsonl")
    draft = confirm(draft, {}, by="Петров П.П.", audit_log=log)
    assert draft["confirmed_by"]["reason_missing"] is True
    assert log.history()[0]["payload"]["reason_missing"] is True


def test_confirm_excludes_line_and_recounts(prices_dir):
    draft = build_draft([_line("Бетон В25 — плита", 10, confidence="HIGH"),
                         _line("Опалубка щитовая", 100, unit="m2", confidence="MEDIUM")],
                        mapping=load_map(), prices_dir=prices_dir, as_of="2025-10-01")
    draft = confirm(draft, {"Опалубка щитовая": {"exclude": True, "note": "в объём не входит"}},
                    by="Иванов И.И.", reason="опалубка в аренду, не в смету")
    assert draft["totals"]["amount_rub"] == pytest.approx(62000)
    assert any(l["status"] == STATUS_EXCLUDED for l in draft["lines"])


def test_confirm_rejects_non_numeric_qty(prices_dir):
    draft = build_draft([_line("Бетон В25", 10)], mapping=load_map(), prices_dir=prices_dir,
                        as_of="2025-10-01")
    draft = confirm(draft, {"Бетон В25": {"qty": "много"}}, by="Иванов И.И.", reason="тест")
    assert draft["lines"][0]["qty"] == pytest.approx(10)
    assert any("не число" in p for p in draft["confirmed_by"]["problems"])


def test_draft_without_confirmation_says_so(prices_dir):
    draft = build_draft([_line("Бетон В25", 10)], mapping=load_map(), prices_dir=prices_dir,
                        as_of="2025-10-01")
    text = draft_to_markdown(draft)
    assert "не подтверждён человеком" in text
    assert "не смета в ФЕР" in text


# ─────────────────────────────── выгрузки, CLI, MCP ───────────────────────────────
def test_xlsx_has_three_sheets(prices_dir, tmp_path):
    draft = build_draft([_line("Бетон В25", 10)], mapping=load_map(), prices_dir=prices_dir,
                        as_of="2025-10-01")
    path = export_draft_xlsx(draft, tmp_path / "draft.xlsx")
    from openpyxl import load_workbook
    assert load_workbook(path).sheetnames == ["Ведомость объёмов", "Не включено", "Итоги"]


def test_cli_takeoff_draft_and_confirm(tmp_path):
    prices = tmp_path / "prices"
    import_prices(PRICES_KR, source="ФГИС ЦС (КР)", price_date="2025-09-01", region="47",
                  out_dir=prices)
    out = tmp_path / "takeoff"
    res = subprocess.run([sys.executable, "-m", "pto_agent", "takeoff", "--out", str(out),
                          "draft", "--boq", str(BOQ), "--prices-dir", str(prices),
                          "--as-of", "2025-10-01", "--object-name", "Объект"],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    assert "Ведомость объёмов КР" in res.stdout and (out / "draft.xlsx").exists()

    res = subprocess.run([sys.executable, "-m", "pto_agent", "takeoff", "--out", str(out),
                          "confirm", "--draft", str(out / "draft.json"),
                          "--set", "Бетон В25 — плита перекрытия на отм. +3,000=40",
                          "--by", "Иванов И.И.", "--reason", "уточнил по КР-1",
                          "--audit", str(out / "audit.jsonl")],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    draft = json.loads((out / "draft.json").read_text(encoding="utf-8"))
    assert draft["confirmed_by"]["by"] == "Иванов И.И."
    assert AuditLog(out / "audit.jsonl").history()[0]["action"] == "takeoff.confirmed"


def test_mcp_takeoff_draft_and_confirm(tmp_path, prices_dir):
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx
    ctx = make_ctx(str(ROOT))

    def call(arguments):
        res = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "takeoff", "arguments": arguments}}, ctx)
        return json.loads(res["result"]["content"][0]["text"])

    out = call({"action": "draft", "boq_path": str(BOQ), "prices_dir": str(prices_dir),
                "as_of": "2025-10-01", "out_dir": str(tmp_path / "out")})
    assert out["ok"] and out["draft"]["totals"]["priced_lines"] == 5

    confirmed = call({"action": "confirm", "draft_path": str(tmp_path / "out" / "draft.json"),
                      "changes": {"Бетон В25 — стены подвала, оси 1-7": {"qty": 115}},
                      "by": "Иванов И.И.", "reason": "уточнение по КР-2",
                      "out_dir": str(tmp_path / "out")})
    assert confirmed["draft"]["confirmed_by"]["by"] == "Иванов И.И."


def test_external_mcp_takeoff_is_medium_confidence(prices_dir):
    """Второй путь — чужой обмер по MCP: HIGH не выставляется, «не оценено» — не ноль."""
    from pto_agent.takeoff import from_external_report
    report = {"source": "opentakeoff", "license": "AGPL-3.0 (отдельный сервис)",
              "tool": "takeoff_area", "file": "kr.pdf",
              "positions": [{"code": "K-1", "name": "Бетон В25 — плита", "qty": 40,
                             "unit": "m3"},
                            {"code": "K-2", "name": "Арматура А500С — каркасы", "qty": None,
                             "unit": "t"}]}
    converted = from_external_report(report)
    draft = build_draft(converted["lines"], mapping=load_map(), prices_dir=prices_dir,
                        as_of="2025-10-01")
    assert draft["lines"][0]["confidence"] == "medium"
    assert draft["lines"][0]["status"] == STATUS_REVIEW
    assert draft["lines"][0]["source"] == "opentakeoff"
    assert [e["element"] for e in draft["excluded"]] == ["Арматура А500С — каркасы"]
    assert "HIGH не выставляется" in converted["note"]


def test_registry_of_takeoff_projects_is_sane():
    from scripts.check_takeoff_oss import check_offline
    doc = json.loads((ROOT / "data" / "reference" / "takeoff_oss.json").read_text(encoding="utf-8"))
    assert check_offline(doc) == []
    by_repo = {r["repo"].split("/")[-1]: r for r in doc["repos"]}
    assert by_repo["autoconst-claude-2d-pdf-to-priced-boq"]["copy_allowed"] is True
    assert by_repo["OpenConstructionERP"]["copy_allowed"] is False       # AGPL
    assert by_repo["AI-Powered-Material-Estimation-from-Architectural-PDFs"]["copy_allowed"] is False


def test_write_roundtrip(tmp_path, prices_dir):
    draft = build_draft([_line("Бетон В25", 10)], mapping=load_map(), prices_dir=prices_dir,
                        as_of="2025-10-01")
    path = write(tmp_path / "draft.json", draft)
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == "pto-agent/takeoff-draft@1"
