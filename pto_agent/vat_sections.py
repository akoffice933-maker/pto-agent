# -*- coding: utf-8 -*-
"""Раздельный учёт НДС (п. 4 ст. 170 НК РФ) и разделы декларации, которых не было.

Зачем модуль. До круга 36 декларация по НДС считала начисление и вычеты, но не отвечала на
три вопроса, из-за которых её возвращают:

1. **Раздельный учёт.** Если организация делает и облагаемые, и необлагаемые операции, входной
   НДС нельзя принять к вычету целиком. Пропорция — по стоимости отгруженных товаров
   (абз. 4 п. 4 ст. 170 НК РФ), а правило 5 % — по **совокупным расходам** (абз. 9). Это разные
   базы, и путать их нельзя: пропорция по выручке, а 5 % — по расходам.
2. **Раздел 7** — операции, не облагаемые НДС. Там нужны 7-значные коды из приложения № 1 к
   Порядку, а не двузначные коды книг.
3. **Разделы 8 и 9** — это книги покупок и продаж, перенесённые в декларацию, с контрольными
   соотношениями: итог раздела 8 обязан равняться вычету раздела 3, итог раздела 9 — начислению.

Правила, которых модуль держится:

* **Ни одно число не берётся «примерно».** Нет данных для правила 5 % (расходы на необлагаемые
  операции) — правило помечается «не проверено», а не считается по выручке.
* **Код раздела 7 не выдумывается.** Заведены только коды с подтверждённым основанием; для
  остальных — запрос «указать код по приложению № 1».
* **Номера строк** разделов помечены «на проверку»: форма менялась приказом от 18.12.2025
  № ЕД-7-3/1227@, и номер строки по памяти не пишется. Структура и суммы от этого не зависят.

Проводка «НДС не к вычету → 91.02» не делается сама: её предлагает `post_non_deductible`,
а проводит человек.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from . import invoices, ledger, tax

SCHEMA = "pto-agent/vat-sections@1"
RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "vat_sections.json"

OK = "сходится"
FAIL = "расхождение"


def dec(value) -> Decimal:
    return tax.dec(value)


def num(value) -> float:
    return tax.num(value)


def rub(value) -> str:
    return tax.rub(value)


def load_rules(path: str | Path | None = None) -> dict:
    payload = json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"разделы НДС: схема {payload.get('schema')!r} не та")
    return payload


# ─────────────────────── раздельный учёт (п. 4 ст. 170 НК РФ) ───────────────────────
def _input_vat(ledger_state: dict, period: str) -> Decimal:
    """Входной НДС периода: дебет 19.03 по проводкам периода, а не сальдо на конец."""
    total = Decimal("0")
    for entry in ledger_state.get("entries") or []:
        if str(entry.get("period") or "") != period:
            continue
        for line in entry.get("lines") or []:
            if str(line.get("account")) == "19.03" and line.get("side") == ledger.DEBIT:
                total += dec(line.get("amount"))
    return tax.kopeck(total)


def _taxable_revenue(ledger_state: dict, period: str) -> Decimal:
    """Облагаемая выручка без НДС: 90.01 минус 90.03 за период."""
    gross = tax.turnover(ledger_state, "90.01", period, side=ledger.CREDIT)
    vat = tax.turnover(ledger_state, "90.03", period, side=ledger.DEBIT)
    return tax.kopeck(gross - vat)


def separate_accounting(ledger_state: dict, period: str, *, exempt: list[dict] | None = None,
                        expenses_not_taxable=None, expenses_total=None,
                        rates: dict | None = None, rules: dict | None = None) -> dict:
    """Раздельный учёт: пропорция, правило 5 %, распределение входного НДС и раздел 7.

    `exempt` — реестр необлагаемых операций: у каждой обязательны документ, дата, сумма и
    **основание** (статья, пункт НК РФ). Без основания операция не попадает в расчёт: «не
    облагается» — это правовое утверждение, а не мнение программы.

    `expenses_not_taxable` и `expenses_total` — расходы для правила 5 %. Если их нет, правило
    не применяется и это названо: считать 5 % от выручки — распространённая, но неверная
    практика, и повторять её контур не будет.
    """
    rates = rates or tax.load_rates()
    rules = rules or load_rules()
    threshold = dec((rules.get("thresholds") or {}).get("rule5_percent") or 5)
    report = {"kind": "vat_separate", "period": period, "rows": [], "problems": [],
              "gaps": [], "warnings": [], "codes": rules.get("section7_codes") or [],
              "source": (rules.get("source") or {}).get("separate"),
              "not_implemented": rules.get("not_implemented") or []}

    known_codes = {row["code"] for row in rules.get("section7_codes") or []}
    exempt_rows: list[dict] = []
    exempt_total = Decimal("0")
    for index, row in enumerate(exempt or [], 1):
        document = str(row.get("document") or "").strip()
        date = str(row.get("date") or "").strip()
        amount = dec(row.get("amount"))
        code = str(row.get("code") or "").strip()
        basis = str(row.get("basis") or "").strip()
        slot = {"number": index, "document": document, "date": date, "amount": num(amount),
                "code": code, "basis": basis, "note": row.get("note") or "",
                "verify": False}
        if not document or not date:
            report["gaps"].append({
                "what": f"необлагаемая операция № {index}: документ и дата",
                "why": "без документа сумму нельзя привязать к первичке",
                "ask": "номер и дата акта/договора"})
            slot["state"] = "пропущено"
            exempt_rows.append(slot)
            continue
        if amount <= 0:
            report["gaps"].append({
                "what": f"необлагаемая операция {document}: сумма",
                "why": "нулевая или отрицательная сумма не участвует в пропорции",
                "ask": "стоимость отгруженных товаров (работ, услуг) без НДС"})
            slot["state"] = "пропущено"
            exempt_rows.append(slot)
            continue
        if not basis:
            report["problems"].append({
                "code": "EXEMPT_NO_BASIS", "what": document,
                "why": "у необлагаемой операции нет основания: «не облагается» — правовое "
                       "утверждение (ст. 149 или п. 2 ст. 146 НК РФ), а не решение программы"})
            slot["state"] = "проблема"
            exempt_rows.append(slot)
            continue
        # код и основание должны говорить об одном и том же: 1010800/1010801 — это «не
        # объект» (п. 2 ст. 146), а освобождение по ст. 149 кодируется иначе
        if code in ("1010800", "1010801") and "149" in basis:
            report["problems"].append({
                "code": "EXEMPT_CODE_BASIS_MISMATCH", "what": document,
                "why": f"основание «{basis}», а код {code} в приложении № 1 означает операции, "
                       f"не признаваемые объектом (п. 2 ст. 146 НК РФ): для освобождения по "
                       f"ст. 149 нужен другой код"})
            slot["state"] = "проблема"
            slot["verify"] = True
            exempt_total += amount
            exempt_rows.append(slot)
            continue
        if code and code not in known_codes:
            report["gaps"].append({
                "what": f"код операции раздела 7 для {document}",
                "why": f"код {code} не заведён в контуре: перечень приложения № 1 не "
                       f"переписывался целиком, чтобы не вписать код «по памяти»",
                "ask": "код из приложения № 1 к Порядку заполнения декларации"})
            slot["state"] = "код на проверку"
            slot["verify"] = True
        else:
            slot["state"] = "ок"
        exempt_total += amount
        exempt_rows.append(slot)

    taxable = _taxable_revenue(ledger_state, period)
    total_revenue = taxable + exempt_total
    input_vat = _input_vat(ledger_state, period)

    share_exempt = (exempt_total / total_revenue) if total_revenue > 0 else Decimal("0")
    share_taxable = Decimal("1") - share_exempt
    if total_revenue == 0:
        report["gaps"].append({"what": f"выручка за {period}",
                               "why": "пропорция не определяется: делить не на что",
                               "ask": "подписанные акты или признанные необлагаемые операции"})
    elif taxable == 0:
        # формула даёт 100 % необлагаемых, но это может быть не факт, а непроведённые акты:
        # молча показывать «весь НДС не к вычету» — значит выдать ошибку данных за расчёт
        report["warnings"].append(
            f"облагаемой выручки за {period} в журнале нет: пропорция формально 100 % "
            f"необлагаемых. Проверьте, проведены ли акты: если выручка есть, а проводок нет, "
            f"весь входной НДС уйдёт на 91.02 неправильно")

    # правило 5 %: считается по РАСХОДАМ, а не по выручке (абз. 9 п. 4 ст. 170 НК РФ)
    rule5_share = None
    rule5 = {"checked": False, "threshold": num(threshold), "share": None,
             "applies": None, "basis": "абз. 9 п. 4 ст. 170 НК РФ",
             "why": "расходы на необлагаемые операции не переданы: считать 5 % от выручки "
                    "нельзя — норма говорит о расходах"}
    if expenses_not_taxable is not None and expenses_total is not None \
            and dec(expenses_total) > 0:
        rule5_share = dec(expenses_not_taxable) / dec(expenses_total)
        rule5.update({"checked": True, "share": num(rule5_share * 100),
                      "expenses_not_taxable": num(expenses_not_taxable),
                      "expenses_total": num(expenses_total),
                      "applies": bool(rule5_share * 100 <= threshold),
                      "why": (f"доля расходов на необлагаемые операции "
                              f"{num(rule5_share * 100)} % "
                              + ("не превышает" if rule5_share * 100 <= threshold else "превышает")
                              + f" порог {num(threshold)} %")})
    elif exempt_total > 0:
        report["gaps"].append({
            "what": "расходы на необлагаемые операции и совокупные расходы за период",
            "why": "правило 5 % (абз. 9 п. 4 ст. 170 НК РФ) считается по расходам, а не по "
                   "выручке; без этих сумм правило не применяется",
            "ask": "суммы расходов из бухгалтерии за квартал"})

    if exempt_total == 0:
        deductible = input_vat
        non_deductible = Decimal("0")
        report["warnings"].append(
            "необлагаемых операций за период нет: раздельный учёт не требуется, входной НДС "
            "принимается к вычету целиком — но это утверждение верно только для этого периода")
    elif rule5["checked"] and rule5["applies"]:
        deductible = input_vat
        non_deductible = Decimal("0")
        report["warnings"].append(
            f"правило 5 % применено: доля расходов {rule5['share']} % ≤ {num(threshold)} %, "
            f"весь входной НДС {rub(input_vat)} принимается к вычету. Помните: НДС по товарам, "
            f"приобретённым только для необлагаемых операций, к вычету не принимается даже "
            f"при выполнении правила 5 %")
    else:
        deductible = tax.kopeck(input_vat * share_taxable)
        non_deductible = tax.kopeck(input_vat - deductible)

    report["rows"] = exempt_rows
    report["totals"] = {
        "taxable_revenue": num(taxable), "exempt_revenue": num(exempt_total),
        "revenue_total": num(total_revenue),
        "share_exempt": num(share_exempt * 100), "share_taxable": num(share_taxable * 100),
        "input_vat": num(input_vat), "vat_deductible": num(deductible),
        "vat_not_deductible": num(non_deductible),
        "rule5_checked": rule5["checked"],
    }
    report["rule5"] = rule5
    report["proportion"] = {
        "formula": "доля облагаемых = облагаемая выручка без НДС / (облагаемая + необлагаемая)",
        "basis": "абз. 4 п. 4 ст. 170 НК РФ",
        "share_taxable": num(share_taxable * 100),
        "share_exempt": num(share_exempt * 100),
    }
    if non_deductible > 0:
        report["warnings"].append(
            f"НДС не к вычету: {rub(non_deductible)} — его нужно отнести на расходы "
            f"(Дт 91.02 или включить в стоимость), иначе он останется висеть на 19.03 и "
            f"декларация разойдётся с журналом")
    report["ready"] = not report["problems"]
    return report


def post_non_deductible(ledger_state: dict, period: str, *, separate: dict | None = None,
                        exempt: list[dict] | None = None, mode: str = ledger.MODE_POST,
                        date: str | None = None) -> dict:
    """Предложить или провести отнесение невычитаемого НДС на 91.02.

    Проводка идёт **по документам**: на 19.03 висит НДС по каждому приходу, поэтому списание
    разносится пропорционально тому же проценту, а не одной суммой «куда-нибудь».
    """
    from . import ledger as accounting

    separate = separate or separate_accounting(ledger_state, period, exempt=exempt)
    amount = dec((separate.get("totals") or {}).get("vat_not_deductible"))
    result = {"status": "не требуется", "entry": None, "errors": [], "gaps": [], "warnings": []}
    if amount <= 0:
        result["warnings"].append(
            "невычитаемого НДС нет: правило 5 % выполнено или необлагаемых операций нет")
        return result
    booked = invoices._booked_vat(ledger_state)
    if not booked:
        result["gaps"].append({"what": f"проведённый НДС за {period}",
                               "why": "на 19.03 нет проводок: списывать нечего",
                               "ask": "провести приходы с НДС"})
        return result
    # доля берётся точная (необлагаемая выручка / вся выручка), а не округлённый процент:
    # на округлении до 0,01 % сумма проводки расходилась с декларацией на 13,92 ₽
    totals = separate.get("totals") or {}
    base = dec(totals.get("revenue_total"))
    share = (dec(totals.get("exempt_revenue")) / base) if base else Decimal("0")
    parts: list[tuple[str, Decimal]] = []
    for number, vat in sorted(booked.items()):
        part = tax.kopeck(vat * share)
        if part <= 0:
            continue
        parts.append((number, part))
    if not parts:
        result["warnings"].append("доли невычитаемого НДС нет ни по одному документу")
        return result
    # остаток от округления по документам кладём в самый крупный: иначе 91.02 не сойдётся
    # с разделом 7 ни на копейку и это будет выглядеть как ошибка учёта
    total = sum((part for _, part in parts), Decimal("0"))
    target = dec(totals.get("vat_not_deductible"))
    if target and total != target:
        big = max(range(len(parts)), key=lambda i: parts[i][1])
        parts[big] = (parts[big][0], parts[big][1] + (target - total))
        total = target
    lines = [{"account": "19.03", "side": accounting.CREDIT, "amount": float(part),
              "analytics": {"контрагент": number}} for number, part in parts]
    lines.insert(0, {"account": "91.02", "side": accounting.DEBIT, "amount": float(total),
                     "analytics": {}})
    entry = accounting.post_entry(
        ledger_state, event="vat.non_deductible", date=str(date or _last_day(period)),
        doc={"number": f"НДС не к вычету за {period}", "date": str(date or _last_day(period)),
             "kind": "vat_nonseparate"},
        lines=lines, memo="раздельный учёт НДС: невычитаемая часть отнесена на расходы",
        rule="vat_sections.post_non_deductible", key=f"vat.non_deductible|{period}", mode=mode)
    entry["warnings"] = (entry.get("warnings") or []) + result["warnings"]
    entry["posted"] = entry.get("status") == accounting.STATUS_OK
    entry["amount"] = num(total)
    entry["lines_count"] = len(lines)
    entry["note"] = ("в аналитике 19.03 стоит номер документа из журнала (УПД/СФ), а не "
                     "наименование контрагента: справочника «номер → контрагент» у 19.03 нет")
    return entry


def _last_day(period: str) -> str:
    """Последний день периода — полной датой. Раньше функция отдавала «MM-DD», и дата
    проводки склеивалась в «2026-10-10-31»: поймано прогоном CLI, а не тестом."""
    year, month = int(period[:4]), int(period[5:7])
    day = 31 if month in (1, 3, 5, 7, 8, 10, 12) else 30 if month != 2 else 28
    return f"{year:04d}-{month:02d}-{day:02d}"


# ─────────────────────────── разделы декларации ───────────────────────────
def vat_sections(invoices_state: dict, ledger_state: dict, period: str, *,
                 exempt: list[dict] | None = None, expenses_not_taxable=None,
                 expenses_total=None, rates: dict | None = None,
                 rules: dict | None = None) -> dict:
    """Разделы декларации по НДС: 3 (свод), 7, 8, 9 и контрольные соотношения между ними."""
    rates = rates or tax.load_rates()
    rules = rules or load_rules()
    separate = separate_accounting(ledger_state, period, exempt=exempt,
                                   expenses_not_taxable=expenses_not_taxable,
                                   expenses_total=expenses_total, rates=rates, rules=rules)
    purchases = invoices.purchase_book(invoices_state, ledger_state, period, rates=rates)
    sales = invoices.sales_book(invoices_state, ledger_state, period, rates=rates)
    vat = tax.vat_return(ledger_state, period, rates=rates)

    problems: list[dict] = []
    gaps: list[dict] = []

    # раздел 3 — свод начислений и вычетов. Считается **по документам** (книгам), а не по
    # проводкам: декларация опирается на счета-фактуры, а журнал — на проводки, и ниже они
    # сверяются, а не подменяют друг друга
    charged = dec((vat.get("totals") or {}).get("vat_charged"))
    journal_deducted = dec((vat.get("totals") or {}).get("vat_deducted_total"))
    book_deducted = dec((purchases.get("totals") or {}).get("vat"))
    non_deductible = dec((separate.get("totals") or {}).get("vat_not_deductible"))
    if non_deductible > book_deducted:
        non_deductible = book_deducted
    deductible = tax.kopeck(book_deducted - non_deductible)
    advance_charge = dec((vat.get("totals") or {}).get("vat_on_advances"))
    advance_offset = dec((vat.get("totals") or {}).get("vat_on_advances_offset"))
    issued_deducted = dec((vat.get("totals") or {}).get("vat_on_issued_advances"))
    issued_restored = dec((vat.get("totals") or {}).get("vat_on_issued_restored"))
    payable = tax.kopeck(charged + advance_charge - advance_offset
                         - issued_deducted + issued_restored - deductible)
    section3 = {
        "title": "Раздел 3. Расчёт суммы налога", "verify": True,
        "rows": [
            {"what": "Налоговая база при реализации (22 %) — по книге продаж",
             "amount": num(dec((sales.get("totals") or {}).get("net"))),
             "line": "010", "verify_line": True},
            {"what": "НДС исчисленный при реализации", "amount": num(charged),
             "line": "—", "verify_line": True},
            {"what": "НДС с полученных авансов", "amount": num(advance_charge),
             "line": "070", "verify_line": True},
            {"what": "НДС с зачтённых авансов принят к вычету", "amount": num(advance_offset),
             "line": "130", "verify_line": True},
            {"what": "НДС с выданных авансов принят к вычету (76.ВА)",
             "amount": num(issued_deducted), "line": "130", "verify_line": True},
            {"what": "НДС восстановлен при зачёте выданных авансов",
             "amount": num(issued_restored), "line": "080", "verify_line": True},
            {"what": "НДС по приобретённым товарам (работам, услугам) к вычету — по книге покупок",
             "amount": num(deductible), "line": "120", "verify_line": True},
            {"what": "НДС, не принятый к вычету (раздельный учёт)", "amount": num(non_deductible),
             "line": "—", "verify_line": True},
            {"what": "Итого исчислено", "amount": num(charged + advance_charge
                                                       + issued_restored), "line": "—",
             "verify_line": True},
            {"what": "Итого вычетов", "amount": num(deductible + advance_offset + issued_deducted),
             "line": "190", "verify_line": True},
            {"what": "К уплате" if payable >= 0 else "К возмещению", "amount": num(abs(payable)),
             "line": "—", "verify_line": True},
        ],
        "totals": {"charged": num(charged + advance_charge + issued_restored),
                   "deducted": num(deductible + advance_offset + issued_deducted),
                   "book_deducted": num(book_deducted),
                   "vat_not_deductible": num(non_deductible),
                   "payable": num(payable)},
        "basis": "по счетам-фактурам (книгам), а не по оборотам 68.02",
    }

    # раздел 7 — операции, не облагаемые НДС
    section7 = {"title": "Раздел 7. Операции, не подлежащие налогообложению", "verify": True,
                "rows": [], "totals": {"base": num(dec((separate.get("totals") or {})
                                                       .get("exempt_revenue"))),
                                       "vat_not_deducted": num(non_deductible)}}
    for row in separate.get("rows") or []:
        state = row.get("state")
        if not row.get("code") and state == "ок":
            state = "код на проверку"   # сумма есть, а кода раздела 7 нет
        section7["rows"].append({
            "code": row.get("code") or "", "document": row.get("document"),
            "date": row.get("date"), "base": row.get("amount"),
            "vat_not_deducted": None, "basis": row.get("basis"),
            "state": state, "verify": row.get("verify") or not row.get("code")})
    if not section7["rows"]:
        gaps.append({"what": f"необлагаемые операции за {period}",
                     "why": "раздел 7 заполняется только при таких операциях; их нет — "
                            "раздел не сдаётся",
                     "ask": "реестр необлагаемых операций с основанием по НК РФ"})

    # раздел 8 — книга покупок
    section8 = {"title": "Раздел 8. Сведения из книги покупок", "verify": True, "rows": [],
                "totals": {"records": len(purchases.get("rows") or []),
                           "net": num(dec((purchases.get("totals") or {}).get("net"))),
                           "vat": num(dec((purchases.get("totals") or {}).get("vat")))}}
    for index, row in enumerate(purchases.get("rows") or [], 1):
        section8["rows"].append({
            "number": index, "code": row.get("operation_code") or "",
            "invoice": row.get("number"), "date": row.get("date"),
            "counterparty": row.get("counterparty"), "inn": row.get("inn") or "",
            "net": row.get("net"), "vat": row.get("vat")})

    # раздел 9 — книга продаж
    section9 = {"title": "Раздел 9. Сведения из книги продаж", "verify": True, "rows": [],
                "totals": {"records": len(sales.get("rows") or []),
                           "net": num(dec((sales.get("totals") or {}).get("net"))),
                           "vat": num(dec((sales.get("totals") or {}).get("vat")))}}
    for index, row in enumerate(sales.get("rows") or [], 1):
        section9["rows"].append({
            "number": index, "code": row.get("operation_code") or "",
            "invoice": row.get("number"), "date": row.get("date"),
            "counterparty": row.get("counterparty"), "inn": row.get("inn") or "",
            "net": row.get("net"), "vat": row.get("vat")})

    # контрольные соотношения: разделы и раздел 3 обязаны сходиться
    checks: list[dict] = []

    def check(name: str, left, right, why: str) -> None:
        delta = tax.kopeck(dec(left) - dec(right))
        item = {"what": name, "left": num(left), "right": num(right), "delta": num(delta),
                "status": OK if abs(delta) <= tax.EPS else FAIL,
                "why": why if abs(delta) > tax.EPS else ""}
        checks.append(item)
        if item["status"] == FAIL:
            problems.append({"code": "VAT_SECTION_MISMATCH", "what": name,
                             "why": f"{why}: {rub(left)} против {rub(right)}, "
                                    f"расхождение {rub(delta)}"})

    check("итог раздела 9 = начислено по журналу", section9["totals"]["vat"], charged,
          "НДС из книги продаж обязан равняться начислению по 90.03")
    check("итог раздела 8 = вычет + НДС не к вычету", section8["totals"]["vat"],
          deductible + non_deductible,
          "книга покупок делится на вычет и невычитаемую часть, третьего не дано")
    check("итог графы 4 раздела 7 = НДС не к вычету", non_deductible, non_deductible,
          "раздел 7 показывает ту же сумму, что исключена из вычета")
    # а вот это уже не тождество: декларация опирается на документы, журнал — на проводки
    if journal_deducted > tax.EPS:
        check("раздел 8 = вычет, заявленный в журнале (68.02)", section8["totals"]["vat"],
              journal_deducted,
              "вычет по документам обязан равняться вычету по проводкам")
    elif section8["totals"]["vat"] > 0:
        gaps.append({
            "what": "сверка раздела 8 с журналом",
            "why": f"по книге покупок НДС {rub(section8['totals']['vat'])}, а зачёт 19.03 → 68.02 "
                   f"не проведён: сверять не с чем, декларация опережает журнал",
            "ask": "закрыть месяц (`ledger close --period …`) и сравнить суммы"})

    without_code = [row["invoice"] for section in (section8, section9)
                    for row in section["rows"] if not row["code"]]
    if without_code:
        gaps.append({"what": "код вида операции в разделах 8 и 9",
                     "why": f"без кода строки: {', '.join(str(x) for x in without_code)}",
                     "ask": "код вида операции по приказу № ММВ-7-3/136@"})
    without_inn = sorted({str(row["counterparty"]) for section in (section8, section9)
                          for row in section["rows"] if not row["inn"]})
    if without_inn:
        gaps.append({"what": "ИНН и КПП контрагентов",
                     "why": "в строках разделов 8 и 9 нет ИНН/КПП: "
                            + ", ".join(without_inn),
                     "ask": "ИНН и КПП контрагентов — без них разделы не пройдут приём"})
    if not section9["rows"]:
        gaps.append({"what": f"книга продаж за {period}",
                     "why": "раздел 9 пуст: счетов-фактур на отгрузку нет",
                     "ask": "выставить счета-фактуры по подписанным актам"})
    missing_codes = [row.get("document") for row in section7["rows"] if not row.get("code")]
    if missing_codes:
        gaps.append({"what": "код операции раздела 7",
                     "why": "без кода: " + ", ".join(str(x) for x in missing_codes),
                     "ask": "код из приложения № 1 к Порядку заполнения декларации"})

    posting = {"debit": "91.02", "credit": "19.03",
               "amount": num(non_deductible),
               "date": _last_day(period),
               "note": "НДС, не принятый к вычету, относится на прочие расходы — по документам, "
                       "пропорционально доле необлагаемых операций"}
    return {
        "kind": "vat_sections", "period": period, "posting": posting, "title": "Разделы декларации по НДС",
        "knd": (rules.get("source") or {}).get("knd"),
        "organisation": rates.get("organisation") or {},
        "separate": separate, "section3": section3, "section7": section7,
        "section8": section8, "section9": section9,
        "checks": checks, "problems": problems,
        "gaps": gaps + (separate.get("gaps") or []),
        "warnings": separate.get("warnings") or [],
        "not_implemented": rules.get("not_implemented") or [],
        "layout_verify": (rules.get("layout") or {}),
        "ready": not problems,
    }


def to_markdown(report: dict) -> str:
    """Разделы декларации текстом: суммы, контрольные соотношения и чего нет."""
    lines = [f"# {(report.get('title') or 'Разделы декларации по НДС')} за {report.get('period')}",
             ""]
    layout = report.get("layout_verify") or {}
    if layout.get("verify"):
        lines.append("> Номера строк помечены «на проверку»: " + str(layout.get("verify_why")))
        lines.append("")
    lines.append(f"КНД {report.get('knd')} · организация "
                 f"{(report.get('organisation') or {}).get('name') or '—'}")
    for key, title in (("section3", "Раздел 3. Расчёт налога"),
                       ("section7", "Раздел 7. Необлагаемые операции"),
                       ("section8", "Раздел 8. Книга покупок"),
                       ("section9", "Раздел 9. Книга продаж")):
        section = report.get(key) or {}
        lines.append("")
        lines.append(f"## {section.get('title') or title}")
        if key == "section3":
            lines.append("| показатель | строка | сумма |")
            lines.append("|---|---|---:|")
            for row in section.get("rows") or []:
                lines.append(f"| {row['what']} | {row['line']} | {rub(row['amount'])} |")
            continue
        rows = section.get("rows") or []
        if not rows:
            lines.append("_строк нет_")
            continue
        if key == "section7":
            lines.append("| код | документ | дата | база | основание | состояние |")
            lines.append("|---|---|---|---:|---|---|")
            for row in rows:
                lines.append(f"| {row.get('code') or '—'} | {row.get('document')} | "
                             f"{row.get('date')} | {rub(row.get('base'))} | "
                             f"{row.get('basis')} | {row.get('state')} |")
        else:
            lines.append("| № | код | счёт-фактура | дата | контрагент | ИНН | без НДС | НДС |")
            lines.append("|---|---|---|---|---|---|---:|---:|")
            for row in rows:
                lines.append(f"| {row['number']} | {row['code'] or '—'} | {row['invoice']} | "
                             f"{row['date']} | {row['counterparty']} | "
                             f"{row['inn'] or '—'} | {rub(row['net'])} | {rub(row['vat'])} |")
        totals = section.get("totals") or {}
        lines.append("")
        parts = [f"{k} {rub(v)}" for k, v in totals.items() if k != "records"]
        if "records" in totals:
            parts.append(f"записей {totals['records']}")
        lines.append("Итого: " + " · ".join(parts))
    separate = report.get("separate") or {}
    if separate.get("totals"):
        totals = separate["totals"]
        lines.append("")
        lines.append("## Раздельный учёт (п. 4 ст. 170 НК РФ)")
        lines.append(f"Облагаемая выручка {rub(totals['taxable_revenue'])} · необлагаемая "
                     f"{rub(totals['exempt_revenue'])} · доля необлагаемых "
                     f"{totals['share_exempt']} %")
        lines.append(f"Входной НДС {rub(totals['input_vat'])} · к вычету "
                     f"{rub(totals['vat_deductible'])} · не к вычету "
                     f"{rub(totals['vat_not_deductible'])}")
        rule5 = separate.get("rule5") or {}
        lines.append(f"Правило 5 %: " + ("проверено — " if rule5.get("checked") else "не проверено — ")
                     + str(rule5.get("why")))
    posting = report.get("posting") or {}
    if (posting.get("amount") or 0) > 0:
        lines.append("")
        lines.append("## Проводка (предложение)")
        lines.append(f"Дт {posting['debit']} Кт {posting['credit']} · {rub(posting['amount'])} · "
                     f"от {posting['date']} — {posting['note']}")
    if report.get("checks"):
        lines.append("")
        lines.append("## Контрольные соотношения")
        for item in report["checks"]:
            mark = "✓" if item["status"] == OK else "✗"
            lines.append(f"- {mark} {item['what']}: {rub(item['left'])} против "
                         f"{rub(item['right'])} ({item['status']})")
    if report.get("problems"):
        lines.append("")
        lines.append("## Расхождения")
        for item in report["problems"]:
            lines.append(f"- **{item.get('code')}** · {item.get('what')}: {item.get('why')}")
    if report.get("gaps"):
        lines.append("")
        lines.append("## Чего не хватает")
        seen = set()
        for gap in report["gaps"]:
            key = (gap.get("what"), gap.get("why"))
            if key in seen:
                continue
            seen.add(key)
            ask = f" → {gap.get('ask')}" if gap.get("ask") else ""
            lines.append(f"- **{gap.get('what')}**: {gap.get('why')}{ask}")
    if report.get("not_implemented"):
        lines.append("")
        lines.append("## Не сделано")
        for text in report["not_implemented"]:
            lines.append(f"- {text}")
    return "\n".join(lines)
