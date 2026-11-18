#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Импорт федеральных единичных расценок (ФЕР) из официального XML ФГИС ЦС.

Источник — открытые данные Минстроя России: набор `7707082071-fsnbfer` (ФСНБ-2020)
на портале ФГИС ЦС. XML скачивается безвозмездно и без регистрации:

    GET https://fgiscs.minstroyrf.ru/api/OpenData            → список наборов
    GET https://fgiscs.minstroyrf.ru/api/OpenData/4          → паспорт ФСНБ-2022
    GET https://fgiscs.minstroyrf.ru/api/values/GetFileContent/<uuid> → zip с XML

В архиве ФСНБ-2020 (доп. № 9, действует с 01.02.2022) есть `ФЕР.xml` — единичные
расценки в базисном уровне цен 01.01.2000 с полями `Cost/Salary/Machines/Materials`.
Это ФЕР-2020: та самая редакция, что применяется в сметах сегодня, а не демо-ставки.

Скрипт не «придумывает» цены: если у работы нет `<Prices><Price Cost=…>` — она попадает
в список пропущенных с причиной, а не с нулём.

    python3 scripts/import_fsnb.py --zip /tmp/dl1.bin --dst data/prices --sections 08 20
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

BASE_SOURCE = ("ФСНБ-2020 (ФЕР-2020, доп. № 9, приказы Минстроя России от 20.12.2021 "
               "№ 961/пр, 962/пр), открытые данные ФГИС ЦС 7707082071-fsnbfer")
PRICE_DATE = "2022-02-01"        # дата, с которой действует доп. № 9
PRICE_LEVEL = "01.01.2000"       # базисный уровень цен ФЕР


