# -*- coding: utf-8 -*-
"""Индексы Минстроя: значение только с источником, прочерк — не ноль."""
import gzip
import json
from pathlib import Path

import pytest

from pto_agent import indices

FIELDS = ["quarter", "letter", "table_label", "table_title", "subject_no",
          "subject", "column", "column_title", "value", "page"]


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    rows = [
        # 1) индекс «дороги» для Краснодарского края, две колонки
        ("III кв. 2026", "53691-АЛ/09 от 28.08.2026", "Автомобильные дороги",
         "Индексы к ФЕР-2001/ТЕР-2001 по объектам строительства «Автомобильные дороги»",
         37, "Краснодарский край", 1, "Автомобильные дороги", "12.66", 52),
        ("III кв. 2026", "53691-АЛ/09 от 28.08.2026", "Автомобильные дороги",
         "Индексы к ФЕР-2001/ТЕР-2001 по объектам строительства «Автомобильные дороги»",
         37, "Краснодарский край", 2, "Искусственные дорожные сооружения", "20.02", 52),
        # 2) прочерк: индекса нет (НЕ ноль)
        ("III кв. 2026", "53691-АЛ/09 от 28.08.2026", "Автомобильные дороги",
         "Индексы к ФЕР-2001/ТЕР-2001 по объектам строительства «Автомобильные дороги»",
         3, "Ямало-Ненецкий автономный округ (3 зона)", 1, "Автомобильные дороги", "", 2),
    ]
    with gzip.open(tmp_path / indices.CSV_NAME, "wt", encoding="utf-8", newline="") as f:
        f.write(",".join(FIELDS) + "\n")
        for row in rows:
            f.write(",".join(str(v) for v in row) + "\n")
    (tmp_path / indices.META_NAME).write_text(json.dumps({
        "source": "ФГИС ЦС NormLegalDocPublished baseKind=40",
        "values": len(rows), "not_parsed_reason": "приложения 1, 2, 4, 6 не разобраны",
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_found_index_has_source_and_page(data_dir):
    result = indices.index_for("Краснодарский край", directory=data_dir)
    assert result["status"] == "found"
    table = result["tables"][0]
    assert table["page"] == 52
    assert "53691-АЛ/09" in table["letter"]
    assert table["values"][0]["value"] == 12.66
    assert table["values"][0]["column_title"] == "Автомобильные дороги"
    assert result["source"].startswith("ФГИС ЦС")


def test_subject_lookup_ignores_case(data_dir):
    assert indices.index_for("краснодарский край", directory=data_dir)["status"] == "found"


def test_dash_means_unset_not_zero(data_dir):
    result = indices.index_for("Ямало-Ненецкий автономный округ (3 зона)", directory=data_dir)
    assert result["status"] == "found"
    assert result["tables"][0]["values"][0]["value"] is None     # не 0.0 и не 1.0
    assert result["tables"][0]["values"][0]["unset"] is True
    assert result["unset"] == 1
    assert "не установлен" in result["note"]


def test_unknown_subject_is_not_guessed(data_dir):
    result = indices.index_for("Чукотский автономный округ", directory=data_dir)
    assert result["status"] == "not_found"
    assert "не разбирается" in result["reason"] or "нет" in result["reason"]
    assert result["available_subjects"] == 2


def test_table_filter_separates_kinds(data_dir):
    result = indices.index_for("Краснодарский край", directory=data_dir, table="нет такой")
    assert result["status"] == "not_found"
    result = indices.index_for("Краснодарский край", directory=data_dir, table="Автомобильные")
    assert result["status"] == "found"


def test_no_data_says_why(tmp_path):
    result = indices.index_for("Краснодарский край", directory=tmp_path / "nope")
    assert result["status"] == "no_data"
    assert "scripts/import_indices.py" in result["reason"]


def test_markdown_keeps_unparsed_scope_visible(data_dir):
    text = indices.indices_to_markdown(
        indices.index_for("Краснодарский край", directory=data_dir),
        indices.load(data_dir))
    assert "стр. 52" in text
    assert "не разобраны" in text          # честный список того, что вне разбора
    assert "12.66" in text


def test_unstick_restores_spaces():
    assert indices._unstick("РеспубликаТыва") == "Республика Тыва"
    assert indices._unstick("Краснодарский край") == "Краснодарский край"


def test_bundled_data_is_loadable():
    """В репозитории лежит разобранное письмо — проверяем, что оно читается."""
    data = indices.load(Path(__file__).resolve().parents[1] / "data" / "indices")
    assert data["status"] == "ok"
    assert any(r["subject"] == "Краснодарский край" for r in data["rows"])
