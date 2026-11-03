# -*- coding: utf-8 -*-
"""Корпоративный шаблон оформления и проверки чертежа перед выпуском схемы."""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from ezdxf import bbox as ezbbox

DEFAULT_CHECKS = {
    "objects_on_protected_layers": "blocking",
    "text_height": "blocking",
    "titleblock_filled": "blocking",
    "unknown_layer": "warning",
    "unknown_block": "warning",
    "unknown_text_style": "warning",
    "outside_frame": "warning",
    "takeoff_vs_act_pct": 5,
}


@dataclass
class CorpTemplate:
    id: str
    version: int
    units: str = "mm"
    paper: str = "A1"
    scale: str = "1:100"
    frame_block: str | None = None
    titleblock_block: str | None = None
    protected_layers: list = field(default_factory=lambda: ["0", "DEFPOINTS"])
    required_layers: dict = field(default_factory=dict)
    allowed_blocks: list = field(default_factory=list)
    text_styles: list = field(default_factory=list)
    dim_styles: list = field(default_factory=list)
    min_text_height_mm: float = 2.5
    max_polyline_vertices: int = 2000
    titleblock_fields: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.id}/v{self.version}"

    @property
    def scale_factor(self) -> float:
        """Знаменатель масштаба: «1:100» → 100. Высота текста в модели = мм печати × фактор."""
        m = re.search(r"1\s*[:/]\s*(\d+)", str(self.scale or "1:1"))
        return float(m.group(1)) if m else 1.0

    def print_mm(self, model_height: float) -> float:
        return round(float(model_height) / self.scale_factor, 3)

    def check_level(self, name: str) -> str:
        v = (self.checks or {}).get(name, DEFAULT_CHECKS.get(name, "warning"))
        return v if isinstance(v, str) else "warning"

    def takeoff_pct(self) -> float:
        v = (self.checks or {}).get("takeoff_vs_act_pct", 5)
        return float(v) if isinstance(v, (int, float)) else 5.0

    def human_only_fields(self) -> set:
        return {k for k, v in (self.titleblock_fields or {}).items()
                if isinstance(v, dict) and v.get("human_only")}


def load_template(path: str | Path) -> CorpTemplate:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    known = {f for f in CorpTemplate.__dataclass_fields__}
    return CorpTemplate(**{k: v for k, v in data.items() if k in known})


def _issue(code, severity, message, handle=None, extra=None):
    return {"code": code, "severity": severity, "message": message,
            "handle": handle, **(extra or {})}


