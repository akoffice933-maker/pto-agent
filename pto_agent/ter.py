# -*- coding: utf-8 -*-
"""Территориальные сметные нормативы: что о них известно точно, а что — нет.

Разделение принципиальное, и модуль его не размывает:

* `registry()` — реквизиты сборников из Федерального реестра сметных нормативов
  (ФРСН, открытый API ФГИС ЦС): наименование, приказ, регистрационный номер,
  ссылка на первоисточник. Это ответ на вопрос «какой сборник по региону вообще
  существует и чем он утверждён»;
* расценок здесь нет и быть не может: в API ФРСН лежат только реквизиты, в архиве
  ФСНБ территориальных расценок нет, открытые данные ФГИС ЦС — федеральная база.
  Поэтому `prices()` не существует, а `registry()` в каждом ответе возвращает
  `prices_available: False` с причиной — чтобы отсутствие не приняли за ноль.

Данные: `data/ter/ter_registry.csv.gz` + `ter_registry.meta.json`
(`scripts/import_ter_registry.py`).
"""
from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

DEFAULT_DIR = Path("data/ter")
CSV_NAME = "ter_registry.csv.gz"
META_NAME = "ter_registry.meta.json"


def _fold(text: str) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _stem(text: str) -> str:
    """Основа первого значимого слова: «Краснодарский край» → «краснодар».

    В реестре регион часто стоит в косвенном падеже («Краснодарского края»),
    поэтому ищем по основе, а не по точной форме. Основа короче 6 букв не
    используется — иначе «Крым» начнёт совпадать с чем попало.
    """
    for word in _fold(text).split():
        if word in ("республика", "область", "край", "город", "г.", "автономный",
                    "округ", "федерального", "значения", "саха", "якутия"):
            continue
        return word[:9]
    return ""


def _matches(region: str, name: str) -> bool:
    region_f, name_f = _fold(region), _fold(name)
    if not region_f:
        return False
    if region_f in name_f:
        return True
    stem = _stem(region)
    return len(stem) >= 6 and stem in name_f


def registry(directory: str | Path = DEFAULT_DIR) -> dict:
    """Записи реестра ТЕР с реквизитами; при отсутствии файла — причина, не пустота."""
    directory = Path(directory)
    csv_path = directory / CSV_NAME
    if not csv_path.exists():
        return {
            "status": "no_data",
            "rows": [],
            "reason": (f"нет файла {csv_path}: реестр не импортирован. Импорт — "
                       "scripts/import_ter_registry.py (API ФРСН FrsnEdition/Estimate/Section)"),
            "prices_available": False,
        }
    meta_path = directory / META_NAME
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    with gzip.open(csv_path, "rt", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return {
        "status": "ok",
        "rows": rows,
        "meta": meta,
        "records": len(rows),
        "regions": sorted({r["region"] for r in rows if r.get("region")}),
        "prices_available": False,
        "reason_prices": meta.get("not_included_reason", ""),
    }


def for_region(region: str, data: dict | None = None, *,
               directory: str | Path = DEFAULT_DIR) -> dict:
    """Сборники региона по реестру. Расценок в ответе нет — и это сказано явно."""
    data = data if data is not None else registry(directory)
    if data.get("status") != "ok":
        return {"status": data.get("status"), "reason": data.get("reason", ""),
                "prices_available": False}
    rows = [r for r in data["rows"] if _matches(region, r["name"])]
    if not rows:
        return {
            "status": "not_found",
            "region": region,
            "reason": ("в наименованиях записей реестра регион не указан: часть записей "
                       "внесена общей формулировкой без субъекта РФ"),
            "regions_in_registry": data["regions"],
            "prices_available": False,
        }
    return {
        "status": "found",
        "region": region,
        "records": len(rows),
        "items": [{"name": r["name"], "order": r["order"],
                   "reg_number": r["reg_number"], "url": r["url"]} for r in rows],
        "prices_available": False,
        "reason_prices": data.get("reason_prices", ""),
        "source": data["meta"].get("source", ""),
        "actual_date": data["meta"].get("actual_date", ""),
    }


def registry_to_markdown(result: dict | None, data: dict | None = None) -> str:
    data = data if data is not None else registry()
    lines = ["# Территориальные сметные нормативы: реестр", ""]
    if data.get("status") != "ok":
        lines += [f"**Данных нет.** {data.get('reason', '')}", ""]
        return "\n".join(lines)
    meta = data["meta"]
    lines += [f"Источник: {meta.get('source', '')}",
              f"Актуальность реестра: {meta.get('actual_date', '')}",
              f"Записей: {meta.get('records', data.get('records'))}, "
              f"регионов распознано: {len(data.get('regions') or [])}", ""]
    if result:
        if result.get("status") == "found":
            lines += [f"## {result['region']} — сборников {result['records']}", "",
                      "| наименование | приказ | рег. номер |", "|---|---|---|"]
            for item in result["items"]:
                lines.append(f"| {item['name'][:120]} | {item['order']} | "
                             f"{item['reg_number']} |")
            lines.append("")
        else:
            lines += [f"**Не найдено.** {result.get('reason', '')}", ""]
    lines += ["## Чего здесь нет", "", meta.get("not_included_reason", ""), ""]
    return "\n".join(lines)
