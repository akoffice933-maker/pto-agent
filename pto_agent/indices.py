# -*- coding: utf-8 -*-
"""Индексы изменения сметной стоимости Минстроя России — из письма, а не «из головы».

Индекс — множитель перехода от базисной стоимости (ФЕР-2001/ТЕР-2001, ОСНБЖ-2001)
к текущей. Хранить его «числом в коде» нельзя: индекс привязан к кварталу, к
субъекту РФ, к виду работ и к колонке конкретной таблицы письма. Поэтому запись
здесь — это (квартал, письмо, шапка таблицы, субъект, номер колонки, значение,
страница PDF).

Чего модуль принципиально не делает:

* не подставляет индекс другого субъекта, другого квартала или другой таблицы —
  нет записи, значит `not_found` с перечнем того, что вообще есть;
* не превращает прочерк «-» из письма в ноль или единицу — это `None` и статус
  «индекс не установлен»;
* не угадывает смысл колонки: `table_title` — дословная шапка со страницы, и
  рядом всегда лежит номер страницы, чтобы человек мог открыть PDF и сверить.

Данные: `data/indices/minstroy_indices.csv.gz` + `minstroy_indices.meta.json`,
готовятся `scripts/import_indices.py` из PDF, скачанных из открытого API ФГИС ЦС.
"""
from __future__ import annotations

import csv
import gzip
import json
import re
from pathlib import Path

DEFAULT_DIR = Path("data/indices")
CSV_NAME = "minstroy_indices.csv.gz"
META_NAME = "minstroy_indices.meta.json"


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _unstick(name: str) -> str:
    """«РеспубликаТыва(1зона)» → «Республика Тыва (1 зона)».

    В части писем PDF отдаёт строку без пробелов между номером и названием, а
    внутри названия пробелы сохраняются не всегда. Разлепляем только когда
    пробелов в строке нет вовсе — иначе легко испортить нормальное название.
    """
    if " " in name.strip():
        return name
    out = re.sub(r"(?<=[а-яё])(?=[А-ЯЁ])", " ", name)
    out = re.sub(r"(?<=[а-яёА-ЯЁ])(?=\()", " ", out)
    out = re.sub(r"(?<=[а-яёА-ЯЁ])(?=\d)", " ", out)
    return out


