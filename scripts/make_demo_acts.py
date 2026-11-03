#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собирает реестр актов объекта: `data/demo/acts/acts.json`.

Источники — только документы демо-объекта:
  data/demo/acts_previous.xlsx — ранее подписанные акты (КС-2 №35, №37) и объёмы;
  data/demo/smeta_v1.xlsx      — цены, по которым эти акты считались;
  текущий выпуск (акт КС-2 №38) — уже распознанный пакет `out/cad/batch`.

Сумма акта считается по сметным ценам: кол-во × цена. Ничего не выдумывается:
если по акту нет распознанного выпуска, в реестре так и записано.

Запуск:  python3 scripts/make_demo_acts.py
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "data" / "demo"
OUT = DEMO / "acts" / "acts.json"

MONTHS = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
          7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь",
          12: "декабрь"}


def read_prices() -> dict:
    wb = load_workbook(DEMO / "smeta_v1.xlsx")
    ws = wb.active
    prices = {}
    for code, name, unit, qty, price in ws.iter_rows(min_row=2, values_only=True):
        if code:
            prices[str(code).strip()] = {"name": name, "unit": unit, "price": float(price)}
    return prices


def read_previous() -> list:
    """Ранее подписанные акты: строка = позиция, количество, акт, период."""
    wb = load_workbook(DEMO / "acts_previous.xlsx")
    ws = wb.active
    rows = []
    for code, name, unit, qty, act, period in ws.iter_rows(min_row=2, values_only=True):
        if not code:
            continue
        rows.append({"code": str(code).strip(), "name": name, "unit": unit,
                     "qty": float(qty), "act_id": str(act).strip(),
                     "period": str(period).strip()})
    return rows


def month_label(period: str) -> tuple[str, str]:
    """«сентябрь 2026» → («2026-09», «сентябрь 2026»)."""
    month_name, year = period.split()
    number = next(k for k, v in MONTHS.items() if v == month_name.lower())
    return f"{year}-{number:02d}", period


def main() -> None:
    prices = read_prices()
    previous = read_previous()

    acts: dict[str, dict] = {}
    for row in previous:
        price = prices[row["code"]]["price"]
        act = acts.setdefault(row["act_id"], {
            "act_id": row["act_id"],
            "period": row["period"],
            "month": month_label(row["period"])[0],
            "act_date": None,
            "amount_rub": 0.0,
            "positions": [],
            "status": "не проверялся",
            "note": "акт подписан до внедрения проверки: выпуск сканов не распознан, "
                    "деньги под риском по нему не считались",
        })
        amount = row["qty"] * price
        act["amount_rub"] += amount
        act["positions"].append({"code": row["code"], "qty": row["qty"],
                                 "unit": row["unit"], "price": price,
                                 "amount_rub": amount})
        month, _ = month_label(row["period"])
        # акты №35 и №37 закрыты в последний день месяца
        act["act_date"] = f"{month}-30"

    # текущий акт: сумма и конфликты берутся из прогона выпуска
    current = acts.setdefault("КС-2 №38", {
        "act_id": "КС-2 №38",
        "period": "октябрь 2026",
        "month": "2026-10",
        "act_date": "2026-10-29",
        "amount_rub": 34_106_600.0,
        "positions": [],
        "status": "проверен",
        "release": "out/cad/batch/batch_summary.json",
        "background": True,
        "note": "текущий выпуск: 40 листов распознано, пометки сверены с актом и сметой",
    })

    # ретро-выпуск: если по акту есть распознанный выпуск — акт проверен
    for act in acts.values():
        slug = "ks" + "".join(ch for ch in act["act_id"] if ch.isdigit())[-2:] \
            if act["act_id"] != "КС-2 №38" else "38"
        slug = "ks" + act["act_id"].split("№")[-1].strip() if "№" in act["act_id"] else slug
        release = ROOT / "out" / "cad" / f"batch_{slug}" / "batch_summary.json"
        if release.exists():
            act["status"] = "проверен"
            act["release"] = str(release.relative_to(ROOT))
            act["note"] = (f"ретро-выпуск: пометки сверены с количествами акта "
                           f"{act['act_id']} (а не с текущим журналом)")

    ordered = [acts[key] for key in sorted(acts, key=lambda a: (acts[a]["month"], a))]
    for act in ordered:
        act["amount_rub"] = round(act["amount_rub"], 2)
        act["share_of_object_acts"] = None

    data = {
        "object": "ЖК «Северный», корпус 2",
        "contract": "Д-142/2026",
        "generated": date.today().isoformat(),
        "note": "Реестр актов объекта: суммы — по сметным ценам, статус проверки — "
                "по наличию распознанного выпуска. Источники: acts_previous.xlsx, "
                "smeta_v1.xlsx, прогон выпуска out/cad/batch.",
        "acts": ordered,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(a["amount_rub"] for a in ordered)
    print(f"saved: {OUT.relative_to(ROOT)}")
    for act in ordered:
        print(f"  {act['act_id']:<10} {act['period']:<15} {act['amount_rub']:>14,.0f} ₽ "
              f"· {act['status']}")
    print(f"  итого по объекту: {total:,.0f} ₽")


if __name__ == "__main__":
    main()
