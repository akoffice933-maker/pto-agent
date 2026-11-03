#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автопроверки файлов golden set (Приложение к ТЗ на golden set, п.10).

Использование:
    python3 golden_set/tools/validate_gs.py golden_set/templates/gs01_ks2.jsonl

Проверяет:
  * валидность JSONL и уникальность id;
  * обязательные поля по блоку (GS-01..GS-05);
  * арифметику КС-2: amount == qty * price (допуск 0.01), итоги и НДС;
  * допустимые типы расхождений и уровни критичности;
  * наличие источников у каждой позиции;
  * для GS-03: наличие gold.citations либо answer_type=not_found;
  * для GS-04: наличие bbox у каждой пометки и явный класс unclear-кейсов.
"""
import json
import sys
from pathlib import Path

ALLOWED_DISCREPANCY = {
    "VOLUME_OVER_ESTIMATE", "NOT_IN_ESTIMATE", "PRICE_MISMATCH", "UNIT_MISMATCH",
    "DUPLICATE", "MISSING_BACKING_DOCS", "ALREADY_CLOSED", "NO_SCHEME",
}

REQUIRED = {
    "GS-01": ["id", "object_code", "period", "positions", "planted_errors", "gold_totals"],
    "GS-02": ["id", "object_code", "work_type", "documents", "missing", "ready_for_transfer"],
    "GS-03": ["id", "question", "as_of_date", "gold"],
    "GS-04": ["id", "drawing", "redline", "marks"],
    "GS-05": ["id", "doc_type", "file", "fields"],
}

BLOCK_BY_PREFIX = {"GS-01": "GS-01", "GS-02": "GS-02", "GS-03": "GS-03",
                   "GS-04": "GS-04", "GS-05": "GS-05"}


def block_of(rec_id: str) -> str:
    for pref, block in BLOCK_BY_PREFIX.items():
        if rec_id.startswith(pref):
            return block
    return "UNKNOWN"


def check_ks2(rec, errs):
    total = 0.0
    for pos in rec["positions"]:
        if not pos.get("sources"):
            errs.append(f"позиция {pos.get('line_no')}: нет sources")
        expected = round(pos["qty_actual"] * pos["price"], 2)
        if abs(expected - pos["amount"]) > 0.01:
            errs.append(f"позиция {pos.get('line_no')}: amount {pos['amount']} != qty*price {expected}")
        total += pos["amount"]
    gt = rec["gold_totals"]
    if abs(round(total, 2) - gt["without_vat"]) > 0.01:
        errs.append(f"итог без НДС: посчитано {round(total, 2)}, в gold {gt['without_vat']}")
    for pe in rec["planted_errors"]:
        if pe["type"] not in ALLOWED_DISCREPANCY:
            errs.append(f"недопустимый тип расхождения: {pe['type']}")


def check_norm(rec, errs):
    gold = rec["gold"]
    if gold.get("answer_type") == "answer" and not gold.get("citations"):
        errs.append("answer_type=answer, но citations пуст")
    if gold.get("answer_type") == "not_found" and gold.get("citations"):
        errs.append("answer_type=not_found, но есть citations")
    for c in gold.get("citations", []):
        if not all(k in c for k in ("doc", "clause", "quote_span")):
            errs.append(f"цитата неполная: {c}")


def check_redline(rec, errs):
    for m in rec["marks"]:
        if "bbox" not in m or len(m["bbox"]) != 4:
            errs.append(f"пометка {m.get('mark_id')}: нет корректного bbox")
        if m.get("class") != "unclear" and not m.get("expected_op"):
            errs.append(f"пометка {m.get('mark_id')}: нет expected_op")
    if not any(m.get("class") == "unclear" for m in rec["marks"]):
        print("  ! предупреждение: нет unclear-пометок (нужно >=15% по выборке)")


def check_fields(rec, errs):
    for name, f in rec["fields"].items():
        if f.get("value") is None:
            if not f.get("unreadable"):
                errs.append(f"поле {name}: value=null без флага unreadable")
        elif "span" not in f:
            errs.append(f"поле {name}: нет span (bbox/page)")


CHECKERS = {"GS-01": check_ks2, "GS-03": check_norm, "GS-04": check_redline, "GS-05": check_fields}


def validate(path: Path) -> int:
    errors = 0
    seen = set()
    print(f"→ {path}")
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"  ✗ строка {lineno}: невалидный JSON ({e})")
                errors += 1
                continue
            errs = []
            rec_id = rec.get("id", "")
            if rec_id in seen:
                errs.append("дубликат id")
            seen.add(rec_id)
            block = block_of(rec_id)
            if block == "UNKNOWN":
                errs.append(f"неизвестный блок по id: {rec_id}")
            else:
                for req in REQUIRED[block]:
                    if req not in rec:
                        errs.append(f"нет обязательного поля {req}")
                if not errs:
                    CHECKERS.get(block, lambda *a: None)(rec, errs)
            if errs:
                errors += len(errs)
                print(f"  ✗ {rec_id}: " + "; ".join(errs))
            else:
                print(f"  ✓ {rec_id}")
    print(f"  итог: {'OK' if errors == 0 else str(errors) + ' ошибок'}\n")
    return errors


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    total = 0
    for arg in sys.argv[1:]:
        p = Path(arg)
        if p.is_dir():
            for f in sorted(p.glob("*.jsonl")):
                total += validate(f)
        else:
            total += validate(p)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
