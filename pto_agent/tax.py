# -*- coding: utf-8 -*-
"""Налоги: НДС, 6-НДФЛ, РСВ — из журнала и ведомости, а не «из головы».

Принцип, на котором стоит модуль: **декларация — это отчёт о журнале, а не отдельный
расчёт**. Если сумма в декларации не равна обороту по счёту, значит либо в журнале ошибка,
либо в декларации — и то и другое нельзя сдавать.

Что проверяется само:
* ставка НДС соответствует дате отгрузки (с 01.01.2026 — 22 %, а не 20 %);
* начислено по декларации = оборот Кт 68.02, к вычету = Дт 68.02;
* удержано НДФЛ = сумма по ведомости, перечислено = оборот Дт 68.01;
* взносы по РСВ = оборот Кт 69.01, травматизм = Кт 69.11;
* ИНН и КПП проходят проверку контрольной суммы.

XML собирается в нашем пространстве имён `urn:pto-agent:tax:2026`: состав показателей полный,
но это **не** формат ФНС — для сдачи нужен XSD и форма сдачи. Это записано в «не сделано»,
а не замалчивается.
"""
from __future__ import annotations

import json
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

from . import ledger

SCHEMA = "pto-agent/tax@1"
RATES_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "tax_rates.json"
NS = "urn:pto-agent:tax:2026"
EPS = Decimal("0.01")
KINDS = {"vat": "НДС (налоговая декларация)", "ndfl6": "6-НДФЛ",
         "rsv": "РСВ (расчёт по страховым взносам)", "psv": "Персонифицированные сведения",
         "profit": "Налог на прибыль (декларация)",
         "property": "Налог на имущество организаций"}


# ─────────────────────────── числа ───────────────────────────
def dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def kopeck(value) -> Decimal:
    return dec(value).quantize(EPS, rounding=ROUND_HALF_UP)


def num(value) -> float:
    return float(kopeck(value))


def rub(value) -> str:
    return f"{kopeck(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def load_rates(path: str | Path | None = None) -> dict:
    payload = json.loads(Path(path or RATES_PATH).read_text(encoding="utf-8"))
    if payload.get("schema") != "pto-agent/tax-rates@1":
        raise ValueError(f"ставки налогов: схема {payload.get('schema')!r} не та")
    return payload


def vat_rate_on(day: str, rates: dict) -> dict:
    """Ставка НДС, действующая на дату отгрузки. Дата решает, а не договор."""
    day = str(day or "")
    for row in (rates.get("vat") or {}).get("periods") or []:
        start = str(row.get("from") or "")
        end = str(row.get("to") or "9999-12-31")
        if start <= day <= end:
            return {"rate": float(row.get("rate") or 0), "basis": row.get("basis") or "",
                    "from": start, "to": row.get("to")}
    return {"rate": None, "basis": "на эту дату ставка не задана в tax_rates.json",
            "from": "", "to": ""}


# ─────────────────────────── реквизиты ───────────────────────────
def inn_ok(inn: str) -> bool:
    """Контрольная сумма ИНН: 10 знаков для организации, 12 — для ИП и физлица."""
    digits = [int(char) for char in str(inn or "") if char.isdigit()]
    if len(digits) == 10:
        weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        control = sum(digit * weight for digit, weight in zip(digits, weights)) % 11 % 10
        return control == digits[9]
    if len(digits) == 12:
        weights1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        weights2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c1 = sum(digit * weight for digit, weight in zip(digits, weights1)) % 11 % 10
        c2 = sum(digit * weight for digit, weight in zip(digits, weights2)) % 11 % 10
        return c1 == digits[10] and c2 == digits[11]
    return False


def kpp_ok(kpp: str) -> bool:
    """КПП: 9 знаков — 4 цифры кода инспекции, 2 знака причины, 3 цифры. Контрольной суммы нет."""
    text = str(kpp or "")
    return (len(text) == 9 and text[:4].isdigit() and text[4:6].isalnum()
            and text[6:].isdigit())


# ─────────────────────────── обороты ───────────────────────────
def in_period(entry_period, period: str) -> bool:
    """Попадает ли запись журнала в период.

    Период — либо месяц `ГГГГ-ММ`, либо год `ГГГГ`. Год нужен потому, что налог считается
    за отчётный период целиком (ст. 286 НК РФ — нарастающим итогом), а не суммой месяцев:
    сумма помесячных расчётов и годовой расчёт совпадают не всегда, и выдавать одно за
    другое нельзя.
    """
    entry_period = str(entry_period or "")
    period = str(period or "")
    if len(period) == 4:
        return entry_period.startswith(period)
    return entry_period == period


def turnover(state: dict, account: str, period: str, *, side: str | None = None,
             prefix: bool = True) -> Decimal:
    total = Decimal("0")
    period = str(period)
    for entry in state.get("entries") or []:
        if not in_period(entry.get("period"), period):
            continue
        for line in entry.get("lines") or []:
            code = str(line.get("account") or "")
            match = code.startswith(account) if prefix else code == account
            if not match:
                continue
            if side and line.get("side") != side:
                continue
            total += kopeck(line.get("amount"))
    return total


def _entries(state: dict, period: str, event: str | None = None) -> list[dict]:
    return [entry for entry in state.get("entries") or []
            if in_period(entry.get("period"), period)
            and (event is None or entry.get("event") == event)]


# ─────────────────────────── НДС ───────────────────────────

# ─────────────────────────── НДС с авансов ───────────────────────────
def advances_vat(state: dict, period: str, *, rates: dict | None = None) -> dict:
    """НДС с авансов отдельными строками: начислено с полученных, принято к вычету.

    Логика: аванс в контуре приходит на 62.02 целиком (с НДС), значит налог с аванса —
    `сумма × ставка / (100 + ставка)`. Вычет появляется, когда аванс зачтён отгрузкой:
    проводка `advance_offset` (Дт 62.02 Кт 62.01) — налог с зачтённой части.

    **Проводки.** Счёт 76.АВ в плане есть, налог ставится проводками
    `tax post-advances <период>` (Дт 76.АВ Кт 68.02 при авансе, Дт 68.02 Кт 76.АВ при зачёте).
    Если проводок нет — расхождение называется кодом `VAT_ADVANCE_NOT_POSTED` с числами.
    **Счёт-фактура на аванс не нумеруется:** реестра счетов-фактур в контуре нет, поэтому
    вычет без документа — отдельное предупреждение `VAT_ADVANCE_NO_INVOICE`, а не тишина.
    """
    rates = rates or load_rates()
    rate = vat_rate_on(_last_day(period), rates)
    percent = Decimal(str(rate["rate"] or 0))

    received = Decimal("0")
    received_docs: list[dict] = []
    for entry in _entries(state, period):
        credit = [line for line in entry.get("lines") or []
                  if str(line.get("account")) == "62.02" and line.get("side") == ledger.CREDIT]
        if not credit:
            continue
        amount = sum((kopeck(line.get("amount")) for line in credit), Decimal("0"))
        received += amount
        received_docs.append({"document": (entry.get("doc") or {}).get("number") or "",
                              "date": entry.get("date"), "amount": num(amount),
                              "counterparty": (credit[0].get("analytics") or {}).get("контрагент")
                              or ""})

    offset = Decimal("0")
    for entry in _entries(state, period):
        debit = [line for line in entry.get("lines") or []
                 if str(line.get("account")) == "62.02" and line.get("side") == ledger.DEBIT
                 and entry.get("event") == "advance_offset"]
        if debit:
            offset += sum((kopeck(line.get("amount")) for line in debit), Decimal("0"))

    def tax_of(amount: Decimal) -> Decimal:
        if percent <= 0:
            return Decimal("0")
        return kopeck(amount * percent / (Decimal("100") + percent))

    vat_received = tax_of(received)
    vat_offset = tax_of(min(offset, received))
    # что из этого уже стоит в журнале на 76.АВ (проводки делает post_advance_vat)
    # 76.АВ — активный счёт: начисление налога идёт в дебет, вычет — в кредит
    on_ledger = turnover(state, "76.АВ", period, side=ledger.DEBIT, prefix=False)
    off_ledger = turnover(state, "76.АВ", period, side=ledger.CREDIT, prefix=False)
    return {
        "rate": rate["rate"], "basis": rate["basis"],
        "received": num(received), "vat_on_received": num(vat_received),
        "offset": num(offset), "vat_on_offset": num(vat_offset),
        "in_ledger": {"accrued": num(on_ledger), "deducted": num(off_ledger),
                      "account": "76.АВ"},
        "issued": {
            "advance": num(turnover(state, "60.02", period, side=ledger.DEBIT, prefix=False)),
            "offset": num(turnover(state, "60.02", period, side=ledger.CREDIT, prefix=False)),
            # 76.ВА — активный счёт: вычет налога с выданного аванса идёт в КРЕДИТ
            # (Дт 68.02 Кт 76.ВА), восстановление при зачёте — в ДЕБЕТ (Дт 76.ВА Кт 68.02).
            # В круге 32 здесь были перепутаны стороны, и вычет выглядел как 0.
            "in_ledger": num(turnover(state, "76.ВА", period, side=ledger.CREDIT, prefix=False)),
            "restored": num(turnover(state, "76.ВА", period, side=ledger.DEBIT, prefix=False)),
            "account": "76.ВА",
            "note": ("выданные авансы: вычет идёт на 76.ВА только по документам со ставкой; "
                     "если ставки в документе нет — вычет не ставим и запрашиваем счёт-фактуру"),
        },
        "to_pay": num(vat_received - vat_offset),
        "documents": received_docs,
        "lines": [
            {"line": "3-070", "what": "НДС с полученных авансов (начислено)",
             "amount": num(vat_received), "verify": True,
             "source": "обороты Кт 62.02 × ставка / (100 + ставка)"},
            {"line": "3-130", "what": "НДС с авансов, принятый к вычету при отгрузке",
             "amount": num(vat_offset), "verify": True,
             "source": "проводки зачёта аванса Дт 62.02 Кт 62.01"},
            {"line": None, "what": "НДС с авансов, выданных поставщикам (к вычету)",
             "amount": num(turnover(state, "76.ВА", period, side=ledger.CREDIT, prefix=False)),
             "verify": True,
             "source": "проводки Дт 68.02 Кт 76.ВА по документам со ставкой (п. 12 ст. 171 НК РФ)"},
            {"line": None, "what": "НДС с выданных авансов, восстановленный при зачёте",
             "amount": num(turnover(state, "76.ВА", period, side=ledger.DEBIT, prefix=False)),
             "verify": True,
             "source": "проводки Дт 76.ВА Кт 68.02 при зачёте аванса (п. 3 ст. 170 НК РФ)"},
        ],
        "note": ("номера строк помечены «на проверку»: форма НДС менялась приказом ФНС "
                 "от 18.12.2025 № ЕД-7-3/1227@ — перед сдачей сверять с действующей редакцией"),
    }



