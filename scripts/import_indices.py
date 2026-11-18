#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Импорт индексов изменения сметной стоимости Минстроя России из ФГИС ЦС.

Почему так, а не «руками из письма»: письма Минстроя лежат в открытом API ФГИС ЦС
как документы (`NormLegalDocPublished`, `normLegalDocBaseKind=40`). Дерево:

    GET /api/NormLegalDocPublished?limit=100&normLegalDocPublishedType=10&state=20
        &normLegalDocBaseKind=40&parentGuid=<год>        → кварталы
    ... &parentGuid=<квартал>   → два узла:
            «по группам однородных строительных ресурсов» и «по объектам строительства»
    ... &parentGuid=<узел>      → листья-письма: `number`, `name`, `filePath` (uuid)

Сам файл: `GET /api/values/GetFileContent/<uuid>` → PDF. Авторизация не нужна.

Формат хранения — «длинный»: одна запись = одно значение.

    quarter, letter, table_title, subject_no, subject, column, value, page

Так, а не «по колонке на вид индекса», потому что состав приложений в письмах
разный: в письме 53691-АЛ/09 есть приложение «Автомобильные дороги / Искусственные
дорожные сооружения», а в 45358-ИФ/09 его нет вообще, зато есть «перевозка грузов
автомобильным транспортом». Широкая таблица молча теряла бы эти различия.

`table_title` — дословная шапка таблицы со страницы PDF (склейка строк), поэтому
видно, к чему относятся `column 1..N`. `value = None` при прочерке «-» в письме:
это НЕ ноль.

    python3 scripts/import_indices.py --pdf /tmp/idx.pdf --quarter "III кв. 2026" \\
        --letter "53691-АЛ/09 от 28.08.2026" --dst data/indices
    python3 scripts/import_indices.py --fetch 8b89677f-... --dst data/indices
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path

API = "https://fgiscs.minstroyrf.ru/api"
KIND_INDICES = 40          # normLegalDocBaseKind: письма с индексами
SOURCE = ("Письма Минстроя России с рекомендуемыми индексами изменения сметной "
          "стоимости, дерево документов ФГИС ЦС (NormLegalDocPublished, "
          "normLegalDocBaseKind=40)")

# строка субъекта: «37 Краснодарский край», «21 Республика Коми (2 зона)»
# между номером и названием пробел может отсутствовать: «22 РеспубликаТыва»
RE_SUBJECT = re.compile(r"^(\d{1,3})\s*([А-ЯЁ][^\n]{2,90})$")
# строка чисел: «12,66 20,02» / «- -» / «17,90 14,62 15,88 12,26 12,04»
RE_NUMROW = re.compile(r"^(-|[\d,]+)(\s+(-|[\d,]+))*\s*$")
# шапка таблицы «субъект → числа» — ищем по нормализованному (без пробелов) тексту
HEADER_MARK = ("субъектроссийскойфедерации", "субъектыроссийскойфедерации")


def _ssl() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get_json(path: str) -> dict:
    with urllib.request.urlopen(API + path, context=_ssl(), timeout=120) as r:
        return json.loads(r.read() or b"{}")


def _get_bytes(path: str) -> bytes:
    with urllib.request.urlopen(API + path, context=_ssl(), timeout=600) as r:
        return r.read()


def letter_tree(year_guid: str) -> list[dict]:
    """Все письма под узлом (год или квартал), рекурсивно по двум уровням."""
    out: list[dict] = []
    data = _get_json("/NormLegalDocPublished?limit=100&normLegalDocPublishedType=10"
                     f"&state=20&normLegalDocBaseKind={KIND_INDICES}&parentGuid={year_guid}")
    for _, nodes in data.items():
        for node in nodes or []:
            kids = _get_json("/NormLegalDocPublished?limit=100&normLegalDocPublishedType=10"
                             f"&state=20&normLegalDocBaseKind={KIND_INDICES}"
                             f"&parentGuid={node['guid']}")
            leaves = [x for v in kids.values() for x in (v or [])]
            if leaves:
                out.extend(leaves)
            else:
                out.append(node)
    return out


def _is_header(text: str) -> bool:
    flat = "".join(text.split()).lower().replace("ё", "е")
    return any(m in flat for m in ("субъектроссийскойфедерации", "субъектыроссийскойфедерации"))


def _cut_tail(text: str) -> str:
    """Отрезать хвост страницы: сноски-примечания и служебную подпись ФГИС ЦС."""
    for marker in ("\nПримечани", "\nП римечани", "\n1."):
        i = text.find(marker)
        if i > 0:
            text = text[:i]
    return text


