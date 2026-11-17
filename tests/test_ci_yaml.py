# -*- coding: utf-8 -*-
"""CI-файл — тоже код, и он тоже ломается.

Круг 43: шаг с именем «… по ФСБУ 4/2023: актив = пассив …» содержал двоеточие с пробелом
внутри значения без кавычек — YAML не разобрался, workflow упал **до** создания задач,
и в списке проверок не было ни одного job. Локальные тесты при этом были зелёными.

Что проверяем:
1. `.github/workflows/ci.yml` разбирается как YAML;
2. в нём есть задачи unit/e2e, и в unit есть шаг тестов;
3. каждый скрипт из `run:` существует в репозитории — иначе шаг упадёт уже на CI;
4. счётчик MCP-инструментов в CI совпадает с числом инструментов в коде.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

yaml = pytest.importorskip("yaml")

CI = REPO / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_ci_yaml_parses(workflow):
    assert set(workflow["jobs"]) >= {"unit", "e2e"}
    steps = workflow["jobs"]["unit"]["steps"]
    assert any("pytest" in str(step.get("run", "")) for step in steps)


def test_every_script_in_ci_exists(workflow):
    missing = []
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            run = str(step.get("run") or "")
            for script in re.findall(r"scripts/[\w./-]+\.py", run):
                if not (REPO / script).exists():
                    missing.append(script)
    assert not missing, f"в CI есть шаги с несуществующими скриптами: {sorted(set(missing))}"


def test_mcp_counter_in_ci_matches_code(workflow):
    pytest.importorskip("ezdxf")          # без ezdxf MCP-сервер не импортируется
    from pto_agent.cad.mcp_server import TOOLS

    total = len(TOOLS)
    text = CI.read_text(encoding="utf-8")
    counters = {int(value) for value in re.findall(r"n\s*==\s*(\d+)", text)}
    assert counters == {total}, f"в CI {counters}, а инструментов {total}"
