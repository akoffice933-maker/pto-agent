# -*- coding: utf-8 -*-
"""Распознанные пометки → типизированные операции над чертежом + конфликты.

Пометка становится операцией только если: есть текст, уверенность ≥ 0,7,
найден объект-адресат на чертеже и нет конфликта с данными ПТО (КС-2/журнал).
Иначе — вопрос человеку с причиной.
"""
from ..ops import MIN_CONFIDENCE
from ezdxf import bbox as ezbbox

from ..takeoff import takeoff
from .schema import dimensions_in, is_approval, quantities_in

TARGET_RADIUS_MM = 3000.0   # допуск привязки пометки к позиции на чертеже

CONFLICT_APPROVAL = "CONFLICT_APPROVAL"
CONFLICT_QUANTITY = "CONFLICT_QUANTITY"


def plan_from_scan(run: dict, drawing, template, summary: dict | None = None,
                   markup_layer: str = "ПОМЕТКИ", max_ops: int = 60) -> dict:
    marks = run.get("marks") or []
    takeoff_items = {t["handle"]: t for t in takeoff(drawing)}
    lines_by_code = {}
    discrepancies_by_code = {}
    if summary:
        lines_by_code = {l["code"]: l for l in summary.get("lines") or [] if l.get("code")}
        for d in summary.get("discrepancies") or []:
            if d.get("code"):
                discrepancies_by_code.setdefault(d["code"], []).append(d)

    operations, needs_decision, conflicts, accepted = [], [], [], []
    op_n = 0

    for m in marks:
        mid = m["mark_id"]
        text = (m.get("text") or "").strip()
        conf = float(m.get("confidence") or 0)
        box = m.get("coords_model") or [0, 0, 0, 0]

        target_handle, target_info, match = _find_target(
            drawing, box, takeoff_items, text=text)
        code = (target_info or {}).get("code")
        m["_match"] = match

        # конфликт: «согласовано» там, где у ПТО критичное расхождение
        if text and is_approval(text) and code and discrepancies_by_code.get(code):
            critical = [d for d in discrepancies_by_code[code] if d.get("severity") == "critical"]
            if critical:
                conflicts.append({
                    "type": CONFLICT_APPROVAL, "mark_id": mid, "code": code,
                    "mark_text": text,
                    "critical_types": sorted({d.get("type", "") for d in critical}),
                    "critical_count": len(critical),
                    "resolution": _approval_resolution(code, critical),
                })
                needs_decision.append(_decision(m, "конфликт с данными ПТО: " + CONFLICT_APPROVAL,
                                                "Снять пометку или получить подписанное обоснование"))
                continue

        # конфликт по количеству: цифра в пометке против акта
        if text and code and code in lines_by_code and quantities_in(text):
            line = lines_by_code[code]
            act_qty = float(line.get("qty_journal") or 0)
            for qty, unit in quantities_in(text):
                if act_qty and abs(qty - act_qty) / act_qty > 0.01:
                    conflicts.append({
                        "type": CONFLICT_QUANTITY, "mark_id": mid, "code": code,
                        "mark_value": f"{qty:g} {unit}",
                        "act_value": f"{act_qty:g} {line.get('unit')}",
                        # числа отдельно от подписей: по ним считается стоимость спора
                        "mark_qty": float(qty), "act_qty": float(act_qty),
                        "unit": line.get("unit") or unit,
                        "delta_qty": round(float(qty) - float(act_qty), 4),
                        "resolution": ("объём в пометке не совпадает с журналом/актом; "
                                       "нужен пересчёт или подтверждение объёма"),
                    })
                    needs_decision.append(_decision(
                        m, f"конфликт по объёму: в пометке {qty:g} {unit}, "
                           f"в акте {act_qty:g} {line.get('unit')}",
                        "Сверить объём с журналом работ и подтвердить"))
                    break
            else:
                pass
            if any(n["mark_id"] == mid for n in needs_decision):
                continue

        # нет текста или низкая уверенность — не операция, а вопрос человеку
        if not text:
            needs_decision.append(_decision(
                m, "текст пометки не распознан",
                "Ввести текст пометки вручную (--texts) или подключить VL-модель"))
            continue
        if conf < MIN_CONFIDENCE:
            needs_decision.append(_decision(
                m, f"уверенность {conf:.2f} ниже порога {MIN_CONFIDENCE}",
                "Проверить пометку глазами и подтвердить текст"))
            continue

        m["_handle"], m["_info"] = target_handle, target_info
        accepted.append(m)

    # группировка: одна пометка — одно облако + один текст
    for m in accepted:
        mid = m["mark_id"]
        box = m["coords_model"]
        handle = m.get("_handle")
        info = m.get("_info")
        match = m.get("_match")
        if handle is None:
            handle, info, match = _find_target(drawing, box, takeoff_items,
                                               text=m.get("text", ""))
        conf = float(m["confidence"])
        match_note = _MATCH_NOTES.get(match, "")
        if handle:
            op_n += 1
            operations.append({
                "op_id": f"op-{op_n:03d}", "type": "ADD_CLOUD",
                "params": {"entity_handle": handle,
                           "margin": round(25 * template.scale_factor, 1),
                           "layer": markup_layer},
                "reason": (f"пометка на скане ({m['type']}) относится к позиции "
                           f"{(info or {}).get('code') or 'без шифра'}{match_note}"),
                "source": {"type": "redline_scan", "ref": mid, "confidence": conf,
                           "image": run.get("image")},
                "requires_confirmation": True,
            })
        else:
            op_n += 1
            x0, y0, x1, y1 = box
            operations.append({
                "op_id": f"op-{op_n:03d}", "type": "ADD_POLYLINE",
                "params": {"vertices": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                           "closed": True, "layer": markup_layer},
                "reason": "рамка вокруг пометки: объект-адресат на чертеже не найден",
                "source": {"type": "redline_scan", "ref": mid, "confidence": conf,
                           "image": run.get("image")},
                "requires_confirmation": True,
            })
        op_n += 1
        point = [box[2] + 20 * template.scale_factor / 10,
                 box[3] + 20 * template.scale_factor / 10]
        operations.append({
            "op_id": f"op-{op_n:03d}", "type": "ADD_TEXT",
            "params": {"point": [round(point[0], 1), round(point[1], 1)],
                       "text": _note_text(m),
                       "height": round(template.min_text_height_mm * template.scale_factor, 1),
                       "layer": markup_layer},
            "reason": f"текст пометки со скана ({m['engine']})",
            "source": {"type": "redline_scan", "ref": mid, "confidence": conf,
                       "image": run.get("image")},
            "requires_confirmation": True,
        })

    if len(operations) > max_ops:
        needs_decision.append({
            "mark_id": "plan", "reason": f"операций {len(operations)} больше лимита {max_ops}",
            "suggested_action": "Разбить выпуск схемы на несколько листов"})
        operations = operations[:max_ops]

    return {
        "source": {
            "image": run.get("image"), "image_size": run.get("image_size"),
            "engine": run.get("engine_used"), "provider": run.get("provider"),
            "calibration": run.get("calibration"), "texts_source": run.get("texts_source"),
            "recognition_warnings": run.get("warnings"),
        },
        "operations": operations,
        "needs_human_decision": needs_decision,
        "conflicts": conflicts,
        "summary": {
            "marks": len(marks),
            "operations": len(operations),
            "conflicts": len(conflicts),
            "needs_human_decision": len(needs_decision),
        },
        "next_step": "Показать diff (dry_run) и запросить подтверждение инженера ПТО",
    }


