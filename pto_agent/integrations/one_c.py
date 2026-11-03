# -*- coding: utf-8 -*-
"""Обмен с 1С / БИТ.Строительство: CSV в обе стороны без платформенных зависимостей.

Зачем именно файлы, а не «интеграция»
-------------------------------------
БИТ.Строительство — это учётная система на 1С: там живут объекты, сметы, журналы, КС-2/КС-3,
бюджеты и архив ИД. Мы не заменяем её и не пытаемся стать второй учётной системой: наш слой —
проверка данных и деньги под риском. Поэтому граница обмена — **CSV-файл**, который 1С умеет
и отдавать, и принимать штатно (обработка «Загрузка данных из табличного документа»), а мы —
читать и писать обычными средствами Python.

Что даёт обмен:
  * **вход:** смета (из Гранд-Сметы или 1С) → наши строки для сверки с журналом и актом;
  * **вход:** реестр актов КС-2 из 1С → портфель и «деньги под риском» по объекту;
  * **выход:** проверенная КС-2 с пометками «что подтверждено, что урезано» → загрузка в 1С.

Формат
------
* кодировка `utf-8-sig` (BOM — чтобы Excel и 1С видели русские буквы), разделитель `;`,
  десятичная запятая, пробелы-разделители тысяч допускаются при чтении;
* шапка на русском, но чтение терпит синонимы (`Код`/`Шифр`/`Code`, `ЕдИзм`/`Единица`,
  `Кол-во`/`Количество`/`Объем`), регистр и лишние пробелы игнорируются;
* деньги и объёмы считаются на нашей стороне и сверяются с суммой в файле, если она есть.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")
DELIMITERS = (";", "\t", ",", "|")

# каноническое имя колонки → синонимы в выгрузках 1С / Гранд-Сметы
ALIASES: dict[str, tuple[str, ...]] = {
    "code": ("код", "шифр", "code", "кодрасценки", "шифррасценки", "позиция", "номер"),
    "name": ("наименование", "наименованиеработ", "работа", "name", "описание", "вид работ"),
    "unit": ("едизм", "единица", "единицаизмерения", "ед", "uom", "unit", "едизм."),
    "qty": ("количество", "объем", "объём", "колво", "кол-во", "qty", "quantity"),
    "price": ("цена", "ценазаединицу", "стоимостьединицы", "price", "ценаруб"),
    "amount": ("сумма", "суммабезндс", "стоимость", "всего", "amount", "суммаруб"),
    "act_id": ("акт", "номер акт", "актid", "документ", "act", "actid", "кс-2", "кс2"),
    "period": ("период", "месяц", "period"),
    "act_date": ("дата", "датаакта", "date", "датадокумента"),
    "status": ("статус", "status", "состояние"),
}

LABELS = {"code": "Код", "name": "Наименование", "unit": "ЕдИзм", "qty": "Количество",
          "price": "Цена", "amount": "Сумма", "act_id": "Акт", "period": "Период"}

KS2_COLUMNS = ("Код", "Наименование", "ЕдИзм", "КоличествоАкта", "КоличествоЖурнала",
               "Цена", "СуммаБезНДС")
ACT_COLUMNS = ("Акт", "Период", "ДатаАкта", "Статус", "Код", "Наименование", "ЕдИзм",
               "Количество", "Цена", "СуммаБезНДС")


class OneCError(RuntimeError):
    """Файл не читается или в нём нет обязательных колонок."""


# ─────────────────────────── чтение CSV ──────────────────────────────────
def read_csv(path: str | Path) -> tuple[list[dict], dict]:
    """Прочитать CSV, подобрать кодировку и разделитель. Возвращает (строки, мета)."""
    path = Path(path)
    if not path.exists():
        raise OneCError(f"нет файла обмена: {path}")
    raw = path.read_bytes()
    text, encoding = None, None
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise OneCError(f"не удалось определить кодировку файла {path.name}")

    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = max(DELIMITERS, key=first.count)
    if first.count(delimiter) == 0:
        delimiter = ";"
    rows = [dict(r) for r in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
    meta = {"file": str(path), "encoding": encoding, "delimiter": delimiter,
            "columns": list(rows[0].keys()) if rows else []}
    return rows, meta


def _norm_header(name: str) -> str:
    return re.sub(r"[\s._\-]+", "", str(name or "").strip().lower())


def _pick(row: dict, canonical: str):
    """Значение колонки по каноническому имени с учётом синонимов."""
    wanted = ALIASES.get(canonical, (canonical,))
    for key, value in row.items():
        if _norm_header(key) in {_norm_header(w) for w in wanted}:
            if value not in (None, ""):
                return value
    return None


def _num(value) -> float | None:
    """«1 240,50» / «1,240.50» / «1240.5» → float. Мусор → None (не ноль!)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("\xa0", "").replace(" ", "").replace("₽", "")
    if s.count(",") and s.count("."):
        if s.rfind(",") > s.rfind("."):      # 1.240,50 — европейский вид
            s = s.replace(".", "").replace(",", ".")
        else:                                # 1,240.50 — американский
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}".replace(".", ",")


