# -*- coding: utf-8 -*-
"""Сетевые предохранители и чтение недоверенных XLSX (то, чего не покрывало ревью).

Каждый тест — воспроизведение конкретной дыры:

* LLM: `PTO_LLM_BASE_URL` уводил запрос на `169.254.169.254` (метаданные облака), а класс
  данных «коммерческая тайна» не проверялся при явном `provider_id`;
* vision: проверка адреса была перевёрнута — внутренний контур запрещён, метаданные разрешены;
* 1С: принимался `file:///etc`, basic-auth мог уйти по открытому http, POST повторялся без
  `Idempotency-Key` (второй документ в 1С), тело ответа попадало в текст ошибки, размер
  ответа не ограничивался, редирект уносил `Authorization` на другой хост;
* парсеры: книга грузилась целиком, а битое число становилось `0.0`, нераспознанная дата —
  сегодняшним числом;
* выгрузки CSV: наименование из внешней сметы с «=» исполнялось Excel как формула.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pto_agent.netguard import (UnsafeEndpoint, csv_safe, open_bounded, public_message,
                                validate_url)

ROOT = Path(__file__).resolve().parents[1]


# ─────────────────────────── адреса ─────────────────────────────────────
@pytest.mark.parametrize("url,reason", [
    ("http://169.254.169.254/latest/meta-data", "METADATA_ENDPOINT"),
    ("http://100.100.100.200/latest/meta-data", "METADATA_ENDPOINT"),
    ("http://metadata.google.internal/computeMetadata/v1", "METADATA_ENDPOINT"),
    ("file:///etc/passwd", "BAD_SCHEME"),
    ("ftp://example.com/x", "BAD_SCHEME"),
    ("gopher://127.0.0.1:6379/_INFO", "BAD_SCHEME"),
    ("https://example.com", None),
    ("http://10.20.0.12:8000", None),          # внутренний контур — это норма
    ("http://1c.local/ws", None),
])
def test_validate_url(url, reason):
    if reason is None:
        assert validate_url(url, purpose="тест") == url
    else:
        with pytest.raises(UnsafeEndpoint) as err:
            validate_url(url, purpose="тест")
        assert err.value.reason == reason


def test_open_http_to_external_host_rejected():
    with pytest.raises(UnsafeEndpoint) as err:
        validate_url("http://smeta.example.com/api", purpose="тест")
    assert err.value.reason == "INSECURE_HTTP"
    # осознанное исключение возможно, но только явным флагом
    assert validate_url("http://smeta.example.com/api", purpose="тест",
                        allow_insecure_http=True)


def test_private_forbidden_when_policy_says_so():
    with pytest.raises(UnsafeEndpoint) as err:
        validate_url("http://10.0.0.5", purpose="тест", allow_private=False)
    assert err.value.reason == "PRIVATE_FORBIDDEN"


def test_public_message_strips_url_and_credentials():
    text = public_message(ValueError("нет связи с https://user:pa55@evil.example.com/x?t=1"))
    assert "evil.example.com" not in text and "pa55" not in text
    assert "<адрес>" in text


# ─────────────────────────── чтение ответа ──────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, *args):
        pass

    def do_GET(self):                                        # noqa: N802
        route = self.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, body = route
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def server():
    routes: dict = {}
    _Handler.routes = routes
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{srv.server_port}", "routes": routes}
    srv.shutdown()


def test_open_bounded_reads_body(server):
    import urllib.request
    server["routes"]["/ok"] = (200, {"Content-Type": "application/json"}, b'{"a": 1}')
    req = urllib.request.Request(server["url"] + "/ok")
    body, status, _headers = open_bounded(req, timeout=5, max_bytes=1024)
    assert status == 200 and json.loads(body) == {"a": 1}


def test_open_bounded_stops_oversized_response(server):
    import urllib.request
    server["routes"]["/big"] = (200, {}, b"x" * 200_000)
    req = urllib.request.Request(server["url"] + "/big")
    with pytest.raises(UnsafeEndpoint) as err:
        open_bounded(req, timeout=5, max_bytes=1024)
    assert err.value.reason == "RESPONSE_TOO_LARGE"


def test_cross_host_redirect_blocked(server):
    import urllib.request
    server["routes"]["/jump"] = (302, {"Location": "http://169.254.169.254/latest/meta-data"}, b"")
    req = urllib.request.Request(server["url"] + "/jump")
    with pytest.raises(UnsafeEndpoint) as err:
        open_bounded(req, timeout=5, max_bytes=1024)
    assert err.value.reason == "CROSS_HOST_REDIRECT"


# ─────────────────────────── LLM ────────────────────────────────────────
def test_llm_refuses_metadata_endpoint(monkeypatch):
    from pto_agent import llm

    monkeypatch.setenv("PTO_LLM_BASE_URL", "http://169.254.169.254")
    result = llm.chat_ex("смета", data_class="public")
    assert result["ok"] is False and result["text"] is None
    assert "метаданные" in result["reason"]


def test_llm_refuses_env_override_for_restricted_data(monkeypatch):
    from pto_agent import llm

    monkeypatch.setenv("PTO_LLM_BASE_URL", "https://attacker.example.com")
    result = llm.chat_ex("КТ по объекту", data_class="commercial_secret")
    assert result["ok"] is False
    assert "переопределён переменной" in result["reason"]


def test_llm_policy_blocks_class_of_data():
    from pto_agent.llm import check_provider_policy

    cloud = {"id": "cloud", "data_classes": ["public", "internal"], "residency": "outside_rf"}
    assert check_provider_policy(cloud, "commercial_secret")
    assert check_provider_policy(cloud, "public") is None
    outside = {"id": "x", "data_classes": ["public", "commercial_secret"],
               "residency": "outside_rf"}
    assert "пределы контура" in (check_provider_policy(outside, "commercial_secret") or "")


def test_llm_route_policy_is_enforced_at_call_time(monkeypatch):
    """`route()` считал blocked_by_policy, но `chat()` это игнорировал — теперь нет."""
    from pto_agent import llm

    cfg = {"providers": [{"id": "cloud", "data_classes": ["public"], "residency": "rf_cloud",
                          "base_url": "https://cloud.example.com", "model": "m",
                          "auth": {"type": "api_key", "key_env": "PTO_TEST_KEY"}}],
           "routing": [{"id": "r", "when": {}, "to": ["cloud"]}]}
    monkeypatch.setattr(llm, "load_config", lambda path=llm.CONFIG_PATH: cfg)
    result = llm.chat_ex("персональные данные", data_class="personal")
    assert result["ok"] is False
    assert "не принимает класс данных" in result["reason"] or "пределы контура" in result["reason"]


# ─────────────────────────── vision ────────────────────────────────────
def test_vision_address_check_rejects_metadata_and_file():
    from pto_agent.cad.vision.providers import _base_is_reachable_shape

    assert _base_is_reachable_shape("http://169.254.169.254/latest/meta-data") is False
    assert _base_is_reachable_shape("file:///etc/passwd") is False
    assert _base_is_reachable_shape("http://10.20.0.12:8000") is False   # контур недоступен
    assert _base_is_reachable_shape("https://vlm.example.com/v1") is True


def test_vision_refuses_external_residency_for_client_drawing():
    from pto_agent.cad.vision.providers import (OpenAIVisionProvider, ProviderError,
                                                VisionRequest)

    provider = OpenAIVisionProvider({"id": "vlm-cloud", "model": "m",
                                     "base_url": "https://vlm.example.com",
                                     "data_classes": ["commercial_secret"],
                                     "residency": "outside_rf"})
    with pytest.raises(ProviderError) as err:
        provider.analyze(VisionRequest(image_bytes=b"png"))
    assert "пределы контура" in str(err.value)


# ─────────────────────────── 1С ─────────────────────────────────────────
def test_one_c_rejects_non_http_scheme():
    from pto_agent.integrations.one_c_http import OneCHttpClient, OneCHttpError

    with pytest.raises(OneCHttpError):
        OneCHttpClient("file:///etc")


def test_one_c_rejects_plain_http_for_external_host():
    from pto_agent.integrations.one_c_http import OneCHttpClient, OneCHttpError

    with pytest.raises(OneCHttpError) as err:
        OneCHttpClient("http://1c.example.com", user="admin", password="secret")
    assert "https" in str(err.value)
    assert "secret" not in str(err.value)


def test_one_c_post_without_idempotency_key_is_not_retried(server, monkeypatch):
    from pto_agent.integrations import one_c_http

    calls = {"n": 0}

    def always_500(req, *, timeout, max_bytes, purpose=""):
        calls["n"] += 1
        raise one_c_http.urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(one_c_http, "open_bounded", always_500)
    client = one_c_http.OneCHttpClient(server["url"], retries=3, retry_delay=0)
    with pytest.raises(one_c_http.OneCServerError):
        client.request("POST", "acts", body={"a": 1})
    assert calls["n"] == 1, "POST без Idempotency-Key повторять нельзя — второй документ в 1С"


def test_one_c_post_with_idempotency_key_is_retried(server, monkeypatch):
    from pto_agent.integrations import one_c_http

    calls = {"n": 0}

    def always_500(req, *, timeout, max_bytes, purpose=""):
        calls["n"] += 1
        raise one_c_http.urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(one_c_http, "open_bounded", always_500)
    client = one_c_http.OneCHttpClient(server["url"], retries=2, retry_delay=0)
    with pytest.raises(one_c_http.OneCServerError):
        client.request("POST", "acts", body={"a": 1}, idempotency_key="k1")
    assert calls["n"] == 3


def test_one_c_error_text_has_no_credentials(server, monkeypatch):
    from pto_agent.integrations import one_c_http

    def unauthorized(req, *, timeout, max_bytes, purpose=""):
        raise one_c_http.urllib.error.HTTPError(
            req.full_url, 401, "unauth", {},
            __import__("io").BytesIO(b"password=hunter2; see http://1c.internal/db"))

    monkeypatch.setattr(one_c_http, "open_bounded", unauthorized)
    client = one_c_http.OneCHttpClient(server["url"], user="admin", password="hunter2")
    with pytest.raises(one_c_http.OneCAuthError) as err:
        client.request("GET", "acts")
    message = str(err.value)
    assert "hunter2" not in message
    assert "1c.internal" not in message


def test_one_c_stops_giant_response(server):
    from pto_agent.integrations.one_c_http import OneCHttpClient, OneCHttpError

    server["routes"]["/huge"] = (200, {"Content-Type": "application/json"}, b"y" * 100_000)
    client = OneCHttpClient(server["url"], max_bytes=1024, retries=0)
    with pytest.raises(OneCHttpError) as err:
        client.request("GET", "huge")
    assert "больше" in str(err.value)


# ─────────────────────────── XLSX ──────────────────────────────────────
def _write_xlsx(path: Path, rows: list[list]) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Код", "Наименование", "Ед", "Кол-во", "Цена"])
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_parsers_report_bad_numbers_instead_of_silent_zero(tmp_path):
    from pto_agent.parsers import read_estimate_ex

    path = _write_xlsx(tmp_path / "smeta.xlsx", [
        ["1", "Работа А", "м3", 10, 100],
        ["2", "Работа Б", "м3", "десять", 100],       # количество не число
        ["3", "Работа В", "м3", 5, "по договору"],    # цена не число
    ])
    items, problems = read_estimate_ex(path)
    assert len(items) == 3
    assert len(problems) == 2
    assert {p["what"] for p in problems} == {"количество не число", "цена не число"}
    assert all("не посчитана" in p["effect"] for p in problems)


def test_parsers_skip_unparsed_date_instead_of_today(tmp_path):
    from datetime import date

    from pto_agent.parsers import read_journal_ex

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Дата", "Код", "Наименование", "Ед", "Кол-во", "Зона", "Исполнитель", "Док"])
    ws.append([date(2026, 10, 1), "1", "Работа А", "м3", 10, "A", "Иванов", "акт"])
    ws.append(["не дата", "2", "Работа Б", "м3", 5, "A", "Петров", "акт"])
    path = tmp_path / "journal.xlsx"
    wb.save(path)

    entries, problems = read_journal_ex(path)
    assert len(entries) == 1 and entries[0].date == date(2026, 10, 1)
    assert len(problems) == 1 and "дата не распознана" in problems[0]["what"]


def test_parsers_enforce_row_limit(tmp_path, monkeypatch):
    from pto_agent import parsers

    monkeypatch.setattr(parsers, "MAX_ROWS", 5)
    path = _write_xlsx(tmp_path / "big.xlsx",
                       [[str(i), f"Работа {i}", "м3", 1, 1] for i in range(20)])
    with pytest.raises(parsers.WorkbookTooLarge) as err:
        parsers.read_estimate(path)
    assert "строк больше предела" in str(err.value)


def test_parsers_enforce_file_size(tmp_path, monkeypatch):
    from pto_agent import parsers

    path = _write_xlsx(tmp_path / "s.xlsx", [["1", "А", "м3", 1, 1]])
    monkeypatch.setattr(parsers, "MAX_FILE_BYTES", path.stat().st_size - 1)
    with pytest.raises(parsers.WorkbookTooLarge) as err:
        parsers.read_estimate(path)
    assert "больше предела" in str(err.value)


def test_parsers_enforce_sheet_limit(tmp_path, monkeypatch):
    from openpyxl import Workbook

    from pto_agent import parsers

    wb = Workbook()
    for _ in range(4):
        wb.create_sheet()
    path = tmp_path / "many.xlsx"
    wb.save(path)
    monkeypatch.setattr(parsers, "MAX_SHEETS", 2)
    with pytest.raises(parsers.WorkbookTooLarge) as err:
        parsers.read_estimate(path)
    assert "листов" in str(err.value)


# ─────────────────────────── CSV-инъекции ──────────────────────────────
def test_csv_safe_neutralises_formulas():
    for value in ("=cmd|' /C calc'!A0", "+1", "-1", "@SUM(A1)", "\t=1+1"):
        assert csv_safe(value).startswith("'")
    assert csv_safe("Бетон В25") == "Бетон В25"
    assert csv_safe(42.5) == 42.5            # числа не трогаем
    assert csv_safe(None) == ""


def test_export_csv_neutralises_untrusted_name(tmp_path):
    """Наименование позиции приходит из чужой сметы: «=cmd|…» не должно стать формулой."""
    from datetime import date

    from pto_agent.export import export_discrepancies_csv
    from pto_agent.models import Discrepancy
    from pto_agent.reconcile import Reconciliation

    rec = Reconciliation(object_name="тест", period=(date(2026, 10, 1), date(2026, 10, 31)))
    rec.discrepancies = [Discrepancy(severity="major", type="QTY_MISMATCH",
                                     code="1", name="=cmd|' /C calc'!A0",
                                     values={"qty": 1}, sources=["акт"],
                                     suggested_action="проверить")]
    path = export_discrepancies_csv(rec, tmp_path / "d.csv")
    text = path.read_text(encoding="utf-8-sig")
    assert "=cmd" not in text.replace("'=cmd", "")
    assert "'=cmd|' /C calc'!A0" in text
