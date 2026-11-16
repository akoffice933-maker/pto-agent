"""Приказы по людям: больничные, отпускные, премии.

Зачем отдельный модуль: оклад считается по табелю, а всё, что приходит **приказом**,
живёт в другом документе и по другим правилам. Больничный делят работодатель и СФР,
отпускные считаются по среднему заработку за 12 месяцев, премия — это сумма из приказа,
а не доля оклада.

Что здесь принципиально:

* **Сумма без источника не считается.** Больничный требует заработка за два года
  (`earnings_2y`), отпускные — заработка за 12 месяцев (`base_12m`) или прямо СДЗ.
  Нет базы — приказ не рассчитывается, в отчёт идёт код `SICK_NO_BASE` / `VACATION_NO_BASE`
  и вопрос, что принести. «Примерно по окладу» здесь запрещено.
* **Больничный делится по закону, а не по желанию.** За первые три дня своей болезни
  платит работодатель, с четвёртого — СФР напрямую; при уходе за членом семьи, карантине
  и протезировании работодатель не платит ничего. В контуре остаётся только часть
  работодателя, часть СФР — справочно: она не наш расход и не наша база по взносам.
* **Взносы с пособия не начисляются** (п. 1 ст. 422 НК РФ), НДФЛ удерживается.
  Поэтому доход месяца для НДФЛ и база для взносов — разные величины, и это видно в строках.

Ставки и ограничения — в `data/reference/payroll_rules.json`, раздел `benefits`.
"""
from __future__ import annotations

import calendar
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

KINDS = {"sick": "Больничный", "vacation": "Отпуск", "bonus": "Премия",
         "dismissal": "Увольнение (компенсация за неиспользованный отпуск)"}

EPS = Decimal("0.01")

NOT_INCLUDED = [
    {"what": "пособия, которые платит СФР напрямую",
     "why": "с 4-го дня болезни, весь уход за членом семьи, карантин и протезирование "
            "фонд перечисляет работнику сам: это не расход организации и не база по взносам, "
            "поэтому в журнал и в РСВ такие суммы не попадают — они только справочно"},
    {"what": "расчёт больничного из МРОТ при неполной ставке",
     "why": "минимум зависит от доли ставки и районного коэффициента; в карточке сотрудника "
            "этих полей нет, поэтому минимум берётся общий и помечается предупреждением"},
    {"what": "выходное пособие и выплаты при увольнении, кроме компенсации за отпуск",
     "why": "средний месячный заработок, двухнедельный и трёхкратный средний — отдельный "
            "расчёт по п. 3–6 Положения № 540; в контуре только компенсация за "
            "неиспользованный отпуск"},
    {"what": "декретные (пособие по беременности и родам)",
     "why": "нужны замена лет расчётного периода, районный коэффициент и доля ставки: "
            "механизм тот же, что у больничного, но цифры без этих полей будут неверными"},
]


