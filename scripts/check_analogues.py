#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Аналоги и смежные проекты: проверка списка и лицензионной политики.

Список лежит в `data/reference/analogue_projects.json`; документ — `docs/11_…`.

Зачем скрипт, а не просто таблица в README:
  * звёзды и лицензии в чужой таблице устаревают молча — скрипт сверяет их с GitHub API;
  * главный риск при «добавим к себе» — скопировать AGPL или код без лицензии в MIT-поставку;
    политика (`copy_allowed`) проверяется автоматически и о breaking change сообщает exit code 2;
  * тесты (`tests/test_analogues.py`) гоняют `--offline`, поэтому в CI нет сети и нет флаки.

Использование:
    python3 scripts/check_analogues.py --offline          # проверка структуры и политики (без сети)
    python3 scripts/check_analogues.py                    # сверить с GitHub API
    python3 scripts/check_analogues.py --write            # обновить звёзды/лицензии в JSON
    GH_TOKEN=... python3 scripts/check_analogues.py       # если упёрлись в лимит API
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
LIST = ROOT / "data" / "reference" / "analogue_projects.json"

REQUIRED = ("repo", "url", "license", "stars", "role", "we_take", "we_do_not_take",
            "integration", "copy_allowed")
INTEGRATIONS = {"reference-only", "external-mcp-server", "pattern-and-code",
                "schema-reference", "data-source", "terminology-reference"}
# лицензии, код которых можно брать в MIT-репозиторий (с уведомлением об авторстве)
COPY_OK = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC"}
# лицензии, код которых брать нельзя ни при каких условиях
COPY_FORBIDDEN = {"AGPL-3.0", "GPL-3.0", "GPL-2.0", "NO-LICENSE", "NOASSERTION", "UNLICENSED"}


def load(path: Path = LIST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_structure(doc: dict) -> list[str]:
    """Офлайн-проверка: поля, типы, уникальность ссылок, согласованность политики."""
    errors: list[str] = []
    if doc.get("schema") != "pto-agent/analogues@1":
        errors.append(f"неизвестная схема: {doc.get('schema')!r}")
    if doc.get("our_license") != "MIT":
        errors.append("лицензия проекта должна быть MIT")
    try:
        date.fromisoformat(str(doc.get("checked_at")))
    except ValueError:
        errors.append(f"checked_at не ISO-дата: {doc.get('checked_at')!r}")
    for key in ("copy_code", "copy_data", "agpl", "no_license"):
        if not (doc.get("rules") or {}).get(key):
            errors.append(f"не описано правило политики: {key}")

    seen_urls: set[str] = set()
    for i, p in enumerate(doc.get("projects") or []):
        name = p.get("repo") or f"#{i}"
        for field in REQUIRED:
            if field not in p:
                errors.append(f"{name}: нет поля {field}")
        url = p.get("url") or ""
        if not url.startswith("https://github.com/"):
            errors.append(f"{name}: url не похож на ссылку GitHub")
        if url in seen_urls:
            errors.append(f"{name}: дубль ссылки {url}")
        seen_urls.add(url)
        if p.get("repo") and url and url.rstrip("/").lower() != \
                f"https://github.com/{p['repo']}".lower():
            errors.append(f"{name}: url не совпадает с repo")
        if p.get("integration") not in INTEGRATIONS:
            errors.append(f"{name}: неизвестный integration {p.get('integration')!r}")
        if not isinstance(p.get("stars"), int) or p["stars"] < 0:
            errors.append(f"{name}: stars должен быть неотрицательным целым")
        if not isinstance(p.get("we_take"), list) or not p["we_take"]:
            errors.append(f"{name}: we_take должен быть непустым списком")
        # главное: политика копирования обязана совпадать с лицензией
        lic = str(p.get("license") or "").upper()
        if p.get("copy_allowed") and lic not in COPY_OK:
            errors.append(f"{name}: copy_allowed=true при лицензии {p.get('license')} — "
                          f"так нельзя, код попадёт в MIT-поставку")
        if not p.get("copy_allowed") and lic in COPY_FORBIDDEN and \
                p.get("integration") in {"pattern-and-code"}:
            errors.append(f"{name}: integration=pattern-and-code требует права копирования")
    if not doc.get("projects"):
        errors.append("список проектов пуст")
    return errors


def license_notes(doc: dict) -> list[str]:
    notes = []
    for p in doc.get("projects") or []:
        lic = str(p.get("license") or "")
        if lic in {"AGPL-3.0", "NO-LICENSE", "NOASSERTION"}:
            notes.append(f"{p['repo']}: {lic} → только чтение как идеи, код не копируем")
    return notes


def fetch(repo: str, token: str | None) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "pto-agent-analogues-check"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    return {"stars": d.get("stargazers_count"), "license": (d.get("license") or {}).get("spdx_id")
            or "NO-LICENSE", "language": d.get("language"), "pushed_at": str(d.get("pushed_at"))[:10]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Проверка списка смежных проектов и лицензий")
    ap.add_argument("--offline", action="store_true", help="только локальная проверка, без сети")
    ap.add_argument("--write", action="store_true", help="записать свежие звёзды/лицензии в JSON")
    ap.add_argument("--list", default=str(LIST), help="путь к JSON")
    args = ap.parse_args(argv)

    path = Path(args.list)
    doc = load(path)
    errors = check_structure(doc)
    projects = doc.get("projects") or []

    print(f"смежных проектов: {len(projects)}; лицензия проекта: {doc.get('our_license')}")
    print(f"{'репозиторий':<58} {'лицензия':<13} {'★':>5}  {'копировать':<10} интеграция")
    for p in projects:
        can = "да" if p.get("copy_allowed") else "нет"
        print(f"{p.get('repo', '?'):<58} {p.get('license', '?'):<13} "
              f"{p.get('stars', 0):>5}  {can:<10} {p.get('integration')}")

    if not args.offline:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        drift, failed = [], []
        for p in projects:
            try:
                live = fetch(p["repo"], token)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                failed.append(f"{p['repo']}: {e}")
                continue
            if live["license"] != p.get("license"):
                drift.append(f"{p['repo']}: лицензия была {p.get('license')}, стала {live['license']}")
            if live["stars"] != p.get("stars"):
                drift.append(f"{p['repo']}: звёзд было {p.get('stars')}, стало {live['stars']}")
            if args.write:
                p.update({k: live[k] for k in ("stars", "license", "language", "pushed_at")})
        if drift:
            print("\nрасхождения с GitHub:")
            for d in drift:
                print("  •", d)
        if failed:
            print("\nне удалось проверить:")
            for f in failed:
                print("  •", f)
        if args.write:
            doc["checked_at"] = date.today().isoformat()
            errors += check_structure(doc)   # перепроверяем после обновления лицензий
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"\nзаписано: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")

    notes = license_notes(doc)
    if notes:
        print("\nлицензионные ограничения:")
        for n in notes:
            print("  •", n)

    if errors:
        print("\nОШИБКИ:")
        for e in errors:
            print("  ✗", e)
        return 2
    copyable = sum(1 for p in projects if p.get("copy_allowed"))
    print(f"\nок: структура и политика копирования согласованы "
          f"(код можно брать у {copyable} из {len(projects)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
