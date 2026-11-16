# -*- coding: utf-8 -*-
"""Счета-фактуры и книга продаж: нумерация, реестр, сверка с журналом.

Зачем модуль. До круга 33 в контуре не было ни реестра счетов-фактур, ни нумерации, ни
книги продаж: бланк `forms.build_advance_invoice` печатался с пустым номером, а суммы
декларации считались прямо из оборотов. Это работало, но оставляло дырку — **вычет по
счёту-фактуре нельзя было подтвердить документом**, а декларация не сверялась с книгой.

Что здесь есть:

* `next_number` — следующий номер по правилам организации (`invoice_rules.json`):
  сквозная нумерация с 1 января, номер не переиспользуется;
* `register` — регистрация **выставленного** счёта-фактуры: дата основания, ставка,
  контрагент, срок 5 календарных дней (п. 3 ст. 168 НК РФ), НДС из базы и ставки;
* `auto_issue` — собрать выставленные СФ из журнала: отгрузка по актам, аванс по 62.02;
* `sales_book` — книга продаж (приложение № 5 к постановлению № 1137) и сверка с
  начислением в журнале (`90.03` + НДС с авансов на `76.АВ`);
* `register_purchase` / `auto_register_purchases` — регистрация **полученных** СФ. Номер и
  дата здесь принадлежат поставщику: контур их не присваивает и не придумывает;
* `purchase_book` — книга покупок (приложение № 4) и сверка с вычетом: 19.03 → 68.02;
* `register_correction` — корректировочный счёт-фактура (приложение № 2) к выставленному
  или полученному документу: увеличение или уменьшение, с основанием;
* `intermediary_journal` — журнал учёта полученных и выставленных СФ (приложение № 3):
  только посреднические документы, которые в книги не попадают;
* `to_fns_xml` — выгрузка книги или журнала в структуре формата ФНС (имена элементов и
  версии — из приказа ФНС от 08.06.2021 № ЕД-7-26/547@), с явной пометкой, что со схемой
  файл не сверялся.

Чего нет и почему — в `invoice_rules.json → not_implemented`.
"""
from __future__ import annotations

import json
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import ledger, tax

SCHEMA = "pto-agent/invoices@3"
DATA = Path(__file__).resolve().parents[1] / "data"
RULES_PATH = DATA / "reference" / "invoice_rules.json"
KINDS = {"shipped": "отгрузка (акт подписан)", "advance": "аванс полученный",
         "correction": "корректировочный"}

STATUS_OK = "выставлен"
STATUS_DUPLICATE = "уже выставлен"
STATUS_REJECTED = "отклонён"


def dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def num(value) -> float:
    return float((dec(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def rub(value) -> str:
    return f"{dec(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def load_rules(path: str | Path | None = None) -> dict:
    payload = json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))
    if str(payload.get("schema") or "") not in ("pto-agent/invoice-rules@1",
                                                "pto-agent/invoice-rules@2"):
        raise ValueError(f"правила счетов-фактур: схема {payload.get('schema')!r} не та")
    return payload


OPERATION_CODES_PATH = DATA / "reference" / "vat_operation_codes.json"


def load_operation_codes(path: str | Path | None = None) -> dict:
    """Справочник кодов видов операций (приказ ФНС от 14.03.2016 № ММВ-7-3/136@).

    Коды в контуре не назначаются «на глаз»: справочник переписан из приказа целиком, а
    правило вывода кода для конкретного документа лежит рядом с кодом (`rules`), чтобы
    расхождение можно было проверить, а не поверить.
    """
    payload = json.loads(Path(path or OPERATION_CODES_PATH).read_text(encoding="utf-8"))
    if payload.get("schema") != "pto-agent/vat-operation-codes@1":
        raise ValueError(f"коды видов операций: схема {payload.get('schema')!r} не та")
    return payload


def operation_code_for(kind: str, *, direction: str = "", agency: bool = False,
                       agent: bool = False, codes: dict | None = None) -> dict:
    """Код вида операции для документа: код, правило и источник.

    `kind`: `shipped` (отгрузка), `advance` (аванс), `purchase` (приобретение),
    `correction` (корректировочный). Возвращает словарь с кодом, коротким названием,
    правилом и источником — либо `code=None` и причину, если правила нет. Выдумывать код,
    которого нет в приказе, контур не будет.
    """
    codes = codes or load_operation_codes()
    by_code = {row["code"]: row for row in codes.get("codes") or []}

    def answer(code: str, why: str) -> dict:
        row = by_code.get(code) or {}
        return {"code": code, "short": row.get("short") or "", "why": why,
                "source": codes.get("source"), "resolved": True}

    if agent:
        return answer("06", "операция налогового агента по ст. 161 НК РФ")
    if kind == "correction":
        if direction == "decrease":
            return answer("18", "корректировочный СФ на уменьшение стоимости")
        return answer("01", "увеличение стоимости идёт по коду 01: отдельного кода в приказе нет")
    if kind == "shipped":
        if agency:
            return answer("01", "отгрузка по агентскому/комиссионному договору — входит в код 01")
        return answer("01", "отгрузка товаров (работ, услуг) покупателю")
    if kind == "advance":
        return answer("02", "счёт-фактура на полученный аванс")
    if kind == "purchase":
        if agency:
            return answer("01", "приобретение по агентскому/комиссионному договору")
        return answer("01", "приобретение товаров (работ, услуг) у поставщика")
    return {"code": None, "why": f"для вида {kind!r} правила вывода кода нет",
            "resolved": False}


# ─────────────────────────── состояние ───────────────────────────
def new_state(*, object_name: str = "", organization: dict | None = None) -> dict:
    return {"schema": SCHEMA, "object": object_name,
            "organization": organization or {}, "issued": [], "received": [],
            "corrections": [], "keys": []}


def load(path: str | Path) -> dict:
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    state.setdefault("issued", [])
    state.setdefault("received", [])
    state.setdefault("corrections", [])
    state.setdefault("keys", [])
    return state


def save(state: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ─────────────────────────── нумерация ───────────────────────────
def next_number(state: dict, date: str, *, kind: str = "shipped",
                rules: dict | None = None) -> str:
    """Следующий номер счёта-фактуры на дату.

    Нумерация сквозная в хронологическом порядке выставления и с 1 января начинается
    заново (Правила заполнения СФ, п. 3 раздела II прил. 1 к постановлению № 1137).
    Номер **не переиспользуется**: если счёт-фактура аннулирована, её номер остаётся
    занятым — иначе в книге продаж окажутся два разных документа с одним номером.
    """
    rules = rules or load_rules()
    policy = rules.get("numbering") or {}
    year = str(date or "")[:4]
    prefix = str((policy.get("prefixes") or {}).get(kind) or "")
    pad = int(policy.get("pad") or 0)
    highest = 0
    for row in state.get("issued") or []:
        row_date = str(row.get("date") or "")
        if policy.get("reset") == "год" and row_date[:4] != year:
            continue
        if str(row.get("kind") or "") != kind:
            continue
        try:
            highest = max(highest, int(str(row.get("number") or "0").lstrip("Кк")))
        except ValueError:
            continue
    number = highest + 1
    if policy.get("reset") == "год":
        number = max(number, int(policy.get("start") or 1))
    text = str(number).rjust(pad, "0") if pad else str(number)
    return f"{prefix}{text}"


def _days_between(start: str, end: str) -> int | None:
    try:
        return (_date.fromisoformat(end) - _date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


# ─────────────────────────── регистрация ───────────────────────────
def register(state: dict, *, kind: str, date: str, base_date: str, base_number: str,
             counterparty: str, contract: str = "", net=None, vat=None, rate=None,
             number: str | None = None, object_name: str = "", source: str = "",
             agency: bool = False, agent: bool = False, operation_code: str | None = None,
             rules: dict | None = None) -> dict:
    """Зарегистрировать выставленный счёт-фактуру.

    Суммы не выдумываются: НДС считается из базы и ставки, если не передан явно. Ставки нет —
    документа нет (`INVOICE_NO_RATE`): «22 % по умолчанию» в счёте-фактуре — это подлог.
    """
    rules = rules or load_rules()
    result = {"status": STATUS_REJECTED, "invoice": None, "errors": [], "gaps": [],
              "warnings": []}
    if kind not in KINDS:
        result["errors"].append({"code": "INVOICE_KIND", "what": kind,
                                 "why": f"вид счёта-фактуры {kind!r} неизвестен: "
                                        f"{', '.join(KINDS)}"})
        return result
    date = str(date or "")
    base_date = str(base_date or "")
    if not date or not base_date:
        result["errors"].append({"code": "INVOICE_NO_DATE", "what": base_number,
                                 "why": "нужны дата счёта-фактуры и дата основания: "
                                        "без них невозможно проверить срок 5 дней"})
        return result
    if date < base_date:
        result["errors"].append({"code": "INVOICE_DATE_BEFORE_BASE", "what": base_number,
                                 "why": f"дата счёта-фактуры {date} раньше даты основания "
                                        f"{base_date}: документ не может появиться до события"})
        return result
    if not str(counterparty or "").strip():
        result["gaps"].append({"what": f"покупатель по счёту-фактуре {base_number}",
                              "why": "без покупателя документ не идентифицирует сделку",
                              "ask": "контрагент из договора/платёжки"})
        return result
    if rate is None or str(rate).strip() == "":
        result["gaps"].append({"what": f"ставка НДС в счёте-фактуре {base_number}",
                              "why": "ставки нет: НДС не считаем, документ не регистрируем",
                              "ask": "ставка из договора или счёта-фактуры поставщика"})
        return result
    rate_value = dec(rate)
    if rate_value < 0:
        result["errors"].append({"code": "INVOICE_BAD_RATE", "what": base_number,
                                 "why": f"ставка {rate} отрицательная"})
        return result
    if rate_value == 0 and dec(vat) not in (Decimal("0"), None):
        result["errors"].append({"code": "INVOICE_RATE_VAT", "what": base_number,
                                 "why": f"ставка 0 %, а НДС {rub(dec(vat))}: одно из двух неверно"})
        return result
    net_value = dec(net)
    if net_value <= 0:
        result["errors"].append({"code": "INVOICE_NO_BASE", "what": base_number,
                                 "why": "база счёта-фактуры не положительна"})
        return result
    vat_value = dec(vat) if vat is not None else \
        (net_value * rate_value / Decimal("100")).quantize(Decimal("0.01"),
                                                           rounding=ROUND_HALF_UP)
    gross = net_value + vat_value

    key = f"invoice|{kind}|{base_number}|{base_date}"
    if key in (state.get("keys") or []):
        result["status"] = STATUS_DUPLICATE
        existing = next((row for row in state.get("issued") or [] if row.get("key") == key),
                        None)
        result["warnings"].append(
            f"{base_number}: счёт-фактура уже выставлен" +
            (f" — № {existing.get('number')} от {existing.get('date')}" if existing else ""))
        result["invoice"] = existing
        return result

    deadline_days = int((rules.get("issue") or {}).get("deadline_days") or 5)
    lag = _days_between(base_date, date)
    late = lag is not None and lag > deadline_days
    if late:
        result["warnings"].append(
            f"{base_number}: счёт-фактура выставлен через {lag} дн. после основания "
            f"(срок — {deadline_days} календарных дней, п. 3 ст. 168 НК РФ): "
            f"это нарушение срока, а не «почти вовремя»")

    if rate_value == 0:
        result["warnings"].append(
            f"{base_number}: счёт-фактура с нулевой ставкой — нужна норма, по которой "
            f"операция не облагается (ст. 149 или ст. 164 НК РФ), иначе это не нулевая "
            f"ставка, а забытый налог")

    number_value = number or next_number(state, date, kind=kind, rules=rules)
    if any(str(row.get("number")) == str(number_value)
           and str(row.get("date") or "")[:4] == date[:4]
           and str(row.get("kind")) == kind for row in state.get("issued") or []):
        result["errors"].append({"code": "INVOICE_NUMBER_TAKEN", "what": number_value,
                                 "why": "номер уже занят в этом году: нумерация не "
                                        "переиспользуется даже для аннулированного документа"})
        return result

    # код вида операции: у документа может быть свой (его ввёл человек), иначе — по правилу
    # из справочника; правило и источник сохраняем рядом с кодом, чтобы это можно было проверить
    derived = operation_code_for("advance" if kind == "advance" else "shipped", agency=agency,
                                agent=agent)
    if operation_code:
        derived = dict(derived, code=str(operation_code), why="код указан человеком",
                       source="введён при регистрации документа", resolved=True)

    invoice = {"number": str(number_value), "date": date, "kind": kind,
               "base_number": base_number, "base_date": base_date,
               "counterparty": str(counterparty), "contract": str(contract or ""),
               "object": object_name or str(state.get("object") or ""),
               "net": num(net_value), "rate": float(rate_value), "vat": num(vat_value),
               "gross": num(gross), "late_days": lag, "late": bool(late),
               "source": source, "key": key, "agency": bool(agency),
               "operation_code": derived.get("code"),
               "operation_code_why": derived.get("why"),
               "operation_code_source": derived.get("source"),
               "period": str(date)[:7]}
    state.setdefault("issued", []).append(invoice)
    state.setdefault("keys", []).append(key)
    result.update({"status": STATUS_OK, "invoice": invoice})
    return result


def auto_issue(state: dict, ledger_state: dict, period: str, *, rates: dict | None = None,
               rules: dict | None = None, mode: str = "post") -> dict:
    """Выставить счета-фактуры за период из журнала: отгрузка по актам, аванс по 62.02.

    `mode="propose"` — только показать, что было бы выставлено, номеров не присваивать.
    """
    from . import tax as _tax

    rules = rules or load_rules()
    rates = rates or _tax.load_rates()
    results: list[dict] = []
    propose = str(mode or "post") in ("propose", "предложить", "предложение")

    for entry in ledger_state.get("entries") or []:
        if str(entry.get("period") or "") != period:
            continue
        day = str(entry.get("date") or "")
        doc = entry.get("doc") or {}
        if entry.get("event") == "act_signed":
            credit = [line for line in entry.get("lines") or []
                      if str(line.get("account")) == "90.01" and line.get("side") == ledger.CREDIT]
            debit = [line for line in entry.get("lines") or []
                     if str(line.get("account")) == "90.03" and line.get("side") == ledger.DEBIT]
            if not credit:
                continue
            gross = sum((dec(line.get("amount")) for line in credit), Decimal("0"))
            vat = sum((dec(line.get("amount")) for line in debit), Decimal("0"))
            rate_row = _tax.vat_rate_on(day, rates)
            net_value = gross - vat
            # ставка восстанавливается из документа, а не подставляется законная:
            # счёт-фактура повторяет акт; расхождение с законом — отдельная проблема
            implied = ((vat / net_value * 100).quantize(Decimal("0.01"),
                                                        rounding=ROUND_HALF_UP)
                       if net_value > 0 else Decimal("0"))
            if implied > 0 and Decimal(str(rate_row.get("rate") or 0)) != implied:
                pass  # расхождение уже поймано в vat_return как VAT_RATE_STALE
            issue = _issue_one(
                state, kind="shipped", date=day, base_date=day,
                base_number=str(doc.get("number") or ""),
                counterparty=_counterparty_of(entry),
                contract=str(_analytics(entry).get("договор") or ""),
                net=net_value, vat=vat, rate=implied,
                object_name=str(_analytics(entry).get("объект") or ""),
                source=f"акт {doc.get('number') or ''} ({entry.get('id') or ''})",
                rules=rules, propose=propose)
            results.append(issue)
        elif entry.get("event") == "receipt_payment":
            credit = [line for line in entry.get("lines") or []
                      if str(line.get("account")) == "62.02" and line.get("side") == ledger.CREDIT]
            if not credit:
                continue
            gross = sum((dec(line.get("amount")) for line in credit), Decimal("0"))
            rate_percent = dec((entry.get("doc") or {}).get("vat", {}).get("rate")
                               if isinstance((entry.get("doc") or {}).get("vat"), dict) else
                               _tax.vat_rate_on(day, rates).get("rate"))
            if rate_percent <= 0:
                results.append({"status": STATUS_REJECTED, "invoice": None,
                                "gaps": [{"what": f"ставка НДС для аванса "
                                                  f"{doc.get('number') or ''}",
                                          "why": "счёт-фактура на аванс без ставки не "
                                                 "выставляется",
                                          "ask": "ставка по договору"}],
                                "errors": [], "warnings": []})
                continue
            net = (gross / (Decimal("1") + rate_percent / Decimal("100"))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            results.append(_issue_one(
                state, kind="advance", date=day, base_date=day,
                base_number=str(doc.get("number") or ""),
                counterparty=str((credit[0].get("analytics") or {}).get("контрагент") or ""),
                contract=str((credit[0].get("analytics") or {}).get("договор") or ""),
                net=net, vat=gross - net, rate=rate_percent,
                source=f"платёжка {doc.get('number') or ''} ({entry.get('id') or ''})",
                rules=rules, propose=propose))

    posted = sum(1 for row in results if row.get("status") == STATUS_OK)
    return {"posted": 0 if propose else posted, "planned": len(results) if propose else 0,
            "duplicates": sum(1 for row in results if row.get("status") == STATUS_DUPLICATE),
            "rejected": sum(1 for row in results if row.get("status") == STATUS_REJECTED),
            "results": results, "mode": "propose" if propose else "post",
            "gaps": [gap for row in results for gap in (row.get("gaps") or [])],
            "warnings": [w for row in results for w in (row.get("warnings") or [])]}


def _analytics(entry: dict) -> dict:
    """Аналитика проводки, слитая по строкам: контрагент и договор живут на разных счетах."""
    merged: dict[str, str] = {}
    for line in entry.get("lines") or []:
        for key, value in (line.get("analytics") or {}).items():
            if str(value).strip() and not merged.get(key):
                merged[key] = str(value)
    return merged


def _counterparty_of(entry: dict) -> str:
    analytics = _analytics(entry)
    return str(analytics.get("заказчик") or analytics.get("контрагент") or "")


def _issue_one(state: dict, *, propose: bool, **kwargs) -> dict:
    if not propose:
        return register(state, **kwargs)
    # «показать, что было бы»: номер не занимаем, в реестр не пишем
    preview = {"number": next_number(state, kwargs["date"], kind=kwargs["kind"],
                                     rules=kwargs.get("rules")), "date": kwargs["date"],
               "kind": kwargs["kind"], "proposed": True}
    return {"status": "предложен", "invoice": preview, "errors": [], "warnings": [],
            "gaps": []}



# ─────────────────────── полученные счета-фактуры (книга покупок) ───────────────────────
def register_purchase(state: dict, *, number: str, date: str, base_number: str,
                      counterparty: str, net=None, rate=None, vat=None,
                      base_date: str = "", received_date: str = "", inn: str = "",
                      kpp: str = "", contract: str = "", object_name: str = "",
                      agency: bool = False, agent: bool = False, source: str = "",
                      operation_code: str | None = None,
                      rules: dict | None = None) -> dict:
    """Зарегистрировать **полученный** счёт-фактуру поставщика.

    Номер и дата здесь чужие: их присваивает поставщик. Контур не нумерует входящие
    документы и не придумывает дату — без номера или даты документ не регистрируется
    (`PURCHASE_NO_NUMBER`). Ставки нет — нет и вычета: НДС берётся из счёта-фактуры, а не
    из справочника ставок.
    """
    rules = rules or load_rules()
    result = {"status": STATUS_REJECTED, "invoice": None, "errors": [], "gaps": [],
              "warnings": []}
    number = str(number or "").strip()
    date = str(date or "").strip()
    if not number or not date:
        result["errors"].append({
            "code": "PURCHASE_NO_NUMBER", "what": base_number or counterparty,
            "why": "у полученного счёта-фактуры нет номера или даты: нумеровать входящие "
                   "документы за поставщика контур не может"})
        result["gaps"].append({
            "what": f"номер и дата полученного счёта-фактуры"
                    f"{' от ' + counterparty if counterparty else ''}",
            "why": "номер присваивает поставщик: контур его не выдумывает",
            "ask": "запросить счёт-фактуру у поставщика или внести номер и дату в реестр"})
        return result
    if not str(base_number or "").strip():
        result["errors"].append({
            "code": "PURCHASE_NO_BASE", "what": number,
            "why": "у счёта-фактуры нет документа-основания: вычет нечем подтвердить"})
        result["gaps"].append({
            "what": f"документ-основание (накладная, УПД) для СФ {number}",
            "why": "счёт-фактура без первички не даёт вычета",
            "ask": "указать номер и дату документа, к которому выставлен счёт-фактура"})
        return result
    if not str(counterparty or "").strip():
        result["gaps"].append({"what": f"поставщик по СФ {number}",
                               "why": "без поставщика документ не идентифицирует сделку",
                               "ask": "наименование, ИНН и КПП поставщика"})
        return result
    if rate is None or str(rate).strip() == "":
        result["gaps"].append({"what": f"ставка НДС в СФ {number}",
                               "why": "ставки нет: вычет не считаем",
                               "ask": "ставка из счёта-фактуры поставщика"})
        return result
    rate_value = dec(rate)
    net_value = dec(net)
    if net_value <= 0:
        result["errors"].append({"code": "PURCHASE_NO_BASE", "what": number,
                                 "why": "база счёта-фактуры не положительна"})
        return result
    vat_value = dec(vat) if vat is not None else \
        (net_value * rate_value / Decimal("100")).quantize(Decimal("0.01"),
                                                           rounding=ROUND_HALF_UP)
    key = f"purchase|{number}|{date}|{counterparty}"
    if key in (state.get("keys") or []):
        existing = next((row for row in state.get("received") or []
                         if row.get("key") == key), None)
        result.update({"status": STATUS_DUPLICATE, "invoice": existing})
        result["warnings"].append(f"СФ {number} от {date} уже зарегистрирован")
        return result

    base_date = str(base_date or date)
    if date < base_date:
        result["errors"].append({
            "code": "PURCHASE_DATE_BEFORE_BASE", "what": number,
            "why": f"дата счёта-фактуры {date} раньше даты документа-основания "
                   f"{base_date}: так не бывает, значит одна из дат неверна"})
        return result

    lag = _days_between(base_date, str(received_date or date))
    if lag is not None and lag > int((rules.get("issue") or {}).get("deadline_days") or 5):
        result["warnings"].append(
            f"СФ {number}: получен через {lag} дн. после документа-основания — вычет "
            f"заявляется в квартале получения, а не задним числом")

    derived = operation_code_for("purchase", agency=agency, agent=agent)
    if operation_code:
        derived = dict(derived, code=str(operation_code), why="код указан человеком",
                       source="введён при регистрации документа", resolved=True)
    invoice = {"number": number, "date": date, "base_number": str(base_number or ""),
               "base_date": base_date, "counterparty": str(counterparty),
               "inn": str(inn or ""), "kpp": str(kpp or ""), "contract": str(contract or ""),
               "object": object_name or str(state.get("object") or ""),
               "net": num(net_value), "rate": float(rate_value), "vat": num(vat_value),
               "gross": num(net_value + vat_value), "received_date": str(received_date or date),
               "agency": bool(agency), "source": source, "kind": "received",
               "operation_code": derived.get("code"),
               "operation_code_why": derived.get("why"),
               "operation_code_source": derived.get("source"),
               "period": date[:7], "key": key}
    state.setdefault("received", []).append(invoice)
    state.setdefault("keys", []).append(key)
    result.update({"status": STATUS_OK, "invoice": invoice})
    return result


def auto_register_purchases(state: dict, payload: dict, period: str, *,
                            ledger_state: dict | None = None,
                            rules: dict | None = None) -> dict:
    """Зарегистрировать полученные СФ из реестра, который ведёт бухгалтер.

    Реестр — файл с номерами и датами поставщиков. Контур ничего не выдумывает: если
    документа-основания нет в журнале, регистрация проходит, но в книге покупок появится
    `PURCHASE_NO_BASE_DOC` — именно так и должно быть, счёт-фактура без проводки не даёт вычета.
    """
    rules = rules or load_rules()
    results: list[dict] = []
    booked = _booked_vat(ledger_state) if ledger_state else {}
    for row in payload.get("purchases") or payload.get("invoices") or []:
        if str(row.get("date") or "")[:7] != period:
            continue
        issue = register_purchase(state, number=str(row.get("number") or ""),
                                  date=str(row.get("date") or ""),
                                  base_number=str(row.get("base_number")
                                                  or row.get("document") or ""),
                                  base_date=str(row.get("base_date") or ""),
                                  received_date=str(row.get("received_date") or ""),
                                  counterparty=str(row.get("counterparty") or ""),
                                  inn=str(row.get("inn") or ""), kpp=str(row.get("kpp") or ""),
                                  contract=str(row.get("contract") or ""),
                                  net=row.get("net"), rate=row.get("rate"),
                                  vat=row.get("vat"), agency=bool(row.get("agency")),
                                  source=str(row.get("source") or ""), rules=rules)
        base = str(row.get("base_number") or row.get("document") or "")
        if issue["status"] == STATUS_OK and booked and base and base not in booked:
            issue["warnings"].append(
                f"СФ {row.get('number')}: документ-основание «{base}» не проведён — "
                f"вычет по такому счёту-фактуре не подтверждён проводкой")
        results.append(issue)
    return {"status": STATUS_OK, "mode": "post",
            "posted": sum(1 for row in results if row["status"] == STATUS_OK),
            "duplicates": sum(1 for row in results if row["status"] == STATUS_DUPLICATE),
            "rejected": sum(1 for row in results if row["status"] == STATUS_REJECTED),
            "results": results,
            "gaps": [gap for row in results for gap in (row.get("gaps") or [])],
            "warnings": [w for row in results for w in (row.get("warnings") or [])]}


def _booked_vat(ledger_state: dict, account: str = "19.03") -> dict:
    """НДС, проведённый по документам: сколько вычета журнал реально знает."""
    booked: dict[str, Decimal] = {}
    for entry in ledger_state.get("entries") or []:
        lines = [line for line in entry.get("lines") or []
                 if str(line.get("account")) == account
                 and line.get("side") == ledger.DEBIT]
        if not lines:
            continue
        number = str((entry.get("doc") or {}).get("number") or "")
        booked[number] = booked.get(number, Decimal("0")) + sum(
            (dec(line.get("amount")) for line in lines), Decimal("0"))
    return booked


def purchase_book(state: dict, ledger_state: dict, period: str, *, rates: dict | None = None,
                  rules: dict | None = None) -> dict:
    """Книга покупок за период (приложение № 4 к постановлению № 1137) и сверка с 19.03.

    Правило, ради которого книга и нужна: **вычет подтверждает документ, а не проводка**.
    Поэтому сумма по книге сверяется с вычетом, который журнал реально заявил, а остаток на
    19.03 без счёта-фактуры называется числом (`PURCHASE_UNREGISTERED`).
    """
    rules = rules or load_rules()
    rates = rates or tax.load_rates()
    booked = _booked_vat(ledger_state)
    rows: list[dict] = []
    problems: list[dict] = []
    gaps: list[dict] = []

    for invoice in state.get("received") or []:
        if str(invoice.get("period") or "") != period:
            continue
        if invoice.get("agency"):
            continue  # посреднические документы идут в журнал, не в книгу
        base = str(invoice.get("base_number") or "")
        vat = dec(invoice.get("vat"))
        row = {"number": invoice.get("number"), "date": invoice.get("date"),
               "fix_number": invoice.get("fix_number") or "",
               "fix_date": invoice.get("fix_date") or "",
               "fix_note": _fix_note(invoice),
               "received_date": invoice.get("received_date"),
               "counterparty": invoice.get("counterparty"), "inn": invoice.get("inn"),
               "base_number": base, "net": invoice.get("net"), "rate": invoice.get("rate"),
               "vat": invoice.get("vat"), "gross": invoice.get("gross"),
               "kind": "received", "kind_title": "полученный",
               "operation_code": invoice.get("operation_code") or "",
               "operation_code_why": invoice.get("operation_code_why") or "",
               "fix_number": invoice.get("fix_number") or "",
               "fix_date": invoice.get("fix_date") or "",
               "fix_note": _fix_note(invoice)}
        if base and base not in booked:
            problems.append({
                "code": "PURCHASE_NO_BASE_DOC", "what": f"СФ {invoice.get('number')}",
                "why": f"документ-основание «{base}» не проведён в журнале: вычет по этому "
                       f"счёту-фактуре не подтверждён первичкой"})
            gaps.append({"what": f"проводка по документу {base}",
                         "why": f"СФ {invoice.get('number')} есть, документа в журнале нет",
                         "ask": "провести приход или убрать счёт-фактуру из реестра"})
            row["state"] = "проблема"
        elif base and base in booked and vat > booked[base] + Decimal("0.01"):
            problems.append({
                "code": "PURCHASE_VAT_OVER_BOOKED", "what": f"СФ {invoice.get('number')}",
                "why": f"НДС в СФ {rub(vat)} больше проведённого {rub(booked[base])} по "
                       f"«{base}»: вычет больше, чем налог в первичке"})
            row["state"] = "проблема"
        else:
            row["state"] = "ok"
        rows.append(row)

    # ставка в полученном СФ — ставка поставщика: вычет берётся по документу, но
    # расхождение с законной ставкой должно быть названо, а не проглочено
    rate_notes: list[str] = []
    for row in rows:
        lawful = tax.vat_rate_on(str(row.get("date")), rates).get("rate")
        if lawful is not None and row.get("rate") is not None \
                and abs(dec(row["rate"]) - dec(lawful)) > Decimal("0.01"):
            rate_notes.append(
                f"{row['number']}: ставка {row['rate']:g} % при законной {lawful:g} % на "
                f"{row['date']} — вычет заявляется по документу поставщика, но документ "
                f"выписан по недействующей ставке")

    total_net = sum((dec(row["net"]) for row in rows), Decimal("0"))
    total_vat = sum((dec(row["vat"]) for row in rows), Decimal("0"))
    corrections = _corrections_for(state, period, "received")
    for row in corrections:
        # разницы хранятся со знаком: уменьшение уже отрицательное
        total_net += dec(row.get("net"))
        total_vat += dec(row.get("vat"))
        rows.append(row)

    journal_vat = sum(booked.values(), Decimal("0"))
    deducted = _deducted_vat(ledger_state, period)
    difference = journal_vat - total_vat
    if abs(difference) > Decimal("0.01"):
        problems.append({
            "code": "PURCHASE_BOOK_MISMATCH", "what": period,
            "why": f"НДС по книге покупок {rub(total_vat)} ≠ вычету, который знает журнал "
                   f"{rub(journal_vat)} (сальдо 19.03): разница {rub(difference)} — часть "
                   f"приходов без счетов-фактур; вычет заявлять нельзя, пока документов нет"})
        gaps.append({"what": f"счета-фактуры поставщиков за {period}",
                     "why": "книга покупок меньше вычета в журнале",
                     "ask": f"получить счета-фактуры на {rub(difference)} НДС или "
                            f"объяснить, почему вычет не заявляется"})
    if not rows:
        gaps.append({"what": f"полученные счета-фактуры за {period}",
                     "why": "реестр пуст: книга покупок пустая, вычета нет",
                     "ask": "реестр входящих СФ от поставщиков"})

    return {"kind": "purchase_book", "period": period, "rows": rows,
            "totals": {"invoices": len(rows), "net": num(total_net), "vat": num(total_vat),
                       "gross": num(total_net + total_vat),
                       "journal_vat": num(journal_vat), "deducted": num(deducted),
                       "difference": num(difference)},
            "problems": problems, "gaps": gaps, "ready": not problems,
            "rate_notes": rate_notes,
            "title": "Книга покупок", "rules": rules,
            "not_implemented": rules.get("not_implemented") or []}


def for_closing(state: dict, period: str) -> dict:
    """Реестр входящих СФ в том виде, в каком его ждёт закрытие месяца (`closing.step_vat`).

    Меняем форму, а не смысл: закрытие зачитывает НДС только по документу, и берёт оно
    ровно те счета-фактуры, что зарегистрированы. Посреднические сюда не попадают — их
    НДС не наш вычет.
    """
    return {"invoices": [{"number": row.get("number"), "date": row.get("date"),
                          "document": row.get("base_number"),
                          "counterparty": row.get("counterparty"),
                          "vat": row.get("vat"), "net": row.get("net")}
                         for row in state.get("received") or []
                         if str(row.get("period") or "") == period
                         and not row.get("agency")]}


def _deducted_vat(ledger_state: dict, period: str) -> Decimal:
    """Сколько НДС журнал зачёл в этом периоде: Дт 68.02 по закрытию месяца."""
    total = Decimal("0")
    for entry in ledger_state.get("entries") or []:
        if str(entry.get("period") or "") != period:
            continue
        if str(entry.get("event") or "") not in ("close.vat", "vat_deduct"):
            continue
        total += sum((dec(line.get("amount")) for line in entry.get("lines") or []
                      if str(line.get("account")) == "68.02"
                      and line.get("side") == ledger.DEBIT), Decimal("0"))
    return total


# ─────────────────────── корректировочные счета-фактуры ───────────────────────
def register_correction(state: dict, *, for_kind: str, base_number: str, base_date: str,
                        number: str | None = None, date: str = "",
                        delta_net=None, delta_vat=None, rate=None, direction: str = "decrease",
                        reason: str = "", counterparty: str = "", contract: str = "",
                        operation_code: str | None = None,
                        rules: dict | None = None) -> dict:
    """Корректировочный счёт-фактура (приложение № 2 к постановлению № 1137).

    `for_kind` — к чему корректировка: `issued` (мы выставили) или `received` (нам выставили).
    `direction` — `decrease` (уменьшение) или `increase`. Разницы хранятся со знаком:
    уменьшение уменьшает книгу, увеличение увеличивает. Причина обязательна: без неё
    корректировка неотличима от подгонки цифр.
    """
    rules = rules or load_rules()
    result = {"status": STATUS_REJECTED, "correction": None, "errors": [], "gaps": [],
              "warnings": []}
    if for_kind not in ("issued", "received"):
        result["errors"].append({"code": "CORRECTION_KIND", "what": for_kind,
                                 "why": "корректировка бывает к выставленному (issued) или "
                                        "полученному (received) счёту-фактуре"})
        return result
    if direction not in ("increase", "decrease"):
        result["errors"].append({"code": "CORRECTION_DIRECTION", "what": direction,
                                 "why": "направление корректировки: increase или decrease"})
        return result
    pool = state.get("issued" if for_kind == "issued" else "received") or []
    original = next((row for row in pool if str(row.get("number")) == str(base_number)
                     and str(row.get("date")) == str(base_date)), None)
    if not original:
        result["errors"].append({
            "code": "CORRECTION_NO_ORIGINAL", "what": f"{base_number} от {base_date}",
            "why": "корректировочный счёт-фактура без исходного документа: исправлять нечего"})
        return result
    date = str(date or "")
    if not date:
        result["errors"].append({"code": "CORRECTION_NO_DATE", "what": base_number,
                                 "why": "у корректировочного СФ нет даты"})
        return result
    if date < str(original.get("date") or ""):
        result["errors"].append({
            "code": "CORRECTION_DATE_BEFORE_BASE", "what": base_number,
            "why": f"корректировка от {date} раньше самого документа "
                   f"{original.get('date')}: так не бывает"})
        return result
    if not str(reason or "").strip():
        result["gaps"].append({"what": f"основание корректировки к СФ {base_number}",
                               "why": "без причины корректировка неотличима от правки цифр",
                               "ask": "основание: изменение цены, объёма, брак, корректировка КС-2"})
        return result
    net_value = abs(dec(delta_net))
    if net_value <= 0:
        result["errors"].append({"code": "CORRECTION_NO_AMOUNT", "what": base_number,
                                 "why": "разница стоимости нулевая: корректировать нечего"})
        return result
    rate_value = dec(rate) if rate is not None else dec(original.get("rate"))
    vat_value = abs(dec(delta_vat)) if delta_vat is not None else \
        (net_value * rate_value / Decimal("100")).quantize(Decimal("0.01"),
                                                           rounding=ROUND_HALF_UP)
    sign = Decimal("-1") if direction == "decrease" else Decimal("1")
    key = f"correction|{for_kind}|{base_number}|{base_date}|{date}|{num(net_value):.2f}"
    if key in (state.get("keys") or []):
        result.update({"status": STATUS_DUPLICATE,
                       "correction": next((row for row in state.get("corrections") or []
                                           if row.get("key") == key), None)})
        result["warnings"].append(f"корректировка к СФ {base_number} от {date} уже есть")
        return result
    if number is None or str(number).strip() == "":
        if for_kind == "received":
            result["gaps"].append({
                "what": f"номер корректировочного СФ к {base_number}",
                "why": "номер присваивает поставщик: контур его не выдумывает",
                "ask": "номер и дата корректировочного СФ от поставщика"})
            return result
        number = next_number(state, date, kind="correction", rules=rules)
    derived = operation_code_for("correction", direction=direction)
    if operation_code:
        derived = dict(derived, code=str(operation_code), why="код указан человеком",
                       source="введён при регистрации документа", resolved=True)
    correction = {"number": str(number), "date": date, "for_kind": for_kind,
                  "base_number": str(base_number), "base_date": str(base_date),
                  "counterparty": str(counterparty or original.get("counterparty") or ""),
                  "contract": str(contract or original.get("contract") or ""),
                  "direction": direction, "reason": str(reason),
                  "net": num(sign * net_value), "rate": float(rate_value),
                  "vat": num(sign * vat_value),
                  "gross": num(sign * (net_value + vat_value)),
                  "kind": "correction", "kind_title": f"корректировочный ({direction})",
                  "operation_code": derived.get("code"),
                  "operation_code_why": derived.get("why"),
                  "operation_code_source": derived.get("source"),
                  "period": date[:7], "key": key}
    state.setdefault("corrections", []).append(correction)
    state.setdefault("keys", []).append(key)
    result.update({"status": STATUS_OK, "correction": correction})
    return result


def fix_invoice(state: dict, *, for_kind: str, number: str, date: str,
                fix_number: str | None = None, fix_date: str = "",
                reason: str = "", rules: dict | None = None) -> dict:
    """Отметить исправление счёта-фактуры: номер и дата **исправления**, а не новый документ.

    Исправление (п. 2 Правил, приложение № 1 к постановлению № 1137; п. 7 разд. II и п. 6
    разд. III там же) не создаёт новый счёт-фактуру: номер и дата документа остаются
    прежними, добавляются номер и дата исправления. Поэтому документ не перевыставляется,
    а получает отметку, которую видно и в книге, и в выгрузке.

    Номер исправления своего документа налогоплательщик присваивает сам (по порядку), номер
    исправления входящего счёта-фактуры — поставщик: контур его не выдумывает.
    """
    rules = rules or load_rules()
    result = {"status": STATUS_REJECTED, "invoice": None, "errors": [], "gaps": [],
              "warnings": []}
    if for_kind not in ("issued", "received"):
        result["errors"].append({"code": "FIX_KIND", "what": for_kind,
                                 "why": "исправление бывает у выставленного (issued) или "
                                        "полученного (received) счёта-фактуры"})
        return result
    pool = state.get("issued" if for_kind == "issued" else "received") or []
    document = next((row for row in pool if str(row.get("number")) == str(number)
                     and str(row.get("date")) == str(date)), None)
    if not document:
        result["errors"].append({
            "code": "FIX_NO_ORIGINAL", "what": f"{number} от {date}",
            "why": "исправлять нечего: счёта-фактуры с таким номером и датой нет в реестре"})
        return result
    if document.get("agency") and not document.get("operation_code"):
        result["warnings"].append("документ посреднический: код вида операции у него не выведен")
    fix_date = str(fix_date or "")
    if not fix_date:
        result["gaps"].append({"what": f"дата исправления счёта-фактуры {number}",
                               "why": "без даты исправление невозможно датировать",
                               "ask": "дата исправления из документа поставщика/нашего"})
        return result
    if fix_date < str(document.get("date") or ""):
        result["errors"].append({
            "code": "FIX_DATE_BEFORE_DOC", "what": number,
            "why": f"дата исправления {fix_date} раньше даты счёта-фактуры "
                   f"{document.get('date')}: так не бывает"})
        return result
    if not str(reason or "").strip():
        result["gaps"].append({"what": f"причина исправления СФ {number}",
                               "why": "без причины исправление неотличимо от правки цифр задним "
                                      "числом",
                               "ask": "основание: неверные реквизиты, сумма, ставка, адрес"})
        return result
    if not fix_number or not str(fix_number).strip():
        if for_kind == "received":
            result["gaps"].append({
                "what": f"номер исправления полученного СФ {number}",
                "why": "номер исправления присваивает поставщик: контур его не выдумывает",
                "ask": "номер и дата исправления из документа поставщика"})
            return result
        # свой документ: номер исправления — по порядку в пределах документа
        fix_number = str(len(document.get("fixes") or []) + 1)

    fix = {"number": str(fix_number), "date": fix_date, "reason": str(reason),
           "period": fix_date[:7]}
    document.setdefault("fixes", []).append(fix)
    document["fix_number"] = fix["number"]
    document["fix_date"] = fix["date"]
    document["fix_reason"] = fix["reason"]
    if fix_date[:7] != str(document.get("period") or ""):
        result["warnings"].append(
            f"исправление {fix['number']} от {fix_date} относится к другому периоду, чем сам "
            f"документ {document.get('period')}: в книгу оно войдёт строкой текущего периода, "
            f"а дополнительный лист (КНД 1115105/1115106) контур не формирует — это запрос к "
            f"бухгалтеру")
    # отдаём копию: иначе следующее исправление перепишет уже возвращённый результат,
    # и вызывающий увидит не то состояние, которое получил
    result.update({"status": STATUS_OK,
                   "invoice": dict(document, fixes=list(document.get("fixes") or []))})
    return result


def _fix_note(row: dict) -> str:
    if not row.get("fix_number"):
        return ""
    return f"исправление № {row['fix_number']} от {row['fix_date']}"


def _corrections_for(state: dict, period: str, for_kind: str) -> list[dict]:
    return [row for row in state.get("corrections") or []
            if str(row.get("period") or "") == period and row.get("for_kind") == for_kind]


# ─────────────── журнал учёта для посредников (приложение № 3) ───────────────
def intermediary_journal(state: dict, period: str, *, rules: dict | None = None) -> dict:
    """Журнал учёта полученных и выставленных СФ: только посреднические документы.

    Посредник (агент, комиссионер, экспедитор, застройщик) ведёт журнал вместо книг: его
    счета-фактуры по операциям в пользу третьих лиц в книгу продаж и книгу покупок не
    попадают. Счёт-фактура на собственное вознаграждение в журнал не включается — она идёт
    в книгу обычным порядком.

    Сдаётся ежеквартально не позднее 20-го числа месяца, следующего за кварталом
    (п. 5 ст. 174 НК РФ).
    """
    rules = rules or load_rules()
    part1 = [row for row in state.get("issued") or []
             if row.get("agency") and str(row.get("period") or "") == period]
    part2 = [row for row in state.get("received") or []
             if row.get("agency") and str(row.get("period") or "") == period]
    quarterly = not part1 and not part2
    vat1 = sum((dec(row.get("vat")) for row in part1), Decimal("0"))
    vat2 = sum((dec(row.get("vat")) for row in part2), Decimal("0"))
    gaps: list[dict] = []
    if quarterly:
        gaps.append({"what": f"посреднические счета-фактуры за {period}",
                     "why": "журнал пуст: посреднических операций в контуре нет",
                     "ask": "отметить документ как посреднический (agency) или не сдавать журнал"})
    missing_code = [row for row in part1 + part2 if not row.get("operation_code")]
    agency_codes = {str(row.get("number")): (row.get("operation_code"),
                                             row.get("operation_code_why") or "")
                    for row in part1 + part2}
    if missing_code:
        gaps.append({"what": "код вида операции в журнале",
                     "why": "перечень кодов по приказу ФНС от 14.03.2016 № ММВ-7-3/136@ в "
                            "контур не заведён: подставлять код «на глаз» нельзя",
                     "ask": "код вида операции по каждой строке журнала"})
    return {
        "kind": "journal", "period": period, "title": "Журнал учёта полученных и "
                                                     "выставленных счетов-фактур",
        "part1": [{"number": row.get("number"), "date": row.get("date"),
                   "base_number": row.get("base_number"),
                   "counterparty": row.get("counterparty"),
                   "net": row.get("net"), "vat": row.get("vat"),
                   "gross": num(dec(row.get("net")) + dec(row.get("vat"))),
                   "operation_code": agency_codes.get(str(row.get("number")), ("",))[0] or "",
                   "operation_code_why": agency_codes.get(str(row.get("number")),
                                                          ("", ""))[1]} for row in part1],
        "part2": [{"number": row.get("number"), "date": row.get("date"),
                   "counterparty": row.get("counterparty"),
                   "net": row.get("net"), "vat": row.get("vat"),
                   "gross": num(dec(row.get("net")) + dec(row.get("vat"))),
                   "operation_code": agency_codes.get(str(row.get("number")), ("",))[0] or "",
                   "operation_code_why": agency_codes.get(str(row.get("number")),
                                                          ("", ""))[1]} for row in part2],
        "totals": {"part1": len(part1), "part2": len(part2), "vat1": num(vat1),
                   "vat2": num(vat2)},
        "gaps": gaps, "ready": not missing_code, "rules": rules,
        "knd": ((rules.get("books") or {}).get("journal") or {}).get("knd") or "",
        "deadline": "не позднее 20-го числа месяца, следующего за кварталом (п. 5 ст. 174 НК РФ)",
        "applicable_why": (rules.get("journal") or {}).get("who") or "",
        "not_implemented": (rules.get("journal") or {}).get("not_implemented")
                           or rules.get("not_implemented") or [],
        "note": ("счёт-фактура на вознаграждение посредника в журнал не включается; "
                 "срок сдачи — 20-е число месяца, следующего за кварталом (п. 5 ст. 174 НК РФ)"),
        "not_implemented": rules.get("not_implemented") or [],
    }


# ─────────────────────────── книга продаж ───────────────────────────
def sales_book(state: dict, ledger_state: dict, period: str, *, rates: dict | None = None,
               rules: dict | None = None) -> dict:
    """Книга продаж за период из реестра счетов-фактур и сверка с журналом.

    Книга строится **только из выставленных счетов-фактур**: если акт подписан, а СФ не
    выставлен, книга будет меньше начисленного НДС, и это расхождение называется числом
    (`SALES_BOOK_MISMATCH`), а не закрывается подстановкой суммы из журнала.
    """
    rules = rules or load_rules()
    rates = rates or tax.load_rates()
    rows = []
    for invoice in state.get("issued") or []:
        if str(invoice.get("period") or "") != period:
            continue
        if invoice.get("agency"):
            continue  # посреднические документы идут в журнал, не в книгу продаж
        rows.append({"number": invoice.get("number"), "date": invoice.get("date"),
                     "kind": invoice.get("kind"),
                     "kind_title": KINDS.get(str(invoice.get("kind")), ""),
                     "operation_code": invoice.get("operation_code") or "",
                     "operation_code_why": invoice.get("operation_code_why") or "",
                     "fix_number": invoice.get("fix_number") or "",
                     "fix_date": invoice.get("fix_date") or "",
                     "fix_note": _fix_note(invoice),
                     "base_number": invoice.get("base_number"),
                     "counterparty": invoice.get("counterparty"),
                     "contract": invoice.get("contract"),
                     "object": invoice.get("object"),
                     "net": invoice.get("net"), "rate": invoice.get("rate"),
                     "vat": invoice.get("vat"), "gross": invoice.get("gross"),
                     "late": invoice.get("late"), "late_days": invoice.get("late_days")})
    corrections = _corrections_for(state, period, "issued")
    for row in corrections:
        rows.append({"number": row["number"], "date": row["date"], "kind": "correction",
                     "kind_title": row["kind_title"], "base_number": row["base_number"],
                     "counterparty": row["counterparty"], "contract": row.get("contract"),
                     "object": row.get("object"),
                     "net": row["net"], "rate": row["rate"], "vat": row["vat"],
                     "gross": row["gross"], "late": False, "late_days": None})
    rows.sort(key=lambda row: str(row.get("date")))

    total_net = sum((dec(row["net"]) for row in rows), Decimal("0"))
    total_vat = sum((dec(row["vat"]) for row in rows), Decimal("0"))
    total_gross = sum((dec(row["gross"]) for row in rows), Decimal("0"))
    shipped_vat = sum((dec(row["vat"]) for row in rows if row["kind"] == "shipped"),
                      Decimal("0"))
    advance_vat = sum((dec(row["vat"]) for row in rows if row["kind"] == "advance"),
                      Decimal("0"))

    vat_report = tax.vat_return(ledger_state, period, rates=rates)
    journal_vat = (dec(vat_report["totals"]["vat_from_sales"])
                   + dec((vat_report.get("advances") or {}).get("in_ledger", {})
                         .get("accrued")))
    problems = list(vat_report.get("problems") or [])
    gaps: list[dict] = []

    book_total = total_vat
    if abs(book_total - journal_vat) > Decimal("0.01"):
        problems.append({
            "code": "SALES_BOOK_MISMATCH", "what": period,
            "why": f"НДС по книге продаж {rub(book_total)} ≠ начислено в журнале "
                   f"{rub(journal_vat)} (90.03 + НДС с авансов на 76.АВ): разница "
                   f"{rub(journal_vat - book_total)} — часть отгрузок или авансов без "
                   f"счета-фактуры; сдавать нельзя, пока не выставлены документы"})
        gaps.append({"what": f"счета-фактуры за {period}",
                     "why": "книга продаж меньше начисленного НДС",
                     "ask": f"выставить счета-фактуры на {rub(journal_vat - book_total)} "
                            f"НДС (`invoices auto {period}`) или объяснить расхождение"})
    if not rows:
        gaps.append({"what": f"выставленные счета-фактуры за {period}",
                     "why": "реестр пуст: книга продаж пустая",
                     "ask": f"`invoices auto {period}` — собрать счета-фактуры из журнала"})
    late = [row for row in rows if row.get("late")]
    if late:
        # перечисление собирается снаружи f-строки: вложенные кавычки — PEP 701,
        # на Python 3.11 это SyntaxError (поймано CI, а не локальным прогоном)
        listing = ", ".join("№ {0} (+{1} дн.)".format(row["number"], row["late_days"])
                            for row in late)
        problems.append({
            "code": "INVOICE_LATE", "what": period,
            "why": (f"счета-фактуры с нарушением срока 5 календарных дней: {listing}")})

    return {"kind": "sales_book", "period": period, "rows": rows,
            "totals": {"invoices": len(rows), "shipped_vat": num(shipped_vat),
                       "advance_vat": num(advance_vat), "net": num(total_net),
                       "vat": num(total_vat), "gross": num(total_gross),
                       "journal_vat": num(journal_vat),
                       "difference": num(journal_vat - book_total)},
            "problems": problems, "gaps": gaps,
            "ready": not problems,
            "title": (rules.get("sales_book") or {}).get("title") or "Книга продаж",
            "rules": rules,
            "not_implemented": rules.get("not_implemented") or []}


# ─────────────────── выгрузка книг и журнала (структура формата ФНС) ───────────────────
BOOK_TYPES = {"sales": "sales_book", "purchases": "purchase_book", "journal": "journal"}


def to_fns_xml(report: dict, *, organisation: dict | None = None,
               rules: dict | None = None) -> str:
    """XML книги продаж, книги покупок или журнала в структуре формата ФНС.

    Имена элементов, КНД и версии формата взяты из приказа ФНС от 08.06.2021
    № ЕД-7-26/547@ и приложений 3–5 к постановлению № 1137. **Со схемой XSD файл не
    сверялся** — этого в песочнице сделать нельзя (format.nalog.ru недоступен), поэтому в
    корне стоит `СвереноСоСхемой="нет"`, а рядом — список того, что не проверено. Молча
    выдавать такой файл за готовый к сдаче нельзя.

    Чего в файле нет и почему: `КодВидОпер` (перечень кодов не заведён), `Подписант`
    подписывается УКЭП, которой в контуре нет.
    """
    from xml.etree import ElementTree as ET

    rules = rules or load_rules()
    kind = str(report.get("kind") or "")
    if kind == "sales_book":
        spec = (rules.get("books") or {}).get("sales") or {}
        code = "sales"
    elif kind == "purchase_book":
        spec = (rules.get("books") or {}).get("purchases") or {}
        code = "purchases"
    elif kind == "journal":
        spec = (rules.get("books") or {}).get("journal") or {}
        code = "journal"
    else:
        raise ValueError(f"выгрузка книг: неизвестный отчёт {kind!r}")

    period = str(report.get("period") or "")
    year, quarter = period[:4], (int(period[5:7]) - 1) // 3 + 1 if period[5:7] else 0
    period_code = str(20 + quarter) if quarter else ""
    version = spec.get("format_version") or ""
    organisation = organisation or tax.load_rates().get("organisation") or {}
    inn = str(organisation.get("inn") or "")
    kpp = str(organisation.get("kpp") or "")
    file_id = f"KND_{spec.get('knd')}_{inn}{kpp}_{period.replace('-', '')}_1"

    root = ET.Element("Файл", {"ИдФайл": file_id, "ВерсФорм": version,
                               "ВерсПрог": "ИИ-агент ПТО", "ТипИнф": "КНД " + str(spec.get("knd")),
                               "СвереноСоСхемой": "нет"})
    document = ET.SubElement(root, "Документ", {"КНД": str(spec.get("knd") or ""),
                                                "ДатаДок": period + "-01",
                                                "Период": period_code, "ОтчетГод": year})
    ET.SubElement(document, str(spec.get("party") or "СвПродав"),
                  {"НаимОрг": str(organisation.get("name") or ""),
                   "ИННЮЛ": inn, "КПП": kpp})

    if code == "journal":
        for part, rows in (("СвЧ1", report.get("part1") or []),
                           ("СвЧ2", report.get("part2") or [])):
            node = ET.SubElement(document, part)
            for index, row in enumerate(rows, 1):
                item = ET.SubElement(node, "СвСчФакт", {"НомПП": str(index)})
                if row.get("operation_code"):
                    ET.SubElement(item, "КодВидОпер").text = str(row["operation_code"])
                ET.SubElement(item, "НомерСчФ").text = str(row.get("number") or "")
                ET.SubElement(item, "ДатаСчФ").text = _dmy(row.get("date"))
                counterparty = str(row.get("counterparty") or "")
                ET.SubElement(item, "НаимПок" if part == "СвЧ1" else "НаимПрод").text = \
                    counterparty
                ET.SubElement(item, "СтТовУчНалВсего").text = _money(dec(row.get("net"))
                                                                     + dec(row.get("vat")))
                ET.SubElement(item, "ВтчСумНДС").text = _money(row.get("vat"))
    else:
        book = ET.SubElement(document, str(spec.get("root") or "СвКнПрод"),
                            {"Период": period_code, "ОтчетГод": year})
        ET.SubElement(book, "Период").text = period_code
        ET.SubElement(book, "ОтчетГод").text = year
        for index, row in enumerate(report.get("rows") or [], 1):
            item = ET.SubElement(document, str(spec.get("row") or "СвПродаж"),
                                 {"НомПП": str(index)})
            if row.get("operation_code"):
                ET.SubElement(item, "КодВидОпер").text = str(row["operation_code"])
            attrs = {"НомерСчФ": str(row.get("number") or ""),
                     "ДатаСчФ": _dmy(row.get("date"))}
            if row.get("fix_number"):
                # исправление — реквизит документа, а не новая строка (ИспрСчФ в формате)
                attrs["НомерИспр"] = str(row["fix_number"])
                attrs["ДатаИспр"] = _dmy(row.get("fix_date"))
            ET.SubElement(item, "НомДатаСчФ", attrs)
            ET.SubElement(item, "НаимКонтрагент").text = str(row.get("counterparty") or "")
            ET.SubElement(item, "СтТовУчНалВсего").text = _money(row.get("gross"))
            ET.SubElement(item, "ВтчСумНДС").text = _money(row.get("vat"))
            if row.get("kind") == "correction":
                ET.SubElement(item, "РазнСтоимКСчФ").text = _money(row.get("gross"))

    problems: list[str] = []
    if not version:
        problems.append("версия формата не подтверждена: сверить с приказом ФНС")
    if not (rules.get("operation_codes") or {}).get("loaded"):
        problems.append("КодВидОпер не заполнен: перечень кодов (приказ ФНС от 14.03.2016 "
                        "№ ММВ-7-3/136@) в контуре не заведён")
    # код вида операции выводится по правилу из справочника, а не вводится человеком:
    # это допустимо (правило есть в приказе), но должно быть названо
    without_code = [str(row.get("number")) for row in report.get("rows") or []
                    if not row.get("operation_code")]
    without_code += [str(row.get("number")) for row in (report.get("part1") or []) +
                     (report.get("part2") or []) if not row.get("operation_code")]
    if without_code:
        problems.append("КодВидОпер не определён для документов: " + ", ".join(without_code))
    problems.append("файл не сверялся со схемой XSD: format.nalog.ru недоступен из песочницы")
    problems.append("Подписант не заполнен: УКЭП не подключена")
    checks = ET.SubElement(root, "ПроверкаФайла")
    for text in problems:
        ET.SubElement(checks, "Замечание").text = text

    ET.indent(root, space="  ")
    header = '<?xml version="1.0" encoding="UTF-8"?>\n'
    return header + ET.tostring(root, encoding="unicode")


def _money(value) -> str:
    return f"{dec(value):.2f}"


def _dmy(value) -> str:
    text = str(value or "")
    if len(text) == 10 and text[4] == "-":
        return f"{text[8:10]}.{text[5:7]}.{text[0:4]}"
    return text


def to_markdown(report: dict) -> str:
    """Отчёт книгой текстом: графы названы, «на проверку» отмечено, не сделано перечислено."""
    kind = str(report.get("kind") or "sales_book")
    rules = report.get("rules") or {}
    period = report.get("period")
    if kind == "journal":
        return _journal_markdown(report, rules, period)
    if kind == "purchase_book":
        spec = rules.get("purchases_book") or {}
        title = "Книга покупок"
        head = ("| № СФ поставщика | дата | код | вид | основание | поставщик | без НДС "
                "| ставка | НДС | с НДС |", "|---|---|---|---|---|---|---:|---:|---:|---:|")
    else:
        spec = rules.get("sales_book") or {}
        title = spec.get("title") or "Книга продаж"
        head = ("| № СФ | дата | код | вид | основание | покупатель | без НДС | ставка "
                "| НДС | с НДС |", "|---|---|---|---|---|---|---:|---:|---:|---:|")
    lines = [f"# {title} за {period}", ""]
    if spec.get("verify"):
        lines.append("> Графы и их порядок помечены «на проверку»: " + str(spec.get("verify_why")))
        lines.append("")
    lines.extend(head)
    for row in report.get("rows") or []:
        mark = " ⏰" if row.get("late") else ""
        kind_title = row.get("kind_title") or ("корректировочный" if row["kind"] == "correction"
                                               else "")
        fix = f" · {row['fix_note']}" if row.get("fix_note") else ""
        lines.append(f"| {row.get('number')}{mark}{fix} | {row.get('date')} | "
                     f"{row.get('operation_code') or '—'} | {kind_title} | "
                     f"{row.get('base_number')} | {row.get('counterparty')} | "
                     f"{rub(row.get('net'))} | {dec(row.get('rate')):g} % | "
                     f"{rub(row.get('vat'))} | {rub(row.get('gross'))} |")
    totals = report.get("totals") or {}
    lines.append("")
    if kind == "purchase_book":
        lines.append(f"Счетов-фактур {totals.get('invoices')} · НДС по книге "
                     f"{rub(totals.get('vat'))} · вычет в журнале {rub(totals.get('journal_vat'))} "
                     f"· разница {rub(totals.get('difference'))} · зачтено "
                     f"{rub(totals.get('deducted'))}")
    else:
        lines.append(f"Счетов-фактур {totals.get('invoices')} · НДС по книге "
                     f"{rub(totals.get('vat'))} (отгрузка {rub(totals.get('shipped_vat'))}, "
                     f"авансы {rub(totals.get('advance_vat'))}) · начислено в журнале "
                     f"{rub(totals.get('journal_vat'))} · разница {rub(totals.get('difference'))}")
    if report.get("rate_notes"):
        lines.append("")
        lines.append("## Ставки в документах")
        for text in report["rate_notes"]:
            lines.append(f"- {text}")
    if report.get("problems"):
        lines.append("")
        lines.append("## Расхождения")
        for item in report["problems"]:
            lines.append(f"- **{item.get('code')}** · {item.get('what')}: {item.get('why')}")
    if report.get("gaps"):
        lines.append("")
        lines.append("## Чего не хватает")
        for gap in report["gaps"]:
            ask = f" → {gap.get('ask')}" if gap.get("ask") else ""
            lines.append(f"- **{gap.get('what')}**: {gap.get('why')}{ask}")
    if report.get("not_implemented"):
        lines.append("")
        lines.append("## Не сделано")
        for text in report["not_implemented"]:
            lines.append(f"- {text}")
    return "\n".join(lines)


def _journal_markdown(report: dict, rules: dict, period) -> str:
    """Журнал учёта: две части, посреднические документы, срок сдачи."""
    lines = [f"# Журнал учёта полученных и выставленных счетов-фактур за {period}", ""]
    lines.append(f"КНД {report.get('knd') or '—'} · {report.get('note') or ''}")
    if report.get("applicable_why"):
        lines.append("")
        lines.append(f"Кому нужен: {report['applicable_why']}")
    for title, rows, party in (("Часть 1. Выставленные", report.get("part1") or [], "покупатель"),
                               ("Часть 2. Полученные", report.get("part2") or [], "продавец")):
        lines.append("")
        lines.append(f"## {title}")
        if not rows:
            lines.append("_строк нет_")
            continue
        lines.append(f"| № п/п | № СФ | дата | {party} | с НДС | НДС | код вида операции |")
        lines.append("|---|---|---|---|---:|---:|---|")
        for index, row in enumerate(rows, 1):
            code = row.get("operation_code") or "— не определён"
            lines.append(f"| {index} | {row.get('number')} | {row.get('date')} | "
                         f"{row.get('counterparty')} | {rub(row.get('gross'))} | "
                         f"{rub(row.get('vat'))} | {code} |")
    totals = report.get("totals") or {}
    lines.append("")
    lines.append(f"Выставленных {totals.get('part1')} (НДС {rub(totals.get('vat1'))}) · "
                 f"полученных {totals.get('part2')} (НДС {rub(totals.get('vat2'))})")
    if report.get("gaps"):
        lines.append("")
        lines.append("## Чего не хватает")
        for gap in report["gaps"]:
            ask = f" → {gap.get('ask')}" if gap.get("ask") else ""
            lines.append(f"- **{gap.get('what')}**: {gap.get('why')}{ask}")
    if report.get("not_implemented"):
        lines.append("")
        lines.append("## Не сделано")
        for text in report["not_implemented"]:
            lines.append(f"- {text}")
    return "\n".join(lines)
