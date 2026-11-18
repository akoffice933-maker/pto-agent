#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Позиции КС-2 из разобранной сметы (PDF АРОС) → JSON для `pto_agent forms ks2`.

Что делает и чего не делает:

* берёт из `smeta_parsed.json` (результат `scripts/check_smeta_pdf.py`) позиции
  **работ** (обоснование — сметная норма) с ценой за единицу, посчитанной из
  документа: сумма по строке ÷ количество. Материальные строки (код ресурса
  ФГИС ЦС) в акт не идут — они внутри расценки;
* `qty_journal` = `qty_claim` = объём из сметы. Это **не** подтверждение, что
  работы выполнены: подтверждает журнал работ и обмер, которых у нас нет.
  Поэтому каждая позиция помечается `restrictions` с этой оговоркой, а в акте
  остаётся строка «основание объёма — смета, не журнал»;
* НЕ подставляет период, номер, заказчика и подрядчика, если их нет: пустые
  реквизиты идут в список запросов, а не выдумываются.

    python3 scripts/ks2_from_smeta.py --parsed out/smeta_check/smeta_parsed.json \
        --out out/ks2_from_smeta/lines.json
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
from pathlib import Path

# материальные строки — код ресурса ФГИС ЦС, а не сметная норма
WORK_PREFIXES = ("ГЭСН", "ФЕР", "ТЕР", "ФССЦпг")


def month_bounds(period: str) -> list[str]:
    """'2026-11' → ['2026-11-01', '2026-11-30'].

    Последний день берётся из календаря, а не подставляется «-31»: в ноябре
    30 дней, и «2026-11-31» — несуществующая дата, которую потом не разобрать.
    """
    year, month = (int(x) for x in str(period).split("-")[:2])
    return [f"{year:04d}-{month:02d}-01",
            f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"]


def is_work(code: str) -> bool:
    return str(code or "").upper().startswith(WORK_PREFIXES)


def build_lines(parsed: dict, *, only_priced: bool = True) -> tuple[list[dict], list[dict]]:
    lines: list[dict] = []
    skipped: list[dict] = []
    for item in parsed.get("items") or []:
        code = str(item.get("code") or "")
        if not is_work(code):
            skipped.append({"code": code, "name": item.get("name"),
                            "why": "материал/оборудование: входит в расценку, отдельной "
                                   "строкой акта не предъявляется"})
            continue
        qty = item.get("qty")
        values = item.get("values") or []
        total = values[-1] if values else None
        if qty in (None, 0) or total in (None, 0):
            skipped.append({"code": code, "name": item.get("name"),
                            "why": "нет количества или суммы — цену за единицу не вывести"})
            continue
        price = round(float(total) / float(qty), 2)
        lines.append({
            "code": code,
            "name": str(item.get("name") or "").strip(),
            "unit": str(item.get("unit") or "").strip(),
            "qty_journal": qty,
            "qty_claim": qty,
            "price": price,
            "section": item.get("section"),
            "n": item.get("n"),
            "source_page": item.get("page"),
            "restrictions": [
                "объём взят из сметы, а не из журнала работ: факт выполнения не подтверждён",
            ],
        })
    return lines, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parsed", required=True, help="smeta_parsed.json из check_smeta_pdf.py")
    ap.add_argument("--out", required=True)
    ap.add_argument("--object", default="")
    ap.add_argument("--period", default="", help="YYYY-MM — отчётный период акта")
    a = ap.parse_args(argv)

    parsed = json.loads(Path(a.parsed).read_text(encoding="utf-8"))
    lines, skipped = build_lines(parsed)
    if not lines:
        print("позиций работ с ценой нет — акт собрать не из чего", file=sys.stderr)
        return 1

    payload = {
        "schema": "pto-agent/ks2-lines@1",
        "object": a.object or (parsed.get("header") or {}).get("object") or "",
        "period": (month_bounds(a.period) if a.period else []),
        "vat_rate": None,
        "lines": lines,
        "excluded": skipped,
        "source": "смета (PDF АРОС) → позиции работ; объёмы из сметы, не из журнала",
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    total = sum(l["qty_claim"] * l["price"] for l in lines)
    print(f"{out}: позиций {len(lines)}, сумма без НДС {total:,.2f} ₽".replace(",", " "))
    print(f"не вошло: {len(skipped)} строк (материалы и строки без цены)")
    if skipped:
        for s in skipped[:3]:
            print(f"  — {s['code']}: {s['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
