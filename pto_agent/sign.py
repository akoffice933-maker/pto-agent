# -*- coding: utf-8 -*-
"""Подпись документов: провайдеры и запрет на подделку.

Главное правило модуля: **подписи нет, пока её нет**. Если КриптоПро или другой провайдер
не подключён, модуль не рисует «подписано», а отказывает с кодом `SIGNING_UNAVAILABLE`
и пишет причину. Файл `.sig` появляется только из настоящего криптопровайдера.

Что подключено сейчас:

* `pycades` (CAdES поверх КриптоПро CSP, BSD-2) — если установлен; на Linux метки времени
  не поддерживаются, на это есть отдельная оговорка;
* `stub` — «провайдера нет»: нужен, чтобы в тестах и демо честно отказывать, а не молчать.

Что не сделано: подключение сертификата и подпись через браузер (УКЭП в вебе — этап 7 в части
клиента), проверка цепочки сертификатов до корневого, метки времени, работа с ЕСИА.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

STATUS_SIGNED = "подписано"
STATUS_UNAVAILABLE = "подпись недоступна"
STATUS_FAILED = "ошибка подписи"
SIGNATURE_SUFFIX = ".sig"


class SigningError(RuntimeError):
    """Подпись не поставлена. Всегда с кодом и причиной."""

    def __init__(self, code: str, why: str):
        super().__init__(why)
        self.code = code
        self.why = why


class SignatureProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        ...

    def sign(self, data: bytes, *, pin: str | None = None) -> bytes:
        ...


class StubProvider:
    """Провайдера нет. Отказывает, но не мешает считать отпечаток документа."""

    name = "stub"

    def available(self) -> tuple[bool, str]:
        return False, ("криптопровайдер не подключён: подпись не ставится. "
                       "Нужен КриптоПро CSP и сертификат (УКЭП) — это не делается «пока»")

    def sign(self, data: bytes, *, pin: str | None = None) -> bytes:  # noqa: ARG002
        raise SigningError("SIGNING_UNAVAILABLE", self.available()[1])


class PycadesProvider:
    """CAdES-BES через `pycades` (КриптоПро). Импорт мягкий: без CSP модуль не падает."""

    name = "pycades"

    def __init__(self, *, thumbprint: str | None = None, detached: bool = True):
        self.thumbprint = thumbprint or os.environ.get("PTO_CERT_THUMBPRINT") or ""
        self.detached = detached

    def _module(self):
        try:
            import pycades  # noqa: PLC0415 — импорт внутри: без CSP пакета может не быть
        except Exception as exc:  # noqa: BLE001
            raise SigningError("SIGNING_UNAVAILABLE",
                               f"pycades не установлен: {type(exc).__name__}: {exc}. "
                               f"Ставится из исходников под КриптоПро CSP") from exc
        return pycades

    def available(self) -> tuple[bool, str]:
        try:
            pycades = self._module()
        except SigningError as exc:
            return False, exc.why
        try:
            store = pycades.CStore()
            store.Open(pycades.CAPICOM_CURRENT_USER_STORE, "My",
                       pycades.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED)
            count = store.Certificates.Count
        except Exception as exc:  # noqa: BLE001
            return False, f"CSP не отвечает: {type(exc).__name__}: {exc}"
        if not count:
            return False, "в хранилище «Мой» нет сертификатов: подписывать нечем"
        return True, f"сертификатов в хранилище: {count}"

    def sign(self, data: bytes, *, pin: str | None = None) -> bytes:
        pycades = self._module()
        if not self.thumbprint:
            raise SigningError("NO_CERTIFICATE",
                               "не указан отпечаток сертификата (--thumbprint или "
                               "PTO_CERT_THUMBPRINT): подписывать «первым попавшимся» нельзя")
        store = pycades.CStore()
        store.Open(pycades.CAPICOM_CURRENT_USER_STORE, "My",
                   pycades.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED)
        certificates = pycades.CCertificates()
        certificates.Find(pycades.CCAPICOM_STORE_INFO_TYPE_ANY_KEYS, self.thumbprint)
        if not certificates.Count:
            raise SigningError("NO_CERTIFICATE", f"сертификат {self.thumbprint} не найден")
        signer = pycades.CPSigner()
        signer.Certificate = certificates.Item(1)
        if pin:
            signer.KeyPin = pin
        signed = pycades.CPSignedData()
        signed.Content = _b64(data) if not self.detached else _b64(data)
        signed.SignCades(signer, pycades.CADES_TYPE_BES, self.detached)
        return signed.Signature.encode("utf-8")


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def get_provider(name: str | None = None) -> SignatureProvider:
    name = (name or os.environ.get("PTO_SIGN_PROVIDER") or "pycades").lower()
    if name in ("stub", "none"):
        return StubProvider()
    if name == "pycades":
        return PycadesProvider()
    raise SigningError("UNKNOWN_PROVIDER", f"провайдер {name!r} не поддержан: есть pycades, stub")


def fingerprint(data: bytes) -> dict:
    """Отпечаток документа. Это **не** подпись: он не подтверждает автора, только содержимое."""
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "kind": "отпечаток, а не подпись (ЭП не ставилась)"}


def sign_file(path: str | Path, *, provider: SignatureProvider | None = None,
              out: str | Path | None = None, pin: str | None = None) -> dict:
    """Подписать файл. Без провайдера — отказ с кодом, а не «подписано»."""
    path = Path(path)
    if not path.exists():
        raise SigningError("NO_FILE", f"файла {path} нет: подписывать нечего")
    data = path.read_bytes()
    provider = provider or get_provider()
    result = {"file": str(path), "provider": provider.name, "status": "",
              "fingerprint": fingerprint(data), "signature": None, "why": ""}
    ok, why = provider.available()
    if not ok:
        result.update({"status": STATUS_UNAVAILABLE, "why": why})
        return result
    try:
        signature = provider.sign(data, pin=pin)
    except SigningError as exc:
        result.update({"status": STATUS_FAILED, "why": f"{exc.code}: {exc.why}"})
        return result
    except Exception as exc:  # noqa: BLE001 — CSP отвечает чем угодно
        result.update({"status": STATUS_FAILED, "why": f"{type(exc).__name__}: {exc}"})
        return result
    target = Path(out) if out else path.with_suffix(path.suffix + SIGNATURE_SUFFIX)
    target.write_bytes(signature)
    result.update({"status": STATUS_SIGNED, "signature": str(target),
                   "signature_size": len(signature)})
    return result


def verify_file(path: str | Path, signature: str | Path | None = None, *,
                provider: SignatureProvider | None = None) -> dict:
    """Проверить подпись. Без провайдера честно говорит, что проверить нечем."""
    path = Path(path)
    signature_path = Path(signature) if signature else path.with_suffix(
        path.suffix + SIGNATURE_SUFFIX)
    if not signature_path.exists():
        return {"ok": False, "code": "NO_SIGNATURE", "why": f"файла подписи {signature_path} нет"}
    provider = provider or get_provider()
    ok, why = provider.available()
    if not ok:
        return {"ok": False, "code": "PROVIDER_UNAVAILABLE", "why": why,
                "note": "подпись есть, но проверить её нечем: нужен тот же криптопровайдер"}
    try:
        import pycades  # noqa: PLC0415
        data = path.read_bytes()
        signed = pycades.CPSignedData()
        signed.VerifyCades(signature_path.read_text(encoding="utf-8"),
                           pycades.CADES_TYPE_BES, True)
        content_ok = signed.Content == _b64(data)
        return {"ok": bool(content_ok), "code": "OK" if content_ok else "CONTENT_MISMATCH",
                "why": "" if content_ok else "подпись верна, но файл изменён после подписания"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "VERIFY_FAILED", "why": f"{type(exc).__name__}: {exc}"}


def toolchain() -> dict:
    """Что есть в системе для подписи — чтобы отчёт не врал «подпись подключена»."""
    cades = shutil.which("cades") or shutil.which("cryptcp")
    version = ""
    if cades:
        try:
            version = subprocess.run([cades, "-help"], capture_output=True, text=True,
                                     timeout=10).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            version = "не отвечает"
    try:
        import pycades  # noqa: F401,PLC0415
        has_pycades = True
    except Exception:  # noqa: BLE001
        has_pycades = False
    provider = get_provider()
    ok, why = provider.available()
    return {"cades_cli": cades or "", "cades_version": version, "pycades": has_pycades,
            "provider": provider.name, "available": ok, "why": why,
            "note": "На Linux pycades работает без меток времени: для полноценного CAdES-T "
                    "нужен Windows или отдельный сервис подписи"}


def scenario_report(result: dict) -> str:
    """Короткая строка для отчёта: подписано или нет, и почему."""
    if result.get("status") == STATUS_SIGNED:
        return f"подписано: {result['file']} → {result['signature']} ({result['provider']})"
    return json.dumps({"file": result.get("file"), "status": result.get("status"),
                       "why": result.get("why")}, ensure_ascii=False)