def _require(rows: list[dict], canonical: str, path: str) -> None:
    if not rows:
        raise OneCError(f"файл {path} пуст")
    if all(_pick(r, canonical) is None for r in rows):
        cols = ", ".join(str(c) for c in rows[0])
        label = LABELS.get(canonical, canonical)
        raise OneCError(f"в файле {path} нет колонки «{label}» (есть: {cols})")


# ─────────────────────────── выгрузка для 1С ─────────────────────────────
def export_ks2_csv(summary: dict, path: str | Path, *, quantity: str = "claim") -> dict:
    """Проверенная КС-2 → CSV для загрузки в 1С/БИТ.

    `quantity`: `claim` — объём к предъявлению (`qty_claim`, по умолчанию),
    `journal` — объём по журналу, `estimate` — объём сметы. Сумма пересчитывается
    по цене сметы, чтобы в 1С не попало число, которое ниоткуда не следует.
    """
    key = {"claim": "qty_claim", "journal": "qty_journal", "estimate": "estimate_qty"}[quantity]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows, total = [], 0.0
    for line in summary.get("lines") or []:
        qty = _num(line.get(key))
        price = _num(line.get("price"))
        amount = round(qty * price, 2) if qty is not None and price is not None else None
        total += amount or 0.0
        rows.append({
            "Код": line.get("code") or "",
            "Наименование": line.get("name") or "",
            "ЕдИзм": line.get("unit") or "",
            "КоличествоАкта": _fmt(qty, 3),
            "КоличествоЖурнала": _fmt(_num(line.get("qty_journal")), 3),
            "Цена": _fmt(price),
            "СуммаБезНДС": _fmt(amount),
        })
    _write_csv(path, KS2_COLUMNS, rows)
    return {"file": str(path), "rows": len(rows), "amount": round(total, 2),
            "object": summary.get("object"), "period": summary.get("period"),
            "quantity": quantity}


