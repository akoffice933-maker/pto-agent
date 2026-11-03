# -*- coding: utf-8 -*-
"""LLM-адаптер: провайдер-агностичный шлюз + маршрутизация по классу данных.

Работает по протоколу OpenAI-compatible (/v1/chat/completions).
Если ключи/эндпоинт не заданы — все шаги выполняются детерминированно, без модели.
Это осознанное решение для MVP: арифметика и правила не должны зависеть от LLM.
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import yaml

CONFIG_PATH = Path("config/llm_providers.yaml")


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


def chat(prompt: str, system: str = "", provider_id: str | None = None,
         intent: str = "doc_classify", data_class: str = "internal",
         timeout: int = 60) -> Optional[str]:
    """Вызов модели. Возвращает None, если провайдер не сконфигурирован для этого контура."""
    cfg = load_config()
    providers = {p["id"]: p for p in cfg.get("providers", [])}
    if provider_id is None:
        provider_id = route(intent, data_class, config=cfg)["provider"]
    provider = providers.get(provider_id, {})
    if not provider:
        return None

    base = os.getenv("PTO_LLM_BASE_URL", provider.get("base_url", ""))
    if base.startswith("http://10.") or not base.startswith("http"):
        # локальный контур недоступен из среды разработки
        return None
    headers = _auth_headers(provider)
    if "Authorization" not in headers and provider.get("auth", {}).get("type") in ("api_key", "iam"):
        return None

    body = {
        "model": provider.get("model"),
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError):
        return None


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
