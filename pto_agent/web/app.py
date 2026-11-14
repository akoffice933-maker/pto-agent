# -*- coding: utf-8 -*-
"""Серверный интерфейс: FastAPI + Jinja2 + HTMX.

Правила этого слоя:
1. Ни одна цифра не считается здесь — всё берётся из модулей контура (`stock`,
   `treasury`, `ledger`, `closing`, `forms`). Экран показывает, а не вычисляет.
2. Права проверяются на сервере по файлу матрицы (`rbac.json`); шаблон только
   прячет кнопки. Отказ — 403 с кодом и причиной.
3. ИИ-агент не проводит документы. Это не договорённость: у роли нет прав, и любой
   пишущий обработчик дополнительно отвергает сервисного актора (`AGENT_FORBIDDEN`).
4. Каждое действие пишется в журнал аудита: кто, роль, что, объект, IP.
"""
from __future__ import annotations

import os
import secrets
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import closing, forms, ledger, stock, treasury
from . import auth, rbac
from .store import Store

HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))

STATUS_LABELS = {
    stock.STATUS_OK: "проведено",
    "отклонено": "отклонено",
    treasury.ON_APPROVAL: "на согласовании",
    treasury.APPROVED: "согласована",
    treasury.PAID: "оплачена",
    treasury.PARTIALLY_PAID: "оплачена частично",
    treasury.BLOCKED: "заблокирована лимитом",
    treasury.REJECTED: "отклонена",
}


# ─────────────────────────── шаблонные фильтры ───────────────────────────
def _rub(value) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} ₽"


def _qty(value) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number:,.3f}".replace(",", " ").replace(".", ",")
    text = text.rstrip("0").rstrip(",")
    return text or "0"


def _pct(value) -> str:
    try:
        return f"{float(value) * 100:.1f} %".replace(".", ",")
    except (TypeError, ValueError):
        return "—"


def _short(value, limit: int = 60) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


TEMPLATES.env.filters["rub"] = _rub
TEMPLATES.env.filters["qty"] = _qty
TEMPLATES.env.filters["pct"] = _pct
TEMPLATES.env.filters["short"] = _short
TEMPLATES.env.globals["status_label"] = lambda value: STATUS_LABELS.get(value, value)


# ─────────────────────────── отказы ───────────────────────────
class Denied(Exception):
    """Отказ сервера: всегда с кодом, всегда с причиной."""

    def __init__(self, code: str, why: str, *, action: str = "", entity: str = "",
                 status: int = 403):
        super().__init__(why)
        self.code = code
        self.why = why
        self.action = action
        self.entity = entity
        self.status = status


