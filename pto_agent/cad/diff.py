# -*- coding: utf-8 -*-
"""Сравнение двух состояний чертежа: что реально изменилось (для подтверждения человеком)."""
import hashlib


def _sig(e) -> str:
    t = e.dxftype()
    parts = [t, str(e.dxf.layer)]
    try:
        if t == "TEXT":
            parts += [e.dxf.text, _pts([e.dxf.insert]), str(round(float(e.dxf.height), 3)),
                      str(e.dxf.get("style", ""))]
        elif t == "MTEXT":
            parts += [e.text, _pts([e.dxf.insert]), str(round(float(e.dxf.char_height), 3))]
        elif t == "ATTRIB":
            parts += [e.dxf.tag, e.dxf.text, _pts([e.dxf.insert]),
                      str(round(float(e.dxf.height), 3))]
        elif t == "LWPOLYLINE":
            parts += [_pts([tuple(p) for p in e.get_points("xy")]), str(bool(e.closed))]
        elif t == "LINE":
            parts += [_pts([e.dxf.start, e.dxf.end])]
        elif t == "CIRCLE":
            parts += [_pts([e.dxf.center]), str(round(float(e.dxf.radius), 3))]
        elif t == "INSERT":
            attrs = {a.dxf.tag: a.dxf.text for a in getattr(e, "attribs", []) or []}
            parts += [e.dxf.name, _pts([e.dxf.insert]), str(sorted(attrs.items()))]
        else:
            ins = e.dxf.get("insert", None) if e.dxf.is_supported("insert") else None
            parts += [_pts([ins]) if ins is not None else ""]
    except Exception:  # pragma: no cover - защита от экзотических объектов
        parts += [str(e.dxfattribs().get("handle"))]
    parts.append(str(e.dxf.get("color", "BYLAYER")))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _pts(points) -> str:
    out = []
    for p in points:
        try:
            out.append(f"{float(p[0]):.3f},{float(p[1]):.3f}")
        except Exception:
            out.append(str(p))
    return ";".join(out)


def _index(doc) -> dict:
    idx = {}
    for e in doc.modelspace():
        idx[str(e.dxf.handle)] = (_sig(e), e.dxftype(), e.dxf.layer)
        for a in getattr(e, "attribs", []) or []:
            idx[str(a.dxf.handle)] = (_sig(a), "ATTRIB", a.dxf.layer)
    return idx


def diff_drawings(before, after) -> dict:
    """before/after — Drawing или ezdxf-документ."""
    doc_a = getattr(before, "doc", before)
    doc_b = getattr(after, "doc", after)
    a, b = _index(doc_a), _index(doc_b)

    added = [h for h in b if h not in a]
    removed = [h for h in a if h not in b]
    modified = [h for h in b if h in a and a[h][0] != b[h][0]]

    def _layers(handles, src):
        out = {}
        for h in handles:
            layer = src[h][2]
            out[layer] = out.get(layer, 0) + 1
        return out

    items = []
    for h in added[:200]:
        items.append({"change": "added", "handle": h, "type": b[h][1], "layer": b[h][2]})
    for h in modified[:200]:
        items.append({"change": "modified", "handle": h, "type": b[h][1], "layer": b[h][2]})
    for h in removed[:200]:
        items.append({"change": "removed", "handle": h, "type": a[h][1], "layer": a[h][2]})

    return {
        "added": len(added),
        "removed": len(removed),
        "modified": len(modified),
        "total_changes": len(added) + len(removed) + len(modified),
        "added_by_layer": _layers(added, b),
        "modified_by_layer": _layers(modified, b),
        "removed_by_layer": _layers(removed, a),
        "items": items,
    }
