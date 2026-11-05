# -*- coding: utf-8 -*-
"""Заготовка спецификации АОСР из сметы или из КС-2 — без ручного переноса позиций.

Раньше спецификацию приходилось писать руками: инженер открывал смету, переписывал работы,
искал нормы. Теперь агент классифицирует позиции сметы/акта по матрице видов работ и
собирает черновик спецификации: работы, виды работ, нормы **с пунктами**, шаблон приложений.

Что заполняется автоматически и что нет — видно построчно (`fields[].source`):
  * `смета` — работы и объёмы из позиций;
  * `матрица` — виды работ и нормативные документы с пунктами;
  * `нет` — всё, что относится к объекту и людям (представители, даты, сертификаты).
Пустые поля не выдумываются: они попадают в `needs_input`, и АОСР по такой спецификации
выйдет с блокерами — это нормальный рабочий цикл, а не ошибка.
"""
from __future__ import annotations

from pathlib import Path

from ..parsers import read_estimate
from .requirements import classify_positions, load_matrix, required_kinds


def _positions_from_summary(summary: dict) -> list[dict]:
    """Позиции из нашего summary.json (сверка КС-2) — те же поля, что у сметы."""
    out = []
    for pos in summary.get("positions") or summary.get("lines") or []:
        out.append({"code": pos.get("code") or pos.get("price_code"),
                    "name": pos.get("name") or pos.get("work"),
                    "qty": pos.get("qty") if pos.get("qty") is not None else pos.get("claim_qty"),
                    "unit": pos.get("unit")})
    return out


AROS_SUFFIXES = {".md", ".markdown", ".html", ".htm"}


def load_positions(*, smeta: str | Path | None = None,
                   summary_path: str | Path | None = None) -> tuple[list[dict], str]:
    """Позиции из сметы (XLSX или выгрузка АРОС) либо summary.json.

    Смета АРОС читается как есть (html/markdown-выгрузка), без ручного перевода в xlsx.
    У позиций есть `kind`: `work` — работа (обоснование — сметная норма), `material` —
    материал/оборудование отдельной строкой (код ресурса ФГИС ЦС). Материал в акт как
    работу не попадает, но идёт в перечень материалов спецификации.
    """
    if smeta:
        path = Path(smeta)
        if path.suffix.lower() in AROS_SUFFIXES:
            from .aros import aros_positions, read_aros
            doc = read_aros(path)
            return (aros_positions(doc),
                    f"смета {path.name} (АРОС: {len(doc['works'])} работ, "
                    f"{len(doc['materials'])} отдельных материалов, "
                    f"{len(doc.get('nested_resources', []))} вложенных ресурсов)")
        items = read_estimate(path)
        return ([{"code": i.code, "name": i.name, "qty": i.qty, "unit": i.unit,
                  "kind": "work"} for i in items], f"смета {path.name}")
    if summary_path:
        import json
        data = json.loads(Path(summary_path).read_text(encoding="utf-8"))
        return _positions_from_summary(data), f"КС-2 {Path(summary_path).name}"
    raise ValueError("нужен источник позиций: smeta или summary_path")


