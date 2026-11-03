# -*- coding: utf-8 -*-
"""Стоимость спора: пометки на чертежах → деньги по КС-2.

Зачем: сводка распознавания говорит, где чертёж и акт расходятся, но руководителю
нужен ответ в рублях — «сколько денег стоит этот спор». Модуль переводит конфликты
(пометки) и расхождения ПТО в суммы по ценам сметы/акта и группирует их по позициям.

Три правила, без которых цифры врут:

1. **Каждый рубль с формулой.** У каждой суммы есть `formula` (что на что умножено)
   и `source` (откуда взяты цена и объём). Нет данных — сумма не выдумывается,
   а попадает в список «не оценено» с причиной.
2. **Один спор — один раз.** Выпуск состоит из итераций одних и тех же листов: одна
   и та же пометка встречается на 10 сканах. Сумма считается по паре
   «лист комплекта + позиция + тип риска», а количество повторов показывается
   отдельно, чтобы не умножать спор на число итераций.
3. **Направление имеет значение.** Пометка «принять 1300» при акте 1240 м3 — это
   завышение (риск отказа заказчика), а обратная разница — недополученные деньги.
   Это разные строки отчёта, их нельзя складывать в одно «расхождение».
"""
from dataclasses import dataclass, field
from datetime import datetime

MARKUP_RISK = "MARKUP_RISK"            # пометка завышает объём против акта
UNDERCLAIM_RISK = "UNDERCLAIM_RISK"    # пометка занижает — деньги не предъявлены
SIGNATURE_RISK = "SIGNATURE_RISK"      # «согласовано» под критичным расхождением
DOCS_RISK = "DOCS_RISK"                # сумма без подтверждающих документов
OVERRUN_RISK = "OVERRUN_RISK"          # объём сверх ранее закрытого
PRICE_UNKNOWN = "PRICE_UNKNOWN"        # посчитать нельзя — нет цены/объёма

RISK_TITLES = {
    MARKUP_RISK: "Завышение против акта — риск отказа заказчика",
    UNDERCLAIM_RISK: "Недопредъявлено — деньги не получены",
    SIGNATURE_RISK: "Подпись «согласовано» под критичным расхождением",
    DOCS_RISK: "Предъявлено без подтверждающих документов",
    OVERRUN_RISK: "Объём сверх ранее закрытого",
    PRICE_UNKNOWN: "Не оценено в деньгах",
}


@dataclass
class PriceBook:
    """Цены и контекст из сводки ПТО (`out/summary.json`)."""

    lines: dict = field(default_factory=dict)
    discrepancies_by_code: dict = field(default_factory=dict)
    act_amount: float = 0.0
    blocked_amount: float = 0.0
    object_name: str = ""
    period: str = ""

    @classmethod
    def from_summary(cls, summary: dict | None) -> "PriceBook":
        if not summary:
            return cls()
        lines = {l["code"]: l for l in summary.get("lines") or [] if l.get("code")}
        by_code: dict = {}
        for d in summary.get("discrepancies") or []:
            if d.get("code"):
                by_code.setdefault(d["code"], []).append(d)
        return cls(lines=lines, discrepancies_by_code=by_code,
                   act_amount=float(summary.get("amount_act") or 0.0),
                   blocked_amount=float(summary.get("amount_blocked") or 0.0),
                   object_name=summary.get("object") or "",
                   period=summary.get("period") or "")

    def price(self, code: str | None) -> float | None:
        line = self.lines.get(code or "")
        if not line:
            return None
        price = line.get("price")
        return float(price) if price else None


