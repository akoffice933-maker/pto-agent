# -*- coding: utf-8 -*-
"""Реестр исполнительной документации и проверка комплектности.

Реестр — это оглавление комплекта ИД: что в него входит, на сколько листов, в каком статусе
и где лежит файл. Мы не придумываем состав: он приходит из спецификации работ, а реестр
только честно показывает, чего не хватает.

Проверка комплектности (`check_completeness`) отвечает на вопрос «можно ли сдавать комплект»:
какие обязательные документы по виду работ отсутствуют, какие позиции не проверены выпуском,
какие относятся к другому акту.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from .requirements import load_matrix, required_kinds

# обязательный минимум по типу документа для типового комплекта СМР
REQUIRED_KINDS = {
    "аоср": "акт освидетельствования скрытых работ",
    "схема": "исполнительная схема",
    "сертификат": "сертификат/декларация на материалы",
    "протокол": "протокол испытаний",
    "журнал": "общий/специальный журнал работ",
}
STATUS_OK = {"проверен", "подписан", "ok", "готов"}
STATUS_BAD = {"черновик", "не проверен", "отсутствует", "draft"}


def _kind_of(doc: dict) -> str:
    kind = str(doc.get("kind") or "").strip().lower()
    if kind:
        return kind
    name = str(doc.get("name") or "").lower()
    for key, words in (("аоср", ("аоср", "скрытых работ")),
                       ("схема", ("схема",)),
                       ("сертификат", ("сертификат", "деклараци")),
                       ("протокол", ("протокол", "испытан")),
                       ("журнал", ("журнал",))):
        if any(w in name for w in words):
            return key
    return "прочее"


def build_registry(spec: dict, out_dir: str | Path) -> dict:
    """Собрать реестр ИД: XLSX + JSON; вернуть состав, статусы и пробелы."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    documents = list((spec or {}).get("documents") or [])
    object_name = (spec or {}).get("object_name") or ""
    act_ref = (spec or {}).get("act_ref") or ""
    registry_date = (spec or {}).get("registry_date") or date.today().isoformat()

    rows = []
    for i, doc in enumerate(documents, start=1):
        rows.append({
            "no": i,
            "name": doc.get("name") or "",
            "code": doc.get("code") or "",
            "kind": _kind_of(doc),
            "date": doc.get("date") or "",
            "sheets": doc.get("sheets"),
            "status": doc.get("status") or "не проверен",
            "act_ref": doc.get("act_ref") or act_ref,
            "file": doc.get("file") or "",
        })

    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр ИД"
    headers = ["№", "Наименование документа", "Шифр", "Тип", "Дата", "Листов",
               "Статус", "Акт", "Файл"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in rows:
        ws.append([row["no"], row["name"], row["code"], row["kind"], row["date"],
                   row["sheets"], row["status"], row["act_ref"], row["file"]])
    widths = [5, 52, 18, 14, 12, 8, 14, 16, 32]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("Сводка")
    by_kind: dict[str, int] = {}
    for row in rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
    ws2.append(["Объект", object_name])
    ws2.append(["Акт", act_ref])
    ws2.append(["Дата реестра", registry_date])
    ws2.append(["Документов", len(rows)])
    ws2.append(["Листов", sum(r["sheets"] or 0 for r in rows)])
    ws2.append([])
    ws2.append(["Тип", "Количество"])
    for kind, count in sorted(by_kind.items()):
        ws2.append([kind, count])
    ws2.column_dimensions["A"].width = 24
    ws2.column_dimensions["B"].width = 40

    xlsx_path = out / "registry_id.xlsx"
    wb.save(xlsx_path)

    work_types = list((spec or {}).get("work_types") or (spec or {}).get("work_kinds") or [])
    completeness = check_completeness({"documents": rows}, work_types=work_types)
    return {
        "xlsx": str(xlsx_path),
        "object": object_name,
        "act_ref": act_ref,
        "date": registry_date,
        "count": len(rows),
        "sheets": sum(r["sheets"] or 0 for r in rows),
        "by_kind": by_kind,
        "work_types": work_types,
        "documents": rows,
        "completeness": completeness,
    }


def check_completeness(spec: dict, *, required: list[str] | None = None,
                       act_ref: str | None = None,
                       work_types: list[str] | None = None,
                       matrix: dict | None = None) -> dict:
    """Проверить комплектность: обязательные типы, статусы, привязка к акту.

    Обязательный состав берётся по матрице вида работ (`work_types`), если он задан:
    у электромонтажа требуются протоколы изоляции и УЗО, у кровли — испытание на
    водонепроницаемость, у сетей — акт освидетельствования участков сетей. Если вид работ
    не задан — работает типовой минимум СМР, и это явно видно в `required_source`.
    """
    documents = list((spec or {}).get("documents") or [])
    kinds_present = {_kind_of(doc) for doc in documents}
    matrix = matrix or load_matrix()
    profile = {"name": "pto-agent/default", "version": "1.0"}
    protocol_names: list[str] = []
    norm_refs: list[dict] = []

    if work_types:
        req = required_kinds(work_types, matrix)
        required_kinds_list = req["required"]
        profile = req.get("profile") or profile
        protocol_names = req["protocols"]
        norm_refs = req["norms"]
        required_source = "матрица видов работ: " + ", ".join(work_types)
        unknown_types = req["unknown"]
    elif required:
        required_kinds_list = list(required)
        required_source = "заданный вручную список"
        unknown_types = []
    else:
        required_kinds_list = list(REQUIRED_KINDS)
        required_source = "типовой минимум СМР"
        unknown_types = []

    missing = [{"kind": k, "what": (REQUIRED_KINDS.get(k)
                                    or matrix["document_kinds"].get(k) or k)}
               for k in required_kinds_list if k not in kinds_present]
    bad_status = [{"name": doc.get("name"), "status": doc.get("status")}
                  for doc in documents
                  if str(doc.get("status") or "").strip().lower() in STATUS_BAD]
    not_checked = [{"name": doc.get("name")}
                   for doc in documents
                   if str(doc.get("status") or "").strip().lower() not in
                   {s.lower() for s in STATUS_OK}]
    foreign_act = [{"name": doc.get("name"), "act_ref": doc.get("act_ref")}
                   for doc in documents
                   if act_ref and (doc.get("act_ref") or act_ref) != act_ref]

    # протоколы из матрицы: ищем по названию среди документов комплекта
    names_blob = " ".join(str(doc.get("name") or "").lower() for doc in documents)
    missing_protocols = [p for p in protocol_names
                         if not _protocol_present(p, names_blob)]

    complete = not missing and not bad_status and not foreign_act and not missing_protocols
    verdict_parts = []
    if missing:
        verdict_parts.append("не хватает: " + ", ".join(m["kind"] for m in missing))
    if missing_protocols:
        verdict_parts.append(f"нет протоколов: {len(missing_protocols)}")
    if bad_status:
        verdict_parts.append(f"статусы требуют внимания: {len(bad_status)}")
    if foreign_act:
        verdict_parts.append(f"документы другого акта: {len(foreign_act)}")
    return {
        "complete": complete,
        "profile": profile,
        "required_source": required_source,
        "required": required_kinds_list,
        "missing": missing,
        "missing_protocols": missing_protocols,
        "bad_status": bad_status,
        "not_checked": not_checked,
        "foreign_act": foreign_act,
        "documents": len(documents),
        "kinds": sorted(kinds_present),
        "work_types": list(work_types or []),
        "unknown_work_types": unknown_types,
        "norms": norm_refs,
        "verdict": ("комплект полный" if complete else "; ".join(verdict_parts)),
    }


def _protocol_present(protocol: str, names_blob: str) -> bool:
    """Протокол считается приложенным, если в комплекте есть документ с его темой.

    Сравниваем по значимым словам и требуем совпадения большей части: в реестре пишут
    «Протокол проверки УЗО щита ЩР-1», а в матрице — «...УЗО / дифференциальных автоматов
    (ток и время срабатывания)». Полное совпадение строк здесь дало бы ложное «нет документа».
    """
    STOP = {"при", "для", "или", "на", "по", "не", "из", "и", "в", "с", "о"}
    words = [w.strip("«»().,/").lower().replace("ё", "е")
             for w in str(protocol).split()]
    key_words = [w for w in words
                 if len(w) >= 3 and w not in STOP and not w.replace("-", "").isdigit()]
    if not key_words:
        return True
    blob = names_blob.replace("ё", "е")
    matched = [w for w in key_words if w in blob]
    return len(matched) >= 2 and len(matched) / len(key_words) >= 0.5


def registry_to_markdown(result: dict) -> str:
    comp = result["completeness"]
    lines = [f"# Реестр исполнительной документации: {result.get('object') or '—'}", "",
             f"- акт: {result.get('act_ref') or '—'} · дата реестра: {result.get('date')}",
             f"- документов: {result['count']} · листов: {result['sheets']}",
             f"- профиль комплектности: {comp['profile']['name']}/v{comp['profile']['version']}"
             f" · требования: {comp['required_source']}",
             f"- комплектность: **{comp['verdict']}**", "",
             "| № | Документ | Шифр | Тип | Листов | Статус |",
             "|---|---|---|---|---|---|"]
    for doc in result["documents"]:
        lines.append(f"| {doc['no']} | {doc['name']} | {doc['code']} | {doc['kind']} | "
                     f"{doc['sheets'] if doc['sheets'] is not None else '—'} | {doc['status']} |")
    if comp["missing"]:
        lines += ["", "**Не хватает обязательных документов:**"]
        lines += [f"- {m['kind']} — {m['what']}" for m in comp["missing"]]
    if comp["foreign_act"]:
        lines += ["", "**Документы другого акта:** " +
                  ", ".join(f"{d['name']} ({d['act_ref']})" for d in comp["foreign_act"])]
    if comp.get("missing_protocols"):
        lines += ["", "**Не приложены протоколы, требуемые видом работ:**"]
        lines += [f"- {p}" for p in comp["missing_protocols"]]
    if comp["not_checked"]:
        lines += ["", f"Не проверено выпуском: {len(comp['not_checked'])} документ(ов)"]
    return "\n".join(lines) + "\n"
