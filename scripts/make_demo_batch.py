# -*- coding: utf-8 -*-
"""Демо-выпуск: 40 листов со сканами и пометками (пакетная обработка).

Структура выпуска — как в жизни: 4 разных листа комплекта (разные шифры и виды
работ) × 10 сканов на каждый (разные итерации правок). Итого 40 листов в работе:
у каждого свой набор пометок, часть пометок без текста, часть конфликтует с КС-2.

Создаёт:
  data/demo/batch/drawings/sheet_NN.dxf      — 4 чертежа комплекта
  data/demo/batch/scans/<id>.png             — 40 сканов
  data/demo/batch/truth/<id>.json            — эталон пометок каждого скана
  data/demo/batch/manifest.json              — манифест для `cad scan-batch`
"""
import json
import random
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from demo_common import (MARK_LIBRARY, RED, BLUE, SheetCanvas, apply_mark,  # noqa: E402
                         render_sheet)
from pto_agent.cad.drawing import Drawing                                   # noqa: E402

SRC_DXF = ROOT / "drawings" / "korpus2_plan_v1.dxf"
OUT = ROOT / "data" / "demo" / "batch"
TITLEBLOCK = "ШТАМП_КОРП_A"

# 4 листа комплекта: шифр и вид работ
VARIANTS = [
    ("К2-АР-01", "Монолитные работы. Фундаментная плита", ["pipe_du", "qty_1300", "strip_out", "arrow_silent"]),
    ("К2-АР-02", "Монолитные работы. Стены подвала", ["approved", "hydro_1180", "arrow_silent", "screed_210"]),
    ("К2-АР-03", "Кровля. ПВХ-мембрана", ["roof_900", "qty_1300", "arrow_147", "slab_note"]),
    ("К2-АР-04", "Водопровод. Ввод в здание", ["pipe_du", "approved", "strip_out", "arrow_silent"]),
]

SCANS_PER_SHEET = 10
POOL = [k for k in MARK_LIBRARY if k not in ("arrow_silent",)]
# положения «немой» стрелки (текста нет) — координаты чертежа, мм
SILENT_ARROWS = [
    [29600, 46200, 31200, 48800],
    [20000, 15000, 21600, 17600],
    [36000, 38000, 37600, 40600],
    [16000, 21000, 17600, 23600],
]


def make_variant_dxf(code: str, work: str, path: Path,
                     marks_extra: list | None = None) -> Path:
    """Копия демо-чертежа с заполненным шифром и видом работ в штампе."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC_DXF, path)
    d = Drawing.open(path)
    for ent in d.msp.query("INSERT"):
        if ent.dxf.name != TITLEBLOCK:
            continue
        for att in ent.attribs:
            if att.dxf.tag == "ШИФР":
                att.dxf.text = code
            elif att.dxf.tag == "ВИД_РАБОТ":
                att.dxf.text = work
            elif att.dxf.tag == "ФИО":
                att.dxf.text = "Иванов И.И."
    d.doc.saveas(path)
    return path


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "drawings").mkdir(parents=True)
    (OUT / "scans").mkdir(parents=True)
    (OUT / "truth").mkdir(parents=True)

    rng = random.Random(20261103)
    sheets, rendered = [], {}

    for vi, (code, work, mandatory) in enumerate(VARIANTS, 1):
        dxf_path = make_variant_dxf(code, work, OUT / "drawings" / f"sheet_{vi:02d}.dxf")
        d = Drawing.open(dxf_path)
        extents = d.inventory()["extents"]
        print(f"лист комплекта {vi}: {code} — рендер подложки…")
        base = render_sheet(d, extents)
        rendered[code] = (base, extents, str(dxf_path))

        for si in range(1, SCANS_PER_SHEET + 1):
            sheet_id = f"{code}-v{si:02d}"
            seed = 100 + vi * 20 + si
            canvas = SheetCanvas(base, extents, seed=seed)
            # состав пометок: обязательные листа + 0–2 случайных из общего набора,
            # иногда «забытая» пометка без текста
            keys = list(mandatory)
            extra = rng.sample(POOL, rng.choice([0, 1, 1, 2]))
            keys += [k for k in extra if k not in keys]
            rng.shuffle(keys)
            for key in keys:
                apply_mark(canvas, key)
            # на части сканов есть стрелка без подписи — уходит человеку
            if rng.random() < 0.45:
                free = [b for b in SILENT_ARROWS
                        if all(b != MARK_LIBRARY[k]["model_bbox"] for k in keys)]
                if free:
                    canvas.mark("arrow", rng.choice(free), "", 0.55, RED,
                                "текст не читается — уходит человеку")

            scan_path = OUT / "scans" / f"{sheet_id}.png"
            truth_path = OUT / "truth" / f"{sheet_id}.json"
            texts_path = OUT / "truth" / f"{sheet_id}_texts.json"
            truth = canvas.save(scan_path, truth_path, texts_path,
                                extra={"sheet_id": sheet_id, "code": code, "work": work,
                                       "variant": vi, "iteration": si})
            sheets.append({
                "sheet_id": sheet_id, "title": f"{code} · {work} · итерация {si}",
                "scan": str(scan_path.relative_to(ROOT)),
                "drawing": str(Path(rendered[code][2]).relative_to(ROOT)),
                "texts": str(texts_path.relative_to(ROOT)),
                "truth": str(truth_path.relative_to(ROOT)),
                "note": f"лист комплекта {vi}, итерация правок {si}",
                "expected_marks": len(truth["marks"]),
            })
            print(f"  скан {sheet_id}: {len(truth['marks'])} пометок")

    manifest = {
        "release": "ЖК «Северный», корпус 2 — выпуск исполнительных схем 03.11.2026",
        "generated_by": "scripts/make_demo_batch.py",
        "sheets": sheets,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(f"\nманифест: {OUT / 'manifest.json'} — {len(sheets)} листов, "
          f"{len(VARIANTS)} чертежей комплекта")
    total_marks = sum(s["expected_marks"] for s in sheets)
    silent = 0
    for s in sheets:
        t = json.loads((ROOT / s["truth"]).read_text(encoding="utf-8"))
        silent += sum(1 for m in t["marks"] if not m["text"])
    print(f"пометок всего: {total_marks}, из них без текста (вопрос человеку): {silent}")


if __name__ == "__main__":
    main()
