# -*- coding: utf-8 -*-
"""Зарплата: табель → начисление → НДФЛ → взносы → проводки → ведомость.

Правила, на которых держится модуль:

1. **НДФЛ считается нарастающим итогом.** Прогрессия 13/15/18/20/22 % устроена так, что
   налог за месяц = налог с начала года − уже удержанный. Считать «13 % от месяца» и потом
   «15 % от месяца» — значит копить ошибку в рублях на каждом переходе через порог.
2. **Вычеты живут до 450 000 ₽ дохода с начала года.** Месяц, в котором порог пройден,
   вычета уже не даёт.
3. **Взносы считаются по человеку, а не по фонду**: 30 % до предельной базы 2 979 000 ₽,
   15,1 % сверх. Травматизм — со всей суммы, отдельной платёжкой.
4. **Оклад делится на норму дней месяца.** Норма берётся из производственного календаря,
   а не «примерно 22»: табель без нормы — не табель.
5. **Чего нет — того не считаем.** Больничные, отпускные, премии требуют данных, которых
   в контуре нет: они попадают в «что запросить» с причиной, а не в ноль.
"""
from __future__ import annotations

import calendar as _calendar
import json
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

SCHEMA = "pto-agent/payroll@1"
RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "payroll_rules.json"
EPS = Decimal("0.01")

ACCRUAL = "начислено"
PAID = "выплачено"


# ─────────────────────────── числа и деньги ───────────────────────────
def dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except Exception:  # noqa: BLE001 — «не число» это тоже ответ
        return None


def kopeck(value) -> Decimal:
    number = dec(value)
    return (number or Decimal("0")).quantize(EPS, rounding=ROUND_HALF_UP)


def num(value) -> float:
    return float(kopeck(value))


def rub(value) -> str:
    number = kopeck(value)
    text = f"{number:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} ₽"


# ─────────────────────────── справочники ───────────────────────────
def load_rules(path: str | Path | None = None) -> dict:
    payload = json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))
    if payload.get("schema") != "pto-agent/payroll-rules@1":
        raise ValueError(f"правила зарплаты: схема {payload.get('schema')!r} не та")
    return payload


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def new_state(*, object_name: str = "", organization: str = "") -> dict:
    return {"schema": SCHEMA, "object": object_name, "organization": organization,
            "periods": [], "payments": [], "rejections": [], "note":
            "Начисления по месяцам. Хранится расчёт, а не только сумма: видно, из чего она."}


def load_state(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return new_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} не {SCHEMA!r}")
    return payload