# ── деньги по одному расхождению ПТО ─────────────────────────────────────
def discrepancy_money(discrepancy: dict, book: PriceBook) -> dict:
    """Сколько стоит одно расхождение ПТО. Возвращает сумму, риск и формулу."""
    code = discrepancy.get("code")
    dtype = discrepancy.get("type")
    values = discrepancy.get("values") or {}
    price = book.price(code)
    line = book.lines.get(code or "") or {}
    unit = _unit(line.get("unit"))

    def result(money, risk, formula, note):
        return {"code": code, "type": dtype, "money_rub": money, "risk": risk,
                "formula": formula, "note": note, "unit": unit, "price": price}

    if dtype == "VOLUME_OVER_ESTIMATE":
        delta = abs(float(values.get("delta") or 0))
        if price and delta:
            return result(delta * price, SIGNATURE_RISK,
                          f"{_num(delta)} {unit} × {_num(price)} ₽/{unit}",
                          "разница «факт против сметы»: столько могут не принять, "
                          "если объём не обоснован изменением сметы")
        return result(None, PRICE_UNKNOWN, "нет цены или объёма",
                      "сумма не определена: в сводке нет цены по позиции")

    if dtype == "ALREADY_CLOSED":
        excess = abs(float(values.get("excess") or 0))
        if price and excess:
            return result(excess * price, OVERRUN_RISK,
                          f"{_num(excess)} {unit} сверх закрытого × {_num(price)} ₽/{unit}",
                          "объём сверх ранее закрытого: повторное предъявление")
        return result(None, PRICE_UNKNOWN, "нет объёма превышения",
                      "сумма не определена")

    if dtype == "MISSING_BACKING_DOCS":
        amount = values.get("amount")
        if amount:
            return result(float(amount), DOCS_RISK, "сумма предъявления без АОСР/схемы",
                          "предъявлено без подтверждающих документов — риск отказа "
                          "по всему объёму позиции")
        return result(None, PRICE_UNKNOWN, "нет суммы в сводке",
                      "сумма не определена")

    if dtype == "UNIT_MISMATCH":
        return result(None, PRICE_UNKNOWN,
                      "единицы в смете и журнале не совпадают",
                      "сначала пересчёт единиц, потом деньги: "
                      f"смета {_unit(values.get('estimate_unit'))}, журнал "
                      f"{', '.join(_unit(u) for u in (values.get('journal_units') or []))}")

    if dtype == "NOT_IN_ESTIMATE":
        return result(None, PRICE_UNKNOWN, "работы нет в смете — цены нет",
                      f"{_num(values.get('qty'), 1 if float(values.get('qty') or 0) % 1 else 0)} "
                      f"{_unit(values.get('unit'))} вне сметы: сумму "
                      "нужно определять по дополнительному соглашению")

    if dtype == "DUPLICATE":
        return result(0.0, DOCS_RISK, "дубли уже исключены из акта",
                      "отдельной суммы нет: строки-дубли не попали в акт")

    amount = line.get("amount_claim")
    if amount:
        return result(float(amount), SIGNATURE_RISK, "сумма к предъявлению по позиции",
                      "тип расхождения без правила расчёта — взята сумма позиции")
    return result(None, PRICE_UNKNOWN, "нет правила расчёта",
                  f"для типа {dtype} нет правила перевода в деньги")


