# -*- coding: utf-8 -*-
"""Профили разбора PDF по типу объекта: КР, слаботочка, ОВиК.

Зачем
-----
`kr_pdf` написан под раздел КР: он ищет «ведомость объёмов работ», «ведомость расхода
стали», «перекрытия», а в конце спрашивает про арматуру и площадь застройки. На ТЗ по
модернизации СКС это даёт вопросы не по адресу: арматуры там нет, а объёмы лежат в
кабельном журнале и таблицах маршрутов.

Профиль — это данные, а не код: что искать (ведомости), какие параметры считать фактами
(с цитатой и страницей), что разрешено выводить расчётом, что спрашивать всегда и каким
текстом заканчивать отчёт. Список профилей — `data/reference/pdf_profiles.json`.

Три правила
-----------
1. **Профиль не расширяет права.** Он выбирает, что искать; правило «нет цитаты — нет
   факта» и «нет объёма — вопрос, а не ноль» остаются в силе для всех профилей.
2. **Непроверенный профиль говорит об этом.** Поле `validated_on`: `null` → в отчёте
   печатается «на реальном документе не проверялся», чтобы шаблон не выглядел гарантией.
3. **Неизвестный профиль — ошибка, а не тихий откат к КР.** Отчёт по чужому профилю хуже,
   чем явная ошибка: инженер увидит вопросы про арматуру и не поймёт, почему.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "pdf_profiles.json"
REQUIRED_KEYS = ("title", "schedules", "facts")
COMPILED = "_compiled"


class ProfileError(ValueError):
    """Профиль не найден или описан неверно — с указанием, что именно не так."""


def profiles_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else DEFAULT_PATH


def load_profiles(path: str | Path | None = None) -> dict:
    """Прочитать файл профилей и проверить его структуру."""
    file = profiles_path(path)
    if not file.exists():
        raise ProfileError(f"файл профилей не найден: {file}")
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ProfileError(f"{file.name}: не JSON ({error.msg}, строка {error.lineno})") from error
    profiles = payload.get("profiles") or {}
    if not profiles:
        raise ProfileError(f"{file.name}: нет ни одного профиля в «profiles»")
    for name, profile in profiles.items():
        missing = [key for key in REQUIRED_KEYS if key not in profile]
        if missing:
            raise ProfileError(f"профиль «{name}»: нет обязательных полей {', '.join(missing)}")
        if not isinstance(profile["facts"], list):
            raise ProfileError(f"профиль «{name}»: «facts» должен быть списком")
        for rule in profile["facts"]:
            if not (rule.get("name") and rule.get("pattern")):
                raise ProfileError(f"профиль «{name}»: у правила факта нет name или pattern")
            try:
                re.compile(rule["pattern"], re.I)
                if rule.get("reject"):
                    re.compile(rule["reject"], re.I)
            except re.error as error:
                raise ProfileError(f"профиль «{name}», факт «{rule.get('name')}»: "
                                   f"битое регулярное выражение — {error}") from error
    default = payload.get("default") or next(iter(profiles))
    if default not in profiles:
        raise ProfileError(f"профиль по умолчанию «{default}» отсутствует в файле")
    payload["default"] = default
    return payload


def list_profiles(path: str | Path | None = None) -> list[dict]:
    """Краткий список для CLI и документации: имя, заголовок, проверен ли на документе."""
    payload = load_profiles(path)
    out = []
    for name, profile in payload["profiles"].items():
        out.append({"name": name, "title": profile["title"],
                    "validated_on": profile.get("validated_on"),
                    "default": name == payload["default"],
                    "facts": len(profile["facts"]),
                    "schedules": len(profile["schedules"]),
                    "derive": list(profile.get("derive") or [])})
    return out


def get_profile(name: str | None = None, path: str | Path | None = None) -> dict:
    """Профиль с скомпилированными регулярками и меткой «проверен на документе»."""
    payload = load_profiles(path)
    key = name or payload["default"]
    if key not in payload["profiles"]:
        raise ProfileError(f"профиль «{key}» не найден; есть: {', '.join(payload['profiles'])}")
    profile = dict(payload["profiles"][key])
    profile["name"] = key
    profile[COMPILED] = [{"name": rule["name"],
                          "pattern": re.compile(rule["pattern"], re.I),
                          "reject": re.compile(rule["reject"], re.I) if rule.get("reject") else None,
                          "unit": rule.get("unit", ""),
                          "note": rule.get("note", "")}
                         for rule in profile["facts"]]
    profile["validated"] = bool(profile.get("validated_on"))
    profile["derive"] = list(profile.get("derive") or [])
    profile["always_ask"] = list(profile.get("always_ask") or [])
    return profile


def fact_rules(profile: dict) -> list[dict]:
    """Скомпилированные правила фактов (для `kr_pdf.extract_facts`)."""
    return profile.get(COMPILED) or []


def schedule_markers(profile: dict) -> dict[str, tuple[str, ...]]:
    """Что искать в томе: имя ведомости → маркеры строк."""
    return {name: tuple(markers) for name, markers in (profile.get("schedules") or {}).items()}


def can_derive(profile: dict, what: str) -> bool:
    """Разрешён ли конкретный расчёт объёма в этом профиле."""
    return what in (profile.get("derive") or [])


def describe(profile: dict) -> str:
    """Одна строка для отчёта: профиль, что проверено, чего ждать."""
    validity = (f"проверен на: {profile['validated_on']}" if profile.get("validated")
                else "на реальном документе **не проверялся** — трактуйте как шаблон")
    derives = ", ".join(profile.get("derive") or []) or "расчётом объёмов не выводим"
    return f"профиль **{profile['name']}** ({profile['title']}); {validity}; {derives}"
