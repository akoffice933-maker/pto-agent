# -*- coding: utf-8 -*-
"""Экраны и роли: проверки идут через HTTP, как у живого пользователя.

Что должно быть верно (а не «страница открылась»):

1. без входа экран не открывается, а логин и пароль проверяются по хэшу;
2. права проверяются на сервере: кнопку можно спрятать, отказ приходит с кодом;
3. прораб подаёт требование из браузера — движение появляется в журнале склада;
4. чужой объект — отказ `OBJECT_FORBIDDEN`, а не пустая таблица «на удачу»;
5. ИИ-агент читает, но не проводит: отказ `AGENT_FORBIDDEN` — технический, а не
   договорённость;
6. бухгалтер закрывает месяц из браузера, и период действительно закрывается;
7. согласование идёт по маршруту: не та ступень — отказ, оплата — только после маршрута;
8. каждое действие (и каждый отказ) попадает в хэш-цепочку с ролью и IP;
9. форма без CSRF-токена не принимается.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pto_agent import ledger, payroll, stock, treasury
from pto_agent.web import build_demo, create_app
from pto_agent.weblogin import login_csrf as weblogin_login_csrf
from pto_agent.weblogin import login as web_login
from pto_agent.weblogin import page_csrf

REPO = Path(__file__).resolve().parents[1]
OBJECT = "ЖК «Северный», корпус 2"
OTHER_OBJECT = "ЖК «Северный», корпус 3"

PASSWORDS = {"prokhorov": "demo-prokhorov", "snab": "demo-snab", "pto": "demo-pto",
             "buh": "demo-buh", "dir": "demo-dir", "admin": "demo-admin",
             "agent": "demo-agent"}


@pytest.fixture(scope="session")
def demo_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("web-demo")
    build_demo(root)
    return root


@pytest.fixture()
def root(tmp_path, demo_root) -> Path:
    target = tmp_path / "out"
    shutil.copytree(demo_root, target)
    return target


@pytest.fixture()
def client(root) -> TestClient:
    return TestClient(create_app(root=root))


def login(client: TestClient, login_name: str) -> TestClient:
    """Вход тем же кодом, что и в `scripts/check_web.py` — см. `pto_agent/weblogin.py`.

    Раньше вход был написан дважды (здесь и в CI-скрипте), и CSRF-защита сломала обе
    копии: тесты починили, скрипт — нет. Теперь копия одна.
    """
    return web_login(client, login_name, PASSWORDS[login_name])


def login_csrf(client: TestClient) -> str:
    """Токен формы входа: логин защищён CSRF, как и остальные POST."""
    return weblogin_login_csrf(client)


def csrf(client: TestClient) -> str:
    """Токен формы берётся со сводки: она открыта всем ролям (там форма выхода)."""
    return page_csrf(client)


def state(root: Path, name: str) -> dict:
    return json.loads((root / name / "state.json").read_text(encoding="utf-8"))


# ─────────────────────────── вход и права ───────────────────────────
def test_anonymous_is_sent_to_login(client):
    response = client.get("/warehouse", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_wrong_password_explains_refusal(client):
    response = client.post("/login", data={"login": "prokhorov", "password": "не-тот",
                                           "csrf": login_csrf(client)})
    assert response.status_code == 401
    assert "неверный пароль" in response.text


def test_login_puts_role_on_screen(client):
    response = client.post("/login", data={"login": "prokhorov",
                                           "password": PASSWORDS["prokhorov"],
                                           "csrf": login_csrf(client)})
    assert "Прохоров" in response.text
    assert "Прораб участка" in response.text
    assert "Вход выполнен" in response.text


def test_forbidden_screen_is_403_with_code(client):
    login(client, "prokhorov")
    response = client.get("/closing")
    assert response.status_code == 403
    assert "FORBIDDEN" in response.text
    assert "closing.view" in response.text


def test_matrix_lives_in_data_file_not_in_template():
    payload = json.loads((REPO / "data" / "reference" / "rbac.json").read_text(
        encoding="utf-8"))
    assert payload["schema"] == "pto-agent/rbac@1"
    for role in ("прораб", "снабженец", "ПТО", "бухгалтер", "руководитель",
                 "администратор", "ИИ-агент"):
        assert role in payload["roles"], role
    admin = set(payload["roles"]["администратор"]["permissions"])
    assert not admin & {"postings.post", "closing.run", "request.pay"}, \
        "администратор не должен проводить документы"
    agent = set(payload["roles"]["ИИ-агент"]["permissions"])
    assert not agent & {"postings.post", "postings.accept", "closing.run",
                        "request.approve", "request.pay", "warehouse.issue"}


# ─────────────────────────── склад: требование прораба ───────────────────────────
def test_prorab_posts_issue_from_browser(client, root):
    login(client, "prokhorov")
    page = client.get("/warehouse/issue")
    assert page.status_code == 200
    token = csrf(client)
    before = state(root, "stock")
    response = client.post("/warehouse/issue", data={
        "csrf": token, "number": "М-11 №77", "date": "2026-10-30", "warehouse": "СК-1",
        "object": OBJECT, "item_1": "Бетон B25", "qty_1": "5",
        "item_2": "", "qty_2": "", "note": "захватка 3"},
        follow_redirects=False)
    assert response.status_code == 303, response.text
    assert "/warehouse/issue/done" in response.headers["location"]
    after = state(root, "stock")
    assert len(after["moves"]) > len(before["moves"])
    moves = [move for move in after["moves"] if move["doc_id"] == "М-11 №77"]
    assert sum(float(move["qty"]) for move in moves) == pytest.approx(5)
    assert all(move["item"] == "Бетон B25" for move in moves)


def test_issue_refused_when_stock_is_short(client, root):
    login(client, "prokhorov")
    token = csrf(client)
    before = state(root, "stock")
    response = client.post("/warehouse/issue", data={
        "csrf": token, "number": "М-11 №78", "date": "2026-10-30", "warehouse": "СК-1",
        "object": OBJECT, "item_1": "Арматура A500", "qty_1": "900",
        "item_2": "", "qty_2": ""})
    assert response.status_code == 200
    assert "NO_BATCH" in response.text or "NEGATIVE_STOCK" in response.text
    assert len(state(root, "stock")["moves"]) == len(before["moves"])


def test_issue_m11_form_is_printed_from_real_moves(client, root):
    login(client, "prokhorov")
    token = csrf(client)
    client.post("/warehouse/issue", data={
        "csrf": token, "number": "М-11 №79", "date": "2026-10-30", "warehouse": "СК-1",
        "object": OBJECT, "item_1": "Бетон B25", "qty_1": "2",
        "item_2": "", "qty_2": ""})
    response = client.get("/warehouse/issue/М-11 №79.docx")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument")


def test_foreign_object_is_refused(client, root):
    login(client, "prokhorov")
    token = csrf(client)
    before = state(root, "stock")
    response = client.post("/warehouse/issue", data={
        "csrf": token, "number": "М-11 №80", "date": "2026-10-30", "warehouse": "СК-1",
        "object": OTHER_OBJECT, "item_1": "Бетон B25", "qty_1": "1",
        "item_2": "", "qty_2": ""})
    assert response.status_code == 403
    assert "OBJECT_FORBIDDEN" in response.text
    assert len(state(root, "stock")["moves"]) == len(before["moves"])


def test_snabeshchenets_cannot_issue_materials(client):
    login(client, "snab")
    assert client.get("/warehouse/issue").status_code == 403


# ─────────────────────────── ИИ-агент ───────────────────────────
def test_agent_reads_but_does_not_post(client, root):
    login(client, "agent")
    assert client.get("/warehouse").status_code == 200
    token = csrf(client)
    before = len(state(root, "stock")["moves"])
    response = client.post("/warehouse/issue", data={
        "csrf": token, "number": "М-11 №81", "date": "2026-10-30", "warehouse": "СК-1",
        "object": OBJECT, "item_1": "Бетон B25", "qty_1": "1",
        "item_2": "", "qty_2": ""})
    assert response.status_code == 403
    assert "AGENT_FORBIDDEN" in response.text
    assert "только предлагает" in response.text
    assert len(state(root, "stock")["moves"]) == before


def test_agent_cannot_close_month(client, root):
    login(client, "agent")
    before = state(root, "ledger")
    response = client.post("/closing", data={"period": "2026-10", "csrf": ""})
    assert response.status_code == 403
    assert "AGENT_FORBIDDEN" in response.text
    assert state(root, "ledger")["periods"] == before["periods"]


# ─────────────────────────── CSRF ───────────────────────────
def test_form_without_csrf_token_is_refused(client, root):
    login(client, "buh")
    before = state(root, "ledger")
    response = client.post("/closing", data={"period": "2026-10", "csrf": "подделка"})
    assert response.status_code == 400
    assert "CSRF" in response.text
    assert state(root, "ledger")["periods"] == before["periods"]


# ─────────────────────────── заявки ───────────────────────────
def test_request_without_basis_is_not_created(client, root):
    login(client, "prokhorov")
    token = csrf(client)
    before = len(state(root, "treasury")["requests"])
    response = client.post("/requests/new", data={
        "csrf": token, "number": "З-777", "date": "2026-10-30",
        "counterparty": "ООО «Тест»", "amount": "1000", "article": "материалы",
        "basis_number": "", "basis_date": "", "object": OBJECT, "note": ""})
    assert response.status_code == 200
    assert "NO_BASIS" in response.text
    assert len(state(root, "treasury")["requests"]) == before


def test_request_is_created_with_basis(client, root):
    login(client, "prokhorov")
    token = csrf(client)
    response = client.post("/requests/new", data={
        "csrf": token, "number": "З-778", "date": "2026-10-30",
        "counterparty": "ООО «Тест»", "amount": "50000", "article": "материалы",
        "basis_number": "УПД №900", "basis_date": "2026-10-29", "object": OBJECT,
        "note": "проверка подачи из браузера"}, follow_redirects=False)
    assert response.status_code == 303
    numbers = [row["number"] for row in state(root, "treasury")["requests"]]
    assert "З-778" in numbers


def test_approval_goes_by_route_not_by_click(client, root):
    login(client, "pto")
    token = csrf(client)
    next_step = "снабжение"
    response = client.post("/requests/З-002/approve", data={"csrf": token, "comment": ""})
    assert response.status_code == 403, response.text
    assert "request.approve" in response.text
    row = treasury.find(state(root, "treasury"), "З-002")
    assert row["approvals"] == [], f"ступень «{next_step}» подписана не той ролью"


def test_full_route_then_payment(client, root):
    for login_name in ("snab", "pto", "dir"):
        token = csrf(login(client, login_name))
        response = client.post("/requests/З-002/approve", data={"csrf": token},
                               follow_redirects=False)
        assert response.status_code == 303, (login_name, response.text[:400])
    cash = state(root, "treasury")
    row = treasury.find(cash, "З-002")
    assert row["status"] == treasury.APPROVED
    assert [item["role"] for item in row["approvals"]] == ["снабжение", "ПТО", "финдиректор"]
    assert len({item["person"] for item in row["approvals"]}) == 3

    token = csrf(login(client, "buh"))
    response = client.post("/requests/З-002/pay", data={"csrf": token, "amount": "100000"},
                           follow_redirects=True)
    assert response.status_code == 200
    cash = state(root, "treasury")
    row = treasury.find(cash, "З-002")
    assert float(row["paid"]) == pytest.approx(100000)
    assert row["status"] == treasury.PARTIALLY_PAID


def test_prorab_cannot_pay(client, root):
    login(client, "prokhorov")
    token = csrf(client)
    response = client.post("/requests/З-002/pay", data={"csrf": token})
    assert response.status_code == 403
    assert "request.pay" in response.text


# ─────────────────────────── проводки и закрытие ───────────────────────────
def test_proposal_is_accepted_by_accountant_only(client, root):
    path = root / "ledger" / "state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["proposals"] = [{
        "id": "Пр-0001", "event": "overhead", "date": "2026-10-30",
        "doc": {"number": "счёт №900", "date": "2026-10-30"},
        "memo": "предложение по счёту №900",
        "lines": [{"account": "26", "side": "Дт", "amount": 60000,
                   "analytics": {"статья затрат": "аренда"}},
                  {"account": "60.01", "side": "Кт", "amount": 60000,
                   "analytics": {"контрагент": "ООО «Аренда»"}}]}]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    login(client, "snab")
    token = csrf(client)
    response = client.post("/postings/proposals/Пр-0001/accept", data={"csrf": token})
    assert response.status_code == 403
    assert state(root, "ledger")["proposals"][0]["id"] == "Пр-0001"

    token = csrf(login(client, "buh"))
    response = client.post("/postings/proposals/Пр-0001/accept", data={"csrf": token},
                           follow_redirects=False)
    assert response.status_code == 303
    ledger_state = state(root, "ledger")
    assert ledger_state["proposals"] == []
    assert ledger.verify(ledger_state)["ok"] is True


def test_accountant_closes_month_from_browser(client, root):
    login(client, "buh")
    token = csrf(client)
    response = client.post("/closing", data={"period": "2026-10", "csrf": token, "dry_run": ""})
    assert response.status_code == 200
    assert "Результат за 2026-10" in response.text
    payload = state(root, "ledger")
    closed = [row for row in payload["periods"] if row.get("closed")]
    assert [row["period"] for row in closed] == ["2026-10"]
    assert ledger.verify(payload)["ok"] is True


def test_closing_dry_run_does_not_change_journal(client, root):
    login(client, "buh")
    token = csrf(client)
    before = len(state(root, "ledger")["entries"])
    response = client.post("/closing", data={"period": "2026-10", "csrf": token, "dry_run": "1"})
    assert response.status_code == 200
    assert len(state(root, "ledger")["entries"]) == before
    assert not state(root, "ledger")["periods"]


# ─────────────────────────── аудит ───────────────────────────
def test_every_action_is_audited_with_role_and_ip(client, root):
    login(client, "prokhorov")
    token = csrf(client)
    client.post("/warehouse/issue", data={
        "csrf": token, "number": "М-11 №90", "date": "2026-10-30", "warehouse": "СК-1",
        "object": OBJECT, "item_1": "Бетон B25", "qty_1": "1",
        "item_2": "", "qty_2": ""})
    client.get("/closing")  # отказ
    records = client.get("/audit")
    assert records.status_code == 403, "прораб не должен видеть журнал аудита"

    login(client, "buh")
    page = client.get("/audit")
    assert page.status_code == 200
    assert "цепочка сходится" in page.text

    lines = [json.loads(line) for line in (root / "audit.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    actions = [row["action"] for row in lines]
    assert "аутентификация.вход" in actions
    assert "склад.требование" in actions
    assert "доступ.отказ" in actions
    issue = next(row for row in lines if row["action"] == "склад.требование")
    assert issue["actor"] == "prokhorov"
    assert issue["payload"]["role"] == "прораб"
    assert issue["payload"]["ip"], "у записи должен быть адрес, с которого пришло действие"
    assert issue["payload"]["object"] == OBJECT

    denied = next(row for row in lines if row["action"] == "доступ.отказ")
    assert denied["payload"]["code"] == "FORBIDDEN"


def test_audit_chain_is_verifiable(client, root):
    login(client, "buh")
    token = csrf(client)
    client.post("/closing", data={"period": "2026-10", "csrf": token, "dry_run": "1"})
    from pto_agent.audit import AuditLog
    ok, count, why = AuditLog(root / "audit.jsonl").verify()
    assert ok, why
    assert count >= 2


# ─────────────────────────── HTMX и фикстуры экрана ───────────────────────────
def test_warehouse_partial_is_a_fragment(client):
    login(client, "prokhorov")
    page = client.get("/warehouse/table", params={"warehouse": "СК-1"})
    assert page.status_code == 200
    assert "<html" not in page.text
    assert "stock-table" in page.text


def test_login_page_shows_demo_roles_for_operator(client):
    page = client.get("/login")
    assert page.status_code == 200
    assert "demo-prokhorov" in page.text
    assert "AD/LDAP" in page.text, "не сделано должно быть написано на экране, а не в отчёте"


# ─────────────────────────── 10. зарплата ───────────────────────────
def test_payroll_screen_shows_sheet_and_rates(client):
    login(client, "buh")
    page = client.get("/payroll")
    assert page.status_code == 200, page.text
    text = page.text
    assert "Т-51" in text, "ведомость должна быть на экране, а не только в CLI"
    assert "1 029 199,29" in text, "начисление за октябрь берётся из расчёта, а не рисуется"
    assert "131 377,91" in text, "НДФЛ за октябрь"
    assert "Не считается" in text, "на экране должно быть написано, чего нет"
    assert "больничные" in text, "причина «не включено» — тоже на экране"
    assert "2 979 000" in text or "30 %" in text


def test_payroll_period_switch_reads_saved_month(client):
    login(client, "buh")
    september = client.get("/payroll?period=2026-09")
    october = client.get("/payroll?period=2026-10")
    assert "910 000,00" in september.text, "сентябрь — по сохранённому расчёту"
    assert "1 029 199,29" in october.text


def test_payroll_screen_shows_withholdings_and_their_limit(client):
    """Удержания — на экране: предел 50 % назван, остаток показан числом, не сделано — вслух."""
    login(client, "buh")
    text = client.get("/payroll?period=2026-10").text
    assert "Удержания за 2026-10" in text, "блок удержаний не на экране"
    assert "после НДФЛ" in text, "база предела должна быть названа: сумма после НДФЛ"
    assert "50 %" in text and "20 %" in text and "70 %" in text
    assert "151 557,17" in text, "итог удержаний"
    assert "17 998,41" in text, "перенесённый остаток должен быть числом, а не забыт"
    assert "удержано в пределах лимита" in text
    assert "у документа нет номера" in text, "документ без номера не исполнен — и это видно"
    assert "Дт 70 Кт 76" in text
    assert "Не сделано по удержаниям" in text


def test_payroll_november_shows_maternity_and_severance(client):
    """Декрет и выходное пособие — на экране, с пометкой «не облагается» и с суммой СФР."""
    login(client, "buh")
    text = client.get("/payroll?period=2026-11").text
    assert "390 422,20" in text, "декретные (платит СФР) не показаны"
    assert "104 975,00" in text, "выходное пособие не показано"
    assert "в т.ч. не облагается" in text, "не сказано, что часть начисления не облагается"
    assert "декрет" in text
    assert "СФР, справочно" in text


def test_agent_sees_payroll_but_cannot_post_it(client):
    login(client, "agent")
    page = client.get("/payroll")
    assert page.status_code == 200
    assert "Провести начисление" not in page.text, "у ИИ-агента нет кнопки проведения"
    response = client.post("/payroll/post", data={"period": "2026-10", "csrf": csrf(client)})
    assert response.status_code == 403
    assert "AGENT_FORBIDDEN" in response.text, "отказ должен быть техническим, с кодом"


def test_foreman_cannot_post_payroll(client):
    login(client, "prokhorov")
    assert client.get("/payroll").status_code == 200
    response = client.post("/payroll/post", data={"period": "2026-10", "csrf": csrf(client)})
    assert response.status_code == 403
    assert "FORBIDDEN" in response.text


def test_accountant_posting_is_idempotent(root):
    """Проводки зарплаты уже сделаны демо-сборкой: повтор даёт «повторно», а не дубль."""
    client = TestClient(create_app(root=root))
    login(client, "buh")
    before = len(state(root, "ledger")["entries"])
    response = client.post("/payroll/post", data={"period": "2026-10", "csrf": csrf(client)},
                           follow_redirects=True)
    assert response.status_code == 200
    assert len(state(root, "ledger")["entries"]) == before, "повторное проведение создало дубль"
    assert "повторно" in response.text


def test_payroll_requests_go_to_treasury_with_route(root):
    client = TestClient(create_app(root=root))
    login(client, "buh")
    response = client.post("/payroll/requests", data={"period": "2026-10",
                                                      "date": "2026-11-05",
                                                      "csrf": csrf(client)},
                           follow_redirects=False)
    assert response.status_code == 303
    cash = state(root, "treasury")
    numbers = [row["number"] for row in cash["requests"] if row["number"].startswith("ЗП-")]
    assert len(numbers) == 7, f"заявок на выплату {len(numbers)}"
    salary = next(row for row in cash["requests"] if row["number"] == numbers[0])
    assert salary["route"] == ["руководитель"], "зарплату согласует руководитель, а не снабжение"
    assert salary["article"] == "зарплата"
    assert salary["basis"]["number"] == "Т-51 за 2026-10"


def test_agent_cannot_create_payroll_requests(client):
    login(client, "agent")
    response = client.post("/payroll/requests", data={"period": "2026-10", "csrf": csrf(client)})
    assert response.status_code == 403
    assert "AGENT_FORBIDDEN" in response.text


def test_taxes_screen_shows_all_reports_and_its_limits(client):
    login(client, "buh")
    response = client.get("/taxes")
    assert response.status_code == 200
    for marker in ("НДС за", "НДС с авансов", "Налог на прибыль", "6-НДФЛ",
                   "РСВ и травматизм", "Налог на имущество", "Не сделано"):
        assert marker in response.text, f"на экране налогов нет блока «{marker}»"
    assert "сдача отчётности" in response.text or "сдавать отчётность" in response.text


def test_taxes_screen_is_closed_for_prorab(client):
    login(client, "prokhorov")
    response = client.get("/taxes")
    assert response.status_code == 403


def test_taxes_are_not_sent_from_screen(client):
    """Сдавать отчётность с экрана нечем: подписи и формата ФНС нет — и это написано."""
    login(client, "buh")
    page = client.get("/taxes").text
    assert "УКЭП не подключена" in page
    assert "XSD" in page


def test_taxes_screen_shows_property_tax_with_sources(client):
    """Имущество считается за год: авансы по срокам, налог за год и «что не сделано»."""
    login(client, "buh")
    page = client.get("/taxes?year=2026").text
    assert "Офис в бизнес-центре" in page, "объект по кадастру должен быть в таблице"
    assert "2 427 540,00" in page, "налог за год из регистра"
    assert "613 196,00" in page, "аванс за I квартал"
    assert "не облагается" in page, "движимое имущество отсекается с причиной"
    assert "Что не сделано по имуществу" in page, "не сделано пишется на экране"
    assert "статьи 380" in page or "льгот" in page, "про льготы должно быть сказано прямо"


def test_taxes_screen_property_reports_missing_registry_honestly(client, root):
    """Если регистра нет — экран говорит об этом, а не показывает нули."""
    import json as _json
    path = root / "property" / "assets.json"
    saved = path.read_text(encoding="utf-8")
    try:
        path.write_text(_json.dumps({"assets": []}, ensure_ascii=False), encoding="utf-8")
        login(client, "buh")
        page = client.get("/taxes?year=2026").text
        assert "Расчёт не готов" in page
        assert "регистр недвижимости" in page
    finally:
        path.write_text(saved, encoding="utf-8")


def test_taxes_screen_shows_sales_book_and_its_gap(client):
    """Книга продаж на экране: реестр пуст — это сказано, а не скрыто."""
    login(client, "buh")
    page = client.get("/taxes?period=2026-10").text
    assert "Книга продаж за 2026-10" in page
    assert "В реестре нет ни одного счёта-фактуры" in page
    assert "Не сделано по счетам-фактурам" in page
    assert "Книга покупок за 2026-10" in page, "книга покупок должна быть на экране"
    assert "книга покупок помечена «на проверку»" in page, "предел книги должен быть на экране"


def test_invoices_can_be_issued_from_screen(client, root):
    """Кнопка на экране нумерует документы и пишет реестр; повтор не задваивает."""
    import json as _json

    from pto_agent import ledger
    state = ledger.load(root / "ledger" / "state.json")
    ledger.post_entry(state, event="act_signed", date="2026-10-29",
                      doc={"number": "КС-2 №77", "date": "2026-10-29", "kind": "ks2"},
                      lines=[{"account": "62.01", "side": ledger.DEBIT, "amount": 12_200_000,
                              "analytics": {"контрагент": "ООО «Заказчик-Инвест»",
                                            "договор": "Д-142/2026"}},
                             {"account": "90.03", "side": ledger.DEBIT, "amount": 2_200_000,
                              "analytics": {"контрагент": "ООО «Заказчик-Инвест»",
                                            "договор": "Д-142/2026"}},
                             {"account": "68.02", "side": ledger.CREDIT, "amount": 2_200_000,
                              "analytics": {}},
                             {"account": "90.01", "side": ledger.CREDIT, "amount": 12_200_000,
                              "analytics": {"объект": OBJECT, "договор": "Д-142/2026"}}],
                      memo="акт для книги продаж", rule="act.signed",
                      chart=ledger.load_chart(), rules=ledger.load_rules())
    ledger.save(state, root / "ledger" / "state.json")

    login(client, "buh")
    token = csrf(client)
    response = client.post("/taxes/invoices", data={"period": "2026-10", "csrf": token},
                           follow_redirects=False)
    assert response.status_code == 303
    registry = _json.loads((root / "invoices" / "registry.json").read_text(encoding="utf-8"))
    assert len(registry["issued"]) == 1
    assert registry["issued"][0]["number"] == "1"
    assert registry["issued"][0]["base_number"] == "КС-2 №77"

    again = client.post("/taxes/invoices", data={"period": "2026-10", "csrf": csrf(client)},
                        follow_redirects=False)
    assert again.status_code == 303
    registry = _json.loads((root / "invoices" / "registry.json").read_text(encoding="utf-8"))
    assert len(registry["issued"]) == 1, "повторное нажатие не должно выдавать второй номер"
    page = client.get("/taxes?period=2026-10").text
    assert "КС-2 №77" in page


# ─────────────── недельный календарь на экране заявок (круг 40) ───────────────
def test_requests_screen_shows_week_calendar_with_disclaimer(client, root):
    login(client, "buh")
    response = client.get("/requests")
    assert response.status_code == 200
    text = response.text
    assert "Платёжный календарь по неделям" in text
    assert "2026-W42" in text                       # неделя с пометкой ISO, а не «1–7»
    assert "остаток (факт)" in text.lower()
    assert "Лимиты объектов нарастающим итогом" in text
    # без недельных лимитов экран обязан сказать об этом, а не показать придуманные
    assert ("недельные лимиты по объектам не заданы" in text
            or "Недельные лимиты заданы организацией" in text)
    assert "неполная" in text                      # границы месяца видны, а не замазаны


def test_week_calendar_is_the_same_money_as_the_api(client, root):
    login(client, "buh")
    text = client.get("/requests").text
    report = treasury.weeks(state(root, "treasury"), "2026-10")
    assert treasury.rub(report["closing_confirmed"]) in text
    assert treasury.rub(report["closing"]) in text


def test_hourly_timesheet_is_shown_in_hours_not_days(tmp_path, demo_root):
    """Суммированный учёт на экране: часы, сверхурочные, ночные и праздничные видны.

    Без этого экран показывал бы «дни» для режима, где дней в табеле нет, — и ведомость
    выглядела бы без переработки, хотя она посчитана.
    """
    target = tmp_path / "out"
    shutil.copytree(demo_root, target)
    hours_demo = tmp_path / "demo-hours"
    shutil.copytree(REPO / "data" / "demo", hours_demo, dirs_exist_ok=True)
    shutil.copy(REPO / "data" / "demo" / "payroll" / "timesheet_hours_2026_11.json",
                hours_demo / "payroll" / "timesheet_2026_11.json")
    state_path = target / "payroll" / "state.json"
    assert state_path.exists(), "демо-состояние зарплаты не собрано"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    rules = payroll.load_rules()
    staff = json.loads((hours_demo / "payroll" / "staff.json").read_text(encoding="utf-8"))
    hours_sheet = json.loads((hours_demo / "payroll" / "timesheet_2026_11.json")
                             .read_text(encoding="utf-8"))
    # демо уже начислило ноябрь по дням; повтор не создаёт начислений — снимаем старый месяц
    state["periods"] = [item for item in state["periods"] if item["period"] != "2026-11"]
    lifecycle = json.loads((hours_demo / "payroll" / "orders_lifecycle_2026.json")
                           .read_text(encoding="utf-8"))["orders"]
    report = payroll.accrue(state, hours_sheet, staff, rules, period="2026-11",
                            orders=lifecycle)
    assert not report["errors"], report["errors"]
    payroll.remember(state, report, rules=rules)
    payroll.save_state(state, state_path)
    row = next(item for item in state["periods"] if item["period"] == "2026-11")
    assert any(line.get("hourly_rate") for line in row["rows"]), "в ведомости нет часового расчёта"
    client = TestClient(create_app(root=target, demo_dir=hours_demo))
    login(client, "buh")
    text = client.get("/payroll?period=2026-11").text
    assert "168.0 / 160.0 ч." in text          # часы вместо дней
    assert "сверхурочные" in text and "ночные" in text and "праздничные" in text
    assert "часовая ставка — на проверку" in text
    assert "из них ночных" in text              # табель в часах
    assert "уход до 1,5 лет" in text            # СФР справочно


def test_statements_screen_shows_balance_and_honest_gaps(client, root):
    """Экран отчётности: те же цифры, что в CLI, и «не сделано» — на экране, а не только в отчёте."""
    login(client, "buh")
    response = client.get("/statements?year=2026")
    assert response.status_code == 200
    text = response.text
    assert "ФСБУ 4/2023" in text and "157н" in text
    assert "1600 баланс (актив)" in text and "1700 баланс (пассив)" in text
    assert "Отчёт о финансовых результатах" in text
    assert "не заполняется" in text                    # пустая строка с причиной
    assert "Не сделано (и почему)" in text
    assert "ПБУ 18/02" in text
    # круг 44: налог виден на экране, и ОДК/ОДДС названы «не строятся» с причиной
    assert "Налог на прибыль" in text and "расхождение" in text
    assert "Чего не хватает" in text and "ОДДС" in text


def test_statements_screen_has_prior_year_column(client, root):
    """Круг 45: на экране отчётности видна графа прошлого года — или причина, почему пусто."""
    login(client, "buh")
    text = client.get("/statements?year=2026").text
    assert "на 31.12.2025" in text and "на 31.12.2026" in text
    if "первый отчётный год" not in text:
        # прошлый год есть: колонка заполнена числами прошлого года
        assert "на 31.12.2025, ₽" in text
    assert "Годом ранее" in text or "первый отчётный год" in text


# ─────────────── вход один на всех: тесты и шаги CI не расходятся ───────────────
def test_login_without_csrf_is_refused_with_400(client):
    """Без токена форма входа отвечает 400 — и это то, что сломало CI 22.11."""
    response = client.post("/login", data={"login": "buh", "password": PASSWORDS["buh"]})
    assert response.status_code == 400
    assert "csfr" in response.text.lower() or "csrf" in response.text.lower()


def test_login_helper_reports_refusal_with_code(client):
    """Неверный пароль: отказ приходит с кодом в сообщении, а не молчаливым assert."""
    from pto_agent.weblogin import LoginRefused, login as web_login_plain

    with pytest.raises(LoginRefused) as caught:
        web_login_plain(client, "buh", "не-тот-пароль")
    assert caught.value.status == 401
    assert "HTTP 401" in str(caught.value)
    assert "пароль" in str(caught.value)


def test_login_helper_reports_csrf_change_as_words():
    """Если форма входа изменится, ошибка скажет об этом, а не упадёт индексом строки."""

    class NoForm:
        class _Response:
            status_code = 200
            text = "<form>без токена</form>"

        def get(self, _page):
            return self._Response()

    from pto_agent.weblogin import LoginRefused, login as web_login_plain

    with pytest.raises(LoginRefused) as caught:
        web_login_plain(NoForm(), "buh", "что-угодно")
    assert "форма входа изменилась" in str(caught.value)


def test_all_web_entries_share_one_login_helper():
    """CSRF сломала вход дважды, потому что он был написан дважды. Копия должна быть одна.

    Проверка читает сами файлы. Если в тестах или в CI-скрипте снова появится разбор
    формы входа своей копией, шаг CI и тесты снова разойдутся молча — как 22.11.
    """
    from pto_agent.weblogin import CSRF_MARKER as marker  # маркер живёт в одном месте
    for name in ("tests/test_web.py", "scripts/check_web.py"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "weblogin" in text, f"{name} не использует общий вход из pto_agent.weblogin"
        assert marker not in text, (
            f"{name} снова разбирает форму входа сам — используйте pto_agent.weblogin")
