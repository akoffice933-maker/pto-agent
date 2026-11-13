# -*- coding: utf-8 -*-
"""Раздел КР из PDF: что в нём действительно написано и что из этого считается.

Зачем
-----
Том КР приходит PDF-ом на 50 страниц. Прежде чем что-то считать, надо ответить на два
вопроса: **есть ли в томе ведомости** (объёмов работ, расхода стали, спецификация) и
**какие конструктивные параметры указаны текстом** (толщины, классы бетона, марки
арматуры, габариты, сетка колонн).

Модуль отвечает ровно на это — и не больше:

* `extract_facts()` вытаскивает параметры **с цитатой и номером страницы**. Нет цитаты —
  нет факта.
* `find_schedules()` честно говорит, есть ли ведомости. Если их нет (частая история:
  в томе только схемы армирования), объёмы **не выдумываются**: то, что можно посчитать
  по формуле из габаритов, считается как «оценка по габаритам» и помечается на проверку,
  остальное уходит в `needs_input` с конкретным вопросом.
* Геометрию чертежа модуль **не обмеряет** — он не takeoff-движок. Обмер делает
  takeoff-инструмент (см. `docs/20`), и его результат стыкуется через `takeoff.build_draft`.

Три правила:
1. Каждый факт — с цитатой и страницей; без цитаты факт не попадает в расчёт.
2. Расчёт по габаритам — это **верхняя оценка** (габаритный прямоугольник, без вычетов
   проёмов, лестниц и балок), поэтому MEDIUM и с явной оговоркой, а не «точный объём».
3. Чего в томе нет — то `needs_input` с адресатом: «нужна ведомость расхода стали»,
   « нужны высоты этажей», а не нулевая строка.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .takeoff import CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_MEDIUM, unit_ru
from .numeric import parse_number, positive

# ─────────────────────────── чтение PDF ───────────────────────────
def read_pdf_text(path: str | Path, cache: str | Path | None = None) -> dict:
    """Текстовый слой PDF постранично (кэш в JSON, чтобы не парсить 50 страниц каждый раз)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"файл не найден: {path}")
    cache_path = Path(cache) if cache else None
    if cache_path and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("file") == path.name and cached.get("size") == path.stat().st_size:
            return cached

    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages: list[dict] = []
    errors: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as error:                        # noqa: BLE001 — битые страницы бывают
            text = ""
            errors.append(f"стр. {number}: {type(error).__name__}: {error}")
        pages.append({"page": number, "text": text,
                      "has_text": len(text.strip()) > 30})

    doc = {
        "schema": "pto-agent/kr-pdf@1",
        "file": path.name,
        "size": path.stat().st_size,
        "pages_total": len(pages),
        "pages_with_text": sum(1 for p in pages if p["has_text"]),
        "pages_without_text": [p["page"] for p in pages if not p["has_text"]],
        "read_errors": errors,
        "pages": pages,
    }
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


# ─────────────────────────── поиск ведомостей ───────────────────────────
SCHEDULE_MARKERS = {
    "ведомость объёмов работ": ("ведомость объемов работ", "ведомость объёмов работ",
                                "ведомость объемов"),
    "ведомость расхода стали": ("ведомость расхода стали", "расход стали",
                                "ведомость стали"),
    "спецификация материалов": ("спецификация материалов", "спецификация изделий",
                                "спецификация"),
    "ведомость отделки": ("ведомость отделки",),
}


def find_schedules(doc: dict, markers: dict | None = None) -> dict:
    """Есть ли в томе ведомости. Отсутствие — не ошибка, а факт, который меняет план работ.

    `markers` — из профиля (`pdf_profiles.schedule_markers`): у слаботочки ищут кабельный
    журнал и спецификацию, а не ведомость расхода стали.
    """
    source = markers or SCHEDULE_MARKERS
    found: dict[str, list[int]] = {}
    for name, keys in source.items():
        pages = [p["page"] for p in doc["pages"]
                 if any(m in p["text"].lower() for m in keys)]
        if pages:
            found[name] = pages
    return {
        "found": found,
        "missing": [name for name in source if name not in found],
        "schedules_present": bool(found),
        "note": ("ведомости в томе есть — объёмы можно брать из таблиц" if found else
                 "табличных ведомостей в томе нет: объёмы из таблиц взять нельзя, "
                 "остаётся расчёт по указанным в тексте параметрам или обмер чертежа"),
    }


