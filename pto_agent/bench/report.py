# -*- coding: utf-8 -*-
"""Отчёт: то, что можно проверить снаружи, и то, чего в нём нельзя сказать.

Правила формулировок (не стилистика, а защита от справедливой обиды вендора):

* отчёт не утверждает «модель подменена», «разбавлена», «деградировала». Он утверждает:
  «в окне таком-то, на задачах таких-то, с конфигурацией такой-то получено столько-то»;
* каждая цифра рядом с размером выборки и интервалом;
* все ограничения перечислены в самом отчёте, а не в переписке;
* команда воспроизведения — в отчёте, а не «пишите, расскажем».
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

RULES = [
    "Отчёт не делает вывода о причинах: он фиксирует, что наблюдалось при заданных условиях.",
    "Технические отказы не выбрасываются — они входят в «долю годного с первой попытки».",
    "Каждая доля дана с интервалом Уилсона: малое число попыток не выдаётся за точность.",
    "Повторный прогон в другом окне — отдельный отчёт, а не «исправление» этого.",
]


def to_json(result: dict, path: str | Path | None = None) -> dict:
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method_version": result["plan"]["method_version"],
        "plan_fingerprint": result["fingerprint"],
        "plan": result["plan"],
        "summary": result["summary"],
        "duration_s": result["duration_s"],
        "rules": RULES,
        "rows": [row.to_row() if hasattr(row, "to_row") else row for row in result["rows"]],
    }
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def _pct(value: float) -> str:
    return f"{value * 100:.1f} %"


def to_markdown(result: dict, *, title: str = "Стенд: поведение модели") -> str:
    summary = result["summary"]
    plan = result["plan"]
    lines = [
        f"# {title}",
        "",
        f"**Провайдер:** {plan['provider']} · **модель:** `{plan['model']}` · "
        f"**методика:** v{plan['method_version']} · **отпечаток плана:** "
        f"`{result['fingerprint']}`",
        "",
        "## Главное",
        "",
        f"- **Доля годного результата с первой попытки:** "
        f"{_pct(summary['first_attempt_rate'])} "
        f"({summary['first_qualified']} из {summary['first_attempts']}), "
        f"интервал Уилсона {_pct(summary['wilson']['low'])}–{_pct(summary['wilson']['high'])}.",
        f"- **Годен среди дошедших ответов:** {_pct(summary['qualified_among_answered'])} "
        f"— вторая цифра нужна отдельно: иначе таймауты прячутся за «качеством».",
        f"- **Технических отказов:** {summary['technical']} из {summary['rows']} "
        f"({_pct(summary['technical_rate'])}) — они не выброшены и входят в первую цифру.",
        # Отметка, а не оценка: сервер, который не соблюдает max_tokens, мешает судить о
        # «многословии» модели, и об этом надо сказать, а не молчать.
        f"- **Ответов длиннее `max_tokens`:** {summary.get('over_max_tokens', 0)} "
        f"из {summary['rows']} — лимит не соблюдён сервером; это наблюдение, не приговор.",
        "",
        "## По видам задач",
        "",
        "| Вид | Запросов | годен | не годен | технический отказ |",
        "|---|---|---|---|---|",
    ]
    for kind, bucket in sorted(summary["by_kind"].items()):
        lines.append(f"| {kind} | {bucket['runs']} | {bucket.get('qualified', 0)} | "
                     f"{bucket.get('unqualified', 0)} | {bucket.get('technical', 0)} |")
    lines += ["", "## По задачам", "",
              "| Задача | Вид | Запросов | годен | не годен | технический отказ |",
              "|---|---|---|---|---|---|"]
    for task, bucket in sorted(summary["by_task"].items()):
        lines.append(f"| {task} | {bucket['kind']} | {bucket['runs']} | "
                     f"{bucket.get('qualified', 0)} | {bucket.get('unqualified', 0)} | "
                     f"{bucket.get('technical', 0)} |")

    broken = [(task, bucket) for task, bucket in sorted(summary["by_task"].items())
              if bucket.get("qualified", 0) == 0]
    lines += ["", "## Что сломалось", ""]
    if broken:
        for task, bucket in broken:
            reasons = sorted({row.why for row in result["rows"]
                              if getattr(row, "task", None) == task and row.why})
            lines.append(f"- **{task}**: ни одного годного ответа. Причины: "
                         f"{'; '.join(reasons[:3]) or '—'}")
    else:
        lines.append("- Ни одна задача не провалилась целиком.")

    lines += ["", "## Ограничения", ""]
    lines += [f"- {rule}" for rule in RULES]
    lines += [
        f"- Выборка: {summary['first_attempts']} первых попыток на {len(plan['tasks'])} "
        f"задач; этого достаточно, чтобы увидеть грубые различия, и недостаточно для "
        f"выводов о малых.",
        f"- Запросов всего: {summary['rows']}, блоков: {plan['blocks']}, попыток на "
        f"задачу: {plan['attempts']}, seed: `{plan['seed']}`.",
        "- Персональных данных нет: задачи синтетические, данные проекта не отправлялись.",
        "",
        "## Как повторить",
        "",
        "```bash",
        "python3 -m pto_agent bench run --base-url <адрес> --model <модель> --out out/bench",
        "```",
        "",
        f"Отпечаток плана `{result['fingerprint']}` в отчёте нужен, чтобы было видно: "
        "план не менялся после того, как стали известны результаты.",
        "",
    ]
    return "\n".join(lines)
