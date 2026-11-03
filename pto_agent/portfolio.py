# -*- coding: utf-8 -*-
"""Деньги под риском по всем актам объекта.

Один акт — это один срез. Руководителю нужен объект: сколько предъявлено по всем
актам, какая доля этого проверена и сколько денег висит под вопросом по месяцам.

Три правила, которые здесь закреплены:

1. **Непроверенный акт — это не «0 ₽ риска»**, а «не проверялся». Такой акт
   попадает в покрытие (`amount_checked_rub`) и в список «что не проверено»,
   но не в сумму под риском: ноль читался бы как «там всё чисто».
2. **Один и тот же расчёт в одном акте считается один раз** (правило модуля
   `impact`, итерации листа — не повод умножать сумму). Одна и та же позиция
   в разных актах — это разные споры за разные периоды, их сумма складывается.
3. **Акты без распознанного выпуска всё равно проверяются по документам**:
   сверка количества со сметой (акт больше сметного объёма — это уже деньги),
   а всё, что требует сканов, честно уходит в «не оценено».
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .impact import (RISK_TITLES, PriceBook, _num, _rub, _scalar, _unit,
                     assess_impact)

MONTHS_RU = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
             7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь",
             12: "декабрь"}
STATUS_CHECKED = "проверен"
STATUS_UNCHECKED = "не проверялся"


def month_label(month: str) -> str:
    """«2026-09» → «сентябрь 2026»."""
    try:
        year, number = month.split("-")
        return f"{MONTHS_RU[int(number)]} {year}"
    except (ValueError, KeyError):
        return month


def load_acts(path) -> dict:
    """Реестр актов: JSON-файл со списком актов (см. data/demo/acts/acts.json)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    acts = data.get("acts") if isinstance(data, dict) else data
    if not acts:
        raise ValueError("в реестре актов нет ни одного акта")
    return data if isinstance(data, dict) else {"acts": acts}


def _load_conflicts(act: dict, root: Path | None) -> list:
    """Конфликты выпуска для акта: из файла прогона или прямо в реестре."""
    if act.get("conflicts"):
        return act["conflicts"]
    release = act.get("release")
    if not release:
        return []
    path = Path(release if root is None else root / release)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("conflicts"):
        return data["conflicts"]
    impact = data.get("impact") if isinstance(data, dict) else None
    return [] if not impact else []


def check_act_vs_estimate(act: dict, book: PriceBook) -> dict:
    """Сверка акта со сметой по количествам — работает без распознанного выпуска."""
    rows, overrun = [], 0.0
    for pos in act.get("positions") or []:
        code = pos.get("code")
        line = book.lines.get(code) or {}
        estimate = line.get("estimate_qty") or line.get("qty")
        qty = float(pos.get("qty") or 0)
        price = float(pos.get("price") or line.get("price") or 0)
        if not estimate:
            rows.append({"code": code, "qty": qty, "estimate_qty": None,
                         "overrun_qty": None, "money_rub": None,
                         "note": "позиции нет в смете — сверить не с чем"})
            continue
        over = max(0.0, qty - float(estimate))
        money = over * price
        overrun += money
        rows.append({"code": code, "qty": qty, "estimate_qty": float(estimate),
                     "overrun_qty": over, "money_rub": round(money, 2),
                     "note": "" if not over else
                     f"акт больше сметного объёма на {_num(over)} "
                     f"{_unit(pos.get('unit'))}"})
    return {"rows": rows, "overrun_rub": round(overrun, 2)}


