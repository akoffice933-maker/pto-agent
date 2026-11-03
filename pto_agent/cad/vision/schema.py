# -*- coding: utf-8 -*-
"""Схема пометок со скана (redline) и её строгая валидация.

Модель возвращает только то, что описано здесь. Всё, что не проходит проверку,
не превращается в операцию над чертежом, а попадает в rejected с причиной.
"""
import re
from dataclasses import asdict, dataclass, field

MARK_TYPES = {
    "circle",          # обводка объекта
    "underline",       # подчёркивание подписи
    "cross_out",       # зачёркивание / крест
    "arrow",           # стрелка/выноска
    "text_note",       # рукописный текст без графики
    "dimension_fix",   # исправление размера/обозначения
    "unknown",
}

MAX_TEXT_LEN = 300
MAX_MARKS_PER_TILE = 40


@dataclass
class Mark:
    mark_id: str
    type: str
    bbox_px: list                 # [x0, y0, x1, y1] в пикселях исходного скана
    bbox_norm: list               # [x0, y0, x1, y1] в долях листа 0..1
    text: str = ""
    confidence: float = 0.0
    engine: str = ""              # vlm | offline_cv | human
    model: str = ""
    tile: int | None = None
    ink_fraction: float = 0.0
    coords_model: list | None = None   # [x0, y0, x1, y1] в единицах чертежа
    source: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    requires_confirmation: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_payload(payload: dict, *, tile_index: int | None = None) -> tuple[list, list]:
    """Разобрать ответ модели: вернуть (валидные сырые пометки, отклонённые).

    Отклоняется всё, что не соответствует схеме: неизвестный тип, координаты вне
    листа, перевёрнутый или нулевой bbox, отсутствие уверенности.
    """
    accepted, rejected = [], []
    marks = payload.get("marks") if isinstance(payload, dict) else None
    if not isinstance(marks, list):
        return [], [{"reason": "NO_MARKS_ARRAY", "payload_type": type(payload).__name__}]
    if len(marks) > MAX_MARKS_PER_TILE:
        rejected.append({"reason": "TOO_MANY_MARKS", "count": len(marks)})
        marks = marks[:MAX_MARKS_PER_TILE]

    for i, m in enumerate(marks, 1):
        ref = f"tile{tile_index}-{i}" if tile_index is not None else f"mark-{i}"
        if not isinstance(m, dict):
            rejected.append({"reason": "MARK_NOT_OBJECT", "ref": ref})
            continue

        mtype = str(m.get("type", "unknown")).lower()
        if mtype not in MARK_TYPES:
            rejected.append({"reason": "UNKNOWN_TYPE", "ref": ref, "type": mtype})
            continue

        bbox = m.get("bbox")
        if (not isinstance(bbox, list) or len(bbox) != 4
                or not all(_is_num(v) for v in bbox)):
            rejected.append({"reason": "BBOX_INVALID", "ref": ref, "bbox": bbox})
            continue
        x0, y0, x1, y1 = [float(v) for v in bbox]
        if not all(0.0 <= v <= 1.0 for v in (x0, y0, x1, y1)):
            rejected.append({"reason": "BBOX_OUT_OF_RANGE", "ref": ref, "bbox": bbox})
            continue
        if x1 - x0 < 0.002 or y1 - y0 < 0.002:
            rejected.append({"reason": "BBOX_TOO_SMALL", "ref": ref, "bbox": bbox})
            continue
        if x1 <= x0 or y1 <= y0:
            rejected.append({"reason": "BBOX_INVERTED", "ref": ref, "bbox": bbox})
            continue

        conf = m.get("confidence")
        if not _is_num(conf):
            rejected.append({"reason": "CONFIDENCE_MISSING", "ref": ref})
            continue
        conf = max(0.0, min(1.0, float(conf)))

        text = str(m.get("text") or "").strip()
        warnings = []
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN]
            warnings.append("TEXT_TRUNCATED")

        accepted.append({
            "type": mtype,
            "bbox": [x0, y0, x1, y1],
            "text": text,
            "confidence": round(conf, 3),
            "ref": str(m.get("ref") or ref),
            "tile": tile_index,
            "warnings": warnings,
        })
    return accepted, rejected


DIMENSION_RE = re.compile(r"(Ду|DN|Ø|d)\s*(\d{2,4})", re.IGNORECASE)
QUANTITY_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(м3|м³|м2|м²|м\.п\.|п\.м\.|м|шт|т)\b",
                         re.IGNORECASE)
APPROVAL_RE = re.compile(r"согласован|допускается|разрешено|принято без замечан", re.IGNORECASE)


def dimensions_in(text: str) -> list:
    return [f"{m.group(1)} {m.group(2)}" for m in DIMENSION_RE.finditer(text or "")]


def quantities_in(text: str) -> list:
    return [(float(m.group(1).replace(",", ".")), m.group(2).lower())
            for m in QUANTITY_RE.finditer(text or "")]


def is_approval(text: str) -> bool:
    return bool(APPROVAL_RE.search(text or ""))
