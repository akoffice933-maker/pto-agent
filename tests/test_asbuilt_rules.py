# -*- coding: utf-8 -*-
"""Правила ИД: действующая форма, матрица по видам работ, хронология, пункты норм.

Эти тесты закрывают замечания к первой версии модуля:
  * форма АОСР — по приказу Минстроя № 344/пр (с 01.09.2023), а не по РД-11-02-2006;
  * комплектность — по матрице видов работ, включая электромонтаж (изоляция, «фаза-нуль», УЗО);
  * хронология дат и ссылки на нормы с пунктами проверяются, а не подразумеваются;
  * агент готовит ЧЕРНОВИК: «готов к подписанию» не используется нигде;
  * спецификация АОСР собирается из сметы/КС-2, а не только руками.
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.asbuilt import (CURRENT_FORM, build_aosr, check_completeness, forms_catalog,
                               get_form, matrix_summary, required_kinds, spec_from_estimate,
                               spec_to_markdown, validate_draft, work_type_requirements)
from pto_agent.asbuilt.forms import form_status
from pto_agent.asbuilt.requirements import classify_work, load_matrix
from pto_agent.asbuilt.spec_from_estimate import load_positions

AOSR_SPEC = ROOT / "data" / "demo" / "asbuilt" / "aosr_ks38.json"
SMETA = ROOT / "data" / "demo" / "smeta_v1.xlsx"
MATRIX = ROOT / "data" / "reference" / "work_type_requirements.json"


def _spec() -> dict:
    return json.loads(AOSR_SPEC.read_text(encoding="utf-8"))


def _docx_text(path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


# ───────────────────────────── форма акта ─────────────────────────────
def test_current_form_is_344pr():
    catalog = forms_catalog()
    assert catalog["current"] == CURRENT_FORM == "344pr"
    ids = {f["id"]: f for f in catalog["forms"]}
    assert ids["344pr"]["status"] == "current"
    assert ids["rd-11-02-2006"]["status"] == "legacy"
    assert ids["344pr"]["applied_from"] == "2023-09-01"


def test_form_is_active_on_today_and_legacy_is_not():
    assert form_status("344pr", date(2026, 11, 4))["active_on_date"] is True
    legacy = form_status("rd-11-02-2006", date(2026, 11, 4))
    assert legacy["active_on_date"] is False
    assert "не применяется" in legacy["warning"]


def test_legacy_form_is_built_with_warning(tmp_path):
    res = build_aosr(_spec(), tmp_path, form="rd-11-02-2006", today=date(2026, 11, 4))
    assert res["form"]["status"] == "legacy"
    assert res["form"]["warning"]
    text = _docx_text(res["docx"])
    assert "ВНИМАНИЕ" in text and "1. Представители сторон" in text


def test_unknown_form_is_rejected():
    with pytest.raises(ValueError, match="неизвестная форма"):
        get_form("нет-такой-формы")


def test_draft_wording_never_says_ready_to_sign(tmp_path):
    from pto_agent.asbuilt import aosr_to_markdown
    res = build_aosr(_spec(), tmp_path, today=date(2026, 11, 4))
    md = aosr_to_markdown(res)
    assert "ЧЕРНОВИК" in md
    assert "готов к подписанию" not in md
    assert "передавать на подпись человеку" in md
    assert "готов к подписанию" not in _docx_text(res["docx"])


# ──────────────────────── матрица видов работ ────────────────────────
def test_matrix_covers_electrical_scope():
    req = work_type_requirements("electrical")
    assert req["ok"] is True
    assert {"аоср", "схема", "протокол", "акт_испытаний"} <= set(req["required"])
    prot = " ".join(req["protocols"]).lower()
    for needle in ("изоляц", "фаза-нуль", "узо", "заземл"):
        assert needle in prot, needle
    assert all(n.get("clause") for n in req["norms"])     # пункты есть у всех норм


def test_matrix_ids_are_consistent():
    matrix = load_matrix()
    kinds = set(matrix["document_kinds"])
    for wt in matrix["work_types"]:
        assert set(wt["required"]) <= kinds, wt["id"]
        assert wt["keywords"], wt["id"]
        for norm in wt["norms"]:
            assert norm["doc"] and norm["clause"], wt["id"]


def test_matrix_profile_has_version():
    profile = load_matrix()["profile"]
    assert profile["name"] and profile["version"]


def test_required_kinds_unions_and_reports_profile():
    req = required_kinds(["concrete", "electrical"])
    assert {"акт_конструкций", "протокол"} <= set(req["required"])
    assert req["profile"]["version"]
    assert req["unknown"] == []
    assert required_kinds(["нет-такого"])["unknown"] == ["нет-такого"]


def test_classify_work_detects_electrical():
    hits = classify_work("Прокладка кабеля ВВГнг в гофре, монтаж щита ЩР-1")
    assert hits and hits[0]["work_type"] == "electrical"


def test_classify_work_returns_nothing_for_unknown():
    assert classify_work("Покраска забора в три слоя аэрографом") == [] or \
        classify_work("Покраска забора в три слоя аэрографом")[0]["work_type"] == "finishing"


def test_matrix_summary_markdown_is_table():
    md = matrix_summary()
    assert md.startswith("| Вид работ |") and "electrical" in md


# ─────────────── комплектность по видам работ (электромонтаж) ────────────────
EL_REGISTRY = {
    "documents": [
        {"name": "АОСР №120 на скрытую прокладку кабеля", "kind": "аоср", "status": "подписан"},
        {"name": "Исполнительная схема кабельных трасс", "kind": "схема", "status": "проверен"},
        {"name": "Сертификат на кабель ВВГнг-LS", "kind": "сертификат", "status": "проверен"},
        {"name": "Протокол испытания сопротивления изоляции кабельных линий", "kind": "протокол",
         "status": "проверен"},
        {"name": "Протокол проверки УЗО щита ЩР-1", "kind": "протокол", "status": "проверен"},
        {"name": "Специальный журнал электромонтажных работ", "kind": "журнал", "status": "проверен"},
    ]
}


def test_electrical_completeness_requires_protocols_and_acts():
    comp = check_completeness(EL_REGISTRY, work_types=["electrical"])
    assert comp["complete"] is False
    kinds = {m["kind"] for m in comp["missing"]}
    assert {"акт_сетей", "акт_испытаний"} <= kinds
    missing_prot = " ".join(comp["missing_protocols"]).lower()
    assert "фаза-нуль" in missing_prot and "заземл" in missing_prot
    # уже приложенные протоколы не считаются отсутствующими
    assert "сопротивления изоляции" not in missing_prot
    assert "узо" not in missing_prot
    assert comp["required_source"].startswith("матрица видов работ")
    assert comp["profile"]["version"]


def test_default_completeness_keeps_smr_minimum():
    comp = check_completeness({"documents": []})
    assert comp["required_source"] == "типовой минимум СМР"
    assert {m["kind"] for m in comp["missing"]} == {"аоср", "схема", "сертификат",
                                                    "протокол", "журнал"}


def test_full_electrical_set_is_complete():
    docs = EL_REGISTRY["documents"] + [
        {"name": "Протокол проверки петли «фаза-нуль» на вводе", "kind": "протокол",
         "status": "проверен"},
        {"name": "Протокол проверки сопротивления заземляющих устройств", "kind": "протокол",
         "status": "проверен"},
        {"name": "Протокол испытания осветительной установки", "kind": "протокол",
         "status": "проверен"},
        {"name": "Акт освидетельствования участков сетей электроснабжения", "kind": "акт_сетей",
         "status": "проверен"},
        {"name": "Акт испытания электроустановки", "kind": "акт_испытаний", "status": "проверен"},
    ]
    comp = check_completeness({"documents": docs}, work_types=["electrical"])
    assert comp["missing"] == []
    assert comp["missing_protocols"] == [], comp["missing_protocols"]
    assert comp["complete"] is True


# ───────────────────── хронология и нормы с пунктами ─────────────────────
def test_dates_chronology_blocks_act_before_end():
    spec = _spec()
    spec["act_date"] = "2026-10-25"          # раньше окончания работ (27.10)
    draft = validate_draft(spec, today=date(2026, 11, 4))
    codes = {i["code"] for i in draft["blocking"]}
    assert "act_before_end" in codes
    assert draft["can_transfer"] is False


def test_dates_work_order_and_future_act():
    spec = _spec()
    spec["start_date"], spec["end_date"] = "2026-10-27", "2026-10-19"
    draft = validate_draft(spec, today=date(2026, 11, 4))
    assert "start_after_end" in {i["code"] for i in draft["issues"]}
    spec = _spec()
    spec["act_date"] = "2026-12-01"
    draft = validate_draft(spec, today=date(2026, 11, 4))
    assert "act_in_future" in {i["code"] for i in draft["warnings"]}


def test_attachment_after_act_is_blocking():
    spec = _spec()
    spec["attachments"][0]["date"] = "2026-11-01"     # позже акта 28.10
    draft = validate_draft(spec, today=date(2026, 11, 4))
    issue = next(i for i in draft["blocking"] if i["code"] == "attachment_after_act")
    assert "позже даты акта" in issue["text"]


def test_norms_without_clause_are_blocking():
    spec = _spec()
    spec["norms"] = ["СП 70.13330.2012", {"doc": "СП 63.13330.2018"}]
    draft = validate_draft(spec, today=date(2026, 11, 4))
    codes = [i["code"] for i in draft["blocking"]]
    assert codes.count("norm_without_clause") == 2
    assert "без указания пункта" in draft["blocking"][0]["text"]


def test_norms_with_clause_pass():
    spec = _spec()
    draft = validate_draft(spec, today=date(2026, 11, 4))
    assert "norm_without_clause" not in {i["code"] for i in draft["issues"]}


def test_norm_without_edition_is_warning_only():
    spec = _spec()
    spec["norms"] = [{"doc": "СП 70.13330", "clause": "5.3.3"}]
    draft = validate_draft(spec, today=date(2026, 11, 4))
    assert "norm_without_edition" in {i["code"] for i in draft["warnings"]}
    assert "norm_without_clause" not in {i["code"] for i in draft["blocking"]}


def test_project_docs_and_materials_are_checked():
    spec = _spec()
    spec.pop("project_docs")
    spec["materials"] = ["бетон B25"]
    draft = validate_draft(spec, today=date(2026, 11, 4))
    codes = {i["code"] for i in draft["issues"]}
    assert "project_docs_missing" in codes
    assert "material_without_docs" in codes


def test_nrs_missing_is_warning():
    spec = _spec()
    spec["representatives"]["customer"].pop("nrs")
    draft = validate_draft(spec, today=date(2026, 11, 4))
    codes = {i["code"] for i in draft["warnings"]}
    assert "nrs_missing" in codes
    assert "нрс" in " ".join(i["text"] for i in draft["warnings"]).lower()


def test_missing_scheme_attachment_is_reported():
    spec = _spec()
    spec["attachments"] = [a for a in spec["attachments"] if "схема" not in a["name"].lower()]
    draft = validate_draft(spec, work_types=["concrete"], today=date(2026, 11, 4))
    assert "attachment_no_scheme" in {i["code"] for i in draft["issues"]}


# ─────────────────── спецификация из сметы (без ручного ввода) ───────────────────
@pytest.mark.skipif(not SMETA.exists(), reason="нет демо-сметы")
def test_spec_from_estimate_fills_works_and_norms():
    positions, source = load_positions(smeta=SMETA)
    spec = spec_from_estimate(positions, object_name="ЖК «Северный»", act_number="115",
                              act_date="2026-11-03", source=source)
    assert spec["source"]["positions_total"] == 8
    assert spec["source"]["positions_classified"] == 8
    assert {"concrete", "waterproofing"} <= set(spec["work_types"])
    assert spec["norms"] and all(n.get("clause") for n in spec["norms"])
    assert spec["work"].startswith("Устройство фундаментной плиты")
    assert spec["attachments"]                       # шаблон приложений из матрицы
    assert {"representatives", "project_docs", "materials"} <= set(spec["needs_input"])
    md = spec_to_markdown(spec)
    assert "Заполнить вручную" in md


@pytest.mark.skipif(not SMETA.exists(), reason="нет демо-сметы")
def test_spec_from_estimate_electrical(tmp_path):
    positions, source = load_positions(smeta=SMETA)
    spec = spec_from_estimate(positions, work_types=["electrical"], source=source)
    assert spec["work_types"] == ["electrical"]
    text = " ".join(a["name"] for a in spec["attachments"]).lower()
    assert "изоляц" in text and "узо" in text


def test_spec_from_estimate_can_classify_from_summary(tmp_path):
    summary = {"positions": [{"code": "ФЕР 08-02-001-01",
                              "name": "Гидроизоляция фундаментной плиты обмазочная",
                              "qty": 100, "unit": "м2"}]}
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    positions, source = load_positions(summary_path=path)
    spec = spec_from_estimate(positions, source=source)
    assert spec["work_types"] == ["waterproofing"]
    assert "КС-2" in spec["source"]["positions_from"]


# ───────────────────────────────── CLI / MCP ─────────────────────────────────
def test_cli_requirements_and_forms():
    req = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "requirements",
                          "--work-type", "electrical"], capture_output=True, text=True,
                         timeout=120, cwd=ROOT)
    assert req.returncode == 0
    payload = json.loads(req.stdout)
    assert payload["work_type"] == "electrical" and payload["protocols"]
    forms = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "forms"],
                           capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert forms.returncode == 0 and json.loads(forms.stdout)["current"] == "344pr"


@pytest.mark.skipif(not SMETA.exists(), reason="нет демо-сметы")
def test_cli_spec_from_smeta(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "spec",
                          "--smeta", str(SMETA), "--object-name", "Объект",
                          "--out", str(tmp_path)], capture_output=True, text=True,
                         timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    spec = json.loads((tmp_path / "aosr_spec.json").read_text(encoding="utf-8"))
    assert spec["work_types"] and spec["norms"]


def test_cli_legacy_form_warns(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "aosr",
                          "--spec", str(AOSR_SPEC), "--form", "rd-11-02-2006",
                          "--out", str(tmp_path)], capture_output=True, text=True,
                         timeout=180, cwd=ROOT)
    assert "не применяется" in res.stderr


def test_mcp_validate_and_requirements():
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx
    ctx = make_ctx(str(ROOT))
    spec = _spec()
    spec["norms"] = ["СП 70.13330.2012"]
    res = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "asbuilt_docs",
                                 "arguments": {"kind": "validate", "spec": spec}}}, ctx)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["can_transfer"] is False
    assert "norm_without_clause" in {i["code"] for i in payload["blocking"]}

    req = handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "asbuilt_docs",
                                 "arguments": {"kind": "requirements",
                                               "work_types": ["electrical"]}}}, ctx)
    payload = json.loads(req["result"]["content"][0]["text"])
    assert payload["ok"] is True and "узо" in " ".join(payload["protocols"]).lower()
