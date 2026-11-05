# -*- coding: utf-8 -*-
"""Ведомости расхода стали и спецификации: арифметика, конфликты, покрытие, BOQ.

Тесты работают на синтетических ведомостях (в CI нет ни uploads/, ни data/input/) и на
демо-файле, если он есть. Главное, что проверяется: агент не «поправляет» цифры в ведомости
и не выдаёт отсутствующие конструкции за долю от известных.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pto_agent.kr_schedules import (coverage, marks_of, read_schedules, schedules_report,
                                   to_boq, validate_schedules)

ROOT = Path(__file__).resolve().parents[1]
DEMO_SCHEDULES = ROOT / "data" / "input" / "kr1_schedules.json"
DEMO_PRICES = ROOT / "data" / "demo" / "prices_kr1"
DEMO_MAP = ROOT / "data" / "reference" / "takeoff_map_kr1.json"


def _doc(**overrides) -> dict:
    doc = {
        "schema": "pto-agent/kr-schedules@1",
        "object": "Тестовый объект",
        "project": "Т-1-КР",
        "source": "тестовый набор",
        "entered_by": "test",
        "needs_verification": True,
        "sheets": [
            {"sheet": 1, "file": "s1.pdf", "tables": [
                {"name": "Спецификация материалов на фундамент", "rows": [
                    {"name": "Бетон В25 W6 F50", "unit": "м3", "qty": 100.0},
                    {"name": "Щебень гранитный фр. 20-40", "unit": "м3", "qty": 40.0},
                ]},
            ]},
            {"sheet": 2, "file": "s2.pdf", "tables": [
                {"name": "Ведомость расхода стали. Плита",
                 "steel": {"grade": "А500С", "diameters": {"Ø12": 5000.0, "Ø25": 3000.0},
                           "total_kg": 8000.0},
                 "concrete": {"mark": "В25 W6 F150", "qty": 100.0}},
                {"name": "Ведомость расхода стали. Стены",
                 "steel": {"grade": "А500С", "diameters": {"Ø10": 1000.0, "Ø25": 1000.0},
                           "total_kg": 2000.0},
                 "concrete": {"mark": "В25 W6 F150", "qty": 200.0}},
                {"name": "Ведомость расхода стали. Перекрытия",
                 "steel": {"grade": "А500С", "diameters": {"Ø12": 1000.0}, "total_kg": 1500.0},
                 "concrete": {"mark": "В25", "qty": 120.0}},
            ]},
        ],
    }
    doc.update(overrides)
    return doc


# ─────────────────────────── чтение ───────────────────────────
def test_read_schedules_keeps_file_name(tmp_path):
    path = tmp_path / "sch.json"
    path.write_text(json.dumps(_doc(), ensure_ascii=False), encoding="utf-8")
    doc = read_schedules(path)
    assert doc["_file"] == "sch.json"


def test_marks_of_splits_class_strength_frost():
    assert marks_of("В25 W6 F150") == {"В25", "W6", "F150"}
    assert marks_of("B25 W6 F50") == {"B25", "W6", "F50"}


# ─────────────────────────── арифметика ───────────────────────────
def test_sum_matches_total_is_not_a_finding():
    validation = validate_schedules(_doc())
    mismatches = [f for f in validation["findings"] if f["type"] == "SUM_MISMATCH"]
    assert [f["where"] for f in mismatches] == ["Ведомость расхода стали. Перекрытия"]


def test_sum_mismatch_reports_both_numbers():
    validation = validate_schedules(_doc())
    finding = next(f for f in validation["findings"] if f["type"] == "SUM_MISMATCH")
    assert finding["expected"] == 1000.0
    assert finding["found"] == 1500.0
    assert "1 000,00" in finding["detail"] and "1 500,00" in finding["detail"]


def test_missing_total_is_a_finding():
    doc = _doc()
    doc["sheets"][1]["tables"][0]["steel"]["total_kg"] = None
    validation = validate_schedules(doc)
    assert any(f["type"] == "SUM_MISMATCH" and "нет «Итого»" in f["detail"]
               for f in validation["findings"])


def test_mark_conflict_with_pdf_facts():
    facts = {"by_name": {"бетон плиты и стен подвала": {
        "value": "В25 W6 F150", "page": 11, "quote": "бетон В25 W6 F150"}}}
    validation = validate_schedules(_doc(), facts)
    conflict = next(f for f in validation["findings"] if f["type"] == "MARK_CONFLICT")
    assert conflict["where"] == "Бетон В25 W6 F50"
    assert "F50" in conflict["detail"] and "F150" in conflict["detail"]
    assert "стр. 11" in conflict["detail"]


def test_mark_conflict_only_within_same_concrete_class():
    """В10 подбетонки и В25 плиты — разные конструкции, это не конфликт марок."""
    doc = _doc()
    doc["sheets"][0]["tables"][0]["rows"].append({"name": "Бетон В10", "unit": "м3", "qty": 5.0})
    facts = {"by_name": {"бетон плиты и стен подвала": {"value": "В25 W6 F150", "page": 11}}}
    validation = validate_schedules(doc, facts)
    wheres = [f["where"] for f in validation["findings"] if f["type"] == "MARK_CONFLICT"]
    assert wheres == ["Бетон В25 W6 F50"]


def test_source_unverified_is_always_reported():
    validation = validate_schedules(_doc())
    assert any(f["type"] == "SOURCE_UNVERIFIED" for f in validation["findings"])


def test_source_verified_doc_has_no_such_finding():
    validation = validate_schedules(_doc(needs_verification=False))
    assert not any(f["type"] == "SOURCE_UNVERIFIED" for f in validation["findings"])


def test_plausibility_flags_impossible_rebar_rate():
    """1 500 кг на 120 м3 = 12,5 кг/м3 — так не бывает, значит опечатка."""
    validation = validate_schedules(_doc())
    flagged = [f["where"] for f in validation["findings"] if f["type"] == "PLAUSIBILITY"]
    assert "Ведомость расхода стали. Перекрытия" in flagged


def test_plausibility_is_called_heuristic_not_norm():
    validation = validate_schedules(_doc())
    finding = next(f for f in validation["findings"] if f["type"] == "PLAUSIBILITY")
    assert "не норматив" in finding["detail"]


# ─────────────────────────── BOQ ───────────────────────────
def test_boq_uses_printed_total_and_keeps_alternate():
    boq = to_boq(_doc())
    line = next(l for l in boq["lines"] if l["description"].startswith("Арматура")
                and "Перекрытия" in l["description"])
    assert line["qty"] == 1.5          # «Итого» 1 500 кг
    assert line["alt_qty"] == 1.0      # сумма по диаметрам 1 000 кг
    assert line["confidence"] == "low"
    assert "не идёт" in line["caveat"]


def test_boq_marks_good_schedules_as_medium_not_high():
    """Перенесённое с фото — не «факт проекта»: выше medium не поднимаем."""
    boq = to_boq(_doc())
    assert {l["confidence"] for l in boq["lines"]} <= {"medium", "low"}


def test_boq_does_not_double_concrete_from_spec_and_schedule():
    boq = to_boq(_doc())
    concrete = [l for l in boq["lines"] if l["description"].startswith("Бетон")
                and l["qty"] == 100.0 and l["unit"] == "м3"]
    assert len(concrete) == 1
    assert "не суммируется" in concrete[0].get("note", "")


def test_boq_units_are_normalised():
    boq = to_boq(_doc())
    units = {l["unit"] for l in boq["lines"]}
    assert "м3" in units and "m3" not in units


def test_boq_totals_by_kind():
    boq = to_boq(_doc())
    assert boq["totals_by_kind"]["арматура"]["qty"]["т"] == 11.5


# ─────────────────────────── покрытие ───────────────────────────
def test_coverage_lists_absent_floors_as_missing():
    doc = _doc()                       # убираем ведомость на перекрытия — их в наборе нет
    doc["sheets"][1]["tables"] = doc["sheets"][1]["tables"][:2]
    cover = coverage(doc)
    missing = " ".join(m["position"] for m in cover["missing"])
    assert "2 этажа" in missing and "3 этажа" in missing
    assert "перекрытия" in missing


def test_coverage_has_addressee_and_no_share_estimates():
    cover = coverage(_doc())
    for item in cover["missing"]:
        assert item["status"] == "missing"
        assert item["to"]
        assert item["ask"]
        assert "долей" in item["note"]


def test_coverage_detects_present_element():
    doc = _doc()
    doc["sheets"][1]["tables"][2]["name"] = "Ведомость расхода стали. Перекрытия 1 этажа"
    cover = coverage(doc)
    assert "перекрытия (плиты) подвала, 1–3 этажей" in cover["covered"]


# ─────────────────────────── отчёт ───────────────────────────
def test_report_has_sections_and_warnings():
    doc = _doc()
    validation = validate_schedules(doc)
    boq = to_boq(doc, validation)
    text = schedules_report(doc, validation, boq, cover=coverage(doc))
    assert "Проверка арифметики" in text
    assert "Чего в ведомостях нет" in text
    assert "F50" in text and "не сходится" in text
    assert "смета" not in text.lower() or "не смета" in text.lower()


def test_report_never_claims_estimate_is_ready():
    doc = _doc()
    validation = validate_schedules(doc)
    text = schedules_report(doc, validation, to_boq(doc, validation)).lower()
    assert "смета готова" not in text


# ─────────────────────────── демо-данные и CLI ───────────────────────────
@pytest.mark.skipif(not DEMO_SCHEDULES.exists(), reason="нет data/input (каталог не в CI)")
def test_demo_schedules_arithmetic():
    doc = read_schedules(DEMO_SCHEDULES)
    validation = validate_schedules(doc)
    wheres = [f["where"] for f in validation["findings"] if f["type"] == "SUM_MISMATCH"]
    assert wheres == ["Ведомость расхода стали. Фундаментная плита"]


@pytest.mark.skipif(not DEMO_SCHEDULES.exists(), reason="нет data/input (каталог не в CI)")
def test_demo_schedules_boq_concrete_volumes():
    boq = to_boq(read_schedules(DEMO_SCHEDULES))
    concrete = {l["description"]: l["qty"] for l in boq["lines"]
                if l["description"].startswith("Бетон")}
    assert concrete["Бетон В25 W6 F150 — Фундаментная плита"] == 2871.0
    assert concrete["Бетон В25 W6 F150 — Стены подвала"] == 469.8
    assert concrete["Бетон В25 — Колонны подвала"] == 53.25


@pytest.mark.skipif(not (DEMO_SCHEDULES.exists() and DEMO_PRICES.exists()
                        and DEMO_MAP.exists()),
                    reason="нет демо-данных для прогона по ведомостям")
def test_cli_schedules_run(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "pto_agent", "takeoff", "--out", str(tmp_path), "schedules",
         "--schedules", str(DEMO_SCHEDULES), "--map", str(DEMO_MAP),
         "--prices-dir", str(DEMO_PRICES), "--as-of", "2025-10-01"],
        capture_output=True, text=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    report = (tmp_path / "kr_schedules_report.md").read_text(encoding="utf-8")
    payload = json.loads((tmp_path / "kr_schedules_report.json").read_text(encoding="utf-8"))
    assert "требует сверки с PDF" in report
    assert payload["draft_totals"]["excluded_lines"] == 1     # арматура плиты — low
    assert payload["draft_totals"]["no_price_lines"] == 2     # земляные работы — не материал
    assert (tmp_path / "draft.xlsx").exists()


@pytest.mark.skipif(not DEMO_SCHEDULES.exists(), reason="нет data/input (каталог не в CI)")
def test_mcp_takeoff_schedules_action(tmp_path):
    """MCP-инструмент отдаёт те же объёмы и те же missing, что и CLI: наращивание через MCP."""
    from pto_agent.cad.mcp_server import make_ctx, t_takeoff
    ctx = make_ctx(str(ROOT), str(tmp_path / "audit.jsonl"))
    result = t_takeoff({"action": "schedules", "schedules_path": str(DEMO_SCHEDULES),
                        "map_path": str(DEMO_MAP), "prices_dir": str(DEMO_PRICES),
                        "as_of": "2025-10-01", "out_dir": str(tmp_path / "mcp")}, ctx)
    assert result["ok"] is True
    assert result["needs_verification"] is True
    assert len(result["missing"]) == 8
    assert result["draft_totals"]["excluded_lines"] == 1
    assert "требует сверки с PDF" in result["markdown"]