def export_acts_csv(acts_doc: dict, path: str | Path) -> dict:
    """Реестр актов (из 1С или наш) → CSV: одна строка на позицию акта."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows, total = [], 0.0
    for act in acts_doc.get("acts") or []:
        for pos in act.get("positions") or [{"code": None, "qty": None, "price": None}]:
            qty = _num(pos.get("qty"))
            price = _num(pos.get("price"))
            amount = _num(pos.get("amount_rub"))
            if amount is None and qty is not None and price is not None:
                amount = round(qty * price, 2)
            total += amount or 0.0
            rows.append({
                "Акт": act.get("act_id") or "",
                "Период": act.get("period") or "",
                "ДатаАкта": act.get("act_date") or "",
                "Статус": act.get("status") or "",
                "Код": pos.get("code") or "",
                "Наименование": pos.get("name") or "",
                "ЕдИзм": pos.get("unit") or "",
                "Количество": _fmt(qty, 3),
                "Цена": _fmt(price),
                "СуммаБезНДС": _fmt(amount),
            })
    _write_csv(path, ACT_COLUMNS, rows)
    return {"file": str(path), "rows": len(rows), "amount": round(total, 2),
            "acts": len(acts_doc.get("acts") or [])}


def _write_csv(path: Path, columns, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), delimiter=";",
                                quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)


# ─────────────────────────── чтение из 1С ────────────────────────────────
def import_smeta_csv(path: str | Path) -> dict:
    """Смета из 1С/Гранд-Сметы → строки в форме `summary["lines"]`."""
    rows, meta = read_csv(path)
    _require(rows, "name", meta["file"])
    lines, warnings = [], []
    for i, row in enumerate(rows, start=2):     # 2 — первая строка данных после шапки
        code = _pick(row, "code")
        name = _pick(row, "name")
        qty = _num(_pick(row, "qty"))
        price = _num(_pick(row, "price"))
        amount = _num(_pick(row, "amount"))
        if price is None and amount is not None and qty:
            price = round(amount / qty, 2)
        if qty is None:
            warnings.append({"row": i, "code": code, "message": "нет количества — "
                                                               "позиция остаётся не оценённой"})
        if not code and not name:
            warnings.append({"row": i, "message": "строка без шифра и наименования пропущена"})
            continue
        lines.append({
            "code": str(code).strip() if code else None,
            "name": str(name).strip() if name else "",
            "unit": str(_pick(row, "unit") or "").strip(),
            "estimate_qty": qty,
            "price": price,
            "amount_estimate": amount if amount is not None else
            (round(qty * price, 2) if qty and price else None),
        })
    return {"lines": lines, "count": len(lines), "warnings": warnings, "meta": meta}


def import_acts_csv(path: str | Path) -> dict:
    """Реестр актов из 1С → структура `acts.json` для портфеля."""
    rows, meta = read_csv(path)
    _require(rows, "code", meta["file"])
    acts: dict[str, dict] = {}
    warnings = []
    for i, row in enumerate(rows, start=2):
        act_id = (_pick(row, "act_id") or "КС-2 (без номера)")
        act_id = str(act_id).strip()
        qty = _num(_pick(row, "qty"))
        price = _num(_pick(row, "price"))
        amount = _num(_pick(row, "amount"))
        if amount is None and qty is not None and price is not None:
            amount = round(qty * price, 2)
        if qty is None:
            warnings.append({"row": i, "act": act_id,
                             "message": "нет объёма — позиция не оценена, в сумму акта не входит"})
        act = acts.setdefault(act_id, {
            "act_id": act_id,
            "period": str(_pick(row, "period") or "").strip(),
            "act_date": str(_pick(row, "act_date") or "").strip(),
            "status": str(_pick(row, "status") or "").strip(),
            "positions": [],
            "amount_rub": 0.0,
        })
        act["positions"].append({
            "code": str(_pick(row, "code") or "").strip() or None,
            "name": str(_pick(row, "name") or "").strip(),
            "unit": str(_pick(row, "unit") or "").strip(),
            "qty": qty,
            "price": price,
            "amount_rub": amount,
        })
        act["amount_rub"] = round(act["amount_rub"] + (amount or 0.0), 2)
    ordered = list(acts.values())
    for act in ordered:
        act["positions_count"] = len(act["positions"])
        if not act["period"]:
            warnings.append({"act": act["act_id"], "message": "не указан период акта"})
    return {"acts": ordered, "count": len(ordered), "warnings": warnings, "meta": meta}


def acts_document(imported: dict, object_name: str = "из 1С") -> dict:
    """Обернуть импортированный реестр в структуру `acts.json` (объект, договор, акты)."""
    return {
        "object": object_name,
        "contract": None,
        "generated": None,
        "note": "реестр актов импортирован из 1С/БИТ по CSV",
        "acts": imported.get("acts") or [],
    }