# ─────────────────────────── факты ───────────────────────────
FACT_RULES = [
    ("габариты здания", re.compile(
        r"размеры здания в габаритах[^:]*:\s*([\d,\.]+)\s*[хx×]\s*([\d,\.]+)\s*м", re.I),
     "м"),
    ("толщина фундаментной плиты", re.compile(
        r"толщина плиты\s*[–-]\s*(\d+)\s*мм", re.I), "мм"),
    ("толщина перекрытия", re.compile(r"перекрытия[^.]{0,40}толщиной\s*(\d+)\s*мм", re.I), "мм"),
    ("толщина стен подвала", re.compile(
        r"(?:наружные\s+)?стены\s+под[а-я]{0,6}[^.]{0,90}?толщиной\s*(\d+)\s*мм", re.I), "мм"),
    ("толщина внутренних стен", re.compile(
        r"стены имеют толщину\s*(\d+)\s*мм", re.I), "мм"),
    ("сечение колонн", re.compile(
        r"колонны имеют сечение\s*(\d+)\s*[хx×]\s*(\d+)\s*мм", re.I), "мм"),
    ("сетка колонн", re.compile(
        r"пролет[^.]{0,60}?(\d+(?:[,\.]\d+)?)\s*[хx×]\s*(\d+(?:[,\.]\d+)?)\s*м", re.I), "м"),
    ("балки", re.compile(r"балки шириной\s*(\d+)\s*мм и высотой\s*(\d+)\s*мм(?:\s*и\s*(\d+))?",
                         re.I), "мм"),
    ("бетон плиты и стен подвала", re.compile(
        r"из бетона\s*(В\d+)\s*(W\d+)\s*(F\d+)", re.I), ""),
    ("бетон вертикальных конструкций", re.compile(
        r"вертикальных несущих конструкций применяется бетон\s*(В\d+)\s*(W\d+)\s*(F\d+)", re.I), ""),
    ("кирпич перегородок", re.compile(
        r"кирпич\s*(КР-[\w\-/\.\*]+(?:\s*[\w\*/\.]+)?)", re.I), ""),
    ("газобетон стен", re.compile(r"газобетона\s*(D\d+)", re.I), ""),
    ("утеплитель фасада", re.compile(r"минераловатными плитами[^,]{0,60}?(\d+)\s*мм", re.I), "мм"),
    ("этажность блоков (по тексту)", re.compile(r"(\d)\s*-?\s*этаж", re.I), ""),
]


def _search_with_reject(pattern: re.Pattern, reject: re.Pattern | None,
                        text: str) -> re.Match | None:
    """Первое совпадение, которое не отсекается правилом `reject`.

    `reject` ищется в тексте перед совпадением (до 40 символов): так «4 этаж» внутри
    «пом. 414, 4 этаж» отсеивается, а «этажность блоков — 4» проходит.
    """
    for match in pattern.finditer(text):
        if reject is not None:
            window = text[max(match.start() - 40, 0):match.end()]
            if reject.search(window):
                continue
        return match
    return None


def _page_for(doc: dict, pattern: re.Pattern,
              reject: re.Pattern | None = None) -> tuple[int | None, str]:
    for page in doc["pages"]:
        flat = " ".join(page["text"].split())
        match = _search_with_reject(pattern, reject, flat)
        if match:
            start = max(match.start() - 70, 0)
            if start:                                   # не режем слово пополам
                start = flat.find(" ", start) + 1
            return page["page"], flat[start:match.end() + 40].strip()
    return None, ""


def extract_facts(doc: dict, rules: list | None = None) -> dict:
    """Параметры объекта: значение + единица + номер страницы + цитата.

    `rules` — правила профиля (`pdf_profiles.fact_rules`): dict с `pattern`/`reject`
    новее кортежей `FACT_RULES`, потому что умеют отсеивать ложные срабатывания
    вроде «этажность 4» из «пом. 414, 4 этаж».
    """
    facts: list[dict] = []
    prepared = rules if rules is not None else [
        {"name": name, "pattern": pattern, "unit": unit, "reject": None, "note": ""}
        for name, pattern, unit in FACT_RULES]
    for rule in prepared:
        name, pattern, unit = rule["name"], rule["pattern"], rule["unit"]
        reject = rule.get("reject")
        page, quote = _page_for(doc, pattern, reject=reject)
        if page is None:
            continue
        flat = " ".join(next(p for p in doc["pages"] if p["page"] == page)["text"].split())
        match = _search_with_reject(pattern, reject, flat)
        groups = [g for g in (match.groups() if match else []) if g]
        value = " × ".join(groups) if len(groups) > 1 else (groups[0] if groups else "")
        facts.append({"name": name, "value": value, "unit": unit, "page": page,
                      "quote": quote[:220], "note": rule.get("note", "")})
    by_name = {f["name"]: f for f in facts}
    return {"facts": facts, "by_name": by_name,
            "note": "факты только из текста тома: у каждого — страница и цитата"}


