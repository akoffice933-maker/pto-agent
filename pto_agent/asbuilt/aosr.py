# -*- coding: utf-8 -*-
"""АОСР: черновик акта освидетельствования скрытых работ по действующей форме.

Форма по умолчанию — приложение № 3 к приказу Минстроя России № 344/пр (действует с
01.09.2023). Семь пунктов акта: работы → проектная документация → материалы → предъявленные
документы → нормы с пунктами → сроки → разрешение последующих работ, затем подписи
представителей сторон.

Ключевые правила:
  * агент готовит **черновик**; формулировка «акт готов к подписанию» не используется —
    подписывает человек (`can_transfer` = «можно передавать на подпись», а не «подписан»);
  * нормы без пункта, отсутствие ссылки на РД, пустые приложения и нарушенная хронология
    — блокеры, а не примечания в логе;
  * вид работ определяет матрицу приложений (`work_type_requirements`), в том числе для
    электромонтажа: изоляция, петля «фаза-нуль», УЗО, заземление.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from .forms import get_form, form_status
from .validation import validate_draft


def _p(doc, text: str, *, bold: bool = False, size: int = 10, center: bool = False,
       italic: bool = False):
    p = doc.add_paragraph()
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = bold
    run.italic = italic
    run.font.size = Pt(size)
    return p


def _table(doc, header: list[str], rows: list[list], widths: list[float] | None = None,
           size: int = 9):
    table = doc.add_table(rows=1, cols=len(header))
    table.style = "Table Grid"
    for cell, name in zip(table.rows[0].cells, header):
        cell.text = ""
        run = cell.paragraphs[0].add_run(name)
        run.bold = True
        run.font.size = Pt(size)
    for values in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, values):
            cell.text = ""
            cell.paragraphs[0].add_run("—" if value in (None, "") else str(value)).font.size = Pt(size)
    if widths:
        for row in table.rows:
            for cell, w in zip(row.cells, widths):
                cell.width = Cm(w)
    return table


def _norm_line(norm) -> str:
    if isinstance(norm, str):
        return norm
    doc = norm.get("doc") or norm.get("document") or "—"
    clause = norm.get("clause") or norm.get("item")
    edition = norm.get("edition")
    parts = [doc]
    if clause:
        parts.append(f"п. {clause}")
    if edition:
        parts.append(f"({edition})")
    return " ".join(parts)


def _material_line(mat) -> str:
    if isinstance(mat, str):
        return mat
    name = mat.get("name") or "—"
    cert = mat.get("certificate") or mat.get("doc")
    return f"{name} — {cert}" if cert else name


def _project_doc_line(doc) -> str:
    if isinstance(doc, str):
        return doc
    parts = [str(doc.get("doc") or doc.get("name") or "—")]
    if doc.get("sheet"):
        sheet = str(doc["sheet"]).strip()
        parts.append(sheet if sheet.lower().startswith("лист") else f"лист {sheet}")
    if doc.get("org"):
        parts.append(f"({doc['org']})")
    return ", ".join(parts)


def build_aosr(spec: dict, out_dir: str | Path, *, form: str | None = None,
               matrix: dict | None = None, today: date | None = None) -> dict:
    """Собрать черновик АОСР → `out_dir/aosr_<номер>.docx`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    spec = spec or {}
    form_meta = get_form(form)
    status_meta = form_status(form_meta["id"], today)

    act_number = str(spec.get("act_number") or "").strip()
    stem = (act_number or "aosr").replace(" ", "_").replace("/", "-").replace("№", "N")
    date_str = spec.get("act_date") or (today or date.today()).isoformat()

    work_types = spec.get("work_types") or []
    validation = validate_draft(spec, work_types=work_types, matrix=matrix, today=today)

    doc = Document()
    section = doc.sections[0]
    section.left_margin = section.right_margin = Cm(1.5)
    section.top_margin = section.bottom_margin = Cm(1.2)

    _p(doc, "ЧЕРНОВИК — на подпись человеку", bold=True, size=9, center=True, italic=True)
    _p(doc, form_meta["title"], bold=True, size=14, center=True)
    _p(doc, f"№ {act_number or '—'} от {date_str}", size=11, center=True)
    _p(doc, f"Форма: {form_meta['legal_basis']}", size=8, center=True, italic=True)
    if status_meta["warning"]:
        _p(doc, "ВНИМАНИЕ: " + status_meta["warning"], size=8, center=True, bold=True)
    doc.add_paragraph()

    # ── шапка: объект и представители ──
    _p(doc, "Объект капитального строительства", bold=True, size=10)
    _table(doc, ["Наименование", "Адрес"], [[spec.get("object_name"), spec.get("object_address")]],
           widths=[9.5, 8.0])
    doc.add_paragraph()
    if form_meta["layout"] == "sections":
        _p(doc, form_meta["sections"][0], bold=True, size=11)
    else:
        _p(doc, "Представители сторон", bold=True, size=10)
    rows = []
    for role in form_meta["roles"]:
        rep = (spec.get("representatives") or {}).get(role["key"]) or {}
        person = rep.get("person") or "—"
        if role["key"] == "customer" and rep.get("nrs"):
            person += f", НРС {rep['nrs']}"
        rows.append([role["label"], rep.get("org"), person, rep.get("position"), rep.get("doc")])
    _table(doc, ["Полномочия представителя", "Организация", "Ф.И.О.", "Должность",
                 "Документ о допуске"], rows, widths=[5.4, 3.4, 3.0, 2.6, 3.1])
    _p(doc, "составили настоящий акт о нижеследующем:", size=10)
    doc.add_paragraph()

    formatted = {
        "work": _with_location(spec),
        "project_docs": "; ".join(_project_doc_line(d) for d in (spec.get("project_docs") or [])),
        "materials": "; ".join(_material_line(m) for m in (spec.get("materials") or [])),
        "attachments": spec.get("attachments") or [],
        "norms": "; ".join(_norm_line(n) for n in (spec.get("norms") or [])),
        "period": f"с {spec.get('start_date') or '—'} по {spec.get('end_date') or '—'}",
        "next_works": spec.get("next_works") or spec.get("conclusion") or "",
    }

    for item in form_meta["items"]:
        _p(doc, f"{item['no']}. {item['label']}:", bold=True, size=10)
        if item["key"] == "attachments":
            docs = formatted["attachments"]
            if docs:
                _table(doc, ["№", "Наименование документа", "Шифр", "Дата", "Листов"],
                       [[i, d.get("name"), d.get("code"), d.get("date"), d.get("sheets")]
                        for i, d in enumerate(docs, start=1)],
                       widths=[1.0, 8.0, 3.6, 2.4, 2.0])
            else:
                _p(doc, "— (перечень пуст)", size=10)
        else:
            _p(doc, formatted[item["key"]] or "—", size=10)
        doc.add_paragraph()

    if form_meta["layout"] == "sections":
        # устаревшая форма: три раздела, а не семь пунктов
        _p(doc, form_meta["sections"][1], bold=True, size=11)
        docs = formatted["attachments"]
        if docs:
            _table(doc, ["№", "Наименование документа", "Шифр", "Дата", "Листов"],
                   [[i, d.get("name"), d.get("code"), d.get("date"), d.get("sheets")]
                    for i, d in enumerate(docs, start=1)],
                   widths=[1.0, 8.0, 3.6, 2.4, 2.0])
        else:
            _p(doc, "— (перечень пуст)", size=10)
        doc.add_paragraph()
        _p(doc, form_meta["sections"][2], bold=True, size=11)
        _table(doc, ["Параметр", "Значение"], [
            ["Работы", formatted["work"]],
            ["Проектная документация", formatted["project_docs"]],
            ["Материалы и изделия", formatted["materials"]],
            ["Нормативные документы", formatted["norms"]],
            ["Период выполнения", formatted["period"]],
        ], widths=[5.0, 12.5])
        doc.add_paragraph()

    _p(doc, "Подписи представителей сторон:", bold=True, size=10)
    sign_rows = []
    for role in form_meta["roles"]:
        rep = (spec.get("representatives") or {}).get(role["key"]) or {}
        sign_rows.append([role["label"], rep.get("person") or "______________",
                          "____________________"])
    _table(doc, ["Полномочия", "Ф.И.О.", "Подпись, дата"], sign_rows,
           widths=[7.0, 4.8, 5.6])

    if validation["issues"]:
        doc.add_paragraph()
        _p(doc, "Замечания к черновику (устранить до передачи на подпись):", bold=True, size=9)
        for issue in validation["issues"]:
            _p(doc, f"[{issue['severity']}] {issue['text']}", size=8,
               italic=issue["severity"] == "warning")

    missing = [i["text"] for i in validation["blocking"]]
    docx_path = out / f"aosr_{stem}.docx"
    doc.save(docx_path)

    return {
        "docx": str(docx_path),
        "act_number": act_number,
        "act_date": date_str,
        "object": spec.get("object_name"),
        "work": spec.get("work"),
        "attachments": len(spec.get("attachments") or []),
        "form": {"id": status_meta["form"],
                 **{k: status_meta[k] for k in ("short", "legal_basis", "status",
                                                "active_on_date", "warning")}},
        "work_types": work_types,
        "status": "draft_complete" if validation["can_transfer"] else "draft_incomplete",
        "can_transfer": validation["can_transfer"],
        "validation": validation,
        "missing_fields": missing,
        "note": "черновик: подпись, штампы и регистрация — только человеком",
    }


