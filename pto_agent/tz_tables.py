# -*- coding: utf-8 -*-
"""Таблицы и перечни технического задания (ТЗ) → выгрузка объёмов (BOQ).

Зачем
-----
`kr_pdf` написан под раздел КР: он ищет ведомости объёмов, расхода стали и перекрытия.
На ТЗ по слаботочным системам (СКС, АСУ, ОПС) такого не бывает — объёмы лежат в таблицах
маршрутов кабелей, таблице шкафов и в перечнях «наименование – N шт.». На прогоне ТЗ
«Адлерская ТЭС» (240203-11-СКС) `takeoff pdf` дал **0 объёмов** и спросил про арматуру.

Этот модуль закрывает ровно эту дырку: читает таблицы и перечни ТЗ и отдаёт BOQ, который
дальше идёт по обычному стыку `takeoff.build_draft` (объёмы → цены → черновик).

Три правила
-----------
1. **У каждой строки есть адрес.** Страница + строка текста + пункт ТЗ + формула, если объём
   получен расчётом. Нет адреса — нет строки.
2. **Нераспознанное не выбрасывается.** Всё, что не удалось разобрать, попадает в `unparsed`
   с текстом и причиной; всё, где в ТЗ нет количества, — в `lines` с `confidence=low` и
   причиной (дальше `takeoff` сам уводит их в «не включено»).
3. **Итоги в ТЗ проверяются, а не принимаются на веру.** Строки «Итого 8270 м / 16»,
   «Распределение по прокладке» и оговорки вида «исправлено с 5 до 6» дают записи в `checks`:
   сходится / расходится, с формулой.

Модуль не сметчик и не takeoff-движок: он не обмеряет чертежи и не ставит расценки.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .kr_pdf import read_pdf_text
from .numeric import parse_number
from .takeoff import CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_MEDIUM

# ─────────────────────────── словари ───────────────────────────
UNIT_MAP = {
    "шт": "шт", "шт.": "шт", "штук": "шт", "компл": "компл", "компл.": "компл",
    "м": "м", "м.": "м", "м2": "м2", "м²": "м2", "м3": "м3", "м³": "м3",
    "порт": "порт", "порта": "порт", "портов": "порт",
    "конец": "конец", "конца": "конец", "концов": "конец",
    "волокно": "волокно", "волокон": "волокно", "волокна": "волокно",
    "участок": "участок", "участках": "участок", "участков": "участок",
    "сварка": "сварка", "сварок": "сварка",
    "измерение": "измерение", "измерений": "измерение",
    "линия": "линия", "линий": "линия",
}

NUM = r"\d+(?:[.,]\d+)?"
HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+([А-ЯЁA-Z«].{2,})$")
TABLE_HEADERS = {
    "cabinet": re.compile(r"^\s*Шкаф\s+Место установки", re.I),
    "routes": re.compile(r"^\s*Маршрут\s+(Длина|Марка)", re.I),
    "distribution": re.compile(r"^\s*Распределение по прокладке", re.I),
}
TOTAL_RE = re.compile(r"^\s*Итого", re.I)
BULLET_RE = re.compile(r"^\s*[·•]")
DOC_ITEM_RE = re.compile(r"^\s*[-–—]\s+(?P<text>[А-ЯЁA-Zа-яё].{5,})$")
QTY_TAIL_RE = re.compile(rf"^(?P<head>.*?)\s+(?P<qty>{NUM})\s+(?P<count>\d+)\s*\.?$")
BULLET_QTY_RE = re.compile(
    rf"(?P<name>[^·•;–—]{{2,}}?)\s*[–—]\s*(?P<qty>{NUM})\s*"
    r"(?P<unit>шт\.?|компл\.?|м³|м3|м²|м2|м\.?|порт(?:а|ов)?|волокон|волокна)(?![а-я])",
    re.I)
PLACE_RE = re.compile(r"(?<![А-Яа-яЁёA-Za-z.])(?:ГК|КРУЭ|ЦТП|АБК|ОВК|КПП\d?|Склад)\s*,.*$")
CABLE_MARK_RE = re.compile(r"((?:ВВГЭ|ВВГ|ТОД|NYM|КСПВ|FTP|UTP)[^\s,]*)", re.I)


def unit_ru(unit: str | None) -> str | None:
    if not unit:
        return None
    return UNIT_MAP.get(str(unit).strip().lower().replace("ё", "е"))


def _num(text: str | None) -> float | None:
    return parse_number(text)


def fmt(value) -> str:
    """«45.8» → «45,8»: в документах ПТО десятичный разделитель — запятая."""
    if value is None:
        return "—"
    text = f"{float(value):g}"
    return text.replace(".", ",")


def _numbers(text: str) -> list[float | None]:
    """Числа строки с учётом разрядного пробела: «Итого 8 270 м» → [8270, ...]."""
    glued = re.sub(r"(?<=\d)[\s\u00a0](?=\d{3}(?:\D|$))", "", str(text or ""))
    return [_num(n) for n in re.findall(NUM, glued)]


def _clean(text: str) -> str:
    return " ".join(str(text or "").replace("\u00a0", " ").split())


# ─────────────────────────── чтение страниц ───────────────────────────
def read_lines(doc: dict) -> list[dict]:
    """Плоский список строк с адресом «страница:строка»."""
    out: list[dict] = []
    for page in doc.get("pages") or []:
        for index, raw in enumerate(str(page.get("text") or "").split("\n")):
            out.append({"page": page.get("page"), "line": index, "text": _clean(raw),
                        "raw": raw})
    return out


def _address(item: dict) -> str:
    return f"стр. {item['page']}, строка {item['line'] + 1}"


# ─────────────────────────── разбор таблиц ───────────────────────────
def parse_cabinet_table(block: list[dict]) -> tuple[list[dict], list[str]]:
    """Таблица «Шкаф / Место установки / Типоразмер / Кол-во».

    В текстовом слое строка таблицы рвётся на 2–4 строки, поэтому блок склеивается и
    режется по началу «ШТ.» — это устойчивее, чем гадать по номерам строк.
    """
    rows: list[dict] = []
    problems: list[str] = []
    if not block:
        return rows, problems
    joined = " ".join(item["text"] for item in block if item["text"])
    head_text = re.split(r"(?=ШТ\.)", joined)[0].strip()
    if len(head_text) > 25:
        problems.append(f"{_address(block[0])}: до первой строки шкафа стоит текст, "
                        f"не похожий на строку таблицы: «{head_text[:60]}»")
    for chunk in re.split(r"(?=ШТ\.)", joined):
        chunk = chunk.strip()
        if not chunk.startswith("ШТ."):
            continue
        address = _address(block[0])
        type_probe = re.search(r"(\d{2}U)\s*(напольный|настенный)?|(настенный|напольный)", chunk)
        tail = chunk[type_probe.end():] if type_probe else chunk
        qty_match = re.search(r"(?<![A-Za-zА-Яа-я0-9])(\d{1,4})(?![0-9A-Za-zА-Яа-я])", tail)
        qty = int(qty_match.group(1)) if qty_match else None
        body = chunk[:type_probe.end() + qty_match.start()].strip() if (type_probe and qty_match) \
            else chunk
        type_match = re.search(r"(\d{2}U)\s*(напольный|настенный)?", body)
        wall_match = re.search(r"(настенный|напольный)", body)
        if type_match:
            cabinet_type = f"{type_match.group(1)} {type_match.group(2) or ''}".strip()
            head = body[:type_match.start()].strip()
        elif wall_match:
            cabinet_type = wall_match.group(1)
            head = body[:wall_match.start()].strip()
        else:
            cabinet_type, head = None, body
        head = re.sub(r"^ШТ\.?\s*", "", head)
        # место установки начинается с ключевого слова точки и запятой после него
        marker = PLACE_RE.search(head)
        if marker:
            name = head[:marker.start()].strip()
            place = head[marker.start():].strip()
        else:
            name, place = head, ""
        name = _clean(name).rstrip(".") or _clean(head)
        if qty is None:
            problems.append(f"{address}: в строке шкафа «{chunk[:60]}» не найдено количество")
        rows.append({"description": f"Установка и закрепление шкафа ШТ. {name}",
                     "unit": "шт", "qty": qty,
                     "confidence": CONFIDENCE_HIGH if qty is not None else CONFIDENCE_LOW,
                     "source": address, "detail": {"место": place, "типоразмер": cabinet_type},
                     "formula": "строка таблицы п. 5.2 ТЗ",
                     "raw": chunk[:120]})
    return rows, problems


def cable_context(lines: list[dict], header_index: int) -> tuple[bool, str]:
    """Подзаголовок перед таблицей: «Оптические кабели (ТОД-нг(А)-HF-8У-8кН, 8 волокон)»."""
    for offset in range(1, 4):
        position = header_index - offset
        if position < 0:
            break
        text = lines[position]["text"]
        if not text:
            continue
        is_opt = bool(re.search(r"оптическ", text, re.I))
        mark = re.search(r"([А-ЯA-Z]{2,}[А-Яа-яA-Za-z0-9().\-]*нг[А-Яа-яA-Za-z0-9().\-]*|"
                         r"ТОД[А-Яа-яA-Za-z0-9().\-]*|ВВГЭ[А-Яа-яA-Za-z0-9().,×\-]*\d)", text)
        if is_opt or re.search(r"силов", text, re.I) or mark:
            return is_opt, (mark.group(1) if mark else ("оптический кабель" if is_opt
                                                       else "силовой кабель"))
    return False, "силовой кабель"


def parse_route_table(block: list[dict], *, mark: str = "", is_opt: bool = False) -> tuple[list[dict], list[dict], list[str]]:
    """Таблица «Маршрут / (Марка) / Длина, м / Кол-во линий».

    Строка закрывается там, где в конце стоят два числа: длина и количество линий.
    Маршрут может занимать несколько строк (силовые кабели) — они накапливаются.
    """
    rows: list[dict] = []
    totals: list[dict] = []
    problems: list[str] = []
    buffer: list[str] = []
    for item in block:
        text = item["text"]
        if not text:
            continue
        if TOTAL_RE.match(text):
            numbers = _numbers(text)
            if numbers:
                totals.append({"raw": text, "numbers": numbers, "address": _address(item)})
            buffer = []
            continue
        match = QTY_TAIL_RE.match(text)
        if not match:
            buffer.append(text)
            continue
        head = match.group("head")
        length, count = _num(match.group("qty")), int(match.group("count"))
        mark_match = CABLE_MARK_RE.search(head)
        row_mark = mark_match.group(1) if mark_match else mark
        route_text = " ".join(buffer + [head[:mark_match.start()] if mark_match else head])
        route_text = _clean(route_text).rstrip(",;")
        buffer = []
        if not route_text or length is None:
            problems.append(f"{_address(item)}: строка маршрута не разобрана: «{text[:60]}»")
            continue
        rows.append({
            "description": f"Прокладка {'оптического' if is_opt else 'силового'} "
                           f"кабеля {row_mark}: {route_text}",
            "unit": "м", "qty": round(length * count, 3),
            "confidence": CONFIDENCE_HIGH,
            "source": _address(item),
            "detail": {"маршрут": route_text, "длина линии, м": length, "линий": count,
                       "марка": row_mark},
            "formula": f"{length:g} м × {count} лин. = {length * count:g} м",
            "raw": text[:120],
        })
    if buffer:
        problems.append(f"в таблице маршрутов не разобраны строки: «{' / '.join(buffer)[:100]}» "
                        f"— нет закрывающей пары «длина + количество линий»")
    return rows, totals, problems


def parse_distribution(block: list[dict]) -> list[dict]:
    """Строки «Опт. 8270 6970 160 160 120 100 160 240» → для сверки итога."""
    out = []
    for item in block:
        numbers = [_num(n) for n in re.findall(NUM, item["text"])]
        if len(numbers) >= 3:
            out.append({"raw": item["text"], "numbers": numbers,
                        "address": _address(item)})
    return out


# ─────────────────────────── перечни «– N ед.» ───────────────────────────
BULLET_ITEM_RE = re.compile(
    rf"^(?P<head>.*?)\s*[–—]\s*(?P<qty>{NUM})\s*"
    r"(?P<unit>шт\.?|компл\.?|м³|м3|м²|м2|м\.?|порт(?:а|ов)?|волокон|волокна)(?![а-я])",
    re.I)


def parse_bullet_quantities(text: str) -> list[dict]:
    """«Патч-корды: F/UTP Cat.5e 1м – 48 шт., LC-LC 1м – 2 шт.» → две позиции.

    Внутри одного пункта перечисление через запятую: у второго и следующих элементов
    название идёт без группы, поэтому группа («Патч-корды:») подставляется из первого.
    """
    items: list[dict] = []
    prefix = ""
    for segment in re.split(r",\s+", _clean(text)):
        match = BULLET_ITEM_RE.match(segment.strip(" .;·•"))
        if not match:
            continue
        head = match.group("head").strip(" :;")
        if ":" in head:
            group, _, rest = head.partition(":")
            prefix = _clean(group)
            rest = _clean(rest)
            name = f"{prefix}: {rest}" if rest else prefix
        else:
            name = _clean(head)
            if prefix and name:
                name = f"{prefix}: {name}"
        name = _clean(name).strip(" ,;:")
        qty = _num(match.group("qty"))
        if not name or qty is None:
            continue
        items.append({"description": name, "qty": qty, "unit": unit_ru(match.group("unit")),
                      "raw": match.group(0).strip()})
    return items


def collect_bullets(lines: list[dict], start: int) -> tuple[list[dict], int]:
    """Собрать пункты перечня с учётом переносов строк (продолжение без «·»)."""
    items: list[dict] = []
    index = start
    current: dict | None = None
    while index < len(lines):
        text = lines[index]["text"]
        if not text:
            index += 1
            continue
        if HEADING_RE.match(text) or TABLE_HEADERS["cabinet"].match(text):
            break
        if BULLET_RE.match(text):
            if current:
                items.append(current)
            current = {"text": text.lstrip("·• ").strip(),
                       "address": _address(lines[index]), "page": lines[index]["page"],
                       "line": lines[index]["line"]}
        elif current is not None:
            current["text"] = f"{current['text']} {text}"
        else:
            break
        index += 1
    if current:
        items.append(current)
    return items, index


# ─────────────────────────── разбор документа ───────────────────────────
def extract(doc: dict) -> dict:
    """Таблицы и перечни ТЗ → BOQ + проверки + нерешённое."""
    lines = read_lines(doc)
    boq: list[dict] = []
    checks: list[dict] = []
    unparsed: list[dict] = []
    prose: dict[str, list[dict]] = {}     # пункт ТЗ → пункты перечня (для правил по пункту)
    caveats: list[dict] = []
    tables: dict[str, int] = {}

    heading = {"clause": "", "title": ""}
    index = 0
    while index < len(lines):
        text = lines[index]["text"]
        if not text:
            index += 1
            continue
        heading_match = HEADING_RE.match(text)
        if heading_match:
            heading = {"clause": heading_match.group(1), "title": heading_match.group(2).strip()}
            index += 1
            continue

        kind = next((name for name, pattern in TABLE_HEADERS.items() if pattern.match(text)), None)
        if kind:
            header_index = index
            block, index = _collect_block(lines, index + 1)
            tables[kind] = tables.get(kind, 0) + 1
            if kind == "cabinet":
                rows, problems = parse_cabinet_table(block)
                for row in rows:
                    row.update({"clause": heading["clause"], "trade": heading["title"]})
                boq += rows
                unparsed += [{"address": "—", "text": p, "why": "таблица шкафов"} for p in problems]
                declared = _declared_cabinets(lines)
                if declared is not None and rows:
                    ok = declared == len(rows)
                    checks.append({
                        "name": f"п. {heading['clause']}: «Установить и закрепить {declared} новых шкафов»",
                        "formula": f"строк в таблице шкафов: {len(rows)}, заявлено: {declared}",
                        "result": "сходится" if ok else "расходится",
                        "note": "" if ok else "сверить таблицу с текстом ТЗ"})
            elif kind == "routes":
                is_opt, mark = cable_context(lines, header_index)
                rows, totals, problems = parse_route_table(block, mark=mark, is_opt=is_opt)
                for row in rows:
                    row.update({"clause": heading["clause"], "trade": heading["title"]})
                boq += rows
                unparsed += [{"address": "—", "text": p, "why": "таблица маршрутов"}
                             for p in problems]
                if rows and totals:
                    checks.append(_route_total_check(rows, totals[-1], heading))
            else:
                for row in parse_distribution(block):
                    checks.append(_distribution_check(row, heading))
            continue

        if BULLET_RE.match(text):
            items, index = collect_bullets(lines, index)
            for item in items:
                item["clause"] = heading["clause"]
                item["trade"] = heading["title"]
            prose.setdefault(heading["clause"], []).extend(items)
            index = max(index, 1)
            continue

        doc_match = DOC_ITEM_RE.match(text)
        if doc_match and heading["clause"].startswith("6"):
            body = doc_match.group("text").strip()
            cursor = index + 1
            while cursor < len(lines):
                nxt = lines[cursor]["text"]
                if not nxt or nxt.startswith(("-", "–", "—")) or HEADING_RE.match(nxt):
                    break
                body = f"{body} {nxt}".strip()
                cursor += 1
            prose.setdefault(heading["clause"], []).append(
                {"text": body.rstrip(";"), "address": _address(lines[index]),
                 "page": lines[index]["page"], "line": lines[index]["line"],
                 "clause": heading["clause"], "trade": heading["title"]})
            index = cursor
            continue
        index += 1

    # ── правила по пунктам: перечни, где количество есть, и проза, где его нет ──
    boq += _bullets_to_boq(prose, caveats)
    derived, caveats2 = _derive_prose(prose, lines, boq)
    boq += derived
    caveats += caveats2
    boq += _documents_to_boq(prose)

    checks += _cross_checks(boq, lines)

    return {
        "schema": "pto-agent/tz-tables@1",
        "file": doc.get("file"),
        "pages_total": doc.get("pages_total"),
        "pages_with_text": doc.get("pages_with_text"),
        "tables_found": tables,
        "lines": boq,
        "not_estimated": caveats,
        "checks": checks,
        "unparsed": unparsed,
        "totals": {
            "lines": len(boq),
            "high": len([l for l in boq if l["confidence"] == CONFIDENCE_HIGH]),
            "medium": len([l for l in boq if l["confidence"] == CONFIDENCE_MEDIUM]),
            "low": len([l for l in boq if l["confidence"] == CONFIDENCE_LOW]),
            "not_estimated": len(caveats),
            "unparsed": len(unparsed),
        },
    }


def _collect_block(lines: list[dict], start: int) -> tuple[list[dict], int]:
    """Строки таблицы до следующего заголовка пункта/новой таблицы."""
    block: list[dict] = []
    index = start
    while index < len(lines):
        text = lines[index]["text"]
        if HEADING_RE.match(text) or any(p.match(text) for p in TABLE_HEADERS.values()):
            break
        # подзаголовок вида «Силовые кабели:» закрывает предыдущую таблицу
        if text.rstrip().endswith(":") and len(text) < 60:
            break
        # проза после таблицы: длинное предложение, которое не является строкой таблицы
        if len(text) > 45 and text.rstrip().endswith((".", ":")) and not TOTAL_RE.match(text) \
                and not re.match(r"^\s*[·•-]", text):
            break
        if text:
            block.append(lines[index])
        index += 1
    return block, index


def _declared_cabinets(lines: list[dict]) -> int | None:
    for item in lines:
        match = re.search(r"закрепить\s+(\d+)\s+нов", item["text"], re.I)
        if match:
            return int(match.group(1))
    return None


def _route_total_check(rows: list[dict], total: dict, heading: dict) -> dict:
    numbers = total["numbers"]
    declared_length = numbers[0] if numbers else None
    declared_count = numbers[1] if len(numbers) > 1 else None
    actual_length = round(sum(r["qty"] for r in rows), 3)
    actual_count = sum(int(r["detail"]["линий"]) for r in rows)
    parts = [f"по строкам: {actual_length:g} м / {actual_count} лин.",
             f"«Итого» в ТЗ: {declared_length:g} м / {declared_count} лин."
             if declared_length is not None else "«Итого» без длины"]
    ok = (declared_length is not None and abs(actual_length - declared_length) < 0.01
          and declared_count == actual_count)
    return {"name": f"п. {heading['clause']}: итог таблицы маршрутов ({heading['title']})",
            "formula": "; ".join(parts),
            "result": "сходится" if ok else "расходится",
            "note": "" if ok else "разница в итоге — уточнить у проектировщика"}


def _distribution_check(distribution: dict, heading: dict) -> dict:
    numbers = [n for n in distribution["numbers"] if n is not None]
    if len(numbers) < 3:
        return {"name": f"п. {heading['clause']}: распределение по прокладке",
                "formula": distribution["raw"][:80], "result": "не проверено",
                "note": "слишком мало чисел в строке"}
    total, parts = numbers[0], numbers[1:]
    summary = sum(parts)
    ok = abs(summary - total) < 0.01
    return {"name": f"п. {heading['clause']}: сумма способов прокладки = общая длина",
            "formula": f"{' + '.join(f'{p:g}' for p in parts)} = {summary:g} против "
                       f"общей длины {total:g} ({distribution['address']})",
            "result": "сходится" if ok else "расходится",
            "note": "" if ok else f"расхождение {abs(total - summary):g} — сумма способов "
                                  f"прокладки не равна итогу; объём принят по итогу таблицы"}


DERIVED_CLAUSES = {"5.4", "5.7", "5.8", "5.9"}


def _bullets_to_boq(prose: dict[str, list[dict]], caveats: list[dict]) -> list[dict]:
    """Перечни с явным количеством «наименование – N ед.»."""
    out: list[dict] = []
    for clause, items in prose.items():
        if not clause.startswith("5") or clause in DERIVED_CLAUSES:
            continue
        for item, owner in with_owner(items):
            parsed = parse_bullet_quantities(item["text"])
            if parsed:
                for part in parsed:
                    tag = f"{clause} · {owner}" if owner else clause
                    out.append({
                        "description": f"[п. {tag}] {part['description']}",
                        "unit": part["unit"] or "шт", "qty": part["qty"],
                        "confidence": CONFIDENCE_HIGH, "clause": clause,
                        "trade": item.get("trade") or "",
                        "source": item["address"] + f" ({item['page']}:{item['line'] + 1})",
                        "formula": "количество прямо в перечне ТЗ",
                        "raw": part["raw"][:120],
                    })
            elif _is_work_item(item["text"]):
                caveats.append({
                    "description": f"[п. {clause}] {_clean(item['text']).rstrip('.')}",
                    "clause": clause, "source": item["address"],
                    "why": "объём в ТЗ не нормирован: работа названа, количество не задано",
                    "raw": item["text"][:120]})
    return out


def with_owner(items: list[dict]):
    """Идти по перечню, помня текущий подзаголовок («АБК (00CYA04, 42U):»)."""
    owner = ""
    for item in items:
        text = _clean(item["text"])
        if text.rstrip().endswith(":"):
            owner = re.sub(r"\s*\(.*?\)\s*:?$", "", text).strip() or text.rstrip(":").strip()
            yield item, owner
            continue
        yield item, owner


def _is_work_item(text: str) -> bool:
    text = _clean(text)
    if len(text) <= 8 or re.match(r"^(В каждом|Остальные)", text):
        return False
    if text.rstrip().endswith(":"):          # «АБК (00CYA04, 42U):» — подзаголовок перечня
        return False
    if re.match(r"^(Переподключение|Доукомплектация)", text):
        return False                          # разбираются отдельными правилами объёма
    return True


def _derive_prose(prose: dict[str, list[dict]], lines: list[dict],
                  boq: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Объёмы, которые в ТЗ заданы прозой: формулы, а не догадки."""
    out: list[dict] = []
    caveats: list[dict] = []
    all_text = " ".join(item["text"] for items in prose.values() for item in items)

    def add(clause, description, unit, qty, confidence, formula, source, trade=""):
        out.append({"description": f"[п. {clause}] {description}", "unit": unit, "qty": qty,
                    "confidence": confidence, "clause": clause, "trade": trade,
                    "source": source, "formula": formula,
                    "raw": formula[:120]})

    # 5.4 земляные работы
    trench = " ".join(i["text"] for i in prose.get("5.4") or [])
    length = re.search(rf"траншеи длиной\s+({NUM})\s*м", trench)
    volume = re.search(rf"объ[её]м выемки\s+({NUM})\s*м3|объ[её]м выемки\s+({NUM})\s*м³", trench)
    width = re.search(rf"ширин[а-я]*\s*~?({NUM})\s*м", trench)
    depth = re.search(rf"глубин[а-я]*\s*~?({NUM})\s*м", trench)
    address = (prose.get("5.4") or [{}])[0].get("address", "—")
    if length and volume:
        l_val = _num(length.group(1))
        v_val = _num(volume.group(1) or volume.group(2))
        w_val = _num(width.group(1)) if width else None
        d_val = _num(depth.group(1)) if depth else None
        add("5.4", "Разработка траншеи — объём выемки", "м3", v_val, CONFIDENCE_MEDIUM,
            f"в ТЗ «{fmt(v_val)} м³»; проверка {fmt(l_val)} × {fmt(w_val)} × {fmt(d_val)} = "
            f"{fmt(l_val * w_val * d_val)} м³" if (w_val and d_val)
            else f"в ТЗ «{fmt(v_val)} м³»", address)
        add("5.4", "Обратная засыпка с послойным уплотнением", "м3", v_val, CONFIDENCE_MEDIUM,
            "принято по объёму выемки; уточняется вместе с траншеей", address)
        add("5.4", "Планировка трассы", "м", l_val, CONFIDENCE_MEDIUM,
            f"принята длина траншеи {fmt(l_val)} м: площадь планировки в ТЗ не задана", address)
        add("5.4", "Установка сигнальной ленты", "м", l_val, CONFIDENCE_HIGH,
            f"по длине траншеи {fmt(l_val)} м", address)
        if re.search(r"двух\s+ПНД-труб", trench, re.I):
            add("5.4", "Укладка двух ПНД-труб Ø32 мм для оптического кабеля", "м",
                round(l_val * 2, 3), CONFIDENCE_HIGH,
                f"2 трубы × {fmt(l_val)} м = {fmt(l_val * 2)} м", address)
        if re.search(r"при необходимости", trench, re.I):
            caveats.append({"description": "[п. 5.4] Планировка, вывоз излишков грунта",
                            "clause": "5.4", "source": address,
                            "why": "в ТЗ «при необходимости»: объём не задан, верхняя граница — "
                                   f"вся выемка {fmt(v_val)} м³", "raw": trench[:120]})

    # 5.7 разделка и подключение
    text57 = " ".join(i["text"] for i in prose.get("5.7") or [])
    ends = re.search(rf"\(({NUM})\s+конц", text57)
    if ends:
        add("5.7", "Ввод и зачистка оптических кабелей в кроссах", "конец",
            _num(ends.group(1)), CONFIDENCE_HIGH,
            f"в ТЗ «{ends.group(1)} концов»", (prose.get("5.7") or [{}])[0].get("address", "—"))
    shield = re.search(rf"ПТ-10.{{0,80}}?[–—]\s*({NUM})\s+конц", text57)
    shield_ends = _num(shield.group(1)) if shield else 0
    power_lines = sum(int(r["detail"]["линий"]) for r in (boq or [])
                      if r.get("detail", {}).get("марка") and "силов" in r["description"])
    if power_lines:
        tails = power_lines * 2 - shield_ends
        formula = (f"{power_lines} силовые линии × 2 конца − {shield_ends:g} конца в ПТ-10 = "
                   f"{fmt(tails)}; в ТЗ количество прямо не задано")
        if tails > 0:
            add("5.7", "Разделка силовых кабелей, подключение к вводным автоматам/ИБП в шкафах",
                "конец", tails, CONFIDENCE_MEDIUM, formula,
                (prose.get("5.7") or [{}])[0].get("address", "—"))
        else:
            caveats.append({
                "description": "[п. 5.7] Разделка силовых кабелей в шкафах", "clause": "5.7",
                "source": (prose.get("5.7") or [{}])[0].get("address", "—"),
                "why": f"расчёт даёт {fmt(tails)} концов ({formula}) — нулевой объём не "
                       f"становится строкой: уточнить, где разделываются силовые кабели",
                "raw": text57[:120]})
    if shield:
        add("5.7", "Подключение силовых кабелей в щитах ПТ-10", "конец",
            shield_ends, CONFIDENCE_HIGH,
            f"в ТЗ «{shield.group(1)} конца» (работы по наряду-допуску)",
            (prose.get("5.7") or [{}])[0].get("address", "—"))

    # 5.8 сварка
    text58 = " ".join(i["text"] for i in prose.get("5.8") or [])
    weld = re.search(rf"Сварка\s+({NUM})\s+одномодов", text58)
    if weld:
        count = _num(weld.group(1))
        address58 = (prose.get("5.8") or [{}])[0].get("address", "—")
        add("5.8", "Сварка одномодовых волокон (пигтейл — магистральное волокно)", "сварка",
            count, CONFIDENCE_HIGH, f"в ТЗ «{weld.group(1)} волокон»", address58)
        add("5.8", "Монтаж защитных гильз КДЗС", "шт", count, CONFIDENCE_MEDIUM,
            f"1 гильза на сварку: {fmt(count)}", address58)
        add("5.8", "Укладка волокон в сплайс-кассеты", "волокно", count, CONFIDENCE_MEDIUM,
            f"по числу сварок {fmt(count)}; число кассет в ТЗ не задано", address58)

    # 5.9 измерения
    text59 = " ".join(i["text"] for i in prose.get("5.9") or [])
    sections = re.search(rf"на\s+({NUM})\s+оптических участках", text59)
    if sections:
        count = _num(sections.group(1))
        address59 = (prose.get("5.9") or [{}])[0].get("address", "—")
        two_sides = bool(re.search(r"с двух сторон", text59, re.I))
        add("5.9", "Рефлектометрические измерения на оптических участках", "измерение",
            round(count * 2) if two_sides else count, CONFIDENCE_HIGH,
            f"{fmt(count)} участков × 2 стороны" if two_sides else f"{fmt(count)} участков", address59)
        add("5.9", "Измерение затухания оптических линий", "участок", count, CONFIDENCE_HIGH,
            f"по числу участков {fmt(count)}", address59)
        add("5.9", "Паспортизация параметров (протоколы)", "протокол", count, CONFIDENCE_HIGH,
            f"по числу участков {fmt(count)}", address59)

    # 5.6.5 доукомплектация кроссов: «(24 порта)», «двух кроссов (каждый 24 порта)»
    for item, owner in with_owner(prose.get("5.6.5") or []):
        if not re.match(r"^\s*Доукомплектация", _clean(item["text"]), re.I):
            continue
        text = _clean(item["text"])
        ports = re.search(rf"\(?\s*(?:каждый\s+)?({NUM})\s+порт", text)
        two = bool(re.search(r"\bдвух\b", text, re.I))
        if ports:
            one = _num(ports.group(1))
            qty = one * 2 if two else one
            add("5.6.5", f"{owner + ': ' if owner else ''}доукомплектация оптического кросса "
                         f"адаптерами и пигтейлами", "порт",
                qty, CONFIDENCE_HIGH,
                f"«{fmt(one)} порта» × 2 кросса = {fmt(qty)}" if two else f"«{fmt(one)} порта»",
                item["address"], item.get("trade") or "")
        elif two:
            add("5.6.5", f"{owner + ': ' if owner else ''}доукомплектация двух существующих "
                         f"оптических кроссов (адаптеры, пигтейлы)", "порт", 48, CONFIDENCE_MEDIUM,
                "в ТЗ «два кросса» без числа портов: принято 2 × 24 порта по аналогии "
                "с предыдущей строкой — уточнить", item["address"], item.get("trade") or "")

    # 5.6.5 переподключение «~192»
    for item, owner in with_owner(prose.get("5.6.5") or []):
        for match in re.finditer(rf"Переподключение\s*~?\s*({NUM})", item["text"]):
            tag = f"5.6.5 · {owner}" if owner else "5.6.5"
            caveats.append({"description": f"[п. {tag}] Переподключение существующих "
                                           f"патч-кордов — {match.group(1)}",
                            "clause": "5.6.5", "source": item["address"],
                            "why": "количество в ТЗ оценочное («~»): объём не зафиксирован, "
                                   "уточняется по факту",
                            "raw": item["text"][:120]})
    return out, caveats