# ─────────────────────────── проводки по НДС с авансов ───────────────────────────
def post_advance_vat(state: dict, period: str, *, rates: dict | None = None,
                     mode: str | None = None, chart: dict | None = None,
                     rules: dict | None = None) -> dict:
    """Провести НДС с авансов: Дт 76.АВ Кт 68.02 при получении и вычет при зачёте.

    Зачем: без этих проводок НДС с авансов жил только в декларации, а журнал о нём не знал —
    расхождение называлось `VAT_ADVANCE_NOT_POSTED`. Теперь сумма оседает на 76.АВ, и
    сверка «декларация ↔ журнал» возможна по каждому авансу.

    Границы: счёт-фактура на аванс **не нумеруется** — реестра счетов-фактур в контуре нет,
    поэтому в memo проводки так и написано. Ключ идемпотентности включает документ и сумму:
    повторный вызов не задвоит налог.
    """
    from . import ledger as accounting

    rates = rates or load_rates()
    chart = chart or accounting.load_chart()
    rules = rules if rules is not None else accounting.load_rules()
    result = {"posted": 0, "duplicates": 0, "rejected": 0, "results": [],
              "accrued": Decimal("0"), "deducted": Decimal("0"),
              "issued_deducted": Decimal("0"), "issued_restored": Decimal("0"),
              "errors": [], "gaps": [], "warnings": [],
              "no_invoice": []}

    def _vat(gross: Decimal, day: str) -> tuple[Decimal, float]:
        rate = vat_rate_on(day, rates)
        percent = rate.get("rate")
        if not percent:
            return Decimal("0"), 0.0
        return kopeck(gross * Decimal(str(percent)) / (Decimal("100") + Decimal(str(percent)))), \
            float(percent)

    def _post(event: str, rule: str, date: str, doc: dict, lines: list[dict], memo: str,
              key: str) -> None:
        outcome = accounting.post_entry(state, event=event, date=date, doc=doc, lines=lines,
                                       memo=memo, rule=rule, key=key, chart=chart, rules=rules,
                                       mode=mode or accounting.MODE_POST)
        result["results"].append({"event": event, "number": doc.get("number"),
                                  "status": outcome.get("status"),
                                  "errors": outcome.get("errors") or []})
        if outcome.get("status") == accounting.STATUS_OK:
            result["posted"] += 1
        elif outcome.get("status") == accounting.STATUS_DUPLICATE:
            result["duplicates"] += 1
        else:
            result["rejected"] += 1
            result["errors"].extend(outcome.get("errors") or [])
            result["gaps"].extend(outcome.get("gaps") or [])
        result["warnings"].extend(outcome.get("warnings") or [])
        return outcome.get("status")

    for entry in _entries(state, period, "receipt_payment"):
        for line in entry.get("lines") or []:
            if str(line.get("account")) != "62.02" or line.get("side") != ledger.CREDIT:
                continue
            gross = kopeck(line.get("amount"))
            vat, percent = _vat(gross, str(entry.get("date") or ""))
            number = (entry.get("doc") or {}).get("number") or ""
            if vat <= 0:
                result["gaps"].append({"what": f"ставка НДС для аванса {number}",
                                       "why": "на дату аванса ставка не задана в справочнике",
                                       "ask": "ставка НДС по договору на дату получения аванса"})
                continue
            analytics = {key: str(value) for key, value in (line.get("analytics") or {}).items()
                         if str(value).strip()}
            missing = [field for field in ("контрагент", "договор") if field not in analytics]
            if missing:
                result["gaps"].append({"what": f"аналитика {', '.join(missing)} для аванса {number}",
                                       "why": "76.АВ требует контрагента и договор: без них налог "
                                              "не привязан к авансу",
                                       "ask": f"указать в первичке: {', '.join(missing)}"})
                continue
            _post("vat_advance", "vat.advance_accrual", str(entry.get("date") or ""),
                  {"number": f"НДС с аванса {number}", "date": entry.get("date"),
                   "kind": "расчёт НДС"},
                  [{"account": "76.АВ", "side": ledger.DEBIT, "amount": num(vat),
                    "analytics": analytics},
                   {"account": "68.02", "side": ledger.CREDIT, "amount": num(vat),
                    "analytics": {}}],
                  (f"НДС {percent:g} % с аванса {number} ({rub(gross)}): ставка "
                   f"{percent:g}/{(100 + percent):g}; счёт-фактура на аванс не нумеровалась — "
                   f"реестра СФ в контуре нет"),
                  key=f"vat_advance|accrual|{number}|{entry.get('date')}|{num(vat):.2f}")
            result["accrued"] += vat
            result["no_invoice"].append({"document": number, "date": entry.get("date"),
                                         "vat": num(vat),
                                         "why": "счёт-фактура на аванс не формируется: реестра "
                                                "счетов-фактур в контуре нет"})

    for entry in _entries(state, period, "advance_offset"):
        for line in entry.get("lines") or []:
            if str(line.get("account")) != "62.02" or line.get("side") != ledger.DEBIT:
                continue
            gross = kopeck(line.get("amount"))
            vat, percent = _vat(gross, str(entry.get("date") or ""))
            number = (entry.get("doc") or {}).get("number") or ""
            if vat <= 0:
                continue
            analytics = {key: str(value) for key, value in (line.get("analytics") or {}).items()
                         if str(value).strip()}
            if any(field not in analytics for field in ("контрагент", "договор")):
                result["gaps"].append({"what": f"аналитика зачёта аванса {number}",
                                       "why": "76.АВ требует контрагента и договор",
                                       "ask": "указать контрагента и договор в зачёте"})
                continue
            _post("vat_advance_offset", "vat.advance_deduction",
                  str(entry.get("date") or ""),
                  {"number": f"вычет НДС с аванса {number}", "date": entry.get("date"),
                   "kind": "расчёт НДС"},
                  [{"account": "68.02", "side": ledger.DEBIT, "amount": num(vat),
                    "analytics": {}},
                   {"account": "76.АВ", "side": ledger.CREDIT, "amount": num(vat),
                    "analytics": analytics}],
                  (f"вычет НДС {percent:g} % с зачтённой части аванса {number} ({rub(gross)})"),
                  key=f"vat_advance|deduction|{number}|{entry.get('date')}|{num(vat):.2f}")
            result["deducted"] += vat
    # ── выданные авансы: НДС к вычету (76.ВА) ─────────────────────────────
    # Сумма налога берётся только из документа: ставку «по умолчанию» не подставляем —
    # ровно как в правиле stock.receipt для 19.03.
    # ставка по контрагенту и договору из всех документов: зачёт аванса — это не новый
    # документ, ставка в нём не повторяется, её надо взять из самого аванса
    rates_by_deal: dict[tuple[str, str], Decimal] = {}
    for entry in state.get("entries") or []:
        doc = entry.get("doc") or {}
        hint = (doc.get("vat") or {}).get("rate") if isinstance(doc.get("vat"), dict) else None
        if not hint:
            continue
        line = next((row for row in entry.get("lines") or []
                     if str(row.get("account")) == "60.02"), None)
        if not line:
            continue
        analytics = line.get("analytics") or {}
        key = (str(analytics.get("контрагент") or ""), str(analytics.get("договор") or ""))
        rates_by_deal.setdefault(key, Decimal(str(hint)))

    for entry in _entries(state, period):
        doc = entry.get("doc") or {}
        rate_hint = (doc.get("vat") or {}).get("rate") if isinstance(doc.get("vat"), dict) else \
            doc.get("vat_rate")
        if not rate_hint:
            # зачёт аванса: берём ставку из документа самого аванса
            probe = next((row for row in entry.get("lines") or []
                          if str(row.get("account")) == "60.02"), None)
            analytics_probe = (probe or {}).get("analytics") or {}
            found = rates_by_deal.get((str(analytics_probe.get("контрагент") or ""),
                                       str(analytics_probe.get("договор") or "")))
            if found:
                rate_hint = found
        if not rate_hint:
            result["gaps"].append(
                {"what": f"ставка НДС в документе аванса выданного {doc.get('number') or ''}",
                 "why": "без ставки вычет с аванса выданного не считаем",
                 "ask": "счёт-фактура на аванс или указание ставки в заявке"})
            continue
        percent = Decimal(str(rate_hint))
        debit = [line for line in entry.get("lines") or []
                 if str(line.get("account")) == "60.02" and line.get("side") == ledger.DEBIT]
        credit = [line for line in entry.get("lines") or []
                  if str(line.get("account")) == "60.02" and line.get("side") == ledger.CREDIT]
        # аванс выдан (Дт 60.02): вычет — Дт 68.02 Кт 76.ВА;
        # аванс зачтён (Кт 60.02): восстановление — Дт 76.ВА Кт 68.02
        for line, event, rule, side_debit, side_credit in (
                (debit[0] if debit else None, "vat_issued_advance", "vat.issued_advance",
                 "68.02", "76.ВА"),
                (credit[0] if credit else None, "vat_issued_advance_restore",
                 "vat.issued_advance_restore", "76.ВА", "68.02")):
            if not line:
                continue
            gross = kopeck(line.get("amount"))
            vat = kopeck(gross * percent / (Decimal("100") + percent))
            if vat <= 0:
                continue
            analytics = {key: str(value) for key, value in (line.get("analytics") or {}).items()
                         if str(value).strip()}
            if not analytics.get("контрагент") or not analytics.get("договор"):
                missing = [field for field in ("контрагент", "договор")
                           if not analytics.get(field)]
                result["gaps"].append({"what": f"аналитика {', '.join(missing)} для аванса "
                                               f"выданного {doc.get('number') or ''}",
                                       "why": "76.ВА требует контрагента и договор: без них налог "
                                              "не привязан к авансу",
                                       "ask": f"указать в первичке: {', '.join(missing)}"})
                continue
            status = _post(event, rule, str(entry.get("date") or ""),
                  {"number": f"{'вычет' if side_debit == '68.02' else 'восстановление'} "
                             f"НДС с аванса выданного {doc.get('number') or ''}",
                   "date": entry.get("date"), "kind": "расчёт НДС"},
                  [{"account": side_debit, "side": ledger.DEBIT, "amount": num(vat),
                    "analytics": analytics if side_debit == "76.ВА" else {}},
                   {"account": side_credit, "side": ledger.CREDIT, "amount": num(vat),
                    "analytics": analytics if side_credit == "76.ВА" else {}}],
                  (f"НДС {percent:g} % с аванса выданного {doc.get('number') or ''} "
                   f"({rub(gross)}): ставка взята из документа"),
                  key=f"vat_issued|{event}|{doc.get('number') or ''}|"
                      f"{entry.get('date')}|{num(vat):.2f}")
            if status != accounting.STATUS_OK:
                continue
            if side_debit == "68.02":
                result["issued_deducted"] = num(kopeck(result.get("issued_deducted") or 0) + vat)
            else:
                result["issued_restored"] = num(kopeck(result.get("issued_restored") or 0) + vat)
    result["accrued"] = num(result["accrued"])
    result["deducted"] = num(result["deducted"])
    result["issued_deducted"] = num(result["issued_deducted"])
    result["issued_restored"] = num(result["issued_restored"])
    return result

