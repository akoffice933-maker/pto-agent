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


def _prep(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


# Шифр расценки («ФЕРм 21-02-012-01») — это не текст работ: «ФЕРм» не должен читаться
# как «ферма». Базу расценки вырезаем до сопоставления ключей.
PRICE_BASE_RE = re.compile(r"(?<![а-яa-z])(ферм|терм|фер|тер|гэсн|фсбц|тсн)(?![а-яa-z])")


def _kw_re(keyword: str):
    """Ключ ищется с начала слова: «кладк» не должен срабатывать внутри «прокладка»."""
    return re.compile(r"(?<![а-яa-z0-9])" + re.escape(_prep(keyword)))


def strip_qualifiers(text: str, wt: dict) -> str:
    """Вырезать из текста конструкции, которые описывают место, а не вид работ.

    «Заделка отверстий в бетонных и кирпичных стенах» — это электромонтаж, а не бетон:
    слова про материалы описывают место работ. Такие конструкции перечислены в матрице
    (`qualifiers`) у того вида работ, к которому они относятся.
    """
    out = PRICE_BASE_RE.sub(" ", _prep(text))
    for pattern in wt.get("qualifiers") or []:
        out = re.sub(pattern, " ", out)
    return out


def classify_work(text: str, matrix: dict | None = None) -> list[dict]:
    """Определить виды работ по тексту позиции (наименование, шифр расценки).

    Вид работ определяется **сильными** ключами: «кабель», «штробление», «щит», «УЗО».
    Слабые ключи (материалы и контекст: бетон, кирпич, стена) сами по себе вид работ не
    дают — иначе «заделка в бетонных стенах» тянет протоколы испытаний бетона.
    Нераспознанное возвращает пустой список: пусть лучше позиция попадёт в unknown, чем
    получит чужой комплект документов.
    """
    matrix = matrix or load_matrix()
    strong_weight = (matrix.get("classifier") or {}).get("strong_weight", 2)
    weak_weight = (matrix.get("classifier") or {}).get("weak_weight", 1)
    hits = []
    for wt in work_types(matrix):
        text_clean = strip_qualifiers(text, wt)
        strong = [kw for kw in wt.get("strong") or [] if _kw_re(kw).search(text_clean)]
        weak = [kw for kw in wt.get("keywords") or [] if _kw_re(kw).search(text_clean)]
        if not strong:
            continue                        # без сильного ключа вид работ не присваивается
        hits.append({"work_type": wt["id"], "name": wt["name"],
                     "matched": strong, "weak": weak,
                     "hidden_work": wt.get("hidden_work", True),
                     "score": strong_weight * len(strong) + weak_weight * len(weak)})
    # «Демонтаж светильников» — это демонтаж, а не монтаж освещения: работы по снятию
    # не оформляются АОСР, и они не должны попадать в акт на скрытые работы.
    hits.sort(key=lambda h: (h["work_type"] != "demolition",
                             h["work_type"] != "waste_removal", -h["score"]))
    return hits


def classify_positions(positions: list[dict], matrix: dict | None = None,
                       dominant: list[str] | None = None) -> dict:
    """Классифицировать позиции сметы/КС-2 и определить виды работ документа.

    Два шага, потому что смета — это документ, а не набор независимых строк:
      1) по каждой позиции — сильные ключи;
      2) по документу — доля сильного веса. Вид работ, набравший меньше `min_share`,
         в комплект не идёт (он в `candidates`): одна строка «кронштейны под вентиляцию»
         не должна приносить протоколы балансировки вентиляции в электромонтажный акт.
    Нераспознанные позиции вид работ не наследуют молча: они в `unknown`, а если вид работ
    документа задан явно (`dominant`), помечаются `inherited` — «отнесено к виду работ
    документа по решению инженера, ключей в тексте нет».
    """
    matrix = matrix or load_matrix()
    min_share = (matrix.get("classifier") or {}).get("min_share", 0.15)
    materials_skipped = sum(1 for p in positions if str(p.get("kind") or "work") != "work")
    positions = [p for p in positions if str(p.get("kind") or "work") == "work"]

    by_type: dict[str, dict] = {}
    unknown: list[dict] = []
    total_score = 0
    for pos in positions:
        if str(pos.get("kind") or "work") != "work":
            # материал/оборудование отдельной строкой сметы — не работа и не «неизвестная
            # позиция»: он идёт в перечень материалов, а не в акт
            continue
        text = " ".join(str(pos.get(k) or "") for k in ("name", "work"))
        hits = classify_work(text, matrix)
        if not hits:
            unknown.append({"name": pos.get("name"), "code": pos.get("code"),
                            "qty": pos.get("qty"), "unit": pos.get("unit")})
            continue
        best = hits[0]
        total_score += best["score"]
        slot = by_type.setdefault(best["work_type"], {
            "work_type": best["work_type"], "name": best["name"], "positions": [],
            "score": 0, "hidden_work": best["hidden_work"]})
        slot["positions"].append({"code": pos.get("code"), "name": pos.get("name"),
                                  "qty": pos.get("qty"), "unit": pos.get("unit")})
        slot["score"] += best["score"]

    ranked = sorted(by_type.values(), key=lambda x: -x["score"])
    for slot in ranked:
        slot["share"] = round(slot["score"] / total_score, 3) if total_score else 0.0

    if dominant:
        chosen = [w for w in ranked if w["work_type"] in dominant]
        candidates = [w for w in ranked if w["work_type"] not in dominant]
    else:
        chosen = [w for w in ranked if w["hidden_work"] and w["share"] >= min_share]
        candidates = [w for w in ranked if w not in chosen]

    # дочерние виды работ (например, заземление внутри электромонтажа) вливаются в родителя,
    # если родитель выбран: иначе их позиции выпали бы из актов
    chosen_ids = {w["work_type"] for w in chosen}
    merged = []
    for slot in list(ranked):
        wt = get_work_type(slot["work_type"], matrix) or {}
        parent = wt.get("parent")
        if parent and parent in chosen_ids and slot["work_type"] not in chosen_ids:
            for target in chosen:
                if target["work_type"] == parent:
                    target["positions"] += slot["positions"]
                    target["score"] += slot["score"]
                    merged.append({"work_type": slot["work_type"], "into": parent,
                                   "positions": len(slot["positions"])})
            if slot in candidates:
                candidates = [c for c in candidates if c["work_type"] != slot["work_type"]]

    inherited = []
    if dominant:
        inherited = [dict(u, inherited_from=dominant[0]) for u in unknown]
        unknown = []

    return {
        "work_types": chosen,
        "candidates": candidates,
        "unknown": unknown,
        "inherited": inherited,
        "merged": merged,
        "classified": len(positions) - len(unknown) - len(inherited),
        "total": len(positions),
        "materials_skipped": materials_skipped,
        "min_share": min_share,
        "hidden_work_types": [w["work_type"] for w in chosen if w["hidden_work"]],
        "not_hidden_work_types": [w["work_type"] for w in ranked if not w["hidden_work"]],
    }


def act_groups(work_type_id: str, matrix: dict | None = None) -> list[dict]:
    """Группы скрытых работ внутри вида работ: по ним режутся отдельные акты."""
    matrix = matrix or load_matrix()
    wt = get_work_type(work_type_id, matrix) or {}
    groups = wt.get("act_groups") or []
    if not groups:
        groups = [{"id": "main", "name": f"Основные работы: {wt.get('name', work_type_id)}",
                   "keywords": []}]
    return groups


def hidden_work(work_type_id: str, matrix: dict | None = None) -> bool:
    matrix = matrix or load_matrix()
    wt = get_work_type(work_type_id, matrix) or {}
    return bool(wt.get("hidden_work", True))


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
