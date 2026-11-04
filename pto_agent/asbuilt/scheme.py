# -*- coding: utf-8 -*-
"""Исполнительная схема: лист чертежа + титул, штамп и таблица объёмов.

Что делает
----------
Берёт DXF, который мы уже умеем читать (`pto_agent.cad`), рендерит лист в PNG и собирает
DOCX-документ исполнительной схемы: шапка с объектом и шифром, сам лист, таблица объёмов
из блоков `ПОЗ_ОБЪЕМ`, блок подписей (разработал / проверил / согласовано) и примечание
о том, из чего схема собрана (акт КС-2, прогон выпуска, номер листа).

Чего не делает
--------------
Не рисует «красивый» лист заново: если в DXF есть рамка и штамп, они остаются как есть —
исполнительная схема должна выглядеть как чертёж, а не как отчёт. Не подписывает:
строки подписей пустые, заполняет человек или ЭП (pyHanko — отдельный контур).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from ..cad.drawing import Drawing
from ..cad.render import render_png
from ..cad.takeoff import takeoff


def _cell_text(cell, text: str, bold: bool = False, size: int = 10):
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run("—" if text in (None, "") else str(text))
    run.bold = bold
    run.font.size = Pt(size)
    return cell


def build_scheme(drawing_path: str | Path, out_dir: str | Path, *, object_name: str,
                 work: str, code: str = "", act_ref: str = "", executor: str = "",
                 reviewer: str = "", organization: str = "", drawing_no: str = "",
                 scheme_date: str | None = None, notes: str = "",
                 with_image: bool = True, dpi: int = 110) -> dict:
    """Собрать исполнительную схему: `out_dir/scheme_<code>.docx` + PNG листа."""
    drawing_path = Path(drawing_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    scheme_date = scheme_date or date.today().isoformat()
    stem = (code or drawing_path.stem).replace(" ", "_").replace("/", "-")

    drawing = Drawing.open(drawing_path)
    positions = takeoff(drawing)
    png_path = None
    if with_image:
        png_path = render_png(drawing, out / f"scheme_{stem}.png", dpi=dpi)

    doc = Document()
    section = doc.sections[0]
    section.left_margin = section.right_margin = Cm(1.5)
    section.top_margin = section.bottom_margin = Cm(1.2)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("ИСПОЛНИТЕЛЬНАЯ СХЕМА")
    run.bold = True
    run.font.size = Pt(16)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub_run = subtitle.add_run(work or "—")
    sub_run.font.size = Pt(12)

    meta = [
        ("Объект", object_name),
        ("Организация", organization),
        ("Шифр схемы", code),
        ("Лист чертежа", drawing_no or drawing_path.name),
        ("Акт КС-2", act_ref),
        ("Дата составления", scheme_date),
    ]
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for key, value in meta:
        row = table.add_row().cells
        _cell_text(row[0], key, bold=True)
        _cell_text(row[1], value)
    for row in table.rows:
        row.cells[0].width = Cm(4.5)
        row.cells[1].width = Cm(13.0)

    if png_path is not None:
        doc.add_paragraph()
        doc.add_picture(str(png_path), width=Cm(17.5))
        caption = doc.add_paragraph()
        caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption_run = caption.add_run(f"Лист {drawing_no or drawing_path.name} · "
                                      f"объёмов на схеме: {len(positions)}")
        caption_run.italic = True
        caption_run.font.size = Pt(9)

    doc.add_paragraph()
    head = doc.add_paragraph()
    head_run = head.add_run("Ведомость объёмов работ")
    head_run.bold = True
    head_run.font.size = Pt(12)
    if positions:
        vt = doc.add_table(rows=1, cols=5)
        vt.style = "Table Grid"
        for cell, name in zip(vt.rows[0].cells,
                              ("№", "Шифр расценки", "Наименование", "Объём", "Ед.")):
            _cell_text(cell, name, bold=True, size=9)
        for i, pos in enumerate(positions, start=1):
            cells = vt.add_row().cells
            _cell_text(cells[0], i, size=9)
            _cell_text(cells[1], pos.get("code"), size=9)
            _cell_text(cells[2], pos.get("name"), size=9)
            _cell_text(cells[3], f"{pos['qty']:g}" if pos.get("qty") else "не оценено", size=9)
            _cell_text(cells[4], pos.get("unit"), size=9)
    else:
        doc.add_paragraph("В чертеже нет блоков ПОЗ_ОБЪЕМ — ведомость объёмов пуста.")

    doc.add_paragraph()
    sign = doc.add_table(rows=1, cols=3)
    sign.style = "Table Grid"
    for cell, (role, who) in zip(sign.rows[0].cells,
                                 (("Разработал", executor), ("Проверил", reviewer),
                                  ("Согласовано", ""))):
        cell.text = ""
        p = cell.paragraphs[0]
        p.add_run(f"{role}\n").bold = True
        p.add_run("_______________ / " + (who or "_______________") + " /")

    if notes:
        doc.add_paragraph()
        note = doc.add_paragraph()
        note_run = note.add_run("Примечание: " + notes)
        note_run.font.size = Pt(9)

    docx_path = out / f"scheme_{stem}.docx"
    doc.save(docx_path)

    missing = [k for k, v in (("шифр схемы", code), ("акт КС-2", act_ref),
                              ("исполнитель", executor), ("проверил", reviewer))
               if not v]
    return {
        "docx": str(docx_path),
        "png": str(png_path) if png_path else None,
        "drawing": str(drawing_path),
        "positions": len(positions),
        "volumes_estimated": sum(1 for p in positions if p.get("qty")),
        "missing_fields": missing,
        "object": object_name,
        "work": work,
        "act_ref": act_ref,
        "date": scheme_date,
    }


def scheme_to_markdown(result: dict) -> str:
    lines = [f"# Исполнительная схема: {result.get('work') or '—'}", "",
             f"- объект: {result.get('object') or '—'}",
             f"- лист: `{Path(result['drawing']).name}` · акт: {result.get('act_ref') or '—'}",
             f"- дата: {result.get('date')}",
             f"- объёмов на схеме: {result['positions']} "
             f"(с объёмом {result['volumes_estimated']})",
             f"- DOCX: `{Path(result['docx']).name}`"]
    if result.get("png"):
        lines.append(f"- лист PNG: `{Path(result['png']).name}`")
    if result.get("missing_fields"):
        lines += ["", "**Не заполнено:** " + ", ".join(result["missing_fields"]) +
                  " — печатается как «—», подписывать нельзя, пока не заполнено"]
    return "\n".join(lines) + "\n"
