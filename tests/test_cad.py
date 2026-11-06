# -*- coding: utf-8 -*-
"""Тесты CAD-модуля: DXF headless, шаблон, белый список операций, HITL, takeoff, MCP."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.audit import AuditLog                          # noqa: E402
from pto_agent.cad.diff import diff_drawings                  # noqa: E402
from pto_agent.cad.drawing import Drawing, DwgNotSupported    # noqa: E402
from pto_agent.cad.mcp_server import TOOLS, handle_rpc, make_ctx  # noqa: E402
from pto_agent.cad.ops import apply_operations, validate_operations  # noqa: E402
from pto_agent.cad.redline import plan_from_discrepancies     # noqa: E402
from pto_agent.cad.takeoff import compare_takeoff, takeoff    # noqa: E402
from pto_agent.cad.template import load_template, validate_drawing  # noqa: E402

DXF_V1 = ROOT / "drawings" / "korpus2_plan_v1.dxf"
TEMPLATE = ROOT / "config" / "cad_template_corp_A.yaml"
SUMMARY = ROOT / "out" / "summary.json"


@pytest.fixture(scope="module")
def tpl():
    return load_template(TEMPLATE)


@pytest.fixture()
def drawing(tmp_path):
    """Копия демо-чертежа в tmp: тесты не меняют исходники."""
    target = tmp_path / "korpus2_plan_v1.dxf"
    shutil.copy(DXF_V1, target)
    return Drawing.open(target)


@pytest.fixture(scope="module")
def summary():
    if not SUMMARY.exists():
        pytest.skip("нет сводки ПТО: python3 -m pto_agent run --data data/demo --out out")
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


# ── 1. Чтение и инвентаризация ────────────────────────────────────────────
def test_inventory_headless(drawing):
    inv = drawing.inventory()
    assert inv["units"] == "mm"
    assert inv["entities_total"] == 31
    assert "ПЛ_ФУНДАМЕНТ" in inv["entities_by_layer"]
    assert inv["text_heights"]["min"] == 200.0
    assert inv["extents"]["max"][0] > 80000


def test_titleblock_detected(drawing, tpl):
    tb = drawing.titleblock(tpl.titleblock_block)
    assert tb and tb["block"] == "ШТАМП_КОРП_A"
    assert tb["fields"]["ОБЪЕКТ"].startswith("ЖК «Северный»")
    assert tb["fields"]["ШИФР"] == ""          # заложенный дефект


def test_dwg_is_rejected_with_instruction(tmp_path):
    dwg = tmp_path / "plan.dwg"
    dwg.write_bytes(b"AC1032" + b"\x00" * 64)
    with pytest.raises(DwgNotSupported) as exc:
        Drawing.open(dwg)
    assert "DXF" in str(exc.value) and "ODA" in str(exc.value)


# ── 2. Проверка по корпоративному шаблону ─────────────────────────────────
def test_template_validation_finds_all_planted_defects(drawing, tpl):
    res = validate_drawing(drawing, tpl)
    codes = {i["code"] for i in res["issues"]}
    assert {"OBJECTS_ON_PROTECTED_LAYER", "TEXT_HEIGHT_BELOW_MIN",
            "TITLEBLOCK_INCOMPLETE", "UNKNOWN_LAYER", "UNKNOWN_BLOCK",
            "UNKNOWN_TEXT_STYLE"} <= codes
    assert res["ok"] is False and res["blocking"] == 3


def test_text_height_uses_print_scale(tpl):
    # 200 мм в модели при 1:100 — это 2,0 мм на бумаге, меньше нормы 2,5
    assert tpl.scale_factor == 100
    assert tpl.print_mm(200) == 2.0
    assert tpl.print_mm(250) == 2.5


def test_block_not_in_template_is_warning_only(drawing, tpl):
    res = validate_drawing(drawing, tpl)
    warn = [i for i in res["issues"] if i["code"] == "UNKNOWN_BLOCK"]
    assert warn and warn[0]["severity"] == "warning"


# ── 3. Белый список операций ──────────────────────────────────────────────
def _op(type_, **params):
    return {"op_id": "op-test", "type": type_, "params": params,
            "reason": "тест", "source": {"type": "test", "ref": "test#1", "confidence": 0.9}}


def test_delete_operation_is_forbidden(drawing, tpl):
    issues = validate_operations(drawing.doc, [_op("DELETE_ENTITY", handle="F2")], tpl)
    assert any(i["code"] == "OP_NOT_ALLOWED" and i["severity"] == "blocking" for i in issues)


def test_operation_on_protected_layer_blocked(drawing, tpl):
    issues = validate_operations(
        drawing.doc, [_op("ADD_TEXT", point=[0, 0], text="тест", layer="ОС_ШТАМП")], tpl)
    assert any(i["code"] == "PROTECTED_LAYER" for i in issues)


def test_text_height_validation_on_operation(drawing, tpl):
    issues = validate_operations(
        drawing.doc, [_op("ADD_TEXT", point=[0, 0], text="мелко", height=200, layer="ПОМЕТКИ")], tpl)
    assert any(i["code"] == "TEXT_HEIGHT" for i in issues)
    ok = validate_operations(
        drawing.doc, [_op("ADD_TEXT", point=[0, 0], text="норм", height=250, layer="ПОМЕТКИ")], tpl)
    assert not [i for i in ok if i["severity"] == "blocking"]


def test_human_only_titleblock_field_blocked(drawing, tpl):
    tb = drawing.titleblock(tpl.titleblock_block)
    op = _op("SET_ATTRIBUTE", handle=tb["handle"], tag="ФИО", value="Петров П.П.")
    issues = validate_operations(drawing.doc, [op], tpl)
    assert any(i["code"] == "HUMAN_ONLY_FIELD" for i in issues)


def test_low_confidence_operation_needs_human(drawing, tpl):
    op = _op("ADD_CLOUD", entity_handle="F2")
    op["source"]["confidence"] = 0.42
    issues = validate_operations(drawing.doc, [op], tpl)
    assert any(i["code"] == "LOW_CONFIDENCE" for i in issues)


# ── 4. dry_run не пишет файл, commit требует человека ─────────────────────
def _plan(summary, drawing, tpl):
    return plan_from_discrepancies(summary, drawing, tpl)["operations"]


def test_dry_run_does_not_touch_file(drawing, tpl, summary):
    before_hash = drawing.sha256
    ops = _plan(summary, drawing, tpl)
    res = apply_operations(drawing, ops, tpl, mode="dry_run")
    assert res["ok"] and res["written"] is False
    assert res["applied"] == len(ops)
    assert res["diff"]["added"] == len(ops)
    assert drawing.sha256 == before_hash
    assert not (drawing.path.parent / "korpus2_plan_v2.dxf").exists()


def test_commit_without_human_confirmation_is_refused(drawing, tpl, summary):
    ops = _plan(summary, drawing, tpl)
    res = apply_operations(drawing, ops, tpl, mode="commit")
    assert res["ok"] is False
    assert any(i["code"] == "NO_HUMAN_CONFIRMATION" for i in res["issues"])


def test_commit_writes_new_version_with_audit(drawing, tpl, summary, tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    ops = _plan(summary, drawing, tpl)
    res = apply_operations(drawing, ops, tpl, mode="commit",
                           approval={"by": "Иванов И.И.", "role": "инженер ПТО"},
                           audit=audit, actor="Иванов И.И.")
    assert res["ok"] and res["written"]
    out = Path(res["file"])
    assert out.name.endswith("_v2.dxf") and out.exists()
    assert res["objects_before"] == 31
    assert res["objects_after"] == 31 + len(ops)
    assert res["objects_lost"] == 0
    ok, count, msg = audit.verify()
    assert ok and count == 1 and msg == "цепочка целостна"
    # новая версия читается и содержит пометки на слое ПОМЕТКИ
    reopened = Drawing.open(out)
    assert reopened.inventory()["entities_by_layer"].get("ПОМЕТКИ") == len(ops)


def test_diff_detects_only_added_markup(drawing, tpl, summary):
    dry = apply_operations(drawing, _plan(summary, drawing, tpl), tpl, mode="dry_run")
    assert dry["diff"]["added"] == 9
    # подтверждение — от человека: строка «тест» теперь обоснованно отклоняется
    # (см. tests/test_review_fixes.py::test_machine_or_empty_identity_is_rejected)
    new_doc = apply_operations(drawing, _plan(summary, drawing, tpl), tpl, mode="commit",
                               approval={"by": "Иванов И.И.", "role": "инженер ПТО"})
    assert new_doc["ok"]
    # после commit исходный файл на диске не изменился — сравниваем его с новой версией
    d = diff_drawings(Drawing.open(drawing.path).doc, Drawing.open(new_doc["file"]).doc)
    assert d["removed"] == 0
    assert set(d["added_by_layer"]) == {"ПОМЕТКИ"}


# ── 5. Пометки по расхождениям ────────────────────────────────────────────
def test_redline_plan_marks_all_positions_and_defers_unknown(drawing, tpl, summary):
    plan = plan_from_discrepancies(summary, drawing, tpl)
    ops = plan["operations"]
    assert plan["summary"]["operations"] == len(ops) == 9
    # балки отсутствуют на чертеже → честно уходят человеку, а не «притягиваются»
    deferred = {(n["type"], n["code"]) for n in plan["needs_human_decision"]}
    assert ("VOLUME_OVER_ESTIMATE", "ФЕР 06-01-001-02") in deferred
    for op in ops:
        assert op["source"]["confidence"] >= 0.7
        assert op["source"]["ref"]
        assert op["requires_confirmation"] is True
    # на один объект — одно облако
    clouds = [o for o in ops if o["type"] == "ADD_CLOUD"]
    assert len({c["params"]["entity_handle"] for c in clouds}) == len(clouds) == 4


# ── 6. Сверка объёмов чертежа с актом ────────────────────────────────────
def test_takeoff_extracts_attributes(drawing):
    items = takeoff(drawing)
    assert len(items) == 6
    codes = {i["code"] for i in items}
    assert "ФЕР 06-01-001-01" in codes and None in codes      # у отмостки нет шифра


def test_takeoff_comparison_flags_differences(drawing, summary):
    res = compare_takeoff(takeoff(drawing), summary["lines"], pct=5)
    types = {i["type"] for i in res["issues"]}
    assert "CAD_QTY_MISMATCH" in types          # 440 м на схеме против 480 м в акте
    assert "CAD_UNIT_MISMATCH" in types         # стяжка: м3 на схеме, м² в смете
    assert "CAD_POSITION_NOT_IN_ACT" in types   # отмостка
    assert "ACT_POSITION_NOT_IN_CAD" in types   # балки
    pipes = next(m for m in res["matched"] if m["code"] == "ФЕР 16-02-003-01")
    assert pipes["delta_pct"] == pytest.approx(-8.33, abs=0.05)


# ── 7. MCP-сервер ─────────────────────────────────────────────────────────
def test_mcp_exposes_spec_tools():
    names = {t["name"] for t in TOOLS}
    assert {"get_drawing_info", "analyze_redline", "validate_operations",
            "apply_operations", "export_scheme"} <= names
    for t in TOOLS:
        assert t["inputSchema"]["type"] == "object"


def test_mcp_handshake_and_tools_list():
    ctx = make_ctx(ROOT)
    init = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {}}, ctx)
    assert init["result"]["protocolVersion"]
    assert init["result"]["capabilities"]["tools"]
    lst = handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ctx)
    assert len(lst["result"]["tools"]) == len(TOOLS)


def test_mcp_unknown_tool_returns_error():
    ctx = make_ctx(ROOT)
    res = handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "drop_database", "arguments": {}}}, ctx)
    assert "error" in res and res["error"]["code"] == -32602


def test_mcp_commit_requires_confirmation(drawing, tpl, summary, tmp_path):
    ctx = make_ctx(tmp_path)
    ops = _plan(summary, drawing, tpl)
    res = handle_rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "apply_operations", "arguments": {
                          "file_path": str(drawing.path), "operations": ops,
                          "mode": "commit", "out_path": str(tmp_path / "v2.dxf"),
                          "template_path": str(TEMPLATE)}}}, ctx)
    assert res["result"]["isError"] is True
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["ok"] is False
    assert not (tmp_path / "v2.dxf").exists()


def test_mcp_dry_run_via_rpc(drawing, tpl, summary, tmp_path):
    ctx = make_ctx(tmp_path)
    ops = _plan(summary, drawing, tpl)
    res = handle_rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                      "params": {"name": "apply_operations", "arguments": {
                          "file_path": str(drawing.path), "operations": ops,
                          "mode": "dry_run", "template_path": str(TEMPLATE)}}}, ctx)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["ok"] and payload["diff"]["added"] == len(ops)
    assert payload["written"] is False


def test_mcp_stdio_process_smoke():
    """Реальный запуск MCP-сервера по stdio: initialize + tools/list."""
    reqs = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"
    proc = subprocess.run([sys.executable, "-m", "pto_agent", "mcp"],
                          input=reqs, capture_output=True, text=True, timeout=120, cwd=ROOT)
    lines = [json.loads(x) for x in proc.stdout.strip().splitlines() if x.strip()]
    assert lines[0]["result"]["serverInfo"]["name"] == "pto-cad-agent"
    assert len(lines[1]["result"]["tools"]) >= 5
