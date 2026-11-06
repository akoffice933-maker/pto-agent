# -*- coding: utf-8 -*-
"""График финансирования и комплекты: что попадёт в платёж, а что переносится и по какой цене.

Портфель (круг 14) отвечает «сколько денег держит непредъявление». Руководителю нужен третий
вопрос: **«что из этих денег попадёт в платёж этого месяца, а что переносится — и сколько стоит
перенос»**.

Модуль не считает деньги «по логике», а связывает два файла: график финансирования (сроки
предъявления, даты платежей, суммы) и портфель комплектов (можно ли предъявлять). Отсюда правила:

1. **Сроки и даты платежей берутся только из графика.** Нет даты предъявления — `DEADLINE_NOT_STATED`,
   а не «обычно к 10-му числу». Нет графика — платёж не придумывается.
2. **Сумма платежа сверяется с актом.** Расхождение графика и акта — `PAYMENT_AMOUNT_MISMATCH`;
   в расчёт идёт сумма акта, потому что платят по акту.
3. **В платёж попадает только предъявляемый акт.** Комплект не сдан — деньги не в платеже,
   а в переносе, с указанием, какие разделы мешают.
4. **Пени считаются только по заявленной ставке.** Есть ставка в договоре (с пунктом) — считаем
   по дням просрочки и приводим формулу. Нет ставки — «цена переноса не заявлена»
   (`TRANSFER_COST_NOT_STATED`), а не «0 ₽».
5. **Удержание — только из договора.** Нет процента удержания — «к оплате = сумма акта»,
   и отдельно сказано, что удержание не заявлено.
6. **Акт считается один раз.** Акт в двух строках графика — `ACT_DOUBLE_IN_PAYMENTS`.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .pack_portfolio import ACT_RELEASABLE, build_portfolio

SCHEMA = "pto-agent/funding-plan@1"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

PAY_IN_PAYMENT = "в платёж"
PAY_TRANSFER = "перенос"
PAY_NOT_ESTIMATED = "не оценено"
PAY_NOT_LINKED = "не сверено с актом"

CHECK_PAYMENTS_EMPTY = "PAYMENTS_EMPTY"
CHECK_PORTFOLIO_MISSING = "PORTFOLIO_MISSING"
CHECK_PORTFOLIO_NOT_ALLOWED = "PORTFOLIO_HAS_BLOCKERS"
CHECK_ACT_NOT_IN_PORTFOLIO = "ACT_NOT_IN_PORTFOLIO"
CHECK_ACT_WITHOUT_PAYMENT = "ACT_WITHOUT_PAYMENT"
CHECK_PAYMENT_WITHOUT_AMOUNT = "PAYMENT_WITHOUT_AMOUNT"
CHECK_AMOUNT_MISMATCH = "PAYMENT_AMOUNT_MISMATCH"
CHECK_DEADLINE_NOT_STATED = "DEADLINE_NOT_STATED"
CHECK_PAYMENT_BEFORE_DEADLINE = "PAYMENT_DATE_BEFORE_DEADLINE"
CHECK_DEADLINE_MISSED = "DEADLINE_MISSED"
CHECK_DEADLINE_SOON = "DEADLINE_SOON"
CHECK_TRANSFER_COST_NOT_STATED = "TRANSFER_COST_NOT_STATED"
CHECK_RETENTION_NOT_STATED = "RETENTION_NOT_STATED"
CHECK_TERMS_NOT_STATED = "PAYMENT_TERMS_NOT_STATED"
CHECK_ACT_DOUBLE_IN_PAYMENTS = "ACT_DOUBLE_IN_PAYMENTS"
CHECK_MONTH_BAD = "MONTH_UNREADABLE"

MONTHS_RU = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
             7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"}


def _rub(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _money(value: float | None) -> str:
    return "не оценено" if value is None else f"{value:,.2f} ₽".replace(",", " ")


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    text = str(value).strip().replace(" ", "").replace("\u00a0", "").replace("%", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number else None


def _date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def month_label(month: str) -> str:
    try:
        year, number = str(month).split("-")
        return f"{MONTHS_RU[int(number)]} {year}"
    except (ValueError, KeyError):
        return str(month)


def _portfolio_config(config: dict, base: Path) -> tuple[dict | None, str | None]:
    value = config.get("portfolio")
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        path = Path(value) if Path(value).is_absolute() else base / value
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except (json.JSONDecodeError, OSError) as exc:
            return None, f"{path.name}: портфель не читается ({exc.__class__.__name__})"
    if config.get("objects"):
        return {"portfolio": config.get("name") or "портфель",
                "objects": config["objects"]}, None
    return None, "в конфиге нет ни портфеля, ни объектов"


def build_plan(config: dict, base_dir: str | Path = ".", out_dir: str | Path = "out/finance",
               today: date | None = None) -> dict:
    base = Path(base_dir)
    out = Path(out_dir)
    today = today or date.today()
    checks: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    portfolio_cfg, problem = _portfolio_config(config, base)
    if portfolio_cfg is None:
        add(CHECK_PORTFOLIO_MISSING, LEVEL_ERROR,
            f"портфель комплектов не собран: {problem} — без него нельзя сказать, что попадёт "
            f"в платёж")
        portfolio = {"objects": [], "money": {"at_risk_rub": 0.0, "released_rub": 0.0},
                     "totals": {"objects": 0}, "checks": []}
    else:
        portfolio = build_portfolio(portfolio_cfg, base_dir=base, out_dir=out / "packs",
                                    today=today)
        if portfolio["totals"].get("blocked") or portfolio["totals"].get("without_pack"):
            add(CHECK_PORTFOLIO_NOT_ALLOWED, LEVEL_WARNING,
                f"в портфеле объектов с блокерами {portfolio['totals']['blocked']} и без "
                f"комплекта {portfolio['totals']['without_pack']} — часть платежей уйдёт "
                f"в перенос")

    acts: dict[str, dict] = {}
    for obj in portfolio["objects"]:
        for act in obj["acts"]:
            acts.setdefault(act["act_id"], {**act, "object": obj["object_name"],
                                            "pack_allowed": obj["can_submit"]})

    contract = config.get("contract") or {}
    terms = contract.get("payment_terms") or {}
    retention = contract.get("retention") or {}
    penalty = contract.get("penalty") or {}
    terms_basis = terms.get("basis") or contract.get("basis")
    if not terms_basis:
        add(CHECK_TERMS_NOT_STATED, LEVEL_WARNING,
            "в договоре не заявлены срок оплаты и пункт-основание — даты платежей берутся "
            "только из графика, а «когда заплатят после предъявления» не считается")
    retention_pct = _num(retention.get("pct"))
    if retention_pct is None:
        add(CHECK_RETENTION_NOT_STATED, LEVEL_WARNING,
            "процент гарантийного удержания в договоре не заявлен — «к оплате» показано "
            "равным сумме акта, удержание не выдумывается")
    penalty_pct = _num(penalty.get("pct_per_day"))
    penalty_basis = penalty.get("basis")

    payments_cfg = config.get("payments") or []
    if not payments_cfg:
        add(CHECK_PAYMENTS_EMPTY, LEVEL_ERROR,
            "в конфиге нет ни одной строки графика финансирования — считать нечего")

    rows: list[dict] = []
    seen: dict[str, str] = {}
    for item in payments_cfg:
        month = str(item.get("month") or "")
        act_id = str(item.get("act_id") or "").strip()
        row: dict = {
            "month": month, "month_label": month_label(month),
            "act_id": act_id, "object": None,
            "amount_rub": None, "amount_source": None, "amount_status": "не оценена",
            "submission_deadline": item.get("submission_deadline"),
            "payment_date": item.get("payment_date"),
            "basis": item.get("basis") or config.get("basis"),
            "status": PAY_NOT_ESTIMATED, "why": None,
            "blocked_sections": [], "retention_rub": None, "to_pay_rub": None,
            "penalty_rub": None, "penalty_formula": None,
            "days_to_deadline": None, "days_overdue": None,
        }
        if not month or "-" not in month:
            add(CHECK_MONTH_BAD, LEVEL_WARNING,
                f"строка графика по акту «{act_id or '—'}»: месяц «{month}» не читается "
                f"(нужен вид 2026-11)", act=act_id)

        act = acts.get(act_id)
        if act is None:
            add(CHECK_ACT_NOT_IN_PORTFOLIO, LEVEL_ERROR,
                f"платёж по акту «{act_id}»: такого акта нет в портфеле комплектов — "
                f"нельзя сказать, можно ли его предъявлять, сумма не сверена", act=act_id)
            row["status"] = PAY_NOT_LINKED
            payment_amount = _num(item.get("amount_rub"))
            if payment_amount is None:
                add(CHECK_PAYMENT_WITHOUT_AMOUNT, LEVEL_ERROR,
                    f"акт «{act_id}»: в графике нет суммы — платёж «не оценён»", act=act_id)
            else:
                row.update({"amount_rub": _rub(payment_amount),
                            "amount_source": "график финансирования (акта нет в портфеле)",
                            "amount_status": "известна из графика"})
            rows.append(row)
            continue

        row["object"] = act["object"]
        schedule_amount = _num(item.get("amount_rub"))
        act_amount = act.get("amount_rub")
        row["schedule_amount_rub"] = _rub(schedule_amount)
        if schedule_amount is not None and act_amount is not None and \
                abs(schedule_amount - float(act_amount)) > 0.01:
            add(CHECK_AMOUNT_MISMATCH, LEVEL_ERROR,
                f"акт «{act_id}»: в графике {_money(schedule_amount)}, в акте "
                f"{_money(act_amount)} — платят по акту, в расчёт идёт сумма акта",
                act=act_id)
        if act_amount is not None:
            amount = act_amount
            row["amount_source"] = act.get("amount_source")
        else:
            amount = schedule_amount
            row["amount_source"] = (item.get("source") or "график финансирования"
                                    if schedule_amount is not None else None)
        if amount is None:
            add(CHECK_PAYMENT_WITHOUT_AMOUNT, LEVEL_ERROR,
                f"акт «{act_id}»: суммы нет ни в графике, ни в акте — платёж «не оценён», "
                f"а не 0 ₽", act=act_id)
            row.update({"status": PAY_NOT_ESTIMATED, "why": "нет суммы ни в графике, ни в акте"})
            rows.append(row)
            continue
        row.update({"amount_rub": _rub(amount), "amount_status": "известна"})

        # ─── в платёж или в перенос ───
        if act["status"] == ACT_RELEASABLE:
            row["status"] = PAY_IN_PAYMENT
        else:
            row["status"] = PAY_TRANSFER
            row["blocked_sections"] = act.get("blocked_by_sections") or []
            row["why"] = (act.get("hindrance") or act.get("why")
                          or "комплект объекта не предъявляется")

        # ─── сроки ───
        deadline = _date(row["submission_deadline"])
        payment_date = _date(row["payment_date"])
        if deadline is None:
            add(CHECK_DEADLINE_NOT_STATED, LEVEL_WARNING,
                f"акт «{act_id}»: срок предъявления не заявлен — «успеваем / не успеваем» "
                f"не считается, дата не выдумывается", act=act_id)
        else:
            row["days_to_deadline"] = (deadline - today).days
            if row["status"] == PAY_TRANSFER:
                if (deadline - today).days < 0:
                    row["days_overdue"] = (today - deadline).days
                    add(CHECK_DEADLINE_MISSED, LEVEL_ERROR,
                        f"акт «{act_id}» ({row['object']}): срок предъявления "
                        f"{deadline.isoformat()} прошёл {row['days_overdue']} дн. назад, "
                        f"комплект не сдан — {_money(row['amount_rub'])} переносятся",
                        act=act_id)
                elif (deadline - today).days <= 7:
                    add(CHECK_DEADLINE_SOON, LEVEL_WARNING,
                        f"акт «{act_id}» ({row['object']}): до срока предъявления "
                        f"{(deadline - today).days} дн., комплект не сдан — "
                        f"{_money(row['amount_rub'])} под угрозой переноса", act=act_id)
        if deadline and payment_date and payment_date < deadline:
            add(CHECK_PAYMENT_BEFORE_DEADLINE, LEVEL_ERROR,
                f"акт «{act_id}»: дата платежа {payment_date.isoformat()} раньше срока "
                f"предъявления {deadline.isoformat()} — платёж не может наступить до "
                f"предъявления", act=act_id)

        # ─── удержание и к оплате ───
        # «К оплате» есть только там, где платёж наступает: перенесённая строка — это не
        # деньги месяца, а долг, который ещё не предъявлен.
        if retention_pct is not None:
            row["retention_rub"] = _rub(row["amount_rub"] * retention_pct / 100)
        if row["status"] == PAY_IN_PAYMENT:
            row["to_pay_rub"] = (row["amount_rub"] - (row["retention_rub"] or 0))
        else:
            row["to_pay_rub"] = None

        # ─── цена переноса ───
        if row["status"] == PAY_TRANSFER:
            if penalty_pct is None:
                add(CHECK_TRANSFER_COST_NOT_STATED, LEVEL_WARNING,
                    f"акт «{act_id}»: срок нарушен, но ставка пеней в договоре не заявлена — "
                    f"цена переноса «не заявлена», а не 0 ₽", act=act_id)
                row["penalty_note"] = "цена переноса не заявлена: ставка пеней в договоре не указана"
            elif row["days_overdue"]:
                row["penalty_rub"] = _rub(row["amount_rub"] * penalty_pct / 100
                                          * row["days_overdue"])
                row["penalty_formula"] = (f"{_money(row['amount_rub'])} × {penalty_pct} % × "
                                          f"{row['days_overdue']} дн. = "
                                          f"{_money(row['penalty_rub'])}")
                row["penalty_basis"] = penalty_basis
            else:
                row["penalty_note"] = ("срок не нарушен — пени не считаются; цена переноса "
                                       "появится после срока")

        if act_id in seen:
            add(CHECK_ACT_DOUBLE_IN_PAYMENTS, LEVEL_ERROR,
                f"акт «{act_id}» встречается в графике дважды ({seen[act_id]} и {month}) — "
                f"деньги в платёж нельзя ставить второй раз", act=act_id)
            row["double"] = True
        else:
            seen[act_id] = month
        rows.append(row)

    # акты портфеля, которых нет в графике
    for act_id, act in acts.items():
        if act_id not in seen:
            add(CHECK_ACT_WITHOUT_PAYMENT, LEVEL_WARNING,
                f"акт «{act_id}» ({act['object']}) есть в портфеле, но не заявлен в графике — "
                f"срок платежа по нему не проверен", act=act_id)

    # ─── итоги ───
    months: dict[str, dict] = {}
    for row in rows:
        bucket = months.setdefault(row["month"], {
            "month": row["month"], "label": row["month_label"], "payments": 0,
            "planned_rub": 0.0, "in_payment_rub": 0.0, "transfer_rub": 0.0,
            "penalty_rub": 0.0, "unestimated": 0, "missed": 0,
        })
        bucket["payments"] += 1
        if row["status"] == PAY_IN_PAYMENT:
            bucket["in_payment_rub"] += row["to_pay_rub"] or 0
        elif row["status"] == PAY_TRANSFER:
            bucket["transfer_rub"] += row["amount_rub"] or 0
            if row["penalty_rub"]:
                bucket["penalty_rub"] += row["penalty_rub"]
            if row["days_overdue"]:
                bucket["missed"] += 1
        else:
            bucket["unestimated"] += 1
        bucket["planned_rub"] += row["amount_rub"] or 0

    plan = {
        "schema": SCHEMA, "kind": "funding_plan",
        "title": "График финансирования и комплекты исполнительной документации",
        "name": config.get("name") or "График финансирования",
        "contract": {"number": contract.get("number"),
                     "payment_terms": terms_basis,
                     "retention": ({"pct": retention_pct, "basis": retention.get("basis")}
                                   if retention_pct is not None else None),
                     "penalty": ({"pct_per_day": penalty_pct, "basis": penalty_basis}
                                 if penalty_pct is not None else None)},
        "built_at": today.isoformat(),
        "payments": rows,
        "months": [months[key] for key in sorted(months)],
        "checks": checks,
        "portfolio": {"objects": portfolio["totals"].get("objects", 0),
                      "blocked": portfolio["totals"].get("blocked", 0),
                      "without_pack": portfolio["totals"].get("without_pack", 0),
                      "at_risk_rub": portfolio.get("money", {}).get("at_risk_rub"),
                      "released_rub": portfolio.get("money", {}).get("released_rub")},
        "totals": {
            "payments": len(rows),
            "plan_rub": _rub(sum(r["amount_rub"] or 0 for r in rows)),
            "in_payment_rub": _rub(sum(r["to_pay_rub"] or 0 for r in rows
                                       if r["status"] == PAY_IN_PAYMENT)),
            "transfer_rub": _rub(sum(r["amount_rub"] or 0 for r in rows
                                     if r["status"] == PAY_TRANSFER)),
            "penalty_rub": _rub(sum(r["penalty_rub"] or 0 for r in rows)),
            "retention_rub": _rub(sum(r["retention_rub"] or 0 for r in rows
                                      if r["retention_rub"] is not None)),
            "unestimated": len([r for r in rows
                                if r["status"] in (PAY_NOT_ESTIMATED, PAY_NOT_LINKED)]),
            "missed": len([r for r in rows if r["days_overdue"]]),
            "errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
            "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING]),
        },
        "disclaimer": ("График показывает, какая часть запланированных платежей попадёт "
                       "в срок, а какая переносится из-за несданного комплекта. Сроки, суммы и "
                       "ставки берутся из файлов графика и договора; отсутствие данных — "
                       "«не заявлено», а не 0 ₽. Решение о предъявлении принимает ИТР."),
    }
    plan["verdict"] = verdict(plan)
    files = write(plan, out)
    plan["files"] = files
    return plan


def verdict(plan: dict) -> dict:
    totals = plan.get("totals") or {}
    planned = totals.get("plan_rub") or 0
    in_payment = totals.get("in_payment_rub") or 0
    transfer = totals.get("transfer_rub") or 0
    reasons: list[str] = []
    twice = [c for c in plan.get("checks") or [] if c["code"] == CHECK_ACT_DOUBLE_IN_PAYMENTS]
    if twice:
        reasons.append("акт стоит в графике дважды — сумма в платёж не может считаться")
    if totals.get("missed"):
        reasons.append(f"сроков предъявления пропущено: {totals['missed']}")
    if totals.get("unestimated"):
        reasons.append(f"строк графика без суммы: {totals['unestimated']}")
    return {
        "plan_rub": planned,
        "in_payment_rub": in_payment,
        "transfer_rub": transfer,
        "penalty_rub": totals.get("penalty_rub") or 0,
        "share_in_payment_pct": (round(in_payment / planned * 100, 2) if planned else None),
        "summary": (f"в платёж попадёт {_money(in_payment)} из {_money(planned)} "
                    f"запланированных; переносится {_money(transfer)}, "
                    f"пени {_money(totals.get('penalty_rub'))}"),
        "reasons": reasons,
    }


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(plan: dict) -> str:
    totals = plan["totals"]
    lines = [f"# {plan['title']}", "",
             f"**{plan['name']}** · договор {plan['contract'].get('number') or 'не указан'} · "
             f"на {plan['built_at']}", ""]
    lines += [f"Строк графика: {totals['payments']} · запланировано {_money(totals['plan_rub'])} · "
              f"**в платёж {_money(totals['in_payment_rub'])}** · переносится "
              f"{_money(totals['transfer_rub'])} · пени {_money(totals['penalty_rub'])}", ""]
    if totals.get("unestimated"):
        lines += [f"Не оценено строк: {totals['unestimated']} — по ним суммы нет ни в графике, "
                  f"ни в акте.", ""]

    lines += ["## По месяцам", "",
              "| Месяц | Строк | Запланировано | В платёж | Перенос | Пени | Пропущено сроков |",
              "|---|---|---|---|---|---|---|"]
    for month in plan["months"]:
        lines.append(f"| {month['label']} | {month['payments']} | "
                     f"{_money(month['planned_rub'])} | {_money(month['in_payment_rub'])} | "
                     f"{_money(month['transfer_rub'])} | {_money(month['penalty_rub'])} | "
                     f"{month['missed']} |")
    lines.append("")

    lines += ["## Платежи", "",
              "| Месяц | Акт | Объект | Сумма | Срок предъявления | Платёж | Состояние |",
              "|---|---|---|---|---|---|---|"]
    for row in plan["payments"]:
        deadline = row["submission_deadline"] or "не заявлен"
        if row.get("days_overdue"):
            deadline += f" (просрочен на {row['days_overdue']} дн.)"
        elif row.get("days_to_deadline") is not None:
            deadline += f" (осталось {row['days_to_deadline']} дн.)"
        lines.append(f"| {row['month_label']} | {row['act_id']} | {row['object'] or '—'} | "
                     f"{_money(row['amount_rub'])} | {deadline} | "
                     f"{row['payment_date'] or 'не заявлен'} | {row['status']} |")
    lines.append("")

    transfers = [r for r in plan["payments"] if r["status"] == PAY_TRANSFER]
    if transfers:
        lines += ["## Что переносится и почему", ""]
        for row in transfers:
            sections = ", ".join(row["blocked_sections"]) or row.get("why") or "—"
            lines.append(f"- **{row['act_id']}** ({row['month_label']}, "
                         f"{_money(row['amount_rub'])}) — {sections}")
        lines.append("")
        priced = [r for r in transfers if r["penalty_rub"]]
        if priced:
            lines += ["### Цена переноса по договору", ""]
            for row in priced:
                lines.append(f"- {row['act_id']}: {row['penalty_formula']} "
                             f"({row.get('penalty_basis') or 'основание не указано'})")
            lines.append("")
        if any(r.get("penalty_note") for r in transfers):
            lines += ["### Где цену назвать нельзя", ""]
            lines += [f"- {r['act_id']}: {r['penalty_note']}" for r in transfers
                      if r.get("penalty_note")] + [""]

    if plan["contract"].get("retention"):
        item = plan["contract"]["retention"]
        lines += [f"Удержание {item['pct']} % "
                  f"({item.get('basis') or 'основание не указано'}): «в платёж» показан за "
                  f"вычетом удержания.", ""]

    blockers = [c for c in plan["checks"] if c["level"] == LEVEL_ERROR]
    if blockers:
        lines += ["## Блокеры", ""] + [f"- **{c['code']}** — {c['message']}"
                                       for c in blockers] + [""]
    warnings = [c for c in plan["checks"] if c["level"] == LEVEL_WARNING]
    if warnings:
        lines += ["## Требует решения", ""] + [f"- {c['code']} — {c['message']}"
                                               for c in warnings] + [""]

    lines += ["## Вердикт", "", f"- {plan['verdict']['summary']}"]
    lines += [f"- {reason}" for reason in plan["verdict"]["reasons"]]
    lines += ["", f"> {plan['disclaimer']}", ""]
    return "\n".join(lines)


def export_xlsx(plan: dict, path: str | Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    fill = PatternFill("solid", fgColor="1F4E79")
    font = Font(color="FFFFFF", bold=True)

    def sheet(title, headers, widths, rows):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        for row in rows:
            ws.append(row)
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width
        ws.freeze_panes = "A2"

    wb.remove(wb.active)
    sheet("Месяцы",
          ["Месяц", "Строк", "Запланировано, ₽", "В платёж, ₽", "Перенос, ₽", "Пени, ₽",
           "Пропущено сроков", "Не оценено"],
          [20, 8, 18, 16, 16, 14, 16, 12],
          [[m["label"], m["payments"], m["planned_rub"], m["in_payment_rub"], m["transfer_rub"],
            m["penalty_rub"], m["missed"], m["unestimated"]] for m in plan["months"]])
    sheet("Платежи",
          ["Месяц", "Акт", "Объект", "Сумма, ₽", "Срок предъявления", "Дней до срока",
           "Просрочено, дн.", "Дата платежа", "Удержание, ₽", "К оплате, ₽", "Пени, ₽",
           "Состояние", "Мешает"],
          [16, 14, 26, 16, 18, 14, 14, 14, 14, 14, 12, 24, 46],
          [[r["month_label"], r["act_id"], r["object"], r["amount_rub"],
            r["submission_deadline"], r["days_to_deadline"], r["days_overdue"],
            r["payment_date"], r["retention_rub"], r["to_pay_rub"], r["penalty_rub"],
            r["status"], ", ".join(r["blocked_sections"]) or (r.get("why") or "")]
           for r in plan["payments"]])
    sheet("Блокеры", ["Уровень", "Код", "Акт", "Что не так"],
          [10, 30, 16, 100],
          [[("ошибка" if c["level"] == "error" else "внимание"), c["code"], c.get("act", ""),
            c["message"]] for c in plan["checks"]])
    wb.save(path)
    return path


def export_docx(plan: dict, path: str | Path) -> Path:
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("График финансирования и комплекты ИД", level=1)
    doc.add_paragraph(f"{plan['name']} · договор {plan['contract'].get('number') or 'не указан'} · "
                      f"на {plan['built_at']}")
    totals = plan["totals"]
    run = doc.add_paragraph().add_run(
        f"Запланировано: {_money(totals['plan_rub'])}. В платёж попадёт: "
        f"{_money(totals['in_payment_rub'])}. Переносится: {_money(totals['transfer_rub'])}. "
        f"Пени по договору: {_money(totals['penalty_rub'])}.")
    run.bold = True

    doc.add_heading("По месяцам", level=2)
    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells,
                          ["Месяц", "Строк", "В платёж", "Перенос", "Пени", "Пропущено"]):
        cell.text = text
    for month in plan["months"]:
        cells = table.add_row().cells
        cells[0].text = month["label"]
        cells[1].text = str(month["payments"])
        cells[2].text = _money(month["in_payment_rub"])
        cells[3].text = _money(month["transfer_rub"])
        cells[4].text = _money(month["penalty_rub"])
        cells[5].text = str(month["missed"])

    transfers = [r for r in plan["payments"] if r["status"] == PAY_TRANSFER]
    if transfers:
        doc.add_heading("Что переносится", level=2)
        for row in transfers:
            sections = ", ".join(row["blocked_sections"]) or row.get("why") or "—"
            doc.add_paragraph(f"• {row['act_id']} ({row['month_label']}, "
                              f"{_money(row['amount_rub'])}): {sections}", style="List Bullet")

    doc.add_paragraph()
    doc.add_paragraph(f"Вердикт: {plan['verdict']['summary']}")
    for reason in plan["verdict"]["reasons"]:
        doc.add_paragraph(f"• {reason}", style="List Bullet")
    doc.add_paragraph()
    doc.add_paragraph("График подготовил: ______________________ / ______________________")
    doc.add_paragraph("Проверил (начальник ПТО): ______________________ / ______________________")
    doc.add_paragraph("Решение по платежам принял: ______________________ / _________________")
    doc.add_paragraph(plan["disclaimer"]).runs[0].font.size = Pt(9)
    doc.save(path)
    return path


def write(plan: dict, out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tail = str(plan.get("name") or "график")
    for bad in (" ", "/", ",", ":", "«", "»", '"', "\\"):
        tail = tail.replace(bad, "_" if bad == " " else "-")
    tail = tail.strip("_-")[:40] or "график"
    (out / f"funding_{tail}.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    (out / f"funding_{tail}.md").write_text(to_markdown(plan), encoding="utf-8")
    export_xlsx(plan, out / f"funding_{tail}.xlsx")
    export_docx(plan, out / f"funding_{tail}.docx")
    return {name: str(out / f"funding_{tail}.{ext}")
            for name, ext in (("json", "json"), ("markdown", "md"),
                              ("xlsx", "xlsx"), ("docx", "docx"))}
