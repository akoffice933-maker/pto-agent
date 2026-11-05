# -*- coding: utf-8 -*-
"""Чтение сметы, выгруженной из АРОС-Лидер (HTML/Markdown-представление документа).

АРОС отдаёт смету как таблицу по форме приложения № 3 к приказу № 421/пр: разделы,
позиции с обоснованием (ГЭСН/ФЕР/ФССЦ), наименованием, единицей измерения и количеством,
а под каждой позицией — строки ресурсов (ОТ, ЭМ, машины, материалы) и отдельные строки
ресурсов ФГИС ЦС с кодами вида `21.1.06.09-0151`.

Два важных следствия, из-за которых этот парсер вообще нужен:

* **Позиция с кодом ресурса — это материал, а не работа.** «Кабель силовой ВВГнг(A)-LS
  3х1,5» в смете идёт отдельной строкой, и если положить её в акт как работу, в АОСР
  появится «прокладка кабеля», которой в смете нет, а объём задвоится. Поэтому позиции
  делятся на `work` (обоснование — сметная норма) и `material` (код ресурса ФГИС ЦС);
  материалы идут в перечень материалов, работы — в акты.
* **Наименование работы часто без глагола.** «Светильник светодиодный линейный
  встраиваемый…», «Прибор или аппарат», «Провод в коробах» — это работы, названные по
  предмету. Классификатор должен понимать такие строки, иначе половина сметы «не
  распознана».

Поддерживается HTML-таблица (то, что даёт конвертация docx → html/markdown через pandoc).
PDF и нативный `.docx` АРОС этим модулем **не читаются** — для них нужен образец выгрузки.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

# обоснование — сметная норма (работа). ГЭСНр/ФЕРр — ремонтные, ФССЦпг — погрузка/перевозка
WORK_CODE_RE = re.compile(
    r"^\(?\s*(ГЭСН|ФЕР|ТЕР|ФССЦпг|ФССЦ|Сборник)", re.IGNORECASE)  # без \b: ГЭСНр, ГЭСНм
# код ресурса ФГИС ЦС: 21.1.06.09-0151, 62.1.01.09-1101, 91.06.06-048 (машины)
RESOURCE_CODE_RE = re.compile(r"^\(?\s*\d{2}\.\d(\.\d{2})?[.\-]\d")
MACHINE_CODE_RE = re.compile(r"^\(?\s*9\d\.\d{2}\.\d{2}-\d{3}")
SECTION_RE = re.compile(r"^Раздел\s+(\d+)\s*[.:]\s*(.+)$", re.IGNORECASE)
FORMULA_RE = re.compile(r"\|\|\s*V\s*=\s*(.+)$", re.IGNORECASE)
QTY_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _clean(text: str) -> str:
    text = re.sub(r"<p><em>Формула расчета объема:</em></p>", " || ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _cells(row_html: str) -> list[str]:
    return [_clean(m.group(1)) for m in re.finditer(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S)]


def _num(value: str) -> float | None:
    if not value:
        return None
    text = value.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    m = QTY_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _kind(code: str) -> str:
    if MACHINE_CODE_RE.match(code):
        return "machine"
    if WORK_CODE_RE.match(code):
        return "work"
    if RESOURCE_CODE_RE.match(code):
        return "material"
    return "other"           # шапка таблицы и прочие строки — не позиции


def parse_aros_markdown(text: str) -> dict:
    """Разобрать выгрузку АРОС (html-таблицы в markdown) в позиции и разделы."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S)
    sections: list[dict] = []
    positions: list[dict] = []
    nested_resources: list[dict] = []
    section = None
    totals: dict[str, str] = {}

    title = re.search(r"Локальный\s+СМЕТ[Нн]ый\s+расчет\s*\(СМЕТА\)\s*№\s*([^\n<]+)", text)
    object_line = re.search(r"Ремонтные работы[^\n<]+", text)
    if object_line:
        totals["object"] = _clean(object_line.group(0))
    if title:
        totals["smeta_no"] = _clean(title.group(1))
    method = re.search(r"Составлен\s+([а-яё\- ]+методом)", text, re.IGNORECASE)
    if method:
        totals["method"] = _clean(method.group(1))

    for row in rows:
        cells = _cells(row)
        if not cells:
            continue
        if len(cells) == 1:
            m = SECTION_RE.match(cells[0])
            if m:
                section = {"index": int(m.group(1)), "title": m.group(2).strip()}
                sections.append(section)
            continue
        if len(cells) < 5:
            continue
        number, code = cells[0], cells[1]
        # Номер вида 13.1 — ресурс внутри позиции, а не самостоятельная работа.
        # Графа 5 — норма на единицу работы; количество всего находится в графе 7.
        nested = re.fullmatch(r"(\d+)\.(\d+)", number or "")
        if nested and _kind(code) == "material":
            parent = int(nested.group(1))
            if any(p["n"] == parent and p["section_index"] ==
                   (section["index"] if section else None) for p in positions):
                nested_resources.append({
                    "n": number, "parent_n": parent, "code": code.strip(),
                    "name": FORMULA_RE.sub("", cells[2]).strip(" |"),
                    "unit": cells[3], "qty": _num(cells[6]) if len(cells) > 6 else None,
                    "qty_text": cells[6] if len(cells) > 6 else "",
                    "section_index": section["index"] if section else None,
                    "section": section["title"] if section else "",
                    "kind": "material", "nested": True,
                    "resource_role_unverified": True,
                })
            continue
        if not re.fullmatch(r"\d+", number or ""):
            continue          # строки ресурсов под позицией (1, 2, ОТ, ЭМ …) — не позиции
        # позицией считается только строка с обоснованием: сметная норма (работа)
        # или код ресурса ФГИС ЦС (материал). Иначе в позиции попадает шапка таблицы
        kind = _kind(code)
        if kind not in {"work", "material"}:
            continue
        name_field = cells[2]
        formula = ""
        m = FORMULA_RE.search(name_field)
        if m:
            formula = m.group(1).strip()
            name_field = FORMULA_RE.sub("", name_field).strip()
        name = name_field.strip(" |")
        if not code or not name:
            continue
        positions.append({
            "n": int(number),
            "code": code.strip(),
            "name": name,
            "unit": cells[3] if len(cells) > 3 else "",
            "qty": _num(cells[4]) if len(cells) > 4 else None,
            "qty_text": cells[4] if len(cells) > 4 else "",
            "formula": formula,
            "section_index": section["index"] if section else None,
            "section": section["title"] if section else "",
            "kind": kind,
        })

    works = [p for p in positions if p["kind"] == "work"]
    materials = [p for p in positions if p["kind"] == "material"]
    return {"totals": totals, "sections": sections, "positions": positions,
            "works": works, "materials": materials, "nested_resources": nested_resources,
            "source": "АРОС (html/markdown-выгрузка)"}


