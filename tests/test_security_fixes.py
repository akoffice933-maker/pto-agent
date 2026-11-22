# -*- coding: utf-8 -*-
"""Регрессионные тесты по аудиту 2026-11-22 (Claude-BugHunter).

Каждый тест — инверсия PoC из docs/security/bughunter_reproduce.py: до исправлений
проверка падала (уязвимость воспроизводилась), теперь она фиксирует исправленное
поведение. Если тест падает — уязвимость вернулась.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pto_agent import treasury
from pto_agent.cad.mcp_server import check_paths, handle_rpc, make_ctx
from pto_agent.web import build_demo, create_app

REPO = Path(__file__).resolve().parents[1]
FOREIGN = "AUDIT-FOREIGN-OBJECT"
MARKER = "AUDIT-PRIVATE-COUNTERPARTY"


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    base = tmp_path_factory.mktemp("security-seed")
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


def rpc(ctx, name, **arguments):
    return handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}, ctx)["result"]


# ── IDOR: чтение чужих заявок ──
@pytest.mark.parametrize("endpoint", ["/requests", "/requests/table", "/"])
def test_foreign_request_is_not_disclosed(web, endpoint):
    client, root = web
    foreign_request(root)
    assert login(client).status_code == 303
    response = client.get(endpoint)
    assert response.status_code == 200
    assert MARKER not in response.text


def test_all_scope_still_sees_foreign_request(web):
    client, root = web
    foreign_request(root)
    assert login(client, "buh").status_code == 303
    assert MARKER in client.get("/requests").text


# ── IDOR: запись по чужим заявкам ──
@pytest.mark.parametrize("action", ["approve", "reject"])
def test_foreign_request_mutation_is_refused(web, action):
    client, root = web
    path = foreign_request(root)
    assert login(client, "snab").status_code == 303
    before = treasury.find(treasury.load(path), "З-002")
    response = client.post(f"/requests/З-002/{action}",
                           data={"csrf": token(client), "reason": "audit-only"},
                           follow_redirects=False)
    assert response.status_code == 403
    assert "OBJECT_FORBIDDEN" in response.text
    after = treasury.find(treasury.load(path), "З-002")
    assert after.get("approvals") == before.get("approvals")
    assert after["status"] == before["status"]


def test_own_request_can_still_be_approved(web):
    client, root = web
    path = root / "treasury/state.json"
    assert login(client, "snab").status_code == 303
    response = client.post("/requests/З-002/approve",
                           data={"csrf": token(client), "comment": "ок"},
                           follow_redirects=False)
    assert response.status_code == 303
    row = treasury.find(treasury.load(path), "З-002")
    assert len(row["approvals"]) == 1


# ── IDOR: склад ──
@pytest.mark.parametrize("endpoint", ["/warehouse", "/warehouse/table"])
def test_foreign_warehouse_is_refused(web, endpoint):
    client, root = web
    path = root / "stock/state.json"
    state = json.loads(path.read_text())
    state["object"] = FOREIGN
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    login(client)
    response = client.get(endpoint)
    assert response.status_code == 403
    assert "OBJECT_FORBIDDEN" in response.text


def test_own_warehouse_and_agent_reads_still_work(web):
    client, root = web
    login(client)
    assert client.get("/warehouse").status_code == 200
    assert client.get("/warehouse/table").status_code == 200
    login(client, "agent")
    assert client.get("/warehouse").status_code == 200


# ── open redirect / login CSRF ──
def test_open_redirect_is_closed(web):
    client, _ = web
    response = login(client, params={"next": "//audit.invalid/landing"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_login_without_csrf_is_refused(web):
    client, _ = web
    response = client.post("/login", data={"login": "prokhorov",
                                           "password": "demo-prokhorov"},
                           follow_redirects=False)
    assert response.status_code == 400
    assert "CSRF" in response.text


def test_login_with_csrf_works(web):
    client, _ = web
    assert login(client).status_code == 303
    assert "Прохоров" in client.get("/").text


# ── MCP sandbox ──
def test_mcp_summary_source_outside_is_refused(tmp_path):
    base = tmp_path / "sandbox"
    base.mkdir()
    source = tmp_path / "outside-summary.json"
    source.write_text(json.dumps({"positions": [{"name": "x", "qty": 1, "unit": "м"}]}))
    result = rpc(make_ctx(base, sandbox=True), "asbuilt_docs", kind="spec",
                 summary_source=str(source))
    assert result["isError"] is True


def test_mcp_manifest_cannot_overwrite_outside_file(tmp_path):
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
    assert json.loads(victim.read_text()) == {"original": True}


def test_mcp_check_paths_covers_all_path_keys(tmp_path):
    ctx = {"base_dir": str(tmp_path), "sandbox_writes": True, "sandbox_reads": True}
    outside = str(tmp_path.parent / "evil")
    for args in ({"summary_source": outside}, {"norms_dir": outside},
                 {"quotes_paths": [outside]}, {"receipts_paths": [outside]},
                 {"act_paths": [outside]}, {"paths": [outside]},
                 {"prices_dir": outside}, {"base_dir": outside}):
        with pytest.raises(ValueError):
            check_paths(args, ctx)
    # внутри корня — можно
    check_paths({"summary_source": "out/summary.json", "paths": ["out/tk"],
                 "prices_dir": "data/prices", "base_dir": str(tmp_path)}, ctx)


# ── контрольные ──
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
    login(client, "buh")  # all-scope видит заявку — проверяем экранирование
    page = client.get("/requests").text
    assert payload not in page
    assert "&lt;img src=x onerror=" in page
