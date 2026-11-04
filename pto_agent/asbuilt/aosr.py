# -*- coding: utf-8 -*-
"""АОСР — акт освидетельствования скрытых работ (форма РД-11-02-2006, приложение 3).

Структура акта — три раздела плюс подписи:

1. представители застройщика (техзаказчика), лица, выполнившего работы, проектировщика
   и подрядчика, с реквизитами документов о допуске;
2. перечень исполнительной документации, прилагаемой к акту (реестр ИД);
3. работы, материалы и нормативные документы, по которым выполнены работы, и заключение.

Форму заполняем из данных, а не «по образцу»: чего нет — печатается прочерком и попадает
в `missing_fields`, чтобы акт нельзя было подписать с дырами незаметно.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

REQUIRED_REPRESENTATIVES = ("customer", "contractor")
REQUIRED_FIELDS = ("object_name", "work", "start_date", "end_date")


def _p(doc, text: str, *, bold: bool = False, size: int = 10, center: bool = False):
    p = doc.add_paragraph()
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    return p


def _table(doc, header: list[str], rows: list[list[str]], widths: list[float] | None = None):
    table = doc.add_table(rows=1, cols=len(header))
    table.style = "Table Grid"
    for cell, name in zip(table.rows[0].cells, header):
        cell.text = ""
        run = cell.paragraphs[0].add_run(name)
        run.bold = True
        run.font.size = Pt(9)
    for values in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, values):
            cell.text = ""
            cell.paragraphs[0].add_run("—" if value in (None, "") else str(value)).font.size = Pt(9)
    if widths:
        for row in table.rows:
            for cell, w in zip(row.cells, widths):
                cell.width = Cm(w)
    return table


def build_aosr(spec: dict, out_dir: str | Path) -> dict:
    """Собрать АОСР по спецификации (dict из JSON/YAML) → `out_dir/aosr_<шифр>.docx`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    spec = spec or {}

    act_number = str(spec.get("act_number") or "").strip()
    stem = (act_number or "aosr").replace(" ", "_").replace("/", "-").replace("№", "N")
    date_str = spec.get("act_date") or date.today().isoformat()

    doc = Document()
    section = doc.sections[0]
    section.left_margin = section.right_margin = Cm(1.5)
    section.top_margin = section.bottom_margin = Cm(1.2)

    _p(doc, "АКТ", bold=True, size=14, center=True)
    _p(doc, "освидетельствования скрытых работ", bold=True, size=12, center=True)
    _p(doc, f"№ {act_number or '—'} от {date_str}", size=11, center=True)
    doc.add_paragraph()

    # ── раздел 1: представители ──
    _p(doc, "1. Представители сторон", bold=True, size=11)
    rows = []
    for key, label in (("customer", "Застройщик (технический заказчик)"),
                       ("builder", "Лицо, выполнившее работы"),
                       ("designer", "Лицо, подготовившее проектную документацию"),
                       ("contractor", "Лицо, осуществляющее строительство")):
        rep = (spec.get("representatives") or {}).get(key) or {}
        rows.append([label, rep.get("org"), rep.get("person"), rep.get("position"),
                     rep.get("doc")])
    _table(doc, ["Сторона", "Организация", "Ф.И.О.", "Должность", "Документ о допуске"],
           rows, widths=[3.4, 3.6, 3.2, 2.6, 4.2])

    doc.add_paragraph()
    _p(doc, "2. Исполнительная документация, предъявленная при освидетельствовании",
       bold=True, size=11)
    docs = spec.get("attachments") or []
    _table(doc, ["№", "Наименование документа", "Шифр", "Дата", "Листов"],
           [[i, d.get("name"), d.get("code"), d.get("date"), d.get("sheets")]
            for i, d in enumerate(docs, start=1)],
           widths=[1.0, 8.0, 3.6, 2.4, 2.0])

    doc.add_paragraph()
    _p(doc, "3. Работы, материалы и нормативные документы", bold=True, size=11)
    _table(doc, ["Параметр", "Значение"], [
        ["Объект", spec.get("object_name")],
        ["Работы", spec.get("work")],
        ["Период выполнения", f"{spec.get('start_date') or '—'} — {spec.get('end_date') or '—'}"],
        ["Материалы и изделия", ", ".join(spec.get("materials") or []) or None],
        ["Нормативные документы", ", ".join(spec.get("norms") or []) or None],
        ["Заключение", spec.get("conclusion")
         or "Работы выполнены в соответствии с проектной документацией; "
            "разрешается производство последующих работ"],
    ], widths=[4.6, 12.8])

    doc.add_paragraph()
    _p(doc, "Подписи сторон:", bold=True, size=10)
    sign_rows = []
    for key, label in (("customer", "Застройщик (технический заказчик)"),
                       ("builder", "Лицо, выполнившее работы"),
                       ("designer", "Проектировщик"),
                       ("contractor", "Лицо, осуществляющее строительство")):
        rep = (spec.get("representatives") or {}).get(key) or {}
        sign_rows.append([label, rep.get("person") or "______________",
                          "______________________"])
    _table(doc, ["Сторона", "Ф.И.О.", "Подпись, дата"], sign_rows,
           widths=[6.0, 5.4, 6.0])

    missing = [f for f in REQUIRED_FIELDS if not spec.get(f)]
    reps = spec.get("representatives") or {}
    missing += [f"представитель: {k}" for k in REQUIRED_REPRESENTATIVES
                if not (reps.get(k) or {}).get("person")]
    if not docs:
        missing.append("перечень ИД (без приложений акт недействителен)")

    docx_path = out / f"aosr_{stem}.docx"
    doc.save(docx_path)
    return {
        "docx": str(docx_path),
        "act_number": act_number,
        "act_date": date_str,
        "object": spec.get("object_name"),
        "work": spec.get("work"),
        "attachments": len(docs),
        "missing_fields": missing,
        "ready_to_sign": not missing,
    }


def aosr_to_markdown(result: dict) -> str:
    status = "готов к подписанию" if result["ready_to_sign"] else "подписывать нельзя"
    lines = [f"# АОСР № {result.get('act_number') or '—'} от {result.get('act_date')}", "",
             f"- объект: {result.get('object') or '—'}",
             f"- работы: {result.get('work') or '—'}",
             f"- приложений ИД: {result.get('attachments')}",
             f"- статус: **{status}**",
             f"- DOCX: `{Path(result['docx']).name}`"]
    if result["missing_fields"]:
        lines += ["", "**Не хватает для подписи:**"]
        lines += [f"- {m}" for m in result["missing_fields"]]
    return "\n".join(lines) + "\n"
