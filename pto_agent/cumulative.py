# -*- coding: utf-8 -*-
"""Накопительная ведомость выполненных работ: нарастающим итогом по актам.

Зачем отдельный модуль, а не сумма актов из портфеля:

* **портфель отвечает «сколько денег под риском»**, а не «сколько предъявлено нарастающим
  итогом по позиции». Инженеру нужна ведомость по позициям: код, наименование, единица,
  количество и сумма по периодам, итог с начала работ, остаток к смете;
* **сумма акта и сумма позиций — разные числа.** В акте КС-2 стоит итог, а построчно он
  может не сходиться (округления, забытая строка). Показываем оба и расхождение, а не
  выбираем «удобное»;
* **двойной счёт прячется в деталях:** один и тот же акт внесён дважды, одна позиция попала
  в два акта одного периода, один код идёт в м² и в м³. Всё это проверки, а не молчаливая
  склейка;
* **позиция вне сметы — не ошибка, а кандидат в АТН** на дополнительные работы. Ведомость
  помечает такие строки и считает разницу, но сама ничего не предъявляет.

Числа разбираются `numeric.parse_number`: `nan` и мусор не становятся нулём, а попадают в
`checks` с уровнем «error», и позиция не входит в итог.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .numeric import parse_number

SCHEMA = "pto-agent/cumulative@1"
TOL = 0.01

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

CHECK_DUPLICATE_ACT = "DUPLICATE_ACT"
CHECK_ACT_SUM = "ACT_SUM_MISMATCH"
CHECK_NO_POSITIONS = "NO_POSITIONS"
CHECK_BAD_NUMBER = "BAD_NUMBER"
CHECK_NEGATIVE = "NEGATIVE_QTY"
CHECK_UNIT = "UNIT_MISMATCH"
CHECK_PERIOD_DOUBLE = "SAME_PERIOD_DOUBLE"
CHECK_NOT_IN_ESTIMATE = "NOT_IN_ESTIMATE"
CHECK_OVER_ESTIMATE = "OVER_ESTIMATE"


def _rub(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")
    return text.rstrip("0").rstrip(",") if "," in text else text


def load_acts(path: str | Path) -> tuple[dict, list[str]]:
    """Прочитать реестр актов объекта (`acts.json`). Возвращает (данные, проблемы чтения)."""
    path = Path(path)
    problems: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {}, [f"{path.name}: не читается ({exc.__class__.__name__})"]
    if isinstance(data, list):
        data = {"acts": data}
    acts = data.get("acts")
    if not isinstance(acts, list):
        return {}, [f"{path.name}: нет списка актов (ожидался ключ «acts»)"]
    return data, problems


def load_estimate(path: str | Path | None) -> dict[str, dict]:
    """Смета как справочник цен и объёмов: код → {name, unit, qty, price}."""
    if not path:
        return {}
    from .parsers import read_estimate_ex

    items, problems = read_estimate_ex(Path(path))
    book: dict[str, dict] = {}
    for item in items:
        book[item.code] = {"name": item.name, "unit": item.unit, "qty": item.qty,
                           "price": item.price}
    for problem in problems:
        book.setdefault("_problems", []).append(problem)
    return book


def build_ledger(acts_data: dict, *, estimate: dict[str, dict] | None = None,
                 today: date | None = None) -> dict:
    """Собрать накопительную ведомость по позициям с проверками.

    Периоды подписываются как есть из актов (`period`, иначе `month`); если нет ничего —
    «период не указан», и это видно в ведомости, а не растворяется в итоге.
    """
    today = today or date.today()
    estimate = estimate or {}
    checks: list[dict] = []
    acts = acts_data.get("acts") or []

    seen_acts: dict[str, str] = {}
    code_units: dict[str, set] = {}
    periods: list[str] = []
    rows: dict[tuple[str, str], dict] = {}
    act_sums: list[dict] = []
    counterparties: dict[str, float] = {}

    def add_check(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    for act in acts:
        act_id = str(act.get("act_id") or act.get("number") or "").strip() or "акт без номера"
        period = str(act.get("period") or act.get("month") or "").strip() or "период не указан"
        party = str(act.get("counterparty") or act.get("contractor") or "").strip()

        if act_id in seen_acts:
            add_check(CHECK_DUPLICATE_ACT, LEVEL_ERROR,
                      f"{act_id}: акт уже учтён ({seen_acts[act_id]}), второй раз не суммируется",
                      act_id=act_id)
            continue
        seen_acts[act_id] = period
        if period not in periods:
            periods.append(period)

        claimed_sum = parse_number(act.get("amount_rub") if act.get("amount_rub") is not None
                                   else act.get("amount"))
        positions = act.get("positions") or []
        act_positions_sum = 0.0
        counted = 0

        if not positions:
            add_check(CHECK_NO_POSITIONS, LEVEL_WARNING,
                      f"{act_id}: сумма {_rub(claimed_sum)} ₽ без построчного подтверждения — "
                      f"позиций в акте нет, сверить нечем", act_id=act_id,
                      amount_rub=claimed_sum)
        for position in positions:
            code = str(position.get("code") or "").strip()
            unit = str(position.get("unit") or "").strip()
            name = str(position.get("name") or "").strip()
            qty = parse_number(position.get("qty"))
            price = parse_number(position.get("price"))
            amount = parse_number(position.get("amount_rub"))
            if amount is None and qty is not None and price is not None:
                amount = round(qty * price, 2)

            if qty is None or amount is None:
                add_check(CHECK_BAD_NUMBER, LEVEL_ERROR,
                          f"{act_id} · {code or 'код не указан'}: количество или сумма не число "
                          f"(«{position.get('qty')}» / «{position.get('amount_rub')}») — "
                          f"позиция не входит в итог", act_id=act_id, item_code=code or None)
                continue
            if qty < 0:
                add_check(CHECK_NEGATIVE, LEVEL_WARNING,
                          f"{act_id} · {code}: отрицательное количество {_num(qty)} — "
                          f"учтено как корректировка (сторно)", act_id=act_id, item_code=code)

            # одна и та же позиция в разных единицах — не складываем, а говорим прямо
            if code and unit:
                known = code_units.setdefault(code, set())
                if known and unit not in known:
                    add_check(CHECK_UNIT, LEVEL_ERROR,
                              f"{code}: единица «{unit}» не совпадает с «{', '.join(sorted(known))}»"
                              f" в других актах — строки разделены, суммы не складывались",
                              act_id=act_id, item_code=code)
                known.add(unit)

            key = (code or f"без кода: {name or 'наименование не указано'}", unit)
            row = rows.get(key)
            if row is None:
                row = {"code": key[0], "unit": unit or "ед. не указана",
                       "name": name, "by_period": {}, "qty_total": 0.0, "amount_total": 0.0,
                       "acts": [], "estimate_qty": None, "estimate_price": None}
                rows[key] = row
            else:
                if name and not row["name"]:
                    row["name"] = name

            slot = row["by_period"].setdefault(period, {"qty": 0.0, "amount_rub": 0.0,
                                                        "acts": []})
            if slot["acts"] and act_id not in slot["acts"]:
                add_check(CHECK_PERIOD_DOUBLE, LEVEL_WARNING,
                          f"{code} · {period}: позиция уже предъявлена в акте "
                          f"{', '.join(slot['acts'])}, сейчас ещё в {act_id} — возможен двойной "
                          f"счёт", act_id=act_id, item_code=code, period=period)
            slot["qty"] += qty
            slot["amount_rub"] += amount
            if act_id not in slot["acts"]:
                slot["acts"].append(act_id)

            row["qty_total"] += qty
            row["amount_total"] += amount
            if act_id not in row["acts"]:
                row["acts"].append(act_id)
            act_positions_sum += amount
            counted += 1

        act_sums.append({"act_id": act_id, "period": period, "counterparty": party or None,
                         "amount_rub": claimed_sum, "positions_sum": round(act_positions_sum, 2),
                         "positions": counted})
        if party:
            counterparties[party] = counterparties.get(party, 0.0) + (claimed_sum or 0.0)

        if claimed_sum is None:
            add_check(CHECK_ACT_SUM, LEVEL_ERROR,
                      f"{act_id}: сумма акта не указана — сверить итог нечем", act_id=act_id)
        elif positions and abs(claimed_sum - act_positions_sum) > TOL:
            add_check(CHECK_ACT_SUM, LEVEL_ERROR,
                      f"{act_id}: сумма акта {_rub(claimed_sum)} ₽ не равна сумме позиций "
                      f"{_rub(act_positions_sum)} ₽ (расхождение "
                      f"{_rub(claimed_sum - act_positions_sum)} ₽)", act_id=act_id,
                      amount_rub=claimed_sum, positions_sum=round(act_positions_sum, 2))

    # сверка со сметой: остаток, позиции вне сметы, превышение
    for (code, unit), row in rows.items():
        book = estimate.get(code)
        if code and book:
            row["estimate_qty"] = book.get("qty")
            row["estimate_price"] = book.get("price")
            row["estimate_amount"] = round((book.get("qty") or 0) * (book.get("price") or 0), 2)
            if not row["name"]:
                row["name"] = book.get("name") or ""
            estimate_qty = book.get("qty")
            if estimate_qty is not None:
                row["remainder_qty"] = round(estimate_qty - row["qty_total"], 3)
                if row["qty_total"] - estimate_qty > TOL:
                    add_check(CHECK_OVER_ESTIMATE, LEVEL_WARNING,
                              f"{code}: предъявлено {_num(row['qty_total'])} {unit} при смете "
                              f"{_num(estimate_qty)} — превышение {_num(row['qty_total'] - estimate_qty)} "
                              f"(кандидат в АТН или ошибка в акте)", item_code=code)
        else:
            row["source"] = "вне сметы"
            add_check(CHECK_NOT_IN_ESTIMATE, LEVEL_WARNING,
                      f"{code}: позиции нет в смете — кандидат в АТН на дополнительные работы",
                      item_code=code)
        row["by_period"] = {p: {"qty": round(v["qty"], 3), "amount_rub": round(v["amount_rub"], 2),
                                "acts": v["acts"]}
                            for p, v in sorted(row["by_period"].items())}
        row["qty_total"] = round(row["qty_total"], 3)
        row["amount_total"] = round(row["amount_total"], 2)

    amount_acts = round(sum(a["amount_rub"] or 0 for a in act_sums), 2)
    amount_positions = round(sum(r["amount_total"] for r in rows.values()), 2)
    errors = [c for c in checks if c["level"] == LEVEL_ERROR]
    return {
        "schema": SCHEMA,
        "built_at": today.isoformat(),
        "object": acts_data.get("object") or "",
        "contract": acts_data.get("contract") or "",
        "periods": periods,
        "acts": act_sums,
        "rows": sorted(rows.values(), key=lambda r: (-r["amount_total"], r["code"])),
        "by_counterparty": [{"counterparty": k, "amount_rub": round(v, 2)}
                            for k, v in sorted(counterparties.items())] or
                           ([{"counterparty": "контрагент не указан", "amount_rub": amount_acts}]
                            if act_sums else []),
        "totals": {
            "acts": len(act_sums),
            "periods": len(periods),
            "rows": len(rows),
            "amount_acts_rub": amount_acts,
            "amount_positions_rub": amount_positions,
            "difference_rub": round(amount_acts - amount_positions, 2),
            "positions_without_estimate": len([r for r in rows.values()
                                               if r.get("source") == "вне сметы"]),
            "over_estimate": len([c for c in checks if c["code"] == CHECK_OVER_ESTIMATE]),
            "errors": len(errors),
            "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING]),
        },
        "checks": checks,
        "disclaimer": ("Ведомость показывает предъявленное и сверяет его со сметой; она не "
                       "подтверждает объёмы. Выполнение подтверждает человек, а расхождения "
                       "объёмов идут в АТН только с основанием."),
    }


def to_markdown(ledger: dict) -> str:
    totals = ledger.get("totals") or {}
    lines = ["# Накопительная ведомость выполненных работ", "",
             f"**Объект:** {ledger.get('object') or 'не указан'} · "
             f"**договор:** {ledger.get('contract') or 'не указан'} · "
             f"**собрана:** {ledger.get('built_at')}", "",
             f"Актов: {totals.get('acts')} · периодов: {totals.get('periods')} · позиций: "
             f"{totals.get('rows')} · **сумма актов:** {_rub(totals.get('amount_acts_rub'))} ₽ · "
             f"**сумма по позициям:** {_rub(totals.get('amount_positions_rub'))} ₽ · "
             f"расхождение: {_rub(totals.get('difference_rub'))} ₽", ""]

    errors = [c for c in ledger.get("checks") or [] if c["level"] == LEVEL_ERROR]
    warnings = [c for c in ledger.get("checks") or [] if c["level"] == LEVEL_WARNING]
    if errors:
        lines += ["## Ошибки: цифры не сходятся", ""]
        lines += [f"- **{c['code']}** — {c['message']}" for c in errors] + [""]
    if warnings:
        lines += ["## Требует решения", ""]
        lines += [f"- {c['code']} — {c['message']}" for c in warnings] + [""]

    periods = ledger.get("periods") or []
    header = "| Позиция | Ед. | " + " | ".join(periods) + " | Итого кол-во | Итого, ₽ | Смета |"
    divider = "|---|---|" + "---|" * len(periods) + "---|---|"
    lines += ["## Ведомость", "", header, divider] if periods else \
             ["## Ведомость", "", "| Позиция | Ед. | Итого кол-во | Итого, ₽ | Смета |",
              "|---|---|---|---|---|"]
    for row in ledger.get("rows") or []:
        name = row.get("name") or row.get("code") or "наименование не указано"
        cell = f"{row['code']} — {name}" if row.get("code") and row.get("code") != name else name
        est = (f"{_num(row.get('estimate_qty'))} / {_rub(row.get('estimate_amount'))} ₽"
               if row.get("estimate_qty") is not None else
               ("вне сметы" if row.get("source") == "вне сметы" else "—"))
        if periods:
            cells = []
            for period in periods:
                slot = (row.get("by_period") or {}).get(period)
                cells.append(f"{_num(slot['qty'])} / {_rub(slot['amount_rub'])}" if slot else "—")
            lines.append(f"| {cell} | {row['unit']} | " + " | ".join(cells) +
                         f" | {_num(row.get('qty_total'))} | {_rub(row.get('amount_total'))} | {est} |")
        else:
            lines.append(f"| {cell} | {row['unit']} | {_num(row.get('qty_total'))} | "
                         f"{_rub(row.get('amount_total'))} | {est} |")
    lines.append("")

    if len(ledger.get("by_counterparty") or []) > 1:
        lines += ["## По контрагентам", "", "| Контрагент | Предъявлено, ₽ |", "|---|---|"]
        lines += [f"| {c['counterparty']} | {_rub(c['amount_rub'])} |"
                  for c in ledger["by_counterparty"]] + [""]

    candidates = [r for r in ledger.get("rows") or []
                  if r.get("source") == "вне сметы" or (r.get("remainder_qty") or 0) < 0]
    if candidates:
        lines += ["## Кандидаты в АТН (сверх сметы или вне её)", ""]
        for row in candidates:
            tail = ("позиции нет в смете" if row.get("source") == "вне сметы"
                    else f"превышение {_num(-row['remainder_qty'])} {row['unit']}")
            lines.append(f"- {row['code']} — {row.get('name') or ''}: {tail}; "
                         f"предъявлено на {_rub(row.get('amount_total'))} ₽")
        lines.append("")
    lines += [f"> {ledger.get('disclaimer')}", ""]
    return "\n".join(lines)


def export_xlsx(ledger: dict, path: str | Path) -> Path:
    """Ведомость в XLSX: лист «Ведомость», «Акты», «Проверки»."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    periods = ledger.get("periods") or []

    sheet = wb.active
    sheet.title = "Ведомость"
    header = ["Код", "Наименование", "Ед."]
    for period in periods:
        header += [f"{period}: кол-во", f"{period}: сумма, ₽"]
    header += ["Итого кол-во", "Итого, ₽", "По смете, кол-во", "По смете, ₽", "Остаток, кол-во"]
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in ledger.get("rows") or []:
        line = [row.get("code"), row.get("name") or "", row.get("unit")]
        for period in periods:
            slot = (row.get("by_period") or {}).get(period)
            line += [slot["qty"] if slot else None, slot["amount_rub"] if slot else None]
        line += [row.get("qty_total"), row.get("amount_total"), row.get("estimate_qty"),
                 row.get("estimate_amount"), row.get("remainder_qty")]
        sheet.append(line)
    for column, width in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (26, 46, 8) + (14,) * 20):
        sheet.column_dimensions[column].width = width

    acts = wb.create_sheet("Акты")
    acts.append(["Акт", "Период", "Контрагент", "Сумма акта, ₽", "Сумма позиций, ₽",
                 "Позиций", "Расхождение, ₽"])
    for cell in acts[1]:
        cell.font = Font(bold=True)
    for act in ledger.get("acts") or []:
        acts.append([act["act_id"], act["period"], act.get("counterparty") or "не указан",
                     act.get("amount_rub"), act.get("positions_sum"), act.get("positions"),
                     round((act.get("amount_rub") or 0) - (act.get("positions_sum") or 0), 2)])
    for column, width in zip("ABCDEFG", (16, 16, 24, 18, 18, 10, 18)):
        acts.column_dimensions[column].width = width

    checks = wb.create_sheet("Проверки")
    checks.append(["Уровень", "Код", "Акт", "Сообщение"])
    for cell in checks[1]:
        cell.font = Font(bold=True)
    for check in ledger.get("checks") or []:
        checks.append([check["level"], check["code"], check.get("act_id") or "",
                       check["message"]])
    for column, width in zip("ABCD", (10, 22, 16, 110)):
        checks.column_dimensions[column].width = width
    wb.save(path)
    return path


def write(ledger: dict, out_dir: str | Path) -> dict:
    """Сохранить ведомость: JSON, Markdown, XLSX."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cumulative.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    (out / "cumulative.md").write_text(to_markdown(ledger), encoding="utf-8")
    export_xlsx(ledger, out / "cumulative.xlsx")
    return {"json": str(out / "cumulative.json"), "markdown": str(out / "cumulative.md"),
            "xlsx": str(out / "cumulative.xlsx")}
