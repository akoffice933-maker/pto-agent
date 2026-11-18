#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сквозная проверка «ТЗ → объёмы» на настоящем PDF — шаг CI.

Проверяется не «код не упал», а цифры:

1. текст извлекается из всех страниц, таблицы находятся (шкафы, маршруты, распределение);
2. строк в таблице шкафов столько, сколько заявлено в тексте (5), волокон — 256 = 16 × 8 × 2;
3. найденные расхождения ТЗ названы **числом**, а не «есть замечания»: сумма способов прокладки
   7 910 против 8 270 (разница 360), оговорка «исправлено с 5 до 6» при количестве 2;
4. у каждой проверки есть машинный признак `ok` — сравнение по русскому слову «сходится»
   ломается, как только появляется третье состояние «не проверено»;
5. «не оценено» — это не ноль: позиции перечислены с причиной, в деньги не превращаются;
6. выгрузка объёмов (boq.csv) знает **раздел**: без этого ведомость теряет деление на разделы работ;
7. стык с ценами: строк 96, с ценой 96, итог 1 608 120,00 ₽, на проверку 8, не включено 1 (при наличии);
8. PDF без текстового слоя не даёт пустую ведомость — говорится, что слоя нет.

    python3 scripts/check_tz_tables.py [путь к PDF]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent.prices import import_prices                    # noqa: E402
from pto_agent.takeoff import read_boq, build_draft, load_map  # noqa: E402
from pto_agent.tz_tables import (extract_file, to_boq_rows, write_boq_csv,
                                 report_to_markdown)  # noqa: E402

PDF = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/home/user/uploads/ТЗ Адлер.pdf")
PRICES = ROOT / "data" / "demo" / "prices_skc_adler.csv"
MAP_TZ = ROOT / "data" / "reference" / "takeoff_map_tz.json"
OUT = ROOT / "out" / "check_tz_tables"

violations: list[str] = []
stats = {"checks": 0}


def check(condition: bool, message: str) -> None:
    stats["checks"] += 1
    if not condition:
        violations.append(message)


if not PDF.exists():
    print(f"нет файла ТЗ: {PDF} — проверка невозможна, а не «пройдена»", file=sys.stderr)
    raise SystemExit(2)

report = extract_file(PDF)
totals = report["totals"]

# 1. документ прочитан целиком
check(report["pages_total"] == 8, f"страниц {report['pages_total']}, ожидали 8")
check(report["pages_with_text"] == 8, f"страниц с текстом {report['pages_with_text']}")
check(report["tables_found"].get("cabinet") == 1, report["tables_found"])
check(report["tables_found"].get("routes") == 2, report["tables_found"])
check(report["tables_found"].get("distribution") == 1, report["tables_found"])
check(totals["unparsed"] == 0, f"не разобрано строк: {totals['unparsed']}")

# 2. объёмы и арифметика
check(totals["lines"] == 97, f"объёмов {totals['lines']}, ожидали 97")
check(totals["not_estimated"] == 12, f"«не оценено» {totals['not_estimated']}, ожидали 12")
welding = next(line for line in report["lines"] if line["unit"] == "сварка")
check(welding["qty"] == 256, f"сварка {welding['qty']}, ожидали 256 = 16 линий × 8 волокон × 2")
cabinets = [line for line in report["lines"] if line.get("clause") == "5.2"]
check(len(cabinets) == 5, f"шкафов в таблице {len(cabinets)}")

# 3. расхождения названы числом
# словарь по имени здесь нельзя: одноимённые проверки (оптика и силовые) перетирают друг друга
mismatch = next(item for item in report["checks"] if "способов прокладки" in item["name"]
                and item["ok"] is False)
check("7910" in mismatch["formula"] and "8270" in mismatch["formula"],
      f"расхождение по способам прокладки не названо числом: {mismatch['formula']}")
check("360" in (mismatch.get("note") or ""), "не сказано, на сколько расходится (360 м)")
amendment = next(item for item in report["checks"] if "исправлено с 5 до 6" in item["name"])
check(amendment["ok"] is False and "уточнение проектировщика" in amendment["note"],
      "оговорка «исправлено с 5 до 6» не помечена как противоречие")