def _last_day(period: str) -> str:
    """Последний день месяца: ставка НДС берётся на дату операции, а не на «сейчас»."""
    import calendar as _calendar
    try:
        year, month = int(period[:4]), int(period[5:7])
    except (TypeError, ValueError):
        return ""
    return f"{period[:7]}-{_calendar.monthrange(year, month)[1]:02d}"


def vat_return(state: dict, period: str, *, rates: dict | None = None,
               organisation: dict | None = None) -> dict:
    """Декларация по НДС из журнала: начислено, вычеты, к уплате и проверка ставок."""
    rates = rates or load_rates()
    organisation = organisation or rates.get("organisation") or {}
    report = {"kind": "vat", "period": period, "organisation": organisation,
              "rows": [], "rate_audit": [], "problems": [], "gaps": [], "warnings": []}

    gross = turnover(state, "90.01", period, side=ledger.CREDIT)
    # обороты счёта 68.02 — это то, что уже стоит в журнале (включая НДС с авансов, если
    # проводки сделаны); декларация считается из первички, журнал — из проводок, и ниже
    # они сверяются, а не подменяют друг друга
    vat_account_credit = turnover(state, "68.02", period, side=ledger.CREDIT)
    vat_account_debit = turnover(state, "68.02", period, side=ledger.DEBIT)
    output_vat = turnover(state, "90.03", period, side=ledger.DEBIT)
    net_sales = gross - output_vat
    advances = advances_vat(state, period, rates=rates)
    # выданные авансы: вычет живёт в журнале (76.ВА) и до круга 33 в декларации отсутствовал —
    # журнал знал про вычет больше, чем декларация. Теперь это отдельная строка.
    issued_deducted = kopeck(advances["issued"]["in_ledger"])
    issued_restored = kopeck(advances["issued"]["restored"])
    # вычеты, не связанные с авансами: зачёт 19.03 и прочие проводки по 68.02.
    # Вычеты по авансам (полученным на 76.АВ и выданным на 76.ВА) вычитаются из оборота
    # 68.02 явно, иначе они попадут в «прочие» и потеряют название
    non_advance_deducted = (vat_account_debit
                            - kopeck(advances["in_ledger"]["deducted"]) - issued_deducted)
    if non_advance_deducted < 0:
        non_advance_deducted = Decimal("0")
    charged, deducted = output_vat, non_advance_deducted

    # по объектам: 90.01 проводится с НДС, поэтому база = 90.01 − 90.03 того же акта,
    # а НДС берётся строкой 90.03 (у неё нет аналитики объекта — берём объект из акта)
    by_object: dict[str, dict] = {}
    for entry in _entries(state, period, "act_signed"):
        number = (entry.get("doc") or {}).get("number") or ""
        credit = [line for line in entry.get("lines") or []
                  if str(line.get("account")) == "90.01" and line.get("side") == ledger.CREDIT]
        debit = [line for line in entry.get("lines") or []
                 if str(line.get("account")) == "90.03" and line.get("side") == ledger.DEBIT]
        object_name = str(((credit[0].get("analytics") or {}).get("объект")
                           if credit else "") or "без объекта")
        gross_sum = sum((kopeck(line.get("amount")) for line in credit), Decimal("0"))
        vat_sum = sum((kopeck(line.get("amount")) for line in debit), Decimal("0"))
        slot = by_object.setdefault(object_name, {"object": object_name, "gross": Decimal("0"),
                                                  "vat": Decimal("0"), "net": Decimal("0"),
                                                  "acts": []})
        slot["gross"] += gross_sum
        slot["vat"] += vat_sum
        slot["net"] += gross_sum - vat_sum
        slot["acts"].append(number)

        if gross_sum - vat_sum <= 0:
            continue
        implied = (vat_sum / (gross_sum - vat_sum) * 100).quantize(Decimal("0.01"),
                                                                  rounding=ROUND_HALF_UP)
        lawful = vat_rate_on(str(entry.get("date")), rates)
        expected = kopeck((gross_sum - vat_sum) * Decimal(str(lawful["rate"] or 0))
                          / Decimal("100"))
        gap = expected - vat_sum
        report["rate_audit"].append({
            "document": number, "date": entry.get("date"), "base": num(gross_sum - vat_sum),
            "vat": num(vat_sum), "rate_implied": float(implied),
            "rate_lawful": lawful["rate"], "basis": lawful["basis"],
            "vat_expected": num(expected), "gap": num(gap)})
        if abs(implied - Decimal(str(lawful["rate"] or 0))) > Decimal("0.01"):
            report["problems"].append({
                "code": "VAT_RATE_STALE", "what": number,
                "why": f"НДС начислен по {implied} % при законной ставке {lawful['rate']} % "
                       f"на {entry.get('date')} ({lawful['basis']}): расхождение {rub(gap)}. "
                       f"Это ошибка в первичке: исправляется корректировочным счётом-фактурой, "
                       f"а не правкой декларации"})

    for slot in by_object.values():
        report["rows"].append({"object": slot["object"], "sales": num(slot["net"]),
                               "gross": num(slot["gross"]), "vat": num(slot["vat"]),
                               "acts": slot["acts"]})

    report["advances"] = advances
    advance_vat = kopeck(advances["vat_on_received"]) - kopeck(advances["vat_on_offset"])
    # вычет по выданным авансам уменьшает налог, восстановление — увеличивает
    payable = charged - deducted + advance_vat - issued_deducted + issued_restored
    report["totals"] = {"sales": num(net_sales), "sales_gross": num(gross),
                        "vat_charged": num(charged), "vat_deducted": num(deducted),
                        "vat_from_sales": num(output_vat),
                        "vat_on_advances": num(kopeck(advances["vat_on_received"])),
                        "vat_on_advances_offset": num(kopeck(advances["vat_on_offset"])),
                        "vat_on_issued_advances": num(issued_deducted),
                        "vat_on_issued_restored": num(issued_restored),
                        "vat_deducted_total": num(deducted + kopeck(advances["in_ledger"]["deducted"])
                                                  + issued_deducted),
                        "payable": num(payable),
                        "refund": num(-payable) if payable < 0 else 0.0}

    posted = advances["in_ledger"]
    if kopeck(advances["vat_on_received"]) > 0 and \
            (kopeck(posted["accrued"]) != kopeck(advances["vat_on_received"])
             or kopeck(posted["deducted"]) != kopeck(advances["vat_on_offset"])):
        # авансовый НДС посчитан, но на 76.АВ его нет (или он другой): сумма попадёт в строки
        # декларации, а в журнале будет другая — это надо назвать числом, а не спрятать
        report["problems"].append({
            "code": "VAT_ADVANCE_NOT_POSTED", "what": period,
            "why": f"НДС с авансов по декларации: начислено "
                   f"{rub(advances['vat_on_received'])}, вычет "
                   f"{rub(advances['vat_on_offset'])}; в журнале на 76.АВ: "
                   f"{rub(posted['accrued'])} и {rub(posted['deducted'])}. Разница — "
                   f"{rub(kopeck(advances['vat_on_received']) - kopeck(posted['accrued']))} "
                   f"начисления: провести `tax post-advances {period}` или объяснить, почему "
                   f"налог с авансов не выделяется"})
    elif kopeck(advances["vat_on_received"]) > 0:
        report["warnings"].append(
            f"НДС с авансов {rub(advances['vat_on_received'])} стоит на 76.АВ, но счёт-фактура "
            f"на аванс не нумеровалась: реестра счетов-фактур в контуре нет, а без документа "
            f"вычет (п. 8 ст. 171 НК РФ) не подтверждён")
    if kopeck(advances["offset"]) > kopeck(advances["received"]):
        report["problems"].append({
            "code": "VAT_ADVANCE_OVER_OFFSET", "what": period,
            "why": f"зачтено авансов {rub(advances['offset'])} больше, чем получено "
                   f"{rub(advances['received'])}: вычет больше начисленного"})

    advance_vat = kopeck(advances["vat_on_received"])
    advance_off = kopeck(advances["vat_on_offset"])
    expected_credit = output_vat + kopeck(advances["in_ledger"]["accrued"]) + issued_restored
    if expected_credit != vat_account_credit:
        report["problems"].append({
            "code": "VAT_MISMATCH", "what": period,
            "why": f"НДС начисленный: по 90.03 {rub(output_vat)} + с авансов полученных "
                   f"{rub(advance_vat)} + восстановлено по выданным {rub(issued_restored)} ≠ "
                   f"оборот Кт 68.02 {rub(vat_account_credit)}: расхождение "
                   f"{rub(expected_credit - vat_account_credit)} — либо не все счета-фактуры "
                   f"зарегистрированы, либо налог начислен не тем счётом"})

    # вычет по выданным авансам: журнал и декларация должны сходиться по каждой стороне
    if issued_deducted > 0 and not report.get("problems"):
        report["warnings"].append(
            f"НДС с выданных авансов принят к вычету на {rub(issued_deducted)} (76.ВА), "
            f"но счёт-фактура поставщика на аванс в контуре не зарегистрирована: вычет "
            f"заявлен по проводке, а не по документу — до сдачи нужен реестр счетов-фактур")
    if advance_off != (kopeck(advances["vat_on_offset"])) and \
            kopeck(advances["in_ledger"]["deducted"]) > kopeck(advances["vat_on_offset"]):
        report["problems"].append({
            "code": "VAT_ADVANCE_OVER_DEDUCTION", "what": period,
            "why": f"вычет НДС с авансов в журнале "
                   f"{rub(advances['in_ledger']['deducted'])} больше налога с зачтённой части "
                   f"{rub(advances['vat_on_offset'])}: вычитается незакрытый аванс"})
    if payable < 0:
        report["warnings"].append(
            f"к возмещению {rub(-payable)}: в декларации это отдельная строка, и ФНС "
            f"запросит подтверждение — проверить до сдачи")

    if not report["rows"]:
        report["gaps"].append({"what": "реализация за период", "why": "в журнале нет подписанных "
                                "актов: декларация будет нулевой при живом объекте",
                                "ask": "подписанные заказчиком КС-2 за период"})
    report["deadline"] = (rates.get("deadlines") or {}).get("vat_return_day")
    report["ready"] = not report["problems"]
    return report