DOC_UNITS = (
    (r"^комплект", "компл"),
    (r"^пояснительная записка", "книга"),
    (r"^(протокол|протоколы)", "протокол"),
    (r"^(схема|план|чертеж|структурная схема)", "лист"),
    (r"^технический отчет", "отчёт"),
    (r"^(таблица соединений|спецификация)", "док"),
)


def doc_unit(text: str) -> str:
    """Вид документа ИД → единица измерения (книга, лист, протокол, комплект)."""
    for pattern, unit in DOC_UNITS:
        if re.match(pattern, _clean(text), re.I):
            return unit
    return "док"


def _documents_to_boq(prose: dict[str, list[dict]]) -> list[dict]:
    """Пункт 6: перечень документов ИД — по 1 комплекту, «при наличии» → на проверку."""
    out: list[dict] = []
    for item in prose.get("6") or []:
        text = _clean(item["text"]).rstrip(".")
        if not text or len(text) < 10:
            continue
        if re.match(r"^(В рамках|Вся требуемая|Отчетная документация|Заказчик должен)", text):
            continue
        low = CONFIDENCE_LOW if "при наличии" in text.lower() else CONFIDENCE_HIGH
        out.append({
            "description": f"[п. 6] {text}",
            "unit": doc_unit(text), "qty": 1 if low == CONFIDENCE_HIGH else None,
            "confidence": low, "clause": "6", "trade": item.get("trade") or "",
            "source": item["address"], "formula": "перечень ИД из п. 6 ТЗ",
            "raw": text[:120]})
    return out


