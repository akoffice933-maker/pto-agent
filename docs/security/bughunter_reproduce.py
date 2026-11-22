"""Local audit probes (2026-11-22), INVERTED after remediation.

Originally each `test_probe_*` demonstrated a live defect (all passed on the
vulnerable code). After the fixes the assertions below check the SECURE
behavior, so a green run now proves the vulnerabilities are closed. The
canonical regression suite is tests/test_security_fixes.py; this file is the
audit artifact and stays runnable standalone:

    .venv/bin/python -m pytest -q -s docs/security/bughunter_reproduce.py

All writes go to pytest temporary directories; no real secrets, external HTTP
requests, live services or modifications to source data are involved.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pto_agent import treasury
from pto_agent.cad.mcp_server import handle_rpc, make_ctx
from pto_agent.web import build_demo, create_app

REPO = Path(__file__).resolve().parents[2]
FOREIGN = "AUDIT-FOREIGN-OBJECT"
MARKER = "AUDIT-PRIVATE-COUNTERPARTY"


def rpc(ctx, name, **arguments):
    return handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}, ctx)["result"]


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    base = tmp_path_factory.mktemp("bughunter-seed")
    data = base / "reference"
    shutil.copytree(REPO / "data/reference", data)
    root = base / "out"
    build_demo(root, data_dir=data)
    return root, data


@pytest.fixture
def web(tmp_path, seed):
    root, data = seed
    target = tmp_path / "out"
    shutil.copytree(root, target)
    client = TestClient(create_app(root=target, data_dir=data))
    yield client, target
    client.close()


def login(client, name="prokhorov", **kwargs):
    page = client.get("/login")
    marker = 'name="csrf" value="'
    start = page.text.index(marker) + len(marker)
    token = page.text[start: page.text.index('"', start)]
    return client.post("/login", data={"login": name, "password": "demo-" + name,
                                       "csrf": token}, follow_redirects=False, **kwargs)


def token(client):
    page = client.get("/")
    return page.text.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def foreign_request(root):
    path = root / "treasury/state.json"
    state = treasury.load(path)
    row = treasury.find(state, "З-002")
    row["object"] = FOREIGN
    row["counterparty"] = MARKER
    treasury.save(state, path)
    return path


@pytest.mark.parametrize("endpoint", ["/requests", "/requests/table", "/"])
def test_probe_foreign_request_disclosure(web, endpoint):
    client, root = web
    foreign_request(root)
    assert login(client).status_code == 303
    response = client.get(endpoint)
    assert response.status_code == 200
    assert MARKER not in response.text
    print(f"FIXED IDOR read {endpoint}: foreign counterparty no longer disclosed")


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_probe_foreign_request_mutation(web, action):
    client, root = web
    path = foreign_request(root)
    assert login(client, "snab").status_code == 303
    before = treasury.find(treasury.load(path), "З-002")
    assert before["object"] == FOREIGN and not before.get("approvals")
    response = client.post(f"/requests/З-002/{action}",
                           data={"csrf": token(client), "reason": "audit-only"},
                           follow_redirects=False)
    assert response.status_code == 403
    assert "OBJECT_FORBIDDEN" in response.text
    after = treasury.find(treasury.load(path), "З-002")
    assert after.get("approvals") == before.get("approvals")
    assert after["status"] == before["status"]
    print(f"FIXED IDOR write {action}: foreign request change refused, state unchanged")


@pytest.mark.parametrize("endpoint", ["/warehouse", "/warehouse/table"])
def test_probe_warehouse_scope_bypass(web, endpoint):
    client, root = web
    path = root / "stock/state.json"
    state = json.loads(path.read_text())
    state["object"] = FOREIGN
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    login(client)
    response = client.get(endpoint)
    assert response.status_code == 403
    assert "OBJECT_FORBIDDEN" in response.text
    print(f"FIXED IDOR stock {endpoint}: foreign stock refused with and without filter")


def test_probe_open_redirect(web):
    client, _ = web
    response = login(client, params={"next": "//audit.invalid/landing"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    print("FIXED open redirect: Location=/ (protocol-relative URL rejected)")


def test_probe_login_csrf(web):
    client, _ = web
    assert not client.cookies
    response = client.post("/login", data={"login": "prokhorov",
                                           "password": "demo-prokhorov"},
                           headers={"Origin": "https://audit.invalid",
                                    "Referer": "https://audit.invalid/form"},
                           follow_redirects=False)
    assert response.status_code == 400
    assert "CSRF" in response.text
    # сессия не установлена: сводка по-прежнему отправляет на логин
    assert client.get("/", follow_redirects=False).status_code == 303
    print("FIXED login CSRF: cross-origin form without token refused")


def test_probe_mcp_unchecked_summary_source(tmp_path):
    base = tmp_path / "sandbox"
    base.mkdir()
    source = tmp_path / "outside-summary.json"
    marker = "AUDIT-OUTSIDE-PRIVATE-WORK"
    source.write_text(json.dumps({"positions": [{"name": marker, "qty": 7, "unit": "м3"}]}))
    ctx = make_ctx(base, sandbox=True)
    leaked = rpc(ctx, "asbuilt_docs", kind="spec", summary_source=str(source))
    assert leaked["isError"] is True
    assert marker not in leaked["content"][0]["text"]
    print("FIXED MCP sandbox read: outside summary_source refused before execution")


def test_probe_mcp_manifest_outside_write(tmp_path):
    base = tmp_path / "sandbox"
    base.mkdir()
    victim = tmp_path / "audit-victim.json"
    victim.write_text('{"original": true}')
    manifest = base / "manifest.json"
    manifest.write_text(json.dumps({"sheets": [{
        "sheet_id": str(victim.with_suffix("")),
        "scan": str(base / "missing.png"),
        "drawing": str(base / "missing.dxf"),
    }]}))
    rpc(make_ctx(base, sandbox=True), "scan_release_batch",
        manifest_path=str(manifest), out_dir=str(base / "results"),
        engine="offline", resume=False)
    after = json.loads(victim.read_text())
    assert after == {"original": True}
    print("FIXED MCP sandbox write: sheet_id sanitized, outside file untouched")


def test_control_agent_cannot_write_and_csrf_is_checked(web):
    client, root = web
    login(client, "agent")
    before = (root / "ledger/state.json").read_bytes()
    response = client.post("/closing", data={"period": "2026-10"})
    assert response.status_code == 403 and "AGENT_FORBIDDEN" in response.text
    login(client, "buh")
    response = client.post("/closing", data={"period": "2026-10", "csrf": "wrong"})
    assert response.status_code == 400 and "CSRF" in response.text
    assert before == (root / "ledger/state.json").read_bytes()


def test_control_xss_is_escaped(web):
    client, root = web
    path = foreign_request(root)
    state = treasury.load(path)
    payload = '<img src=x onerror="alert(1)">'
    treasury.find(state, "З-002")["counterparty"] = payload
    treasury.save(state, path)
    login(client, "buh")
    page = client.get("/requests").text
    assert payload not in page
    assert "&lt;img src=x onerror=" in page
