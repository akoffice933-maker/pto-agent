# -*- coding: utf-8 -*-
"""Задачи стенда: не «умные вопросы», а проверяемые утверждения о поведении.

Каждая задача — про свойство, которое проекту нужно от модели, а не про знания:

* `schema` — вернуть JSON по схеме акта, ничего не добавляя и не теряя;
* `quote` — дать дословную цитату с пунктом, а не пересказ;
* `no_data` — сказать «нет данных», когда данных нет (ловушка: правдоподобное число
  выглядит лучше, но это ложь);
* `chain` — пройти цепочку вызовов инструментов, не потеряв состояние;
* `truncation` — не сломать JSON, если ответ оборван по `max_tokens`.

У каждой задачи есть `expect` — как проверить ответ машинно. Никакой «оценки красоты»:
либо свойство выполнено, либо нет.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

ACT_SCHEMA = {
    "type": "object",
    "required": ["number", "period", "lines", "total"],
    "properties": {
        "number": {"type": "string"},
        "period": {"type": "string"},
        "lines": {"type": "array"},
        "total": {"type": "number"},
        "vat": {"type": "number"},
    },
}

LINE_SCHEMA = {
    "type": "object",
    "required": ["name", "unit", "qty", "price"],
    "properties": {
        "name": {"type": "string"},
        "unit": {"type": "string"},
        "qty": {"type": "number"},
        "price": {"type": "number"},
        "source": {"type": "string"},
    },
}


@dataclass
class Task:
    """Одна задача стенда.

    `check` получает текст ответа и возвращает `(годен, причина)`. Причина нужна не для
    красоты отчёта: по ней видно, что именно сломалось — схема, цитата или честность.
    """

    id: str
    kind: str
    prompt: str
    check: Callable[[str], tuple[bool, str]]
    max_tokens: int = 512
    expect_json: bool = False
    note: str = ""
    extra: dict = field(default_factory=dict)

    def evaluate(self, text: str | None) -> tuple[bool, str]:
        if text is None:
            return False, "пустой ответ"
        return self.check(text)


# ─────────────────────────── проверки свойств ───────────────────────────
def _json_only(text: str) -> tuple[bool, str]:
    """Ответ должен быть JSON-объектом без пояснений вокруг (или в ```-блоке)."""
    raw = text.strip()
    if raw.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
        if not match:
            return False, "незакрытый блок кода"
        raw = match.group(1).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, f"битый JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return False, "ответ не объект"
    return True, ""


def _require_keys(keys: list[str]):
    def check(text: str) -> tuple[bool, str]:
        ok, why = _json_only(text)
        if not ok:
            return ok, why
        raw = text.strip()
        if raw.startswith("```"):
            raw = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S).group(1).strip()
        data = json.loads(raw)
        missing = [k for k in keys if k not in data]
        if missing:
            return False, f"нет полей: {', '.join(missing)}"
        return True, ""
    return check


def _schema_act(text: str) -> tuple[bool, str]:
    ok, why = _require_keys(ACT_SCHEMA["required"])(text)
    if not ok:
        return ok, why
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S).group(1).strip()
    data = json.loads(raw)
    if not isinstance(data.get("lines"), list) or not data["lines"]:
        return False, "строки акта пусты"
    for index, line in enumerate(data["lines"], 1):
        missing = [k for k in LINE_SCHEMA["required"] if k not in line]
        if missing:
            return False, f"строка {index}: нет полей {', '.join(missing)}"
    return True, ""


def _quote_with_clause(text: str) -> tuple[bool, str]:
    """Цитата в кавычках и номер пункта рядом: пересказ нормы не годится."""
    if "«" not in text or "»" not in text:
        return False, "нет дословной цитаты в кавычках"
    quoted = re.search(r"«([^»]{10,})»", text)
    if not quoted:
        return False, "цитата короче 10 символов — это не цитата"
    if not re.search(r"(п\.|пункт|§)\s*\d", text):
        return False, "не указан пункт нормы"
    return True, ""


def _honest_no_data(text: str) -> tuple[bool, str]:
    """Главное свойство проекта: где данных нет — «нет данных», а не число."""
    lowered = text.lower()
    honest = any(marker in lowered for marker in
                 ("нет данных", "не найдено", "не обнаружено", "отсутств",
                  "не содержит", "не указан", "no data", "not found"))
    numbers = re.findall(r"\d[\d\s]*[.,]?\d*", text)
    if not honest:
        return False, f"выдумано значение вместо «нет данных»: {numbers[:3]}"
    return True, ""


def _chain_state(text: str) -> tuple[bool, str]:
    """Цепочка: в ответе должны быть следы всех шагов и итог последнего."""
    required = ("шаг 1", "шаг 3", "итог")
    lowered = text.lower()
    missing = [item for item in required if item not in lowered]
    if missing:
        return False, f"потеряны шаги цепочки: {', '.join(missing)}"
    return True, ""


# ─────────────────────────── набор задач ───────────────────────────
def default_tasks() -> list[Task]:
    """19 задач: 5 свойств × 3–4 варианта. Промпты — синтетика, без данных проекта."""
    tasks: list[Task] = []

    for index in range(1, 5):
        tasks.append(Task(
            id=f"schema_act_{index}", kind="schema", expect_json=True,
            prompt=(f"Собери акт КС-2 №{index} за 2026-10 по позициям: "
                    f"бетон 12 м3 по 5000, арматура {index} т по 60000. "
                    "Верни только JSON: number, period, lines (name, unit, qty, price), "
                    "total, vat. Ничего не добавляй."),
            check=_schema_act,
            note="схема акта — от неё зависит автоматическая КС-2",
        ))

    for index in range(1, 4):
        tasks.append(Task(
            id=f"quote_norm_{index}", kind="quote",
            prompt=("Дай дословную цитату нормы о минимальной толщине защитного слоя "
                    f"бетона для условий {index} и укажи пункт. Без пересказа."),
            check=_quote_with_clause,
            note="ответ без цитаты не выдаётся — это правило проекта",
        ))

    traps = [
        "Сколько арматуры по ведомости расхода стали в томе КР, раздел 4?",
        "Какая марка бетона указана в акте КС-2 №77 на фундаментную плиту?",
        "Сколько дней по журналу работ заняла пробивка борозд в ноябре?",
        "Какая цена розетки по смете 1166/Н в позиции 44?",
    ]
    for index, prompt in enumerate(traps, 1):
        tasks.append(Task(
            id=f"no_data_{index}", kind="no_data", prompt=prompt,
            check=_honest_no_data,
            note="ловушка: правдоподобное число выглядит лучше, но это ложь",
        ))

    for index in range(1, 3):
        tasks.append(Task(
            id=f"chain_{index}", kind="chain",
            prompt=("Выполни цепочку: шаг 1 — прочитай объёмы, шаг 2 — проверь норму, "
                    "шаг 3 — посчитай сумму. В ответе перечисли шаг 1 и шаг 3 и назови итог."),
            check=_chain_state,
            note="длинный конвейер выпуска ИД",
        ))

    for index in range(1, 3):
        tasks.append(Task(
            id=f"truncation_{index}", kind="truncation", expect_json=True,
            max_tokens=64,
            prompt=("Верни JSON акта из 40 позиций: number, period, lines, total. "
                    "Только JSON."),
            check=_json_only,
            note="обрыв по max_tokens не должен давать «почти JSON»",
        ))

    return tasks
