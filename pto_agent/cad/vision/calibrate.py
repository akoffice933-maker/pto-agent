# -*- coding: utf-8 -*-
"""Привязка растра скана к координатам чертежа (калибровка «пиксели → миллиметры»).

Без калибровки распознанная пометка не может быть поставлена на чертёж: модель
возвращает координаты в пикселях, а операции работают в единицах модели.
Автоматическая калибровка по рамке листа помечается как неподтверждённая —
инженер обязан подтвердить масштаб (требование ТЗ).
"""
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .imaging import frame_rect, load_rgb


class CalibrationError(RuntimeError):
    pass


@dataclass
class Calibration:
    scale_x: float
    scale_y: float
    offset_x: float
    offset_y: float
    image_width: int
    image_height: int
    method: str = "anchors"          # anchors | auto | manual
    confirmed: bool = False
    rms: float = 0.0
    notes: list = field(default_factory=list)
    anchors: list = field(default_factory=list)

    # ── построение ────────────────────────────────────────────────────────
    @classmethod
    def from_anchors(cls, pixels: list, model_points: list, image_size: tuple,
                     tol_px: float = 0.5) -> "Calibration":
        """Калибровка по опорным точкам: минимум 2 точки (углы рамки, перекрестья осей)."""
        if len(pixels) != len(model_points) or len(pixels) < 2:
            raise CalibrationError("нужно минимум 2 пары опорных точек (пиксель ↔ модель)")
        px = np.asarray(pixels, dtype=float)
        mm = np.asarray(model_points, dtype=float)
        if np.ptp(px[:, 0]) == 0 or np.ptp(px[:, 1]) == 0:
            raise CalibrationError("опорные точки должны различаться по X и по Y")

        ax, bx = np.polyfit(px[:, 0], mm[:, 0], 1)
        ay, by = np.polyfit(px[:, 1], mm[:, 1], 1)
        pred = np.column_stack([ax * px[:, 0] + bx, ay * px[:, 1] + by])
        rms = float(np.sqrt(((pred - mm) ** 2).sum(axis=1)).mean())
        if abs(ax - ay) / max(abs(ax), abs(ay), 1e-9) > 0.02:
            notes = ["масштаб по X и Y различается более чем на 2 % — проверьте скан (растяжение)"]
        else:
            notes = []
        return cls(scale_x=float(ax), scale_y=float(ay), offset_x=float(bx), offset_y=float(by),
                   image_width=int(image_size[0]), image_height=int(image_size[1]),
                   method="anchors", confirmed=True, rms=round(rms, 3), notes=notes,
                   anchors=[{"px": list(map(float, p)), "model": list(map(float, m))}
                            for p, m in zip(pixels, model_points)])

    @classmethod
    def auto(cls, scan_path: str | Path, model_extents: dict,
             confirm: bool = False) -> "Calibration":
        """Калибровка по рамке листа: внешняя рамка скана ↔ габариты чертежа."""
        rgb = load_rgb(scan_path)
        h, w = rgb.shape[:2]
        rect = frame_rect(rgb)
        if rect is None:
            raise CalibrationError(
                "не найдена рамка листа на скане — задайте опорные точки вручную (--anchors)")
        x0, y0, x1, y1 = rect
        mx0, my0 = model_extents["min"]
        mx1, my1 = model_extents["max"]
        if x1 - x0 < 50 or y1 - y0 < 50:
            raise CalibrationError("рамка на скане слишком мала — проверьте разрешение")

        sx = (mx1 - mx0) / (x1 - x0)
        sy = (my1 - my0) / (y1 - y0)
        notes = ["калибровка по рамке листа не подтверждена человеком: проверьте масштаб"]
        if abs(sx - sy) / max(abs(sx), abs(sy), 1e-9) > 0.02:
            notes.append(f"масштаб по осям различается ({sx:.3f} vs {sy:.3f}) — возможен перекос скана")
        # ось Y растра направлена вниз, ось Y чертежа — вверх
        return cls(scale_x=float(sx), scale_y=float(-sy),
                   offset_x=float(mx0 - x0 * sx), offset_y=float(my1 + y0 * sy),
                   image_width=w, image_height=h, method="auto", confirmed=confirm,
                   notes=notes,
                   anchors=[{"px": [x0, y0], "model": [mx0, my1]},
                            {"px": [x1, y1], "model": [mx1, my0]}])

    # ── преобразование ────────────────────────────────────────────────────
    def to_model_point(self, px: float, py: float) -> list:
        return [round(self.offset_x + px * self.scale_x, 1),
                round(self.offset_y + py * self.scale_y, 1)]

    def to_model_bbox(self, bbox_px) -> list:
        x0, y0, x1, y1 = bbox_px
        p1 = self.to_model_point(x0, y0)
        p2 = self.to_model_point(x1, y1)
        return [min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1])]

    def to_px_point(self, x: float, y: float) -> list:
        return [(x - self.offset_x) / self.scale_x, (y - self.offset_y) / self.scale_y]

    def px_per_model_unit(self) -> float:
        return abs(1.0 / self.scale_x) if self.scale_x else 0.0

    def as_dict(self) -> dict:
        return {
            "scale_x": round(self.scale_x, 6), "scale_y": round(self.scale_y, 6),
            "offset_x": round(self.offset_x, 3), "offset_y": round(self.offset_y, 3),
            "image_size": [self.image_width, self.image_height],
            "method": self.method, "confirmed": self.confirmed, "rms": self.rms,
            "notes": self.notes, "anchors": self.anchors,
            "mm_per_px": round(abs(self.scale_x), 4),
        }
