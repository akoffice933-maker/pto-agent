#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OSS-реестр для расширения pto-agent в платформу: проверка списка и лицензионной политики.

Список — `data/reference/platform_oss.json`, документ — `docs/17_…`, тесты — `tests/test_platform_oss.py`.

Зачем скрипт, а не таблица в документе:
  * звёзды, дата последнего коммита и лицензия в чужой таблице устаревают молча — скрипт
    сверяет их с GitHub API и показывает расхождения;
  * главный риск расширения — утащить AGPL/GPL или код без лицензии в MIT-поставку.
    Политика (`copy_allowed`) проверяется автоматически, нарушение = exit code 2;
  * покрытие слоёв платформы тоже проверяется: слой без единого проекта — это дыра в плане,
    а не «потом посмотрим».

Использование:
    python3 scripts/check_platform_oss.py --offline     # структура и лицензии, без сети
    python3 scripts/check_platform_oss.py               # сверить с GitHub API (нужна сеть)
    python3 scripts/check_platform_oss.py --refresh     # сверить и обновить звёзды/даты
    GH_TOKEN=... python3 scripts/check_platform_oss.py  # если упёрлись в лимит API
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "data" / "reference" / "platform_oss.json"

REQUIRED = ("repo", "url", "license", "stars", "layer", "priority", "role", "we_take",
            "we_do_not_take", "integration", "copy_allowed")

