# -*- coding: utf-8 -*-
"""Чек-лист выполненных работ: пункты контроля с источником, результат и подпись человека.

Зачем отдельный документ, а не строка в акте:

* **подпись без проверки — самая частая фикция.** Поэтому пункт имеет три состояния
  («соответствует» / «не соответствует» / «не проверено»), и подписать чек-лист с
  непроверенными пунктами нельзя: отказ с перечнем, а не «подписал, но не глядя»;
* **пункт без источника не создаётся.** Требование контроля берётся из нормы (документ,
  пункт, редакция) или из чек-листа организации, который передали файлом. Текст требования
  агент не сочиняет: он либо ссылается на пункт нормы, либо берёт формулировку из файла;
* **несоответствие без описания не подписывается.** «Не соответствует» без того, что именно
  не так, — это отметка без факта;
* **правка после подписи аннулирует подпись** (отпечаток содержания, общий с ТК и АТН).

Матрица требований даёт только профиль (какие нормы и протоколы нужны виду работ). Пункты,
взятые из матрицы, помечаются `source_scope: "matrix"` — это не первоисточник, и в отчёте так и
написано. Норма считается сверенной только если документ и пункт есть в нормативной базе.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .asbuilt.norms_check import verify_norms
from .asbuilt.requirements import get_work_type, load_matrix
from .fingerprint import check_signature, content_fingerprint

SCHEMA = "pto-agent/checklist@1"

RESULT_OK = "соответствует"
RESULT_FAIL = "не соответствует"
RESULT_UNCHECKED = "не проверено"
RESULTS = (RESULT_OK, RESULT_FAIL, RESULT_UNCHECKED)

STATUS_DRAFT = "черновик"
STATUS_FILLED = "заполнен, не подписан"
STATUS_SIGNED = "подписан"
STATUS_SIGNED_WITH_ISSUES = "подписан с несоответствиями (работы не приняты)"
STATUS_CHANGED = "изменён после подписи — подпись недействительна"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

CHECK_ITEM_NO_SOURCE = "ITEM_WITHOUT_SOURCE"
CHECK_ITEM_UNVERIFIED = "ITEM_SOURCE_UNVERIFIED"
CHECK_DUPLICATE_ITEM = "DUPLICATE_ITEM"
CHECK_BAD_RESULT = "BAD_RESULT"
CHECK_FAIL_NO_NOTE = "NONCONFORMITY_WITHOUT_NOTE"
CHECK_UNCHECKED = "ITEM_NOT_CHECKED"
CHECK_EMPTY = "CHECKLIST_EMPTY"


def _num(value) -> str:
    return str(value)


def build_checklist(*, work_type_id: str = "", object_name: str = "", number: str = "",
                    doc_date: date | None = None, act_ref: str = "", matrix=None,
                    items: list[dict] | None = None, folder=None,
                    today: date | None = None) -> dict:
    """Собрать чек-лист по виду работ: пункты из переданного файла или из матрицы/норм."""
    today = today or date.today()
    matrix = matrix or load_matrix()
    work_type = get_work_type(work_type_id, matrix) if work_type_id else None
    checks: list[dict] = []
    prepared: list[dict] = []
    rejected: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    def item_record(raw: dict, origin: str) -> dict | None:
        text = str(raw.get("requirement") or raw.get("text") or "").strip()
        source = raw.get("source") or {}
        doc = str(source.get("doc") or "").strip()
        point = str(source.get("point") or source.get("clause") or "").strip()
        edition = str(source.get("edition") or "").strip()
        record = {
            "requirement": text,
            "control": str(raw.get("control") or "").strip() or None,
            "source": {"doc": doc, "point": point, "edition": edition},
            "source_scope": origin,
            "result": RESULT_UNCHECKED,
            "note": "",
            "measured": raw.get("measured"),
            "result_by": None,
            "result_at": None,
        }
        missing = [name for name, ok in (("документ", bool(doc)), ("пункт", bool(point)))
                   if not ok]
        if missing or not text:
            record["why"] = (f"нет {' и '.join(missing)}" if missing
                             else "нет текста требования")
            rejected.append(record)
            add(CHECK_ITEM_NO_SOURCE, LEVEL_ERROR,
                f"пункт «{text or '(без текста)'}»: {record['why']} — в чек-лист не попадает "
                f"(требование без источника не создаётся)", requirement=text or None)
            return None
        return record

    if items:
        for raw in items:
            record = item_record(raw, str(raw.get("source_scope") or "spec"))
            if record is not None:
                prepared.append(record)
    else:
        if not work_type:
            add(CHECK_EMPTY, LEVEL_ERROR,
                "не указан вид работ и не передан список пунктов — чек-лист пуст")
        else:
            for norm in work_type.get("norms") or []:
                topic = str(norm.get("topic") or "").strip()
                record = item_record({
                    "requirement": (f"Соответствие требованиям {norm.get('doc')}, п. "
                                    f"{norm.get('clause')}" + (f" ({topic})" if topic else "")),
                    "control": "проверить по пункту нормы (текст пункта — в первоисточнике)",
                    "source": {"doc": norm.get("doc"), "point": norm.get("clause"),
                               "edition": norm.get("edition")},
                }, "matrix")
                if record is not None:
                    prepared.append(record)
            for protocol in work_type.get("protocols") or []:
                # протокол — это документ, а не пункт нормы: источником считается профиль
                prepared.append({
                    "requirement": f"Наличие документа: {protocol}",
                    "control": "проверить наличие и дату документа",
                    "source": {"doc": "матрица требований проекта", "point": work_type_id,
                               "edition": str(matrix.get("profile") or "профиль проекта")},
                    "source_scope": "matrix",
                    "result": RESULT_UNCHECKED, "note": "", "measured": None,
                    "result_by": None, "result_at": None,
                })

    seen: dict[str, int] = {}
    numbered: list[dict] = []
    for record in prepared:
        key = record["requirement"].strip().lower()
        if key in seen:
            add(CHECK_DUPLICATE_ITEM, LEVEL_WARNING,
                f"пункт «{record['requirement']}» уже есть (№{seen[key]}) — второй раз не "
                f"добавляется", requirement=record["requirement"])
            continue
        seen[key] = len(numbered) + 1
        record["no"] = len(numbered) + 1
        numbered.append(record)

    # сверка источников с нормативной базой
    refs = [{"doc": r["source"]["doc"], "clause": r["source"]["point"]} for r in numbered]
    try:
        verified = verify_norms(refs, folder)
    except Exception:                                   # база недоступна — это не повод врать
        verified = {"results": [{"status": "base_error", "note": "база не прочитана"}] * len(refs)}
    for record, result in zip(numbered, verified.get("results") or []):
        record["source_status"] = result.get("status")
        record["source_note"] = result.get("note")
        record["source_verified"] = result.get("status") in ("verified", "verified_partial")
        if not record["source_verified"]:
            add(CHECK_ITEM_UNVERIFIED, LEVEL_WARNING,
                f"пункт №{record['no']}: источник ({record['source']['doc']}, п. "
                f"{record['source']['point']}) не подтверждён базой агента — "
                f"{result.get('note') or 'нет статуса'}; текст пункта смотреть в первоисточнике",
                requirement=record["requirement"])

    checklist = {
        "schema": SCHEMA,
        "kind": "checklist",
        "title": "Чек-лист выполненных работ",
        "number": number,
        "doc_date": (doc_date or today).isoformat(),
        "built_at": today.isoformat(),
        "object_name": object_name,
        "work_type": ({"id": work_type.get("id"), "name": work_type.get("name")}
                      if work_type else ({"id": work_type_id or None, "name": None}
                                         if work_type_id else None)),
        "act_ref": act_ref or None,
        "items": numbered,
        "rejected": rejected,
        "checks": checks,
        "status": STATUS_DRAFT if any(r["result"] == RESULT_UNCHECKED for r in numbered) else
                  STATUS_FILLED,
        "confirmed_by": None,
        "totals": {
            "items": len(numbered),
            "rejected": len(rejected),
            "checked": len([r for r in numbered if r["result"] != RESULT_UNCHECKED]),
            "unchecked": len([r for r in numbered if r["result"] == RESULT_UNCHECKED]),
            "ok": len([r for r in numbered if r["result"] == RESULT_OK]),
            "failed": len([r for r in numbered if r["result"] == RESULT_FAIL]),
            "source_verified": len([r for r in numbered if r.get("source_verified")]),
        },
        "disclaimer": ("Чек-лист фиксирует результат контроля по пунктам с источником. Работы "
                       "принимает человек: подпись ставится только на полностью проверенный "
                       "чек-лист, а несоответствия закрываются отдельно."),
    }
    checklist["can_close"] = can_close(checklist)
    return checklist


def fill(checklist: dict, results: list[dict], *, by: str = "", role: str = "",
         today: date | None = None) -> dict:
    """Заполнить результаты по номерам пунктов: [{no, result, note, measured}]."""
    today = today or date.today()
    checklist = json.loads(json.dumps(checklist))          # работаем с копией
    indexed = {item["no"]: item for item in checklist.get("items") or []}
    checks = list(checklist.get("checks") or [])
    problems: list[str] = []

    for raw in results or []:
        number = raw.get("no") or raw.get("item")
        try:
            number = int(number)
        except (TypeError, ValueError):
            problems.append(f"пункт «{number}»: номер не число")
            continue
        item = indexed.get(number)
        if item is None:
            problems.append(f"пункта №{number} в чек-листе нет")
            continue
        result = str(raw.get("result") or "").strip().lower()
        if result not in RESULTS:
            checks.append({"code": CHECK_BAD_RESULT, "level": LEVEL_ERROR,
                           "message": f"пункт №{number}: результат «{raw.get('result')}» не из "
                                      f"списка {RESULTS} — не принят", "item": number})
            continue
        item["result"] = result
        item["note"] = str(raw.get("note") or "").strip()
        if raw.get("measured") not in (None, ""):
            item["measured"] = raw["measured"]
        item["result_by"] = (by or "").strip() or None
        item["result_at"] = today.isoformat()

    checklist["checks"] = checks
    checklist["problems"] = problems
    return _recount(checklist)


def _recount(checklist: dict) -> dict:
    """Пересчитать итоги и статус по фактическим результатам."""
    items = checklist.get("items") or []
    unchecked = [i for i in items if i["result"] == RESULT_UNCHECKED]
    failed = [i for i in items if i["result"] == RESULT_FAIL]
    checklist["totals"] = {
        **(checklist.get("totals") or {}),
        "items": len(items),
        "checked": len(items) - len(unchecked),
        "unchecked": len(unchecked),
        "ok": len([i for i in items if i["result"] == RESULT_OK]),
        "failed": len(failed),
        "source_verified": len([i for i in items if i.get("source_verified")]),
    }
    if checklist.get("confirmed_by"):
        check = check_signature(checklist)
        checklist["status"] = (STATUS_CHANGED if check["changed_after_confirm"]
                              else (STATUS_SIGNED_WITH_ISSUES if failed else STATUS_SIGNED))
    else:
        checklist["status"] = STATUS_DRAFT if unchecked else STATUS_FILLED
    checklist["can_close"] = can_close(checklist)
    return checklist


def can_close(checklist: dict) -> dict:
    """Можно ли предъявлять работы по этому чек-листу. Ответ — с причинами, не «да/нет»."""
    reasons: list[str] = []
    items = checklist.get("items") or []
    if not items:
        reasons.append("в чек-листе нет ни одного пункта с источником")
    if not checklist.get("confirmed_by"):
        reasons.append("чек-лист не подписан человеком (нет ФИО и роли)")
    elif check_signature(checklist)["changed_after_confirm"]:
        reasons.append("содержание изменено после подписи — подпись недействительна")
    unchecked = [i for i in items if i["result"] == RESULT_UNCHECKED]
    if unchecked:
        reasons.append(f"не проверено пунктов: {len(unchecked)} "
                       f"({', '.join('№' + str(i['no']) for i in unchecked[:5])})")
    failed = [i for i in items if i["result"] == RESULT_FAIL]
    if failed:
        reasons.append(f"несоответствий: {len(failed)} — работы не приняты, нужны мероприятия")
    missing_note = [i for i in failed if not i.get("note")]
    if missing_note:
        reasons.append(f"несоответствия без описания: {', '.join('№' + str(i['no']) for i in missing_note[:5])}")
    return {"allowed": not reasons, "reasons": reasons}


def sign(checklist: dict, *, by: str, role: str, reason: str = "", audit_log=None,
         today: date | None = None) -> dict:
    """Подписать чек-лист. Без ФИО и роли, с непроверенными пунктами — отказ."""
    today = today or date.today()
    checklist = json.loads(json.dumps(checklist))
    problems: list[str] = []
    if not str(by or "").strip():
        problems.append("анонимная подпись: не указано ФИО проверившего")
    if not str(role or "").strip():
        problems.append("не указана роль (должность) проверившего")
    items = checklist.get("items") or []
    if not items:
        problems.append("в чек-листе нет ни одного пункта с источником — подписывать нечего")
    unchecked = [i for i in items if i["result"] == RESULT_UNCHECKED]
    if unchecked:
        problems.append(f"не проверено пунктов: {len(unchecked)} "
                        f"({', '.join('№' + str(i['no']) for i in unchecked[:8])}) — "
                        f"непроверенное не подписывается")
    missing_note = [i for i in items
                    if i["result"] == RESULT_FAIL and not i.get("note")]
    if missing_note:
        problems.append("несоответствия без описания: "
                        f"{', '.join('№' + str(i['no']) for i in missing_note[:8])}")
    if problems:
        checklist["sign_problems"] = problems
        return checklist

    failed = [i for i in items if i["result"] == RESULT_FAIL]
    checklist["confirmed_by"] = {"by": str(by).strip(), "role": str(role).strip(),
                                 "reason": str(reason or ""), "at": today.isoformat(),
                                 "items": len(items), "failed": len(failed)}
    checklist["confirmed_by"]["content_fingerprint"] = content_fingerprint(checklist)
    checklist.pop("sign_problems", None)
    checklist["status"] = STATUS_SIGNED_WITH_ISSUES if failed else STATUS_SIGNED
    checklist["can_close"] = can_close(checklist)
    if audit_log is not None:
        audit_log.append("agent:checklist", "agent", "checklist.signed",
                         f"checklist:{checklist.get('number') or 'б/н'}",
                         {"by": checklist["confirmed_by"]["by"],
                          "role": checklist["confirmed_by"]["role"],
                          "items": len(items), "failed": len(failed),
                          "can_close": checklist["can_close"]["allowed"]})
    return checklist


def to_markdown(checklist: dict) -> str:
    totals = checklist.get("totals") or {}
    work = checklist.get("work_type") or {}
    lines = ["# Чек-лист выполненных работ", "",
             f"**Объект:** {checklist.get('object_name') or 'не указан'} · "
             f"**вид работ:** {work.get('name') or work.get('id') or 'не указан'} · "
             f"**чек-лист:** {checklist.get('number') or 'б/н'} от {checklist.get('doc_date')}"
             + (f" · **акт:** {checklist['act_ref']}" if checklist.get("act_ref") else ""), "",
             f"Пунктов: {totals.get('items')} · проверено {totals.get('checked')} · "
             f"не проверено {totals.get('unchecked')} · соответствует {totals.get('ok')} · "
             f"не соответствует {totals.get('failed')} · источников сверено "
             f"{totals.get('source_verified')}", "",
             f"**Статус:** {checklist.get('status')}", ""]

    close = checklist.get("can_close") or {}
    if close.get("allowed"):
        lines += ["Работы по чек-листу можно предъявлять: все пункты проверены, "
                  "несоответствий нет, чек-лист подписан.", ""]
    else:
        lines += ["**Работы предъявлять нельзя:**", ""]
        lines += [f"- {reason}" for reason in close.get("reasons") or []] + [""]

    if checklist.get("items"):
        lines += ["## Пункты контроля", "",
                  "| № | Требование | Источник | Результат | Примечание |",
                  "|---|---|---|---|---|"]
        for item in checklist["items"]:
            source = f"{item['source']['doc']}, п. {item['source']['point']}"
            if not item.get("source_verified"):
                source += " (не сверено)"
            lines.append(f"| {item['no']} | {item['requirement']} | {source} | "
                         f"{item['result']} | {item.get('note') or '—'} |")
        lines.append("")

    if checklist.get("rejected"):
        lines += ["## Пункты, не попавшие в чек-лист", ""]
        lines += [f"- {r.get('requirement') or '(без текста)'}: {r.get('why')}"
                  for r in checklist["rejected"]] + [""]

    errors = [c for c in checklist.get("checks") or [] if c["level"] == LEVEL_ERROR]
    warnings = [c for c in checklist.get("checks") or [] if c["level"] == LEVEL_WARNING]
    if errors:
        lines += ["## Ошибки", ""] + [f"- **{c['code']}** — {c['message']}" for c in errors] + [""]
    if warnings:
        lines += ["## Требует решения", ""] + [f"- {c['code']} — {c['message']}" for c in warnings] + [""]

    if checklist.get("confirmed_by"):
        who = checklist["confirmed_by"]
        lines += [f"Подписал: {who.get('by')}, {who.get('role')} ({who.get('at')}).", ""]
    if checklist.get("sign_problems"):
        lines += ["**Подпись не принята:**", ""]
        lines += [f"- {p}" for p in checklist["sign_problems"]] + [""]
    lines += [f"> {checklist.get('disclaimer')}", ""]
    return "\n".join(lines)


def export_docx(checklist: dict, path: str | Path) -> Path:
    """Чек-лист в DOCX: таблица пунктов, результат, примечание, места подписей."""
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("Чек-лист выполненных работ", level=1)
    work = checklist.get("work_type") or {}
    doc.add_paragraph(f"Объект: {checklist.get('object_name') or '—'}　"
                      f"Вид работ: {work.get('name') or work.get('id') or '—'}")
    doc.add_paragraph(f"Чек-лист № {checklist.get('number') or 'б/н'} от "
                      f"{checklist.get('doc_date')}　"
                      + (f"Акт: {checklist['act_ref']}" if checklist.get("act_ref") else ""))

    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells,
                          ["№", "Требование (источник)", "Результат", "Примечание",
                           "Проверил"]):
        cell.text = text
    for item in checklist.get("items") or []:
        cells = table.add_row().cells
        cells[0].text = str(item["no"])
        source = f"{item['source']['doc']}, п. {item['source']['point']}"
        cells[1].text = f"{item['requirement']}\n({source})"
        cells[2].text = item["result"]
        cells[3].text = item.get("note") or ""
        cells[4].text = item.get("result_by") or ""

    doc.add_paragraph()
    total = doc.add_paragraph()
    run = total.add_run(f"Статус: {checklist.get('status')}. "
                        f"Проверено {checklist['totals']['checked']} из "
                        f"{checklist['totals']['items']} пунктов.")
    run.bold = True
    run.font.size = Pt(10)

    close = checklist.get("can_close") or {}
    if not close.get("allowed"):
        doc.add_paragraph("Работы предъявлять нельзя:")
        for reason in close.get("reasons") or []:
            doc.add_paragraph(f"• {reason}", style="List Bullet")

    doc.add_paragraph()
    doc.add_paragraph("Проверил (инженер ПТО): ______________________ / ______________________")
    doc.add_paragraph("Принял (представитель заказчика): ______________________ / ______________")
    doc.add_paragraph(f"{checklist.get('disclaimer')}").runs[0].font.size = Pt(9)
    doc.save(path)
    return path


def write(checklist: dict, out_dir: str | Path, *, stem: str | None = None) -> dict:
    """Сохранить чек-лист: JSON, Markdown, DOCX."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    work = (checklist.get("work_type") or {}).get("id") or "объект"
    tail = stem or (str(checklist.get("number") or "").replace(" ", "_").replace("/", "-")
                    or work)
    (out / f"checklist_{tail}.json").write_text(json.dumps(checklist, ensure_ascii=False,
                                                           indent=1), encoding="utf-8")
    (out / f"checklist_{tail}.md").write_text(to_markdown(checklist), encoding="utf-8")
    export_docx(checklist, out / f"checklist_{tail}.docx")
    return {"json": str(out / f"checklist_{tail}.json"),
            "markdown": str(out / f"checklist_{tail}.md"),
            "docx": str(out / f"checklist_{tail}.docx")}
