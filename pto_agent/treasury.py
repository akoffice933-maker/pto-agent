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
    limit = _limit_for(state, article, period)
    used = committed(state, article=article, period=period)
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
        "limit": None if limit is None else num(limit),
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
        if str(row.get("date") or "") <= str(date):
            total += kopeck(row.get("amount"))
    for row in state.get("payments") or []:
        if str(row.get("date") or "") <= str(date):
            total -= kopeck(row.get("amount"))
    return total


def pay(state: dict, number: str, *, date: str, amount=None, allow_overdraft: bool = False,
        reason: str = "", person: str = "") -> dict:
    """Оплатить заявку. Согласованную, в пределах остатка и не больше суммы заявки."""
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
        if f"payment|{payment['id']}" in known:
            # проверять долг уже поздно: прошлая проводка его погасила
            results.append(_already(f"payment|{payment['id']}", payment["request"]))
            continue
        debt = ledger.balance_as_of(ledger_state, "60.01", str(payment["date"]),
                                    group_by=("контрагент",))
        owed = -sum((value for key, value in debt.items()
                     if key[0] == payment["counterparty"]), Decimal("0"))
        amount = kopeck(payment["amount"])
        if amount > owed + EPS:
            results.append({"status": ledger.STATUS_REJECTED, "entry": None,
                            "errors": [{"code": "PAYMENT_OVER_DEBT",
                                        "what": f"{payment['request']} / "
                                                f"{payment['counterparty']}",
                                        "why": f"оплата {rub(amount)} больше долга "
                                               f"{rub(owed)}: аванс поставщику на отдельном "
                                               f"субсчёте в этом круге не реализован"}],
                            "gaps": [], "warnings": []})
            continue
        line_debit = {"account": "60.01", "side": ledger.DEBIT, "amount": payment["amount"],
                      "analytics": {"контрагент": payment["counterparty"]}}
        line_credit = {"account": "51", "side": ledger.CREDIT, "amount": payment["amount"],
                       "analytics": {}}
        result = ledger.post_entry(
            ledger_state, event="payment", date=str(payment["date"]),
            doc={"number": f"п/п по заявке {payment['request']}", "date": str(payment["date"]),
                 "kind": "payment"},
            lines=[line_debit, line_credit],
            memo=(f"оплата {payment['counterparty']} по заявке {payment['request']}: "
                  f"{rub(amount)} · {payment.get('article')}"
                  + (" · кассовый разрыв" if payment.get("overdraft") else "")),
            rule="treasury.payment", key=f"payment|{payment['id']}",
            chart=chart, rules=rules, mode=mode or ledger.MODE_POST)
        result["payment"] = payment["id"]
        results.append(result)

    for receipt in state.get("receipts") or []:
        if f"receipt|{receipt['id']}" in known:
            results.append(_already(f"receipt|{receipt['id']}", receipt["number"]))
            continue
        receivable = sum(ledger.balance_as_of(ledger_state, "62.01", str(receipt["date"]),
                                             group_by=("контрагент", "договор")).values(),
                         Decimal("0"))
        amount = kopeck(receipt["amount"])
        warnings: list[str] = []
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
        if receipt.get("advance"):
            warnings.append(f"{receipt['number']}: помечено как аванс — отдельный субсчёт "
                            f"62.02 в этом круге не используется")
        result = ledger.post_entry(
            ledger_state, event="receipt_payment", date=str(receipt["date"]),
            doc={"number": f"п/п {receipt['number']}", "date": str(receipt["date"]),
                 "kind": "receipt_payment"},
            lines=[{"account": "51", "side": ledger.DEBIT, "amount": receipt["amount"],
                    "analytics": {}},
                   {"account": "62.01", "side": ledger.CREDIT, "amount": receipt["amount"],
                    "analytics": {"контрагент": receipt["counterparty"],
                                  "договор": receipt["contract"]}}],
            memo=(f"поступление {rub(amount)} от {receipt['counterparty']} "
                  f"по договору {receipt['contract']}"
                  + (" · аванс" if receipt.get("advance") else "")),
            rule="treasury.receipt", key=f"receipt|{receipt['id']}",
            chart=chart, rules=rules, mode=mode or ledger.MODE_POST)
        result["warnings"] = (result.get("warnings") or []) + warnings
        result["receipt"] = receipt["id"]
        results.append(result)

    posted = [r for r in results if r.get("status") in (ledger.STATUS_OK,
                                                        ledger.STATUS_PROPOSED)]
    return {"posted": len(posted),
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
