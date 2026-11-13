# -*- coding: utf-8 -*-
"""Закрытие месяца: затраты → себестоимость, НДС к вычету, финансовый результат.

Что делает закрытие
-------------------
Ядро учёта (`ledger.py`) записывает факты. Закрытие месяца превращает факты в результат:
распределяет косвенные расходы, списывает затраты по сданным работам в себестоимость,
зачитывает НДС и сводит субсчета 90/91 так, чтобы на 99 появился финансовый результат.

Шесть шагов, и каждый честно говорит, что с ним стало
-----------------------------------------------------
1. **25/26 → 20.01** — пропорционально прямым затратам периода. База распределения
   попадает в проводку строками `bases`, а не «считается где-то в отчёте».
2. **20.01 → 90.02** — только по объектам с признанной выручкой. По остальным затраты
   остаются незавершённым производством: работа не сдана, списывать нечего.
3. **19.03 → 68.02** — по зарегистрированным счетам-фактурам и только если НДС в счёте-фактуре
   сходится с проведённым. Непокрытый остаток не зачитывается и попадает в запросы.
4. **90.01/90.02/90.03 → 90.09** — закрытие субсчетов продаж.
5. **91.01/91.02 → 91.09** — то же для прочих доходов и расходов.
6. **90.09/91.09 → 99** — прибыль или убыток. Убыток — отдельной стороной, а не «минус».

Чего закрытие не делает и почему
--------------------------------
* **94 (недостачи) не закрывает.** «В затраты / на виновного / в прочие расходы» — решение
  человека; автоматика означала бы, что программа решила, кто виноват.
* **Не реформирует 99 → 84** — это операция года.
* **Не распределяет косвенные по выручке или ФОТ** — для этого нужна зарплата.
* **Не придумывает базу.** Нет базы распределения — шаг не выполнен с причиной, а не
  «распределили поровну».

Аналитика: затраты и выручка живут на разных разрезах
-----------------------------------------------------
Прораб пишет затраты по захваткам («корпус 2, плита»), договор — по объекту
(«ЖК «Северный», корпус 2»). Без сведения этих разрезов 20.01 и 90.02 не встретятся
никогда. Поэтому свёртка захваток в объект — **запись в учётной политике**
(`analytics.object.rollup`), а не догадка программы. Нет свёртки — закрытие отказывается
и говорит, какие захватки не сведены.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from . import ledger

SCHEMA = "pto-agent/policy@1"
POLICY_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "accounting_policy.json"

DONE = "выполнено"
PARTIAL = "выполнено частично"
NOT_NEEDED = "не требуется"
NOT_DONE = "не выполнено"
NEEDS_DECISION = "требует решения"
PROPOSED = "предложено"
#: «отложено» — шаг не выполнен, но закрытие не блокирует: НДС можно зачесть позже,
#: когда придёт счёт-фактура. Остаток при этом виден, а не спрятан.
PENDING = "отложено"

BLOCKING = {NOT_DONE}


# ─────────────────────────── политика ───────────────────────────
def load_policy(path: str | Path | None = None) -> dict:
    path = Path(path) if path else POLICY_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} не {SCHEMA!r}")
    return payload


def rollup_of(policy: dict, name: str) -> str:
    """Захватка → объект. Нет свёртки — объект остаётся как есть (и это будет видно)."""
    tables = (policy.get("analytics") or {}).get("object", {}).get("rollup") or {}
    for target, captures in tables.items():
        if name == target or name in captures:
            return target
    return name


def unrolled(policy: dict, names: set[str]) -> list[str]:
    """Захватки, для которых свёртка в объект не задана."""
    tables = (policy.get("analytics") or {}).get("object", {}).get("rollup") or {}
    known = set()
    for target, captures in tables.items():
        known.add(target)
        known.update(captures)
    return sorted(name for name in names if name and name not in known)


# ─────────────────────────── срезы журнала ───────────────────────────
def _period_end(period: str) -> str:
    year, month = period.split("-")
    month = int(month)
    if month == 12:
        return f"{year}-12-31"
    nxt = f"{int(year) + (1 if month == 12 else 0)}-{month + 1:02d}-01"
    from datetime import date, timedelta
    return (date.fromisoformat(nxt) - timedelta(days=1)).isoformat()


def balance_of(state: dict, account: str, *, date: str,
               group: str | tuple[str, ...] | None = None) -> dict[tuple, Decimal]:
    """Сальдо по счёту с разбивкой по аналитике. group — один ключ или набор ключей."""
    if group is None:
        keys: tuple[str, ...] = ()
    elif isinstance(group, str):
        keys = (group,)
    else:
        keys = tuple(group)
    return ledger.balance_as_of(state, account, date, group_by=keys)


def _positive(rows: dict[tuple, Decimal]) -> dict[tuple, Decimal]:
    return {key: value for key, value in rows.items() if abs(value) > ledger.EPS}


def turnover_in_period(state: dict, account: str, period: str,
                       group: str | None = None) -> dict[tuple, dict]:
    """Обороты счёта за месяц (не накопительно): пригодится как база распределения."""
    out: dict[tuple, dict] = {}
    for entry in state.get("entries") or []:
        if str(entry.get("period") or "") != period:
            continue
        for line in entry.get("lines") or []:
            if line["account"] != account:
                continue
            key = (str((line.get("analytics") or {}).get(group, "")) if group else "",)
            row = out.setdefault(key, {"debit": Decimal("0"), "credit": Decimal("0")})
            sign = "debit" if line["side"] == ledger.DEBIT else "credit"
            row[sign] += ledger.kopeck(line["amount"])
    return out


def _total(rows: dict[tuple, Decimal]) -> Decimal:
    return ledger.kopeck(sum(rows.values(), Decimal("0")))


def _step(step_id: str, title: str, status: str, *, amount: Decimal | None = None,
          entry: dict | None = None, why: str = "", requests: list[dict] | None = None,
          detail: list[dict] | None = None, blocking: bool | None = None) -> dict:
    return {"id": step_id, "title": title, "status": status,
            "amount": ledger.as_float(amount) if amount is not None else None,
            "entry": (entry or {}).get("id"), "why": why,
            "requests": requests or [], "detail": detail or [],
            "blocking": (status in BLOCKING) if blocking is None else blocking}


# ─────────────────────────── шаги закрытия ───────────────────────────
def step_overheads(state: dict, period: str, *, policy: dict, mode: str,
                   chart: dict, rules: dict, date: str) -> dict:
    """25/26 → 20.01.

    Два разных случая, и путать их нельзя:
    * счёт **с аналитикой по объекту** (25) уже собран по объектам — переносим как есть,
      распределять нечего;
    * счёт **без аналитики** (26) — общий на всю организацию, его раскладываем по объектам
      пропорционально прямым затратам периода. База и доли уходят в проводку.
    """
    accounts = [str(code) for code in (policy.get("overheads") or {}).get("accounts") or []]
    target = str((policy.get("overheads") or {}).get("target") or "20.01")
    balances: dict[str, tuple[Decimal, dict[tuple, Decimal], bool]] = {}
    for account in accounts:
        info = ledger.account_of(chart, account) or {}
        by_object = bool(info.get("analytics"))
        rows = _positive(balance_of(state, account, date=date, group="объект" if by_object else None))
        total = _total(rows)
        if total:
            balances[account] = (total, rows, by_object)
    if not balances:
        return _step("overheads", "Косвенные расходы 25/26 → 20.01", NOT_NEEDED,
                     amount=Decimal("0"), why="на 25 и 26 нет сальдо за период")

    grand_total = ledger.kopeck(sum(value for value, _, _ in balances.values()))

    # база нужна только для счетов без аналитики
    base_by_object: dict[str, Decimal] = {}
    base_total = Decimal("0")
    if any(not by_object for _, _, by_object in balances.values()):
        direct = turnover_in_period(state, target, period, group="объект")
        for (name,), row in direct.items():
            object_name = rollup_of(policy, name)
            base_by_object[object_name] = base_by_object.get(object_name, Decimal("0")) + row["debit"]
        base_total = ledger.kopeck(sum(base_by_object.values(), Decimal("0")))
        if base_total <= 0:
            return _step("overheads", "Косвенные расходы 25/26 → 20.01", NOT_DONE,
                         amount=grand_total,
                         why="нет базы распределения: за период нет прямых затрат на 20.01",
                         requests=[{"what": "база распределения косвенных расходов",
                                    "why": "на 20.01 нет оборотов за период",
                                    "ask": "провести требования М-11/М-8 или выбрать другую "
                                           "базу в учётной политике"}])

    lines: list[dict] = []
    bases: list[dict] = []
    detail: list[dict] = []
    for account, (total, rows, by_object) in balances.items():
        if by_object:
            for (name,), value in sorted(rows.items()):
                if abs(value) <= ledger.EPS:
                    continue
                lines.append({"account": target, "side": ledger.DEBIT, "amount": value,
                              "analytics": {"объект": name}})
                lines.append({"account": account, "side": ledger.CREDIT, "amount": value,
                              "analytics": {"объект": name}})
                detail.append({"объект": name, "счёт": account, "база": "собрано по объекту",
                               "доля": "100 %", "распределено": ledger.as_float(value)})
            continue
        objects = sorted(base_by_object)
        allocated = Decimal("0")
        for index, object_name in enumerate(objects):
            base_amount = base_by_object[object_name]
            share = base_amount / base_total
            piece = (total - allocated) if index == len(objects) - 1 else ledger.kopeck(total * share)
            allocated += piece
            if piece == 0:
                continue
            lines.append({"account": target, "side": ledger.DEBIT, "amount": piece,
                          "analytics": {"объект": object_name}})
            bases.append({"object": object_name, "base": "прямые затраты 20.01 за " + period,
                          "base_amount": ledger.as_float(base_amount),
                          "share": float(round(share, 6)), "account": account,
                          "amount": ledger.as_float(piece)})
            detail.append({"объект": object_name, "счёт": account,
                           "база": ledger.as_float(base_amount),
                           "доля": f"{float(share) * 100:.2f} %",
                           "распределено": ledger.as_float(piece)})
        lines.append({"account": account, "side": ledger.CREDIT, "amount": total,
                      "analytics": {}})

    result = ledger.post_entry(
        state, event="close.overheads", date=date,
        doc={"number": f"закрытие {period}: распределение 25/26", "date": date, "kind": "closing"},
        lines=lines, memo=(f"25/26 → 20.01: {ledger._rub(grand_total)} "
                           f"({', '.join(sorted(balances))})"),
        rule="close.overheads", bases=bases, chart=chart, rules=rules, mode=mode,
        key=f"close.overheads|{period}")
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    return _step("overheads", "Косвенные расходы 25/26 → 20.01", status,
                 amount=grand_total, entry=result.get("entry"),
                 why="" if status in (DONE, PROPOSED) else "не удалось провести распределение",
                 requests=result.get("gaps") or [], detail=detail)


def step_costs(state: dict, period: str, *, policy: dict, mode: str, chart: dict,
               rules: dict, date: str) -> dict:
    """20.01 → 90.02 по объектам с признанной выручкой; остальное — НЗП."""
    revenue_rows = _positive(balance_of(state, "90.01", date=date, group="объект"))
    revenue_objects = {key[0]: value for key, value in revenue_rows.items()}
    cost_rows = _positive(balance_of(state, "20.01", date=date, group="объект"))
    cost_by_object: dict[str, Decimal] = {}
    for (name,), value in cost_rows.items():
        object_name = rollup_of(policy, name)
        cost_by_object[object_name] = cost_by_object.get(object_name, Decimal("0")) + value

    if not cost_by_object:
        return _step("costs", "Затраты 20.01 → 90.02", NOT_NEEDED, amount=Decimal("0"),
                     why="на 20.01 нет сальдо")

    loose = unrolled(policy, set(cost_by_object))
    if loose:
        return _step("costs", "Затраты 20.01 → 90.02", NOT_DONE,
                     amount=ledger.kopeck(sum(cost_by_object.values())),
                     why="аналитика затрат не сведена с объектами: " + ", ".join(loose),
                     requests=[{"what": f"свёртка захваток в объект: {', '.join(loose)}",
                                "why": "выручка учитывается по объекту, затраты — по захваткам; "
                                       "без свёртки 20.01 и 90.02 не встречаются",
                                "ask": "дополнить analytics.object.rollup в учётной политике"}])

    if not revenue_objects:
        return _step("costs", "Затраты 20.01 → 90.02", NOT_NEEDED,
                     amount=Decimal("0"),
                     why=(f"нет объектов с признанной выручкой: {ledger._rub(sum(cost_by_object.values()))} "
                          f"остаётся незавершённым производством (НЗП)"),
                     detail=[{"объект": name, "НЗП": ledger.as_float(value)}
                             for name, value in sorted(cost_by_object.items())])

    closeable = {name: value for name, value in cost_by_object.items() if name in revenue_objects}
    staying = {name: value for name, value in cost_by_object.items() if name not in revenue_objects}
    if not closeable:
        return _step("costs", "Затраты 20.01 → 90.02", NOT_NEEDED, amount=Decimal("0"),
                     why=("выручка есть по другим объектам, чем затраты: "
                          + ", ".join(sorted(revenue_objects))),
                     detail=[{"объект": name, "НЗП": ledger.as_float(value)}
                             for name, value in sorted(staying.items())])

    lines: list[dict] = []
    total = Decimal("0")
    for object_name, value in sorted(closeable.items()):
        amount = ledger.kopeck(value)                   # всё сальдо 20.01 по объекту
        total += amount
        lines.append({"account": "90.02", "side": ledger.DEBIT, "amount": amount,
                      "analytics": {"объект": object_name}})
        lines.append({"account": "20.01", "side": ledger.CREDIT, "amount": amount,
                      "analytics": {"объект": object_name}})
    result = ledger.post_entry(
        state, event="close.costs", date=date,
        doc={"number": f"закрытие {period}: себестоимость сданных работ", "date": date,
             "kind": "closing"},
        lines=lines, memo=(f"20.01 → 90.02 по объектам с подписанным актом: "
                           f"{ledger._rub(total)}"
                           + (f"; в НЗП остаётся {ledger._rub(sum(staying.values()))}"
                              if staying else "")),
        rule="close.costs", chart=chart, rules=rules, mode=mode, key=f"close.costs|{period}",
        bases=[{"object": name, "basis": "сальдо 20.01 на конец периода"}
               for name in sorted(closeable)])
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    return _step("costs", "Затраты 20.01 → 90.02", status, amount=total,
                 entry=result.get("entry"),
                 why=("НЗП: " + ", ".join(f"{name} {ledger._rub(value)}"
                                         for name, value in sorted(staying.items()))
                      if staying else ""),
                 detail=[{"объект": name, "закрыто": ledger.as_float(value)}
                         for name, value in sorted(closeable.items())])


def step_vat(state: dict, period: str, *, policy: dict, mode: str, chart: dict, rules: dict,
             date: str, invoices: dict | None) -> dict:
    """19.03 → 68.02 по зарегистрированным счетам-фактурам, с проверкой суммы."""
    rows = _positive(balance_of(state, "19.03", date=date, group="контрагент"))
    balance = _total(rows)
    if balance == 0:
        return _step("vat", "НДС к вычету 19.03 → 68.02", NOT_NEEDED, amount=Decimal("0"),
                     why="на 19.03 нет сальдо")
    if not invoices:
        return _step("vat", "НДС к вычету 19.03 → 68.02", PENDING, amount=balance,
                     why=f"нет реестра счетов-фактур: {ledger._rub(balance)} остаётся на 19.03",
                     requests=[{"what": "реестр счетов-фактур за " + period,
                                "why": "зачёт НДС без счёта-фактуры невозможен, но и не мешает "
                                       "закрыть месяц: зачёт — операция налогового периода",
                                "ask": "номера и даты СФ, документ-основание, сумма НДС"}],
                     blocking=False)

    booked: dict[str, Decimal] = {}
    counterparty_of: dict[str, str] = {}
    for entry in state.get("entries") or []:
        if entry.get("event") != "receipt":
            continue
        for line in entry.get("lines") or []:
            if line["account"] != "19.03":
                continue
            number = str((entry.get("doc") or {}).get("number") or "")
            booked[number] = booked.get(number, Decimal("0")) + ledger.kopeck(line["amount"])
            counterparty_of[number] = str((line.get("analytics") or {}).get("контрагент") or "")

    usable: dict[str, Decimal] = {}
    problems: list[dict] = []
    requests: list[dict] = []
    for invoice in invoices.get("invoices") or []:
        base_doc = str(invoice.get("document") or "")
        vat = ledger.kopeck(invoice.get("vat"))
        if base_doc not in booked:
            problems.append({"what": f"СФ {invoice.get('number')}", "code": "NO_BASE_DOC",
                             "why": f"документ-основание «{base_doc}» не проведён — "
                                    f"зачитывать нечего"})
            continue
        if vat > booked[base_doc] + ledger.EPS:
            problems.append({"what": f"СФ {invoice.get('number')}", "code": "VAT_MISMATCH",
                             "why": f"НДС в СФ {ledger._rub(vat)} больше проведённого "
                                    f"{ledger._rub(booked[base_doc])} по {base_doc}"})
            requests.append({"what": f"НДС по {base_doc}",
                             "why": "сумма в счёте-фактуре расходится с проводкой",
                             "ask": "сверить накладную и счёт-фактуру"})
            continue
        counterparty = str(invoice.get("counterparty") or counterparty_of.get(base_doc) or "")
        usable[counterparty] = usable.get(counterparty, Decimal("0")) + vat
        booked[base_doc] -= vat

    uncovered = _total({key: value for key, value in booked.items() if value > ledger.EPS})
    for number, value in sorted(booked.items()):
        if value > ledger.EPS:
            requests.append({"what": f"счёт-фактура к {number}",
                             "why": f"НДС {ledger._rub(value)} проведён, но СФ не зарегистрирован",
                             "ask": f"счёт-фактура на {number}"})
    offset = _total({key: value for key, value in usable.items()})
    if offset <= 0:
        return _step("vat", "НДС к вычету 19.03 → 68.02", PENDING, amount=Decimal("0"),
                     why="ни одна СФ не подошла к проведённым документам",
                     requests=requests, detail=problems, blocking=False)

    lines = [{"account": "68.02", "side": ledger.DEBIT, "amount": offset, "analytics": {}}]
    for counterparty, value in sorted(usable.items()):
        lines.append({"account": "19.03", "side": ledger.CREDIT, "amount": value,
                      "analytics": {"контрагент": counterparty}})
    result = ledger.post_entry(
        state, event="close.vat", date=date,
        doc={"number": f"зачёт НДС за {period}", "date": date, "kind": "closing"},
        lines=lines, memo=(f"НДС к вычету по {len(usable)} контрагентам: {ledger._rub(offset)}"
                           + (f"; без СФ остаётся {ledger._rub(uncovered)}" if uncovered else "")),
        rule="close.vat", chart=chart, rules=rules, mode=mode, key=f"close.vat|{period}",
        bases=[{"invoice": row.get("number"), "document": row.get("document"),
                "vat": row.get("vat")} for row in invoices.get("invoices") or []])
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    if uncovered > ledger.EPS and status == DONE:
        status = PARTIAL          # «предложено» не размываем: это ещё не проводка
    return _step("vat", "НДС к вычету 19.03 → 68.02", status, amount=offset,
                 entry=result.get("entry"),
                 why=(f"не зачтено {ledger._rub(uncovered)}: нет счетов-фактур"
                      if uncovered > ledger.EPS else ""),
                 requests=requests, detail=problems)


def _close_subaccounts(state: dict, period: str, *, accounts: dict[str, str], target: str,
                       event: str, title: str, memo_lead: str, mode: str, chart: dict,
                       rules: dict, date: str, key_prefix: str, step_id: str) -> dict:
    """Свернуть субсчета на общий: 90.01/90.02/90.03 → 90.09 и 91.* → 91.09.

    Две вещи, которые нельзя потерять:
    * **направление** берётся из знака сальдо, а не из таблицы: кредитовое закрывается
      дебетом, дебетовое — кредитом; счёт с нетипичным сальдо не даст проводку с минусом;
    * **аналитика переезжает** вместе с суммой: 90.01 требует объект и договор, 90.02 — объект.
      Закрытие «в общую кучу» потеряло бы, по какому объекту получена выручка.
    """
    lines: list[dict] = []
    total = Decimal("0")
    detail: list[dict] = []
    for account in accounts:
        keys = tuple((ledger.account_of(chart, account) or {}).get("analytics") or ())
        rows = _positive(balance_of(state, account, date=date, group=keys or None))
        for signature, value in sorted(rows.items()):
            if abs(value) <= ledger.EPS:
                continue
            amount = ledger.kopeck(abs(value))
            analytics = dict(zip(keys, signature)) if keys else {}
            total += amount
            detail.append({"счёт": account, "сальдо": ledger.as_float(value),
                           "аналитика": analytics})
            if value < 0:
                lines.append({"account": account, "side": ledger.DEBIT, "amount": amount,
                              "analytics": analytics})
                lines.append({"account": target, "side": ledger.CREDIT, "amount": amount,
                              "analytics": {}})
            else:
                lines.append({"account": target, "side": ledger.DEBIT, "amount": amount,
                              "analytics": {}})
                lines.append({"account": account, "side": ledger.CREDIT, "amount": amount,
                              "analytics": analytics})
    if not lines:
        return _step(step_id, title, NOT_NEEDED, amount=Decimal("0"),
                     why="оборотов за период нет")
    result = ledger.post_entry(
        state, event=event, date=date,
        doc={"number": f"{memo_lead} за {period}", "date": date, "kind": "closing"},
        lines=lines, memo=f"{memo_lead}: " + ", ".join(f"{row['счёт']} {ledger._rub(row['сальдо'])}"
                                                       for row in detail),
        rule=event, chart=chart, rules=rules, mode=mode, key=f"{key_prefix}|{period}")
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    return _step(step_id, title, status, amount=total, entry=result.get("entry"), detail=detail,
                 why="" if status in (DONE, PROPOSED) else
                 "; ".join(item["why"] for item in result.get("errors") or []))


def step_sales(state: dict, period: str, *, mode: str, chart: dict, rules: dict, date: str) -> dict:
    return _close_subaccounts(state, period,
                              accounts={"90.01": "credit", "90.02": "debit", "90.03": "debit"},
                              target="90.09", event="close.sales",
                              title="Закрытие субсчетов 90 на 90.09",
                              memo_lead="закрытие продаж", mode=mode, chart=chart, rules=rules,
                              date=date, key_prefix="close.sales", step_id="sales")


def step_other(state: dict, period: str, *, mode: str, chart: dict, rules: dict, date: str) -> dict:
    return _close_subaccounts(state, period,
                              accounts={"91.01": "credit", "91.02": "debit"},
                              target="91.09", event="close.other",
                              title="Закрытие субсчетов 91 на 91.09",
                              memo_lead="закрытие прочих доходов и расходов", mode=mode,
                              chart=chart, rules=rules, date=date, key_prefix="close.other",
                              step_id="other")


def step_profit(state: dict, period: str, *, mode: str, chart: dict, rules: dict, date: str) -> dict:
    """90.09/91.09 → 99: прибыль или убыток. Убыток — отдельной стороной проводки."""
    lines: list[dict] = []
    total = Decimal("0")
    detail: list[dict] = []
    for account in ("90.09", "91.09"):
        value = _total(_positive(balance_of(state, account, date=date)))
        if value == 0:
            continue
        amount = ledger.kopeck(abs(value))
        total += amount
        detail.append({"счёт": account, "сальдо": ledger.as_float(value)})
        if value < 0:      # кредитовое сальдо — прибыль
            lines.append({"account": account, "side": ledger.DEBIT, "amount": amount,
                          "analytics": {}})
            lines.append({"account": "99", "side": ledger.CREDIT, "amount": amount,
                          "analytics": {}})
        else:              # дебетовое — убыток
            lines.append({"account": "99", "side": ledger.DEBIT, "amount": amount,
                          "analytics": {}})
            lines.append({"account": account, "side": ledger.CREDIT, "amount": amount,
                          "analytics": {}})
    if not lines:
        return _step("profit", "Финансовый результат 90.09/91.09 → 99", NOT_NEEDED,
                     amount=Decimal("0"), why="закрывать нечего: 90.09 и 91.09 пусты")
    result = ledger.post_entry(
        state, event="close.profit", date=date,
        doc={"number": f"финансовый результат за {period}", "date": date, "kind": "closing"},
        lines=lines, memo=("финансовый результат: "
                           + "; ".join(f"{row['счёт']} {ledger._rub(row['сальдо'])}"
                                       for row in detail)),
        rule="close.profit", chart=chart, rules=rules, mode=mode, key=f"close.profit|{period}")
    status = DONE if result["status"] == ledger.STATUS_OK else (
        PROPOSED if result["status"] == ledger.STATUS_PROPOSED else NOT_DONE)
    return _step("profit", "Финансовый результат 90.09/91.09 → 99", status,
                 amount=abs(total), entry=result.get("entry"), detail=detail)


def step_shortages(state: dict, period: str, *, policy: dict, date: str) -> dict:
    """94 не закрываем: это решение человека, а не арифметика."""
    rows = _positive(balance_of(state, "94", date=date, group="склад"))
    total = _total(rows)
    options = (policy.get("shortages") or {}).get("options") or []
    if total == 0:
        return _step("shortages", "Недостачи 94: решение человека", NOT_NEEDED,
                     amount=Decimal("0"), why="сальдо 94 нет")
    return _step("shortages", "Недостачи 94: решение человека", NEEDS_DECISION, amount=total,
                 why=("сальдо 94 не закрывается автоматически: программа не решает, кто виноват. "
                      "Варианты: " + ", ".join(options)),
                 requests=[{"what": f"решение по недостачам {ledger._rub(total)}",
                            "why": "94 закрывается только решением (акт, приказ, согласие МОЛ)",
                            "ask": "решение: " + " / ".join(options)}],
                 detail=[{"склад": key[0], "сумма": ledger.as_float(value)}
                         for key, value in sorted(rows.items())], blocking=False)


# ─────────────────────────── закрытие месяца ───────────────────────────
def close_month(state: dict, period: str, *, stock_state: dict | None = None,
                invoices: dict | None = None, policy: dict | None = None,
                chart: dict | None = None, rules: dict | None = None,
                mode: str | None = None, dry_run: bool = False) -> dict:
    """Провести закрытие месяца шаг за шагом. Каждый шаг отчитывается, а не молчит."""
    policy = policy or load_policy()
    chart = chart or ledger.load_chart()
    rules = rules or ledger.load_rules()
    mode = mode or str((policy.get("mode") or {}).get("value") or ledger.MODE_POST)
    result = {"period": period, "closed": False, "mode": mode, "steps": [], "problems": [],
              "warnings": [], "requests": [], "not_done": []}

    if len(period) != 7 or period[4] != "-":
        result["problems"].append({"code": "BAD_PERIOD", "what": period,
                                   "why": "период задаётся как ГГГГ-ММ"})
        return result
    if any(row.get("period") == period and row.get("closed") for row in state.get("periods") or []):
        result["warnings"].append(f"{period} уже закрыт — повторное закрытие не создаёт проводок")
        result["closed"] = True
        result["steps"] = (next(row for row in state["periods"]
                                if row["period"] == period).get("steps") or [])
        return result

    summary = ledger.verify(state, chart=chart)
    if not summary["ok"]:
        result["problems"].append({"code": "JOURNAL_BROKEN", "what": period,
                                   "why": f"журнал не сходится: {summary['summary']}"})
        return result
    report = ledger.trial_balance(state, period, chart=chart)
    if not report["balanced"]:
        result["problems"].append({"code": "UNBALANCED_TB", "what": period,
                                   "why": "ОСВ не сходится до закрытия"})
        return result
    if stock_state:
        from . import stock as stock_ledger
        negative = [row for row in stock_ledger.balances(stock_state, daval=None)
                    if row["negative"]]
        for row in negative:
            result["problems"].append({"code": "NEGATIVE_STOCK",
                                       "what": f"{row['item']} / {row['warehouse']}",
                                       "why": f"отрицательный остаток {row['qty']}"})
        if negative:
            return result

    date = _period_end(period)
    # шаги получают журнал аргументом: так сухой прогон идёт по копии, но цепочкой —
    # иначе распределение косвенных не влияло бы на следующую цифру, и это был бы обман
    steps = [
        ("overheads", lambda st: step_overheads(st, period, policy=policy, mode=mode,
                                                chart=chart, rules=rules, date=date)),
        ("costs", lambda st: step_costs(st, period, policy=policy, mode=mode, chart=chart,
                                        rules=rules, date=date)),
        ("vat", lambda st: step_vat(st, period, policy=policy, mode=mode, chart=chart,
                                    rules=rules, date=date, invoices=invoices)),
        ("sales", lambda st: step_sales(st, period, mode=mode, chart=chart, rules=rules,
                                        date=date)),
        ("other", lambda st: step_other(st, period, mode=mode, chart=chart, rules=rules,
                                        date=date)),
        ("profit", lambda st: step_profit(st, period, mode=mode, chart=chart, rules=rules,
                                          date=date)),
        ("shortages", lambda st: step_shortages(st, period, policy=policy, date=date)),
    ]
    if dry_run:
        work = json.loads(json.dumps(state))      # копия журнала: настоящий не трогаем
        for _, run in steps:
            result["steps"].append(run(work))
        result["dry_run"] = True
        result["requests"] = [item for step in result["steps"]
                              for item in step.get("requests") or []]
        result["result"] = _result_block(work, period)
        result["turnover"] = ledger.trial_balance(work, period, chart=chart)["totals"]
        return result

    for _, run in steps:
        step = run(state)
        result["steps"].append(step)
        result["requests"].extend(step.get("requests") or [])
        if step["status"] == NOT_DONE:
            result["not_done"].append({"step": step["id"], "why": step["why"]})
        if step.get("blocking") and step["status"] == NOT_DONE:
            result["problems"].append({"code": "STEP_FAILED", "what": step["title"],
                                       "why": step["why"] or "шаг не выполнен"})

    after = ledger.trial_balance(state, period, chart=chart)
    if not after["balanced"]:
        result["problems"].append({"code": "UNBALANCED_AFTER", "what": period,
                                   "why": "ОСВ не сходится после закрытия — период не закрыт"})
        return result

    if mode == ledger.MODE_PROPOSE:
        result["closed"] = False
        result["warnings"].append("режим «предлагать»: проводки закрытия ждут решения "
                                  "(ledger proposals / ledger accept), период не закрыт")
        result["turnover"] = after["totals"]
        return result

    if result["problems"]:
        return result

    result["closed"] = True
    result["turnover"] = after["totals"]
    result["entries"] = after["entries"]
    result["result"] = _result_block(state, period)
    state.setdefault("periods", []).append({
        "period": period, "closed": True, "seq": len(state.get("entries") or []),
        "entries": after["entries"], "debit": after["totals"]["debit"],
        "credit": after["totals"]["credit"], "mode": mode,
        "steps": [{"id": step["id"], "title": step["title"], "status": step["status"],
                   "amount": step["amount"]} for step in result["steps"]],
        "not_done": [row["step"] for row in result["not_done"]],
    })
    return result


def _result_block(state: dict, period: str) -> dict:
    """Что получилось на 99: выручка, себестоимость, НДС, результат."""
    rows = {row["code"]: row for row in ledger.trial_balance(state, period)["rows"]}
    row = rows.get("99") or {}
    profit = float((row.get("closing_credit") or 0.0) - (row.get("closing_debit") or 0.0))
    revenue = _result_component(state, "90.01", period, "credit")
    vat_out = _result_component(state, "90.03", period, "debit")
    return {"profit": profit,
            "revenue": revenue,
            "revenue_net": float(round(revenue - vat_out, 2)),
            "cost": _result_component(state, "90.02", period, "debit"),
            "vat_out": vat_out,
            "vat_in": _result_component(state, "68.02", period, "debit")}


def _result_component(state: dict, account: str, period: str, side: str) -> float:
    """Оборот счёта за месяц: выручка и себестоимость считаются за период, а не накопительно."""
    total = Decimal("0")
    for entry in state.get("entries") or []:
        if str(entry.get("period") or "") != period:
            continue
        for line in entry.get("lines") or []:
            if line["account"] == account and line["side"] == (ledger.DEBIT if side == "debit"
                                                               else ledger.CREDIT):
                total += ledger.kopeck(line["amount"])
    return ledger.as_float(total)


def policy_markdown(policy: dict) -> str:
    """Человеческая запись учётной политики: что выбрано и чего в ней нет."""
    lines = [f"# Учётная политика: {policy.get('organization') or 'организация'}", "",
             f"Действует с {policy.get('effective_from')}", "",
             "| Что | Выбор |", "|---|---|"]
    lines.append(f"| Режим проводок | {(policy.get('mode') or {}).get('value')} |")
    lines.append(f"| Свёртка аналитики | "
                 f"{len((policy.get('analytics') or {}).get('object', {}).get('rollup') or {})} "
                 f"объектов |")
    lines.append(f"| База косвенных расходов | {(policy.get('overheads') or {}).get('basis')} |")
    lines.append(f"| Закрытие затрат | {(policy.get('cost_close') or {}).get('mode')} |")
    lines.append(f"| Зачёт НДС | {(policy.get('vat') or {}).get('offset')} |")
    lines.append(f"| Недостачи | {(policy.get('shortages') or {}).get('mode')} |")
    lines += ["", "## Чего в политике нет", ""]
    for row in policy.get("not_implemented") or []:
        lines.append(f"- {row}")
    return "\n".join(lines) + "\n"
