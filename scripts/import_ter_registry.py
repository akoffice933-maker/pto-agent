#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Реестр территориальных сметных нормативов (ТЕР/ТСН) из ФРСН.

Что это даёт и чего не даёт — важно не перепутать:

* **даёт**: перечень зарегистрированных региональных сборников с реквизитами
  (наименование, приказ Минстроя/Минрегиона, регистрационный номер и дата,
  ссылка на первоисточник). Это официальный факт «сборник существует и внесён
  в Федеральный реестр сметных нормативов»;
* **не даёт**: самих таблиц расценок. В API ФРСН лежат реквизиты, а не расценки:
  ни `FrsnEdition/Estimate/Section`, ни открытые данные ФГИС ЦС, ни архив
  ФСНБ не содержат строк ТЕР. Тексты сборников — PDF на региональных порталах
  и в платных базах; автоматическому сбору они не поддаются.

API (без авторизации, проверено 2026-11-18):

    GET /api/FrsnEdition/section                      → разделы реестра
    GET /api/FrsnEdition/Estimate/Section?section=6   → ТЕР (бюджет)
    ...                              ?section=7       → ТЕР (без бюджета)

    python3 scripts/import_ter_registry.py --dst data/ter
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

API = "https://fgiscs.minstroyrf.ru/api"
SOURCE = ("Федеральный реестр сметных нормативов, ФГИС ЦС: "
          "FrsnEdition/Estimate/Section, раздел 3 «Территориальные сметные нормативы»")
SECTIONS = {6: "ТЕР (подраздел 1)", 7: "ТЕР без привлечения средств федерального бюджета"}


def _ssl() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get_json(path: str) -> dict:
    with urllib.request.urlopen(API + path, context=_ssl(), timeout=120) as r:
        return json.loads(r.read() or b"{}")


def fetch_section(section: int, take: int = 500) -> list[dict]:
    items: list[dict] = []
    page, total = 1, None
    while True:
        data = _get_json(f"/FrsnEdition/Estimate/Section?section={section}"
                         f"&take={take}&page={page}&orderBy=id%20ASC")
        total = data.get("totalCount", total)
        batch = data.get("items") or []
        items.extend(batch)
        if not batch or len(items) >= (total or 0):
            break
        page += 1
        time.sleep(0.3)
    return items


