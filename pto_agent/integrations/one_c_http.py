# -*- coding: utf-8 -*-
"""Уровень 2 обмена с 1С/БИТ: HTTP-сервис вместо CSV.

Зачем
-----
CSV-обмен (уровень 1, `one_c.py`) хорош тем, что работает без прав в конфигурации, но требует
человека с файлом. Если в 1С можно опубликовать HTTP-сервис, тот же обмен идёт напрямую:
забрали акты за период, посчитали, вернули проверенную КС-2 — с теми же полями, что в CSV.

Поля те же намеренно: переход с уровня 1 на уровень 2 не меняет ни расчёты, ни тесты —
меняется только транспорт. Поэтому нормализация (`normalize_smeta_rows`, `group_acts`)
переиспользуется из `one_c.py`, а не пишется заново.

Что важно для промышленной эксплуатации (и реализовано здесь)
-------------------------------------------------------------
* **Таймаут и различимые ошибки:** сеть, 401/403, 4xx и 5xx — разные типы исключений,
  чтобы агент не путал «нет прав» с «сервер упал».
* **Повтор только там, где безопасно:** 5xx и обрыв сети повторяются (идемпотентность
  обеспечивает `Idempotency-Key`), 4xx — нет: повтор не исправит неверный запрос.
* **Idempotency-Key** = sha256 от канонического JSON полезной нагрузки: повторная отправка
  того же результата не создаёт второй документ в 1С.
* **Никаких «тихих» нулей:** позиция без объёма уходит как `null` с пометкой `not_estimated`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .one_c import (OneCError, _fmt, _num, acts_document, group_acts,
                    normalize_smeta_rows)

DEFAULT_TIMEOUT = 30.0
RETRY_STATUSES = {500, 502, 503, 504}


class OneCHttpError(OneCError):
    """Базовая ошибка HTTP-обмена."""


class OneCAuthError(OneCHttpError):
    """401/403: нет прав или неверные учётные данные."""


class OneCRequestError(OneCHttpError):
    """4xx: запрос неверный — повторять бессмысленно."""


class OneCServerError(OneCHttpError):
    """5xx или сеть: сервер недоступен; повтор возможен."""


class OneCHttpClient:
    """Клиент HTTP-сервиса 1С. Аутентификация — basic или заголовок с токеном."""

    def __init__(self, base_url: str, *, user: str | None = None, password: str | None = None,
                 token: str | None = None, token_header: str = "Authorization",
                 token_prefix: str = "Bearer ", timeout: float = DEFAULT_TIMEOUT,
                 retries: int = 2, retry_delay: float = 0.5,
                 headers: dict | None = None):
        if not base_url:
            raise OneCHttpError("не задан адрес HTTP-сервиса 1С")
        self.base_url = base_url.rstrip("/")
        self.user, self.password = user, password
        self.token, self.token_header, self.token_prefix = token, token_header, token_prefix
        self.timeout, self.retries, self.retry_delay = float(timeout), int(retries), retry_delay
        self.extra_headers = dict(headers or {})
        self.calls: list[dict] = []          # журнал вызовов: что и когда дернули

    # ── конфиг ──
    @classmethod
    def from_config(cls, cfg: dict) -> "OneCHttpClient":
        """Конфиг из YAML/JSON: url, user/password или token, таймауты, заголовки."""
        if not isinstance(cfg, dict):
            raise OneCHttpError("конфиг должен быть объектом")
        url = cfg.get("url") or cfg.get("base_url")
        if not url:
            raise OneCHttpError("в конфиге нет url HTTP-сервиса 1С")
        return cls(url,
                   user=cfg.get("user"), password=cfg.get("password"),
                   token=cfg.get("token"), token_header=cfg.get("token_header") or "Authorization",
                   token_prefix=cfg.get("token_prefix", "Bearer "),
                   timeout=cfg.get("timeout_s", DEFAULT_TIMEOUT),
                   retries=cfg.get("retries", 2),
                   retry_delay=cfg.get("retry_delay_s", 0.5),
                   headers=cfg.get("headers") or {})

    # ── транспорт ──
    def _headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": "pto-agent/1c-http"}
        h.update(self.extra_headers)
        if self.token:
            h[self.token_header] = f"{self.token_prefix}{self.token}"
        elif self.user is not None:
            raw = f"{self.user}:{self.password or ''}".encode("utf-8")
            h["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return h

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, idempotency_key: str | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key

        last_error = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            started = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    status = r.status
                self.calls.append({"method": method, "url": url, "status": status,
                                   "ms": round(1000 * (time.time() - started))})
                return _parse_json(raw, url)
            except urllib.error.HTTPError as e:
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8", "replace")[:500]
                except Exception:                                    # noqa: BLE001
                    pass
                self.calls.append({"method": method, "url": url, "status": e.code,
                                   "ms": round(1000 * (time.time() - started))})
                if e.code in (401, 403):
                    raise OneCAuthError(f"1С отклонила доступ ({e.code}) на {url}: {body_text}") from e
                if e.code in RETRY_STATUSES and attempt < self.retries:
                    last_error = OneCServerError(f"1С ответила {e.code} на {url}: {body_text}")
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
                if e.code in RETRY_STATUSES:
                    raise OneCServerError(f"1С ответила {e.code} на {url}: {body_text}") from e
                raise OneCRequestError(f"запрос отклонён ({e.code}) {url}: {body_text}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self.calls.append({"method": method, "url": url, "status": None,
                                   "ms": round(1000 * (time.time() - started))})
                last_error = OneCServerError(f"1С недоступна ({url}): {e}")
                if attempt < self.retries:
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
                raise last_error from e
        raise last_error or OneCHttpError("неизвестная ошибка обмена")

    # ── операции обмена ──
    def ping(self) -> dict:
        """Проверка доступности: любой ответ 2xx считается «сервис жив»."""
        return self.request("GET", "health")

    def fetch_acts(self, period: str | None = None, act_id: str | None = None,
                   path: str = "acts") -> dict:
        """Акты КС-2 из 1С (по периоду/номеру) → структура `acts.json`."""
        payload = self.request("GET", path, params={"period": period, "act_id": act_id})
        imported = acts_from_json(payload)
        doc = acts_document(imported, object_name=payload.get("object") or "из 1С")
        return {"document": doc, "imported": imported, "raw": payload}

    def fetch_estimate(self, estimate_id: str | None = None,
                       path: str = "estimates/{id}/lines") -> dict:
        """Строки сметы из 1С → `summary["lines"]`-совместимые позиции."""
        if "{id}" in path:
            path = path.replace("{id}", str(estimate_id or ""))
        payload = self.request("GET", path)
        return {"smeta": smeta_from_json(payload), "raw": payload}

    def push_validated_ks2(self, payload: dict, path: str = "validated-ks2") -> dict:
        """Отправить проверенную КС-2 в 1С. Идемпотентно по содержимому."""
        key = payload.get("idempotency_key") or idempotency_key(payload)
        body = dict(payload, idempotency_key=key)
        return self.request("POST", path, body=body, idempotency_key=key)


def _parse_json(raw: bytes, url: str) -> dict:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise OneCHttpError(f"ответ {url} не JSON: {text[:200]!r}") from e
    if not isinstance(data, (dict, list)):
        raise OneCHttpError(f"ответ {url} не объект и не список: {type(data).__name__}")
    return data if isinstance(data, dict) else {"items": data}


# ───────────────────── нормализация JSON-ответов 1С ─────────────────────
def _rows(payload, keys: tuple[str, ...]) -> list[dict]:
    """Найти плоский список строк в ответе: {"items"|"lines"|"acts"|"data": [...]}."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def smeta_from_json(payload: dict) -> dict:
    """Ответ 1С со строками смет → то же, что даёт CSV-импорт."""
    rows = _rows(payload, ("lines", "items", "rows", "positions", "smeta"))
    if not rows:
        raise OneCHttpError("в ответе 1С нет строк сметы")
    return normalize_smeta_rows([_flat(r) for r in rows], start_row=1)