def load(directory: str | Path = DEFAULT_DIR) -> dict:
    """Прочитать таблицу индексов → {'status': 'ok'|'no_data', 'meta':…, 'rows':[…]}."""
    directory = Path(directory)
    csv_path = directory / CSV_NAME
    if not csv_path.exists():
        return {
            "status": "no_data",
            "meta": {},
            "rows": [],
            "reason": (
                f"нет файла {csv_path}: индексы не импортированы. Импорт — "
                "scripts/import_indices.py --pdf <письмо>.pdf; PDF берётся из открытого "
                "API ФГИС ЦС: NormLegalDocPublished?normLegalDocBaseKind=40 → "
                "values/GetFileContent/<uuid>"
            ),
        }
    meta_path = directory / META_NAME
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    rows: list[dict] = []
    with gzip.open(csv_path, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            value = row.get("value", "")
            row["value"] = float(value) if value not in ("", "None") else None
            row["column"] = int(row["column"])
            row["subject_no"] = int(row["subject_no"])
            row["page"] = int(row["page"])
            rows.append(row)
    return {"status": "ok", "meta": meta, "rows": rows}


def index_for(subject: str, data: dict | None = None, *, quarter: str | None = None,
              table: str | None = None, directory: str | Path = DEFAULT_DIR) -> dict:
    """Индексы субъекта РФ, сгруппированные по таблицам письма.

    `table` — подстрока метки или шапки таблицы («Автомобильные дороги»,
    «перевозку грузов»), чтобы не смешивать разные виды индексов в одном ответе.
    """
    data = data if data is not None else load(directory)
    if data.get("status") != "ok":
        return {"status": data.get("status", "no_data"), "reason": data.get("reason", "")}

    wanted = _norm(subject)
    rows = [r for r in data["rows"]
            if quarter is None or _norm(r.get("quarter")) == _norm(quarter)]
    if table:
        needle = _norm(table)
        rows = [r for r in rows
                if needle in _norm(r["table_title"]) or needle in _norm(r.get("table_label", ""))]

    runs = [r for r in rows if _norm(r["subject"]) == wanted]
    if not runs:
        runs = [r for r in rows
                if wanted and (wanted in _norm(r["subject"])
                               or _norm(_unstick(r["subject"])) == wanted)]
    if not runs:
        return {
            "status": "not_found",
            "subject": subject,
            "quarter": quarter,
            "reason": (
                "в разобранных таблицах письма такого субъекта нет. У Минстроя письмо "
                "состоит из нескольких приложений, и часть из них (по объектам "
                "строительства, по элементам прямых затрат) в CSV не разбирается — "
                "она остаётся в PDF"
            ),
            "available_subjects": len({r["subject"] for r in rows}),
            "tables": sorted({(r.get("table_label") or r["table_title"]) for r in rows})[:8],
            "quarters": sorted({r.get("quarter", "") for r in rows if r.get("quarter")}),
        }

    groups: dict[str, list[dict]] = {}
    for r in runs:
        groups.setdefault(r.get("table_label") or r["table_title"], []).append(r)
    tables = []
    for title, items in groups.items():
        items.sort(key=lambda x: x["column"])
        tables.append({
            "table_label": title,
            "table_title": items[0]["table_title"],
            "letter": items[0].get("letter", ""),
            "page": items[0]["page"],
            "values": [{
                "column": it["column"],
                "column_title": it.get("column_title") or f"колонка {it['column']}",
                "value": it["value"],
                "unset": it["value"] is None,
            } for it in items],
        })
    return {
        "status": "found",
        "subject": runs[0]["subject"],
        "subject_no": runs[0]["subject_no"],
        "quarter": runs[0].get("quarter", ""),
        "source": data["meta"].get("source", ""),
        "tables": tables,
        "unset": sum(1 for t in tables for v in t["values"] if v["unset"]),
        "note": "прочерк в письме означает «индекс не установлен», а не ноль"
                if any(v["unset"] for t in tables for v in t["values"]) else "",
    }


def indices_to_markdown(result: dict | None, data: dict | None = None) -> str:
    """Отчёт по индексам: значение + шапка таблицы + страница PDF + границы разбора."""
    data = data if data is not None else load()
    lines = ["# Индексы изменения сметной стоимости", ""]
    if data.get("status") != "ok":
        lines += [f"**Данных нет.** {data.get('reason', '')}", ""]
        return "\n".join(lines)
    meta = data["meta"]
    lines += [
        f"Источник: {meta.get('source', '')}",
        f"Значений в таблице: {meta.get('values', len(data['rows']))} "
        f"(пустых, т.е. прочерк «-»: {sum(1 for r in data['rows'] if r['value'] is None)})",
        "",
    ]
    if result:
        if result.get("status") == "found":
            lines += [f"## {result['subject']} — {result['quarter']}", ""]
            for table in result["tables"]:
                lines += [f"**{table['table_label']}**", "",
                          f"Шапка таблицы в письме: «{table['table_title']}»",
                          f"Письмо: {table['letter']}, стр. {table['page']} PDF", "",
                          "| колонка | значение |", "|---|---|"]
                for value in table["values"]:
                    shown = "— (прочерк, индекс не установлен)" if value["unset"] \
                        else value["value"]
                    lines.append(f"| {value['column_title']} | {shown} |")
                lines.append("")
            if result.get("note"):
                lines += [f"**{result['note']}**", ""]
        else:
            lines += [f"**Не найдено.** {result.get('reason', '')}", ""]
            if result.get("quarters"):
                lines += [f"Есть данные за: {', '.join(result['quarters'])}", ""]
    lines += ["## Границы разбора", "", meta.get("not_parsed_reason", ""), ""]
    return "\n".join(lines)
