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


def test_pre_registration_written_before_run(tmp_path: Path):
    """Предрегистрация пишется до первого запроса и содержит отпечаток плана."""
    from pto_agent.bench import pre_registration

    plan = Plan(attempts=2, blocks=2, model="test-model", provider="test")
    path = pre_registration(plan, "https://example.invalid/v1", tmp_path / "pre.md")
    text = path.read_text(encoding="utf-8")
    assert plan.fingerprint() in text
    assert "до первого запроса" in text
    assert "лучший из N" in text          # правило зафиксировано словами
    assert "test-model" in text
    for task in plan.tasks:
        assert task.id in text


def test_cli_requires_key_for_live_run(monkeypatch):
    """Без ключа живой прогон не начинается: код 2 и понятное сообщение."""
    import subprocess
    import sys
    env = {k: v for k, v in __import__("os").environ.items()
           if "GROQ" not in k and "API_KEY" not in k}
    result = subprocess.run(
        [sys.executable, "-m", "pto_agent", "bench", "run", "--provider", "groq",
         "--model", "llama"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "GROQ_API_KEY" in result.stderr


def test_auth_failure_is_technical_not_silent(tmp_path: Path):
    """Неверный ключ — это технический отказ в каждой строке, а не «0 %» без объяснений."""
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "-m", "pto_agent", "bench", "run", "--provider", "groq",
         "--model", "llama", "--attempts", "1", "--blocks", "1", "--out", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        timeout=300, env={**__import__("os").environ, "GROQ_API_KEY": "definitely-not-a-key"})
    assert result.returncode == 0, result.stderr[-1500:]
    rows = [json.loads(line) for line in
            (tmp_path / "rows.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and all(row["outcome"] == "technical" for row in rows)
    # 401 или 403 — в обоих случаях это отказ авторизации, и он виден в каждой строке
    assert all(row["error"].startswith(("HTTP 401", "HTTP 403")) for row in rows)
    assert (tmp_path / "pre_registration.md").exists()


# ─── дефекты, найденные ПЕРВЫМ живым прогоном (23.11.2026) ────────────────────
# Каждый тест здесь — не про мок, а про то, что прибор мерил себя вместо модели.

def test_mock_is_clean_on_every_kind():
    """Инвариант: мок без сбоев даёт 15 из 15. Если он даёт меньше — сломан прибор.

    Ровно это и случилось: три задачи были невыполнимы по построению, и первый живой
    прогон показал 46,7 % вместо реального результата модели.
    """
    mock = MockModel().start()
    try:
        result = run_plan(Plan(attempts=1, blocks=1), mock.base_url)
    finally:
        mock.stop()
    summary = result["summary"]
    assert summary["first_qualified"] == summary["first_attempts"] == 15, summary["by_kind"]


def test_kind_of_matches_every_default_task():
    """Вид задачи определяется по промпту так же, как он задан в наборе.

    Раньше проверка «json» стояла первой, и все 15 промптов уходили в truncation/schema:
    мок отвечал не тем телом, а тесты этого не видели.
    """
    for task in default_tasks():
        assert MockModel.kind_of(task.prompt) == task.kind, task.id


def test_no_data_requires_the_marker_from_the_prompt():
    """«Не вижу документа» без названного признака — отговорка, а не честность."""
    task = next(t for t in default_tasks() if t.id == "no_data_1")
    ok_honest, _ = task.evaluate("В выгрузке отмечено [нет данных], поэтому расход не назван.")
    ok_evasive, why = task.evaluate("У меня нет доступа к вашим документам, не могу помочь.")
    assert ok_honest is True
    assert ok_evasive is False and "нет данных" in why


def test_quote_task_is_checkable():
    """Цитата: правильный ответ проходит, пересказ — нет."""
    task = next(t for t in default_tasks() if t.id == "quote_norm_1")
    assert task.evaluate("«…составляет 20 мм» (п. 10.3.1).")[0] is True
    assert task.evaluate("Норма требует достаточного защитного слоя.")[0] is False


def test_truncation_task_is_possible_but_not_free():
    """Болтливый ответ обрывается, короткий и точный — проходит."""
    task = next(t for t in default_tasks() if t.id == "truncation_1")
    short = '{"number":"1","period":"2026-10","lines":[],"total":0}'
    chatty = "Конечно! Вот JSON акта, который вы просили: " + short
    assert task.evaluate(short)[0] is True
    assert task.evaluate(chatty)[0] is False


def test_length_mode_penalises_the_truncation_task():
    """Если сервер обрезал ответ по max_tokens, «почти JSON» не засчитывается."""
    mock = MockModel(policy={"truncation": "length"}, length_cut_chars=30).start()
    try:
        result = run_plan(Plan(attempts=1, blocks=1), mock.base_url,
                          log_path=None)
    finally:
        mock.stop()
    kind = result["summary"]["by_kind"]["truncation"]
    assert kind["runs"] == 2
    assert kind["unqualified"] + kind["technical"] == kind["runs"]


def test_json_mode_satisfies_json_tasks():
    """Ответ голым JSON на просьбу «только JSON» — это годный ответ, а не сбой."""
    mock = MockModel(policy={"truncation": "json_mode"}).start()
    try:
        result = run_plan(Plan(attempts=1, blocks=1), mock.base_url)
    finally:
        mock.stop()
    assert result["summary"]["first_qualified"] == 15


def test_quote_accepts_clause_label_case_insensitive():
    """«Пункт: 10.3.1.» — указание пункта. Точный ответ не должен считаться провалом."""
    task = next(t for t in default_tasks() if t.id == "quote_norm_2")
    assert task.evaluate("«…составляет 20 мм».\n\nПункт: 10.3.1.")[0] is True
    assert task.evaluate("«…составляет 20 мм». См. таблицу.")[0] is False


def test_no_data_accepts_morphology_but_not_excuses():
    """Отсутствие, названное иначе («не находится»), — честность; «не могу» — нет."""
    task = next(t for t in default_tasks() if t.id == "no_data_4")
    assert task.evaluate("Позиция 44 не находится в смете.")[0] is True
    assert task.evaluate("У меня нет доступа к смете, не могу помочь.")[0] is False


def test_oversized_output_is_flagged_not_scored():
    """Ответ длиннее max_tokens — отметка, а не оценка: иначе оценка завышается.

    Живой прогон показал 640 токенов при лимите 320 — сервер шлюза лимит не соблюдал,
    и «ответ длинный» нельзя было отличить от «ответ правильный».
    """
    mock = MockModel(policy={"truncation": "long"}, length_cut_chars=5000).start()
    try:
        result = run_plan(Plan(attempts=1, blocks=1), mock.base_url, log_path=None)
    finally:
        mock.stop()
    flagged = [row for row in result["rows"] if row.over_max_tokens]
    assert flagged, "нет ни одной отметки over_max_tokens"
    assert result["summary"]["over_max_tokens"] == len(flagged)


# ─── сравнение двух адресов (шаг 2 пилота) ────────────────────────────────────

def test_schedule_separates_attempts_across_blocks():
    """Попытки одной задачи идут в разных блоках, а не подряд."""
    plan = Plan(attempts=2, blocks=3)
    slots = plan.schedule()
    for attempt in (1, 2):
        blocks = [slot["block"] for slot in slots
                  if slot["attempt"] == attempt and slot["task"] == "schema_act_1"]
        assert len(set(blocks)) == 3, blocks
    assert len(slots) == len(plan.tasks) * plan.attempts * plan.blocks


def test_plan_reports_twin_tasks():
    """Копии одной задачи видны до прогона: иначе они выдают себя за наблюдения."""
    twins = Plan().blocks_mismatch()
    assert "chain_1 = chain_2" in twins
    assert "truncation_1 = truncation_2" in twins


def test_compare_detects_the_planted_difference():
    """Сравнение видит ровно те виды задач, где разошлись прогоны."""
    from pto_agent.bench.runner import compare

    good = MockModel().start()
    broken = MockModel(policy={"quote": "paraphrase", "no_data": "invented"}).start()
    try:
        left = run_plan(Plan(attempts=1, blocks=1), good.base_url)
        right = run_plan(Plan(attempts=1, blocks=1), broken.base_url)
    finally:
        good.stop()
        broken.stop()
    diff = compare(left, right, "good", "broken")
    assert diff["same_fingerprint"] is True
    assert set(diff["only_left"]) == {
        "quote_norm_1", "quote_norm_2", "quote_norm_3",
        "no_data_1", "no_data_2", "no_data_3", "no_data_4"}
    assert diff["only_right"] == []
    assert diff["delta"] < 0


def test_compare_refuses_mismatched_plans():
    """Разные планы — повод сказать «сравнивать нельзя», а не молча сравнить."""
    from pto_agent.bench.runner import compare

    mock = MockModel().start()
    try:
        left = run_plan(Plan(attempts=1, blocks=1), mock.base_url)
        right = run_plan(Plan(attempts=1, blocks=2), mock.base_url)
    finally:
        mock.stop()
    assert compare(left, right, "a", "b")["same_fingerprint"] is False


def test_compare_report_has_no_verdict_words():
    """В сравнении нет «лучше/хуже»: только разница и оговорки."""
    from pto_agent.bench.report import compare_markdown

    text = compare_markdown({"left": "a", "right": "b", "left_rate": 0.5,
                             "right_rate": 0.7, "delta": 0.2, "only_left": [],
                             "only_right": ["quote_norm_1"], "same_fingerprint": True})
    lowered = text.lower()
    for word in ("лучше", "хуже", "подмен", "деградировал", "обман", "разбавлен"):
        assert word not in lowered, word
    assert "наблюдение" in lowered


def test_same_answers_flags_identical_responses():
    """Идентичные ответы при temperature > 0 — повод сказать это, а не промолчать."""
    mock = MockModel().start()
    try:
        result = run_plan(Plan(attempts=2, blocks=1), mock.base_url)
    finally:
        mock.stop()
    assert result["summary"]["same_answers_share"] == 1.0


def test_cli_compare_end_to_end(tmp_path: Path):
    """`bench compare` пишет отчёт и умеет падать по порогу разницы."""
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    a, b = tmp_path / "a", tmp_path / "b"
    for target, extra in ((a, []), (b, ["--break", "quote=paraphrase"])):
        subprocess.run([sys.executable, "-m", "pto_agent", "bench", "mock",
                        "--out", str(target), "--attempts", "1", "--blocks", "1", *extra],
                       cwd=root, check=True, capture_output=True, timeout=300)
    out = tmp_path / "cmp"
    ok = subprocess.run([sys.executable, "-m", "pto_agent", "bench", "compare",
                         str(a / "report.json"), str(b / "report.json"),
                         "--out", str(out)], cwd=root, capture_output=True, text=True,
                        timeout=300)
    assert ok.returncode == 0, ok.stderr[-800:]
    assert (out / "compare.md").exists() and (out / "compare.json").exists()
    strict = subprocess.run([sys.executable, "-m", "pto_agent", "bench", "compare",
                             str(a / "report.json"), str(b / "report.json"),
                             "--out", str(out), "--max-delta", "0.1"],
                            cwd=root, capture_output=True, text=True, timeout=300)
    assert strict.returncode == 1
