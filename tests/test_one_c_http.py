# -*- coding: utf-8 -*-
"""Уровень 2 обмена с 1С: HTTP-сервис.

Фейковый сервис 1С поднимается в потоке и отвечает так же, как будет отвечать настоящий
расширение-сервис: акты с позициями, строки сметы, приём КС-2 с `Idempotency-Key`.
Проверяем то, что ломается в бою: аутентификацию, 4xx против 5xx, повторы, не-JSON ответ,
и что «не оценено» уходит как `null`, а не нулём.
"""
import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.integrations.one_c_http import (OneCAuthError, OneCHttpClient, OneCHttpError,
                                               OneCRequestError, OneCServerError,
                                               idempotency_key, ks2_payload, load_config,
                                               smeta_from_json, sync)

SUMMARY = ROOT / "out" / "summary.json"

ACTS = {"object": "ЖК «Северный», корпус 2", "acts": [
    {"act_id": "КС-2 №35", "period": "2026-09", "act_date": "2026-09-30", "status": "проверен",
     "positions": [
         {"Код": "ФЕР 06-01-001-01", "Наименование": "Плита фундаментная", "ЕдИзм": "м3",
          "Количество": "200", "Цена": "18 500"},
         {"Код": "ФЕР 08-02-001-01", "Наименование": "Гидроизоляция", "ЕдИзм": "м2",
          "Количество": "600", "Цена": "780"}]},
    {"act_id": "КС-2 №37", "period": "2026-09", "act_date": "2026-09-30", "status": "проверен",
     "positions": [
         {"Код": "ФЕР 16-02-003-01", "Наименование": "Водопровод Ду50", "ЕдИзм": "м",
          "Количество": "0", "Цена": "620"}]},
]}
LINES = {"lines": [
    {"Шифр": "ФЕР 06-01-001-01", "Наименование": "Плита фундаментная", "Ед. изм.": "м3",
     "Количество": "1200", "Цена": "18500"},
    {"Шифр": "ФЕР 11-01-011-01", "Наименование": "Стяжка полов", "Ед. изм.": "м3",
     "Количество": "210", "Сумма": "2 520 000"},
]}


class _State:
    flaky_hits = 0
    posts: list = []
    auth_seen: list = []


