# -*- coding: utf-8 -*-
"""Пользователи и пароли для экранов.

Пароли не хранятся в открытом виде: PBKDF2-HMAC-SHA256 с солью на пользователя.
Файл `data/reference/users.json` — демонстрационный набор; в реальной эксплуатации
его заменяет интеграция с AD/LDAP (пока не сделана и честно указана в «не сделано»).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "reference" / "users.json"
SCHEMA = "pto-agent/users@1"
ITERATIONS = 200_000


class UsersError(ValueError):
    pass


def hash_password(password: str, *, salt: str | None = None,
                  iterations: int = ITERATIONS) -> dict:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), iterations)
    return {"algo": "pbkdf2_sha256", "salt": salt, "iterations": iterations,
            "hash": digest.hex()}


def verify_password(user: dict, password: str) -> bool:
    spec = (user or {}).get("password") or {}
    if not spec.get("hash") or not spec.get("salt"):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"),
                                 bytes.fromhex(spec["salt"]),
                                 int(spec.get("iterations") or ITERATIONS))
    return hmac.compare_digest(digest.hex(), str(spec["hash"]))


def load(path: str | Path | None = None) -> dict:
    path = Path(path or DEFAULT_PATH)
    if not path.exists():
        return {"schema": SCHEMA, "users": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise UsersError(f"{path.name}: схема {payload.get('schema')!r} не {SCHEMA!r}")
    return payload


def index(payload: dict) -> dict[str, dict]:
    return {str(row.get("login")): row for row in payload.get("users") or []
            if row.get("login")}


def authenticate(payload: dict, login: str, password: str) -> tuple[dict | None, str]:
    """Проверить вход. Возвращает (пользователь, причина отказа)."""
    login = str(login or "").strip()
    user = index(payload).get(login)
    if not user:
        return None, "нет такого логина"
    if not user.get("active", True):
        return None, "учётная запись отключена"
    if not verify_password(user, password):
        return None, "неверный пароль"
    return user, ""


def public(user: dict) -> dict:
    """Пользователь без хэша пароля — то, что можно класть в сессию и шаблон."""
    return {key: value for key, value in (user or {}).items() if key != "password"}


def build_demo_users() -> dict:
    """Демонстрационный набор: по человеку на роль плюс сервисный ИИ-агент.

    Пароли одинаковы по построению (`demo-<роль>`) и это написано на странице входа:
    это витрина ролей, а не боевая аутентификация.
    """
    rows = [
        {"login": "prokhorov", "name": "Прохоров П.П.", "role": "прораб",
         "objects": ["ЖК «Северный», корпус 2"], "password_text": "demo-prokhorov",
         "note": "подаёт требование М-11 и заявку по своему объекту"},
        {"login": "snab", "name": "Снабженцев С.С.", "role": "снабженец",
         "objects": ["ЖК «Северный», корпус 2"], "password_text": "demo-snab",
         "note": "приход на склад и первая ступень согласования"},
        {"login": "pto", "name": "Сметчикова А.А.", "role": "ПТО",
         "objects": ["ЖК «Северный», корпус 2"], "password_text": "demo-pto",
         "note": "акты КС-2 и вторая ступень согласования"},
        {"login": "buh", "name": "Бухгалтерова Б.Б.", "role": "бухгалтер",
         "objects": ["*"], "password_text": "demo-buh",
         "note": "проводки, оплата, закрытие месяца"},
        {"login": "dir", "name": "Директоров Д.Д.", "role": "руководитель",
         "objects": ["*"], "password_text": "demo-dir",
         "note": "дашборд и ступень «финдиректор»"},
        {"login": "admin", "name": "Админов А.А.", "role": "администратор",
         "objects": [], "password_text": "demo-admin",
         "note": "администрирование: прав на проводки нет"},
        {"login": "agent", "name": "ИИ-агент (сервис)", "role": "ИИ-агент",
         "kind": "agent", "objects": [], "password_text": "demo-agent",
         "note": "читает и предлагает; проводить, согласовывать и закрывать не может"},
    ]
    users = []
    for row in rows:
        row = dict(row)
        users.append({
            "login": row["login"], "name": row["name"], "role": row["role"],
            "kind": row.get("kind") or "human",
            "objects": row["objects"], "active": True, "note": row["note"],
            "password": hash_password(row.pop("password_text")),
        })
    return {
        "schema": SCHEMA,
        "title": "Пользователи демонстрационного контура",
        "note": "Пароли — PBKDF2-SHA256. Логины и пароли ниже только для демонстрации; "
                "боевая аутентификация (AD/LDAP, 2FA) не сделана.",
        "demo_logins": [{"login": row["login"], "password": row["password_text"],
                         "role": row["role"]} for row in rows],
        "users": users,
    }