def spec_from_estimate(positions: list[dict], *, object_name: str = "",
                       act_number: str = "", act_date: str = "", start_date: str = "",
                       end_date: str = "", work: str = "", work_types: list[str] | None = None,
                       matrix: dict | None = None, source: str = "",
                       representatives: dict | None = None,
                       project_docs: list | None = None) -> dict:
    """Собрать черновик спецификации АОСР из позиций сметы/КС-2."""
    matrix = matrix or load_matrix()
    classified = classify_positions(positions, matrix, dominant=work_types)

    if work_types:
        chosen = list(work_types)
    else:
        chosen = [w["work_type"] for w in classified["work_types"] if w["hidden_work"]]

    # в акт идут только скрытые работы: демонтаж и вывоз мусора в черновике не участвуют,
    # но перечислены в excluded с причиной — чтобы их не искали в акте
    hidden_positions: list[dict] = []
    excluded: list[dict] = []
    for slot in classified["work_types"]:
        if slot["hidden_work"]:
            hidden_positions += slot["positions"]
        else:
            for pos in slot["positions"]:
                excluded.append({**pos, "reason": f"не скрытые работы: {slot['name']}",
                                 "work_type": slot["work_type"]})
    for pos in classified["unknown"]:
        excluded.append({**pos, "reason": "вид работ не распознан"})
    for pos in classified["inherited"]:
        hidden_positions.append(pos)

    req = required_kinds(chosen, matrix) if chosen else {"required": [], "protocols": [],
                                                         "norms": [], "profile": None,
                                                         "protocols_conditional": []}
    if not work:
        seen: list[str] = []
        for pos in hidden_positions or positions:
            name = str(pos.get("name") or "").strip()
            if name and name not in seen:
                seen.append(name)
        work = "; ".join(seen)

    # нормы из матрицы — это профиль, а не нормативная база: помечаем источник честно
    norms = [dict(n, source="matrix") for n in req["norms"]]
    attachments = [{"name": proto, "code": "", "date": "", "sheets": None}
                   for proto in req["protocols"]]
    attachments += [{"name": what, "code": "", "date": "", "sheets": None}
                    for what in (matrix["document_kinds"][k] for k in req["required"]
                                 if k not in {"аоср", "протокол"})]

    spec = {
        "object_name": object_name,
        "object_address": "",
        "act_number": act_number,
        "act_date": act_date,
        "work": work,
        "location": {},
        "start_date": start_date,
        "end_date": end_date,
        "work_types": chosen,
        "representatives": representatives or {},
        "project_docs": project_docs or [],
        "materials": [],
        "norms": norms,
        "attachments": attachments,
        "next_works": "",
        "excluded": excluded,
        "source": {"positions_from": source,
                   "positions_total": sum(1 for p in positions if not p.get("nested")),
                   "nested_resources_total": sum(1 for p in positions if p.get("nested")),
                   "positions_classified": classified["classified"],
                   "positions_in_act": len(hidden_positions),
                   "positions_excluded": len(excluded),
                   "unclassified": classified["unknown"],
                   "inherited": classified["inherited"],
                   "merged": classified.get("merged") or [],
                   "candidates": [{"work_type": w["work_type"], "positions": len(w["positions"])}
                                  for w in classified["candidates"]],
                   "profile": matrix.get("profile")},
        "norms_source": "матрица видов работ (профиль, не нормативная база)",
        "norms_unverified": sorted({n.get("doc") for n in norms if n.get("doc")}),
        "work_type_details": [{"work_type": w["work_type"], "name": w["name"],
                               "positions": len(w["positions"])}
                              for w in classified["work_types"]],
    }

    fields = [
        {"field": "work", "source": "смета" if work else "нет", "auto": bool(work)},
        {"field": "work_types", "source": "матрица" if chosen else "нет", "auto": bool(chosen)},
        {"field": "norms", "source": "матрица (проверить по базе)" if norms else "нет",
         "auto": bool(norms)},
        {"field": "attachments", "source": "матрица" if attachments else "нет",
         "auto": bool(attachments)},
        {"field": "object_name", "source": "задано" if object_name else "нет",
         "auto": bool(object_name)},
        {"field": "act_number", "source": "задано" if act_number else "нет",
         "auto": bool(act_number)},
        {"field": "act_date", "source": "задано" if act_date else "нет", "auto": bool(act_date)},
        {"field": "start_date", "source": "задано" if start_date else "нет", "auto": bool(start_date)},
        {"field": "end_date", "source": "задано" if end_date else "нет", "auto": bool(end_date)},
        {"field": "representatives", "source": "нет", "auto": False},
        {"field": "project_docs", "source": "нет", "auto": False},
        {"field": "materials", "source": "нет", "auto": False},
    ]
    spec["fields"] = fields
    spec["needs_input"] = [f["field"] for f in fields if not f["auto"]]
    spec["notes"] = [
        "Attachments — шаблон: наименования из матрицы, даты/шифры/листы заполняет человек.",
        "Нормы взяты из матрицы с пунктами; редакции проверяются на дату акта.",
    ]
    if classified["unknown"]:
        spec["notes"].append(f"Часть позиций не классифицирована ({len(classified['unknown'])}): "
                             f"указать вид работ вручную (work_types).")
    if excluded:
        spec["notes"].append(f"Вне акта {len(excluded)} позиций (демонтаж, вывоз мусора, "
                             f"нераспознанные) — см. excluded.")
    return spec


def spec_to_markdown(spec: dict) -> str:
    src = spec.get("source") or {}
    lines = ["# Черновик спецификации АОСР", "",
             f"- объект: {spec.get('object_name') or '—'}",
             f"- акт: № {spec.get('act_number') or '—'} от {spec.get('act_date') or '—'}",
             f"- работы: {spec.get('work') or '—'}",
             f"- виды работ: {', '.join(spec.get('work_types') or []) or '—'}",
             f"- источник позиций: {src.get('positions_from') or '—'} "
             f"({src.get('positions_classified')} из {src.get('positions_total')} опознано)",
             f"- норм с пунктами: {len(spec.get('norms') or [])} "
             f"(источник: {spec.get('norms_source') or '—'})",
             f"- позиций в акте: {src.get('positions_in_act')} · вне акта: "
             f"{src.get('positions_excluded')}",
             f"- шаблон приложений: {len(spec.get('attachments') or [])}", "",
             "| Поле | Источник |", "|---|---|"]
    for f in spec.get("fields") or []:
        lines.append(f"| {f['field']} | {f['source']} |")
    if spec.get("needs_input"):
        lines += ["", "**Заполнить вручную:** " + ", ".join(spec["needs_input"])]
    if spec.get("notes"):
        lines += ["", *[f"- {n}" for n in spec["notes"]]]
    return "\n".join(lines) + "\n"
