# -*- coding: utf-8 -*-
"""Типизированные операции над чертежом: белый список, валидация, dry-run и commit.

Агент не редактирует DXF напрямую: он формирует список операций, каждая из которых
проходит валидацию по шаблону, затем применяется в режиме dry_run (без записи файла),
и только после подтверждения человеком — в режиме commit (новой версией файла).
"""
import copy
import re
import tempfile
from datetime import datetime
from pathlib import Path

import ezdxf
from ezdxf import bbox as ezbbox

from .diff import diff_drawings
from .template import CorpTemplate

MIN_CONFIDENCE = 0.7
DEFAULT_MARKUP_LAYER = "ПОМЕТКИ"


class OperationError(RuntimeError):
    pass


# ── обработчики операций (белый список) ──────────────────────────────────
def _op_add_text(doc, op, tpl):
    p, q = op["params"], op["params"]
    e = doc.modelspace().add_text(
        q["text"], height=float(q.get("height", tpl.min_text_height_mm * tpl.scale_factor)),
        dxfattribs={"layer": _layer(doc, q.get("layer"), tpl),
                    "style": q.get("style", (tpl.text_styles or [""])[0])})
    e.dxf.insert = (float(p["point"][0]), float(p["point"][1]))
    return {"handle": str(e.dxf.handle), "type": "TEXT", "layer": e.dxf.layer}


def _op_add_polyline(doc, op, tpl):
    q = op["params"]
    verts = [(float(x), float(y)) for x, y in q["vertices"]]
    e = doc.modelspace().add_lwpolyline(verts, close=bool(q.get("closed", False)),
                                        dxfattribs={"layer": _layer(doc, q.get("layer"), tpl)})
    return {"handle": str(e.dxf.handle), "type": "LWPOLYLINE", "layer": e.dxf.layer}