# ─────────────────────────── НДФЛ и взносы ───────────────────────────
def _payroll_period(payroll_state: dict, period: str) -> dict | None:
    return next((row for row in payroll_state.get("periods") or []
                 if row.get("period") == period), None)


def ndfl_return(payroll_state: dict, period: str, *, ledger_state: dict | None = None,
                rates: dict | None = None) -> dict:
    """6-НДФЛ: доход, вычеты, исчислено, удержано, перечислено, срок."""
    rates = rates or load_rates()
    organisation = rates.get("organisation") or {}
    report = {"kind": "ndfl6", "period": period, "organisation": organisation,
              "rows": [], "problems": [], "gaps": [], "warnings": []}
    row = _payroll_period(payroll_state, period)
    if not row:
        report["problems"].append({"code": "NO_ACCRUAL", "what": period,
                                   "why": "за этот месяц зарплата не начислена: 6-НДФЛ "
                                          "не из чего собрать"})
        return report
    total_income = total_deduction = total_tax = 0.0
    for employee in row.get("rows") or []:
        report["rows"].append({
            "tab": employee.get("tab"), "name": employee.get("name"),
            "income": employee.get("accrued"), "deduction": employee.get("deduction"),
            "tax": employee.get("ndfl"), "rate_steps": [step["rate"] for step in
                                                         employee.get("ndfl_steps") or []],
            "resident": not employee.get("ndfl_note")})
        total_income += float(employee.get("accrued") or 0)
        total_deduction += float(employee.get("deduction") or 0)
        total_tax += float(employee.get("ndfl") or 0)
    report["totals"] = {"income": round(total_income, 2), "deduction": round(total_deduction, 2),
                        "tax_withheld": round(total_tax, 2), "employees": len(report["rows"])}
    if ledger_state is not None:
        accrued_tax = turnover(ledger_state, "68.01", period, side=ledger.CREDIT)
        paid = turnover(ledger_state, "68.01", period, side=ledger.DEBIT)
        report["totals"]["tax_in_ledger"] = num(accrued_tax)
        report["totals"]["tax_paid"] = num(paid)
        report["totals"]["tax_due"] = num(accrued_tax - paid)
        if kopeck(total_tax) != kopeck(accrued_tax):
            report["problems"].append({
                "code": "NDFL_LEDGER_MISMATCH", "what": period,
                "why": f"по ведомости удержано {rub(total_tax)}, а в журнале по 68.01 "
                       f"{rub(accrued_tax)}: начисление не проведено или проведено дважды"})
    report["deadline"] = (rates.get("deadlines") or {}).get("ndfl_return_day")
    report["ready"] = not report["problems"]
    return report


def rsv(payroll_state: dict, period: str, *, ledger_state: dict | None = None,
        rates: dict | None = None) -> dict:
    """РСВ и травматизм: база, взносы по тарифам, сверка с 69.01 и 69.11."""
    rates = rates or load_rates()
    row = _payroll_period(payroll_state, period)
    report = {"kind": "rsv", "period": period,
              "organisation": rates.get("organisation") or {},
              "rows": [], "problems": [], "gaps": [], "warnings": []}
    if not row:
        report["problems"].append({"code": "NO_ACCRUAL", "what": period,
                                   "why": "взносы не из чего считать: месяца нет в ведомостях"})
        return report
    total_base = total_contrib = total_injury = 0.0
    for employee in row.get("rows") or []:
        base = employee.get("contrib_base")
        if base is None:
            base = employee.get("accrued")
        report["rows"].append({"tab": employee.get("tab"), "name": employee.get("name"),
                               "base": base,
                               "contrib": employee.get("contrib"),
                               "injury": employee.get("injury"),
                               "note": employee.get("contrib_note") or ""})
        total_base += float(employee.get("contrib_base")
                            if employee.get("contrib_base") is not None
                            else employee.get("accrued") or 0)
        total_contrib += float(employee.get("contrib") or 0)
        total_injury += float(employee.get("injury") or 0)
    report["totals"] = {"base": round(total_base, 2), "contrib": round(total_contrib, 2),
                        "injury": round(total_injury, 2), "employees": len(report["rows"])}
    contrib_rates = row.get("rates") or {}
    report["tariffs"] = {"rate": contrib_rates.get("contrib_rate"),
                         "over_rate": contrib_rates.get("over_rate"),
                         "base_limit": contrib_rates.get("base_limit"),
                         "injury": (contrib_rates.get("injury") or {}).get("rate"),
                         "injury_class": (contrib_rates.get("injury") or {}).get("class")}
    if ledger_state is not None:
        in_ledger = turnover(ledger_state, "69.01", period, side=ledger.CREDIT)
        injury_ledger = turnover(ledger_state, "69.11", period, side=ledger.CREDIT)
        report["totals"]["contrib_in_ledger"] = num(in_ledger)
        report["totals"]["injury_in_ledger"] = num(injury_ledger)
        if kopeck(total_contrib) != kopeck(in_ledger):
            report["problems"].append({
                "code": "RSV_LEDGER_MISMATCH", "what": period,
                "why": f"взносы по ведомости {rub(total_contrib)} ≠ оборот Кт 69.01 "
                       f"{rub(in_ledger)}"})
        if kopeck(total_injury) != kopeck(injury_ledger):
            report["problems"].append({
                "code": "INJURY_LEDGER_MISMATCH", "what": period,
                "why": f"травматизм по ведомости {rub(total_injury)} ≠ оборот Кт 69.11 "
                       f"{rub(injury_ledger)}"})
    conflicts = ({"note": "тариф на травматизм взят из payroll_rules.json; при расхождении "
                          "с уведомлением СФР сумма меняется пропорционально"})
    report["warnings"].append(conflicts["note"])
    report["deadline"] = (rates.get("deadlines") or {}).get("rsv_day")
    report["ready"] = not report["problems"]
    return report



