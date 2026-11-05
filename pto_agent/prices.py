# -*- coding: utf-8 -*-
"""Цены ресурсов: импорт выгрузки с журналом версий — «цена с источником или никакой».

Смета и закупка не могут работать на «цене из головы». Поэтому цена здесь — это всегда запись
с пятью обязательными атрибутами:

* значение и единица измерения;
* источник (ФГИС ЦС, письмо с индексами Минстроя, прайс поставщика, КП);
* дата, на которую цена действует (и дата импорта отдельно);
* регион и ценовая зона (для бюджетных строек это часть условия);
* версия выгрузки — чтобы через месяц было видно, что цифра поменялась именно потому,
  что обновилась выгрузка, а не «кто-то поправил вручную».

Отсюда две вещи, которых модуль принципиально не делает: не подставляет цену «похожего»
ресурса вместо отсутствующего и не берёт цену из более свежей версии, если спрашивают дату
раньше. Нет цены — так и говорит: `not_found` со списком ближайших версий, где смотрели.

Формат выгрузки, который читаем: xlsx/csv с колонками (регистр и порядок не важны)
`code`, `name`, `unit`, `price` и необязательными `vat`, `zone`, `region`, `date`.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path

REQUIRED_COLUMNS = ("code", "name", "unit", "price")
COLUMN_ALIASES = {
    "код": "code", "code": "code", "код ресурса": "code", "кодрес": "code",
    "наименование": "name", "name": "name", "ресурс": "name",
    "ед. изм.": "unit", "ед.изм.": "unit", "единица": "unit", "unit": "unit", "ед": "unit",
    "цена": "price", "цена, руб.": "price", "price": "price", "стоимость": "price",
    "ндс": "vat", "vat": "vat",
    "зона": "zone", "zone": "zone", "регион": "region", "region": "region",
    "дата": "date", "date": "date",
}
DEFAULT_INDEX = Path("data/prices/index.json")


def _num(value) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace("\xa0", " ").replace(" ", "").replace(",", ".")
    try:
        return round(float(text), 4)
    except ValueError:
        return None


def _norm_header(text: str) -> str:
    return COLUMN_ALIASES.get(str(text or "").strip().lower().replace("ё", "е"), "")


def read_rows(path: str | Path) -> tuple[list[dict], list[str]]:
    """Прочитать выгрузку цен → (строки, проблемы чтения)."""
    path = Path(path)
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = [[c for c in row] for row in ws.iter_rows(values_only=True)]
    elif path.suffix.lower() in {".csv", ".txt"}:
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = [row for row in csv.reader(f)]
    else:
        raise ValueError(f"неизвестный формат выгрузки цен: {path.suffix} "
                         f"(поддержаны xlsx и csv)")

    header_index, mapping = None, {}
    for i, row in enumerate(rows[:10]):
        candidate = {_norm_header(cell): j for j, cell in enumerate(row) if _norm_header(cell)}
        if set(REQUIRED_COLUMNS) <= set(candidate):
            header_index, mapping = i, candidate
            break
    if header_index is None:
        return [], ["не найдены обязательные колонки: " + ", ".join(REQUIRED_COLUMNS)]

    problems: list[str] = []
    items: list[dict] = []
    for row in rows[header_index + 1:]:
        if not row or all(cell in (None, "") for cell in row):
            continue
        def get(field, default=None):
            index = mapping.get(field)
            return row[index] if index is not None and index < len(row) else default
        price = _num(get("price"))
        code = str(get("code") or "").strip()
        name = str(get("name") or "").strip()
        if not code or price is None:
            problems.append(f"строка пропущена: код={code or '—'}, цена={get('price')!r}")
            continue
        items.append({"code": code, "name": name, "unit": str(get("unit") or "").strip(),
                      "price": price, "vat": _num(get("vat")),
                      "zone": str(get("zone") or "").strip(),
                      "region": str(get("region") or "").strip(),
                      "date": str(get("date") or "").strip()})
    return items, problems


def import_prices(path: str | Path, *, source: str, price_date: str, region: str = "",
                  zone: str = "", out_dir: str | Path = "data/prices",
                  actor: str = "agent:prices", audit_log=None) -> dict:
    """Импортировать выгрузку как новую версию. Файл версии — снимок, а не «текущее состояние»."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items, problems = read_rows(path)
    if not items:
        return {"ok": False, "error": "в выгрузке нет пригодных строк",
                "problems": problems, "source": source}

    for item in items:
        item["region"] = item["region"] or region
        item["zone"] = item["zone"] or zone
        item["date"] = item["date"] or price_date
        item["source"] = source

    version_id = f"{price_date}-{_slug(source)}" + (f"-{_slug(region)}" if region else "")
    body = {"version": version_id, "source": source, "price_date": price_date,
            "imported_at": date.today().isoformat(), "region": region, "zone": zone,
            "file": Path(path).name, "items": items}
    digest = hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()[:16]
    body["digest"] = digest
    version_path = out / f"prices_{version_id}.json"
    version_path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")

    index_path = out / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else \
        {"schema": "pto-agent/prices@1", "versions": []}
    index["versions"] = [v for v in index["versions"] if v["version"] != version_id]
    index["versions"].append({"version": version_id, "source": source,
                              "price_date": price_date, "region": region, "zone": zone,
                              "file": version_path.name, "items": len(items),
                              "digest": digest, "imported_at": body["imported_at"]})
    index["versions"].sort(key=lambda v: (v["price_date"], v["version"]))
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")

    if audit_log is not None:
        audit_log.append(actor, "agent", "prices.imported", f"prices:{version_id}",
                         {"source": source, "price_date": price_date, "region": region,
                          "zone": zone, "items": len(items), "digest": digest,
                          "file": str(version_path), "problems": len(problems)})

    return {"ok": True, "version": version_id, "items": len(items), "problems": problems,
            "file": str(version_path), "digest": digest, "index": str(index_path),
            "region": region, "zone": zone, "price_date": price_date, "source": source}


