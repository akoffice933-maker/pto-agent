# -*- coding: utf-8 -*-
"""Headless-чтение чертежей DXF (ezdxf) и инвентаризация для агента."""
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
from ezdxf import bbox as ezbbox

UNITS = {0: "unitless", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m", 7: "km"}


class DwgNotSupported(RuntimeError):
    """DWG нельзя прочитать headless-библиотекой — нужна конвертация в DXF."""


@dataclass
class Drawing:
    path: Path
    doc: object
    sha256: str
    version: int = 1
    meta: dict = field(default_factory=dict)

    # ── открытие ──────────────────────────────────────────────────────────
    @classmethod
    def open(cls, path: str | Path) -> "Drawing":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"файл не найден: {path}")
        head = path.open("rb").read(6)
        if path.suffix.lower() == ".dwg" or head[:3] == b"AC1":
            raise DwgNotSupported(
                f"{path.name}: формат DWG закрыт и не читается headless-библиотекой ezdxf. "
                "Нужна предварительная конвертация в DXF (ODA File Converter, "
                "dwg2dxf из LibreDWG) на стороне заказчика; после конвертации сверьте "
                "количество объектов до/после (требование ТЗ).")
        doc = ezdxf.readfile(str(path))
        return cls(path=path, doc=doc, sha256=_sha256(path))

    # ── сохранение ────────────────────────────────────────────────────────
    def save_as(self, path: str | Path) -> "Drawing":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.saveas(str(path))
        return Drawing(path=path, doc=ezdxf.readfile(str(path)),
                       sha256=_sha256(path), version=self.version, meta=dict(self.meta))

    # ── доступ к объектам ─────────────────────────────────────────────────
    @property
    def msp(self):
        return self.doc.modelspace()

    def entity(self, handle: str):
        h = str(handle).lstrip("#").upper()
        for e in self.msp:
            if str(e.dxf.handle).upper() == h:
                return e
            for att in getattr(e, "attribs", []) or []:
                if str(att.dxf.handle).upper() == h:
                    return att
        return None

    def attribs_by_handle(self, handle: str) -> dict:
        ent = self.entity(handle)
        if ent is None or ent.dxftype() != "INSERT":
            return {}
        return {a.dxf.tag: a.dxf.text for a in ent.attribs}

    # ── инвентаризация ────────────────────────────────────────────────────
    def inventory(self) -> dict:
        by_type = Counter(e.dxftype() for e in self.msp)
        by_layer = Counter(e.dxf.layer for e in self.msp)
        text_heights = []
        for e in self.msp:
            if e.dxftype() in ("TEXT", "ATTRIB"):
                text_heights.append(round(float(e.dxf.height), 2))
            elif e.dxftype() == "MTEXT":
                text_heights.append(round(float(e.dxf.char_height), 2))
        inserts = Counter(e.dxf.name for e in self.msp if e.dxftype() == "INSERT")

        ext = ezbbox.extents(self.msp)
        extents = ({"min": _v(ext.extmin), "max": _v(ext.extmax)}
                   if ext.has_data else None)

        return {
            "file": str(self.path),
            "sha256": self.sha256,
            "version": self.version,
            "dxf_version": self.doc.dxfversion,
            "units": UNITS.get(self.doc.units, self.doc.units),
            "entities_total": len(self.msp),
            "entities_by_type": dict(by_type.most_common()),
            "entities_by_layer": dict(by_layer.most_common()),
            "layers": [{"name": l.dxf.name, "color": l.dxf.color,
                        "linetype": l.dxf.linetype, "off": bool(l.is_off())}
                       for l in self.doc.layers],
            "blocks_defined": sorted(b.name for b in self.doc.blocks if not b.name.startswith("*")),
            "block_inserts": dict(inserts.most_common()),
            "text_styles": sorted(s.dxf.name for s in self.doc.styles),
            "dim_styles": sorted(d.dxf.name for d in self.doc.dimstyles),
            "text_heights": {"min": min(text_heights) if text_heights else None,
                             "max": max(text_heights) if text_heights else None,
                             "count": len(text_heights)},
            "extents": extents,
            "layers_total": len(self.doc.layers),
        }

    def titleblock(self, block_name: str | None = None) -> dict | None:
        """Найти вставку штампа и вернуть её поля (по имени блока или по тегам)."""
        for e in self.msp:
            if e.dxftype() != "INSERT":
                continue
            tags = {a.dxf.tag for a in e.attribs}
            if (block_name and e.dxf.name == block_name) or {"ШИФР", "ОБЪЕКТ"} <= tags:
                return {
                    "handle": str(e.dxf.handle),
                    "block": e.dxf.name,
                    "fields": {a.dxf.tag: a.dxf.text for a in e.attribs},
                }
        return None


def open_drawing(path: str | Path) -> Drawing:
    return Drawing.open(path)


def _v(vec) -> list:
    return [round(float(vec.x), 3), round(float(vec.y), 3)]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
