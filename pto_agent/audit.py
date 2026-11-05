# -*- coding: utf-8 -*-
"""Журнал аудита: JSONL с хэш-цепочкой (append-only, проверяемый).

Две формы записи:

* `append` — факт действия («агент сформировал КС-2», «человек утвердил акт»);
* `append_change` — изменение сущности по образцу `django-simple-history`: снимок «до»/«после»,
  дельта только по изменившимся полям, автор, причина и ссылка на документ-основание.

Смысл второй формы: через полгода на вопрос «почему в акте эта цена» нужен ответ «кто, когда,
на основании чего», а не «в журнале была строка». Поэтому причина (`reason`) обязательна —
без неё изменение не пишется молча, а получает пометку в `payload.reason_missing`.

Хэш-цепочка та же: каждая запись содержит хэш предыдущей, правка любого прошлого поля ломает
проверку `verify()`.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def diff(before: dict, after: dict) -> dict:
    """Дельта: только изменившиеся поля, со значениями до и после.

    Вложенные словари сравниваются по верхнему уровню: для акта/цены/КП этого достаточно,
    а полный рекурсивный diff даёт нечитаемые простыни в журнале.
    """
    delta: dict = {}
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key), after.get(key)
        if was != now:
            delta[key] = {"from": was, "to": now}
    return delta


def summarize_changes(records: list[dict]) -> list[dict]:
    """Короткая лента изменений для отчёта: что, кем, когда, почему."""
    out = []
    for rec in records:
        payload = rec.get("payload") or {}
        out.append({
            "ts": rec.get("ts"),
            "hash": str(rec.get("hash", ""))[:12],
            "actor": rec.get("actor"),
            "action": rec.get("action"),
            "entity": rec.get("entity"),
            "fields": payload.get("changed_fields") or [],
            "reason": payload.get("reason") or "",
            "ref": payload.get("ref") or "",
            "reason_missing": payload.get("reason_missing", False),
        })
    return out


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

    def append_change(self, actor: str, actor_type: str, action: str, entity: str,
                      before: dict | None, after: dict | None, *,
                      reason: str = "", ref: str = "",
                      payload: dict | None = None) -> dict:
        """Записать изменение сущности: снимок до/после, дельта, причина, основание."""
        before = before or {}
        after = after or {}
        delta = diff(before, after)
        record_payload = {
            "before": before,
            "after": after,
            "delta": delta,
            "changed_fields": sorted(delta),
            "reason": reason,
            "reason_missing": not reason,
            "ref": ref,
            **(payload or {}),
        }
        return self.append(actor, actor_type, action, entity, record_payload)

    def history(self, entity: str | None = None, *, limit: int | None = None,
                action: str | None = None) -> list[dict]:
        """Записи журнала (при необходимости — только по сущности/действию), новые в конце."""
        records: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if entity and rec.get("entity") != entity:
                continue
            if action and rec.get("action") != action:
                continue
            records.append(rec)
        if limit:
            records = records[-limit:]
        return records

    def last_change(self, entity: str) -> dict | None:
        changes = self.history(entity, action="entity.changed")
        return changes[-1] if changes else None

    def find(self, digest: str) -> dict | None:
        """Найти запись по началу хэша — так удобно ссылаться на запись в переписке."""
        for rec in self.history():
            if str(rec.get("hash", "")).startswith(digest):
                return rec
        return None

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
