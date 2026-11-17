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


def _year(value) -> str:
    """Год — строкой: `2026` и `"2026"` должны значить одно и то же."""
    return str(value).strip()[:4]


def prior_year(year) -> str:
    """Предыдущий отчётный год: для графы «на 31 декабря предыдущего года»."""
    return str(int(_year(year)) - 1)


def years_in(state: dict) -> list[str]:
    """Годы, за которые в журнале есть проводки (по возрастанию)."""
    years = {str(entry.get("period") or "")[:4] for entry in state.get("entries") or []}
    return sorted(year for year in years if year.isdigit())


def prior_closed(state: dict, year) -> bool:
    """Закрыт ли предыдущий год: реформация есть, сальдо 99 нулевое."""
    prior = prior_year(year)
    if not any(str(item.get("year")) == prior and item.get("closed")
               for item in state.get("years") or []):
        return False
    rows = closings(state, prior)
    return abs(_signed(rows.get("99"))) <= EPS


def prior_entries(state: dict, year) -> int:
    """Сколько проводок в журнале за предыдущий год. Ноль — сравнивать не с чем."""
    prefix = f"{prior_year(year)}-"
    return sum(1 for entry in state.get("entries") or []
               if str(entry.get("period") or "").startswith(prefix))


def turnovers(state: dict, year: str) -> dict:
    """Обороты за весь год по каждому счёту: Дт и Кт отдельно.

    Считается по записям журнала за все 12 месяцев, а не по одному декабрю: иначе в отчёт
    попал бы декабрь вместо года.
    """
    year = _year(year)
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
def _balance_lines(form: dict, rows: dict, index: dict) -> tuple[list[dict], set[str]]:
    """Строки баланса из сальдо: так одну и ту же формулу можно посчитать за два года."""
    lines: list[dict] = []
    used: set[str] = set()
    for item in form["lines"]:
        amount, parts = line_amount(item.get("accounts") or [], rows, index,
                                    side=item["side"])
        used.update(part["account"] for part in parts)
        specs = [code for code, _ in _parse_accounts(item.get("accounts") or [])]
        for spec_code in specs:
            used.update(expand(spec_code, index))
        # пустая строка обязана называть причину: «ноль, потому что счёта нет» — это тоже
        # причина, и она отличается от «ноль, потому что остатка нет»
        reason = item.get("why_empty") or ""
        if not parts and not reason:
            reason = (f"сальдо по счетам {', '.join(specs)} нулевое: счёт в рабочем плане есть, "
                      f"остатка за год нет"
                      if specs else "строка не заполняется: в форме ей нечего соответствовать")
        line = {"code": item["code"], "title": item["title"], "section": item["section"],
                "side": item["side"], "amount": num(amount), "thousand": thousand(amount),
                "accounts": parts, "note": item.get("note") or "",
                "why_empty": reason if not parts else ""}
        if amount < 0:
            line["negative"] = True
        lines.append(line)
    return lines, used


def _section_totals(form: dict, lines: list[dict]) -> dict:
    totals = {}
    for section in form["sections"]:
        section_total = sum((kopeck(line["amount"]) for line in lines
                             if line["section"] == section["id"]), Decimal("0"))
        totals[section["total"]] = num(section_total)
    totals["1600"] = num(kopeck(totals["1100"]) + kopeck(totals["1200"]))
    totals["1700"] = num(kopeck(totals["1300"]) + kopeck(totals["1400"])
                         + kopeck(totals["1500"]))
    return totals


