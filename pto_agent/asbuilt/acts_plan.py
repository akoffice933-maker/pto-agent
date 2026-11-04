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

from .requirements import (act_groups, classify_positions, get_work_type, load_matrix,
                           required_kinds)


def _match_group(position: dict, groups: list[dict]) -> dict | None:
    """Первая совпавшая группа забирает позицию: порядок групп в матрице = приоритет.

    Иначе «штробление стен под кабель-канал» уезжает в кабельные трассы (там два совпадения
    вместо одного), и акт на борозды получается пустым.
    """
    text = " ".join(str(position.get(k) or "") for k in ("name", "code")).lower().replace("ё", "е")
    for group in groups:
        if any(kw.lower() in text for kw in group.get("keywords") or []):
            return group
    return None


def plan_acts(positions: list[dict], *, work_types: list[str] | None = None,
              matrix: dict | None = None, act_prefix: str = "АОСР",
              object_name: str = "", source: str = "",
              act_date: str | None = None) -> dict:
    """Построить план актов: какой акт на какие работы, что в акты не входит."""
    matrix = matrix or load_matrix()
    classified = classify_positions(positions, matrix, dominant=work_types)

    excluded: list[dict] = []
    acts: list[dict] = []
    total = len(positions)

    # позиции нераспознанных видов работ, которые не являются скрытыми (демонтаж, вывоз)
    hidden_types = [w["work_type"] for w in classified["work_types"] if w["hidden_work"]]
    unclassified_hidden = [w for w in classified["work_types"] if not w["hidden_work"]]

    for slot in classified["work_types"]:
        if not slot["hidden_work"]:
            for pos in slot["positions"]:
                excluded.append({**pos, "reason": f"не скрытые работы: {slot['name']}",
                                 "work_type": slot["work_type"]})
            continue

        groups = act_groups(slot["work_type"], matrix)
        buckets: dict[str, dict] = {}
        for pos in slot["positions"]:
            group = _match_group(pos, groups) or {"id": "other", "name": "Прочие скрытые работы"}
            buckets.setdefault(group["id"], {"group": group, "positions": []})["positions"].append(pos)

        for group_id, payload in buckets.items():
            group = payload["group"]
            work_names = []
            for pos in payload["positions"]:
                name = str(pos.get("name") or "").strip()
                if name and name not in work_names:
                    work_names.append(name)
            acts.append({
                "index": len(acts) + 1,
                "group": group["id"],
                "group_name": group["name"],
                "work_type": slot["work_type"],
                "work_type_name": slot["name"],
                "work": "; ".join(work_names),
                "positions": payload["positions"],
                "positions_count": len(payload["positions"]),
                "required": required_kinds([slot["work_type"]], matrix)["required"],
                "protocols": required_kinds([slot["work_type"]], matrix)["protocols"],
                "inherited": [],
            })

    # нераспознанные позиции: в «прочие работы» вида работ документа, с пометкой
    if classified["unknown"] or classified["inherited"]:
        pool = classified["unknown"] + classified["inherited"]
        if work_types:
            groups = act_groups(work_types[0], matrix)
            buckets: dict[str, dict] = {}
            for pos in pool:
                group = _match_group(pos, groups) or {"id": "other",
                                                     "name": "Прочие скрытые работы"}
                buckets.setdefault(group["id"], {"group": group, "positions": []})["positions"].append(pos)
            for group_id, payload in buckets.items():
                group = payload["group"]
                existing = next((a for a in acts if a["work_type"] == work_types[0]
                                 and a["group"] == group_id), None)
                if existing:
                    existing["positions"] += payload["positions"]
                    existing["positions_count"] = len(existing["positions"])
                    existing["inherited"] += [p.get("name") for p in payload["positions"]]
                    existing["work"] = existing["work"] or "; ".join(
                        str(p.get("name")) for p in payload["positions"])
                else:
                    acts.append({
                        "index": len(acts) + 1,
                        "group": group_id,
                        "group_name": group["name"],
                        "work_type": work_types[0],
                        "work_type_name": (get_work_type(work_types[0], matrix) or {}).get("name"),
                        "work": "; ".join(str(p.get("name")) for p in payload["positions"]),
                        "positions": payload["positions"],
                        "positions_count": len(payload["positions"]),
                        "required": required_kinds(work_types, matrix)["required"],
                        "protocols": required_kinds(work_types, matrix)["protocols"],
                        "inherited": [p.get("name") for p in payload["positions"]],
                    })
        else:
            for pos in pool:
                excluded.append({**pos, "reason": "вид работ не распознан: указать work_types "
                                                  "или переклассифицировать позицию"})

    for slot in unclassified_hidden:
        for pos in slot["positions"]:
            excluded.append({**pos, "reason": f"не скрытые работы: {slot['name']}",
                             "work_type": slot["work_type"]})

    act_date = act_date or date.today().isoformat()
    for act in acts:
        act["code"] = f"{act_prefix}-{act['index']}"
        act["act_date"] = act_date
        act["object_name"] = object_name
        act["attachments_template"] = [
            {"name": p, "code": "", "date": "", "sheets": None} for p in act["protocols"]]
        act["attachments_template"].append(
            {"name": "Исполнительная схема на скрытые работы", "code": "", "date": "",
             "sheets": None})

    return {
        "object_name": object_name,
        "source": source,
        "act_date": act_date,
        "positions_total": total,
        "positions_in_acts": sum(a["positions_count"] for a in acts),
        "positions_excluded": len(excluded),
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
            "profile": matrix.get("profile"),
        },
        "warning": ("позиции вне актов: " + ", ".join(sorted({e["reason"] for e in excluded}))
                    if excluded else None),
    }


def plan_to_specs(plan: dict) -> list[dict]:
    """Превратить план в спецификации АОСР (по одной на акт), готовые для `asbuilt aosr`."""
    specs = []
    for act in plan["acts"]:
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
            "materials": [],
            "norms": [],
            "attachments": act["attachments_template"],
            "next_works": "",
            "source": {"positions_from": plan.get("source"),
                       "act_group": act["group_name"],
                       "positions": act["positions_count"],
                       "inherited": act["inherited"]},
            "needs_input": ["representatives", "project_docs", "materials", "norms",
                            "start_date", "end_date"],
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
