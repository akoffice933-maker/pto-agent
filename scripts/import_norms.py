#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Импорт нормативных документов из внешней базы в `data/norms/`.

Источник: <https://github.com/ivan-in-sky/norm-md-db> — канонические Markdown-тексты
СП/ГОСТ/СНиП. Тексты нормативных документов по пп. 1 п. 6 ст. 1259 ГК РФ не являются
объектами авторских прав (официальные документы), лицензия к ним не применяется;
каталог и метаданные источника — CC0, код — MIT (`LICENSE-data` источника).

Что делает скрипт, кроме копирования:

1. **Не переписывает то, что уже есть.** Уже загруженный документ (по `designation`)
   остаётся как есть: иначе прогоны начнут считать по другому тексту молча.
2. **Не выдаёт чужой статус за свой.** `status: active` ставится только если источник
   его подтверждает; `unknown` переносится как `unknown`, а поиск такой документ
   **не цитирует**, пока человек не подтвердит статус.
3. **Разбирает текст на пункты.** `## <номер>` — только настоящий номер пункта
   (`10.3.3`, `5.1`, `А.2`); разделы, приложения и «Предисловие» пунктами не считаются.
4. **Сохраняет провенанс.** В front-matter пишется `source_url`, `source_repo`,
   `source_commit`, `source_status`, `imported_at` — чтобы через полгода было видно,
   откуда текст и какой ревизии.

    python3 scripts/import_norms.py --source /tmp/norm-md-db --dst data/norms
    python3 scripts/import_norms.py --source /tmp/norm-md-db --dst data/norms --check
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

POINT_RE = re.compile(r"^\d+(?:\.\d+)+$")          # 10.3.3, 5.1, 1.2.10
APPENDIX_POINT_RE = re.compile(r"^[А-ЯЁ]\.\d+(?:\.\d+)*$")   # А.2, Б.1.3


