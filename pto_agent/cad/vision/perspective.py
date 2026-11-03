# -*- coding: utf-8 -*-
"""Перспективная калибровка фото с телефона: 4 опорные точки → координаты чертежа.

Скан с планшета можно привязать к чертежу масштабом и сдвигом (см. `calibrate.py`).
Фото с телефона снято под углом: лист на нём — четырёхугольник, а не прямоугольник,
и линейная калибровка даёт ошибку в несколько сантиметров на краях листа. Поэтому
здесь считается проективное преобразование (гомография) по 4 точкам:

    пиксель фото (x, y)  ──H──▶  координаты чертежа (X, Y), мм

Дальше два пути, оба поддержаны модулем:

1. **Выпрямление** (`rectify`): фото пересчитывается в «фронтальный» растр листа
   (как будто снято сверху). После этого работает весь обычный конвейер — тайлы,
   модель, проверки, координаты. Это основной путь: модель видит ровный лист,
   а не наклонный.
2. **Прямой пересчёт bbox** (`to_model_bbox`): найденный на фото bbox переводится
   в модель по 4 углам (bbox четырёхугольника на фото → прямоугольник в модели).

Чего модуль не делает: не подменяет человека. Опорные точки ставит инженер (углы
рамки листа), и калибровка попадает в отчёт с остатком (rms) и предупреждениями
о качестве съёмки — блики, темнота, смаз.
"""
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .calibrate import Calibration, CalibrationError
from .imaging import load_rgb

MIN_ANCHORS = 4


class PerspectiveError(CalibrationError):
    """Опорные точки не годятся: мало точек, вырожденный четырёхугольник, сдвиг."""


# ── гомография ───────────────────────────────────────────────────────────
def homography_from_points(src, dst) -> np.ndarray:
    """Матрица 3×3, переводящая точки `src` в `dst` (нормированный DLT, ≥4 точек).

    Больше 4 точек — переопределённая система, решается методом наименьших
    квадратов: инженер может отметить 6–8 точек, если телефон снял с искажением.
    """
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    if src.shape != dst.shape or src.ndim != 2 or src.shape[1] != 2:
        raise PerspectiveError("наборы опорных точек должны быть списками пар (x, y)")
    if len(src) < MIN_ANCHORS:
        raise PerspectiveError(
            f"для перспективной калибровки нужно минимум {MIN_ANCHORS} опорные точки, "
            f"передано {len(src)}")

    def normalize(pts):
        center = pts.mean(axis=0)
        dist = np.sqrt(((pts - center) ** 2).sum(axis=1)).mean()
        if dist < 1e-9:
            raise PerspectiveError("опорные точки совпадают — калибровка невозможна")
        scale = np.sqrt(2.0) / dist
        T = np.array([[scale, 0, -scale * center[0]],
                      [0, scale, -scale * center[1]],
                      [0, 0, 1.0]])
        return pts @ T[:2, :2].T + T[:2, 2], T

    s_n, T_s = normalize(src)
    d_n, T_d = normalize(dst)

    rows = []
    for (x, y), (u, v) in zip(s_n, d_n):
        rows.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        rows.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    A = np.asarray(rows, dtype=float)
    # вырожденный набор (все точки на одной прямой) формально решается, но
    # преобразование по нему бессмысленно — отказываем сразу
    if len(src) == MIN_ANCHORS:
        if _quad_area(src) < 1e-6 or _quad_area(dst) < 1e-6:
            raise PerspectiveError(
                "опорные точки лежат на одной линии — выберите углы листа, а не точки "
                "на одной стороне")
    _, _, vt = np.linalg.svd(A)
    h = vt[-1].reshape(3, 3)
    H = np.linalg.inv(T_d) @ h @ T_s
    if abs(H[2, 2]) > 1e-12:
        H = H / H[2, 2]
    if not np.all(np.isfinite(H)):
        raise PerspectiveError("не удалось вычислить преобразование по этим точкам")
    return H


def apply_homography(H: np.ndarray, pts) -> np.ndarray:
    """Перевести точки (N×2) матрицей H. Возвращает N×2."""
    p = np.asarray(pts, dtype=float).reshape(-1, 2)
    homo = np.column_stack([p, np.ones(len(p))]) @ H.T
    w = homo[:, 2:3]
    w = np.where(np.abs(w) < 1e-12, 1e-12, w)
    return homo[:, :2] / w


