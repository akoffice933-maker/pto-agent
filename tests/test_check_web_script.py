# -*- coding: utf-8 -*-
"""Сторож для самого `scripts/check_web.py`.

Зачем тесты на CI-скрипт. 22.11 шаг «Экраны и роли» упал не потому, что экраны были
сломаны, а потому что скрипт проверял одно, а падал на другом: вход отвалился (CSRF),
а `check()` копит нарушения и не останавливает выполнение — поэтому скрипт дошёл до
`next(...)` по пустому списку и умер с `StopIteration`. Тесты `test_web.py` при этом
были зелёные, потому что проверяли приложение, а не скрипт.

Здесь три вещи:

1. скрипт запускается целиком и должен быть зелёным (иначе он не работает вовсе);
2. вход и права проверяются через `gate` — падение на своём шаге, а не в конце;
3. в скрипте снова не появилось `next(...)` без значения по умолчанию.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_web.py"


def test_check_web_runs_green():
    pytest.importorskip("fastapi")
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, (
        f"check_web.py упал (код {result.returncode}):\n{result.stderr[-2000:]}")
    assert "нарушений: 0" in result.stdout, result.stdout[-2000:]
    assert "закрытие месяца: шагов" in result.stdout, result.stdout[-1000:]


def test_login_and_rights_are_gates():
    """Вход и права — предусловия: если они сломаны, остальные числа ничего не значат."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'gate(response.status_code == 303 and' in text, "вход без проверки через gate"
    assert 'gate(prorab.get("/closing").status_code == 403' in text, \
        "права прораба проверяются мягко"
    assert 'gate(response.status_code == 403 and "AGENT_FORBIDDEN"' in text, \
        "отказ ИИ-агенту проверяется мягко"
    assert not re.search(r"^check\(response\.status_code == 403", text, re.M), \
        "отказ по правам снова проверяется через check(), а не gate()"


def test_no_bare_next_in_web_script():
    """`next(генератор)` без значения по умолчанию — это будущий StopIteration."""
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0]          # комментарии не считаем
        if "next(" not in code:
            continue
        assert "None" in code, f"голый next() без значения по умолчанию: {line.strip()}"


def test_gate_reports_before_exit(capsys):
    """gate печатает причину до выхода: иначе шаг падает молча."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("def gate(")
    end = text.index("\ndef ", start + 1)
    module = {"sys": sys, "print": print}
    exec(compile(text[start:end], "<gate>", "exec"), module)  # noqa: S102 — свой же код
    with pytest.raises(SystemExit) as caught:
        module["gate"](False, "права не проверяются")
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert "права не проверяются" in captured.err
    assert "остановлена" in captured.err
