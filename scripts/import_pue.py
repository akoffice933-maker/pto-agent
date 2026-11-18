#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Импорт полного текста ПУЭ (6-е и 7-е издания, действующие разделы) в `data/norms/`.

Источник: PDF «Правила устройства электроустановок. Шестое и седьмое издания (все
действующие разделы)», 499 страниц (https://gktgs.ru/assets/files/gost/pue-6-7-izdanie.pdf).
ПУЭ — официальный документ, объектом авторских прав не является (пп. 1 п. 6 ст. 1259 ГК РФ).

Что делает, кроме извлечения текста:

* выбрасывает оглавление (страницы 2–11) и колонтитулы «ПУЭ108» — иначе номер страницы
  попадает внутрь пункта и цитата перестаёт быть дословной;
* склеивает переносы: «мероприя-\nтий» → «мероприятий». Без этого проверка дословности
  цитаты (`cite_verify`) не находит текст, который человек видит в книге;
* режет по маркерам пунктов `1.8.37.`, `2.1.11.`; главы становятся разделами `# Глава X.Y`;
* сохраняет номер страницы каждого пункта, чтобы цитату можно было сверить по бумаге.

    python3 scripts/import_pue.py --pdf /tmp/pue67.pdf --dst data/norms
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import date
from pathlib import Path

POINT = re.compile(r"^\s*(\d{1,2}\.\d{1,2}\.\d{1,4})\.\s*(.*)$")
CHAPTER = re.compile(r"^\s*Глава\s+(\d{1,2}\.\d{1,2})\.?\s*(.*)$", re.I)
PAGE_HEAD = re.compile(r"^\s*(ПУЭ\s*\d+|\d+\s*$|\d+\s*Глава.*|Глава\s+\d+\.\d+\s*\|?\s*\d*)\s*$")


def read_pages(pdf: Path, skip_first: int = 11) -> list[tuple[int, str]]:
    import pypdf
    reader = pypdf.PdfReader(str(pdf))
    pages = []
    for index in range(skip_first, len(reader.pages)):
        text = reader.pages[index].extract_text() or ""
        if text.strip():
            pages.append((index + 1, text))
    return pages


def clean_page(text: str) -> str:
    """Убрать колонтитулы и склеить переносы — иначе цитата не совпадёт с книгой."""
    lines = []
    for line in text.splitlines():
        if PAGE_HEAD.match(line):
            continue
        # «мероприя-» + следующая строка со строчной — это перенос, а не дефис
        lines.append(line.rstrip())
    text = "\n".join(lines)
    text = re.sub(r"([а-яё])-\s*\n\s*([а-яё])", r"\1\2", text)
    return text


def build(pdf: Path) -> tuple[str, dict]:
    pages = read_pages(pdf)
    points: list[tuple[str, str, int]] = []
    chapters: dict[str, str] = {}
    current_chapter = ""
    current_point, buffer, page_of = None, [], 0

    def flush():
        nonlocal current_point, buffer
        if current_point:
            body = "\n".join(line for line in buffer).strip()
            # В PDF строка — это типографская строка, а не абзац. Склеиваем построчные
            # разрывы в пробел (пустые строки остаются границей абзаца), иначе цитата
            # в акте выглядит как обрывки фраз.
            body = re.sub(r"\n{2,}", "\n\n", body)
            body = re.sub(r"(?<!\n)\n(?!\n)", " ", body)
            if len(body) > 30:
                points.append((current_point, body, page_of))
        current_point, buffer = None, []

    for page_no, raw in pages:
        text = clean_page(raw)
        for line in text.splitlines():
            chapter = CHAPTER.match(line)
            if chapter and len(chapter.group(2)) > 3:
                flush()
                current_chapter = chapter.group(1)
                chapters[current_chapter] = chapter.group(2).strip().rstrip(".")
                continue
            match = POINT.match(line)
            if match:
                flush()
                current_point, page_of = match.group(1), page_no
                buffer = [match.group(2)] if match.group(2).strip() else []
                continue
            if current_point:
                buffer.append(line)
    flush()

    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    head = [
        "---",
        "designation: ПУЭ, 7-е изд.",
        "title: Правила устройства электроустановок (6-е и 7-е издания, действующие разделы)",
        "kind: ПУЭ",
        "edition: действующие разделы 6-го и 7-го изданий",
        "status: active",
        "effective_from: 2003-01-01",
        "version: 1",
        "source: Минэнерго России, приказ от 08.07.2002 № 204 (главы 7-го изд.)",
        "source_url: https://gktgs.ru/assets/files/gost/pue-6-7-izdanie.pdf",
        f"source_sha256: {digest}",
        f"imported_at: {date.today().isoformat()}",
        f"pages: {len(pages) + 11}",
        f"clauses: {len(points)}",
        "note: >",
        "  Полный текст действующих разделов. Нумерация пунктов сохранена как в книге; главы 6-го",
        "  издания помечены в тексте. Текст получен из PDF с текстовым слоем, переносы склеены;",
        "  цитаты проверяются дословно, номер страницы указан у каждого пункта.",
        "---",
        "# ПУЭ, 7-е изд.",
        "",
    ]
    out = list(head)
    last_chapter = ""
    for number, body, page in sorted(points, key=lambda item: [int(x) for x in item[0].split(".")]):
        chapter = ".".join(number.split(".")[:2])
        if chapter != last_chapter:
            out += [f"# Глава {chapter} {chapters.get(chapter, '')}".rstrip(), ""]
            last_chapter = chapter
        out += [f"## {number}", body, f"<!-- стр. {page} -->", ""]
    stats = {"clauses": len(points), "chapters": len(chapters), "pages": len(pages) + 11,
             "sha256": digest}
    return "\n".join(out), stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ПУЭ → data/norms")
    parser.add_argument("--pdf", default="/tmp/pue67.pdf")
    parser.add_argument("--dst", default="data/norms")
    parser.add_argument("--name", default="pue_7_full.md")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"нет PDF: {pdf}", file=sys.stderr)
        return 2
    text, stats = build(pdf)
    print(f"пунктов {stats['clauses']}, глав {stats['chapters']}, страниц {stats['pages']}, "
          f"sha256 {stats['sha256'][:12]}")
    if not args.check:
        Path(args.dst, args.name).write_text(text, encoding="utf-8")
        print(f"записано: {Path(args.dst, args.name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
