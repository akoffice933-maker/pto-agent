# -*- coding: utf-8 -*-
"""Чтение документов (XLSX) и нормализация единиц измерения."""
import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from .models import ClosedVolume, EstimateItem, JournalEntry

UNIT_MAP = {
    "м3": "м3", "м³": "м3", "m3": "м3", "куб.м": "м3", "куб м": "м3",
    "м2": "м2", "м²": "м2", "m2": "м2", "кв.м": "м2", "кв м": "м2",
    "м": "м", "м.п.": "м", "п.м.": "м", "пог.м": "м", "m": "м",
    "шт": "шт", "шт.": "шт", "pcs": "шт", "компл": "шт", "компл.": "шт",
    "т": "т", "тн": "т", "тонн": "т", "t": "т",
}


def norm_unit(value: str) -> str:
    s = str(value or "").strip().lower().replace(" ", "")
    return UNIT_MAP.get(s, s)


def norm_text(value: str) -> str:
    s = str(value or "").lower().replace("ё", "е")
    s = re.sub(r"[^0-9a-zа-я]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_code(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _num(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return date.today()


def _rows(path: Path):
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    return rows[1:]


def read_estimate(path: Path) -> list[EstimateItem]:
    items = []
    for r in _rows(path):
        if not r or not r[1]:
            continue
        items.append(EstimateItem(
            code=str(r[0] or "").strip(),
            name=str(r[1]).strip(),
            unit=norm_unit(r[2]),
            qty=_num(r[3]),
            price=_num(r[4]),
        ))
    return items


def read_journal(path: Path) -> list[JournalEntry]:
    out = []
    for i, r in enumerate(_rows(path), start=2):
        if not r or not r[2]:
            continue
        out.append(JournalEntry(
            date=_date(r[0]),
            code=str(r[1] or "").strip(),
            name=str(r[2]).strip(),
            unit=norm_unit(r[3]),
            qty=_num(r[4]),
            zone=str(r[5] or "").strip(),
            executor=str(r[6] or "").strip(),
            doc=str(r[7] or "").strip(),
            row=i,
        ))
    return out


def read_closed(path: Path) -> list[ClosedVolume]:
    out = []
    for r in _rows(path):
        if not r or not r[1]:
            continue
        out.append(ClosedVolume(
            code=str(r[0] or "").strip(),
            name=str(r[1]).strip(),
            unit=norm_unit(r[2]),
            qty=_num(r[3]),
            act=str(r[4] or "").strip(),
            period=str(r[5] or "").strip(),
        ))
    return out