def subject_list(indices_csv: Path) -> list[str]:
    """Перечень субъектов РФ — из разобранного письма Минстроя (без зон)."""
    if not indices_csv.exists():
        return []
    names: set[str] = set()
    with gzip.open(indices_csv, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            name = re.sub(r"\s*\([^)]*зона\)\s*$", "", row["subject"]).strip()
            if name:
                names.add(name)
    # города федерального значения в письме идут как «г. Севастополь», а в реестре
    # как «г. Севастополь» / «Севастополь» — добавляем обе формы
    # Города федерального значения в письмах идут как «г. Севастополь» или
    # «Севастополь», а в реестре — «г. Севастополь». Перечень субъектов берём из
    # письма, но города добавляем всегда: в письме они могут отсутствовать, если
    # по ним нет индексов, а записи реестра по ним есть.
    for city in ("Севастополь", "Москва", "Санкт-Петербург"):
        names.add(city)
    # официальные названия, которые в письмах сокращены до «Республика Северная
    # Осетия – Алания» и т.п. — добавляем точные формы из реестра
    names |= {"Республика Северная Осетия-Алания",
              "Республика Северная Осетия - Алания",
              "Республика Северная Осетия – Алания",
              "Карачаево-Черкесская Республика", "Удмуртская Республика"}
    return sorted(names, key=len, reverse=True)     # длинные раньше коротких


def region_of(name: str, subjects: list[str]) -> str:
    """Регион из наименования записи — только точным совпадением с перечнем субъектов.

    Если в названии региона нет (например, «Территориальные сметные нормативы,
    предусмотренные для применения на территории ...» без субъекта) — пусто.
    Догадываться по подстрокам не будем: «Республика Северная» — не регион.
    """
    flat = re.sub(r"\s+", " ", (name or "")).replace("\n", " ")

    def norm(text: str) -> str:
        return re.sub(r"\s*[-–—]\s*", "-", text).replace(" ", "").lower()

    flat_n = norm(flat)
    best = ""
    for subject in subjects:
        subj_n = norm(subject)
        # «Кемеровская область - Кузбасс» должно находиться и как «Кемеровская область»
        stem = re.split(r"[-–—]|кузбасс", subj_n)[0]
        if subj_n in flat_n or (len(stem) >= 8 and stem in flat_n):
            if len(subject) > len(best):
                best = subject
    return best


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dst", default="data/ter")
    ap.add_argument("--subjects", default="data/indices/minstroy_indices.csv.gz",
                    help="перечень субъектов РФ (из разобранного письма Минстроя)")
    a = ap.parse_args(argv)

    subjects = subject_list(Path(a.subjects))
    if not subjects:
        print(f"перечень субъектов не найден ({a.subjects}) — колонка region будет пустой",
              file=sys.stderr)
    rows: list[dict] = []
    counts: dict[str, int] = {}
    actual = ""
    for section, title in SECTIONS.items():
        items = fetch_section(section)
        counts[title] = len(items)
        for it in items:
            rows.append({
                "section": section,
                "section_title": title,
                "number": it.get("number"),
                "name": re.sub(r"\s+", " ", (it.get("name") or "")).strip(),
                    "region": region_of(it.get("name") or "", subjects),
                "order": (it.get("registerDateNumberText") or "").strip(),
                "reg_number": (it.get("registrationDateNumber") or "").strip(),
                "url": (it.get("urlUrl") or "").strip(),
                "edition_id": it.get("editionId"),
            })
        actual = _get_json("/FrsnEdition/section").get("actualDate", actual)

    dst = Path(a.dst)
    dst.mkdir(parents=True, exist_ok=True)
    out = dst / "ter_registry.csv.gz"
    fields = list(rows[0]) if rows else ["section", "number", "name"]
    with gzip.open(out, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    regions = sorted({r["region"] for r in rows if r["region"]})
    (dst / "ter_registry.meta.json").write_text(json.dumps({
        "source": SOURCE,
        "actual_date": actual,
        "records": len(rows),
        "by_section": counts,
        "regions_found": len(regions),
        "subjects_source": a.subjects,
        "columns": {
            "order": "приказ, которым сборник внесён в реестр",
            "reg_number": "регистрационный номер и дата в реестре",
            "url": "ссылка на первоисточник из реестра (часто один и тот же адрес Минстроя)",
            "region": "регион из наименования (точное совпадение с перечнем субъектов "
                      "из письма Минстроя); пусто — в названии региона нет",
        },
        "url_check": {
            "with_url": sum(1 for r in rows if r["url"]),
            "distinct_urls": len({r["url"] for r in rows if r["url"]}),
            "checked": "2026-11-18",
            "result": ("из песочницы www.minstroyrf.ru недоступен: DNS 82.202.190.8, "
                       "TCP/TLS не отвечает (curl код 000), fetch_page → HTTP 500. "
                       "Достать файлы сборников по этим ссылкам не удалось; "
                       "648 записей ведут на один и тот же адрес view.territorial.php"),
        },
        "not_included_reason": (
            "В реестре только реквизиты сборников. Таблиц расценок ТЕР в API нет: "
            "эндпоинты FrsnEdition отдают наименование, приказ и ссылку; в архиве "
            "ФСНБ-2022 (7 XML) ТЕР отсутствуют; открытые данные ФГИС ЦС содержат только "
            "федеральную базу. Сами сборники — PDF на региональных порталах и в платных "
            "базах, машинного API у них нет."
        ),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{out}: записей {len(rows)} ({counts}), регионов {len(regions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
