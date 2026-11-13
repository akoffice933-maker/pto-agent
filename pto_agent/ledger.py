# -*- coding: utf-8 -*-
"""Ядро учёта: план счетов, правила проводок, неизменяемый журнал, ОСВ.

Зачем этот слой
---------------
Склад (`stock.py`) отвечает на вопрос «сколько лежит и по какой цене». Учёт отвечает на
другой: «что это стоило, кому мы должны и сколько заработали». Между ними — проводка.
Без проводки склад остаётся инвентаризацией, а КС-2 — бумагой: суммы в них не сходятся
ни с чем, и месяц нельзя закрыть иначе как вручную.

Четыре сущности
---------------
*документ* → *правило* → *проводка* → *отчёт*.

Правило — запись в `data/reference/posting_rules.json`, а не код: у него есть дата
действия, ссылка на норму (94н) и список того, что оно требует от первички. Поэтому
смена учётной политики — это новый файл, а не переписанный модуль.

Восемь правил, на которых держится журнал
-----------------------------------------
1. **Двойная запись проверяется на входе.** Сумма Дт ≠ сумма Кт — проводка не создаётся.
   Не «предупреждение», не «поправим в отчёте»: отказ с точной разницей.
2. **Журнал неизменяем.** Записи только добавляются, у каждой — хеш предыдущей. Правка
   задним числом ломает цепочку и видна в `verify`. Ошибка исправляется сторно.
3. **Аналитика обязательна.** Счёт 20.01 без объекта, 60.01 без поставщика — отказ с
   запросом «что прислать». Проводка «в никуда» не создаётся.
4. **Закрытый период закрыт.** После закрытия месяца проводок в нём нет — только сторно
   в открытом периоде, со ссылкой на закрытый.
5. **НДС — отдельная строка.** Ставка берётся из документа. Нет ставки — нет проводки
   на 19, а не «20 % по умолчанию»: выдуманный НДС хуже отсутствующего.
6. **Копейка не растворяется.** Каждая строка округляется до копейки; расхождение
   с документом выводится отдельной строкой «округление», а не прячется в сумме.
7. **Повтор не удваивает.** Повторная загрузка того же документа возвращает «уже
   проведено» — ключ записи строится из документа, правила и строки.
8. **Забалансовые счета не ломают баланс.** 003 ведётся отдельно: односторонняя запись,
   количества и «стоимость по данным заказчика», в итог Дт=Кт не подмешивается.

Чего этот круг сознательно не делает
------------------------------------
Закрытие 20.01 → 90.02, распределение 25/26, зачёт НДС, налог на прибыль. Это следующий
круг: сначала должен сойтись журнал фактов, потом — закрытие. Обещание «закрываем месяц
сами» без этих операций было бы неправдой, поэтому в `close_period` они перечислены как
невыполненные шаги, а не спрятаны.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

SCHEMA = "pto-agent/ledger@1"
CHART_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "chart_of_accounts_94n.json"
RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "posting_rules.json"

#: копейка — предел точности учёта; всё, что мельче, обязано быть показано строкой «округление»
EPS = Decimal("0.01")

STATUS_OK = "проведено"
STATUS_DUPLICATE = "уже проведено"
STATUS_REJECTED = "отклонено"

DEBIT = "Дт"
CREDIT = "Кт"


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
    """Округление до копейки — единственное место, где деньги теряют точность."""
    number = dec(value)
    if number is None:
        return Decimal("0.00")
    return number.quantize(EPS, rounding=ROUND_HALF_UP)


def as_float(value: Decimal) -> float:
    return float(kopeck(value))


def _rub(value) -> str:
    number = kopeck(value)
    text = f"{number:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} ₽"


# ─────────────────────────── справочники ───────────────────────────
def load_chart(path: str | Path | None = None) -> dict:
    path = Path(path) if path else CHART_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "pto-agent/chart@1":
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} не pto-agent/chart@1")
    return payload


def load_rules(path: str | Path | None = None) -> dict:
    path = Path(path) if path else RULES_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "pto-agent/rules@1":
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} не pto-agent/rules@1")
    return payload


def accounts_index(chart: dict) -> dict[str, dict]:
    return {row["code"]: row for row in chart.get("accounts") or []}


def account_of(chart: dict, code: str) -> dict | None:
    return accounts_index(chart).get(code)


def rule_for(rules: dict, event: str, date: str = "") -> dict | None:
    """Правило на дату: из нескольких редакций берём последнюю с датой действия ≤ даты."""
    found = None
    for row in rules.get("rules") or []:
        if row.get("event") != event:
            continue
        if date and str(row.get("date_from") or "") > date:
            continue
        if found is None or str(row.get("date_from") or "") >= str(found.get("date_from") or ""):
            found = row
    return found


def check_chart(chart: dict) -> list[dict]:
    """План счетов сам с собой: ссылки на родителей, типы, забалансовые."""
    problems: list[dict] = []
    index = accounts_index(chart)
    for code, row in index.items():
        if str(row.get("type")) not in {"А", "П", "АП"}:
            problems.append({"code": "BAD_TYPE", "what": code,
                             "why": f"тип {row.get('type')!r} не из А/П/АП"})
        parent = row.get("parent")
        if parent and parent not in index:
            problems.append({"code": "NO_PARENT", "what": code,
                             "why": f"родитель {parent} не найден в плане счетов"})
        if parent and index[parent].get("off_balance") != row.get("off_balance"):
            problems.append({"code": "OFFBALANCE_MISMATCH", "what": code,
                             "why": "признак забалансового счёта расходится с родителем"})
    return problems


# ─────────────────────────── состояние ───────────────────────────
def new_state(*, object_name: str = "", organization: str = "", contract: str = "") -> dict:
    return {"schema": SCHEMA, "object": object_name, "organization": organization,
            "contract": contract, "entries": [], "periods": [], "rejections": [],
            "keys": []}


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


def period_of(date: str) -> str:
    return str(date or "")[:7]


def is_closed(state: dict, date: str) -> str | None:
    """Возвращает период, если дата попадает в закрытый месяц, иначе None."""
    period = period_of(date)
    for row in state.get("periods") or []:
        if row.get("period") == period and row.get("closed"):
            return period
    return None


# ─────────────────────────── запись в журнал ───────────────────────────
def _digest(prev: str, payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256((prev + raw).encode("utf-8")).hexdigest()


def _entry_id(state: dict) -> str:
    return f"П-{len(state.get('entries') or []) + 1:06d}"


def _analytics_ok(chart: dict, line: dict) -> list[str]:
    """Что из обязательной аналитики не заполнено. Пустой список — проводка допустима."""
    info = account_of(chart, line["account"])
    if not info:
        return []
    given = {key: value for key, value in (line.get("analytics") or {}).items() if str(value).strip()}
    missing = [name for name in info.get("analytics") or [] if name not in given]
    return missing


def post_entry(state: dict, *, event: str, date: str, doc: dict, lines: list[dict],
               kind: str = "balance", memo: str = "", rule: str = "", key: str = "",
               bases: list[dict] | None = None, chart: dict | None = None,
               rules: dict | None = None) -> dict:
    """Провести одну операцию. Всё, что не проходит проверку, — отказ, а не «проведём как есть»."""
    chart = chart or load_chart()
    rules = rules if rules is not None else load_rules()
    date = str(date or "")
    number = str(doc.get("number") or "")
    result = {"status": STATUS_REJECTED, "entry": None, "errors": [], "gaps": [], "warnings": []}

    closed = is_closed(state, date)
    if closed:
        result["errors"].append({"code": "CLOSED_PERIOD", "what": number or event,
                                 "why": f"период {closed} закрыт: проведение только сторно "
                                        f"в открытом периоде"})
        return result

    clean: list[dict] = []
    for line in lines:
        account = str(line.get("account") or "")
        info = account_of(chart, account)
        if not info:
            result["errors"].append({"code": "NO_ACCOUNT", "what": account,
                                     "why": "счёта нет в рабочем плане счетов"})
            continue
        side = line.get("side")
        if side not in (DEBIT, CREDIT):
            result["errors"].append({"code": "BAD_SIDE", "what": account,
                                     "why": f"сторона {side!r} не Дт и не Кт"})
            continue
        amount = kopeck(line.get("amount"))
        qty = dec(line.get("qty"))
        if amount < 0:
            result["errors"].append({"code": "NEGATIVE_AMOUNT", "what": account,
                                     "why": "отрицательная сумма: для исправления есть сторно"})
            continue
        if kind == "balance" and amount == 0 and qty is None:
            continue  # нулевая строка не создаёт проводку, но и не мешает
        missing = _analytics_ok(chart, {**line, "account": account})
        if missing:
            result["errors"].append({"code": "ANALYTICS_MISSING", "what": f"{account}",
                                     "why": f"счёт {account} требует аналитику: "
                                            f"{', '.join(missing)} — без неё проводка "
                                            f"не привязана ни к объекту, ни к контрагенту"})
            result["gaps"].append({"what": f"аналитика {', '.join(missing)} для {number or event}",
                                   "why": "счёт не закрывается проводкой без аналитики",
                                   "ask": f"указать в первичке: {', '.join(missing)}"})
            continue
        clean.append({"account": account, "side": side, "amount": as_float(amount),
                      "qty": float(qty) if qty is not None else None,
                      "unit": line.get("unit") or "",
                      "analytics": {k: v for k, v in (line.get("analytics") or {}).items()
                                    if str(v).strip()}})

    if result["errors"]:
        return result

    debits = sum((kopeck(line["amount"]) for line in clean if line["side"] == DEBIT), Decimal("0"))
    credits = sum((kopeck(line["amount"]) for line in clean if line["side"] == CREDIT), Decimal("0"))
    if kind == "balance":
        if debits != credits:
            result["errors"].append({"code": "UNBALANCED", "what": number or event,
                                     "why": f"Дт {_rub(debits)} ≠ Кт {_rub(credits)}: "
                                            f"разница {_rub(debits - credits)} — операция "
                                            f"не сохраняется"})
            return result
        if not clean:
            result["errors"].append({"code": "EMPTY", "what": number or event,
                                     "why": "нет ни одной проводки: операция не создана"})
            return result
    else:
        if not clean:
            result["errors"].append({"code": "EMPTY", "what": number or event,
                                     "why": "нет ни одной записи"})
            return result
        for line in clean:
            if not account_of(chart, line["account"]).get("off_balance"):
                result["errors"].append({"code": "NOT_OFFBALANCE", "what": line["account"],
                                         "why": "запись помечена забалансовой, а счёт — нет"})
        if result["errors"]:
            return result

    entry_key = key or f"{rule}|{date}|{number}|{event}"
    if entry_key in (state.get("keys") or []):
        result["status"] = STATUS_DUPLICATE
        result["warnings"].append(f"{number or event}: повтор — запись с тем же ключом "
                                  f"уже есть, дубль не создан")
        return result

    prev = str((state.get("entries") or [{}])[-1].get("hash") or "") if state.get("entries") else ""
    payload = {"id": _entry_id(state), "date": date, "period": period_of(date),
               "event": event, "rule": rule, "kind": kind,
               "doc": {"number": number, "date": str(doc.get("date") or date),
                       "kind": doc.get("kind") or ""},
               "memo": memo, "lines": clean, "bases": list(bases or []),
               "key": entry_key, "prev_hash": prev}
    entry = dict(payload)
    entry["hash"] = _digest(prev, payload)
    state.setdefault("entries", []).append(entry)
    state.setdefault("keys", []).append(entry_key)
    result.update({"status": STATUS_OK, "entry": entry})
    return result


# ─────────────────────────── суммы и НДС ───────────────────────────
def split_vat(gross: Decimal, rate_percent, included: bool) -> tuple[Decimal, Decimal, str]:
    """Разделить сумму и НДС. Ставка приходит из документа; нет ставки — нет и НДС."""
    gross = kopeck(gross)
    rate = dec(rate_percent)
    if rate is None or rate <= 0:
        return gross, Decimal("0.00"), "ставка НДС в документе не указана — НДС не выделялся"
    factor = rate / Decimal("100")
    if included:
        net = kopeck(gross / (Decimal("1") + factor))
        vat = gross - net
        return net, vat, f"НДС {rate.normalize():f} % в том числе"
    vat = kopeck(gross * factor)
    net = gross
    return net, vat, f"НДС {rate.normalize():f} % сверху"


def _vat_of(doc: dict) -> tuple[object, bool] | tuple[None, bool]:
    raw = doc.get("vat")
    if isinstance(raw, dict):
        rate = raw.get("rate")
        included = bool(raw.get("included", True))
        return rate, included
    if isinstance(raw, (int, float)):
        return raw, True
    return None, True


def _requisites(doc: dict) -> dict:
    return {
        "контрагент": str(doc.get("counterparty") or doc.get("supplier") or doc.get("customer") or ""),
        "договор": str(doc.get("contract") or ""),
    }


# ─────────────────────────── проводки по складу ───────────────────────────
#: вид складского документа → событие в справочнике правил
STOCK_EVENTS = {
    "receipt": "receipt",
    "issue": "issue",
    "limit": "limit",
    "transfer": "transfer",
    "writeoff": "writeoff",
    "return": "return",
    "daval_receipt": "daval_receipt",
    "daval_return": "daval_return",
}


def _rule_line(rule: dict, amount_kind: str, *, debit: bool) -> dict | None:
    for line in rule.get("lines") or []:
        side = line.get("debit") if debit else line.get("credit")
        if side and line.get("amount") == amount_kind:
            return line
    return None


def _build_balance_entry(rule: dict, *, amounts: dict, debit_analytics: dict,
                         credit_analytics: dict,
                         by_account: dict[str, dict] | None = None) -> tuple[list[dict], str]:
    """Собрать строки проводки по правилу: какие суммы правило просит, те и берём.

    `by_account` — аналитика, которая зависит от счёта, а не от документа: НДС на 19.03
    требует контрагента, тогда как материал на 10.01 — склад и номенклатуру. Один и тот
    же документ даёт строки с разной аналитикой, и это нормально.
    """
    lines: list[dict] = []
    notes: list[str] = []
    credits: OrderedDict[tuple, float] = OrderedDict()
    by_account = by_account or {}
    for line in rule.get("lines") or []:
        kind_amount = str(line.get("amount") or "")
        amount = kopeck(amounts.get(kind_amount))
        if amount == 0:
            if kind_amount == "vat" and amounts.get("gross"):
                notes.append("НДС не начислен")
            continue
        debit_account = line.get("debit")
        credit_account = line.get("credit")
        if debit_account:
            lines.append({"account": debit_account, "side": DEBIT, "amount": amount,
                          "analytics": {**debit_analytics,
                                        **by_account.get(debit_account, {}),
                                        **(line.get("debit_analytics_fixed") or {})}})
        if credit_account:
            analytics = {**credit_analytics, **by_account.get(credit_account, {}),
                         **(line.get("credit_analytics_fixed") or {})}
            key = (credit_account, json.dumps(analytics, ensure_ascii=False, sort_keys=True))
            credits[key] = float(kopeck(Decimal(str(credits.get(key, 0))) + amount))
    for (account, analytics_raw), amount in credits.items():
        lines.append({"account": account, "side": CREDIT, "amount": amount,
                      "analytics": json.loads(analytics_raw)})
    return lines, "; ".join(notes)


def post_stock(state: dict, stock_state: dict, *, documents: dict | None = None,
               chart: dict | None = None, rules: dict | None = None) -> dict:
    """Проводки из складских движений: приход, требование, перемещение, списание, 003.

    Суммы берутся из движений (а не из документа): при списании это себестоимость
    конкретных партий по FIFO, при приходе — цена партии. Одинаковых источников
    у склада и у учёта быть не должно — иначе они разойдутся молча.
    """
    chart = chart or load_chart()
    rules = rules or load_rules()
    documents = documents or {}
    meta = {}
    for doc in (documents.get("documents") if isinstance(documents, dict) else documents) or []:
        if isinstance(doc, dict) and doc.get("number"):
            meta[str(doc.get("number"))] = doc

    state.setdefault("object", state.get("object") or stock_state.get("object") or "")
    warehouse_titles = {row.get("code"): row.get("title") or row.get("code")
                        for row in stock_state.get("warehouses") or []}
    results: list[dict] = []

    # 1. входящие остатки: сальдо начальное — тоже движения, значит и проводка
    opening_moves = [m for m in stock_state.get("moves") or []
                     if str(m.get("doc_id")) == "сальдо начальное"]
    if opening_moves:
        rule = rule_for(rules, "opening", "")
        grouped: OrderedDict[tuple, dict] = OrderedDict()
        for move in opening_moves:
            key = (move.get("warehouse"), move.get("item"))
            row = grouped.setdefault(key, {"qty": Decimal("0"), "price": dec(move.get("price")) or
                                           Decimal("0"), "unit": move.get("unit")})
            row["qty"] += dec(move.get("qty")) or Decimal("0")
        lines = []
        bases = []
        total = Decimal("0")
        for (warehouse, item), row in grouped.items():
            amount = kopeck(row["qty"] * row["price"])
            if amount == 0:
                continue
            total += amount
            lines.append({"account": rule["lines"][0]["debit"], "side": DEBIT, "amount": amount,
                          "analytics": {"склад": warehouse, "номенклатура": item}})
            bases.append({"item": item, "warehouse": warehouse, "qty": float(row["qty"]),
                          "unit": row["unit"], "price": float(row["price"]),
                          "amount": float(amount), "basis": "сальдо начальное"})
        if lines:
            lines.append({"account": rule["lines"][0]["credit"], "side": CREDIT,
                          "amount": float(total), "analytics": {}})
            date = str(opening_moves[0].get("date") or "")
            result = post_entry(state, event="opening", date=date,
                                doc={"number": "сальдо начальное", "date": date,
                                     "kind": "opening"},
                                lines=lines, rule=str(rule.get("id")), bases=bases,
                                memo=(f"входящие остатки склада на {date}: "
                                      f"{len(bases)} позиций, {_rub(total)}"),
                                key=f"opening|{date}", chart=chart, rules=rules)
            results.append(result)

    # 2. документы склада
    for doc_row in stock_state.get("documents") or []:
        number = str(doc_row.get("number") or "")
        kind = str(doc_row.get("kind") or "")
        date = str(doc_row.get("date") or "")
        moves = [m for m in stock_state.get("moves") or [] if str(m.get("doc_id")) == number]
        if kind == "transfer":
            # приём на складе-получателе — внутренняя запись склада, отдельной проводки не даёт
            moves = [m for m in moves if not m.get("internal")]
        doc_meta = meta.get(number) or {}
        event = STOCK_EVENTS.get(kind)
        if kind == "inventory":
            # инвентаризация — не своё событие, а два: излишек и недостача
            _inventory_entries(state, stock_state, doc_row, chart, rules, results)
            continue
        if not event:
            continue
        rule = rule_for(rules, event, date)
        if not rule:
            results.append({"status": STATUS_REJECTED, "entry": None,
                            "errors": [{"code": "NO_RULE", "what": number,
                                        "why": f"нет правила для события {event}"}],
                            "gaps": [], "warnings": []})
            continue
        requisites = _requisites(doc_meta)
        rate, included = _vat_of(doc_meta)
        object_of = {str(line.get("item") or ""): str(line.get("object") or "")
                     for line in doc_meta.get("lines") or []}

        grouped: OrderedDict[tuple, dict] = OrderedDict()
        for move in moves:
            key = (move.get("item"), move.get("warehouse"))
            row = grouped.setdefault(key, {"qty": Decimal("0"), "amount": Decimal("0"),
                                           "unit": move.get("unit"), "batches": []})
            qty = dec(move.get("qty")) or Decimal("0")
            price = dec(move.get("price")) or Decimal("0")
            amount = kopeck(abs(qty) * price)
            row["qty"] += abs(qty)
            row["amount"] += amount
            row["batches"].append({"batch": move.get("batch"), "qty": float(abs(qty)),
                                   "price": float(price), "amount": float(amount),
                                   "warehouse": move.get("warehouse")})

        for (item, warehouse), row in grouped.items():
            if row["amount"] == 0 and not kind.startswith("daval_"):
                results.append({"status": STATUS_REJECTED, "entry": None,
                                "errors": [{"code": "NO_PRICE", "what": f"{number} / {item}",
                                            "why": "в движениях нет цены — себестоимость "
                                                   "не определена, проводка не создаётся"}],
                                "gaps": [{"what": f"цена {item} по {number}",
                                          "why": "движение с нулевой ценой",
                                          "ask": "цена в накладной или оценка по ИНВ-3"}],
                                "warnings": []})
                continue
            if kind.startswith("daval_"):
                result = _post_daval(state, rule=rule, doc_row=doc_row, date=date,
                                     item=item, warehouse=warehouse, row=row,
                                     requisites=requisites, chart=chart, rules=rules,
                                     side=CREDIT if kind == "daval_return" else DEBIT)
                results.append(result)
                continue

            debit_analytics: dict = {"склад": warehouse, "номенклатура": item}
            credit_analytics: dict = {"склад": warehouse, "номенклатура": item}
            by_account: dict[str, dict] = {}
            pre_gaps: list[dict] = []
            amounts = {"gross": row["amount"], "net": row["amount"], "vat": Decimal("0")}
            memo = (f"{item} · {row['qty'].normalize():f} {row['unit']} · "
                    f"{len(row['batches'])} парт. · {_rub(row['amount'])}")

            if event == "receipt":
                if not requisites["контрагент"]:
                    results.append({"status": STATUS_REJECTED, "entry": None,
                                    "errors": [{"code": "NO_COUNTERPARTY",
                                                "what": f"{number} / {item}",
                                                "why": "приход без поставщика: счёт 60.01 "
                                                       "не к кому отнести"}],
                                    "gaps": [{"what": f"поставщик по {number}",
                                              "why": "в накладной нет контрагента",
                                              "ask": "поставщик и ИНН из УПД"}],
                                    "warnings": []})
                    continue
                net, vat, vat_memo = split_vat(row["amount"], rate, included)
                amounts = {"gross": row["amount"], "net": net, "vat": vat}
                credit_analytics = {"контрагент": requisites["контрагент"]}
                by_account = {"19.03": {"контрагент": requisites["контрагент"]}}
                memo += f" · {vat_memo}"
                if vat == 0:
                    pre_gaps.append({"what": f"ставка НДС по {number}",
                                     "why": "в документе ставка не указана — проводка на 19.03 "
                                            "не сформирована, чтобы не выдумывать налог",
                                     "ask": "ставка НДС (в том числе или сверху)"})
            elif event in ("issue", "limit"):
                debit_analytics = {"объект": object_of.get(item) or ""}
                if not debit_analytics["объект"]:
                    results.append({"status": STATUS_REJECTED, "entry": None,
                                    "errors": [{"code": "NO_OBJECT", "what": f"{number} / {item}",
                                                "why": "списание без объекта: 20.01 некуда "
                                                       "отнести"}],
                                    "gaps": [{"what": f"объект списания {item} по {number}",
                                              "why": "в требовании не указан объект",
                                              "ask": "объект или захватка"}],
                                    "warnings": []})
                    continue
                credit_analytics = {"склад": warehouse, "номенклатура": item}
            elif event == "transfer":
                target = str(doc_row.get("warehouse_to") or "")
                debit_analytics = {"склад": target, "номенклатура": item}
                memo = (f"{item} · {row['qty'].normalize():f} {row['unit']}: "
                        f"{warehouse} → {target} · {_rub(row['amount'])}")
            elif event == "writeoff":
                debit_analytics = {"склад": warehouse}
                memo = (f"{item} · {row['qty'].normalize():f} {row['unit']} · причина: "
                        f"{_reason_of(doc_meta) or 'не указана'} · {_rub(row['amount'])}")
            elif event == "return":
                debit_analytics = {"контрагент": requisites["контрагент"]}
                credit_analytics = {"склад": warehouse, "номенклатура": item}

            lines, rule_note = _build_balance_entry(rule, amounts=amounts,
                                                    debit_analytics=debit_analytics,
                                                    credit_analytics=credit_analytics,
                                                    by_account=by_account)
            if rule_note:
                memo += f" · {rule_note}"
            result = post_entry(state, event=event, date=date,
                                doc={"number": number, "date": date, "kind": kind},
                                lines=lines, rule=str(rule.get("id")), memo=memo,
                                bases=row["batches"],
                                key=f"{rule.get('id')}|{date}|{number}|{item}|{warehouse}",
                                chart=chart, rules=rules)
            result["document"] = {"number": number, "kind": kind, "date": date}
            if pre_gaps:
                result["gaps"] = (result.get("gaps") or []) + pre_gaps
            results.append(result)

    return _summarise(results, state)


def _reason_of(doc_meta: dict) -> str:
    for line in doc_meta.get("lines") or []:
        if line.get("note"):
            return str(line.get("note"))
    return str(doc_meta.get("reason") or "")


def _post_daval(state: dict, *, rule: dict, doc_row: dict, date: str, item: str,
                warehouse: str, row: dict, requisites: dict, chart: dict, rules: dict,
                side: str) -> dict:
    """Забалансовая запись по 003: односторонняя, количественная, без «встречного» счёта."""
    account = (rule["lines"][0].get("debit") or rule["lines"][0].get("credit") or "003")
    credit_side = side == CREDIT
    if not requisites["контрагент"]:
        return {"status": STATUS_REJECTED, "entry": None,
                "errors": [{"code": "NO_COUNTERPARTY", "what": f"{doc_row.get('number')} / {item}",
                            "why": "давальческий материал без заказчика: 003 не к кому отнести"}],
                "gaps": [{"what": "заказчик-давалец",
                          "why": "в акте нет контрагента",
                          "ask": "заказчик и его накладная"}],
                "warnings": []}
    moves = [m for m in state.get("entries") or [] if m.get("event") == "daval_receipt"]
    qty = float(row["qty"])
    warnings: list[str] = []
    gaps: list[dict] = []
    if credit_side:
        have = _daval_balance(state, item=item, counterparty=requisites["контрагент"])
        if qty > have + 1e-9:
            return {"status": STATUS_REJECTED, "entry": None,
                    "errors": [{"code": "DAVAL_OVER_RETURN",
                                "what": f"{doc_row.get('number')} / {item}",
                                "why": f"возврат {qty} при принятом {have}: больше, чем "
                                       f"принято от заказчика, вернуть нельзя"}],
                    "gaps": [], "warnings": []}
    if not row["amount"]:
        gaps.append({"what": f"стоимость давальческого {item}",
                     "why": "в акте указано только количество — учёт по 003 количественный",
                     "ask": "стоимость по данным заказчика"})
    line = {"account": account, "side": side, "amount": row["amount"] or 0.0,
            "qty": qty, "unit": row["unit"],
            "analytics": {"контрагент": requisites["контрагент"], "номенклатура": item}}
    memo = (f"{item} · {row['qty'].normalize():f} {row['unit']} · 003 (забаланс), "
            f"{'возврат заказчику' if credit_side else 'принято от заказчика'}")
    result = post_entry(state, event="daval_return" if credit_side else "daval_receipt",
                        date=date, doc={"number": doc_row.get("number"), "date": date,
                                        "kind": doc_row.get("kind")},
                        lines=[line], kind="off_balance", memo=memo,
                        rule=str(rule.get("id")),
                        key=f"{rule.get('id')}|{date}|{doc_row.get('number')}|{item}",
                        chart=chart, rules=rules)
    result["gaps"] = (result.get("gaps") or []) + gaps
    result["warnings"] = (result.get("warnings") or []) + warnings
    return result


def _daval_balance(state: dict, *, item: str, counterparty: str) -> float:
    total = 0.0
    for entry in state.get("entries") or []:
        if entry.get("kind") != "off_balance":
            continue
        for line in entry.get("lines") or []:
            if line.get("account") != "003" or line.get("analytics", {}).get("номенклатура") != item:
                continue
            if line.get("analytics", {}).get("контрагент") != counterparty:
                continue
            qty = line.get("qty") or 0
            total += qty if line.get("side") == DEBIT else -qty
    return total


def _inventory_entries(state: dict, stock_state: dict, doc_row: dict, chart: dict,
                       rules: dict, results: list) -> list:
    """ИНВ-3: излишек и недостача — два разных правила, а не «одна корректировка»."""
    number = str(doc_row.get("number") or "")
    date = str(doc_row.get("date") or "")
    moves = [m for m in stock_state.get("moves") or [] if str(m.get("doc_id")) == number]
    grouped: OrderedDict[tuple, dict] = OrderedDict()
    for move in moves:
        kind = str(move.get("kind") or "")
        if kind not in ("receipt", "writeoff"):
            continue
        event = "inventory_surplus" if kind == "receipt" else "inventory_shortage"
        key = (event, move.get("item"), move.get("warehouse"))
        row = grouped.setdefault(key, {"qty": Decimal("0"), "amount": Decimal("0"),
                                       "unit": move.get("unit"), "batches": []})
        qty = abs(dec(move.get("qty")) or Decimal("0"))
        price = dec(move.get("price")) or Decimal("0")
        row["qty"] += qty
        row["amount"] += kopeck(qty * price)
        row["batches"].append({"batch": move.get("batch"), "qty": float(qty),
                               "price": float(price), "amount": float(kopeck(qty * price))})
    for (event, item, warehouse), row in grouped.items():
        rule = rule_for(rules, event, date)
        if not rule:
            results.append({"status": STATUS_REJECTED, "entry": None,
                            "errors": [{"code": "NO_RULE", "what": number,
                                        "why": f"нет правила для события {event}"}],
                            "gaps": [], "warnings": []})
            continue
        if row["amount"] == 0:
            results.append({"status": STATUS_REJECTED, "entry": None,
                            "errors": [{"code": "NO_PRICE", "what": f"{number} / {item}",
                                        "why": "в описи нет цены — излишек/недостача "
                                               "не оценены, проводка не создаётся"}],
                            "gaps": [{"what": f"цена {item} по ИНВ-3",
                                      "why": "оценка отсутствует",
                                      "ask": "цена по ведомости ИНВ-3"}],
                            "warnings": []})
            continue
        amounts = {"gross": row["amount"], "net": row["amount"], "vat": Decimal("0")}
        lines, _ = _build_balance_entry(rule, amounts=amounts,
                                        debit_analytics=({"склад": warehouse,
                                                          "номенклатура": item}
                                                         if event == "inventory_surplus"
                                                         else {"склад": warehouse}),
                                        credit_analytics={"склад": warehouse,
                                                          "номенклатура": item})
        result = post_entry(state, event=event, date=date,
                            doc={"number": number, "date": date, "kind": "inventory"},
                            lines=lines, rule=str(rule.get("id")), bases=row["batches"],
                            memo=(f"{'излишек' if event == 'inventory_surplus' else 'недостача'} "
                                  f"{item} · {row['qty'].normalize():f} {row['unit']} · "
                                  f"{_rub(row['amount'])}"),
                            key=f"{rule.get('id')}|{date}|{number}|{item}|{warehouse}",
                            chart=chart, rules=rules)
        results.append(result)
    return results


def _summarise(results: list[dict], state: dict) -> dict:
    def _flat(field):
        return [item for result in results for item in (result.get(field) or [])]

    return {"posted": sum(1 for r in results if r.get("status") == STATUS_OK),
            "duplicates": sum(1 for r in results if r.get("status") == STATUS_DUPLICATE),
            "rejected": sum(1 for r in results if r.get("status") == STATUS_REJECTED),
            "entries": len(state.get("entries") or []),
            "results": results,
            "errors": _flat("errors"), "warnings": _flat("warnings"), "gaps": _flat("gaps")}


# ─────────────────────────── проводки по актам ───────────────────────────
SIGNED = {"подписан", "подписана", "подписано заказчиком"}


def post_acts(state: dict, acts: dict | list, *, contract: str = "", customer: str = "",
              chart: dict | None = None, rules: dict | None = None) -> dict:
    """Выручка по КС-2. Признаётся только подписанный заказчиком акт — не «проверенный»."""
    chart = chart or load_chart()
    rules = rules or load_rules()
    rows = acts.get("acts") if isinstance(acts, dict) else acts
    contract = contract or (acts.get("contract") if isinstance(acts, dict) else "") or ""
    results: list[dict] = []
    for act in rows or []:
        number = str(act.get("act_id") or "")
        date = str(act.get("act_date") or "")
        status = str(act.get("status") or "").strip().lower()
        if status not in SIGNED and not act.get("signed"):
            results.append({"status": "не признано", "entry": None,
                            "document": {"number": number, "kind": "ks2", "date": date},
                            "errors": [],
                            "warnings": [f"{number}: статус «{act.get('status') or 'нет'}» — "
                                         f"это внутренняя проверка, а не подпись заказчика; "
                                         f"выручка не признана"],
                            "gaps": [{"what": f"подпись заказчика на {number}",
                                      "why": "акт не подписан",
                                      "ask": "подписанный заказчиком экземпляр КС-2"}]})
            continue
        rule = rule_for(rules, "act_signed", date)
        amount = dec(act.get("amount_rub"))
        if not rule or amount is None:
            results.append({"status": STATUS_REJECTED, "entry": None,
                            "errors": [{"code": "NO_DATA", "what": number,
                                        "why": "нет правила или суммы акта"}],
                            "gaps": [], "warnings": []})
            continue
        counterparty = str(act.get("customer") or customer or "")
        contract_no = str(act.get("contract") or contract or "")
        if not counterparty or not contract_no:
            results.append({"status": STATUS_REJECTED, "entry": None,
                            "errors": [{"code": "ANALYTICS_MISSING", "what": number,
                                        "why": "для 62.01 нужны заказчик и договор — "
                                               f"заказчик {'есть' if counterparty else 'нет'}, "
                                               f"договор {'есть' if contract_no else 'нет'}"}],
                            "gaps": [{"what": f"заказчик/договор для {number}",
                                      "why": "выручку не на что опереть",
                                      "ask": "наименование заказчика и номер договора"}],
                            "warnings": []})
            continue
        rate, included = _vat_of(act)
        net, vat, vat_memo = split_vat(amount, rate, included)
        amounts = {"gross": amount, "net": net, "vat": vat}
        debit_analytics = {"контрагент": counterparty, "договор": contract_no}
        credit_analytics = {"объект": str(act.get("object") or state.get("object") or ""),
                            "договор": contract_no}
        lines, _ = _build_balance_entry(rule, amounts=amounts, debit_analytics=debit_analytics,
                                        credit_analytics=credit_analytics)
        result = post_entry(state, event="act_signed", date=date,
                            doc={"number": number, "date": date, "kind": "ks2"},
                            lines=lines, rule=str(rule.get("id")),
                            memo=f"{number} · {_rub(net)} без НДС · {vat_memo}",
                            key=f"act|{date}|{number}", chart=chart, rules=rules)
        if vat == 0 and result.get("status") == STATUS_OK:
            result.setdefault("gaps", []).append(
                {"what": f"ставка НДС по {number}",
                 "why": "НДС с реализации не начислен: ставка в акте не указана",
                 "ask": "ставка НДС по договору"})
        results.append(result)
    return _summarise(results, state)


# ─────────────────────────── сторно ───────────────────────────
def storno(state: dict, entry_id: str, *, date: str, reason: str,
           chart: dict | None = None, rules: dict | None = None) -> dict:
    """Исправление — обратной записью. Прямая правка журнала невозможна по устройству."""
    chart = chart or load_chart()
    source = next((e for e in state.get("entries") or [] if e.get("id") == entry_id), None)
    if not source:
        return {"status": STATUS_REJECTED, "entry": None,
                "errors": [{"code": "NO_ENTRY", "what": entry_id, "why": "записи нет в журнале"}],
                "gaps": [], "warnings": []}
    if not str(reason).strip():
        return {"status": STATUS_REJECTED, "entry": None,
                "errors": [{"code": "NO_REASON", "what": entry_id,
                            "why": "сторно без причины нельзя: причина — часть записи"}],
                "gaps": [], "warnings": []}
    if f"storno|{entry_id}" in (state.get("keys") or []):
        return {"status": STATUS_DUPLICATE, "entry": None, "errors": [], "gaps": [],
                "warnings": [f"{entry_id} уже сторнирована — второй раз не сторнируем"]}
    flip = {DEBIT: CREDIT, CREDIT: DEBIT}
    lines = [{"account": line["account"], "side": flip[line["side"]],
              "amount": line["amount"], "qty": line.get("qty"),
              "unit": line.get("unit") or "", "analytics": line.get("analytics") or {}}
             for line in source.get("lines") or []]
    result = post_entry(state, event=f"storno.{source.get('event')}", date=date,
                        doc=source.get("doc") or {}, lines=lines,
                        kind=source.get("kind") or "balance",
                        memo=f"сторно {entry_id}: {reason}", rule=f"storno.{source.get('rule')}",
                        key=f"storno|{entry_id}", chart=chart, rules=rules)
    if result["status"] == STATUS_OK:
        result["entry"]["storno_of"] = entry_id
        result["entry"]["reason"] = reason
        # пересчитываем хеш: storno_of и reason — часть записи, цепочка не должна врать
        payload = {k: v for k, v in result["entry"].items() if k != "hash"}
        result["entry"]["hash"] = _digest(payload["prev_hash"], payload)
    return result


# ─────────────────────────── ОСВ и регистры ───────────────────────────
def _entry_in_period(entry: dict, period: str) -> bool:
    return str(entry.get("period") or "") == period


def trial_balance(state: dict, period: str, *, chart: dict | None = None,
                  with_zero: bool = False) -> dict:
    """Оборотно-сальдовая ведомость: сальдо на начало, обороты, сальдо на конец.

    Считается из журнала, а не хранится: хранимый остаток счёта рано или поздно
    расходится с проводками, и тогда непонятно, чему верить.
    """
    chart = chart or load_chart()
    index = accounts_index(chart)
    rows: dict[str, dict] = {}

    def _row(code: str) -> dict:
        if code not in rows:
            info = index.get(code) or {}
            rows[code] = {"code": code, "title": info.get("title") or "счёт вне плана",
                          "type": info.get("type") or "", "off_balance": bool(info.get("off_balance")),
                          "opening": Decimal("0"), "debit": Decimal("0"),
                          "credit": Decimal("0"), "closing": Decimal("0"),
                          "qty_opening": Decimal("0"), "qty_debit": Decimal("0"),
                          "qty_credit": Decimal("0"), "qty_closing": Decimal("0")}
        return rows[code]

    for entry in state.get("entries") or []:
        before = str(entry.get("period") or "") < period
        inside = _entry_in_period(entry, period)
        if not before and not inside:
            continue
        for line in entry.get("lines") or []:
            row = _row(line["account"])
            amount = kopeck(line.get("amount"))
            qty = dec(line.get("qty")) or Decimal("0")
            sign = 1 if line["side"] == DEBIT else -1
            if before:
                row["opening"] += amount * sign
                row["qty_opening"] += qty * sign
            else:
                if line["side"] == DEBIT:
                    row["debit"] += amount
                    row["qty_debit"] += qty
                else:
                    row["credit"] += amount
                    row["qty_credit"] += qty
                row["closing"] += amount * sign
                row["qty_closing"] += qty * sign

    for row in rows.values():
        row["closing"] = row["opening"] + row["debit"] - row["credit"]
        row["qty_closing"] = row["qty_opening"] + row["qty_debit"] - row["qty_credit"]
        row["opening_debit"], row["opening_credit"] = _split_saldo(row["opening"], row["type"])
        row["closing_debit"], row["closing_credit"] = _split_saldo(row["closing"], row["type"])

    ordered = [rows[code] for code in sorted(rows)
               if with_zero or any(abs(rows[code][field]) > Decimal("0.001") for field in
                                   ("opening", "debit", "credit",
                                    "qty_opening", "qty_debit", "qty_credit"))]
    balance_rows = [row for row in ordered if not row["off_balance"]]
    off_rows = [row for row in ordered if row["off_balance"]]
    totals = {
        "opening_debit": sum((row["opening_debit"] for row in balance_rows), Decimal("0")),
        "opening_credit": sum((row["opening_credit"] for row in balance_rows), Decimal("0")),
        "debit": sum((row["debit"] for row in balance_rows), Decimal("0")),
        "credit": sum((row["credit"] for row in balance_rows), Decimal("0")),
        "closing_debit": sum((row["closing_debit"] for row in balance_rows), Decimal("0")),
        "closing_credit": sum((row["closing_credit"] for row in balance_rows), Decimal("0")),
    }
    totals = {key: as_float(value) for key, value in totals.items()}
    balanced = (totals["debit"] == totals["credit"]
                and totals["opening_debit"] == totals["opening_credit"]
                and totals["closing_debit"] == totals["closing_credit"])
    return {"period": period, "rows": [_jsonable(row) for row in balance_rows],
            "off_balance": [_jsonable(row) for row in off_rows],
            "totals": totals, "balanced": balanced,
            "entries": sum(1 for e in state.get("entries") or [] if _entry_in_period(e, period))}


def _split_saldo(value: Decimal, account_type: str) -> tuple[Decimal, Decimal]:
    """Сальдо в дебетовое/кредитовое по типу счёта; для АП — по знаку."""
    if account_type == "П":
        return Decimal("0"), -value
    if account_type == "А":
        return value, Decimal("0")
    return (value, Decimal("0")) if value >= 0 else (Decimal("0"), -value)


def _jsonable(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        out[key] = as_float(value) if isinstance(value, Decimal) else value
    return out


def register(state: dict, account: str, period: str, *, chart: dict | None = None) -> dict:
    """Обороты по счёту с аналитикой: «сколько израсходовано на корпус 2» — здесь."""
    chart = chart or load_chart()
    info = account_of(chart, account) or {}
    keys = list(info.get("analytics") or [])
    rows: OrderedDict[tuple, dict] = OrderedDict()
    for entry in state.get("entries") or []:
        if str(entry.get("period") or "") != period:
            continue
        for line in entry.get("lines") or []:
            if line["account"] != account:
                continue
            signature = tuple(str((line.get("analytics") or {}).get(key, "")) for key in keys)
            row = rows.setdefault(signature, {"analytics": dict(zip(keys, signature)),
                                              "debit": 0.0, "credit": 0.0, "entries": []})
            row[line["side"] == DEBIT and "debit" or "credit"] += line.get("amount") or 0.0
            row["entries"].append(entry.get("id"))
    total_debit = as_float(sum((Decimal(str(row["debit"])) for row in rows.values()),
                               Decimal("0")))
    total_credit = as_float(sum((Decimal(str(row["credit"])) for row in rows.values()),
                                Decimal("0")))
    return {"account": account, "title": info.get("title") or "", "period": period,
            "rows": list(rows.values()), "debit": total_debit, "credit": total_credit}


# ─────────────────────────── закрытие периода ───────────────────────────
#: что должно произойти при закрытии месяца и чего этот круг ещё не делает
CLOSING_STEPS = [
    {"step": "20.01 → 90.02", "done": False,
     "why": "закрытие затрат месяца в себестоимость — следующий круг"},
    {"step": "зачёт НДС 19.03 → 68.02", "done": False,
     "why": "зачёт — операция налогового периода, требует книги покупок"},
    {"step": "90 → 99", "done": False, "why": "финансовый результат — после закрытия затрат"},
]


def close_period(state: dict, period: str, *, stock_state: dict | None = None,
                 chart: dict | None = None) -> dict:
    """Закрыть месяц явно: после этого в него не проводят, только сторно в открытом."""
    chart = chart or load_chart()
    result = {"period": period, "closed": False, "problems": [], "warnings": [],
              "steps": CLOSING_STEPS}
    if len(period) != 7 or period[4] != "-":
        result["problems"].append({"code": "BAD_PERIOD", "what": period,
                                   "why": "период задаётся как ГГГГ-ММ"})
        return result
    for row in state.get("periods") or []:
        if row.get("period") == period and row.get("closed"):
            result["warnings"].append(f"{period} уже закрыт")
            result["closed"] = True
            return result

    summary = verify(state, chart=chart)
    if not summary["ok"]:
        result["problems"].extend(summary["problems"])
        return result

    report = trial_balance(state, period, chart=chart)
    if not report["balanced"]:
        result["problems"].append({"code": "UNBALANCED_TB", "what": period,
                                   "why": f"ОСВ не сходится: Дт {report['totals']['debit']} ≠ "
                                          f"Кт {report['totals']['credit']}"})
        return result

    if stock_state:
        negative = [row for row in _stock_balances(stock_state)
                    if row["negative"]]
        for row in negative:
            result["problems"].append({"code": "NEGATIVE_STOCK",
                                       "what": f"{row['item']} / {row['warehouse']}",
                                       "why": f"отрицательный остаток {row['qty']}: "
                                              f"месяц с минусом не закрывается"})
        if negative:
            return result

    result["closed"] = True
    result["turnover"] = report["totals"]
    result["entries"] = report["entries"]
    result["not_done"] = [step for step in CLOSING_STEPS if not step["done"]]
    state.setdefault("periods", []).append({
        "period": period, "closed": True, "seq": len(state.get("entries") or []),
        "entries": report["entries"], "debit": report["totals"]["debit"],
        "credit": report["totals"]["credit"],
        "not_done": [step["step"] for step in result["not_done"]],
    })
    return result


def _stock_balances(stock_state: dict) -> list[dict]:
    from pto_agent import stock as stock_ledger
    return stock_ledger.balances(stock_state, daval=None)


# ─────────────────────────── контроль ───────────────────────────
def verify(state: dict, *, chart: dict | None = None) -> dict:
    """Сходится ли журнал сам с собой: хеш-цепочка, Дт=Кт, аналитика, закрытые периоды."""
    chart = chart or load_chart()
    problems: list[dict] = []
    prev = ""
    debits = Decimal("0")
    credits = Decimal("0")
    off_qty = Decimal("0")
    for index, entry in enumerate(state.get("entries") or [], start=1):
        if entry.get("prev_hash") != prev:
            problems.append({"code": "CHAIN", "what": entry.get("id"),
                             "why": f"разрыв цепочки: prev_hash {str(entry.get('prev_hash'))[:8]} "
                                    f"≠ {prev[:8]}"})
        payload = {k: v for k, v in entry.items() if k != "hash"}
        calc = _digest(prev, payload)
        if entry.get("hash") != calc:
            problems.append({"code": "HASH", "what": entry.get("id"),
                             "why": "хеш записи не совпадает с содержимым — журнал правили"})
        prev = str(entry.get("hash") or "")
        entry_debit = sum((kopeck(line["amount"]) for line in entry.get("lines") or []
                           if line["side"] == DEBIT), Decimal("0"))
        entry_credit = sum((kopeck(line["amount"]) for line in entry.get("lines") or []
                            if line["side"] == CREDIT), Decimal("0"))
        if entry.get("kind") == "off_balance":
            for line in entry.get("lines") or []:
                if not account_of(chart, line["account"]):
                    problems.append({"code": "NO_ACCOUNT", "what": line["account"],
                                     "why": f"в записи {entry.get('id')} счёт вне плана"})
                missing = _analytics_ok(chart, line)
                if missing:
                    problems.append({"code": "ANALYTICS_MISSING",
                                     "what": f"{entry.get('id')} / {line['account']}",
                                     "why": f"нет аналитики: {', '.join(missing)}"})
                qty = dec(line.get("qty")) or Decimal("0")
                off_qty += qty if line["side"] == DEBIT else -qty
            continue
        if entry_debit != entry_credit:
            problems.append({"code": "UNBALANCED", "what": entry.get("id"),
                             "why": f"Дт {entry_debit} ≠ Кт {entry_credit}"})
        debits += entry_debit
        credits += entry_credit
        for line in entry.get("lines") or []:
            if not account_of(chart, line["account"]):
                problems.append({"code": "NO_ACCOUNT", "what": line["account"],
                                 "why": f"в записи {entry.get('id')} счёт вне плана"})
            missing = _analytics_ok(chart, line)
            if missing:
                problems.append({"code": "ANALYTICS_MISSING",
                                 "what": f"{entry.get('id')} / {line['account']}",
                                 "why": f"нет аналитики: {', '.join(missing)}"})
        closed = is_closed(state, str(entry.get("date")))
        if closed and not str(entry.get("event", "")).startswith("storno."):
            # закрытие фиксирует, сколько записей в периоде было на момент закрытия:
            # то, что появилось позже, — уже нарушение
            seq = next((row.get("seq", 0) for row in state.get("periods") or []
                        if row.get("period") == closed), 0)
            if index > int(seq or 0):
                problems.append({"code": "IN_CLOSED_PERIOD", "what": entry.get("id"),
                                 "why": f"запись от {entry.get('date')} появилась после "
                                        f"закрытия периода {closed}"})
    if debits != credits:
        problems.append({"code": "TOTAL_MISMATCH", "what": "журнал целиком",
                         "why": f"сумма Дт {_rub(debits)} ≠ сумма Кт {_rub(credits)}"})
    ok = not problems
    return {"ok": ok, "entries": len(state.get("entries") or []),
            "debits": as_float(debits), "credits": as_float(credits),
            "off_balance_qty": float(off_qty),
            "problems": problems,
            "summary": ("Журнал сходится: Дт = Кт, цепочка целая"
                        if ok else f"расхождений: {len(problems)}")}


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(state: dict, period: str, *, chart: dict | None = None) -> str:
    report = trial_balance(state, period, chart=chart)
    lines = [f"# Оборотно-сальдовая ведомость за {period}", "",
             f"Объект: {state.get('object') or 'не задан'} · "
             f"записей за месяц: {report['entries']}", "",
             "| Счёт | Наименование | Сальдо на начало | Оборот Дт | Оборот Кт | Сальдо на конец |",
             "|---|---|---:|---:|---:|---:|"]
    for row in report["rows"]:
        opening = _saldo_text(row, "opening")
        closing = _saldo_text(row, "closing")
        lines.append(f"| {row['code']} | {row['title']} | {opening} | "
                     f"{_rub(row['debit'])} | {_rub(row['credit'])} | {closing} |")
    totals = report["totals"]
    lines.append(f"| | **Итого** | {_rub(totals['opening_debit'])} / "
                 f"{_rub(totals['opening_credit'])} | **{_rub(totals['debit'])}** | "
                 f"**{_rub(totals['credit'])}** | {_rub(totals['closing_debit'])} / "
                 f"{_rub(totals['closing_credit'])} |")
    if report["off_balance"]:
        lines += ["", "## Забалансовые счета", "",
                  "| Счёт | Наименование | Приход | Расход | Остаток |", "|---|---|---:|---:|---:|"]
        for row in report["off_balance"]:
            lines.append(f"| {row['code']} | {row['title']} | {row['qty_debit']:g} | "
                         f"{row['qty_credit']:g} | {row['qty_closing']:g} |")
    lines += ["", f"Сходится: {'да' if report['balanced'] else 'НЕТ'}"]
    return "\n".join(lines) + "\n"


def _saldo_text(row: dict, prefix: str) -> str:
    debit = row[f"{prefix}_debit"]
    credit = row[f"{prefix}_credit"]
    parts = []
    if debit:
        parts.append(f"Дт {_rub(debit)}")
    if credit:
        parts.append(f"Кт {_rub(credit)}")
    return " · ".join(parts) or "—"


def to_xlsx(report: dict, path: str | Path) -> Path:
    """ОСВ в Excel: та же ведомость, что в markdown, без «подкрасим и поправим»."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = f"ОСВ {report['period']}"
    headers = ["Счёт", "Наименование", "Сальдо на начало Дт", "Сальдо на начало Кт",
               "Оборот Дт", "Оборот Кт", "Сальдо на конец Дт", "Сальдо на конец Кт"]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in report["rows"]:
        sheet.append([row["code"], row["title"], row["opening_debit"] or None,
                      row["opening_credit"] or None, row["debit"] or None,
                      row["credit"] or None, row["closing_debit"] or None,
                      row["closing_credit"] or None])
    totals = report["totals"]
    sheet.append(["", "Итого", totals["opening_debit"], totals["opening_credit"],
                  totals["debit"], totals["credit"], totals["closing_debit"],
                  totals["closing_credit"]])
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True)
    for column, width in zip("ABCDEFGH", (10, 42, 18, 18, 16, 16, 18, 18)):
        sheet.column_dimensions[column].width = width
    for row in sheet.iter_rows(min_row=2, min_col=3, max_col=8):
        for cell in row:
            cell.number_format = "# ##0.00"
    book.save(path)
    return path


