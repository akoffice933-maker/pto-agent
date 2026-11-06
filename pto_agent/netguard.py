# -*- coding: utf-8 -*-
"""Сетевые предохранители: куда агенту можно ходить и что можно прочитать в ответ.

Зачем отдельный модуль
----------------------
Агент ходит в сеть в трёх местах: LLM (`llm.chat`), vision-провайдер
(`cad/vision/providers.py`) и HTTP-сервис 1С (`integrations/one_c_http.py`). Все три адреса
задаются конфигом и **переменными окружения** — то есть подменяются без правки кода. До этого
модуля проверки были несистемными: в `llm.py` отсекались адреса `10.*` (при этом разрешались
`127.0.0.1`, `169.254.169.254` и любой внешний хост), в vision-провайдере логика была
перевёрнута (внутренний контур запрещён, метаданные облака разрешены), а в клиенте 1С не
проверялась даже схема — принимался `file:///etc`, basic-auth уходил по открытому http.

Что проверяется
---------------
1. **Схема.** Только `http`/`https`. `file://`, `ftp://`, `gopher://` и прочее — отказ.
   `urllib` умеет `file://`, и это не гипотетическая дыра, а чтение локального файла
   в ответ на «адрес сервиса».
2. **Адреса, которые нельзя трогать никогда** — link-local и облачные метаданные
   (`169.254.169.254`, `metadata.google.internal`, `100.100.100.200`, `fd00:ec2::254`):
   через них утекают токены инстанса.
3. **Открытый http.** Для внешнего хоста пароль и данные по открытому каналу — отказ;
   для локального/внутреннего контура — предупреждение в журнал, потому что 1С на предприятии
   часто живёт на `http://10.x` и это осознанное решение, а не дефект.
4. **Переадресация.** Редирект на другой хост не выполняется: `urllib` переносит заголовки
   (включая `Authorization`) на новый адрес, то есть редирект уводит токен чужому серверу.
5. **Размер ответа.** Чтение ограничено `max_bytes`: иначе скомпрометированный или ошибочно
   настроенный сервис отдаёт гигабайты и роняет процесс (проверка «на лету», без доверия к
   `Content-Length`).
6. **Текст ошибок.** Наружу (в MCP-ответ, в отчёт) не уходят URL с параметрами, заголовки
   и тело ответа целиком: они остаются в stderr/журнале, клиент получает короткое сообщение.

Чего модуль не делает: не проксирует трафик, не подменяет TLS и не проверяет сертификаты
вручную — используется проверка по умолчанию из `ssl`/`urllib`.
"""
from __future__ import annotations

import ipaddress
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_SCHEMES = {"http", "https"}

# Адреса, которые отдают токены инстанса. Их нельзя запрашивать никогда — независимо от
# того, что написано в конфиге.
METADATA_HOSTS = {
    "169.254.169.254",          # AWS / GCP / OpenStack / Azure (IMDS)
    "169.254.170.2",            # AWS ECS task metadata
    "100.100.100.200",          # Aliyun
    "fd00:ec2::254",            # AWS IPv6 IMDS
    "metadata.google.internal",
    "metadata.goog",
    "metadata",                 # короткое имя внутри облака
}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}

DEFAULT_MAX_BYTES = 10 * 1024 * 1024       # 10 МБ на ответ сервиса
MAX_BYTES_LLM = 4 * 1024 * 1024            # ответ модели больше не бывает


class UnsafeEndpoint(ValueError):
    """Адрес не прошёл проверку. Текст безопасен для показа клиенту."""

    def __init__(self, message: str, *, host: str = "", reason: str = ""):
        super().__init__(message)
        self.host = host
        self.reason = reason


def _host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower().strip("[]")