def _cross_checks(boq: list[dict], lines: list[dict]) -> list[dict]:
    """Сквозные проверки: оговорки в тексте, арифметика волокон, противоречия."""
    checks: list[dict] = []
    # «исправлено с 5 до 6» — оговорка, которая не совпадает с указанным количеством
    for item in lines:
        match = re.search(r"исправлено с\s+(\d+)\s+до\s+(\d+)", item["text"], re.I)
        if not match:
            continue
        stated = re.search(rf"[–-]\s*({NUM})\s*шт", item["text"])
        name = _clean(item["text"]).split("–")[0].lstrip("· ")
        checks.append({
            "name": f"оговорка «исправлено с {match.group(1)} до {match.group(2)}»: {name[:45]}",
            "formula": f"в тексте количество {stated.group(1) if stated else '—'}, "
                       f"оговорка называет {match.group(1)} и {match.group(2)}",
            "result": ("сходится" if stated and stated.group(1) in
                       (match.group(1), match.group(2)) else "расходится"),
            "note": "требуется письменное уточнение проектировщика: оговорка противоречит "
                    "указанному количеству"})
    # волокна: линии × волокон × 2 конца
    routes = [l for l in boq if "Прокладка оптического кабеля" in l["description"]]
    welding = [l for l in boq if l["unit"] == "сварка"]
    if routes and welding:
        lines_count = sum(int(r["detail"]["линий"]) for r in routes)
        fibers = re.search(r"(\d+)\s+волокон", " ".join(l["text"] for l in lines))
        fiber_count = int(fibers.group(1)) if fibers else 8
        expected = lines_count * fiber_count * 2
        actual = welding[0]["qty"]
        checks.append({
            "name": "сварка волокон = линии × волокна × 2 конца",
            "formula": f"{lines_count} лин. × {fiber_count} волокон × 2 = {expected}; "
                       f"в ТЗ {actual:g}",
            "result": "сходится" if abs(expected - float(actual)) < 0.01 else "расходится",
            "note": "" if abs(expected - float(actual)) < 0.01
                    else "количество сварок в ТЗ не бьётся с числом линий"})
    return checks


