# -*- coding: utf-8 -*-
"""Смежные проекты: проверка списка и лицензионной политики.

Тесты офлайновые намеренно: в CI нет гарантированного доступа к GitHub, а падать должна
не сеть, а смысл — «можно ли брать этот код в MIT-поставку».
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "data" / "reference" / "analogue_projects.json"
sys.path.insert(0, str(ROOT / "scripts"))

import check_analogues as ca  # noqa: E402


@pytest.fixture(scope="module")
def doc():
    return ca.load(LIST)


def test_list_is_valid_offline(doc):
    """Структура и политика копирования согласованы."""
    assert ca.check_structure(doc) == []


def test_projects_are_unique_and_linked(doc):
    urls = [p["url"] for p in doc["projects"]]
    assert len(urls) == len(set(urls))
    for p in doc["projects"]:
        assert p["url"].rstrip("/").lower() == f"https://github.com/{p['repo']}".lower()


def test_agpl_and_unlicensed_code_is_never_copyable(doc):
    """Ни один AGPL/без-лицензии проект не помечен как «можно копировать»."""
    risky = [p for p in doc["projects"] if p["license"] in ca.COPY_FORBIDDEN]
    assert risky, "в списке должен быть хоть один AGPL — иначе проверка ничего не стоит"
    for p in risky:
        assert p["copy_allowed"] is False, p["repo"]
        assert p["integration"] != "pattern-and-code", p["repo"]


def test_mit_projects_have_a_concrete_use(doc):
    """У каждого MIT-проекта написано, что именно берём и что не берём."""
    for p in doc["projects"]:
        if p["copy_allowed"]:
            assert p["license"] in ca.COPY_OK
            assert len(p["we_take"]) >= 2
            assert p["we_do_not_take"], p["repo"]


def test_every_project_says_what_we_take(doc):
    for p in doc["projects"]:
        assert p["we_take"] and all(len(x) > 20 for x in p["we_take"]), p["repo"]
        assert len(p["role"]) > 30, p["repo"]


def test_interfaces_with_our_modules(doc):
    """Интеграционные типы ограничены известным набором — их разбирает документ 11."""
    kinds = {p["integration"] for p in doc["projects"]}
    assert kinds <= ca.INTEGRATIONS
    # хотя бы один проект должен стыковаться по MCP — иначе список не про нас
    assert "external-mcp-server" in kinds


def test_data_source_requires_manual_license_check(doc):
    """База расценок отдана как data-source, но с обязательной ручной проверкой."""
    ds = [p for p in doc["projects"] if p["integration"] == "data-source"]
    assert ds
    for p in ds:
        assert p["copy_allowed"] is False
        assert "лиценз" in " ".join(p["we_do_not_take"]).lower()


def test_policy_rules_are_written(doc):
    rules = doc["rules"]
    assert "AGPL" in rules["agpl"]
    assert "MIT" in rules["copy_code"]
    assert "все права защищены" in rules["no_license"]


def test_breaking_change_is_caught(doc):
    """Если кто-то разрешит копирование AGPL-кода — проверка обязана это поймать."""
    bad = json.loads(json.dumps(doc))
    agpl = next(p for p in bad["projects"] if p["license"] == "AGPL-3.0")
    agpl["copy_allowed"] = True
    assert any("copy_allowed" in e for e in ca.check_structure(bad))


def test_cli_offline_exit_code():
    """CLI в офлайн-режиме возвращает 0 и печатает сводку."""
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_analogues.py"), "--offline"],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "смежных проектов" in res.stdout
    assert "ok:" in res.stdout or "ок:" in res.stdout