def assess_portfolio(acts_data: dict, pto_summary: dict | None = None,
                     root=None, discrepancies_act: str | None = None) -> dict:
    """Свод «выпуск → деньги» по всем актам объекта, с разбивкой по месяцам."""
    pto_summary = pto_summary or {}
    book = PriceBook.from_summary(pto_summary)
    acts = acts_data.get("acts") or []
    root = Path(root) if root else None

    per_act: list[dict] = []
    merged_positions: dict[str, dict] = {}
    merged_unpriced: dict[tuple, dict] = {}
    coverage_rows: list[dict] = []

    for act in acts:
        act_id = act.get("act_id") or act.get("id") or "—"
        amount = float(act.get("amount_rub") or 0)
        month = act.get("month") or (act.get("act_date") or "")[:7]
        conflicts = _load_conflicts(act, root)
        record = {
            "act_id": act_id, "act_date": act.get("act_date"),
            "month": month, "month_label": month_label(month),
            "period": act.get("period") or month_label(month),
            "amount_rub": round(amount, 2),
            "status": STATUS_UNCHECKED, "note": act.get("note") or "",
            "at_risk_rub": 0.0, "background_risk_rub": 0.0,
            "markup_rub": 0.0, "underclaim_rub": 0.0, "share_pct": None,
            "positions_priced": 0, "items_unpriced": 0,
            "sheets_total": 0, "occurrences_total": 0,
            "estimate_overrun_rub": 0.0, "checked_rows": 0,
        }

        # сверка со сметой по количествам — доступна всегда
        check = check_act_vs_estimate(act, book)
        record["estimate_overrun_rub"] = check["overrun_rub"]
        record["checked_rows"] = len(check["rows"])
        for row in check["rows"]:
            if row["money_rub"]:
                coverage_rows.append({"act_id": act_id, **row, "kind": "estimate_overrun"})

        if conflicts:
            background = pto_summary.get("discrepancies") if (
                act.get("background") or act_id == discrepancies_act) else None
            impact = assess_impact(conflicts, pto_summary, discrepancies=background,
                                   act_amount=amount)
            t = impact["totals"]
            record.update({
                "status": STATUS_CHECKED,
                "at_risk_rub": t["at_risk_rub"],
                "background_risk_rub": t["background_risk_rub"],
                "markup_rub": t["markup_rub"],
                "underclaim_rub": t["underclaim_rub"],
                "share_pct": (round(t["at_risk_rub"] / amount * 100, 2) if amount else None),
                "positions_priced": t["positions_priced"],
                "items_unpriced": t["items_unpriced"],
                "sheets_total": t["sheets_total"],
                "occurrences_total": t["occurrences_total"],
            })
            _merge_positions(merged_positions, impact, act_id)
            _merge_unpriced(merged_unpriced, impact, act_id)
        per_act.append(record)

    months: dict[str, dict] = {}
    for rec in per_act:
        m = months.setdefault(rec["month"], {
            "month": rec["month"], "label": rec["month_label"],
            "amount_rub": 0.0, "at_risk_rub": 0.0, "background_risk_rub": 0.0,
            "acts": [], "acts_checked": 0, "acts_unchecked": 0,
        })
        m["amount_rub"] += rec["amount_rub"]
        m["at_risk_rub"] += rec["at_risk_rub"]
        m["background_risk_rub"] += rec["background_risk_rub"]
        m["acts"].append(rec["act_id"])
        if rec["status"] == STATUS_CHECKED:
            m["acts_checked"] += 1
        else:
            m["acts_unchecked"] += 1
    for m in months.values():
        # месяц без проверенных актов — это «не оценено», а не «0 ₽ риска»
        m["risk_assessed"] = m["acts_checked"] > 0
        m["share_pct"] = (round(m["at_risk_rub"] / m["amount_rub"] * 100, 2)
                          if m["amount_rub"] and m["risk_assessed"] else None)
        for key in ("amount_rub", "at_risk_rub", "background_risk_rub"):
            m[key] = round(m[key], 2)
    months_list = sorted(months.values(), key=lambda m: m["month"])

    amount_total = sum(r["amount_rub"] for r in per_act)
    checked = [r for r in per_act if r["status"] == STATUS_CHECKED]
    amount_checked = sum(r["amount_rub"] for r in checked)
    at_risk = sum(r["at_risk_rub"] for r in checked)
    background = sum(r["background_risk_rub"] for r in checked)
    markup = sum(r["markup_rub"] for r in checked)
    underclaim = sum(r["underclaim_rub"] for r in checked)
    estimate_overrun = sum(r["estimate_overrun_rub"] for r in per_act)

    positions = sorted(merged_positions.values(),
                       key=lambda p: (-p["money_release_rub"], -p["money_background_rub"]))
    unpriced = sorted(merged_unpriced.values(), key=lambda u: -u["occurrences"])

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "object": acts_data.get("object") or pto_summary.get("object") or "—",
        "contract": acts_data.get("contract"),
        "totals": {
            "acts_total": len(per_act),
            "acts_checked": len(checked),
            "acts_unchecked": len(per_act) - len(checked),
            "amount_rub": round(amount_total, 2),
            "amount_checked_rub": round(amount_checked, 2),
            "amount_unchecked_rub": round(amount_total - amount_checked, 2),
            "coverage_pct": (round(amount_checked / amount_total * 100, 2)
                             if amount_total else None),
            "at_risk_rub": round(at_risk, 2),
            "background_risk_rub": round(background, 2),
            "markup_rub": round(markup, 2),
            "underclaim_rub": round(underclaim, 2),
            "estimate_overrun_rub": round(estimate_overrun, 2),
            "share_of_checked_pct": (round(at_risk / amount_checked * 100, 2)
                                     if amount_checked else None),
            "share_of_all_pct": (round(at_risk / amount_total * 100, 2)
                                 if amount_total else None),
            "positions_priced": len(positions),
            "items_unpriced": len(unpriced),
            "occurrences_total": sum(r["occurrences_total"] for r in per_act),
            "sheets_total": sum(r["sheets_total"] for r in per_act),
        },
        "acts": per_act,
        "months": months_list,
        "positions": positions,
        "unpriced": unpriced,
        "checks": coverage_rows,
        "method": [
            "Акт считается проверенным, если по нему распознан выпуск и пометки "
            "сверены с актом и сметой; иначе акт попадает в «не проверено» — "
            "это не ноль риска, а отсутствие проверки.",
            "Деньги под риском по акту = сумма споров по позициям (см. модуль impact): "
            "объём спора × цена сметы, один спор — один раз.",
            "Одна и та же позиция в разных актах — разные споры за разные периоды, "
            "суммы складываются; повторы внутри акта не складываются.",
            "Акты без распознанного выпуска проверяются по документам: количество "
            "в акте против сметного объёма; превышение × цена — тоже деньги под риском.",
            "Доля под риском считается от проверенной суммы актов; покрытие "
            "(сколько предъявленного проверено) показывается отдельно.",
        ],
    }


