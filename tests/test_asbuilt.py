# -*- coding: utf-8 -*-
"""Исполнительная документация: схема, АОСР, реестр ИД и комплектность.

Проверяем не «файл создался», а то, что документ можно предъявить: есть ли в нём лист и
ведомость объёмов, не подписывается ли акт с дырами, ловится ли отсутствующий документ
в комплекте и не приписывается ли чужой акт.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.asbuilt import (aosr_to_markdown, build_aosr, build_registry, build_scheme,
                               check_completeness, registry_to_markdown, scheme_to_markdown)
from pto_agent.asbuilt.registry import _kind_of

DXF = ROOT / "drawings" / "korpus2_plan_v1.dxf"
AOSR_SPEC = ROOT / "data" / "demo" / "asbuilt" / "aosr_ks38.json"
REGISTRY_SPEC = ROOT / "data" / "demo" / "asbuilt" / "registry.json"

needs_dxf = pytest.mark.skipif(not DXF.exists(), reason="нет демо-чертежа")
needs_specs = pytest.mark.skipif(not (AOSR_SPEC.exists() and REGISTRY_SPEC.exists()),
                                 reason="нет демо-спецификаций ИД")


def _docx_text(path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


# ─────────────────────────── исполнительная схема ────────────────────────
@needs_dxf
def test_scheme_builds_docx_and_sheet(tmp_path):
    res = build_scheme(DXF, tmp_path, object_name="ЖК «Северный»", work="Плита фундаментная",
                       code="ИС-01-26", act_ref="КС-2 №38", executor="Иванов И.И.",
                       reviewer="Петров П.П.", dpi=80)
    assert Path(res["docx"]).exists()
    assert Path(res["png"]).exists()
    assert res["positions"] == 6 and res["volumes_estimated"] == 6
    assert res["missing_fields"] == []
    text = _docx_text(res["docx"])
    for needle in ("ИСПОЛНИТЕЛЬНАЯ СХЕМА", "Ведомость объёмов работ", "ИС-01-26",
                   "КС-2 №38", "ФЕР 06-01-001-01", "Разработал", "Проверил"):
        assert needle in text, needle


@needs_dxf
def test_scheme_embeds_sheet_image(tmp_path):
    res = build_scheme(DXF, tmp_path, object_name="Объект", work="Работы", dpi=70)
    doc = Document(res["docx"])
    assert len(doc.inline_shapes) == 1          # лист вставлен, а не только подпись


@needs_dxf
def test_scheme_without_image(tmp_path):
    res = build_scheme(DXF, tmp_path, object_name="Объект", work="Работы", with_image=False)
    assert res["png"] is None
    assert len(Document(res["docx"]).inline_shapes) == 0


@needs_dxf
def test_scheme_reports_missing_fields(tmp_path):
    res = build_scheme(DXF, tmp_path, object_name="Объект", work="Работы")
    assert set(res["missing_fields"]) == {"шифр схемы", "акт КС-2", "исполнитель", "проверил"}
    md = scheme_to_markdown(res)
    assert "подписывать нельзя" in md
    assert "—" in _docx_text(res["docx"])       # нет данных → прочерк, а не выдумка


# ─────────────────────────────── АОСР ───────────────────────────────────
@needs_specs
def test_aosr_ready_to_sign_from_demo_spec(tmp_path):
    spec = json.loads(AOSR_SPEC.read_text(encoding="utf-8"))
    res = build_aosr(spec, tmp_path)
    assert res["ready_to_sign"] is True
    assert res["attachments"] == 4
    assert res["missing_fields"] == []
    text = _docx_text(res["docx"])
    for needle in ("АКТ", "освидетельствования скрытых работ", "№ 114",
                   "1. Представители сторон", "2. Исполнительная документация",
                   "3. Работы, материалы и нормативные документы",
                   "СП 70.13330.2012", "ИС-01-26", "Подписи сторон"):
        assert needle in text, needle


@needs_specs
def test_aosr_without_representatives_is_not_signable(tmp_path):
    spec = json.loads(AOSR_SPEC.read_text(encoding="utf-8"))
    spec["representatives"].pop("customer")
    res = build_aosr(spec, tmp_path)
    assert res["ready_to_sign"] is False
    assert any("представитель: customer" in m for m in res["missing_fields"])
    assert "подписывать нельзя" in aosr_to_markdown(res)


@needs_specs
def test_aosr_without_attachments_is_not_signable(tmp_path):
    spec = json.loads(AOSR_SPEC.read_text(encoding="utf-8"))
    spec["attachments"] = []
    res = build_aosr(spec, tmp_path)
    assert res["ready_to_sign"] is False
    assert any("перечень ИД" in m for m in res["missing_fields"])


def test_aosr_minimal_spec_reports_all_gaps(tmp_path):
    res = build_aosr({"act_number": "7"}, tmp_path)
    assert res["ready_to_sign"] is False
    joined = " ".join(res["missing_fields"])
    for field in ("object_name", "work", "start_date", "end_date", "перечень ИД"):
        assert field in joined


# ──────────────────────────── реестр и комплектность ─────────────────────
@needs_specs
def test_registry_builds_xlsx_with_kinds(tmp_path):
    spec = json.loads(REGISTRY_SPEC.read_text(encoding="utf-8"))
    res = build_registry(spec, tmp_path)
    assert Path(res["xlsx"]).exists()
    assert res["count"] == 5 and res["sheets"] == 56
    assert res["by_kind"] == {"аоср": 1, "схема": 1, "сертификат": 1, "протокол": 1, "журнал": 1}
    assert res["completeness"]["complete"] is True
    md = registry_to_markdown(res)
    assert "комплект полный" in md and "АОСР-114" in md


def test_completeness_detects_missing_kind():
    spec = {"documents": [{"name": "АОСР №1", "kind": "аоср", "status": "подписан"},
                          {"name": "Схема", "kind": "схема", "status": "проверен"}]}
    comp = check_completeness(spec)
    assert comp["complete"] is False
    kinds = {m["kind"] for m in comp["missing"]}
    assert {"сертификат", "протокол", "журнал"} <= kinds
    assert "не хватает" in comp["verdict"]


def test_completeness_detects_draft_status():
    spec = {"documents": [{"name": "АОСР №1", "kind": "аоср", "status": "черновик"}]}
    comp = check_completeness(spec, required=["аоср"])
    assert comp["complete"] is False
    assert comp["bad_status"]
    assert comp["not_checked"]


def test_completeness_detects_foreign_act():
    spec = {"documents": [{"name": "Схема", "kind": "схема", "status": "проверен",
                           "act_ref": "КС-2 №37"}]}
    comp = check_completeness(spec, required=["схема"], act_ref="КС-2 №38")
    assert comp["complete"] is False
    assert comp["foreign_act"][0]["act_ref"] == "КС-2 №37"
    assert "другого акта" in comp["verdict"]


def test_kind_is_inferred_from_name():
    assert _kind_of({"name": "Акт освидетельствования скрытых работ №5"}) == "аоср"
    assert _kind_of({"name": "Исполнительная схема фундамента"}) == "схема"
    assert _kind_of({"name": "Протокол испытаний бетона"}) == "протокол"
    assert _kind_of({"name": "Сертификат соответствия"}) == "сертификат"
    assert _kind_of({"name": "Общий журнал работ"}) == "журнал"
    assert _kind_of({"name": "Что-то ещё"}) == "прочее"


# ───────────────────────────────── CLI ─────────────────────────────────
@needs_dxf
def test_cli_scheme_and_check(tmp_path):
    out = tmp_path / "asbuilt"
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "scheme",
                          "--file", str(DXF), "--object-name", "Объект", "--work", "Работы",
                          "--code", "ИС-9", "--act-ref", "КС-2 №38", "--executor", "Иванов",
                          "--reviewer", "Петров", "--dpi", "60", "--out", str(out)],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr
    assert (out / "scheme_ИС-9.docx").exists() and (out / "scheme.md").exists()


@needs_specs
def test_cli_aosr_and_registry_exit_codes(tmp_path):
    a = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "aosr",
                        "--spec", str(AOSR_SPEC), "--out", str(tmp_path)],
                       capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert a.returncode == 0 and "готов к подписанию" in a.stdout
    r = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "registry",
                        "--spec", str(REGISTRY_SPEC), "--out", str(tmp_path)],
                       capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert r.returncode == 0 and "комплект полный" in r.stdout


def test_cli_check_reports_gap_in_exit_code(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"documents": [{"name": "Только АОСР", "kind": "аоср",
                                               "status": "подписан"}]}), encoding="utf-8")
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "check",
                          "--spec", str(spec)], capture_output=True, text=True, timeout=120,
                         cwd=ROOT)
    assert res.returncode == 1
    out = json.loads(res.stdout)
    assert out["complete"] is False and out["missing"]


@needs_specs
def test_cli_missing_spec_file(tmp_path):
    res = subprocess.run([sys.executable, "-m", "pto_agent", "asbuilt", "aosr",
                          "--spec", str(tmp_path / "нет.json"), "--out", str(tmp_path)],
                         capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert res.returncode == 2
    assert "нет файла спецификации" in res.stderr


# ───────────────────────────────── MCP ─────────────────────────────────
def test_mcp_asbuilt_docs_check_and_listing():
    from pto_agent.cad.mcp_server import TOOLS, handle_rpc, make_ctx
    assert "asbuilt_docs" in {t["name"] for t in TOOLS}
    ctx = make_ctx(str(ROOT))
    res = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "asbuilt_docs",
                                 "arguments": {"kind": "check", "spec": {"documents": [
                                     {"name": "Схема", "kind": "схема", "status": "черновик"}]}}}},
                     ctx)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["ok"] is True and payload["complete"] is False
    assert {m["kind"] for m in payload["missing"]} == {"аоср", "сертификат", "протокол", "журнал"}


@needs_specs
def test_mcp_asbuilt_docs_registry_markdown(tmp_path):
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx
    ctx = make_ctx(str(ROOT))
    res = handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "asbuilt_docs",
                                 "arguments": {"kind": "registry",
                                               "spec_path": str(REGISTRY_SPEC),
                                               "out_dir": str(tmp_path),
                                               "format": "markdown"}}}, ctx)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["complete"] is True
    assert "комплект полный" in payload["markdown"]
    assert (tmp_path / "registry_id.xlsx").exists()


def test_mcp_asbuilt_docs_rejects_unknown_kind():
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx
    ctx = make_ctx(str(ROOT))
    res = handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "asbuilt_docs",
                                 "arguments": {"kind": "нет-такого"}}}, ctx)
    assert "error" in res or "isError" in str(res)
