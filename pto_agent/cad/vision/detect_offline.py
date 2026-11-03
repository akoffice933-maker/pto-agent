# -*- coding: utf-8 -*-
"""Офлайн-детектор пометок (без VL-модели): геометрия чернил классическим CV.

Что умеет: найти цветные рукописные пометки на скане, определить их тип по форме
и координаты. Чего не умеет: прочитать рукописный текст — поэтому уверенность
всегда ниже порога 0,7, и такие пометки уходят инженеру (текст вводит человек
или распознаёт VL-модель).
"""
from pathlib import Path

import numpy as np
from PIL import Image

from .imaging import (colored_ink_mask, downsample_for_analysis, ink_fraction,
                      label_components, load_rgb)
from .reference import estimate_tolerance, handwriting_mask, render_reference

ENGINE_NAME = "offline_cv"
MIN_AREA_PX = 120          # в пикселях уменьшенного изображения
MAX_AREA_FRACTION = 0.25   # пометка не может занимать четверть листа


def detect_marks_offline(scan_path: str | Path | None = None, *, image=None,
                         drawing=None, calibration=None,
                         saturation_min: int = 35, min_area_px: int = MIN_AREA_PX,
                         tolerance_px: int | None = None,
                         max_side: int = 2400, close_iterations: int = 4) -> dict:
    """Найти пометки. При наличии DXF и калибровки — по разнице со эталоном чертежа.

    Без чертежа работает грубая эвристика «цветные чернила» — она ловит и цветную
    графику чертежа, поэтому результат помечается как черновой.
    """
    # image — уже подготовленный растр (например, выпрямленное фото): не перечитываем файл
    rgb = np.asarray(image) if image is not None else load_rgb(scan_path)
    h, w = rgb.shape[:2]
    method = "colored_ink_heuristic"

    if drawing is not None and calibration is not None:
        ref = render_reference(drawing, calibration, w, h)
        tol = tolerance_px if tolerance_px is not None else None
        hand_full = handwriting_mask(rgb, ref, tolerance_px=tol)
        method = "reference_diff"
        used_tolerance = (tolerance_px if tolerance_px is not None
                          else estimate_tolerance(rgb, ref) + 1)
    else:
        hand_full = colored_ink_mask(rgb, saturation_min=saturation_min)
        used_tolerance = None

    small, k = downsample_for_analysis(rgb, max_side)
    mask_small = np.asarray(Image.fromarray((hand_full * 255).astype(np.uint8))
                            .resize((small.shape[1], small.shape[0]), Image.BILINEAR)) > 90
    mask_full = hand_full
    labels, n = label_components(mask_small, close_iterations=close_iterations)
    marks = []
    for idx in range(1, n + 1):
        ys, xs = np.where(labels == idx)
        if len(xs) < min_area_px:
            continue
        x0, y0, x1, y1 = xs.min(), ys.min(), xs.max(), ys.max()
        area = (x1 - x0 + 1) * (y1 - y0 + 1)
        if area > MAX_AREA_FRACTION * mask_small.size:
            continue

        # координаты в исходном растре
        px_box = [round(float(x0 / k)), round(float(y0 / k)),
                  round(float((x1 + 1) / k)), round(float((y1 + 1) / k))]
        frac = ink_fraction(mask_full, px_box)
        mtype, conf = _classify(x1 - x0 + 1, y1 - y0 + 1, len(xs), area, frac)
        marks.append({
            "type": mtype,
            "bbox_px": px_box,
            "bbox_norm": [round(px_box[0] / w, 5), round(px_box[1] / h, 5),
                          round(px_box[2] / w, 5), round(px_box[3] / h, 5)],
            "text": "",
            "confidence": conf,
            "engine": ENGINE_NAME,
        "detection_method": method,
            "ink_fraction": round(frac, 4),
            "detector": {"w_px": int(x1 - x0 + 1), "h_px": int(y1 - y0 + 1),
                         "ink_px": int(len(xs)), "fill_ratio": round(len(xs) / area, 3)},
            "warnings": ["TEXT_NOT_RECOGNIZED"] if True else [],
        })

    marks = _cluster(marks)
    marks.sort(key=lambda m: (m["bbox_px"][1], m["bbox_px"][0]))
    for i, m in enumerate(marks, 1):
        m["mark_id"] = f"m{i}"
    return {
        "engine": ENGINE_NAME,
        "detection_method": method,
        "tolerance_px": used_tolerance,
        "image": str(scan_path) if scan_path else "<prepared image>",
        "image_size": [w, h],
        "analysis_scale": k,
        "marks": marks,
        "note": ("офлайн-детектор находит геометрию пометок, но не читает рукописный текст; "
                 "пометки без текста и с уверенностью < 0,7 уходят инженеру ПТО"),
    }


def _classify(w: int, h: int, ink_px: int, bbox_area: int, frac: float) -> tuple:
    """Тип пометки по форме и уверенность (всегда ниже 0,7 — текста нет)."""
    fill = ink_px / max(bbox_area, 1)
    aspect = max(w / max(h, 1), h / max(w, 1))
    if fill < 0.22 and bbox_area > 2000:
        mtype, conf = "circle", 0.66
    elif aspect > 6 and fill < 0.45:
        mtype, conf = "underline", 0.64
    elif fill > 0.35 and min(w, h) < 60:
        mtype, conf = "arrow", 0.58
    elif aspect < 3 and fill > 0.15:
        mtype, conf = "text_note", 0.60
    else:
        mtype, conf = "unknown", 0.50
    if frac < 0.005:
        conf = min(conf, 0.45)
    return mtype, round(conf, 2)


def _cluster(marks: list, gap_px: int = 45) -> list:
    """Склеить близкие штрихи (буквы подписи рядом с обводкой) в одну пометку."""
    if not marks:
        return marks
    groups = []
    for m in sorted(marks, key=lambda x: -x["ink_fraction"]):
        box = m["bbox_px"]
        for g in groups:
            gb = g["bbox_px"]
            if not (box[2] < gb[0] - gap_px or box[0] > gb[2] + gap_px
                    or box[3] < gb[1] - gap_px or box[1] > gb[3] + gap_px):
                g["_members"].append(m)
                g["bbox_px"] = [min(box[0], gb[0]), min(box[1], gb[1]),
                                max(box[2], gb[2]), max(box[3], gb[3])]
                break
        else:
            groups.append({**m, "_members": [m]})

    out = []
    for g in groups:
        box = g["bbox_px"]
        members = g["_members"]
        w_box = max(box[2] - box[0], 1)
        h_box = max(box[3] - box[1], 1)
        aspect = w_box / h_box
        fill = max(m["ink_fraction"] for m in members)
        if aspect >= 5:
            mtype = "underline"
        elif 0.55 <= aspect <= 3.0 and fill <= 0.22:
            mtype = "circle"
        elif aspect <= 0.55:
            mtype = "arrow"
        else:
            mtype = "text_note"
        out.append({
            "type": mtype,
            "bbox_px": box,
            "bbox_norm": members[0]["bbox_norm"],
            "text": "",
            "confidence": round(max(m["confidence"] for m in members), 2),
            "engine": ENGINE_NAME,
            "ink_fraction": round(max(m["ink_fraction"] for m in members), 4),
            "detector": {"strokes": len(members)},
            "warnings": ["TEXT_NOT_RECOGNIZED"],
        })
    return out