def _merge_positions(target: dict, impact: dict, act_id: str) -> None:
    for pos in impact["positions"]:
        code = pos["code"] or "—"
        row = target.setdefault(code, {
            "code": pos["code"], "name": pos["name"], "unit": pos.get("unit"),
            "price": pos.get("price"), "money_release_rub": 0.0,
            "money_background_rub": 0.0, "money_rub": 0.0,
            "risks": [], "sheets": [], "acts": [], "occurrences": 0,
        })
        row["money_release_rub"] += pos.get("money_release_rub") or 0
        row["money_background_rub"] += pos.get("money_background_rub") or 0
        row["money_rub"] += pos.get("money_rub") or 0
        row["occurrences"] += pos.get("occurrences") or 0
        if act_id not in row["acts"]:
            row["acts"].append(act_id)
        for sheet in pos.get("sheets") or []:
            if sheet not in row["sheets"]:
                row["sheets"].append(sheet)
        for risk in pos["risks"]:
            same = next((r for r in row["risks"] if r["risk"] == risk["risk"]
                         and r["formula"] == risk["formula"] and r["act_id"] == act_id), None)
            if same:
                same["money_rub"] += risk["money_rub"]
                same["occurrences"] += risk["occurrences"]
            else:
                row["risks"].append({**risk, "act_id": act_id})
    for row in target.values():
        for key in ("money_release_rub", "money_background_rub", "money_rub"):
            row[key] = round(row[key], 2)


def _merge_unpriced(target: dict, impact: dict, act_id: str) -> None:
    for item in impact.get("unpriced") or []:
        key = (item.get("code"), item.get("conflict_type"), item.get("note"))
        row = target.setdefault(key, {
            "code": item.get("code"), "conflict_type": item.get("conflict_type"),
            "note": item.get("note"), "risk": item.get("risk"),
            "occurrences": 0, "acts": [],
        })
        row["occurrences"] += item.get("occurrences") or 1
        if act_id not in row["acts"]:
            row["acts"].append(act_id)