# ── деньги по конфликту «пометка ↔ акт» ──────────────────────────────────
def conflict_money(conflict: dict, book: PriceBook) -> dict:
    """Сколько стоит конфликт пометки с данными ПТО."""
    code = conflict.get("code")
    ctype = conflict.get("type")
    price = book.price(code)
    line = book.lines.get(code or "") or {}
    unit = _unit(line.get("unit") or conflict.get("unit"))

    if ctype == "CONFLICT_QUANTITY":
        mark_qty = conflict.get("mark_qty")
        act_qty = conflict.get("act_qty")
        if mark_qty is None or act_qty is None:
            mark_qty = _first_number(conflict.get("mark_value"))
            act_qty = _first_number(conflict.get("act_value"))
        if price and mark_qty is not None and act_qty is not None:
            delta = float(mark_qty) - float(act_qty)
            money = abs(delta) * price
            if delta > 0:
                return {"money_rub": money, "risk": MARKUP_RISK, "unit": unit,
                        "price": price, "delta_qty": delta,
                        "formula": f"({_num(mark_qty)} − {_num(act_qty)}) {unit} × "
                                   f"{_num(price)} ₽/{unit}",
                        "note": "пометка требует принять больше, чем в акте: "
                                "заказчик вправе отказать по разнице"}
            return {"money_rub": money, "risk": UNDERCLAIM_RISK, "unit": unit,
                    "price": price, "delta_qty": delta,
                    "formula": f"({_num(act_qty)} − {_num(mark_qty)}) {unit} × "
                               f"{_num(price)} ₽/{unit}",
                    "note": "пометка требует принять меньше, чем в акте: разница "
                            "останется не предъявленной"}
        return {"money_rub": None, "risk": PRICE_UNKNOWN, "unit": unit, "price": price,
                "formula": "нет цены или объёма", "note": "сумма не определена"}

    if ctype == "CONFLICT_APPROVAL":
        # под подписью «согласовано» лежит сумма критичных расхождений по позиции
        parts, total = [], 0.0
        for d in book.discrepancies_by_code.get(code or "", []):
            if d.get("severity") != "critical":
                continue
            money = discrepancy_money(d, book)
            if money["money_rub"]:
                total += money["money_rub"]
                parts.append(f"{d['type']}: {money['formula']}")
        if total:
            return {"money_rub": total, "risk": SIGNATURE_RISK, "unit": unit,
                    "price": price,
                    "formula": " + ".join(parts),
                    "note": "подпись «согласовано» стоит на позиции, где у ПТО "
                            "критичное расхождение: если подпись проведут, спор "
                            "перейдёт в подписанный документ"}
        amount = line.get("amount_claim")
        if amount:
            return {"money_rub": float(amount), "risk": SIGNATURE_RISK, "unit": unit,
                    "price": price, "formula": "сумма к предъявлению по позиции",
                    "note": "критичные расхождения по позиции не оценены в деньгах — "
                            "взята вся сумма к предъявлению как верхняя оценка"}
        return {"money_rub": None, "risk": PRICE_UNKNOWN, "unit": unit, "price": price,
                "formula": "нет цены или суммы", "note": "сумма не определена"}

    return {"money_rub": None, "risk": PRICE_UNKNOWN, "unit": unit, "price": price,
            "formula": "нет правила расчёта",
            "note": f"для типа {ctype} нет правила перевода в деньги"}


def _first_number(text) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    import re
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(text))
    return float(m.group(0).replace(",", ".")) if m else None


