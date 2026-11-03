# -*- coding: utf-8 -*-
"""Общие помощники для генерации демо-листов: рендер подложки, «сканирование», пометки.

Вынесено из `make_demo_redline.py`, чтобы фото, одиночный скан и пакетный выпуск
рисовались одинаково — тогда результаты сравнимы между собой.
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RED = (206, 32, 32)
BLUE = (24, 60, 170)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

DEFAULT_SIZE = (3000, 2119)     # A1 при ~90 dpi
DEFAULT_MARGIN = 0.018          # поля бумаги вокруг рамки (доля габаритов)


def render_sheet(drawing, extents: dict, size=DEFAULT_SIZE, margin: float = DEFAULT_MARGIN,
                 dpi: int = 150) -> Image.Image:
    """Отрисовать лист с полями бумаги: рамка не прилипает к краю растра."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    width, height = size
    xmin, ymin = extents["min"]
    xmax, ymax = extents["max"]
    mx = (xmax - xmin) * margin
    my = (ymax - ymin) * margin
    xmin, xmax = xmin - mx, xmax + mx
    ymin, ymax = ymin - my, ymax + my

    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    fig.patch.set_facecolor("#FFFFFF")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor("#FFFFFF")
    ax.set_axis_off()
    ctx = RenderContext(drawing.doc)
    try:
        ctx.current_layout_properties.set_colors("#FFFFFF", "#1A1A1A")
    except Exception:                                          # noqa: BLE001
        pass
    for layer in drawing.doc.layers:
        if layer.dxf.color == 7:
            layer.dxf.color = 250
    Frontend(ctx, MatplotlibBackend(ax)).draw_layout(drawing.msp, finalize=True)
    fig.set_size_inches(width / dpi, height / dpi)
    ax.set_aspect("auto")
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    tmp = ROOT / "drawings" / "_base_tmp.png"
    fig.savefig(tmp, dpi=dpi, facecolor="#FFFFFF")
    plt.close(fig)
    base = Image.open(tmp).convert("RGB").resize((width, height), Image.LANCZOS)
    tmp.unlink(missing_ok=True)
    return base


def paper_rect(extents: dict, margin: float = DEFAULT_MARGIN) -> list:
    """Границы бумаги в координатах чертежа: [X0, Y0, X1, Y1]."""
    xmin, ymin = extents["min"]
    xmax, ymax = extents["max"]
    mx = (xmax - xmin) * margin
    my = (ymax - ymin) * margin
    return [xmin - mx, ymin - my, xmax + mx, ymax + my]


