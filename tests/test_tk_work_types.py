# -*- coding: utf-8 -*-
"""ТК по всем видам работ матрицы: прогон, который не даёт появиться пустым обещаниям.

Тест держит скрипт `scripts/check_tk_all_work_types.py` в рабочем состоянии: если в матрицу
добавят вид работ, у которого не выводится комплект ИД или нет ни одной нормы, — упадёт здесь,
а не в руках инженера.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_tk_all_work_types as checker       # noqa: E402
from pto_agent.asbuilt.requirements import load_matrix, work_types  # noqa: E402


@pytest.fixture(scope="module")
def report():
    return checker.run()


def test_all_work_types_have_no_violations(report):
    bad = [(r["id"], r["problems"]) for r in report["results"] if r["problems"]]
    assert bad == [], bad


def test_every_work_type_in_matrix_is_covered(report):
    assert len(report["results"]) == len(work_types(load_matrix()))
    assert report["totals"]["work_types"] >= 17


def test_every_draft_asks_for_what_it_does_not_know(report):
    """У каждого вида работ минимум 3 запроса данных: операции, машины, трудозатраты."""
    for result in report["results"]:
        assert result["draft"]["totals"]["needs_input"] >= 3, result["id"]


def test_every_draft_has_quality_section(report):
    for result in report["results"]:
        quality = next(s for s in result["draft"]["sections"] if s["key"] == "quality")
        assert quality["items"], result["id"]


def test_demolition_states_matrix_requires_no_documents(report):
    """Демонтаж: матрица не требует ИД — это пишется явно, а не остаётся пустым разделом."""
    result = next(r for r in report["results"] if r["id"] == "demolition")
    quality = next(s for s in result["draft"]["sections"] if s["key"] == "quality")
    assert "не задаёт исполнительных документов" in quality["items"][0]["text"]
    assert quality["needs_input"], "и подтверждение этого у инженера запрашивается"


def test_unverified_norms_have_reasons(report):
    for result in report["results"]:
        for norm in result["draft"]["norms"]:
            assert norm["verified"] or norm["reason"], (result["id"], norm)


def test_markdown_lists_what_to_load_into_norm_base(report):
    text = checker.to_markdown(report, load_matrix())
    assert "Что загрузить в нормативную базу" in text
    assert "СП 70.13330.2012" in text          # самый частый документ в матрице
    assert "готова к применению" not in text.lower()


def test_cli_reports_zero_violations(capsys):
    assert checker.main([]) == 0
    out = capsys.readouterr().out
    assert "с нарушениями: 0" in out and out.strip().endswith("ok")
