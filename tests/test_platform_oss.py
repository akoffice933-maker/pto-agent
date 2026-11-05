# -*- coding: utf-8 -*-
"""OSS-реестр для расширения платформы: структура, лицензионная политика, покрытие слоёв."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from check_platform_oss import (COPY_LICENSES, PLATFORM_LAYERS, check_license_policy,
                               check_structure, load, summarize)

REGISTRY = ROOT / "data" / "reference" / "platform_oss.json"


@pytest.fixture(scope="module")
def doc():
    return load(REGISTRY)


def test_registry_is_valid_and_covers_platform_layers(doc):
    assert check_structure(doc) == []
    layers = {p["layer"] for p in doc["projects"]}
    assert PLATFORM_LAYERS <= layers            # 2…9 — каждый слой разобран


def test_license_policy_holds(doc):
    """Главная защита: код копируем только из permissive-лицензий."""
    assert check_license_policy(doc) == []
    for item in doc["projects"]:
        if item["copy_allowed"]:
            assert item["license"] in COPY_LICENSES, item["repo"]


def test_copyleft_projects_are_reference_only(doc):
    """AGPL/GPL/Sustainable Use — только идеи, схема или внешний сервис."""
    for item in doc["projects"]:
        if item["license"] in {"AGPL-3.0", "GPL-3.0", "Sustainable Use License"}:
            assert item["integration"] in {"reference-only", "external-mcp-server",
                                           "schema-reference"}, item["repo"]
            assert not item["copy_allowed"], item["repo"]


def test_no_license_project_is_ideas_only(doc):
    none_licensed = [p for p in doc["projects"] if p["license"] == "нет лицензии"]
    assert none_licensed, "ожидался хотя бы один проект без лицензии — как пример «нельзя»"
    for item in none_licensed:
        assert item["copy_allowed"] is False
        assert item["integration"] == "reference-only"


def test_every_project_says_what_we_take_and_what_we_skip(doc):
    for item in doc["projects"]:
        assert item["we_take"] and item["we_do_not_take"], item["repo"]
        assert len(item["we_take"]) >= 1


def test_priority_values_are_known(doc):
    assert {p["priority"] for p in doc["projects"]} <= {"высокий", "средний", "низкий"}


def test_summary_counts_layers(doc):
    text = summarize(doc)
    assert "проектов" in text and "слой 3" in text


def test_policy_violation_is_detected(tmp_path):
    """Скрипт обязан поймать попытку скопировать код из GPL/AGPL."""
    bad = load(REGISTRY)
    bad["projects"][0] = {**bad["projects"][0], "license": "AGPL-3.0", "copy_allowed": True}
    problems = check_license_policy(bad)
    assert problems and "копировать код нельзя" in problems[0]


def test_layer_gap_is_detected():
    doc = load(REGISTRY)
    trimmed = {** doc, "projects": [p for p in doc["projects"] if p["layer"] != "4"]}
    problems = check_structure(trimmed)
    assert any("слой 4" in p for p in problems)


def test_cli_offline_passes():
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_platform_oss.py"),
                          "--offline"], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ok" in res.stdout


def test_registry_matches_asbuilt_registry_names():
    """Реестры не должны противоречить друг другу по лицензии одного и того же проекта."""
    asbuilt = json.loads((ROOT / "data" / "reference" / "asbuilt_tools.json")
                         .read_text(encoding="utf-8"))
    platform = load(REGISTRY)
    ours = {p["repo"]: p["license"] for p in platform["projects"]}
    for item in asbuilt.get("projects") or []:
        if item["repo"] in ours:
            assert ours[item["repo"]] == item["license"], item["repo"]
