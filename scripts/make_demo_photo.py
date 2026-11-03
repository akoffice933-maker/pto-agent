# -*- coding: utf-8 -*-
"""Генерация демо-фото листа «с телефона»: перспектива, блики, смаз, шум JPEG.

Берёт ровный скан `drawings/redline_scan.png` и снимает его «под углом»: считает
гомографию scan → фото, пересчитывает картинку, добавляет виньетку, размытие и шум.
Заодно пишет опорные точки — 4 угла рамки листа в пикселях фото и в миллиметрах
чертежа — именно их в жизни ставит инженер, отмечая углы листа на снимке.

Результат:
  drawings/redline_photo.jpg          — фото 2600×1950 (≈5 Мп, как с телефона)
  data/demo/photo_anchors.json        — 4 опорные точки + параметры выпрямления
  data/demo/photo_truth.json          — эталонные пометки в координатах выпрямленного растра
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.cad.drawing import Drawing                                   # noqa: E402
from pto_agent.cad.vision.perspective import (homography_from_points,       # noqa: E402
                                             sample_bilinear)

SCAN = ROOT / "drawings" / "redline_scan.png"
PHOTO = ROOT / "drawings" / "redline_photo.jpg"
ANCHORS = ROOT / "data" / "demo" / "photo_anchors.json"
TRUTH_IN = ROOT / "data" / "demo" / "redline_truth.json"
TRUTH_OUT = ROOT / "data" / "demo" / "photo_truth.json"
DXF = ROOT / "drawings" / "korpus2_plan_v1.dxf"

PHOTO_W, PHOTO_H = 2600, 1950      # ≈5 Мп — типичный кадр телефона
MARGIN = 0.018                      # те же поля бумаги, что в make_demo_redline.py


def warp_to_photo(scan: np.ndarray, H_scan_to_photo: np.ndarray) -> np.ndarray:
    """Пересчитать скан в кадр фото: обратная выборка по гомографии."""
    H_inv = np.linalg.inv(H_scan_to_photo)
    out = np.zeros((PHOTO_H, PHOTO_W, 3), dtype=np.uint8)
    xs = np.arange(PHOTO_W, dtype=float)
    for start in range(0, PHOTO_H, 200):
        stop = min(start + 200, PHOTO_H)
        ys = np.arange(start, stop, dtype=float)
        gx, gy = np.meshgrid(xs, ys)
        pts = np.column_stack([gx.ravel(), gy.ravel(), np.ones(gx.size)]) @ H_inv.T
        w = np.where(np.abs(pts[:, 2]) < 1e-12, 1e-12, pts[:, 2])
        sx = (pts[:, 0] / w).reshape(gx.shape)
        sy = (pts[:, 1] / w).reshape(gy.shape)
        inside = (sx >= 0) & (sx <= scan.shape[1] - 1) & (sy >= 0) & (sy <= scan.shape[0] - 1)
        band = sample_bilinear(scan, sx, sy)
        band[~inside] = 45.0            # за пределами листа — тёмный стол
        out[start:stop] = np.clip(band, 0, 255).astype(np.uint8)
    return out


def camera_effects(img: np.ndarray, seed: int = 11) -> np.ndarray:
    """Виньетка, лёгкий смаз, тёплый оттенок и шум — как у камеры телефона."""
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    vignette = np.clip(1.0 - 0.22 * r ** 2, 0.55, 1.0)[..., None]
    arr = img.astype(np.float32) * vignette
    arr[..., 0] *= 1.02
    arr[..., 2] *= 0.97                 # тёплый оттенок
    rng = np.random.default_rng(seed)
    arr += rng.normal(0, 4.0, arr.shape)
    blurred = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(0.9))
    return np.asarray(blurred)


def main():
    drawing = Drawing.open(DXF)
    extents = drawing.inventory()["extents"]
    xmin, ymin = extents["min"]
    xmax, ymax = extents["max"]
    mx = (xmax - xmin) * MARGIN
    my = (ymax - ymin) * MARGIN

    scan = np.asarray(Image.open(SCAN).convert("RGB"))
    sh, sw = scan.shape[:2]

    # углы растра скана → «сняты под углом» (порядок: ЛВ, ПВ, ПН, ЛН)
    scan_corners = [[0, 0], [sw - 1, 0], [sw - 1, sh - 1], [0, sh - 1]]
    photo_corners = [[215, 165], [2410, 250], [2360, 1790], [170, 1725]]
    H = homography_from_points(scan_corners, photo_corners)

    photo = warp_to_photo(scan, H)
    photo = camera_effects(photo)
    PHOTO.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(photo).save(PHOTO, quality=88)
    print(f"фото:   {PHOTO} ({PHOTO_W}×{PHOTO_H})")

    # те же 4 угла в координатах чертежа
    model_corners = [[xmin - mx, ymax + my], [xmax + mx, ymax + my],
                     [xmax + mx, ymin - my], [xmin - mx, ymin - my]]
    rect_model = [xmin - mx, ymin - my, xmax + mx, ymax + my]
    ANCHORS.parent.mkdir(parents=True, exist_ok=True)
    ANCHORS.write_text(json.dumps({
        "image_size": [PHOTO_W, PHOTO_H],
        "rect_size": [sw, sh],
        "rect_model": [round(v, 3) for v in rect_model],
        "anchors": [
            {"name": name, "px": [round(float(a), 1), round(float(b), 1)],
             "model": [round(float(c), 1), round(float(d), 1)]}
            for name, (a, b), (c, d) in zip(
                ("левый верхний", "правый верхний", "правый нижний", "левый нижний"),
                photo_corners, model_corners)
        ],
        "note": ("4 угла рамки листа: пиксели на фото ↔ миллиметры чертежа. "
                 "Такую разметку делает инженер, отмечая углы листа в приложении."),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"опорные точки: {ANCHORS}")

    truth = json.loads(TRUTH_IN.read_text(encoding="utf-8"))
    truth["image"] = str(PHOTO.relative_to(ROOT))
    truth["photo"] = {"size": [PHOTO_W, PHOTO_H],
                      "note": "bbox пометок — в координатах выпрямленного растра "
                              f"{sw}×{sh}, он же растр исходного скана"}
    truth["marks"] = [{
        **m,
        "bbox_photo": [round(float(v), 1) for v in _to_photo(m["bbox_px"], H)],
    } for m in truth["marks"]]
    TRUTH_OUT.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"эталон для фото: {TRUTH_OUT} ({len(truth['marks'])} пометок)")


def _to_photo(bbox, H):
    """bbox в растре скана → bbox на фото (по четырём углам, для справки)."""
    x0, y0, x1, y1 = bbox
    pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float)
    homo = np.column_stack([pts, np.ones(4)]) @ H.T
    w = homo[:, 2:3]
    q = homo[:, :2] / w
    return [q[:, 0].min(), q[:, 1].min(), q[:, 0].max(), q[:, 1].max()]


if __name__ == "__main__":
    main()
