# -*- coding: utf-8 -*-
"""Стенд: проверяем измеритель, а не модель.

Главная мысль этих тестов: у mock-модели ответы известны заранее. Значит, если стенд
показывает «всё хорошо» там, где заложены сбои, — сломан стенд. Обычный бенчмарк так
проверить нельзя, и это единственное, что отличает измеритель от мнения с таблицей.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pto_agent.bench import MockModel, Plan, run_plan, to_json, to_markdown
from pto_agent.bench.mock import INVENTED, PARAPHRASE, BAD_JSON, TRUNCATED
from pto_agent.bench.runner import QUALIFIED, wilson_interval
from pto_agent.bench.tasks import default_tasks


def test_plan_fingerprint_is_stable_and_changes_with_plan():
    plan = Plan(attempts=2, blocks=2)
    assert plan.fingerprint() == Plan(attempts=2, blocks=2).fingerprint()
    assert plan.fingerprint() != Plan(attempts=3, blocks=2).fingerprint()


def test_schedule_keeps_blocks_separate_and_complete():
    plan = Plan(attempts=2, blocks=3)
    schedule = plan.schedule()
    assert len(schedule) == len(plan.tasks) * plan.attempts * plan.blocks
    blocks = {slot["block"] for slot in schedule}
    assert blocks == {1, 2, 3}
    # внутри каждого блока — полный набор задач и попыток, без повторов и пропусков
    for block in blocks:
        chunk = [(s["task"], s["attempt"]) for s in schedule if s["block"] == block]
        assert len(chunk) == len(set(chunk)) == len(plan.tasks) * plan.attempts


def test_good_mock_gives_high_rate():
    with MockModel() as mock:
        result = run_plan(Plan(attempts=1, blocks=1), mock.base_url)
    assert result["summary"]["first_attempt_rate"] == 1.0
    assert result["summary"]["technical"] == 0


@pytest.mark.parametrize("kind,mode", [
    ("schema", BAD_JSON), ("no_data", INVENTED), ("quote", PARAPHRASE),
    ("truncation", TRUNCATED),
])
def test_instrument_catches_each_planted_failure(kind, mode):
    """Заложили сбой одного вида — стенд обязан его увидеть именно в этом виде задач."""
    with MockModel(policy={kind: mode}) as mock:
        result = run_plan(Plan(attempts=1, blocks=1), mock.base_url)
    bucket = result["summary"]["by_kind"][kind]
    assert bucket.get(QUALIFIED, 0) == 0, f"сбой «{mode}» в «{kind}» не замечен"
    # и не размазался на остальные виды
    others = [b for k, b in result["summary"]["by_kind"].items() if k != kind]
    assert all(b.get(QUALIFIED, 0) == b["runs"] for b in others)


def test_technical_failures_are_counted_not_hidden():
    """Таймауты и 500 — отдельная категория и входят в главную метрику."""
    with MockModel(fail_every=5) as mock:
        result = run_plan(Plan(attempts=1, blocks=1), mock.base_url)
    summary = result["summary"]
    assert summary["technical"] > 0
    assert summary["technical_rate"] > 0
    # «годен среди дошедших» выше, чем «годен с первой попытки» — иначе отказы спрятаны
    assert summary["qualified_among_answered"] >= summary["first_attempt_rate"]


def test_only_first_attempt_counts_in_main_metric():
    """Двадцать попыток не превращаются в «лучший из двадцати»."""
    with MockModel(policy={"no_data": INVENTED}) as mock:
        result = run_plan(Plan(attempts=3, blocks=2), mock.base_url)
    summary = result["summary"]
    # первая попытка — одна на задачу в каждом блоке, а не все 3×2 запроса
    assert summary["first_attempts"] == len(default_tasks()) * 2
    assert summary["rows"] == len(default_tasks()) * 2 * 3
    assert summary["first_qualified"] < summary["first_attempts"]
    assert summary["by_task"]["no_data_1"]["qualified"] == 0


def test_wilson_interval_narrows_with_sample():
    small = wilson_interval(8, 10)
    big = wilson_interval(800, 1000)
    assert small["p"] == big["p"] == 0.8
    assert (small["high"] - small["low"]) > (big["high"] - big["low"])
    assert 0.0 <= small["low"] <= small["p"] <= small["high"] <= 1.0


def test_report_has_limits_and_no_accusations(tmp_path: Path):
    with MockModel(policy={"no_data": INVENTED}) as mock:
        result = run_plan(Plan(attempts=1, blocks=1), mock.base_url)
    markdown = to_markdown(result)
    for marker in ("Ограничения", "Как повторить", "интервал Уилсона", "seed"):
        assert marker in markdown, f"в отчёте нет «{marker}»"
    for forbidden in ("подмен", "разбавлен", "деградировал", "обман"):
        assert forbidden not in markdown.lower(), \
            f"отчёт утверждает причину, которую не измерял: «{forbidden}»"


def test_report_written_to_disk(tmp_path: Path):
    with MockModel() as mock:
        result = run_plan(Plan(attempts=1, blocks=1), mock.base_url,
                          log_path=tmp_path / "rows.jsonl")
    payload = to_json(result, tmp_path / "report.json")
    assert payload["plan_fingerprint"] == result["fingerprint"]
    assert (tmp_path / "rows.jsonl").exists()
    rows = [json.loads(line) for line in
            (tmp_path / "rows.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and {"block", "task", "attempt", "outcome", "latency_ms"} <= set(rows[0])


def test_mock_answers_in_openai_format():
    import urllib.request
    with MockModel() as mock:
        request = urllib.request.Request(
            mock.base_url + "/v1/chat/completions",
            data=json.dumps({"model": "mock-model", "messages": [
                {"role": "user", "content": "Собери акт, верни только JSON"}]}).encode(),
            headers={"Content-Type": "application/json"})
        data = json.loads(urllib.request.urlopen(request, timeout=10).read())
    assert data["choices"][0]["message"]["content"].startswith("{")
    assert data["usage"]["total_tokens"] > 0


def test_cli_bench_mock_writes_report(tmp_path: Path):
    """Команда `bench mock` работает целиком: отчёт и сырые строки на диске."""
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "-m", "pto_agent", "bench", "mock", "--out", str(tmp_path),
         "--attempts", "1", "--blocks", "1"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "годен с первой попытки" in result.stdout
    assert (tmp_path / "report.md").exists()
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "rows.jsonl").exists()
