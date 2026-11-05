# -*- coding: utf-8 -*-
"""Разбивка сметы на отдельные акты по скрытым работам.

АОСР — акт на **конкретные скрытые работы**: борозды, проходки, закладные, прокладка кабеля
в гофре. Один акт на всю смету — это документ, который не примут: в нём смешаны демонтаж,
вывоз мусора и работы, которые скрытыми не являются.

Поэтому здесь смета режется на акты по группам скрытых работ из матрицы
(`act_groups` у вида работ). Позиции, которые скрытыми работами не являются (демонтаж,
вывоз мусора), в акты не попадают, но и не теряются: они перечислены в `excluded` с причиной.
Нераспознанные позиции, если вид работ задан явно, идут в «прочие работы» с пометкой
`inherited` — это видно в плане и в спецификации, а не выдаётся за распознанное.
"""
from __future__ import annotations

from datetime import date

from .requirements import (_kw_re, act_groups, classify_positions, classify_work, get_work_type,
                           load_matrix, required_kinds)


def _match_group(position: dict, groups: list[dict]) -> dict | None:
    """Первая совпавшая группа забирает позицию: порядок групп в матрице = приоритет.

    Иначе «штробление стен под кабель-канал» уезжает в кабельные трассы (там два совпадения
    вместо одного), и акт на борозды получается пустым.
    """
    text = " ".join(str(position.get(k) or "") for k in ("name", "code")).lower().replace("ё", "е")
    for group in groups:
        # граница начала слова: иначе «защиты проводов» попадает в щиты («щит»), а «прокладка»
        # в кладку — те же грабли, что были в классификаторе
        if any(_kw_re(kw).search(text) for kw in group.get("keywords") or []):
            return group
    return None


def _materials(positions: list[dict]) -> list[dict]:
    """Материалы сметы: одна строка на код ресурса, объёмы складываются."""
    acc: dict[str, dict] = {}
    for pos in positions:
        if str(pos.get("kind") or "work") != "material":
            continue
        key = str(pos.get("code") or pos.get("name"))
        slot = acc.setdefault(key, {"code": pos.get("code"), "name": pos.get("name"),
                                    "unit": pos.get("unit"), "qty": 0.0, "lines": 0,
                                    "documents": None, "needs_input": True})
        try:
            slot["qty"] = round(slot["qty"] + float(pos.get("qty") or 0), 4)
        except (TypeError, ValueError):
            pass
        slot["lines"] += 1
    return list(acc.values())


def _material_group(material: dict, groups: list[dict]) -> str | None:
    """Материал идёт в первый подходящий акт: автомат — в щиты, а не ещё и в освещение."""
    text = str(material.get("name") or "").lower().replace("ё", "е")
    for group in groups:
        if any(_kw_re(kw).search(text) for kw in group.get("material_keywords") or []):
            return group["id"]
    return None


def _group_protocols(group: dict, work_type: str, matrix: dict) -> list[str]:
    """Протоколы акта этой группы: акт на борозды не прикладывает испытания изоляции."""
    all_protocols = required_kinds([work_type], matrix).get("protocols") or []
    keys = group.get("protocols")
    if keys is None:
        return all_protocols
    out = []
    for protocol in all_protocols:
        text = str(protocol).lower()
        if any(str(k).lower() in text for k in keys):
            out.append(protocol)
    return out


def _group_required(group: dict, work_type: str, matrix: dict) -> list[str]:
    """Минимальный комплект документов акта: у группы он свой, иначе — по виду работ."""
    required = required_kinds([work_type], matrix).get("required") or []
    override = group.get("required")
    if override is None:
        return required
    return [k for k in required if k in override]


def _key(pos: dict) -> tuple:
    """Ключ позиции: позиции из classify_positions — копии, сверять по id нельзя."""
    return (str(pos.get("code") or ""), str(pos.get("name") or ""),
            str(pos.get("unit") or ""), str(pos.get("qty")))


