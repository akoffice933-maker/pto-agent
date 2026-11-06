# -*- coding: utf-8 -*-
"""Стык «takeoff → наша ведомость объёмов → цены с источником».

Зачем
-----
Раздел КР приходит чертежом, а смета — таблицей. Между ними takeoff: извлечение объёмов
(бетон, арматура, опалубка, площади). Готового «PDF КР → российская смета» в открытом коде
нет (см. `data/reference/takeoff_oss.json`), поэтому стык делаем сами: берём **выгрузку
объёмов** от takeoff-инструмента (например, `autoconst-claude-2d-pdf-to-priced-boq`, MIT —
формат `masterformat_code, description, unit, qty, confidence, source`) и превращаем её в
ведомость объёмов с ценами из наших версий ФГИС ЦС.

Три правила, чтобы стык не превратился в генератор красивых цифр:

1. **Чужой confidence-флаг не выбрасываем.** `HIGH` — в итог; `MEDIUM` — в итог с пометкой
   «проверить»; `LOW` / `RATE_NOT_FOUND` — **не в итог**, а в раздел «не включено» с
   причиной. Итог по строкам разной уверенности не складывается в одно «итого».
2. **Цена — только из базы с датой и источником.** Каждая цена берётся `prices.lookup(as_of)`
   и несёт версию, дату и digest. Нет цены — `no_price`, сумма по строке `null`, а не 0 ₽.
   Валютные ставки из чужой rate-library не используются: рубли не выдумываются.
3. **Смету собирает человек.** Draft — это ведомость объёмов с ценами, а не смета в ФЕР:
   шифр расценки ставит инженер при подтверждении (`confirm`), и это уходит в журнал аудита
   с автором и причиной. Агент не заявляет «смета готова».
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from .numeric import parse_number
from .prices import lookup as price_lookup

# ─────────────────────────── единицы ───────────────────────────
UNIT_RU = {"m3": "м3", "m2": "м2", "m": "м", "lf": "м", "kg": "кг", "t": "т", "ton": "т",
           "tonne": "т", "ea": "шт", "pcs": "шт", "pc": "шт", "no": "шт", "each": "шт",
           "sf": "м2", "sqft": "м2", "cy": "м3", "yd3": "м3", "шт": "шт", "м3": "м3",
           "м2": "м2", "м": "м", "т": "т", "кг": "кг", "1000 шт": "1000 шт",
           "м3 (в плотном теле)": "м3"}
# приведение к единице, в которой лежит цена в базе: (единица, коэффициент)
UNIT_FACTOR = {"кг": ("т", 0.001)}

CONFIDENCE_HIGH, CONFIDENCE_MEDIUM = "high", "medium"
CONFIDENCE_LOW, CONFIDENCE_RATE = "low", "rate_not_found"

STATUS_PRICED = "priced"
STATUS_NO_PRICE = "no_price"
STATUS_REVIEW = "review"             # medium: в итог идёт, но помечен «проверить»
STATUS_EXCLUDED = "excluded"         # low / rate_not_found: не в итог
STATUS_UNIT_UNKNOWN = "unit_unknown"

BOQ_ALIASES = {
    "code": ("masterformat_code", "masterformat", "csi", "code", "шифр", "код"),
    "description": ("description", "item", "name", "наименование", "описание", "элемент"),
    "unit": ("unit", "uom", "ед", "ед. изм.", "единица"),
    "qty": ("qty", "quantity", "количество", "кол-во", "объём", "объем"),
    "confidence": ("confidence", "conf", "уверенность", "достоверность"),
    "source": ("source", "sheet", "source_sheet", "лист", "источник", "чертёж", "чертеж"),
    "trade": ("trade", "division", "раздел", "вид работ"),
}


def unit_ru(unit) -> str | None:
    """«m3» → «м3», «kg» → «кг»; неизвестное возвращается как есть."""
    text = str(unit or "").strip()
    return UNIT_RU.get(_norm(text)) or (text or None)


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("ё", "е").split())


def _pick(row: dict, key: str):
    normalized = {_norm(k): v for k, v in row.items() if not str(k).startswith("_")}
    for alias in BOQ_ALIASES.get(key, ()):
        if _norm(alias) in normalized:
            return normalized[_norm(alias)]
    return None


def _num(value) -> float | None:
    """Конечное число или `None`. «nan»/«inf» — не число: `float()` их принимает, а итог
    с `nan` уже не сходится ни с чем."""
    return parse_number(value)


def _rows(path: str | Path) -> tuple[list[dict], list[str]]:
    path = Path(path)
    if not path.exists():
        return [], [f"файл не найден: {path}"]
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        ws = load_workbook(path, data_only=True).active
        raw = [tuple(r) for r in ws.iter_rows(values_only=True)]
    else:
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                with open(path, encoding=encoding, newline="") as f:
                    raw = [tuple(r) for r in csv.reader(f, delimiter=_delimiter(path))]
                break
            except UnicodeDecodeError:
                continue
        else:
            return [], [f"не удалось определить кодировку: {path}"]
    if not raw:
        return [], [f"файл пуст: {path}"]
    header = [str(c or "").strip() for c in raw[0]]
    out = []
    for number, values in enumerate(raw[1:], start=2):
        if not any(str(v or "").strip() for v in values):
            continue
        out.append({"_row": number, **{header[i] if i < len(header) else f"col{i}": values[i]
                                       for i in range(len(values))}})
    return out, ([] if out else [f"{path.name}: строк с данными нет"])


def _delimiter(path: Path) -> str:
    try:
        with open(path, encoding="utf-8-sig") as f:
            head = f.readline()
        return ";" if head.count(";") > head.count(",") else ","
    except OSError:
        return ","


# ─────────────────────────── чтение BOQ ───────────────────────────
def read_boq(path: str | Path) -> tuple[list[dict], list[str]]:
    """Прочитать выгрузку объёмов (BOQ) от takeoff-инструмента.

    Понимает формат `autoconst` (`masterformat_code/description/unit/qty/confidence/source`)
    и русские заголовки (`наименование, ед. изм., количество, уверенность, лист`).
    """
    rows, problems = _rows(path)
    lines: list[dict] = []
    for row in rows:
        description = str(_pick(row, "description") or "").strip()
        qty = _num(_pick(row, "qty"))
        unit_native = str(_pick(row, "unit") or "").strip()
        confidence = _norm(_pick(row, "confidence") or "")
        if not description:
            problems.append(f"{Path(path).name}:{row.get('_row')}: нет наименования — "
                            f"строка пропущена")
            continue
        if qty is None:
            raw = _pick(row, "qty")
            what = ("нет количества" if raw in (None, "")
                    else f"количество «{raw}» не число")
            problems.append(f"{Path(path).name}:{row.get('_row')}: {what} — "
                            f"строка в «не включено»")
        unit_ru = UNIT_RU.get(_norm(unit_native))
        factor, unit_ru_final = 1.0, unit_ru
        if unit_ru and unit_ru in UNIT_FACTOR:
            unit_ru_final, factor = UNIT_FACTOR[unit_ru]
        lines.append({
            "row": row.get("_row"),
            "code": str(_pick(row, "code") or "").strip(),
            "description": description,
            "unit_native": unit_native,
            "unit": unit_ru_final or unit_native,
            "qty_native": qty,
            "qty": None if qty is None else round(qty * factor, 6),
            "factor": factor,
            "confidence": confidence or "",
            "source": str(_pick(row, "source") or "").strip(),
            "trade": str(_pick(row, "trade") or "").strip(),
            "file": Path(path).name,
        })
    return lines, problems


def from_external_report(report: dict, *, confidence: str = CONFIDENCE_MEDIUM) -> dict:
    """Позиции внешнего обмера (MCP-обмен, `cad/external_takeoff.py`) → строки BOQ.

    Чужой обмер не получает флаг HIGH: мы не видели, как он посчитан. По умолчанию
    `medium` — «в итог, но проверить». Позиция без объёма уходит в «не включено» с
    причиной «объём не оценён», а не превращается в ноль.
    """
    lines = []
    for position in report.get("positions") or []:
        qty = position.get("qty")
        lines.append({
            "code": position.get("code") or "",
            "description": position.get("name") or "",
            "unit": unit_ru(position.get("unit")) or position.get("unit"),
            "unit_native": position.get("unit"),
            "qty": qty, "qty_native": qty,
            "confidence": confidence if qty is not None else CONFIDENCE_LOW,
            "source": report.get("source") or "внешний обмер",
            "trade": "", "file": report.get("file") or "",
        })
    return {"lines": lines, "source": report.get("source"),
            "license": report.get("license"), "tool": report.get("tool"),
            "note": "внешний обмер: флаг HIGH не выставляется — источник чужой, объём проверяется"}


# ─────────────────────────── словарь соответствия ───────────────────────────
def load_map(path: str | Path | None = None) -> dict:
    """Словарь «описание элемента → код ресурса в базе цен». Без него цены не подбираются."""
    path = Path(path or (Path(__file__).resolve().parents[1] / "data" / "reference"
                         / "takeoff_map.json"))
    if not path.exists():
        return {"rules": []}
    return json.loads(path.read_text(encoding="utf-8"))


def match_resource(line: dict, mapping: dict) -> dict:
    """Подобрать ресурс по описанию: только по явным правилам, без «угадывания по смыслу»."""
    text = _norm(line.get("description"))
    for rule in mapping.get("rules") or []:
        keywords = [_norm(k) for k in rule.get("keywords") or []]
        if keywords and all(word in text for word in keywords):
            return {"code": rule.get("code") or "", "name": rule.get("name") or "",
                    "rule": rule.get("name") or rule.get("code") or "",
                    "note": rule.get("note") or "", "matched": True}
    return {"code": "", "name": "", "rule": "", "note": "", "matched": False}


# ─────────────────────────── черновик ведомости ───────────────────────────
def build_draft(boq: list[dict], *, mapping: dict | None = None, prices_dir: str | Path = "data/prices",
                as_of: str | None = None, object_name: str = "", section: str = "КР",
                today: date | None = None) -> dict:
    """Собрать черновик ведомости объёмов с ценами: статус по каждой строке, без «итого» из
    строк разной уверенности."""
    mapping = mapping if mapping is not None else load_map()
    today = today or date.today()
    lines: list[dict] = []
    excluded: list[dict] = []
    review: list[dict] = []

    for line in boq:
        confidence = _norm(line.get("confidence") or "")   # HIGH / Medium / low → high/medium/low
        resource = match_resource(line, mapping)
        price = (price_lookup(code=resource["code"], as_of=as_of, out_dir=prices_dir)
                 if resource["code"] else {"found": False, "reason": "ресурс не сопоставлен"})

        item = {
            "code": resource["code"] or line.get("code") or "",
            "element": line.get("description"),
            "resource": resource["name"] or None,
            "rule": resource["rule"] or None,
            "unit": unit_ru(line.get("unit")),
            "qty": line.get("qty"),
            "qty_native": line.get("qty_native"),
            "unit_native": line.get("unit_native"),
            "confidence": confidence,
            "source": line.get("source"),
            "trade": line.get("trade"),
        }

        if confidence in (CONFIDENCE_LOW, CONFIDENCE_RATE) or not confidence:
            item["status"] = STATUS_EXCLUDED
            item["reason"] = (f"уверенность «{confidence or 'не указана'}»: объём не проверен — "
                              f"в итог не идёт")
            item["price"], item["amount"] = None, None
            excluded.append(item)
            continue
        if not resource["matched"]:
            item["status"] = STATUS_NO_PRICE
            item["reason"] = "нет правила сопоставления «элемент → код ресурса»"
            item["price"], item["amount"] = None, None
            lines.append(item)
            continue
        if line.get("qty") is None:
            item["status"] = STATUS_NO_PRICE
            item["reason"] = "в выгрузке нет количества"
            item["price"], item["amount"] = None, None
            lines.append(item)
            continue
        if float(line["qty"]) < 0:
            item["status"] = STATUS_REVIEW
            item["reason"] = (f"отрицательный объём {line['qty']:g}: это либо возврат, либо "
                              f"ошибка знака — в итог строка не идёт до подтверждения")
            item["amount"] = None
            review.append(item)
            lines.append(item)
            continue
        if not price.get("found"):
            item["status"] = STATUS_NO_PRICE
            item["reason"] = price.get("reason") or "цены нет в базе"
            item["searched"] = price.get("searched")
            item["price"], item["amount"] = None, None
            lines.append(item)
            continue

        item.update({
            "price": price["price"],
            "price_unit": unit_ru(price.get("unit")),
            "price_source": price.get("source"),
            "price_version": price.get("version"),
            "price_date": price.get("price_date"),
            "price_digest": price.get("digest"),
            "amount": round(price["price"] * float(line["qty"]), 2),
            "status": STATUS_REVIEW if confidence == CONFIDENCE_MEDIUM else STATUS_PRICED,
            "confirmed": False,
            "norm": None,
        })
        if confidence == CONFIDENCE_MEDIUM:
            item["reason"] = "объём средней уверенности: сумма посчитана, но требует проверки"
            review.append(item)
        if (item["price_unit"] and _norm(unit_ru(item["price_unit"])) != _norm(item["unit"])):
            # деньги в другой единице в «итого» попасть не должны: шт × ₽/м3 — не сумма
            item["amount_wrong_unit"] = item["amount"]
            item["amount"] = None
            item["reason"] = (f"единица объёма «{item['unit']}» не совпадает с единицей цены "
                              f"«{item['price_unit']}» — сумма {item['amount_wrong_unit']:,.2f} ₽ "
                              f"в итог не идёт, нужен пересчёт")
            item["amount_placeholder"] = True
            item["status"] = STATUS_REVIEW
            review.append(item)
        lines.append(item)

    priced = [l for l in lines if l.get("amount") is not None]
    duplicated = _mark_duplicates(lines)
    return {
        "schema": "pto-agent/takeoff-draft@1",
        "object_name": object_name, "section": section,
        "as_of": as_of or today.isoformat(),
        "built_at": today.isoformat(),
        "source_note": ("объёмы — из выгрузки takeoff-инструмента, цены — из базы версий "
                        "ФГИС ЦС на дату; это ведомость объёмов с ценами, не смета в ФЕР"),
        "lines": lines,
        "excluded": excluded,
        "review": [{"element": i["element"], "reason": i["reason"]} for i in review],
        "totals": {
            "lines": len(lines),
            "priced_lines": len(priced),
            "amount_rub": round(sum(l["amount"] for l in priced), 2) if priced else None,
            # в «высокую» сумму не идут строки, помеченные как возможный дубль
            "amount_high_rub": round(sum(l["amount"] for l in priced
                                         if l["confidence"] == CONFIDENCE_HIGH
                                         and not l.get("duplicate_of")), 2) if priced else None,
            "amount_duplicates_rub": round(sum(l["amount"] for l in priced
                                               if l.get("duplicate_of")), 2) if priced else None,
            "review_lines": len({i["element"] for i in review}),
            "excluded_lines": len(excluded),
            "no_price_lines": len([l for l in lines if l["status"] == STATUS_NO_PRICE]),
            "duplicate_lines": duplicated,
            "negative_lines": len([l for l in lines if "отрицательный объём" in (l.get("reason") or "")]),
            "wrong_unit_lines": len([l for l in lines if l.get("amount_placeholder")]),
        },
        "confirmed_by": None,
    }


def _norm_key(text) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _mark_duplicates(lines: list[dict]) -> int:
    """Одинаковые строки (элемент + единица + количество) в одной ведомости.

    Выгрузки склеивают из нескольких листов, и одна и та же работа попадает дважды. Сумма при
    этом удваивается, поэтому такие строки помечаются и человек решает, а не «итого».
    """
    seen: dict[tuple, list[dict]] = {}
    for line in lines:
        if line.get("qty") is None:
            continue
        key = (_norm_key(line.get("element")), _norm_key(line.get("unit")), round(float(line["qty"]), 6))
        seen.setdefault(key, []).append(line)
    groups = 0
    for key, group in seen.items():
        if len(group) < 2:
            continue
        groups += 1
        for line in group:
            line["duplicate_of"] = len(group)
            line["status"] = STATUS_REVIEW
            line.setdefault("reason", f"такая же строка встречается {len(group)} раза: "
                                      f"сумма удвоится — проверить, не склеены ли листы")
    return groups


def confirm(draft: dict, changes: dict | None = None, *, by: str, role: str = "инженер ПТО",
            reason: str = "", audit_log=None, today: date | None = None) -> dict:
    """Подтверждение человеком: количества и шифры расценок ставит он, а не агент.

    `changes` — {элемент: {"qty": 12.5, "norm": "ФЕР 06-01-001-01"}}. Каждое изменение
    и подтверждение пишется в журнал аудита с автором, ролью и причиной.
    """
    today = today or date.today()
    changes = changes or {}
    applied: list[dict] = []
    problems: list[str] = []

    for line in draft.get("lines") or []:
        key = line["element"]
        change = changes.get(key) or {}
        before = {"qty": line.get("qty"), "norm": line.get("norm"), "status": line["status"]}
        if "qty" in change:
            qty = _num(change["qty"])
            if qty is None:
                problems.append(f"{key}: количество «{change['qty']}» не число — не применено")
                continue
            line["qty"] = qty
            line["qty_source"] = f"правка человека ({by})"
            if line.get("amount") is not None and line.get("price") is not None:
                line["amount"] = round(line["price"] * qty, 2)
        if change.get("norm"):
            line["norm"] = str(change["norm"]).strip()
        if change.get("exclude"):
            line["status"] = STATUS_EXCLUDED
            line["reason"] = change.get("note") or f"исключено человеком ({by})"
        if line["status"] != STATUS_EXCLUDED:
            line["confirmed"] = True
            line["confirmed_by"] = by
        after = {"qty": line.get("qty"), "norm": line.get("norm"), "status": line["status"]}
        if before != after:
            applied.append({"element": key, "before": before, "after": after})
            if audit_log is not None:
                audit_log.append_change(
                    by, "human", "takeoff.confirmed", f"takeoff:{draft.get('section')}:{key}",
                    before, after, reason=reason,
                    ref=f"черновик ведомости от {draft.get('built_at')}")

    if audit_log is not None and not applied:
        audit_log.append(by, "human", "takeoff.confirmed",
                         f"takeoff:{draft.get('section')}",
                         {"confirmed_lines": len([l for l in draft["lines"] if l.get('confirmed')]),
                          "reason": reason or "без изменений",
                          "reason_missing": not reason, "role": role})

    draft["confirmed_by"] = {"by": by, "role": role, "reason": reason,
                             "reason_missing": not reason, "at": today.isoformat(),
                             "changes": applied, "problems": problems}
    _recount(draft)
    return draft


def _recount(draft: dict) -> None:
    counted = [l for l in draft.get("lines") or []
               if l.get("amount") is not None and l["status"] != STATUS_EXCLUDED]
    totals = draft.setdefault("totals", {})
    totals["priced_lines"] = len(counted)
    totals["amount_rub"] = round(sum(l["amount"] for l in counted), 2) if counted else None
    totals["amount_high_rub"] = (round(sum(l["amount"] for l in counted
                                           if l["confidence"] == CONFIDENCE_HIGH
                                           and not l.get("duplicate_of")), 2)
                                 if counted else None)
    totals["amount_duplicates_rub"] = (round(sum(l["amount"] for l in counted
                                                 if l.get("duplicate_of")), 2)
                                       if counted else None)
    totals["confirmed_lines"] = len([l for l in draft["lines"] if l.get("confirmed")])


# ─────────────────────────── выгрузки ───────────────────────────
def draft_to_markdown(draft: dict) -> str:
    totals = draft.get("totals") or {}
    lines = [f"# Ведомость объёмов {draft.get('section')}: "
             f"{draft.get('object_name') or 'объект не указан'}", "",
             f"- цены на дату: {draft.get('as_of')} · версия базы в каждой строке",
             f"- строк: {totals.get('lines')} · с ценой: {totals.get('priced_lines')} · "
             f"на проверку: {totals.get('review_lines')} · не включено: "
             f"{totals.get('excluded_lines')}",
             f"- **итог по строкам с известной ценой: "
             f"{_money(totals.get('amount_rub'))} ₽**"
             + (f" (из них высокая уверенность: {_money(totals.get('amount_high_rub'))} ₽)"
                if totals.get("amount_high_rub") is not None else "")
             + (f" · \n- **под сомнением (повторяющиеся строки, в высокую уверенность не "
                f"вошли): {_money(totals.get('amount_duplicates_rub'))} ₽**"
                if totals.get("amount_duplicates_rub") else "")
             + (f" · \n- строк с несовпадением единицы или отрицательным объёмом: "
                f"{(totals.get('wrong_unit_lines') or 0) + (totals.get('negative_lines') or 0)} "
                f"— их суммы в итог не включены" if (totals.get("wrong_unit_lines")
                                                     or totals.get("negative_lines")) else ""), "",
             "| Элемент | Кол-во | Ед. | Ресурс | Цена | Сумма | Уверенность | Статус |",
             "|---|---|---|---|---|---|---|---|"]
    for line in draft.get("lines") or []:
        lines.append(f"| {line['element'][:44]} | {_qty(line.get('qty'))} | {line.get('unit') or '—'} | "
                     f"{line.get('resource') or '—'} | {_money(line.get('price'))} | "
                     f"{_money(line.get('amount'))} | {line.get('confidence') or '—'} | "
                     f"{line['status']} |")
    if draft.get("excluded"):
        lines += ["", "## Не включено в итог", ""]
        for item in draft["excluded"]:
            lines.append(f"- {item['element'][:70]} — {item['qty'] or '—'} "
                         f"{item.get('unit') or ''}: {item.get('reason')}")
    to_check = [l for l in draft.get("lines") or []
                if l["status"] == STATUS_REVIEW and l.get("reason")]
    if to_check:
        lines += ["", "## На проверку", ""]
        for item in to_check:
            tail = ("" if item.get("amount") is not None
                    else " — **в итог не вошла**")
            lines.append(f"- {item['element'][:60]}: {item.get('reason')}{tail}")
    no_price = [l for l in draft.get("lines") or [] if l["status"] == STATUS_NO_PRICE]
    if no_price:
        lines += ["", "## Без цены (в итог не вошло)", ""]
        for item in no_price:
            lines.append(f"- {item['element'][:70]}: {item.get('reason')}")
    confirmed = draft.get("confirmed_by")
    if confirmed:
        lines += ["", f"## Подтверждение ({confirmed['by']}, {confirmed['role']})",
                  "", f"- причина: {confirmed.get('reason') or '— (не указана)'}",
                  f"- правок: {len(confirmed.get('changes') or [])}"]
        for change in confirmed.get("changes") or []:
            if change["before"].get("qty") != change["after"].get("qty"):
                lines.append(f"- {change['element'][:60]}: количество "
                             f"{_qty(change['before'].get('qty'))} → "
                             f"{_qty(change['after'].get('qty'))}")
    else:
        lines += ["", "_Черновик не подтверждён человеком: количества и шифры расценок "
                      "не зафиксированы._"]
    lines += ["", "_Это ведомость объёмов с ценами, а не смета в ФЕР/ГЭСН: шифр расценки "
                  "ставит инженер при подтверждении._"]
    return "\n".join(lines) + "\n"


def export_draft_xlsx(draft: dict, path: str | Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Ведомость объёмов"
    ws.append(["Элемент", "Код ресурса", "Ресурс", "Кол-во", "Ед.", "Цена, ₽", "Сумма, ₽",
               "Уверенность", "Лист-источник", "Версия цены", "Дата цены", "Шифр расценки",
               "Статус"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for line in draft.get("lines") or []:
        ws.append([line["element"], line.get("code"), line.get("resource"), line.get("qty"),
                   line.get("unit"), line.get("price"), line.get("amount"),
                   line.get("confidence"), line.get("source"), line.get("price_version"),
                   line.get("price_date"), line.get("norm"), line["status"]])

    excluded = wb.create_sheet("Не включено")
    excluded.append(["Элемент", "Кол-во", "Ед.", "Причина"])
    for cell in excluded[1]:
        cell.font = Font(bold=True)
    for item in draft.get("excluded") or []:
        excluded.append([item["element"], item.get("qty"), item.get("unit"), item.get("reason")])
    for item in draft.get("lines") or []:
        if item["status"] == STATUS_NO_PRICE:
            excluded.append([item["element"], item.get("qty"), item.get("unit"),
                             item.get("reason")])

    totals = wb.create_sheet("Итоги")
    totals.append(["Показатель", "Значение"])
    for cell in totals[1]:
        cell.font = Font(bold=True)
    for key, title in (("lines", "Строк"), ("priced_lines", "Строк с ценой"),
                       ("amount_rub", "Итог, ₽"), ("amount_high_rub", "Итог по высокой уверенности, ₽"),
                       ("review_lines", "Строк на проверку"), ("excluded_lines", "Не включено строк"),
                       ("no_price_lines", "Без цены")):
        totals.append([title, (draft.get("totals") or {}).get(key)])
    totals.append(["Цены на дату", draft.get("as_of")])
    confirmed = draft.get("confirmed_by")
    totals.append(["Подтвердил", (confirmed or {}).get("by") or "черновик не подтверждён"])
    totals.append(["Причина", (confirmed or {}).get("reason") or ""])

    for sheet in wb.worksheets:
        sheet.freeze_panes = "A2"
        sheet.column_dimensions["A"].width = 42
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                    cell.number_format = '# ##0.00'
    wb.save(path)
    return path


def _money(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.2f}".replace(",", "\u00a0").replace(".", ",")


def _qty(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):g}".replace(".", ",")


def write(path: str | Path, draft: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