# ── сводка по выпуску ────────────────────────────────────────────────────
def assess_impact(conflicts: list, pto_summary: dict | None, *,
                  discrepancies: list | None = None,
                  act_amount: float | None = None) -> dict:
    """Перевести конфликты выпуска и расхождения ПТО в деньги по позициям.

    `conflicts` — список из сводки пакета (поля: type, code, sheet_id, drawing, …).
    Возвращает позиции (ранжированные по сумме), расчёт по каждой строке, список
    «не оценено» и итоги с долей от суммы акта.
    """
    book = PriceBook.from_summary(pto_summary)
    if act_amount is not None:          # сумма акта этого периода, а не всего объекта
        book.act_amount = float(act_amount)
    items = []

    for c in conflicts:
        money = conflict_money(c, book)
        items.append({
            "source": "пометка со скана",
            "sheet_id": c.get("sheet_id"), "drawing": c.get("drawing"),
            "mark_id": c.get("mark_id"), "code": c.get("code"),
            "conflict_type": c.get("type"),
            "mark_text": c.get("mark_text") or _quantity_text(c),
            "mark_value": c.get("mark_value"), "act_value": c.get("act_value"),
            **money,
        })

    for d in discrepancies or []:
        money = discrepancy_money(d, book)
        items.append({
            "source": "расхождение ПТО",
            "sheet_id": None, "drawing": None, "mark_id": None,
            "code": d.get("code"), "conflict_type": d.get("type"),
            "mark_text": d.get("name") or "",
            "severity": d.get("severity"),
            **money,
        })

    priced = [i for i in items if i.get("money_rub") is not None]
    # «не оценено» тоже группируем: иначе одна и та же причина повторяется 20 раз
    unpriced_groups: dict = {}
    for i in items:
        if i.get("money_rub") is not None:
            continue
        key = (i.get("code"), i.get("conflict_type"), i.get("note"), i.get("formula"))
        g = unpriced_groups.setdefault(key, {
            "source": i.get("source"), "code": i.get("code"),
            "conflict_type": i.get("conflict_type"), "risk": i.get("risk"),
            "note": i.get("note"), "formula": i.get("formula"),
            "occurrences": 0, "sheets": [],
        })
        g["occurrences"] += 1
        if i.get("sheet_id") and i["sheet_id"] not in g["sheets"]:
            g["sheets"].append(i["sheet_id"])
    unpriced = list(unpriced_groups.values())

    # один спор — один раз: ключ — позиция + вид риска + формула.
    # Итерации одного листа и разные листы комплекта описывают один и тот же спор
    # по одной позиции; иначе сумма умножается на число сканов.
    groups: dict = {}
    for i in priced:
        if not i.get("money_rub"):
            continue
        key = (i.get("code"), i.get("risk"), i.get("formula"))
        g = groups.setdefault(key, {
            "drawing": i.get("drawing"), "code": i.get("code"), "risk": i.get("risk"),
            "risk_title": RISK_TITLES[i["risk"]],
            "money_rub": i["money_rub"], "formula": i.get("formula"),
            "note": i.get("note"), "unit": i.get("unit"), "price": i.get("price"),
            "sheets": [], "marks": [], "occurrences": 0, "source": i.get("source"),
        })
        g["occurrences"] += 1
        if i.get("sheet_id") and i["sheet_id"] not in g["sheets"]:
            g["sheets"].append(i["sheet_id"])
        if i.get("mark_id"):
            g["marks"].append({"sheet_id": i.get("sheet_id"), "mark_id": i["mark_id"]})

    positions: dict = {}
    for g in groups.values():
        pos = positions.setdefault(g["code"], {
            "code": g["code"],
            "name": (book.lines.get(g["code"] or "") or {}).get("name", ""),
            "unit": g.get("unit") or "", "price": g.get("price"),
            "money_rub": 0.0, "money_release_rub": 0.0, "money_background_rub": 0.0,
            "risks": [], "sheets": [], "occurrences": 0,
            "source": g.get("source"),
        })
        pos["money_rub"] += g["money_rub"]
        if g.get("source") == "пометка со скана":
            pos["money_release_rub"] += g["money_rub"]
        else:
            pos["money_background_rub"] += g["money_rub"]
        same = next((r for r in pos["risks"]
                     if r["risk"] == g["risk"] and r["formula"] == g["formula"]), None)
        if same:                       # один и тот же расчёт пришёл из двух источников
            same["occurrences"] += g["occurrences"]
            same["sources"] = sorted(set(same["sources"]) | {g.get("source")})
        else:
            pos["risks"].append({"risk": g["risk"], "risk_title": g["risk_title"],
                                 "money_rub": g["money_rub"], "formula": g["formula"],
                                 "note": g["note"], "occurrences": g["occurrences"],
                                 "sheets": g["sheets"], "sources": [g.get("source")]})
        pos["occurrences"] += g["occurrences"]
        for s in g["sheets"]:
            if s not in pos["sheets"]:
                pos["sheets"].append(s)

    # сначала то, что принесли пометки выпуска, потом фон ПТО
    positions_list = sorted(
        positions.values(),
        key=lambda p: (0 if p.get("source") == "пометка со скана" else 1, -p["money_rub"]))
    # суммы по пометкам выпуска и по уже известным расхождениям ПТО не смешиваем:
    # руководителю нужно понимать, что добавил именно этот выпуск сканов
    release_groups = [g for g in groups.values() if g["source"] == "пометка со скана"]
    background_groups = [g for g in groups.values() if g["source"] != "пометка со скана"]
    at_risk = sum(g["money_rub"] for g in release_groups)
    background = sum(g["money_rub"] for g in background_groups)
    underclaim = sum(g["money_rub"] for g in release_groups
                     if g["risk"] == UNDERCLAIM_RISK)
    markup = sum(g["money_rub"] for g in release_groups if g["risk"] == MARKUP_RISK)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "object": book.object_name, "period": book.period,
        "totals": {
            "at_risk_rub": round(at_risk, 2),          # только по пометкам выпуска
            "background_risk_rub": round(background, 2),  # фон ПТО (расхождения объекта)
            "markup_rub": round(markup, 2),
            "underclaim_rub": round(underclaim, 2),
            "act_amount_rub": round(book.act_amount, 2),
            "blocked_amount_rub": round(book.blocked_amount, 2),
            "share_of_act_pct": (round(at_risk / book.act_amount * 100, 2)
                                 if book.act_amount else None),
            "positions_priced": len([p for p in positions_list if p["source"] == "пометка со скана"]),
            "positions_background": len([p for p in positions_list
                                         if p["source"] != "пометка со скана"]),
            "items_priced": len(groups),
            "rows_raw": len(items),
            "items_unpriced": len(unpriced),
            "items_unpriced_raw": len([i for i in items if i.get("money_rub") is None]),
            "occurrences_total": sum(g["occurrences"] for g in groups.values()),
            "sheets_total": len({s for g in groups.values() for s in g["sheets"]}),
        },
        "positions": positions_list,
        "items": sorted(priced, key=lambda i: -(i.get("money_rub") or 0)),
        "unpriced": unpriced,
        "method": [
            "Сумма по позиции = объём спора × цена сметы; объём берётся из пометки "
            "и акта (журнала), цена — из строки КС-2/сметы.",
            "Конфликт по объёму: разница «пометка минус акт». Больше — завышение "
            f"({MARKUP_RISK}), меньше — недополучение ({UNDERCLAIM_RISK}).",
            "«Согласовано» под критичным расхождением: сумма критичных расхождений "
            "по этой позиции (столько перейдёт в подписанный документ).",
            "Одинаковые пометки на итерациях одного листа считаются один раз; "
            "число повторов и листов показывается отдельно.",
            "Если нет цены или объёма — сумма не выдумывается, строка уходит "
            "в «не оценено в деньгах» с причиной.",
        ],
    }


