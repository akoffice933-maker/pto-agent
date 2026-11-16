# -*- coding: utf-8 -*-
"""Остальные разделы декларации по НДС: 4–6 (ставка 0 %), 12 (п. 5 ст. 173 НК РФ)
и приложения № 1 к разделам 8 и 9 — вместе с дополнительными листами книг.

Зачем модуль. Круг 36 закрыл разделы 3, 7, 8, 9 и раздельный учёт. За их пределами
оставались ровно те листы, из-за которых декларация считается «неполной»:

* **разделы 4–6** — операции по ставке 0 %. Тут нельзя «просто посчитать»: раздел 4
  заполняется только по подтверждённым операциям, раздел 6 — по тем, где 180 дней истекли
  и документы не собраны (п. 9 ст. 165 НК РФ), а раздел 5 — по вычетам, право на которые
  появилось в этом периоде. Один и тот же экспорт не может попасть и в 4, и в 6;
* **раздел 12** — счета-фактуры, выставленные лицами из п. 5 ст. 173 НК РФ. Для нашей
  организации (плательщика) он пуст, и это должно быть сказано, а не «забыто»;
* **приложения № 1 к разделам 8 и 9** — сведения из дополнительных листов книг. Сами
  дополнительные листы (A2) в контуре не формировались вовсе.

Правила, которых модуль держится:

* **Код операции не выдумывается.** В разделах 4 и 6 нужен 7-значный код приложения № 1 к
  Порядку; в контуре его нет, поэтому код спрашивается у человека, а строка помечается
  «код на проверку».
* **Срок 180 дней считается числом, а не «на глаз»**: от даты отгрузки до конца периода.
  Если срок не истёк — начислять в разделе 6 нечего, и модуль это говорит.
* **Дополнительный лист — две записи**, а не правка старой строки: аннулируемая («до») и
  новая («после»). Без основания и без исходного документа запись не создаётся.
* **Ни одна сумма не берётся из воздуха**: налог в разделе 6 считается по ставке на дату
  отгрузки (`tax.vat_rate_on`), вычеты — только те, что переданы вместе с операцией.
"""
from __future__ import annotations

import json
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

from . import tax

SCHEMA = "pto-agent/vat-declaration-sections@1"
RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "vat_declaration_sections.json"

OK = "сходится"
FAIL = "расхождение"
KINDS = {"purchases": "книга покупок", "sales": "книга продаж"}
KINDS_GEN = {"purchases": "книги покупок", "sales": "книги продаж"}


def dec(value) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0)).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001 — мусор в данных не должен ронять отчёт
        return Decimal("0.00")


def num(value) -> float:
    return float(dec(value))


def rub(value) -> str:
    return f"{dec(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def load_rules(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))


def _days_between(start: str, end: str) -> int | None:
    try:
        first = _date.fromisoformat(str(start)[:10])
        second = _date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    return (second - first).days