def create_app(*, root: str | Path | None = None, data_dir: str | Path | None = None,
               demo_dir: str | Path | None = None, secret: str | None = None) -> FastAPI:
    root = Path(root or os.environ.get("PTO_WEB_ROOT") or "out")
    data_dir = Path(data_dir or os.environ.get("PTO_WEB_DATA")
                    or Path(__file__).resolve().parents[2] / "data" / "reference")
    demo_dir = Path(demo_dir or os.environ.get("PTO_WEB_DEMO") or
                    Path(__file__).resolve().parents[2] / "data" / "demo")
    store = Store(root, data_dir=data_dir, demo_dir=demo_dir)
    matrix = rbac.Matrix(rbac.load(store.rbac_path))

    app = FastAPI(title="ИИ-агент ПТО · экраны учёта", docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.add_middleware(SessionMiddleware, secret_key=secret or secrets.token_urlsafe(32),
                       session_cookie="pto_session", same_site="lax", https_only=False)
    app.state.store = store
    app.state.matrix = matrix
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ── контекст запроса ──────────────────────────────────────────
    def current_user(request: Request) -> dict | None:
        login = request.session.get("login")
        if not login:
            return None
        payload = auth.load(store.users_path)
        user = auth.index(payload).get(login)
        return auth.public(user) if user and user.get("active", True) else None

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(24)
            request.session["csrf"] = token
        return token

    def _flash(request: Request, kind: str, text: str) -> None:
        messages = list(request.session.get("flash") or [])
        messages.append({"kind": kind, "text": text})
        request.session["flash"] = messages[-5:]

    def _pop_flash(request: Request) -> list[dict]:
        messages = list(request.session.get("flash") or [])
        request.session["flash"] = []
        return messages

    def _client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else ""

    def _audit(request: Request, user: dict | None, action: str, entity: str,
               payload: dict | None = None) -> None:
        body = dict(payload or {})
        body.setdefault("role", (user or {}).get("role", ""))
        body.setdefault("ip", _client_ip(request))
        body.setdefault("path", request.url.path)
        body.setdefault("object", (user or {}).get("object") or "")
        store.audit.append((user or {}).get("login") or "аноним",
                           "человек" if (user or {}).get("kind", "human") == "human"
                           else "agent",
                           action, entity, body)

    def _require(request: Request, action: str, *, step: str | None = None) -> dict:
        user = current_user(request)
        if not user:
            raise Denied("UNAUTHENTICATED", "нужен вход в систему", action=action,
                         status=401)
        if not matrix.can(str(user.get("role")), action, step=step):
            raise Denied("FORBIDDEN", matrix.why_not(str(user.get("role")), action, step=step),
                         action=action)
        return user

    def _guard(request: Request, action: str, *, check_permission: bool = True) -> dict:
        """Путь для пишущих обработчиков: сначала «человек ли», потом «можно ли».

        Порядок важен: сервисный актор получает именно `AGENT_FORBIDDEN`, а не
        размытое «нет права» — так видно, что ограничение техническое.
        """
        user = current_user(request)
        if not user:
            raise Denied("UNAUTHENTICATED", "нужен вход в систему", action=action, status=401)
        _require_human(user, action)
        if check_permission and not matrix.can(str(user.get("role")), action):
            raise Denied("FORBIDDEN", matrix.why_not(str(user.get("role")), action),
                         action=action)
        return user

    def _require_human(user: dict, action: str) -> None:
        """ИИ-агент и любой сервисный актор не проводят документы — на уровне кода."""
        if str(user.get("kind") or "human") != "human" or user.get("role") == "ИИ-агент":
            raise Denied("AGENT_FORBIDDEN",
                         "ИИ-агент не проводит документы, не согласовывает и не закрывает "
                         "период: он только предлагает и готовит черновики. Проводку делает "
                         "человек с ролью.", action=action)

    def _require_object(user: dict, object_name: str, action: str) -> None:
        objects = list(user.get("objects") or [])
        if not matrix.object_allowed(str(user.get("role")), objects, object_name):
            raise Denied("OBJECT_FORBIDDEN",
                         f"объект «{object_name}» не закреплён за {user.get('name')}: "
                         f"доступны {', '.join(objects) or '—'}", action=action)

    def _check_csrf(request: Request, token: str) -> None:
        expected = str(request.session.get("csrf") or "")
        given = str(token or "")
        # сравниваем байты: compare_digest на строках с не-ASCII падает, а токен приходит
        # из формы, то есть от пользователя — падать на нём нельзя
        if not expected or not secrets.compare_digest(given.encode("utf-8"),
                                                      expected.encode("utf-8")):
            raise Denied("CSRF", "форма устарела или пришла не из этого сеанса: обновите "
                                 "страницу и повторите", status=400)

    def _ctx(request: Request, user: dict | None, **extra) -> dict:
        base = {
            "request": request,
            "user": user,
            "role_title": matrix.role_title(str((user or {}).get("role") or "")),
            "can": (lambda action, step=None: bool(user)
                    and matrix.can(str(user.get("role")), action, step=step)),
            "csrf": csrf_token(request),
            "flash": _pop_flash(request),
            "nav": _nav(user),
            "today": _date.today().isoformat(),
            "matrix": matrix,
        }
        base.update(extra)
        return base

    def _nav(user: dict | None) -> list[dict]:
        if not user:
            return []
        allowed = matrix.allowed(str(user.get("role")))
        items = [
            ("dashboard.view", "/", "Сводка"),
            ("warehouse.view", "/warehouse", "Склад"),
            ("act.view", "/acts", "Акты КС-2"),
            ("request.view", "/requests", "Заявки"),
            ("postings.view", "/postings", "Проводки и ОСВ"),
            ("closing.view", "/closing", "Закрытие месяца"),
            ("audit.view", "/audit", "Аудит"),
            ("admin.rbac", "/roles", "Роли"),
        ]
        return [{"href": href, "title": title, "action": action}
                for action, href, title in items if action in allowed]

    def render(request: Request, template: str, user: dict | None, **extra):
        return TEMPLATES.TemplateResponse(request, template, _ctx(request, user, **extra))

    # ── обработчики ошибок ────────────────────────────────────────
    @app.exception_handler(Denied)
    async def _denied_handler(request: Request, exc: Denied):
        user = current_user(request)
        _audit(request, user, "доступ.отказ", exc.action or request.url.path,
               {"code": exc.code, "why": exc.why, "status": exc.status})
        if exc.status == 401:
            target = request.url.path
            query = f"?{urlencode({'next': target})}" if target not in ("/", "") else ""
            return RedirectResponse(f"/login{query}", status_code=303)
        return TEMPLATES.TemplateResponse(
            request, "error.html", _ctx(request, user, code=exc.code, why=exc.why,
                                        action=exc.action), status_code=exc.status)

    @app.exception_handler(Exception)
    async def _oops_handler(request: Request, exc: Exception):  # pragma: no cover
        return TEMPLATES.TemplateResponse(
            request, "error.html",
            _ctx(request, current_user(request), code="ОШИБКА",
                 why=f"{type(exc).__name__}: {exc}", action=""),
            status_code=500)

    # ─────────────────────────── вход ───────────────────────────
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        user = current_user(request)
        if user:
            return RedirectResponse("/", status_code=303)
        payload = auth.load(store.users_path)
        return TEMPLATES.TemplateResponse(
            request, "login.html",
            _ctx(request, None, demo=payload.get("demo_logins") or [], error="", login=""))

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        login = str(form.get("login") or "").strip()
        password = str(form.get("password") or "")
        payload = auth.load(store.users_path)
        user, why = auth.authenticate(payload, login, password)
        if not user:
            _audit(request, {"login": login or "аноним"}, "аутентификация.отказ", login,
                   {"why": why})
            return TEMPLATES.TemplateResponse(
                request, "login.html",
                _ctx(request, None, demo=payload.get("demo_logins") or [],
                     error=why, login=login), status_code=401)
        public = auth.public(user)
        request.session.clear()
        request.session["login"] = public["login"]
        request.session["csrf"] = secrets.token_urlsafe(24)
        _audit(request, public, "аутентификация.вход", public["login"],
               {"role": public.get("role")})
        _flash(request, "ok", f"Вход выполнен: {public.get('name')} · "
                              f"{matrix.role_title(str(public.get('role')))}")
        target = str(request.query_params.get("next") or "/")
        return RedirectResponse(target if target.startswith("/") else "/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        user = current_user(request)
        _audit(request, user, "аутентификация.выход", (user or {}).get("login") or "")
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ─────────────────────────── сводка ───────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        user = current_user(request)
        if not user:
            raise Denied("UNAUTHENTICATED", "нужен вход в систему", status=401)
        if not matrix.allowed(str(user.get("role"))):
            raise Denied("FORBIDDEN",
                         f"у роли «{matrix.role_title(str(user.get('role')))}» нет ни одного "
                         f"экранного права", action="dashboard.view")
        period = request.query_params.get("period") or ""
        summary = store.summary(period=period or _default_period(store),
                               date=_date.today().isoformat())
        requests = store.treasury_state().get("requests") or []
        open_rows = [row for row in requests
                     if row.get("status") not in (treasury.PAID, treasury.REJECTED)][:6]
        history = store.audit.history(limit=6) if store.audit.path.exists() else []
        return render(request, "dashboard.html", user, summary=summary,
                      open_requests=open_rows, history=list(reversed(history)))

    # ─────────────────────────── склад ───────────────────────────
    @app.get("/warehouse", response_class=HTMLResponse)
    def warehouse(request: Request):
        user = _require(request, "warehouse.view")
        state = store.stock_state()
        warehouse_code = request.query_params.get("warehouse") or ""
        rows = stock.balances(state, warehouse=warehouse_code or None)
        if warehouse_code:
            _check_warehouse_allowed(state, rows, user, matrix)
        documents = list(reversed(state.get("documents") or []))[:12]
        return render(request, "warehouse.html", user,
                      rows=rows, documents=documents,
                      warehouses=state.get("warehouses") or [],
                      items=state.get("items") or [],
                      warehouse_code=warehouse_code,
                      total=round(sum(float(row.get("amount") or 0) for row in rows), 2),
                      object_name=state.get("object") or "")

    @app.get("/warehouse/table", response_class=HTMLResponse)
    def warehouse_table(request: Request):
        user = _require(request, "warehouse.view")
        state = store.stock_state()
        warehouse_code = request.query_params.get("warehouse") or ""
        rows = stock.balances(state, warehouse=warehouse_code or None)
        return render(request, "partials/stock_table.html", user, rows=rows,
                      warehouse_code=warehouse_code,
                      total=round(sum(float(row.get("amount") or 0) for row in rows), 2))

    @app.get("/warehouse/issue", response_class=HTMLResponse)
    def issue_form(request: Request):
        user = _require(request, "warehouse.issue")
        state = store.stock_state()
        return render(request, "warehouse_issue.html", user,
                      state=state, result=None,
                      objects=_objects_for(user, matrix, state),
                      form={"number": _next_issue_number(state),
                            "date": _date.today().isoformat(),
                            "warehouse": (state.get("warehouses") or [{}])[0].get("code", ""),
                            "object": _objects_for(user, matrix, state)[0]
                            if _objects_for(user, matrix, state) else "",
                            "note": ""},
                      lines=[{"item": "", "qty": ""} for _ in range(3)])

    @app.post("/warehouse/issue", response_class=HTMLResponse)
    async def issue_submit(request: Request):
        user = _guard(request, "warehouse.issue")
        form = await request.form()
        _check_csrf(request, str(form.get("csrf") or ""))
        state = store.stock_state()
        objects = _objects_for(user, matrix, state)
        number = str(form.get("number") or "").strip()
        date = str(form.get("date") or "").strip()
        warehouse_code = str(form.get("warehouse") or "").strip()
        object_name = str(form.get("object") or "").strip()
        note = str(form.get("note") or "").strip()
        raw_lines = [{"item": str(form.get(f"item_{index}") or "").strip(),
                      "qty": str(form.get(f"qty_{index}") or "").strip()}
                     for index in range(1, 6)]
        lines = []
        for row in raw_lines:
            if not row["item"] and not row["qty"]:
                continue
            info = stock.item_info(state, row["item"])
            quantity = stock.dec(row["qty"].replace(",", "."))
            lines.append({"item": row["item"], "qty": quantity,
                          "unit": info.get("unit") or "", "object": object_name,
                          "note": note or "требование прораба"})
        shown = {"number": number, "date": date, "warehouse": warehouse_code,
                 "object": object_name, "note": note}
        if not number or not date or not warehouse_code:
            return render(request, "warehouse_issue.html", user, state=state, result=None,
                          objects=objects, form=shown, lines=raw_lines,
                          problem="номер, дата и склад обязательны")
        if not object_name or object_name not in objects:
            raise Denied("OBJECT_FORBIDDEN",
                         f"требование подаётся по объекту из списка доступных: "
                         f"{', '.join(objects) or '—'}", action="warehouse.issue")
        if not lines or any(not (row.get("qty") or 0) > 0 for row in lines):
            return render(request, "warehouse_issue.html", user, state=state, result=None,
                          objects=objects, form=shown, lines=raw_lines,
                          problem="нужна хотя бы одна строка с количеством больше нуля")
        doc = {"kind": "issue", "number": number, "date": date,
               "warehouse": warehouse_code, "object": object_name, "lines": lines,
               "basis": str(form.get("basis") or "")}
        with store.lock:
            result = stock.post_document(state, doc)
            if result["status"] == stock.STATUS_OK:
                store.save_stock(state)
        _audit(request, user, "склад.требование", number,
               {"object": object_name, "warehouse": warehouse_code, "date": date,
                "lines": [{"item": row["item"], "qty": float(row["qty"])}
                          for row in lines],
                "status": result["status"], "moves": len(result["moves"])})
        if result["status"] != stock.STATUS_OK:
            return render(request, "warehouse_issue.html", user, state=state,
                          result=result, objects=objects, form=shown, lines=raw_lines)
        _flash(request, "ok", f"Требование {number} проведено: движений {len(result['moves'])}")
        return RedirectResponse(f"/warehouse/issue/done?number={quote(number)}",
                                status_code=303)

    @app.get("/warehouse/issue/done", response_class=HTMLResponse)
    def issue_done(request: Request):
        user = _require(request, "warehouse.issue")
        number = request.query_params.get("number") or ""
        state = store.stock_state()
        moves = stock.moves_of(state)
        rows = [move for move in moves if move.get("doc_id") == number]
        return render(request, "warehouse_done.html", user, number=number, moves=rows,
                      warnings=[], gaps=[])

    @app.get("/warehouse/issue/{number}.docx")
    def issue_form_docx(request: Request, number: str):
        user = _require(request, "warehouse.issue")
        state = store.stock_state()
        moves = [move for move in stock.moves_of(state) if move.get("doc_id") == number]
        if not moves:
            raise Denied("NO_DOCUMENT", f"движений по требованию {number} нет: бланк печатается "
                                       f"по проведённому документу", status=404)
        doc = {"kind": "issue", "number": number, "date": moves[0].get("date") or "",
               "warehouse": moves[0].get("warehouse") or "",
               "lines": [{"item": move["item"], "qty": move.get("qty"),
                          "unit": move.get("unit"), "price": move.get("price")}
                         for move in moves]}
        form = forms.build_stock_form("m11", doc, state=state)
        target = store.root / "forms" / f"m11_{_safe(number)}.docx"
        target.parent.mkdir(parents=True, exist_ok=True)
        forms.to_docx(form, target)
        _audit(request, user, "склад.бланк", number, {"form": "М-11"})
        return FileResponse(str(target), filename=target.name,
                            media_type="application/vnd.openxmlformats-officedocument."
                                       "wordprocessingml.document")

    # ─────────────────────────── акты ───────────────────────────
    @app.get("/acts", response_class=HTMLResponse)
    def acts(request: Request):
        user = _require(request, "act.view")
        payload = store.acts()
        rows = list(payload.get("acts") or [])
        ledger_state = store.ledger_state()
        posted = {str((entry.get("doc") or {}).get("number") or "")
                  for entry in ledger_state.get("entries") or []
                  if entry.get("event") == "act_signed"}
        for row in rows:
            row["posted"] = str(row.get("act_id")) in posted
        return render(request, "acts.html", user, rows=rows,
                      note=payload.get("note") or "", object_name=payload.get("object") or "",
                      contract=payload.get("contract") or "")

    # ─────────────────────────── заявки ───────────────────────────
    @app.get("/requests", response_class=HTMLResponse)
    def requests_list(request: Request):
        user = _require(request, "request.view")
        state = store.treasury_state()
        rules = treasury.load_rules()
        rows = _requests_view(state, user, matrix)
        return render(request, "requests.html", user, rows=rows,
                      articles=_articles(rules), state_object=state.get("object") or "",
                      objects=_objects_for(user, matrix, store.stock_state()))

    @app.get("/requests/table", response_class=HTMLResponse)
    def requests_table(request: Request):
        user = _require(request, "request.view")
        state = store.treasury_state()
        return render(request, "partials/requests_table.html", user,
                      rows=_requests_view(state, user, matrix))

    @app.get("/requests/new", response_class=HTMLResponse)
    def request_form(request: Request):
        user = _require(request, "request.create")
        rules = treasury.load_rules()
        state = store.treasury_state()
        objects = _objects_for(user, matrix, store.stock_state()) or [state.get("object") or ""]
        return render(request, "request_new.html", user, result=None,
                      articles=_articles(rules), objects=objects,
                      form={"number": _next_request_number(state),
                            "date": _date.today().isoformat(),
                            "counterparty": "", "amount": "", "article": "материалы",
                            "basis_number": "", "basis_date": "",
                            "object": objects[0] if objects else "", "note": ""})

    @app.post("/requests/new", response_class=HTMLResponse)
    async def request_submit(request: Request):
        user = _guard(request, "request.create")
        form = await request.form()
        _check_csrf(request, str(form.get("csrf") or ""))
        rules = treasury.load_rules()
        state = store.treasury_state()
        shown = {key: str(form.get(key) or "") for key in
                 ("number", "date", "counterparty", "amount", "article", "basis_number",
                  "basis_date", "object", "note")}
        objects = _objects_for(user, matrix, store.stock_state())
        object_name = shown["object"] or state.get("object") or ""
        try:
            _require_object(user, object_name, "request.create")
        except Denied as exc:
            return render(request, "request_new.html", user, result=None,
                          articles=_articles(rules), objects=objects, form=shown,
                          problem=exc.why)
        payload = {
            "number": shown["number"], "date": shown["date"],
            "counterparty": shown["counterparty"],
            "amount": _number(shown["amount"]), "article": shown["article"],
            "object": object_name,
            "basis": {"number": shown["basis_number"], "date": shown["basis_date"]},
            "note": shown["note"], "author": user.get("name"),
        }
        with store.lock:
            result = treasury.add_request(state, payload, rules=rules)
            if result.get("request"):
                store.save_treasury(state)
        _audit(request, user, "заявка.подана", shown["number"],
               {"object": object_name, "amount": payload["amount"],
                "article": shown["article"], "status": result.get("status", ""),
                "errors": [item.get("code") for item in result.get("errors") or []]})
        if result.get("request"):
            _flash(request, "ok", f"Заявка {shown['number']} принята: {result['status']}, "
                                  f"маршрут {(result.get('request') or {}).get('route_id')}")
            return RedirectResponse("/requests", status_code=303)
        return render(request, "request_new.html", user, result=result,
                      articles=_articles(rules), objects=objects, form=shown)

    @app.post("/requests/{number}/approve")
    async def request_approve(request: Request, number: str):
        # право на ступень зависит от маршрута, поэтому базовая проверка здесь
        # отложена: ступень проверяется ниже, после того как известно, чья она
        user = _guard(request, "request.approve", check_permission=False)
        form = await request.form()
        _check_csrf(request, str(form.get("csrf") or ""))
        state = store.treasury_state()
        row = treasury.find(state, number)
        if not row:
            raise Denied("NO_REQUEST", f"заявки {number} нет", status=404)
        route = row.get("route") or []
        done = row.get("approvals") or []
        if len(done) >= len(route):
            _flash(request, "warn", f"{number}: маршрут уже пройден")
            return RedirectResponse("/requests", status_code=303)
        step = route[len(done)]
        _require(request, "request.approve", step=str(step))
        comment = str(form.get("comment") or "")
        with store.lock:
            result = treasury.approve(state, number, role=str(step), person=str(user.get("name")),
                                      date=str(form.get("date") or _date.today().isoformat()),
                                      comment=comment)
            if result.get("status") in (treasury.APPROVED, treasury.ON_APPROVAL):
                store.save_treasury(state)
        _audit(request, user, "заявка.согласована", number,
               {"step": step, "result": result.get("status"),
                "errors": [item.get("code") for item in result.get("errors") or []]})
        if result.get("errors"):
            _flash(request, "err", f"{number}: {result['errors'][0]['why']}")
        else:
            _flash(request, "ok", f"{number}: ступень «{step}» согласована, статус "
                                  f"{status_text(result.get('status'))}")
        return RedirectResponse("/requests", status_code=303)

    @app.post("/requests/{number}/reject")
    async def request_reject(request: Request, number: str):
        # право на ступень зависит от маршрута, поэтому базовая проверка здесь
        # отложена: ступень проверяется ниже, после того как известно, чья она
        user = _guard(request, "request.approve", check_permission=False)
        form = await request.form()
        _check_csrf(request, str(form.get("csrf") or ""))
        state = store.treasury_state()
        row = treasury.find(state, number)
        if not row:
            raise Denied("NO_REQUEST", f"заявки {number} нет", status=404)
        route = row.get("route") or []
        done = row.get("approvals") or []
        step = route[len(done)] if len(done) < len(route) else (route[-1] if route else "")
        _require(request, "request.approve", step=str(step))
        reason = str(form.get("reason") or "").strip()
        with store.lock:
            result = treasury.reject(state, number, role=str(step),
                                     person=str(user.get("name")),
                                     date=_date.today().isoformat(), reason=reason)
            if not result.get("errors"):
                store.save_treasury(state)
        _audit(request, user, "заявка.отклонена", number,
               {"step": step, "reason": reason,
                "errors": [item.get("code") for item in result.get("errors") or []]})
        if result.get("errors"):
            _flash(request, "err", f"{number}: {result['errors'][0]['why']}")
        else:
            _flash(request, "warn", f"{number}: отклонена")
        return RedirectResponse("/requests", status_code=303)

    @app.post("/requests/{number}/pay")
    async def request_pay(request: Request, number: str):
        user = _guard(request, "request.pay")
        form = await request.form()
        _check_csrf(request, str(form.get("csrf") or ""))
        state = store.treasury_state()
        amount = form.get("amount")
        with store.lock:
            result = treasury.pay(
                state, number, date=str(form.get("date") or _date.today().isoformat()),
                amount=_number(amount) if str(amount or "").strip() else None,
                person=str(user.get("name")),
                allow_overdraft=bool(form.get("overdraft")),
                reason=str(form.get("reason") or ""))
            if not result.get("errors"):
                store.save_treasury(state)
        _audit(request, user, "заявка.оплачена", number,
               {"amount": _number(amount), "result": result.get("status"),
                "errors": [item.get("code") for item in result.get("errors") or []]})
        if result.get("errors"):
            _flash(request, "err", f"{number}: {result['errors'][0]['why']}")
        else:
            _flash(request, "ok", f"{number}: оплата проведена, статус "
                                  f"{status_text(result.get('status'))}")
        return RedirectResponse("/requests", status_code=303)

    # ─────────────────────────── проводки ───────────────────────────
    @app.get("/postings", response_class=HTMLResponse)
    def postings(request: Request):
        user = _require(request, "postings.view")
        state = store.ledger_state()
        period = request.query_params.get("period") or _default_period(store)
        report = ledger.trial_balance(state, period)
        entries = [entry for entry in state.get("entries") or []
                   if entry.get("period") == period][-14:]
        return render(request, "postings.html", user, period=period, current=period, report=report,
                      entries=list(reversed(entries)),
                      proposals=list(reversed(state.get("proposals") or [])),
                      mode=state.get("mode") or ledger.MODE_POST,
                      periods=_periods(state, period),
                      treasury_state=store.treasury_state())

    @app.post("/postings/sync")
    async def postings_sync(request: Request):
        user = _guard(request, "postings.post")
        form = await request.form()
        _check_csrf(request, str(form.get("csrf") or ""))
        treasury_state = store.treasury_state()
        ledger_state = store.ledger_state()
        with store.lock:
            result = treasury.post_to_ledger(treasury_state, ledger_state)
            store.save_treasury(treasury_state)
            store.save_ledger(ledger_state)
        _audit(request, user, "проводки.проведены", "казначейство",
               {"posted": result.get("posted"), "duplicates": result.get("duplicates"),
                "rejected": result.get("rejected"), "errors":
                [item.get("code") for item in result.get("errors") or []]})
        _flash(request, "ok" if not result.get("errors") else "warn",
               f"проводок по казначейству: создано {result.get('posted')}, повторно "
               f"{result.get('duplicates')}, отказано {result.get('rejected')}")
        return RedirectResponse("/postings", status_code=303)

    @app.post("/postings/proposals/{proposal_id}/accept")
    async def proposal_accept(request: Request, proposal_id: str):
        user = _guard(request, "postings.accept")
        form = await request.form()
        _check_csrf(request, str(form.get("csrf") or ""))
        state = store.ledger_state()
        with store.lock:
            result = ledger.accept_proposal(state, proposal_id)
            if not result.get("errors"):
                store.save_ledger(state)
        _audit(request, user, "проводка.принята", proposal_id,
               {"result": result.get("status"),
                "errors": [item.get("code") for item in result.get("errors") or []]})
        _flash(request, "ok" if not result.get("errors") else "err",
               f"{proposal_id}: {result.get('status')}")
        return RedirectResponse("/postings", status_code=303)

    @app.post("/postings/proposals/{proposal_id}/reject")
    async def proposal_reject(request: Request, proposal_id: str):
        user = _guard(request, "postings.accept")
        form = await request.form()
        _check_csrf(request, str(form.get("csrf") or ""))
        state = store.ledger_state()
        reason = str(form.get("reason") or "").strip()
        with store.lock:
            result = ledger.reject_proposal(state, proposal_id, reason=reason)
            store.save_ledger(state)
        _audit(request, user, "проводка.отклонена", proposal_id, {"reason": reason})
        _flash(request, "warn", f"{proposal_id}: отклонена" + ("" if reason else
                                                               " (причина не указана)"))
        return RedirectResponse("/postings", status_code=303)

    # ─────────────────────────── закрытие месяца ───────────────────────────
    @app.get("/closing", response_class=HTMLResponse)
    def closing_page(request: Request):
        user = _require(request, "closing.view")
        state = store.ledger_state()
        period = request.query_params.get("period") or _default_period(store)
        closed = [row for row in state.get("periods") or [] if row.get("closed")]
        return render(request, "closing.html", user, period=period, current=period,
                      periods=_periods(state, period), closed=closed,
                      last=closed[-1] if closed else None,
                      policy=closing.load_policy(), report=None)

    @app.post("/closing", response_class=HTMLResponse)
    async def closing_run(request: Request):
        user = _guard(request, "closing.run")
        form = await request.form()
        _check_csrf(request, str(form.get("csrf") or ""))
        period = str(form.get("period") or "").strip()
        dry_run = bool(form.get("dry_run"))
        state = store.ledger_state()
        stock_state = store.stock_state()
        with store.lock:
            report = closing.close_month(state, period, stock_state=stock_state,
                                         dry_run=dry_run)
            if report.get("closed") and not dry_run:
                store.save_ledger(state)
        _audit(request, user, "закрытие.месяц", period,
               {"dry_run": dry_run, "closed": report.get("closed"),
                "problems": [item.get("code") for item in report.get("problems") or []],
                "steps": len(report.get("steps") or [])})
        closed = [row for row in state.get("periods") or [] if row.get("closed")]
        return render(request, "closing.html", user, period=period, current=period, report=report,
                      periods=_periods(state, period), closed=closed,
                      last=closed[-1] if closed else None, policy=closing.load_policy())

    # ─────────────────────────── аудит и роли ───────────────────────────
    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request):
        user = _require(request, "audit.view")
        log = store.audit
        records = log.history(limit=120) if log.path.exists() else []
        ok, count, why = log.verify() if log.path.exists() else (True, 0, "журнала ещё нет")
        return render(request, "audit.html", user, records=list(reversed(records)),
                      ok=ok, count=count, why=why)

    @app.get("/roles", response_class=HTMLResponse)
    def roles_page(request: Request):
        user = _require(request, "admin.rbac")
        payload = auth.load(store.users_path)
        users = [{key: value for key, value in row.items() if key != "password"}
                 for row in payload.get("users") or []]
        return render(request, "roles.html", user, users=users, matrix=matrix,
                      rules=matrix.denials(), not_implemented=matrix.not_implemented())

    return app


# ─────────────────────────── помощники ───────────────────────────
def _number(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _safe(text: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in str(text))


def _objects_for(user: dict, matrix: rbac.Matrix, stock_state: dict) -> list[str]:
    """Объекты, по которым человек вправе действовать.

    Справочник объектов — из состояния склада плюс то, что закреплено за человеком:
    если объект есть в его списке, но ещё не заведён в складе, он всё равно виден.
    """
    known = []
    if stock_state.get("object"):
        known.append(str(stock_state["object"]))
    for name in user.get("objects") or []:
        if name and name != "*" and name not in known:
            known.append(name)
    return matrix.objects_for(str(user.get("role")), list(user.get("objects") or []), known)


def _articles(rules: dict) -> list[dict]:
    return [{"name": name, "note": (info or {}).get("note") or "",
             "account": (info or {}).get("account") or ""}
            for name, info in sorted((rules.get("articles") or {}).items())
            if (info or {}).get("kind") == "расход"]


def _next_issue_number(state: dict) -> str:
    used = 0
    for doc in state.get("documents") or []:
        number = str(doc.get("number") or "")
        if number.startswith("М-11 №"):
            tail = number.split("№", 1)[-1].strip()
            if tail.isdigit():
                used = max(used, int(tail))
    return f"М-11 №{used + 1}"


def _next_request_number(state: dict) -> str:
    used = 0
    for row in state.get("requests") or []:
        number = str(row.get("number") or "")
        if number.startswith("З-"):
            tail = number[2:]
            if tail.isdigit():
                used = max(used, int(tail))
    return f"З-{used + 1:03d}"


def _periods(state: dict, current: str) -> list[str]:
    seen = {str(entry.get("period") or "") for entry in state.get("entries") or []}
    seen |= {str(row.get("period") or "") for row in state.get("periods") or []}
    seen = {period for period in seen if len(period) == 7}
    if current:
        seen.add(current)
    return sorted(seen, reverse=True)


def _default_period(store: Store) -> str:
    state = store.ledger_state()
    periods = sorted({str(entry.get("period") or "") for entry in state.get("entries") or []
                      if entry.get("period")})
    if periods:
        return periods[-1]
    today = _date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _check_warehouse_allowed(state: dict, rows: list[dict], user: dict,
                             matrix: rbac.Matrix) -> None:
    object_name = str(state.get("object") or "")
    if not matrix.object_allowed(str(user.get("role")), list(user.get("objects") or []),
                                 object_name):
        raise Denied("OBJECT_FORBIDDEN",
                     f"склад объекта «{object_name}» не закреплён за {user.get('name')}",
                     action="warehouse.view")


def _requests_view(state: dict, user: dict, matrix: rbac.Matrix) -> list[dict]:
    """Строки заявок с тем, что человек вправе с ними сделать (проверка — на POST)."""
    role = str(user.get("role"))
    rows = []
    for row in state.get("requests") or []:
        route = row.get("route") or []
        done = row.get("approvals") or []
        next_step = route[len(done)] if len(done) < len(route) else ""
        object_name = str(row.get("object") or state.get("object") or "")
        visible = matrix.object_allowed(role, list(user.get("objects") or []), object_name) \
            or matrix.scope_of(role) == "all"
        rows.append({
            **row,
            "object": object_name,
            "visible": visible,
            "next_step": next_step,
            "can_approve": bool(next_step) and matrix.can(role, "request.approve",
                                                          step=str(next_step))
                           and row.get("status") not in (treasury.BLOCKED, treasury.REJECTED,
                                                         treasury.PAID),
            "can_pay": row.get("status") in (treasury.APPROVED, treasury.PARTIALLY_PAID)
                       and matrix.can(role, "request.pay"),
            "route_text": " → ".join(route),
            "approved_text": ", ".join(f"{item.get('role')} ({item.get('person')})"
                                       for item in done) or "—",
        })
    return list(reversed(rows))


def status_text(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", status or "—")
