# -*- coding: utf-8 -*-
"""Удержания из заработной платы: алименты, исполнительные листы, распоряжения работодателя.

Зачем модуль. До круга 38 контур считал начисление, НДФЛ и взносы, но не отвечал на вопрос
«сколько человек реально получит», если у него есть исполнительный документ. Ошибка здесь
дорогая: недодержали — долг перед взыскателем и риск для бухгалтера, передержали — нарушение
ст. 138 ТК РФ.

Три правила, из которых собрано всё остальное:

1. **Предел считается от суммы после НДФЛ.** Не от начисления: ст. 138 ТК РФ говорит о
   заработной плате, причитающейся работнику, а НДФЛ удерживается раньше. Считать 50 % от
   начисления — распространённая ошибка, и контур её не повторяет.
2. **Лимит зависит от основания, а не от желания.** Распоряжение работодателя — 20 %,
   исполнительный документ — 50 %, алименты на несовершеннолетних, вред здоровью, вред в связи
   со смертью кормильца и ущерб от преступления — 70 %. Меньший лимит не «повышается» сам.
3. **Не влезло — долг, а не тишина.** Удержали предел, остаток переносится и называется числом
   в отчёте. Молча удержать больше предела нельзя, молча забыть остаток — тоже.

Несколько документов удовлетворяются по очерёдности (ст. 111 № 229-ФЗ): сначала первая
очередь, внутри очереди — пропорционально суммам требований.

Что контур не делает и говорит об этом: перечень доходов, на которые не обращается взыскание
(ст. 101 № 229-ФЗ), разобран частично; индексация алиментов в твёрдой сумме не выполняется;
перечисление взыскателю идёт заявкой в казначействе, а не автоматически.
"""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

SCHEMA = "pto-agent/withholdings@1"
RULES_PATH = (Path(__file__).resolve().parents[1] / "data" / "reference"
              / "withholdings_rules.json")

EPS = Decimal("0.01")
OK = "удержано"
LIMITED = "удержано в пределах лимита"
SKIPPED = "не удержано"


def dec(value) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0)).quantize(EPS)
    except Exception:  # noqa: BLE001 — мусор в данных не должен ронять расчёт
        return Decimal("0.00")