def _quantity_text(conflict: dict) -> str:
    if conflict.get("mark_value") and conflict.get("act_value"):
        return f"{conflict['mark_value']} против {conflict['act_value']} в акте"
    return ""


# ── отчёты ───────────────────────────────────────────────────────────────
def impact_to_markdown(impact: dict) -> str:
    t = impact["totals"]
    lines = ["## Деньги: чего стоят споры", "",
             f"**Под риском по пометкам выпуска:** {_rub(t['at_risk_rub'])} "
             f"({_num(t['share_of_act_pct'], 2)} % от акта "
             f"{_rub(t['act_amount_rub'])})", "",
             f"Фон ПТО (расхождения объекта, к выпуску не относятся): "
             f"{_rub(t.get('background_risk_rub', 0))}", ""]
    if impact["positions"]:
        lines += ["| Позиция | Наименование | Риск | Сумма по выпуску | Фон ПТО | Листов |",
                  "|---|---|---|---|---|---|"]
        for p in impact["positions"]:
            risk = "; ".join(dict.fromkeys(r["risk_title"] for r in p["risks"]))
            release = _rub(p.get("money_release_rub")) if p.get("money_release_rub") else "—"
            background = (_rub(p.get("money_background_rub"))
                          if p.get("money_background_rub") else "—")
            lines.append(f"| {p['code'] or '—'} | {p['name'][:40]} | {risk} | "
                         f"{release} | {background} | {len(p['sheets']) or '—'} |")
        lines.append("")
    if impact["unpriced"]:
        lines += ["**Не оценено в деньгах:**", ""]
        for u in impact["unpriced"][:6]:
            times = f" · повторов {u['occurrences']}" if u.get("occurrences") else ""
            lines.append(f"- {u.get('code') or '—'} ({u.get('conflict_type')}): "
                         f"{u.get('note')}{times}")
        lines.append("")
    return "\n".join(lines)