def _approval_resolution(code: str, critical: list) -> str:
    types = ", ".join(sorted({d.get("type", "") for d in critical}))
    return (f"пометка «согласовано» на позиции {code}, по которой есть "
            f"{len(critical)} критичных расхождений ({types}); приоритет у подписанных "
            f"документов — требуется решение человека")


def _find_target(drawing, box, takeoff_items, text: str = ""):
    """Объект-адресат пометки. Возвращает (handle, info, способ_привязки).

    1. ``in_box``      — центр позиции внутри рамки пометки;
    2. ``nearest``     — рамка пересекает или почти касается позиции (≤ TARGET_RADIUS_MM);
    3. ``by_quantity`` — пометка с объёмом: позиция той же единицы с ближайшим объёмом
                         (при ``cross_out``/``underline`` пометку ставят на ведомость, а не на план);
    4. ``text_entity`` — надпись/блок под пометкой без шифра расценки.
    """
    if not takeoff_items:
        return None, None, None
    extents = {}
    for handle in takeoff_items:
        ent = drawing.entity(handle)
        if ent is None:
            continue
        ext = ezbbox.extents([ent])
        if ext.has_data:
            extents[handle] = (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)

    inside = [h for h, b in extents.items()
              if box[0] <= (b[0] + b[2]) / 2 <= box[2] and box[1] <= (b[1] + b[3]) / 2 <= box[3]]
    if inside:
        inside.sort(key=lambda h: _area(extents[h]))
        return inside[0], takeoff_items[inside[0]], "in_box"

    distances = sorted((_rect_distance(box, b), h) for h, b in extents.items())
    if distances and distances[0][0] <= TARGET_RADIUS_MM:
        return distances[0][1], takeoff_items[distances[0][1]], "nearest"

    quantities = quantities_in(text) if text else []
    if quantities:
        qty, unit = quantities[0]
        same_unit = [(abs(float(t.get("qty") or 0) - qty), h)
                     for h, t in takeoff_items.items()
                     if (t.get("unit") or "").strip().lower() == unit.strip().lower()
                     and t.get("qty")]
        if same_unit:
            same_unit.sort()
            best_delta, handle = same_unit[0]
            relative = best_delta / max(qty, 1e-9)
            if relative <= 0.25:      # объём отличается не более чем на четверть
                return handle, takeoff_items[handle], "by_quantity"

    for ent in drawing.msp:
        if ent.dxftype() not in ("TEXT", "MTEXT", "INSERT"):
            continue
        ext = ezbbox.extents([ent])
        if not ext.has_data:
            continue
        cx = (ext.extmin.x + ext.extmax.x) / 2
        cy = (ext.extmin.y + ext.extmax.y) / 2
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            info = takeoff_items.get(str(ent.dxf.handle))
            return str(ent.dxf.handle), info, "text_entity"
    return None, None, None


_MATCH_NOTES = {
    "in_box": "",
    "nearest": ", привязка по ближайшей позиции",
    "by_quantity": ", позиция сопоставлена по объёму (проверить глазами)",
    "text_entity": ", привязка к надписи без шифра расценки",
}


def _area(b) -> float:
    return max(b[2] - b[0], 0) * max(b[3] - b[1], 0)


def _rect_distance(a, b) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0)
    dy = max(b[1] - a[3], a[1] - b[3], 0)
    return (dx * dx + dy * dy) ** 0.5


def _decision(mark, reason, action):
    return {
        "mark_id": mark["mark_id"],
        "type": mark["type"],
        "text": mark.get("text", ""),
        "confidence": mark.get("confidence"),
        "bbox_px": mark.get("bbox_px"),
        "coords_model": mark.get("coords_model"),
        "reason": reason,
        "suggested_action": action,
    }


def _note_text(mark) -> str:
    dims = dimensions_in(mark.get("text", ""))
    text = f"ПОМЕТКА СО СКАНА ({mark['engine']}): {mark['text']}"
    if dims:
        text += f" [обозначения: {', '.join(dims)}]"
    return text
