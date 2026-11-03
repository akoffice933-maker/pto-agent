# -*- coding: utf-8 -*-
"""Объёмы, «проставленные» на чертеже: извлечение и сверка с актом/сметой.

Блок ПОЗ_ОБЪЁМ содержит атрибуты PTO_CODE (шифр расценки), PTO_QTY (объём),
PTO_UNIT (единица), PTO_NAME (наименование). Это машиночитаемая связь чертежа
с КС-2: агент сверяет объёмы схемы с объёмами, предъявленными в акте (±5 % по ТЗ).
"""
from difflib import SequenceMatcher

from ..parsers import norm_text, norm_unit

TAG_CODE, TAG_QTY, TAG_UNIT, TAG_NAME = "PTO_CODE", "PTO_QTY", "PTO_UNIT", "PTO_NAME"


def takeoff(drawing) -> list:
    items = []
    for e in drawing.msp:
        if e.dxftype() != "INSERT":
            continue
        attrs = {a.dxf.tag: (a.dxf.text or "").strip() for a in e.attribs}
        if TAG_CODE not in attrs and TAG_NAME not in attrs:
            continue
        items.append({
            "code": attrs.get(TAG_CODE) or None,
            "name": attrs.get(TAG_NAME, ""),
            "qty": _num(attrs.get(TAG_QTY)),
            "qty_raw": attrs.get(TAG_QTY, ""),
            "unit": norm_unit(attrs.get(TAG_UNIT, "")),
            "block": e.dxf.name,
            "handle": str(e.dxf.handle),
            "layer": e.dxf.layer,
        })
    return items


def compare_takeoff(items: list, act_lines: list, pct: float = 5.0) -> dict:
    """Сверка объёмов чертежа с объёмами акта. act_lines: [{code,name,unit,qty_journal}]."""
    by_code = {l["code"]: l for l in act_lines if l.get("code")}
    by_name = {norm_text(l["name"]): l for l in act_lines}
    issues, matched = [], []

    used = set()
    for it in items:
        line = by_code.get(it["code"]) if it["code"] else None
        kind = "code"
        if line is None:
            line = by_name.get(norm_text(it["name"]))
            kind = "name"
        if line is None:
            best, score = None, 0.0
            for l in act_lines:
                s = SequenceMatcher(None, norm_text(it["name"]), norm_text(l["name"])).ratio()
                if s > score:
                    best, score = l, s
            if best is not None and score >= 0.82:
                line, kind = best, f"fuzzy:{score:.2f}"

        if line is None:
            issues.append({
                "type": "CAD_POSITION_NOT_IN_ACT",
                "severity": "critical",
                "code": it["code"],
                "name": it["name"],
                "cad_qty": it["qty"], "unit": it["unit"],
                "message": "работа есть на схеме, но отсутствует в смете/акте",
                "handle": it["handle"],
            })
            continue

        used.add(line["code"])
        act_qty = float(line.get("qty_journal") or 0)
        if it["unit"] and line.get("unit") and it["unit"] != line["unit"]:
            issues.append({
                "type": "CAD_UNIT_MISMATCH", "severity": "critical",
                "code": line["code"], "name": line["name"],
                "cad_unit": it["unit"], "act_unit": line["unit"],
                "message": f"единицы на схеме ({it['unit']}) не совпадают с актом ({line['unit']})",
                "handle": it["handle"],
            })
            continue

        delta = it["qty"] - act_qty
        delta_pct = round(100 * delta / act_qty, 2) if act_qty else None
        record = {
            "code": line["code"], "name": line["name"], "unit": line["unit"],
            "cad_qty": it["qty"], "act_qty": act_qty, "delta": round(delta, 4),
            "delta_pct": delta_pct, "match": kind, "handle": it["handle"],
        }
        matched.append(record)
        if delta_pct is not None and abs(delta_pct) > pct:
            issues.append({
                "type": "CAD_QTY_MISMATCH",
                "severity": "critical" if abs(delta_pct) > 15 else "major",
                "code": line["code"], "name": line["name"],
                "cad_qty": it["qty"], "act_qty": act_qty,
                "delta_pct": delta_pct, "threshold_pct": pct,
                "message": f"объём на схеме {it['qty']:g} {it['unit']} против "
                           f"{act_qty:g} {line['unit']} в акте ({delta_pct:+.1f} %)",
                "handle": it["handle"],
            })

    for line in act_lines:
        if line.get("code") in used or not (line.get("qty_journal") or 0):
            continue
        issues.append({
            "type": "ACT_POSITION_NOT_IN_CAD",
            "severity": "warning",
            "code": line.get("code"),
            "name": line.get("name"),
            "act_qty": float(line.get("qty_journal") or 0),
            "unit": line.get("unit"),
            "message": "объём предъявлен в акте, но на схеме не подтверждён",
            "handle": None,
        })

    return {
        "positions_on_drawing": len(items),
        "positions_in_act": len(act_lines),
        "matched": matched,
        "issues": issues,
        "critical": sum(1 for i in issues if i["severity"] == "critical"),
        "tolerance_pct": pct,
    }


def _num(value) -> float:
    if value is None:
        return 0.0
    s = str(value).replace(" ", "").replace(",", ".").replace("\xa0", "")
    try:
        return float(s)
    except ValueError:
        return 0.0