def acts_from_json(payload: dict) -> dict:
    """Ответ 1С с актами → реестр.

    Поддерживает две формы, которые встречаются в жизни:
      * плоские строки с колонкой акта (`[{act_id, code, qty, ...}]`);
      * вложенная (`[{act_id, period, positions: [...]}]`) — её и отдаёт наш тестовый сервис.
    """
    if isinstance(payload, dict) and isinstance(payload.get("acts"), list) and any(
            isinstance(a, dict) and isinstance(a.get("positions"), list)
            for a in payload["acts"]):
        rows: list[dict] = []
        for act in payload["acts"]:
            for pos in act.get("positions") or []:
                rows.append({"act_id": act.get("act_id") or act.get("number"),
                             "period": act.get("period"),
                             "act_date": act.get("act_date") or act.get("date"),
                             "status": act.get("status"),
                             **_flat(pos)})
        if not rows:
            raise OneCHttpError("акты пришли без позиций")
        return group_acts(rows, start_row=1)

    rows = _rows(payload, ("items", "lines", "rows", "acts", "positions"))
    if not rows:
        raise OneCHttpError("в ответе 1С нет актов")
    return group_acts([_flat(r) for r in rows], start_row=1)


def _flat(row: dict) -> dict:
    """Вложенные словари 1С (`{"Стоимость": {"Сумма": 100}}`) — в плоскую строку."""
    flat: dict = {}
    for key, value in row.items():
        if isinstance(value, dict):
            for k2, v2 in value.items():
                flat.setdefault(f"{key}.{k2}", v2)
                flat.setdefault(k2, v2)
        else:
            flat[key] = value
    return flat


