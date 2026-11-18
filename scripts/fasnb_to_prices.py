#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ФЕР (CSV из `import_fsnb.py`) → выгрузка цен агента (`pto_agent.prices`).

Зачем отдельный шаг: ФЕР — это расценки на *работы* в базисном уровне цен 01.01.2000,
а `prices.lookup` ищет цену *ресурса* по коду. Разделять их нельзя (иначе смета молча
сравнит цену работы с ценой материала), поэтому:

* работы ФЕР попадают в базу цен **с кодом «база/расценка»** (`ФЕР/01-01-001-01`,
  `ФЕРм/08-02-001-01`) и единицей
  измерения из расценки — их и подставляет словарь `takeoff_map`, когда правило ведёт на ФЕР;
* в наименование добавляется префикс «ФЕР-2020», чтобы в смете было видно, что это
  базисная цена 2000 года, а не текущая рыночная;
* дата цены — 2022-02-01 (дата ввода доп. № 9), источник — строка с реквизитами приказов.

    python3 scripts/fasnb_to_prices.py --fer data/fsnb/fer2020.csv --out data/prices_fsnb
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path

SOURCE = ("ФСНБ-2020, ФЕР-2020 (доп. № 9; приказы Минстроя России от 20.12.2021 "
          "№ 961/пр, 962/пр), открытые данные ФГИС ЦС, базисный уровень цен 01.01.2000")
PRICE_DATE = "2022-02-01"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ФЕР CSV → выгрузка цен агента")
    parser.add_argument("--fer", default="data/fsnb/fer2020.csv")
    parser.add_argument("--out", default="data/prices_fsnb/fer2020.csv")
    parser.add_argument("--limit", type=int, default=0, help="ограничить число строк (проверка)")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(Path(args.fer).open(encoding="utf-8")))
    if args.limit:
        rows = rows[: args.limit]
    out = Path(args.out)
    written, skipped = 0, []
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "name", "unit", "price", "vat", "zone", "region", "date"])
        for row in rows:
            try:
                price = float(row["price"])
            except (KeyError, TypeError, ValueError):
                skipped.append({"code": row.get("code"), "reason": "цена не число"})
                continue
            if price <= 0:
                skipped.append({"code": row.get("code"), "reason": f"цена {price} ≤ 0"})
                continue
            # Код в базе цен — «база/код»: `08-02-001-01` есть и в ФЕР (строительные),
            # и в ФЕРм (монтаж оборудования) — это разные расценки с разными ценами.
            base = row.get("base_type") or "ФЕР"
            writer.writerow([f"{base}/{row['code']}", f"{base}-2020: {row['name']}",
                             row["unit"], f"{price:.2f}", "", "", "", PRICE_DATE])
            written += 1
    print(f"строк для импорта {written}, пропущено {len(skipped)}")
    if not args.check:
        Path(args.out).with_suffix(".meta.json").write_text(json.dumps(
            {"source": SOURCE, "price_level": "01.01.2000", "price_date": PRICE_DATE,
             "imported_at": date.today().isoformat(), "written": written,
             "skipped": len(skipped)}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