def _op_add_cloud(doc, op, tpl):
    """Облако пометки вокруг объекта (замкнутая полилиния с зазубринами)."""
    q = op["params"]
    target = _require_entity(doc, q["entity_handle"])
    margin = float(q.get("margin", 40))
    box = ezbbox.extents([target])
    if not box.has_data:
        raise OperationError(f"не удалось определить габариты объекта {q['entity_handle']}")
    x0, y0 = box.extmin.x - margin, box.extmin.y - margin
    x1, y1 = box.extmax.x + margin, box.extmax.y + margin
    step = max((x1 - x0), (y1 - y0)) / 12.0
    pts = []
    corners = [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]
    for ax, ay, bx, by in corners:
        n = max(int(abs(bx - ax) + abs(by - ay)) // max(int(step), 1), 2)
        for i in range(n):
            t = i / n
            x, y = ax + (bx - ax) * t, ay + (by - ay) * t
            bump = (step / 6.0) if i % 2 == 0 else -(step / 8.0)
            # смещение «наружу» от центра
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            dx, dy = x - cx, y - cy
            norm = max((dx ** 2 + dy ** 2) ** 0.5, 1e-6)
            pts.append((x + dx / norm * bump, y + dy / norm * bump))
    e = doc.modelspace().add_lwpolyline(pts, close=True,
                                        dxfattribs={"layer": _layer(doc, q.get("layer"), tpl)})
    return {"handle": str(e.dxf.handle), "type": "LWPOLYLINE",
            "layer": e.dxf.layer, "around": q["entity_handle"]}


def _op_set_attribute(doc, op, tpl):
    q = op["params"]
    ent = _require_entity(doc, q["handle"])
    if ent.dxftype() != "INSERT":
        raise OperationError(f"{q['handle']}: атрибуты есть только у вставок блоков")
    tag = q["tag"]
    human_only = tpl.human_only_fields()
    if tag in human_only:
        raise OperationError(f"поле «{tag}» заполняет только человек (шаблон {tpl.name})")
    target = next((a for a in ent.attribs if a.dxf.tag == tag), None)
    if target is None:
        raise OperationError(f"в блоке {ent.dxf.name} нет атрибута {tag}")
    before = target.dxf.text
    target.dxf.text = str(q["value"])
    return {"handle": str(target.dxf.handle), "type": "ATTRIB", "tag": tag,
            "before": before, "after": str(q["value"])}


def _op_move_entity(doc, op, tpl):
    q = op["params"]
    ent = _require_entity(doc, q["handle"])
    if ent.dxf.layer in set(tpl.protected_layers):
        raise OperationError(f"объект на защищённом слое {ent.dxf.layer} — перемещение запрещено")
    dx, dy = float(q["dx"]), float(q["dy"])
    ent.translate(dx, dy, 0)
    return {"handle": str(ent.dxf.handle), "type": ent.dxftype(), "dx": dx, "dy": dy}


def _op_update_titleblock(doc, op, tpl):
    q = op["params"]
    fields = q.get("fields") or {}
    human_only = tpl.human_only_fields()
    allowed = set(tpl.titleblock_fields or {}) - human_only
    bad = sorted(set(fields) - allowed)
    if bad:
        raise OperationError(f"поля штампа вне шаблона или только для человека: {', '.join(bad)}")
    tb = _find_titleblock(doc, tpl.titleblock_block)
    if tb is None:
        raise OperationError("вставка штампа не найдена")
    changed = {}
    for tag, value in fields.items():
        att = next((a for a in tb.attribs if a.dxf.tag == tag), None)
        if att is None:
            raise OperationError(f"в штампе нет поля {tag}")
        changed[tag] = {"before": att.dxf.text, "after": str(value)}
        att.dxf.text = str(value)
    return {"handle": str(tb.dxf.handle), "type": "INSERT", "fields": changed}


def _op_add_block(doc, op, tpl):
    q = op["params"]
    name = q["name"]
    if tpl.allowed_blocks and name not in tpl.allowed_blocks:
        raise OperationError(f"блок «{name}» не входит в корпоративный шаблон {tpl.name}")
    if name not in doc.blocks:
        raise OperationError(f"блок «{name}» отсутствует в файле")
    ref = doc.modelspace().add_blockref(name, (float(q["point"][0]), float(q["point"][1])),
                                        dxfattribs={"layer": _layer(doc, q.get("layer"), tpl)})
    if q.get("attrs"):
        try:
            ref.add_auto_attribs(q["attrs"])
        except Exception as exc:
            raise OperationError(f"атрибуты блока не применены: {exc}") from exc
    return {"handle": str(ref.dxf.handle), "type": "INSERT", "block": name}


OPERATIONS = {
    "ADD_TEXT": {"handler": _op_add_text, "label": "Текстовая пометка",
                 "params": ["point", "text"]},
    "ADD_POLYLINE": {"handler": _op_add_polyline, "label": "Полилиния",
                     "params": ["vertices"]},
    "ADD_CLOUD": {"handler": _op_add_cloud, "label": "Облако пометки вокруг объекта",
                  "params": ["entity_handle"]},
    "SET_ATTRIBUTE": {"handler": _op_set_attribute, "label": "Значение атрибута блока",
                      "params": ["handle", "tag", "value"]},
    "MOVE_ENTITY": {"handler": _op_move_entity, "label": "Перенос объекта",
                    "params": ["handle", "dx", "dy"]},
    "UPDATE_TITLEBLOCK": {"handler": _op_update_titleblock, "label": "Заполнение штампа",
                          "params": ["fields"]},
    "ADD_BLOCK": {"handler": _op_add_block, "label": "Вставка блока из шаблона",
                  "params": ["name", "point"]},
}
# DELETE_*, MODIFY_LAYER, ADD_RASTER, ROTATE_ENTITY и прочее сознательно не реализованы:
# операция вне белого списка отклоняется на валидации.

_FORBIDDEN = ["DELETE", "ERASE", "MODIFY_LAYER", "ADD_RASTER", "ROTATE", "SCALE", "COPY"]


# ── валидация ────────────────────────────────────────────────────────────
def _issue(severity, code, message, op_id=None, extra=None):
    return {"severity": severity, "code": code, "message": message,
            "op_id": op_id, **(extra or {})}


def validate_operations(doc, ops, tpl: CorpTemplate) -> list:
    """Проверка плана операций: белый список, шаблон, ограничения, confidence."""
    issues = []
    protected = set(tpl.protected_layers)
    msp = doc.modelspace()

    for i, op in enumerate(ops, 1):
        op_id = op.get("op_id") or f"op-{i:03d}"
        op_type = str(op.get("type", "")).upper()
        params = op.get("params") or {}

        if op_type not in OPERATIONS:
            hint = " (операция запрещена политикой)" if any(f in op_type for f in _FORBIDDEN) else ""
            issues.append(_issue("blocking", "OP_NOT_ALLOWED",
                                 f"{op_type or 'без типа'}: операция вне белого списка{hint}", op_id))
            continue

        missing = [p for p in OPERATIONS[op_type]["params"] if p not in params]
        if missing:
            issues.append(_issue("blocking", "OP_PARAMS_MISSING",
                                 f"{op_type}: нет параметров {', '.join(missing)}", op_id))
            continue

        conf = (op.get("source") or {}).get("confidence")
        if conf is not None and float(conf) < MIN_CONFIDENCE:
            issues.append(_issue("blocking", "LOW_CONFIDENCE",
                                 f"{op_type}: уверенность {conf} ниже порога {MIN_CONFIDENCE} — "
                                 "нужно решение человека", op_id))
            continue
        if not op.get("reason"):
            issues.append(_issue("warning", "NO_REASON", f"{op_type}: не указана причина операции", op_id))
        if not (op.get("source") or {}).get("ref"):
            issues.append(_issue("warning", "NO_SOURCE", f"{op_type}: нет ссылки на источник", op_id))

        # слой
        layer = params.get("layer", DEFAULT_MARKUP_LAYER if op_type in ("ADD_TEXT", "ADD_CLOUD") else None)
        if layer and layer in protected:
            issues.append(_issue("blocking", "PROTECTED_LAYER",
                                 f"{op_type}: слой «{layer}» защищён шаблоном", op_id))
        if layer and layer not in [l.dxf.name for l in doc.layers]:
            issues.append(_issue("warning", "LAYER_WILL_BE_CREATED",
                                 f"{op_type}: слой «{layer}» будет создан", op_id))

        # специфика операций
        if op_type == "ADD_TEXT":
            h = float(params.get("height", tpl.min_text_height_mm * tpl.scale_factor))
            if tpl.print_mm(h) < tpl.min_text_height_mm:
                issues.append(_issue("blocking", "TEXT_HEIGHT",
                                     f"высота текста {h:g} (в печати {tpl.print_mm(h)} мм) "
                                     f"меньше {tpl.min_text_height_mm} мм", op_id))
        elif op_type == "ADD_POLYLINE":
            n = len(params["vertices"])
            if n < 2:
                issues.append(_issue("blocking", "POLYLINE_VERTICES", "нужно минимум 2 вершины", op_id))
            if n > tpl.max_polyline_vertices:
                issues.append(_issue("blocking", "POLYLINE_TOO_MANY",
                                     f"{n} вершин больше лимита {tpl.max_polyline_vertices}", op_id))
        elif op_type in ("ADD_CLOUD", "MOVE_ENTITY", "SET_ATTRIBUTE"):
            handle = params.get("entity_handle") or params.get("handle")
            ent = _find_entity(msp, handle)
            if ent is None:
                issues.append(_issue("blocking", "ENTITY_NOT_FOUND",
                                     f"{op_type}: объект {handle} не найден", op_id))
            else:
                if ent.dxf.layer in protected:
                    issues.append(_issue("blocking", "PROTECTED_OBJECT",
                                         f"{op_type}: объект на защищённом слое {ent.dxf.layer}", op_id))
                if op_type == "SET_ATTRIBUTE":
                    if ent.dxftype() != "INSERT":
                        issues.append(_issue("blocking", "NOT_A_BLOCK",
                                             "атрибуты есть только у вставок блоков", op_id))
                    elif params["tag"] in tpl.human_only_fields():
                        issues.append(_issue("blocking", "HUMAN_ONLY_FIELD",
                                             f"поле «{params['tag']}» заполняет только человек", op_id))
        elif op_type == "UPDATE_TITLEBLOCK":
            bad = sorted(set(params["fields"]) - set(tpl.titleblock_fields or {}))
            human = sorted(set(params["fields"]) & tpl.human_only_fields())
            if bad:
                issues.append(_issue("blocking", "TITLEBLOCK_FIELD_UNKNOWN",
                                     f"поля вне шаблона: {', '.join(bad)}", op_id))
            if human:
                issues.append(_issue("blocking", "HUMAN_ONLY_FIELD",
                                     f"поля только для человека: {', '.join(human)}", op_id))
        elif op_type == "ADD_BLOCK":
            if tpl.allowed_blocks and params["name"] not in tpl.allowed_blocks:
                issues.append(_issue("blocking", "BLOCK_NOT_IN_TEMPLATE",
                                     f"блок «{params['name']}» не из шаблона {tpl.name}", op_id))

    return issues


# ── применение ───────────────────────────────────────────────────────────
def apply_operations(drawing, ops, tpl: CorpTemplate, mode: str = "dry_run",
                     out_path: str | Path | None = None, actor: str = "agent:cad",
                     approval: dict | None = None, audit=None,
                     require_confirmation: bool = True) -> dict:
    """Применить операции. dry_run ничего не пишет на диск.

    commit требует подтверждения человека (approval: {by, role, comment}),
    пишет новую версию файла и запись в журнал аудита с хэшами до/после.
    """
    issues = validate_operations(drawing.doc, ops, tpl)
    blocking = [i for i in issues if i["severity"] == "blocking"]

    if mode == "commit" and require_confirmation and not (approval or {}).get("by"):
        issue = _issue("blocking", "NO_HUMAN_CONFIRMATION",
                       "commit без подтверждения человека запрещён: укажите, кто подтвердил")
        issues.append(issue)
        blocking.append(issue)

    if blocking:
        return {"ok": False, "mode": mode, "issues": issues, "blocking": len(blocking),
                "applied": 0, "diff": None, "file": str(drawing.path)}

    results = []
    if mode == "dry_run":
        # работаем на копии через временный файл: исходный файл и объект не меняются
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "scratch.dxf"
            drawing.doc.saveas(str(tmp_path))
            work = ezdxf.readfile(str(tmp_path))
            for op in ops:
                results.append(_execute(work, op, tpl))
            diff = diff_drawings(drawing.doc, work)
        return {"ok": True, "mode": "dry_run", "issues": issues, "blocking": 0,
                "applied": len(results), "operations": results, "diff": diff,
                "file": str(drawing.path), "written": False, "file_unchanged": True}

    if mode != "commit":
        raise OperationError(f"неизвестный режим: {mode}")

    before_doc_hash = drawing.sha256
    # снимок «до» делаем ДО применения операций, иначе diff покажет пустоту
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "before.dxf"
        drawing.doc.saveas(str(snapshot))
        before_doc = ezdxf.readfile(str(snapshot))

    for op in ops:
        results.append(_execute(drawing.doc, op, tpl))
    target = Path(out_path) if out_path else _next_version_path(drawing.path)
    saved = drawing.save_as(target)
    diff = diff_drawings(before_doc, saved.doc)
    # контроль потери объектов при сохранении (требование ТЗ: количество объектов до/после)
    before_count = len(before_doc.modelspace())
    after_count = len(saved.msp)
    expected_added = sum(1 for r in results if r.get("handle"))
    lost = before_count - (after_count - expected_added)

    record = None
    if audit is not None:
        record = audit.append(
            actor, "agent", "cad.operations.commit", str(target),
            {"ops": len(results), "types": sorted({r["type"] for r in results}),
             "from_file": str(drawing.path), "from_hash": before_doc_hash,
             "to_hash": saved.sha256, "approval": approval, "diff": {
                 "added": diff["added"], "modified": diff["modified"], "removed": diff["removed"]}})

    return {"ok": True, "mode": "commit", "issues": issues, "blocking": 0,
            "applied": len(results), "operations": results, "diff": diff,
            "file": str(target), "sha256": saved.sha256, "written": True,
            "objects_before": before_count, "objects_after": after_count,
            "objects_lost": max(lost, 0), "audit_hash": (record or {}).get("hash")}


def _execute(doc, op, tpl):
    op_type = str(op["type"]).upper()
    handler = OPERATIONS[op_type]["handler"]
    try:
        res = handler(doc, op, tpl)
    except OperationError:
        raise
    except Exception as exc:
        raise OperationError(f"{op.get('op_id')}: {op_type} не выполнена: {exc}") from exc
    res.update({"op_id": op.get("op_id"), "op_type": op_type,
                "reason": op.get("reason"), "source": op.get("source")})
    return res


def _layer(doc, layer, tpl):
    name = layer or DEFAULT_MARKUP_LAYER
    if name in set(tpl.protected_layers):
        raise OperationError(f"слой «{name}» защищён шаблоном")
    if name not in doc.layers:
        doc.layers.add(name, color=30)
    return name


def _find_entity(msp, handle):
    if not handle:
        return None
    h = str(handle).lstrip("#").upper()
    for e in msp:
        if str(e.dxf.handle).upper() == h:
            return e
        for a in getattr(e, "attribs", []) or []:
            if str(a.dxf.handle).upper() == h:
                return a
    return None


def _require_entity(doc, handle):
    ent = _find_entity(doc.modelspace(), handle)
    if ent is None:
        raise OperationError(f"объект {handle} не найден в чертеже")
    return ent


def _find_titleblock(doc, block_name):
    for e in doc.modelspace():
        if e.dxftype() == "INSERT" and (
                (block_name and e.dxf.name == block_name)
                or {"ШИФР", "ОБЪЕКТ"} <= {a.dxf.tag for a in e.attribs}):
            return e
    return None


def _next_version_path(path: Path) -> Path:
    m = re.search(r"_v(\d+)$", path.stem)
    if m:
        n = int(m.group(1)) + 1
        return path.with_name(path.stem[:m.start()] + f"_v{n}" + path.suffix)
    return path.with_name(path.stem + "_v2" + path.suffix)


def history_entry(result: dict, drawing, tpl: CorpTemplate, approval: dict | None,
                  actor: str) -> dict:
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "mode": result["mode"],
        "file_in": str(drawing.path),
        "file_out": result.get("file"),
        "sha256_in": drawing.sha256,
        "sha256_out": result.get("sha256"),
        "template": tpl.name,
        "operations": [{"op_id": r.get("op_id"), "type": r.get("op_type"),
                        "handle": r.get("handle")} for r in result.get("operations", [])],
        "diff": None if not result.get("diff") else {
            k: result["diff"][k] for k in ("added", "modified", "removed")},
        "approval": approval,
        "actor": actor,
    }
