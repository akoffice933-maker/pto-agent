# -*- coding: utf-8 -*-
"""Экспорт результатов: черновик КС-2, таблица расхождений, остатки."""
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .reconcile import Reconciliation

NAVY = "1F3864"
HDR = Font(bold=True, color="FFFFFF", size=10)
BOLD = Font(bold=True, size=10)
BASE = Font(size=10)
FILL = PatternFill("solid", fgColor=NAVY)
WARN = PatternFill("solid", fgColor="FDF1EC")
GREY = PatternFill("solid", fgColor="F4F6FA")
THIN = Side(style="thin", color="D0D7E4")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
MONEY = '#,##0.00'
QTY = '#,##0.###'


def _sheet(wb, title, headers, widths):
    ws = wb.active if wb.active.max_row == 1 else wb.create_sheet(title)
    ws.title = title[:31]
    for i, h in enumerate(headers, 1):
        c = ws.cell(1, i, h)
        c.font, c.fill, c.border = HDR, FILL, BOX
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]
    ws.freeze_panes = "A2"
    return ws


def export_ks2(rec: Reconciliation, path: Path, vat_rate: float = 20.0) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "КС-2"
    ws["A1"] = "АКТ О ПРИЁМКЕ ВЫПОЛНЕННЫХ РАБОТ (форма № КС-2) — ЧЕРНОВИК"
    ws["A1"].font = Font(bold=True, size=13, color=NAVY)
    ws["A2"] = f"Объект: {rec.object_name}"
    ws["A3"] = f"Отчётный период: {rec.period[0]:%d.%m.%Y} — {rec.period[1]:%d.%m.%Y}"
    ws["A4"] = (f"Сформировано автоматически ИИ-агентом ПТО {date.today():%d.%m.%Y}. "
                "Требует проверки и подписи инженера ПТО.")
    ws["A4"].font = Font(italic=True, size=9, color="666666")

    headers = ["№", "Шифр расценки", "Наименование работ", "Ед. изм.",
               "Кол-во по журналу", "К предъявлению", "Цена, руб.",
               "Стоимость к предъявлению, руб.", "Ограничения и флаги"]
    widths = [5, 20, 46, 9, 14, 14, 13, 17, 40]
    start = 6
    for i, h in enumerate(headers, 1):
        c = ws.cell(start, i, h)
        c.font, c.fill, c.border = HDR, FILL, BOX
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]

    row = start + 1
    n = 0
    for plan in rec.act_plan():
        line = plan["line"]
        n += 1
        flags = list(plan["restrictions"])
        if line.duplicates:
            flags.append(f"исключены дубли: {line.duplicates}")
        if not line.has_docs:
            flags.append("нет ИД")
        values = [n, line.item.code, line.item.name, line.item.unit,
                  line.qty_journal, plan["qty_claim"], line.item.price,
                  plan["amount_claim"], "; ".join(flags)]
        for i, v in enumerate(values, 1):
            c = ws.cell(row, i, v)
            c.border, c.font = BOX, BASE
            c.alignment = Alignment(vertical="top", wrap_text=(i == 9))
            if i in (5, 6):
                c.number_format = QTY
            if i in (7, 8):
                c.number_format = MONEY
            if flags and i == 9:
                c.fill = WARN
        row += 1
    ws.freeze_panes = f"A{start + 1}"

    total = round(sum(l.amount_act for l in rec.lines_with_qty), 2)
    safe = rec.amount_act_safe
    vat = round(total * vat_rate / 100, 2)
    rows = [("Итого по журналу (все позиции)", total),
            ("К предъявлению после снятия ограничений", safe),
            ("Заблокировано замечаниями", rec.amount_blocked),
            (f"НДС {vat_rate:.0f} % (от суммы к предъявлению)",
             round(safe * vat_rate / 100, 2)),
            ("Всего с НДС", round(safe * (1 + vat_rate / 100), 2))]
    for label, value in rows:
        ws.cell(row, 3, label).font = BOLD
        c = ws.cell(row, 8, value)
        c.number_format = MONEY
        c.font = BOLD
        row += 1
    ws.cell(row + 1, 3, "Сдал (подрядчик) ______________").font = BASE
    ws.cell(row + 2, 3, "Принял (заказчик) ______________").font = BASE

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def export_discrepancies(rec: Reconciliation, path: Path) -> Path:
    wb = Workbook()
    headers = ["№", "Критичность", "Тип", "Шифр", "Наименование", "Значения",
               "Источники", "Предлагаемое действие", "Решение человека"]
    widths = [5, 12, 22, 20, 42, 34, 26, 40, 18]
    ws = _sheet(wb, "Расхождения", headers, widths)
    for i, d in enumerate(rec.discrepancies, 1):
        vals = "; ".join(f"{k}={v}" for k, v in d.values.items())
        row = [i, d.severity, d.type, d.code or "—", d.name, vals,
               " | ".join(d.sources), d.suggested_action, ""]
        for j, v in enumerate(row, 1):
            c = ws.cell(i + 1, j, v)
            c.border, c.font = BOX, BASE
            c.alignment = Alignment(vertical="top", wrap_text=True)
            if d.severity == "critical":
                c.fill = WARN
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def export_remainder(rec: Reconciliation, path: Path) -> Path:
    wb = Workbook()
    headers = ["Шифр", "Наименование", "Ед.", "Смета", "Закрыто ранее",
               "Выполнено (журнал)", "Остаток", "Цена", "Остаток, руб."]
    widths = [20, 46, 8, 12, 14, 18, 12, 12, 15]
    ws = _sheet(wb, "Остатки", headers, widths)
    for i, line in enumerate(rec.lines, 1):
        row = [line.item.code, line.item.name, line.item.unit, line.item.qty,
               line.qty_prev_closed, line.qty_journal, line.qty_remainder,
               line.item.price, round(max(line.qty_remainder, 0) * line.item.price, 2)]
        for j, v in enumerate(row, 1):
            c = ws.cell(i + 1, j, v)
            c.border, c.font = BOX, BASE
            if j in (4, 5, 6, 7):
                c.number_format = QTY
            if j in (8, 9):
                c.number_format = MONEY
            if line.qty_remainder < 0:
                c.fill = WARN
    n = len(rec.lines) + 2
    ws.cell(n, 2, "Итого остаток, руб.").font = BOLD
    c = ws.cell(n, 9, rec.amount_remainder)
    c.number_format, c.font = MONEY, BOLD
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def export_discrepancies_csv(rec: Reconciliation, path: Path) -> Path:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["severity", "type", "code", "name", "values", "sources", "action"])
        for d in rec.discrepancies:
            w.writerow([d.severity, d.type, d.code or "", d.name,
                        "; ".join(f"{k}={v}" for k, v in d.values.items()),
                        " | ".join(d.sources), d.suggested_action])
    return path