def kopeck(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(EPS, rounding=ROUND_HALF_UP)


def num(value) -> float:
    return float(kopeck(value))


def rub(value) -> str:
    return f"{kopeck(value):,.2f} ₽".replace(",", " ")


def load_orders(path: str | Path) -> list[dict]:
    """Прочитать файл приказов: {"orders": [...]} или просто список."""
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return list(data.get("orders") or [])
    return list(data)


def _days_in_month(period: str) -> int:
    try:
        year, month = int(period[:4]), int(period[5:7])
    except (TypeError, ValueError):
        return 30
    return calendar.monthrange(year, month)[1]


def _seniority_percent(rules: dict, years) -> tuple[Decimal, str]:
    """Процент пособия по страховому стажу: 8+ лет — 100 %, 5–8 — 80 %, меньше 5 — 60 %."""
    tiers = ((rules.get("benefits") or {}).get("sick") or {}).get("seniority") or []
    years_value = Decimal(str(years if years is not None else -1))
    for tier in sorted(tiers, key=lambda item: -float(item.get("from_years") or 0)):
        if years_value >= Decimal(str(tier.get("from_years") or 0)):
            return Decimal(str(tier.get("percent"))), str(tier.get("basis") or "")
    return Decimal("60"), "стаж не указан — взят минимальный процент 60 %"


def sick(order: dict, employee: dict, rules: dict, *, period: str) -> dict:
    """Больничный: пособие за день, часть работодателя и часть СФР.

    Дневное пособие = заработок за два года / 730 × процент по стажу, но не выше
    максимума и не ниже минимума из МРОТ за месяц болезни.
    """
    rules_sick = (rules.get("benefits") or {}).get("sick") or {}
    days = int(order.get("days") or 0)
    cause = str(order.get("cause") or "").strip()
    employer_causes = [str(item) for item in rules_sick.get("employer_pays") or []]
    fund_causes = [str(item) for item in rules_sick.get("fund_pays_all") or []]

    errors: list[dict] = []
    warnings: list[str] = []

    if days <= 0:
        errors.append({"code": "ORDER_NO_DAYS", "what": order.get("number"),
                       "why": "в приказе нет числа календарных дней болезни"})
    start, end = order.get("start"), order.get("end")
    if start and end and days:
        try:
            from datetime import date
            span = (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days + 1
            if span != days:
                errors.append({"code": "ORDER_DAYS_MISMATCH", "what": order.get("number"),
                               "why": f"с {start} по {end} — это {span} дн., а в приказе {days}: "
                                      f"одна из цифр неверна"})
        except ValueError:
            errors.append({"code": "ORDER_BAD_DATE", "what": order.get("number"),
                           "why": f"даты {start!r}..{end!r} не читаются как ГГГГ-ММ-ДД"})
    if not cause:
        errors.append({"code": "ORDER_NO_CAUSE", "what": order.get("number"),
                       "why": "не указана причина нетрудоспособности: от неё зависит, платит "
                              "ли работодатель первые три дня"})
    elif cause not in employer_causes and cause not in fund_causes:
        errors.append({"code": "ORDER_UNKNOWN_CAUSE", "what": cause,
                       "why": f"причина не из справочника; известны: {', '.join(employer_causes + fund_causes)}"})

    earnings = order.get("earnings_2y")
    years = order.get("service_years")
    base_missing = earnings in (None, "") or kopeck(earnings) <= 0
    if earnings in (None, ""):
        errors.append({"code": "SICK_NO_BASE", "what": order.get("number"),
                       "why": "нет заработка за два предшествующих года: из оклада пособие не "
                              "считается — нужна справка о заработке (в т.ч. с прошлых мест)"})
    elif kopeck(earnings) <= 0:
        errors.append({"code": "SICK_NO_BASE", "what": order.get("number"),
                       "why": f"заработок за два года {rub(earnings)} — пособие из нуля"})
    if years is None:
        warnings.append("стаж не указан — взят минимальный процент 60 %: цифра занижена, "
                        "пока стаж не подтверждён")

    percent, percent_basis = _seniority_percent(rules, years)
    max_daily = Decimal(str(rules_sick.get("max_daily") or 0))
    min_sdz = Decimal(str(rules_sick.get("min_sdz") or 0))
    mrot = Decimal(str(rules.get("mrot") or 0))
    month_floor = mrot / Decimal(_days_in_month(period)) if mrot else Decimal("0")
    floor_daily = max(min_sdz, month_floor)

    if base_missing:
        # базы нет — сумма не считается: ноль честнее, чем «минимальное пособие из воздуха»
        return {
            "kind": "sick", "number": order.get("number"), "date": order.get("date"),
            "cause": cause, "days": days, "start": start, "end": end,
            "daily": 0.0, "percent": float(percent), "percent_basis": percent_basis,
            "sdz": 0.0, "sdz_raw": 0.0, "capped": False, "raised_to_min": False,
            "employer_days": 0, "fund_days": days, "amount": 0.0, "fund_amount": 0.0,
            "taxable": 0.0, "contrib_base": 0.0, "not_calculated": True,
            "errors": errors, "warnings": warnings,
            "memo": (f"больничный {order.get('number')}: не рассчитано — нет заработка за два "
                     f"предшествующих года"),
        }

    sdz_raw = kopeck(Decimal(str(earnings)) / Decimal("730"))
    sdz = sdz_raw
    capped = False
    if max_daily and sdz > max_daily:
        sdz, capped = max_daily, True
        warnings.append(f"средний дневной заработок {rub(sdz_raw)} выше предела "
                        f"{rub(max_daily)}: взят предел ({rules_sick.get('max_daily_basis')})")
    daily = kopeck(sdz * percent / Decimal("100"))
    raised = False
    if floor_daily and daily < floor_daily:
        daily, raised = floor_daily, True
        warnings.append(f"пособие за день {rub(daily)} ниже минимума из МРОТ "
                        f"{rub(floor_daily)}: взят минимум (МРОТ {rub(mrot)} / "
                        f"{_days_in_month(period)} дн.; минимум по ставке и районному "
                        f"коэффициенту не проверяется — их нет в карточке)")

    employer_days = min(int(rules_sick.get("employer_days") or 3), days) \
        if cause in employer_causes else 0
    employer_pay = kopeck(daily * employer_days)
    fund_pay = kopeck(daily * (days - employer_days))
    return {
        "kind": "sick", "number": order.get("number"), "date": order.get("date"),
        "cause": cause, "days": days, "start": start, "end": end,
        "daily": num(daily), "percent": float(percent), "percent_basis": percent_basis,
        "sdz": num(sdz), "sdz_raw": num(sdz_raw), "capped": capped, "raised_to_min": raised,
        "employer_days": employer_days, "fund_days": days - employer_days,
        "amount": num(employer_pay), "fund_amount": num(fund_pay),
        "taxable": num(employer_pay), "contrib_base": 0.0,
        "errors": errors, "warnings": warnings,
        "memo": (f"больничный {order.get('number')}: {days} к.д. по {rub(daily)}/день "
                 f"({float(percent):g} % стажа); работодатель — {employer_days} дн. = "
                 f"{rub(employer_pay)}, СФР — {days - employer_days} дн. = {rub(fund_pay)}"),
    }


def vacation(order: dict, employee: dict, rules: dict, *, period: str) -> dict:
    """Отпускные: СДЗ = база за 12 месяцев / 12 / 29,3, но не ниже минимума из МРОТ."""
    rules_vac = (rules.get("benefits") or {}).get("vacation") or {}
    days = int(order.get("days") or 0)
    errors: list[dict] = []
    warnings: list[str] = []
    if days <= 0:
        errors.append({"code": "ORDER_NO_DAYS", "what": order.get("number"),
                       "why": "в приказе нет числа дней отпуска"})
    avg_days = Decimal(str(rules_vac.get("avg_month_days") or "29.3"))
    months = Decimal(str(rules_vac.get("months") or 12))
    base = order.get("base_12m")
    direct_sdz = order.get("sdz")
    sdz = Decimal("0")
    source = ""
    if direct_sdz not in (None, ""):
        sdz = kopeck(direct_sdz)
        source = "средний дневной заработок указан в приказе"
    elif base not in (None, "") and kopeck(base) > 0:
        sdz = kopeck(Decimal(str(base)) / months / avg_days)
        source = f"база за 12 месяцев {rub(base)} / {int(months)} / {avg_days}"
    else:
        errors.append({"code": "VACATION_NO_BASE", "what": order.get("number"),
                       "why": "нет ни заработка за 12 месяцев, ни среднего дневного заработка: "
                              "отпускные из оклада не считаются — нужна ведомость за расчётный "
                              "период (Положение № 540)"})
    mrot = Decimal(str(rules.get("mrot") or 0))
    min_sdz = kopeck(mrot / avg_days) if mrot else Decimal(str(rules_vac.get("min_sdz") or 0))
    raised = False
    if sdz and min_sdz and sdz < min_sdz:
        sdz, raised = min_sdz, True
        warnings.append(f"средний дневной заработок ниже минимума {rub(min_sdz)} "
                        f"(МРОТ {rub(mrot)} / {avg_days}): взят минимум")
    amount = kopeck(sdz * days)
    return {
        "kind": "vacation", "number": order.get("number"), "date": order.get("date"),
        "days": days, "start": order.get("start"), "end": order.get("end"),
        "sdz": num(sdz), "sdz_source": source, "raised_to_min": raised,
        "amount": num(amount), "fund_amount": 0.0,
        "taxable": num(amount), "contrib_base": num(amount),
        "errors": errors, "warnings": warnings,
        "memo": (f"отпускные {order.get('number')}: {days} к.д. × {rub(sdz)} = {rub(amount)} "
                 f"({source}); облагаются НДФЛ и взносами"),
    }


def bonus(order: dict, employee: dict, rules: dict, *, period: str) -> dict:
    """Премия по приказу: сумма берётся из приказа, а не считается процентом оклада."""
    errors: list[dict] = []
    warnings: list[str] = []
    amount = kopeck(order.get("amount"))
    if not order.get("number"):
        errors.append({"code": "BONUS_NO_ORDER", "what": employee.get("name"),
                       "why": "нет номера приказа о премировании: сумма без документа — "
                              "не начисление, а договорённость"})
    if amount <= 0:
        errors.append({"code": "ORDER_AMOUNT_ZERO", "what": order.get("number"),
                       "why": "сумма премии не положительна"})
    if not order.get("basis"):
        warnings.append("в приказе не указано основание премии: для расхода нужна формулировка "
                        "(за что именно), иначе налоговая снимет выплату")
    return {
        "kind": "bonus", "number": order.get("number"), "date": order.get("date"),
        "amount": num(amount), "fund_amount": 0.0, "basis": order.get("basis") or "",
        "taxable": num(amount), "contrib_base": num(amount),
        "errors": errors, "warnings": warnings,
        "memo": (f"премия {order.get('number')}: {rub(amount)} — "
                 f"{order.get('basis') or 'основание не указано'}"),
    }


def seniority_months(hired: str, dismissal: str, *, excluded_days: int = 0) -> dict:
    """Отпускной стаж в месяцах по п. 35 Правил НКТ СССР № 169.

    Считается полными месяцами от даты приёма до даты увольнения; излишек меньше половины
    месяца отбрасывается, 15 дней и более — округляются до полного месяца. Дни, не входящие
    в стаж (отпуск без сохранения зарплаты свыше 14 дней, отпуск по уходу за ребёнком —
    ст. 121 ТК РФ), отнимаются от даты увольнения: иначе стаж окажется больше реального.
    """
    from datetime import date, timedelta

    start = date.fromisoformat(str(hired)[:10])
    end = date.fromisoformat(str(dismissal)[:10])
    excluded = int(excluded_days or 0)
    if excluded:
        end = end - timedelta(days=excluded)
    if end < start:
        return {"months": 0, "rest_days": 0, "rounded_up": False, "excluded_days": excluded,
                "basis": "дата увольнения раньше даты приёма — стажа нет"}
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    months = max(months, 0)
    # остаток дней после полных месяцев
    year, month = start.year + (start.month - 1 + months) // 12, (start.month - 1 + months) % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    anchor = date(year, month, day)
    rest = (end - anchor).days
    rounded = False
    if rest >= 15:
        months += 1
        rest -= 15
        rounded = True
    return {"months": months, "rest_days": rest, "rounded_up": rounded,
            "excluded_days": excluded,
            "basis": (f"стаж с {hired} по {dismissal}"
                      + (f" без {excluded} дн. вне стажа" if excluded else "")
                      + f" = {months} полных месяцев"
                      + (" (излишек 15+ дней округлён)" if rounded else
                         " (излишек меньше 15 дней отброшен)"))}


def dismissal(order: dict, employee: dict, rules: dict, *, period: str) -> dict:
    """Увольнение: компенсация за неиспользованный отпуск.

    Дни компенсации = стаж в месяцах × 2,33 (28 / 12) минус отгулянные дни; за 11 и более
    месяцев рабочего года — полные 28 дней (п. 28 Правил № 169). Средний дневной заработок —
    как у отпускных (база за 12 месяцев / 12 / 29,3), но не ниже МРОТ / 29,3.

    Компенсация облагается НДФЛ и страховыми взносами: это выплата в рамках трудовых
    отношений, а не государственное пособие.
    """
    rules_dis = (rules.get("benefits") or {}).get("dismissal") or {}
    days_per_month = Decimal(str(rules_dis.get("days_per_month") or "2.3333"))
    year_days = Decimal(str(rules_dis.get("year_days") or 28))
    errors: list[dict] = []
    warnings: list[str] = []
    verify = False

    hired = order.get("hired") or employee.get("hired")
    date_out = order.get("dismissal_date") or order.get("date")
    if not hired:
        errors.append({"code": "DISMISSAL_NO_HIRED", "what": order.get("number"),
                       "why": "нет даты приёма: отпускной стаж не из чего считать"})
    if not date_out:
        errors.append({"code": "DISMISSAL_NO_DATE", "what": order.get("number"),
                       "why": "нет даты увольнения"})

    taken_raw = order.get("vacation_days_taken")
    taken_given = taken_raw not in (None, "")
    taken = Decimal(str(taken_raw if taken_given else 0))
    if not taken_given:
        verify = True
        warnings.append("не указано, сколько дней отпуска уже отгуляно: принято 0 — "
                        "компенсация может быть завышена, цифра на проверку")

    months_info = {"months": 0, "basis": "", "rounded_up": False}
    if hired and date_out:
        months_info = seniority_months(hired, date_out,
                                       excluded_days=int(order.get("excluded_days") or 0))
    months = int(months_info.get("months") or 0)
    full_years, rest_months = divmod(months, 12)
    if rest_months >= 11:
        rest_days = year_days  # п. 28 Правил № 169: 11 месяцев и более — полная компенсация
        rest_rule = "п. 28 Правил № 169: 11 месяцев и более — полные 28 дней"
    else:
        rest_days = (Decimal(rest_months) * days_per_month).quantize(EPS, rounding=ROUND_HALF_UP)
        rest_rule = f"{rest_months} мес. × {days_per_month}"
    earned = kopeck(Decimal(full_years) * year_days + rest_days)
    left = kopeck(earned - taken)
    if left < 0:
        errors.append({"code": "DISMISSAL_OVERPAID", "what": order.get("number"),
                       "why": f"отгуляно {taken} дн. больше заработанного {earned}: "
                              f"переплата отпускных, а не компенсация — нужен расчёт удержания"})
        left = Decimal("0")
    # округление дней — только в пользу работника (письмо Роструда от 31.10.2008 № 5921-ТЗ)
    days_paid = Decimal(int(left.to_integral_value(rounding=ROUND_HALF_UP)))
    if days_paid < left:
        days_paid += Decimal("1")
    if days_paid > left:
        warnings.append(f"дни компенсации {left} округлены до {days_paid} в пользу работника "
                        f"(письмо Роструда от 31.10.2008 № 5921-ТЗ); законом округление не "
                        f"предусмотрено")

    avg_days = Decimal(str(((rules.get("benefits") or {}).get("vacation") or {})
                           .get("avg_month_days") or "29.3"))
    months_base = Decimal(str(((rules.get("benefits") or {}).get("vacation") or {})
                              .get("months") or 12))
    base = order.get("base_12m")
    direct_sdz = order.get("sdz")
    sdz = Decimal("0")
    source = ""
    if direct_sdz not in (None, ""):
        sdz = kopeck(direct_sdz)
        source = "средний дневной заработок указан в приказе"
    elif base not in (None, "") and kopeck(base) > 0:
        sdz = kopeck(Decimal(str(base)) / months_base / avg_days)
        source = f"база за 12 месяцев {rub(base)} / {int(months_base)} / {avg_days}"
    else:
        errors.append({"code": "DISMISSAL_NO_BASE", "what": order.get("number"),
                       "why": "нет ни заработка за 12 месяцев, ни среднего дневного заработка: "
                              "компенсация из оклада не считается — нужна ведомость за "
                              "расчётный период (Положение № 540)"})
    mrot = Decimal(str(rules.get("mrot") or 0))
    min_sdz = kopeck(mrot / avg_days) if mrot else Decimal("0")
    raised = False
    if sdz and min_sdz and sdz < min_sdz:
        sdz, raised = min_sdz, True
        warnings.append(f"средний дневной заработок ниже минимума {rub(min_sdz)} "
                        f"(МРОТ {rub(mrot)} / {avg_days}): взят минимум")

    amount = kopeck(sdz * days_paid)
    row = {
        "kind": "dismissal", "number": order.get("number"), "date": order.get("date"),
        "days": float(days_paid), "start": hired, "end": date_out,
        "months": months, "years": full_years, "rest_months": rest_months,
        "earned_days": num(earned), "taken_days": num(taken),
        "days_rule": rest_rule, "seniority_basis": months_info.get("basis") or "",
        "sdz": num(sdz), "sdz_source": source, "raised_to_min": raised,
        "amount": num(amount), "fund_amount": 0.0,
        "taxable": num(amount), "contrib_base": num(amount),
        "verify": verify, "errors": errors, "warnings": warnings,
        "memo": (f"увольнение {order.get('number')}: стаж {months} мес. → заработано {earned} дн., "
                 f"отгуляно {taken}, к компенсации {days_paid} дн. × {rub(sdz)} = {rub(amount)}; "
                 f"облагается НДФЛ и взносами"),
    }
    if any(item.get("code") == "DISMISSAL_OVERPAID" for item in errors):
        row["not_calculated"] = True
    return row


def report(orders: list[dict], staff: dict, rules: dict, *, period: str) -> dict:
    """Собрать все приказы месяца: строки по людям, суммы, что не посчиталось и почему."""
    employees = {str(row.get("tab")): row for row in staff.get("employees") or []}
    result = {"period": period, "lines": [], "by_tab": {}, "errors": [], "gaps": [],
              "warnings": [], "totals": {"count": 0, "amount": 0.0, "sick_employer": 0.0,
                                          "sick_fund": 0.0, "vacation": 0.0, "bonus": 0.0,
                                          "dismissal": 0.0,
                                          "taxable": 0.0, "contrib_base": 0.0},
              "not_included": [dict(item) for item in NOT_INCLUDED]}
    seen: set[tuple] = set()
    for order in orders:
        kind = str(order.get("kind") or "")
        tab = str(order.get("tab") or "")
        number = order.get("number")
        if kind not in KINDS:
            result["errors"].append({"code": "ORDER_BAD_KIND", "what": number,
                                     "why": f"вид приказа {kind!r} не поддержан; есть: "
                                            f"{', '.join(KINDS)}"})
            continue
        employee = employees.get(tab)
        if not employee:
            result["errors"].append({"code": "ORDER_NO_EMPLOYEE", "what": tab,
                                     "why": f"табельный номер {tab} не найден в карточках: "
                                            f"приказ {number} не к кому применить"})
            continue
        if (kind, tab, number) in seen:
            result["errors"].append({"code": "ORDER_DUPLICATE", "what": number,
                                     "why": "этот приказ уже учтён в месяце"})
            continue
        seen.add((kind, tab, number))
        # месяц приказа берётся из самого приказа: явное поле, иначе дата. Иначе приказ
        # октября молча попал бы в ноябрь, стоило запросить другой период
        order_period = str(order.get("period") or str(order.get("date") or "")[:7] or period)
        if order_period != period:
            result["gaps"].append({"what": f"приказ {number}", "why": "относится к другому месяцу",
                                   "ask": f"провести в {order_period} или изменить дату приказа"})
            continue
        row = {"sick": sick, "vacation": vacation, "bonus": bonus,
               "dismissal": dismissal}[kind](
            order, employee, rules, period=period)
        row["tab"] = tab
        row["name"] = employee.get("name")
        row["position"] = employee.get("position")
        row["object"] = employee.get("object")
        result["errors"].extend(row.pop("errors"))
        result["warnings"].extend(row.pop("warnings"))
        result["lines"].append(row)
        if row["amount"] <= 0 and kind != "sick":
            continue
        slot = result["by_tab"].setdefault(tab, {"taxable": Decimal("0"),
                                                 "contrib_base": Decimal("0"),
                                                 "sick_employer": Decimal("0"),
                                                 "sick_fund": Decimal("0"),
                                                 "vacation": Decimal("0"),
                                                 "bonus": Decimal("0"),
                                                 "dismissal": Decimal("0")})
        slot["taxable"] += kopeck(row["taxable"])
        slot["contrib_base"] += kopeck(row["contrib_base"])
        slot["sick_employer"] += kopeck(row["amount"]) if kind == "sick" else Decimal("0")
        slot["sick_fund"] += kopeck(row["fund_amount"])
        slot["vacation"] += kopeck(row["amount"]) if kind == "vacation" else Decimal("0")
        slot["bonus"] += kopeck(row["amount"]) if kind == "bonus" else Decimal("0")
        slot["dismissal"] += kopeck(row["amount"]) if kind == "dismissal" else Decimal("0")
        totals = result["totals"]
        totals["amount"] = num(kopeck(totals["amount"]) + kopeck(row["amount"]))
        totals["sick_employer"] = num(kopeck(totals["sick_employer"])
                                      + (kopeck(row["amount"]) if kind == "sick" else 0))
        totals["sick_fund"] = num(kopeck(totals["sick_fund"]) + kopeck(row["fund_amount"]))
        totals["vacation"] = num(kopeck(totals["vacation"])
                                 + (kopeck(row["amount"]) if kind == "vacation" else 0))
        totals["bonus"] = num(kopeck(totals["bonus"])
                              + (kopeck(row["amount"]) if kind == "bonus" else 0))
        totals["dismissal"] = num(kopeck(totals["dismissal"])
                                  + (kopeck(row["amount"]) if kind == "dismissal" else 0))
        totals["taxable"] = num(kopeck(totals["taxable"]) + kopeck(row["taxable"]))
        totals["contrib_base"] = num(kopeck(totals["contrib_base"]) + kopeck(row["contrib_base"]))
        totals["count"] += 1
    if not orders:
        result["gaps"].append({"what": "приказы за месяц",
                               "why": "файл приказов пуст: считаем только оклад по табелю",
                               "ask": "приказы на больничные, отпуска и премии (или подтверждение, "
                                      "что их не было)"})
    return result


def to_markdown(result: dict, rules: dict | None = None) -> str:
    """Приказы месяца: таблица и то, что не посчиталось."""
    rules = rules or {}
    lines = [f"# Приказы за {result.get('period')}", ""]
    if result.get("lines"):
        lines.append("| приказ | вид | сотрудник | дни | сумма | из них СФР | примечание |")
        lines.append("|---|---|---|---:|---:|---:|---|")
        for row in result["lines"]:
            lines.append(f"| {row.get('number')} | {KINDS.get(row['kind'], row['kind'])} | "
                         f"{row.get('name')} | {row.get('days') or ''} | {rub(row.get('amount'))} | "
                         f"{rub(row.get('fund_amount')) if row['kind'] == 'sick' else ''} | "
                         f"{row.get('cause') or row.get('sdz_source') or row.get('basis') or row.get('percent_basis') or ''} |")
    else:
        lines.append("Приказов нет: считается только оклад по табелю.")
    totals = result.get("totals") or {}
    lines.append("")
    lines.append(f"Приказов {totals.get('count')}: начислено {rub(totals.get('amount'))} "
                 f"(больничные работодателя {rub(totals.get('sick_employer'))}, отпускные "
                 f"{rub(totals.get('vacation'))}, премии {rub(totals.get('bonus'))}, "
                 f"компенсации при увольнении {rub(totals.get('dismissal'))}); "
                 f"за счёт СФР {rub(totals.get('sick_fund'))} — не наш расход.")
    if result.get("errors"):
        lines.append("")
        lines.append("## Не посчитано")
        for item in result["errors"]:
            lines.append(f"- **{item.get('code')}** · {item.get('what')}: {item.get('why')}")
    if result.get("warnings"):
        lines.append("")
        lines.append("## На проверку")
        for item in result["warnings"]:
            lines.append(f"- {item}")
    if result.get("not_included"):
        lines.append("")
        lines.append("## Не включено")
        for item in result["not_included"]:
            lines.append(f"- **{item.get('what')}**: {item.get('why')}")
    return "\n".join(lines) + "\n"
