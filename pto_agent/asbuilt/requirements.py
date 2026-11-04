# -*- coding: utf-8 -*-
"""Матрица требований комплектности по видам работ.

Профиль заказчика почти всегда отличается от типового: у электромонтажа свои протоколы
(изоляция, петля «фаза-нуль», УЗО), у кровли — испытание на водонепроницаемость, у сетей —
акт освидетельствования участков сетей. Держать это в коде нельзя: матрица лежит в
`data/reference/work_type_requirements.json`, и заказчик правит её под себя.

Ничего не выдумываем: если вид работ не опознан, возвращаем `unknown` и явно просим
указать `work_type` руками — «угаданная» матрица даёт ложное «комплект полный».
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_MATRIX = Path(__file__).resolve().parents[2] / "data" / "reference" / "work_type_requirements.json"

# виды документов, которые в матрице считаются «проверяемыми выпуском» (есть файл/шифр)
_CACHE: dict[str, dict] = {}


def load_matrix(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_MATRIX
    key = str(path)
    if key not in _CACHE:
        raw = json.loads(path.read_text(encoding="utf-8"))
        kinds = raw.get("document_kinds") or {}
        for wt in raw.get("work_types") or []:
            unknown = [k for k in wt.get("required", []) if k not in kinds]
            if unknown:
                raise ValueError(f"{wt['id']}: неизвестные виды документов {unknown}")
            wt.setdefault("keywords", [])
        _CACHE[key] = raw
    return _CACHE[key]


def work_types(matrix: dict | None = None) -> list[dict]:
    return list((matrix or load_matrix()).get("work_types") or [])


def get_work_type(work_type_id: str, matrix: dict | None = None) -> dict | None:
    return next((w for w in work_types(matrix) if w["id"] == work_type_id), None)


def classify_work(text: str, matrix: dict | None = None) -> list[dict]:
    """Определить виды работ по тексту (наименование работ, позиция КС-2/сметы).

    Возвращает список совпадений с числом ключевых слов — без «победителя по умолчанию»:
    пустой список означает «не опознано», и это честнее любой догадки.
    """
    text = (text or "").lower().replace("ё", "е")
    hits = []
    for wt in work_types(matrix):
        matched = [kw for kw in wt["keywords"]
                   if re.search(re.escape(kw.lower().replace("ё", "е")), text)]
        if matched:
            hits.append({"work_type": wt["id"], "name": wt["name"],
                         "matched": matched, "score": len(matched)})
    return sorted(hits, key=lambda h: -h["score"])


def classify_positions(positions: list[dict], matrix: dict | None = None) -> dict:
    """Классифицировать позиции сметы/КС-2 → виды работ с позициями и весами."""
    matrix = matrix or load_matrix()
    by_type: dict[str, dict] = {}
    unknown = []
    for pos in positions:
        text = " ".join(str(pos.get(k) or "") for k in ("name", "code", "work"))
        hits = classify_work(text, matrix)
        if not hits:
            unknown.append({"name": pos.get("name"), "code": pos.get("code")})
            continue
        best = hits[0]
        slot = by_type.setdefault(best["work_type"], {"work_type": best["work_type"],
                                                      "name": best["name"], "positions": [],
                                                      "score": 0})
        slot["positions"].append({"code": pos.get("code"), "name": pos.get("name"),
                                  "qty": pos.get("qty"), "unit": pos.get("unit")})
        slot["score"] += best["score"]
    return {"work_types": sorted(by_type.values(), key=lambda x: -x["score"]),
            "unknown": unknown,
            "classified": len(positions) - len(unknown),
            "total": len(positions)}


def work_type_requirements(work_type_id: str, matrix: dict | None = None) -> dict:
    """Матрица требований по одному виду работ: документы, протоколы, нормы с пунктами."""
    matrix = matrix or load_matrix()
    wt = get_work_type(work_type_id, matrix)
    if wt is None:
        return {"ok": False, "error": f"неизвестный вид работ: {work_type_id}",
                "available": [w["id"] for w in work_types(matrix)],
                "profile": matrix.get("profile")}
    return {
        "ok": True,
        "work_type": wt["id"],
        "name": wt["name"],
        "profile": matrix.get("profile"),
        "required": wt["required"],
        "required_descriptions": [{"kind": k, "what": matrix["document_kinds"][k]}
                                  for k in wt["required"]],
        "protocols": wt.get("protocols") or [],
        "protocols_conditional": wt.get("protocols_conditional") or [],
        "act_tests": wt.get("act_tests") or [],
        "extra_docs": wt.get("extra_docs") or [],
        "norms": wt.get("norms") or [],
        "disclaimer": matrix.get("disclaimer"),
    }


def required_kinds(work_type_ids: list[str], matrix: dict | None = None) -> dict:
    """Объединение требований по нескольким видам работ + профиль с версией."""
    matrix = matrix or load_matrix()
    union: list[str] = []
    per_type: dict[str, list[str]] = {}
    unknown = []
    for wid in work_type_ids:
        wt = get_work_type(wid, matrix)
        if wt is None:
            unknown.append(wid)
            continue
        per_type[wid] = list(wt["required"])
        for kind in wt["required"]:
            if kind not in union:
                union.append(kind)
    protocols: list[str] = []
    protocols_conditional: list[str] = []
    norms: list[dict] = []
    for wid in per_type:
        wt = get_work_type(wid, matrix) or {}
        protocols += [p for p in (wt.get("protocols") or []) if p not in protocols]
        protocols_conditional += [p for p in (wt.get("protocols_conditional") or [])
                                  if p not in protocols_conditional]
        norms += [n for n in (wt.get("norms") or []) if n not in norms]
    return {
        "profile": matrix.get("profile"),
        "work_types": list(per_type),
        "required": union,
        "per_type": per_type,
        "protocols": protocols,
        "protocols_conditional": protocols_conditional,
        "norms": norms,
        "document_kinds": {k: matrix["document_kinds"][k] for k in union},
        "unknown": unknown,
    }


def matrix_summary(matrix: dict | None = None) -> str:
    """Короткая таблица «вид работ → обязательные документы» для отчёта."""
    matrix = matrix or load_matrix()
    lines = ["| Вид работ | Обязательные документы | Протоколы |", "|---|---|---|"]
    for wt in work_types(matrix):
        docs = ", ".join(wt["required"])
        prot = "; ".join(wt.get("protocols") or []) or "—"
        lines.append(f"| {wt['name']} (`{wt['id']}`) | {docs} | {prot} |")
    return "\n".join(lines) + "\n"
