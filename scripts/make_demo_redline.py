# -*- coding: utf-8 -*-
"""Генерация демо-скана с рукописными пометками (redline) для vision-модуля.

Делает три файла:
  drawings/redline_scan.png          — «скан» листа с цветными пометками (с шумом и лёгким размытием)
  data/demo/redline_truth.json       — эталон: bbox и текст каждой пометки (для mock-провайдера и тестов)
  data/demo/redline_texts.json       — тексты, «введённые инженером» вручную для 4 из 5 пометок

Пометки заложены так, чтобы проверить разные ветки логики:
  1. обводка блока объёма + текст «Ду50 → Ду100 по АОСР-146»  → операция;
  2. подчёркивание подписи + текст «1300 м3 — принять по факту» → конфликт по объёму с актом;
  3. зачёркивание блока отмостки + «отмостку исключить»        → операция;
  4. стрелка без текста                                        → решение человека;
  5. «СОГЛАСОВАНО» на позиции с критичным расхождением ПТО     → конфликт с данными ПТО.
"""
import json
import sys
from pathlib import Path

import matplotlib
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

matplotlib.use("Agg")
import matplotlib.pyplot as plt                                    # noqa: E402
from ezdxf.addons.drawing import Frontend, RenderContext           # noqa: E402
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend      # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.cad.drawing import Drawing                          # noqa: E402

DXF = ROOT / "drawings" / "korpus2_plan_v1.dxf"
OUT_PNG = ROOT / "drawings" / "redline_scan.png"
TRUTH = ROOT / "data" / "demo" / "redline_truth.json"
TEXTS = ROOT / "data" / "demo" / "redline_texts.json"

W, H = 3000, 2119          # пиксели скана (A1 при ~90 dpi)
RED = (206, 32, 32)
BLUE = (24, 60, 170)


def render_base(drawing, extents, margin: float = 0.018) -> Image.Image:
    """Рендер листа с полями бумаги: рамка не прилипает к краю скана."""
    xmin, ymin = extents["min"]
    xmax, ymax = extents["max"]
    mx = (xmax - xmin) * margin
    my = (ymax - ymin) * margin
    xmin, xmax = xmin - mx, xmax + mx
    ymin, ymax = ymin - my, ymax + my
    dpi = 150
    fig = plt.figure(figsize=(W / dpi, H / dpi), dpi=dpi)
    fig.patch.set_facecolor("#FFFFFF")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor("#FFFFFF")
    ax.set_axis_off()
    ctx = RenderContext(drawing.doc)
    try:
        ctx.current_layout_properties.set_colors("#FFFFFF", "#1A1A1A")
    except Exception:
        pass
    # цвет 7 в библиотеке рендера — белый; для скана переводим в чёрный
    for layer in drawing.doc.layers:
        if layer.dxf.color == 7:
            layer.dxf.color = 250
    Frontend(ctx, MatplotlibBackend(ax)).draw_layout(drawing.msp, finalize=True)
    fig.set_size_inches(W / dpi, H / dpi)
    ax.set_aspect("auto")
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    tmp = ROOT / "drawings" / "_base.png"
    fig.savefig(tmp, dpi=dpi, facecolor="#FFFFFF")
    plt.close(fig)
    base = Image.open(tmp).convert("RGB").resize((W, H), Image.LANCZOS)
    tmp.unlink(missing_ok=True)
    return base


def make_scan(base: Image.Image) -> Image.Image:
    """Придать вид скана: тёплый фон бумаги, шум, лёгкое размытие."""
    arr = np.asarray(base).astype(np.float32)
    paper = np.array([252, 250, 245], dtype=np.float32)
    arr = arr * 0.94 + paper * 0.06
    rng = np.random.default_rng(7)
    arr += rng.normal(0, 3.2, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(0.5))
    return img