@pytest.fixture()
def service():
    _State.flaky_hits = 0
    _State.posts = []
    _State.auth_seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            auth = self.headers.get("Authorization") or ""
            _State.auth_seen.append(auth)
            if auth.startswith("Basic "):
                user, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
                return user == "ptot" and pwd == "secret"
            if auth.startswith("Bearer "):
                return auth[7:] == "tok-123"
            return False

        def do_GET(self):
            url = urlparse(self.path)
            path, query = url.path, parse_qs(url.query)
            if path == "/health":
                return self._json(200, {"status": "ok", "version": "1С 8.3"})
            if path == "/acts":
                return self._json(200, ACTS)
            if path == "/acts_flat":
                rows = [dict(p, act_id=a["act_id"], period=a["period"], status=a["status"])
                        for a in ACTS["acts"] for p in a["positions"]]
                return self._json(200, {"items": rows})
            if path.startswith("/estimates/"):
                return self._json(200, LINES)
            if path == "/secure/acts":
                if not self._authorized():
                    return self._json(401, {"error": "нет прав"})
                return self._json(200, ACTS)
            if path == "/flaky/acts":
                _State.flaky_hits += 1
                if _State.flaky_hits == 1:
                    return self._json(503, {"error": "занято"})
                return self._json(200, ACTS)
            if path == "/down/acts":
                return self._json(500, {"error": "внутренняя ошибка"})
            if path == "/broken":
                body = "это не JSON".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if path == "/reject/acts":
                return self._json(400, {"error": "неверный период"})
            self._json(404, {"error": "no route"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            _State.posts.append({"path": self.path, "body": body,
                                 "key": self.headers.get("Idempotency-Key")})
            self._json(200, {"document": "КС-2 №99", "accepted": True,
                             "idempotency_key": self.headers.get("Idempotency-Key")})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def client(service, **kw):
    return OneCHttpClient(service, timeout=5, retry_delay=0.01, **kw)


@pytest.fixture()
def summary():
    if not SUMMARY.exists():
        pytest.skip("нет out/summary.json: run --data data/demo --out out")
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


# ───────────────────────────── транспорт ────────────────────────────────
def test_ping(service):
    assert client(service).ping()["status"] == "ok"


def test_basic_auth(service):
    c = client(service, user="ptot", password="secret")
    assert c.fetch_acts()["imported"]["count"] == 2


def test_bad_credentials_raise_auth_error(service):
    c = client(service, user="ptot", password="wrong")
    with pytest.raises(OneCAuthError) as e:
        c.fetch_acts(path="secure/acts")
    assert "401" in str(e.value)
    assert c.calls[-1]["status"] == 401


def test_token_auth(service):
    c = client(service, token="tok-123")
    assert c.fetch_acts(path="secure/acts")["imported"]["count"] == 2


def test_4xx_is_not_retried(service):
    c = client(service)
    with pytest.raises(OneCRequestError):
        c.fetch_acts(path="reject/acts")
    assert len([x for x in c.calls if "/reject/" in x["url"]]) == 1


def test_5xx_is_retried_and_then_succeeds(service):
    c = client(service, retries=1)
    res = c.fetch_acts(path="flaky/acts")
    assert res["imported"]["count"] == 2
    assert [x["status"] for x in c.calls if "/flaky/" in x["url"]] == [503, 200]


def test_persistent_5xx_raises_server_error(service):
    c = client(service, retries=1)
    with pytest.raises(OneCServerError):
        c.fetch_acts(path="down/acts")
    assert len([x for x in c.calls if "/down/" in x["url"]]) == 2


def test_non_json_response_is_reported(service):
    with pytest.raises(OneCHttpError) as e:
        client(service).request("GET", "broken")
    assert "не JSON" in str(e.value)


def test_unreachable_service(service):
    c = OneCHttpClient("http://127.0.0.1:9", timeout=1, retries=0)
    with pytest.raises(OneCServerError):
        c.fetch_acts()


def test_config_from_dict_and_yaml(service, tmp_path):
    import yaml
    cfg = {"one_c": {"url": service, "user": "ptot", "password": "secret", "timeout_s": 5}}
    p = tmp_path / "one_c.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    c = OneCHttpClient.from_config(load_config(p))
    assert c.fetch_acts()["imported"]["count"] == 2
    with pytest.raises(OneCHttpError):
        OneCHttpClient.from_config({"user": "x"})


# ───────────────────── нормализация ответов 1С ──────────────────────────
def test_acts_nested_and_flat_forms_agree(service):
    c = client(service)
    nested = c.fetch_acts()
    flat = c.fetch_acts(path="acts_flat")
    assert nested["imported"]["count"] == flat["imported"]["count"] == 2
    assert nested["document"]["object"] == "ЖК «Северный», корпус 2"
    amounts = {a["act_id"]: a["amount_rub"] for a in nested["document"]["acts"]}
    assert amounts["КС-2 №35"] == pytest.approx(200 * 18500 + 600 * 780, abs=0.01)
    assert amounts["КС-2 №37"] == 0.0            # объём «0» → не оценено, в сумму не идёт
    assert any("не оценена" in w["message"] for w in nested["imported"]["warnings"])


def test_estimate_lines(service):
    res = client(service).fetch_estimate("smeta-1")
    lines = res["smeta"]["lines"]
    assert res["smeta"]["count"] == 2
    assert lines[0]["estimate_qty"] == pytest.approx(1200.0)
    assert lines[1]["price"] == pytest.approx(12000.0)   # 2 520 000 / 210


def test_empty_payload_is_error():
    with pytest.raises(OneCHttpError):
        smeta_from_json({"lines": []})


# ───────────────────── отправка проверенной КС-2 ────────────────────────
def test_ks2_payload_marks_unestimated_as_null(summary):
    payload = ks2_payload(summary, quantity="claim")
    assert payload["source"] == "pto-agent"
    assert payload["totals"]["positions"] == len(summary["lines"])
    for p in payload["positions"]:
        assert "quantity" in p and isinstance(p["estimated"], bool)
        assert (p["quantity"] is None) == (not p["estimated"])
    assert payload["idempotency_key"] == idempotency_key(payload)


def test_idempotency_key_is_stable_and_content_bound(summary):
    a = ks2_payload(summary, quantity="claim")
    b = ks2_payload(summary, quantity="claim")
    c = ks2_payload(summary, quantity="journal")
    assert a["idempotency_key"] == b["idempotency_key"]
    assert a["idempotency_key"] != c["idempotency_key"]


def test_push_sends_idempotency_header(service, summary):
    c = client(service)
    payload = ks2_payload(summary, quantity="claim")
    res = c.push_validated_ks2(payload)
    assert res["accepted"] is True
    assert _State.posts[0]["key"] == payload["idempotency_key"]
    assert _State.posts[0]["body"]["totals"]["amount"] == payload["totals"]["amount"]


def test_sync_pulls_acts_and_pushes_ks2(service, summary):
    result = sync(client(service), period="2026-09", summary=summary, push=True)
    assert result["acts"]["count"] == 2
    assert result["acts"]["amount"] == pytest.approx(4_168_000.0, abs=0.01)
    assert result["pushed"]["positions"] == len(summary["lines"])
    assert any(call["method"] == "POST" for call in result["calls"])


def test_sync_without_push_does_not_write_to_1c(service, summary):
    result = sync(client(service), summary=summary, push=False)
    assert "pushed" not in result
    assert _State.posts == []


def test_synced_acts_feed_our_portfolio(service):
    from pto_agent.portfolio import assess_portfolio
    result = sync(client(service), period="2026-09")
    portfolio = assess_portfolio(result["acts_document"])
    assert len(portfolio["acts"]) == 2
    assert portfolio["totals"]["amount_rub"] == pytest.approx(4_168_000.0, abs=0.01)


# ───────────────────── конфиг из окружения и CLI ────────────────────────
def test_env_placeholders_are_expanded(tmp_path, monkeypatch):
    import yaml
    monkeypatch.setenv("ONEC_URL_TEST", "http://1c.local/hs/pto")
    monkeypatch.setenv("ONEC_USER_TEST", "ptot")
    p = tmp_path / "one_c.yaml"
    p.write_text(yaml.safe_dump({"one_c": {"url": "${ONEC_URL_TEST}", "user": "${ONEC_USER_TEST}",
                                           "password": "${ONEC_PASSWORD_TEST}"}}, allow_unicode=True),
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg["url"] == "http://1c.local/hs/pto"
    assert cfg["user"] == "ptot"
    assert cfg["password"] is None          # нет переменной — нет и пароля в файле


def test_cli_sync_1c_end_to_end(service, tmp_path):
    """CLI: акты из 1С → файлы, проверенная КС-2 → обратно в сервис."""
    import subprocess
    cfg = tmp_path / "one_c.json"
    cfg.write_text(json.dumps({"one_c": {"url": service, "timeout_s": 5, "retries": 1}}),
                   encoding="utf-8")
    out = tmp_path / "out"
    res = subprocess.run([sys.executable, "-m", "pto_agent", "sync-1c",
                          "--config", str(cfg), "--period", "2026-09",
                          "--push", "--out", str(out)],
                         capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Из 1С:" in res.stdout and "Ключ повтора:" in res.stdout
    doc = json.loads((out / "acts_from_1c.json").read_text(encoding="utf-8"))
    assert len(doc["acts"]) == 2
    payload = json.loads((out / "ks2_payload.json").read_text(encoding="utf-8"))
    assert payload["totals"]["positions"] > 0
    assert _State.posts and _State.posts[0]["body"]["source"] == "pto-agent"
