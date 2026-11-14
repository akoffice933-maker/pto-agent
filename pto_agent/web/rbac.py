# -*- coding: utf-8 -*-
"""Матрица ролей: файл данных + проверка на сервере.

Права живут в `data/reference/rbac.json`, а не в шаблонах: шаблон может спрятать
кнопку, но отказать должен сервер, и отказ обязан быть с кодом и причиной. Поэтому
единственный входной путь — `Matrix.can(...)`, который вызывают обработчики, а не
разметка.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "reference" / "rbac.json"
SCHEMA = "pto-agent/rbac@1"


class RbacError(ValueError):
    """Файл матрицы не тот, который ожидает сервер."""


def load(path: str | Path | None = None) -> dict:
    path = Path(path or DEFAULT_PATH)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise RbacError(f"{path.name}: схема {payload.get('schema')!r} не {SCHEMA!r}")
    return payload


class Matrix:
    """Права ролей. Один экземпляр на приложение, менять его на ходу нельзя."""

    def __init__(self, data: dict):
        self.data = data
        self._permissions = {
            role: frozenset((info or {}).get("permissions") or [])
            for role, info in (data.get("roles") or {}).items()
        }
        self._titles = {role: (info or {}).get("title") or role
                        for role, info in (data.get("roles") or {}).items()}
        self._actions = dict(data.get("actions") or {})
        self._scope = dict(data.get("object_scope") or {})

    # ── роли и права ──────────────────────────────────────────────
    @property
    def roles(self) -> list[str]:
        return list(self._permissions)

    def role_title(self, role: str) -> str:
        return self._titles.get(role) or role

    def permissions(self, role: str) -> frozenset[str]:
        return self._permissions.get(role, frozenset())

    def action_title(self, action: str) -> str:
        base = action.split(":", 1)[0]
        return self._actions.get(base) or base

    def can(self, role: str, action: str, *, step: str | None = None) -> bool:
        """Может ли роль выполнить действие. Ступень маршрута — часть права."""
        grant = f"{action}:{step}" if step else action
        return grant in self.permissions(role)

    def why_not(self, role: str, action: str, *, step: str | None = None) -> str:
        grant = f"{action}:{step}" if step else action
        if role not in self._permissions:
            known = ", ".join(sorted(self._permissions)) or "нет"
            return f"роль «{role}» не описана в матрице; известные роли: {known}"
        return (f"у роли «{self.role_title(role)}» нет права «{self.action_title(grant)}» "
                f"({grant}) — действие выполняет другая роль")

    def allowed(self, role: str) -> set[str]:
        """Действия роли без ступеней — для меню. Проверка всё равно по `can`."""
        return {grant.split(":", 1)[0] for grant in self.permissions(role)}

    # ── объекты ───────────────────────────────────────────────────
    def scope_of(self, role: str) -> str:
        """`own` — только свои объекты, `all` — все. По умолчанию `own`."""
        return str(self._scope.get(role) or "own")

    def objects_for(self, role: str, user_objects: list[str], all_objects: list[str]) -> list[str]:
        if self.scope_of(role) == "all" or "*" in (user_objects or []):
            return list(all_objects)
        return [name for name in all_objects if name in set(user_objects or [])]

    def object_allowed(self, role: str, user_objects: list[str], object_name: str) -> bool:
        if self.scope_of(role) == "all" or "*" in (user_objects or []):
            return True
        return object_name in set(user_objects or [])

    # ── пояснения для экранов ─────────────────────────────────────
    def denials(self) -> list[str]:
        return list(self.data.get("rules") or [])

    def not_implemented(self) -> list[str]:
        return list(self.data.get("not_implemented") or [])
