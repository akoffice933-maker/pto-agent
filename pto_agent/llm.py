# -*- coding: utf-8 -*-
"""LLM-адаптер: провайдер-агностичный шлюз + маршрутизация по классу данных.

Работает по протоколу OpenAI-compatible (/v1/chat/completions).
Если ключи/эндпоинт не заданы — все шаги выполняются детерминированно, без модели.
Это осознанное решение для MVP: арифметика и правила не должны зависеть от LLM.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import yaml

from .netguard import (MAX_BYTES_LLM, UnsafeEndpoint, log_decision, open_bounded,
                       public_message, validate_url)

CONFIG_PATH = Path("config/llm_providers.yaml")

# Классы данных, которые нельзя отдавать за пределы контура ни при какой конфигурации.
RESTRICTED_DATA_CLASSES = {"commercial_secret", "personal"}


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def route(intent: str, data_class: str = "internal", has_images: bool = False,
          complexity: str = "medium", config: dict | None = None) -> dict:
    """Выбор правила маршрутизации — первое совпавшее. Логика повторяет config/llm_providers.yaml."""
    cfg = config if config is not None else load_config()
    providers = {p["id"]: p for p in cfg.get("providers", [])}
    rules = cfg.get("routing", [])

    for rule in rules:
        when = rule.get("when", {}) or {}
        ok = True
        if "data_class" in when and data_class not in when["data_class"]:
            ok = False
        if "intent" in when and intent not in when["intent"]:
            ok = False
        if "has_images" in when and bool(when["has_images"]) != bool(has_images):
            ok = False
        if "complexity" in when and when["complexity"] != complexity:
            ok = False
        if ok:
            chain = [p for p in rule.get("to", [])]
            chosen = chain[0] if chain else "local-qwen-32b"
            blocked = []
            for pid in chain:
                p = providers.get(pid, {})
                if p and data_class not in (p.get("data_classes") or []):
                    blocked.append(pid)
            return {
                "rule": rule.get("id"),
                "provider": chosen,
                "chain": chain,
                "blocked_by_policy": blocked,
                "rationale": rule.get("rationale", ""),
            }
    return {"rule": "default", "provider": "local-qwen-32b", "chain": ["local-qwen-32b"],
            "blocked_by_policy": [], "rationale": "fallback rule"}


def _auth_headers(provider: dict) -> dict:
    auth = provider.get("auth", {}) or {}
    headers = {"Content-Type": "application/json"}
    if auth.get("type") == "api_key":
        key = os.getenv(auth.get("key_env", ""), "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
    elif auth.get("type") == "iam":
        token = os.getenv(auth.get("token_env", ""), "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def check_provider_policy(provider: dict, data_class: str) -> str | None:
    """Политика маршрутизации данных: можно ли этому провайдеру отдать такой класс данных.

    Проверка стоит **в момент вызова**, а не только в `route()`: до этого `route()` считал
    `blocked_by_policy`, но `chat()` это игнорировал — достаточно было передать `provider_id`
    явно, чтобы КТ и персональные данные ушли в облако.
    """
    if not provider:
        return "провайдер не найден в конфиге"
    allowed = provider.get("data_classes")
    if allowed is not None and data_class not in allowed:
        return f"провайдер {provider.get('id')} не принимает класс данных «{data_class}»"
    if data_class in RESTRICTED_DATA_CLASSES and provider.get("residency") not in (
            "on_premise", "rf_cloud"):
        return (f"класс данных «{data_class}» нельзя отправлять за пределы контура "
                f"(residency={provider.get('residency')})")
    return None


def chat_ex(prompt: str, system: str = "", provider_id: str | None = None,
            intent: str = "doc_classify", data_class: str = "internal",
            timeout: int = 60) -> dict:
    """Вызов модели с объяснением: почему не позвали и куда собирались идти.

    Возвращает `{"ok", "text", "provider", "reason"}`. `ok=False` — это факт, а не ошибка
    программы: нет ключа, политика запретила, контур недоступен.
    """
    cfg = load_config()
    providers = {p["id"]: p for p in cfg.get("providers", [])}
    routed = route(intent, data_class, config=cfg)
    if provider_id is None:
        provider_id = routed["provider"]
    provider = providers.get(provider_id, {})
    if not provider:
        return {"ok": False, "text": None, "provider": provider_id,
                "reason": "провайдер не сконфигурирован"}

    refusal = check_provider_policy(provider, data_class)
    if refusal:
        sys.stderr.write(f"[llm] отказ маршрутизации: {refusal}\n")
        return {"ok": False, "text": None, "provider": provider_id, "reason": refusal}

    env_base = os.getenv("PTO_LLM_BASE_URL", "")
    base = env_base or provider.get("base_url", "")
    if env_base and data_class in RESTRICTED_DATA_CLASSES:
        # Переменная окружения легко подменяется (в т.ч. через CI/чужой .env), поэтому для
        # КТ и ПДн адрес берём только из конфига, прошедшего проверку политики.
        return {"ok": False, "text": None, "provider": provider_id,
                "reason": ("адрес провайдера переопределён переменной PTO_LLM_BASE_URL: "
                           f"для класса данных «{data_class}» это запрещено — "
                           "укажите адрес в config/llm_providers.yaml")}
    try:
        url = validate_url(base, purpose="LLM", allow_private=True,
                           allow_insecure_http=True) + "/v1/chat/completions"
    except UnsafeEndpoint as exc:
        log_decision("LLM", str(base), allowed=False, reason=exc.reason)
        return {"ok": False, "text": None, "provider": provider_id,
                "reason": public_message(exc)}
    log_decision("LLM", url, allowed=True)

    headers = _auth_headers(provider)
    if "Authorization" not in headers and provider.get("auth", {}).get("type") in ("api_key", "iam"):
        return {"ok": False, "text": None, "provider": provider_id,
                "reason": "нет ключа доступа в окружении"}

    body = {
        "model": provider.get("model"),
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        raw, _status, _headers = open_bounded(req, timeout=timeout, max_bytes=MAX_BYTES_LLM,
                                              purpose="LLM")
        data = json.loads(raw.decode("utf-8"))
        return {"ok": True, "text": data["choices"][0]["message"]["content"],
                "provider": provider_id, "reason": None}
    except UnsafeEndpoint as exc:
        return {"ok": False, "text": None, "provider": provider_id,
                "reason": public_message(exc)}
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as exc:
        # В журнал — по-человечески, клиенту — без адресов и тела ответа
        sys.stderr.write(f"[llm] ошибка вызова {provider_id}: {type(exc).__name__}\n")
        return {"ok": False, "text": None, "provider": provider_id,
                "reason": public_message(exc)}


def chat(prompt: str, system: str = "", provider_id: str | None = None,
         intent: str = "doc_classify", data_class: str = "internal",
         timeout: int = 60) -> Optional[str]:
    """Вызов модели. Возвращает None, если провайдер не сконфигурирован для этого контура."""
    return chat_ex(prompt, system=system, provider_id=provider_id, intent=intent,
                   data_class=data_class, timeout=timeout)["text"]


def describe_discrepancies(rec, use_llm: bool = False) -> str:
    """Пояснение к расхождениям. Без модели — шаблон; с моделью — переформулировка фактов."""
    crit = [d for d in rec.discrepancies if d.severity == "critical"]
    major = [d for d in rec.discrepancies if d.severity == "major"]
    facts = (f"Объект {rec.object_name}, период {rec.period[0]:%d.%m.%Y}–{rec.period[1]:%d.%m.%Y}. "
             f"Позиций в акте: {len(rec.lines_with_qty)}. Стоимость к предъявлению: "
             f"{rec.amount_act:,.2f} ₽, из них безопасно {rec.amount_act_safe:,.2f} ₽. "
             f"Критичных расхождений: {len(crit)}, существенных: {len(major)}. "
             f"Остаток по смете: {rec.amount_remainder:,.2f} ₽.")
    if not use_llm:
        return facts

    prompt = (facts + "\nСоставь краткое деловое резюме для руководителя ПТО: "
                       "что блокирует предъявление и что нужно сделать. Только факты из текста, без домыслов.")
    out = chat(prompt, system="Ты — инженер ПТО. Отвечай по-русски, кратко, со ссылками на факты.",
               intent="ks2_validate", data_class="internal")
    return out or facts
