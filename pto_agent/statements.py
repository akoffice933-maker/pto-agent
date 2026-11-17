# -*- coding: utf-8 -*-
"""Годовая бухгалтерская отчётность: баланс и отчёт о финансовых результатах.

Зачем отдельный модуль
----------------------
Закрытие года (`year_close.py`) приводит счета в порядок, но отчётности не выпускает.
Отчётность — это не «ещё один отчёт по счетам»: у неё есть утверждённая форма, коды строк
и внутренние увязки, которые проверяющий смотрит первым делом. Поэтому здесь:

* формы и коды строк берутся из справочника `data/reference/financial_statements_fsbu4.json`
  (ФСБУ 4/2023, приказ Минфина от 04.10.2023 № 157н, приложение № 10), а не из головы;
* каждая строка собирается из сальдо конкретных счетов — и в строке видно, из каких;
* строка, которую нечем заполнить, остаётся пустой **с причиной**, а не нулём;
* счета, не попавшие ни в одну строку, называются списком с суммой: это не «мелочь»,
  это деньги или долг, выпавшие из отчётности.

Что здесь принципиально
-----------------------
1. **Действующий стандарт, а не привычный.** Приказ № 66н, по которому отчётность собирали
   до 2025 года, отменён с 01.01.2025. Отчётность за 2026 год по нему не составляют.
2. **Свёрнуто, а не двумя строками.** Прибыль (84.01) и убыток (84.02) дают в 1370 сальдо.
3. **Дебиторка и кредиторка не гасятся друг другом.** Сальдо одного счёта идёт в свою
   строку актива или пассива: свёрнутая задолженность скрывает и долг, и риск.
4. **Две дороги к одной цифре.** Выручка и себестоимость сверяются с оборотами 90-х счетов,
   а результат — с оборотами 90.09 и 91.09. Отчёт и журнал должны сойтись.
5. **Отрицательный актив — ошибка.** Это не «минус в балансе»: чаще всего списали больше,
   чем было. Показывается как есть, с пометкой.
6. **Служебный счёт 000 не притворяется капиталом.** Входящий остаток, который в настоящем
   учёте пришёл бы из уставного капитала и прошлогодней прибыли, виден отдельной строкой
   «не распределено по строкам» — с суммой.
"""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from . import ledger

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "data" / "reference" / "financial_statements_fsbu4.json"
EPS = Decimal("0.01")
SERVICE_ACCOUNT = "000"


# ─────────────────────────── справочник ───────────────────────────
def load_rules(path: str | Path | None = None) -> dict:
    """Формы, коды строк, правила увязок и честный список «не сделано»."""
    return json.loads(Path(path or RULES).read_text(encoding="utf-8"))


def fmt(value) -> str:
    """Число в русской записи: 68 400 000,00 (пробел между разрядами, запятая в копейках)."""
    return f"{kopeck(value):,.2f}".replace(",", " ").replace(".", ",")


def rub(value) -> str:
    return f"{fmt(value)} ₽"