def git_commit(repo: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Плоский и вложенный YAML — без внешних зависимостей: нужны только строки `ключ: значение`."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if not line.strip() or line.startswith((" ", "-", "#")):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, parts[2]


INLINE_POINT_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(?=[А-ЯЁA-Z(«\"])")
INLINE_APPENDIX_RE = re.compile(r"^([А-ЯЁ]\.\d+(?:\.\d+)*)\s+(?=[А-ЯЁA-Z(«\"])")


def split_points(body: str) -> tuple[list[tuple[str, str]], int]:
    """Тело документа → [(номер пункта, текст)].

    В источнике пункт — не всегда заголовок: часто это абзац «10.3.3 Толщина защитного слоя…».
    Поэтому границей пункта считается и заголовок `## 10.3.3`, и строка, начинающаяся с номера
    вида `10.3.3 ` или `А.2 `. Разделы («# 10 Конструктивные требования», «## 10.1 Общие
    положения») и приложения без номера пункта пунктами не считаются.
    """
    points: list[tuple[str, str]] = []
    dropped = 0
    current, buf = None, []

    def flush():
        nonlocal current, buf
        if current is not None:
            text = "\n".join(buf).strip()
            if text:
                points.append((current, text))
        current, buf = None, []

    for line in body.splitlines():
        stripped = line.strip()
        heading = re.match(r"^#{1,4}\s+(.+)$", stripped)
        if heading:
            flush()
            title = heading.group(1).strip()
            number = title.split()[0] if title else ""
            if POINT_RE.match(number) or APPENDIX_POINT_RE.match(number):
                current, buf = number, [title[len(number):].strip()]
            else:
                dropped += 1              # раздел/приложение целиком — не пункт
            continue
        match = INLINE_POINT_RE.match(stripped) or INLINE_APPENDIX_RE.match(stripped)
        if match and len(stripped) > 25:
            flush()
            number = match.group(1)
            current, buf = number, [stripped[match.end():]]
            continue
        if current is not None and stripped:
            buf.append(stripped)
    flush()
    return points, dropped


def split_nested(number: str, text: str) -> list[tuple[str, str]]:
    """Пункт с внутренней нумерацией → отдельные пункты.

    В сводах правил пункт 5.1 содержит 5.1.1, 5.1.2 — и именно на них ссылаются акты.
    Пока они лежали внутри тела 5.1, ссылка «СП 56.13330.2021 п. 5.1.1» не проверялась:
    пункт в базе есть, но под своим номером не находится (статус `clause_missing`).
    Вложенные пункты в источнике помечены жирным: `**5.1.1** текст`.
    """
    pattern = re.compile(r"\*\*(" + re.escape(number) + r"\.\d{1,3})\*\*\s*")
    marks = list(pattern.finditer(text))
    if not marks:
        return [(number, text)]
    out: list[tuple[str, str]] = []
    head = text[:marks[0].start()].strip()
    if head:
        out.append((number, head))          # текст до первой вложенной нумерации
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((mark.group(1), text[mark.end():end].strip()))
    return out


def clean_text(body: str) -> str:
    """Абзацы подряд идут одной строкой в исходнике — разлепляем по правилам разметки источника."""
    body = re.sub(r"\s+\^c[\d\-]+\s*$", "", body, flags=re.M)     # якоря вида ^c10-3-3
    body = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", body)     # вики-ссылки → текст
    body = re.sub(r"\[\[([^\]]+)\]\]", r"\1", body)
    return body.strip()


def build_document(meta: dict, body: str, *, repo: Path, commit: str) -> tuple[str, dict]:
    designation = meta.get("designation") or meta.get("note_name") or ""
    source_status = (meta.get("status") or "unknown").strip()
    points, dropped = split_points(body)
    expanded: list[tuple[str, str]] = []
    for number, text in points:
        expanded += split_nested(number, clean_text(text))
    points = expanded

    lines = ["---",
             f"designation: {designation}",
             f"title: {meta.get('title_short') or meta.get('title') or designation}",
             f"kind: {(meta.get('kind') or 'СП').upper()}",
             f"edition: {meta.get('amendment') or meta.get('issued') or ''}".rstrip(),
             # статус не выдумываем: unknown остаётся unknown, и поиск такой документ не цитирует
             f"status: {source_status}",
             f"effective_from: {meta.get('in_force_since') or ''}".rstrip(),
             "version: 1",
             f"source: {meta.get('issuing_body') or ''}".rstrip(),
             f"source_repo: https://github.com/ivan-in-sky/norm-md-db",
             f"source_url: {(meta.get('source.uri') or meta.get('uri') or '')}".rstrip(),
             f"source_commit: {commit}",
             f"source_status: {source_status}",
             f"imported_at: {date.today().isoformat()}",
             f"clauses: {len(points)}",
             "---",
             f"# {designation}",
             ""]
    for number, text in points:
        lines += [f"## {number}", clean_text(text), ""]
    stats = {"designation": designation, "clauses": len(points),
             "skipped_sections": dropped, "source_status": source_status}
    return "\n".join(lines), stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="импорт нормативных документов в data/norms")
    parser.add_argument("--source", default="/tmp/norm-md-db", help="клонированный репозиторий-источник")
    parser.add_argument("--dst", default="data/norms")
    parser.add_argument("--check", action="store_true", help="ничего не писать, только отчёт")
    args = parser.parse_args(argv)

    source, dst = Path(args.source), Path(args.dst)
    if not (source / "data").is_dir():
        print(f"нет каталога data/ в источнике {source} — сначала склонируйте репозиторий", file=sys.stderr)
        return 2
    dst.mkdir(parents=True, exist_ok=True)
    commit = git_commit(source)

    existing = {}
    for path in sorted(dst.glob("*.md")):
        if path.name == "README.md":
            continue
        meta, _ = parse_front_matter(path.read_text(encoding="utf-8"))
        if meta.get("designation"):
            existing[meta["designation"]] = path.name

    report = {"source_commit": commit, "added": [], "skipped_existing": [], "no_points": []}
    for md in sorted((source / "data").rglob("*.md")):
        if md.name.lower() in ("readme.md", "index.md"):
            continue
        meta, body = parse_front_matter(md.read_text(encoding="utf-8"))
        designation = meta.get("designation") or ""
        if not designation:
            continue
        if designation in existing:
            report["skipped_existing"].append({"designation": designation, "file": existing[designation]})
            continue
        text, stats = build_document(meta, body, repo=source, commit=commit)
        if not stats["clauses"]:
            report["no_points"].append(stats)
            continue
        name = re.sub(r"[^0-9А-Яа-яЁёA-Za-z]+", "_", designation).strip("_").lower() + ".md"
        if not args.check:
            (dst / name).write_text(text, encoding="utf-8")
        report["added"].append({"designation": designation, "file": name, **stats})

    print(json.dumps(report, ensure_ascii=False, indent=1))
    note = " (--check: файлы не записаны)" if args.check else ""
    print(f"добавлено {len(report['added'])}, уже было {len(report['skipped_existing'])}, "
          f"без пунктов {len(report['no_points'])}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
