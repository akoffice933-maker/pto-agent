# -*- coding: utf-8 -*-
"""Отпечаток содержания документа: подпись действительна только на тот текст, что проверили.

Одна функция на проект: реестр ТК сверяет ею содержание карты, АТН — состав акта на
дополнительные работы. Служебные поля (статус, отметка о проверке, ключи вида `_path`)
в отпечаток не входят: они не про содержание документа.
"""
from __future__ import annotations

import hashlib
import json

# производные от содержания поля: вердикты и находки, а не сам документ
SKIP_KEYS = ("confirmed_by", "status", "confirm_problems", "sign_problems", "registry",
             "can_close", "can_transfer", "problems", "checks")


def content_fingerprint(document: dict) -> str:
    """sha256 по содержанию документа без статуса и отметки о проверке (16 hex-знаков)."""
    body = {k: v for k, v in (document or {}).items()
            if not k.startswith("_") and k not in SKIP_KEYS}
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def check_signature(document: dict) -> dict:
    """Действительна ли отметка о проверке: есть ФИО и роль, содержание не менялось."""
    confirmed = document.get("confirmed_by") or {}
    by = str(confirmed.get("by") or "").strip()
    role = str(confirmed.get("role") or "").strip()
    stored = confirmed.get("content_fingerprint")
    current = content_fingerprint(document)
    return {
        "has_signature": bool(by and role),
        "by": by or None,
        "role": role or None,
        "at": confirmed.get("at"),
        "stored_fingerprint": stored,
        "current_fingerprint": current,
        "valid": bool(by and role) and (not stored or stored == current),
        "changed_after_confirm": bool(stored) and stored != current,
    }
