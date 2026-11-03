# -*- coding: utf-8 -*-
"""Сопоставление номенклатуры: шифр → точное наименование → нечёткое сравнение."""
from difflib import SequenceMatcher

from .models import EstimateItem, JournalEntry, MatchedLine
from .parsers import norm_code, norm_text

FUZZY_THRESHOLD = 0.72


def match_entries(items: list[EstimateItem], entries: list[JournalEntry]) -> list[MatchedLine]:
    by_code = {norm_code(i.code): i for i in items if i.code}
    by_name_unit = {(norm_text(i.name), i.unit): i for i in items}

    lines: dict[int, MatchedLine] = {id(i): MatchedLine(item=i) for i in items}
    unmatched: list[MatchedLine] = []

    for entry in entries:
        item, kind, score = None, "none", 0.0

        if entry.code and norm_code(entry.code) in by_code:
            item, kind, score = by_code[norm_code(entry.code)], "code", 1.0
        elif (norm_text(entry.name), entry.unit) in by_name_unit:
            item, kind, score = by_name_unit[(norm_text(entry.name), entry.unit)], "name", 1.0
        else:
            best, best_score = None, 0.0
            for cand in items:
                s = SequenceMatcher(None, norm_text(entry.name), norm_text(cand.name)).ratio()
                if s > best_score:
                    best, best_score = cand, s
            if best is not None and best_score >= FUZZY_THRESHOLD:
                item, kind, score = best, "fuzzy", round(best_score, 3)
            else:
                item, kind, score = None, "none", round(best_score, 3)

        if item is None:
            line = MatchedLine(item=None, entries=[entry], match_kind="none", match_score=score)
            line.qty_journal = entry.qty
            line.has_docs = bool(entry.doc)
            unmatched.append(line)
            continue

        key = id(item)
        if kind != "none" and not lines[key].match_kind:
            lines[key].match_kind, lines[key].match_score = kind, score
        lines[key].entries.append(entry)

    result = list(lines.values()) + unmatched
    for line in result:
        _finalize(line)
    return result


def _finalize(line: MatchedLine) -> None:
    """Дедупликация и агрегирование объёмов с фиксацией несоответствий."""
    if line.item is None:
        return
    seen: dict[tuple, int] = {}
    unique = []
    for e in line.entries:
        key = (e.date, e.zone.strip().lower(), round(e.qty, 4), norm_code(e.code))
        if key in seen:
            seen[key] += 1
            line.duplicates += 1
            continue
        seen[key] = 1
        unique.append(e)
    line.qty_journal = round(sum(e.qty for e in unique), 4)
    mismatched = [e for e in unique if e.unit != line.item.unit]
    line.unit_mismatch = bool(mismatched) and len(mismatched) == len(unique)
    line.has_docs = all(bool(e.doc) for e in unique) if unique else False
