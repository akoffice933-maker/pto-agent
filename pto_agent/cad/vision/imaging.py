# -*- coding: utf-8 -*-
"""Работа с растром скана: маска чернил, поиск рамки листа, доли пикселей.

Всё на numpy/scipy (BSD) — без OpenCV, чтобы не тянуть лишние зависимости в поставку.
"""
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

MAX_SIDE_FOR_ANALYSIS = 2400


def load_rgb(path: str | Path) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    return np.asarray(img)


def to_gray(rgb: np.ndarray) -> np.ndarray:
    return (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]).astype(np.uint8)


def colored_ink_mask(rgb: np.ndarray, saturation_min: int = 35,
                     brightness_max: int = 250) -> np.ndarray:
    """Маска цветных чернил (ручка/маркер): цветной и не слишком светлый пиксель."""
    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = mx - mn
    bright = (r + g + b) / 3.0
    return (sat >= saturation_min) & (bright <= brightness_max)


def pen_ink_mask(rgb: np.ndarray, saturation_min: int = 55,
                 value_max: int = 252) -> np.ndarray:
    """Маска чернил ручки красного или синего цвета (типовая практика ПТО).

    Пометки на исполнительной документации делают красной или синей ручкой, а печать
    чёрно-белая (плюс цветные заливки вроде жёлтых маркеров). Отбор по тону отсекает
    как раз заливки и штампы: жёлтый (≈30°), голубой (≈180°), зелёный (≈120°) не
    попадают в диапазоны красного (≈0°/360°) и синего (≈240°).
    """
    arr = rgb[:, :, :3].astype(np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = arr.max(axis=-1)
    mn = arr.min(axis=-1)
    delta = mx - mn
    sat = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0.0) * 255.0
    value = mx * 255.0

    hue = np.zeros_like(mx)
    safe = delta > 1e-6
    rmax = safe & (mx == r)
    gmax = safe & (mx == g) & ~rmax
    bmax = safe & (mx == b) & ~rmax & ~gmax
    hue[rmax] = ((g[rmax] - b[rmax]) / np.maximum(delta[rmax], 1e-6)) % 6.0
    hue[gmax] = (b[gmax] - r[gmax]) / np.maximum(delta[gmax], 1e-6) + 2.0
    hue[bmax] = (r[bmax] - g[bmax]) / np.maximum(delta[bmax], 1e-6) + 4.0
    hue_deg = hue * 60.0

    red = (hue_deg <= 30) | (hue_deg >= 335)      # красный и бордовый
    blue = (hue_deg >= 200) & (hue_deg <= 268)    # синий и тёмно-синий
    return (sat >= saturation_min) & (value <= value_max) & (red | blue)


def dark_ink_mask(rgb: np.ndarray, threshold: int = 90) -> np.ndarray:
    """Маска тёмных (чёрных) чернил — линии чертежа и чёрная ручка."""
    gray = to_gray(rgb)
    return gray < threshold


def ink_fraction(mask: np.ndarray, bbox_px) -> float:
    x0, y0, x1, y1 = [int(round(v)) for v in bbox_px]
    h, w = mask.shape
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    part = mask[y0:y1, x0:x1]
    return float(part.mean()) if part.size else 0.0


def frame_rect(rgb: np.ndarray, line_ratio: float = 0.45) -> tuple | None:
    """Найти рамку листа: строки/столбцы, заполненные тёмным более чем на line_ratio."""
    dark = dark_ink_mask(rgb, threshold=120)
    rows = dark.mean(axis=1)
    cols = dark.mean(axis=0)
    row_lines = np.where(rows > line_ratio)[0]
    col_lines = np.where(cols > line_ratio)[0]
    if len(row_lines) < 2 or len(col_lines) < 2:
        return None
    return (int(col_lines.min()), int(row_lines.min()),
            int(col_lines.max()), int(row_lines.max()))


def ink_bbox(rgb: np.ndarray, mask: np.ndarray | None = None) -> tuple | None:
    m = colored_ink_mask(rgb) if mask is None else mask
    if not m.any():
        return None
    rows = np.where(m.any(axis=1))[0]
    cols = np.where(m.any(axis=0))[0]
    return (int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max()))


def downsample_for_analysis(rgb: np.ndarray, max_side: int = MAX_SIDE_FOR_ANALYSIS):
    h, w = rgb.shape[:2]
    k = max(1, int(np.ceil(max(h, w) / max_side)))
    if k == 1:
        return rgb, 1.0
    small = rgb[::k, ::k]
    return small, 1.0 / k


def label_components(mask: np.ndarray, close_iterations: int = 4):
    """Склеить штрихи одной пометки и разметить связные области."""
    if close_iterations:
        mask = ndimage.binary_dilation(mask, iterations=close_iterations)
    labels, n = ndimage.label(mask)
    return labels, n


def crop_rgb(rgb: np.ndarray, bbox_px) -> np.ndarray:
    x0, y0, x1, y1 = [int(round(v)) for v in bbox_px]
    return rgb[max(0, y0):y1, max(0, x0):x1]


def iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    inter_x = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    inter_y = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = inter_x * inter_y
    area_a = max(0.0, (ax1 - ax0)) * max(0.0, (ay1 - ay0))
    area_b = max(0.0, (bx1 - bx0)) * max(0.0, (by1 - by0))
    union = area_a + area_b - inter
    return float(inter / union) if union > 0 else 0.0
