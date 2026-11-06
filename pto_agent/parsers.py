# -*- coding: utf-8 -*-
"""Чтение документов (XLSX) и нормализация единиц измерения.

Документы приходят извне (смета от подрядчика, журнал с площадки), поэтому чтен**ие
ограничено**: размер файла, число строк, режим только-чтение. И — главное — **битая ячейка
не превращается в ноль молча**: нечисловое количество и нераспознанная дата попадают в
`problems`, а не в «0.0» и не в «сегодняшнее число». Раньше было именно так: смета с
испорченной ценой давала нулевую сумму без единого предупреждения.
"""
import os
import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from .models import ClosedVolume, EstimateItem, JournalEntry

# Пределы на входной файл. Значения можно переопределить переменными окружения — но не
# «на лету» в коде: раздувание лимита должно быть осознанным решением администратора.
MAX_FILE_BYTES = int(os.getenv("PTO_MAX_XLSX_BYTES", 25 * 1024 * 1024))     # 25 МБ
MAX_ROWS = int(os.getenv("PTO_MAX_XLSX_ROWS", 200_000))                     # строк на книгу
MAX_SHEETS = int(os.getenv("PTO_MAX_XLSX_SHEETS", 20))


class WorkbookTooLarge(ValueError):
    """Файл или книга выходят за пределы, после которых чтение превращается в атаку на память."""


def workbook_problems() -> list[str]:
    """Пустой список — заглушка для единообразия API отчётов."""
    return []

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


def _parse_number(value) -> float | None:
    """Число или None. Пустая ячейка — 0.0 (это нормально), текст-мусор — None (это ошибка)."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _num(value) -> float:
    """Обратная совместимость: мусор → 0.0. Для новых мест используйте `_parse_number`."""
    parsed = _parse_number(value)
    return 0.0 if parsed is None else parsed


def _parse_date(value) -> date | None:
    """Дата или None. Раньше нераспознанный текст молча становился сегодняшним числом."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _date(value) -> date:
    return _parse_date(value) or date.today()


def _check_file(path: Path) -> None:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"нет файла: {path.name}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise WorkbookTooLarge(
            f"{path.name}: {size / 1024 / 1024:.1f} МБ больше предела "
            f"{MAX_FILE_BYTES / 1024 / 1024:.0f} МБ — чтение остановлено")


def _rows(path: Path, problems: list[dict] | None = None):
    """Строки активного листа. Читаем потоково и с ограничениями.

    Раньше книга грузилась целиком (`list(ws.iter_rows())`) в обычном режиме: файл с
    миллионом строк съедал память до того, как кто-то успевал сказать «нет».
    """
    path = Path(path)
    _check_file(path)
    wb = load_workbook(path, data_only=True, read_only=True, keep_links=False)
    try:
        if len(wb.worksheets) > MAX_SHEETS:
            raise WorkbookTooLarge(
                f"{path.name}: листов {len(wb.worksheets)} больше предела {MAX_SHEETS}")
        ws = wb.active
        count = 0
        for index, row in enumerate(ws.iter_rows(values_only=True)):
            count += 1
            if count > MAX_ROWS + 1:
                raise WorkbookTooLarge(
                    f"{path.name}: строк больше предела {MAX_ROWS} — чтение остановлено")
            if index == 0:
                continue
            yield row
    finally:
        wb.close()


def read_estimate_ex(path: Path) -> tuple[list[EstimateItem], list[dict]]:
    """Смета + список замечаний чтения (битые числа не становятся нулями молча)."""
    items: list[EstimateItem] = []
    problems: list[dict] = []
    for i, r in enumerate(_rows(path, problems), start=2):
        if not r or not r[1]:
            continue
        qty = _parse_number(r[3]) if len(r) > 3 else 0.0
        price = _parse_number(r[4]) if len(r) > 4 else 0.0
        row_problems = []
        if qty is None:
            row_problems.append("количество не число")
            qty = 0.0
        if price is None:
            row_problems.append("цена не число")
            price = 0.0
        if row_problems:
            problems.append({"where": Path(path).name, "row": i,
                             "what": ", ".join(row_problems),
                             "raw": [str(r[3])[:40] if len(r) > 3 else "",
                                     str(r[4])[:40] if len(r) > 4 else ""],
                             "effect": "позиция вошла с нулём — сумма по ней не посчитана"})
        items.append(EstimateItem(
            code=str(r[0] or "").strip(),
            name=str(r[1]).strip(),
            unit=norm_unit(r[2]),
            qty=qty,
            price=price,
        ))
    return items, problems


def read_estimate(path: Path) -> list[EstimateItem]:
    return read_estimate_ex(path)[0]


def read_journal_ex(path: Path) -> tuple[list[JournalEntry], list[dict]]:
    out: list[JournalEntry] = []
    problems: list[dict] = []
    for i, r in enumerate(_rows(path, problems), start=2):
        if not r or not r[2]:
            continue
        when = _parse_date(r[0])
        if when is None:
            # Раньше здесь подставлялось сегодняшнее число — то есть выдуманная дата работ
            problems.append({"where": Path(path).name, "row": i,
                             "what": f"дата не распознана: {str(r[0])[:40]!r}",
                             "effect": "строка журнала пропущена, а не отнесена к сегодняшнему дню"})
            continue
        out.append(JournalEntry(
            date=when,
            code=str(r[1] or "").strip(),
            name=str(r[2]).strip(),
            unit=norm_unit(r[3]),
            qty=_num(r[4]),
            zone=str(r[5] or "").strip(),
            executor=str(r[6] or "").strip(),
            doc=str(r[7] or "").strip(),
            row=i,
        ))
    return out, problems


def read_journal(path: Path) -> list[JournalEntry]:
    return read_journal_ex(path)[0]


def read_closed_ex(path: Path) -> tuple[list[ClosedVolume], list[dict]]:
    out: list[ClosedVolume] = []
    problems: list[dict] = []
    for i, r in enumerate(_rows(path, problems), start=2):
        if not r or not r[1]:
            continue
        qty = _parse_number(r[3]) if len(r) > 3 else 0.0
        if qty is None:
            problems.append({"where": Path(path).name, "row": i,
                             "what": "объём не число",
                             "raw": [str(r[3])[:40]],
                             "effect": "закрытый объём принят за 0 — остаток завышен"})
            qty = 0.0
        out.append(ClosedVolume(
            code=str(r[0] or "").strip(),
            name=str(r[1]).strip(),
            unit=norm_unit(r[2]),
            qty=qty,
            act=str(r[4] or "").strip(),
            period=str(r[5] or "").strip(),
        ))
    return out, problems


def read_closed(path: Path) -> list[ClosedVolume]:
    return read_closed_ex(path)[0]
