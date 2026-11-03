# -*- coding: utf-8 -*-
"""Журнал аудита: JSONL с хэш-цепочкой (append-only, проверяемый)."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def last_hash(self) -> str:
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            return GENESIS
        return json.loads(lines[-1])["hash"]

    def append(self, actor: str, actor_type: str, action: str, entity: str,
               payload: dict | None = None) -> dict:
        prev = self.last_hash()
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": actor,
            "actor_type": actor_type,
            "action": action,
            "entity": entity,
            "payload": payload or {},
            "prev_hash": prev,
        }
        raw = json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")
        rec["hash"] = hashlib.sha256(prev.encode() + raw).hexdigest()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def verify(self) -> tuple[bool, int, str]:
        prev = GENESIS
        count = 0
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            rec = json.loads(line)
            claimed = rec.pop("hash")
            if rec["prev_hash"] != prev:
                return False, count, f"строка {i}: разрыв цепочки prev_hash"
            raw = json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")
            calc = hashlib.sha256(prev.encode() + raw).hexdigest()
            if calc != claimed:
                return False, count, f"строка {i}: хэш не совпадает (запись изменена)"
            prev, count = claimed, count + 1
        return True, count, "цепочка целостна"