def validate_drawing(drawing, template: CorpTemplate) -> dict:
    """Проверка чертежа на соответствие корпоративному шаблону.

    Возвращает {issues, blocking, warnings, ok, checked}. Blocking-замечания
    запрещают выпуск исполнительной схемы.
    """
    issues = []
    msp = drawing.msp

    # 1a. Объекты на «пустых» слоях (0, DEFPOINTS) — блокирует выпуск.
    #     Слои оформления (рамка, штамп) проверяются отдельно: на них ЗАКОННО
    #     находятся только сами рамка и штамп.
    draft_layers = {"0", "DEFPOINTS"} & set(template.protected_layers)
    bad_layer_objs = [e for e in msp if e.dxf.layer in draft_layers]
    if bad_layer_objs:
        issues.append(_issue(
            "OBJECTS_ON_PROTECTED_LAYER",
            template.check_level("objects_on_protected_layers"),
            f"объектов на слоях {', '.join(sorted({e.dxf.layer for e in bad_layer_objs}))}: "
            f"{len(bad_layer_objs)} — их нужно перенести на рабочие слои",
            bad_layer_objs[0].dxf.handle,
            {"count": len(bad_layer_objs),
             "layers": sorted({e.dxf.layer for e in bad_layer_objs})}))

    # 1b. Посторонние объекты на слоях оформления
    design_layers = set(template.protected_layers) - draft_layers
    allowed_design = {template.frame_block, template.titleblock_block}
    strays = [e for e in msp if e.dxf.layer in design_layers
              and not (e.dxftype() == "INSERT" and e.dxf.name in allowed_design)]
    if strays:
        issues.append(_issue(
            "OBJECTS_ON_DESIGN_LAYER", "warning",
            f"на слоях оформления посторонних объектов: {len(strays)} "
            f"({', '.join(sorted({e.dxf.layer for e in strays}))})",
            strays[0].dxf.handle,
            {"count": len(strays), "handles": [str(e.dxf.handle) for e in strays[:10]]}))

    # 2. Высота текста
    low_text = []
    for e in msp:
        h = None
        if e.dxftype() in ("TEXT", "ATTRIB"):
            h = float(e.dxf.height)
        elif e.dxftype() == "MTEXT":
            h = float(e.dxf.char_height)
        if h is not None and template.print_mm(h) < template.min_text_height_mm:
            low_text.append((e, h))
    if low_text:
        issues.append(_issue(
            "TEXT_HEIGHT_BELOW_MIN",
            template.check_level("text_height"),
            f"текстов с высотой менее {template.min_text_height_mm} мм в печати: {len(low_text)} "
            f"(минимум {template.print_mm(min(h for _, h in low_text))} мм при масштабе {template.scale})",
            low_text[0][0].dxf.handle,
            {"count": len(low_text),
             "handles": [str(e.dxf.handle) for e, _ in low_text[:20]]}))

    # 3. Штамп
    tb = drawing.titleblock(template.titleblock_block)
    if tb is None:
        issues.append(_issue("TITLEBLOCK_MISSING",
                             template.check_level("titleblock_filled"),
                             f"не найден блок штампа «{template.titleblock_block}»"))
    else:
        empty = [f for f, props in (template.titleblock_fields or {}).items()
                 if isinstance(props, dict) and props.get("required")
                 and not (tb["fields"].get(f) or "").strip()]
        if empty:
            issues.append(_issue(
                "TITLEBLOCK_INCOMPLETE",
                template.check_level("titleblock_filled"),
                f"в штампе не заполнены обязательные поля: {', '.join(empty)}",
                tb["handle"], {"empty_fields": empty}))

    # 4. Слои не из шаблона
    known_layers = set(template.required_layers) | set(template.protected_layers) | {"ПОМЕТКИ"}
    unknown = sorted({e.dxf.layer for e in msp} - known_layers)
    if unknown:
        issues.append(_issue("UNKNOWN_LAYER", template.check_level("unknown_layer"),
                             f"слои отсутствуют в шаблоне: {', '.join(unknown)}",
                             None, {"layers": unknown}))

    # 5. Слои шаблона, которых нет в файле (предупреждение, не блокирует)
    missing = sorted(set(template.required_layers) - {l.dxf.name for l in drawing.doc.layers})
    if missing:
        issues.append(_issue("TEMPLATE_LAYER_ABSENT", "warning",
                             f"в файле нет слоёв шаблона: {', '.join(missing)}",
                             None, {"layers": missing}))

    # 6. Блоки не из шаблона
    if template.allowed_blocks:
        bad_blocks = sorted({e.dxf.name for e in msp if e.dxftype() == "INSERT"}
                            - set(template.allowed_blocks))
        if bad_blocks:
            issues.append(_issue("UNKNOWN_BLOCK", template.check_level("unknown_block"),
                                 f"блоки не из шаблона: {', '.join(bad_blocks)}",
                                 None, {"blocks": bad_blocks}))

    # 7. Текстовые стили
    if template.text_styles:
        bad_styles = sorted({e.dxf.style for e in msp
                             if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB")
                             and e.dxf.hasattr("style")}
                            - set(template.text_styles))
        if bad_styles:
            issues.append(_issue("UNKNOWN_TEXT_STYLE", template.check_level("unknown_text_style"),
                                 f"текстовые стили не из шаблона: {', '.join(bad_styles)}",
                                 None, {"styles": bad_styles}))

    # 8. Объекты вне рамки
    frame = next((e for e in msp if e.dxftype() == "INSERT"
                  and e.dxf.name == template.frame_block), None)
    if frame is not None:
        fb = ezbbox.extents([frame])
        mb = ezbbox.extents([e for e in msp if e.dxftype() != "INSERT"
                             or e.dxf.name != template.frame_block])
        if fb.has_data and mb.has_data and not _inside(mb, fb):
            issues.append(_issue("OUTSIDE_FRAME", template.check_level("outside_frame"),
                                 "часть объектов выходит за границы рамки листа"))

    blocking = [i for i in issues if i["severity"] == "blocking"]
    return {
        "template": template.name,
        "file": str(drawing.path),
        "issues": issues,
        "blocking": len(blocking),
        "warnings": len(issues) - len(blocking),
        "ok": not blocking,
    }


def _inside(inner, outer) -> bool:
    pad = 1.0
    return (inner.extmin.x >= outer.extmin.x - pad and inner.extmin.y >= outer.extmin.y - pad
            and inner.extmax.x <= outer.extmax.x + pad and inner.extmax.y <= outer.extmax.y + pad)