# ── отчёты ────────────────────────────────────────────────────────────────
def portfolio_to_markdown(pf: dict) -> str:
    t = pf["totals"]
    lines = [
        f"# Деньги под риском по актам объекта",
        "",
        f"Объект: **{pf['object']}**"
        + (f" · договор {pf['contract']}" if pf.get("contract") else ""),
        "",
        f"- Актов в реестре: **{t['acts_total']}** на {_rub(t['amount_rub'])}",
        f"- Проверено выпуском: **{t['acts_checked']}** на {_rub(t['amount_checked_rub'])} "
        f"— покрытие {_num(t['coverage_pct'], 1)} %",
        f"- **Деньги под риском: {_rub(t['at_risk_rub'])}** "
        f"({_num(t['share_of_checked_pct'], 2)} % проверенной суммы, "
        f"{_num(t['share_of_all_pct'], 2)} % всех актов)",
        f"- Фон ПТО (расхождения объекта, вне выпуска): {_rub(t['background_risk_rub'])}",
        (f"- **Не проверено: {t['acts_unchecked']}** "
         f"{'акт' if t['acts_unchecked'] == 1 else 'акта'} на "
         f"{_rub(t['amount_unchecked_rub'])}" if t["acts_unchecked"]
         else "- Непроверенных актов нет: покрытие полное"),
        "",
        "| Акт | Период | Сумма акта | Под риском | Доля | Статус |",
        "|---|---|---|---|---|---|",
    ]
    for act in pf["acts"]:
        risk = _rub(act["at_risk_rub"]) if act["status"] == STATUS_CHECKED else "не оценено"
        share = (f"{_num(act['share_pct'], 2)} %" if act.get("share_pct") is not None else "—")
        lines.append(f"| {act['act_id']} | {act['period']} | {_rub(act['amount_rub'])} | "
                     f"{risk} | {share} | {act['status']} |")
    lines += ["", "| Месяц | Актов | Сумма | Под риском | Доля |",
              "|---|---|---|---|---|"]
    for m in pf["months"]:
        share = f"{_num(m['share_pct'], 2)} %" if m.get("share_pct") is not None else "—"
        risk = _rub(m["at_risk_rub"]) if m.get("risk_assessed", True) else "не оценено"
        lines.append(f"| {m['label']} | {len(m['acts'])} | {_rub(m['amount_rub'])} | "
                     f"{risk} | {share} |")
    if pf["positions"]:
        lines += ["", "## Позиции под риском", "",
                  "| Позиция | Наименование | По выпуску | Фон ПТО | Акты |",
                  "|---|---|---|---|---|"]
        for p in pf["positions"]:
            release = _rub(p["money_release_rub"]) if p["money_release_rub"] else "—"
            background = _rub(p["money_background_rub"]) if p["money_background_rub"] else "—"
            lines.append(f"| {p['code'] or '—'} | {p['name'][:40]} | {release} | "
                         f"{background} | {', '.join(p['acts'])} |")
    if pf["unpriced"]:
        lines += ["", "## Не оценено в деньгах", ""]
        for u in pf["unpriced"][:8]:
            lines.append(f"- {u.get('code') or '—'} ({u.get('conflict_type')}): "
                         f"{u.get('note')} · акты: {', '.join(u['acts'])}")
    unchecked = [a for a in pf["acts"] if a["status"] != STATUS_CHECKED]
    if unchecked:
        lines += ["", "## Что не проверено — и почему это не «0 ₽»", ""]
        for a in unchecked:
            lines.append(f"- **{a['act_id']}** ({a['period']}, {_rub(a['amount_rub'])}): "
                         f"{a['note']}")
    return "\n".join(lines)


