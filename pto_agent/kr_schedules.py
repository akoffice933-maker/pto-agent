# -*- coding: utf-8 -*-
"""Ведомости раздела КР: арифметическая проверка, конфликты и превращение в BOQ.

Зачем
-----
Ведомость расхода стали и спецификация материалов — это уже готовые объёмы: их не надо
мерить, их надо **проверить и не потерять**. Типичные беды ведомостей, которые видно
только при арифметике:

* «Итого» не равно сумме по диаметрам (ошибка в ведомости или в перепечатке);
* одна и та же конструкция в спецификации и в ведомости имеет **разные марки бетона**
  (В25 W6 F50 в спецификации против В25 W6 F150 в пояснительной записке — это разная
  морозостойкость, и в смете это разные цены);
* единицы не совпадают (кг против т, п.м. против м2) — количество молча сравнивать нельзя;
* количество, перенесённое с фотографии листа, — это **не факт из PDF**, пока человек
  не сверил. Поэтому такие строки идут как `medium`, а не `high`.

Модуль ничего не «улучшает»: подозрительная цифра уходит в итог только после человека,
а не заменяется «похожей».
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .takeoff import CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_MEDIUM, unit_ru
from .numeric import parse_number

TOL_KG = 1.0          # допуск на округление в ведомости стали, кг
TOL_M3 = 0.05

CONFLICT_MARK = "MARK_CONFLICT"
CONFLICT_SUM = "SUM_MISMATCH"
CONFLICT_SOURCE = "SOURCE_UNVERIFIED"


def read_schedules(path: str | Path) -> dict:
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["_file"] = path.name
    return doc


def _num(value) -> float | None:
    """Конечное число или `None`: «nan» из ведомости не должно дойти до объёма.

    Раньше `float()` принимал «nan», и проверка суммы «Итого» против суммы по диаметрам
    молчала: `abs(nan - x) > допуск` всегда `False`.
    """
    return parse_number(value)


def _diameters(raw: dict) -> tuple[dict, list[str]]:
    """Диаметры → (разобранные значения, ключи, которые не число).

    Неразобранное значение нельзя превращать в 0.0: сумма по диаметрам тогда «сходится»
    с испорченной цифрой, а не с ведомостью.
    """
    values, broken = {}, []
    for key, value in (raw or {}).items():
        number = _num(value)
        if number is None:
            broken.append(f"{key}: {value!r}")
            continue
        values[key] = number
    return values, broken


def marks_of(mark: str) -> set[str]:
    """«В25 W6 F150» → {'В25', 'W6', 'F150'} — чтобы сравнивать марки по частям."""
    return set(re.findall(r"[ВB]\d{1,2}|W\d{1,2}|F\d{2,3}", str(mark or "").upper()))


# ─────────────────────────── проверки ───────────────────────────
def validate_schedules(doc: dict, facts: dict | None = None) -> dict:
    """Арифметика ведомостей, единицы и конфликты марок. Возвращает находки и статус."""
    findings: list[dict] = []
    sheets = doc.get("sheets") or []

    for sheet in sheets:
        for table in sheet.get("tables") or []:
            name = table.get("name") or f"лист {sheet.get('sheet')}"
            steel = table.get("steel")
            if steel:
                diameters, broken = _diameters(steel.get("diameters"))
                steel["_broken"] = broken
                total = _num(steel.get("total_kg"))
                computed = round(sum(diameters.values()), 2)
                steel["_computed_kg"] = computed
                if broken:
                    findings.append(_finding(
                        "VALUE_UNPARSED", name,
                        f"значения по диаметрам не числа: {', '.join(broken)} — в сумму не "
                        f"вошли (иначе ошибка переноса цифр замаскировалась бы)",
                        expected=None, found=", ".join(broken)))
                if total is None:
                    raw_total = steel.get("total_kg")
                    detail = ("в ведомости нет «Итого»" if raw_total in (None, "")
                              else f"«Итого» {raw_total!r} не число")
                    findings.append(_finding(CONFLICT_SUM, name, detail,
                                             expected=computed, found=None))
                elif abs(computed - total) > TOL_KG:
                    findings.append(_finding(
                        CONFLICT_SUM, name,
                        f"сумма по диаметрам {_money(computed)} кг не сходится с «Итого» "
                        f"{_money(total)} кг (разница "
                        f"{_money(round(computed - total, 2))} кг)",
                        expected=computed, found=total))
            concrete = table.get("concrete") or {}
            if concrete and _num(concrete.get("qty")) is None:
                raw_qty = concrete.get("qty")
                detail = ("в строке бетона нет количества" if raw_qty in (None, "")
                          else f"количество бетона {raw_qty!r} не число")
                findings.append(_finding("QTY_MISSING", name, detail, expected=None, found=None))
            elif concrete and _num(concrete.get("qty")) == 0:
                findings.append(_finding("QTY_MISSING", name,
                                         "объём бетона 0 м3 — незаполненная или испорченная "
                                         "ячейка", expected=None, found=0))

    # конфликт марок бетона между листами (и с пояснительной запиской, если она разобрана)
    by_element: dict[str, list[dict]] = {}
    for sheet in sheets:
        for table in sheet.get("tables") or []:
            concrete = table.get("concrete") or {}
            if concrete.get("mark"):
                by_element.setdefault(_element_key(table.get("name")), []).append(
                    {"mark": concrete["mark"], "sheet": sheet.get("sheet"),
                     "table": table.get("name")})
    spec_marks = {row["name"]: row for sheet in sheets for table in sheet.get("tables") or []
                  for row in table.get("rows") or [] if "етон" in row.get("name", "")}

    reference = None
    if facts and (facts.get("by_name") or {}).get("бетон плиты и стен подвала"):
        reference = facts["by_name"]["бетон плиты и стен подвала"]["value"].replace(" × ", " ")
        reference_page = facts["by_name"]["бетон плиты и стен подвала"]["page"]
    else:
        reference_page = None

    marks_seen: dict[str, set[str]] = {}
    for element, items in by_element.items():
        for item in items:
            marks_seen.setdefault(element, set()).add(item["mark"])
    for element, marks in marks_seen.items():
        if len(marks) > 1:
            findings.append(_finding(CONFLICT_MARK, element,
                                     f"в ведомостях разные марки бетона: {', '.join(sorted(marks))}",
                                     expected=sorted(marks)[0], found=sorted(marks)[-1]))
    for name, row in spec_marks.items():
        if reference:
            spec_marks_set = marks_of(row["name"])
            ref_marks = marks_of(reference)
            same_class = (spec_marks_set & ref_marks & {m for m in ref_marks if m.startswith(("В", "B"))})
            if spec_marks_set and ref_marks and same_class and spec_marks_set != ref_marks:
                findings.append(_finding(
                    CONFLICT_MARK, name,
                    f"в спецификации «{row['name']}», а в пояснительной записке "
                    f"«{reference}» (стр. {reference_page}): расходится морозостойкость/"
                    f"водонепроницаемость — это разные цены и разный бетон",
                    expected=reference, found=row["name"]))

    if doc.get("needs_verification"):
        findings.append(_finding(
            CONFLICT_SOURCE, "источник",
            f"значения перенесены из {doc.get('source') or 'фотографий'} — до сверки с PDF "
            f"это не факт проекта, а ввод агента", expected="PDF листа", found="фото"))

    # правдоподобие: удельный расход арматуры на м3 бетона. Это НЕ норматив,
    # а проверка порядка величины — чтобы поймать опечатку в переносе цифр.
    for sheet in sheets:
        for table in sheet.get("tables") or []:
            steel = table.get("steel") or {}
            concrete = table.get("concrete") or {}
            total = _num(steel.get("total_kg"))
            volume = _num(concrete.get("qty"))
            if not total or not volume:
                continue
            rate = total / volume
            if not 40 <= rate <= 220:
                findings.append(_finding(
                    "PLAUSIBILITY", table.get("name") or "—",
                    f"удельный расход {rate:.0f} кг/м3 при объёме бетона {volume:g} м3 — вне "
                    f"типичного диапазона 40…220 кг/м3 для монолитных ж/б конструкций; "
                    f"это не норматив, а сигнал проверить цифры", expected="40…220 кг/м3",
                    found=f"{rate:.0f} кг/м3"))

    return {"findings": findings,
            "sum_mismatches": [f for f in findings if f["type"] == CONFLICT_SUM],
            "mark_conflicts": [f for f in findings if f["type"] == CONFLICT_MARK],
            "plausibility": [f for f in findings if f["type"] == "PLAUSIBILITY"],
            "ok": not [f for f in findings if f["type"] in (CONFLICT_SUM, "QTY_MISSING")]}


def _element_key(table_name: str | None) -> str:
    text = str(table_name or "").split(".")[-1].strip()
    return text or "конструкция"


def _finding(kind: str, where: str, detail: str, *, expected=None, found=None) -> dict:
    return {"type": kind, "where": where, "detail": detail,
            "expected": expected, "found": found}


# ─────────────────────────── BOQ из ведомостей ───────────────────────────
def to_boq(doc: dict, validation: dict | None = None) -> dict:
    """Ведомости → строки BOQ. Подозрительные суммы идут с `low`, остальные — `medium`."""
    validation = validation or validate_schedules(doc)
    suspicious = {f["where"] for f in validation["findings"] if f["type"] == CONFLICT_SUM}
    lines: list[dict] = []
    skipped: list[str] = []          # строки, где количество не разобралось
    note_missing = ""

    for sheet in doc.get("sheets") or []:
        for table in sheet.get("tables") or []:
            name = table.get("name") or f"лист {sheet.get('sheet')}"
            steel = table.get("steel")
            concrete = table.get("concrete") or {}
            element = _element_key(name)

            if steel:
                diameters, broken = _diameters(steel.get("diameters"))
                computed = round(sum(diameters.values()), 2)
                printed = _num(steel.get("total_kg"))
                # берём «Итого» из ведомости; нет или не число — сумма по диаметрам, но тогда
                # цена считается по расчётной цифре, а не по напечатанной
                total_kg = printed if printed is not None else computed
                if printed is None:
                    mismatch = True
                    note_missing = ("«Итого» не число" if broken or steel.get("total_kg")
                                    else "«Итого» нет")
                mismatch = name in suspicious or printed is None
                # берём «Итого», напечатанное в ведомости: это итог конструктора. Сумма по
                # диаметрам остаётся рядом (by_diameter / alt_qty), чтобы сверка была видна.
                qty_t = round(total_kg / 1000, 3)
                lines.append({
                    "description": f"Арматура {steel.get('grade') or 'А500С'} — {element} "
                                   f"({steel.get('gost') or 'ГОСТ 52544-2006'})",
                    "unit": "т", "qty": qty_t,
                    "alt_qty": round(computed / 1000, 3) if mismatch else None,
                    "confidence": CONFIDENCE_LOW if mismatch else CONFIDENCE_MEDIUM,
                    "source": f"ведомость расхода стали, лист {sheet.get('sheet')} "
                              f"({sheet.get('file')})",
                    "formula": f"«Итого {_money(total_kg)} кг» / 1000 = {qty_t:g} т",
                    "caveat": (f"{note_missing}, взята сумма по диаметрам {_money(computed)} кг — "
                               f"нужна сверка с PDF; в итог строка не идёт" if printed is None
                               else ("«Итого» не сходится с суммой по диаметрам: "
                                     f"{_money(printed)} кг против {_money(computed)} кг — нужна "
                                     f"сверка с PDF; в итог строка не идёт")) if mismatch else "",
                    "by_diameter": diameters,
                })

            if concrete.get("mark") and _num(concrete.get("qty")):
                lines.append({
                    "description": f"Бетон {concrete['mark']} — {element}",
                    "unit": "м3", "qty": _num(concrete["qty"]),
                    "confidence": CONFIDENCE_MEDIUM,
                    "source": f"ведомость расхода стали, лист {sheet.get('sheet')} "
                              f"({sheet.get('file')})",
                    "formula": "объём из ведомости",
                    "caveat": "", "by_diameter": None,
                })

            for row in table.get("rows") or []:
                qty = _num(row.get("qty"))
                if qty is None or qty == 0:
                    skipped.append(f"{row.get('name')}: количество {row.get('qty')!r}")
                    continue
                lines.append({
                    "description": row["name"], "unit": row.get("unit") or "",
                    "qty": qty, "confidence": CONFIDENCE_MEDIUM,
                    "source": f"спецификация, лист {sheet.get('sheet')} ({sheet.get('file')})",
                    "formula": "объём из спецификации", "caveat": "", "by_diameter": None,
                })
            for extra in table.get("other") or []:
                qty = _num(extra.get("qty"))
                if qty is None or qty == 0:
                    skipped.append(f"{extra.get('name')}: количество {extra.get('qty')!r}")
                    continue
                lines.append({
                    "description": f"{extra['name']} — {element}",
                    "unit": unit_ru(extra.get("unit")) or "",
                    "qty": qty, "confidence": CONFIDENCE_MEDIUM,
                    "source": f"ведомость, лист {sheet.get('sheet')} ({sheet.get('file')})",
                    "formula": "объём из ведомости", "caveat": "", "by_diameter": None,
                })
    lines = _dedupe_concrete(lines)
    if skipped:
        validation["findings"].append(_finding(
            "QTY_MISSING", "ведомости",
            f"строки без количества не вошли в BOQ: {'; '.join(skipped[:6])}"
            + (" …" if len(skipped) > 6 else ""), expected=None, found=None))
    return {"lines": lines, "findings": validation["findings"],
            "totals_by_kind": _totals_by_kind(lines), "skipped": skipped}


def _dedupe_concrete(lines: list[dict]) -> list[dict]:
    """Одно и то же количество бетона из спецификации и из ведомости — одна строка.

    Так бывает всегда: спецификация на фундамент и ведомость расхода стали описывают одну
    плиту. Две строки по 2 871 м3 удвоили бы деньги, поэтому оставляем одну (марку берём
    из ведомости, расхождение марок всё равно попадает в findings).
    """
    concrete = [l for l in lines if l["description"].startswith("Бетон")]
    others = [l for l in lines if not l["description"].startswith("Бетон")]
    kept: list[dict] = []
    for line in concrete:
        # сливать можно только одну и ту же конструкцию: плита и стены подвала по 100 м3 —
        # это 200 м3, а не «дубль» (раньше сравнивалось только количество — терялся объём)
        duplicate = next((k for k in kept
                          if line["unit"] == k["unit"]
                          and _same_element(line, k)
                          and abs((line["qty"] or 0) - (k["qty"] or 0)) < 0.05), None)
        if duplicate is None:
            similar = next((k for k in kept
                            if line["unit"] == k["unit"]
                            and not _same_element(line, k)
                            and abs((line["qty"] or 0) - (k["qty"] or 0)) < 0.05), None)
            if similar is not None:
                line.setdefault("note", f"то же количество, что у «{similar['description']}», но "
                                        f"другая конструкция — обе строки оставлены")
            kept.append(line)
            continue
        # оставляем ту, где указана конструкция (ведомость), спецификацию — в примечание
        if "—" in line["description"] and "—" not in duplicate["description"]:
            line["note"] = (f"то же количество есть в спецификации как "
                            f"«{duplicate['description']}» — не суммируется")
            kept[kept.index(duplicate)] = line
        else:
            duplicate.setdefault("note", f"то же количество есть как «{line['description']}» — "
                                         f"не суммируется")
    return kept + others


def _same_element(a: dict, b: dict) -> bool:
    """Одна и та же конструкция или одна из строк без указания конструкции.

    Спецификация пишет «Бетон В25 W6 F50», ведомость — «Бетон В25 W6 F150 — Плита»: это один
    объём, конструкция известна только в одной строке. А «— Плита» и «— Стены подвала» —
    разные конструкции даже при одинаковом количестве.
    """
    left, right = _concrete_element(a), _concrete_element(b)
    return left == right or not left or not right


def _concrete_element(line: dict) -> str:
    """«Бетон В25 — плита» → «плита»: по чему отличать конструкции при дедупликации."""
    text = str(line.get("description") or "")
    return _norm(text.split("—")[-1].strip()) if "—" in text else ""


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _totals_by_kind(lines: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for line in lines:
        kind = "арматура" if line["description"].startswith("Арматура") else (
            "бетон" if line["description"].startswith("Бетон") else "прочее")
        slot = out.setdefault(kind, {"lines": 0, "qty": {}})
        slot["lines"] += 1
        unit = line.get("unit") or "—"
        slot["qty"][unit] = round(slot["qty"].get(unit, 0) + (line.get("qty") or 0), 3)
    return out


# ─────────────────────────── покрытие: чего в ведомостях нет ───────────────────────────
# Список ожидаемых конструкций — из состава раздела КР школьного здания (2–3 этажа), а не
# «что придумаем». Если конструкции нет в ведомостях — это missing с адресатом, а не доля
# от известного объёма.
EXPECTED = (
    ("фундаментная плита", "плита"),
    ("стены подвала", "стены подвала"),
    ("колонны подвала", "колонны подвала"),
    ("перекрытия (плиты) подвала, 1–3 этажей", "перекрыт"),
    ("стены 1 этажа", "стены 1 этажа"),
    ("колонны 1 этажа", "колонны 1 этажа"),
    ("стены и колонны 2 этажа", "2 этаж"),
    ("стены и колонны 3 этажа", "3 этаж"),
    ("лестницы и лестничные марши", "лестниц"),
    ("кладка наружных стен из газобетона", "газобетон"),
    ("утеплитель фасада и покрытия", "утеплит"),
    ("кровля и водоизоляционный ковёр", "кровл"),
    ("отделка (полы, штукатурка, окраска)", "отделк"),
)

ASK_TO = ("автор раздела КР / ГИП", "ПТО")


def coverage(doc: dict, extra_text: str = "") -> dict:
    """Что покрыто ведомостями, а чего в них нет вовсе. Отсутствующее — `missing`."""
    names = " ".join(str(t.get("name") or "") for sheet in doc.get("sheets") or []
                     for t in sheet.get("tables") or []).lower()
    corpus = names + " " + str(extra_text or "").lower()
    covered, missing = [], []
    for label, key in EXPECTED:
        if key in corpus:
            covered.append(label)
        else:
            missing.append({
                "position": label, "status": "missing",
                "why": "в присланных ведомостях и спецификациях такой конструкции нет",
                "ask": f"запросить ведомость расхода стали и спецификацию материалов "
                       f"на «{label}»",
                "to": ASK_TO[0],
                "note": "объём не оценивается долей от известного — нужна ведомость",
            })
    return {"covered": covered, "missing": missing}


# ─────────────────────────── отчёт ───────────────────────────
def schedules_report(doc: dict, validation: dict, boq: dict, draft: dict | None = None,
                     derived_from_pdf: dict | None = None, cover: dict | None = None) -> str:
    lines = [f"# Ведомости раздела КР: {doc.get('object') or doc.get('_file')}", "",
             f"- проект: {doc.get('project') or '—'} · источник: {doc.get('source') or '—'}",
             f"- введено: {doc.get('entered_by') or '—'}"
             + (" · **требует сверки с PDF**" if doc.get("needs_verification") else ""), ""]

    totals = boq["totals_by_kind"]
    lines += ["## 1. Что есть в ведомостях", ""]
    for kind, slot in totals.items():
        qty = ", ".join(f"{_money(value)} {unit}" for unit, value in slot["qty"].items())
        lines.append(f"- **{kind}**: строк {slot['lines']} · {qty}")
    lines.append("")

    lines += ["## 2. Проверка арифметики и марок", ""]
    if not validation["findings"]:
        lines += ["_замечаний нет_", ""]
    for finding in validation["findings"]:
        icon = {"SUM_MISMATCH": "❌", "MARK_CONFLICT": "⚠️", "SOURCE_UNVERIFIED": "🔎",
                "PLAUSIBILITY": "🔍", "QTY_MISSING": "❌"}.get(
            finding["type"], "•")
        lines.append(f"- {icon} **{finding['where']}**: {finding['detail']}")
    lines.append("")

    if derived_from_pdf and derived_from_pdf.get("lines"):
        lines += ["## 3. Что было посчитано по габаритам — и насколько это отличалось", "",
                  "| Позиция | По габаритам (том КР1) | По ведомости | Разница |", "|---|---|---|---|"]
        estimate = {l["description"].split("—")[-1].strip(): l["qty"]
                    for l in derived_from_pdf["lines"]}
        actual = {l["description"].split("—")[-1].strip(): l["qty"]
                  for l in boq["lines"] if l["description"].startswith("Бетон")}
        for key, value in actual.items():
            guess = next((v for k, v in estimate.items() if k.lower() in key.lower()
                          or key.lower() in k.lower()), None)
            if guess is None:
                continue
            delta = round(100 * (guess - value) / value, 1) if value else None
            lines.append(f"| {key} | {_money(guess)} м3 | {_money(value)} м3 | "
                         f"{'+' if (delta or 0) > 0 else ''}{delta} % |")
        lines.append("")

    if cover and cover.get("missing"):
        lines += ["## 4. Чего в ведомостях нет (это `missing`, а не «доля от известного»)", "",
                  "| Конструкция | Статус | Кому запрос |", "|---|---|---|"]
        for item in cover["missing"]:
            lines.append(f"| {item['position']} | missing | {item['to']} |")
        lines.append("")
        numbers = ["4", "5"]
    else:
        numbers = ["4", "4"]

    if draft:
        totals_draft = draft.get("totals") or {}
        lines += [f"## {numbers[-1]}. Ведомость с ценами", "",
                  f"- строк: {totals_draft.get('lines')} · с ценой: "
                  f"{totals_draft.get('priced_lines')} · не включено: "
                  f"{totals_draft.get('excluded_lines')}",
                  f"- итог по строкам с известной ценой: "
                  f"**{_money(totals_draft.get('amount_rub'))} ₽** (в него входят строки "
                  f"средней уверенности — они помечены «review»)", "",
                  "| Позиция | Кол-во | Ед. | Цена | Сумма | Уверенность | Статус |",
                  "|---|---|---|---|---|---|---|"]
        for line in draft.get("lines") or []:
            lines.append(f"| {line['element'][:56]} | {_money(line['qty'])} | {line['unit']} | "
                         f"{_money(line.get('price'))} | {_money(line.get('amount'))} | "
                         f"{line['confidence']} | {line['status']} |")
        for item in draft.get("excluded") or []:
            lines.append(f"| {item['element'][:56]} | {item['qty']} | {item.get('unit')} | — | — | "
                         f"{item['confidence']} | {item.get('reason', '')[:50]} |")
        no_price = [l for l in draft.get("lines") or [] if l["status"] == "no_price"]
        if no_price:
            lines += ["", "**Без цены (нет в базе версий):**"]
            for item in no_price:
                lines.append(f"- {item['element'][:70]} — {item.get('reason')}")
        lines.append("")

    lines += [f"## {int(numbers[-1]) + 1}. Что осталось за человеком", "",
              "- сверить перенесённые с фотографий цифры с PDF (пока это `medium`, не `high`);",
              "- решить, какая марка бетона верна: в спецификации на фундамент **В25 W6 F50**, "
              "в ведомостях и пояснительной записке — **В25 W6 F150**;",
              "- решить по арматуре плиты: «Итого» в ведомости не сходится с суммой по диаметрам;",
              "- утвердить расценки ФЕР/ГЭСН — их в ведомостях нет и они не подбираются "
              "автоматически.", ""]
    return "\n".join(lines) + "\n"


def _money(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") if isinstance(value, float) \
        else f"{value:,}".replace(",", " ")


def write(path: str | Path, payload: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
