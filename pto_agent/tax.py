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
         "rsv": "РСВ (расчёт по страховым взносам)", "psv": "Персонифицированные сведения"}


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
def turnover(state: dict, account: str, period: str, *, side: str | None = None,
             prefix: bool = True) -> Decimal:
    total = Decimal("0")
    for entry in state.get("entries") or []:
        if not str(entry.get("period") or "").startswith(period[:7]):
            continue
        if str(entry.get("period") or "") != period:
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
            if str(entry.get("period") or "") == period
            and (event is None or entry.get("event") == event)]


# ─────────────────────────── НДС ───────────────────────────
def vat_return(state: dict, period: str, *, rates: dict | None = None,
               organisation: dict | None = None) -> dict:
    """Декларация по НДС из журнала: начислено, вычеты, к уплате и проверка ставок."""
    rates = rates or load_rates()
    organisation = organisation or rates.get("organisation") or {}
    report = {"kind": "vat", "period": period, "organisation": organisation,
              "rows": [], "rate_audit": [], "problems": [], "gaps": [], "warnings": []}

    gross = turnover(state, "90.01", period, side=ledger.CREDIT)
    charged = turnover(state, "68.02", period, side=ledger.CREDIT)
    deducted = turnover(state, "68.02", period, side=ledger.DEBIT)
    output_vat = turnover(state, "90.03", period, side=ledger.DEBIT)
    net_sales = gross - output_vat

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

    payable = charged - deducted
    report["totals"] = {"sales": num(net_sales), "sales_gross": num(gross),
                        "vat_charged": num(charged), "vat_deducted": num(deducted),
                        "vat_from_sales": num(output_vat), "payable": num(payable),
                        "refund": num(-payable) if payable < 0 else 0.0}

    if output_vat != charged:
        report["problems"].append({
            "code": "VAT_MISMATCH", "what": period,
            "why": f"НДС с реализации по 90.03 {rub(output_vat)} ≠ начислено по 68.02 "
                   f"{rub(charged)}: расхождение {rub(output_vat - charged)} — либо не все "
                   f"счета-фактуры зарегистрированы, либо налог начислен не тем счётом"})
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
        report["rows"].append({"tab": employee.get("tab"), "name": employee.get("name"),
                               "base": employee.get("accrued"),
                               "contrib": employee.get("contrib"),
                               "injury": employee.get("injury"),
                               "note": employee.get("contrib_note") or ""})
        total_base += float(employee.get("accrued") or 0)
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
    if not str(report.get("period") or "").count("-") == 1:
        problems.append({"code": "BAD_PERIOD", "what": report.get("period"),
                         "why": "период задаётся как ГГГГ-ММ"})
    totals = report.get("totals") or {}
    if report.get("kind") == "vat":
        expected = kopeck(totals.get("vat_charged")) - kopeck(totals.get("vat_deducted"))
        if abs(expected - kopeck(totals.get("payable"))) > EPS:
            problems.append({"code": "VAT_TOTALS", "what": report.get("period"),
                             "why": "к уплате не равно начислено минус вычеты"})
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
