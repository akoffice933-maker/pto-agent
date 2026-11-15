# -*- coding: utf-8 -*-
"""Счета-фактуры и книга продаж: нумерация, реестр, сверка с журналом.

Зачем модуль. До круга 33 в контуре не было ни реестра счетов-фактур, ни нумерации, ни
книги продаж: бланк `forms.build_advance_invoice` печатался с пустым номером, а суммы
декларации считались прямо из оборотов. Это работало, но оставляло дырку — **вычет по
счёту-фактуре нельзя было подтвердить документом**, а декларация не сверялась с книгой.

Что здесь есть:

* `next_number` — следующий номер по правилам организации (`invoice_rules.json`):
  сквозная нумерация с 1 января, номер не переиспользуется;
* `register` — регистрация выставленного счёта-фактуры: проверяет дату основания, ставку,
  контрагента и срок 5 календарных дней (п. 3 ст. 168 НК РФ), считает НДС из базы и ставки;
* `auto_issue` — собрать счета-фактуры из журнала: на отгрузку по подписанным актам, на
  аванс по поступлениям на 62.02. Ключ идемпотентности — документ-основание, повтор не
  задваивает номер;
* `sales_book` — книга продаж за период и её сверка с начислением в журнале
  (`90.03` + НДС с авансов на `76.АВ`).

Чего нет и почему — в `invoice_rules.json → not_implemented`: книги покупок (нет реестра
входящих СФ), корректировочных счетов-фактур, журнала учёта для посредников, выгрузки книг
в формате ФНС.
"""
from __future__ import annotations

import json
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import ledger, tax

SCHEMA = "pto-agent/invoices@1"
RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "invoice_rules.json"
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
    if payload.get("schema") != "pto-agent/invoice-rules@1":
        raise ValueError(f"правила счетов-фактур: схема {payload.get('schema')!r} не та")
    return payload


# ─────────────────────────── состояние ───────────────────────────
def new_state(*, object_name: str = "", organization: dict | None = None) -> dict:
    return {"schema": SCHEMA, "object": object_name,
            "organization": organization or {}, "issued": [], "keys": []}


def load(path: str | Path) -> dict:
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    state.setdefault("issued", [])
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

    invoice = {"number": str(number_value), "date": date, "kind": kind,
               "base_number": base_number, "base_date": base_date,
               "counterparty": str(counterparty), "contract": str(contract or ""),
               "object": object_name or str(state.get("object") or ""),
               "net": num(net_value), "rate": float(rate_value), "vat": num(vat_value),
               "gross": num(gross), "late_days": lag, "late": bool(late),
               "source": source, "key": key,
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
        rows.append({"number": invoice.get("number"), "date": invoice.get("date"),
                     "kind": invoice.get("kind"),
                     "kind_title": KINDS.get(str(invoice.get("kind")), ""),
                     "base_number": invoice.get("base_number"),
                     "counterparty": invoice.get("counterparty"),
                     "contract": invoice.get("contract"),
                     "object": invoice.get("object"),
                     "net": invoice.get("net"), "rate": invoice.get("rate"),
                     "vat": invoice.get("vat"), "gross": invoice.get("gross"),
                     "late": invoice.get("late"), "late_days": invoice.get("late_days")})
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


def to_markdown(report: dict) -> str:
    """Книга продаж текстом: графы названы, «на проверку» отмечено."""
    lines = [f"# {(report.get('rules') or {}).get('sales_book', {}).get('title') or 'Книга продаж'} "
             f"за {report.get('period')}", ""]
    verify = (report.get("rules") or {}).get("sales_book", {}).get("verify")
    if verify:
        lines.append("> Графы и их порядок помечены «на проверку»: "
                     + str((report.get("rules") or {}).get("sales_book", {}).get("verify_why")))
        lines.append("")
    lines.append("| № СФ | дата | вид | основание | покупатель | без НДС | ставка | НДС | с НДС |")
    lines.append("|---|---|---|---|---|---:|---:|---:|---:|")
    for row in report.get("rows") or []:
        mark = " ⏰" if row.get("late") else ""
        lines.append(f"| {row['number']}{mark} | {row['date']} | {row['kind_title']} | "
                     f"{row['base_number']} | {row['counterparty']} | {rub(row['net'])} | "
                     f"{row['rate']:g} % | {rub(row['vat'])} | {rub(row['gross'])} |")
    totals = report.get("totals") or {}
    lines.append("")
    lines.append(f"Счетов-фактур {totals.get('invoices')} · НДС по книге {rub(totals.get('vat'))} "
                 f"(отгрузка {rub(totals.get('shipped_vat'))}, авансы "
                 f"{rub(totals.get('advance_vat'))}) · начислено в журнале "
                 f"{rub(totals.get('journal_vat'))} · разница {rub(totals.get('difference'))}")
    if report.get("problems"):
        lines.append("")
        lines.append("## Расхождения")
        for item in report["problems"]:
            lines.append(f"- **{item.get('code')}** · {item.get('what')}: {item.get('why')}")
    if report.get("not_implemented"):
        lines.append("")
        lines.append("## Не сделано")
        for text in report["not_implemented"]:
            lines.append(f"- {text}")
    return "\n".join(lines)