def entries_markdown(state: dict, *, period: str = "", account: str = "", limit: int = 200) -> str:
    rows = []
    for entry in state.get("entries") or []:
        if period and entry.get("period") != period:
            continue
        lines = entry.get("lines") or []
        if account and not any(line["account"].startswith(account) for line in lines):
            continue
        rows.append(entry)
    out = [f"# Журнал проводок{' за ' + period if period else ''}", "",
           f"записей: {len(rows)}", "",
           "| № | Дата | Событие | Документ | Дт | Кт | Сумма | Основание |",
           "|---|---|---|---|---|---|---:|---|"]
    for entry in rows[-limit:]:
        debit = [f"{line['account']} {_rub(line['amount'])}" for line in entry.get("lines") or []
                 if line["side"] == DEBIT]
        credit = [f"{line['account']} {_rub(line['amount'])}" for line in entry.get("lines") or []
                  if line["side"] == CREDIT]
        total = sum((kopeck(line["amount"]) for line in entry.get("lines") or []
                     if line["side"] == DEBIT), Decimal("0"))
        out.append(f"| {entry.get('id')} | {entry.get('date')} | {entry.get('event')} | "
                   f"{entry.get('doc', {}).get('number')} | {'; '.join(debit) or '—'} | "
                   f"{'; '.join(credit) or '—'} | {_rub(total)} | {entry.get('memo')} |")
    return "\n".join(out) + "\n"