def plan_acts(positions: list[dict], *, work_types: list[str] | None = None,
              matrix: dict | None = None, act_prefix: str = "АОСР",
              object_name: str = "", source: str = "",
              act_date: str | None = None) -> dict:
    """Построить план актов: какой акт на какие работы, что в акты не входит.

    Материалы отдельными строками сметы (код ресурса ФГИС ЦС) работами не считаются:
    они не попадают в акты и не искажают доли видов работ, но идут в перечень материалов
    того акта, к которому относятся по `material_keywords` группы.
    """
    matrix = matrix or load_matrix()
    works = [p for p in positions if str(p.get("kind") or "work") == "work"]
    materials = _materials(positions)
    classified = classify_positions(works, matrix, dominant=work_types)

    acts: list[dict] = []
    in_acts: set[tuple] = set()

    for slot in classified["work_types"]:
        if not slot["hidden_work"]:
            continue
        groups = act_groups(slot["work_type"], matrix)
        buckets: dict[str, list[dict]] = {}
        for pos in slot["positions"]:
            group = _match_group(pos, groups) or {"id": "other", "name": "Прочие скрытые работы"}
            buckets.setdefault(group["id"], [])
            buckets[group["id"]].append(pos)
        # порядок актов = порядок групп в матрице (он же приоритет), «прочие» — в конце
        order = {g["id"]: i for i, g in enumerate(groups)}
        for group_id in sorted(buckets, key=lambda g: (order.get(g, len(order)), g)):
            payload = buckets[group_id]
            group = next((g for g in groups if g["id"] == group_id),
                         {"id": "other", "name": "Прочие скрытые работы"})
            work_names: list[str] = []
            for pos in payload:
                name = str(pos.get("name") or "").strip()
                if name and name not in work_names:
                    work_names.append(name)
            act_materials = [m for m in materials if m.get("act_group") == group["id"]]
            acts.append({
                "group": group["id"],
                "group_name": group["name"],
                "work_type": slot["work_type"],
                "work_type_name": slot["name"],
                "work": "; ".join(work_names),
                "positions": payload,
                "positions_count": len(payload),
                "materials": act_materials,
                "required": _group_required(group, slot["work_type"], matrix),
                "protocols": _group_protocols(group, slot["work_type"], matrix),
                "inherited": [],
            })
            in_acts.update(_key(p) for p in payload)

    # нераспознанные позиции: в «прочие работы» вида работ, если он задан явно
    pool = classified["unknown"] + classified["inherited"]
    if pool and work_types:
        groups = act_groups(work_types[0], matrix)
        buckets: dict[str, list[dict]] = {}
        for pos in pool:
            group = _match_group(pos, groups) or {"id": "other", "name": "Прочие скрытые работы"}
            buckets.setdefault(group["id"], [])
            buckets[group["id"]].append(pos)
        order = {g["id"]: i for i, g in enumerate(groups)}
        for group_id in sorted(buckets, key=lambda g: (order.get(g, len(order)), g)):
            payload = buckets[group_id]
            group = next((g for g in groups if g["id"] == group_id),
                         {"id": "other", "name": "Прочие скрытые работы"})
            existing = next((a for a in acts if a["work_type"] == work_types[0]
                             and a["group"] == group_id), None)
            if existing:
                existing["positions"] += payload
                existing["positions_count"] = len(existing["positions"])
                existing["inherited"] += [p.get("name") for p in payload]
                existing["work"] = existing["work"] or "; ".join(
                    str(p.get("name")) for p in payload)
                in_acts.update(_key(p) for p in payload)
            else:
                acts.append({
                    "group": group_id,
                    "group_name": group["name"],
                    "work_type": work_types[0],
                    "work_type_name": (get_work_type(work_types[0], matrix) or {}).get("name"),
                    "work": "; ".join(str(p.get("name")) for p in payload),
                    "positions": payload,
                    "positions_count": len(payload),
                    "materials": [m for m in materials if m.get("act_group") == group["id"]],
                    "required": required_kinds(work_types, matrix)["required"],
                    "protocols": required_kinds(work_types, matrix)["protocols"],
                    "inherited": [p.get("name") for p in payload],
                })
                in_acts.update(_key(p) for p in payload)

    # всё, что не попало ни в один акт, — явно и с причиной
    excluded: list[dict] = []
    for pos in works:
        if _key(pos) in in_acts:
            continue
        hits = classify_work(" ".join(str(pos.get(k) or "") for k in ("name", "work")), matrix)
        if not hits:
            reason = ("вид работ не распознан: указать work_types или переклассифицировать "
                      "позицию")
        else:
            best = hits[0]
            if not best["hidden_work"]:
                reason = f"не скрытые работы: {best['name']}"
            else:
                reason = (f"вид работ «{best['name']}» не набрал долю в документе "
                          f"(меньше {(matrix.get('classifier') or {}).get('min_share', 0.15)})")
        excluded.append({**pos, "reason": reason,
                         "work_type": hits[0]["work_type"] if hits else None})

    # материал попадает в перечень материалов того акта, к которому относится;
    # не разложился — остаётся в общем перечне плана и никуда не теряется
    for material in materials:
        assigned = None
        for act in acts:
            groups = act_groups(act["work_type"], matrix)
            groups = [g for g in groups if g["id"] == act["group"]]
            if groups and _material_group(material, groups) == act["group"]:
                assigned = act["group"]
                break
        material["act_group"] = assigned
    for act in acts:
        act["materials"] = [m for m in materials if m.get("act_group") == act["group"]]

    act_date = act_date or date.today().isoformat()
    for index, act in enumerate(acts, 1):
        act["index"] = index
        act["code"] = f"{act_prefix}-{index}"
        act["act_date"] = act_date
        act["object_name"] = object_name
        act["work_type"] = act["work_type"]
        act["attachments_template"] = [
            {"name": p, "code": "", "date": "", "sheets": None} for p in act["protocols"]]
        act["attachments_template"].append(
            {"name": "Исполнительная схема на скрытые работы", "code": "", "date": "",
             "sheets": None})

    return {
        "object_name": object_name,
        "source": source,
        "act_date": act_date,
        "positions_total": len(works),
        "positions_in_acts": sum(a["positions_count"] for a in acts),
        "positions_excluded": len(excluded),
        "materials_total": len(materials),
        "materials": materials,
        "acts": acts,
        "excluded": excluded,
        "classification": {
            "work_types": [{"work_type": w["work_type"], "name": w["name"],
                            "positions": len(w["positions"]), "share": w.get("share")}
                           for w in classified["work_types"]],
            "candidates": [{"work_type": w["work_type"], "name": w["name"],
                            "positions": len(w["positions"]), "share": w.get("share")}
                           for w in classified["candidates"]],
            "unknown": classified["unknown"],
            "inherited": classified["inherited"],
            "merged": classified.get("merged") or [],
            "profile": matrix.get("profile"),
        },
        "warning": ("позиции вне актов: " + ", ".join(sorted({e["reason"] for e in excluded}))
                    if excluded else None),
    }


