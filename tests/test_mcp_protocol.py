# -*- coding: utf-8 -*-
"""MCP-обвязка: протокол, транспорты, схемы инструментов, установка в клиенты.

Проверяется то, что ломает интеграцию чаще всего:
  * stdio-транспорт отвечает на initialize / tools/list / tools/call и молчит,
    когда ответа быть не должно (нотификации);
  * stdout чистый — в него не попадает ничего, кроме JSON-RPC (иначе клиент
    разваливает соединение);
  * ошибки отдаются кодами JSON-RPC, а не падением процесса;
  * схемы всех инструментов валидны (object + properties + required ⊆ properties);
  * http-транспорт отдаёт /health, /tools и POST /mcp с CORS;
  * установщик пишет конфиг нужного клиента и не затирает чужие MCP-серверы.
"""
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.cad.mcp_server import PROTOCOL_VERSION, TOOLS, _tools_public  # noqa: E402
from pto_agent.mcp_server import build_argv, tool_names                     # noqa: E402

INSTALLER = ROOT / "scripts" / "install_mcp.py"


def _rpc_stdio(lines: list) -> list:
    """Один процесс, несколько запросов: так делает настоящий MCP-клиент."""
    payload = "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n"
    proc = subprocess.run([sys.executable, "-m", "pto_agent.mcp_server", "--base-dir", str(ROOT)],
                          input=payload, capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    out = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return out, proc


# ── stdio ───────────────────────────────────────────────────────────────
def test_stdio_handshake_and_tools():
    out, proc = _rpc_stdio([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ])
    assert [r["id"] for r in out] == [1, 2, 3]          # на нотификацию ответа нет
    init = out[0]["result"]
    assert init["protocolVersion"] == PROTOCOL_VERSION
    assert init["serverInfo"]["name"] == "pto-cad-agent"
    assert init["capabilities"]["tools"]["listChanged"] is False
    assert len(out[1]["result"]["tools"]) == 15
    assert out[2]["result"] == {}
    # stdout — только JSON-RPC, никаких print
    for line in proc.stdout.splitlines():
        if line.strip():
            assert json.loads(line)["jsonrpc"] == "2.0"


def test_stdio_tool_call_returns_text_content():
    out, _ = _rpc_stdio([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_drawing_info",
                    "arguments": {"file_path": "drawings/korpus2_plan_v1.dxf"}}},
    ])
    result = out[0]["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    info = json.loads(result["content"][0]["text"])
    # инструмент возвращает факты из файла, а не «ok»: чертёж, штамп, объёмы, шаблон
    assert info["drawing"]["units"] == "mm"
    assert info["drawing"]["extents"]["max"][0] > 0
    assert info["titleblock"]["fields"]["ОБЪЕКТ"]
    assert any(pos["code"] == "ФЕР 06-01-001-01" for pos in info["takeoff_positions"])


def test_stdio_errors_are_json_rpc_not_crashes():
    out, _ = _rpc_stdio([
        {"jsonrpc": "2.0", "id": 1, "method": "resources/nonexistent"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "no_such_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_drawing_info", "arguments": {}}},
    ])
    assert out[0]["error"]["code"] == -32601            # метод не поддерживается
    assert out[1]["error"]["code"] == -32602            # неизвестный инструмент
    # ошибка инструмента — это ответ с isError, а не падение сервера
    assert out[2]["result"]["isError"] is True
    assert "file_path" in out[2]["result"]["content"][0]["text"]


def test_stdio_invalid_json_is_reported():
    proc = subprocess.run([sys.executable, "-m", "pto_agent.mcp_server", "--base-dir", str(ROOT)],
                          input="{не json}\n", capture_output=True, text=True,
                          timeout=60, cwd=ROOT)
    response = json.loads(proc.stdout.strip().splitlines()[0])
    assert response["error"]["code"] == -32700


def test_resources_and_prompts_lists_are_empty_not_missing():
    out, _ = _rpc_stdio([
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "prompts/list"},
    ])
    assert out[0]["result"] == {"resources": []}
    assert out[1]["result"] == {"prompts": []}


# ── схемы инструментов ──────────────────────────────────────────────────
def test_tool_schemas_are_valid():
    public = _tools_public()
    assert len(public) == len(TOOLS) == 15
    for tool in public:
        assert tool["name"] and tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema.get("properties"), dict)
        required = schema.get("required") or []
        assert set(required) <= set(schema["properties"]), tool["name"]
        for name, spec in schema["properties"].items():
            assert "type" in spec or "enum" in spec, f"{tool['name']}.{name}"