def save_state(state: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def period_bounds(period: str) -> tuple[str, str]:
    year, month = int(period[:4]), int(period[5:7])
    last = _calendar.monthrange(year, month)[1]
    return f"{period}-01", f"{period}-{last:02d}"


def work_calendar(period: str, rules: dict) -> dict:
    """Норма дней и часов месяца: пн–пт минус федеральные праздники из правил."""
    year, month = int(period[:4]), int(period[5:7])
    holidays = set((rules.get("calendar") or {}).get("holidays") or [])
    days = [f"{period}-{day:02d}" for day in range(1, _calendar.monthrange(year, month)[1] + 1)]
    workdays = [day for day in days if _date.fromisoformat(day).weekday() < 5
                and day not in holidays]
    return {"days": len(workdays), "hours": len(workdays) * 8,
            "holidays": sorted(day for day in holidays if day.startswith(period)),
            "source": "производственный календарь: пн–пт минус праздники из payroll_rules.json"}


def month_of(index_date: str) -> str:
    return str(index_date or "")[:7]


# ─────────────────────────── НДФЛ ───────────────────────────
def ndfl_scale(rules: dict, base: Decimal) -> dict:
    """НДФЛ по прогрессии: каждая ступень — только к сумме сверх её порога.

    Ошибка, которую здесь легко сделать: умножить весь доход на ставку ступени, которую
    он перешагнул. Тогда на 2,6 млн ₽ выходит 15 % со всех 2,6 млн вместо 13 % с первых
    2,4 млн — разница 48 000 ₽ на одном человеке.
    """
    brackets = (rules.get("ndfl") or {}).get("resident_brackets") or []
    remaining = max(kopeck(base), Decimal("0"))
    lower = Decimal("0")
    total = Decimal("0")
    steps = []
    for bracket in brackets:
        if remaining <= 0:
            break
        up_to = bracket.get("up_to")
        ceiling = Decimal(str(up_to)) if up_to is not None else lower + remaining
        piece = min(remaining, ceiling - lower)
        if piece <= 0:
            break
        rate = Decimal(str(bracket.get("rate") or 0))
        tax = piece * rate / Decimal("100")
        steps.append({"from": num(lower), "to": num(lower + piece), "rate": float(rate),
                      "base": num(piece), "tax": num(tax)})
        total += tax
        remaining -= piece
        lower = ceiling
    return {"tax": kopeck(total), "steps": steps}


def deductions_of(rules: dict, employee: dict, income_ytd_before: Decimal) -> dict:
    """Вычеты за месяц: дети по очерёдности, ребёнок-инвалид, двойной — единственному."""
    settings = rules.get("deductions") or {}
    limit = Decimal(str(settings.get("income_limit") or 450000))
    if income_ytd_before >= limit:
        return {"amount": Decimal("0"), "children": 0, "why":
                f"доход с начала года {rub(income_ytd_before)} ≥ {rub(limit)} — вычет не применяется"}
    children = int(employee.get("children") or 0)
    disabled = int(employee.get("disabled_children") or 0)
    table = settings.get("children") or {}
    amount = Decimal("0")
    for index in range(1, children + 1):
        key = "1" if index == 1 else ("2" if index == 2 else "3+")
        amount += Decimal(str(table.get(key) or 0))
    amount += Decimal(str(settings.get("disabled_child") or 0)) * disabled
    if employee.get("sole_parent"):
        amount *= 2
    return {"amount": kopeck(amount), "children": children, "disabled": disabled,
            "sole_parent": bool(employee.get("sole_parent")), "why": ""}


def ndfl_for_month(rules: dict, *, employee: dict, income_month: Decimal,
                   income_ytd_before: Decimal, withheld_ytd_before: Decimal,
                   deductions_ytd_before: Decimal) -> dict:
    """Налог за месяц = налог с начала года − удержанный ранее. Это и есть нарастающий итог."""
    rules_ndfl = rules.get("ndfl") or {}
    if not employee.get("resident", True):
        base = income_ytd_before + income_month
        tax_ytd = kopeck(base * Decimal(str(rules_ndfl.get("non_resident_rate") or 30))
                         / Decimal("100"))
        return {"tax": kopeck(tax_ytd - withheld_ytd_before), "tax_ytd": tax_ytd,
                "base_ytd": num(base), "deduction": 0.0, "resident": False,
                "steps": [], "why": "нерезидент: плоская ставка 30 % без вычетов"}
    deduction = deductions_of(rules, employee, income_ytd_before)
    deductions_ytd = deductions_ytd_before + deduction["amount"]
    base_ytd = income_ytd_before + income_month - deductions_ytd
    base_ytd = max(base_ytd, Decimal("0"))
    scale = ndfl_scale(rules, base_ytd)
    tax_month = kopeck(scale["tax"] - withheld_ytd_before)
    return {"tax": tax_month, "tax_ytd": scale["tax"], "base_ytd": num(base_ytd),
            "deduction": num(deduction["amount"]), "deductions_ytd": num(deductions_ytd),
            "deduction_why": deduction["why"], "resident": True, "steps": scale["steps"]}


# ─────────────────────────── взносы ───────────────────────────
def contributions_for_month(rules: dict, *, income_month: Decimal,
                            base_ytd_before: Decimal) -> dict:
    settings = rules.get("contributions") or {}
    limit = Decimal(str(settings.get("base_limit") or 2979000))
    rate = Decimal(str(settings.get("rate") or 30))
    over_rate = Decimal(str(settings.get("over_rate") or 15.1))
    injury_rate = Decimal(str((settings.get("injury") or {}).get("rate") or 0))

    def _total(base_ytd: Decimal) -> Decimal:
        within = min(base_ytd, limit)
        above = max(base_ytd - limit, Decimal("0"))
        return within * rate / Decimal("100") + above * over_rate / Decimal("100")

    total_before = _total(base_ytd_before)
    total_after = _total(base_ytd_before + income_month)
    injury = kopeck(income_month * injury_rate / Decimal("100"))
    return {"total": kopeck(total_after - total_before),
            "total_ytd": kopeck(total_after),
            "injury": injury,
            "rate": float(rate), "over_rate": float(over_rate), "injury_rate": float(injury_rate),
            "base_limit": num(limit),
            "limit_reached": bool(base_ytd_before + income_month > limit),
            "note": ("предельная база пройдена: часть месяца облагается по 15,1 %"
                     if base_ytd_before + income_month > limit else "")}


def prior_totals(state: dict, *, period: str) -> dict[str, dict]:
    """Что уже начислено и удержано по каждому человеку до этого месяца — в этом году."""
    year = period[:4]
    totals: dict[str, dict] = {}
    for row in state.get("periods") or []:
        if not str(row.get("period") or "").startswith(year):
            continue
        if str(row.get("period") or "") >= period:
            continue
        for employee in row.get("rows") or []:
            slot = totals.setdefault(employee["tab"], {
                "income": Decimal("0"), "withheld": Decimal("0"),
                "deductions": Decimal("0"), "contrib_base": Decimal("0")})
            slot["income"] += money_dec(employee.get("accrued"))
            slot["withheld"] += money_dec(employee.get("ndfl"))
            slot["deductions"] += money_dec(employee.get("deduction"))
            slot["contrib_base"] += money_dec(employee.get("contrib_base"))
    return totals


def money_dec(value) -> Decimal:
    return dec(value) or Decimal("0")


# ─────────────────────────── табель ───────────────────────────
def check_timesheet(timesheet: dict, staff: dict, rules: dict) -> tuple[dict, list[dict], list[dict]]:
    """Проверить табель: норма месяца, дни не больше нормы, каждый сотрудник с табелем."""
    errors: list[dict] = []
    gaps: list[dict] = []
    period = str(timesheet.get("period") or "")
    if len(period) != 7:
        errors.append({"code": "BAD_PERIOD", "what": period, "why": "период задаётся как ГГГГ-ММ"})
        return {}, errors, gaps
    norm_claim = timesheet.get("norm") or {}
    norm = work_calendar(period, rules)
    if not norm_claim.get("days"):
        errors.append({"code": "NO_NORM", "what": period,
                       "why": "в табеле нет нормы дней месяца: «отработано 20 дней» без нормы — "
                              "не табель, а число"})
    elif int(norm_claim.get("days")) != norm["days"]:
        errors.append({"code": "NORM_MISMATCH", "what": period,
                       "why": f"в табеле норма {norm_claim.get('days')} дн., по календарю "
                              f"{norm['days']} дн. — расхождение меняет оклад пропорционально"})
    employees = {str(row["tab"]): row for row in staff.get("employees") or []}
    seen: set[str] = set()
    for row in timesheet.get("rows") or []:
        tab = str(row.get("tab") or "")
        if tab not in employees:
            errors.append({"code": "NO_EMPLOYEE", "what": tab,
                           "why": "табельный номер не найден в карточках сотрудников"})
            continue
        if tab in seen:
            errors.append({"code": "DUPLICATE_ROW", "what": tab, "why": "сотрудник в табеле дважды"})
            continue
        seen.add(tab)
        worked = dec(row.get("worked_days"))
        if worked is None:
            errors.append({"code": "BAD_DAYS", "what": tab,
                           "why": f"отработано {row.get('worked_days')!r} — не число"})
            continue
        if worked < 0:
            errors.append({"code": "NEGATIVE_DAYS", "what": tab, "why": "отрицательные дни"})
        if worked > norm["days"]:
            errors.append({"code": "OVER_NORM", "what": tab,
                           "why": f"отработано {worked} дн. при норме {norm['days']}: переработка "
                                  f"не оплачивается автоматически — нужен приказ и отдельный расчёт"})
    for tab, employee in employees.items():
        if tab not in seen and employee.get("status") == "работает":
            gaps.append({"what": f"табель по {employee.get('name')}",
                         "why": "сотрудник работает, а строки в табеле нет",
                         "ask": f"отметить отработанные дни или причину отсутствия ({tab})"})
    return norm, errors, gaps


# ─────────────────────────── начисление ───────────────────────────
def accrue(state: dict, timesheet: dict, staff: dict, rules: dict,
           *, period: str | None = None) -> dict:
    """Начислить зарплату за месяц: оклад по дням → НДФЛ → взносы. Возвращает строки."""
    period = period or str(timesheet.get("period") or "")
    result = {"period": period, "rows": [], "totals": {}, "errors": [], "gaps": [],
              "warnings": [], "not_included": []}
    norm, errors, gaps = check_timesheet(timesheet, staff, rules)
    result["errors"], result["gaps"] = errors, gaps
    result["norm"] = {"days": norm.get("days"), "hours": norm.get("hours"),
                      "claim": (timesheet.get("norm") or {}).get("days"),
                      "source": norm.get("source")}
    if errors:
        return result
    if any(str(row.get("period") or "") == period for row in state.get("periods") or []):
        result["warnings"].append(f"{period} уже начислен — повтор не создаёт начислений")
        existing = next(row for row in state["periods"] if row.get("period") == period)
        result["rows"] = existing.get("rows") or []
        result["totals"] = existing.get("totals") or {}
        result["duplicate"] = True
        return result

    employees = {str(row["tab"]): row for row in staff.get("employees") or []}
    before = prior_totals(state, period=period)
    rows: list[dict] = []
    for row in timesheet.get("rows") or []:
        employee = employees[str(row["tab"])]
        if employee.get("status") != "работает":
            result["not_included"].append({
                "what": employee.get("name"), "why": f"статус «{employee.get('status')}»: "
                "начисление не делаем, нужен приказ"})
            continue
        worked = dec(row.get("worked_days")) or Decimal("0")
        salary = money_dec(employee.get("salary"))
        coefficient = money_dec(employee.get("coefficient") or 1)
        base_pay = (salary / Decimal(norm["days"]) * worked).quantize(EPS, rounding=ROUND_HALF_UP)
        pay = kopeck(base_pay * coefficient)
        slot = before.get(str(row["tab"]), {})
        income_before = money_dec(slot.get("income"))
        withheld_before = money_dec(slot.get("withheld"))
        deductions_before = money_dec(slot.get("deductions"))
        contrib_before = money_dec(slot.get("contrib_base"))
        ndfl = ndfl_for_month(rules, employee=employee, income_month=pay,
                              income_ytd_before=income_before,
                              withheld_ytd_before=withheld_before,
                              deductions_ytd_before=deductions_before)
        contrib = contributions_for_month(rules, income_month=pay,
                                          base_ytd_before=contrib_before)
        net = kopeck(pay - ndfl["tax"])
        rows.append({
            "tab": str(employee["tab"]), "name": employee.get("name"),
            "position": employee.get("position"), "object": employee.get("object"),
            "salary": num(salary), "coefficient": num(coefficient),
            "worked_days": num(worked), "norm_days": norm["days"],
            "accrued": num(pay),
            "deduction": ndfl["deduction"], "deduction_why": ndfl.get("deduction_why") or "",
            "ndfl_base": ndfl["base_ytd"], "ndfl": num(ndfl["tax"]),
            "ndfl_ytd": num(ndfl["tax_ytd"]), "ndfl_steps": ndfl["steps"],
            "ndfl_note": ndfl.get("why") or "",
            "contrib_base": num(pay), "contrib": num(contrib["total"]),
            "contrib_ytd": num(contrib["total_ytd"]),
            "injury": num(contrib["injury"]), "contrib_note": contrib["note"],
            "net": num(net), "children": employee.get("children") or 0,
            "income_ytd": num(income_before + pay),
        })

    totals = {
        "accrued": num(sum((money_dec(row["accrued"]) for row in rows), Decimal("0"))),
        "deductions": num(sum((money_dec(row["deduction"]) for row in rows), Decimal("0"))),
        "ndfl": num(sum((money_dec(row["ndfl"]) for row in rows), Decimal("0"))),
        "contrib": num(sum((money_dec(row["contrib"]) for row in rows), Decimal("0"))),
        "injury": num(sum((money_dec(row["injury"]) for row in rows), Decimal("0"))),
        "net": num(sum((money_dec(row["net"]) for row in rows), Decimal("0"))),
        "employees": len(rows),
    }
    totals["cost"] = num(money_dec(totals["accrued"]) + money_dec(totals["contrib"])
                         + money_dec(totals["injury"]))
    result["rows"] = rows
    result["totals"] = totals
    result["not_included"].extend([
        {"what": "больничные и отпускные", "why": "нужна база за два предшествующих года; "
         "в контуре её нет — сумма не считается, а не «примерно»"},
        {"what": "премии и надбавки", "why": "нет приказов: считаем оклад и отработанные дни"},
    ])
    result["mode"] = "расчёт"
    return result


def remember(state: dict, report: dict, *, rules: dict) -> dict:
    """Запомнить расчёт месяца: без этого не посчитать НДФЛ и взносы нарастающим итогом."""
    if report.get("duplicate") or not report.get("rows"):
        return state
    state.setdefault("periods", []).append({
        "period": report["period"], "norm": report.get("norm"), "rows": report["rows"],
        "totals": report["totals"],
        "rates": {"ndfl": (rules.get("ndfl") or {}).get("resident_brackets"),
                  "contrib_rate": (rules.get("contributions") or {}).get("rate"),
                  "over_rate": (rules.get("contributions") or {}).get("over_rate"),
                  "base_limit": (rules.get("contributions") or {}).get("base_limit"),
                  "injury": (rules.get("contributions") or {}).get("injury")},
        "source": (rules.get("source") or {}).get("items"),
    })
    state["periods"].sort(key=lambda row: str(row.get("period")))
    return state


# ─────────────────────────── проводки ───────────────────────────
def post_ledger(state: dict, accrued: dict, *, chart: dict | None = None,
                mode: str | None = None, date: str | None = None) -> dict:
    """Провести начисление в журнал: 20.01 ← 70, 69.01, 69.11 и 70 → 68.01.

    На дебете 20.01 стоит аналитика «статья затрат» («зарплата» / «взносы»): без неё
    зарплата попала бы в БДР как неразнесённая сумма, а не как статья затрат.
    """
    from . import ledger as accounting

    result = {"posted": 0, "duplicates": 0, "rejected": 0, "results": [],
              "errors": [], "gaps": [], "warnings": []}
    chart = chart or accounting.load_chart()
    period = str(accrued.get("period") or "")
    if len(period) != 7:
        result["errors"].append({"code": "BAD_PERIOD", "what": period,
                                 "why": "проводки требуют месяц ГГГГ-ММ"})
        return result
    rows = accrued.get("rows") or []
    if not rows:
        result["errors"].append({"code": "NOTHING", "what": period,
                                 "why": "начислений нет — проводить нечего"})
        return result

    line_date = date or f"{period}-{_calendar.monthrange(int(period[:4]), int(period[5:7]))[1]:02d}"

    def _post(event: str, number: str, lines: list[dict], memo: str) -> dict:
        outcome = accounting.post_entry(
            state, event=event, date=line_date,
            doc={"number": number, "kind": "расчётная ведомость", "date": line_date},
            lines=lines, chart=chart,
            mode=mode or accounting.MODE_POST, memo=memo,
            key=f"{event}|{period}", rule="payroll")
        result["results"].append({"event": event, "number": number, **{
            key: outcome.get(key) for key in ("status", "errors", "gaps", "warnings")}})
        if outcome.get("status") == accounting.STATUS_OK:
            result["posted"] += 1
        elif outcome.get("status") == accounting.STATUS_DUPLICATE:
            result["duplicates"] += 1
        else:
            result["rejected"] += 1
            result["errors"].extend(outcome.get("errors") or [])
            result["gaps"].extend(outcome.get("gaps") or [])
        result["warnings"].extend(outcome.get("warnings") or [])
        return outcome

    by_object: dict[str, dict] = {}
    for row in rows:
        slot = by_object.setdefault(str(row.get("object") or ""),
                                    {"accrued": Decimal("0"), "contrib": Decimal("0"),
                                     "injury": Decimal("0"), "ndfl": Decimal("0")})
        slot["accrued"] += money_dec(row.get("accrued"))
        slot["contrib"] += money_dec(row.get("contrib"))
        slot["injury"] += money_dec(row.get("injury"))
        slot["ndfl"] += money_dec(row.get("ndfl"))

    for object_name, slot in by_object.items():
        if slot["accrued"] > 0:
            _post("payroll_accrual", f"ведомость Т-51 за {period} ({object_name or 'без объекта'})",
                  [{"account": "20.01", "side": accounting.DEBIT, "amount": num(slot["accrued"]),
                    "analytics": {"объект": object_name, "статья затрат": "зарплата"}},
                   {"account": "70", "side": accounting.CREDIT, "amount": num(slot["accrued"]),
                    "analytics": {"сотрудник": f"сводно по {object_name or 'объекту'}"}}],
                  memo=f"начислена зарплата за {period}: {len(rows)} человек")
        if slot["contrib"] > 0:
            _post("payroll_contributions", f"взносы (единый тариф) за {period}",
                  [{"account": "20.01", "side": accounting.DEBIT, "amount": num(slot["contrib"]),
                    "analytics": {"объект": object_name, "статья затрат": "взносы"}},
                   {"account": "69.01", "side": accounting.CREDIT, "amount": num(slot["contrib"])}],
                  memo=f"страховые взносы по единому тарифу за {period}")
        if slot["injury"] > 0:
            _post("payroll_injury", f"взносы на травматизм за {period}",
                  [{"account": "20.01", "side": accounting.DEBIT, "amount": num(slot["injury"]),
                    "analytics": {"объект": object_name, "статья затрат": "взносы"}},
                   {"account": "69.11", "side": accounting.CREDIT, "amount": num(slot["injury"])}],
                  memo=f"взносы на травматизм за {period}")
        if slot["ndfl"] > 0:
            _post("payroll_ndfl", f"НДФЛ за {period}",
                  [{"account": "70", "side": accounting.DEBIT, "amount": num(slot["ndfl"]),
                    "analytics": {"сотрудник": f"сводно по {object_name or 'объекту'}"}},
                   {"account": "68.01", "side": accounting.CREDIT, "amount": num(slot["ndfl"])}],
                  memo=f"удержан НДФЛ за {period}")
    return result


# ─────────────────────────── выплата ───────────────────────────
def payout_requests(accrued: dict, *, date: str, contract: str = "") -> list[dict]:
    """Заявки на выплату зарплаты для казначейства: по человеку, статья «зарплата».

    Выплата идёт тем же путём, что и любой платёж: заявка → маршрут → оплата →
    Дт 70 Кт 51. Поэтому зарплата не обходит контроль лимитов и подписей.
    """
    rows = []
    period = str(accrued.get("period") or "")
    for index, employee in enumerate(accrued.get("rows") or [], start=1):
        if money_dec(employee.get("net")) <= 0:
            continue
        rows.append({
            "number": f"ЗП-{period.replace('-', '')}-{index:02d}",
            "date": date, "counterparty": employee["name"], "article": "зарплата",
            "object": employee.get("object") or "",
            "amount": employee["net"],
            "purpose": f"зарплата за {period} (таб. {employee['tab']})",
            "basis": {"kind": "ведомость", "number": f"Т-51 за {period}", "date": date},
            "contract": contract,
        })
    return rows


# ─────────────────────────── отчёты ───────────────────────────
def fund_report(state: dict, *, rules: dict | None = None) -> dict:
    """Фонд оплаты труда по месяцам: начислено, НДФЛ, взносы, к выплате, стоимость."""
    rules = rules or load_rules()
    rows = []
    for period in state.get("periods") or []:
        totals = period.get("totals") or {}
        rows.append({"period": period.get("period"), **{key: totals.get(key) for key in
                     ("accrued", "ndfl", "contrib", "injury", "net", "cost", "employees")}})
    return {"rows": rows, "periods": len(rows),
            "note": "Стоимость для организации = начислено + взносы + травматизм. "
                    "НДФЛ в стоимость не входит: это деньги работника, а не расход."}


def sheet(state: dict, period: str) -> dict:
    """Расчётная ведомость Т-51 за месяц — по сохранённому расчёту, а не пересчётом."""
    row = next((item for item in state.get("periods") or [] if item.get("period") == period), None)
    if not row:
        return {"period": period, "rows": [], "totals": {}, "found": False}
    return {"period": period, "rows": row.get("rows") or [], "totals": row.get("totals") or {},
            "norm": row.get("norm"), "rates": row.get("rates"), "found": True}


def verify(state: dict) -> dict:
    """Внутренняя сходимость: сумма строк = итог, НДФЛ и взносы не отрицательные."""
    problems = []
    for period in state.get("periods") or []:
        totals = period.get("totals") or {}
        rows = period.get("rows") or []
        for field in ("accrued", "ndfl", "contrib", "injury", "net"):
            total = sum((money_dec(item.get(field)) for item in rows), Decimal("0"))
            if abs(total - money_dec(totals.get(field))) > EPS:
                problems.append({"code": "TOTAL_MISMATCH", "period": period.get("period"),
                                 "what": field,
                                 "why": f"итог {rub(totals.get(field))} ≠ сумма строк {rub(total)}"})
        for item in rows:
            if money_dec(item.get("net")) < 0:
                problems.append({"code": "NEGATIVE_NET", "period": period.get("period"),
                                 "what": item.get("name"),
                                 "why": "к выплате отрицательная сумма: НДФЛ больше начисления"})
            if money_dec(item.get("accrued")) - money_dec(item.get("ndfl")) - money_dec(item.get("net")) > EPS:
                problems.append({"code": "NET_MISMATCH", "period": period.get("period"),
                                 "what": item.get("name"),
                                 "why": "начислено − НДФЛ ≠ к выплате"})
    return {"ok": not problems, "problems": problems,
            "periods": len(state.get("periods") or []),
            "summary": "начисления сходятся со строками" if not problems
            else f"расхождений: {len(problems)}"}


def to_markdown(report: dict) -> str:
    lines = [f"# Расчётная ведомость за {report['period']}", ""]
    if report.get("norm"):
        norm = report["norm"]
        lines.append(f"Норма месяца: {norm.get('days')} дн. / {norm.get('hours')} ч "
                     f"({norm.get('source') or '—'})")
        lines.append("")
    lines.append("| таб. | сотрудник | объект | оклад | дней | начислено | вычет | НДФЛ | "
                 "взносы | травматизм | к выплате |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in report.get("rows") or []:
        lines.append(
            f"| {row['tab']} | {row['name']} | {row.get('object') or '—'} | "
            f"{rub(row['salary'])} | {row['worked_days']:g}/{row['norm_days']} | "
            f"{rub(row['accrued'])} | {rub(row['deduction'])} | {rub(row['ndfl'])} | "
            f"{rub(row['contrib'])} | {rub(row['injury'])} | {rub(row['net'])} |")
    totals = report.get("totals") or {}
    if totals:
        lines.append("")
        lines.append(f"Итого: начислено {rub(totals.get('accrued'))}, НДФЛ {rub(totals.get('ndfl'))}, "
                     f"взносы {rub(totals.get('contrib'))}, травматизм {rub(totals.get('injury'))}, "
                     f"к выплате {rub(totals.get('net'))}, стоимость для организации "
                     f"{rub(totals.get('cost'))}")
    return "\n".join(lines) + "\n"
