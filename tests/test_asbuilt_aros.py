# -*- coding: utf-8 -*-
"""Смета АРОС как есть: разбор выгрузки, материалы отдельно от работ, акты по группам."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.asbuilt.acts_plan import plan_acts, plan_to_specs
from pto_agent.asbuilt.aros import (aros_positions, aros_to_markdown, parse_aros_markdown,
                                    read_aros, section_summary)
from pto_agent.asbuilt.norms_check import load_base, verify_norms
from pto_agent.asbuilt.requirements import classify_positions, classify_work, load_matrix
from pto_agent.asbuilt.spec_from_estimate import load_positions

FIXTURE = ROOT / "tests" / "fixtures" / "aros_smeta.md"
REAL = ROOT / "uploads" / "smeta_1166N_AROS.md"     # смета пользователя, в репозиторий не входит
needs_real = pytest.mark.skipif(not REAL.exists(), reason="нет выгрузки сметы 1166/Н")


@pytest.fixture(scope="module")
def doc():
    return read_aros(FIXTURE)


# ───────────────────────────── разбор выгрузки ─────────────────────────────
def test_header_row_is_not_a_position(doc):
    """Строка с номерами графов (1, 2, 3 …) раньше попадала в позиции как «работа 3»."""
    assert len(doc["positions"]) == 14
    assert all(p["name"] != "3" for p in doc["positions"])
    assert all(p["code"] for p in doc["positions"])


def test_sections_and_meta(doc):
    assert doc["totals"]["smeta_no"].startswith("900/Т")
    assert [s["index"] for s in doc["sections"]] == [1, 2, 4]
    summary = section_summary(doc)
    assert summary[1]["works"] == 7 and summary[1]["materials"] == 4


def test_works_and_materials_are_separated(doc):
    assert len(doc["works"]) == 10
    assert len(doc["materials"]) == 4
    assert all(p["kind"] == "work" for p in doc["works"])
    assert all(p["kind"] == "material" for p in doc["materials"])


def test_unit_and_qty_are_read_after_colspan(doc):
    """colspan в выгрузке сдвигает графы: единица и объём должны читаться верно."""
    shtroby = next(p for p in doc["works"] if p["code"] == "ГЭСН46-03-011-01")
    assert shtroby["unit"] == "100 м" and shtroby["qty"] == 0.4
    cable = next(p for p in doc["materials"] if "ВВГнг" in p["name"])
    assert cable["unit"] == "1000 м" and cable["qty"] == pytest.approx(0.3264)


def test_volume_formula_is_kept_apart(doc):
    cable = next(p for p in doc["positions"] if p["code"] == "21.1.06.09-0151")
    assert cable["formula"] == "320*1,02/1000"
    assert "V=" not in cable["name"]


def test_resource_rows_under_position_are_skipped(doc):
    codes = {p["code"] for p in doc["positions"]}
    assert "1-100-23" not in codes and "4-100-030" not in codes


def test_markdown_summary_lists_sections(doc):
    md = aros_to_markdown(doc)
    assert "АРОС" in md and "Демонтаж электропроводки" in md


# ─────────────────────── материалы против работ в актах ───────────────────────
def test_materials_are_not_classified_as_works(doc):
    """Кабель отдельной строкой — материал; в акт как работа он попадать не должен."""
    classified = classify_positions(aros_positions(doc))
    assert classified["total"] == 10                 # только работы
    assert classified["materials_skipped"] == 4
    names = [p["name"] for w in classified["work_types"] + classified["candidates"]
             for p in w["positions"]]
    assert not any("Кабель силовой" in n for n in names)
    assert not any("Устройство защитного отключения" in n for n in names)


def test_load_positions_reads_aros_directly():
    positions, source = load_positions(smeta=FIXTURE)
    assert "АРОС" in source and "10 работ" in source
    assert {p["kind"] for p in positions} == {"work", "material"}


def test_materials_are_deduplicated_and_attached_to_acts(doc):
    plan = plan_acts(aros_positions(doc), act_prefix="АОСР-900/Т", act_date="2026-11-04")
    trassy = next(a for a in plan["acts"] if a["group"] == "trassy")
    shtity = next(a for a in plan["acts"] if a["group"] == "shity")
    cables = [m for m in trassy["materials"] if "ВВГнг" in m["name"]]
    assert len(cables) == 1                                  # один код — одна строка
    assert cables[0]["qty"] == pytest.approx(0.3764)          # объёмы сложены
    assert shtity["materials"][0]["name"].startswith("Выключатель автоматический")
    assert "Устройство защитного" in " ".join(m["name"] for m in shtity["materials"])
    assert all(m["act_group"] for m in plan["materials"])


def test_act_groups_split_by_hidden_work(doc):
    plan = plan_acts(aros_positions(doc), act_prefix="АОСР-900/Т", act_date="2026-11-04")
    groups = [a["group"] for a in plan["acts"]]
    assert groups == ["shtroby", "prohodki", "shity", "osveschenie", "trassy"]
    shtroby = next(a for a in plan["acts"] if a["group"] == "shtroby")
    assert shtroby["positions_count"] == 2          # пробивка борозд + заделка борозд
    assert plan["positions_excluded"] == 3          # демонтаж + погрузка + перевозка


def test_demolition_and_waste_are_out_of_acts(doc):
    plan = plan_acts(aros_positions(doc), act_prefix="АОСР", act_date="2026-11-04")
    reasons = " ".join(e["reason"] for e in plan["excluded"]).lower()
    assert "демонтаж" in reasons and ("вывоз" in reasons or "погрузка" in reasons)
    work_text = " ".join(a["work"] for a in plan["acts"]).lower()
    assert "демонтаж" not in work_text and "перевозка" not in work_text


def test_protocols_depend_on_act_group_not_work_type(doc):
    """Акт на борозды не тащит протоколы испытаний — их место в акте на ПНР."""
    plan = plan_acts(aros_positions(doc), act_prefix="АОСР", act_date="2026-11-04")
    by_group = {a["group"]: a for a in plan["acts"]}
    assert by_group["shtroby"]["protocols"] == []
    assert by_group["prohodki"]["protocols"] == []
    trassy = " ".join(by_group["trassy"]["protocols"]).lower()
    assert "изоляци" in trassy
    assert "акт_испытаний" not in by_group["shtroby"]["required"]
    assert "протокол" in by_group["trassy"]["required"]


def test_specs_carry_norms_with_clauses_and_mark_source(doc):
    plan = plan_acts(aros_positions(doc), act_prefix="АОСР-900/Т", act_date="2026-11-04")
    specs = plan_to_specs(plan)
    spec = specs[0]
    assert spec["norms"] and all(n.get("clause") for n in spec["norms"])
    assert all(n.get("source") == "matrix" for n in spec["norms"])
    assert "матрица" in spec["norms_source"]
    assert spec["required_kinds"] == plan["acts"][0]["required"]
    assert spec["act_group"]["id"] == plan["acts"][0]["group"]


# ───────────────────────────── нормативная база ─────────────────────────────
def test_pue_fragment_is_reported_as_fragment_not_full_verification():
    report = verify_norms([{"doc": "ПУЭ, 7-е изд.", "clause": "1.8.37"},
                           {"doc": "ПУЭ, 7-е изд.", "clause": "1.8.999"},
                           {"doc": "СП 70.13330.2012", "clause": "5.3.3"}])
    statuses = {(r["doc"], r["clause"]): r["status"] for r in report["results"]}
    assert statuses[("ПУЭ, 7-е изд.", "1.8.37")] == "verified_partial"
    assert statuses[("ПУЭ, 7-е изд.", "1.8.999")] == "clause_missing"
    assert statuses[("СП 70.13330.2012", "5.3.3")] == "verified"
    assert report["verified_partial"] == 1
    assert "фрагмент" in report["verdict"] or "фрагментами" in report["verdict"]


def test_base_merges_working_and_demo_documents():
    base = load_base()
    designations = " ".join(d["designation"] for d in base["docs"].values())
    assert "ПУЭ" in designations and "СП 70.13330.2012" in designations


# ─────────────────────────────────── CLI ───────────────────────────────────
def test_cli_smeta_parses_aros(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "smeta",
                          "--smeta", str(FIXTURE), "--out", str(tmp_path)],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    payload = json.loads((tmp_path / "smeta_parse.json").read_text(encoding="utf-8"))
    assert payload["works"] == 10 and payload["materials"] == 4
    assert payload["classification"]["total"] == 10
    assert payload["classification"]["classified"] == 10


def test_cli_acts_on_aros_file(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "acts",
                          "--smeta", str(FIXTURE), "--act-prefix", "АОСР-900/Т",
                          "--write-specs", "--out", str(tmp_path)],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    plan = json.loads((tmp_path / "acts_plan.json").read_text(encoding="utf-8"))
    assert len(plan["acts"]) == 5
    assert plan["positions_in_acts"] + plan["positions_excluded"] == plan["positions_total"]


# ─────────────────────── реальная смета пользователя ───────────────────────
@needs_real
def test_real_estimate_1166n_parses_and_splits():
    doc = read_aros(REAL)
    assert len(doc["positions"]) == 47
    assert (len(doc["works"]), len(doc["materials"])) == (33, 14)
    plan = plan_acts(aros_positions(doc), act_prefix="АОСР-1166/Н", act_date="2026-11-04")
    assert len(plan["acts"]) == 5
    assert plan["positions_total"] == 33
    assert plan["positions_in_acts"] == 19
    assert plan["positions_excluded"] == 14
    assert {a["group"] for a in plan["acts"]} == {"shtroby", "prohodki", "shity",
                                                 "osveschenie", "trassy"}


@needs_real
def test_real_estimate_has_no_unknown_positions():
    doc = read_aros(REAL)
    classified = classify_positions(aros_positions(doc))
    assert classified["unknown"] == []
    assert classified["classified"] == classified["total"] == 33
    electrical = classified["work_types"][0]
    assert electrical["work_type"] == "electrical" and len(electrical["positions"]) == 19


@needs_real
def test_real_estimate_terms_are_recognized():
    """Формулировки этой сметы: работы названы предметом, без глагола «монтаж»."""
    matrix = load_matrix()
    for text, expected in (("Прибор или аппарат", "electrical"),
                           ("Рукав металлический наружным диаметром: до 48 мм", "electrical"),
                           ("Сверление отверстий в строительных конструкциях из кирпича",
                            "electrical"),
                           ("Перевозка грузов I класса автомобилями-самосвалами",
                            "waste_removal"),
                           ("Демонтаж - Завеса воздушно-тепловая (регулируемая)", "demolition")):
        hits = classify_work(text, matrix)
        assert hits and hits[0]["work_type"] == expected, text
