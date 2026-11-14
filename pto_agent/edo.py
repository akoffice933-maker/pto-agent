# -*- coding: utf-8 -*-
"""ЭДО: отправка документов в Диадок (Контур) и честный офлайн-режим.

Правила, из которых собран модуль:

1. **Дважды отправить документ нельзя.** У каждой отправки есть ключ идемпотентности
   (отпечаток файлов + ящик + номер документа). Повтор берётся из журнала отправок,
   а не отправляется заново: дубль в ЭДО — это два комплекта у контрагента.
2. **Тренировка не отправляет.** `dry_run=True` пишет письмо в `outbox` со статусом
   «не отправлено» и не делает сетевых вызовов. Так демо и тесты не ходят в чужой контур.
3. **Ошибку 5xx повторяем ограниченно, 4xx — нет.** 401 значит «токен умер», а не
   «сеть моргнула»: повторять бессмысленно и вредно.
4. **Статус документа известен только из ответа.** «Отправлено» появляется лишь после
   реального ответа Диадока с идентификатором сообщения.

Что не сделано: подключение реального ящика (нужны логин, пароль и сертификат), приём
входящих, титулы покупателя, УКЭП под переданный файл внутри пакета (см. `sign.py`).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

try:  # httpx нужен только для реальных вызовов и для MockTransport в тестах
    import httpx
except Exception:  # noqa: BLE001 — без httpx модуль жив, но сетевой режим отключён
    httpx = None  # type: ignore[assignment]

SCHEMA = "pto-agent/edo@1"
DEFAULT_BASE = "https://diadoc-api.kontur.ru"
STATUS_DRAFT = "черновик"
STATUS_SENT = "отправлено"
STATUS_QUEUED = "не отправлено (тренировка)"
STATUS_FAILED = "ошибка отправки"

RETRY_STATUSES = {500, 502, 503, 504}
FATAL_STATUSES = {400, 401, 403, 404, 409, 422}


class EdoError(RuntimeError):
    def __init__(self, code: str, why: str, *, status: int = 0, body: str = ""):
        super().__init__(why)
        self.code = code
        self.why = why
        self.status = status
        self.body = body


def new_state(*, box: str = "") -> dict:
    return {"schema": SCHEMA, "box": box, "sends": [], "inbox": [],
            "note": "Журнал отправок: ключ идемпотентности, статус и ответ сервера. "
                    "Повторная отправка того же документа не создаёт второго сообщения."}


def load_state(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return new_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} не {SCHEMA!r}")
    return payload


def save_state(state: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def idempotency_key(*, box: str, documents: list[Path], number: str) -> str:
    """Ключ: ящик + номер + содержимое файлов. Изменение файла — уже другая отправка."""
    digest = hashlib.sha256()
    digest.update(str(box).encode("utf-8"))
    digest.update(b"|")
    digest.update(str(number).encode("utf-8"))
    for path in sorted(Path(item) for item in documents):
        digest.update(b"|")
        digest.update(path.name.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.exists()
                      else b"missing")
    return digest.hexdigest()


class DiadocClient:
    """Минимальный клиент Диадока: только то, что нужно, и с проверкой ответов."""

    def __init__(self, *, base_url: str = DEFAULT_BASE, login: str = "", password: str = "",
                 token: str = "", transport: Any = None, timeout: float = 30.0,
                 retries: int = 2, sleep: float = 0.0):
        self.base_url = base_url.rstrip("/")
        self.login = login
        self.password = password
        self.token = token
        self.transport = transport
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.calls: list[dict] = []

    # ── низкий уровень ───────────────────────────────────────────
    def _client(self):
        if httpx is None:
            raise EdoError("NO_HTTPX", "httpx не установлен: сетевой режим недоступен")
        return httpx.Client(base_url=self.base_url, timeout=self.timeout,
                            transport=self.transport)

    def _request(self, method: str, url: str, **kwargs) -> dict:
        headers = {"Accept": "application/json", **(kwargs.pop("headers", None) or {})}
        if self.token:
            headers["Authorization"] = f"DiadocAuth ddauth_api_client_id={self.login}, " \
                                       f"ddauth_token={self.token}"
        attempt = 0
        while True:
            attempt += 1
            with self._client() as client:
                try:
                    response = client.request(method, url, headers=headers, **kwargs)
                except Exception as exc:  # noqa: BLE001 — сеть отвечает чем угодно
                    self.calls.append({"method": method, "url": url, "error": str(exc)})
                    if attempt > self.retries:
                        raise EdoError("NETWORK", f"{type(exc).__name__}: {exc}") from exc
                    time.sleep(self.sleep)
                    continue
            self.calls.append({"method": method, "url": url, "status": response.status_code})
            if response.status_code in RETRY_STATUSES and attempt <= self.retries:
                time.sleep(self.sleep)
                continue
            if response.status_code in FATAL_STATUSES:
                raise EdoError("HTTP_" + str(response.status_code),
                               f"Диадок отказал ({response.status_code}): {_short(response.text)}",
                               status=response.status_code, body=response.text)
            if response.status_code >= 400:
                raise EdoError("HTTP_" + str(response.status_code),
                               f"неожидаемый код {response.status_code}: {_short(response.text)}",
                               status=response.status_code, body=response.text)
            try:
                return response.json()
            except Exception:  # noqa: BLE001
                return {"raw": response.text}

    # ── авторизация ──────────────────────────────────────────────
    def authenticate(self) -> str:
        if not self.login or not self.password:
            raise EdoError("NO_CREDENTIALS",
                           "нужны логин и пароль ящика (или готовый токен): без них "
                           "Диадок не авторизует")
        payload = self._request("POST", "/V3/Authenticate",
                                params={"token": self.login},
                                content=self.password.encode("utf-8"),
                                headers={"Content-Type": "text/plain"})
        token = str((payload or {}).get("token") or "")
        if not token:
            raise EdoError("NO_TOKEN", "в ответе нет токена — авторизация не удалась")
        self.token = token
        return token

    # ── отправка ─────────────────────────────────────────────────
    def post_message(self, *, from_box: str, to_box: str, number: str,
                     documents: list[Path], comment: str = "",
                     document_type: str = "Договор") -> dict:
        files = [Path(item) for item in documents]
        missing = [str(item) for item in files if not item.exists()]
        if missing:
            raise EdoError("NO_FILE", f"нет файлов: {', '.join(missing)}")
        if not to_box:
            raise EdoError("NO_RECIPIENT", "не указан ящик получателя")
        body = {
            "FromBoxId": from_box,
            "ToBoxId": to_box,
            "Message": {
                "FromBoxId": from_box,
                "ToBoxId": to_box,
                "Subject": f"{document_type} {number}",
                "Comment": comment,
                "DocumentAttachments": [{
                    "SignedContent": {
                        "Content": _content(files[0]) if len(files) == 1 else None,
                        "Name": files[0].name,
                        "Signature": None,
                    },
                    "Type": document_type,
                    "Comment": comment,
                    "NeedRecipientSignature": True,
                }],
            },
        }
        if len(files) > 1:
            body["Message"]["DocumentAttachments"] = [
                {"SignedContent": {"Content": _content(item), "Name": item.name,
                                   "Signature": None}, "Type": document_type}
                for item in files]
        payload = self._request("POST", "/V3/PostMessage", params={"boxId": from_box},
                                json=body)
        message_id = _dig(payload, "MessageId") or _dig(payload, "messageId")
        if not message_id:
            raise EdoError("NO_MESSAGE_ID", f"Диадок не вернул идентификатор сообщения: "
                                           f"{_short(payload)}")
        return {"message_id": message_id, "to_box": to_box, "from_box": from_box,
                "documents": [item.name for item in files], "response": payload}

    def get_message(self, *, box: str, message_id: str) -> dict:
        return self._request("GET", "/V3/GetMessage",
                             params={"boxId": box, "messageId": message_id})


def _dig(payload: Any, key: str):
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _dig(value, key)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _dig(item, key)
            if found:
                return found
    return None


def _content(path: Path) -> str:
    import base64
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _short(payload: Any, limit: int = 300) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def send(state: dict, *, box: str, to_box: str, number: str, documents: list[Path],
         client: DiadocClient | None = None, dry_run: bool = True,
         document_type: str = "Договор", comment: str = "") -> dict:
    """Отправить комплект. Тренировка пишет в журнал и не звонит в сеть."""
    files = [Path(item) for item in documents]
    key = idempotency_key(box=box, documents=files, number=number)
    previous = next((row for row in state.get("sends") or [] if row.get("key") == key), None)
    if previous and previous.get("status") in (STATUS_SENT, STATUS_QUEUED):
        return {"status": previous["status"], "duplicate": True, "key": key,
                "message_id": previous.get("message_id"),
                "why": "этот комплект уже отправлялся: повтор не создаёт второго сообщения"}
    record = {"key": key, "box": box, "to_box": to_box, "number": number,
              "documents": [{"name": item.name, "size": item.stat().st_size if item.exists()
                             else 0} for item in files],
              "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "dry_run": bool(dry_run),
              "document_type": document_type, "comment": comment}
    if dry_run:
        record.update({"status": STATUS_QUEUED, "message_id": None,
                       "why": "тренировка: сетевых вызовов не было, документ не отправлен"})
        state.setdefault("sends", []).append(record)
        return {"status": STATUS_QUEUED, "duplicate": False, "key": key, "record": record,
                "why": record["why"]}
    client = client or DiadocClient(login=box)
    try:
        if not client.token:
            client.authenticate()
        outcome = client.post_message(from_box=box, to_box=to_box, number=number,
                                      documents=files, comment=comment,
                                      document_type=document_type)
    except EdoError as exc:
        record.update({"status": STATUS_FAILED, "message_id": None,
                       "why": f"{exc.code}: {exc.why}"})
        state.setdefault("sends", []).append(record)
        return {"status": STATUS_FAILED, "duplicate": False, "key": key, "record": record,
                "why": record["why"]}
    record.update({"status": STATUS_SENT, "message_id": outcome["message_id"],
                   "why": "Диадок вернул идентификатор сообщения"})
    state.setdefault("sends", []).append(record)
    return {"status": STATUS_SENT, "duplicate": False, "key": key, "record": record,
            "message_id": outcome["message_id"]}


def outbox_markdown(state: dict) -> str:
    lines = ["# Журнал отправок в ЭДО", ""]
    if not state.get("sends"):
        lines.append("Отправок не было.")
        return "\n".join(lines) + "\n"
    lines.append("| когда | номер | кому | документы | статус | сообщение | причина |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in state["sends"]:
        documents = ", ".join(item["name"] for item in row.get("documents") or [])
        lines.append(f"| {row.get('created')} | {row.get('number')} | {row.get('to_box')} | "
                     f"{documents} | {row.get('status')} | {row.get('message_id') or '—'} | "
                     f"{row.get('why') or ''} |")
    return "\n".join(lines) + "\n"