def _slug(text: str) -> str:
    table = {" ": "-", "/": "-", ".": "-", ",": "", "«": "", "»": ""}
    for a, b in table.items():
        text = text.replace(a, b)
    return "".join(ch for ch in text.lower() if ch.isalnum() or ch == "-")[:40] or "x"


def load_index(out_dir: str | Path = "data/prices") -> dict:
    path = Path(out_dir) / "index.json"
    if not path.exists():
        return {"schema": "pto-agent/prices@1", "versions": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_version(version: dict | str, out_dir: str | Path = "data/prices") -> dict:
    if isinstance(version, str):
        match = next((v for v in load_index(out_dir)["versions"] if v["version"] == version), None)
        if not match:
            raise KeyError(f"версия {version} не найдена в индексе")
        version = match
    return json.loads((Path(out_dir) / version["file"]).read_text(encoding="utf-8"))


def lookup(code: str = "", name: str = "", *, as_of: str | None = None,
           out_dir: str | Path = "data/prices") -> dict:
    """Найти цену по коду (или точному наименованию) на дату. Нет цены — так и говорит.

    `as_of` ограничивает версии сверху: запрос «цена на 1 сентября» не должен получить цену
    из выгрузки за декабрь.
    """
    index = load_index(out_dir)
    versions = [v for v in index["versions"] if not as_of or v["price_date"] <= as_of]
    searched = [v["version"] for v in versions]
    if not versions:
        return {"found": False, "reason": "нет версий цен на эту дату", "searched": [],
                "code": code, "name": name, "as_of": as_of}

    for version in reversed(versions):            # свежая подходящая версия первой
        doc = load_version(version, out_dir)
        for item in doc["items"]:
            if (code and item["code"] == code) or (name and item["name"] == name):
                return {"found": True, "price": item["price"], "unit": item["unit"],
                        "name": item["name"], "code": item["code"], "vat": item.get("vat"),
                        "source": doc["source"], "price_date": doc["price_date"],
                        "region": doc.get("region"), "zone": doc.get("zone"),
                        "version": doc["version"], "file": version["file"],
                        "digest": doc.get("digest"), "as_of": as_of,
                        "verified": True}
    return {"found": False, "reason": "ресурс не найден ни в одной версии на эту дату",
            "searched": searched, "code": code, "name": name, "as_of": as_of}


def price_history(code: str = "", name: str = "", *,
                  out_dir: str | Path = "data/prices") -> dict:
    """История цены по версиям: что менялось, когда и на сколько процентов."""
    index = load_index(out_dir)
    points: list[dict] = []
    for version in index["versions"]:
        doc = load_version(version, out_dir)
        item = next((i for i in doc["items"]
                     if (code and i["code"] == code) or (name and i["name"] == name)), None)
        if item:
            points.append({"version": doc["version"], "price_date": doc["price_date"],
                           "source": doc["source"], "price": item["price"],
                           "unit": item["unit"], "region": doc.get("region")})
    changes = []
    for prev, cur in zip(points, points[1:]):
        if prev["price"] and cur["price"] != prev["price"]:
            changes.append({"from_version": prev["version"], "to_version": cur["version"],
                            "from": prev["price"], "to": cur["price"],
                            "delta": round(cur["price"] - prev["price"], 2),
                            "percent": round((cur["price"] - prev["price"]) / prev["price"] * 100, 2)})
    return {"code": code, "name": name, "points": points, "changes": changes,
            "found": bool(points)}


def prices_to_markdown(lookup_result: dict | None = None, history: dict | None = None,
                       index: dict | None = None) -> str:
    lines: list[str] = []
    if lookup_result is not None:
        r = lookup_result
        lines.append(f"# Цена: {r.get('name') or r.get('code') or '—'}")
        lines.append("")
        if r.get("found"):
            lines += [f"- **{r['price']} руб.** за {r['unit']}",
                      f"- источник: {r['source']}, дата цены: {r['price_date']}",
                      f"- версия: `{r['version']}` (digest {r.get('digest', '—')})",
                      f"- регион/зона: {r.get('region') or '—'} / {r.get('zone') or '—'}"]
        else:
            lines += [f"- цены нет: {r.get('reason')}",
                      f"- смотрели версии: {', '.join(r.get('searched') or []) or '—'}",
                      "- подставлять «похожий» ресурс агент не будет — нужна выгрузка или КП"]
        lines.append("")
    if history and history.get("points"):
        lines += ["| Версия | Дата цены | Цена | Изменение |", "|---|---|---|---|"]
        prev = None
        for point in history["points"]:
            change = ""
            if prev is not None and prev["price"]:
                delta = point["price"] - prev["price"]
                if delta:
                    change = f"{delta:+.2f} ({delta / prev['price'] * 100:+.1f} %)"
            lines.append(f"| {point['version']} | {point['price_date']} | {point['price']} | "
                         f"{change or '—'} |")
            prev = point
        lines.append("")
    if index is not None:
        lines.append("# Версии цен")
        lines.append("")
        lines += ["| Версия | Источник | Дата цены | Ресурсов | Импорт |", "|---|---|---|---|---|"]
        for version in index.get("versions") or []:
            lines.append(f"| {version['version']} | {version['source']} | {version['price_date']} | "
                         f"{version['items']} | {version.get('imported_at', '—')} |")
        if not index.get("versions"):
            lines.append("| — | версий нет | — | — | — |")
        lines.append("")
    return "\n".join(lines)