def main():
    drawing = Drawing.open(DXF)
    extents = drawing.inventory()["extents"]
    xmin, ymin = extents["min"]
    xmax, ymax = extents["max"]

    base = render_base(drawing, extents)
    scan = make_scan(base)

    m = 0.018
    mx = (xmax - xmin) * m
    my = (ymax - ymin) * m
    px_xmin, px_xmax = xmin - mx, xmax + mx
    px_ymin, px_ymax = ymin - my, ymax + my

    def px(x, y):
        return ((x - px_xmin) / (px_xmax - px_xmin) * (W - 1),
                (px_ymax - y) / (px_ymax - px_ymin) * (H - 1))

    draw = ImageDraw.Draw(scan)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(font_path, 26)
    font_small = ImageFont.truetype(font_path, 22)

    marks = []

    def add_mark(mtype, model_bbox, text, confidence, color=RED, note=""):
        x0, y0 = px(model_bbox[0], model_bbox[3])   # левый верх в пикселях
        x1, y1 = px(model_bbox[2], model_bbox[1])   # правый низ
        marks.append({
            "mark_id": f"m{len(marks) + 1}",
            "type": mtype,
            "bbox_px": [round(x0), round(y0), round(x1), round(y1)],
            "text": text,
            "confidence": confidence,
            "note": note,
        })
        return (x0, y0, x1, y1)

    # ── 1. обводка блока водопровода + смена диаметра ────────────────────
    b = add_mark("circle", [43100, 42300, 48000, 45200],
                 "Ду50 → Ду100 по АОСР-146", 0.88,
                 note="пометка меняет диаметр: нужна операция и ссылка на АОСР")
    draw.ellipse(b, outline=RED, width=6)
    draw.text((b[0] + 12, b[3] + 6), "Ду50 → Ду100", font=font, fill=RED)
    draw.text((b[0] + 12, b[3] + 36), "по АОСР-146", font=font_small, fill=RED)

    # ── 2. подчёркивание подписи плиты + «объём» ─────────────────────────
    b = add_mark("underline", [24800, 26300, 28600, 27200],
                 "1300 м3 — принять по факту", 0.84,
                 note="цифра в пометке не совпадает с актом (1240 м3) — конфликт")
    draw.line([(b[0], b[3]), (b[2], b[3])], fill=BLUE, width=7)
    draw.line([(b[0], b[3] - 6), (b[2], b[3] - 6)], fill=BLUE, width=3)
    draw.text((b[0] + 4, b[3] + 12), "1300 м3 — принять по факту", font=font_small, fill=BLUE)

    # ── 3. зачёркивание блока отмостки ───────────────────────────────────
    b = add_mark("cross_out", [7800, 19600, 12000, 22200],
                 "отмостку исключить", 0.90,
                 note="работы нет в смете: пометка подтверждает исключение")
    draw.line([(b[0], b[1]), (b[2], b[3])], fill=RED, width=7)
    draw.line([(b[2], b[1]), (b[0], b[3])], fill=RED, width=7)
    draw.text((b[0], b[3] + 8), "отмостку исключить", font=font_small, fill=RED)

    # ── 4. стрелка без текста ────────────────────────────────────────────
    b = add_mark("arrow", [29600, 46200, 31200, 48800], "", 0.58,
                 note="текст не читается — должно уйти человеку")
    cx = (b[0] + b[2]) / 2
    draw.line([(cx, b[1]), (cx, b[3] - 22)], fill=RED, width=6)
    draw.polygon([(cx - 14, b[3] - 26), (cx + 14, b[3] - 26), (cx, b[3])], fill=RED)

    # ── 5. «СОГЛАСОВАНО» на позиции с критичным расхождением ─────────────
    b = add_mark("circle", [7300, 31200, 12000, 34200],
                 "СОГЛАСОВАНО", 0.91,
                 note="конфликт: по позиции есть критичные расхождения ПТО")
    draw.ellipse(b, outline=RED, width=6)
    draw.text((b[0] + 10, b[3] + 8), "СОГЛАСОВАНО", font=font, fill=RED)

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    scan.save(OUT_PNG)
    print(f"скан:  {OUT_PNG} ({W}×{H})")

    TRUTH.parent.mkdir(parents=True, exist_ok=True)
    TRUTH.write_text(json.dumps({
        "image": str(OUT_PNG.relative_to(ROOT)),
        "image_size": [W, H],
        "calibration": {"model_extents": extents, "method": "render_exact"},
        "marks": marks,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"эталон: {TRUTH} ({len(marks)} пометок)")

    texts = {"marks": [
        {"mark_id": m["mark_id"], "bbox_px": m["bbox_px"], "text": m["text"],
         "by": "Иванов И.И.", "ts": "2026-11-03T10:15:00"}
        for m in marks if m["text"]
    ], "note": "тексты, введённые инженером ПТО вручную (пометка 4 без текста — остаётся вопросом)"}
    TEXTS.write_text(json.dumps(texts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"тексты инженера: {TEXTS} ({len(texts['marks'])} из {len(marks)})")

    for m in marks:
        print(f"  {m['mark_id']} {m['type']:<10} bbox={m['bbox_px']} text={m['text']!r}")


if __name__ == "__main__":
    main()