# ─────────────────────────── налог на прибыль ───────────────────────────
def profit_return(state: dict, period: str, *, rates: dict | None = None,
                  closed: bool | None = None) -> dict:
    """Налог на прибыль **от реализации** по журналу: 8 % в федеральный, 17 % в региональный.

    Что считается: доходы от реализации (90.01 − 90.03, то есть без НДС) минус расходы,
    списанные на 90.02. Всё остальное — внереализационные доходы и расходы, убытки прошлых
    лет, ПБУ 18/02, — **не считается** и названо в `not_included`: цифры из воздуха хуже,
    чем честная дырка.

    Если 90.02 пуст, а затраты на 20.01 есть — значит месяц не закрыт: сумма не берётся
    «на глазок», а показывается расхождением `PROFIT_NOT_CLOSED`.
    """
    period = str(period)
    rates = rates or load_rates()
    forms = rates.get("forms") or {}
    form = forms.get("profit") or {}
    tax_rates = form.get("rates") or {}
    federal = Decimal(str(tax_rates.get("federal") or 8))
    regional = Decimal(str(tax_rates.get("regional") or 17))

    organisation = rates.get("organisation") or {}
    report = {"kind": "profit", "period": period, "organisation": organisation,
              "rows": [], "problems": [], "gaps": [], "warnings": [],
              "not_included": [
                  {"what": "внереализационные доходы и расходы",
                   "why": "в контуре нет счетов 91.01/91.02 в разрезе видов: сумма не считается"},
                  {"what": "убыток прошлых лет (приложение 4 к листу 02)",
                   "why": "нет данных о переносимых убытках — без них налог занижается или "
                          "завышается, поэтому строка не заполняется"},
                  {"what": "ПБУ 18/02 (отложенные налоги)",
                   "why": "разницы не ведутся: бухгалтерская и налоговая прибыль в контуре "
                          "совпадают по построению"},
                  {"what": "авансовые платежи и строка 210",
                   "why": "нет истории платежей по 68.04: считается налог за период, а не "
                          "доплата с учётом авансов"},
              ]}

    gross = turnover(state, "90.01", period, side=ledger.CREDIT)
    output_vat = turnover(state, "90.03", period, side=ledger.DEBIT)
    revenue = gross - output_vat
    cost = turnover(state, "90.02", period, side=ledger.DEBIT)
    costs_open = turnover(state, "20.01", period, side=ledger.DEBIT)

    by_object: dict[str, dict] = {}
    for entry in _entries(state, period, "act_signed"):
        credit = [line for line in entry.get("lines") or []
                  if str(line.get("account")) == "90.01" and line.get("side") == ledger.CREDIT]
        if not credit:
            continue
        object_name = str((credit[0].get("analytics") or {}).get("объект") or "без объекта")
        slot = by_object.setdefault(object_name, {"object": object_name, "revenue": Decimal("0"),
                                                  "cost": Decimal("0"), "acts": []})
        slot["revenue"] += sum((kopeck(line.get("amount")) for line in credit), Decimal("0")) - \
            sum((kopeck(line.get("amount")) for line in entry.get("lines") or []
                 if str(line.get("account")) == "90.03"), Decimal("0"))
        slot["acts"].append((entry.get("doc") or {}).get("number") or "")

    # расходы по объектам: 90.02 несёт аналитику объекта, пустая — распределяется общей строкой
    for entry in _entries(state, period):
        for line in entry.get("lines") or []:
            if str(line.get("account")) != "90.02" or line.get("side") != ledger.DEBIT:
                continue
            object_name = str((line.get("analytics") or {}).get("объект") or "без объекта")
            slot = by_object.setdefault(object_name, {"object": object_name,
                                                      "revenue": Decimal("0"),
                                                      "cost": Decimal("0"), "acts": []})
            slot["cost"] += kopeck(line.get("amount"))

    for slot in by_object.values():
        profit = slot["revenue"] - slot["cost"]
        report["rows"].append({"object": slot["object"], "revenue": num(slot["revenue"]),
                               "cost": num(slot["cost"]), "profit": num(profit),
                               "acts": slot["acts"],
                               "partial": bool(slot["acts"]) and slot["cost"] == 0})

    profit_total = revenue - cost
    if profit_total < 0:
        report["problems"].append({
            "code": "PROFIT_LOSS", "what": period,
            "why": f"по реализации убыток {rub(-profit_total)}: декларация по прибыли с "
                   f"убытком — отдельное решение, а не «поставим ноль»"})
    if cost == 0 and costs_open > 0:
        report["problems"].append({
            "code": "PROFIT_NOT_CLOSED", "what": period,
            "why": f"на 20.01 затрат {rub(costs_open)}, а на 90.02 списано {rub(cost)}: месяц "
                   f"не закрыт — прибыль будет завышена. Сначала закрытие, потом декларация"})

    federal_tax = kopeck(profit_total * federal / Decimal("100")) if profit_total > 0 else Decimal("0")
    regional_tax = kopeck(profit_total * regional / Decimal("100")) if profit_total > 0 \
        else Decimal("0")
    report["totals"] = {"revenue": num(revenue), "cost": num(cost), "profit": num(profit_total),
                        "federal_rate": float(federal), "regional_rate": float(regional),
                        "federal_tax": num(federal_tax), "regional_tax": num(regional_tax),
                        "tax": num(federal_tax + regional_tax)}
    report["form"] = {"knd": form.get("knd"), "order": form.get("order"),
                      "note": "форма КНД 1151006; заполнен только расчёт от реализации"}
    report["deadline"] = (rates.get("deadlines") or {}).get("profit_day")

    if not report["rows"]:
        report["gaps"].append({"what": "выручка за период",
                               "why": "в журнале нет подписанных актов",
                               "ask": "КС-2, подписанные заказчиком за период"})
    report["ready"] = not report["problems"]
    return report


# ─────────────────────────── налог на имущество ───────────────────────────
PROPERTY_REPORTING = (("Q1", "I квартал", ("01", "02", "03"), "04", "2026-04-28"),
                      ("H1", "полугодие", ("01", "02", "03", "04", "05", "06"), "07", "2026-07-28"),
                      ("9M", "9 месяцев", ("01", "02", "03", "04", "05", "06", "07", "08", "09"),
                       "10", "2026-10-28"))


def _rub_round(value: Decimal) -> Decimal:
    """Налог считается в полных рублях: 50 копеек и больше — вверх (п. 6 ст. 52 НК РФ)."""
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


BENEFITS_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "property_benefits.json"


def load_benefits(path: str | Path | None = None) -> dict:
    path = Path(path) if path else BENEFITS_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "pto-agent/property-benefits@1":
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} "
                         f"не pto-agent/property-benefits@1")
    return payload


def _benefit_of(asset: dict, catalogue: dict) -> tuple[dict | None, str]:
    """Можно ли применить льготу объекта. Возвращает (льгота|None, причина отказа)."""
    code = str(asset.get("benefit") or "").strip()
    if not code:
        return None, ""
    entry = next((row for row in catalogue.get("benefits") or []
                  if str(row.get("code")) == code), None)
    if not entry:
        return None, (f"льготы «{code}» нет в справочнике: непроверенный код не применяется")
    basis = asset.get("benefit_basis") or {}
    if not (basis.get("number") and basis.get("date")):
        return None, (f"льгота «{code}» заявлена, но основание не указано: нужен номер и дата "
                      f"закона субъекта/документа")
    if not entry.get("confirmed"):
        return None, (f"льгота «{code}» не подтверждена в справочнике (confirmed: false): "
                      f"сумма в отчёт идёт, но налог считается без неё")
    return entry, ""


