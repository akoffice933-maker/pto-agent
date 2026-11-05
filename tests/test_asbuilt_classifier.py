# -*- coding: utf-8 -*-
"""Классификатор, разбивка на акты и проверка норм по базе.

Тесты воспроизводят прогон на смете «1166/Н»: 47 позиций электромонтажа, где первая версия
классификатора узнавала 23 позиции, тянула протоколы бетона и вентиляции из-за конструкций
«в бетонных стенах» и «кронштейны под вентиляцию», и складывала всё в один акт вместе
с демонтажом и вывозом мусора.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.asbuilt.acts_plan import plan_acts, plan_to_markdown, plan_to_specs
from pto_agent.asbuilt.aosr import build_aosr
from pto_agent.asbuilt.norms_check import (load_base, verify_norms, verify_quote,
                                           norms_to_markdown)
from pto_agent.asbuilt.requirements import (classify_positions, classify_work, hidden_work,
                                            load_matrix)
from pto_agent.asbuilt.spec_from_estimate import load_positions, spec_from_estimate
from pto_agent.asbuilt.validation import split_norm_string, validate_draft

SMETA = ROOT / "data" / "demo" / "smeta_1166n.xlsx"
NORMS_DIR = ROOT / "data" / "demo" / "norms"
needs_smeta = pytest.mark.skipif(not SMETA.exists(), reason="нет демо-сметы 1166/Н")


def _positions():
    return load_positions(smeta=SMETA)


# ──────────────────────────── классификатор ────────────────────────────
def test_strong_key_is_required_for_work_type():
    """«Заделка отверстий в бетонных и кирпичных стенах» — электромонтаж, не бетон."""
    hits = classify_work("Заделка отверстий в бетонных и кирпичных стенах")
    assert hits and hits[0]["work_type"] == "electrical"
    assert all(h["work_type"] not in {"concrete", "masonry"} for h in hits)


def test_hvac_noise_does_not_create_work_type():
    """«Кронштейны под вентиляцию» не должны приносить протоколы балансировки."""
    assert classify_work("Монтаж кронштейнов под вентиляцию") == []


def test_price_base_prefix_is_not_a_keyword():
    """«ФЕРм 21-02-012-01» не должен читаться как «ферма» (металлоконструкции)."""
    hits = classify_work("ФЕРм 20-02-001-01 Проходка отверстий в бетонных стенах", )
    assert hits and hits[0]["work_type"] not in {"metal_structures"}


def test_stem_does_not_match_inside_word():
    """«кладк» не должно срабатывать внутри «прокладка»."""
    hits = classify_work("Прокладка провода ПуГВ 3х1,5 в гофротрубе")
    assert hits and hits[0]["work_type"] == "electrical"
    assert all(h["work_type"] != "masonry" for h in hits)


def test_electrical_component_words_are_recognized():
    for text in ("Установка УЗО 2П 40А/30мА в щите",
                 "Установка дифавтомата 1П+N 16А/10мА",
                 "Установка автоматического выключателя ВА47-29 16А в щите",
                 "Монтаж лотка металлического 200х50",
                 "Проверка петли «фаза-нуль»",
                 "Монтаж молниеприёмной сетки",
                 "Заделка борозд цементным раствором"):
        hits = classify_work(text)
        assert hits, text
        assert hits[0]["work_type"] == "electrical", (text, hits[0])


def test_demolition_wins_over_installation():
    hits = classify_work("Демонтаж светильников с люминесцентными лампами")
    assert hits[0]["work_type"] == "demolition"
    assert hidden_work("demolition") is False
    assert hidden_work("waste_removal") is False
    assert hidden_work("electrical") is True


@needs_smeta
def test_demo_estimate_classification_quality():
    positions, _ = _positions()
    r = classify_positions(positions)
    assert r["total"] == 47
    assert r["classified"] >= 46                     # было 23 из 47
    assert r["work_types"][0]["work_type"] == "electrical"
    assert r["work_types"][0]["share"] > 0.8
    # малая доля не тянет протоколы: демонтаж и вывоз мусора — кандидаты, а не комплект
    candidate_ids = {c["work_type"] for c in r["candidates"]}
    assert {"demolition", "waste_removal"} <= candidate_ids
    assert all(w["work_type"] != "hvac" for w in r["work_types"])


@needs_smeta
def test_child_work_type_merges_into_parent():
    """Заземление внутри электроустановки идёт в акт электромонтажа, а не отдельным видом."""
    positions, _ = _positions()
    r = classify_positions(positions)
    assert any(m["work_type"] == "grounding_lightning" and m["into"] == "electrical"
               for m in r["merged"])


def test_min_share_is_declared_in_matrix():
    matrix = load_matrix()
    assert matrix["classifier"]["min_share"] == 0.15
    assert matrix["classifier"]["rules"]


# ─────────────────────────── план отдельных актов ───────────────────────────
@needs_smeta
def test_plan_splits_estimate_into_hidden_work_acts():
    positions, source = _positions()
    plan = plan_acts(positions, act_prefix="АОСР-1166/Н", object_name="Объект 1166/Н",
                     source=source, act_date="2026-11-04")
    groups = [a["group"] for a in plan["acts"]]
    assert plan["positions_in_acts"] == 41
    assert len(plan["acts"]) == 7
    assert {"shtroby", "prohodki", "trassy", "shity", "osveschenie", "zazemlenie",
            "puskonaladka"} <= set(groups)
    # ни демонтаж, ни вывоз мусора в акты не попадают
    all_work = " ".join(a["work"] for a in plan["acts"]).lower()
    assert "демонтаж" not in all_work and "вывоз мусора" not in all_work
    assert plan["acts"][0]["code"] == "АОСР-1166/Н-1"
    assert plan["acts"][0]["work_type"] == "electrical"


@needs_smeta
def test_shtroby_act_gets_dedicated_works():
    """Акт на борозды — это штробление и заделка борозд, а не «всё подряд»."""
    positions, source = _positions()
    plan = plan_acts(positions, act_prefix="АОСР", source=source)
    shtroby = next(a for a in plan["acts"] if a["group"] == "shtroby")
    assert shtroby["positions_count"] == 2
    assert "Штробление" in shtroby["work"] and "Заделка борозд" in shtroby["work"]
    assert all("демонтаж" not in p["name"].lower() for p in shtroby["positions"])


@needs_smeta
def test_acts_markdown_lists_excluded_positions():
    positions, source = _positions()
    plan = plan_acts(positions, source=source, act_prefix="АОСР")
    md = plan_to_markdown(plan)
    assert "вне актов" in md and "кронштейнов" in md.lower()
    assert "| Акт | Группа скрытых работ |" in md


@needs_smeta
def test_each_act_makes_its_own_draft(tmp_path):
    positions, source = _positions()
    plan = plan_acts(positions, act_prefix="АОСР", object_name="Объект", source=source)
    specs = plan_to_specs(plan)
    assert len(specs) == len(plan["acts"])
    spec = specs[0]
    assert spec["work_types"] == [plan["acts"][0]["work_type"]]
    spec["project_docs"] = [{"doc": "26-ЭО-1", "sheet": "лист 4"}]
    spec["materials"] = [{"name": "кабель ВВГнг-LS", "certificate": "сертификат № К-1"}]
    spec["start_date"] = spec["end_date"] = "2026-11-01"
    spec["representatives"] = {"customer": {"person": "Петров П.П.", "nrs": "НРС-1"},
                               "builder": {"person": "Иванов И.И."},
                               "contractor": {"person": "Сидоров С.С."}}
    spec["norms"] = [{"doc": "СП 70.13330.2012", "clause": "5.3.3", "edition": "с изм. 1"}]
    res = build_aosr(spec, tmp_path, today=__import__("datetime").date(2026, 11, 4))
    assert Path(res["docx"]).exists()
    assert res["act_number"] == spec["act_number"].replace("/", "-")


# ─────────────────────── нормы: проверка по базе ───────────────────────
def test_norms_base_loads_repo_documents():
    base = load_base(NORMS_DIR)
    assert base["exists"] is True
    assert len(base["docs"]) >= 3
    assert any("70.13330" in d["designation"] for d in base["docs"].values())


def test_verify_norms_distinguishes_outcomes():
    report = verify_norms([{"doc": "СП 70.13330.2012", "clause": "5.3.3"},
                           {"doc": "СП 63.13330.2018", "clause": "99.9.9"},
                           {"doc": "СП 76.13330.2016", "clause": "6.1"}], NORMS_DIR)
    statuses = {r["doc"]: r["status"] for r in report["results"]}
    assert statuses["СП 70.13330.2012"] == "verified"
    assert statuses["СП 63.13330.2018"] == "clause_missing"
    assert statuses["СП 76.13330.2016"] == "doc_missing"
    assert report["verified"] == 1 and report["unverified"] == 2
    assert "СП 76.13330.2016" in report["doc_missing"]


def test_verify_norms_markdown_does_not_claim_false_verification():
    report = verify_norms([{"doc": "ПУЭ, 7-е изд.", "clause": "1.8.37"}], NORMS_DIR)
    md = norms_to_markdown(report)
    assert "не проверена" in md or "не подтверждена" in md
    assert "Это не значит, что ссылка неверна" in md


def test_quote_verification_uses_base_text():
    ok = verify_quote("перерыв между укладкой слоёв бетонной смеси не должен превышать",
                      "СП 70.13330.2012", "5.3.3", NORMS_DIR)
    assert ok["ok"] is True and ok["body_chars"] > 0
    bad = verify_quote("бетон укладывается слоями не более 10 см без вибрирования",
                       "СП 70.13330.2012", "5.3.3", NORMS_DIR)
    assert bad["ok"] is False and "дословно" in bad["reason"]
    missing = verify_quote("любая цитата длиннее двадцати символов", "ПУЭ", "1.8.37", NORMS_DIR)
    assert missing["ok"] is False and "не найден" in missing["reason"]


def test_pue_is_recognized_as_document_designation():
    """«ПУЭ, 7-е изд.» — обозначение документа без номера; раньше это было замечание."""
    draft = validate_draft({"norms": [{"doc": "ПУЭ, 7-е изд.", "clause": "1.8.37",
                                       "edition": "действующая"}],
                            "project_docs": [{"doc": "26-ЭО"}],
                            "materials": ["кабель ВВГнг — сертификат № 1"],
                            "attachments": [{"name": "Исполнительная схема"}],
                            "representatives": {"customer": {"person": "П", "nrs": "1"},
                                                "builder": {"person": "И"},
                                                "contractor": {"person": "С"}},
                            "object_name": "О", "work": "Р", "start_date": "2026-11-01",
                            "end_date": "2026-11-02", "act_date": "2026-11-03"},
                           today=__import__("datetime").date(2026, 11, 4), norms_dir=NORMS_DIR)
    assert "norm_unrecognized" not in {i["code"] for i in draft["issues"]}
    assert "norm_not_in_base" in {i["code"] for i in draft["warnings"]}
    assert draft["can_transfer"] is True


def test_matrix_norms_are_marked_as_matrix_in_spec():
    positions = [{"code": "ФЕРм 21-03-001-01", "name": "Прокладка кабеля ВВГнг 3х2,5",
                  "qty": 10, "unit": "м"}]
    spec = spec_from_estimate(positions, source="тест")
    assert spec["norms_source"].startswith("матрица")
    assert all(n.get("source") == "matrix" for n in spec["norms"])


def test_norm_string_with_clause_is_parsed():
    assert split_norm_string("ПУЭ, 7-е изд. п. 1.8.37") == ("ПУЭ, 7-е изд.", "1.8.37")
    assert split_norm_string("СП 70.13330.2012") == ("СП 70.13330.2012", "")


# ─────────────────────────────── CLI / MCP ───────────────────────────────
@needs_smeta
def test_cli_acts_writes_plan_and_specs(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "acts",
                          "--smeta", str(SMETA), "--act-prefix", "АОСР-1166/Н",
                          "--object-name", "Объект 1166/Н", "--write-specs",
                          "--out", str(tmp_path)], capture_output=True, text=True,
                         timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    plan = json.loads((tmp_path / "acts_plan.json").read_text(encoding="utf-8"))
    assert len(plan["acts"]) == 7
    assert len(list((tmp_path / "specs").glob("*.json"))) == 7
    assert (tmp_path / "acts_plan.md").exists()


@needs_smeta
def test_cli_acts_with_explicit_work_type(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "acts",
                          "--smeta", str(SMETA), "--work-type", "electrical",
                          "--out", str(tmp_path)], capture_output=True, text=True,
                         timeout=180, cwd=ROOT)
    assert res.returncode == 0
    plan = json.loads((tmp_path / "acts_plan.json").read_text(encoding="utf-8"))
    assert all(a["work_type"] == "electrical" for a in plan["acts"])


def test_cli_verify_norms_exit_code(tmp_path):
    """Ссылка, документа которого нет в базе, — код возврата 1: «не проверено»."""
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"norms": [{"doc": "СП 76.13330.2016", "clause": "6.1"}]},
                               ensure_ascii=False), encoding="utf-8")
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "verify-norms",
                          "--spec", str(spec)], capture_output=True, text=True,
                         timeout=120, cwd=ROOT)
    assert res.returncode == 1
    report = json.loads(res.stdout)
    assert report["doc_missing"] == ["СП 76.13330.2016"]


def test_cli_verify_norms_pue_fragment_passes_with_note(tmp_path):
    """ПУЭ 1.8.37 в базе фрагментом: код 0, но статус — «подтверждено фрагментом»."""
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"norms": [{"doc": "ПУЭ, 7-е изд.", "clause": "1.8.37"}]},
                               ensure_ascii=False), encoding="utf-8")
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "verify-norms",
                          "--spec", str(spec)], capture_output=True, text=True,
                         timeout=120, cwd=ROOT)
    report = json.loads(res.stdout)
    assert report["results"][0]["status"] == "verified_partial"
    assert report["verified_partial"] == 1


def test_cli_verify_norms_ok_exit_code(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"norms": [{"doc": "СП 70.13330.2012", "clause": "5.3.3"}]},
                               ensure_ascii=False), encoding="utf-8")
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "verify-norms",
                          "--spec", str(spec)], capture_output=True, text=True,
                         timeout=120, cwd=ROOT)
    assert res.returncode == 0


def test_mcp_acts_and_verify_norms():
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx
    ctx = make_ctx(str(ROOT))
    if SMETA.exists():
        res = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "asbuilt_docs",
                                     "arguments": {"kind": "acts",
                                                   "smeta_path": str(SMETA),
                                                   "act_prefix": "АОСР-1166/Н"}}}, ctx)
        payload = json.loads(res["result"]["content"][0]["text"])
        assert len(payload["plan"]["acts"]) == 7
        assert "Борозды" in payload["markdown"]
    res = handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "asbuilt_docs",
                                 "arguments": {"kind": "verify-norms",
                                               "spec": {"norms": [{"doc": "СП 76.13330.2016",
                                                                   "clause": "6.1"}]}}}}, ctx)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["report"]["doc_missing"] == ["СП 76.13330.2016"]