MATRIX_NORMS_NOTE = ("матрица видов работ (профиль, не нормативная база): ссылки требуют "
                     "сверки — asbuilt verify-norms")


def plan_to_specs(plan: dict, *, matrix: dict | None = None) -> list[dict]:
    """Превратить план в спецификации АОСР (по одной на акт), готовые для `asbuilt aosr`."""
    matrix = matrix or load_matrix()
    specs = []
    for act in plan["acts"]:
        kinds = required_kinds([act["work_type"]], matrix)
        norms = [{**n, "source": "matrix"} for n in kinds.get("norms") or []]
        specs.append({
            "act_number": act["code"].replace("АОСР-", ""),
            "act_date": act["act_date"],
            "object_name": act["object_name"],
            "work": act["work"],
            "location": {},
            "start_date": "",
            "end_date": "",
            "work_types": [act["work_type"]],
            "representatives": {},
            "project_docs": [],
            "materials": [{"name": m["name"], "qty": m["qty"], "unit": m["unit"],
                           "code": m.get("code"), "certificate": None,
                           "certificate_date": None, "needs_input": True}
                          for m in act.get("materials") or []],
            "norms": norms,
            "norms_source": MATRIX_NORMS_NOTE,
            "attachments": act["attachments_template"],
            "required_kinds": act["required"],
            "act_group": {"id": act["group"], "name": act["group_name"]},
            "next_works": "",
            "source": {"positions_from": plan.get("source"),
                       "act_group": act["group_name"],
                       "positions": act["positions_count"],
                       "positions_list": [{"code": p.get("code"), "name": p.get("name"),
                                           "qty": p.get("qty"), "unit": p.get("unit")}
                                          for p in act["positions"]],
                       "materials_from_smeta": len(act.get("materials") or []),
                       "inherited": act["inherited"]},
            "needs_input": [f for f in ["representatives", "project_docs",
                                        "materials_documents", "norms", "start_date",
                                        "end_date"]
                            if f != "materials_documents" or act.get("materials")],
        })
    return specs


def plan_to_markdown(plan: dict) -> str:
    cls = plan["classification"]
    lines = [f"# План актов по скрытым работам: {plan.get('object_name') or '—'}", "",
             f"- источник: {plan.get('source') or '—'}",
             f"- позиций всего: {plan['positions_total']} · в актах: {plan['positions_in_acts']} "
             f"· вне актов: {plan['positions_excluded']}",
             f"- актов: {len(plan['acts'])}"]
    if cls["work_types"]:
        lines.append("- виды работ: " + ", ".join(
            f"{w['work_type']} ({w['positions']}, доля {w['share']})" for w in cls["work_types"]))
    if cls["candidates"]:
        lines.append("- не взяты в комплект (малая доля, протоколы не подтягиваются): " +
                     ", ".join(f"{w['work_type']} ({w['positions']})" for w in cls["candidates"]))
    if cls["inherited"]:
        lines.append(f"- отнесено к виду работ документа без ключей в тексте: "
                     f"{len(cls['inherited'])} позиций")
    if cls["unknown"]:
        lines.append(f"- не распознано: {len(cls['unknown'])} позиций — указать work_types вручную")
    lines += ["", "| Акт | Группа скрытых работ | Позиций | Документов по матрице |",
              "|---|---|---|---|"]
    for act in plan["acts"]:
        lines.append(f"| {act['code']} | {act['group_name']} | {act['positions_count']} | "
                     f"{len(act['required'])} + протоколов {len(act['protocols'])} |")
    if plan["excluded"]:
        lines += ["", "**Не входит в акты:**"]
        for item in plan["excluded"]:
            lines.append(f"- {item.get('name')} — {item['reason']}")
    return "\n".join(lines) + "\n"
