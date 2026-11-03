# -*- coding: utf-8 -*-
"""Растеризация чертежа для вычитания «печатной» графики из скана.

Идея: у ПТО есть исходный DXF и скан с пометками. Растеризуем DXF в масштабе
скана (по калибровке) и вычитаем: то, что осталось на скане и чего нет на
чертеже, — рукописные пометки. Это устойчивее любых эвристик «по цвету/толщине».
"""
from pathlib import Path

import numpy as np
from PIL import Image

from .imaging import colored_ink_mask, dark_ink_mask, pen_ink_mask


def render_reference(drawing, calibration, width: int, height: int,
                     dpi: int = 100) -> np.ndarray:
    """Отрисовать чертёж ровно в пиксельной сетке скана (по калибровке)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    fig.patch.set_facecolor("#FFFFFF")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor("#FFFFFF")
    ax.set_axis_off()
    ctx = RenderContext(drawing.doc)
    try:
        ctx.current_layout_properties.set_colors("#FFFFFF", "#1A1A1A")
    except Exception:
        pass
    for layer in drawing.doc.layers:
        if layer.dxf.color == 7:
            layer.dxf.color = 250
    Frontend(ctx, MatplotlibBackend(ax)).draw_layout(drawing.msp, finalize=True)
    fig.set_size_inches(width / dpi, height / dpi)
    ax.set_aspect("auto")
    ax.set_xlim(calibration.offset_x, calibration.offset_x + width * calibration.scale_x)
    ax.set_ylim(calibration.offset_y + height * calibration.scale_y, calibration.offset_y)
    tmp = Path(f"/tmp/_pto_reference_{width}x{height}.png")
    fig.savefig(tmp, dpi=dpi, facecolor="#FFFFFF")
    plt.close(fig)
    img = Image.open(tmp).convert("RGB").resize((width, height), Image.LANCZOS)
    tmp.unlink(missing_ok=True)
    return np.asarray(img)


def estimate_tolerance(scan_rgb: np.ndarray, reference_rgb: np.ndarray,
                       max_k: int = 14, covered_target: float = 0.97) -> int:
    """Подобрать допуск вычитания по факту: насколько линии на снимке толще эталона.

    Печать на фото толще и мягче, чем в растеризации DXF (объектив, JPEG, выпрямление).
    Если вычитать с фиксированным допуском, вокруг каждой линии остаётся «ореол»,
    и он склеивается в ложные пометки вдоль рамок и осей. Здесь допуск подбирается так,
    чтобы эталон с запасом накрывал именно печатные штрихи снимка.
    """
    from scipy import ndimage

    print_scan = dark_ink_mask(scan_rgb, 210) & ~pen_ink_mask(scan_rgb)
    print_ref = dark_ink_mask(reference_rgb, 245) | colored_ink_mask(reference_rgb, 12)
    total = max(int(print_scan.sum()), 1)
    for k in range(0, max_k + 1):
        covered = print_scan & ndimage.binary_dilation(print_ref, iterations=k)
        if covered.sum() / total >= covered_target:
            return k
    return max_k


def handwriting_mask(scan_rgb: np.ndarray, reference_rgb: np.ndarray,
                     tolerance_px: int | None = None, pen_only: bool = True) -> np.ndarray:
    """Пиксели скана, которых нет на эталонной растеризации чертежа.

    `pen_only=True` — берём только чернила красной/синей ручки: печать чёрная, а
    заливки (жёлтые маркеры и т.п.) отсекаются по тону. На фото с телефона это
    принципиально: цвет после съёмки плывёт, и «любые цветные чернила» дают мусор.
    """
    from scipy import ndimage

    if tolerance_px is None:
        tolerance_px = estimate_tolerance(scan_rgb, reference_rgb) + 1
    if pen_only:
        ink_scan = pen_ink_mask(scan_rgb)
    else:
        ink_scan = colored_ink_mask(scan_rgb, 30) | dark_ink_mask(scan_rgb, 135)
    # на эталоне берём ЛЮБОЙ печатный штрих: бледно-розовые и тонкие линии после
    # съёмки теряют насыщенность и иначе не вычитаются
    ink_ref = (dark_ink_mask(reference_rgb, 240)
               | colored_ink_mask(reference_rgb, 12))
    ink_ref = ndimage.binary_dilation(ink_ref, iterations=tolerance_px)
    return ink_scan & ~ink_ref