def write_impact_xlsx(impact: dict, path) -> "Path":
    from pathlib import Path

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
            ws.append([_scalar(v) for v in r])       # списки/словари → строка
        ws.freeze_panes = "A2"
        return ws

    t = impact["totals"]
    wb = Workbook()
    wb.remove(wb.active)
    add(wb, "Деньги — итог", ["Показатель", "Значение"], [
        ["Объект", impact.get("object") or "—"],
        ["Период", impact.get("period") or "—"],
        ["Сформировано", impact["generated_at"]],
        ["Сумма акта, ₽", t["act_amount_rub"]],
        ["Заблокировано ранее, ₽", t["blocked_amount_rub"]],
        ["Под риском по выпуску, ₽", t["at_risk_rub"]],
        ["  в том числе завышение против акта, ₽", t["markup_rub"]],
        ["  в том числе недополучено, ₽", t["underclaim_rub"]],
        ["Доля от акта, %", t["share_of_act_pct"]],
        ["Позиций с ценой", t["positions_priced"]],
        ["Строк оценено", t["items_priced"]],
        ["Строк не оценено (нет цены)", t["items_unpriced"]],
        ["Пометок-повторов учтено", t["occurrences_total"]],
        ["Листов затронуто", t["sheets_total"]],
    ], [44, 40])
    add(wb, "Позиции под риском",
        ["Позиция", "Наименование", "Ед.", "Цена, ₽", "Листов", "Повторов",
         "Сумма, ₽", "Виды риска"],
        [[p["code"] or "—", p["name"], p["unit"], p["price"], len(p["sheets"]),
          p["occurrences"], p["money_rub"],
          "; ".join(r["risk_title"] for r in p["risks"])]
         for p in impact["positions"]],
        [20, 46, 8, 14, 9, 10, 16, 52])
    add(wb, "Расчёт по строкам",
        ["Источник", "Лист", "Пометка", "Позиция", "Тип", "Текст/значение",
         "Риск", "Формула", "Сумма, ₽", "Пояснение"],
        [[i["source"], i.get("sheet_id") or "—", i.get("mark_id") or "—",
          i.get("code") or "—", i.get("conflict_type"),
          i.get("mark_text") or "", RISK_TITLES.get(i["risk"], i["risk"]),
          i.get("formula") or "", i.get("money_rub"), i.get("note") or ""]
         for i in impact["items"]],
        [18, 16, 10, 20, 22, 34, 34, 42, 15, 60])
    add(wb, "Не оценено в деньгах",
        ["Источник", "Позиция", "Тип", "Риск", "Почему нет суммы", "Формула"],
        [[i["source"], i.get("code") or "—", i.get("conflict_type"),
          RISK_TITLES.get(i["risk"], i["risk"]), i.get("note") or "",
          i.get("formula") or ""] for i in impact["unpriced"]],
        [18, 22, 24, 34, 70, 40])
    add(wb, "Методика", ["Правило"], [[m] for m in impact["method"]], [120])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def impact_chart(impact: dict, path, top: int = 8, include_background: bool = True):
    """Диаграмма «где деньги»: споры выпуска и фон ПТО разными цветами.

    Сумма выпуска и фон ПТО — разные вещи: первое принесли пометки со сканов,
    второе было известно и раньше. На диаграмме они не сливаются в одно число.
    """
    from pathlib import Path

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    positions = [p for p in impact["positions"] if p["money_rub"]]
    if not include_background:
        positions = [p for p in positions if p.get("money_release_rub")]
    positions = positions[:top]
    if not positions:
        return None

    labels = [f"{p['code'] or 'без шифра'}\n{p['name'][:30]}" for p in positions][::-1]
    release = [p.get("money_release_rub", 0) / 1000.0 for p in positions][::-1]
    background = [p.get("money_background_rub", 0) / 1000.0 for p in positions][::-1]

    fig, ax = plt.subplots(figsize=(9.5, 1.1 + 0.66 * len(positions)))
    bars_r = ax.barh(labels, release, color="#7B2D26", label="пометки выпуска")
    bars_b = ax.barh(labels, background, left=release, color="#B9AFA8",
                     label="фон ПТО (расхождения объекта)")
    peak = max(release[i] + background[i] for i in range(len(release))) or 1.0
    for i, bar in enumerate(bars_r):
        value = release[i]
        if not value:
            continue
        text = f"{value:,.0f}".replace(",", " ")
        if value > peak * 0.22:            # подпись внутри сегмента, чтобы не наезжала
            ax.text(value / 2, bar.get_y() + bar.get_height() / 2, text,
                    va="center", ha="center", fontsize=9, color="white")
        else:
            ax.text(value + peak * 0.01, bar.get_y() + bar.get_height() / 2, text,
                    va="center", fontsize=9, color="#7B2D26")
    for i, bar in enumerate(bars_b):
        value = background[i]
        if value:
            ax.text(release[i] + value + peak * 0.01, bar.get_y() + bar.get_height() / 2,
                    f"{value:,.0f}".replace(",", " "), va="center", fontsize=9,
                    color="#6B625C")
    ax.set_xlabel("Сумма спора, тыс. ₽")
    t = impact["totals"]
    share = _num(t["share_of_act_pct"], 2)
    release_k = _num(t["at_risk_rub"] / 1000)
    background_k = _num(t.get("background_risk_rub", 0) / 1000)
    ax.set_title(f"Пометки выпуска: {release_k} тыс. ₽ ({share} % от акта) · "
                 f"фон ПТО: {background_k} тыс. ₽", fontsize=11)
    ax.spines[["top", "right"]].set_visible(False)
    if any(background):
        ax.legend(loc="lower right", frameon=False, fontsize=9)
    fig.tight_layout()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _unit(unit) -> str:
    """м3 → м³: единицы в отчётах руководителя должны выглядеть как в смете."""
    return (str(unit or "").replace("м2", "м²").replace("м3", "м³")
            .replace("m2", "м²").replace("m3", "м³"))


def _num(value, digits: int = 0) -> str:
    """Русский формат числа: пробел между тысячами, запятая в дробной части."""
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}"
    return text.replace(",", "\u00a0").replace(".", ",")


def _scalar(value):
    """Привести значение к тому, что Excel умеет хранить."""
    if isinstance(value, (list, tuple)):
        return "; ".join(_scalar(v) for v in value)
    if isinstance(value, dict):
        import json
        return json.dumps(value, ensure_ascii=False)
    return value if value is not None else "—"


def _rub(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} ₽".replace(",", " ")


__all__ = ["assess_impact", "conflict_money", "discrepancy_money", "PriceBook",
           "impact_to_markdown", "write_impact_xlsx", "impact_chart",
           "MARKUP_RISK", "UNDERCLAIM_RISK", "SIGNATURE_RISK", "DOCS_RISK",
           "OVERRUN_RISK", "PRICE_UNKNOWN", "RISK_TITLES"]