def test_expected_tools_are_exposed():
    names = set(tool_names())
    for expected in ("get_drawing_info", "analyze_redline", "scan_redline_image",
                     "assess_release_impact", "scan_release_batch", "plan_from_scan_redline",
                     "validate_operations", "apply_operations", "cad_takeoff",
                     "cad_diff", "export_scheme", "cad_list_operations", "cad_history",
                     "cad_validate_template"):
        assert expected in names


def test_destructive_tool_requires_confirmation():
    """apply_operations в режиме commit без подтверждения человека — не операция."""
    tool = next(t for t in _tools_public() if t["name"] == "apply_operations")
    assert "confirm" in tool["inputSchema"]["properties"]
    confirm = tool["inputSchema"]["properties"]["confirm"]
    assert {"by", "role", "comment"} <= set(confirm["properties"])
    assert "подтвержд" in tool["description"].lower()
    assert "commit" in json.dumps(tool["inputSchema"], ensure_ascii=False)


# ── http ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def http_server():
    from pto_agent.cad.mcp_server import make_ctx, serve_http

    port = 8791
    ctx = make_ctx(ROOT, "out/cad/audit.jsonl")
    thread = threading.Thread(target=serve_http, args=(ctx, "127.0.0.1", port), daemon=True)
    thread.start()
    import time
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.25)
    return f"http://127.0.0.1:{port}"


def _post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def test_http_health_and_tools(http_server):
    with urllib.request.urlopen(f"{http_server}/health", timeout=10) as resp:
        health = json.loads(resp.read())
    assert health == {"status": "ok", "tools": 15}

    with urllib.request.urlopen(f"{http_server}/tools", timeout=10) as resp:
        tools = json.loads(resp.read())["tools"]
    assert len(tools) == 15


def test_http_rpc(http_server):
    init = _post(f"{http_server}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["protocolVersion"] == PROTOCOL_VERSION
    call = _post(f"{http_server}/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "cad_list_operations",
                                                   "arguments": {}}})
    assert call["result"]["isError"] is False


def test_http_batch_requests(http_server):
    req = urllib.request.Request(
        f"{http_server}/mcp",
        data=json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        batch = json.loads(resp.read())
    assert [r["id"] for r in batch] == [1, 2]


def test_inspector_page_is_served(http_server):
    with urllib.request.urlopen(f"{http_server}/", timeout=10) as resp:
        html = resp.read().decode()
    assert "MCP-сервер" in html and "tools/list" in html and "fetch('mcp'" in html


# ── настройки из окружения и установщик ─────────────────────────────────
def test_env_builds_argv(monkeypatch):
    monkeypatch.setenv("PTO_MCP_BASE_DIR", "/srv/pto")
    monkeypatch.setenv("PTO_MCP_TRANSPORT", "http")
    monkeypatch.setenv("PTO_MCP_PORT", "9100")
    argv = build_argv([])
    assert "--transport" in argv and argv[argv.index("--transport") + 1] == "http"
    assert argv[argv.index("--base-dir") + 1] == "/srv/pto"
    assert argv[argv.index("--port") + 1] == "9100"
    # явный флаг важнее переменной окружения
    argv2 = build_argv(["--transport", "stdio"])
    assert argv2.count("--transport") == 1 and "stdio" in argv2


def test_installer_prints_and_merges(tmp_path):
    res = subprocess.run([sys.executable, str(INSTALLER), "--project", str(tmp_path)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    config = json.loads(res.stdout)
    server = config["mcpServers"]["pto-cad"]
    assert server["args"][:2] == ["-m", "pto_agent.mcp_server"]
    assert str(tmp_path) in server["args"]
    assert server["env"]["PTO_MCP_BASE_DIR"] == str(tmp_path)

    target = tmp_path / ".vscode" / "mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"servers": {"other": {"type": "stdio", "command": "x"}}}))
    res = subprocess.run([sys.executable, str(INSTALLER), "--client", "vscode",
                          "--write", "--project", str(tmp_path)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    merged = json.loads(target.read_text())
    assert set(merged["servers"]) == {"other", "pto-cad"}      # чужой сервер сохранён
    assert merged["servers"]["pto-cad"]["type"] == "stdio"
    assert list(target.parent.glob("mcp.json.bak-*"))           # резервная копия сделана


def test_installer_http_variant(tmp_path):
    res = subprocess.run([sys.executable, str(INSTALLER), "--http", "--port", "9001",
                          "--project", str(tmp_path)], capture_output=True, text=True)
    server = json.loads(res.stdout)["mcpServers"]["pto-cad"]
    assert server["type"] == "http"
    assert server["url"].endswith(":9001/mcp")