def _quad_area(pts) -> float:
    p = np.asarray(pts, dtype=float)
    x, y = p[:, 0], p[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _is_convex(pts) -> bool:
    p = np.asarray(pts, dtype=float)
    signs = []
    for i in range(len(p)):
        a, b, c = p[i], p[(i + 1) % len(p)], p[(i + 2) % len(p)]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        signs.append(np.sign(cross))
    return len(set(signs)) == 1


# ── качество съёмки ──────────────────────────────────────────────────────
def photo_quality(rgb: np.ndarray) -> dict:
    """Проверка фото «до распознавания»: темнота, блики, смаз.

    Дешёвые метрики по самому снимку. Если фото негодное, лучше сказать это сразу,
    чем получить от модели мусор и потом разбираться, откуда он.
    """
    gray = rgb[..., :3].astype(np.float32).mean(axis=2)
    luma = float(gray.mean())
    # блик — это локально ярче бумаги, а не «вообще белый пиксель»: у скана бумага
    # сама белая, поэтому порог считается от медианной яркости листа
    glare_threshold = max(245.0, float(np.median(gray)) + 35.0)
    glare = float((gray >= glare_threshold).mean())
    # дисперсия второй разности — дешёвая замена лаплассиану, по ней виден смаз
    dx = np.diff(gray[::4, ::4], axis=1)
    sharpness = float(dx.var())
    height, width = gray.shape
    warnings = []
    if luma < 95:
        warnings.append("PHOTO_DARK: снимок тёмный — переснимите при лучшем свете")
    if glare > 0.10:
        warnings.append("PHOTO_GLARE: блики на листе — часть пометок может пропасть")
    if sharpness < 12:
        warnings.append("PHOTO_BLURRY: снимок смазан — держите телефон неподвижно")
    return {
        "megapixels": round(height * width / 1e6, 2),
        "luma_mean": round(luma, 1),
        "glare_share": round(glare, 4),
        "sharpness": round(sharpness, 2),
        "warnings": warnings,
    }


# ── калибровка ───────────────────────────────────────────────────────────
@dataclass
class PhotoCalibration:
    """Фото ↔ чертёж: гомография по опорным точкам + параметры выпрямления."""

    matrix: list                    # 3×3, пиксели фото → координаты чертежа
    image_size: list                # [ширина, высота] фото в пикселях
    rect_size: list                 # [ширина, высота] выпрямленного растра
    rect_model: list                # [X0, Y0, X1, Y1] — что покрывает выпрямление
    anchors: list = field(default_factory=list)
    method: str = "perspective"
    confirmed: bool = True
    rms: float = 0.0                # остаток на опорных точках, ед. чертежа (мм)
    notes: list = field(default_factory=list)

    # ── построение ────────────────────────────────────────────────────────
    @classmethod
    def from_anchors(cls, pixels, model_points, image_size, *,
                     rect_size=None, rect_model=None, margin: float = 0.018,
                     confirmed: bool = True) -> "PhotoCalibration":
        """pixels — точки на фото, model_points — те же точки на чертеже (мм)."""
        px = np.asarray(pixels, dtype=float)
        mm = np.asarray(model_points, dtype=float)
        if len(px) != len(mm):
            raise PerspectiveError("опорные точки фото и чертежа должны быть парами")
        H = homography_from_points(px, mm)

        # четырёхугольник на фото должен быть выпуклым и не вырожденным
        if len(px) == MIN_ANCHORS and not _is_convex(px):
            raise PerspectiveError(
                "опорные точки на фото не образуют выпуклый четырёхугольник — "
                "проверьте порядок точек (по часовой стрелке от левого верхнего угла)")
        if len(px) > MIN_ANCHORS:
            # для контроля выпуклости берём выпуклую оболочку четырёх крайних точек
            pass

        pred = apply_homography(H, px)
        rms = float(np.sqrt(((pred - mm) ** 2).sum(axis=1)).mean())
        note_list = []
        if rms > 20:
            note_list.append(
                f"остаток на опорных точках {rms:.1f} мм — точки поставлены неточно, "
                "перепроверьте углы рамки")

        x0, y0 = mm[:, 0].min(), mm[:, 1].min()
        x1, y1 = mm[:, 0].max(), mm[:, 1].max()
        if rect_model is None:
            pad_x = (x1 - x0) * margin
            pad_y = (y1 - y0) * margin
            rect_model = [float(x0 - pad_x), float(y0 - pad_y),
                          float(x1 + pad_x), float(y1 + pad_y)]
        if rect_size is None:
            width_mm = rect_model[2] - rect_model[0]
            height_mm = rect_model[3] - rect_model[1]
            target_w = 3000
            rect_size = [int(target_w), int(round(target_w * height_mm / width_mm))]
        return cls(matrix=[[round(float(v), 10) for v in row] for row in H],
                   image_size=[int(image_size[0]), int(image_size[1])],
                   rect_size=[int(rect_size[0]), int(rect_size[1])],
                   rect_model=[round(float(v), 3) for v in rect_model],
                   anchors=[{"px": list(map(float, p)), "model": list(map(float, m))}
                            for p, m in zip(px, mm)],
                   confirmed=confirmed, rms=round(rms, 3), notes=note_list)

    @classmethod
    def from_dict(cls, data: dict) -> "PhotoCalibration":
        anchors = data.get("anchors") or []
        if anchors and len(data.get("matrix") or []) == 3:
            return cls(matrix=data["matrix"], image_size=data.get("image_size") or [0, 0],
                       rect_size=data.get("rect_size") or [3000, 2120],
                       rect_model=data.get("rect_model") or [0, 0, 1, 1],
                       anchors=anchors, confirmed=bool(data.get("confirmed", True)),
                       rms=float(data.get("rms") or 0.0),
                       notes=list(data.get("notes") or []))
        if not anchors:
            raise PerspectiveError("в файле калибровки нет опорных точек")
        return cls.from_anchors([a["px"] for a in anchors], [a["model"] for a in anchors],
                                data.get("image_size") or (3000, 2120),
                                rect_size=data.get("rect_size"),
                                rect_model=data.get("rect_model"),
                                confirmed=bool(data.get("confirmed", True)))

    # ── свойства ──────────────────────────────────────────────────────────
    @property
    def H(self) -> np.ndarray:
        return np.asarray(self.matrix, dtype=float)

    @property
    def H_inv(self) -> np.ndarray:
        return np.linalg.inv(self.H)

    def to_model_point(self, px: float, py: float) -> list:
        x, y = apply_homography(self.H, [[px, py]])[0]
        return [round(float(x), 1), round(float(y), 1)]

    def to_model_bbox(self, bbox_px) -> list:
        """bbox на фото → прямоугольник в модели: крайние из четырёх углов."""
        x0, y0, x1, y1 = bbox_px
        quad = apply_homography(self.H, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
        return [round(float(quad[:, 0].min()), 1), round(float(quad[:, 1].min()), 1),
                round(float(quad[:, 0].max()), 1), round(float(quad[:, 1].max()), 1)]

    def to_photo_points(self, pts) -> list:
        """Обратный пересчёт: координаты модели → пиксели фото."""
        out = apply_homography(self.H_inv, pts)
        return [[round(float(x), 1), round(float(y), 1)] for x, y in out]

    def rectified_calibration(self) -> Calibration:
        """Калибровка выпрямленного растра: он уже без наклона, хватает масштаба."""
        rx0, ry0, rx1, ry1 = self.rect_model
        rw, rh = self.rect_size
        sx = (rx1 - rx0) / max(rw - 1, 1)
        sy = -(ry1 - ry0) / max(rh - 1, 1)
        return Calibration(scale_x=sx, scale_y=sy, offset_x=rx0, offset_y=ry1,
                           image_width=rw, image_height=rh,
                           method="perspective_rectified", confirmed=self.confirmed,
                           rms=self.rms, notes=list(self.notes), anchors=list(self.anchors))

    def rectified_to_photo_matrix(self) -> np.ndarray:
        """Матрица «пиксель выпрямленного растра → пиксель фото» (для обратного пересчёта)."""
        rx0, ry0, rx1, ry1 = self.rect_model
        rw, rh = self.rect_size
        sx = (rx1 - rx0) / max(rw - 1, 1)
        sy = -(ry1 - ry0) / max(rh - 1, 1)
        M = np.array([[sx, 0, rx0], [0, sy, ry1], [0, 0, 1]], dtype=float)   # rect → model
        return self.H_inv @ M

    def as_dict(self) -> dict:
        return {
            "method": self.method, "confirmed": self.confirmed, "rms": self.rms,
            "image_size": self.image_size, "rect_size": self.rect_size,
            "rect_model": self.rect_model, "matrix": self.matrix,
            "anchors": self.anchors, "notes": self.notes,
            "mm_per_px_center": round(
                1.0 / max(self._px_per_mm_center(), 1e-9), 4),
        }

    def _px_per_mm_center(self) -> float:
        """Сколько пикселей фото приходится на 1 мм чертежа в центре листа (оценка)."""
        rx0, ry0, rx1, ry1 = self.rect_model
        cx, cy = (rx0 + rx1) / 2, (ry0 + ry1) / 2
        a = apply_homography(self.H_inv, [[cx, cy]])[0]
        b = apply_homography(self.H_inv, [[cx + 100.0, cy]])[0]
        return float(np.hypot(*(b - a)) / 100.0)


def normalize_illumination(rgb: np.ndarray, radius: int = 31) -> np.ndarray:
    """Выровнять освещённость: поделить на размытый фон (виньетка, тени, лампа сбоку).

    Фото с телефона почти всегда темнее по краям и с градиентом света. Для распознавания
    это вредно: «серое» поле листа перестаёт отличаться от графики. Делим снимок на его
    сильно размытую копию — фон становится белым, линии и пометки сохраняют контраст.
    """
    from PIL import Image, ImageFilter
    img = Image.fromarray(rgb)
    k = radius * 2 + 1
    background = np.asarray(img.filter(ImageFilter.BoxBlur(radius)).resize(img.size))         if False else np.asarray(img.filter(ImageFilter.GaussianBlur(radius))).astype(np.float32)
    background = np.maximum(background, 12.0)
    out = rgb.astype(np.float32) / background * 245.0
    return np.clip(out, 0, 255).astype(np.uint8)


# ── выпрямление ──────────────────────────────────────────────────────────
def sample_bilinear(rgb: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Билинейная выборка по массивам координат (с прижатием к краям)."""
    h, w = rgb.shape[:2]
    xs = np.clip(xs, 0, w - 1)
    ys = np.clip(ys, 0, h - 1)
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    x1 = np.clip(x0 + 1, 0, w - 1)
    y1 = np.clip(y0 + 1, 0, h - 1)
    fx = (xs - x0)[..., None]
    fy = (ys - y0)[..., None]
    top = rgb[y0, x0].astype(np.float32) * (1 - fx) + rgb[y0, x1].astype(np.float32) * fx
    bot = rgb[y1, x0].astype(np.float32) * (1 - fx) + rgb[y1, x1].astype(np.float32) * fx
    return top * (1 - fy) + bot * fy


def rectify(rgb: np.ndarray, calib: PhotoCalibration, *, band_rows: int = 256) -> np.ndarray:
    """Выпрямить фото в «фронтальный» растр листа заданного размера.

    Для каждого пикселя результата считается координата чертежа, затем — пиксель фото,
    и берётся билинейная выборка. Полосами по строкам, чтобы не держать в памяти
    массивы на весь кадр (фото с телефона — 12 Мп и больше).
    """
    rw, rh = calib.rect_size
    rx0, ry0, rx1, ry1 = calib.rect_model
    H_inv = calib.H_inv
    out = np.zeros((rh, rw, 3), dtype=np.uint8)
    us = np.arange(rw, dtype=np.float64)
    model_x = rx0 + us / max(rw - 1, 1) * (rx1 - rx0)
    for start in range(0, rh, band_rows):
        stop = min(start + band_rows, rh)
        vs = np.arange(start, stop, dtype=np.float64)
        model_y = ry1 - vs / max(rh - 1, 1) * (ry1 - ry0)
        mx, my = np.meshgrid(model_x, model_y)
        pts = np.column_stack([mx.ravel(), my.ravel(), np.ones(mx.size)]) @ H_inv.T
        wgt = np.where(np.abs(pts[:, 2]) < 1e-12, 1e-12, pts[:, 2])
        px = (pts[:, 0] / wgt).reshape(mx.shape)
        py = (pts[:, 1] / wgt).reshape(my.shape)
        band = sample_bilinear(rgb, px, py)
        out[start:stop] = np.clip(band, 0, 255).astype(np.uint8)
    return out


def rectify_file(photo_path: str | Path, calib: PhotoCalibration,
                 out_path: str | Path | None = None) -> np.ndarray:
    """Выпрямить файл фото и (по желанию) сохранить результат."""
    rgb = load_rgb(photo_path)
    out = rectify(rgb, calib)
    if out_path:
        from PIL import Image
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(out).save(out_path)
    return out


__all__ = ["PhotoCalibration", "PerspectiveError", "homography_from_points",
           "apply_homography", "photo_quality", "rectify", "rectify_file",
           "normalize_illumination", "sample_bilinear"]