def balance(state: dict, year: str, *, chart: dict | None = None,
            rules: dict | None = None) -> dict:
    """Бухгалтерский баланс на 31 декабря: строки, итоги и графа прошлого года.

    Сравнительные показатели считаются **той же формулой** по сальдо прошлого года, а не
    берутся из прошлогоднего файла: иначе «прошлый год» мог бы разойтись с тем, что этот же
    контур показал год назад. Если за прошлый год в журнале нет проводок, графа не
    заполняется, и это сказано причиной (`prior.reason`), а не пустой клеткой.
    """
    year = _year(year)
    chart = chart or ledger.load_chart()
    rules = rules or load_rules()
    index = _chart_index(chart)
    form = rules["balance"]

    rows = closings(state, year, chart=chart)
    lines, used = _balance_lines(form, rows, index)
    totals = _section_totals(form, lines)

    prior_rows = closings(state, prior_year(year), chart=chart)
    prior_lines, _ = _balance_lines(form, prior_rows, index)
    prior_totals = _section_totals(form, prior_lines)
    has_prior = prior_entries(state, year) > 0
    for line, prior in zip(lines, prior_lines):
        line["prior"] = prior["amount"] if has_prior else None
        line["prior_thousand"] = prior["thousand"] if has_prior else None
    first_year = not years_in(state) or min(years_in(state)) == year
    prior = {"year": prior_year(year), "date": f"{prior_year(year)}-12-31",
             "available": has_prior, "closed": prior_closed(state, year) if has_prior else None,
             "first_year": first_year and not has_prior,
             "totals": prior_totals if has_prior else {},
             "lines": ({item["code"]: item["amount"] for item in prior_lines}
                       if has_prior else {}),
             "reason": "" if has_prior else
             (f"графа «на 31 декабря {prior_year(year)} года» не заполняется: это первый "
              f"отчётный год — в журнале нет проводок за {prior_year(year)}. Первый год "
              f"показывается без сравнительных данных, и это сказано здесь, а не пустой клеткой"
              if first_year else
              f"графа «на 31 декабря {prior_year(year)} года» не заполняется: за "
              f"{prior_year(year)} год проводок нет, хотя журнал ведётся с "
              f"{min(years_in(state))}-го — данные прошлого года потеряны")}

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

    return {"year": year, "date": f"{year}-12-31", "prior": prior,
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
    """Отчёт о финансовых результатах за год: цепочка от выручки к чистой прибыли.

    Рядом считается тот же отчёт за предыдущий год — для графы сравнительных показателей.
    """
    year = _year(year)
    rules = rules or load_rules()
    form = rules["pnl"]
    values, lines = _pnl_values(turnovers(state, year), form)
    has_prior = prior_entries(state, year) > 0
    prior_values, prior_lines = _pnl_values(turnovers(state, prior_year(year)), form)
    for line, prior in zip(lines, prior_lines):
        line["prior"] = prior["amount"] if has_prior else None
    return {"year": year, "prior_year": prior_year(year), "prior_available": has_prior,
            "form": "отчёт о финансовых результатах (образец приложения № 4 к ФСБУ 4/2023)",
            "unit": "руб.", "lines": lines, "values": values,
            "prior_values": prior_values if has_prior else {}}


def _pnl_values(moves: dict, form: dict) -> tuple[dict, list[dict]]:
    """Строки ОФР из оборотов: одна и та же формула для любого года."""
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
    return values, lines


def profit_tax(state: dict, year: str, *, rules: dict | None = None) -> dict:
    """Налог на прибыль: сколько начислено в журнале и сколько насчитал налоговый модуль.

    Круг 44: счёт 68.04 в рабочем плане есть, и закрытие года проводит `Дт 99 Кт 68.04`
    (шаг `profit_tax`). Поэтому строка 2410 теперь заполняется из журнала, а не остаётся
    пустой. Но «проведено» и «посчитано» — две разные цифры, и обе показываются:

    * `posted` — кредитовый оборот 68.04 за год (то, что реально легло в учёт и в 2410);
    * `computed` — налог за год по налоговому модулю (8 % + 17 % от прибыли от реализации);
    * `months_sum` — сумма помесячных расчётов: она **не обязана** совпадать с годом
      (ст. 286 НК РФ — период считается нарастающим итогом, плюс помесячные округления),
      и расхождение называется, а не прячется;
    * `difference` — расхождение «проведено против посчитано». Ноль — хорошо; не ноль —
      это разрыв, который видно в отчёте.
    """
    from . import tax

    year = _year(year)
    moves = turnovers(state, year)
    posted = float((moves.get("68.04") or {}).get("credit") or 0.0)
    report = tax.profit_return(state, year)
    computed = float((report.get("totals") or {}).get("tax") or 0.0)
    months = []
    for month in range(1, 13):
        period = f"{year}-{month:02d}"
        amount = float((tax.profit_return(state, period).get("totals") or {}).get("tax") or 0.0)
        if amount:
            months.append({"period": period, "tax": num(amount)})
    months_sum = float(sum(item["tax"] for item in months))
    return {"posted": num(posted), "computed": num(computed),
            "months_sum": num(months_sum), "difference": num(posted - computed),
            "months": months,
            "base": (report.get("totals") or {}).get("profit"),
            "rates": {"federal": (report.get("totals") or {}).get("federal_rate"),
                      "regional": (report.get("totals") or {}).get("regional_rate")},
            "problems": list(report.get("problems") or []),
            "not_included": list(report.get("not_included") or []),
            "why_diff": ("сумма помесячных расчётов и налог за год расходятся законно: "
                         "период считается нарастающим итогом (ст. 286 НК РФ), а каждый месяц "
                         "округляется отдельно. В отчётность идёт год, а не сумма месяцев"
                         if abs(months_sum - computed) > 0.01 else "")}


def profit_tax_gap(state: dict, year: str) -> dict:
    """Разрыв по налогу — только если он есть: проведённое ≠ посчитанному.

    Раньше (круг 43) разрыв был «по определению»: счёта 68.04 не было, и налог не проводился.
    Теперь он есть и проводится шагом закрытия года, поэтому разрыв вычисляется, а не
    предполагается. Если разрыва нет — возвращается пустая сумма и объяснение, почему ноль
    здесь означает «сходится», а не «не считали».
    """
    info = profit_tax(state, year)
    difference = float(info["difference"])
    return {"amount": num(abs(difference)), "code": "PROFIT_TAX_GAP",
            "what": "налог на прибыль: проведено против посчитанного",
            "posted": info["posted"], "computed": info["computed"],
            "months_sum": info["months_sum"], "why_diff": info["why_diff"],
            "why": ("в журнале 68.04 {posted}, налоговый модуль насчитал {computed}: "
                    "налог не проведён или проведён другой суммой — строка 2410 и 2400 "
                    "в отчётности будут неверными".format(
                        posted=rub(info["posted"]), computed=rub(info["computed"]))
                    if abs(difference) > 0.01 else
                    "налог проведён и совпадает с расчётом: 2410 в отчёте — из журнала, "
                    "а не «по памяти налогового модуля»")}


def readiness(state: dict, year: str, *, chart: dict | None = None) -> list[dict]:
    """Чего не хватает, чтобы ОДК и ОДДС считались, а не рисовались.

    Обе формы входят в годовую отчётность, но их нельзя «собрать из ничего»:

    * **ОДДС** (4000–4500) требует движения денег: касса 50, расчётные счета 51, валюта 52,
      специальные счета 55, переводы в пути 57 — и у каждого платежа назначение (какой вид
      деятельности: текущая, инвестиционная, финансовая);
    * **ОДК** (3000–3300) требует капитала: уставный (80), добавочный (83), резервный (82),
      собственные акции (81) и движений по 84, кроме одной реформации: дивиденды, пополнения,
      переоценка.

    Функция не «оценивает готовность» на глаз: она смотрит рабочий план и журнал и называет,
    что именно отсутствует. Пока данных нет — формы не строятся, и это сказано на экране.
    Когда данные появятся, здесь же станет видно, что́ уже есть, а чего ещё нет.
    """
    year = _year(year)
    chart = chart or ledger.load_chart()
    index = _chart_index(chart)
    moves = turnovers(state, year)

    cash = ("50", "51", "52", "55", "57")
    cash_absent = [code for code in cash if code not in index]
    cash_silent = [code for code in cash if code in index and not moves.get(code)]
    odds = {
        "form": "отчёт о движении денежных средств (4000–4500)",
        "short": "ОДДС",
        "built": False,
        "missing": ([f"счетов нет в рабочем плане: {', '.join(cash_absent)}"] if cash_absent else [])
                   + ([f"счета есть, но оборотов за {year} нет: {', '.join(cash_silent)}"]
                      if cash_silent else []),
        "need": ("банковские выписки и касса: дата, сумма, контрагент, назначение платежа "
                 "(вид деятельности по п. 8–11 ПБУ 23/2011)"),
        "why": ("форма не строится: без движения денег и назначения платежей строки 4110–4500 "
                "можно только выдумать"),
    }

    capital = ("80", "81", "82", "83", "84")
    capital_absent = [code for code in capital if code not in index]
    capital_moves = {code: moves.get(code) for code in capital if moves.get(code)}
    only_close = [code for code, row in capital_moves.items()
                  if code.startswith("84")
                  and not float((row or {}).get("debit") or 0.0) - float((row or {}).get("credit") or 0.0)]
    odk = {
        "form": "отчёт об изменениях капитала (3000–3300)",
        "short": "ОДК",
        "built": False,
        "missing": ([f"счетов нет в рабочем плане: {', '.join(capital_absent)}"] if capital_absent else [])
                   + ([f"по 84 только реформация: {len(only_close)}/1 проводка"] if capital_moves else
                      [f"движений по капиталу за {year} нет вовсе"]),
        "need": ("движения капитала: вклад/выкуп долей, дивиденды, переоценка, резервный фонд — "
                 "каждое с документом и датой"),
        "why": ("форма не строится: одна реформация 99 → 84 — это перенос результата, а не "
                "изменение капитала за счёт операций с собственниками"),
    }
    return [odk, odds]


# ─────────────────────────── проверки ───────────────────────────
def _check(code: str, what: str, ok: bool, why: str = "", **extra) -> dict:
    item = {"code": code, "what": what, "ok": bool(ok), "why": "" if ok else why}
    item.update(extra)
    return item


def check(state: dict, year: str, *, chart: dict | None = None,
          rules: dict | None = None) -> dict:
    """Увязки, без которых отчётность не отчётность."""
    year = _year(year)
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
    # на 84 ложится чистая прибыль (после налога), поэтому сверяется 2400, а не 2300
    net = float(values.get("2400") or 0.0)
    if abs(net - profit_90) > 0.01:
        diffs.append(f"2400 {rub(net)} ≠ реформация 84 {rub(profit_90)}")
    checks.append(_check("PNL_VS_TURNOVER", "ОФР совпадает с журналом: выручка и себестоимость — "
                                            "с 90-ми, чистая прибыль — с реформацией 84",
                         not diffs, "; ".join(diffs)))

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

    # 8б. сравнительные показатели: сопоставимость с прошлым годом
    prior = sheet["prior"]
    # сравнительные показатели: их отсутствие — не ошибка только в первом отчётном году
    checks.append(_check(
        "PRIOR_COMPARATIVES",
        f"есть графа «на 31 декабря {prior['year']} года» — сравнивать есть с чем",
        prior["available"] or prior["first_year"], prior["reason"],
        available=prior["available"], first_year=prior["first_year"],
        prior_year=prior["year"]))
    if prior["available"]:
        checks.append(_check(
            "PRIOR_CLOSED",
            f"{prior['year']} год закрыт: сравнительные показатели — из закрытого года",
            bool(prior["closed"]),
            f"за {prior['year']} есть проводки, но год не закрыт (нет реформации 99 → 84): "
            f"сравнительные показатели показывают не результат года, а незакрытый журнал"))
    if prior["available"]:
        p_assets = prior["totals"]["1600"]
        p_liab = prior["totals"]["1700"]
        checks.append(_check("PRIOR_BALANCE_TIES",
                             f"баланс за {prior['year']} сходится: 1600 = 1700",
                             abs(p_assets - p_liab) <= 0.01,
                             f"актив {rub(p_assets)} ≠ пассив {rub(p_liab)}: прошлый год "
                             f"показан в отчёте кривым"))
        # непрерывность: прошлогоднее сальдо и входящее сальдо этого года — одна и та же сумма
        opening = closings(state, prior["year"], chart=chart)
        mismatch = []
        for code, row in sorted(opening.items()):
            if (index.get(code) or {}).get("off_balance"):
                continue
            value = _signed(row)
            if abs(value) <= EPS:
                continue
            # сальдо «на 01.01» — это сальдо до первой проводки года: сумма по счёту
            # на конец предыдущего дня, то есть ровно прошлогоднее закрытие
            start = ledger.balance_as_of(state, code, f"{year}-01-01")
            start_value = sum((kopeck(item) for item in start.values()), Decimal("0"))
            if abs(start_value - value) > 0.01:
                mismatch.append(f"{code}: на 31.12.{prior['year']} {rub(value)}, "
                                f"на 01.01.{year} {rub(start_value)}")
        checks.append(_check("OPENING_MATCHES_PRIOR",
                             "входящие остатки года равны сальдо прошлого года",
                             not mismatch, "; ".join(mismatch)))
        # прибыль прошлого года + прибыль этого = остаток 84 на конец этого года
        prior_retained = float(prior["lines"].get("1370") or 0.0)
        checks.append(_check(
            "RETAINED_CONTINUITY",
            "1370 этого года = 1370 прошлого года + чистая прибыль этого года",
            abs(retained - (prior_retained + net)) <= 0.01,
            f"в 1370 {rub(retained)}, а прошлый остаток {rub(prior_retained)} + чистая "
            f"прибыль {rub(net)} = {rub(prior_retained + net)}: прибыль прошлых лет "
            f"потерялась или посчитана дважды",
            prior_retained=num(prior_retained)))
        # цепочка прошлого года тоже обязана пересчитываться
        prior_values = profit["prior_values"]
        broken_prior = []
        for code, formula in chain:
            total = 0.0
            for part in formula:
                sign = -1 if part.startswith("-") else 1
                total += sign * float(prior_values.get(part.lstrip("+-")) or 0.0)
            if abs(total - float(prior_values.get(code) or 0.0)) > 0.01:
                broken_prior.append(f"{code}: {rub(prior_values.get(code) or 0)} вместо {rub(total)}")
        checks.append(_check("PNL_PRIOR_CHAIN",
                             f"цепочка ОФР за {prior['year']} пересчитывается",
                             not broken_prior, "; ".join(broken_prior)))

    # 9. налог на прибыль проведён и совпадает с расчётом
    info = profit_tax(state, year)
    tax_ok = abs(float(info["difference"])) <= 0.01
    tax_line = float(values.get("2410") or 0.0)
    checks.append(_check("TAX_POSTED",
                         "налог на прибыль проведён (68.04) и равен расчёту налогового модуля",
                         tax_ok,
                         f"в журнале 68.04 {rub(info['posted'])}, расчёт "
                         f"{rub(info['computed'])}: разница {rub(info['difference'])}. "
                         f"Строка 2410 в ОФР = {rub(abs(tax_line))}",
                         posted=info["posted"], computed=info["computed"],
                         months_sum=info["months_sum"]))
    checks.append(_check("TAX_BASE_MATCHES",
                         "база налога совпадает с прибылью до налога в ОФР",
                         abs(float(info["base"] or 0.0) - float(values.get("2300") or 0.0)) <= 0.01,
                         f"налоговый модуль считает от {rub(info['base'])}, а 2300 ОФР = "
                         f"{rub(values.get('2300'))}: базы разные, значит и налог не тот"))

    # 10. в отчёте нет «тихих нулей»: у каждой пустой строки есть причина
    silent = [line["code"] for line in sheet["lines"] if not line["accounts"]
              and not line["why_empty"]]
    silent += [line["code"] for line in profit["lines"] if line.get("not_calculated")
               and not line.get("why_empty")]
    checks.append(_check("NO_SILENT_ZEROS", "у каждой незаполненной строки названа причина",
                         not silent, "строки без причины: " + ", ".join(silent)))

    forms = readiness(state, year, chart=chart)
    problems = [item for item in checks if not item["ok"]]
    gap = profit_tax_gap(state, year)
    gaps = [gap] if float(gap["amount"]) > 0.01 else []
    return {"year": year, "checks": checks, "problems": problems, "gaps": gaps,
            "ok": not problems, "tax": info, "readiness": forms,
            "balance": sheet, "pnl": profit,
            "not_implemented": list(rules.get("not_implemented") or [])}


def _prior_retained(state: dict, year: str) -> float:
    """Остаток 84 на начало года: из проводок до января отчётного года."""
    year = _year(year)
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

    prior = sheet.get("prior") or {}
    prior_head = (f"на 31.12.{prior['year']}" if prior.get("available") else "прошлый год")
    out += ["## Бухгалтерский баланс", "",
            f"| строка | наименование | счета | {prior_head}, ₽ | на 31.12.{report['year']}, ₽ "
            f"| тыс. ₽ |",
            "|---|---|---|---:|---:|---:|"]
    for section in sheet["sections"]:
        out.append(f"| | **{section['title']}** | | | |")
        for line in sheet["lines"]:
            if line["section"] != section["id"]:
                continue
            accounts = ", ".join(f"{part['account']}{'−' if part['minus'] else ''}"
                                 for part in line["accounts"]) or "—"
            mark = " ⚠" if line.get("negative") else ""
            prior_cell = (fmt(line["prior"]) if line.get("prior") is not None else "—")
            out.append(f"| {line['code']} | {line['title']}{mark} | {accounts} | "
                       f"{prior_cell} | {fmt(line['amount'])} | {fmt(line['thousand'])} |")
        prior_total = (fmt(prior["totals"][section["total"]])
                       if prior.get("available") else "—")
        out.append(f"| **{section['total']}** | **итого {section['title'].split('. ')[-1].lower()}** "
                   f"| | {prior_total} | **{fmt(sheet['totals'][section['total']])}** | |")
        out.append("")
    out.append(f"**1600 баланс (актив) = {fmt(sheet['totals']['1600'])} ₽**, "
               f"**1700 баланс (пассив) = {fmt(sheet['totals']['1700'])} ₽**."
               + (f" Годом ранее: 1600 = {fmt(prior['totals']['1600'])}, "
                  f"1700 = {fmt(prior['totals']['1700'])}."
                  if prior.get("available") else ""))
    if prior.get("reason"):
        out += ["", f"> {prior['reason']}"]

    empty = [line for line in sheet["lines"] if not line["accounts"] and line["why_empty"]]
    if empty:
        out += ["", "### Строки, которые нечем заполнить", ""]
        for line in empty:
            out.append(f"- **{line['code']} {line['title']}** — {line['why_empty']}")

    out += ["", "## Отчёт о финансовых результатах", "",
            f"| строка | наименование | {prior_head}, ₽ | за {report['year']}, ₽ | тыс. ₽ |",
            "|---|---|---:|---:|---:|"]
    for line in profit["lines"]:
        amount = -abs(line["amount"]) if line.get("negative") and line["amount"] else line["amount"]
        mark = " (не считается)" if line.get("not_calculated") else ""
        prior_cell = fmt(line["prior"]) if line.get("prior") is not None else "—"
        out.append(f"| {line['code']} | {line['title']}{mark} | {prior_cell} | {fmt(amount)} | "
                   f"{fmt(thousand(amount))} |")
    empty_pnl = [line for line in profit["lines"] if line.get("not_calculated")]
    if empty_pnl:
        out += ["", "### Строки ОФР, которые не заполняются", ""]
        for line in empty_pnl:
            out.append(f"- **{line['code']} {line['title']}** — {line['why_empty']}")

    tax = report.get("tax") or {}
    if tax:
        out += ["", "## Налог на прибыль", "",
                f"- проведено в журнале (Дт 99 Кт 68.04): **{rub(tax.get('posted'))}**",
                f"- насчитал налоговый модуль за {report['year']}: {rub(tax.get('computed'))} "
                f"(база {rub(tax.get('base'))}, ставки "
                f"{(tax.get('rates') or {}).get('federal')}% + "
                f"{(tax.get('rates') or {}).get('regional')}%)",
                f"- сумма помесячных расчётов: {rub(tax.get('months_sum'))}"
                + (f" — {tax['why_diff']}" if tax.get("why_diff") else
                   " — совпадает с годом"),
                f"- расхождение «проведено против посчитано»: "
                f"**{rub(tax.get('difference'))}**"
                + (" — это разрыв, а не ноль" if abs(float(tax.get("difference") or 0)) > 0.01
                   else " (сходится)")]

    out += ["", "## Проверки", "", "| код | что проверено | результат |", "|---|---|---|"]
    for item in report["checks"]:
        out.append(f"| {item['code']} | {item['what']} | "
                   f"{'ок' if item['ok'] else '✗ ' + item['why']} |")
    out += ["", f"Проверок {len(report['checks'])}: нарушений {len(report['problems'])}.", ""]
    if report.get("gaps"):
        out += ["## Разрывы, названные числом", ""]
        for gap in report["gaps"]:
            out.append(f"- **{gap['what']}: {rub(gap['amount'])}** — {gap['why']}")
    if report.get("readiness"):
        out += ["", "## Чего не хватает, чтобы считались ОДК и ОДДС", ""]
        for item in report["readiness"]:
            out.append(f"- **{item['short']} (не строится)** — {item['why']}")
            for miss in item.get("missing") or []:
                out.append(f"  - нет данных: {miss}")
            out.append(f"  - нужно: {item['need']}")
    if report.get("not_implemented"):
        out += ["", "## Не сделано (и почему)", ""]
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
    prior = sheet.get("prior") or {}
    if prior.get("available"):
        growth = float(sheet["totals"]["1600"]) - float(prior["totals"]["1600"])
        lines.append(f"  сравнительно: на 31.12.{prior['year']} актив "
                     f"{rub(prior['totals']['1600'])}, изменение {rub(growth)}")
        net_prior = (profit.get("prior_values") or {}).get("2400")
        if net_prior is not None:
            lines.append(f"  чистая прибыль: {rub(net_prior)} за {prior['year']} → "
                         f"{rub(values.get('2400'))} за {report['year']}")
    else:
        lines.append(f"  прошлый год: графа не заполняется — {prior.get('reason', '')}")
    tax = report.get("tax") or {}
    if tax:
        tail = (f"разрыв {rub(tax['difference'])} — проведено {rub(tax['posted'])}, "
                f"посчитано {rub(tax['computed'])}"
                if abs(float(tax.get("difference") or 0)) > 0.01 else
                f"проведён и совпадает с расчётом ({rub(tax['posted'])})")
        lines.append(f"  налог на прибыль: {tail}")
    for gap in report.get("gaps") or []:
        lines.append(f"  разрыв: {gap['what']} — {rub(gap['amount'])}; {gap['why']}")
    for item in report.get("readiness") or []:
        lines.append(f"  {item['short']}: не строится — {'; '.join(item['missing']) or item['why']}")
    for item in report["problems"]:
        lines.append(f"  ✗ [{item['code']}] {item['what']}: {item['why']}")
    if report.get("not_implemented"):
        lines.append(f"  не сделано: {len(report['not_implemented'])} пунктов "
                     f"(отчёт об изменениях капитала, ОДДС, пояснения, ПБУ 18/02, сдача в ФНС)")
    return "\n".join(lines)