def property_tax(assets_payload: dict, year: str, *, rates: dict | None = None,
                 benefits: dict | None = None) -> dict:
    """Налог на имущество организаций: авансы за I квартал, полугодие, 9 месяцев и налог за год.

    База — регистр недвижимости (`data/demo/property/assets_2026.json`): в контуре нет учёта
    основных средств (счетов 01/02), поэтому стоимости приходят файлом, а не «примерно из
    20.01». Что считается:

    * **среднегодовая стоимость** — средняя за отчётный период по п. 4 ст. 376 НК РФ:
      (сумма остаточных стоимостей на 1-е число каждого месяца периода + на 1-е число
      следующего месяца) / (число месяцев периода + 1); аванс = средняя × ставка / 4;
    * **кадастровая стоимость** — кадастр на 01.01 × ставка / 4; по таким объектам ФНС
      считает налог сама и присылает сообщение, поэтому расчёт контура — сверка, а не замена.

    Ставки — из `tax_rates.json → property` (Ленинградская область: 2,2 % среднегодовая,
    2,0 % кадастр). Движимое имущество не облагается с 2019 года и в базу не идёт.
    Если стоимость на какую-то дату отсутствует — аванс **не считается**: код
    `PROPERTY_NO_DATA`, потому что средняя «по имеющимся месяцам» — это другая цифра.
    """
    rates = rates or load_rates()
    benefits = benefits if benefits is not None else load_benefits()
    section = rates.get("property") or {}
    rate_average = Decimal(str((section.get("rates") or {}).get("average") or 0))
    rate_cadastral = Decimal(str((section.get("rates") or {}).get("cadastral") or 0))

    report = {"kind": "property", "period": year, "organisation": rates.get("organisation") or {},
              "rows": [], "problems": [], "gaps": [], "warnings": [],
              "totals": {}, "advance_payments": [],
              "deadlines": (rates.get("deadlines") or {}).get("property")
              or {}, "rates": {"average": float(rate_average), "cadastral": float(rate_cadastral),
                               "region": section.get("region"),
                               "source": section.get("rates_source") or []},
              "benefits": {"catalogue": benefits.get("benefits") or [], "applied": [],
                           "not_applied": [],
                           "note": "льгота применяется только по подтверждённому основанию; "
                                   "неприменённая показывается суммой, которую она дала бы",
                           "not_implemented": list(benefits.get("not_implemented") or [])},
              "not_included": [
                  {"what": "основные средства в журнале (счета 01 и 02)",
                   "why": "в контуре их нет: амортизация не начисляется, остаточная стоимость "
                          "приходит регистром из бухгалтерии"},
                  {"what": "льготы и пониженные ставки субъекта",
                   "why": "в справочнике только общие ставки Ленинградской области; льгота "
                          "конкретной организации не проверяется — её подтверждают документы"},
                  {"what": "налог по объектам, которых нет в регистре",
                   "why": "регистр — единственный источник; полноту сверяют с выпиской ЕГРН"},
                  {"what": "выгрузка декларации в формате ФНС 5.13",
                   "why": "форма подтверждена (КНД 1152026), но XML со схемой не сверялся"},
              ]}

    assets = list((assets_payload or {}).get("assets") or [])
    if not assets:
        report["gaps"].append({"what": "регистр недвижимости",
                               "why": "файл активов пуст: налог считать не из чего",
                               "ask": "выгрузка из бухучёта: объект, остаточная стоимость на 1-е "
                                      "число каждого месяца, кадастровая стоимость"})
        report["totals"] = {"advances": 0.0, "year": 0.0}
        report["ready"] = False
        return report

    def value_on(asset: dict, day: str) -> Decimal | None:
        raw = (asset.get("residual") or {}).get(day)
        if raw in (None, ""):
            return None
        return kopeck(raw)

    taxable: list[dict] = []
    for asset in assets:
        kind = str(asset.get("kind") or "")
        name = str(asset.get("name") or "без названия")
        base = str(asset.get("base") or "average")
        if kind != "недвижимость":
            # движимое с 2019 года не облагается: оно не «теряется», а объяснено
            report["rows"].append({"name": name, "inventory": asset.get("inventory") or "",
                                   "base": base, "kind": kind, "taxable": False,
                                   "why": "движимое имущество не облагается (п. 1 ст. 374 НК РФ)",
                                   "average": None, "advances": {}, "year": 0.0})
            continue
        if base == "cadastral":
            cadastral = kopeck(asset.get("cadastral_value"))
            if cadastral <= 0:
                report["problems"].append({
                    "code": "PROPERTY_NO_DATA", "what": name,
                    "why": "база кадастровая, а кадастровой стоимости нет: сумму не считаем — "
                           "она берётся из ЕГРН, а не «примерно»"})
                continue
            benefit, why_not = _benefit_of(asset, benefits)
            year_base = _rub_round(cadastral * rate_cadastral / Decimal("100"))
            advances = {code: _rub_round(cadastral * rate_cadastral
                                         / Decimal("100") / Decimal("4"))
                        for code, _, _, _, _ in PROPERTY_REPORTING}
            _remember_benefit(report, asset, name, benefit, why_not, advances, year_base, benefits)
            years_base = {code: cadastral for code, _, _, _, _ in PROPERTY_REPORTING}
            advances, year_base = _apply_benefit(benefit, advances, year_base)
            taxable.append({"asset": asset, "name": name, "base": base, "cadastral": cadastral,
                            "advances": advances, "bases": years_base, "year": year_base})
            continue

        # среднегодовая база: проверяем, что есть стоимость на все нужные даты
        months_needed = [f"{year}-{month}-01" for month in
                         ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12")]
        months_needed.append(f"{int(year) + 1}-01-01")
        missing = [day for day in months_needed if value_on(asset, day) is None]
        if missing:
            report["problems"].append({
                "code": "PROPERTY_NO_DATA", "what": name,
                "why": f"нет остаточной стоимости на {', '.join(missing[:4])}"
                       f"{' и ещё ' + str(len(missing) - 4) if len(missing) > 4 else ''}: "
                       f"средняя за период не считается по неполному ряду — она была бы другой "
                       f"цифрой, а не той же"})
            continue
        benefit, why_not = _benefit_of(asset, benefits)
        advances: dict[str, Decimal] = {}
        bases: dict[str, Decimal] = {}
        for code, _, months, next_month, _ in PROPERTY_REPORTING:
            days = [f"{year}-{month}-01" for month in months] + [f"{year}-{next_month}-01"]
            total = sum((value_on(asset, day) for day in days), Decimal("0"))
            average = total / Decimal(len(days))
            advances[code] = _rub_round(average * rate_average / Decimal("100") / Decimal("4"))
            bases[code] = average
        annual_days = months_needed
        average_year = sum((value_on(asset, day) for day in annual_days), Decimal("0")) \
            / Decimal(len(annual_days))
        year_amount = _rub_round(average_year * rate_average / Decimal("100"))
        _remember_benefit(report, asset, name, benefit, why_not, advances, year_amount, benefits)
        advances, year_amount = _apply_benefit(benefit, advances, year_amount)
        taxable.append({"asset": asset, "name": name, "base": base,
                        "average": average_year, "advances": advances, "bases": bases,
                        "year": year_amount})

    advance_totals: dict[str, Decimal] = {code: Decimal("0") for code, *_ in PROPERTY_REPORTING}
    base_totals: dict[str, Decimal] = {code: Decimal("0") for code, *_ in PROPERTY_REPORTING}
    year_total = Decimal("0")
    for row in taxable:
        for code, amount in row["advances"].items():
            advance_totals[code] += amount
        for code, value in (row.get("bases") or {}).items():
            base_totals[code] += value
        year_total += row["year"]
        report["rows"].append({
            "name": row["name"], "inventory": row["asset"].get("inventory") or "",
            "base": row["base"], "kind": "недвижимость", "taxable": True,
            "average": num(row.get("average")) if row.get("average") is not None else None,
            "cadastral": num(row.get("cadastral")) if row.get("cadastral") is not None else None,
            "advances": {code: num(amount) for code, amount in row["advances"].items()},
            "year": num(row["year"])})

    for code, title, _, _, deadline in PROPERTY_REPORTING:
        report["advance_payments"].append({
            "code": code, "title": title, "amount": num(advance_totals[code]),
            "deadline": deadline,
            "operation": f"аванс за {title}",
            "basis": num(base_totals[code])})
    report["totals"] = {
        "advances_q1": num(advance_totals["Q1"]), "advances_h1": num(advance_totals["H1"]),
        "advances_9m": num(advance_totals["9M"]),
        "advances_total": num(sum(advance_totals.values(), Decimal("0"))),
        "year": num(year_total),
        "to_pay_year": num(year_total - sum(advance_totals.values(), Decimal("0"))),
        "objects": len(taxable),
    }
    if not taxable:
        report["gaps"].append({"what": "объекты, облагаемые налогом",
                               "why": "в регистре нет ни одного объекта с полными данными",
                               "ask": "остаточная стоимость по месяцам и кадастровая стоимость"})
    report["ready"] = not report["problems"]
    return report


def _apply_benefit(entry: dict | None, advances: dict[str, Decimal],
                   year_amount: Decimal) -> tuple[dict[str, Decimal], Decimal]:
    """Применить подтверждённую льготу. Освобождение — нули; иные виды эффекта не считаются."""
    effect = (entry or {}).get("effect") or {}
    if entry is None or effect.get("type") != "exempt":
        return advances, year_amount
    return {code: Decimal("0") for code in advances}, Decimal("0")


def _remember_benefit(report: dict, asset: dict, name: str, entry: dict | None, why: str,
                      advances: dict[str, Decimal], year_amount: Decimal,
                      catalogue: dict | None = None) -> None:
    """Записать, применилась ли льгота: неприменённая — это не «нет льготы», а «не подтверждена»."""
    code = str(asset.get("benefit") or "").strip()
    if not code:
        return
    block = report["benefits"]
    basis = asset.get("benefit_basis") or {}
    # заявленную льготу ищем и по неподтверждённой записи: «сколько дала бы» — это тоже цифра
    declared = entry or next((row for row in (catalogue or {}).get("benefits") or []
                              if str(row.get("code")) == code), None)
    # экономия — это налог за год: авансы внутри года, складывать их с годом нельзя
    saving_year = _benefit_saving(declared, year_amount)
    saving_advances = (kopeck(sum(advances.values(), Decimal("0")))
                       if _benefit_saving(declared, Decimal("1")) else Decimal("0"))
    if entry is None:
        block["not_applied"].append({
            "code": code, "asset": name, "why": why,
            "basis": f"{basis.get('number')} от {basis.get('date')}" if basis.get("number") else "",
            "would_save_year": num(saving_year),
            "would_save_advances": num(saving_advances),
            "effect": (declared or {}).get("effect"),
            "confirmed": bool((declared or {}).get("confirmed"))})
        return
    block["applied"].append({"code": code, "asset": name, "title": entry.get("title"),
                             "basis": f"{basis.get('number')} от {basis.get('date')}",
                             "effect": entry.get("effect"),
                             "would_save_year": num(saving_year)})


def _benefit_saving(entry: dict | None, year_amount: Decimal) -> Decimal:
    """Сколько дала бы льгота за год. Только для «освобождения»: долю считать не из чего."""
    effect = (entry or {}).get("effect") or {}
    if entry is None or effect.get("type") != "exempt":
        # без подтверждённого типа эффекта экономия не считается — иначе это выдуманная цифра
        return Decimal("0")
    return kopeck(year_amount)


def post_property_tax(state: dict, report: dict, *, code: str = "Q1", date: str | None = None,
                      mode: str | None = None, chart: dict | None = None,
                      rules: dict | None = None) -> dict:
    """Провести аванс (или налог за год) по имуществу: Дт 91.02 Кт 68.08.

    Сумма **берётся из готового расчёта** `property_tax` и не пересчитывается: проводка —
    это решение человека, а не место, где появляются цифры. Дата — по сроку платежа, если
    не передана явно. Повторный вызов даёт дубль, а не второе начисление.
    """
    from . import ledger as accounting

    chart = chart or accounting.load_chart()
    rules = rules if rules is not None else accounting.load_rules()
    year = str(report.get("period") or "")
    if code == "YEAR":
        amount = kopeck((report.get("totals") or {}).get("to_pay_year"))
        deadline = str(((report.get("deadlines") or {}).get("tax") or ""))
        title = f"налог за {year} (доплата за год)"
        default_date = f"{int(year) + 1}-02-28"
    else:
        row = next((item for item in report.get("advance_payments") or []
                    if item.get("code") == code), None)
        if not row:
            return {"status": accounting.STATUS_REJECTED, "posted": 0,
                    "errors": [{"code": "PROPERTY_NO_PERIOD", "what": code,
                                "why": f"в расчёте нет аванса {code}: есть "
                                       f"{', '.join(item.get('code') for item in report.get('advance_payments') or []) or '—'}"}],
                    "gaps": [], "warnings": []}
        amount = kopeck(row.get("amount"))
        deadline = str(row.get("deadline") or "")
        title = f"{row.get('operation')} за {year}"
        default_date = deadline or f"{year}-12-31"
    if amount <= 0:
        return {"status": accounting.STATUS_REJECTED, "posted": 0,
                "errors": [{"code": "PROPERTY_ZERO", "what": code,
                            "why": "сумма к начислению нулевая: проводить нечего"}],
                "gaps": [], "warnings": []}
    outcome = accounting.post_entry(
        state, event="property_tax", date=str(date or default_date),
        doc={"number": f"налог на имущество: {title}", "date": str(date or default_date),
             "kind": "расчёт налога"},
        lines=[{"account": "91.02", "side": accounting.DEBIT, "amount": num(amount),
                "analytics": {}},
               {"account": "68.08", "side": accounting.CREDIT, "amount": num(amount),
                "analytics": {}}],
        memo=(f"{title}: {rub(amount)} отнесено на прочие расходы; "
              f"срок уплаты — {deadline or 'по расчёту'}"),
        rule="tax.property", key=f"property|{year}|{code}|{num(amount):.2f}",
        chart=chart, rules=rules, mode=mode or accounting.MODE_POST)
    return {**outcome, "posted": 1 if outcome.get("status") == accounting.STATUS_OK else 0,
            "amount": num(amount), "date": str(date or default_date)}

# ─────────────────────────── XML ───────────────────────────
def _tag(parent: ET.Element, name: str, value=None, **attributes) -> ET.Element:
    node = ET.SubElement(parent, name, {key: str(item) for key, item in attributes.items()})
    if value is not None:
        node.text = str(value)
    return node


def to_xml(report: dict) -> str:
    """Собрать XML отчёта. Пространство имён наше: это не формат ФНС (см. «не сделано»)."""
    kind = str(report.get("kind") or "")
    if kind not in KINDS:
        raise ValueError(f"вид отчёта {kind!r} не поддержан; есть: {', '.join(KINDS)}")
    ET.register_namespace("", NS)
    root = ET.Element(f"{{{NS}}}Отчёт", {"Вид": kind, "Период": str(report.get("period") or ""),
                                        "Программа": "ИИ-агент ПТО", "Схема": SCHEMA,
                                        "НеФорматФНС": "1"})
    organisation = report.get("organisation") or {}
    org = _tag(root, "Организация")
    _tag(org, "Наименование", organisation.get("name") or "")
    _tag(org, "ИНН", organisation.get("inn") or "")
    _tag(org, "КПП", organisation.get("kpp") or "")
    _tag(org, "ОКВЭД", organisation.get("okved") or "")
    totals = _tag(root, "Итоги")
    for key, value in (report.get("totals") or {}).items():
        _tag(totals, "Показатель", value, Код=key)
    rows = _tag(root, "Строки")
    for row in report.get("rows") or []:
        node = _tag(rows, "Строка")
        for key, value in row.items():
            if isinstance(value, (list, tuple)):
                value = "; ".join(str(item) for item in value)
            _tag(node, "Показатель", value, Код=key)
    if report.get("advances"):
        block = _tag(root, "АвансыНДС", Ставка=report["advances"].get("rate"))
        _tag(block, "Получено", report["advances"].get("received"))
        _tag(block, "НДСНачислено", report["advances"].get("vat_on_received"))
        _tag(block, "Зачтено", report["advances"].get("offset"))
        _tag(block, "НДСКВычету", report["advances"].get("vat_on_offset"))
        for row in report["advances"].get("documents") or []:
            _tag(block, "Документ", row.get("amount"), Номер=row.get("document") or "",
                 Дата=row.get("date") or "", Контрагент=row.get("counterparty") or "")
        for row in report["advances"].get("lines") or []:
            _tag(block, "Строка", row.get("amount"), Номер=row.get("line") or "",
                 Что=row.get("what") or "", НаПроверку="1" if row.get("verify") else "0")
    if report.get("advance_payments"):
        block = _tag(root, "АвансыПоИмуществу")
        for row in report["advance_payments"]:
            _tag(block, "Аванс", row.get("amount"), Период=row.get("title") or "",
                 Срок=row.get("deadline") or "", База=row.get("basis"))
    if report.get("not_included"):
        block = _tag(root, "НеСчитается")
        for item in report["not_included"]:
            _tag(block, "Пункт", item.get("why"), Что=item.get("what") or "")
    if report.get("rate_audit"):
        audit = _tag(root, "ПроверкаСтавок")
        for row in report["rate_audit"]:
            _tag(audit, "Акт", row.get("vat_expected"), Документ=row.get("document") or "",
                 Дата=row.get("date") or "", СтавкаФакт=row.get("rate_implied"),
                 СтавкаЗакон=row.get("rate_lawful"), Расхождение=row.get("gap"))
    problems = _tag(root, "Расхождения")
    for item in report.get("problems") or []:
        _tag(problems, "Расхождение", item.get("why"), Код=item.get("code") or "")
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def validate(report: dict, *, rates: dict | None = None) -> list[dict]:
    """Проверить отчёт перед сдачей: реквизиты, суммы, готовность."""
    rates = rates or load_rates()
    problems: list[dict] = []
    organisation = report.get("organisation") or {}
    if not inn_ok(organisation.get("inn")):
        problems.append({"code": "BAD_INN", "what": organisation.get("inn"),
                         "why": "ИНН не проходит проверку контрольной суммы"})
    if not kpp_ok(organisation.get("kpp")):
        problems.append({"code": "BAD_KPP", "what": organisation.get("kpp"),
                         "why": "КПП не той структуры: 4 цифры + буква + 5 цифр"})
    period_text = str(report.get("period") or "")
    if report.get("kind") == "property":
        # имущество считается за год: формат периода — ГГГГ, а не ГГГГ-ММ
        if not (len(period_text) == 4 and period_text.isdigit()):
            problems.append({"code": "BAD_PERIOD", "what": period_text,
                             "why": "налог на имущество считается за год: период задаётся как ГГГГ"})
    elif not period_text.count("-") == 1:
        problems.append({"code": "BAD_PERIOD", "what": period_text,
                         "why": "период задаётся как ГГГГ-ММ"})
    totals = report.get("totals") or {}
    if report.get("kind") == "vat":
        expected = kopeck(totals.get("vat_charged")) - kopeck(totals.get("vat_deducted"))
        # НДС с авансов — отдельные строки декларации: без них «к уплате» не сойдётся
        expected += kopeck(totals.get("vat_on_advances")) \
            - kopeck(totals.get("vat_on_advances_offset"))
        if abs(expected - kopeck(totals.get("payable"))) > EPS:
            problems.append({"code": "VAT_TOTALS", "what": report.get("period"),
                             "why": "к уплате не равно начислено минус вычеты"})
    if report.get("kind") == "profit":
        profit = kopeck(totals.get("profit"))
        if abs(profit - (kopeck(totals.get("revenue")) - kopeck(totals.get("cost")))) > EPS:
            problems.append({"code": "PROFIT_TOTALS", "what": report.get("period"),
                             "why": "прибыль не равна доходам минус расходы"})
        expected_tax = kopeck(kopeck(totals.get("federal_tax")) + kopeck(totals.get("regional_tax")))
        if abs(expected_tax - kopeck(totals.get("tax"))) > EPS:
            problems.append({"code": "PROFIT_TAX_SPLIT", "what": report.get("period"),
                             "why": "налог не равен сумме федеральной и региональной частей"})
        split = kopeck(totals.get("federal_rate")) + kopeck(totals.get("regional_rate"))
        if split != Decimal("25"):
            problems.append({"code": "PROFIT_RATE_SUM", "what": f"{split} %",
                             "why": "ставки прибыли в сумме не 25 % (8 % + 17 %): проверить, "
                                    "нет ли региональной льготы"})
    if report.get("kind") == "property":
        totals = report.get("totals") or {}
        advances = kopeck(totals.get("advances_q1")) + kopeck(totals.get("advances_h1")) \
            + kopeck(totals.get("advances_9m"))
        if abs(advances - kopeck(totals.get("advances_total"))) > EPS:
            problems.append({"code": "PROPERTY_TOTALS", "what": report.get("period"),
                             "why": "сумма авансов не равна сумме трёх платежей"})
        if kopeck(totals.get("to_pay_year")) + advances != kopeck(totals.get("year")):
            problems.append({"code": "PROPERTY_YEAR", "what": report.get("period"),
                             "why": "налог за год ≠ авансы + доплата за год"})
        if kopeck(totals.get("year")) < 0:
            problems.append({"code": "PROPERTY_NEGATIVE", "what": report.get("period"),
                             "why": "налог отрицательный: проверить базу"})
    if report.get("kind") == "ndfl6":
        if kopeck(totals.get("tax_withheld")) < 0:
            problems.append({"code": "NDFL_NEGATIVE", "what": report.get("period"),
                             "why": "удержанный НДФЛ отрицательный"})
    problems.extend({"code": item.get("code"), "what": item.get("what"),
                     "why": item.get("why")} for item in report.get("problems") or [])
    return problems


def to_markdown(report: dict) -> str:
    kind = str(report.get("kind") or "")
    lines = [f"# {KINDS.get(kind, kind)} за {report.get('period')}", ""]
    organisation = report.get("organisation") or {}
    lines.append(f"Организация: {organisation.get('name')} · ИНН {organisation.get('inn')} · "
                 f"КПП {organisation.get('kpp')}")
    lines.append("")
    if kind == "vat":
        lines.append("| объект | реализация без НДС | НДС | акты |")
        lines.append("|---|---:|---:|---|")
        for row in report.get("rows") or []:
            lines.append(f"| {row['object']} | {rub(row['sales'])} | {rub(row['vat'])} | "
                         f"{', '.join(row.get('acts') or [])} |")
        totals = report.get("totals") or {}
        lines.append("")
        lines.append(f"Начислено {rub(totals.get('vat_charged'))}, к вычету "
                     f"{rub(totals.get('vat_deducted'))}, к уплате {rub(totals.get('payable'))}")
    elif kind == "profit":
        lines.append("| объект | доходы без НДС | расходы | прибыль | акты |")
        lines.append("|---|---:|---:|---:|---|")
        for row in report.get("rows") or []:
            mark = " (месяц не закрыт)" if row.get("partial") else ""
            lines.append(f"| {row['object']}{mark} | {rub(row['revenue'])} | "
                         f"{rub(row['cost'])} | {rub(row['profit'])} | "
                         f"{', '.join(row.get('acts') or [])} |")
        totals = report.get("totals") or {}
        lines.append("")
        lines.append(f"Прибыль от реализации {rub(totals.get('profit'))} · налог "
                     f"{rub(totals.get('federal_tax'))} в федеральный "
                     f"({totals.get('federal_rate')} %) + "
                     f"{rub(totals.get('regional_tax'))} в региональный "
                     f"({totals.get('regional_rate')} %) = {rub(totals.get('tax'))}")
        if report.get("not_included"):
            lines.append("")
            lines.append("## Не считается")
            for item in report["not_included"]:
                lines.append(f"- **{item.get('what')}**: {item.get('why')}")
    elif kind == "property":
        benefits = report.get("benefits") or {}
        if benefits.get("applied") or benefits.get("not_applied"):
            lines.append("")
            lines.append("## Льготы")
            for row in benefits.get("applied") or []:
                lines.append(f"- **{row['code']}** · {row['asset']}: применена "
                             f"({row.get('basis')}), годовая экономия "
                             f"{rub(row.get('would_save_year'))}")
            for row in benefits.get("not_applied") or []:
                lines.append(f"- **{row['code']}** · {row['asset']}: НЕ применена — {row['why']}; "
                             f"налог без льготы, справочно экономия за год "
                             f"{rub(row.get('would_save_year'))}, "
                             f"по авансам {rub(row.get('would_save_advances'))}")
        totals = report.get("totals") or {}
        lines.append(f"Регион ставок: {(report.get('rates') or {}).get('region')} · "
                     f"среднегодовая {(report.get('rates') or {}).get('average')} % · "
                     f"кадастровая {(report.get('rates') or {}).get('cadastral')} %")
        lines.append("")
        lines.append("| объект | база | средняя/кадастр | I кв. | полугодие | 9 мес. | за год |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for row in report.get("rows") or []:
            if not row.get("taxable"):
                lines.append(f"| {row['name']} | {row['base']} | — | — | — | — | "
                             f"не облагается: {row.get('why')} |")
                continue
            base_value = row.get("cadastral") if row["base"] == "cadastral" else row.get("average")
            adv = row.get("advances") or {}
            lines.append(f"| {row['name']} | {row['base']} | {rub(base_value)} | "
                         f"{rub(adv.get('Q1'))} | {rub(adv.get('H1'))} | {rub(adv.get('9M'))} | "
                         f"{rub(row.get('year'))} |")
        lines.append("")
        lines.append(f"Авансы: I кв. {rub(totals.get('advances_q1'))}, полугодие "
                     f"{rub(totals.get('advances_h1'))}, 9 мес. {rub(totals.get('advances_9m'))} · "
                     f"за год {rub(totals.get('year'))} · к доплате за год "
                     f"{rub(totals.get('to_pay_year'))}")
        deadlines = report.get("deadlines") or {}
        if deadlines.get("advances"):
            lines.append(f"Сроки: {', '.join(deadlines['advances'])} (авансы), "
                         f"{deadlines.get('declaration')} (декларация). "
                         f"{deadlines.get('notice')}")
    elif kind == "ndfl6":
        lines.append("| таб. | сотрудник | доход | вычет | НДФЛ | ставки |")
        lines.append("|---|---|---:|---:|---:|---|")
        for row in report.get("rows") or []:
            steps = "/".join(str(step) for step in row.get("rate_steps") or [])
            lines.append(f"| {row.get('tab')} | {row.get('name')} | {rub(row.get('income'))} | "
                         f"{rub(row.get('deduction'))} | {rub(row.get('tax'))} | {steps} |")
        totals = report.get("totals") or {}
        lines.append("")
        lines.append(f"Доход {rub(totals.get('income'))}, вычеты {rub(totals.get('deduction'))}, "
                     f"исчислено и удержано {rub(totals.get('tax_withheld'))}, сотрудников "
                     f"{totals.get('employees')}")
        if totals.get("tax_in_ledger") is not None:
            lines.append(f"В журнале по 68.01 начислено {rub(totals.get('tax_in_ledger'))}, "
                         f"уплачено {rub(totals.get('tax_paid'))}, к доплате "
                         f"{rub(totals.get('tax_due'))}")
    else:
        lines.append("| таб. | сотрудник | база | взносы | травматизм |")
        lines.append("|---|---|---:|---:|---:|")
        for row in report.get("rows") or []:
            base = row.get("base") or row.get("income")
            lines.append(f"| {row.get('tab')} | {row.get('name')} | {rub(base)} | "
                         f"{rub(row.get('contrib') or row.get('tax'))} | "
                         f"{rub(row.get('injury') or 0)} |")
        totals = report.get("totals") or {}
        lines.append("")
        counts = {"employees"}
        lines.append("; ".join(
            f"{key}: {int(value) if key in counts else rub(value)}"
            for key, value in totals.items() if key not in ("income", "deduction")))
    if report.get("advances"):
        adv = report["advances"]
        lines.append("")
        lines.append("## НДС с авансов")
        lines.append("")
        lines.append("| строка | что | сумма | источник |")
        lines.append("|---|---|---:|---|")
        for row in adv.get("lines") or []:
            flag = " ⚠" if row.get("verify") else ""
            lines.append(f"| {row.get('line')}{flag} | {row.get('what')} | "
                         f"{rub(row.get('amount'))} | {row.get('source')} |")
        lines.append("")
        lines.append(f"Получено авансов {rub(adv.get('received'))} · зачтено "
                     f"{rub(adv.get('offset'))} · к уплате с авансов "
                     f"{rub(adv.get('to_pay'))} (ставка {adv.get('rate')} %).")
        lines.append(f"{adv.get('note')}")
    if report.get("rate_audit"):
        lines.append("")
        lines.append("| документ | дата | база | НДС | ставка в журнале | законная | расхождение |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for row in report["rate_audit"]:
            lines.append(f"| {row['document']} | {row['date']} | {rub(row['base'])} | "
                         f"{rub(row['vat'])} | {row['rate_implied']} % | "
                         f"{row['rate_lawful']} % | {rub(row['gap'])} |")
    if report.get("problems"):
        lines.append("")
        lines.append("## Расхождения")
        for item in report["problems"]:
            lines.append(f"- **{item.get('code')}** · {item.get('what')}: {item.get('why')}")
    lines.append("")
    lines.append("Что не сделано: " + "; ".join((load_rates().get("not_implemented") or [])[:3]))
    return "\n".join(lines) + "\n"
