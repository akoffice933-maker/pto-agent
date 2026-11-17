# -*- coding: utf-8 -*-
"""Льготы по налогу на имущество: применяются только с подтверждённым основанием.

Правило, которое проверяется: налог, уменьшенный по неподтверждённой льготе, — это не
экономия, а недоимка. Поэтому спорная льгота не применяется, а её сумма называется числом.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pto_agent import tax                                             # noqa: E402

ASSETS = REPO / "data" / "demo" / "property" / "assets_2026.json"
BENEFITS = REPO / "data" / "reference" / "property_benefits.json"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def assets() -> dict:
    return read(ASSETS)


def catalogue(confirmed: bool) -> dict:
    payload = read(BENEFITS)
    for row in payload["benefits"]:
        row["confirmed"] = confirmed
    return payload


def row_of(report: dict, inventory: str) -> dict:
    return next(row for row in report["rows"] if row.get("inventory") == inventory)


# ─────────────────────── справочник ───────────────────────
def test_benefits_catalogue_declares_schema_and_sources():
    payload = read(BENEFITS)
    assert payload["schema"] == "pto-agent/property-benefits@1"
    assert "ст. 381" in payload["source"]["federal"]
    assert payload["policy"]["rule"]
    assert payload["not_implemented"], "у механизма льгот обязан быть список «чего нет»"


def test_loader_refuses_foreign_schema(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema": "чужое"}), encoding="utf-8")
    with pytest.raises(ValueError):
        tax.load_benefits(path)


# ─────────────────────── неприменение ───────────────────────
def test_unconfirmed_benefit_is_not_applied_and_saving_is_named():
    report = tax.property_tax(assets(), "2026", benefits=catalogue(False))
    not_applied = report["benefits"]["not_applied"]
    assert len(not_applied) == 1
    row = not_applied[0]
    assert row["code"] == "REGION-CONSTRUCTION-DEMO"
    assert "не подтверждена" in row["why"]
    assert row["would_save_year"] == pytest.approx(126_500.0)
    assert report["benefits"]["applied"] == []
    # налог посчитан без льготы: ангар остаётся в базе
    assert row_of(report, "ОС-000204")["year"] > 0


def test_benefit_without_basis_document_is_not_applied():
    payload = assets()
    for asset in payload["assets"]:
        if asset["inventory"] == "ОС-000204":
            asset.pop("benefit_basis", None)
    report = tax.property_tax(payload, "2026", benefits=catalogue(True))
    assert report["benefits"]["applied"] == []
    row = report["benefits"]["not_applied"][0]
    assert "основание не указано" in row["why"]


def test_unknown_benefit_code_is_refused_not_ignored():
    payload = assets()
    for asset in payload["assets"]:
        if asset["inventory"] == "ОС-000204":
            asset["benefit"] = "ЛЮБАЯ-ЛЬГОТА"
    report = tax.property_tax(payload, "2026", benefits=catalogue(True))
    row = report["benefits"]["not_applied"][0]
    assert "нет в справочнике" in row["why"]
    assert row["would_save_year"] == 0.0      # эффект неизвестен — цифру не выдумываем


# ─────────────────────── применение ───────────────────────
def test_confirmed_benefit_zeroes_the_asset_tax():
    with_benefit = tax.property_tax(assets(), "2026", benefits=catalogue(True))
    without = tax.property_tax(assets(), "2026", benefits=catalogue(False))
    assert len(with_benefit["benefits"]["applied"]) == 1
    assert row_of(with_benefit, "ОС-000204")["year"] == 0.0
    assert row_of(with_benefit, "ОС-000204")["advances"]["Q1"] == 0.0
    assert row_of(without, "ОС-000204")["year"] > 0
    assert with_benefit["totals"]["year"] < without["totals"]["year"]
    assert without["totals"]["year"] - with_benefit["totals"]["year"] == pytest.approx(126_500.0)


def test_benefit_does_not_touch_other_objects():
    with_benefit = tax.property_tax(assets(), "2026", benefits=catalogue(True))
    without = tax.property_tax(assets(), "2026", benefits=catalogue(False))
    for inventory in ("ОС-000112", "ОС-000311"):
        assert row_of(with_benefit, inventory)["year"] == row_of(without, inventory)["year"]


def test_report_says_which_benefits_are_not_implemented():
    report = tax.property_tax(assets(), "2026", benefits=catalogue(False))
    assert report["benefits"]["not_implemented"]
    markdown = tax.to_markdown(report)
    assert "## Льготы" in markdown
    assert "НЕ применена" in markdown
    assert "126 500,00" in markdown
