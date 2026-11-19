# -*- coding: utf-8 -*-
"""Backlog как граф работ: провал проверки становится узлом, узел — следующим действием.

Зачем граф, а не список
-----------------------
Список «что починить» живёт до первого крупного провала: в нём нет ни причины, ни того,
что от чего зависит, ни доказательства, что починено. Здесь узел — это утверждение,
которое должно стать истинным, с четырьмя обязательными полями:

* **что именно неверно** (наблюдение из проверки, а не «улучшить качество»);
* **чем это доказано** (какая проверка дала провал и с каким текстом);
* **что разблокирует** (зависимости: узлы-предки, без которых этот не закрыть);
* **чем будет закрыто** (какая проверка должна пройти, чтобы узел можно было закрыть).

Из этого получается граф: узлы ссылаются друг на друга через `blocked_by`, а закрытие
разрешено только по прошедшей проверке — не по слову «сделано».

Хранилище — файл в репозитории (по умолчанию `out/backlog/backlog.json`): без БД,
без сети, диффится в git.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

SCHEMA = "pto-agent/backlog@1"
OPEN, CLOSED, BLOCKED, DROPPED = "open", "closed", "blocked", "dropped"
MAX_NEW_PER_CYCLE = 5      # самоторможение: за один круг не больше пяти новых узлов


def fingerprint(code: str, why: str) -> str:
    """Устойчивый отпечаток провала: цифры вырезаны, иначе «5 строк» и «6 строк» — разные узлы."""
    text = re.sub(r"\d+([.,]\d+)?", "#", str(why).lower())
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(f"{code}|{text}".encode("utf-8")).hexdigest()[:12]


class Backlog:
    def __init__(self, path: str | Path = "out/backlog/backlog.json"):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"schema": SCHEMA, "nodes": [], "edges": [], "cycles": []}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    # ── узлы ──
    def node(self, node_id: str) -> dict | None:
        return next((n for n in self.data["nodes"] if n["id"] == node_id), None)

    def open_nodes(self) -> list[dict]:
        return [n for n in self.data["nodes"] if n["status"] in (OPEN, BLOCKED)]

    def add(self, *, code: str, title: str, observed: str, evidence: str,
            closed_by: str, priority: str = "important",
            blocked_by: list[str] | None = None, cycle: str = "") -> dict | None:
        """Добавить узел. Возвращает None, если такой провал уже есть — дубли не плодим."""
        fp = fingerprint(code, observed)
        # Один провал проверки — один узел. Дедуплицируем по коду проверки, а не по тексту:
        # «5 строк без цены» и «6 строк без цены» — это один и тот же незакрытый вопрос,
        # и он должен накапливать `seen`, а не порождать второй узел рядом с первым.
        for n in self.data["nodes"]:
            if n["code"] == code and n["status"] in (OPEN, BLOCKED):
                n["seen"] = n.get("seen", 1) + 1
                n["last_seen_cycle"] = cycle
                n["last_observed"] = observed
                return None
        node = {
            "id": f"N{len(self.data['nodes']) + 1:03d}",
            "fingerprint": fp, "code": code, "title": title,
            "observed": observed, "evidence": evidence,
            "will_close_when": closed_by, "priority": priority,
            "status": OPEN, "seen": 1, "attempts": 0,
            "opened_at": datetime.now().isoformat(timespec="seconds"),
            "opened_cycle": cycle, "last_seen_cycle": cycle,
            "blocked_by": blocked_by or [], "closed_at": None, "closed_note": "",
        }
        self.data["nodes"].append(node)
        for parent in node["blocked_by"]:
            self.data["edges"].append({"from": parent, "to": node["id"], "kind": "blocks"})
        return node

    def close(self, node_id: str, *, note: str, cycle: str = "") -> dict | None:
        """Закрыть узел можно только с доказательством — текстом прошедшей проверки."""
        node = self.node(node_id)
        if not node:
            return None
        for parent_id in node.get("blocked_by") or []:
            parent = self.node(parent_id)
            if parent and parent["status"] != CLOSED:
                node["status"] = BLOCKED
                node["closed_note"] = f"не закрыт: ждёт {parent_id} ({parent['title']})"
                return node
        node["status"] = CLOSED
        node["closed_at"] = datetime.now().isoformat(timespec="seconds")
        node["closed_note"] = note
        node["closed_cycle"] = cycle
        return node

    def bump(self, node_id: str) -> None:
        node = self.node(node_id)
        if node:
            node["attempts"] = node.get("attempts", 0) + 1

    # ── граф ──
    def graph(self) -> dict:
        """Что от чего зависит: только по открытым узлам, чтобы граф был читаемым."""
        nodes = self.open_nodes()
        ids = {n["id"] for n in nodes}
        edges = [e for e in self.data["edges"] if e["from"] in ids and e["to"] in ids]
        ready = [n["id"] for n in nodes
                 if all((self.node(p) or {}).get("status") == CLOSED
                        for p in n.get("blocked_by") or [])
                 and n["status"] == OPEN]
        return {"nodes": nodes, "edges": edges, "ready": sorted(ready),
                "blocked": sorted(ids - set(ready))}

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for n in self.data["nodes"]:
            counts[n["status"]] = counts.get(n["status"], 0) + 1
        return {"total": len(self.data["nodes"]), "by_status": counts,
                "cycles": len(self.data["cycles"]),
                "open": len(self.open_nodes())}


def to_markdown(backlog: Backlog) -> str:
    g, st = backlog.graph(), backlog.stats()
    lines = ["# Backlog: граф работ, выросший из провалов проверок", "",
             f"Всего узлов {st['total']}: "
             + ", ".join(f"{k} — {v}" for k, v in sorted(st["by_status"].items()))
             + f"; циклов прогона: {st['cycles']}", ""]
    ready = [n for n in g["nodes"] if n["id"] in g["ready"]]
    blocked = [n for n in g["nodes"] if n["id"] not in g["ready"]]
    lines += ["## Можно брать сейчас", ""]
    if not ready:
        lines.append("— открытых узлов, готовых к работе, нет")
    for n in ready:
        lines.append(f"- **{n['id']}** ({n['priority']}) {n['title']} — {n['observed']}\n"
                     f"  *доказательство:* {n['evidence']}; *закроется, когда:* "
                     f"{n['will_close_when']}; видели раз: {n['seen']}")
    lines += ["", "## Ждут зависимостей", ""]
    if not blocked:
        lines.append("— нет")
    for n in blocked:
        waits = ", ".join(n.get("blocked_by") or []) or "—"
        lines.append(f"- **{n['id']}** {n['title']} — ждёт {waits}")
    closed = [n for n in backlog.data["nodes"] if n["status"] == CLOSED]
    if closed:
        lines += ["", "## Закрыто (с доказательством)", ""]
        for n in closed[-10:]:
            lines.append(f"- ~~{n['id']} {n['title']}~~ — {n['closed_note']}")
    return "\n".join(lines) + "\n"


# ─────────────────── связь eval → backlog (осаждение провалов) ───────────────────
# Как провал превращается в вопрос для следующего круга, а не в строчку в логе:
#   проверка            → что наблюдается       → чем закрывается
RULES = {
    "ks2_arithmetic": ("Сумма акта не сходится с пересчётом по строкам",
                       "повторный прогон eval: ks2_arithmetic = pass"),
    "ks2_sources": ("В акте есть строки без цены или без признака происхождения",
                    "eval: ks2_sources = pass на том же наборе"),
    "honesty": ("Пропущенная проверка показана как успех",
                "eval: honesty = pass (пропуски помечены skipped, деньги не показаны)"),
    "prices": ("В базе цен есть версия без источника, даты или digest",
               "eval: prices = pass"),
    "norms": ("Нормативный ответ может выйти без цитаты",
              "eval: norms = pass"),
    "refusals": ("Агент пропускает опасный адрес",
                 "eval: refusals = pass по всем адресам"),
    "signature": ("Подпись возможна без человека или без роли",
                  "eval: signature = pass"),
    "harness": ("Манифест границ расходится с кодом",
                "eval: harness = pass (digest пересчитан после правки)"),
    "smeta": ("Смета не сходится сама с собой",
              "eval: smeta = pass"),
    "index": ("Проверка по индексу не сообщает свои ограничения",
              "eval: index = pass"),
}


def ingest(self, report: dict, *, cycle: str = "", limit: int = MAX_NEW_PER_CYCLE) -> int:
    """Осадить провалы eval в узлы графа. Возвращает число новых узлов.

    Два ограничения, чтобы граф не расползся:
      * один и тот же провал (с точностью до чисел) не создаёт второй узел — растёт
        счётчик `seen`, и это сигнал, что узел не решается, а повторяется;
      * за круг добавляется не больше `limit` узлов: агент не переписывает весь бэклог
        после каждого прогона.
    """
    added = 0
    for res in report.get("results") or []:
        if res.get("status") != "fail":
            continue
        code = res.get("code") or "?"
        title, closes = RULES.get(code, (f"Провал проверки {code}", f"eval: {code} = pass"))
        if added >= limit:
            break
        node = self.add(code=code, title=title, observed=res.get("why", ""),
                        evidence=f"eval:{code} — {res.get('why', '')}",
                        closed_by=closes, cycle=cycle)
        if node is not None:
            added += 1
    open_by_code = {n["code"]: n for n in self.open_nodes()}
    for res in report.get("results") or []:
        if res.get("status") == "pass" and res.get("code") in open_by_code:
            self.close(open_by_code[res["code"]]["id"], note=f"проверка прошла: {res['why']}",
                       cycle=cycle)
    self.data.setdefault("cycles", []).append({
        "cycle": cycle or datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "score": report.get("score"), "counts": report.get("counts"),
        "added": added, "at": datetime.now().isoformat(timespec="seconds"),
    })
    return added


Backlog.ingest = ingest
