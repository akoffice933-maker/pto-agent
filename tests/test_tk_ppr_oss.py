# -*- coding: utf-8 -*-
"""Реестр OSS по ТК, ППР и ИД: проверка лицензий и проверяемость утверждений.

Список — `data/reference/tk_ppr_oss.json`, документ — `docs/26_…`, проверка онлайн —
`scripts/check_platform_oss.py --list data/reference/tk_ppr_oss.json`.

Тесты держат два правила, которые легко потерять при обновлении списка:

* **лицензионная политика** — код нельзя брать без лицензии (в списке три таких проекта);
* **«заявлено» ≠ «есть»** — у каждого проекта с заявленной функцией должен быть список
  `verified` с тем, что реально проверено (дерево файлов, вызов в коде), иначе запись
  превращается в пересказ README.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_platform_oss as checker          # noqa: E402

REGISTRY = ROOT / "data" / "reference" / "tk_ppr_oss.json"


@pytest.fixture(scope="module")
def registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def test_registry_structure_ok(registry):
    assert checker.check_structure(registry) == []


def test_license_policy_ok(registry):
    assert checker.check_license_policy(registry) == []


def test_no_code_from_unlicensed_projects(registry):
    """Три проекта без лицензии — только идеи, copy_allowed=false."""
    unlicensed = [p for p in registry["projects"] if p["license"] == "нет лицензии"]
    assert len(unlicensed) >= 3
    for item in unlicensed:
        assert item["copy_allowed"] is False
        assert item["integration"] in {"reference-only", "schema-reference"}


def test_cc0_allows_copying(registry):
    """CC0 — общественное достояние: код и тексты брать можно, уведомление не требуется."""
    cc0 = [p for p in registry["projects"] if p["license"] == "CC0-1.0"]
    assert cc0 and all(p["copy_allowed"] for p in cc0)
    assert "CC0-1.0" in checker.COPY_LICENSES


def test_every_project_has_verified_evidence(registry):
    """«Заявлено в README» не считается фактом: нужна проверка по файлам."""
    for item in registry["projects"]:
        assert item.get("verified"), f"{item['repo']}: нет списка verified"
        assert all(len(v) > 10 for v in item["verified"]), item["repo"]


def test_construction_ai_core_ppr_is_external_bridge(registry):
    """Ключевой факт проверки: ТК/ППР там — вызов внешнего генератора, а не свой код."""
    item = next(p for p in registry["projects"]
                if p["repo"].endswith("construction-ai-core"))
    text = " ".join(item["verified"])
    assert "tk-generator" in text and "subprocess" in text
    assert item["copy_allowed"] is False


def test_verdict_says_no_ready_tk_ppr(registry):
    verdict = registry["verdict"]
    assert verdict["tk_ppr_ready_oss"] is False
    assert len(verdict["evidence"]) >= 4
    assert "ТК/ППР" in verdict["conclusion"] or "ТК и ППР" in verdict["conclusion"]


def test_our_repo_is_registered_without_tk_claim(registry):
    ours = next(p for p in registry["projects"]
                if p["repo"] == "akoffice933-maker/pto-agent")
    assert ours["license"] == "MIT"
    assert any("не делает" in x or "не генерирует" in x for x in ours["we_do_not_take"])


def test_registry_layers_are_covered(registry):
    """Каждый слой реестра (ИД, ТК, ППР, нормативы) должен иметь хотя бы один проект."""
    for layer in registry["layers"]:
        assert [p for p in registry["projects"] if p["layer"] == layer], layer


def test_checker_cli_accepts_other_registry(registry, tmp_path, capsys):
    """Один скрипт проверяет любой реестр: --list, а не жёстко platform_oss.json."""
    assert checker.main(["--offline", "--list", str(REGISTRY)]) == 0
    assert "ok" in capsys.readouterr().out


def test_checker_catches_copy_from_unlicensed(tmp_path, capsys):
    """Если кто-то поставит copy_allowed=true на проект без лицензии — exit 2."""
    broken = json.loads(REGISTRY.read_text(encoding="utf-8"))
    broken["projects"][1]["copy_allowed"] = True
    broken["projects"][1]["license"] = "нет лицензии"
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    assert checker.main(["--offline", "--list", str(path)]) == 2
    assert "ЛИЦЕНЗИОННАЯ ПОЛИТИКА НАРУШЕНА" in capsys.readouterr().out


def test_policy_does_not_allow_agpl_as_library(tmp_path):
    """Регресс-страж общей политики: AGPL нельзя тянуть как зависимость."""
    doc = {"layers": {"x": "слой"}, "projects": [
        {"repo": "o/r", "url": "https://github.com/o/r", "license": "AGPL-3.0", "stars": 0,
         "layer": "x", "priority": "низкий", "role": "тест", "we_take": ["идея"],
         "we_do_not_take": ["код"], "integration": "dependency", "copy_allowed": False}]}
    assert checker.check_license_policy(doc)
