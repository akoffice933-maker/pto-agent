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

    completeness = check_completeness({"documents": rows})
    return {
        "xlsx": str(xlsx_path),
        "object": object_name,
        "act_ref": act_ref,
        "date": registry_date,
        "count": len(rows),
        "sheets": sum(r["sheets"] or 0 for r in rows),
        "by_kind": by_kind,
        "documents": rows,
        "completeness": completeness,
    }


def check_completeness(spec: dict, *, required: list[str] | None = None,
                       act_ref: str | None = None) -> dict:
    """Проверить комплектность: обязательные типы, статусы, привязка к акту."""
    documents = list((spec or {}).get("documents") or [])
    kinds_present = {_kind_of(doc) for doc in documents}
    required_kinds = required or list(REQUIRED_KINDS)

    missing = [{"kind": k, "what": REQUIRED_KINDS.get(k, k)}
               for k in required_kinds if k not in kinds_present]
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

    complete = not missing and not bad_status and not foreign_act
    return {
        "complete": complete,
        "missing": missing,
        "bad_status": bad_status,
        "not_checked": not_checked,
        "foreign_act": foreign_act,
        "documents": len(documents),
        "kinds": sorted(kinds_present),
        "verdict": ("комплект полный" if complete else
                    f"не хватает: {', '.join(m['kind'] for m in missing) or '—'}"
                    + (f"; статусы требуют внимания: {len(bad_status)}" if bad_status else "")
                    + (f"; документы другого акта: {len(foreign_act)}" if foreign_act else "")),
    }


def registry_to_markdown(result: dict) -> str:
    comp = result["completeness"]
    lines = [f"# Реестр исполнительной документации: {result.get('object') or '—'}", "",
             f"- акт: {result.get('act_ref') or '—'} · дата реестра: {result.get('date')}",
             f"- документов: {result['count']} · листов: {result['sheets']}",
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
    if comp["not_checked"]:
        lines += ["", f"Не проверено выпуском: {len(comp['not_checked'])} документ(ов)"]
    return "\n".join(lines) + "\n"