# код можно копировать только из этих лицензий (и с сохранением уведомления об авторстве;
# CC0 — посвящение в общественное достояние, уведомление не требуется)
COPY_LICENSES = {"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "ISC", "CC0-1.0"}

INTEGRATIONS = {"reference-only", "external-mcp-server", "pattern-and-code",
                "schema-reference", "data-source", "library", "dependency"}
PRIORITIES = {"высокий", "средний", "низкий"}
# обязательные слои платформы (для реестров с другой структурой слои берутся из самого файла)
PLATFORM_LAYERS = {"2", "3", "4", "5", "6", "7", "8", "9"}


def required_layers(doc: dict) -> set[str]:
    """Какие слои обязаны быть покрыты.

    У реестра платформы — 2…9. У других реестров (ТК/ППР/ИД) свой набор слоёв, и требовать
    от них платформенные нельзя: список слоёв берётся из самого файла, кроме помеченных
    в `optional_layers`.
    """
    layers = set((doc.get("layers") or {}).keys())
    if layers & PLATFORM_LAYERS:
        return PLATFORM_LAYERS
    return layers - set(doc.get("optional_layers") or [])

# ожидаемое семейство лицензии по нашей пометке → как это видит GitHub API
LICENSE_API = {
    "MIT": {"MIT"},
    "Apache-2.0": {"Apache-2.0"},
    "BSD-3-Clause": {"BSD-3-Clause"},
    "BSD-2-Clause": {"BSD-2-Clause"},
    "CC0-1.0": {"CC0-1.0", "NOASSERTION", "Other"},
    "LGPL-3.0": {"LGPL-3.0", "NOASSERTION"},
    "MPL-2.0": {"MPL-2.0"},
    "EPL-1.0": {"EPL-1.0"},
    "GPL-3.0": {"GPL-3.0"},
    "AGPL-3.0": {"AGPL-3.0"},
    "MIT (частично)": {"MIT"},
    "MIT + товарный знак": {"NOASSERTION", "MIT"},
    "двойная (не OSI)": {"NOASSERTION", "Other"},
    "Sustainable Use License": {"NOASSERTION", "Other"},
    "нет лицензии": {"NOASSERTION", "None", None},
}


def load(path: Path = LIST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_structure(doc: dict) -> list[str]:
    problems: list[str] = []
    projects = doc.get("projects") or []
    if not projects:
        return ["список проектов пуст"]
    seen: dict[str, int] = {}
    for i, item in enumerate(projects):
        name = item.get("repo") or f"#{i}"
        for field in REQUIRED:
            if field not in item or item[field] in (None, "", []):
                problems.append(f"{name}: нет поля {field}")
        if item.get("layer") not in doc.get("layers", {}):
            problems.append(f"{name}: неизвестный слой {item.get('layer')!r}")
        if item.get("priority") not in PRIORITIES:
            problems.append(f"{name}: неизвестный приоритет {item.get('priority')!r}")
        if item.get("integration") not in INTEGRATIONS:
            problems.append(f"{name}: неизвестная интеграция {item.get('integration')!r}")
        if item.get("url") and not str(item["url"]).startswith("https://github.com/"):
            problems.append(f"{name}: url не на GitHub")
        # дубликат (один проект может быть в двух слоях — тогда это осознанное решение,
        # но пометка нужна)
        key = (item.get("repo"), item.get("layer"))
        if key in seen:
            problems.append(f"{name}: дубликат в слое {item.get('layer')}")
        seen[key] = i
    for layer in sorted(required_layers(doc)):
        if not any(p.get("layer") == layer for p in projects):
            problems.append(f"слой {layer} ({doc.get('layers', {}).get(layer, '?')}) без проектов")
    return problems


def check_license_policy(doc: dict) -> list[str]:
    """Копировать код можно только из permissive-лицензий. Нарушение — breaking, exit 2."""
    problems: list[str] = []
    for item in doc.get("projects") or []:
        license_name = str(item.get("license") or "")
        allowed = item.get("copy_allowed")
        if allowed and license_name not in COPY_LICENSES:
            problems.append(f"{item.get('repo')}: copy_allowed=true при лицензии {license_name} "
                            f"— копировать код нельзя (идеи, схема, внешний сервис)")
        if not allowed and item.get("integration") == "library" and license_name in COPY_LICENSES:
            problems.append(f"{item.get('repo')}: permissive-лицензия, но library запрещена — "
                            f"противоречие")
        if license_name == "нет лицензии" and allowed:
            problems.append(f"{item.get('repo')}: без лицензии копировать нельзя")
        if license_name in {"AGPL-3.0", "GPL-3.0", "Sustainable Use License"} \
                and item.get("integration") in {"library", "dependency"}:
            problems.append(f"{item.get('repo')}: {license_name} нельзя тянуть как "
                            f"{item.get('integration')} в MIT-поставку")
    return problems


def _api(path: str, token: str | None) -> dict:
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "pto-agent-registry",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def check_online(doc: dict, token: str | None, *, write: bool = False,
                 path: Path | None = None) -> tuple[list[str], int]:
    problems: list[str] = []
    updated = 0
    today = date.today().isoformat()
    for item in doc.get("projects") or []:
        repo = item["repo"]
        try:
            meta = _api(f"/repos/{repo}", token)
        except urllib.error.HTTPError as exc:
            problems.append(f"{repo}: GitHub API {exc.code} — проект недоступен "
                            f"(переименован/удалён/приватный)")
            continue
        except Exception as exc:                       # сеть, таймаут
            problems.append(f"{repo}: не удалось проверить ({exc})")
            continue
        spdx = (meta.get("license") or {}).get("spdx_id")
        # у части проектов GitHub отдаёт NOASSERTION при понятной лицензии в файле —
        # такие случаи помечены в реестре полем license_api и проверены глазами по LICENSE
        expected = set(item.get("license_api") or LICENSE_API.get(item["license"], set()))
        if expected and spdx not in expected:
            problems.append(f"{repo}: в реестре {item['license']}, на GitHub {spdx} — "
                            f"проверить вручную")
        stars, pushed = meta.get("stargazers_count", 0), (meta.get("pushed_at") or "")[:10]
        if meta.get("archived"):
            pushed = pushed or ""
            problems.append(f"{repo}: репозиторий архивирован — брать только идеи")
        if write and (stars != item.get("stars") or pushed != item.get("pushed")):
            item["stars"], item["pushed"], item["verified_at"] = stars, pushed, today
            updated += 1
        elif not write and stars != item.get("stars"):
            problems.append(f"{repo}: звёзд в реестре {item.get('stars')}, на GitHub {stars}")
    if write:
        doc["checked_at"] = today
        doc.pop("_file", None)
        target = path or LIST
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return problems, updated


def summarize(doc: dict) -> str:
    projects = doc.get("projects") or []
    layers = doc.get("layers") or {}
    by_layer: dict[str, list[dict]] = {}
    for item in projects:
        by_layer.setdefault(item["layer"], []).append(item)
    lines = [f"проектов: {len(projects)} · слоёв: {len(by_layer)}"]
    for layer in sorted(by_layer, key=lambda x: (x != "x", x)):
        items = by_layer[layer]
        copy = sum(1 for i in items if i.get("copy_allowed"))
        lines.append(f"  слой {layer} — {layers.get(layer, '?')}: {len(items)} проектов, "
                     f"код можно брать у {copy}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="без сети: структура и лицензии")
    parser.add_argument("--list", dest="list_path", default=str(LIST),
                        help="файл реестра (по умолчанию data/reference/platform_oss.json)")
    parser.add_argument("--refresh", action="store_true",
                        help="сверить с GitHub и обновить звёзды/даты в JSON")
    parser.add_argument("--path", default="", help="то же, что --list (прежний ключ)")
    args = parser.parse_args(argv)

    list_path = Path(args.path or args.list_path)
    doc = load(list_path)
    doc["_file"] = list_path.name
    print(summarize(doc))

    structural = check_structure(doc)
    policy = check_license_policy(doc)
    for problem in structural:
        print("  структура:", problem)
    for problem in policy:
        print("  лицензии:", problem)

    online: list[str] = []
    updated = 0
    if not args.offline:
        online, updated = check_online(doc, os.environ.get("GH_TOKEN"), write=args.refresh,
                                      path=list_path)
        for problem in online:
            print("  github:", problem)
        if updated:
            print(f"обновлено записей: {updated}")

    if policy:
        print("ЛИЦЕНЗИОННАЯ ПОЛИТИКА НАРУШЕНА — расширять нельзя")
        return 2
    if structural:
        return 1
    if online:
        print("есть расхождения с GitHub (не блокирующие)")
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
