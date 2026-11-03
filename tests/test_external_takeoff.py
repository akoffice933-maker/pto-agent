# -*- coding: utf-8 -*-
"""Внешний обмер по MCP: клиент, нормализация, сверка, лицензионный режим.

Все тесты офлайновые: вместо opentakeoff/estimaite поднимаются фейковые MCP-серверы
(stdio-процесс и http-сервер в потоке), а совместимость протокола проверяется на нашем же
`pto_agent.mcp_server` — если чужая схема изменится, упадёт именно этот тест.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.cad.drawing import Drawing                        # noqa: E402
from pto_agent.cad.external_takeoff import (                      # noqa: E402
    ExternalLicenseError, ExternalTakeoffError, MCPClient, align, load_config, normalize,
    payload_from_result, run_external_takeoff, validate_config)
from pto_agent.cad.external_takeoff import local_positions_from_dxf  # noqa: E402

DXF = ROOT / "drawings" / "korpus2_plan_v1.dxf"

FAKE_SERVER = r'''
import json, os, sys
MODE = os.environ.get("FAKE_MODE", "ok")
PAYLOAD = {"items": [
    {"cost_code": "ФЕР 06-01-001-01", "description": "Плита фундаментная B25",
     "quantity": "1 240", "uom": "m3"},
    {"cost_code": "ФЕР 08-02-001-01", "description": "Гидроизоляция обмазочная",
     "quantity": 1180, "unit": "м2"},
    {"description": "Устройство отмостки вокруг здания", "unit": "м2"},
]}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    rid = req.get("id")
    if rid is None:            # нотификация: initialize/initialized и т.п.
        continue
    method = req.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-takeoff", "version": "0.0.1"}}}), flush=True)
    elif method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "takeoff", "description": "PDF takeoff", "inputSchema": {"type": "object"}},
            {"name": "ping_me", "description": "test", "inputSchema": {"type": "object"}}]}}),
            flush=True)
    elif method == "tools/call":
        if MODE == "rpc_error":
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601, "message": "нет метода"}}), flush=True)
            continue
        if MODE == "tool_error":
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "сервер не смог прочитать PDF"}],
                "isError": True}}), flush=True)
            continue
        args = (req.get("params") or {}).get("arguments") or {}
        payload = dict(PAYLOAD, echo=args)
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}},
            ensure_ascii=False), flush=True)
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": rid,
                          "error": {"code": -32601, "message": "unknown method"}}), flush=True)
'''


@pytest.fixture()
def fake_server(tmp_path):
    script = tmp_path / "fake_takeoff_mcp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return script


def cfg_stdio(script, **kw):
    base = {"name": "fake-takeoff", "transport": "stdio",
            "command": [sys.executable, str(script)], "tool": "takeoff", "license": "MIT"}
    base.update(kw)
    return base


# ─────────────────────────── клиент: stdio ───────────────────────────────
def test_stdio_initialize_and_list_tools(fake_server):
    with MCPClient("stdio", command=[sys.executable, str(fake_server)]) as c:
        init = c.initialize()
        assert init["protocolVersion"] == "2025-06-18"
        assert c.server_info["name"] == "fake-takeoff"
        assert [t["name"] for t in c.list_tools()] == ["takeoff", "ping_me"]


def test_stdio_call_tool_parses_payload(fake_server):
    with MCPClient("stdio", command=[sys.executable, str(fake_server)]) as c:
        c.initialize()
        res = c.call_tool("takeoff", {"pdf_path": "/tmp/x.pdf"})
        payload = payload_from_result(res)
        assert len(payload["items"]) == 3
        assert payload["echo"]["pdf_path"] == "/tmp/x.pdf"


def test_rpc_error_becomes_external_error(fake_server, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "rpc_error")
    with MCPClient("stdio", command=[sys.executable, str(fake_server)]) as c:
        c.initialize()
        with pytest.raises(ExternalTakeoffError) as e:
            c.call_tool("takeoff", {})
        assert "код -32601" in str(e.value)


def test_tool_error_flag_is_not_silently_ignored(fake_server, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "tool_error")
    with MCPClient("stdio", command=[sys.executable, str(fake_server)]) as c:
        c.initialize()
        with pytest.raises(ExternalTakeoffError) as e:
            c.call_tool("takeoff", {})
        assert "не смог прочитать PDF" in str(e.value)


# ─────────────────────────── клиент: http ────────────────────────────────
@pytest.fixture()
def http_server():
    state = {"sse": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            rid, method = req.get("id"), req.get("method")
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {},
                          "serverInfo": {"name": "fake-http", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "takeoff", "inputSchema": {"type": "object"}}]}
            elif method == "tools/call":
                body = json.dumps({"items": [{"code": "A1", "name": "Работа", "qty": 10,
                                              "unit": "м3"}]}, ensure_ascii=False)
                result = {"content": [{"type": "text", "text": body}]}
            else:
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result},
                                 ensure_ascii=False)
            if state["sse"]:
                body = f"event: message\ndata: {payload}\n\n".encode()
                ctype = "text/event-stream"
            else:
                body = payload.encode()
                ctype = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/mcp", state
    finally:
        srv.shutdown()


def test_http_transport_json(http_server):
    url, _ = http_server
    with MCPClient("http", url=url, timeout=10) as c:
        assert c.initialize()["protocolVersion"] == "2025-06-18"
        assert [t["name"] for t in c.list_tools()] == ["takeoff"]
        payload = payload_from_result(c.call_tool("takeoff", {"pdf_path": "x.pdf"}))
        assert payload["items"][0]["code"] == "A1"


def test_http_transport_sse(http_server):
    url, state = http_server
    state["sse"] = True
    with MCPClient("http", url=url, timeout=10) as c:
        c.initialize()
        payload = payload_from_result(c.call_tool("takeoff", {}))
        assert payload["items"][0]["qty"] == 10


# ───────────────────── нормализация чужой схемы ──────────────────────────
def test_normalize_different_shapes():
    """Чужой сервер может отдать items / positions / lines / голый список."""
    item = {"cost_code": "ФЕР 06-01-001-01", "description": "Плита", "quantity": "1 240",
            "uom": "m3"}
    for payload in ([item], {"items": [item]}, {"positions": [item]},
                    {"data": {"lines": [item]}}, {"takeoff": [item]}):
        result = normalize(payload)
        assert result["count"] == 1, payload
        p = result["positions"][0]
        assert p["code"] == "ФЕР 06-01-001-01"
        assert p["name"] == "Плита"
        assert p["qty"] == 1240.0
        assert p["unit"] == "м3"


def test_unestimated_qty_is_none_not_zero():
    """Позиция без объёма остаётся «не оценено», а не 0."""
    result = normalize({"items": [{"code": "A1", "name": "Отмостка", "unit": "м2"},
                                  {"code": "A2", "name": "Стяжка", "qty": 0, "unit": "м3"}]})
    assert [p["qty"] for p in result["positions"]] == [None, None]
    assert result["not_estimated"] == 2 and result["estimated"] == 0
    assert [w["type"] for w in result["warnings"]].count("QTY_NOT_ESTIMATED") == 2


def test_noise_items_are_reported_not_dropped_silently():
    result = normalize({"items": [{"code": "A1", "name": "Работа", "qty": 5, "unit": "шт"},
                                  {"note": "итого"}, "строка", {"name": "Без шифра",
                                                                 "qty": 3, "unit": "м"}]})
    assert result["count"] == 2                       # «итого» и строка отброшены, но с записью
    types = {w["type"] for w in result["warnings"]}
    assert {"ITEM_WITHOUT_CODE_AND_NAME", "ITEM_NOT_OBJECT"} <= types


def test_units_are_normalized_to_our_scheme():
    result = normalize({"items": [{"code": "A1", "name": "Кровля", "qty": 900, "unit": "м²"},
                                  {"code": "A2", "name": "Труба", "qty": 440, "unit": "пог.м"}]})
    assert result["positions"][0]["unit"] == "м2"
    assert result["positions"][1]["unit"] == "м"


# ─────────────────────── сверка с нашим обмером ──────────────────────────
def test_align_matches_by_code_and_finds_deltas():
    external = normalize({"items": [
        {"code": "A1", "name": "Плита", "qty": 100, "unit": "м3"},
        {"code": "A2", "name": "Стяжка", "qty": 50, "unit": "м3"},
        {"code": "A9", "name": "Лишняя работа", "qty": 7, "unit": "шт"},
    ]})["positions"]
    local = [{"code": "A1", "name": "Плита", "qty": 100, "unit": "м3"},
             {"code": "A2", "name": "Стяжка", "qty": 80, "unit": "м3"},
             {"code": "A3", "name": "Наша работа", "qty": 12, "unit": "шт"}]
    result = align(external, local, pct=5.0)
    types = [i["type"] for i in result["issues"]]
    assert "QTY_DELTA" in types          # 50 против 80 = −37,5 %
    assert "EXTERNAL_ONLY" in types      # A9
    assert "LOCAL_ONLY" in types         # A3
    assert len(result["matched"]) == 2
    delta = next(i for i in result["issues"] if i["type"] == "QTY_DELTA")
    assert delta["severity"] == "critical" and delta["delta_pct"] == -37.5


def test_align_reports_unit_mismatch_and_unestimated():
    external = normalize({"items": [
        {"code": "A1", "name": "Плита", "qty": 100, "unit": "м3"},
        {"code": "A2", "name": "Стяжка", "unit": "м3"},
    ]})["positions"]
    local = [{"code": "A1", "name": "Плита", "qty": 100, "unit": "м2"},
             {"code": "A2", "name": "Стяжка", "qty": 80, "unit": "м3"}]
    types = [i["type"] for i in align(external, local)["issues"]]
    assert "UNIT_MISMATCH" in types
    assert "EXTERNAL_NOT_ESTIMATED" in types


# ─────────────────────── конфиг и лицензионный режим ─────────────────────
def test_agpl_requires_separate_service_ack(tmp_path, fake_server):
    cfg = cfg_stdio(fake_server, license="AGPL-3.0")
    with pytest.raises(ExternalLicenseError) as e:
        validate_config(cfg)
    assert "--ack-separate-service" in str(e.value)
    ok = validate_config(dict(cfg), ack_separate_service=True)
    assert ok["separate_service"] is True


def test_mit_config_needs_no_ack(fake_server):
    cfg = validate_config(cfg_stdio(fake_server, license="MIT"))
    assert cfg["separate_service"] is False


def test_config_validation_errors(fake_server):
    with pytest.raises(ExternalTakeoffError):
        validate_config({"name": "x", "transport": "stdio", "license": "MIT", "tool": "t"})
    with pytest.raises(ExternalTakeoffError):
        validate_config({"name": "x", "transport": "http", "license": "MIT", "tool": "t"})
    with pytest.raises(ExternalTakeoffError):
        validate_config({"name": "x", "transport": "carrier-pigeon", "license": "MIT",
                         "tool": "t"})
    with pytest.raises(ExternalTakeoffError):
        validate_config({"transport": "stdio", "license": "MIT", "tool": "t"})


def test_load_config_reads_yaml_and_json(tmp_path, fake_server):
    import yaml
    y = tmp_path / "cfg.yaml"
    y.write_text(yaml.safe_dump({"external_takeoff": cfg_stdio(fake_server)}), encoding="utf-8")
    assert load_config(y)["name"] == "fake-takeoff"
    j = tmp_path / "cfg.json"
    j.write_text(json.dumps(cfg_stdio(fake_server)), encoding="utf-8")
    assert load_config(j)["tool"] == "takeoff"


# ─────────────────────────── прогон целиком ──────────────────────────────
def test_run_external_takeoff_end_to_end(fake_server, tmp_path):
    pdf = tmp_path / "list.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    report = run_external_takeoff(cfg_stdio(fake_server), pdf, timeout=30)
    assert report["ok"] is True
    assert report["protocol"] == "2025-06-18"
    assert report["count"] == 3
    assert report["not_estimated"] == 1        # отмостка без объёма
    assert report["positions"][0]["qty"] == 1240.0

    from pto_agent.cad.external_takeoff import write_report
    local = [{"code": "ФЕР 06-01-001-01", "name": "Плита фундаментная B25",
              "qty": 1240, "unit": "м3"}]
    alignment = align(report["positions"], local)
    paths = write_report(report, alignment, tmp_path / "out")
    assert Path(paths["json"]).exists() and Path(paths["markdown"]).exists()
    saved = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert saved["alignment"]["external_positions"] == 3
    md = Path(paths["markdown"]).read_text(encoding="utf-8")
    assert "не оценено" in md


def test_run_refuses_missing_file(fake_server, tmp_path):
    with pytest.raises(ExternalTakeoffError):
        run_external_takeoff(cfg_stdio(fake_server), tmp_path / "нет.pdf")


def test_run_reports_unknown_tool(fake_server, tmp_path):
    pdf = tmp_path / "list.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(ExternalTakeoffError) as e:
        run_external_takeoff(cfg_stdio(fake_server, tool="measure_everything"), pdf, timeout=30)
    assert "нет инструмента" in str(e.value)


# ─────────────── совместимость с реальным сервером (наш протокол) ────────
def test_client_works_with_our_own_mcp_server():
    """Наш сервер — эталон протокола: если клиент сломался, ломается и это."""
    if not DXF.exists():
        pytest.skip("нет демо-чертежа")
    cfg = {"name": "pto-agent", "transport": "stdio", "license": "MIT", "tool": "cad_takeoff",
           "command": [sys.executable, "-m", "pto_agent.mcp_server", "--base-dir", "."],
           "arguments": {"file_path": "{pdf_path}"}}
    drawing = Drawing.open(DXF)
    local = local_positions_from_dxf(drawing)
    report = run_external_takeoff(cfg, DXF, timeout=120)
    assert report["protocol"] == "2025-06-18"
    assert report["count"] == len(local) == 6
    alignment = align(report["positions"], local)
    assert alignment["issues"] == []           # один и тот же обмер — расхождений нет


# ─────────────── инструмент MCP: агент сам вызывает внешний обмер ────────
def test_mcp_tool_external_takeoff(fake_server, tmp_path):
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx

    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg_stdio(fake_server, arguments={"pdf_path": "{pdf_path}"})),
                        encoding="utf-8")
    pdf = tmp_path / "list.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    ctx = make_ctx(tmp_path, tmp_path / "out" / "cad" / "audit.jsonl")
    req = {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
           "params": {"name": "external_takeoff", "arguments": {
               "config_path": str(cfg_path), "file_path": str(pdf),
               "drawing_path": str(DXF), "tolerance_pct": 5}}}
    res = handle_rpc(req, ctx)
    out = json.loads(res["result"]["content"][0]["text"])
    assert out["ok"] is True
    assert out["count"] == 3
    assert out["alignment"]["external_positions"] == 3
    # в нашем чертеже 6 позиций, внешний обмер подтвердил 3 → остальные попадают как LOCAL_ONLY
    assert any(i["type"] == "LOCAL_ONLY" for i in out["alignment"]["issues"])


def test_mcp_tool_agpl_without_ack_is_refused(fake_server, tmp_path):
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx

    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg_stdio(fake_server, license="AGPL-3.0")), encoding="utf-8")
    pdf = tmp_path / "list.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    ctx = make_ctx(tmp_path, tmp_path / "out" / "cad" / "audit.jsonl")
    req = {"jsonrpc": "2.0", "id": 22, "method": "tools/call",
           "params": {"name": "external_takeoff", "arguments": {
               "config_path": str(cfg_path), "file_path": str(pdf)}}}
    res = handle_rpc(req, ctx)
    assert res["result"]["isError"] is True
    assert "AGPL-3.0" in res["result"]["content"][0]["text"]
