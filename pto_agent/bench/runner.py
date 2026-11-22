# -*- coding: utf-8 -*-
"""Протокол прогона: блоки, интерливинг, все исходы, первая попытка.

Чем это отличается от «сделали 20 запросов и посчитали»
--------------------------------------------------------
Четыре правила, без которых число в отчёте ничего не значит (они же — из статьи
Folkbench о деградации, и это указано в отчёте как источник методики):

1. **Плана придерживаемся, а не подгоняем.** `Plan` фиксируется до первого запроса:
   задачи, попытки, блоки, размер выборки. Остановка «когда станет интересно» запрещена.
2. **Блоки и перемешивание.** Прогон делится на блоки, внутри блока порядок случайный,
   seed записан. Иначе «канал» и «время суток» смешиваются, и разница между моделями
   оказывается разницей между утром и вечером.
3. **Все исходы сохраняются.** Техническая ошибка (таймаут, 429, 500) — это отдельная
   категория, а не выброшенная строка. «Годен / не годен» считается и по всем запросам,
   и отдельно по полученным ответам, чтобы было видно обе цифры.
4. **Главная метрика — доля годного результата с первой попытки.** Не «лучший из N»:
   пользователь платит за первую попытку, а не за двадцатую.
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .tasks import Task, default_tasks

QUALIFIED, UNQUALIFIED, TECHNICAL = "qualified", "unqualified", "technical"


@dataclass
class Plan:
    """План прогона. Публикуется до первого запроса — вместе с хешем."""

    tasks: list[Task] = field(default_factory=default_tasks)
    attempts: int = 3
    blocks: int = 3
    seed: int = 20261122
    model: str = "mock-model"
    provider: str = "mock"
    temperature: float = 0.1
    request_timeout_s: float = 30.0
    method_version: str = "1.0"

    def to_dict(self) -> dict:
        return {"tasks": [{"id": t.id, "kind": t.kind, "max_tokens": t.max_tokens,
                           "note": t.note} for t in self.tasks],
                "attempts": self.attempts, "blocks": self.blocks, "seed": self.seed,
                "model": self.model, "provider": self.provider,
                "temperature": self.temperature,
                "request_timeout_s": self.request_timeout_s,
                "method_version": self.method_version}

    def fingerprint(self) -> str:
        import hashlib
        raw = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def schedule(self) -> list[dict]:
        """Расписание: блок × (задача, попытка), порядок внутри блока случайный."""
        rng = random.Random(self.seed)
        slots = []
        for block in range(1, self.blocks + 1):
            for task in self.tasks:
                for attempt in range(1, self.attempts + 1):
                    slots.append({"block": block, "task": task.id, "attempt": attempt})
        # перемешиваем внутри блока, а не весь список: блоки должны быть сравнимы
        result, index = [], 0
        per_block = len(self.tasks) * self.attempts
        for block in range(1, self.blocks + 1):
            chunk = slots[index:index + per_block]
            rng.shuffle(chunk)
            result.extend(chunk)
            index += per_block
        return result


@dataclass
class RunResult:
    """Один запрос. Пишется в лог целиком — это и есть сырые данные отчёта."""

    block: int
    task: str
    kind: str
    attempt: int
    outcome: str
    why: str
    status: int
    latency_ms: int
    tokens_in: int
    tokens_out: int
    response: str
    error: str = ""
    # Ответ длиннее запрошенного `max_tokens`: сервер не соблюл лимит. Это отметка, а не
    # приговор — но в отчёте она видна, иначе «ответ длинный» не отличить от «ответ верный».
    over_max_tokens: bool = False

    def to_row(self) -> dict:
        return asdict(self)


def _post(base_url: str, body: dict, timeout: float,
          api_key: str | None = None) -> tuple[int, dict | None, str]:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key or 'mock'}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, None, f"{type(exc).__name__}: {exc}"


def run_plan(plan: Plan, base_url: str, *, log_path: str | Path | None = None,
             api_key: str | None = None) -> dict:
    """Прогнать план против OpenAI-совместимого адреса и вернуть сводку + сырые строки."""
    by_id = {task.id: task for task in plan.tasks}
    rows: list[RunResult] = []
    started = time.time()

    for slot in plan.schedule():
        task = by_id[slot["task"]]
        body = {"model": plan.model, "temperature": plan.temperature,
                "max_tokens": task.max_tokens,
                "messages": [{"role": "user", "content": task.prompt}]}
        mark = time.monotonic()
        status, data, error = _post(base_url, body, plan.request_timeout_s, api_key)
        latency = int((time.monotonic() - mark) * 1000)

        if data is None:
            rows.append(RunResult(slot["block"], task.id, task.kind, slot["attempt"],
                                  TECHNICAL, "запрос не дошёл", status, latency,
                                  0, 0, "", error))
            continue

        usage = data.get("usage") or {}
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        ok, why = task.evaluate(text)
        tokens_out = int(usage.get("completion_tokens") or 0)
        rows.append(RunResult(slot["block"], task.id, task.kind, slot["attempt"],
                              QUALIFIED if ok else UNQUALIFIED, why, status, latency,
                              int(usage.get("prompt_tokens") or 0),
                              tokens_out,
                              (text or "")[:2000], "",
                              over_max_tokens=bool(task.max_tokens
                                                   and tokens_out > task.max_tokens)))

    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row.to_row(), ensure_ascii=False)
                                  for row in rows) + "\n", encoding="utf-8")

    return {"plan": plan.to_dict(), "fingerprint": plan.fingerprint(),
            "duration_s": round(time.time() - started, 1), "rows": rows,
            "summary": summarise(rows)}


def pre_registration(plan: Plan, base_url: str, path: str | Path) -> Path:
    """Записать план прогона ДО первого запроса.

    Это не формальность: отпечаток плана, зафиксированный заранее, — единственное
    доказательство, что набор задач и размер выборки не подбирались после того, как стали
    известны результаты. Файл пишется до `run_plan`, поэтому иначе быть не может.
    """
    hours = round(len(plan.tasks) * plan.attempts * plan.blocks * 2.5 / 60, 1)
    lines = [
        "# Предрегистрация прогона стенда",
        "",
        f"**Зафиксировано:** {datetime.now().astimezone().isoformat(timespec='seconds')} "
        f"(до первого запроса)",
        f"**Методика:** v{plan.method_version} · **отпечаток плана:** `{plan.fingerprint()}`",
        "",
        "## Что и где",
        "",
        f"- Провайдер: `{plan.provider}` · модель: `{plan.model}`",
        f"- Адрес: `{base_url}` · температура: {plan.temperature}",
        f"- Задач: {len(plan.tasks)} · попыток на задачу: {plan.attempts} · блоков: "
        f"{plan.blocks} · seed: `{plan.seed}`",
        f"- Запросов всего: {len(plan.tasks) * plan.attempts * plan.blocks} "
        f"(≈{hours} мин при 2,5 с на запрос)",
        "",
        "## Вопросы, на которые отвечает прогон",
        "",
        "1. Какова доля запросов с годным результатом **с первой попытки** — по каждому "
        "виду задач?",
        "2. Какие свойства держатся, а какие нет: схема акта, цитата с пунктом, «нет "
        "данных» вместо числа, цепочка шагов, JSON при обрыве?",
        "3. Сколько технических отказов (таймаут, 429, 5xx) и как они влияют на первую "
        "цифру?",
        "",
        "## Правила, принятые заранее",
        "",
        "- Главная метрика — доля годного с первой попытки; «лучший из N» не считается.",
        "- Технические отказы не выбрасываются и входят в главную метрику.",
        "- Ретраев в основной серии нет; отдельного «повтора с подсказкой» в этом прогоне "
        "тоже нет.",
        "- Остановка и продление выборки по результатам запрещены: план зафиксирован выше.",
        "- Интервал Уилсона показывается всегда; при малом числе попыток выводы "
        "формулируются как «в этих условиях», а не «всегда».",
        "- Отчёт не утверждает причин (подмена, деградация): фиксируется наблюдение при "
        "заданных условиях.",
        "- Данные: только синтетические задачи стенда. Персональных данных и документов "
        "проекта в запросах нет.",
        "",
        "## Задачи",
        "",
        "| Задача | Вид | max_tokens | Что проверяет |",
        "|---|---|---|---|",
    ]
    for task in plan.tasks:
        lines.append(f"| {task.id} | {task.kind} | {task.max_tokens} | {task.note} |")
    lines += ["", f"Отпечаток `{plan.fingerprint()}` повторяется в отчёте: если он "
                  "совпадёт, план не менялся.", ""]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def summarise(rows: list[RunResult]) -> dict:
    """Сводка: главная метрика (первая попытка), все исходы и разбивка по задачам."""
    first = [row for row in rows if row.attempt == 1]
    total_first = len(first)
    qualified_first = sum(1 for row in first if row.outcome == QUALIFIED)
    technical = sum(1 for row in rows if row.outcome == TECHNICAL)
    by_task: dict[str, dict] = {}
    for row in rows:
        bucket = by_task.setdefault(row.task, {"kind": row.kind, "runs": 0,
                                               QUALIFIED: 0, UNQUALIFIED: 0, TECHNICAL: 0})
        bucket["runs"] += 1
        bucket[row.outcome] += 1
    by_kind: dict[str, dict] = {}
    for row in rows:
        bucket = by_kind.setdefault(row.kind, {"runs": 0, QUALIFIED: 0,
                                              UNQUALIFIED: 0, TECHNICAL: 0})
        bucket["runs"] += 1
        bucket[row.outcome] += 1
    answered = [row for row in first if row.outcome != TECHNICAL]
    over = sum(1 for row in rows if row.over_max_tokens)
    return {
        "over_max_tokens": over,
        "rows": len(rows),
        "first_attempts": total_first,
        "first_qualified": qualified_first,
        "first_attempt_rate": round(qualified_first / total_first, 4) if total_first else 0.0,
        # главная метрика дополняется второй: «годен среди дошедших ответов» —
        # иначе таймауты прячутся за «качеством»
        "qualified_among_answered": (
            round(sum(1 for row in answered if row.outcome == QUALIFIED) / len(answered), 4)
            if answered else 0.0),
        "technical": technical,
        "technical_rate": round(technical / len(rows), 4) if rows else 0.0,
        "by_task": by_task,
        "by_kind": by_kind,
        "wilson": wilson_interval(qualified_first, total_first),
    }


def wilson_interval(successes: int, total: int, z: float = 1.96) -> dict:
    """Интервал Уилсона: без него «80 %» и «80 % на 20 попытках» читаются одинаково."""
    if total == 0:
        return {"low": 0.0, "high": 0.0, "p": 0.0}
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denominator
    return {"low": round(max(0.0, centre - margin), 4),
            "high": round(min(1.0, centre + margin), 4), "p": round(p, 4)}
