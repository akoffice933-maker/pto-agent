# -*- coding: utf-8 -*-
"""Казначейство: заявка на оплату, маршрут согласования, платёжный календарь, БДДС.

Зачем этот слой
---------------
Учёт (`ledger.py`) отвечает на вопрос «кому мы должны». Казначейство — на другой:
«когда и чем мы заплатим, и хватит ли на это денег». На стройке это обычно два Excel:
список заявок на оплату и платёжный календарь, между которыми суммы расходятся, а
кассовый разрыв обнаруживается в день платежа.

Что здесь есть
--------------
*заявка* → *маршрут согласования* → *оплата* → *календарь и БДДС*.

Девять правил, без которых казначейство превращается в переписку
----------------------------------------------------------------
1. **Заявка без основания не принимается.** «Оплатить» без счёта, УПД, акта или договора —
   это не заявка, а просьба.
2. **Маршрут проходится по порядку.** Нельзя подписать у финдиректора, перескочив ПТО:
   маршрут — данные (`treasury_rules.json`), а не «кто быстрее ответил».
3. **Один человек не согласует две ступени подряд.** Разделение полномочий проверяется,
   а не подразумевается.
4. **Лимит проверяется до согласования.** Превышение — блокировка с точной суммой превышения,
   а не «потом посмотрим».
5. **Обход лимита — только явным решением** с причиной и автором, и это попадает в журнал.
6. **Оплата — только по согласованной заявке** и только в пределах остатка на счёте.
7. **Оплата сверх остатка — кассовый разрыв**, оформленный явно (причина + решение), а не минус,
   который никто не заметил.
8. **Переплата по заявке невозможна:** сумма оплат не больше суммы заявки.
9. **Просрочка не исчезает.** Заявка с истёкшим сроком остаётся в календаре и в отчёте
   с числом дней просрочки.

Чего этот круг не делает
------------------------
Зарплата, налоги, импорт банковской выписки, авансы на отдельных субсчетах, резервирование
денег под заявку. БДР по статьям — не здесь: он считается в закрытии месяца по счетам
20.01/90.02, и сводить два разреза — отдельная работа. Всё это перечислено в
`treasury_rules.json` в `not_implemented`, чтобы «нет» было видно, а не подразумевалось.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from datetime import date as Date, timedelta
from decimal import Decimal
from pathlib import Path

SCHEMA = "pto-agent/treasury@1"
RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "treasury_rules.json"
EPS = Decimal("0.01")

DRAFT = "черновик"
ON_APPROVAL = "на согласовании"
APPROVED = "согласована"
REJECTED = "отклонена"
BLOCKED = "заблокирована лимитом"
PARTIALLY_PAID = "оплачена частично"
PAID = "оплачена"

PAYABLE = (APPROVED, PARTIALLY_PAID)


# ─────────────────────────── числа ───────────────────────────
def dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def kopeck(value) -> Decimal:
    number = dec(value)
    return Decimal("0.00") if number is None else number.quantize(EPS)


def num(value) -> float:
    return float(kopeck(value))


def rub(value) -> str:
    text = f"{kopeck(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} ₽"


def period_of(value: str) -> str:
    return str(value or "")[:7]


# ─────────────────────────── справочники ───────────────────────────
def load_rules(path: str | Path | None = None) -> dict:
    path = Path(path) if path else RULES_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "pto-agent/treasury-rules@1":
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} "
                         f"не pto-agent/treasury-rules@1")
    return payload


def route_for(rules: dict, amount) -> dict:
    """Маршрут по сумме: первый порог, в который сумма попадает; иначе последний."""
    value = kopeck(amount)
    routes = rules.get("routes") or []
    for route in routes:
        limit = dec(route.get("up_to"))
        if limit is None or value <= limit:
            return route
    return routes[-1] if routes else {"id": "none", "steps": [], "title": "маршрут не задан"}


# ─────────────────────────── состояние ───────────────────────────
def new_state(*, object_name: str = "", opening: float | dict | None = None,
              limits: dict | None = None, contract: str = "") -> dict:
    state = {"schema": SCHEMA, "object": object_name, "contract": contract,
             "requests": [], "payments": [], "receipts": [], "limits": dict(limits or {}),
             "audit": [], "rejections": [], "keys": []}
    if isinstance(opening, dict):
        state["opening"] = {"date": str(opening.get("date") or ""),
                            "amount": num(opening.get("amount") or 0),
                            "basis": str(opening.get("basis") or "")}
    elif opening is not None:
        state["opening"] = {"date": "", "amount": num(opening), "basis": "не указано"}
    return state


def load(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return new_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} не {SCHEMA!r}")
    return payload


def save(state: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ─────────────────────────── заявки ───────────────────────────
def _request_key(request: dict) -> str:
    return "|".join(str(request.get(field) or "") for field in
                    ("number", "date", "counterparty", "amount"))


def find(state: dict, number: str) -> dict | None:
    return next((row for row in state.get("requests") or []
                 if str(row.get("number")) == str(number)), None)


def _limit_for(state: dict, article: str, period: str) -> Decimal | None:
    table = (state.get("limits") or {}).get(period) or {}
    value = table.get(article)
    if value is None:
        table_all = state.get("limits") or {}
        value = table_all.get(article)
    return None if value is None else kopeck(value)


def _object_limit_for(state: dict, object_name: str, article: str, period: str) -> Decimal | None:
    """Лимит статьи на объект: объект дороже статьи не считается «в среднем по фирме»."""
    table = (state.get("object_limits") or {}).get(period) or {}
    row = table.get(str(object_name or "")) or {}
    value = row.get(article)
    return None if value is None else kopeck(value)


def committed_object(state: dict, *, object_name: str, article: str, period: str,
                     skip: str = "") -> Decimal:
    """Сколько занято лимита объекта: поданные заявки этого объекта и статьи."""
    total = Decimal("0")
    for row in state.get("requests") or []:
        if str(row.get("number")) == skip:
            continue
        if row.get("status") in (REJECTED, BLOCKED):
            continue
        if str(row.get("object") or "") != str(object_name or ""):
            continue
        if period_of(str(row.get("date"))) != period:
            continue
        if str(row.get("article")) != article:
            continue
        total += kopeck(row.get("amount"))
    return total


def limits_usage(state: dict, period: str) -> list[dict]:
    """Использование лимитов: статьи и объекты в одном списке, с остатком и процентом."""
    rows: list[dict] = []
    for article, limit in ((state.get("limits") or {}).get(period) or {}).items():
        used = committed(state, article=article, period=period)
        rows.append({"scope": "статья", "name": article, "limit": num(limit),
                     "used": num(used), "left": num(kopeck(limit) - used),
                     "pct": round(float(used / kopeck(limit) * 100), 1) if kopeck(limit) else None})
    for object_name, table in ((state.get("object_limits") or {}).get(period) or {}).items():
        for article, limit in (table or {}).items():
            used = committed_object(state, object_name=object_name, article=article,
                                    period=period)
            rows.append({"scope": "объект", "name": f"{object_name} / {article}",
                         "limit": num(limit), "used": num(used),
                         "left": num(kopeck(limit) - used),
                         "pct": round(float(used / kopeck(limit) * 100), 1) if kopeck(limit) else None})
    return rows


def committed(state: dict, *, article: str, period: str, skip: str = "") -> Decimal:
    """Сколько уже занято лимита: поданные заявки периода, кроме отклонённых."""
    total = Decimal("0")
    for row in state.get("requests") or []:
        if str(row.get("number")) == skip:
            continue
        if row.get("status") in (REJECTED, BLOCKED):
            continue      # заблокированная заявка лимит не занимает: она не в работе
        if period_of(str(row.get("date"))) != period:
            continue
        if str(row.get("article")) != article:
            continue
        total += kopeck(row.get("amount"))
    return total


def add_request(state: dict, request: dict, *, rules: dict | None = None) -> dict:
    """Принять заявку на согласование. Что не проходит проверку — не заявка, а запрос."""
    rules = rules or load_rules()
    result = {"status": "", "errors": [], "gaps": [], "warnings": [], "request": None}
    number = str(request.get("number") or "").strip()
    if not number:
        result["errors"].append({"code": "NO_NUMBER", "what": "заявка",
                                 "why": "заявка без номера не встанет в календарь"})
        return result
    if request.get("amount") is None or kopeck(request.get("amount")) <= 0:
        result["errors"].append({"code": "BAD_AMOUNT", "what": number,
                                 "why": "сумма заявки не указана или равна нулю"})
        return result
    amount = kopeck(request.get("amount"))
    counter = str(request.get("counterparty") or "").strip()
    if not counter:
        result["errors"].append({"code": "NO_COUNTERPARTY", "what": number,
                                 "why": "заявка без получателя денег не проверяема"})
        return result
    article = str(request.get("article") or "").strip()
    articles = rules.get("articles") or {}
    if article not in articles:
        result["errors"].append({"code": "NO_ARTICLE", "what": number,
                                 "why": f"статьи «{article}» нет в справочнике; есть: "
                                        f"{', '.join(sorted(articles))}"})
        return result
    if (articles[article] or {}).get("kind") != "расход":
        result["errors"].append({"code": "NOT_EXPENSE", "what": number,
                                 "why": f"статья «{article}» — поступление, а заявка на оплату "
                                        f"её не создаёт"})
        return result
    basis = request.get("basis") or {}
    if not (isinstance(basis, dict) and str(basis.get("number") or "").strip()):
        result["errors"].append({"code": "NO_BASIS", "what": number,
                                 "why": "заявка без документа-основания: «оплатить» без счёта, "
                                        "УПД, акта или договора — это не заявка"})
        result["gaps"].append({"what": f"документ-основание по заявке {number}",
                               "why": "нечем подтвердить сумму",
                               "ask": f"счёт, УПД, акт или договор к заявке {number}"})
        return result
    key = _request_key(request)
    if key in (state.get("keys") or []):
        result["status"] = "уже подана"
        result["warnings"].append(f"{number}: заявка с той же датой, контрагентом и суммой "
                                  f"уже есть — дубль не создан")
        return result

    date = str(request.get("date") or "")
    period = period_of(date)
    route = route_for(rules, amount)
    object_name = str(request.get("object") or state.get("object") or "")
    limit = _limit_for(state, article, period)
    used = committed(state, article=article, period=period)
    object_limit = _object_limit_for(state, object_name, article, period)
    object_used = committed_object(state, object_name=object_name, article=article,
                                   period=period)
    status = ON_APPROVAL
    if limit is not None and used + amount > limit:
        excess = used + amount - limit
        override = request.get("limit_override") or {}
        if str(override.get("reason") or "").strip() and str(override.get("by") or "").strip():
            status = ON_APPROVAL
            result["warnings"].append(f"{number}: лимит по статье «{article}» превышен на "
                                      f"{rub(excess)} — разрешено {override.get('by')}, "
                                      f"причина: {override.get('reason')}")
            state.setdefault("audit", []).append({
                "what": "превышение лимита разрешено", "request": number,
                "article": article, "limit": num(limit), "amount": num(amount),
                "excess": num(excess), "by": override.get("by"),
                "reason": override.get("reason"), "date": date})
        else:
            status = BLOCKED
            result["errors"].append({"code": "LIMIT_EXCEEDED", "what": number,
                                     "why": f"лимит по статье «{article}» на {period}: "
                                            f"{rub(limit)}, уже занято {rub(used)}, "
                                            f"заявка {rub(amount)} — превышение {rub(excess)}"})
            result["gaps"].append({"what": f"решение по превышению лимита {rub(excess)}",
                                   "why": "заявка выходит за лимит статьи",
                                   "ask": "разрешение с причиной и автором либо перенос на "
                                          "следующий месяц"})

    if object_limit is not None and object_used + amount > object_limit:
        excess_object = object_used + amount - object_limit
        override_object = request.get("limit_override") or {}
        if str(override_object.get("reason") or "").strip() and \
                str(override_object.get("by") or "").strip():
            result["warnings"].append(
                f"{number}: лимит по объекту «{object_name}» (статья «{article}») превышен "
                f"на {rub(excess_object)} — разрешено {override_object.get('by')}, "
                f"причина: {override_object.get('reason')}")
            state.setdefault("audit", []).append({
                "what": "превышение лимита объекта разрешено", "request": number,
                "object": object_name, "article": article,
                "limit": num(object_limit), "amount": num(amount),
                "excess": num(excess_object), "by": override_object.get("by"),
                "reason": override_object.get("reason"), "date": date})
        else:
            status = BLOCKED
            result["errors"].append({
                "code": "OBJECT_LIMIT_EXCEEDED", "what": number,
                "why": f"лимит по объекту «{object_name}» на {period}, статья «{article}»: "
                       f"{rub(object_limit)}, уже занято {rub(object_used)}, заявка "
                       f"{rub(amount)} — превышение {rub(excess_object)}"})
            result["gaps"].append({
                "what": f"решение по превышению лимита объекта {rub(excess_object)}",
                "why": f"объект «{object_name}» выходит за лимит статьи «{article}»",
                "ask": "разрешение с причиной и автором, перенос на следующий месяц "
                       "или перераспределение лимита между объектами"})

    row = {
        "id": f"З-{len(state.get('requests') or []) + 1:04d}",
        "number": number, "date": date, "object": str(request.get("object")
                                                      or state.get("object") or ""),
        "counterparty": counter, "article": article,
        "purpose": str(request.get("purpose") or ""), "amount": num(amount),
        "vat": request.get("vat"), "basis": {"kind": str(basis.get("kind") or ""),
                                             "number": str(basis.get("number") or ""),
                                             "date": str(basis.get("date") or "")},
        "due_date": str(request.get("due_date") or ""),
        "route_id": route.get("id"), "route": list(route.get("steps") or []),
        "approvals": [], "status": status, "paid": 0.0,
        "block_reason": (result["errors"][-1]["why"] if status == BLOCKED
                         and result["errors"] else ""),
        "advance": bool(request.get("advance")),
        "limit": None if limit is None else num(limit),
        "object_limit": None if object_limit is None else num(object_limit),
        "key": key,
    }
    state.setdefault("requests", []).append(row)
    state.setdefault("keys", []).append(key)
    result["request"] = row
    result["status"] = status
    return result


def approve(state: dict, number: str, *, role: str, person: str, date: str,
            comment: str = "") -> dict:
    """Согласовать очередную ступень. Порядок и разделение полномочий проверяются."""
    request = find(state, number)
    if not request:
        return {"status": "нет заявки", "errors": [{"code": "NO_REQUEST", "what": number,
                                                    "why": "заявки с таким номером нет"}],
                "gaps": [], "warnings": []}
    if request["status"] == BLOCKED:
        return {"status": BLOCKED, "errors": [{"code": "LIMIT_EXCEEDED", "what": number,
                                               "why": "заявка заблокирована лимитом: сначала "
                                                      "решение по превышению"}],
                "gaps": [], "warnings": []}
    if request["status"] in (APPROVED, PAID, PARTIALLY_PAID):
        return {"status": request["status"],
                "warnings": [f"{number}: уже согласована — повторная подпись не нужна"],
                "errors": [], "gaps": []}
    if request["status"] == REJECTED:
        return {"status": REJECTED, "errors": [{"code": "REJECTED", "what": number,
                                                "why": "заявка отклонена: новая подпись её "
                                                       "не вернёт"}], "gaps": [], "warnings": []}
    if not str(person or "").strip():
        return {"status": "нет подписи", "errors": [{"code": "NO_PERSON", "what": number,
                                                     "why": "согласование без ФИО — не "
                                                            "согласование"}],
                "gaps": [], "warnings": []}
    route = request.get("route") or []
    # setdefault, а не `or []`: на пустом списке `or []` создаёт новый — подпись уходила в никуда
    done = request.setdefault("approvals", [])
    if len(done) >= len(route):
        return {"status": "маршрут пройден", "warnings": [f"{number}: маршрут уже пройден"],
                "errors": [], "gaps": []}
    expected = route[len(done)]
    if str(role or "").strip() != expected:
        return {"status": "не та ступень",
                "errors": [{"code": "WRONG_STEP", "what": number,
                            "why": f"сейчас очередь «{expected}», а подписывает «{role}»: "
                                   f"маршрут {request.get('route_id')} проходится по порядку"}],
                "gaps": [], "warnings": []}
    if done and str(done[-1].get("person")) == str(person):
        return {"status": "одно лицо",
                "errors": [{"code": "SAME_PERSON", "what": number,
                            "why": f"{person} уже согласовал предыдущую ступень "
                                   f"«{done[-1].get('role')}»: две ступени подряд одним "
                                   f"лицом — это отсутствие контроля"}],
                "gaps": [], "warnings": []}
    done.append({"step": expected, "role": expected, "person": str(person), "date": str(date),
                 "decision": "согласовано", "comment": str(comment or "")})
    request["status"] = APPROVED if len(done) >= len(route) else ON_APPROVAL
    return {"status": request["status"], "step": expected,
            "next_role": None if request["status"] == APPROVED else route[len(done)],
            "errors": [], "gaps": [], "warnings": []}


def reject(state: dict, number: str, *, role: str, person: str, date: str,
           reason: str) -> dict:
    request = find(state, number)
    if not request:
        return {"status": "нет заявки", "errors": [{"code": "NO_REQUEST", "what": number,
                                                    "why": "заявки с таким номером нет"}],
                "gaps": [], "warnings": []}
    if not str(reason or "").strip():
        return {"status": "нет причины", "errors": [{"code": "NO_REASON", "what": number,
                                                     "why": "отказ без причины нельзя: причина — "
                                                            "часть решения"}],
                "gaps": [], "warnings": []}
    if request["status"] in (PAID, PARTIALLY_PAID):
        return {"status": request["status"], "errors": [{"code": "ALREADY_PAID",
                                                         "what": number,
                                                         "why": "заявка уже оплачена: отказ "
                                                                "не отменяет платёж"}],
                "gaps": [], "warnings": []}
    request["status"] = REJECTED
    request.setdefault("approvals", []).append({
        "step": role, "role": role, "person": str(person), "date": str(date),
        "decision": "отклонено", "comment": str(reason)})
    state.setdefault("rejections", []).append({"number": number, "role": role,
                                               "person": person, "date": str(date),
                                               "reason": str(reason)})
    return {"status": REJECTED, "errors": [], "gaps": [], "warnings": []}


# ─────────────────────────── деньги ───────────────────────────
def balance_at(state: dict, date: str) -> Decimal:
    """Остаток на счёте на дату: поступления минус оплаты, начиная с входящего остатка."""
    opening = state.get("opening") or {}
    total = kopeck(opening.get("amount") or 0)
    for row in state.get("receipts") or []:
        if row.get("plan"):
            continue          # ожидаемое поступление — не деньги на счёте
        if str(row.get("date") or "") <= str(date):
            total += kopeck(row.get("amount"))
    for row in state.get("payments") or []:
        if str(row.get("date") or "") <= str(date):
            total -= kopeck(row.get("amount"))
    return total


def pay(state: dict, number: str, *, date: str, amount=None, allow_overdraft: bool = False,
        reason: str = "", person: str = "", advance: bool = False) -> dict:
    """Оплатить заявку. Согласованную, в пределах остатка и не больше суммы заявки.

    `advance=True` помечает платёж как аванс: часть сверх долга уйдёт на 60.02, а не
    растворится в «прочих расчётах». Пометка меняет счёт, поэтому она явная.
    """
    request = find(state, number)
    if not request:
        return {"status": "нет заявки", "errors": [{"code": "NO_REQUEST", "what": number,
                                                    "why": "заявки с таким номером нет"}],
                "gaps": [], "warnings": [], "payment": None}
    if request["status"] not in PAYABLE:
        return {"status": request["status"],
                "errors": [{"code": "NOT_APPROVED", "what": number,
                            "why": f"оплата возможна только по согласованной заявке, "
                                   f"а она в статусе «{request['status']}»"}],
                "gaps": [], "warnings": [], "payment": None}
    total = kopeck(request.get("amount"))
    paid = kopeck(request.get("paid"))
    left = total - paid
    piece = kopeck(amount) if amount is not None else left
    if piece <= 0:
        return {"status": "нечего платить", "errors": [{"code": "BAD_AMOUNT", "what": number,
                                                        "why": "сумма оплаты не положительна"}],
                "gaps": [], "warnings": [], "payment": None}
    if piece > left:
        return {"status": "переплата",
                "errors": [{"code": "OVERPAY", "what": number,
                            "why": f"по заявке осталось {rub(left)}, а оплата {rub(piece)}: "
                                   f"переплата — отдельное решение, а не «доплатим ещё раз»"}],
                "gaps": [], "warnings": [], "payment": None}
    balance = balance_at(state, date)
    warnings: list[str] = []
    if piece > balance and not allow_overdraft:
        return {"status": "нет денег",
                "errors": [{"code": "NO_MONEY", "what": number,
                            "why": f"на {date} на счёте {rub(balance)}, платёж {rub(piece)} — "
                                   f"не хватает {rub(piece - balance)}"}],
                "gaps": [{"what": f"деньги на платёж {rub(piece)} по заявке {number}",
                          "why": "кассовый разрыв: платёж больше остатка на счёте",
                          "ask": "ускорить поступление, перенести платёж или согласовать "
                                 "оплату с разрывом (причина и автор)"}],
                "warnings": [], "payment": None}
    if piece > balance:
        if not str(reason or "").strip():
            return {"status": "нужна причина",
                    "errors": [{"code": "OVERDRAFT_NO_REASON", "what": number,
                                "why": "оплата сверх остатка разрешена, но причина не указана"}],
                    "gaps": [], "warnings": [], "payment": None}
        warnings.append(f"{number}: оплата {rub(piece)} при остатке {rub(balance)} — "
                        f"кассовый разрыв {rub(piece - balance)}, причина: {reason}")
        state.setdefault("audit", []).append({
            "what": "оплата с кассовым разрывом", "request": number, "date": str(date),
            "amount": num(piece), "balance": num(balance),
            "gap": num(piece - balance), "reason": str(reason), "by": str(person or "не указан")})
    payment = {"id": f"Пл-{len(state.get('payments') or []) + 1:04d}", "request": number,
               "date": str(date), "amount": num(piece), "counterparty": request["counterparty"],
               "article": request["article"], "purpose": request.get("purpose") or "",
               "basis": request.get("basis") or {}, "overdraft": bool(piece > balance),
               "advance": bool(advance or request.get("advance")),
               "reason": str(reason or "")}
    state.setdefault("payments", []).append(payment)
    request["paid"] = num(paid + piece)
    request["status"] = PAID if kopeck(request["paid"]) >= total else PARTIALLY_PAID
    if request.get("due_date") and str(date) > str(request["due_date"]):
        late = (Date.fromisoformat(str(date)) - Date.fromisoformat(str(request["due_date"]))).days
        payment["late_days"] = late
        warnings.append(f"{number}: оплачено с просрочкой {late} дн. "
                        f"(срок {request['due_date']})")
    return {"status": request["status"], "payment": payment, "errors": [], "gaps": [],
            "warnings": warnings}


def add_receipt(state: dict, receipt: dict, *, rules: dict | None = None) -> dict:
    """Поступление от заказчика: основание обязательно, дубль не создаётся."""
    rules = rules or load_rules()
    number = str(receipt.get("number") or "").strip()
    amount = kopeck(receipt.get("amount"))
    result = {"status": "", "errors": [], "gaps": [], "warnings": [], "receipt": None}
    if not number:
        result["errors"].append({"code": "NO_NUMBER", "what": "поступление",
                                 "why": "без номера платёжного поручения поступление не встанет "
                                        "в календарь"})
        return result
    if amount <= 0:
        result["errors"].append({"code": "BAD_AMOUNT", "what": number,
                                 "why": "сумма поступления не положительна"})
        return result
    article = str(receipt.get("article") or "оплата КС-2")
    articles = rules.get("articles") or {}
    if article not in articles or (articles[article] or {}).get("kind") != "поступление":
        result["errors"].append({"code": "NO_ARTICLE", "what": number,
                                 "why": f"статья «{article}» не поступление или её нет в "
                                        f"справочнике"})
        return result
    if not str(receipt.get("counterparty") or "").strip():
        result["errors"].append({"code": "NO_COUNTERPARTY", "what": number,
                                 "why": "поступление без плательщика не сверяется с долгом"})
        return result
    if not str(receipt.get("contract") or "").strip():
        result["errors"].append({"code": "NO_CONTRACT", "what": number,
                                 "why": "поступление без договора некуда отнести в учёте"})
        result["gaps"].append({"what": f"договор по поступлению {number}",
                               "why": "счёт 62.01 требует договор",
                               "ask": "номер договора (или графика финансирования)"})
        return result
    key = f"{number}|{receipt.get('date')}|{amount}"
    if key in (state.get("keys") or []):
        result["status"] = "уже есть"
        result["warnings"].append(f"{number}: такое поступление уже записано — дубль не создан")
        return result
    row = {"id": f"Пс-{len(state.get('receipts') or []) + 1:04d}", "number": number,
           "date": str(receipt.get("date") or ""), "amount": num(amount),
           "counterparty": str(receipt.get("counterparty")),
           "contract": str(receipt.get("contract")), "article": article,
           "basis": str(receipt.get("basis") or ""),
           "advance": bool(receipt.get("advance")), "plan": bool(receipt.get("plan")),
           "key": key}
    state.setdefault("receipts", []).append(row)
    state.setdefault("keys", []).append(key)
    result.update({"status": "принято", "receipt": row})
    return result


# ─────────────────────────── календарь ───────────────────────────
def _month_bounds(period: str) -> tuple[str, str]:
    year, month = int(period[:4]), int(period[5:7])
    first = Date(year, month, 1)
    last = (Date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1))
    return first.isoformat(), last.isoformat()


def calendar(state: dict, period: str, *, funding: dict | None = None) -> dict:
    """Платёжный календарь месяца: факт и план по датам, остаток и кассовые разрывы.

    План — согласованные заявки по сроку оплаты и ожидаемые поступления из графика
    финансирования. Факт — уже прошедшие платежи и поступления. Разрыв считается по
    нарастающему остатку, а не «в среднем за месяц».
    """
    first, last = _month_bounds(period)
    rows: dict[str, dict] = OrderedDict()

    def _slot(day: str) -> dict:
        return rows.setdefault(day, {"date": day, "in_plan": 0.0, "in_fact": 0.0,
                                     "out_plan": 0.0, "out_fact": 0.0, "items": []})

    for row in state.get("receipts") or []:
        if first <= str(row.get("date")) <= last:
            slot = _slot(str(row["date"]))
            expected = bool(row.get("plan"))
            slot["in_plan" if expected else "in_fact"] += row["amount"]
            slot["items"].append({"what": f"поступление {row['number']} от {row['counterparty']}",
                                  "amount": row["amount"],
                                  "kind": ("поступление (план)" if expected
                                           else "поступление (факт)")})
    for row in state.get("payments") or []:
        if first <= str(row.get("date")) <= last:
            slot = _slot(str(row["date"]))
            slot["out_fact"] += row["amount"]
            slot["items"].append({"what": f"оплата {row['request']} — {row['counterparty']}",
                                  "amount": -row["amount"], "kind": "оплата (факт)"})
    for row in state.get("requests") or []:
        if row.get("status") not in PAYABLE:
            continue
        due = str(row.get("due_date") or row.get("date") or "")
        left = kopeck(row.get("amount")) - kopeck(row.get("paid"))
        if left <= 0:
            continue
        day = due if first <= due <= last else (last if due and due < first else "")
        if not day:
            continue
        slot = _slot(day)
        slot["out_plan"] += num(left)
        slot["items"].append({"what": f"к оплате {row['number']} — {row['counterparty']}"
                                      f"{' (просрочено)' if due and due < day else ''}",
                              "amount": -num(left), "kind": "оплата (план)"})

    funding_rows: list[dict] = []
    funding_unlinked: list[dict] = []
    warnings: list[str] = []
    if funding:
        funding_contract = str((funding.get("contract") or {}).get("number") or "")
        own = str(state.get("contract") or "")
        linked = bool(funding_contract and own and funding_contract == own)
        for row in funding.get("payments") or []:
            pay_date = str(row.get("payment_date") or "")
            if not (first <= pay_date <= last):
                continue
            if linked:
                slot = _slot(pay_date)
                slot["in_plan"] += num(row.get("amount_rub"))
                slot["items"].append({"what": f"ожидается оплата {row.get('act_id')} "
                                              f"(график финансирования)",
                                      "amount": num(row.get("amount_rub")),
                                      "kind": "поступление (план, график)"})
                funding_rows.append(row)
            else:
                funding_unlinked.append(row)
        if funding_unlinked:
            warnings.append(
                f"график финансирования по договору {funding_contract or 'не указан'}, "
                f"а казначейство по договору {own or 'не указан'}: {len(funding_unlinked)} "
                f"строк в остаток не включены — их нельзя считать деньгами этого объекта")

    opening = balance_at(state, (Date.fromisoformat(first) - timedelta(days=1)).isoformat())
    balance = opening
    gaps: list[dict] = []
    ordered = [rows[day] for day in sorted(rows)]
    for slot in ordered:
        balance += kopeck(slot["in_fact"] + slot["in_plan"])
        balance -= kopeck(slot["out_fact"] + slot["out_plan"])
        slot["balance"] = num(balance)
        if balance < 0:
            gaps.append({"date": slot["date"], "amount": num(-balance),
                         "why": "расходы по дате больше остатка на счёте"})
    overdue = [{"number": row["number"], "counterparty": row["counterparty"],
                "due_date": row.get("due_date"), "amount": num(kopeck(row.get("amount"))
                                                               - kopeck(row.get("paid")))}
               for row in state.get("requests") or []
               if row.get("status") in PAYABLE and row.get("due_date")
               and str(row["due_date"]) < last and kopeck(row.get("amount")) >
               kopeck(row.get("paid"))]
    return {"period": period, "opening": num(opening), "warnings": warnings,
            "funding_unlinked": funding_unlinked,
            "rows": ordered, "gaps": gaps, "overdue": overdue,
            "in_fact": num(sum(kopeck(slot["in_fact"]) for slot in ordered)),
            "in_plan": num(sum(kopeck(slot["in_plan"]) for slot in ordered)),
            "out_fact": num(sum(kopeck(slot["out_fact"]) for slot in ordered)),
            "out_plan": num(sum(kopeck(slot["out_plan"]) for slot in ordered)),
            "closing": num(balance),
            "funding_rows": funding_rows}


def funding_contract_note(funding: dict | None, state: dict) -> str | None:
    """График финансирования и казначейство могут быть по разным договорам — это надо сказать.

    Молча сложить чужие поступления с нашим остатком значит показать «денег хватает» там,
    где их нет. Поэтому строки чужого графика не входят в остаток, а называются отдельно.
    """
    if not funding:
        return None
    contract = str((funding.get("contract") or {}).get("number") or "")
    own = str(state.get("contract") or "")
    if not contract:
        return "в графике финансирования не указан номер договора: строки не сверены"
    if not own:
        return f"у казначейства не указан договор: строки графика {contract} не сверены"
    if contract != own:
        return (f"график финансирования по договору {contract}, казначейство по {own}: "
                f"поступления из графика в остаток не включены")
    return None


# ─────────────────────────── БДДС ───────────────────────────
def bdds(state: dict, period: str) -> dict:
    """Движение денег по статьям: факт отдельно, план отдельно. Складывать их нельзя."""
    first, last = _month_bounds(period)
    articles = (load_rules().get("articles") or {})
    rows: dict[str, dict] = OrderedDict()
    for code, info in articles.items():
        rows[code] = {"article": code, "kind": (info or {}).get("kind"),
                      "in_fact": 0.0, "in_plan": 0.0, "out_fact": 0.0, "out_plan": 0.0}
    for row in state.get("receipts") or []:
        if first <= str(row.get("date")) <= last:
            slot = rows.setdefault(str(row["article"]), {"article": row["article"],
                                                         "kind": "поступление", "in_fact": 0.0,
                                                         "in_plan": 0.0, "out_fact": 0.0,
                                                         "out_plan": 0.0})
            slot["in_plan" if row.get("plan") else "in_fact"] += row["amount"]
    for row in state.get("payments") or []:
        if first <= str(row.get("date")) <= last:
            slot = rows.setdefault(str(row["article"]), {"article": row["article"],
                                                         "kind": "расход", "in_fact": 0.0,
                                                         "in_plan": 0.0, "out_fact": 0.0,
                                                         "out_plan": 0.0})
            slot["out_fact"] += row["amount"]
    for row in state.get("requests") or []:
        if row.get("status") not in PAYABLE:
            continue
        left = num(kopeck(row.get("amount")) - kopeck(row.get("paid")))
        if left <= 0:
            continue
        due = str(row.get("due_date") or row.get("date") or "")
        if period_of(due) != period:
            continue
        slot = rows.setdefault(str(row["article"]), {"article": row["article"],
                                                     "kind": "расход", "in_fact": 0.0,
                                                     "in_plan": 0.0, "out_fact": 0.0,
                                                     "out_plan": 0.0})
        slot["out_plan"] += left
    used = [row for row in rows.values()
            if any(abs(row[key]) > 0.004 for key in ("in_fact", "in_plan", "out_fact",
                                                     "out_plan"))]
    income = sum(row["in_fact"] for row in used)
    outcome = sum(row["out_fact"] for row in used)
    planned_in = sum(row["in_plan"] for row in used)
    planned_out = sum(row["out_plan"] for row in used)
    return {"period": period, "rows": used,
            "in_fact": round(income, 2), "out_fact": round(outcome, 2),
            "net_fact": round(income - outcome, 2),
            "in_plan": round(planned_in, 2), "out_plan": round(planned_out, 2),
            "net_plan": round(planned_in - planned_out, 2),
            "note": "факт и план — разные величины: факт уже произошёл, план ещё нет. "
                    "Складывать их в одно «итого» нельзя"}


# ─────────────────────────── проводки ───────────────────────────
def post_to_ledger(state: dict, ledger_state: dict, *, chart: dict | None = None,
                   rules: dict | None = None, mode: str | None = None) -> dict:
    """Оплаты и поступления становятся проводками: Дт 60.01 Кт 51 и Дт 51 Кт 62.01."""
    from . import ledger

    chart = chart or ledger.load_chart()
    rules = rules or ledger.load_rules()
    results: list[dict] = []
    opening = state.get("opening") or {}
    opening_amount = kopeck(opening.get("amount") or 0)
    if opening_amount > 0 and str(opening.get("date") or ""):
        results.append(ledger.post_entry(
            ledger_state, event="opening_cash", date=str(opening["date"]),
            doc={"number": "остаток на расчётном счёте", "date": str(opening["date"]),
                 "kind": "opening"},
            lines=[{"account": "51", "side": ledger.DEBIT, "amount": num(opening_amount),
                    "analytics": {}},
                   {"account": "000", "side": ledger.CREDIT, "amount": num(opening_amount),
                    "analytics": {}}],
            memo=(f"входящий остаток денег на {opening['date']}: {rub(opening_amount)} · "
                  f"{opening.get('basis') or 'основание не указано'}"),
            rule="treasury.opening", key=f"opening-cash|{opening['date']}",
            chart=chart, rules=rules, mode=mode or ledger.MODE_POST))
    known = set(ledger_state.get("keys") or [])
    known |= {str(row.get("key")) for row in ledger_state.get("proposals") or []}

    def _already(key: str, what: str) -> dict:
        return {"status": ledger.STATUS_DUPLICATE, "entry": None, "errors": [], "gaps": [],
                "warnings": [f"{what}: уже проведено — дубль не создан"]}

    for payment in state.get("payments") or []:
        key = f"payment|{payment['id']}"
        if key in known or f"{key}|advance" in known:
            # проверять долг уже поздно: прошлая проводка его погасила
            results.append(_already(key, payment["request"]))
            continue
        debt = ledger.balance_as_of(ledger_state, "60.01", str(payment["date"]),
                                    group_by=("контрагент",))
        owed = -sum((value for key, value in debt.items()
                     if key[0] == payment["counterparty"]), Decimal("0"))
        amount = kopeck(payment["amount"])
        if payment.get("advance"):
            # пометка «аванс» — решение бухгалтера: вся сумма живёт на 60.02 до прихода
            owed = Decimal("0")
        if amount > owed + EPS and not payment.get("advance"):
            results.append({"status": ledger.STATUS_REJECTED, "entry": None,
                            "errors": [{"code": "PAYMENT_OVER_DEBT",
                                        "what": f"{payment['request']} / "
                                                f"{payment['counterparty']}",
                                        "why": f"оплата {rub(amount)} больше долга "
                                               f"{rub(owed)}: либо пометить платёж как аванс "
                                               f"(уйдёт на 60.02), либо не платить вперёд"}],
                            "gaps": [{"what": f"аванс по заявке {payment['request']}",
                                      "why": "оплата больше долга поставщику",
                                      "ask": "пометка «аванс» с основанием (счёт, договор) "
                                             "или ожидание поставки"}],
                            "warnings": []})
            continue
        to_debt = min(amount, owed)
        to_advance = amount - to_debt
        if to_debt > 0:
            result = ledger.post_entry(
                ledger_state, event="payment", date=str(payment["date"]),
                doc={"number": f"п/п по заявке {payment['request']}",
                     "date": str(payment["date"]), "kind": "payment"},
                lines=[{"account": "60.01", "side": ledger.DEBIT, "amount": num(to_debt),
                        "analytics": {"контрагент": payment["counterparty"]}},
                       {"account": "51", "side": ledger.CREDIT, "amount": num(to_debt),
                        "analytics": {}}],
                memo=(f"оплата {payment['counterparty']} по заявке {payment['request']}: "
                      f"{rub(to_debt)} · {payment.get('article')}"
                      + (" · кассовый разрыв" if payment.get("overdraft") else "")),
                rule="treasury.payment", key=key,
                chart=chart, rules=rules, mode=mode or ledger.MODE_POST)
            result["payment"] = payment["id"]
            results.append(result)
        else:
            results.append({"status": "аванс", "entry": None, "errors": [], "gaps": [],
                            "warnings": [f"{payment['request']}: вся сумма — аванс, "
                                         f"проводится на 60.02 отдельной строкой"],
                            "amount": 0.0})
        if to_advance > 0:
            advance_result = ledger.post_entry(
                ledger_state, event="payment", date=str(payment["date"]),
                doc={"number": f"п/п аванс по заявке {payment['request']}",
                     "date": str(payment["date"]), "kind": "payment"},
                lines=[{"account": "60.02", "side": ledger.DEBIT, "amount": num(to_advance),
                        "analytics": {"контрагент": payment["counterparty"]}},
                       {"account": "51", "side": ledger.CREDIT, "amount": num(to_advance),
                        "analytics": {}}],
                memo=(f"аванс {payment['counterparty']} по заявке {payment['request']}: "
                      f"{rub(to_advance)} — долг на дату {rub(owed)}, остальное аванс"),
                rule="treasury.advance_payment", key=f"{key}|advance",
                chart=chart, rules=rules, mode=mode or ledger.MODE_POST)
            advance_result["payment"] = payment["id"]
            advance_result["advance"] = num(to_advance)
            results.append(advance_result)

    for receipt in state.get("receipts") or []:
        if f"receipt|{receipt['id']}" in known or f"receipt|{receipt['id']}|advance" in known:
            results.append(_already(f"receipt|{receipt['id']}", receipt["number"]))
            continue
        if receipt.get("plan"):
            results.append({"status": "план, не проведено", "entry": None, "errors": [],
                            "gaps": [], "warnings": [
                                f"{receipt['number']}: ожидаемое поступление не проводится — "
                                f"это план, а не выписка"]})
            continue
        receivable = sum(ledger.balance_as_of(ledger_state, "62.01", str(receipt["date"]),
                                             group_by=("контрагент", "договор")).values(),
                         Decimal("0"))
        amount = kopeck(receipt["amount"])
        if receipt.get("advance"):
            receivable = Decimal("0")   # аванс не гасит долг: он ждёт зачёта
        if amount > receivable + EPS and not receipt.get("advance"):
            results.append({"status": ledger.STATUS_REJECTED, "entry": None,
                            "errors": [{"code": "RECEIPT_OVER_DEBT",
                                        "what": f"{receipt['number']} / "
                                                f"{receipt['counterparty']}",
                                        "why": f"поступление {rub(amount)} больше долга "
                                               f"заказчика {rub(receivable)} — это аванс: "
                                               f"помечается как аванс или не проводится"}],
                            "gaps": [{"what": f"аванс по {receipt['number']}",
                                      "why": "поступление больше признанной выручки",
                                      "ask": "пометить поступление как аванс"}],
                            "warnings": []})
            continue
        to_debt = min(amount, receivable)
        to_advance = amount - to_debt
        if to_debt > 0:
            result = ledger.post_entry(
                ledger_state, event="receipt_payment", date=str(receipt["date"]),
                doc={"number": f"п/п {receipt['number']}", "date": str(receipt["date"]),
                     "kind": "receipt_payment"},
                lines=[{"account": "51", "side": ledger.DEBIT, "amount": num(to_debt),
                        "analytics": {}},
                       {"account": "62.01", "side": ledger.CREDIT, "amount": num(to_debt),
                        "analytics": {"контрагент": receipt["counterparty"],
                                      "договор": receipt["contract"]}}],
                memo=(f"поступление {rub(to_debt)} от {receipt['counterparty']} "
                      f"по договору {receipt['contract']}"),
                rule="treasury.receipt", key=f"receipt|{receipt['id']}",
                chart=chart, rules=rules, mode=mode or ledger.MODE_POST)
            result["receipt"] = receipt["id"]
            results.append(result)
        else:
            results.append({"status": "аванс", "entry": None, "errors": [], "gaps": [],
                            "warnings": [f"{receipt['number']}: вся сумма — аванс, "
                                         f"проводится на 62.02 отдельной строкой"],
                            "amount": 0.0})
        if to_advance > 0:
            advance_result = ledger.post_entry(
                ledger_state, event="receipt_payment", date=str(receipt["date"]),
                doc={"number": f"п/п аванс {receipt['number']}", "date": str(receipt["date"]),
                     "kind": "receipt_payment"},
                lines=[{"account": "51", "side": ledger.DEBIT, "amount": num(to_advance),
                        "analytics": {}},
                       {"account": "62.02", "side": ledger.CREDIT, "amount": num(to_advance),
                        "analytics": {"контрагент": receipt["counterparty"],
                                      "договор": receipt["contract"]}}],
                memo=(f"аванс заказчика {receipt['counterparty']} по договору "
                      f"{receipt['contract']}: {rub(to_advance)} — выручка ещё не признана"),
                rule="treasury.advance_receipt", key=f"receipt|{receipt['id']}|advance",
                chart=chart, rules=rules, mode=mode or ledger.MODE_POST)
            advance_result["receipt"] = receipt["id"]
            advance_result["advance"] = num(to_advance)
            results.append(advance_result)

    posted = [r for r in results if r.get("status") in (ledger.STATUS_OK,
                                                        ledger.STATUS_PROPOSED)]
    return {"posted": len(posted),
            "skipped": sum(1 for r in results if r.get("status") == "план, не проведено"),
            "advances": sum(1 for r in results if r.get("status") == "аванс"),
            "duplicates": sum(1 for r in results if r.get("status") == ledger.STATUS_DUPLICATE),
            "rejected": sum(1 for r in results if r.get("status") == ledger.STATUS_REJECTED),
            "results": results,
            "errors": [item for r in results for item in r.get("errors") or []],
            "warnings": [item for r in results for item in r.get("warnings") or []],
            "gaps": [item for r in results for item in r.get("gaps") or []]}


def reconcile_ledger(state: dict, ledger_state: dict, *, as_of: str | None = None) -> dict:
    """Деньги в казначействе против сальдо 51: расхождение — это не «мелочь»."""
    from . import ledger

    if not as_of:
        dates = [str(row.get("date")) for row in state.get("payments") or []]
        dates += [str(row.get("date")) for row in state.get("receipts") or []]
        as_of = max(dates) if dates else str((state.get("opening") or {}).get("date") or "")
    cash_balance = balance_at(state, as_of)
    accounts_51 = sum(ledger.balance_as_of(ledger_state, "51", as_of).values(), Decimal("0"))
    diff = accounts_51 - cash_balance
    problems = []
    if abs(diff) > EPS:
        problems.append({"code": "CASH_51_MISMATCH", "what": f"на {as_of}",
                         "why": f"по казначейству {rub(cash_balance)}, сальдо 51 "
                                f"{rub(accounts_51)} — расхождение {rub(diff)}"})
    return {"as_of": as_of, "cash": num(cash_balance), "ledger": num(accounts_51),
            "diff": num(diff), "problems": problems, "ok": not problems}


# ─────────────────────────── авансы ───────────────────────────
def advances(state: dict, ledger_state: dict, *, as_of: str | None = None) -> dict:
    """Что висит на 60.02 и 62.02 и что из этого можно зачесть прямо сейчас.

    Аванс — не долг: он не гасит обязательство и не «исчезает» после оплаты. Он живёт
    на отдельном субсчёте до прихода материалов (60.02) или до подписания акта (62.02).
    """
    from . import ledger

    if not as_of:
        dates = [str(row.get("date")) for row in state.get("payments") or []]
        dates += [str(row.get("date")) for row in state.get("receipts") or []
                  if not row.get("plan")]
        as_of = max(dates) if dates else str((state.get("opening") or {}).get("date") or "")

    given_raw = ledger.balance_as_of(ledger_state, "60.02", as_of, group_by=("контрагент",))
    debt_raw = ledger.balance_as_of(ledger_state, "60.01", as_of, group_by=("контрагент",))
    received_raw = ledger.balance_as_of(ledger_state, "62.02", as_of,
                                        group_by=("контрагент", "договор"))
    due_raw = ledger.balance_as_of(ledger_state, "62.01", as_of, group_by=("контрагент", "договор"))

    given: list[dict] = []
    for key, value in given_raw.items():
        if value <= EPS:
            continue
        debtor = -debt_raw.get(key, Decimal("0"))
        given.append({"counterparty": key[0], "advance": num(value),
                      "debt": num(max(debtor, Decimal("0"))),
                      "can_offset": num(min(value, max(debtor, Decimal("0"))))})
    received: list[dict] = []
    for key, value in received_raw.items():
        if value >= -EPS:
            continue
        advance = -value
        due = due_raw.get(key, Decimal("0"))
        received.append({"counterparty": key[0], "contract": key[1] if len(key) > 1 else "",
                         "advance": num(advance), "due": num(max(due, Decimal("0"))),
                         "can_offset": num(min(advance, max(due, Decimal("0"))))})
    return {"as_of": as_of, "given": given, "received": received,
            "given_total": round(sum(row["advance"] for row in given), 2),
            "received_total": round(sum(row["advance"] for row in received), 2),
            "offsettable": round(sum(row["can_offset"] for row in given)
                                 + sum(row["can_offset"] for row in received), 2),
            "note": ("аванс не гасит долг: он зачитывается отдельной проводкой, когда "
                     "появится долг или выручка")}


def offset_advances(state: dict, ledger_state: dict, *, date: str, chart: dict | None = None,
                    rules: dict | None = None, mode: str | None = None) -> dict:
    """Зачесть авансы: Дт 60.01 Кт 60.02 и Дт 62.02 Кт 62.01 — на меньшую из сумм."""
    from . import ledger

    chart = chart or ledger.load_chart()
    rules = rules or ledger.load_rules()
    report = advances(state, ledger_state, as_of=date)
    results: list[dict] = []
    for row in report["given"]:
        amount = kopeck(row["can_offset"])
        if amount <= 0:
            continue
        result = ledger.post_entry(
            ledger_state, event="advance_offset", date=str(date),
            doc={"number": f"зачёт аванса {row['counterparty']}", "date": str(date),
                 "kind": "advance_offset"},
            lines=[{"account": "60.01", "side": ledger.DEBIT, "amount": num(amount),
                    "analytics": {"контрагент": row["counterparty"]}},
                   {"account": "60.02", "side": ledger.CREDIT, "amount": num(amount),
                    "analytics": {"контрагент": row["counterparty"]}}],
            memo=(f"зачёт аванса {row['counterparty']}: {rub(amount)} из "
                  f"{rub(row['advance'])} при долге {rub(row['debt'])}"),
            rule="treasury.advance_offset_supplier",
            key=f"offset|supplier|{row['counterparty']}|{num(amount):.2f}",
            chart=chart, rules=rules, mode=mode or ledger.MODE_POST)
        result["counterparty"] = row["counterparty"]
        result["amount"] = num(amount)
        results.append(result)
    for row in report["received"]:
        amount = kopeck(row["can_offset"])
        if amount <= 0:
            continue
        analytics = {"контрагент": row["counterparty"], "договор": row["contract"]}
        result = ledger.post_entry(
            ledger_state, event="advance_offset", date=str(date),
            doc={"number": f"зачёт аванса заказчика {row['counterparty']}", "date": str(date),
                 "kind": "advance_offset"},
            lines=[{"account": "62.02", "side": ledger.DEBIT, "amount": num(amount),
                    "analytics": analytics},
                   {"account": "62.01", "side": ledger.CREDIT, "amount": num(amount),
                    "analytics": analytics}],
            memo=(f"зачёт аванса заказчика {row['counterparty']} по договору "
                  f"{row['contract']}: {rub(amount)} из {rub(row['advance'])} "
                  f"при долге {rub(row['due'])}"),
            rule="treasury.advance_offset_customer",
            key=(f"offset|customer|{row['counterparty']}|{row['contract']}"
                 f"|{num(amount):.2f}"),
            chart=chart, rules=rules, mode=mode or ledger.MODE_POST)
        result["counterparty"] = row["counterparty"]
        result["amount"] = num(amount)
        results.append(result)
    posted = [r for r in results if r.get("status") in (ledger.STATUS_OK,
                                                        ledger.STATUS_PROPOSED)]
    return {"posted": len(posted),
            "duplicates": sum(1 for r in results if r.get("status") == ledger.STATUS_DUPLICATE),
            "rejected": sum(1 for r in results if r.get("status") == ledger.STATUS_REJECTED),
            "offset": round(sum(r.get("amount") or 0 for r in results
                                if r.get("status") in (ledger.STATUS_OK,
                                                       ledger.STATUS_PROPOSED)), 2),
            "results": results,
            "errors": [item for r in results for item in r.get("errors") or []],
            "warnings": [item for r in results for item in r.get("warnings") or []],
            "before": report}


# ─────────────────────────── БДР ───────────────────────────
def bdr(state: dict, ledger_state: dict, period: str, *, rules: dict | None = None) -> dict:
    """Бюджет доходов и расходов на том же справочнике статей, что и БДДС.

    Факт — начисление из журнала (обороты по счетам затрат и выручки), а не оплата.
    План — заявки периода (обязательства) и ожидаемые поступления. Это разные методы:
    план и факт в БДР показываются рядом, но не складываются.
    """
    from . import ledger

    rules = rules or load_rules()
    mapping = (rules.get("bdr") or {}).get("articles") or {}
    articles = rules.get("articles") or {}
    index = ledger.accounts_index(ledger.load_chart())
    trial = ledger.trial_balance(ledger_state, period)
    turnover = {row["code"]: row for row in trial.get("rows") or []}

    # счёт, указанный в нескольких статьях, по статьям не делится: без аналитики
    # «статья затрат» в проводке любое распределение будет выдумкой
    owners: dict[str, list[str]] = {}
    for article, plan_row in mapping.items():
        for code in (plan_row.get("cost_accounts") or []):
            owners.setdefault(code, []).append(article)
        for code in (plan_row.get("revenue_accounts") or []):
            owners.setdefault(code, []).append(article)
    shared = {code: names for code, names in owners.items() if len(set(names)) > 1}

    missing: list[str] = [code for code in owners if code not in index]
    rows: list[dict] = []
    unallocated: list[dict] = []
    for article, plan_row in mapping.items():
        info = articles.get(article) or {}
        kind = info.get("kind") or ("поступление" if plan_row.get("revenue_accounts")
                                    else "расход")
        cost_accounts = [code for code in (plan_row.get("cost_accounts") or [])
                         if code not in shared and code in index]
        revenue_accounts = [code for code in (plan_row.get("revenue_accounts") or [])
                            if code not in shared and code in index]
        fact = Decimal("0")
        for code in cost_accounts:
            row = turnover.get(code)
            if row is not None:
                fact += kopeck(row.get("debit") or 0)
        for code in revenue_accounts:
            row = turnover.get(code)
            if row is not None:
                fact += kopeck(row.get("credit") or 0)
        plan = Decimal("0")
        if kind == "расход":
            for request in state.get("requests") or []:
                if request.get("status") in (REJECTED, BLOCKED):
                    continue
                if str(request.get("article")) != article:
                    continue
                if period_of(str(request.get("date"))) != period:
                    continue
                plan += kopeck(request.get("amount"))
            source_plan = "заявки периода (обязательства), кроме отклонённых и заблокированных"
        else:
            for receipt in state.get("receipts") or []:
                if str(receipt.get("article")) != article:
                    continue
                if period_of(str(receipt.get("date"))) != period:
                    continue
                plan += kopeck(receipt.get("amount"))
            source_plan = "ожидаемые и фактические поступления периода"
        rows.append({"article": article, "kind": kind, "fact": num(fact), "plan": num(plan),
                     "cost_accounts": cost_accounts, "revenue_accounts": revenue_accounts,
                     "source_fact": (("обороты Дт " + ", ".join(cost_accounts))
                                     if cost_accounts else
                                     ("обороты Кт " + ", ".join(revenue_accounts))
                                     if revenue_accounts else "счёт не назначен"),
                     "source_plan": source_plan,
                     "shared_accounts": sorted({code for code in
                                                (plan_row.get("cost_accounts") or [])
                                                + (plan_row.get("revenue_accounts") or [])
                                                if code in shared})})
    for code, names in sorted(shared.items()):
        row = turnover.get(code) or {}
        amount = kopeck(row.get("debit") or 0) + kopeck(row.get("credit") or 0)
        if abs(amount) <= EPS:
            continue
        unallocated.append({"account": code, "amount": num(amount),
                            "articles": sorted(set(names)),
                            "why": (f"счёт {code} указан в {len(set(names))} статьях "
                                    f"({', '.join(sorted(set(names)))}) — без аналитики "
                                    f"«статья затрат» в проводке разнести нельзя")})
    used = [row for row in rows if abs(row["fact"]) > 0.004 or abs(row["plan"]) > 0.004]
    revenue_fact = sum((Decimal(str(row["fact"])) for row in used
                        if row["kind"] == "поступление"), Decimal("0"))
    expense_fact = sum((Decimal(str(row["fact"])) for row in used
                        if row["kind"] == "расход"), Decimal("0"))
    revenue_plan = sum((Decimal(str(row["plan"])) for row in used
                        if row["kind"] == "поступление"), Decimal("0"))
    expense_plan = sum((Decimal(str(row["plan"])) for row in used
                        if row["kind"] == "расход"), Decimal("0"))
    return {"period": period, "rows": used, "unallocated": unallocated,
            "unallocated_total": round(sum(row["amount"] for row in unallocated), 2),
            "revenue_fact": num(revenue_fact), "expense_fact": num(expense_fact),
            "profit_fact": num(revenue_fact - expense_fact),
            "revenue_plan": num(revenue_plan), "expense_plan": num(expense_plan),
            "profit_plan": num(revenue_plan - expense_plan),
            "missing_accounts": sorted(set(missing)),
            "note": ("факт — начисление по журналу, план — заявки и ожидаемые поступления: "
                     "разные методы, складывать нельзя. Прибыль по БДР — это прибыль по "
                     "начислению, а не деньги на счёте")}


# ─────────────────────────── один экран ───────────────────────────
def screen(state: dict, period: str, *, funding: dict | None = None,
           ledger_state: dict | None = None, rules: dict | None = None) -> dict:
    """Один экран: остаток, календарь, лимиты, БДДС и БДР, авансы, просрочка, оговорки."""
    rules = rules or load_rules()
    calendar_report = calendar(state, period, funding=funding)
    bdds_report = bdds(state, period)
    bdr_report = bdr(state, ledger_state, period, rules=rules) if ledger_state else None
    advance_report = (advances(state, ledger_state) if ledger_state else None)
    requests = state.get("requests") or []
    blocked = [{"number": row["number"], "amount": row["amount"],
                "why": (row.get("block_reason") or "").strip()}
               for row in requests if row.get("status") == BLOCKED]
    by_status: dict[str, int] = {}
    for row in requests:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    not_included: list[str] = []
    if calendar_report["funding_unlinked"]:
        not_included.append(f"{len(calendar_report['funding_unlinked'])} строк графика "
                            f"финансирования другого договора — в остаток не входят")
    if bdr_report and bdr_report["missing_accounts"]:
        not_included.append("счета БДР вне рабочего плана: "
                            + ", ".join(bdr_report["missing_accounts"]))
    for row in (rules.get("not_implemented") or []) + \
            ((rules.get("bdr") or {}).get("not_implemented") or []):
        if row not in not_included:
            not_included.append(row)
    return {"period": period, "object": state.get("object") or "",
            "contract": state.get("contract") or "",
            "cash": {"opening": calendar_report["opening"],
                     "in_fact": calendar_report["in_fact"], "in_plan": calendar_report["in_plan"],
                     "out_fact": calendar_report["out_fact"],
                     "out_plan": calendar_report["out_plan"],
                     "closing": calendar_report["closing"],
                     "gaps": calendar_report["gaps"]},
            "calendar": calendar_report,
            "funding_note": funding_contract_note(funding, state),
            "funding_rows": calendar_report.get("funding_rows") or [],
            "funding_unlinked": calendar_report["funding_unlinked"],
            "requests": {"count": len(requests), "by_status": by_status, "blocked": blocked},
            "limits": limits_usage(state, period),
            "bdds": bdds_report, "bdr": bdr_report, "advances": advance_report,
            "overdue": calendar_report["overdue"],
            "not_included": not_included}


def screen_text(report: dict) -> str:
    """Тот же экран текстом для терминала: сначала деньги, потом всё остальное."""
    cash = report["cash"]
    out = [f"казначейство на {report['period']}"
           + (f" · {report['object']}" if report.get("object") else "")
           + (f" · {report['contract']}" if report.get("contract") else "")]
    out.append(f"  остаток: начало {cash['opening']:,.2f} ₽ → конец {cash['closing']:,.2f} ₽ "
               f"(пришло {cash['in_fact']:,.2f} ₽, оплачено {cash['out_fact']:,.2f} ₽)")
    out.append(f"  план: ожидается {cash['in_plan']:,.2f} ₽, к оплате "
               f"{cash['out_plan']:,.2f} ₽ — в остаток не входит")
    if cash["gaps"]:
        out.append("  разрывы: " + "; ".join(f"{g['date']} — {g['amount']:,.2f} ₽"
                                            for g in cash["gaps"]))
    else:
        out.append("  разрывов нет")
    if report.get("funding_note"):
        out.append(f"  ! {report['funding_note']}")
    if report.get("funding_rows"):
        out.append(f"  график финансирования в остатке: {len(report['funding_rows'])} строк")
    requests = report["requests"]
    out.append("  заявки: " + ", ".join(f"{k} — {v}" for k, v in sorted(
        requests["by_status"].items())) if requests["by_status"] else "  заявок нет")
    for row in requests["blocked"]:
        out.append(f"    заблокирована {row['number']} на {row['amount']:,.2f} ₽")
    for row in report["limits"]:
        pct = "—" if row["pct"] is None else f"{row['pct']:.1f} %"
        out.append(f"  лимит {row['scope']} {row['name']}: {row['used']:,.2f} из "
                   f"{row['limit']:,.2f} ₽ ({pct})")
    bdds_report = report["bdds"]
    out.append(f"  БДДС факт: +{bdds_report['in_fact']:,.2f} / -{bdds_report['out_fact']:,.2f} = "
               f"{bdds_report['net_fact']:,.2f} ₽; план: +{bdds_report['in_plan']:,.2f} / "
               f"-{bdds_report['out_plan']:,.2f} = {bdds_report['net_plan']:,.2f} ₽")
    bdr_report = report.get("bdr")
    if bdr_report:
        out.append(f"  БДР факт: доходы {bdr_report['revenue_fact']:,.2f}, расходы "
                   f"{bdr_report['expense_fact']:,.2f}, прибыль "
                   f"{bdr_report['profit_fact']:,.2f} ₽")
        out.append(f"  БДР план: доходы {bdr_report['revenue_plan']:,.2f}, расходы "
                   f"{bdr_report['expense_plan']:,.2f}, план по прибыли "
                   f"{bdr_report['profit_plan']:,.2f} ₽")
        if bdr_report["unallocated"]:
            out.append(f"  ! не разнесено по статьям: {bdr_report['unallocated_total']:,.2f} ₽ — "
                       f"в прибыль не вошло")
        if bdr_report["missing_accounts"]:
            out.append("  ! счета вне плана: " + ", ".join(bdr_report["missing_accounts"]))
    advance_report = report.get("advances")
    if advance_report:
        out.append(f"  авансы: выдано {advance_report['given_total']:,.2f}, получено "
                   f"{advance_report['received_total']:,.2f}, к зачёту "
                   f"{advance_report['offsettable']:,.2f} ₽")
    for row in report["overdue"]:
        out.append(f"  просрочено: {row['number']} до {row['due_date']} — "
                   f"{row['amount']:,.2f} ₽")
    if report["not_included"]:
        out.append("  не входит в экран: " + "; ".join(report["not_included"]))
    return "\n".join(out)


def bdr_markdown(report: dict) -> str:
    out = [f"# БДР за {report['period']}", "",
           "| Статья | Тип | Факт (начисление) | План | Источник факта |", "|---|---|---:|---:|---|"]
    for row in report["rows"]:
        out.append(f"| {row['article']} | {row['kind']} | {row['fact']:,.2f} ₽ | "
                   f"{row['plan']:,.2f} ₽ | {row['source_fact']} |")
    out += ["", f"Факт: доходы {report['revenue_fact']:,.2f} ₽, расходы "
                f"{report['expense_fact']:,.2f} ₽, прибыль {report['profit_fact']:,.2f} ₽",
            "", f"План: доходы {report['revenue_plan']:,.2f} ₽, расходы "
                f"{report['expense_plan']:,.2f} ₽, план по прибыли "
                f"{report['profit_plan']:,.2f} ₽",
            "", f"> {report['note']}", ""]
    if report["unallocated"]:
        out += ["## Не разнесено по статьям", "",
                "| Счёт | Сумма | Почему |", "|---|---:|---|"]
        for row in report["unallocated"]:
            out.append(f"| {row['account']} | {row['amount']:,.2f} ₽ | {row['why']} |")
        out += ["", f"Всего не разнесено: {report['unallocated_total']:,.2f} ₽ — расходы "
                    f"есть, но в статьи БДР не попали: прибыль выше на эту сумму", ""]
    if report["missing_accounts"]:
        out += [f"Счета вне рабочего плана: {', '.join(report['missing_accounts'])}", ""]
    return "\n".join(out)


def screen_markdown(report: dict) -> str:
    out = [f"# Казначейство: один экран на {report['period']}", ""]
    if report.get("object") or report.get("contract"):
        out.append(f"Объект: {report.get('object') or '—'} · договор: "
                   f"{report.get('contract') or '—'}")
        out.append("")
    cash = report["cash"]
    out += ["## Деньги", "",
            "| Показатель | Сумма |", "|---|---:|",
            f"| Остаток на начало | {cash['opening']:,.2f} ₽ |",
            f"| Поступления (факт) | {cash['in_fact']:,.2f} ₽ |",
            f"| Поступления (план) | {cash['in_plan']:,.2f} ₽ |",
            f"| Оплаты (факт) | {cash['out_fact']:,.2f} ₽ |",
            f"| К оплате (план) | {cash['out_plan']:,.2f} ₽ |",
            f"| **Остаток на конец** | **{cash['closing']:,.2f} ₽** |", ""]
    if cash["gaps"]:
        out.append("Кассовые разрывы: " + "; ".join(
            f"{gap['date']} — {gap['amount']:,.2f} ₽" for gap in cash["gaps"]))
    else:
        out.append("Кассовых разрывов нет.")
    out.append("")
    if report.get("funding_note"):
        out += ["> " + report["funding_note"], ""]
    if report.get("funding_rows"):
        out += ["## График финансирования (входит в остаток)", "",
                "| Дата | Акт | Сумма |", "|---|---|---:|"]
        for row in report["funding_rows"]:
            out.append(f"| {row.get('payment_date')} | {row.get('act_id')} | "
                       f"{float(row.get('amount_rub') or 0):,.2f} ₽ |")
        out.append("")
    if report.get("funding_unlinked"):
        out += ["## Строки графика по другому договору (в остаток не входят)", "",
                "| Дата | Акт | Сумма |", "|---|---|---:|"]
        for row in report["funding_unlinked"]:
            out.append(f"| {row.get('payment_date')} | {row.get('act_id')} | "
                       f"{float(row.get('amount_rub') or 0):,.2f} ₽ |")
        out.append("")
    out += ["## Заявки", "",
            "Всего: " + str(report["requests"]["count"]) + " · "
            + " · ".join(f"{status}: {count}"
                         for status, count in sorted(report["requests"]["by_status"].items())), ""]
    if report["requests"]["blocked"]:
        out.append("Заблокированы лимитом: " + "; ".join(
            f"{row['number']} на {row['amount']:,.2f} ₽" for row in report["requests"]["blocked"]))
        out.append("")
    if report["limits"]:
        out += ["## Лимиты", "", "| Что | Лимит | Занято | Остаток | % |",
                "|---|---:|---:|---:|---:|"]
        for row in report["limits"]:
            pct = "—" if row["pct"] is None else f"{row['pct']:.1f} %"
            out.append(f"| {row['scope']}: {row['name']} | {row['limit']:,.2f} ₽ | "
                       f"{row['used']:,.2f} ₽ | {row['left']:,.2f} ₽ | {pct} |")
        out.append("")
    bdds_report = report["bdds"]
    out += ["## БДДС (движение денег)", "",
            f"Факт: поступило {bdds_report['in_fact']:,.2f} ₽, оплачено "
            f"{bdds_report['out_fact']:,.2f} ₽, чистый поток {bdds_report['net_fact']:,.2f} ₽",
            "",
            f"План: ожидается {bdds_report['in_plan']:,.2f} ₽, к оплате "
            f"{bdds_report['out_plan']:,.2f} ₽, чистый план {bdds_report['net_plan']:,.2f} ₽",
            "", f"> {bdds_report['note']}", ""]
    bdr_report = report.get("bdr")
    if bdr_report:
        out += ["## БДР (начисление)", "", "| Статья | Факт | План | Источник факта |",
                "|---|---:|---:|---|"]
        for row in bdr_report["rows"]:
            out.append(f"| {row['article']} | {row['fact']:,.2f} ₽ | {row['plan']:,.2f} ₽ | "
                       f"{row['source_fact']} |")
        out += ["", f"Итого факт: доходы {bdr_report['revenue_fact']:,.2f} ₽, расходы "
                    f"{bdr_report['expense_fact']:,.2f} ₽, прибыль "
                    f"{bdr_report['profit_fact']:,.2f} ₽",
                "", f"Итого план: доходы {bdr_report['revenue_plan']:,.2f} ₽, расходы "
                    f"{bdr_report['expense_plan']:,.2f} ₽, план по прибыли "
                    f"{bdr_report['profit_plan']:,.2f} ₽",
                "", f"> {bdr_report['note']}", ""]
        if bdr_report["unallocated"]:
            out += ["Не разнесено по статьям: " + "; ".join(
                f"счёт {row['account']} — {row['amount']:,.2f} ₽" for row in bdr_report["unallocated"]),
                ""]
    advance_report = report.get("advances")
    if advance_report:
        out += ["## Авансы", "",
                f"Выдано: {advance_report['given_total']:,.2f} ₽ · получено "
                f"{advance_report['received_total']:,.2f} ₽ · можно зачесть "
                f"{advance_report['offsettable']:,.2f} ₽ на {advance_report['as_of']}", ""]
        if advance_report["given"]:
            out += ["| Поставщик | Аванс | Долг | К зачёту |", "|---|---:|---:|---:|"]
            for row in advance_report["given"]:
                out.append(f"| {row['counterparty']} | {row['advance']:,.2f} ₽ | "
                           f"{row['debt']:,.2f} ₽ | {row['can_offset']:,.2f} ₽ |")
            out.append("")
        if advance_report["received"]:
            out += ["| Заказчик | Договор | Аванс | Долг | К зачёту |",
                    "|---|---|---:|---:|---:|"]
            for row in advance_report["received"]:
                out.append(f"| {row['counterparty']} | {row['contract']} | "
                           f"{row['advance']:,.2f} ₽ | {row['due']:,.2f} ₽ | "
                           f"{row['can_offset']:,.2f} ₽ |")
            out.append("")
        out += ["> " + advance_report["note"], ""]
    if report["overdue"]:
        out += ["## Просрочено", "", "| Заявка | Контрагент | Срок | Не оплачено |",
                "|---|---|---|---:|"]
        for row in report["overdue"]:
            out.append(f"| {row['number']} | {row['counterparty']} | {row['due_date']} | "
                       f"{row['amount']:,.2f} ₽ |")
        out.append("")
    if report["not_included"]:
        out += ["## Что в этот экран не входит", ""]
        out += [f"- {item}" for item in report["not_included"]]
        out.append("")
    return "\n".join(out)


# ─────────────────────────── контроль ───────────────────────────
def verify(state: dict) -> dict:
    """Сходится ли казначейство само с собой: оплаты, заявки, лимиты, дубли."""
    problems: list[dict] = []
    seen: dict[str, int] = {}
    for row in state.get("requests") or []:
        key = row.get("key") or ""
        if key:
            seen[key] = seen.get(key, 0) + 1
        paid = kopeck(row.get("paid"))
        total = kopeck(row.get("amount"))
        if paid > total + EPS:
            problems.append({"code": "OVERPAID", "what": row.get("number"),
                             "why": f"оплачено {rub(paid)} больше суммы заявки {rub(total)}"})
        if paid > 0 and row.get("status") not in (PAID, PARTIALLY_PAID):
            problems.append({"code": "PAID_WITHOUT_STATUS", "what": row.get("number"),
                             "why": f"оплата есть, а статус «{row.get('status')}»"})
        if row.get("status") in (PAID, PARTIALLY_PAID) and not (row.get("approvals") or []):
            problems.append({"code": "PAID_WITHOUT_APPROVAL", "what": row.get("number"),
                             "why": "оплата без единой подписи согласования"})
        if row.get("status") in (APPROVED, PARTIALLY_PAID, PAID):
            route = row.get("route") or []
            done = [item for item in row.get("approvals") or []
                    if item.get("decision") == "согласовано"]
            if len(done) < len(route) and row.get("status") != REJECTED:
                problems.append({"code": "ROUTE_INCOMPLETE", "what": row.get("number"),
                                 "why": f"маршрут пройден на {len(done)} из {len(route)}"})
            people = [item.get("person") for item in done]
            for left, right in zip(people, people[1:]):
                if left == right:
                    problems.append({"code": "SAME_PERSON", "what": row.get("number"),
                                     "why": f"две ступени подряд согласовал {left}"})
    for key, count in seen.items():
        if count > 1:
            problems.append({"code": "DUPLICATE", "what": key,
                             "why": f"заявок с одинаковым ключом: {count}"})
    paid_by_request: dict[str, Decimal] = {}
    for payment in state.get("payments") or []:
        paid_by_request[payment["request"]] = (paid_by_request.get(payment["request"],
                                                                   Decimal("0"))
                                               + kopeck(payment["amount"]))
    for number, total in paid_by_request.items():
        row = find(state, number)
        if not row:
            problems.append({"code": "ORPHAN_PAYMENT", "what": number,
                             "why": "оплата по заявке, которой нет"})
            continue
        if total > kopeck(row.get("amount")) + EPS:
            problems.append({"code": "OVERPAID", "what": number,
                             "why": f"оплаты {rub(total)} больше заявки {rub(row.get('amount'))}"})
        if kopeck(row.get("paid")) != total:
            problems.append({"code": "PAID_SUM_MISMATCH", "what": number,
                             "why": f"в заявке «оплачено» {rub(row.get('paid'))}, "
                                    f"а по платежам {rub(total)}"})
    for period, table in (state.get("object_limits") or {}).items():
        if not isinstance(table, dict):
            continue
        for object_name, articles in table.items():
            for article in (articles or {}):
                limit = _object_limit_for(state, object_name, article, period)
                if limit is None:
                    continue
                used = committed_object(state, object_name=object_name, article=article,
                                        period=period)
                if used > limit + EPS:
                    overrides = [row for row in state.get("audit") or []
                                 if row.get("what") == "превышение лимита объекта разрешено"
                                 and row.get("article") == article
                                 and row.get("object") == object_name]
                    if not overrides:
                        problems.append({"code": "OBJECT_LIMIT_EXCEEDED",
                                         "what": f"{object_name} / {article} / {period}",
                                         "why": f"заявок на {rub(used)} при лимите "
                                                f"{rub(limit)}"})
    for period, table in (state.get("limits") or {}).items():
        if not isinstance(table, dict):
            continue
        for article in table:
            limit = _limit_for(state, article, period)
            if limit is None:
                continue
            used = committed(state, article=article, period=period)
            if used > limit + EPS:
                overrides = [row for row in state.get("audit") or []
                             if row.get("what") == "превышение лимита разрешено"
                             and row.get("article") == article]
                if not overrides:
                    problems.append({"code": "LIMIT_EXCEEDED", "what": f"{article} / {period}",
                                     "why": f"заявок на {rub(used)} при лимите {rub(limit)}"})
    for row in state.get("receipts") or []:
        if not row.get("plan") and not str(row.get("number") or "").strip():
            problems.append({"code": "BAD_RECEIPT", "what": "поступление без номера",
                             "why": "поступление без платёжного поручения не сверяется с выпиской"})
    balance = balance_at(state, "9999-12-31")
    overdrafts = [row for row in state.get("payments") or [] if row.get("overdraft")]
    return {"ok": not problems, "requests": len(state.get("requests") or []),
            "payments": len(state.get("payments") or []),
            "receipts": len(state.get("receipts") or []),
            "paid_total": num(sum(kopeck(row.get("amount")) for row in
                                  state.get("payments") or [])),
            "received_total": num(sum(kopeck(row.get("amount")) for row in
                                      state.get("receipts") or [])),
            "balance": num(balance), "overdrafts": len(overdrafts),
            "problems": problems,
            "summary": ("казначейство сходится: оплаты не больше заявок, маршруты пройдены"
                        if not problems else f"расхождений: {len(problems)}")}


# ─────────────────────────── отчёты ───────────────────────────
def requests_markdown(state: dict) -> str:
    out = ["# Заявки на оплату", ""]
    rows = state.get("requests") or []
    if not rows:
        return "\n".join(out + ["заявок нет"]) + "\n"
    out += ["| № | Дата | Получатель | Статья | Сумма | Срок | Статус | Маршрут |",
            "|---|---|---|---|---:|---|---|---|"]
    for row in rows:
        done = len([item for item in row.get("approvals") or []
                    if item.get("decision") == "согласовано"])
        route = " → ".join(row.get("route") or []) or "—"
        out.append(f"| {row['number']} | {row['date']} | {row['counterparty']} | "
                   f"{row['article']} | {rub(row['amount'])} | {row.get('due_date') or '—'} | "
                   f"{row['status']} | {done}/{len(row.get('route') or [])}: {route} |")
    return "\n".join(out) + "\n"


def calendar_markdown(report: dict) -> str:
    out = [f"# Платёжный календарь на {report['period']}", "",
           f"Остаток на начало: {rub(report['opening'])}", "",
           "| Дата | Поступления (факт) | Поступления (план) | Оплаты (факт) | Оплаты (план) "
           "| Остаток на конец |", "|---|---:|---:|---:|---:|---:|"]
    for row in report["rows"]:
        out.append(f"| {row['date']} | {rub(row['in_fact']) if row['in_fact'] else '—'} | "
                   f"{rub(row['in_plan']) if row['in_plan'] else '—'} | "
                   f"{rub(row['out_fact']) if row['out_fact'] else '—'} | "
                   f"{rub(row['out_plan']) if row['out_plan'] else '—'} | "
                   f"{rub(row['balance'])} |")
    out += ["", f"Итого факт: поступило {rub(report['in_fact'])}, оплачено "
                f"{rub(report['out_fact'])}; план: ожидается {rub(report['in_plan'])}, "
                f"к оплате {rub(report['out_plan'])}", "",
            f"Остаток на конец: {rub(report['closing'])}"]
    if report["gaps"]:
        out += ["", "## Кассовые разрывы", ""]
        for gap in report["gaps"]:
            out.append(f"- {gap['date']}: не хватает {rub(gap['amount'])} — {gap['why']}")
    for warning in report.get("warnings") or []:
        out += ["", f"> {warning}"]
    if report.get("funding_unlinked"):
        out += ["", "## Строки графика финансирования по другому договору (в остаток не входят)", ""]
        for row in report["funding_unlinked"]:
            out.append(f"- {row.get('payment_date')}: {row.get('act_id')} — "
                       f"{rub(row.get('amount_rub'))}")
    if report.get("overdue"):
        out += ["", "## Просроченные заявки", ""]
        for row in report["overdue"]:
            out.append(f"- {row['number']} ({row['counterparty']}): срок {row['due_date']}, "
                       f"не оплачено {rub(row['amount'])}")
    return "\n".join(out) + "\n"


def bdds_markdown(report: dict) -> str:
    out = [f"# БДДС за {report['period']}", "",
           "| Статья | Тип | Поступления (факт) | Оплаты (факт) | Ожидается | К оплате |",
           "|---|---|---:|---:|---:|---:|"]
    for row in report["rows"]:
        out.append(f"| {row['article']} | {row['kind']} | "
                   f"{rub(row['in_fact']) if row['in_fact'] else '—'} | "
                   f"{rub(row['out_fact']) if row['out_fact'] else '—'} | "
                   f"{rub(row['in_plan']) if row['in_plan'] else '—'} | "
                   f"{rub(row['out_plan']) if row['out_plan'] else '—'} |")
    out += ["", f"Факт: поступления {rub(report['in_fact'])}, оплаты {rub(report['out_fact'])}, "
                f"чистый поток {rub(report['net_fact'])}",
            f"План: ожидается {rub(report['in_plan'])}, к оплате {rub(report['out_plan'])}, "
            f"чистый план {rub(report['net_plan'])}", "", report["note"]]
    return "\n".join(out) + "\n"
