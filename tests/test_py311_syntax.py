# -*- coding: utf-8 -*-
"""Python 3.11: вложенные кавычки в f-строке — SyntaxError, а локально его не видно.

В круге 33 код прошёл локальный прогон на 3.13 и упал в CI на 3.11: в f-строке стояла
вложенная f-строка с теми же кавычками (`f"{', '.join(f'№ {row['number']}')}"`) — это
PEP 701, разрешённый только с 3.12. `ast.parse(..., feature_version=(3,11))` такое **не**
ловит, поэтому проверка идёт разбором строк: ищем f-строку, внутри фигурных скобок которой
встречается кавычка того же типа.

Проверка эвристическая: ловит именно этот класс ошибок, а не «любой синтаксис 3.12».
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TARGETS = sorted((REPO / "pto_agent").rglob("*.py")) + sorted((REPO / "scripts").glob("*.py"))


def _nested_quotes(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for index, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for match in re.finditer(r"(?<![A-Za-z0-9_])f(['\"])", line):
            quote = match.group(1)
            depth = 0
            rest = line[match.end():]
            for char in rest:
                if char == quote and depth == 0:
                    break  # строка закончилась: дальше уже другой литерал
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth = max(depth - 1, 0)
                elif char == quote and depth > 0:
                    found.append((index, line.strip()))
                    break
    return found


def test_no_pep701_fstrings():
    problems: list[str] = []
    for path in TARGETS:
        for line_no, source in _nested_quotes(path.read_text(encoding="utf-8")):
            if "{{" in source or "}}" in source:
                continue  # литеральные скобки формата, а не вложенная строка
            problems.append(f"{path.relative_to(REPO)}:{line_no}: {source[:90]}")
    assert not problems, ("вложенные кавычки в f-строке — PEP 701, CI на 3.11 упадёт:\n"
                          + "\n".join(problems))


@pytest.mark.parametrize("snippet, hits", [
    ("f'{a['x']}'", 1),   # одинарная снаружи и внутри — то, что упало в CI
    ('f"текст {value} ещё"', 0),
    ('f"{{{NS}}}Отчёт"', 0),  # литеральные скобки: не вложенная строка
])
def test_scanner_itself(snippet: str, hits: int):
    assert len(_nested_quotes(snippet)) == hits