def read_aros(path: str | Path) -> dict:
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    doc = parse_aros_markdown(text)
    doc["file"] = path.name
    return doc


def aros_positions(doc: dict) -> list[dict]:
    """Позиции в формате ядра агента: работы — `work`, материалы — `material`."""
    main = [{"code": p["code"], "name": p["name"], "qty": p["qty"], "unit": p["unit"],
             "kind": p["kind"], "section": p["section"], "n": p["n"]}
            for p in doc["positions"]]
    nested = [{**p} for p in doc.get("nested_resources", [])]
    return main + nested



def section_summary(doc: dict) -> list[dict]:
    out = []
    for sec in doc["sections"]:
        idx = sec["index"]
        out.append({"index": idx, "title": sec["title"],
                    "works": sum(1 for p in doc["works"] if p["section_index"] == idx),
                    "materials": sum(1 for p in doc["materials"] if p["section_index"] == idx)})
    return out


def aros_to_markdown(doc: dict) -> str:
    """Короткая сводка разбора: сколько работ и материалов по разделам."""
    lines = [f"# Смета {doc['totals'].get('smeta_no', '')}".strip(),
             "", f"- файл: {doc.get('file', '—')} · {doc.get('source')}",
             f"- объект: {doc['totals'].get('object', '—')}",
             f"- позиций: {len(doc['positions'])} · работ: {len(doc['works'])} · "
             f"материалов и оборудования: {len(doc['materials'])}",
             f"- вложенных номерных ресурсов: {len(doc.get('nested_resources', []))} "
             "(назначение и закупку подтвердить; внутри стоимости родительских позиций)",
             "", "| Раздел | Работ | Материалов |", "|---|---|---|"]
    for sec in section_summary(doc):
        lines.append(f"| {sec['index']}. {sec['title']} | {sec['works']} | {sec['materials']} |")
    return "\n".join(lines) + "\n"
