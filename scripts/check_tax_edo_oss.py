#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Реестр опенсорса и источников под незакрытые задачи этапов 6–7: проверка списка.

Список — `data/reference/tax_edo_oss.json`, документ — `docs/51_…`, отчёт — в нём же.

Зачем скрипт, а не таблица в документе:

  * таблица в документе устаревает молча: звёзды, лицензия и версия формата меняются,
    а текст остаётся. Здесь структура и лицензионная политика проверяются на каждом прогоне;
  * главный риск заимствования — утащить GPL/AGPL или код без лицензии в MIT-поставку.
    Нарушение = exit code 2, как в `check_platform_oss.py`;
  * каждая незакрытая задача обязана иметь хотя бы одну запись: «нет проекта» — это тоже
    вывод, но он должен быть записан, а не забыт.

Использование:
    python3 scripts/check_tax_edo_oss.py            # структура + политика лицензий
    python3 scripts/check_tax_edo_oss.py --list     # что мы берём в работу
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "data" / "reference" / "tax_edo_oss.json"

# политика та же, что в реестре платформы: код копируем только из permissive
COPY_LICENSES = {"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "ISC", "CC0-1.0"}
SOURCE_KINDS = {"github", "gitlab", "official", "pypi", "own"}
PRIORITIES = {"высокий", "средний", "низкий"}
INTEGRATIONS = {"reference-only", "pattern-and-code", "schema-reference", "library",
                "dependency", "external-mcp-server", "data-source"}
REQUIRED = ("repo", "url", "license", "gap", "priority", "role", "we_take",
            "we_do_not_take", "integration", "copy_allowed")

# «нет лицензии» — это состояние, а не имя лицензии: писать явно
NO_LICENSE = "нет лицензии"


def load(path: Path = LIST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_structure(doc: dict) -> list[str]:
    problems: list[str] = []
    entries = doc.get("entries") or []
    gaps = doc.get("gaps") or {}
    if not entries:
        return ["список проектов пуст"]
    for i, item in enumerate(entries):
        name = item.get("repo") or f"#{i}"
        for field in REQUIRED:
            if field not in item or item[field] in (None, "", []):
                problems.append(f"{name}: нет поля {field}")
        if item.get("gap") not in gaps:
            problems.append(f"{name}: неизвестная задача {item.get('gap')!r}")
        if item.get("source_kind") not in SOURCE_KINDS:
            problems.append(f"{name}: неизвестный источник {item.get('source_kind')!r}")
        if item.get("priority") not in PRIORITIES:
            problems.append(f"{name}: неизвестный приоритет {item.get('priority')!r}")
        if item.get("integration") not in INTEGRATIONS:
            problems.append(f"{name}: неизвестная интеграция {item.get('integration')!r}")
        if item.get("source_kind") == "github" and \
                not str(item.get("url", "")).startswith("https://github.com/"):
            problems.append(f"{name}: источник github, а url не на GitHub")
        if not isinstance(item.get("copy_allowed"), bool):
            problems.append(f"{name}: copy_allowed должен быть true/false, а не "
                            f"{item.get('copy_allowed')!r}")
        if not (item.get("we_take") and item.get("we_do_not_take")):
            problems.append(f"{name}: нужно и «что берём», и «что не берём»")
    for gap, title in gaps.items():
        if not any(item.get("gap") == gap for item in entries):
            problems.append(f"задача «{gap}» ({title}) без единого проекта: даже «нечего "
                            f"взять» должно быть записано")
    return problems


def check_license_policy(doc: dict) -> list[str]:
    """Копировать код можно только из permissive-лицензий. Нарушение — exit 2."""
    problems: list[str] = []
    for item in doc.get("entries") or []:
        name = item.get("repo")
        license_name = str(item.get("license") or "")
        allowed = item.get("copy_allowed")
        if allowed and license_name not in COPY_LICENSES:
            problems.append(f"{name}: copy_allowed=true при лицензии «{license_name}» — "
                            f"копировать код нельзя (идеи, схема, внешний сервис)")
        if license_name in {NO_LICENSE, ""} and allowed:
            problems.append(f"{name}: без лицензии копировать нельзя")
        if license_name in {"GPL-2.0", "GPL-3.0", "AGPL-3.0"} and \
                item.get("integration") in {"library", "dependency"}:
            problems.append(f"{name}: {license_name} нельзя тянуть как "
                            f"{item.get('integration')} в MIT-поставку")
    return problems


def check_sources_reachable_claim(doc: dict) -> list[str]:
    """Официальные источники обязаны указывать дату проверки: стареют они быстрее кода."""
    problems: list[str] = []
    if not doc.get("checked_at"):
        problems.append("нет даты проверки реестра")
    for item in doc.get("entries") or []:
        if item.get("source_kind") == "official" and not item.get("note"):
            problems.append(f"{item.get('repo')}: у официального источника нужна пометка, "
                            f"что именно проверено и когда")
    return problems


def summary(doc: dict) -> str:
    entries = doc.get("entries") or []
    by_gap: dict[str, list[dict]] = {}
    for item in entries:
        by_gap.setdefault(str(item.get("gap")), []).append(item)
    lines = [f"реестр: {len(entries)} записей, задач {len(doc.get('gaps') or {})}, "
             f"проверено {doc.get('checked_at')}"]
    for gap, title in (doc.get("gaps") or {}).items():
        rows = by_gap.get(gap) or []
        top = ", ".join(f"{row['repo']} ({row['license'].split(' (')[0]})" for row in rows[:3])
        lines.append(f"  {gap:22} {len(rows)} → {top}")
    return "\n".join(lines)


def listing(doc: dict) -> str:
    lines = []
    for gap, title in (doc.get("gaps") or {}).items():
        lines.append(f"\n## {title}")
        for item in doc.get("entries") or []:
            if item.get("gap") != gap:
                continue
            mark = "можно копировать" if item.get("copy_allowed") else "только идеи"
            lines.append(f"- [{item.get('priority')}] {item.get('repo')} · "
                         f"{item.get('license')} · {mark}")
            for take in item.get("we_take") or []:
                lines.append(f"    + {take}")
            for skip in item.get("we_do_not_take") or []:
                lines.append(f"    − {skip}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    doc = load()
    problems = check_structure(doc) + check_license_policy(doc) + check_sources_reachable_claim(doc)
    print(summary(doc))
    if "--list" in argv:
        print(listing(doc))
    if problems:
        print("НАРУШЕНИЯ:")
        for item in problems:
            print(f" ✗ {item}")
        return 2
    blocked = sum(1 for item in doc["entries"] if not item["copy_allowed"])
    print(f"нарушений: 0 · записей без права копирования (только идеи): {blocked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