def write_portfolio_xlsx(pf: dict, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill("solid", fgColor="7B2D26")
    head_font = Font(color="FFFFFF", bold=True)

    def add(wb, title, header, rows, widths):
        ws = wb.create_sheet(title)
        ws.append(header)
        for c in range(1, len(header) + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill, cell.font = head_fill, head_font
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
        for r in rows:
            ws.append([_scalar(v) for v in r])
        ws.freeze_panes = "A2"
        return ws

    t = pf["totals"]
    wb = Workbook()
    wb.remove(wb.active)
    add(wb, "Портфель — итог", ["Показатель", "Значение"], [
        ["Объект", pf["object"]],
        ["Договор", pf.get("contract") or "—"],
        ["Сформировано", pf["generated_at"]],
        ["Актов в реестре", t["acts_total"]],
        ["Сумма актов, ₽", t["amount_rub"]],
        ["Проверено выпуском, актов", t["acts_checked"]],
        ["Проверено выпуском, ₽", t["amount_checked_rub"]],
        ["Покрытие проверкой, %", t["coverage_pct"]],
        ["Под риском, ₽", t["at_risk_rub"]],
        ["  в том числе завышение, ₽", t["markup_rub"]],
        ["  в том числе недополучено, ₽", t["underclaim_rub"]],
        ["Доля от проверенного, %", t["share_of_checked_pct"]],
        ["Доля от всех актов, %", t["share_of_all_pct"]],
        ["Фон ПТО (вне выпуска), ₽", t["background_risk_rub"]],
        ["Превышение сметы по количествам, ₽", t["estimate_overrun_rub"]],
        ["Не проверено актов", t["acts_unchecked"]],
        ["Не проверено, ₽", t["amount_unchecked_rub"]],
        ["Позиций под риском", t["positions_priced"]],
        ["Строк не оценено", t["items_unpriced"]],
    ], [46, 40])
    add(wb, "Акты", ["Акт", "Месяц", "Период", "Сумма акта, ₽", "Под риском, ₽",
                     "Доля, %", "Статус", "Листов", "Позиций с ценой", "Комментарий"], [
        [a["act_id"], a["month_label"], a["period"], a["amount_rub"],
         a["at_risk_rub"] if a["status"] == STATUS_CHECKED else None,
         a["share_pct"], a["status"], a["sheets_total"], a["positions_priced"],
         a["note"]]
        for a in pf["acts"]], [14, 16, 16, 18, 16, 10, 16, 9, 12, 60])
    add(wb, "Месяцы", ["Месяц", "Актов", "Проверено", "Не проверено", "Сумма, ₽",
                       "Под риском, ₽", "Доля, %", "Фон ПТО, ₽"], [
        [m["label"], len(m["acts"]), m["acts_checked"], m["acts_unchecked"],
         m["amount_rub"], (m["at_risk_rub"] if m.get("risk_assessed", True) else None),
         m["share_pct"], m["background_risk_rub"]]
        for m in pf["months"]], [16, 8, 11, 13, 18, 16, 10, 16])
    add(wb, "Позиции по актам", ["Позиция", "Наименование", "Ед.", "Цена, ₽",
                                 "По выпуску, ₽", "Фон ПТО, ₽", "Акты", "Виды риска"], [
        [p["code"] or "—", p["name"], p["unit"], p["price"], p["money_release_rub"],
         p["money_background_rub"], ", ".join(p["acts"]),
         "; ".join(dict.fromkeys(r["risk_title"] for r in p["risks"]))]
        for p in pf["positions"]], [20, 46, 8, 14, 16, 14, 20, 52])
    add(wb, "Расчёт по актам", ["Акт", "Позиция", "Риск", "Формула", "Сумма, ₽", "Повторов"], [
        [r.get("act_id"), p["code"], RISK_TITLES.get(r["risk"], r["risk"]),
         r.get("formula") or "", r["money_rub"], r.get("occurrences")]
        for p in pf["positions"] for r in p["risks"]], [14, 20, 40, 42, 15, 10])
    add(wb, "Не проверено", ["Акт", "Период", "Сумма, ₽", "Количество позиций", "Причина"], [
        [a["act_id"], a["period"], a["amount_rub"], a["checked_rows"], a["note"]]
        for a in pf["acts"] if a["status"] != STATUS_CHECKED], [14, 16, 18, 18, 70])
    add(wb, "Не оценено", ["Позиция", "Тип", "Причина", "Повторов", "Акты"], [
        [u.get("code") or "—", u.get("conflict_type"), u.get("note"),
         u["occurrences"], ", ".join(u["acts"])] for u in pf["unpriced"]],
        [20, 22, 70, 10, 20])
    add(wb, "Методика", ["Правило"], [[m] for m in pf["method"]], [120])

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def portfolio_chart(pf: dict, path):
    """Полосы по месяцам: сколько предъявлено и сколько из этого под вопросом."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    months = pf["months"]
    if not months:
        return None
    labels = [m["label"] for m in months]
    amount = [m["amount_rub"] / 1e6 for m in months]
    risk = [m["at_risk_rub"] / 1e6 for m in months]

    fig, ax = plt.subplots(figsize=(9.5, 3.4))
    x = range(len(months))
    width = 0.38
    b1 = ax.bar([i - width / 2 for i in x], amount, width, color="#C3C9D4",
                label="сумма актов, млн ₽")
    b2 = ax.bar([i + width / 2 for i in x], risk, width, color="#7B2D26",
                label="под риском, млн ₽")
    for bars in (b1, b2):
        for bar in bars:
            h = bar.get_height()
            if h:
                ax.text(bar.get_x() + bar.get_width() / 2, h + 0.15,
                        f"{h:,.2f}".replace(",", " ").replace(".", ","),
                        ha="center", fontsize=9)
    for i, m in enumerate(months):
        if m["acts_unchecked"]:
            ax.annotate("часть актов не проверена", xy=(i - width / 2, amount[i]),
                        xytext=(0, 30), textcoords="offset points",
                        ha="center", fontsize=8, color="#8A5A10",
                        arrowprops=dict(arrowstyle="-", color="#C9A227", lw=0.8))
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel("млн ₽")
    t = pf["totals"]
    ax.set_title(f"Под риском по актам: {_num(t['at_risk_rub'] / 1000)} тыс. ₽ "
                 f"из {_num(t['amount_checked_rub'] / 1000)} тыс. проверенных", fontsize=11)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=9, loc="upper right")
    fig.tight_layout()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def write_portfolio_reports(pf: dict, out_dir) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "portfolio.json").write_text(
        json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "portfolio.md").write_text(portfolio_to_markdown(pf), encoding="utf-8")
    xlsx = write_portfolio_xlsx(pf, out / "portfolio.xlsx")
    chart = portfolio_chart(pf, out / "portfolio_chart.png")
    return {"json": out / "portfolio.json", "markdown": out / "portfolio.md",
            "xlsx": xlsx, "chart": chart}
