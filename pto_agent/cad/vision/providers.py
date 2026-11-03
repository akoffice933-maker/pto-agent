# -*- coding: utf-8 -*-
"""VL-провайдеры: обращение к локальной vision-модели по OpenAI-совместимому API.

Модель не «вшита» в код: провайдер выбирается конфигом config/llm_providers.yaml,
адрес можно переопределить переменной окружения PTO_VL_BASE_URL. Каждый вызов
пишется в журнал (модель, задержка, токены, стоимость, результат валидации).
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONFIG_PATH = Path("config/llm_providers.yaml")


class ProviderUnavailable(RuntimeError):
    """Провайдер не сконфигурирован/недоступен — вызывающий код решает, что делать."""


class ProviderError(RuntimeError):
    """Провайдер ответил, но ответ нельзя разобрать."""


@dataclass
class VisionRequest:
    image_bytes: bytes
    mime: str = "image/png"
    system: str = ""
    user: str = ""
    prompt_id: str = "redline_scan"
    prompt_version: str = "1.0"


@dataclass
class VisionResponse:
    text: str
    json: dict | None
    model: str
    provider: str
    latency_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    cost_rub: float = 0.0
    error: str | None = None
    raw_usage: dict = field(default_factory=dict)


def _read_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def resolve_vision_provider(provider_id: str | None = None,
                            config: dict | None = None) -> dict:
    """Найти пригодного для vision провайдера и его настройки (без обращения к сети)."""
    cfg = config if config is not None else _read_config()
    providers = cfg.get("providers", []) or []
    pid = provider_id or os.getenv("PTO_VL_PROVIDER")
    candidates = []
    for p in providers:
        if not isinstance(p, dict):
            continue
        if pid and p.get("id") != pid:
            continue
        if not p.get("vision") or not p.get("enabled"):
            continue
        candidates.append(p)
    if not candidates:
        raise ProviderUnavailable(
            f"в {CONFIG_PATH} нет включённого провайдера с vision=true"
            + (f" (искали id={pid})" if pid else ""))

    p = dict(candidates[0])
    base = os.getenv("PTO_VL_BASE_URL", p.get("base_url", ""))
    p["base_url"] = base
    p["model"] = os.getenv("PTO_VL_MODEL", p.get("model", ""))
    if not _base_is_reachable_shape(base):
        raise ProviderUnavailable(
            f"провайдер {p['id']}: адрес {base} — внутренний контур, из этой среды недоступен. "
            "Задайте PTO_VL_BASE_URL (например http://10.20.0.12:8000) в контуре заказчика "
            "или используйте --engine offline / mock.")
    return p


def _base_is_reachable_shape(base: str) -> bool:
    """Отличить «настоящий» адрес от заглушки внутреннего контура (10.x, localhost-пауза)."""
    if not base or not base.startswith("http"):
        return False
    host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
    if os.getenv("PTO_VL_ALLOW_PRIVATE") == "1":
        return True
    return not (host.startswith("10.") or host.startswith("192.168.")
                or host.startswith("172.16."))


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


class OpenAIVisionProvider:
    """Провайдер по протоколу OpenAI (/v1/chat/completions, поле image_url)."""

    def __init__(self, settings: dict, timeout: int = 90, max_retries: int = 1):
        self.settings = settings
        self.id = settings.get("id", "vlm")
        self.model = settings.get("model", "")
        self.base_url = str(settings.get("base_url", "")).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.price_in = float(settings.get("price_per_1k_in_rub") or 0)
        self.price_out = float(settings.get("price_per_1k_out_rub") or 0)

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        auth = self.settings.get("auth") or {}
        env = auth.get("key_env") or auth.get("token_env")
        if env and os.getenv(env):
            headers["Authorization"] = f"Bearer {os.getenv(env)}"
        return headers

    def _poster(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ── вызов ─────────────────────────────────────────────────────────────
    def analyze(self, request: VisionRequest) -> VisionResponse:
        b64 = base64.b64encode(request.image_bytes).decode("ascii")
        body = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": [
                    {"type": "text", "text": request.user},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{request.mime};base64,{b64}"}},
                ]},
            ],
        }
        last_error = None
        started = time.time()
        for attempt in range(self.max_retries + 1):
            try:
                data = self._poster(f"{self.base_url}/v1/chat/completions", body)
                latency = int((time.time() - started) * 1000)
                text = ""
                try:
                    text = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ProviderError(f"неожидаемый формат ответа: {exc}") from exc
                usage = data.get("usage") or {}
                tin = int(usage.get("prompt_tokens") or 0)
                tout = int(usage.get("completion_tokens") or 0)
                parsed = None
                try:
                    parsed = json.loads(_strip_code_fences(text))
                except json.JSONDecodeError as exc:
                    last_error = f"ответ не JSON: {exc}"
                return VisionResponse(
                    text=text, json=parsed, model=self.model, provider=self.id,
                    latency_ms=latency, tokens_in=tin, tokens_out=tout,
                    cost_rub=round(tin / 1000 * self.price_in + tout / 1000 * self.price_out, 4),
                    error=None if parsed is not None else last_error,
                    raw_usage=usage)
            except (urllib.error.URLError, TimeoutError, ProviderError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.5 * (attempt + 1))
        raise ProviderError(f"провайдер {self.id} недоступен: {last_error}")


class LLMCallLog:
    """Журнал вызовов моделей (JSONL): кто, что, сколько стоил, принят ли ответ."""

    def __init__(self, path: str | Path = "out/cad/llm_calls.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, *, provider: str, model: str, prompt_id: str, prompt_version: str,
               image_bytes: bytes, response: VisionResponse | None,
               validated: bool, marks_accepted: int = 0, marks_rejected: int = 0,
               error: str | None = None, extra: dict | None = None) -> dict:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provider": provider,
            "model": model,
            "prompt": {"id": prompt_id, "version": prompt_version},
            "image_sha256": hashlib.sha256(image_bytes).hexdigest()[:16] if image_bytes else None,
            "latency_ms": response.latency_ms if response else None,
            "tokens_in": response.tokens_in if response else 0,
            "tokens_out": response.tokens_out if response else 0,
            "cost_rub": response.cost_rub if response else 0.0,
            "validated": validated,
            "marks_accepted": marks_accepted,
            "marks_rejected": marks_rejected,
            "error": error,
            **(extra or {}),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec


def provider_info(settings: dict) -> dict:
    """Краткая карточка провайдера для отчётов (без секретов)."""
    keep = ("id", "kind", "model", "base_url", "context_window", "vision",
            "residency", "price_per_1k_in_rub", "price_per_1k_out_rub")
    return {k: settings.get(k) for k in keep}
