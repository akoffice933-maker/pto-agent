# -*- coding: utf-8 -*-
"""Закрытие года: реформация баланса, недостачи, оценочные обязательства, РБП.

Что делает закрытие года
------------------------
Месяц закрывает результат на 99 и оставляет его там. Год обязан свести 99 с 84 и разобрать
то, что «висит» между годами: недостачи (94), оценочные обязательства (96) и расходы будущих
периодов (97). Здесь это сделано шагами, и каждый шаг говорит, что с ним стало.

Пять правил, из-за которых шаги устроены именно так
----------------------------------------------------
1. **Нельзя реформировать год, в котором не закрыты месяцы.** Открытый месяц означает, что
   в прибыль года ещё попадут операции — и «результат года» станет другой цифрой.
2. **Недостачи не закрываются арифметикой.** «На виновного / в затраты / в прочие расходы» —
   решение человека с основанием (акт ИНВ-3, приказ, согласие работника). Без решения 94
   остаётся на балансе и называется цифрой, а не списывается «куда-нибудь».
3. **Остаток 96 в доходы не списывается «до нуля».** На 96 лежат оценочные обязательства
   (ФСБУ 8/2020), а не старые «резервы предстоящих расходов»: обязательство переходит на
   следующий год, пока оно существует. В доход — только прекращённое, с основанием.
4. **Остаток 97 — это расходы следующего года**, они переходят; списание — только по графику
   и с основанием.
5. **Реформация — одна проводка на год** и попадает в уже закрытый декабрь. Такая проводка
   разрешена только с названной причиной, и причина остаётся в самой записи журнала.

Чего закрытие года не делает
----------------------------
Не формирует годовую отчётность (баланс, ОФР), не пересчитывает налог на прибыль по данным
бухучёта, не распределяет прибыль (дивиденды, фонды) и не ведёт налоговые регистры.
Полный список — в `data/reference/year_close_rules.json` → `not_implemented`; он же
попадает в отчёт, чтобы «чего нет» было видно на экране, а не только в документации.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from . import ledger

SCHEMA = "pto-agent/year-close@1"
RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "year_close_rules.json"

DONE = "выполнено"
PARTIAL = "выполнено частично"
NOT_NEEDED = "не требуется"
NOT_DONE = "не выполнено"
NEEDS_DECISION = "требует решения"
PROPOSED = "предложено"

MONTHS = tuple(f"{month:02d}" for month in range(1, 13))

#: операции года выполняются 31 декабря, когда декабрь уже закрыт: причина обязательна
YEAR_END_REASON = ("операция закрытия {year} года: выполняется 31 декабря после закрытия "
                   "декабря, поэтому попадает в закрытый период")


# ─────────────────────────── правила и месяцы ───────────────────────────
def load_rules(path: str | Path | None = None) -> dict:
    path = Path(path) if path else RULES_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} не {SCHEMA}")
    return payload


def months_of(year: str) -> list[str]:
    return [f"{year}-{month}" for month in MONTHS]


def closed_months(state: dict, year: str) -> list[str]:
    closed = {str(row.get("period")) for row in state.get("periods") or [] if row.get("closed")}
    return [period for period in months_of(year) if period in closed]


def open_months(state: dict, year: str) -> list[str]:
    closed = set(closed_months(state, year))
    return [period for period in months_of(year) if period not in closed]


def _step(step_id: str, title: str, status: str, *, amount: Decimal | None = None,
          entry: dict | None = None, why: str = "", requests: list[dict] | None = None,
          detail: list[dict] | None = None, blocking: bool | None = None,
          problems: list[dict] | None = None) -> dict:
    return {"id": step_id, "title": title, "status": status,
            "amount": ledger.as_float(amount) if amount is not None else None,
            "entry": (entry or {}).get("id"), "why": why,
            "requests": requests or [], "detail": detail or [], "problems": problems or [],
            "blocking": (status in (NOT_DONE,)) if blocking is None else blocking}


def _balance(state: dict, account: str, date: str,
             group: str | tuple[str, ...] | None = None) -> dict[tuple, Decimal]:
    keys = () if group is None else ((group,) if isinstance(group, str) else tuple(group))
    return ledger.balance_as_of(state, account, date, group_by=keys)


def _total(rows: dict[tuple, Decimal]) -> Decimal:
    return ledger.kopeck(sum((value for value in rows.values()), Decimal("0")))


def _positive(rows: dict[tuple, Decimal]) -> dict[tuple, Decimal]:
    return {key: value for key, value in rows.items() if abs(value) > ledger.EPS}


def year_end(year: str) -> str:
    return f"{year}-12-31"


# ─────────────────────────── предпосылки ───────────────────────────
def precheck(state: dict, year: str, *, chart: dict | None = None) -> dict:
    """Можно ли вообще закрывать год: журнал, месяцы, счета результатов."""
    chart = chart or ledger.load_chart()
    date = year_end(year)
    problems: list[dict] = []
    report = ledger.verify(state, chart=chart)
    if not report["ok"]:
        problems.append({"code": "JOURNAL_BROKEN", "what": year,
                         "why": f"журнал не сходится: {report['summary']}"})
    opened = open_months(state, year)
    if opened:
        problems.append({"code": "MONTH_NOT_CLOSED", "what": ", ".join(opened),
                         "why": "реформация года при открытых месяцах искажает результат года: "
                                f"не закрыты {len(opened)} из 12 месяцев"})
    for account in ("90.09", "91.09"):
        value = _total(_positive(_balance(state, account, date)))
        if abs(value) > ledger.EPS:
            problems.append({"code": "RESULT_NOT_CLOSED", "what": account,
                             "why": f"на {account} сальдо {ledger._rub(value)}: субсчета 90/91 "
                                    f"сводит закрытие месяца, а не закрытие года"})
    return {"ok": not problems, "problems": problems, "closed_months": closed_months(state, year),
            "open_months": opened}


# ─────────────────────────── недостачи 94 ───────────────────────────
SHORTAGE_TARGETS = {
    "victim": {"account": "73.02", "title": "на виновного", "analytics": ("сотрудник",)},
    "costs": {"account": "20.01", "title": "в затраты", "analytics": ("объект",)},
    "other": {"account": "91.02", "title": "в прочие расходы", "analytics": ()},
}


def step_shortages(state: dict, year: str, *, decisions: dict | None, mode: str, chart: dict,
                   rules: dict, date: str | None = None) -> dict:
    """94 закрывается только решением: кто возмещает — вопрос к человеку, а не к программе."""
    date = date or year_end(year)
    rows = _positive(_balance(state, "94", date, group="склад"))
    if not rows:
        return _step("shortages", "Недостачи 94: решение человека", NOT_NEEDED,
                     amount=Decimal("0"), why="сальдо 94 нет")
    decisions = decisions or {}
    given = {str(row.get("склад") or ""): row for row in decisions.get("shortages") or []}
    lines: list[dict] = []
    detail: list[dict] = []
    requests: list[dict] = []
    problems: list[dict] = []
    total = Decimal("0")
    for key, value in sorted(rows.items()):
        warehouse = str(key[0])
        balance = ledger.kopeck(value)
        decision = given.get(warehouse)
        detail.append({"склад": warehouse, "сальдо": ledger.as_float(balance)})
        if not decision:
            requests.append({"what": f"решение по недостаче {ledger._rub(balance)} ({warehouse})",
                            "why": "94 не закрывается автоматически: программа не решает, "
                                   "кто виноват",
                            "ask": "на виновного (ФИО, акт ИНВ-3, приказ) / в затраты (объект) / "
                                   "в прочие расходы"})
            continue
        kind = str(decision.get("decision") or "")
        target = SHORTAGE_TARGETS.get(kind)
        if not target:
            problems.append({"code": "BAD_DECISION", "what": warehouse,
                             "why": f"решение «{kind}» не из " + ", ".join(SHORTAGE_TARGETS)})
            continue
        basis = decision.get("basis") or {}
        if not (basis.get("number") and basis.get("date")):
            problems.append({"code": "NO_BASIS", "what": warehouse,
                             "why": "решение без основания (акт/приказ/согласие): 94 не "
                                    "списывается «по памяти»"})
            requests.append({"what": f"основание решения по {warehouse}",
                            "why": "акт ИНВ-3 и приказ о взыскании — часть проводки",
                            "ask": "номер и дата акта ИНВ-3, номер и дата приказа"})
            continue
        amount = ledger.kopeck(decision.get("amount") or balance)
        if amount <= 0 or amount > balance + ledger.EPS:
            problems.append({"code": "SHORTAGE_OVER_BALANCE", "what": warehouse,
                             "why": f"решение на {ledger._rub(amount)} при сальдо "
                                    f"{ledger._rub(balance)}: списать больше, чем есть, нельзя"})
            continue
        analytics: dict[str, str] = {}
        missing = False
        for field in target["analytics"]:
            value_of = str(decision.get(field) or "").strip()
            if not value_of:
                missing = True
                problems.append({"code": "ANALYTICS_MISSING", "what": f"{warehouse} / {field}",
                                 "why": f"счёт {target['account']} требует «{field}»: "
                                        f" решение {target['title']} без него не привязано "
                                        f"ни к кому"})
            analytics[field] = value_of
        if missing:
            continue
        total += amount
        lines.append({"account": "94", "side": ledger.CREDIT, "amount": amount,
                      "analytics": {"склад": warehouse}})
        lines.append({"account": target["account"], "side": ledger.DEBIT, "amount": amount,
                      "analytics": analytics})
        detail[-1]["решение"] = target["title"]
        detail[-1]["основание"] = f"{basis.get('number')} от {basis.get('date')}"

    if processed_ids(detail) == 0:
        status = NEEDS_DECISION if not problems else NOT_DONE
        return _step("shortages", "Недостачи 94: решение человека", status,
                     amount=_total(rows) if status == NEEDS_DECISION else Decimal("0"),
                     problems=problems,
                     why=("сальдо 94 " + ledger._rub(_total(rows)) + " остаётся: решения нет — "
                          "программа не решает, кто виноват")
                     if status == NEEDS_DECISION else
                     "; ".join(item["why"] for item in problems),
                     requests=requests, detail=detail, blocking=False)
    result = ledger.post_entry(
        state, event="year.shortages", date=date,
        doc={"number": f"закрытие года {year}: недостачи", "date": date, "kind": "closing"},
        lines=lines,
        memo=("недостачи по решению: " + ", ".join(
            f"{row['склад']} → {row.get('решение')} ({ledger._rub(row['сальдо'])})"
            for row in detail if row.get("решение"))),
        rule="year.shortages", key=f"year.shortages|{year}", chart=chart, rules=rules,
        mode=mode, allow_closed=True, late_reason=YEAR_END_REASON.format(year=year))
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    return _step("shortages", "Недостачи 94: решение человека", status, amount=total,
                 entry=result.get("entry"), requests=requests, detail=detail,
                 why="" if status in (DONE, PROPOSED) else
                 "; ".join(item["why"] for item in result.get("errors") or problems),
                 blocking=False)


def _decision_key(row: dict, field: str) -> str:
    """Ключ решения: имя аналитики («вид обязательства») или короткое «вид»."""
    return str(row.get(field) or row.get("вид") or "").strip()


def processed_ids(detail: list[dict]) -> int:
    return len([row for row in detail if row.get("решение")])


# ─────────────────── оценочные обязательства 96 ───────────────────
def step_provisions(state: dict, year: str, *, decisions: dict | None, mode: str, chart: dict,
                    rules: dict, date: str | None = None) -> dict:
    """96: обязательство переходит в следующий год, пока оно существует.

    Соблазн «списать остаток резерва в доходы 31 декабря» — самая дорогая ошибка закрытия года:
    она уменьшает обязательства и увеличивает прибыль. Поэтому по умолчанию остаток остаётся,
    а списание возможно только по прекращённому обязательству с основанием.
    """
    date = date or year_end(year)
    field = "вид обязательства"
    rows = _positive(_balance(state, "96", date, group=field))
    if not rows:
        return _step("provisions", "Оценочные обязательства 96", NOT_NEEDED,
                     amount=Decimal("0"), why="сальдо 96 нет: оценочные обязательства "
                                              "в контуре не начислялись")
    decisions = decisions or {}
    given = {_decision_key(row, field): row for row in decisions.get("provisions") or []}
    lines: list[dict] = []
    detail: list[dict] = []
    requests: list[dict] = []
    problems: list[dict] = []
    released = Decimal("0")
    carried = Decimal("0")
    for key, value in sorted(rows.items()):
        kind = str(key[0])
        balance = ledger.kopeck(-value)          # 96 пассивный: сальдо кредитовое
        decision = given.get(kind)
        detail.append({"вид": kind, "сальдо": ledger.as_float(balance)})
        if not decision:
            carried += balance
            requests.append({"what": f"статус обязательства «{kind}» ({ledger._rub(balance)})",
                            "why": "остаток 96 переходит на следующий год, пока обязательство "
                                   "существует; списание в доходы — только по прекращённому",
                            "ask": "«сохраняется» (остаток остаётся) или «прекращено» "
                                   "(основание: исполнено/отпало)"})
            detail[-1]["статус"] = "переходит (решения нет)"
            continue
        status = str(decision.get("status") or "")
        if status == "continues":
            carried += balance
            detail[-1]["статус"] = "переходит"
            continue
        if status != "released":
            problems.append({"code": "BAD_DECISION", "what": kind,
                             "why": f"статус «{status}» не из continues / released"})
            continue
        basis = decision.get("basis") or {}
        if not (basis.get("number") and basis.get("date")):
            problems.append({"code": "NO_BASIS", "what": kind,
                             "why": "прекращение обязательства без основания: списание в доходы "
                                    "без документа — это подгонка прибыли"})
            requests.append({"what": f"основание прекращения «{kind}»",
                            "why": "Дт 96 Кт 91.01 без основания не проводится",
                            "ask": "чем подтверждено, что обязательство прекращено"})
            continue
        released += balance
        lines.append({"account": "96", "side": ledger.DEBIT, "amount": balance,
                      "analytics": {field: kind}})
        lines.append({"account": "91.01", "side": ledger.CREDIT, "amount": balance,
                      "analytics": {}})
        detail[-1]["статус"] = f"прекращено → доходы ({basis.get('number')})"
    if not lines:
        status = NOT_DONE if problems else (NEEDS_DECISION if requests else DONE)
        return _step("provisions", "Оценочные обязательства 96", status, problems=problems,
                     amount=carried if status == NEEDS_DECISION else Decimal("0"),
                     why=(f"переходит на {int(year) + 1}: {ledger._rub(carried)} — обязательство "
                          f"не прекращено, в доходы не списывается")
                     if status == NEEDS_DECISION else
                     ("; ".join(item["why"] for item in problems) or
                      "все обязательства подтверждены как действующие"),
                     requests=requests, detail=detail, blocking=False)
    result = ledger.post_entry(
        state, event="year.provisions", date=date,
        doc={"number": f"закрытие года {year}: оценочные обязательства", "date": date,
             "kind": "closing"},
        lines=lines,
        memo=(f"прекращённые оценочные обязательства списаны в прочие доходы: "
              f"{ledger._rub(released)}; переходит на {int(year) + 1}: {ledger._rub(carried)}"),
        rule="year.provisions", key=f"year.provisions|{year}", chart=chart, rules=rules,
        mode=mode, allow_closed=True, late_reason=YEAR_END_REASON.format(year=year))
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    if carried > ledger.EPS and status == DONE:
        status = PARTIAL            # часть обязательств осталась — это не «закрыто всё»
    return _step("provisions", "Оценочные обязательства 96", status, amount=released,
                 entry=result.get("entry"), requests=requests, detail=detail,
                 why=(f"переходит на {int(year) + 1}: {ledger._rub(carried)}")
                 if carried > ledger.EPS else "",
                 blocking=False)


# ─────────────────── расходы будущих периодов 97 ───────────────────
def step_future_costs(state: dict, year: str, *, decisions: dict | None, mode: str, chart: dict,
                      rules: dict, date: str | None = None) -> dict:
    """97: остаток — это расходы следующего года. Списывается только по графику и основанию."""
    date = date or year_end(year)
    field = "вид расхода"
    rows = _positive(_balance(state, "97", date, group=field))
    if not rows:
        return _step("future_costs", "Расходы будущих периодов 97", NOT_NEEDED,
                     amount=Decimal("0"), why="сальдо 97 нет")
    decisions = decisions or {}
    given = {_decision_key(row, field): row for row in decisions.get("future_costs") or []}
    lines: list[dict] = []
    detail: list[dict] = []
    requests: list[dict] = []
    problems: list[dict] = []
    written = Decimal("0")
    carried = Decimal("0")
    for key, value in sorted(rows.items()):
        kind = str(key[0])
        balance = ledger.kopeck(value)
        decision = given.get(kind)
        detail.append({"вид": kind, "сальдо": ledger.as_float(balance)})
        if not decision:
            carried += balance
            requests.append({"what": f"график списания «{kind}» ({ledger._rub(balance)})",
                            "why": "остаток 97 относится к следующим периодам: списывать "
                                   "«до нуля» 31 декабря нельзя",
                            "ask": "период и доля списания, документ-основание"})
            detail[-1]["статус"] = "переходит (графика нет)"
            continue
        amount = ledger.kopeck(decision.get("amount") or 0)
        if amount <= 0:
            carried += balance
            detail[-1]["статус"] = "переходит (списание в этом году 0)"
            continue
        if amount > balance + ledger.EPS:
            problems.append({"code": "FUTURE_OVER_BALANCE", "what": kind,
                             "why": f"списание {ledger._rub(amount)} больше остатка "
                                    f"{ledger._rub(balance)}"})
            continue
        to = str(decision.get("to") or "")
        if to not in ("20.01", "26", "91.02"):
            problems.append({"code": "BAD_TARGET", "what": kind,
                             "why": f"счёт списания «{to}» не из 20.01 / 26 / 91.02"})
            continue
        basis = decision.get("basis") or {}
        if not (basis.get("number") and basis.get("date")):
            problems.append({"code": "NO_BASIS", "what": kind,
                             "why": "списание 97 без основания"})
            continue
        analytics: dict[str, str] = {}
        if to == "20.01":
            analytics["объект"] = str(decision.get("object") or "").strip()
        if to == "26":
            analytics["статья затрат"] = str(decision.get("article") or "").strip()
        if any(not value_of for value_of in analytics.values()):
            problems.append({"code": "ANALYTICS_MISSING", "what": kind,
                             "why": f"счёт {to} требует аналитику: {', '.join(analytics)}"})
            continue
        written += amount
        carried += balance - amount
        lines.append({"account": "97", "side": ledger.CREDIT, "amount": amount,
                      "analytics": {field: kind}})
        lines.append({"account": to, "side": ledger.DEBIT, "amount": amount,
                      "analytics": analytics})
        detail[-1]["статус"] = f"списано {ledger._rub(amount)} на {to}"
    if not lines:
        status = NOT_DONE if problems else (NEEDS_DECISION if requests else DONE)
        return _step("future_costs", "Расходы будущих периодов 97", status, problems=problems,
                     amount=carried if status == NEEDS_DECISION else Decimal("0"),
                     why=(f"переходит на {int(year) + 1}: {ledger._rub(carried)}")
                     if status == NEEDS_DECISION else
                     ("; ".join(item["why"] for item in problems) or "списывать нечего"),
                     requests=requests, detail=detail, blocking=False)
    result = ledger.post_entry(
        state, event="year.future_costs", date=date,
        doc={"number": f"закрытие года {year}: расходы будущих периодов", "date": date,
             "kind": "closing"},
        lines=lines, memo=(f"списано РБП {ledger._rub(written)}; переходит на {int(year) + 1}: "
                           f"{ledger._rub(carried)}"),
        rule="year.future_costs", key=f"year.future_costs|{year}", chart=chart, rules=rules,
        mode=mode, allow_closed=True, late_reason=YEAR_END_REASON.format(year=year))
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    if carried > ledger.EPS and status == DONE:
        status = PARTIAL
    return _step("future_costs", "Расходы будущих периодов 97", status, amount=written,
                 entry=result.get("entry"), requests=requests, detail=detail,
                 why=f"переходит на {int(year) + 1}: {ledger._rub(carried)}"
                 if carried > ledger.EPS else "", blocking=False)


# ─────────── перекрытие декабря после операций года (круг 41) ───────────
def _year_turnover(state: dict, year: str, account: str, side: str,
                   dim: str) -> dict[str, Decimal]:
    """Оборот счёта за год по аналитике — база для перекрытия, а не сальдо на 31.12."""
    out: dict[str, Decimal] = {}
    for entry in state.get("entries") or []:
        if str(entry.get("period") or "")[:4] != year:
            continue
        for line in entry.get("lines") or []:
            if line["account"] != account or line["side"] != side:
                continue
            key = str((line.get("analytics") or {}).get(dim) or "")
            out[key] = out.get(key, Decimal("0")) + ledger.kopeck(line["amount"])
    return out


def _close_account(state: dict, account: str, target: str, date: str, *, chart: dict,
                   rules: dict, mode: str, event: str, key: str, memo: str) -> dict:
    """Свернуть остатки счёта (со субсчетами аналитики) на сводный: направление — по знаку."""
    keys = tuple((ledger.account_of(chart, account) or {}).get("analytics") or ())
    rows = _positive(_balance(state, account, date, group=keys or None))
    lines: list[dict] = []
    total = Decimal("0")
    for signature, value in sorted(rows.items()):
        amount = ledger.kopeck(abs(value))
        analytics = dict(zip(keys, signature)) if keys else {}
        total += amount
        if value > 0:      # дебетовое сальдо — закрываем кредитом
            lines.append({"account": account, "side": ledger.CREDIT, "amount": amount,
                          "analytics": analytics})
            lines.append({"account": target, "side": ledger.DEBIT, "amount": amount,
                          "analytics": {}})
        else:
            lines.append({"account": target, "side": ledger.CREDIT, "amount": amount,
                          "analytics": {}})
            lines.append({"account": account, "side": ledger.DEBIT, "amount": amount,
                          "analytics": analytics})
    if not lines:
        return {"posted": False, "amount": 0.0}
    result = ledger.post_entry(
        state, event=event, date=date,
        doc={"number": memo, "date": date, "kind": "closing"}, lines=lines, memo=memo,
        rule=event, key=key, chart=chart, rules=rules, mode=mode,
        allow_closed=True, late_reason=YEAR_END_REASON.format(year=date[:4]))
    return {"posted": result["status"] in (ledger.STATUS_OK, ledger.STATUS_PROPOSED),
            "status": result["status"], "amount": ledger.as_float(total),
            "entry": (result.get("entry") or {}).get("id"),
            "errors": result.get("errors") or []}


def step_reclose(state: dict, year: str, *, policy: dict, mode: str, chart: dict,
                 rules: dict, date: str | None = None) -> dict:
    """Перекрыть декабрь после операций года: 26 → 20.01 → 90.02, 90/91 → 99.

    Зачем это нужно и почему не делается закрытием месяца: операции года (недостачи,
    оценочные обязательства, РБП) относятся к 31 декабря, а декабрь к этому моменту уже
    закрыт. Их расходная сторона попадает на 26, 20.01 или 91.02, и без перекрытия эти
    счета остались бы с сальдо — то есть затраты года не дошли бы до результата.

    База распределения 26 берётся **за год**, а не за декабрь: прямые затраты декабря
    уже закрыты в 90.02, и «за период» база была бы нулевой.
    """
    date = date or year_end(year)
    period = f"{year}-12"
    detail: list[dict] = []
    total = Decimal("0")

    # 1) 26 → 20.01 по статьям, база — дебетовый оборот 20.01 за год по объектам
    rows26 = _positive(_balance(state, "26", date, group="статья затрат"))
    if rows26:
        base = {}
        for object_name, value in _year_turnover(state, year, "20.01", ledger.DEBIT,
                                                 "объект").items():
            object_name = str(object_name)
            if object_name:
                base[object_name] = value
        base_total = ledger.kopeck(sum(base.values(), Decimal("0")))
        if base_total <= 0:
            detail.append({"шаг": "26 → 20.01", "статус": "не выполнено",
                           "почему": "нет базы: за год нет прямых затрат на 20.01"})
        else:
            lines: list[dict] = []
            allocated_total = Decimal("0")
            objects = sorted(base)
            for (article,), value in sorted(rows26.items()):
                article = str(article)
                allocated = Decimal("0")
                for index, object_name in enumerate(objects):
                    share = base[object_name] / base_total
                    piece = (value - allocated) if index == len(objects) - 1 \
                        else ledger.kopeck(value * share)
                    allocated += piece
                    if piece == 0:
                        continue
                    lines.append({"account": "20.01", "side": ledger.DEBIT, "amount": piece,
                                  "analytics": {"объект": object_name,
                                                "статья затрат": article}})
                    detail.append({"шаг": "26 → 20.01", "объект": object_name,
                                   "статья": article, "сумма": ledger.as_float(piece),
                                   "доля": f"{float(share) * 100:.2f} %"})
                lines.append({"account": "26", "side": ledger.CREDIT, "amount": value,
                              "analytics": {"статья затрат": article}})
                allocated_total += value
            outcome = ledger.post_entry(
                state, event="year.reclose.overheads", date=date,
                doc={"number": f"перекрытие декабря {year}: 26 → 20.01", "date": date,
                     "kind": "closing"}, lines=lines,
                memo=f"26 → 20.01 по операциям года: {ledger._rub(allocated_total)}",
                rule="year.reclose.overheads", key=f"year.reclose.overheads|{year}",
                chart=chart, rules=rules, mode=mode, allow_closed=True,
                late_reason=YEAR_END_REASON.format(year=year))
            if outcome["status"] not in (ledger.STATUS_OK, ledger.STATUS_PROPOSED):
                detail.append({"шаг": "26 → 20.01", "статус": "не выполнено",
                               "почему": "; ".join(item["why"]
                                                   for item in outcome.get("errors") or [])})
            else:
                total += allocated_total

    # 2) 20.01 → 90.02 по объектам, у которых была выручка за год; остальные — НЗП
    rows20 = _positive(_balance(state, "20.01", date, group="объект"))
    revenue = _year_turnover(state, year, "90.01", ledger.CREDIT, "объект")
    revenue_objects = {name for name, value in revenue.items() if value > ledger.EPS and name}
    if rows20:
        lines = []
        moved = Decimal("0")
        for (object_name,), value in sorted(rows20.items()):
            object_name = str(object_name)
            if object_name not in revenue_objects:
                detail.append({"шаг": "20.01 → 90.02", "объект": object_name,
                               "статус": "остаётся в НЗП",
                               "почему": "по объекту нет признанной выручки за год",
                               "сумма": ledger.as_float(value)})
                continue
            lines.append({"account": "90.02", "side": ledger.DEBIT, "amount": value,
                          "analytics": {"объект": object_name}})
            lines.append({"account": "20.01", "side": ledger.CREDIT, "amount": value,
                          "analytics": {"объект": object_name}})
            moved += value
            detail.append({"шаг": "20.01 → 90.02", "объект": object_name,
                           "сумма": ledger.as_float(value)})
        if lines:
            outcome = ledger.post_entry(
                state, event="year.reclose.costs", date=date,
                doc={"number": f"перекрытие декабря {year}: 20.01 → 90.02", "date": date,
                     "kind": "closing"}, lines=lines,
                memo=f"20.01 → 90.02 по операциям года: {ledger._rub(moved)}",
                rule="year.reclose.costs", key=f"year.reclose.costs|{year}",
                chart=chart, rules=rules, mode=mode, allow_closed=True,
                late_reason=YEAR_END_REASON.format(year=year))
            if outcome["status"] in (ledger.STATUS_OK, ledger.STATUS_PROPOSED):
                total += moved

    # 3) 90.* → 90.09 и 91.* → 91.09, затем 90.09/91.09 → 99
    for account, target in (("90.01", "90.09"), ("90.02", "90.09"), ("90.03", "90.09"),
                            ("91.01", "91.09"), ("91.02", "91.09")):
        outcome = _close_account(state, account, target, date, chart=chart, rules=rules,
                                 mode=mode, event=f"year.reclose.{account}",
                                 key=f"year.reclose.{account}|{year}",
                                 memo=f"перекрытие {account} → {target} за {year}")
        if outcome["posted"]:
            total += ledger.kopeck(outcome["amount"])
            detail.append({"шаг": f"{account} → {target}", "сумма": outcome["amount"]})
    for account in ("90.09", "91.09"):
        outcome = _close_account(state, account, "99", date, chart=chart, rules=rules,
                                 mode=mode, event=f"year.reclose.{account}",
                                 key=f"year.reclose.{account}.profit|{year}",
                                 memo=f"перекрытие {account} → 99 за {year}")
        if outcome["posted"]:
            total += ledger.kopeck(outcome["amount"])
            detail.append({"шаг": f"{account} → 99", "сумма": outcome["amount"]})
    if not detail:
        return _step("reclose", "Перекрытие декабря после операций года", NOT_NEEDED,
                     amount=Decimal("0"),
                     why="после операций года на 26/20.01/90/91 новых остатков нет")
    return _step("reclose", "Перекрытие декабря после операций года", DONE, amount=total,
                 detail=detail,
                 why=f"перекрыто оборотов на {ledger._rub(total)}: без этого затраты года "
                     f"не дошли бы до результата")


# ─────────────────────────── реформация 99 → 84 ───────────────────────────
def step_profit_tax(state: dict, year: str, *, mode: str, chart: dict, rules: dict,
                    decisions: dict | None = None, date: str | None = None) -> dict:
    """Налог на прибыль по данным бухучёта: Дт 99 Кт 68.04 — до реформации.

    Почему отдельным шагом и почему **до** реформации: если провести налог после, он ляжет
    на 99 уже после того, как результат ушёл на 84, и сальдо 99 станет ненулевым. Тогда
    «чистая прибыль» в отчёте (2400) и остаток на 84 разойдутся — то есть отчётность
    перестанет сходиться с журналом.

    База и ставки
    -------------
    * база — **прибыль от реализации** за год: обороты 90.01 − 90.03 (без НДС) − 90.02.
      Внереализационные (91.01/91.02) не считаются: в контуре нет их разбивки по видам,
      и это названо в `not_included` налогового расчёта, а не выдано за ноль;
    * ставки — из `data/reference/tax_rates.json` (8 % федеральный + 17 % региональный);
    * налог считается **за год целиком**, а не суммой месяцев: сумма помесячных расчётов
      показывается рядом и расхождение объясняется, а не прячется.

    Когда налог не начисляется
    --------------------------
    * убыток — начислять нечего (`NOT_NEEDED`, причина названа);
    * месяц не закрыт (`PROFIT_NOT_CLOSED`) — сначала закрытие, потом налог: иначе прибыль
      завышена и налог посчитан от неверной базы. Шаг не блокирует закрытие года, но и не
      делает вид, что налог проведён: дальше по отчётности снова будет разрыв, названный цифрой.
    """
    from . import tax

    year = str(year)
    date = date or year_end(year)
    report = tax.profit_return(state, year)
    totals = report.get("totals") or {}
    amount = ledger.kopeck(Decimal(str(totals.get("tax") or 0)))
    monthly = Decimal("0")
    for month in MONTHS:
        monthly += ledger.kopeck(Decimal(str(
            (tax.profit_return(state, f"{year}-{month}").get("totals") or {}).get("tax") or 0)))
    base = Decimal(str(totals.get("profit") or 0))

    detail = [{"база, прибыль от реализации": ledger.as_float(base),
               "ставки": f"{(totals.get('federal_rate'))}% + {totals.get('regional_rate')}%",
               "налог за год": ledger.as_float(amount),
               "сумма помесячных расчётов": ledger.as_float(monthly),
               "пояснение": ("налог считается за отчётный период целиком (ст. 286 НК РФ — "
                             "нарастающим итогом); сумма помесячных авансов совпадает с годом "
                             "не всегда, и подменять одно другим нельзя")}]

    if base <= 0 or amount <= 0:
        return _step("profit_tax", "Налог на прибыль: Дт 99 Кт 68.04", NOT_NEEDED,
                     amount=Decimal("0"), detail=detail,
                     why=(f"прибыли от реализации за {year} нет ({ledger._rub(base)}) — "
                          f"начислять налог не с чего"))

    # затраты на 20.01, не списанные в 90.02, — это незакрытый месяц: налог от завышенной
    # прибыли считать нельзя. Проверяем остаток, а не только флаг налогового модуля:
    # модуль ловит лишь случай «90.02 пуст целиком», а частично незакрытый месяц — нет.
    open_costs = ledger.kopeck(_total(_balance(state, "20.01", date)))
    detail.append({"остаток 20.01 на 31.12": ledger.as_float(open_costs)})
    if abs(open_costs) > ledger.EPS:
        return _step("profit_tax", "Налог на прибыль: Дт 99 Кт 68.04", NOT_DONE,
                     amount=amount, detail=detail, blocking=False,
                     why=(f"на 20.01 остаток {ledger._rub(open_costs)}: затраты не списаны в "
                          f"90.02, месяц не закрыт — прибыль завышена на эту сумму, и налог "
                          f"{ledger._rub(amount)} был бы больше настоящего. Сначала закрытие"),
                     requests=[{"what": "закрыть месяцы года",
                                "why": f"остаток 20.01 {ledger._rub(open_costs)}",
                                "ask": "closing: месяц → закрытие → налог"}])

    problems = [item for item in report.get("problems") or []]
    if problems:
        return _step("profit_tax", "Налог на прибыль: Дт 99 Кт 68.04", NOT_DONE,
                     amount=amount, detail=detail, blocking=False,
                     why="; ".join(str(item.get("why")) for item in problems),
                     requests=[{"what": "закрыть месяцы года до расчёта налога",
                                "why": str(problems[0].get("why")),
                                "ask": "closing: месяц → закрытие → налог"}])

    result = ledger.post_entry(
        state, event="year.profit_tax", date=date,
        doc={"number": f"налог на прибыль за {year}", "date": date, "kind": "closing"},
        lines=[{"account": "99", "side": ledger.DEBIT, "amount": amount, "analytics": {}},
               {"account": "68.04", "side": ledger.CREDIT, "amount": amount, "analytics": {}}],
        memo=(f"налог на прибыль {year}: прибыль от реализации {ledger._rub(base)} × "
              f"{totals.get('federal_rate')}% + {totals.get('regional_rate')}% = "
              f"{ledger._rub(amount)}; Дт 99 Кт 68.04"),
        rule="year.profit_tax", key=f"year.profit_tax|{year}", chart=chart, rules=rules,
        mode=mode, allow_closed=True,
        late_reason=(f"налог на прибыль — операция {year} года: проводится 31 декабря после "
                     f"закрытия декабря, чтобы результат года ушёл на 84 уже за вычетом налога"))
    if result["status"] == ledger.STATUS_DUPLICATE:
        # проводка налога за этот год уже есть: дубль не создаётся, и это не «не выполнено»
        return _step("profit_tax", "Налог на прибыль: Дт 99 Кт 68.04", DONE, amount=amount,
                     entry=result.get("entry"), detail=detail,
                     why=(f"проводка за {year} уже проведена (ключ year.profit_tax|{year}): "
                          f"повтор не создан — иначе налог начислился бы дважды"))
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    return _step("profit_tax", "Налог на прибыль: Дт 99 Кт 68.04", status, amount=amount,
                 entry=result.get("entry"), detail=detail,
                 why="" if status in (DONE, PROPOSED) else
                 ("; ".join(item["why"] for item in result.get("errors") or [])
                  or "; ".join(str(item) for item in result.get("warnings") or [])
                  or f"журнал не принял проводку: {result['status']}"))


def step_reformation(state: dict, year: str, *, mode: str, chart: dict, rules: dict,
                     date: str | None = None) -> dict:
    """Дт 99 Кт 84.01 — прибыль, Дт 84.02 Кт 99 — убыток. Одна проводка на год."""
    date = date or year_end(year)
    value = _total(_positive(_balance(state, "99", date)))
    if abs(value) <= ledger.EPS:
        return _step("reformation", "Реформация баланса 99 → 84", NOT_NEEDED,
                     amount=Decimal("0"), why="сальдо 99 нулевое: реформировать нечего")
    amount = ledger.kopeck(abs(value))
    profit = value < 0                    # кредитовое сальдо 99 — прибыль
    if profit:
        lines = [{"account": "99", "side": ledger.DEBIT, "amount": amount, "analytics": {}},
                 {"account": "84.01", "side": ledger.CREDIT, "amount": amount,
                  "analytics": {}}]
        title = "списана чистая прибыль отчётного года"
    else:
        lines = [{"account": "84.02", "side": ledger.DEBIT, "amount": amount,
                  "analytics": {}},
                 {"account": "99", "side": ledger.CREDIT, "amount": amount, "analytics": {}}]
        title = "списан непокрытый убыток отчётного года"
    result = ledger.post_entry(
        state, event="year.reformation", date=date,
        doc={"number": f"реформация баланса за {year}", "date": date, "kind": "closing"},
        lines=lines,
        memo=f"реформация {year}: {title} — {ledger._rub(amount)} "
             f"(сальдо 99 до реформации {ledger._rub(value)})",
        rule="year.reformation", key=f"year.reformation|{year}", chart=chart, rules=rules,
        mode=mode, allow_closed=True,
        late_reason=f"реформация баланса — последняя операция {year} года, "
                    f"она выполняется после закрытия декабря")
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    detail = [{"сальдо 99 до": ledger.as_float(value), "проводка": title,
               "счёт 84": "84.01" if profit else "84.02", "сумма": ledger.as_float(amount)}]
    after = _total(_positive(_balance(state, "99", date))) if status == DONE else None
    if status == DONE and abs(after) > ledger.EPS:
        status = NOT_DONE
    return _step("reformation", "Реформация баланса 99 → 84", status, amount=amount,
                 entry=result.get("entry"), detail=detail,
                 why="" if status in (DONE, PROPOSED) else
                 (f"после реформации сальдо 99 = {ledger._rub(after)} — это ошибка, "
                  f"а не результат" if status == NOT_DONE and after is not None else
                  "; ".join(item["why"] for item in result.get("errors") or [])))


# ─────────────────────────── итог года ───────────────────────────
def account_balance(state: dict, account: str, date: str, *, chart: dict | None = None,
                    with_subaccounts: bool = True) -> Decimal:
    """Сальдо счёта: сумма по счёту и его субсчетам (84 = 84.01 + 84.02)."""
    chart = chart or ledger.load_chart()
    total = _total(_balance(state, account, date))
    if with_subaccounts:
        for code in (ledger.accounts_index(chart) or {}):
            if code.startswith(account + "."):
                total += _total(_balance(state, code, date))
    return ledger.kopeck(total)


def _balances(state: dict, year: str, chart: dict) -> dict:
    date = year_end(year)
    out: dict[str, float] = {}
    for account in ("84", "99", "94", "96", "97"):
        if not ledger.account_of(chart, account):
            continue
        out[account] = ledger.as_float(account_balance(state, account, date, chart=chart))
    return out


def close_year(state: dict, year: str, *, decisions: dict | None = None,
               chart: dict | None = None, rules: dict | None = None,
               policy: dict | None = None, mode: str | None = None,
               dry_run: bool = False) -> dict:
    """Закрыть год шаг за шагом. Каждый шаг отчитывается, а не молчит."""
    from . import closing

    year = str(year).strip()          # 2026 и "2026" — один и тот же год
    chart = chart or ledger.load_chart()
    policy = policy or closing.load_policy()
    rules = rules or load_rules()
    mode = mode or ledger.MODE_POST
    result: dict = {"year": year, "closed": False, "mode": mode, "steps": [],
                    "problems": [], "warnings": [], "requests": [],
                    "not_done": [], "not_included": list(rules.get("not_implemented") or [])}
    if len(year) != 4 or not year.isdigit():
        result["problems"].append({"code": "BAD_YEAR", "what": str(year),
                                   "why": "год задаётся как ГГГГ"})
        return result

    check = precheck(state, year, chart=chart)
    result["precheck"] = check
    result["steps"].append(_step("precheck", "Предпосылки закрытия года",
                                 DONE if check["ok"] else NOT_DONE,
                                 why="" if check["ok"] else
                                 "; ".join(item["why"] for item in check["problems"]),
                                 detail=check["problems"]))
    if not check["ok"]:
        result["problems"].extend(check["problems"])
        result["requests"] = [{"what": "закрыть месяцы года",
                               "why": item["why"], "ask": "ledger close --period …"}
                              for item in check["problems"]
                              if item["code"] == "MONTH_NOT_CLOSED"]
        return result

    work = json.loads(json.dumps(state)) if dry_run else state
    for run in (
        lambda st: step_shortages(st, year, decisions=decisions, mode=mode, chart=chart,
                                  rules=rules),
        lambda st: step_provisions(st, year, decisions=decisions, mode=mode, chart=chart,
                                   rules=rules),
        lambda st: step_future_costs(st, year, decisions=decisions, mode=mode, chart=chart,
                                     rules=rules),
        lambda st: step_reclose(st, year, policy=policy, mode=mode, chart=chart, rules=rules),
        lambda st: step_profit_tax(st, year, mode=mode, chart=chart, rules=rules,
                                   decisions=decisions),
        lambda st: step_reformation(st, year, mode=mode, chart=chart, rules=rules),
    ):
        step = run(work)
        result["steps"].append(step)
        result["requests"].extend(step.get("requests") or [])
        if step["status"] == NOT_DONE:
            result["not_done"].append({"step": step["id"], "why": step["why"]})
            if step["blocking"]:
                result["problems"].append({"code": "STEP_FAILED", "what": step["title"],
                                           "why": step["why"] or "шаг не выполнен"})

    after = ledger.trial_balance(work, year + "-12", chart=chart)
    if not after["balanced"]:
        result["problems"].append({"code": "UNBALANCED_AFTER", "what": year,
                                   "why": "ОСВ не сходится после закрытия года"})
    result["balances"] = _balances(work, year, chart)
    result["result"] = _result_block(work, year, chart)
    result["turnover"] = after["totals"]
    if dry_run:
        result["dry_run"] = True
        return result
    if mode == ledger.MODE_PROPOSE:
        result["warnings"].append("режим «предлагать»: проводки закрытия года ждут решения "
                                  "(ledger proposals / ledger accept), год не закрыт")
        return result
    if result["problems"]:
        return result
    reform = next((step for step in result["steps"] if step["id"] == "reformation"), None)
    if not reform or reform["status"] not in (DONE, NOT_NEEDED):
        result["problems"].append({"code": "NOT_REFORMED", "what": year,
                                   "why": "реформация не выполнена: год не закрыт"})
        return result
    result["closed"] = True
    state.setdefault("years", []).append({
        "year": year, "closed": True, "seq": len(state.get("entries") or []),
        "profit": result["result"]["profit"],
        "reformation": reform.get("entry"),
        "steps": [{"id": step["id"], "status": step["status"], "amount": step["amount"]}
                  for step in result["steps"]],
    })
    return result


def _result_block(state: dict, year: str, chart: dict) -> dict:
    """Что получилось за год: результат и куда он лёг."""
    date = year_end(year)
    profit = Decimal("0")
    for entry in state.get("entries") or []:
        if str(entry.get("period") or "")[:4] != year:
            continue
        if entry.get("event") == "year.reformation":
            continue          # реформация переносит результат на 84, а не создаёт новый
        for line in entry.get("lines") or []:
            if line["account"] != "99":
                continue
            amount = ledger.kopeck(line["amount"])
            profit += amount if line["side"] == ledger.CREDIT else -amount
    profit_tax = Decimal("0")
    for entry in state.get("entries") or []:
        if str(entry.get("period") or "")[:4] != year or entry.get("event") != "year.profit_tax":
            continue
        for line in entry.get("lines") or []:
            if line["account"] == "99" and line["side"] == ledger.DEBIT:
                profit_tax += ledger.kopeck(line["amount"])
    retained = account_balance(state, "84", date, chart=chart)
    return {"profit": ledger.as_float(profit),
            "profit_before_tax": ledger.as_float(profit + profit_tax),
            "profit_tax": ledger.as_float(profit_tax),
            "retained": ledger.as_float(retained),
            "revenue": _turnover(state, year, "90.01", ledger.CREDIT),
            "cost": _turnover(state, year, "90.02", ledger.DEBIT),
            "vat_out": _turnover(state, year, "90.03", ledger.DEBIT),
            "other_income": _turnover(state, year, "91.01", ledger.CREDIT),
            "other_expense": _turnover(state, year, "91.02", ledger.DEBIT)}


def _turnover(state: dict, year: str, account: str, side: str) -> float:
    total = Decimal("0")
    for entry in state.get("entries") or []:
        if str(entry.get("period") or "")[:4] != year:
            continue
        for line in entry.get("lines") or []:
            if line["account"] == account and line["side"] == side:
                total += ledger.kopeck(line["amount"])
    return ledger.as_float(total)


# ─────────────────────────── вывод ───────────────────────────
def screen_text(report: dict) -> str:
    out = [f"закрытие года {report['year']}"
           + (f" · режим: {report['mode']}" if report.get("mode") else "")
           + (" · сухой прогон" if report.get("dry_run") else "")]
    for step in report.get("steps") or []:
        line = f"  [{step['status']}] {step['title']}"
        if step.get("amount"):
            line += f" — {step['amount']:,.2f} ₽"
        out.append(line)
        if step.get("why"):
            out.append(f"      {step['why']}")
    balances = report.get("balances") or {}
    if balances:
        out.append("  остатки на 31.12: " + ", ".join(
            f"{account} {value:,.2f} ₽" for account, value in balances.items()))
    summary = report.get("result") or {}
    if summary:
        out.append(f"  результат года по 99: {summary['profit']:,.2f} ₽; "
                   f"на 84: {summary['retained']:,.2f} ₽")
        out.append(f"  выручка {summary['revenue']:,.2f} ₽, в т.ч. НДС {summary['vat_out']:,.2f} ₽; "
                   f"себестоимость {summary['cost']:,.2f} ₽")
        out.append(f"  прочие доходы {summary['other_income']:,.2f} ₽, "
                   f"прочие расходы {summary['other_expense']:,.2f} ₽")
    for item in report.get("problems") or []:
        out.append(f"  ✗ {item['code']} · {item['what']}: {item['why']}")
    for text in report.get("warnings") or []:
        out.append(f"  · {text}")
    for item in report.get("requests") or []:
        out.append(f"  ? нужно: {item['what']} — {item['why']}" +
                   (f" → {item['ask']}" if item.get("ask") else ""))
    out.append("  не сделано (записано, а не забыто):")
    for row in report.get("not_included") or []:
        out.append(f"    - {row}")
    out.append("  итог: год закрыт" if report.get("closed") else "  итог: год не закрыт")
    return "\n".join(out)


def to_markdown(report: dict) -> str:
    lines = [f"# Закрытие года {report['year']}", ""]
    lines.append(f"Режим: {report.get('mode')}; сухой прогон: "
                 f"{'да' if report.get('dry_run') else 'нет'}; "
                 f"итог: {'год закрыт' if report.get('closed') else 'год не закрыт'}.")
    lines += ["", "| Шаг | Статус | Сумма | Что с ним стало |", "|---|---|---:|---|"]
    for step in report.get("steps") or []:
        amount = f"{step['amount']:,.2f} ₽".replace(",", " ") if step.get("amount") else "—"
        lines.append(f"| {step['title']} | {step['status']} | {amount} | {step.get('why') or ''} |")
    summary = report.get("result") or {}
    if summary:
        lines += ["", "## Результат года", "",
                  f"* результат по 99: {summary['profit']:,.2f} ₽".replace(",", " "),
                  f"* на 84 на конец года: {summary['retained']:,.2f} ₽".replace(",", " "),
                  f"* выручка {summary['revenue']:,.2f} ₽, НДС {summary['vat_out']:,.2f} ₽"
                  .replace(",", " "),
                  f"* себестоимость {summary['cost']:,.2f} ₽".replace(",", " ")]
    balances = report.get("balances") or {}
    if balances:
        lines += ["", "## Остатки на 31.12", "", "| Счёт | Сальдо |", "|---|---:|"]
        for account, value in balances.items():
            lines.append(f"| {account} | {value:,.2f} ₽ |".replace(",", " "))
    if report.get("problems"):
        lines += ["", "## Что мешает", ""]
        lines += [f"* **{item['code']}** · {item['what']}: {item['why']}"
                  for item in report["problems"]]
    if report.get("requests"):
        lines += ["", "## Что нужно от человека", ""]
        lines += [f"* {item['what']} — {item['why']}" for item in report["requests"]]
    if report.get("not_included"):
        lines += ["", "## Не сделано", ""]
        lines += [f"* {row}" for row in report["not_included"]]
    return "\n".join(lines) + "\n"