# ───────────────────── выгрузка проверенной КС-2 ─────────────────────────
def ks2_payload(summary: dict, *, quantity: str = "claim", object_name: str | None = None,
                meta: dict | None = None) -> dict:
    """`summary.json` → тело запроса для 1С (те же поля, что в CSV-контракте)."""
    if quantity not in ("claim", "journal", "estimate"):
        raise OneCHttpError(f"неизвестный режим объёма: {quantity}")
    key = {"claim": "qty_claim", "journal": "qty_journal", "estimate": "estimate_qty"}[quantity]
    positions, total, not_estimated = [], 0.0, 0
    for line in summary.get("lines") or []:
        qty = _num(line.get(key))
        price = _num(line.get("price"))
        amount = round(qty * price, 2) if qty is not None and price is not None else None
        if qty is None:
            not_estimated += 1
        total += amount or 0.0
        positions.append({
            "code": line.get("code"),
            "name": line.get("name") or "",
            "unit": line.get("unit") or "",
            "quantity": qty,               # null, если объём неизвестен: «не оценено», а не 0
            "quantity_journal": _num(line.get("qty_journal")),
            "price": price,
            "amount": amount,
            "estimated": qty is not None,
            "restrictions": line.get("restrictions") or [],
        })
    payload = {
        "source": "pto-agent",
        "object": object_name or summary.get("object"),
        "period": summary.get("period"),
        "quantity_mode": quantity,
        "positions": positions,
        "totals": {"amount": round(total, 2), "positions": len(positions),
                   "not_estimated": not_estimated},
        "meta": meta or {},
    }
    payload["idempotency_key"] = idempotency_key(payload)
    return payload


def idempotency_key(payload: dict) -> str:
    """Стабильный ключ повтора: одинаковый результат — один документ в 1С."""
    clone = {k: v for k, v in payload.items() if k != "idempotency_key"}
    canonical = json.dumps(clone, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


# ───────────────────── сценарий «синхронизация» ──────────────────────────
_ENV = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def expand_env(value):
    """`${ONEC_URL}` → значение из окружения. Пароли не живут в файлах конфигурации."""
    if isinstance(value, str):
        m = _ENV.match(value.strip())
        if m:
            return os.environ.get(m.group(1)) or None
        return value
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def load_config(path: str | Path) -> dict:
    """Прочитать конфиг обмена; значения вида `${VAR}` берутся из окружения."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        cfg = yaml.safe_load(raw) or {}
    else:
        cfg = json.loads(raw)
    return expand_env(cfg.get("one_c", cfg))


def sync(client: OneCHttpClient, *, period: str | None = None, act_id: str | None = None,
         summary: dict | None = None, quantity: str = "claim", push: bool = False,
         object_name: str | None = None) -> dict:
    """Забрать акты (и, если дана сводка, отправить проверенную КС-2)."""
    result: dict = {"period": period, "act_id": act_id}
    acts = client.fetch_acts(period=period, act_id=act_id)
    result["acts"] = {"count": acts["imported"]["count"],
                      "amount": round(sum(a["amount_rub"] or 0 for a in
                                          acts["document"]["acts"]), 2),
                      "warnings": acts["imported"]["warnings"]}
    result["acts_document"] = acts["document"]
    if summary is not None and push:
        payload = ks2_payload(summary, quantity=quantity, object_name=object_name)
        response = client.push_validated_ks2(payload)
        result["pushed"] = {"positions": len(payload["positions"]),
                            "amount": payload["totals"]["amount"],
                            "not_estimated": payload["totals"]["not_estimated"],
                            "idempotency_key": payload["idempotency_key"],
                            "response": response}
    result["calls"] = client.calls
    return result


def csv_preview(summary: dict, quantity: str = "claim") -> str:
    """Человеческий вид полезной нагрузки (для лога и отчёта)."""
    payload = ks2_payload(summary, quantity=quantity)
    lines = [f"# КС-2 для 1С · {payload['object']} · {payload['period']} · "
             f"{payload['quantity_mode']}",
             f"# позиций {payload['totals']['positions']}, "
             f"не оценено {payload['totals']['not_estimated']}, "
             f"сумма {_fmt(payload['totals']['amount'])}",
             "Код;Наименование;ЕдИзм;Количество;Цена;Сумма"]
    for p in payload["positions"]:
        lines.append(";".join([p["code"] or "", p["name"], p["unit"],
                               _fmt(p["quantity"], 3), _fmt(p["price"]), _fmt(p["amount"])]))
    return "\n".join(lines) + "\n"