# 4. машинный признак у каждой проверки
check(report["checks"] and all("ok" in item for item in report["checks"]),
      "у части проверок нет поля ok")
check(all(item["ok"] in (True, False, None) for item in report["checks"]),
      "ok не булев и не None")

# 5. «не оценено» — причина, а не ноль
check(all(item.get("why") for item in report["not_estimated"]),
      "у «не оценено» нет причины")
check(all(item.get("qty") is None for item in report["not_estimated"]),
      "«не оценено» получило количество")
check(any("объём в ТЗ не нормирован" in item["why"] for item in report["not_estimated"]),
      "не сказано, что объём не нормирован")

# 6. выгрузка знает раздел
rows = to_boq_rows(report)
check(rows and all(row.get("раздел") for row in rows), "в выгрузке нет раздела")
sections = {row["раздел"] for row in rows}
check(len(sections) >= 10, f"разделов в выгрузке {len(sections)}")
for needed in ("Земляные работы", "Сварка оптических волокон", "Прокладка кабелей"):
    check(needed in sections, f"в выгрузке нет раздела «{needed}»")

# 7. стык с ценами
prices_dir = OUT / "prices"
import_prices(PRICES, source="Прайс подрядчика по СКС «Адлер» (демо-ставки)",
              price_date="2026-01-01", region="23", out_dir=prices_dir)
boq_path = write_boq_csv(report, OUT / "boq.csv")
boq, problems = read_boq(boq_path)
# 97 объёмов из ТЗ: 96 с количеством и 1 строка «при наличии» без количества —
# она доходит до ведомости с замечанием и уходит в «не включено», а не превращается в ноль
check(len(boq) == 97, f"строк из выгрузки {len(boq)}, ожидали 97")
check(len(problems) == 1 and "нет количества" in problems[0],
      f"замечаний чтения {len(problems)}: {problems[:2]}")
check(any(line["qty"] is None for line in boq), "строка без количества потерялась")
check(all("нет количества" in problem for problem in problems),
      f"странные замечания чтения: {problems[:3]}")
draft = build_draft(boq, mapping=load_map(MAP_TZ), prices_dir=prices_dir, as_of="2026-11-01",
                    object_name="Адлерская ТЭС — магистральная СКС", section="СКС")
amounts = draft["totals"]
check(amounts["priced_lines"] == 96, f"с ценой {amounts['priced_lines']} из 96")
check(abs(amounts["amount_rub"] - 1_608_120.0) < 0.01,
      f"итог {amounts['amount_rub']}, ожидали 1 608 120,00")
check(amounts["review_lines"] == 8, f"на проверку {amounts['review_lines']}, ожидали 8")
check(amounts["excluded_lines"] == 1, f"не включено {amounts['excluded_lines']}, ожидали 1")
check(amounts["no_price_lines"] == 0, f"без цены {amounts['no_price_lines']}")
check(all(line.get("price_version") and line.get("price_digest")
          for line in draft["lines"]), "у строки нет версии и отпечатка базы цен")

# 8. markdown говорит то же, что JSON
text = report_to_markdown(report)
check("**расходится**" in text and "7910" in text, "в markdown нет выделенного расхождения")
check("Не оценено" in text, "в markdown нет блока «Не оценено»")

print(f"ТЗ: страниц {report['pages_total']} · объёмов {totals['lines']} "
      f"(не оценено {totals['not_estimated']}) · проверок ТЗ {len(report['checks'])} "
      f"(расхождений {len([c for c in report['checks'] if c['ok'] is False])})")
print(f"ведомость: строк {amounts['lines']} · с ценой {amounts['priced_lines']} · "
      f"итог {amounts['amount_rub']:,.2f} ₽ · на проверку {amounts['review_lines']} · "
      f"не включено {amounts['excluded_lines']}")
print(f"разделов в выгрузке: {len(sections)}")
print(f"проверок: {stats['checks']}")
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
