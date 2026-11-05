# -*- coding: utf-8 -*-
"""Закупка материалов: заявка из сметы → сравнение КП → обоснованный выбор.

Правила, из которых собран модуль (иначе это «1С с ИИ-обёрткой»):

* **КП без обязательных реквизитов не участвует в сравнении.** Нет даты предложения, срока
  поставки или срока действия цены — предложение уходит в `not_comparable` с причиной.
  Сравнивать «цену без срока» с «ценой со сроком» — это не экономия, а будущий спор.
* **Цена сравнивается приведённая**: с НДС, если у поставщиков разные режимы; база сравнения
  указывается в отчёте, чтобы не было «у одного с НДС, у другого без».
* **Выбор делает человек**, агент готовит расчёт и обоснование. Поэтому `decide()` пишет
  решение в журнал аудита с автором, причиной и ссылкой на протокол сравнения.
* **Ни одна цифра не выдумывается**: если для позиции нет ни одного пригодного КП — это
  `no_quotes`, а не «оставим прошлую цену».

Заявка собирается из сметы (в т.ч. из выгрузки АРОС): материалы — это позиции с кодом ресурса,
работы в заявку не идут.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path

QUOTE_REQUIRED = ("supplier", "code", "price", "unit", "delivery_days", "valid_until",
                  "quote_date", "quote_no")
COLUMN_ALIASES = {
    "поставщик": "supplier", "supplier": "supplier", "контрагент": "supplier",
    "код": "code", "code": "code", "код ресурса": "code",
    "наименование": "name", "name": "name", "ресурс": "name",
    "цена": "price", "цена, руб.": "price", "price": "price",
    "ед": "unit", "ед. изм.": "unit", "ед.изм.": "unit", "unit": "unit",
    "ндс": "vat_rate", "ставка ндс": "vat_rate", "vat_rate": "vat_rate",
    "срок поставки": "delivery_days", "срок, дн": "delivery_days",
    "delivery_days": "delivery_days", "дней": "delivery_days",
    "действует до": "valid_until", "valid_until": "valid_until",
    "дата кп": "quote_date", "дата": "quote_date", "quote_date": "quote_date",
    "номер кп": "quote_no", "кп": "quote_no", "quote_no": "quote_no",
    "условия оплаты": "payment_terms", "payment_terms": "payment_terms",
    "отсрочка, дн": "payment_days", "payment_days": "payment_days",
    "файл": "document", "документ": "document", "document": "document",
}


def _num(value) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace("\xa0", " ").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _header(text: str) -> str:
    return COLUMN_ALIASES.get(str(text or "").strip().lower().replace("ё", "е"), "")


def _parse_date(value) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ─────────────────────────────── заявка на материалы ───────────────────────────────
def build_request(materials: list[dict], *, object_name: str = "", request_no: str = "",
                  request_date: str | None = None, source: str = "",
                  need_by: str = "") -> dict:
    """Заявка на материалы из позиций сметы (в перечень идут только материалы)."""
    # один код ресурса — одна позиция заявки: в смете кабель может стоять двумя строками
    # (разные участки), закупать его двумя лотами бессмысленно
    grouped: dict[str, dict] = {}
    for material in materials:
        if str(material.get("kind") or "material") != "material":
            continue
        code = str(material.get("code") or material.get("name") or "")
        slot = grouped.setdefault(code, {"code": str(material.get("code") or ""),
                                         "name": str(material.get("name") or ""),
                                         "unit": str(material.get("unit") or ""),
                                         "qty": 0.0, "lines": 0})
        try:
            slot["qty"] = round(slot["qty"] + float(material.get("qty") or 0), 4)
        except (TypeError, ValueError):
            pass
        slot["lines"] += 1
    items = []
    for slot in grouped.values():
        items.append({
            "code": slot["code"],
            "name": slot["name"],
            "unit": slot["unit"],
            "qty": slot["qty"] or None,
            "lines": slot["lines"],
            "needs_input": [f for f, ok in (("qty", slot["qty"] not in (None, 0)),
                                            ("unit", bool(slot["unit"]))) if not ok],
        })
    return {
        "schema": "pto-agent/procurement-request@1",
        "request_no": request_no or f"ЗМ-{date.today().isoformat()}",
        "request_date": request_date or date.today().isoformat(),
        "need_by": need_by,
        "object_name": object_name,
        "source": source,
        "items": items,
        "items_count": len(items),
        "note": ("объёмы взяты из сметы; материал без объёма помечен needs_input — "
                 "в закупку без объёма не уходит"),
    }


def request_from_estimate(smeta: str | Path, *, object_name: str = "", request_no: str = "",
                          need_by: str = "") -> dict:
    """Заявка прямо из сметы (xlsx или выгрузка АРОС)."""
    from .asbuilt.spec_from_estimate import load_positions
    positions, source = load_positions(smeta=smeta)
    return build_request(positions, object_name=object_name, request_no=request_no,
                         source=source, need_by=need_by)


# ─────────────────────────────── коммерческие предложения ───────────────────────────────
def load_quotes(path: str | Path) -> tuple[list[dict], list[str]]:
    """Прочитать КП из csv/xlsx → (предложения, проблемы чтения)."""
    path = Path(path)
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = [list(row) for row in ws.iter_rows(values_only=True)]
    elif path.suffix.lower() in {".csv", ".txt"}:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [row for row in csv.reader(handle)]
    else:
        raise ValueError(f"неизвестный формат КП: {path.suffix} (поддержаны xlsx и csv)")

    header_index, mapping = None, {}
    for i, row in enumerate(rows[:10]):
        candidate = {_header(cell): j for j, cell in enumerate(row) if _header(cell)}
        if {"supplier", "code", "price"} <= set(candidate):
            header_index, mapping = i, candidate
            break
    if header_index is None:
        return [], ["не найдены колонки поставщик/код/цена"]

    problems: list[str] = []
    quotes: list[dict] = []
    for row in rows[header_index + 1:]:
        if not row or all(cell in (None, "") for cell in row):
            continue

        def get(field, default=None):
            index = mapping.get(field)
            return row[index] if index is not None and index < len(row) else default

        quote = {
            "supplier": str(get("supplier") or "").strip(),
            "code": str(get("code") or "").strip(),
            "name": str(get("name") or "").strip(),
            "unit": str(get("unit") or "").strip(),
            "price": _num(get("price")),
            "vat_rate": _num(get("vat_rate")),
            "delivery_days": _num(get("delivery_days")),
            "payment_days": _num(get("payment_days")),
            "payment_terms": str(get("payment_terms") or "").strip(),
            "valid_until": str(get("valid_until") or "").strip(),
            "quote_date": str(get("quote_date") or "").strip(),
            "quote_no": str(get("quote_no") or "").strip(),
            "document": str(get("document") or path.name).strip(),
            "file": path.name,
        }
        if not quote["supplier"] or not quote["code"] or quote["price"] is None:
            problems.append(f"строка КП пропущена ({path.name}): поставщик="
                            f"{quote['supplier'] or '—'}, код={quote['code'] or '—'}, "
                            f"цена={quote['price']}")
            continue
        quotes.append(quote)
    return quotes, problems


def validate_quote(quote: dict, *, today: date | None = None) -> dict:
    """Что мешает сравнивать это КП. Возвращает {'comparable': bool, 'missing': [...], 'notes': [...]}."""
    today = today or date.today()
    missing = [field for field in QUOTE_REQUIRED if quote.get(field) in (None, "")]
    notes: list[str] = []
    comparable = True

    if missing:
        comparable = False
        labels = {"supplier": "поставщик", "code": "код ресурса", "price": "цена",
                  "unit": "единица измерения", "delivery_days": "срок поставки, дней",
                  "valid_until": "срок действия цены", "quote_date": "дата предложения",
                  "quote_no": "номер предложения"}
        notes.append("в КП нет обязательных реквизитов: " +
                     ", ".join(labels.get(m, m) for m in missing))

    valid_until = _parse_date(quote.get("valid_until"))
    if quote.get("valid_until") and valid_until and valid_until < today:
        comparable = False
        notes.append(f"срок действия цены истёк {valid_until.isoformat()}")

    quote_date = _parse_date(quote.get("quote_date"))
    if quote.get("quote_date") and quote_date is None:
        comparable = False
        notes.append(f"дата предложения не читается: {quote.get('quote_date')!r}")

    if quote.get("price") is not None and quote["price"] <= 0:
        comparable = False
        notes.append("цена не может быть нулевой или отрицательной")

    return {"comparable": comparable, "missing": missing, "notes": notes,
            "quote_date": quote_date.isoformat() if quote_date else None,
            "valid_until": valid_until.isoformat() if valid_until else None}


def _price_with_vat(quote: dict) -> tuple[float, str]:
    """Цена, приведённая к сравнению. Возвращает (значение, база сравнения)."""
    vat = quote.get("vat_rate")
    price = float(quote.get("price") or 0)
    if vat in (None, ""):
        return price, "как в КП (ставка НДС не указана)"
    if float(vat) == 0:
        return price, "без НДС"
    return round(price * (1 + float(vat) / 100), 2), f"с НДС {float(vat):g}%"


def _money(value, digits: int = 2) -> str:
    """Деньги по-русски: 64 800,00 (неразрывный пробел между разрядами)."""
    if value is None:
        return "—"
    text = f"{float(value):,.{digits}f}".replace(",", "\u00a0").replace(".", ",")
    return text


def _quoted(name: str) -> str:
    """«ЭлектроТорг» без двойных кавычек, если поставщик уже назван в кавычках."""
    name = str(name or "").strip()
    return name if "«" in name else f"«{name}»"


def compare_quotes(request: dict, quotes: list[dict], *, today: date | None = None,
                   prefer: str = "price") -> dict:
    """Сравнить КП по каждой позиции заявки и объяснить выбор.

    `prefer`: `price` — сначала цена, `delivery` — сначала срок поставки.
    """
    today = today or date.today()
    by_code: dict[str, list[dict]] = {}
    for quote in quotes:
        by_code.setdefault(quote["code"], []).append(quote)

    lines: list[dict] = []
    not_comparable: list[dict] = []
    no_quotes: list[dict] = []

    for item in request.get("items") or []:
        candidates = by_code.get(item["code"], [])
        eligible: list[dict] = []
        for quote in candidates:
            verdict = validate_quote(quote, today=today)
            enriched = {**quote, "price_vat": _price_with_vat(quote)[0],
                        "price_basis": _price_with_vat(quote)[1],
                        "validation": verdict}
            if verdict["comparable"]:
                eligible.append(enriched)
            else:
                not_comparable.append({**enriched, "reasons": verdict["notes"]})

        if not eligible:
            reason = ("предложений по позиции нет" if not candidates else
                      "все предложения непригодны для сравнения")
            no_quotes.append({"code": item["code"], "name": item["name"],
                              "qty": item.get("qty"), "unit": item.get("unit"),
                              "reason": reason})
            continue

        key = (lambda q: (q["price_vat"], q.get("delivery_days") or 10 ** 6)) \
            if prefer == "price" else \
            (lambda q: (q.get("delivery_days") or 10 ** 6, q["price_vat"]))
        ranked = sorted(eligible, key=key)
        best = ranked[0]
        worst = ranked[-1]
        saving = round((worst["price_vat"] - best["price_vat"]) *
                       float(item.get("qty") or 0), 2)
        lines.append({
            "code": item["code"], "name": item["name"], "qty": item.get("qty"),
            "unit": item.get("unit"),
            "basis": best["price_basis"],
            "offers": [{k: q.get(k) for k in ("supplier", "price", "price_vat",
                                              "price_basis", "delivery_days",
                                              "valid_until", "quote_no",
                                              "payment_terms")} for q in ranked],
            "winner": {k: best.get(k) for k in ("supplier", "price", "price_vat",
                                               "price_basis", "delivery_days",
                                               "valid_until", "quote_no")},
            "runner_up": ({k: ranked[1].get(k) for k in ("supplier", "price_vat",
                                                        "delivery_days")}
                          if len(ranked) > 1 else None),
            "amount": round(best["price_vat"] * float(item.get("qty") or 0), 2),
            "saving_vs_worst": saving,
            "justification": _justification(item, best, ranked),
        })

    amount = round(sum(line["amount"] for line in lines), 2)
    saving = round(sum(line["saving_vs_worst"] for line in lines), 2)
    return {
        "schema": "pto-agent/procurement-compare@1",
        "compared_at": today.isoformat(),
        "request_no": request.get("request_no"),
        "object_name": request.get("object_name"),
        "lines": lines,
        "lines_count": len(lines),
        "amount": amount,
        "saving_vs_worst": saving,
        "not_comparable": not_comparable,
        "no_quotes": no_quotes,
        "status": ("complete" if lines and not no_quotes else
                   "partial" if lines else "no_data"),
        "note": ("выбор поставщика — за человеком: агент считает и объясняет, "
                 "решение фиксируется через procure decide (журнал аудита)"),
    }


def _justification(item: dict, best: dict, ranked: list[dict]) -> str:
    parts = [f"минимальная приведённая цена {_money(best['price_vat'])} руб. "
             f"({best['price_basis']}) у {_quoted(best['supplier'])}"]
    if best.get("delivery_days") not in (None, ""):
        parts.append(f"срок поставки {float(best['delivery_days']):g} дн.")
    if best.get("valid_until"):
        parts.append(f"цена действует до {best['valid_until']}")
    if len(ranked) > 1:
        second = ranked[1]
        diff = round(second["price_vat"] - best["price_vat"], 2)
        parts.append(f"следующее предложение {_quoted(second['supplier'])} дороже на "
                     f"{_money(diff)} руб./{item.get('unit') or 'ед.'}")
    return "; ".join(parts)


# ─────────────────────────────── решение человека ───────────────────────────────
def decide(comparison: dict, choice: dict, *, by: str, role: str = "инженер ПТО",
           reason: str = "", audit_log=None, today: date | None = None) -> dict:
    """Зафиксировать выбор человека по позициям: {код: поставщик} + причина.

    Решение не «применяется молча»: без причины запись всё равно пишется, но с пометкой
    `reason_missing`, чтобы при проверке это было видно.
    """
    today = today or date.today()
    lines = {line["code"]: line for line in comparison.get("lines") or []}
    decisions: list[dict] = []
    problems: list[str] = []
    for code, supplier in (choice or {}).items():
        line = lines.get(code)
        if not line:
            problems.append(f"{code}: позиции нет в сравнении")
            continue
        allowed = {offer["supplier"] for offer in line["offers"]}
        if supplier not in allowed:
            problems.append(f"{code}: поставщик «{supplier}» не подавал пригодного КП "
                            f"(есть: {', '.join(sorted(allowed))})")
            continue
        offer = next(o for o in line["offers"] if o["supplier"] == supplier)
        chosen = {"code": code, "name": line["name"], "supplier": supplier,
                  "price_vat": offer["price_vat"], "price_basis": offer["price_basis"],
                  "delivery_days": offer.get("delivery_days"),
                  "valid_until": offer.get("valid_until"),
                  "amount": round(offer["price_vat"] * float(line.get("qty") or 0), 2),
                  "over_best": round((offer["price_vat"] - line["winner"]["price_vat"]) *
                                     float(line.get("qty") or 0), 2)}
        decisions.append(chosen)
        if audit_log is not None:
            audit_log.append_change(
                by, "human", "procurement.decided", f"procurement:{comparison.get('request_no')}"
                f":{code}",
                {"supplier": line["winner"]["supplier"], "price_vat": line["winner"]["price_vat"]},
                {"supplier": supplier, "price_vat": offer["price_vat"]},
                reason=reason, ref=f"сравнение КП от {comparison.get('compared_at')}",
                payload={"object_name": comparison.get("object_name"), "role": role,
                         "quote_no": offer.get("quote_no")})

    total = round(sum(d["amount"] for d in decisions), 2)
    over = round(sum(d["over_best"] for d in decisions), 2)
    if audit_log is not None and not decisions:
        audit_log.append(by, "human", "procurement.decided", "procurement:rejected",
                         {"reason": reason or "без причины", "problems": problems})

    return {"schema": "pto-agent/procurement-decision@1", "decided_at": today.isoformat(),
            "by": by, "role": role, "reason": reason, "reason_missing": not reason,
            "request_no": comparison.get("request_no"), "decisions": decisions,
            "amount": total, "over_best": over, "problems": problems,
            "note": ("решение записано в журнал аудита; если поставщик выбран не по минимальной "
                     "цене — в записи видно, на сколько дороже и почему")}


def procurement_to_markdown(comparison: dict, decision: dict | None = None,
                            request: dict | None = None) -> str:
    lines: list[str] = []
    if request:
        lines += [f"# Заявка {request.get('request_no')}",
                  "", f"- объект: {request.get('object_name') or '—'}",
                  f"- источник объёмов: {request.get('source') or '—'}",
                  f"- позиций: {request.get('items_count')}", ""]
        lines += ["| Код | Наименование | Кол-во | Ед. |", "|---|---|---|---|"]
        for item in request.get("items") or []:
            lines.append(f"| {item['code']} | {item['name'][:70]} | "
                         f"{item.get('qty') if item.get('qty') is not None else '—'} | "
                         f"{item.get('unit') or '—'} |")
        lines.append("")

    lines += [f"# Сравнение КП по заявке {comparison.get('request_no')}",
              "", f"- статус: {comparison.get('status')} · позиций: {comparison.get('lines_count')}",
              f"- сумма по предложениям: {_money(comparison.get('amount'))} руб.",
              f"- экономия против худшего пригодного КП: "
              f"{_money(comparison.get('saving_vs_worst'))} руб.",
              "", "| Позиция | Кол-во | Победитель | Цена (с НДС) | Срок | Обоснование |",
              "|---|---|---|---|---|---|"]
    for line in comparison.get("lines") or []:
        win = line["winner"]
        lines.append(f"| {line['name'][:60]} | {line.get('qty')} {line.get('unit') or ''} | "
                     f"{win['supplier']} | {_money(win['price_vat'])} | "
                     f"{win.get('delivery_days') or '—'} дн. | {line['justification']} |")
    if comparison.get("not_comparable"):
        lines += ["", "**Не участвовали в сравнении:**"]
        for item in comparison["not_comparable"]:
            lines.append(f"- {item['supplier']} / {item['code']}: {'; '.join(item['reasons'])}")
    if comparison.get("no_quotes"):
        lines += ["", "**Без пригодных предложений:**"]
        for item in comparison["no_quotes"]:
            lines.append(f"- {item['code']} {item['name'][:60]}: {item['reason']}")
    if decision:
        lines += ["", f"## Решение ({decision['by']}, {decision['role']})",
                  "", f"- причина: {decision['reason'] or '— (не указана)'}"]
        for item in decision["decisions"]:
            extra = (f"; дороже лучшего на {_money(item['over_best'])} руб."
                     if item["over_best"] else "")
            lines.append(f"- {item['code']} → {item['supplier']} "
                         f"({_money(item['price_vat'])} руб., сумма "
                         f"{_money(item['amount'])} руб.{extra})")
        lines.append(f"- итого: {_money(decision['amount'])} руб.")
        if decision.get("problems"):
            lines += ["", "**Замечания:**"] + [f"- {p}" for p in decision["problems"]]
    lines.append("")
    return "\n".join(lines)


def export_comparison_xlsx(comparison: dict, path: str | Path,
                           decision: dict | None = None) -> Path:
    """Сравнение КП в xlsx: лист «сравнение», «непригодные», «решение»."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Сравнение"

    headers = ["Код", "Наименование", "Кол-во", "Ед.", "База сравнения", "Поставщик",
               "Цена КП", "Цена с НДС", "Срок, дн.", "Цена действует до", "КП №", "Сумма, руб."]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for line in comparison.get("lines") or []:
        for offer in line["offers"]:
            ws.append([line["code"], line["name"], line.get("qty"), line.get("unit"),
                       offer["price_basis"], offer["supplier"], offer["price"],
                       offer["price_vat"], offer.get("delivery_days"),
                       offer.get("valid_until"), offer.get("quote_no"),
                       round(offer["price_vat"] * float(line.get("qty") or 0), 2)])
    ws.append([])
    ws.append(["", "", "", "", "", "Итого по выбранным", "", "", "", "", "",
               comparison.get("amount")])
    ws.append(["", "", "", "", "", "Экономия против худшего", "", "", "", "", "",
               comparison.get("saving_vs_worst")])

    bad = wb.create_sheet("Непригодные КП")
    bad.append(["Поставщик", "Код", "Цена", "Причины"])
    for cell in bad[1]:
        cell.font = Font(bold=True)
    for item in comparison.get("not_comparable") or []:
        bad.append([item["supplier"], item["code"], item.get("price"),
                    "; ".join(item["reasons"])])
    for item in comparison.get("no_quotes") or []:
        bad.append(["—", item["code"], None, item["reason"]])

    if decision:
        dec = wb.create_sheet("Решение")
        dec.append(["Код", "Поставщик", "Цена с НДС", "Сумма", "Дороже лучшего, руб."])
        for cell in dec[1]:
            cell.font = Font(bold=True)
        for item in decision["decisions"]:
            dec.append([item["code"], item["supplier"], item["price_vat"], item["amount"],
                        item["over_best"]])
        dec.append([])
        dec.append([f"Решение: {decision['by']} ({decision['role']})"])
        dec.append([f"Причина: {decision['reason'] or 'не указана'}"])

    money = '# ##0.00'
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                    cell.number_format = money
        sheet.freeze_panes = "A2"
        sheet.column_dimensions["B"].width = 46

    wb.save(path)
    return path