def _stream(xml_path: Path, out_csv: Path, sections: set[str] | None,
            append: bool = False) -> dict:
    """Разобрать ФЕР.xml потоком (файл 77 МБ, целиком в память не тянем)."""
    stack: list[tuple[str, str, str]] = []
    base_type = _base_type(xml_path)
    collection = ("", "")
    written, skipped, by_section = 0, [], {}
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("a" if append else "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if not append:
            writer.writerow(["base_type", "code", "name", "unit", "price", "salary", "machines",
                             "materials", "machinist", "nr", "sp", "collection", "collection_name"])
        for event, element in ET.iterparse(xml_path, events=("start", "end")):
            if event == "start" and element.tag in ("Section", "NameGroup"):
                stack.append((element.get("Code") or "", element.get("Name") or "",
                              element.get("BeginName") or ""))
            elif event == "end" and element.tag in ("Section", "NameGroup"):
                if stack:
                    stack.pop()
            elif event == "end" and element.tag == "Work":
                book = next(((c, n) for c, n, _ in stack if c and len(c) == 2), ("", ""))
                begin = " ".join(g for _, _, g in stack if g).strip()
                if sections and book[0] not in sections:
                    element.clear()
                    continue
                price = element.find("Prices/Price")
                if price is None or not price.get("Cost"):
                    skipped.append({"code": element.get("Code"),
                                    "reason": "в источнике нет блока Prices/Price",
                                    "name": element.get("EndName") or ""})
                    element.clear()
                    continue
                name = _full_name(element, begin)
                nr_sp = element.find("NrSp/ReasonItem")
                writer.writerow([
                    base_type, element.get("Code") or "", name, element.get("MeasureUnit") or "",
                    price.get("Cost"), price.get("Salary") or "", price.get("Machines") or "",
                    price.get("Materials") or "", price.get("SalaryMach") or "",
                    (nr_sp.get("Nr") if nr_sp is not None else "") or "",
                    (nr_sp.get("Sp") if nr_sp is not None else "") or "",
                    book[0], book[1],
                ])
                written += 1
                by_section[book[0]] = by_section.get(book[0], 0) + 1
                element.clear()
    return {"written": written, "skipped": skipped, "by_section": by_section}


def _base_type(xml_path: Path) -> str:
    """`BaseType` из корня (`ФЕР`, `ФЕРм`, `ФЕРр`, `ФЕРп`, `ФЕРмр`).

    Коды расценок в разных базах совпадают (`08-02-001-01` есть и в строительных ФЕР,
    и в ФЕРм на монтаж оборудования), поэтому без типа базы код — неоднозначный ключ.
    """
    for _, element in ET.iterparse(xml_path, events=("start",)):
        if element.tag == "base":
            return element.get("BaseType") or "ФЕР"
    return "ФЕР"


def _full_name(work, begin: str = "") -> str:
    """Полное наименование расценки: заголовок группы + уточнение работы.

    Без склейки в CSV остаётся «15 м3, группа грунтов 1» — по такому названию расценку
    в смете не найти. Заголовок группы (`NameGroup/@BeginName`) в XML отдельным тегом,
    поэтому он собирается по стеку обхода.
    """
    end = (work.get("EndName") or work.get("Name") or "").strip()
    begin = (begin or "").strip()
    if begin and end and end.lower() not in begin.lower():
        return f"{begin.rstrip(':')}: {end}"
    return begin or end





def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ФЕР из открытых данных ФГИС ЦС → CSV")
    parser.add_argument("--zip", required=True, help="zip с XML ФСНБ (набор 7707082071-fsnbfer)")
    parser.add_argument("--member", action="append", default=None,
                        help="имя XML в архиве; можно повторять (ФЕР.xml, ФЕРм.xml, …)")
    parser.add_argument("--out", default="data/fsnb/fer2020.csv")
    parser.add_argument("--catalog", action="store_true",
                        help="разобрать ресурсный каталог ФСБЦ вместо ФЕР")
    parser.add_argument("--catalog-out", default="data/fsnb/fsbc2022.csv")
    parser.add_argument("--catalog-member", default="ФСБЦ_Мат&Оборуд.xml")
    parser.add_argument("--sections", nargs="*", default=None,
                        help="коды сборников (пусто — все)")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    archive = Path(args.zip)
    members = args.member or ["ФЕР.xml"]
    if args.catalog:
        return _catalog_main(args)
    sections = set(args.sections) if args.sections else None
    stats = {"written": 0, "skipped": [], "by_section": {}}
    for member in members:
        xml_path = _extract(str(archive), member)
        part = _stream(xml_path, Path(args.out), sections, append=bool(stats["written"]))
        stats["written"] += part["written"]
        stats["skipped"] += part["skipped"]
        for code, count in part["by_section"].items():
            stats["by_section"][code] = stats["by_section"].get(code, 0) + count
        print(f"  {member}: {part['written']}")
    print(f"записано расценок {stats['written']}, пропущено {len(stats['skipped'])}; "
          f"сборников {len(stats['by_section'])}")
    for code, count in sorted(stats["by_section"].items())[:12]:
        print(f"  {code}: {count}")
    if not args.check:
        meta = {"source": BASE_SOURCE, "price_level": PRICE_LEVEL, "price_date": PRICE_DATE,
                "imported_at": date.today().isoformat(), "file": str(args.out),
                "written": stats["written"], "skipped": len(stats["skipped"])}
        Path(args.out).with_suffix(".meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


# ───────────────────────── ресурсный каталог ФСБЦ ─────────────────────────
def _stream_catalog(xml_path: Path, out_csv: Path) -> dict:
    """ФСБЦ (`ФСБЦ_Мат&Оборуд.xml`): код ресурса, наименование, единица, цена.

    Карточка ресурса (`<Resource>`) и её цена (`<Price>`) в XML идут соседними элементами,
    цена стоит ПЕРЕД карточкой. Поэтому цена придерживается и применяется к следующей карточке;
    карточка без цены попадает в пропущенные с причиной, а не с нулём.
    """
    written, skipped, pending = 0, [], None
    section = ""                 # держим раздел, пока не началась новая ветка Section
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "name", "unit", "price", "opt_price", "kind", "section"])
        for event, element in ET.iterparse(xml_path, events=("start", "end")):
            if event == "end" and element.tag == "Section":
                if element.get("Name"):
                    section = element.get("Name")     # ближайший (самый глубокий) раздел
                element.clear()
            elif event == "end" and element.tag == "Price":
                pending = (element.get("Cost"), element.get("OptCost"))
                element.clear()
            elif event == "end" and element.tag == "Resource":
                if pending and pending[0]:
                    writer.writerow([element.get("Code") or "", element.get("Name") or "",
                                     element.get("MeasureUnit") or "", pending[0], pending[1] or "",
                                     element.get("Kind") or "", section])
                    written += 1
                else:
                    skipped.append({"code": element.get("Code"),
                                    "reason": "в источнике нет цены для карточки ресурса"})
                pending = None
                element.clear()
    return {"written": written, "skipped": skipped}


def _catalog_main(args) -> int:
    xml_path = _extract(args.zip, args.catalog_member)
    stats = _stream_catalog(xml_path, Path(args.catalog_out))
    print(f"ресурсов в каталоге {stats['written']}, без цены {len(stats['skipped'])}")
    if not args.check:
        Path(args.catalog_out).with_suffix(".meta.json").write_text(json.dumps(
            {"source": BASE_SOURCE, "price_level": PRICE_LEVEL, "price_date": PRICE_DATE,
             "imported_at": date.today().isoformat(), "file": str(args.catalog_out),
             "written": stats["written"], "skipped": len(stats["skipped"])},
            ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def _extract(archive: str, member_name: str) -> Path:
    work_dir = Path("/tmp/fsnb_extract")
    work_dir.mkdir(exist_ok=True)
    target = work_dir / member_name.replace("/", "_")
    if target.exists():
        return target
    with zipfile.ZipFile(archive) as zf:
        member = next((n for n in zf.namelist() if n.endswith(member_name)), None)
        if member is None:
            raise SystemExit(f"в архиве нет {member_name}")
        with zf.open(member) as src, target.open("wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
    return target


if __name__ == "__main__":
    raise SystemExit(main())