# ─────────────────────────── выход ───────────────────────────
def to_boq_rows(report: dict) -> list[dict]:
    """Строки отчёта → формат выгрузки объёмов (`takeoff.read_boq`)."""
    rows = []
    for line in report.get("lines") or []:
        rows.append({
            "masterformat_code": "",
            "description": line["description"],
            "unit": line["unit"],
            "qty": line["qty"],
            "confidence": line["confidence"],
            "source": line.get("source") or "",
            "trade": line.get("trade") or "",
        })
    return rows


def write_boq_csv(report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = to_boq_rows(report)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["masterformat_code", "description", "unit",
                                                    "qty", "confidence", "source", "trade"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def report_to_markdown(report: dict) -> str:
    totals = report.get("totals") or {}
    out = [f"# Таблицы ТЗ → объёмы: {report.get('file')}", "",
           f"- страниц: {report.get('pages_total')} · с текстовым слоем: "
           f"{report.get('pages_with_text')}",
           f"- таблиц найдено: " + (", ".join(f"{k} — {v}" for k, v in
                                              (report.get("tables_found") or {}).items()) or "нет"),
           f"- строк: {totals.get('lines')} (высокая {totals.get('high')}, "
           f"средняя {totals.get('medium')}, низкая {totals.get('low')}) · "
           f"«не оценено»: {totals.get('not_estimated')} · не разобрано: "
           f"{totals.get('unparsed')}", "",
           "## Объёмы", "", "| № | Позиция | Кол-во | Ед. | Уверенность | Пункт | Источник |",
           "|---|---|---|---|---|---|---|"]
    for number, line in enumerate(report.get("lines") or [], start=1):
        qty = "—" if line.get("qty") is None else f"{line['qty']:g}"
        out.append(f"| {number} | {line['description'][:70]} | {qty} | {line['unit']} | "
                   f"{line['confidence']} | {line.get('clause') or '—'} | "
                   f"{line.get('source') or '—'} |")
    if report.get("checks"):
        out += ["", "## Проверки цифр ТЗ", "", "| Проверка | Как проверено | Результат | Примечание |",
                "|---|---|---|---|"]
        for check in report["checks"]:
            out.append(f"| {check['name'][:60]} | {check['formula'][:80]} | {check['result']} | "
                       f"{check.get('note') or '—'} |")
    if report.get("not_estimated"):
        out += ["", "## Не оценено (в деньги не превращается)", "",
                "| Позиция | Пункт | Причина |", "|---|---|---|"]
        for item in report["not_estimated"]:
            out.append(f"| {item['description'][:70]} | {item.get('clause')} | {item['why']} |")
    if report.get("unparsed"):
        out += ["", "## Не разобрано", "", "| Текст | Причина |", "|---|---|"]
        for item in report["unparsed"]:
            out.append(f"| {item['text'][:80]} | {item['why']} |")
    out += ["", "_Это объёмы из ТЗ, а не смета: расценки ставит инженер при подтверждении._"]
    return "\n".join(out) + "\n"


def extract_file(path: str | Path, *, cache: str | Path | None = None) -> dict:
    doc = read_pdf_text(path, cache=cache)
    return extract(doc)


def write_report(report: dict, out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tz.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    (out / "tz.md").write_text(report_to_markdown(report), encoding="utf-8")
    write_boq_csv(report, out / "boq.csv")
    return {"json": out / "tz.json", "md": out / "tz.md", "boq": out / "boq.csv"}