def kopeck(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(EPS, rounding=ROUND_HALF_UP)


def num(value) -> float:
    return float(kopeck(value))


def rub(value) -> str:
    return f"{kopeck(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def load_rules(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))


def share_of(children: int, rules: dict) -> tuple[Decimal, str]:
    """Доля алиментов по числу детей: 1/4, 1/3, 1/2 (ст. 81 СК РФ).

    Больше трёх детей — 1/2: закон говорит «на трёх и более», и увеличивать долю дальше
    можно только судом, а не арифметикой.
    """
    kinds = (rules.get("kinds") or {}).get("alimony_share") or {}
    table = kinds.get("share_by_children") or {"1": "1/4", "2": "1/3", "3": "1/2"}
    key = "1" if children <= 1 else "2" if children == 2 else "3"
    text = str(table.get(key) or "1/2")
    numerator, denominator = text.split("/")
    return (Decimal(numerator) / Decimal(denominator)), text


def document_amount(document: dict, *, rules: dict, employee: dict,
                    net: Decimal) -> tuple[Decimal, list[dict], list[str]]:
    """Сумма по документу до применения предела: доли, процент или твёрдая сумма."""
    errors: list[dict] = []
    warnings: list[str] = []
    kind = str(document.get("kind") or "")
    spec = (rules.get("kinds") or {}).get(kind)
    if not spec:
        errors.append({"code": "WITHHOLDING_KIND", "what": document.get("number"),
                       "why": f"вид удержания «{kind}» не из справочника; известны: "
                              f"{', '.join(sorted(rules.get('kinds') or {}))}"})
        return Decimal("0"), errors, warnings
    if not str(document.get("number") or "").strip():
        errors.append({"code": "WITHHOLDING_NO_NUMBER", "what": kind,
                       "why": "у документа нет номера: удержание без документа — списание "
                              "«куда-нибудь», а не исполнение"})
        return Decimal("0"), errors, warnings
    if not str(document.get("issued_by") or document.get("payee") or "").strip():
        errors.append({"code": "WITHHOLDING_NO_PAYEE", "what": document.get("number"),
                       "why": "не указано, кому перечислять удержанное"})
        return Decimal("0"), errors, warnings

    if kind == "alimony_share":
        children = int(document.get("children") or employee.get("children") or 0)
        if children <= 0:
            errors.append({"code": "ALIMONY_NO_CHILDREN", "what": document.get("number"),
                           "why": "алименты в долях без числа детей не считаются: доля "
                                  "определяется числом несовершеннолетних детей"})
            return Decimal("0"), errors, warnings
        share, text = share_of(children, rules)
        amount = kopeck(net * share)
        warnings.append(f"{document.get('number')}: {children} реб. → доля {text} от "
                        f"{rub(net)} после НДФЛ = {rub(amount)}")
        return amount, errors, warnings

    if kind in ("writ", "employer_order", "damage_health", "crime_damage",
                "alimony_agreement", "alimony_fixed"):
        debt = dec(document.get("amount"))
        basis = document.get("basis") or spec.get("source")
        if document.get("percent") not in (None, ""):
            percent = dec(document.get("percent"))
            if percent <= 0 or percent > 100:
                errors.append({"code": "WITHHOLDING_PERCENT", "what": document.get("number"),
                               "why": f"процент удержания {percent} — вне диапазона 0…100"})
                return Decimal("0"), errors, warnings
            amount = kopeck(net * percent / Decimal("100"))
            warnings.append(f"{document.get('number')}: {percent} % от {rub(net)} после НДФЛ "
                            f"= {rub(amount)}")
        elif debt > 0:
            amount = kopeck(debt)
            warnings.append(f"{document.get('number')}: сумма по документу {rub(amount)} "
                            f"({basis})")
        else:
            errors.append({"code": "WITHHOLDING_NO_AMOUNT", "what": document.get("number"),
                           "why": "в документе нет ни суммы, ни процента удержания"})
            return Decimal("0"), errors, warnings
        if kind == "employer_order" and not (document.get("reason") or document.get("basis")):
            errors.append({"code": "EMPLOYER_ORDER_NO_BASIS", "what": document.get("number"),
                           "why": "удержание по инициативе работодателя требует основания "
                                  "(ст. 137 ТК РФ) и месячного срока на добровольный возврат"})
            return Decimal("0"), errors, warnings
        return amount, errors, warnings

    errors.append({"code": "WITHHOLDING_KIND", "what": kind,
                   "why": "вид удержания не разобран"})
    return Decimal("0"), errors, warnings


def apply(payroll: dict, documents: list[dict] | None = None, *,
          rules: dict | None = None) -> dict:
    """Применить удержания к начислению месяца.

    `payroll` — результат `payroll.accrue` (строки с `accrued`, `ndfl`, `net`).
    Возвращает по сотруднику: сколько удержано по каждому документу, сколько не влезло в
    предел и переносится, итог к выплате.
    """
    rules = rules or load_rules()
    period = str(payroll.get("period") or "")
    limits = rules.get("limits") or {}
    report: dict = {"kind": "withholdings", "period": period, "rows": [], "problems": [],
                    "gaps": [], "warnings": [], "errors": [],
                    "totals": {"documents": 0, "withheld": 0.0, "carried": 0.0,
                               "net_before": 0.0, "net_after": 0.0},
                    "limits": limits, "rules": rules.get("rules") or [],
                    "not_implemented": rules.get("not_implemented") or [],
                    "title": "Удержания из заработной платы"}

    by_tab: dict[str, list[dict]] = {}
    for document in documents or []:
        by_tab.setdefault(str(document.get("tab") or ""), []).append(dict(document))

    total_withheld = Decimal("0")
    total_carried = Decimal("0")
    net_before = Decimal("0")
    net_after = Decimal("0")
    for row in payroll.get("rows") or []:
        tab = str(row.get("tab") or "")
        # база удержания — сумма после НДФЛ. Если удержания в строке уже применялись,
        # берём `accrued_net`: иначе повторный расчёт удержит алименты с уже уменьшенной суммы
        net = dec(row.get("accrued_net") if row.get("accrued_net") is not None else row.get("net"))
        net_before += net
        docs = by_tab.get(tab) or []
        slot = {"tab": tab, "name": row.get("name"), "net": num(net),
                "documents": [], "withheld": 0.0, "carried": 0.0, "net_after": num(net)}
        if not docs:
            net_after += net
            report["rows"].append(slot)
            continue

        # 1. сумма по каждому документу до предела + проверки документа
        prepared: list[dict] = []
        for document in docs:
            amount, errors, warnings = document_amount(document, rules=rules,
                                                       employee=row, net=net)
            report["errors"].extend(errors)
            report["warnings"].extend(warnings)
            if errors:
                slot["documents"].append({
                    "number": document.get("number"), "kind": document.get("kind"),
                    "title": ((rules.get("kinds") or {}).get(str(document.get("kind")))
                              or {}).get("title") or "",
                    "amount": 0.0, "withheld": 0.0, "carried": 0.0, "state": SKIPPED,
                    "why": errors[0]["why"]})
                continue
            prepared.append({"document": document, "amount": amount})

        # 2. лимит: он зависит от вида документа и не может превышать максимум по человеку
        def limit_percent(items: list[dict]) -> tuple[Decimal, str]:
            percents, basis = [], ""
            for item in items:
                spec = (rules.get("kinds") or {}).get(str(item["document"].get("kind"))) or {}
                key = str(spec.get("limit") or "enforcement")
                percent = Decimal(str((limits.get(key) or {}).get("percent") or 50))
                percents.append(percent)
                if percent == max(percents):
                    basis = (limits.get(key) or {}).get("basis") or ""
            return (max(percents) if percents else Decimal("0")), basis

        cap_percent, cap_basis = limit_percent(prepared)
        cap = kopeck(net * cap_percent / Decimal("100")) if cap_percent else Decimal("0")

        # 3. очерёдность (ст. 111 № 229-ФЗ): внутри очереди — пропорционально суммам
        queues: dict[int, list[dict]] = {}
        for item in prepared:
            kind = str(item["document"].get("kind"))
            queue = int(((rules.get("kinds") or {}).get(kind) or {}).get("queue") or 4)
            queues.setdefault(queue, []).append(item)
        left = cap
        withheld_total = Decimal("0")
        carried_total = Decimal("0")
        for queue in sorted(queues):
            items = queues[queue]
            demand = sum((item["amount"] for item in items), Decimal("0"))
            if left <= 0:
                for item in items:
                    slot["documents"].append(_doc_row(item, rules, withheld=Decimal("0"),
                                                      carried=item["amount"]))
                    carried_total += item["amount"]
                continue
            if demand <= left:
                for item in items:
                    slot["documents"].append(_doc_row(item, rules, withheld=item["amount"],
                                                      carried=Decimal("0")))
                left -= demand
                withheld_total += demand
                continue
            # очередь не влезает целиком — делим пропорционально требованиям
            for item in items:
                part = kopeck(item["amount"] * left / demand) if demand else Decimal("0")
                slot["documents"].append(_doc_row(item, rules, withheld=part,
                                                  carried=item["amount"] - part))
                withheld_total += part
                carried_total += item["amount"] - part
            left = Decimal("0")

        slot["withheld"] = num(withheld_total)
        slot["carried"] = num(carried_total)
        slot["net_after"] = num(net - withheld_total)
        slot["limit_percent"] = float(cap_percent)
        slot["limit_basis"] = cap_basis
        slot["limit_amount"] = num(cap)
        if carried_total > 0:
            report["gaps"].append({
                "what": f"остаток удержания по {row.get('name')}",
                "why": f"{rub(carried_total)} не удержано: предел {cap_percent:g} % от "
                       f"{rub(net)} после НДФЛ = {rub(cap)}; долг переносится на следующий месяц",
                "ask": "погасить остаток в следующем периоде или проверить документ"})
        total_withheld += withheld_total
        total_carried += carried_total
        net_after += net - withheld_total
        report["rows"].append(slot)

    unknown = sorted({tab for tab in by_tab if tab and not any(
        str(row.get("tab")) == tab for row in payroll.get("rows") or [])})
    if unknown:
        report["problems"].append({
            "code": "WITHHOLDING_NO_EMPLOYEE", "what": ", ".join(unknown),
            "why": "документы на удержание относятся к табельным номерам, которых нет в "
                   "начислении: удержание не применено"})
    if documents and not any(slot["documents"] for slot in report["rows"]):
        report["problems"].append({
            "code": "WITHHOLDING_NOT_APPLIED", "what": period,
            "why": "документы переданы, но ни одно удержание не применено"})
    if not documents:
        report["gaps"].append({
            "what": "документы на удержание",
            "why": "исполнительных документов не передано: удержаний нет. Это не значит, что "
                   "их нет у сотрудников — значит, в контур не введены",
            "ask": "реестр документов (алименты, исполнительные листы, распоряжения)"})

    report["totals"] = {"documents": sum(len(slot["documents"]) for slot in report["rows"]),
                        "withheld": num(total_withheld), "carried": num(total_carried),
                        "net_before": num(net_before), "net_after": num(net_after),
                        "employees": len([slot for slot in report["rows"]
                                          if slot["documents"]])}
    report["ready"] = not report["problems"] and not report["errors"]
    return report


def _doc_row(item: dict, rules: dict, *, withheld: Decimal, carried: Decimal) -> dict:
    document = item["document"]
    spec = (rules.get("kinds") or {}).get(str(document.get("kind"))) or {}
    state = OK
    if carried > 0 and withheld > 0:
        state = LIMITED
    elif withheld <= 0:
        state = SKIPPED
    return {"number": document.get("number"), "kind": document.get("kind"),
            "title": spec.get("title") or "", "payee": document.get("payee") or "",
            "amount": num(item["amount"]), "withheld": num(withheld),
            "carried": num(carried), "state": state,
            "why": (spec.get("source") or "") if state != LIMITED else
                   "удержан предел, остаток переносится"}


def apply_to_payroll(payroll: dict, documents: list[dict] | None = None, *,
                     rules: dict | None = None) -> dict:
    """Вписать удержания в начисление: `net` уменьшается, `withheld` появляется в строке.

    Функция меняет копию расчёта: исходный отчёт `payroll.accrue` остаётся тем, что посчитан.
    """
    report = apply(payroll, documents, rules=rules)
    by_tab = {str(row.get("tab")): row for row in report["rows"]}
    result = json.loads(json.dumps(payroll, ensure_ascii=False, default=str))
    for row in result.get("rows") or []:
        slot = by_tab.get(str(row.get("tab")))
        if not slot:
            continue
        row["accrued_net"] = row.get("net")
        row["withheld"] = slot["withheld"]
        row["withheld_carried"] = slot["carried"]
        row["net"] = slot["net_after"]
        row["withholdings"] = slot["documents"]
    totals = result.setdefault("totals", {})
    totals["net_accrued"] = totals.get("net")
    totals["withheld"] = report["totals"]["withheld"]
    totals["net"] = report["totals"]["net_after"]
    result["withholdings"] = {key: report[key] for key in
                              ("totals", "problems", "gaps", "warnings", "errors",
                               "limits", "not_implemented")}
    result["withholdings"]["rows"] = report["rows"]
    return result


def post_ledger(state: dict, report: dict, *, ledger_state: dict, mode: str = "проводить",
                date: str | None = None) -> dict:
    """Провести удержания: Дт 70 Кт 76 (удержано) и Дт 76 Кт 51 (перечислено взыскателю).

    Перечисление взыскателю контур не делает сам: это платёж, а платежи идут заявкой через
    казначейство. Здесь только две проводки, которые отражают факт удержания и факт перевода,
    если перевод уже был.
    """
    from . import ledger as accounting

    period = str(report.get("period") or "")
    result = {"posted": 0, "duplicates": 0, "rejected": 0, "results": [], "errors": [],
              "gaps": [], "warnings": []}
    total = dec((report.get("totals") or {}).get("withheld"))
    if total <= 0:
        result["warnings"].append("удержаний за период нет: проводить нечего")
        return result
    if len(period) != 7:
        result["errors"].append({"code": "BAD_PERIOD", "what": period,
                                 "why": "проводки требуют месяц ГГГГ-ММ"})
        return result
    line_date = date or ledger_last_day(period)
    outcome = accounting.post_entry(
        ledger_state, event="payroll_withheld", date=line_date,
        doc={"number": f"удержания из зарплаты за {period}", "kind": "исполнительные документы",
             "date": line_date},
        lines=[{"account": "70", "side": accounting.DEBIT, "amount": num(total),
                "analytics": {"сотрудник": "сводно по удержаниям"}},
               {"account": "76", "side": accounting.CREDIT, "amount": num(total)}],
        chart=accounting.load_chart(), mode=mode, key=f"payroll_withheld|{period}",
        rule="withholdings",
        memo=f"удержано по исполнительным документам за {period}")
    result["results"].append({"event": "payroll_withheld",
                              **{key: outcome.get(key) for key in
                                 ("status", "errors", "gaps", "warnings")}})
    if outcome.get("status") == accounting.STATUS_OK:
        result["posted"] += 1
    elif outcome.get("status") == accounting.STATUS_DUPLICATE:
        result["duplicates"] += 1
    else:
        result["rejected"] += 1
        result["errors"].extend(outcome.get("errors") or [])
        result["gaps"].extend(outcome.get("gaps") or [])
    result["warnings"].extend(outcome.get("warnings") or [])
    return result


def ledger_last_day(period: str) -> str:
    import calendar

    year, month = int(period[:4]), int(period[5:7])
    return f"{period}-{calendar.monthrange(year, month)[1]:02d}"


def to_markdown(report: dict) -> str:
    """Отчёт удержаний текстом: по человеку, по документам, с пределами и остатками."""
    lines = [f"# {report.get('title') or 'Удержания'} за {report.get('period')}", ""]
    limits = report.get("limits") or {}
    lines.append("> Предел считается от суммы **после НДФЛ**: распоряжение работодателя — "
                 f"{(limits.get('employer') or {}).get('percent')} %, исполнительный документ — "
                 f"{(limits.get('enforcement') or {}).get('percent')} %, алименты на "
                 "несовершеннолетних, вред здоровью, вред в связи со смертью кормильца и ущерб "
                 f"от преступления — {(limits.get('priority') or {}).get('percent')} %.")
    rows = [row for row in report.get("rows") or [] if row.get("documents")]
    if rows:
        lines.append("")
        lines.append("| сотрудник | к выплате до | документ | вид | начислено по документу | "
                     "удержано | остаток | состояние |")
        lines.append("|---|---:|---|---|---:|---:|---:|---|")
        for row in rows:
            for document in row["documents"]:
                lines.append(f"| {row.get('name')} | {rub(row.get('net'))} | "
                             f"{document.get('number')} | {document.get('title')} | "
                             f"{rub(document.get('amount'))} | {rub(document.get('withheld'))} | "
                             f"{rub(document.get('carried'))} | {document.get('state')} |")
        totals = report.get("totals") or {}
        lines.append("")
        lines.append(f"Итого: удержано {rub(totals.get('withheld'))}, перенесено "
                     f"{rub(totals.get('carried'))}; к выплате после удержаний "
                     f"{rub(totals.get('net_after'))} (было {rub(totals.get('net_before'))})")
        limited = [(row.get("name"), row.get("limit_percent"), row.get("limit_basis"))
                   for row in rows if (row.get("carried") or 0) > 0]
        for name, percent, basis in limited:
            lines.append(f"- {name}: предел {percent} % — {basis}")
    else:
        lines.append("")
        lines.append("Удержаний нет: исполнительных документов за период не передано.")
    for title, key, mark in (("Расхождения", "problems", "✗"),
                             ("Чего не хватает", "gaps", "?"),
                             ("Ошибки в документах", "errors", "✗")):
        items = report.get(key) or []
        if not items:
            continue
        lines.append("")
        lines.append(f"## {title}")
        for item in items:
            what = item.get("what") or ""
            why = item.get("why") or ""
            ask = f" → {item['ask']}" if item.get("ask") else ""
            code = f"**{item['code']}** · " if item.get("code") else ""
            lines.append(f"- {mark} {code}{what}: {why}{ask}")
    if report.get("not_implemented"):
        lines.append("")
        lines.append("## Не сделано")
        for text in report["not_implemented"]:
            lines.append(f"- {text}")
    return "\n".join(lines)
