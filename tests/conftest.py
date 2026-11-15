# -*- coding: utf-8 -*-
"""Общие приспособления для тестов.

Главное правило: **тест не зависит от того, что случайно лежит в `out/`**. Раньше два
теста (`review --out`, `sync-1c --push`) читали `out/summary.json`, которого в чистой
песочке нет: локально они падали, в CI — проходили, потому что артефакты успевал создать
предыдущий шаг. «Зелёно» должно значить одно и то же везде, поэтому артефакты собираются
здесь и только во временном каталоге, тем же кодом, что в CI и в CLI.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEMO_DATA = REPO / "data" / "demo"


@pytest.fixture(scope="session")
def demo_out(tmp_path_factory) -> Path:
    """Полный прогон по демо-данным: `run --data data/demo --out <временный каталог>`.

    Один раз на сессию, ~0,3 с; никакие файлы репозитория не трогаются. Именно поэтому
    тесты ниже не «падают на чистом main» и не проходят случайно.
    """
    from pto_agent.cli import main as cli_main

    out = tmp_path_factory.mktemp("demo-out")
    code = cli_main(["run", "--data", str(DEMO_DATA), "--out", str(out)])
    assert code == 0, "демо-прогон не выполнился: тестам не на чем проверять"
    return out


@pytest.fixture(scope="session")
def demo_summary(demo_out) -> Path:
    path = demo_out / "summary.json"
    assert path.exists(), "прогон не создал summary.json"
    return path


@pytest.fixture(scope="session")
def demo_summary_json(demo_summary) -> dict:
    return json.loads(demo_summary.read_text(encoding="utf-8"))
