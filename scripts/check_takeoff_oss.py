# -*- coding: utf-8 -*-
"""Проверка реестра takeoff-проектов: `data/reference/takeoff_oss.json`.

Офлайн-режим (по умолчанию в CI): проверяет сам реестр — обязательные поля, отсутствие
противоречий «лицензия разрешает / copy_allowed false», правило «нет лицензии — не берём».
Онлайн-режим (`--online`) сверяет звёзды и лицензии по GitHub API — тот же подход, что в
`check_platform_oss.py`: цифры в реестре не должны расходиться с GitHub.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "data" / "reference" / "takeoff_oss.json"

REQUIRED = ("repo", "layer", "stars", "license", "language", "pushed_at", "what", "why",
            "copy_allowed", "how", "limits")
COPY_OK = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD"}
COPY_NO = {"AGPL-3.0", "GPL-2.0", "GPL-3.0", "LGPL-3.0", "NOASSERTION", None, "", "None"}


def check_offline(doc: dict) -> list[str]:
    problems: list[str] = []
    repos = doc.get("repos") or []
    if not repos:
        return ["реестр пуст"]

    for repo in repos:
        for field in REQUIRED:
            if field not in repo:
                problems.append(f"{repo.get('repo')}: нет поля {field}")
        if not repo.get("limits"):
            problems.append(f"{repo['repo']}: не указано, чего проект не умеет — "
                            f"это первое, что спросят")
        license_id = repo.get("license")
        api_id = repo.get("license_api", license_id)
        allowed = bool(repo.get("copy_allowed"))
        effective = api_id if api_id is not None else license_id
        if allowed and effective not in COPY_OK:
            problems.append(f"{repo['repo']}: copy_allowed=true, но лицензия {effective!r} "
                            f"не входит в разрешённый список")
        if not allowed and effective in COPY_OK:
            problems.append(f"{repo['repo']}: лицензия {effective} позволяет брать код, "
                            f"а copy_allowed=false — противоречие")
        if effective is None and allowed:
            problems.append(f"{repo['repo']}: лицензии нет — код брать нельзя")

    declared = (doc.get("summary") or {}).get("copy_allowed")
    actual = sum(1 for r in repos if r.get("copy_allowed"))
    if declared is not None and declared != actual:
        problems.append(f"summary.copy_allowed={declared}, а по факту {actual}")
    if (doc.get("summary") or {}).get("repos") not in (None, len(repos)):
        problems.append(f"summary.repos={(doc.get('summary') or {}).get('repos')}, "
                        f"а в реестре {len(repos)}")
    return problems


def _api(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "pto-agent-oss-check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def check_online(doc: dict) -> list[str]:
    problems: list[str] = []
    for repo in doc.get("repos") or []:
        try:
            data = _api(f"https://api.github.com/repos/{repo['repo']}")
        except Exception as error:                      # noqa: BLE001 — сеть бывает разной
            problems.append(f"{repo['repo']}: GitHub недоступен ({error})")
            continue
        if data.get("message"):
            problems.append(f"{repo['repo']}: {data['message']}")
            continue
        stars = data.get("stargazers_count")
        if stars != repo.get("stars"):
            problems.append(f"{repo['repo']}: звёзд на GitHub {stars}, в реестре "
                            f"{repo.get('stars')}")
        api_license = (data.get("license") or {}).get("spdx_id")
        if api_license != repo.get("license_api"):
            problems.append(f"{repo['repo']}: лицензия по API {api_license}, "
                            f"в реестре {repo.get('license_api')}")
        if not data.get("archived") is False and repo.get("copy_allowed"):
            problems.append(f"{repo['repo']}: архивный репозиторий, а помечен как пригодный")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Проверка реестра takeoff-проектов")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--online", action="store_true",
                        help="сверить звёзды и лицензии по GitHub API")
    args = parser.parse_args(argv)

    doc = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    problems = check_offline(doc)
    if args.online:
        problems += check_online(doc)

    repos = doc.get("repos") or []
    allowed = sum(1 for r in repos if r.get("copy_allowed"))
    print(f"проектов: {len(repos)} · код можно брать у {allowed} · "
          f"только идеи: {len(repos) - allowed}")
    if problems:
        print("расхождения:", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 2
    print("расхождений нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