# начало строки данных внутри «шапки»: «28 Вологодская область 50,73»
RE_DATA_START = re.compile(r"\b\d{1,3}\s*[А-ЯЁ][а-яё]")


# Явные подписи колонок там, где они видны в шапке таблицы. Это НЕ догадка:
# заголовки перечислены в самой шапке приложения, здесь только сопоставление
# номеру колонки. Для таблиц без записи подпись остаётся номером колонки.
COLUMN_TITLES = {
    "Автомобильные дороги": {1: "Автомобильные дороги",
                             2: "Искусственные дорожные сооружения"},
    "перевозку грузов": {1: "автомобили бортовые", 2: "автомобили-самосвалы",
                         3: "автомобили-панелевозы", 4: "автомобили-трубоплетевозы",
                         5: "автобетоносмесители"},
    "Электрификация железных дорог": {1: "Электрификация железных дорог",
                                      2: "Железные дороги"},
    "Электрификацияжелезных дорог / Железныедороги": {1: "Электрификация железных дорог",
                                                      2: "Железные дороги"},
}


def column_title(label: str, column: int) -> str:
    """Подпись колонки по шапке таблицы; нет подписи — честный номер."""
    return COLUMN_TITLES.get(label, {}).get(column, f"колонка {column}")


def table_label(title: str) -> str:
    """Короткая метка таблицы: объекты из кавычек-ёлочек, иначе начало шапки."""
    quoted = re.findall(r"«([^»]{3,90})»", title)
    if quoted:
        return " / ".join(q.strip() for q in quoted[:3])
    flat = " ".join(title.split()).lower()
    for tail in ("перевозку грузов", "пусконаладочные работы", "оснбж-2001",
                 "электрификация железных дорог", "отраслевой сметно-нормативной"):
        if tail in flat:
            return {"отраслевой сметно-нормативной": "отраслевые нормативы (ОСНБЖ)"}.get(tail, tail)
    # «Приложение № 7 … ОСНБЖ-2001» без кавычек: ищем в шапке приложения
    m = re.search(r"Приложение\s*№\s*(\d+)", title)
    if m:
        return f"приложение № {m.group(1)} (метка не распознана)"
    return "таблица без метки (метка не распознана)"


def _trailing_numbers(text: str) -> list[str]:
    """Числа в конце строки: «РеспубликаТыва 24,02 20,03» → ['24,02','20,03']."""
    out: list[str] = []
    for token in reversed(text.split()):
        if re.match(r"^(-|[\d,]+)$", token):
            out.append(token)
        else:
            break
    return list(reversed(out))


def _table_title(lines: list[str]) -> str:
    """Шапка таблицы: строки до «№ п/п», склеенные в одну."""
    head = []
    for line in lines:
        if re.match(r"^№\s*$", line) or line.startswith("п/п") or line.startswith("п / п"):
            break
        if not line or line.startswith("Сведения сформированы"):
            continue
        if re.fullmatch(r"[-\d\s]+", line):     # номер страницы и прочерки — не шапка
            continue
        cut = RE_DATA_START.search(line)
        if cut:                                   # «…работы2 28 Вологодская область 50,73»
            head.append(line[:cut.start()].strip())
            break
        head.append(line.strip())
    return re.sub(r"\s+", " ", " ".join(head)).strip()[:200]


def parse_letter_pages(pages: list[str]) -> tuple[list[dict], dict]:
    """Разобрать все таблицы вида «№ п/п | субъект | числа» во всех приложениях."""
    rows: list[dict] = []
    titles: dict[str, int] = {}
    title = ""
    for pno, text in enumerate(pages, start=1):
        lines = [l.strip() for l in _cut_tail(text).split("\n")]
        if _is_header(text):
            candidate = _table_title(lines)
            if candidate and len(candidate) > 15:
                title = candidate
            elif not title:
                title = "(шапка таблицы не распознана, стр. %d)" % pno
        for j, line in enumerate(lines):
            m = RE_SUBJECT.match(line)
            if not m:
                continue
            name, nums = m.group(2), _trailing_numbers(m.group(2))
            if not nums and j + 1 < len(lines) and RE_NUMROW.match(lines[j + 1]):
                # вёрстка «название на строке, числа на следующей»
                nums = [v for v in lines[j + 1].split()
                        if re.match(r"^(-|[\d,]+)$", v)]
                name = m.group(2)
            else:
                # вёрстка «всё в одной строке»: числа уже отрезаны от названия
                name = m.group(2)[:len(m.group(2)) - len(" ".join(nums))].strip() \
                    if nums else m.group(2)
            if not nums:
                continue
            for k, raw in enumerate(nums, start=1):
                rows.append({
                    "page": pno,
                    "table_title": title,
                    "table_label": table_label(title),
                    "subject_no": int(m.group(1)),
                    "subject": re.sub(r"\s+", " ", name).strip(),
                    "column": k,
                    "column_title": column_title(table_label(title), k),
                    "value": None if raw == "-" else float(raw.replace(",", ".")),
                    "raw": raw,
                })
            titles[title] = titles.get(title, 0) + 1
    return rows, {"tables": {k[:120]: v for k, v in titles.items()}}