def _with_location(spec: dict) -> str:
    work = spec.get("work") or ""
    loc = spec.get("location") or {}
    if isinstance(loc, dict):
        parts = [f"{k}: {v}" for k, v in loc.items() if v]
        if parts:
            return f"{work} ({', '.join(parts)})"
    elif loc:
        return f"{work} ({loc})"
    return work


def aosr_to_markdown(result: dict) -> str:
    status = ("черновик заполнен, блокеров нет — можно передавать на подпись человеку"
              if result["can_transfer"]
              else "черновик неполный — передавать на подпись нельзя")
    lines = [f"# ЧЕРНОВИК АОСР № {result.get('act_number') or '—'} от {result.get('act_date')}", "",
             f"- объект: {result.get('object') or '—'}",
             f"- работы: {result.get('work') or '—'}",
             f"- форма: {result['form']['short']}",
             f"- приложений ИД: {result.get('attachments')}",
             f"- статус: **{status}** (подпись — только человек)",
             f"- DOCX: `{Path(result['docx']).name}`"]
    if result["work_types"]:
        lines.append(f"- вид работ: {', '.join(result['work_types'])}")
    blocking = result["validation"]["blocking"]
    warnings = result["validation"]["warnings"]
    if blocking:
        lines += ["", f"**Блокеры ({len(blocking)}):**"]
        lines += [f"- {i['text']}" for i in blocking]
    if warnings:
        lines += ["", f"**Замечания ({len(warnings)}):**"]
        lines += [f"- {i['text']}" for i in warnings]
    return "\n".join(lines) + "\n"
