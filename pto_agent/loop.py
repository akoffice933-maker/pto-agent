# -*- coding: utf-8 -*-
"""Loop: прогон «сделал → проверил → починил → записал» с тормозом и без выдумок.

Что здесь есть, а чего нет
--------------------------
Цикл **не правит код сам**. Это осознанное ограничение, а не недоделка: агент, который
меняет собственную логику без человека, в бухгалтерском документе — это риск, который
дороже выигранного времени. Цикл делает другое:

1. прогоняет eval (проверки поведения);
2. осаждает провалы в граф backlog (из одного провала — один узел, с доказательством);
3. берёт готовые к работе узлы и **закрывает те, что закрываются проверкой** — то есть
   перепроверяет, а не «отмечает сделанным»;
4. останавливается по правилам торможения.

Тормоз (`self-throttle`) — четыре независимых условия остановки:

* **бюджет кругов** — не больше N за запуск;
* **нет прогресса** — балл eval не вырос за `patience` кругов подряд;
* **зацикливание** — один и тот же набор провалов повторился `repeat_limit` раз
  (счётчик `seen` в узлах — прямой индикатор);
* **предел правок** — узел, который не закрылся за `max_attempts` попыток, переводится
  в `blocked` с пометкой «нужен человек», а не долбится бесконечно.

Каждый круг пишется в журнал `out/loop/runs.jsonl` — построчно, с причиной остановки.
Никакой «беспилотности» без следа: 7×24 здесь означает «повторяемо и с журналом», а не
«без ограничений».

    python3 -m pto_agent loop --cycles 3 --out out/loop
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import backlog as backlog_mod
from . import eval as eval_mod

STOP_BUDGET = "бюджет кругов исчерпан"
STOP_NO_PROGRESS = "нет прогресса: балл eval не растёт"
STOP_REPEAT = "зацикливание: те же провалы повторяются"
STOP_STUCK = "узел не закрывается за отведённые попытки — нужен человек"
STOP_DONE = "провалов нет: очередной круг не нашёл, что чинить"


def run(*, cycles: int = 3, out_dir: str | Path = "out/loop",
        backlog_path: str | Path | None = None, patience: int = 2,
        repeat_limit: int = 3, max_attempts: int = 3, sleep_s: float = 0.0,
        summary_path=None, review_path=None) -> dict:
    """Прогнать цикл. Возвращает журнал с причиной остановки — всегда, даже при отказе."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bpath = Path(backlog_path) if backlog_path else out / "backlog.json"
    bl = backlog_mod.Backlog(bpath)

    history: list[dict] = []
    best_score, no_gain = -1.0, 0
    stop = STOP_BUDGET
    previous_fail_set: tuple[str, ...] | None = None
    same_failures = 0

    for i in range(1, cycles + 1):
        cycle = f"{datetime.now().strftime('%Y%m%d')}-{i}"
        report = eval_mod.run_all(out_dir=out / "eval", summary_path=summary_path,
                                  review_path=review_path)
        score = report.get("score") or 0.0
        added = bl.ingest(report, cycle=cycle)

        # «исправление» в цикле — это повторная проверка, а не правка кода руками агента
        closed, stuck = [], []
        for node in list(bl.open_nodes()):
            bl.bump(node["id"])
            if node.get("attempts", 0) >= max_attempts:
                node["status"] = backlog_mod.BLOCKED
                node["closed_note"] = (f"попыток {node['attempts']}, проверка всё ещё "
                                       f"падает — нужен человек")
                stuck.append(node["id"])
                continue
            still_failing = any(r.get("code") == node["code"] and r.get("status") == "fail"
                                for r in report.get("results") or [])
            if not still_failing:
                res = next((r for r in report.get("results") or []
                            if r.get("code") == node["code"]), None)
                note = f"проверка {node['code']} прошла: {res.get('why')}" if res \
                    else f"проверка {node['code']} не выполнялась — узел не закрыт"
                if res:
                    bl.close(node["id"], note=note, cycle=cycle)
                    closed.append(node["id"])
        bl.save()

        fail_set = tuple(sorted(r["code"] for r in report.get("results") or []
                                if r.get("status") == "fail"))
        # «Зацикливание» — это повтор ОДНИХ И ТЕХ ЖЕ провалов. Пустой набор провалов
        # повтором не считается: иначе успешный прогон останавливался бы с ложной
        # причиной «зацикливание», что мы и поймали на первом запуске.
        if fail_set and fail_set == previous_fail_set:
            same_failures += 1
        elif fail_set:
            same_failures = 0
        previous_fail_set = fail_set

        no_gain = no_gain + 1 if score <= best_score else 0
        best_score = max(best_score, score)

        history.append({
            "cycle": cycle, "score": score, "counts": report.get("counts"),
            "failures": list(fail_set), "nodes_added": added,
            "nodes_closed": closed, "nodes_stuck": stuck,
            "open_nodes": len(bl.open_nodes()),
            "at": datetime.now().isoformat(timespec="seconds"),
        })

        if stuck:
            stop = STOP_STUCK
            break
        if not fail_set and not bl.open_nodes():
            stop = STOP_DONE
            break
        if same_failures >= repeat_limit - 1:
            stop = STOP_REPEAT
            break
        if no_gain >= patience:
            stop = STOP_NO_PROGRESS
            break
        if i < cycles and sleep_s:
            time.sleep(sleep_s)

    result = {
        "schema": "pto-agent/loop@1",
        "started_at": history[0]["at"] if history else None,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "cycles_run": len(history),
        "cycles_budget": cycles,
        "best_score": best_score if best_score >= 0 else None,
        "stop_reason": stop,
        "history": history,
        "backlog": bl.stats(),
        "limits_kept": [
            "агент не правит собственный код: «исправление» = повторная проверка и узел "
            "в графе, а не коммит",
            "закрыть узел можно только прошедшей проверкой, словом «сделано» — нельзя",
            "за круг добавляется не больше "
            f"{backlog_mod.MAX_NEW_PER_CYCLE} узлов",
            f"узел, не закрывшийся за {max_attempts} попыток, уходит человеку",
        ],
    }
    (out / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    with (out / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"finished_at": result["finished_at"],
                             "stop_reason": stop, "cycles": len(history),
                             "best_score": result["best_score"],
                             "history": history}, ensure_ascii=False) + "\n")
    (out / "run.md").write_text(to_markdown(result), encoding="utf-8")
    return result


def to_markdown(res: dict) -> str:
    lines = ["# Loop: прогон цикла с тормозом", "",
             f"Кругов: {res['cycles_run']} из {res['cycles_budget']} · лучший балл: "
             f"**{res['best_score']}** · остановка: **{res['stop_reason']}**", "",
             "| Круг | Балл | Провалов | Новых узлов | Закрыто | Открытых узлов |",
             "|---|---|---|---|---|---|"]
    for h in res["history"]:
        lines.append(f"| {h['cycle']} | {h['score']} | {len(h['failures'])} | "
                     f"{h['nodes_added']} | {len(h['nodes_closed'])} | {h['open_nodes']} |")
    lines += ["", "## Что цикл не делает (по замыслу)", ""]
    lines += [f"- {x}" for x in res["limits_kept"]]
    lines += ["", "Повторяемость важнее «беспилотности»: каждый запуск оставляет журнал "
                  "`out/loop/runs.jsonl`, и по нему видно, почему цикл встал."]
    return "\n".join(lines) + "\n"