def write_csv(rows: list[dict], dst: Path, quarter: str, letter: str, pdf_sha: str,
              extra: dict) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    out = dst / "minstroy_indices.csv.gz"
    fields = ["quarter", "letter", "table_label", "table_title", "subject_no",
              "subject", "column", "column_title", "value", "page"]
    with gzip.open(out, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({"quarter": quarter, "letter": r["letter"],
                        "value": "" if r["value"] is None else r["value"],
                        **{k: r[k] for k in ("table_label", "table_title", "subject_no",
                                             "subject", "column", "column_title",
                                             "value", "page")}})
    meta = {
        "source": SOURCE,
        "quarter": quarter,
        "letter": letter,
        "pdf_sha256": pdf_sha,
        "values": len(rows),
        "subjects": len({(r["table_title"], r["subject"]) for r in rows}),
        "columns": {
            "table_label": "короткая метка таблицы (объекты из кавычек шапки)",
            "table_title": "дословная шапка таблицы из PDF (склейка строк)",
            "column": "номер колонки в таблице, 1..N; соответствие — по table_title",
            "column_title": "подпись колонки из шапки таблицы (иначе «колонка N»)",
            "value": "число; пусто = в письме прочерк «-» (индекс не установлен, НЕ ноль)",
            "page": "страница PDF",
        },
        "not_parsed_reason": (
            "Разбираются таблицы вида «№ п/п | субъект | числа». Приложения «по объектам "
            "строительства» и «по элементам прямых затрат» свёрстаны с объединёнными "
            "ячейками, повёрнутыми заголовками и переносами названий объектов — такие "
            "таблицы в CSV не попадают и остаются только в PDF. Число разобранных "
            "значений указано в поле values, а не «сколько должно быть»."
        ),
        **extra,
    }
    (dst / "minstroy_indices.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", action="append", default=[],
                    help="локальный PDF письма (можно повторять)")
    ap.add_argument("--pair", action="append", default=[], metavar="PDF=ПИСЬМО",
                    help="PDF и подпись письма, напр. /tmp/a.pdf=53691-АЛ/09 от 28.08.2026")
    ap.add_argument("--fetch", metavar="GUID", help="guid года в дереве ФГИС ЦС")
    ap.add_argument("--quarter", default="III кв. 2026")
    ap.add_argument("--letter", default="53691-АЛ/09 от 28.08.2026")
    ap.add_argument("--dst", default="data/indices")
    a = ap.parse_args(argv)

    try:
        import pypdf
    except ImportError:                             # pragma: no cover
        print("нужен pypdf: pip install pypdf", file=sys.stderr)
        return 2

    if a.fetch:
        letters = letter_tree(a.fetch)
        print(f"писем в дереве: {len(letters)}")
        for x in letters:
            print("  ", x.get("number"), "|", (x.get("name") or "")[:70],
                  "| файл", x.get("filePath"))
        return 0

    jobs = [(p, a.letter) for p in a.pdf]
    for item in a.pair:
        path, _, letter = item.rpartition("=")
        if not path:
            ap.error(f"--pair без пути к PDF: {item}")
        jobs.append((path, letter))
    if not jobs:
        ap.error("нужен --pdf/--pair или --fetch")

    all_rows: list[dict] = []
    per_letter: dict[str, int] = {}
    for path, letter in jobs:
        reader = pypdf.PdfReader(path)
        pages = [(p.extract_text() or "") for p in reader.pages]
        rows, stats = parse_letter_pages(pages)
        for r in rows:
            r["letter"] = letter
        all_rows.extend(rows)
        per_letter[letter] = per_letter.get(letter, 0) + len(rows)
        print(f"{Path(path).name}: страниц {len(pages)}, значений {len(rows)}, "
              f"таблиц {len(stats['tables'])}")
        for title, n in list(stats["tables"].items())[:4]:
            print(f"    «{title[:80]}…» — строк {n}")

    if not all_rows:
        print("таблиц «субъект → индексы» не найдено — письмо другого состава",
              file=sys.stderr)
        return 1
    out = write_csv(all_rows, Path(a.dst), a.quarter, ", ".join(per_letter), "",
                    {"by_letter": per_letter})
    print(f"{out}: значений {len(all_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
