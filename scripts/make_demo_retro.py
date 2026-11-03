#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ретро-выпуск: сканы по ранее подписанным актам КС-2 №35 и №37.

Зачем: два акта на 4,4 млн ₽ были подписаны без проверки — портфель честно
показывал «не оценено». Здесь для них собирается выпуск (сканы + эталон пометок)
и отдельная сводка ПТО по периоду акта: тогда пометки сверяются не с октябрьским
журналом, а с количествами того акта, который проверяется.

Создаёт:
  data/demo/retro/ks35/...      — сканы акта №35 (плита + гидроизоляция)
  data/demo/retro/ks37/...      — сканы акта №37 (водопровод)
  data/demo/retro/manifest_*.json
  out/acts/summary_ks35.json    — сводка ПТО по акту №35 (количества = акт)
  out/acts/summary_ks37.json

Запуск:  python3 scripts/make_demo_retro.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from demo_common import MARK_LIBRARY, RED, BLUE, SheetCanvas, render_sheet  # noqa: E402
from pto_agent.cad.drawing import Drawing                                  # noqa: E402

DEMO = ROOT / "data" / "demo"
OUT = DEMO / "retro"
ACTS_OUT = ROOT / "out" / "acts"
ITERATIONS = 5                      # итераций правок на каждый лист комплекта

# листы комплекта: индекс файла в основном выпуске → чертёж
SHEETS = {
    "плита": "data/demo/batch/drawings/sheet_01.dxf",
    "гидро": "data/demo/batch/drawings/sheet_02.dxf",
    "труба": "data/demo/batch/drawings/sheet_04.dxf",
}

# пометки ретро-выпуска: (лист, геометрия из библиотеки, текст, уверенность)
# текст — то, что инженер написал на скане; сверяется с количеством того акта
RETRO_MARKS = {
    "КС-2 №35": [
        ("плита", "approved", "210 м3 — принято по факту", 0.87),      # 200 в акте
        ("гидро", "hydro_1180", "640 м2 — принять по факту", 0.86),    # 600 в акте
    ],
    "КС-2 №37": [
        ("труба", "pipe_du", "180 м — факт по исполнительной схеме", 0.85),  # 200 в акте
    ],
}


def read_prices() -> dict:
    ws = load_workbook(DEMO / "smeta_v1.xlsx").active
    return {str(code).strip(): {"name": name, "unit": unit, "price": float(price)}
            for code, name, unit, qty, price in ws.iter_rows(min_row=2, values_only=True)
            if code}


def read_previous() -> list:
    ws = load_workbook(DEMO / "acts_previous.xlsx").active
    return [{"code": str(code).strip(), "qty": float(qty), "act_id": str(act).strip(),
             "period": str(period).strip()}
            for code, name, unit, qty, act, period in ws.iter_rows(min_row=2, values_only=True)
            if code]


def act_summary(act_id: str, prices: dict, previous: list) -> dict:
    """Сводка ПТО по периоду акта: количества — ровно то, что принято этим актом."""
    rows = [r for r in previous if r["act_id"] == act_id]
    lines, amount = [], 0.0
    for row in rows:
        price = prices[row["code"]]["price"]
        amount += row["qty"] * price
        lines.append({
            "code": row["code"], "name": prices[row["code"]]["name"],
            "unit": prices[row["code"]]["unit"], "price": price,
            "estimate_qty": None, "qty_journal": row["qty"],
            "qty_act": row["qty"], "amount_claim": round(row["qty"] * price, 2),
        })
    return {
        "object": "ЖК «Северный», корпус 2",
        "period": rows[0]["period"] if rows else None,
        "act": act_id,
        "note": "сводка по периоду акта: сверка пометок идёт с количествами этого акта, "
                "а не с текущим журналом",
        "lines": lines,
        "amount_act": round(amount, 2),
        "amount_blocked": 0.0,
        "amount_estimate": 0.0,
        "discrepancies": [],
    }


def slug_of(act_id: str) -> str:
    """«КС-2 №35» → «ks35»: латиница в путях, чтобы файлы не зависели от локали."""
    import re
    numbers = re.findall(r"\d+", act_id)
    return "ks" + (numbers[-1] if numbers else "0")


def build_release(act_id: str, marks: list, prices: dict) -> dict:
    slug = slug_of(act_id)
    out = OUT / slug
    if out.exists():
        shutil.rmtree(out)
    (out / "scans").mkdir(parents=True)
    (out / "truth").mkdir(parents=True)

    # какие листы нужны этому акту — по составу пометок
    sheet_keys = sorted({m[0] for m in marks})
    bases = {}
    for key in sheet_keys:
        dxf = ROOT / SHEETS[key]
        drawing = Drawing.open(dxf)
        bases[key] = (render_sheet(drawing, drawing.inventory()["extents"]), dxf,
                      drawing.inventory()["extents"])
        print(f"  подложка «{key}»: {dxf.name}")

    sheets, n = [], 0
    for key in sheet_keys:
        base, dxf, _ = bases[key]
        per_sheet = [m for m in marks if m[0] == key]
        for it in range(1, ITERATIONS + 1):
            n += 1
            sheet_id = f"{key}-{slug}-v{it:02d}"
            canvas = SheetCanvas(base, bases[key][2], seed=500 + n)
            for _, mark_key, text, conf in per_sheet:
                spec = MARK_LIBRARY[mark_key]
                canvas.mark(spec["mtype"], spec["model_bbox"], text, conf,
                            BLUE if "м3" in text or "м2" in text or " м " in text else RED,
                            note=f"ретро: {act_id}")
            scan = out / "scans" / f"{sheet_id}.png"
            truth = out / "truth" / f"{sheet_id}.json"
            texts = out / "truth" / f"{sheet_id}_texts.json"
            data = canvas.save(scan, truth, texts,
                               extra={"sheet_id": sheet_id, "act_id": act_id,
                                      "sheet": key, "iteration": it})
            sheets.append({
                "sheet_id": sheet_id,
                "title": f"{act_id} · {key} · итерация {it}",
                "scan": str(scan.relative_to(ROOT)),
                "drawing": str(dxf.relative_to(ROOT)),
                "texts": str(texts.relative_to(ROOT)),
                "truth": str(truth.relative_to(ROOT)),
                "note": f"ретро-выпуск по акту {act_id}",
                "expected_marks": len(data["marks"]),
            })

    manifest = {
        "release": f"ЖК «Северный», корпус 2 — ретро-выпуск по акту {act_id}",
        "generated_by": "scripts/make_demo_retro.py",
        "act_id": act_id,
        "sheets": sheets,
    }
    path = OUT / f"manifest_{slug}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {act_id}: {len(sheets)} листов → {path.relative_to(ROOT)}")
    return {"manifest": path, "sheets": len(sheets)}


def main() -> None:
    prices, previous = read_prices(), read_previous()
    ACTS_OUT.mkdir(parents=True, exist_ok=True)
    print("ретро-выпуск по ранее подписанным актам")

    for act_id, marks in RETRO_MARKS.items():
        summary = act_summary(act_id, prices, previous)
        path = ACTS_OUT / f"summary_{slug_of(act_id)}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {act_id}: сводка ПТО → {path.relative_to(ROOT)} "
              f"(акт {summary['amount_act']:,.0f} ₽, позиций {len(summary['lines'])})")
        build_release(act_id, marks, prices)


if __name__ == "__main__":
    main()
