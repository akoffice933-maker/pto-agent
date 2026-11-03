# -*- coding: utf-8 -*-
"""Пометки для исполнительной схемы: расхождения ПТО → типизированные операции.

Здесь нет OCR и распознавания картинок: источник пометки — уже найденное
расхождение (журнал/смета/ранее закрытые акты). Координаты берутся из самого
чертежа (габариты объекта), а не «придумываются» моделью. Пометки с уверенностью
ниже порога не превращаются в операции, а уходят в список решений человека.
"""
from difflib import SequenceMatcher

from ezdxf import bbox as ezbbox

from ..parsers import norm_text
from .ops import MIN_CONFIDENCE
from .takeoff import takeoff

RU_TYPE = {
    "VOLUME_OVER_ESTIMATE": "объём сверх сметы",
    "ALREADY_CLOSED": "повторное закрытие объёма",
    "DUPLICATE": "дубль записи в журнале",
    "MISSING_BACKING_DOCS": "нет подтверждающих документов",
    "UNIT_MISMATCH": "несовпадение единиц измерения",
    "NOT_IN_ESTIMATE": "работа отсутствует в смете",
}


def plan_from_discrepancies(summary: dict, drawing, template,
                            markup_layer: str = "ПОМЕТКИ",
                            max_ops: int = 40) -> dict:
    disc = summary.get("discrepancies") or []
    items = takeoff(drawing)
    by_code = {}
    for it in items:
        if it["code"]:
            by_code.setdefault(it["code"], it)

    operations, needs_decision, conflicts = [], [], []
    matched = []      # (idx, discrepancy, target, confidence, how)

    for idx, d in enumerate(disc, 1):
        dtype = d.get("type", "")
        target, confidence, how = None, 0.0, None

        if d.get("code") and d["code"] in by_code:
            target, confidence, how = by_code[d["code"]], 0.94, "шифр расценки в атрибуте блока"
        else:
            # поиск по наименованию: требуем не одно общее слово, а близость строк
            best, score = _best_by_name(d.get("name", ""), items)
            if best is not None and score >= 0.72:
                target, confidence = best, 0.74
                how = f"совпадение по наименованию ({score:.2f})"
            elif best is not None and score >= 0.55:
                target, confidence = best, 0.64          # ниже порога → решение человека
                how = f"слабое совпадение по наименованию ({score:.2f})"
            else:
                text_ent = _find_text(drawing, d.get("name", ""))
                if text_ent is not None:
                    target = {"handle": str(text_ent.dxf.handle), "name": d.get("name"),
                              "qty": None, "unit": None}
                    confidence, how = 0.66, "похожая подпись найдена на чертеже"

        if target is None or confidence < MIN_CONFIDENCE:
            needs_decision.append({
                "ref": f"discrepancies#{idx}",
                "type": dtype,
                "name": d.get("name"),
                "code": d.get("code"),
                "reason": ("позиция не найдена на чертеже" if target is None
                           else f"уверенность {confidence:.2f} ниже порога {MIN_CONFIDENCE}: {how}"),
                "confidence": round(confidence, 2),
                "suggested_action": "Указать объект на чертеже вручную или приложить скан с пометкой",
            })
            continue

        if drawing.entity(target["handle"]) is None:
            needs_decision.append({
                "ref": f"discrepancies#{idx}", "type": dtype, "name": d.get("name"),
                "code": d.get("code"), "reason": "объект-адресат не найден по handle",
                "confidence": 0.0, "suggested_action": "Проверить чертёж вручную",
            })
            continue

        matched.append((idx, d, target, confidence, how))

    # одна пометка на объект: одно облако + пояснения с вертикальным разносом,
    # иначе несколько расхождений по одной позиции накладываются друг на друга
    groups = {}
    for item in matched:
        groups.setdefault(item[2]["handle"], []).append(item)

    note_h = round(template.min_text_height_mm * template.scale_factor, 1)
    op_n = 0
    for handle, group in groups.items():
        ent = drawing.entity(handle)
        base = _note_point(ent, template)
        op_n += 1
        operations.append({
            "op_id": f"op-{op_n:03d}",
            "type": "ADD_CLOUD",
            "params": {"entity_handle": handle,
                       "margin": round(30 * template.scale_factor, 1), "layer": markup_layer},
            "reason": "облако пометки к объекту с расхождениями: "
                      + ", ".join(sorted({g[1]["type"] for g in group})),
            "source": {"type": "discrepancy", "ref": group[0][1].get("type") and
                       f"discrepancies#{group[0][0]}",
                       "confidence": group[0][3]},
            "requires_confirmation": True,
        })
        for line_no, (idx, d, _target, confidence, _how) in enumerate(group):
            op_n += 1
            operations.append({
                "op_id": f"op-{op_n:03d}",
                "type": "ADD_TEXT",
                "params": {"point": [base[0], round(base[1] - line_no * note_h * 1.6, 1)],
                           "text": _note_text(d), "height": note_h, "layer": markup_layer},
                "reason": f"пояснение к расхождению {d.get('type')}",
                "source": {"type": "discrepancy", "ref": f"discrepancies#{idx}",
                           "confidence": confidence},
                "requires_confirmation": True,
            })

    if len(operations) > max_ops:
        needs_decision.append({
            "ref": "plan", "type": "TOO_MANY_OPERATIONS",
            "reason": f"операций {len(operations)} больше лимита {max_ops}",
            "confidence": 1.0,
            "suggested_action": "Разбить выпуск схемы на несколько листов",
        })
        operations = operations[:max_ops]

    return {
        "drawing": {
            "file": str(drawing.path),
            "sha256": drawing.sha256,
            "units": drawing.inventory()["units"],
            "template": template.name,
            "takeoff_positions": len(items),
        },
        "operations": operations,
        "needs_human_decision": needs_decision,
        "conflicts": conflicts,
        "summary": {
            "operations": len(operations),
            "clouds": sum(1 for o in operations if o["type"] == "ADD_CLOUD"),
            "notes": sum(1 for o in operations if o["type"] == "ADD_TEXT"),
            "needs_human_decision": len(needs_decision),
        },
        "next_step": "Показать diff (dry_run) и запросить подтверждение инженера ПТО",
    }


