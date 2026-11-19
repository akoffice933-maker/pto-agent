# -*- coding: utf-8 -*-
"""Harness-слой агента: что агенту можно, где границы и что он обязан доказать.

Зачем отдельный модуль
----------------------
До этого файла границы были разбросаны: `netguard` ограничивает сеть, `audit` пишет
журнал, `sign` требует ФИО и роль, `review` решает «можно подписывать». Но нигде не было
одного места, где написано: **какой у агента набор возможностей, какие из них требуют
человека, что считается доказательством и чего агент не имеет права делать вообще**.

Из этого файла это место. Он не «включает» ограничения — они уже работают в своих модулях.
Он их собирает в манифест и проверяет, что заявленное совпадает с настоящим: у каждого
инструмента есть файл реализации, у каждого опасного действия — точка контроля, у каждого
результата — обязательное свидетельство (источник, файл, цитата).

Правило, которое здесь закреплено: надёжность агента — не в длине инструкции, а в наборе
проверяемых ограничений. Поэтому манифест — данные, и по нему есть тест.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Уровни автономии. Порядок важен: выше — больше последствий.
L_AUTO = "auto"          # агент делает сам, человек не нужен, действие обратимо
L_CONFIRM = "confirm"    # агент готовит, человек подтверждает (ФИО и роль в журнале)
L_HUMAN = "human"        # агент не делает вообще: только собирает материалы для человека
L_NEVER = "never"        # агент не делает и не предлагает — это вне его роли


@dataclass
class Tool:
    """Один инструмент агента с уровнем автономии и доказательством, которое он обязан дать."""
    name: str
    level: str
    proof: str                    # что агент обязан предъявить вместе с результатом
    cli: str                      # как это вызывается (для проверки, что не выдумано)
    module: str                   # файл реализации — проверяется тестом
    why: str = ""                 # почему такой уровень, а не другой

    def as_dict(self) -> dict:
        return {"name": self.name, "level": self.level, "proof": self.proof,
                "cli": self.cli, "module": self.module, "why": self.why}


def tools() -> list[Tool]:
    """Манифест инструментов. Реальные команды, реальные файлы — тест это проверит."""
    return [
        Tool("сверка сметы и журнала", L_AUTO,
             "по каждой строке: шифр, объём, цена, источник цены и версия выгрузки",
             "pto_agent review / run", "pto_agent/reconcile.py",
             "пересчёт не меняет документы, ошибку видно сразу"),
        Tool("акт КС-2", L_CONFIRM,
             "источник каждой позиции; флаг «объём не подтверждён журналом»; пустые реквизиты — в запросы",
             "pto_agent forms ks2", "pto_agent/forms.py",
             "печатная форма — черновик; подпись и УКЭП не в роли агента"),
        Tool("цены ресурсов", L_AUTO,
             "источник, дата действия, регион, версия выгрузки, digest — или отказ not_found",
             "pto_agent prices lookup", "pto_agent/prices.py",
             "цена без даты и источника не используется"),
        Tool("нормативный ответ", L_AUTO,
             "дословная цитата и пункт; при отсутствии — «не найдено», а не пересказ",
             "pto_agent norms", "pto_agent/norms.py",
             "ответ без цитаты не выдаётся"),
        Tool("проверка сметы по индексу", L_AUTO,
             "ряд индексов, медиана, ряд демонтажа, список необъяснённых выбросов",
             "scripts/check_implicit_index.py", "scripts/check_implicit_index.py",
             "не пересчитывает смету — только показывает отношение"),
        Tool("объёмы по чертежу", L_CONFIRM,
             "страница/лист, единица, уверенность и что не распознано",
             "pto_agent takeoff pdf", "pto_agent/takeoff.py",
             "распознавание ошибается; объём без подтверждения не идёт в акт"),
        Tool("подпись документа", L_HUMAN,
             "ФИО и роль человека из справочника пользователей",
             "pto_agent sign", "pto_agent/sign.py",
             "подпись — действие человека; агент только готовит"),
        Tool("проводка в учёте", L_CONFIRM,
             "план проводок с основанием; фактическая проводка — только по команде человека",
             "pto_agent ledger", "pto_agent/ledger.py",
             "регистры ведутся всегда, проводки — по решению человека"),
        Tool("отправка контрагенту", L_NEVER,
             "—",
             "—", "pto_agent/edo.py",
             "внешняя отправка — не действие агента ни в каком виде"),
        Tool("изменение базы нормативов", L_HUMAN,
             "источник (номер приказа/письма) и digest файла",
             "scripts/import_fsnb.py", "scripts/import_fsnb.py",
             "нормативы меняются только решением человека, с указанием документа"),
    ]


# ─────────────────────────── границы ───────────────────────────
@dataclass
class Boundary:
    """Что агенту запрещено всегда — независимо от инструкций и настроек."""
    what: str
    enforced_in: str       # файл, где это реально проверяется
    test: str              # тест, который это доказывает


def boundaries() -> list[Boundary]:
    return [
        Boundary("ходить по file:// и другим схемам, кроме http/https",
                 "pto_agent/netguard.py", "tests/test_net_and_parsers.py"),
        Boundary("ходить в облачные метаданные и link-local адреса",
                 "pto_agent/netguard.py", "tests/test_net_and_parsers.py"),
        Boundary("отдавать пароль по открытому http на внешний хост",
                 "pto_agent/netguard.py", "tests/test_net_and_parsers.py"),
        Boundary("следовать редиректу на другой хост с заголовком Authorization",
                 "pto_agent/netguard.py", "tests/test_net_and_parsers.py"),
        Boundary("подписывать документы вместо человека",
                 "pto_agent/sign.py", "tests/test_forms.py"),
        Boundary("проводить бухгалтерские документы (проводки) самостоятельно",
                 "pto_agent/ledger.py", "tests/test_ledger.py"),
        Boundary("выдумывать цену, объём или норматив при отсутствии данных",
                 "pto_agent/numeric.py", "tests/test_integrity_numbers.py"),
        Boundary("удалять хвост журнала аудита",
                 "pto_agent/audit.py", "tests/test_journals.py"),
    ]


# ─────────────────────────── память ───────────────────────────
def memory_index() -> dict:
    """Где у агента «память»: что и из какого файла он помнит между запусками."""
    return {
        "schema": "pto-agent/harness-memory@1",
        "items": [
            {"what": "решения человека", "file": "out/*/audit.jsonl",
             "kept": "весь журнал, с hash-цепочкой против удаления хвоста"},
            {"what": "версии цен", "file": "data/prices_fsnb/index.json",
             "kept": "каждая выгрузка отдельным снимком, старая не перезаписывается"},
            {"what": "правила сопоставления", "file": "data/reference/takeoff_map*.json",
             "kept": "в git, с указанием источника каждого кода"},
            {"what": "нормативы", "file": "data/norms/*.md", "kept": "текст + пункт + редакция"},
            {"what": "результаты проверок", "file": "out/eval/report.json",
             "kept": "последний прогон + baseline для сравнения с прошлым"},
        ],
        "forgotten_on_purpose": [
            "промежуточные объяснения LLM — не источник истины, не храним",
            "сырые сканы после разбора — остаются у пользователя, не в базе агента",
        ],
    }


def manifest() -> dict:
    """Манифест целиком: он же вход для eval — проверки берут ожидания отсюда."""
    ts = [t.as_dict() for t in tools()]
    body = {
        "schema": "pto-agent/harness@1",
        "levels": {"auto": "делает сам", "confirm": "готовит, человек подтверждает",
                   "human": "делает только человек", "never": "вне роли агента"},
        "tools": ts,
        "boundaries": [b.__dict__ for b in boundaries()],
        "memory": memory_index(),
        "counts": {"tools": len(ts),
                   "by_level": _count_by(ts, "level"),
                   "boundaries": len(boundaries())},
    }
    body["digest"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return body


def _count_by(items: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for item in items:
        out[item[key]] = out.get(item[key], 0) + 1
    return out


def to_markdown(man: dict | None = None) -> str:
    man = man or manifest()
    c = man["counts"]
    lines = ["# Harness: границы, инструменты, доказательства", "",
             f"Инструментов: {c['tools']} · уровни: "
             + ", ".join(f"{k} — {v}" for k, v in sorted(c["by_level"].items()))
             + f" · запретов: {c['boundaries']}", "",
             "| Инструмент | Уровень | Что агент обязан предъявить | Почему так |",
             "|---|---|---|---|"]
    for t in man["tools"]:
        lines.append(f"| {t['name']} | **{t['level']}** | {t['proof']} | {t['why']} |")
    lines += ["", "## Что запрещено всегда", "",
              "| Запрет | Где проверяется | Тест |", "|---|---|---|"]
    for b in man["boundaries"]:
        lines.append(f"| {b['what']} | `{b['enforced_in']}` | `{b['test']}` |")
    lines += ["", "## Память агента", "", "| Что помнит | Где | Как хранится |", "|---|---|---|"]
    for m in man["memory"]["items"]:
        lines.append(f"| {m['what']} | `{m['file']}` | {m['kept']} |")
    lines += ["", "Сознательно не хранится:", ""]
    lines += [f"- {x}" for x in man["memory"]["forgotten_on_purpose"]]
    return "\n".join(lines) + "\n"