def _period_end(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    day = 31 if month in (1, 3, 5, 7, 8, 10, 12) else 30 if month != 2 else 28
    return f"{year:04d}-{month:02d}-{day:02d}"


# ─────────────────────────── разделы 4–6: ставка 0 % ───────────────────────────
def zero_rate(period: str, *, exports: list[dict] | None = None,
              rates: dict | None = None, rules: dict | None = None) -> dict:
    """Разделы 4, 5, 6 по реестру операций со ставкой 0 %.

    Реестр вводит человек: `document`, `date`, `amount` (база без НДС), `code` (7-значный,
    необязательно), `vat_input` (вычеты, необязательно), `confirmed` (True/False),
    `confirmed_date` (когда собраны документы), `claimed` (вычет по разделу 5 уже заявлен).
    """
    rules = rules or load_rules()
    rates = rates or tax.load_rates()
    limit = int(((rules.get("thresholds") or {}).get("zero_rate_days")) or 180)
    period_end = _period_end(period)
    report: dict = {"kind": "vat_zero_rate", "period": period,
                    "title": "Разделы 4–6. Операции по ставке 0 %",
                    "sections": {"4": {"rows": []}, "5": {"rows": []}, "6": {"rows": []}},
                    "problems": [], "gaps": [], "warnings": [], "checks": [],
                    "not_implemented": rules.get("not_implemented") or [],
                    "layout": rules.get("layout") or {},
                    "limit_days": limit, "basis": rules.get("source") or {}}

    total_base = Decimal("0")
    accepted: list[dict] = []
    for index, row in enumerate(exports or [], 1):
        document = str(row.get("document") or "").strip()
        shipped = str(row.get("date") or "").strip()
        amount = dec(row.get("amount"))
        code = str(row.get("code") or "").strip()
        slot = {"number": index, "document": document, "date": shipped,
                "contract": row.get("contract") or "",
                "counterparty": row.get("counterparty") or "",
                "base": num(amount), "code": code, "state": "ок", "verify": False}
        if not document or not shipped:
            report["gaps"].append({"what": f"операция 0 % № {index}: документ и дата",
                                   "why": "без ГТД/контракта и даты отгрузки срок 180 дней "
                                          "не от чего считать",
                                   "ask": "номер и дата документа отгрузки (ГТД, контракт)"})
            slot["state"] = "пропущено"
            report["sections"]["4"]["rows"].append(slot)
            continue
        if amount <= 0:
            report["gaps"].append({"what": f"операция 0 % {document}: налоговая база",
                                   "why": "нулевая база не участвует ни в разделе 4, ни в 6",
                                   "ask": "стоимость отгруженных товаров без НДС"})
            slot["state"] = "пропущено"
            report["sections"]["4"]["rows"].append(slot)
            continue
        if not code:
            report["gaps"].append({
                "what": f"код операции для {document}",
                "why": "в разделах 4 и 6 нужен 7-значный код приложения № 1 к Порядку; "
                       "перечня кодов в контуре нет, а код «по памяти» — ошибка в декларации",
                "ask": "код операции из приложения № 1"})
            slot["state"] = "код на проверку"
            slot["verify"] = True

        deadline = None
        if _days_between(shipped, period_end) is not None:
            try:
                deadline = (_date.fromisoformat(shipped)
                            .toordinal())
                deadline = _date.fromordinal(deadline + limit).isoformat()
            except ValueError:
                deadline = None
        slot["deadline"] = deadline

        confirmed = bool(row.get("confirmed"))
        confirmed_date = str(row.get("confirmed_date") or "").strip()
        vat_input = dec(row.get("vat_input"))
        if confirmed and not confirmed_date:
            report["gaps"].append({"what": f"дата подтверждения ставки 0 % по {document}",
                                   "why": "раздел 4 заполняется в периоде подтверждения: без "
                                          "даты неясно, в какую декларацию попадёт операция",
                                   "ask": "дата, когда собран комплект документов"})
            slot["state"] = "пропущено"
            report["sections"]["4"]["rows"].append(slot)
            continue

        if confirmed:
            confirmed_period = confirmed_date[:7]
            if confirmed_period > period:
                report["gaps"].append({
                    "what": f"подтверждение {document} от {confirmed_date}",
                    "why": f"подтверждение позже периода {period}: операция попадёт в "
                           f"декларацию за {confirmed_period}, а не за этот",
                    "ask": "подтвердить дату либо выбрать период подтверждения"})
                slot["state"] = "пропущено"
                report["sections"]["4"]["rows"].append(slot)
                continue
            if confirmed_period == period:
                slot.update({"vat_input": num(vat_input),
                             "deductions": num(vat_input),
                             "confirmed_date": confirmed_date,
                             "section": "4", "counted": True})
                report["sections"]["4"]["rows"].append(slot)
                total_base += amount
                accepted.append(slot)
                continue
            # подтверждено раньше: вычет заявляется разделом 5, если он ещё не заявлен
            if row.get("claimed"):
                slot["state"] = "вычет уже заявлен"
                slot["section"] = "5"
                report["sections"]["5"]["rows"].append(slot)
                continue
            slot.update({"vat_input": num(vat_input), "deductions": num(vat_input),
                         "confirmed_date": confirmed_date, "confirmed_period": confirmed_period,
                         "section": "5", "counted": True})
            report["sections"]["5"]["rows"].append(slot)
            total_base += amount
            accepted.append(slot)
            continue

        # не подтверждено: пока срок не истёк — начислять нечего
        expired = deadline is not None and deadline <= period_end
        if not expired:
            report["gaps"].append({
                "what": f"подтверждение ставки 0 % по {document}",
                "why": f"срок {limit} дней истекает {deadline}: до этой даты операция не "
                       f"попадает ни в раздел 4, ни в раздел 6",
                "ask": "собрать документы по п. 9 ст. 165 НК РФ"})
            slot["state"] = f"ждём документы до {deadline}"
            slot["section"] = "6"
            report["sections"]["6"]["rows"].append(slot)
            continue
        rate = dec(tax.vat_rate_on(shipped, rates).get("rate"))
        accrued = tax.kopeck(amount * rate / Decimal("100"))
        slot.update({"vat_input": num(vat_input), "deductions": num(vat_input),
                     "rate": float(rate), "accrued": num(accrued),
                     "deadline": deadline, "section": "6", "counted": True})
        report["sections"]["6"]["rows"].append(slot)
        total_base += amount
        accepted.append(slot)

    # контрольные соотношения
    checks: list[dict] = []
    # в итоги идут только строки, которые действительно попали в декларацию: строка
    # «вычет уже заявлен» остаётся в отчёте для следа, но второй раз не считается
    base4 = sum((dec(row.get("base")) for row in report["sections"]["4"]["rows"]
                 if row.get("counted")), Decimal("0"))
    base5 = sum((dec(row.get("base")) for row in report["sections"]["5"]["rows"]
                 if row.get("counted")), Decimal("0"))
    base6 = sum((dec(row.get("base")) for row in report["sections"]["6"]["rows"]
                 if row.get("counted") and row.get("accrued")), Decimal("0"))
    accrued6 = sum((dec(row.get("accrued")) for row in report["sections"]["6"]["rows"]
                    if row.get("accrued")), Decimal("0"))
    expected6 = sum((tax.kopeck(dec(row.get("base"))
                                * dec(row.get("rate")) / Decimal("100"))
                     for row in report["sections"]["6"]["rows"] if row.get("accrued")),
                    Decimal("0"))
    checks.append({"what": "база разделов 4 + 5 + 6 = база принятых операций",
                   "left": num(base4 + base5 + base6), "right": num(total_base),
                   "status": OK if abs((base4 + base5 + base6) - total_base) <= tax.EPS else FAIL})
    checks.append({"what": "налог раздела 6 = база × ставка на дату отгрузки",
                   "left": num(accrued6), "right": num(expected6),
                   "status": OK if abs(accrued6 - expected6) <= tax.EPS else FAIL})
    for item in checks:
        if item["status"] == FAIL:
            report["problems"].append({"code": "VAT_ZERO_RATE_MISMATCH",
                                       "what": item["what"],
                                       "why": f"{rub(item['left'])} против {rub(item['right'])}"})
    report["checks"] = checks

    if not accepted:
        report["gaps"].append({
            "what": "операции по ставке 0 %",
            "why": "разделы 4–6 не заполняются: подтверждённых и неподтверждённых "
                   "экспортных операций в реестре нет",
            "ask": "реестр операций со ставкой 0 % (ГТД, контракт, сумма, дата отгрузки)"})
    report["totals"] = {"base4": num(base4), "base5": num(base5), "base6": num(base6),
                        "accrued6": num(accrued6),
                        "deductions4": num(sum((dec(row.get("deductions"))
                                                for row in report["sections"]["4"]["rows"]
                                                if row.get("counted")), Decimal("0"))),
                        "deductions5": num(sum((dec(row.get("deductions"))
                                                for row in report["sections"]["5"]["rows"]
                                                if row.get("counted")), Decimal("0")))}
    report["ready"] = not report["problems"] and not report["gaps"]
    return report


# ─────────────────────────── раздел 12 ───────────────────────────
def section12(period: str, *, invoices: list[dict] | None = None,
              rates: dict | None = None, rules: dict | None = None) -> dict:
    """Раздел 12: счета-фактуры, выставленные лицами из п. 5 ст. 173 НК РФ.

    Для организации-плательщика раздел пуст. Это не «не сделано»: пустой раздел — законный
    результат, но он должен быть назван вместе с причиной, иначе его отсутствие выглядит
    как забытый лист.
    """
    rules = rules or load_rules()
    rates = rates or tax.load_rates()
    rows: list[dict] = []
    gaps: list[dict] = []
    problems: list[dict] = []
    for index, row in enumerate(invoices or [], 1):
        number = str(row.get("number") or "").strip()
        date = str(row.get("date") or "").strip()
        net = dec(row.get("net"))
        vat = dec(row.get("vat"))
        rate = dec(row.get("rate") if row.get("rate") is not None else 22)
        inn = str(row.get("buyer_inn") or "").strip()
        kpp = str(row.get("buyer_kpp") or "").strip()
        code = str(row.get("code") or "").strip()
        slot = {"number": index, "invoice": number, "date": date, "code": code,
                "buyer": row.get("buyer") or "", "inn": inn, "kpp": kpp,
                "net": num(net), "vat": num(vat), "gross": num(net + vat), "state": "ок",
                "verify": False}
        if not number or not date:
            gaps.append({"what": f"счёт-фактура № {index} раздела 12",
                         "why": "без номера и даты сведения не идентифицируются",
                         "ask": "номер и дата счёта-фактуры"})
            slot["state"] = "пропущено"
            rows.append(slot)
            continue
        if net <= 0:
            gaps.append({"what": f"стоимость без налога по СФ {number}",
                         "why": "нулевая стоимость не заполняет графу 5 счёта-фактуры",
                         "ask": "стоимость товаров (работ, услуг) без налога"})
            slot["state"] = "пропущено"
            rows.append(slot)
            continue
        if not inn or not tax.inn_ok(inn):
            problems.append({"code": "SECTION12_BAD_INN", "what": f"СФ {number}",
                             "why": f"ИНН покупателя «{inn}» не проходит проверку контрольной "
                                    f"суммы: без него раздел 12 не пройдёт приём"})
            slot["state"] = "проблема"
            rows.append(slot)
            continue
        if not kpp or not tax.kpp_ok(kpp):
            gaps.append({"what": f"КПП покупателя по СФ {number}",
                         "why": f"КПП «{kpp}» не той структуры (4 цифры + буква + 5 цифр)",
                         "ask": "КПП из строки 6б счёта-фактуры"})
            slot["state"] = "на проверку"
            slot["verify"] = True
        if not code:
            gaps.append({"what": f"код вида операции по СФ {number} раздела 12",
                         "why": "в строке 010 нужен код из приказа № ММВ-7-3/136@",
                         "ask": "код вида операции (2 знака)"})
            slot["state"] = "код на проверку"
            slot["verify"] = True
        expected = tax.kopeck(net * rate / Decimal("100"))
        if abs(expected - vat) > tax.EPS:
            problems.append({"code": "SECTION12_VAT",
                             "what": f"СФ {number}",
                             "why": f"НДС {rub(vat)} не равен {rub(expected)} "
                                    f"({rate:g} % от {rub(net)})"})
            slot["state"] = "проблема"
        rows.append(slot)

    total_vat = sum((dec(row["vat"]) for row in rows), Decimal("0"))
    report = {"kind": "vat_section12", "period": period,
              "title": "Раздел 12. Счета-фактуры лиц, не являющихся плательщиками",
              "rows": rows, "problems": problems, "gaps": gaps, "warnings": [],
              "totals": {"invoices": len([r for r in rows if r["state"] != "пропущено"]),
                         "net": num(sum((dec(r["net"]) for r in rows), Decimal("0"))),
                         "vat": num(total_vat),
                         "to_line_030": num(total_vat)},
              "line_030_note": "сумма налога по разделу 12 переносится в строку 030 раздела 1 "
                               "(п. 5 ст. 173 НК РФ) и в раздел 3 не попадает",
              "layout": rules.get("layout") or {},
              "not_implemented": rules.get("not_implemented") or []}
    if not rows:
        report["gaps"].append({
            "what": "раздел 12",
            "why": "раздел не заполняется: организация — плательщик НДС, счетов-фактур по "
                   "п. 5 ст. 173 НК РФ (освобождение по ст. 145 НК РФ, УСН) она не выставляла",
            "ask": "если счёт-фактура с выделенным НДС всё же выставлялась — внести её в реестр"})
    report["ready"] = not problems
    return report


# ─────────────────────── дополнительные листы книг (A2) ───────────────────────
def register_book_fix(state: dict, *, sheet: str, base_period: str, number: str, date: str,
                      net_before=None, vat_before=None, net_after=None, vat_after=None,
                      operation_code: str | None = None, inn: str = "", reason: str = "",
                      entry_date: str = "", rules: dict | None = None) -> dict:
    """Записать исправление книги прошлого периода — основу дополнительного листа.

    Дополнительный лист — не правка строки, а две записи: аннулируемая («до») и новая
    («после»). Поэтому нужны обе суммы; если они совпадают, исправлять нечего.
    """
    rules = rules or load_rules()
    result = {"status": "отклонён", "fix": None, "errors": [], "gaps": [], "warnings": []}
    if sheet not in KINDS:
        result["errors"].append({"code": "BOOK_FIX_SHEET", "what": sheet,
                                 "why": "книга бывает purchases (покупки) или sales (продажи)"})
        return result
    if not str(reason or "").strip():
        result["gaps"].append({"what": "основание исправления книги",
                               "why": "без причины исправление неотличимо от подгонки цифр",
                               "ask": "что именно неверно: сумма, ставка, реквизиты, лишний СФ"})
        return result
    entry_date = str(entry_date or "").strip()
    if not entry_date:
        result["gaps"].append({"what": "дата исправления книги",
                               "why": "дополнительный лист относится к периоду исправления",
                               "ask": "дата записи дополнительного листа"})
        return result
    current = entry_date[:7]
    if not base_period or base_period >= current:
        result["errors"].append({
            "code": "BOOK_FIX_NOT_PAST", "what": base_period,
            "why": f"исправляется книга периода {base_period}, а текущий {current}: "
                   f"дополнительный лист нужен только для уже сданного периода, "
                   f"иначе книгу правят напрямую"})
        return result
    pool_key = "received" if sheet == "purchases" else "issued"
    pool = state.get(pool_key) or []
    document = next((row for row in pool if str(row.get("number")) == str(number)
                     and str(row.get("date")) == str(date)
                     and str(row.get("period")) == str(base_period)), None)
    if not document:
        result["errors"].append({
            "code": "BOOK_FIX_NO_DOCUMENT", "what": f"{number} от {date}",
            "why": f"в книге за {base_period} нет такого счёта-фактуры: аннулировать нечего"})
        return result
    before_net = dec(net_before if net_before is not None else document.get("net"))
    before_vat = dec(vat_before if vat_before is not None else document.get("vat"))
    after_net = dec(net_after if net_after is not None else document.get("net"))
    after_vat = dec(vat_after if vat_after is not None else document.get("vat"))
    if before_net == after_net and before_vat == after_vat:
        result["errors"].append({
            "code": "BOOK_FIX_SAME", "what": f"{number} от {date}",
            "why": "суммы «до» и «после» совпадают: исправлять нечего"})
        return result
    key = f"book_fix|{sheet}|{base_period}|{number}|{date}|{entry_date}|{num(after_vat)}"
    if key in (state.get("keys") or []):
        result.update({"status": "уже записано", "fix": next(
            (row for row in state.get("book_fixes") or [] if row.get("key") == key), None)})
        result["warnings"].append(f"исправление книги по СФ {number} от {date} уже есть")
        return result
    code = str(operation_code or document.get("operation_code") or "").strip()
    fix = {"sheet": sheet, "base_period": base_period, "number": str(number), "date": str(date),
           "net_before": num(before_net), "vat_before": num(before_vat),
           "net_after": num(after_net), "vat_after": num(after_vat),
           "inn": str(inn or document.get("inn") or ""),
           "counterparty": document.get("counterparty") or "",
           "operation_code": code,
           "reason": str(reason), "entry_date": entry_date, "period": current, "key": key}
    state.setdefault("book_fixes", []).append(fix)
    state.setdefault("keys", []).append(key)
    result.update({"status": "записано", "fix": fix})
    if not code:
        result["warnings"].append("у записи нет кода вида операции: в приложении 1 к разделу "
                                  "8/9 строка 010 останется пустой — это запрос к человеку")
    return result


def additional_sheets(state: dict, period: str, *, sheet: str = "purchases",
                      book_total_vat=None, rules: dict | None = None) -> dict:
    """Дополнительный лист книги за `period` и строки приложения № 1 к разделу 8/9."""
    if sheet not in KINDS:
        raise ValueError(f"книга бывает purchases или sales, а не {sheet!r}")
    rules = rules or load_rules()
    spec = ((rules.get("sections") or {}).get("appendix8" if sheet == "purchases"
                                              else "appendix9") or {})
    fixes = [row for row in state.get("book_fixes") or []
             if row.get("sheet") == sheet and str(row.get("period")) == period]
    rows: list[dict] = []
    gaps: list[dict] = []
    problems: list[dict] = []
    for index, fix in enumerate(fixes, 1):
        base = {"number": index, "invoice": fix.get("number"), "date": fix.get("date"),
                "base_period": fix.get("base_period"), "code": fix.get("operation_code") or "",
                "counterparty": fix.get("counterparty") or "", "inn": fix.get("inn") or "",
                "reason": fix.get("reason") or ""}
        rows.append(dict(base, kind="аннулируемая", net=fix.get("net_before"),
                         vat=fix.get("vat_before"), state="ок" if fix.get("operation_code")
                         else "код на проверку"))
        rows.append(dict(base, number=index, kind="новая", net=fix.get("net_after"),
                         vat=fix.get("vat_after"), state="ок" if fix.get("operation_code")
                         else "код на проверку"))
        if not fix.get("operation_code"):
            gaps.append({"what": f"код вида операции для СФ {fix.get('number')}",
                         "why": "строка 010 приложения к разделу 8/9 без кода не заполняется",
                         "ask": "код из приказа № МВМ-7-3/136@".replace("МВМ", "ММВ")})
        if not fix.get("inn") and sheet == "purchases":
            gaps.append({"what": f"ИНН продавца по СФ {fix.get('number')}",
                         "why": "в разделе 8 ИНН продавца обязателен",
                         "ask": "ИНН из счёта-фактуры"})
    delta_vat = sum((dec(fix.get("vat_after")) - dec(fix.get("vat_before"))
                     for fix in fixes), Decimal("0"))
    delta_net = sum((dec(fix.get("net_after")) - dec(fix.get("net_before"))
                     for fix in fixes), Decimal("0"))
    appendix = {
        "title": spec.get("title") or "Приложение № 1 к разделу",
        "rows": [{"line": "001", "what": "Признак актуальности ранее представленных сведений",
                  "value": "—"},
                 {"line": "005", "what": "Итоговая сумма налога по книге",
                  "value": num(book_total_vat) if book_total_vat is not None else None},
                 {"line": "190", "what": "Сумма налога всего по дополнительному листу",
                  "value": num(delta_vat)}],
        "records": len(fixes),
        "verify": bool(spec.get("layout_verify")) or bool(spec.get("rows") and any(
            row.get("verify") for row in spec.get("rows") or [])),
    }
    if not fixes:
        gaps.append({"what": f"дополнительный лист книги за {period}",
                     "why": "изменений в ранее сданную книгу не было — лист не формируется",
                     "ask": "если изменения были: `invoices book-fix …`"})
    if book_total_vat is None and fixes:
        gaps.append({"what": "итоговая сумма налога по книге (строка 005 приложения)",
                     "why": "в дополнительном листе строка 005 — итог всей книги, а не только "
                            "дополнительного листа: без книги её не откуда взять",
                     "ask": "передать итог книги (`--book-total-vat`)"})
    report = {"kind": "book_additional_sheet", "period": period, "sheet": sheet,
              "title": f"Дополнительный лист {KINDS_GEN[sheet]} за {period}",
              "rows": rows, "appendix": appendix, "problems": problems, "gaps": gaps,
              "totals": {"rows": len(rows), "fixes": len(fixes),
                         "delta_net": num(delta_net), "delta_vat": num(delta_vat)},
              "knd": ((rules.get("additional_sheets") or {}).get("knd") or {}).get(sheet),
              "knd_verify": bool((rules.get("additional_sheets") or {}).get("knd_verify")),
              "basis": (rules.get("additional_sheets") or {}).get("basis"),
              "not_implemented": rules.get("not_implemented") or []}
    report["ready"] = not problems and not gaps
    return report


# ─────────────────────────── выгрузка текстом ───────────────────────────
def to_markdown(report: dict) -> str:
    kind = report.get("kind")
    if kind == "vat_zero_rate":
        return _zero_rate_markdown(report)
    if kind == "vat_section12":
        return _section12_markdown(report)
    if kind == "book_additional_sheet":
        return _sheet_markdown(report)
    raise ValueError(f"неизвестный отчёт {kind!r}")


def _footer(lines: list[str], report: dict) -> None:
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


def _zero_rate_markdown(report: dict) -> str:
    lines = [f"# Разделы 4–6 декларации по НДС за {report['period']}", ""]
    lines.append(f"> Срок подтверждения нулевой ставки — {report.get('limit_days')} дней "
                 f"(п. 9 ст. 165 НК РФ). Код операции — 7-значный из приложения № 1 к "
                 f"Порядку; в контуре он не заведён и спрашивается у человека.")
    for code, title in (("4", "Раздел 4. Подтверждённая ставка 0 %"),
                        ("5", "Раздел 5. Вычеты по ранее подтверждённым операциям"),
                        ("6", "Раздел 6. Ставка 0 % не подтверждена")):
        rows = (report["sections"].get(code) or {}).get("rows") or []
        if not rows:
            lines.append("")
            lines.append(f"## {title}")
            lines.append("Раздел не заполняется: подходящих операций в реестре нет.")
            continue
        lines.append("")
        lines.append(f"## {title}")
        if code == "6":
            lines.append("| код | документ | отгрузка | база | ставка | налог | срок | состояние |")
            lines.append("|---|---|---|---:|---:|---:|---|---|")
            for row in rows:
                lines.append(f"| {row.get('code') or '—'} | {row['document']} | {row['date']} | "
                             f"{rub(row.get('base'))} | {row.get('rate') or '—'} | "
                             f"{rub(row.get('accrued') or 0)} | {row.get('deadline') or '—'} | "
                             f"{row.get('state')} |")
        else:
            lines.append("| код | документ | отгрузка | база | вычеты | подтверждено | состояние |")
            lines.append("|---|---|---|---:|---:|---|---|")
            for row in rows:
                lines.append(f"| {row.get('code') or '—'} | {row['document']} | {row['date']} | "
                             f"{rub(row.get('base'))} | {rub(row.get('deductions') or 0)} | "
                             f"{row.get('confirmed_date') or '—'} | {row.get('state')} |")
    totals = report.get("totals") or {}
    lines.append("")
    lines.append(f"Итого: раздел 4 — {rub(totals.get('base4'))}, раздел 5 — "
                 f"{rub(totals.get('base5'))}, раздел 6 — {rub(totals.get('base6'))} "
                 f"(налог {rub(totals.get('accrued6'))})")
    if report.get("checks"):
        lines.append("")
        lines.append("## Контрольные соотношения")
        for item in report["checks"]:
            mark = "✓" if item["status"] == OK else "✗"
            lines.append(f"- {mark} {item['what']}: {rub(item['left'])} против "
                         f"{rub(item['right'])} ({item['status']})")
    _footer(lines, report)
    return "\n".join(lines)


def _section12_markdown(report: dict) -> str:
    lines = [f"# Раздел 12 декларации по НДС за {report['period']}", ""]
    lines.append("> Раздел заполняют лица из п. 5 ст. 173 НК РФ — те, кто не платит НДС, но "
                 "выставил счёт-фактуру с выделенным налогом. Налог идёт в строку 030 раздела 1 "
                 "и в раздел 3 не попадает.")
    if report.get("rows"):
        lines.append("")
        lines.append("| № | код | счёт-фактура | дата | покупатель | ИНН | КПП | без НДС | НДС | всего |")
        lines.append("|---|---|---|---|---|---|---|---:|---:|---:|")
        for row in report["rows"]:
            lines.append(f"| {row['number']} | {row.get('code') or '—'} | {row.get('invoice')} | "
                         f"{row.get('date')} | {row.get('buyer')} | {row.get('inn')} | "
                         f"{row.get('kpp')} | {rub(row.get('net'))} | {rub(row.get('vat'))} | "
                         f"{rub(row.get('gross'))} |")
    totals = report.get("totals") or {}
    lines.append("")
    lines.append(f"Итого: счетов-фактур {totals.get('invoices')}, НДС {rub(totals.get('vat'))} "
                 f"→ строка 030 раздела 1.")
    _footer(lines, report)
    return "\n".join(lines)


def _sheet_markdown(report: dict) -> str:
    knd = report.get("knd") or "—"
    lines = [f"# {report['title']}", ""]
    lines.append(f"> КНД {knd}{' (на проверку)' if report.get('knd_verify') else ''}. "
                 f"{report.get('basis') or ''}")
    if report.get("rows"):
        lines.append("")
        lines.append("| № | вид записи | код | счёт-фактура | дата | контрагент | ИНН | без НДС | НДС | состояние |")
        lines.append("|---|---|---|---|---|---|---|---:|---:|---|")
        for row in report["rows"]:
            lines.append(f"| {row['number']} | {row['kind']} | {row.get('code') or '—'} | "
                         f"{row.get('invoice')} | {row.get('date')} | {row.get('counterparty')} | "
                         f"{row.get('inn') or '—'} | {rub(row.get('net'))} | {rub(row.get('vat'))} | "
                         f"{row.get('state')} |")
    totals = report.get("totals") or {}
    lines.append("")
    lines.append(f"Итого по дополнительному листу: записей {totals.get('rows')}, изменение "
                 f"стоимости {rub(totals.get('delta_net'))}, изменение НДС "
                 f"{rub(totals.get('delta_vat'))}")
    appendix = report.get("appendix") or {}
    lines.append("")
    lines.append(f"## {appendix.get('title')}")
    lines.append("| строка | что | значение |")
    lines.append("|---|---|---:|")
    for row in appendix.get("rows") or []:
        raw = row.get("value")
        value = "—" if raw is None else (raw if isinstance(raw, str) else rub(raw))
        lines.append(f"| {row['line']} | {row['what']} | {value} |")
    _footer(lines, report)
    return "\n".join(lines)
