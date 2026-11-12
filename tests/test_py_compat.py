# -*- coding: utf-8 -*-
"""Совместимость синтаксиса с Python 3.11 — самой младшей версией из матрицы CI.

Зачем: PEP 701 (вложенные кавычки в f-строках, переносы внутри выражений) работает с 3.12,
а на 3.11 это SyntaxError. Локально под 3.13 всё зелёное, CI краснеет только в одном
джобе — и падение выглядит как «тесты», хотя это даже не импорт модуля.

Проверка дешёвая: разбор всех модулей пакета грамматикой 3.11, без запуска.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "pto_agent"
MODULES = sorted(PACKAGE.rglob("*.py"))


def test_package_has_modules():
    assert len(MODULES) > 20


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(ROOT)))
def test_module_parses_with_python_311_grammar(path):
    source = path.read_text(encoding="utf-8")
    try:
        ast.parse(source, filename=str(path), feature_version=(3, 11))
    except SyntaxError as error:
        pytest.fail(f"{path.relative_to(ROOT)}: синтаксис новее Python 3.11 (CI гоняет 3.11): "
                    f"{error.msg}, строка {error.lineno}")