def _is_private_host(host: str) -> bool:
    """Внутренний контур: частные диапазоны, .local/.internal, короткое имя без точки."""
    if host in LOOPBACK_HOSTS:
        return True
    if host.endswith((".local", ".internal", ".lan", ".corp")):
        return True
    if "." not in host and ":" not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def validate_url(url: str, *, purpose: str = "запрос", allow_insecure_http: bool = False,
                 allow_private: bool = True) -> str:
    """Проверить адрес до обращения к сети. Возвращает нормализованный URL.

    `allow_private=True` — внутренний контур разрешён (это норма для 1С и локальной модели),
    `allow_insecure_http=False` — открытый http допустим только там же, где и приватные адреса.
    """
    raw = str(url or "").strip()
    if not raw:
        raise UnsafeEndpoint(f"{purpose}: пустой адрес", reason="EMPTY_URL")
    parts = urllib.parse.urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    host = _host_of(raw)

    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeEndpoint(
            f"{purpose}: схема «{scheme or 'нет'}» запрещена — разрешены только http и https",
            host=host, reason="BAD_SCHEME")
    if not host:
        raise UnsafeEndpoint(f"{purpose}: в адресе нет хоста", reason="NO_HOST")
    if host in METADATA_HOSTS:
        raise UnsafeEndpoint(
            f"{purpose}: адрес {host} — метаданные облака, такие запросы запрещены",
            host=host, reason="METADATA_ENDPOINT")

    private = _is_private_host(host)
    if private and not allow_private:
        raise UnsafeEndpoint(
            f"{purpose}: внутренний адрес {host} запрещён настройкой (allow_private=False)",
            host=host, reason="PRIVATE_FORBIDDEN")
    if scheme == "http" and not private and not allow_insecure_http:
        # Снаружи — только https: basic-auth и содержимое сметы по открытому каналу не идут.
        raise UnsafeEndpoint(
            f"{purpose}: для внешнего хоста {host} нужен https "
            f"(открытый http допустим только во внутреннем контуре)",
            host=host, reason="INSECURE_HTTP")
    return raw


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Переадресация только в пределах того же хоста — иначе заголовки уходят чужому серверу."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
        new = (urllib.parse.urlsplit(newurl).hostname or "").lower()
        if old != new:
            raise UnsafeEndpoint(
                f"переадресация на другой хост запрещена: {old} → {new}",
                host=new, reason="CROSS_HOST_REDIRECT")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()          # проверка сертификата и имени хоста включена
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                    urllib.request.HTTPHandler(), urllib.request.HTTPDefaultErrorHandler(),
                    _SameHostRedirect(), urllib.request.HTTPErrorProcessor(),
                    urllib.request.HTTPSHandler(context=ctx)):
        opener.add_handler(handler)
    return opener


def open_bounded(req: urllib.request.Request, *, timeout: float, max_bytes: int,
                 purpose: str = "запрос") -> tuple[bytes, int, dict]:
    """Выполнить запрос и прочитать ответ не больше `max_bytes`.

    Возвращает `(тело, статус, заголовки)`. Превышение размера — `UnsafeEndpoint`, а не
    «прочитаем сколько получится»: тело уже отброшено, память не съедена.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes должен быть положительным")
    try:
        resp = _opener().open(req, timeout=timeout)
    except UnsafeEndpoint:
        raise
    except urllib.error.HTTPError:
        raise                                    # вызывающий разбирает код ответа сам
    with resp:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UnsafeEndpoint(
                    f"{purpose}: ответ больше {max_bytes} байт — чтение остановлено",
                    reason="RESPONSE_TOO_LARGE")
            chunks.append(chunk)
        return b"".join(chunks), getattr(resp, "status", 0), dict(resp.headers or {})


_URL_IN_TEXT = re.compile(r"[a-z][a-z0-9+.\-]*://[^\s'\"<>]+", re.IGNORECASE)
_CREDENTIALS = re.compile(r"(://)[^/@\s]+@")


def public_message(exc: BaseException | str, *, limit: int = 300) -> str:
    """Текст ошибки без адресов, учётных данных и служебных деталей — для показа клиенту."""
    text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    text = _CREDENTIALS.sub(r"\1***@", text)
    text = _URL_IN_TEXT.sub("<адрес>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def log_decision(purpose: str, url: str, *, allowed: bool, reason: str = "") -> None:
    """Записать решение в stderr (в контуре это уходит в журнал сервиса, не клиенту)."""
    host = _host_of(url) or "—"
    verdict = "разрешён" if allowed else f"запрещён ({reason})"
    sys.stderr.write(f"[netguard] {purpose}: {host} — {verdict}\n")


# ─────────────────────────── CSV-инъекции ────────────────────────────────
# Выгрузка для 1С/Excel открывается в таблице, а Excel исполняет ячейку, начинающуюся с
# «=», «+», «-», «@», табуляции или возврата каретки. Наименование позиции приходит из
# внешней сметы — то есть это недоверенный текст, который нельзя писать в CSV как есть.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Экранировать значение для CSV так, чтобы Excel не исполнил его как формулу."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def csv_row(values: list) -> list:
    return [csv_safe(v) for v in values]