# ─────────────────────────── сверка учёта со складом ───────────────────────────
def balance_as_of(state: dict, account: str, date: str, *, group_by: tuple[str, ...] = ()
                  ) -> dict[tuple, Decimal]:
    """Сальдо счёта на дату (включительно), при желании — с разбивкой по аналитике."""
    total: dict[tuple, Decimal] = {}
    for entry in state.get("entries") or []:
        if str(entry.get("date") or "") > date:
            continue
        for line in entry.get("lines") or []:
            if line["account"] != account:
                continue
            signature = tuple(str((line.get("analytics") or {}).get(key, "")) for key in group_by)
            sign = 1 if line["side"] == DEBIT else -1
            total[signature] = total.get(signature, Decimal("0")) + kopeck(line["amount"]) * sign
    return total


def reconcile_stock(state: dict, stock_state: dict, *, as_of: str | None = None) -> dict:
    """Склад в штуках и деньгах против сальдо 10.01 и 003.

    Это главный инвариант этапа: если склад и учёт считают разное, то одно из двух врёт,
    и месяц закрывать нельзя. Поэтому сверка идёт не «в сумме», а по складу и номенклатуре.
    """
    from pto_agent import stock as stock_ledger

    if not as_of:
        dates = [str(m.get("date") or "") for m in stock_state.get("moves") or []]
        as_of = max(dates) if dates else ""
    ledger_own = balance_as_of(state, "10.01", as_of, group_by=("склад", "номенклатура"))
    rows: list[dict] = []
    problems: list[dict] = []
    stock_total = Decimal("0")
    for row in stock_ledger.balances(stock_state, as_of=as_of, daval=False):
        key = (str(row["warehouse"]), str(row["item"]))
        ledger_amount = ledger_own.pop(key, Decimal("0"))
        stock_amount = kopeck(row["amount"] or 0)
        stock_total += stock_amount
        diff = ledger_amount - stock_amount
        rows.append({"warehouse": key[0], "item": key[1], "qty": row["qty"],
                     "stock": as_float(stock_amount), "ledger": as_float(ledger_amount),
                     "diff": as_float(diff)})
        if abs(diff) > EPS:
            problems.append({"code": "STOCK_10_MISMATCH", "what": f"{key[1]} / {key[0]}",
                             "why": f"склад {_rub(stock_amount)}, сальдо 10.01 "
                                    f"{_rub(ledger_amount)} — расхождение {_rub(diff)}"})
    for (warehouse, item), amount in ledger_own.items():
        if abs(amount) > EPS:
            problems.append({"code": "STOCK_10_ORPHAN", "what": f"{item} / {warehouse}",
                             "why": f"в учёте {_rub(amount)}, а на складе позиции уже нет"})
            rows.append({"warehouse": warehouse, "item": item, "qty": 0.0, "stock": 0.0,
                         "ledger": as_float(amount), "diff": as_float(amount)})

    ledger_total = kopeck(sum(balance_as_of(state, "10.01", as_of).values(), Decimal("0")))
    return {"as_of": as_of, "rows": rows, "stock_total": as_float(stock_total),
            "ledger_total": as_float(ledger_total),
            "diff": as_float(ledger_total - stock_total),
            "problems": problems, "ok": not problems}
