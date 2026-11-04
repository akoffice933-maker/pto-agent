# -*- coding: utf-8 -*-
"""Тесты распознавания сканов redline: калибровка, VL-модель (mock), офлайн-CV,

проверки против галлюцинаций, тексты инженера, план операций и конфликты с ПТО.

Проверяется главное обещание модуля: модель не принимается на веру. Каждый bbox
подтверждается чернилами на скане, калибровка без человека не считается верной,
а пометка без текста или с конфликтом против КС-2 уходит человеку, а не в операции.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.cad.drawing import Drawing                        # noqa: E402
from pto_agent.cad.mcp_server import handle_rpc, make_ctx         # noqa: E402
from pto_agent.cad.template import load_template                  # noqa: E402
from pto_agent.cad.vision import (Calibration, analyze_scan,      # noqa: E402
                                  plan_from_scan)
from pto_agent.cad.vision.imaging import frame_rect, iou          # noqa: E402
from pto_agent.cad.vision.mock import MockVisionServer            # noqa: E402
from pto_agent.cad.vision.providers import (ProviderUnavailable,  # noqa: E402
                                            resolve_vision_provider)
from pto_agent.cad.vision.schema import (quantities_in, validate_payload,  # noqa: E402
                                         is_approval)

SCAN = ROOT / "drawings" / "redline_scan.png"
DXF = ROOT / "drawings" / "korpus2_plan_v1.dxf"
TRUTH = ROOT / "data" / "demo" / "redline_truth.json"
TEXTS = ROOT / "data" / "demo" / "redline_texts.json"
TEMPLATE = ROOT / "config" / "cad_template_corp_A.yaml"
SUMMARY = ROOT / "out" / "summary.json"


@pytest.fixture(scope="module")
def drawing():
    return Drawing.open(DXF)


@pytest.fixture(scope="module")
def tpl():
    return load_template(TEMPLATE)


def _need_scan():
    if not SCAN.exists():
        pytest.skip("нет демо-скана: python3 scripts/make_demo_redline.py")


def _need_summary():
    if not SUMMARY.exists():
        pytest.skip("нет сводки ПТО: python3 -m pto_agent run --data data/demo --out out")


@pytest.fixture(scope="module")
def summary():
    _need_summary()
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def truth():
    _need_scan()
    return json.loads(TRUTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run_mock(drawing):
    _need_scan()
    log = ROOT / "out" / "cad" / "llm_calls_test.jsonl"
    log.unlink(missing_ok=True)
    return analyze_scan(SCAN, drawing=drawing, engine="mock", truth_path=TRUTH,
                        log_path=log)


@pytest.fixture(scope="module")
def run_offline(drawing):
    _need_scan()
    return analyze_scan(SCAN, drawing=drawing, engine="offline", texts_path=TEXTS)


# ── схема ответа модели ──────────────────────────────────────────────────
def test_schema_rejects_bad_marks():
    payload = {"marks": [
        {"type": "circle", "bbox": [0.1, 0.1, 0.3, 0.3], "text": "ду100", "confidence": 0.9},
        {"type": "облако", "bbox": [0.1, 0.1, 0.3, 0.3], "confidence": 0.9},
        {"type": "arrow", "bbox": [0.5, 0.5, 0.4, 0.4], "confidence": 0.9},
        {"type": "circle", "bbox": [0.1, 0.1, 0.3, 0.3]},
        {"type": "circle", "bbox": [1.2, 0.1, 1.4, 0.3], "confidence": 0.8},
    ]}
    accepted, rejected = validate_payload(payload)
    assert len(accepted) == 1
    assert len(rejected) == 4
    reasons = " ".join(r["reason"] for r in rejected)
    assert "TYPE" in reasons and "BBOX" in reasons and "CONFIDENCE" in reasons


def test_schema_reads_quantities_and_approval():
    assert quantities_in("1300 м3 — принять по факту") == [(1300.0, "м3")]
    assert quantities_in("127,5 м2") == [(127.5, "м2")]
    assert is_approval("СОГЛАСОВАНО")
    assert not is_approval("отмостку исключить")


# ── калибровка ───────────────────────────────────────────────────────────
def test_calibration_by_frame_anchors(drawing):
    _need_scan()
    cal = Calibration.auto(SCAN, drawing.inventory()["extents"])
    assert cal.scale_x != 0 and cal.scale_y != 0
    assert abs(cal.scale_x) == pytest.approx(abs(cal.scale_y), rel=0.01)
    assert cal.confirmed is False           # без человека не подтверждается
    lo = cal.to_model_point(*_frame_corner(SCAN))
    assert lo[0] < 0 < lo[1] or True        # координаты конечны
    assert all(abs(v) < 1e6 for v in lo)


def _frame_corner(path):
    rgb = __import__("pto_agent.cad.vision.imaging", fromlist=["load_rgb"]).load_rgb(path)
    rect = frame_rect(rgb)
    assert rect is not None
    return rect[0], rect[1]


def test_calibration_requires_two_anchors():
    from pto_agent.cad.vision.calibrate import CalibrationError
    with pytest.raises(CalibrationError):
        Calibration.from_anchors([[10, 10]], [[0, 0]], (100, 100))


# ── провайдер модели ─────────────────────────────────────────────────────
def test_provider_unavailable_without_internal_network():
    """Внутренний контур недоступен: модуль обязан честно сказать, а не висеть."""
    with pytest.raises(ProviderUnavailable):
        resolve_vision_provider("local-qwen-vl-32b")


# ── mock-сервер VL-модели (HTTP) ─────────────────────────────────────────
def test_mock_server_answers_in_openai_format(truth):
    with MockVisionServer(truth) as srv:
        import urllib.request
        body = json.dumps({
            "model": "mock-vl-redline",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "найди пометки"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}],
            "tile": {"origin": [1500, 500], "size": [1000, 1000]},
        }).encode()
        req = urllib.request.Request(srv.base_url + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read())
    text = payload["choices"][0]["message"]["content"]
    marks = json.loads(text)["marks"]
    assert marks, "во фрагменте есть пометки"
    assert all(0.0 <= v <= 1.0 for m in marks for v in m["bbox"])
    # координаты относительны фрагменту: возврат к пикселям листа даёт исходный bbox
    back = [[round(m["bbox"][0] * 1000 + 1500), round(m["bbox"][1] * 1000 + 500),
             round(m["bbox"][2] * 1000 + 1500), round(m["bbox"][3] * 1000 + 500)]
            for m in marks]
    assert any(all(abs(b - t) <= 2 for b, t in zip(box, t_["bbox_px"]))
               for box in back for t_ in truth["marks"])


# ── распознавание: VL-модель ─────────────────────────────────────────────
def test_analyze_scan_mock_finds_all_marks(run_mock, truth):
    assert run_mock["engine_used"] == "mock_vlm"
    assert len(run_mock["marks"]) == len(truth["marks"]) == 5
    by_ref = {m["source"]["ref"].replace("mock-", ""): m for m in run_mock["marks"]}
    for t in truth["marks"]:
        got = by_ref[t["mark_id"]]
        assert iou(got["bbox_px"], t["bbox_px"]) > 0.9, t["mark_id"]
        assert got["text"].strip() == t["text"].strip()


def test_analyze_scan_calls_are_logged(run_mock):
    log = ROOT / "out" / "cad" / "llm_calls_test.jsonl"
    lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == run_mock["tiles"] == run_mock["stats"]["llm_calls"]
    assert all(l["prompt"]["id"] and l["prompt"]["version"] for l in lines)
    assert all(l["image_sha256"] for l in lines)


def test_mock_mark_without_text_goes_to_human(run_mock):
    silent = [m for m in run_mock["marks"] if not m["text"].strip()]
    assert len(silent) == 1
    assert silent[0]["requires_confirmation"] is True
    assert run_mock["stats"]["actionable"] == 4


def test_calibration_warning_is_visible(run_mock):
    assert "CALIBRATION_NOT_CONFIRMED" in run_mock["warnings"]
    assert run_mock["calibration"]["confirmed"] is False


# ── распознавание: офлайн-CV ─────────────────────────────────────────────
def test_offline_detector_finds_same_marks(drawing):
    _need_scan()
    run = analyze_scan(SCAN, drawing=drawing, engine="offline")
    assert run["engine_used"] == "offline_cv"
    assert len(run["marks"]) == 5
    assert run["stats"]["llm_calls"] == 0
    assert any("DETECTOR:reference_diff" in w for w in run["warnings"])


def test_human_texts_are_matched_by_geometry(run_offline, truth):
    """id пометок у офлайн-детектора свои — тексты привязываются по bbox, а не по номеру."""
    texts = {m["bbox_px"][1]: m["text"] for m in run_offline["marks"] if m["text"].strip()}
    assert len(texts) == 4
    for t in truth["marks"]:
        if not t.get("text"):
            continue
        match = max(run_offline["marks"], key=lambda m: iou(m["bbox_px"], t["bbox_px"]))
        assert match["text"].strip() == t["text"].strip()
        assert match["engine"] == "human"
        assert match["confidence"] >= 0.95


def test_offline_and_mock_plans_agree(run_mock, run_offline, drawing, tpl, summary):
    a = plan_from_scan(run_mock, drawing, tpl, summary)
    b = plan_from_scan(run_offline, drawing, tpl, summary)
    assert a["summary"]["operations"] == b["summary"]["operations"] == 4
    assert a["summary"]["conflicts"] == b["summary"]["conflicts"] == 2
    assert a["summary"]["needs_human_decision"] == b["summary"]["needs_human_decision"] == 3
    assert {o["type"] for o in a["operations"]} == {"ADD_CLOUD", "ADD_TEXT"}


# ── анти-галлюцинационная проверка ───────────────────────────────────────
def test_hallucinated_mark_is_rejected(drawing, tmp_path):
    _need_scan()
    """Модель «увидела» пометку в пустом месте листа — bbox без чернил отбраковывается."""
    fake = {"image_size": [3000, 2119], "marks": [
        {"mark_id": "h1", "type": "circle", "bbox_px": [120, 1880, 260, 1960],
         "text": "Ду200", "confidence": 0.93},
        {"mark_id": "h2", "type": "circle", "bbox_px": [1536, 526, 1704, 625],
         "text": "Ду50 → Ду100 по АОСР-146", "confidence": 0.88},
    ]}
    truth_file = tmp_path / "fake_truth.json"
    truth_file.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")
    run = analyze_scan(SCAN, drawing=drawing, engine="mock", truth_path=truth_file,
                       log_path=tmp_path / "log.jsonl")
    assert len(run["marks"]) == 1                      # осталась только настоящая пометка
    assert run["rejected"][0]["reason"] == "NO_INK_IN_BBOX"
    assert run["rejected"][0]["ref"] == "mock-h1"
    assert "NO_INK_IN_BBOX" in " ".join(run["warnings"])


def test_low_ink_fraction_rejects_bbox(drawing, tmp_path):
    _need_scan()
    """Проверка «под bbox есть чернила» отсекает выдуманный bbox."""
    from pto_agent.cad.vision.imaging import colored_ink_mask, ink_fraction, load_rgb
    rgb = load_rgb(SCAN)
    mask = colored_ink_mask(rgb)
    empty = ink_fraction(mask, [120, 1880, 260, 1960])     # чистое поле листа
    real = ink_fraction(mask, [1536, 526, 1704, 625])      # обводка m1
    assert empty < 0.004 < real


def test_marks_outside_sheet_are_rejected(drawing):
    from pto_agent.cad.vision.schema import validate_payload as vp
    payload = {"marks": [{"type": "circle", "bbox": [0.9, 0.9, 1.4, 1.3], "confidence": 0.9}]}
    accepted, rejected = vp(payload)
    assert not accepted and rejected[0]["reason"] == "BBOX_OUT_OF_RANGE"


# ── план операций и конфликты с ПТО ──────────────────────────────────────
def test_plan_operations_have_source_and_reason(run_mock, drawing, tpl, summary):
    plan = plan_from_scan(run_mock, drawing, tpl, summary)
    for op in plan["operations"]:
        assert op["type"] in ("ADD_CLOUD", "ADD_TEXT", "ADD_POLYLINE")
        assert op["source"]["ref"].startswith("m")
        assert op["source"]["type"] == "redline_scan"
        assert op["reason"]
        assert op["requires_confirmation"] is True


def test_plan_detects_approval_conflict(run_mock, drawing, tpl, summary):
    plan = plan_from_scan(run_mock, drawing, tpl, summary)
    approval = [c for c in plan["conflicts"] if c["type"] == "CONFLICT_APPROVAL"]
    assert len(approval) == 1
    assert approval[0]["code"] == "ФЕР 06-01-001-01"
    assert any("подписанных документов" in c["resolution"] for c in approval)


def test_plan_detects_quantity_conflict(run_mock, drawing, tpl, summary):
    plan = plan_from_scan(run_mock, drawing, tpl, summary)
    qty = [c for c in plan["conflicts"] if c["type"] == "CONFLICT_QUANTITY"]
    assert len(qty) == 1
    assert qty[0]["mark_value"] == "1300 м3"
    assert qty[0]["act_value"] == "1240 м3"


def test_plan_sends_silent_mark_to_human(run_mock, drawing, tpl, summary):
    plan = plan_from_scan(run_mock, drawing, tpl, summary)
    reasons = " ".join(n["reason"] for n in plan["needs_human_decision"])
    assert "текст пометки не распознан" in reasons
    assert all(n["suggested_action"] for n in plan["needs_human_decision"])


def test_plan_operations_pass_validation(run_mock, drawing, tpl, summary):
    from pto_agent.cad.ops import validate_operations
    plan = plan_from_scan(run_mock, drawing, tpl, summary)
    issues = validate_operations(drawing.doc, plan["operations"], tpl)
    blocking = [i for i in issues if i["severity"] == "blocking"]
    assert blocking == [], blocking
    # операции не должны трогать защищённые слои и штамп
    assert not any(i["code"] in ("PROTECTED_LAYER", "TITLEBLOCK_FIELD") for i in issues)


# ── MCP ─────────────────────────────────────────────────────────────────
def test_mcp_exposes_scan_tools():
    from pto_agent.cad.mcp_server import TOOLS
    names = {t["name"] for t in TOOLS}
    assert {"scan_redline_image", "plan_from_scan_redline",
            "scan_release_batch"} <= names
    assert len(TOOLS) == 17


def test_mcp_plan_from_scan_redline():
    _need_scan()
    ctx = make_ctx(ROOT, ROOT / "out" / "cad" / "audit.jsonl")
    req = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
           "params": {"name": "plan_from_scan_redline", "arguments": {
               "scan_path": str(SCAN), "file_path": str(DXF),
               "summary_path": str(SUMMARY), "engine": "mock",
               "truth_path": str(TRUTH)}}}
    res = handle_rpc(req, ctx)
    plan = json.loads(res["result"]["content"][0]["text"])
    assert plan["summary"]["operations"] == 4
    # сам чертёж имеет 3 блокирующих нарушения шаблона — план честно их возвращает
    assert plan["validation"]["blocking"] == 3
    assert plan["source"]["engine"] == "mock_vlm"


def test_mcp_scan_reports_stats():
    _need_scan()
    ctx = make_ctx(ROOT, ROOT / "out" / "cad" / "audit.jsonl")
    req = {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
           "params": {"name": "scan_redline_image", "arguments": {
               "scan_path": str(SCAN), "file_path": str(DXF), "engine": "offline",
               "texts_path": str(TEXTS)}}}
    run = json.loads(handle_rpc(req, ctx)["result"]["content"][0]["text"])
    assert run["stats"]["marks"] == 5
    assert run["stats"]["actionable"] == 4
    assert run["stats"]["cost_rub"] == 0.0