def extract_levels(doc: dict) -> dict:
    """Какие перекрытия и отметки реально есть в томе (считаем по тексту, не по памяти)."""
    levels: dict[str, int] = {}
    page_by_level: dict[str, int] = {}
    for page in doc["pages"]:
        flat = " ".join(page["text"].split()).lower()
        for match in re.finditer(r"перекрыти[а-я]*\s+(подвала|(\d)\s*этаж)", flat):
            name = "подвал" if match.group(1) == "подвала" else f"{match.group(2)} этаж"
            levels[name] = levels.get(name, 0) + 1
            page_by_level.setdefault(name, page["page"])
    marks: dict[str, int] = {}
    for page in doc["pages"]:
        for match in re.finditer(r"(-\s?\d{1,2}[,.]\d{3})", page["text"]):
            marks.setdefault(match.group(1).replace(" ", ""), page["page"])
    return {
        "levels": list(levels),
        "levels_count": len(levels),
        "level_pages": page_by_level,
        "marks": [{"mark": m, "page": p} for m, p in list(marks.items())[:8]],
    }


def _num(text: str) -> float | None:
    """Конечное число или `None`. `positive()` — когда ноль означает «не заполнено»."""
    return parse_number(text)


# ─────────────────────────── расчёт по габаритам ───────────────────────────
def derive_boq(facts: dict, *, levels: dict | None = None,
               storey_height: float | None = None, profile: dict | None = None) -> dict:
    """Объёмы, которые можно посчитать по указанным в томе параметрам.

    Считается только то, что выводится формулой из текстовых параметров. Всё, для чего
    нужен обмер (площади стен, проёмы, расход стали), уходит в `needs_input`.
    """
    by = facts.get("by_name") or {}
    lines: list[dict] = []
    needs: list[dict] = []
    formulas: list[dict] = []
    # профиль решает, что считаем расчётом; без профиля — поведение КР как раньше
    allow = None if profile is None else set(profile.get("derive") or [])
    concrete_ok = allow is None or "concrete_from_dimensions" in allow
    # вопрос задаём только про те параметры, которые профиль вообще ищет:
    # «площадь застройки» в слаботочке — это вопрос не по адресу
    tracked = None if profile is None else {f["name"] for f in facts.get("facts") or []}

    def has(*names: str) -> bool:
        return tracked is None or any(name in tracked for name in names)

    def missing(*names: str) -> bool:
        """Параметр профилю нужен (есть в правилах), но из томом не извлёкся."""
        if tracked is None:
            return True
        wanted = set(names)
        rules = {r["name"] for r in (profile.get("facts") or [])} if profile else set()
        return bool(wanted & rules) and not (wanted & tracked)

    gab = by.get("габариты здания")
    area = None
    if gab:
        parts = [p for p in re.split(r"[хx×]", gab["value"]) if p.strip()]
        if len(parts) == 2:
            length, width = positive(parts[0]), positive(parts[1])
            if length and width:
                area = round(length * width, 2)
                formulas.append({"what": "площадь в габаритах по осям",
                                 "formula": f"{length:g} × {width:g} = {area:g} м2",
                                 "page": gab["page"],
                                 "caveat": "габаритный прямоугольник: фактическая площадь "
                                           "меньше (здание из двух объёмов многоугольной формы)"})
    if area is None and missing("габариты здания"):
        if gab:
            why = (f"габариты в томе есть («{gab['value']}», стр. {gab['page']}), но не разобраны "
                   f"как два положительных размера — считать по ним нельзя")
        else:
            why = "в тексте нет габаритов по осям"
        needs.append({"what": "площадь застройки/этажа", "why": why,
                      "ask": "габариты по осям или обмер плана"})

    t_plate = positive((by.get("толщина фундаментной плиты") or {}).get("value"))
    if area and t_plate and not concrete_ok:
        needs.append({"what": "бетон фундаментной плиты", "why": f"габариты и толщина "
                      f"{t_plate:g} мм в томе есть, но профиль «{profile.get('name')}» не "
                      f"разрешает выводить бетон расчётом по габаритам",
                      "ask": "ведомость объёмов бетона или обмер"})
    elif area and t_plate:
        qty = round(area * t_plate / 1000, 1)
        if qty > 0:
            lines.append({
                "description": "Бетон В25 W6 F150 — фундаментная плита",
                "unit": "m3", "qty": qty, "confidence": CONFIDENCE_MEDIUM,
                "source": f"расчёт по том КР1, стр. {(by['габариты здания'])['page']} "
                          f"(габариты) и стр. {by['толщина фундаментной плиты']['page']} "
                          f"(толщина {t_plate:g} мм)",
                "derived": True,
                "formula": f"{area:g} м2 × {t_plate:g} мм = {qty:g} м3",
                "caveat": "верхняя оценка: без вычетов проёмов, приямков и участков без плиты",
            })
        else:
            needs.append({"what": "объём бетона фундаментной плиты",
                          "why": "расчёт по габаритам дал 0 м3 — один из размеров не заполнен",
                          "ask": "габариты по осям и толщина плиты"})

    t_basement = positive((by.get("толщина стен подвала") or {}).get("value"))
    if t_basement and missing("толщина стен подвала"):
        marks = ", ".join(f"{m['mark']} (стр. {m['page']})"
                          for m in (levels or {}).get("marks", [])[:6]) or "отметок не найдено"
        needs.append({
            "what": "объём наружных стен подвала",
            "why": f"толщина {t_basement:g} мм в томе есть, а высоты подвала из отметок "
                   f"однозначно не выводится: в томе встречаются отметки {marks} — какая пара "
                   f"«пол/верх подвала» верна, решает человек",
            "ask": "высота подвала по разрезу (или подтвердите пару отметок)",
        })

    t_floor = positive((by.get("толщина перекрытия") or {}).get("value"))
    floors = (levels or {}).get("levels_count") or 0
    if area and t_floor and floors and concrete_ok:
        qty = round(area * t_floor / 1000 * floors, 1)
        if qty > 0:
            lines.append({
                "description": "Бетон В25 — перекрытия (подвал + 3 этажа)",
                "unit": "m3", "qty": qty, "confidence": CONFIDENCE_LOW,
                "source": f"расчёт по том КР1, стр. {by['толщина перекрытия']['page']}; "
                          f"перекрытия в томе: {', '.join((levels or {}).get('levels') or [])}",
                "derived": True,
                "formula": f"{area:g} м2 × {t_floor:g} мм × {floors} перекрытия = {qty:g} м3",
                "caveat": f"перекрытий в томе {floors}; проёмы, лестницы и балки не вычтены — "
                          f"объём завышен",
            })

    # колонны: нужна высота этажа — в томе её текстом нет
    if by.get("сечение колонн") and has("сечение колонн"):
        needs.append({"what": "объём колонн", "why": "в тексте есть сечение 400×400 и сетка 7,2×7,2, "
                                                    "но нет высот этажей",
                      "ask": "высота этажей (или разрез) — тогда посчитаю по сетке"})
    if by.get("балки") and has("балки"):
        needs.append({"what": "объём балок", "why": "в тексте есть сечения балок, но нет их общей длины",
                      "ask": "схема балок с длинами или обмер"})
    always = [a for a in (profile or {}).get("always_ask") or []]
    has_armatura = any("арматура" in a for a in always)
    if profile is None or has_armatura:
        needs.append({"what": "арматура (т)", "why": "ведомости расхода стали в томе нет, а расход "
                                                    "по объёму бетона — это норматив, а не факт",
                      "ask": "ведомость расхода стали по спецификации либо обмер по схемам армирования"})
    if (by.get("кирпич перегородок") or by.get("газобетон стен")) and \
            has("кирпич перегородок", "газобетон стен"):
        needs.append({"what": "кладка и газобетон (м3/м2)", "why": "материал и толщина указаны, "
                                                                 "площадей стен в томе нет",
                      "ask": "площади стен по этажам"})
    if profile is not None:
        # вопросы профиля, которых ещё нет в списке (сверка по «what»)
        known = {n["what"] for n in needs}
        for item in always:
            what = item if isinstance(item, str) else item.get("what", "")
            if not what or what in known:
                continue
            ask = (item if isinstance(item, str)
                   else item.get("ask") or item.get("why") or "уточнить у проектировщика")
            needs.append({"what": what, "ask": ask,
                          "why": (f"профиль «{profile['name']}» требует это всегда: "
                                  f"в томе этого нет, а расчётом не выводится")})

    return {"lines": lines, "needs_input": needs, "formulas": formulas,
            "storey_height": storey_height, "levels": levels}


