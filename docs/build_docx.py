#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Конвертер markdown -> DOCX для ТЗ (заголовки, таблицы, код, списки)."""
import re, sys
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

SRC = sys.argv[1] if len(sys.argv) > 1 else "ТЗ_ИИ-агент_ПТО_v1.2.md"
OUT = sys.argv[2] if len(sys.argv) > 2 else "ТЗ_ИИ-агент_ПТО_v1.2.docx"

ACCENT = RGBColor(0x1F, 0x38, 0x64)
GREY = RGBColor(0x44, 0x44, 0x44)
BODY_FONT = "Calibri"
MONO_FONT = "Consolas"

doc = Document()

# --- базовые стили ---
st = doc.styles["Normal"]
st.font.name = BODY_FONT
st.font.size = Pt(10.5)
st.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
st.paragraph_format.space_after = Pt(4)
st.paragraph_format.line_spacing = 1.08

for sec in doc.sections:
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.top_margin = Cm(2.0)
    sec.bottom_margin = Cm(2.0)
    sec.left_margin = Cm(2.2)
    sec.right_margin = Cm(1.8)

for lvl, size in ((1, 16), (2, 13), (3, 11.5)):
    h = doc.styles[f"Heading {lvl}"]
    h.font.name = BODY_FONT
    h.font.size = Pt(size)
    h.font.bold = True
    h.font.color.rgb = ACCENT
    h.paragraph_format.space_before = Pt(14 if lvl == 1 else 10)
    h.paragraph_format.space_after = Pt(6)
    h.paragraph_format.keep_with_next = True


def add_page_number_footer(section):
    p = section.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    import os
    run = p.add_run(os.getenv("PTO_DOC_FOOTER", "ТЗ «ИИ-агент ПТО», версия 1.2") + "  ·  стр. ")
    run.font.size = Pt(8)
    run.font.color.rgb = GREY
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "1"
    r.append(t)
    fld.append(r)
    p._p.append(fld)


add_page_number_footer(doc.sections[0])


def shade(cell, hexcolor):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hexcolor)
    tcPr.append(shd)


def set_repeat_header(row):
    trPr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    trPr.append(el)


INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def add_inline(par, text, size=None, color=None, base_bold=False):
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            run = par.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            run = par.add_run(part[1:-1])
            run.font.name = MONO_FONT
            run.font.size = Pt((size or 10.5) - 0.5)
            run.font.color.rgb = RGBColor(0x8B, 0x1A, 0x1A)
        else:
            run = par.add_run(part)
            run.bold = base_bold
        if size and not (part.startswith("`")):
            run.font.size = Pt(size)
        if color:
            run.font.color.rgb = color


def add_table(rows):
    header, body = rows[0], rows[1:]
    n = len(header)
    t = doc.add_table(rows=1, cols=n)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = True
    hdr = t.rows[0]
    for i, htxt in enumerate(header):
        cell = hdr.cells[i]
        cell.text = ""
        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(1)
        add_inline(p, htxt, size=9.5, base_bold=True)
        for r in p.runs:
            r.bold = True
            r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        shade(cell, "1F3864")
    set_repeat_header(hdr)
    for ri, row in enumerate(body):
        cells = t.add_row().cells
        for i in range(n):
            txt = row[i] if i < len(row) else ""
            p = cells[i].paragraphs[0]
            p.paragraph_format.space_after = Pt(1)
            add_inline(p, txt, size=9.5)
            if ri % 2 == 1:
                shade(cells[i], "F2F5FA")
    # ширины колонок: пропорционально объёму текста, суммарно 16.5 см
    try:
        avail = 16.5
        weights = []
        for i in range(n):
            longest = max([len(header[i])] + [len(r[i]) for r in body if i < len(r)])
            longest = min(longest, 60)
            weights.append(max(longest, 8) ** 0.85)
        total_w = sum(weights)
        t.autofit = False
        tblPr = t._tbl.tblPr
        layout = OxmlElement("w:tblLayout")
        layout.set(qn("w:type"), "fixed")
        tblPr.append(layout)
        for i, col in enumerate(t.columns):
            w = Cm(avail * weights[i] / total_w)
            for cell in col.cells:
                cell.width = w
    except Exception as e:
        pass
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def add_code(lines):
    t = doc.add_table(rows=1, cols=1)
    t.style = "Table Grid"
    cell = t.rows[0].cells[0]
    shade(cell, "F5F5F5")
    cell.text = ""
    first = True
    for ln in lines:
        p = cell.paragraphs[0] if first else cell.add_paragraph()
        first = False
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.0
        run = p.add_run(ln if ln else " ")
        run.font.name = MONO_FONT
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0x22, 0x22, 0x22)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in line.split("|")]


def is_sep(line):
    return bool(re.match(r"^\|?[\s:\-\|]+\|[\s:\-\|]*$", line.strip())) and "-" in line


def main():
    text = open(SRC, encoding="utf-8").read()
    lines = text.split("\n")
    i = 0
    in_code = False
    code_buf = []
    table_buf = []
    pending_title_lines = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # код
        if stripped.startswith("```"):
            if in_code:
                add_code(code_buf)
                code_buf = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line.rstrip())
            i += 1
            continue

        # таблицы
        if stripped.startswith("|") and i + 1 < len(lines) and is_sep(lines[i + 1]):
            table_buf = [split_row(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_buf.append(split_row(lines[i].strip()))
                i += 1
            add_table(table_buf)
            table_buf = []
            continue

        if not stripped:
            i += 1
            continue

        if stripped == "---":
            i += 1
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            txt = m.group(2).strip()
            if level == 1 and not (len(doc.paragraphs) <= 1):
                doc.add_page_break()
            p = doc.add_heading(level=min(level, 3))
            add_inline(p, txt)
            for r in p.runs:
                r.font.color.rgb = ACCENT
                r.bold = True
            i += 1
            continue

        # списки
        if re.match(r"^[-*]\s+\[[ x]\]", stripped):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            p.paragraph_format.space_after = Pt(2)
            checked = "[x]" in stripped[:6]
            p.add_run("☑ " if checked else "☐ ")
            add_inline(p, re.sub(r"^[-*]\s+\[[ x]\]\s*", "", stripped))
            i += 1
            continue
        if re.match(r"^[-*]\s+", stripped):
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(2)
            add_inline(p, re.sub(r"^[-*]\s+", "", stripped))
            i += 1
            continue
        mnum = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if mnum:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.75)
            p.paragraph_format.first_line_indent = Cm(-0.75)
            p.paragraph_format.space_after = Pt(2)
            p.add_run(mnum.group(1) + ". ")
            add_inline(p, mnum.group(2))
            i += 1
            continue

        # обычный абзац (склеиваем переносы внутри абзаца)
        para_text = stripped
        j = i + 1
        while j < len(lines):
            nxt = lines[j].strip()
            if (not nxt or nxt.startswith(("#", "|", "```", "-", "*")) or re.match(r"^\d+\.\s", nxt)):
                break
            para_text += " " + nxt
            j += 1
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(5)
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        add_inline(p, para_text)
        i = j

    doc.save(OUT)
    print("saved", OUT)


main()