class SheetCanvas:
    """Подложка листа + рисование пометок с одновременной записью эталона."""

    def __init__(self, base: Image.Image, extents: dict, margin: float = DEFAULT_MARGIN,
                 seed: int = 7):
        self.base = base
        self.size = base.size
        self.extents = extents
        self.rect = paper_rect(extents, margin)
        self.marks = []
        self.seed = seed
        self.scan = self._make_scan(base, seed)
        self.draw = ImageDraw.Draw(self.scan)
        self.font = ImageFont.truetype(FONT, 26)
        self.font_small = ImageFont.truetype(FONT, 22)

    # ── геометрия ────────────────────────────────────────────────────────
    def px(self, x: float, y: float) -> tuple:
        """Координаты чертежа → пиксели растра."""
        x0, y0, x1, y1 = self.rect
        w, h = self.size
        return ((x - x0) / (x1 - x0) * (w - 1), (y1 - y) / (y1 - y0) * (h - 1))

    @staticmethod
    def _make_scan(base: Image.Image, seed: int) -> Image.Image:
        """Придать подложке вид скана: тёплый фон, шум, лёгкое размытие."""
        arr = np.asarray(base).astype(np.float32)
        paper = np.array([252, 250, 245], dtype=np.float32)
        arr = arr * 0.94 + paper * 0.06
        rng = np.random.default_rng(seed)
        arr += rng.normal(0, 3.2, arr.shape)
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(0.5))

    # ── пометки ──────────────────────────────────────────────────────────
    def mark(self, mtype: str, model_bbox, text: str = "", confidence: float = 0.88,
             color=RED, note: str = "", label: bool = True) -> dict:
        """Нарисовать пометку в координатах чертежа и записать её в эталон."""
        x0, y0 = self.px(model_bbox[0], model_bbox[3])
        x1, y1 = self.px(model_bbox[2], model_bbox[1])
        box = (x0, y0, x1, y1)
        mark = {
            "mark_id": f"m{len(self.marks) + 1}",
            "type": mtype,
            "bbox_px": [round(x0), round(y0), round(x1), round(y1)],
            "text": text,
            "confidence": confidence,
            "note": note,
        }
        self.marks.append(mark)
        self._draw(mtype, box, text, color, label)
        return mark

    def _draw(self, mtype: str, box, text: str, color, label: bool) -> None:
        x0, y0, x1, y1 = box
        if mtype == "circle":
            self.draw.ellipse(box, outline=color, width=6)
        elif mtype == "underline":
            self.draw.line([(x0, y1), (x1, y1)], fill=color, width=7)
            self.draw.line([(x0, y1 - 6), (x1, y1 - 6)], fill=color, width=3)
        elif mtype == "cross_out":
            self.draw.line([(x0, y0), (x1, y1)], fill=color, width=7)
            self.draw.line([(x1, y0), (x0, y1)], fill=color, width=7)
        elif mtype == "arrow":
            cx = (x0 + x1) / 2
            self.draw.line([(cx, y0), (cx, y1 - 22)], fill=color, width=6)
            self.draw.polygon([(cx - 14, y1 - 26), (cx + 14, y1 - 26), (cx, y1)], fill=color)
        if text and label:
            font = self.font if len(text) <= 26 else self.font_small
            self.draw.text((x0 + 8, y1 + 8), text, font=font, fill=color)

    # ── сохранение ───────────────────────────────────────────────────────
    def save(self, scan_path: Path, truth_path: Path | None = None,
             texts_path: Path | None = None, extra: dict | None = None) -> dict:
        scan_path.parent.mkdir(parents=True, exist_ok=True)
        self.scan.save(scan_path)
        truth = {
            "image": str(scan_path),
            "image_size": list(self.size),
            "calibration": {"model_extents": self.extents, "method": "render_exact",
                            "paper_rect": [round(v, 3) for v in self.rect]},
            "marks": self.marks,
        }
        if extra:
            truth.update(extra)
        if truth_path:
            truth_path.parent.mkdir(parents=True, exist_ok=True)
            truth_path.write_text(json.dumps(truth, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        if texts_path:
            texts = {"marks": [{"mark_id": m["mark_id"], "bbox_px": m["bbox_px"],
                                "text": m["text"], "by": "Иванов И.И.",
                                "ts": "2026-11-03T10:15:00"}
                               for m in self.marks if m["text"]],
                     "note": "тексты, введённые инженером ПТО вручную"}
            texts_path.parent.mkdir(parents=True, exist_ok=True)
            texts_path.write_text(json.dumps(texts, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        return truth


# ── типовые пометки (координаты в мм чертежа) ────────────────────────────
MARK_LIBRARY = {
    "pipe_du": dict(mtype="circle", model_bbox=[43100, 42300, 48000, 45200],
                    text="Ду50 → Ду100 по АОСР-146", confidence=0.88, color=RED,
                    note="смена диаметра: нужна операция и ссылка на АОСР"),
    "qty_1300": dict(mtype="underline", model_bbox=[24800, 26300, 28600, 27200],
                     text="1300 м3 — принять по факту", confidence=0.84, color=BLUE,
                     note="объём в пометке против акта (1240 м3) → конфликт"),
    "strip_out": dict(mtype="cross_out", model_bbox=[7800, 19600, 12000, 22200],
                      text="отмостку исключить", confidence=0.90, color=RED,
                      note="работы нет в смете: пометка подтверждает исключение"),
    "arrow_silent": dict(mtype="arrow", model_bbox=[29600, 46200, 31200, 48800],
                         text="", confidence=0.58, color=RED,
                         note="текст не читается — уходит человеку"),
    "approved": dict(mtype="circle", model_bbox=[7300, 31200, 12000, 34200],
                     text="СОГЛАСОВАНО", confidence=0.91, color=RED,
                     note="конфликт: по позиции есть критичные расхождения ПТО"),
    "hydro_1180": dict(mtype="circle", model_bbox=[7900, 25800, 12300, 27900],
                       text="принять по факту 1180 м2", confidence=0.86, color=BLUE,
                       note="объём совпадает с актом — просто подтверждение"),
    "roof_900": dict(mtype="cross_out", model_bbox=[43900, 25800, 47600, 27900],
                     text="кровлю исключить из объёма 900 м2", confidence=0.87, color=RED,
                     note="исключение объёма кровли"),
    "screed_210": dict(mtype="underline", model_bbox=[43900, 31800, 47600, 34000],
                       text="стяжка 210 м3 — уточнить", confidence=0.85, color=BLUE,
                       note="уточнение объёма стяжки"),
    "arrow_147": dict(mtype="arrow", model_bbox=[12800, 34500, 14000, 36500],
                      text="см. АОСР-147", confidence=0.79, color=RED,
                      note="ссылка на акт скрытых работ"),
    "slab_note": dict(mtype="underline", model_bbox=[8000, 33000, 12000, 34600],
                      text="бетон B30, не B25", confidence=0.83, color=BLUE,
                      note="изменение класса бетона"),
}


def apply_mark(canvas: SheetCanvas, key: str) -> dict:
    spec = dict(MARK_LIBRARY[key])
    return canvas.mark(spec.pop("mtype"), spec.pop("model_bbox"), **spec)