def _note_point(ent, template):
    box = ezbbox.extents([ent])
    if not box.has_data:
        return [0.0, 0.0]
    step = 40 * template.scale_factor
    return [round(box.extmax.x + step, 1), round(box.extmax.y + step / 3, 1)]


def _note_text(d: dict) -> str:
    t = d.get("type", "")
    label = RU_TYPE.get(t, t)
    vals = d.get("values") or {}
    detail = ""
    if t == "VOLUME_OVER_ESTIMATE":
        detail = f": {vals.get('actual'):g} > {vals.get('estimate'):g} (+{vals.get('delta_pct')} %)"
    elif t == "ALREADY_CLOSED":
        detail = f": {vals.get('previously_closed'):g} + {vals.get('current'):g} > {vals.get('estimate'):g}"
    elif t == "DUPLICATE":
        detail = f": дублей {vals.get('duplicate_rows')}"
    elif t == "MISSING_BACKING_DOCS":
        detail = ": нет АОСР"
    elif t == "UNIT_MISMATCH":
        detail = f": {vals.get('estimate_unit')} в смете, {vals.get('journal_units')} в журнале"
    elif t == "NOT_IN_ESTIMATE":
        detail = ": нет в смете"
    return f"К КОРРЕКТИРОВКЕ — {label}{detail} (КС-2, черновик)"


def _best_by_name(disp_name: str, items: list):
    """Лучшее совпадение наименования расхождения с позицией на чертеже."""
    dn = norm_text(disp_name)
    if not dn:
        return None, 0.0
    words_d = {w for w in dn.split() if len(w) >= 4}
    best, best_score = None, 0.0
    for it in items:
        itn = norm_text(it.get("name") or "")
        if not itn:
            continue
        ratio = SequenceMatcher(None, dn, itn).ratio()
        words_i = {w for w in itn.split() if len(w) >= 4}
        overlap = len(words_d & words_i)
        score = 0.6 * ratio + 0.4 * (min(overlap, 3) / 3)
        if score > best_score:
            best, best_score = it, score
    return best, round(best_score, 3)


def _keywords(name: str) -> list:
    """Ключевые слова для поиска объекта на чертеже — от длинных к коротким.

    Порядок важен: сначала пробуем самое специфичное слово («асфальтобетона»),
    затем более общие («отмостки»), чтобы не потерять позицию из-за
    различий в формулировке сметы и подписи на чертеже.
    """
    words = [w for w in norm_text(name).split() if len(w) >= 5]
    return sorted(set(words), key=len, reverse=True)


def _find_text(drawing, name: str):
    """Похожая подпись на чертеже (по близости строк, а не по одному слову)."""
    dn = norm_text(name)
    if not dn:
        return None
    best, best_score = None, 0.0
    for e in drawing.msp:
        text = e.dxf.text if e.dxftype() == "TEXT" else (e.text if e.dxftype() == "MTEXT" else None)
        if not text:
            continue
        score = SequenceMatcher(None, dn, norm_text(text)).ratio()
        if score > best_score:
            best, best_score = e, score
    return best if best_score >= 0.5 else None