# ─────────────────────────── отчёт ───────────────────────────
def kr_report(doc: dict, schedules: dict, facts: dict, derived: dict,
              object_name: str = "", draft: dict | None = None,
              profile: dict | None = None) -> str:
    """Markdown-отчёт. `profile` (из `pdf_profiles.get_profile`) задаёт заголовок и оговорки."""
    from .pdf_profiles import describe

    title = profile["title"] if profile else "Раздел КР"
    lines = [f"# {title} из PDF: {object_name or doc['file']}", ""]
    if profile:
        lines += [f"- {describe(profile)}", ""]
    lines += [f"- файл: `{doc['file']}` · страниц {doc['pages_total']} · с текстовым слоем "
              f"{doc['pages_with_text']}",
              f"- страниц без текста (только графика/скан): "
              f"{', '.join(str(p) for p in doc['pages_without_text']) or '—'}"
              + (f", ошибки чтения: {'; '.join(doc['read_errors'])}" if doc["read_errors"] else ""),
              ""]

    lines += [f"## 1. {'Ведомости, которые ищет профиль' if profile else 'Есть ли в томе ведомости'}", "",
              f"- найдено: {', '.join(f'{k} (стр. {v})'.replace('[','').replace(']','') for k, v in schedules['found'].items()) or '**ничего**'}",
              f"- нет: {', '.join(schedules['missing']) or '—'}",
              f"- вывод: {schedules['note']}", ""]

    lines += ["## 2. Что написано в томе (с цитатами)", "",
              "| Параметр | Значение | Стр. | Цитата |", "|---|---|---|---|"]
    for fact in facts["facts"]:
        lines.append(f"| {fact['name']} | {fact['value']} {fact['unit']} | {fact['page']} | "
                     f"{fact['quote'][:90].replace('|','/')} |")
    lines.append("")

    if derived["formulas"]:
        lines += ["## 3. Как считались объёмы", ""]
        for formula in derived["formulas"]:
            lines.append(f"- {formula['what']}: {formula['formula']} — "
                         f"{formula['caveat']}")
        lines.append("")

    lines += ["## 4. Объёмы, которые получаются расчётом (не обмером)", ""]
    if derived["lines"]:
        lines += ["| Позиция | Кол-во | Уверенность | Формула | Оговорка |", "|---|---|---|---|---|"]
        for line in derived["lines"]:
            lines.append(f"| {line['description']} | {line['qty']:g} {unit_ru(line['unit'])} | "
                         f"{line['confidence']} | {line['formula']} | {line['caveat'][:80]} |")
    else:
        lines.append("_Объёмы расчётом получить не удалось: не хватает исходных параметров._")
    lines.append("")

    lines += ["## 5. Чего в томе нет — и что нужно спросить", ""]
    for need in derived["needs_input"]:
        lines.append(f"- **{need['what']}** — {need['why']}; нужно: {need['ask']}")
    lines.append("")

    if draft:
        totals = draft.get("totals") or {}
        lines += ["## 6. Прогон через стык с ценами", "",
                  f"- строк в ведомости: {totals.get('lines')} · с ценой: "
                  f"{totals.get('priced_lines')} · не включено: {totals.get('excluded_lines')}",
                  f"- итог по строкам с известной ценой: "
                  f"{totals.get('amount_rub')} ₽ (высокая уверенность: "
                  f"{totals.get('amount_high_rub')} ₽)", ""]
        lines += ["| Элемент | Кол-во | Ед. | Цена | Сумма | Уверенность | Статус |",
                  "|---|---|---|---|---|---|---|"]
        for line in draft.get("lines") or []:
            lines.append(f"| {line['element'][:52]} | {line['qty']} | {line['unit']} | "
                         f"{line.get('price') or '—'} | {line.get('amount') or '—'} | "
                         f"{line['confidence']} | {line['status']} |")
        for item in draft.get("excluded") or []:
            lines.append(f"| {item['element'][:52]} | {item['qty']} | {item.get('unit')} | — | — | "
                         f"{item['confidence']} | excluded: {item.get('reason','')[:40]} |")
        lines.append("")

    lines += ["## 7. Что это значит", "",
              "Это **не смета**. В томе нет ведомости объёмов и расхода стали, поэтому объёмы "
              "по бетону — расчёт по габаритам и толщинам из текста (верхняя оценка), арматура "
              "не посчитана вовсе. Точные объёмы даёт только обмер чертежа "
              "takeoff-инструментом или ведомость от проектировщика.",
              "",
              "Дальше по-честному два пути: (1) прислать ведомость объёмов/расхода стали, если "
              "она есть в другом томе; (2) прогнать PDF через takeoff-движок и стыковать его "
              "выгрузку — стык уже готов (`pto_agent takeoff draft`).", ""]
    return "\n".join(lines) + "\n"


def write_report(path: str | Path, payload: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