def kopeck(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(EPS, rounding=ROUND_HALF_UP)


def thousand(value) -> float:
    """Тыс. руб. — как в образце формы. Контур считает в рублях, показывает оба."""
    return float((kopeck(value) / Decimal("1000")).quantize(EPS, rounding=ROUND_HALF_UP))


def num(value) -> float:
    return float(kopeck(value))


def _chart_index(chart: dict) -> dict:
    return {str(row["code"]): row for row in chart.get("accounts") or []}


# ─────────────────────────── данные из журнала ───────────────────────────
def closings(state: dict, year: str, *, chart: dict | None = None) -> dict:
    """Сальдо каждого счёта на конец года: из журнала, а не из хранимого остатка."""
    chart = chart or ledger.load_chart()
    report = ledger.trial_balance(state, f"{year}-12", chart=chart)
    rows = {row["code"]: row for row in report["rows"]}
    rows.update({row["code"]: row for row in report.get("off_balance") or []})
    return rows


def turnovers(state: dict, year: str) -> dict:
    """Обороты за весь год по каждому счёту: Дт и Кт отдельно.

    Считается по записям журнала за все 12 месяцев, а не по одному декабрю: иначе в отчёт
    попал бы декабрь вместо года.
    """
    prefix = f"{year}-"
    out: dict[str, dict] = {}
    for entry in state.get("entries") or []:
        if not str(entry.get("period") or "").startswith(prefix):
            continue
        for line in entry.get("lines") or []:
            row = out.setdefault(line["account"], {"debit": Decimal("0"), "credit": Decimal("0")})
            row["debit" if line["side"] == ledger.DEBIT else "credit"] += kopeck(line.get("amount"))
    return {code: {"debit": num(row["debit"]), "credit": num(row["credit"])}
            for code, row in out.items()}


def _signed(row: dict | None) -> Decimal:
    """Сальдо со знаком: дебет положительный, кредит отрицательный."""
    return kopeck((row or {}).get("closing"))


def _debit_saldo(row: dict | None) -> Decimal:
    return max(_signed(row), Decimal("0"))


def _credit_saldo(row: dict | None) -> Decimal:
    return max(-_signed(row), Decimal("0"))


def natural_saldo(row: dict | None, account_type: str) -> Decimal:
    """Естественное сальдо счёта: у активного — дебетовое, у пассивного — кредитовое."""
    if account_type == "А":
        return _debit_saldo(row)
    if account_type == "П":
        return _credit_saldo(row)
    value = _signed(row)
    return _debit_saldo(row) if value >= 0 else _credit_saldo(row)


def _parse_accounts(specs: list[str]) -> list[tuple[str, bool]]:
    """`"84.01:minus:84.02"` → [(84.01, False), (84.02, True)]."""
    out: list[tuple[str, bool]] = []
    for spec in specs or []:
        text = str(spec)
        if ":minus:" in text:
            left, right = text.split(":minus:", 1)
            out.append((left, False))
            out.append((right, True))
        else:
            out.append((text, False))
    return out


def expand(code: str, index: dict) -> list[str]:
    """Счёт вместе с субсчетами: «10» — это 10, 10.01, 10.08, 10.09.

    Без этого сальдо, лежащее на субсчёте (а в контуре почти всё лежит на субсчетах),
    не попадало бы в строку, и баланс «не сходился» из-за пробела в разметке.
    """
    return [code] + sorted(one for one in index if one.startswith(f"{code}."))


def line_amount(specs: list[str], rows: dict, index: dict, *, side: str) -> tuple[Decimal, list[dict]]:
    """Сумма строки баланса и расшифровка «из каких счетов».

    В сторону актива берётся дебетовое сальдо, в пассив — кредитовое: свёрнутое сальдо
    одного и того же счёта гасило бы долг дебитора и наш долг, а это разные вещи.
    """
    total = Decimal("0")
    parts: list[dict] = []
    for spec_code, minus in _parse_accounts(specs):
        for code in expand(spec_code, index):
            amount, part = _one(code, minus, rows, index, side)
            total += amount
            if part:
                parts.append(part)
    return kopeck(total), parts


def _one(code: str, minus: bool, rows: dict, index: dict, side: str):
    """Один счёт строки: сальдо, знак и расшифровка."""
    row = rows.get(code)
    account_type = (index.get(code) or {}).get("type") or "АП"
    if minus:
        # «01:minus:02» — из первоначальной стоимости вычитается амортизация,
        # «84.01:minus:84.02» — из прибыли вычитается убыток
        value = natural_saldo(row, account_type)
    elif account_type == ("А" if side == "asset" else "П"):
        # счёт «своего» типа показывается как есть, включая минус: скрывать отрицательное
        # сальдо нельзя — иначе актив перестанет сходиться с пассивом, а причина ошибки
        # исчезнет из отчёта
        value = _signed(row) if side == "asset" else -_signed(row)
    else:
        value = _debit_saldo(row) if side == "asset" else _credit_saldo(row)
    part = ({"account": code, "title": (index.get(code) or {}).get("title") or "",
             "amount": num(value), "minus": minus} if value else None)
    return (-value if minus else value), part


# ─────────────────────────── баланс ───────────────────────────
def balance(state: dict, year: str, *, chart: dict | None = None,
            rules: dict | None = None) -> dict:
    """Бухгалтерский баланс на 31 декабря года: строки, итоги, что осталось вне строк."""
    chart = chart or ledger.load_chart()
    rules = rules or load_rules()
    index = _chart_index(chart)
    rows = closings(state, year, chart=chart)
    form = rules["balance"]

    lines: list[dict] = []
    used: set[str] = set()
    for item in form["lines"]:
        amount, parts = line_amount(item.get("accounts") or [], rows, index,
                                    side=item["side"])
        used.update(part["account"] for part in parts)
        for spec_code, _ in _parse_accounts(item.get("accounts") or []):
            used.update(expand(spec_code, index))
        line = {"code": item["code"], "title": item["title"], "section": item["section"],
                "side": item["side"], "amount": num(amount), "thousand": thousand(amount),
                "accounts": parts, "note": item.get("note") or "",
                "why_empty": item.get("why_empty") or "" if not parts else ""}
        if amount < 0:
            line["negative"] = True
        lines.append(line)

    totals = {}
    for section in form["sections"]:
        section_total = sum((kopeck(line["amount"]) for line in lines
                             if line["section"] == section["id"]), Decimal("0"))
        totals[section["total"]] = num(section_total)
    totals["1100"] = totals["1100"]
    totals["1600"] = num(kopeck(totals["1100"]) + kopeck(totals["1200"]))
    totals["1300"] = totals["1300"]
    totals["1700"] = num(kopeck(totals["1300"]) + kopeck(totals["1400"])
                         + kopeck(totals["1500"]))

    # счета с сальдо, которых нет ни в одной строке: это не «мелочь», а выпавшие суммы
    unmapped = []
    for code, row in sorted(rows.items()):
        if code in used or code == SERVICE_ACCOUNT:
            continue
        if (index.get(code) or {}).get("off_balance"):
            continue
        value = _signed(row)
        if abs(value) > EPS:
            unmapped.append({"account": code, "title": (index.get(code) or {}).get("title") or "",
                             "saldo": num(value)})
    service = num(_signed(rows.get(SERVICE_ACCOUNT))) if SERVICE_ACCOUNT in rows else 0.0

    return {"year": year, "date": f"{year}-12-31",
            "form": "бухгалтерский баланс (образец приложения № 3 к ФСБУ 4/2023)",
            "unit": "руб. (в форме — тыс. руб., обе величины в строке)",
            "sections": form["sections"], "lines": lines, "totals": totals,
            "unmapped": unmapped, "service_account": service,
            "note": (rules.get("standard") or {}).get("unit_note") or ""}


# ─────────────────────────── отчёт о финансовых результатах ───────────────────────────
def _turnover_value(rule: str, moves: dict) -> tuple[float, str]:
    """`"90.01:credit"` → оборот по кредиту счёта 90.01. Нет оборота — ноль и слово «нет»."""
    if not rule or ":" not in rule:
        return 0.0, ""
    account, side = rule.split(":", 1)
    row = moves.get(account) or {}
    value = row.get("credit" if side == "credit" else "debit") or 0.0
    return float(value), account


def pnl(state: dict, year: str, *, rules: dict | None = None) -> dict:
    """Отчёт о финансовых результатах за год: цепочка от выручки к чистой прибыли."""
    rules = rules or load_rules()
    moves = turnovers(state, year)
    form = rules["pnl"]
    values: dict[str, float] = {}
    lines: list[dict] = []

    for item in form["lines"]:
        code = item["code"]
        entry = {"code": code, "title": item["title"], "note": item.get("note") or "",
                 "why_empty": "", "sources": []}
        if item.get("formula"):
            total = 0.0
            for part in item["formula"]:
                sign = -1 if part.startswith("-") else 1
                ref = part.lstrip("+-")
                total += sign * float(values.get(ref) or 0.0)
            entry["amount"] = num(total)
            entry["formula"] = item["formula"]
        else:
            value, account = _turnover_value(item.get("rule") or "", moves)
            minus_total = 0.0
            for rule in item.get("minus") or []:
                part_value, part_account = _turnover_value(rule, moves)
                minus_total += part_value
                if part_value:
                    entry["sources"].append({"account": part_account, "amount": -part_value,
                                             "as": "минус"})
            if account and value:
                entry["sources"].insert(0, {"account": account, "amount": value,
                                            "as": item.get("rule")})
            entry["amount"] = num(value - minus_total)
            entry["negative"] = bool(item.get("negative"))
            if not value and not minus_total:
                entry["not_calculated"] = True
                entry["why_empty"] = item.get("why_empty") or "оборотов нет: считать нечего"
        values[code] = entry["amount"]
        lines.append(entry)

    return {"year": year, "form": "отчёт о финансовых результатах (образец приложения № 4 к ФСБУ 4/2023)",
            "unit": "руб.", "lines": lines, "values": values}


def profit_tax_gap(state: dict, year: str) -> dict:
    """Сколько налога на прибыль контур посчитал и почему он не попал в отчётность.

    Налог считается налоговым модулем по месяцам, но в журнал не проводится: счёта 68.04
    в рабочем плане нет. Поэтому строка 2410 пустая, а разрыв называется цифрой — иначе
    «чистая прибыль» в отчёте тихо равна прибыли до налога.
    """
    from . import tax

    total = 0.0
    months = []
    for month in range(1, 13):
        period = f"{year}-{month:02d}"
        try:
            report = tax.profit_return(state, period)
        except Exception:                      # демо может не покрывать все месяцы
            continue
        amount = float((report.get("totals") or {}).get("tax") or 0.0)
        if amount:
            months.append({"period": period, "tax": amount})
            total += amount
    return {"amount": num(total), "months": months,
            "what": "налог на прибыль за год посчитан, но не проведён в журнал",
            "why": ("в рабочем плане счетов нет счёта 68.04, поэтому строка 2410 пустая, "
                    "а 2400 показана до налога. Разрыв назван числом, а не скрыт нулём")}


# ─────────────────────────── проверки ───────────────────────────
def _check(code: str, what: str, ok: bool, why: str = "", **extra) -> dict:
    item = {"code": code, "what": what, "ok": bool(ok), "why": "" if ok else why}
    item.update(extra)
    return item


def check(state: dict, year: str, *, chart: dict | None = None,
          rules: dict | None = None) -> dict:
    """Увязки, без которых отчётность не отчётность."""
    chart = chart or ledger.load_chart()
    rules = rules or load_rules()
    index = _chart_index(chart)
    sheet = balance(state, year, chart=chart, rules=rules)
    profit = pnl(state, year, rules=rules)
    values = profit["values"]
    moves = turnovers(state, year)
    rows = closings(state, year, chart=chart)
    checks: list[dict] = []

    # 1. актив = пассив
    assets, liabilities = sheet["totals"]["1600"], sheet["totals"]["1700"]
    checks.append(_check("BALANCE_TIES", "актив 1600 = пассив 1700",
                         abs(assets - liabilities) <= 0.01,
                         f"актив {rub(assets)} ≠ пассив {rub(liabilities)}: "
                         f"разница {rub(assets - liabilities)}",
                         assets=assets, liabilities=liabilities))

    # 2. итоги разделов следуют из строк
    lines_by_section = {}
    for line in sheet["lines"]:
        lines_by_section[line["section"]] = lines_by_section.get(line["section"], 0.0) + line["amount"]
    section_ok = all(abs(lines_by_section.get(section["id"], 0.0) - sheet["totals"][section["total"]]) <= 0.01
                     for section in sheet["sections"])
    checks.append(_check("SECTION_TIES", "итоги разделов равны сумме своих строк", section_ok,
                         "итог раздела не равен сумме строк"))

    # 3. отрицательных строк в балансе быть не может: это ошибка учёта, а не «минус»
    negative = [line for line in sheet["lines"] if line["amount"] < -0.01]
    checks.append(_check("NO_NEGATIVE_LINES", "в балансе нет отрицательных строк",
                         not negative,
                         "; ".join(f"{line['code']} «{line['title']}» = {rub(line['amount'])}"
                                   for line in negative)
                         + (" — отрицательное сальдо по активному счёту значит, что списали "
                            "больше, чем было. Строка показана как есть, чтобы баланс сошёлся, "
                            "но отчётность с ней подписывать нельзя: сначала исправляется учёт"
                            if negative else ""),
                         lines=[line["code"] for line in negative]))

    # 4. все счета с сальдо попали в строки
    checks.append(_check("UNMAPPED_ACCOUNTS", "у каждого счёта с сальдо есть строка баланса",
                         not sheet["unmapped"],
                         "вне строк остались: " + ", ".join(
                             f"{item['account']} ({rub(item['saldo'])})"
                             for item in sheet["unmapped"]),
                         unmapped=sheet["unmapped"]))
    checks.append(_check("SERVICE_ACCOUNT", "служебный счёт 000 не распределён по строкам — "
                                            "входящего капитала в контуре нет",
                         abs(sheet["service_account"]) <= 0.01,
                         f"на счёте 000 висит {rub(sheet['service_account'])}: в настоящем учёте "
                         f"это уставный капитал и прошлогодняя прибыль, а не «прочий актив». "
                         f"В балансе суммы нет — она названа здесь",
                         amount=sheet["service_account"]))

    # 5. 99 закрыт реформацией
    saldo_99 = _signed(rows.get("99"))
    checks.append(_check("PROFIT_CLOSED", "счёт 99 закрыт реформацией (сальдо ноль)",
                         abs(saldo_99) <= EPS,
                         f"на 99 осталось {rub(saldo_99)}: год не закрыт, чистая прибыль "
                         f"в балансе не подтверждена", amount=num(saldo_99)))

    # 6. цепочка ОФР
    chain = [
        ("2100", ["2110", "-2120"]), ("2200", ["2100", "-2210", "-2220"]),
        ("2300", ["2200", "+2310", "+2320", "-2330", "+2340", "-2350"]),
        ("2400", ["2300", "-2410", "-2420", "+2460"]),
        ("2500", ["2400", "+2510", "+2520", "-2530"]),
    ]
    broken = []
    for code, formula in chain:
        total = 0.0
        for part in formula:
            sign = -1 if part.startswith("-") else 1
            total += sign * float(values.get(part.lstrip("+-")) or 0.0)
        if abs(total - float(values.get(code) or 0.0)) > 0.01:
            broken.append(f"{code}: {rub(values.get(code) or 0)} вместо {rub(total)}")
    checks.append(_check("PNL_CHAIN", "цепочка ОФР пересчитывается", not broken,
                         "; ".join(broken)))

    # 7. отчёт против журнала
    sales = float(values.get("2110") or 0.0)
    cost = float(values.get("2120") or 0.0)
    from_90 = float((moves.get("90.01") or {}).get("credit") or 0.0) \
        - float((moves.get("90.03") or {}).get("debit") or 0.0)
    cost_90 = float((moves.get("90.02") or {}).get("debit") or 0.0)
    # 90.09 и 91.09 закрываются ежемесячно, их сальдо всегда ноль — поэтому результат
    # берётся оттуда, где он реально осел: из реформации (обороты 84.01 и 84.02)
    profit_90 = (float((moves.get("84.01") or {}).get("credit") or 0.0)
                 - float((moves.get("84.01") or {}).get("debit") or 0.0)
                 + float((moves.get("84.02") or {}).get("debit") or 0.0)
                 - float((moves.get("84.02") or {}).get("credit") or 0.0))
    diffs = []
    if abs(sales - from_90) > 0.01:
        diffs.append(f"2110 {rub(sales)} ≠ 90.01−90.03 {rub(from_90)}")
    if abs(cost - cost_90) > 0.01:
        diffs.append(f"2120 {rub(cost)} ≠ 90.02 {rub(cost_90)}")
    if abs(float(values.get("2300") or 0.0) - profit_90) > 0.01:
        diffs.append(f"2300 {rub(values.get('2300') or 0)} ≠ реформация 84 {rub(profit_90)}")
    checks.append(_check("PNL_VS_TURNOVER", "ОФР совпадает с журналом: выручка и себестоимость — "
                                            "с 90-ми, результат — с реформацией 84", not diffs,
                         "; ".join(diffs)))

    # 8. прибыль из ОФР должна оказаться в капитале
    retained = next((line["amount"] for line in sheet["lines"] if line["code"] == "1370"), 0.0)
    net = float(values.get("2400") or 0.0)
    prior = _prior_retained(state, year)
    expected = prior + net
    checks.append(_check("RETAINED_VS_PNL",
                         "1370 баланса = прошлый остаток + 2400 отчёта",
                         abs(retained - expected) <= 0.01,
                         f"в 1370 {rub(retained)}, а по отчёту должно быть {rub(expected)} "
                         f"(прошлый остаток {rub(prior)} + чистая прибыль {rub(net)})",
                         retained=retained, expected=num(expected), net_profit=num(net)))

    problems = [item for item in checks if not item["ok"]]
    gaps = [profit_tax_gap(state, year)]
    return {"year": year, "checks": checks, "problems": problems, "gaps": gaps,
            "ok": not problems,
            "balance": sheet, "pnl": profit,
            "not_implemented": list(rules.get("not_implemented") or [])}


def _prior_retained(state: dict, year: str) -> float:
    """Остаток 84 на начало года: из проводок до января отчётного года."""
    total = Decimal("0")
    for entry in state.get("entries") or []:
        if str(entry.get("period") or "") >= f"{year}-01":
            continue
        for line in entry.get("lines") or []:
            if not str(line["account"]).startswith("84"):
                continue
            sign = 1 if line["side"] == ledger.CREDIT else -1
            total += kopeck(line.get("amount")) * sign
    return num(total)


# ─────────────────────────── текст ───────────────────────────
def to_markdown(report: dict) -> str:
    """Отчётность в markdown: две формы, проверки и честный список «не сделано»."""
    sheet, profit = report["balance"], report["pnl"]
    out = [f"# Бухгалтерская отчётность за {report['year']} год", "",
           f"Форма: {sheet['form']}; {profit['form']}.", "",
           f"Стандарт: ФСБУ 4/2023 (приказ Минфина от 04.10.2023 № 157н). "
           f"Единица: {sheet['unit']}.", ""]

    out += ["## Бухгалтерский баланс", "",
            "| строка | наименование | счета | сумма, ₽ | тыс. ₽ |",
            "|---|---|---|---:|---:|"]
    for section in sheet["sections"]:
        out.append(f"| | **{section['title']}** | | | |")
        for line in sheet["lines"]:
            if line["section"] != section["id"]:
                continue
            accounts = ", ".join(f"{part['account']}{'−' if part['minus'] else ''}"
                                 for part in line["accounts"]) or "—"
            mark = " ⚠" if line.get("negative") else ""
            out.append(f"| {line['code']} | {line['title']}{mark} | {accounts} | "
                       f"{fmt(line['amount'])} | {fmt(line['thousand'])} |")
        out.append(f"| **{section['total']}** | **итого {section['title'].split('. ')[-1].lower()}** "
                   f"| | **{fmt(sheet['totals'][section['total']])}** | |")
        out.append("")
    out.append(f"**1600 баланс (актив) = {fmt(sheet['totals']['1600'])} ₽**, "
               f"**1700 баланс (пассив) = {fmt(sheet['totals']['1700'])} ₽**.")

    empty = [line for line in sheet["lines"] if not line["accounts"] and line["why_empty"]]
    if empty:
        out += ["", "### Строки, которые нечем заполнить", ""]
        for line in empty:
            out.append(f"- **{line['code']} {line['title']}** — {line['why_empty']}")

    out += ["", "## Отчёт о финансовых результатах", "",
            "| строка | наименование | сумма, ₽ | тыс. ₽ |", "|---|---|---:|---:|"]
    for line in profit["lines"]:
        amount = -abs(line["amount"]) if line.get("negative") and line["amount"] else line["amount"]
        mark = " (не считается)" if line.get("not_calculated") else ""
        out.append(f"| {line['code']} | {line['title']}{mark} | {fmt(amount)} | "
                   f"{fmt(thousand(amount))} |")
    empty_pnl = [line for line in profit["lines"] if line.get("not_calculated")]
    if empty_pnl:
        out += ["", "### Строки ОФР, которые не заполняются", ""]
        for line in empty_pnl:
            out.append(f"- **{line['code']} {line['title']}** — {line['why_empty']}")

    out += ["", "## Проверки", "", "| код | что проверено | результат |", "|---|---|---|"]
    for item in report["checks"]:
        out.append(f"| {item['code']} | {item['what']} | "
                   f"{'ок' if item['ok'] else '✗ ' + item['why']} |")
    out += ["", f"Проверок {len(report['checks'])}: нарушений {len(report['problems'])}.", ""]
    if report.get("gaps"):
        out += ["## Разрывы, названные числом", ""]
        for gap in report["gaps"]:
            out.append(f"- **{gap['what']}: {rub(gap['amount'])}** — {gap['why']}")
    if report.get("not_implemented"):
        out += ["## Не сделано (и почему)", ""]
        for item in report["not_implemented"]:
            out.append(f"- **{item.get('what')}**: {item.get('why')}")
    return "\n".join(out) + "\n"


def screen_text(report: dict) -> str:
    """Короткая сводка для терминала: цифры, проверки, что не сделано."""
    sheet, profit = report["balance"], report["pnl"]
    values = profit["values"]
    lines = [f"отчётность за {report['year']}: ФСБУ 4/2023, коды строк — приложение № 10",
             f"  баланс: актив {rub(sheet['totals']['1600'])} · пассив "
             f"{rub(sheet['totals']['1700'])} · "
             f"{'сходится' if abs(sheet['totals']['1600'] - sheet['totals']['1700']) < 0.01 else 'НЕ сходится'}",
             f"  ОФР: выручка {rub(values.get('2110'))} · себестоимость {rub(values.get('2120'))} · "
             f"прибыль от продаж {rub(values.get('2200'))} · до налогообложения "
             f"{rub(values.get('2300'))} · чистая {rub(values.get('2400'))}",
             f"  проверок {len(report['checks'])}, нарушений {len(report['problems'])}"]
    for gap in report.get("gaps") or []:
        lines.append(f"  разрыв: {gap['what']} — {rub(gap['amount'])} "
                     f"(строка 2410 пустая, 2400 без налога)")
    for item in report["problems"]:
        lines.append(f"  ✗ [{item['code']}] {item['what']}: {item['why']}")
    if report.get("not_implemented"):
        lines.append(f"  не сделано: {len(report['not_implemented'])} пунктов "
                     f"(отчёт об изменениях капитала, ОДДС, пояснения, ПБУ 18/02, сдача в ФНС)")
    return "\n".join(lines)
