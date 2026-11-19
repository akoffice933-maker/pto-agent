# -*- coding: utf-8 -*-
"""Неявный индекс сметы: считает то, что заявлено, и не выдумывает остальное."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location(
        "check_implicit_index", ROOT / "scripts" / "check_implicit_index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FER = {
    "08-02-390-01": {"name": "Короба пластмассовые", "price": 200.0, "unit": "100 м",
                     "salary": 150.0, "machines": 0.0, "materials": 50.0,
                     "labour": 150.0, "collection": "Электротехнические установки"},
    "08-02-399-01": {"name": "Провод в коробах", "price": 40.0, "unit": "100 м",
                     "salary": 20.0, "machines": 0.0, "materials": 20.0,
                     "labour": 20.0, "collection": "Электротехнические установки"},
}
PARSED = {"items": [
    # цена ровно ×100 к базису монтажа
    {"n": 1, "code": "ГЭСНм08-02-390-01", "name": "Короба пластмассовые (116 м)",
     "unit": "100 м", "qty": 1.0, "values": [20_000.0]},
    # демонтаж той же нормы: ×0,4 от монтажного индекса
    {"n": 2, "code": "ГЭСНм08-02-390-01", "name": "Демонтаж коробов пластмассовых",
     "unit": "100 м", "qty": 1.0, "values": [8_000.0]},
    # материал: своей расценки нет
    {"n": 3, "code": "21.1.06.09-0151", "name": "Кабель ВВГнг 3х1,5",
     "unit": "1000 м", "qty": 0.3, "values": [5_000.0]},
    # ремонтная расценка: в базе ФЕР её нет
    {"n": 4, "code": "ГЭСНр67-01-004-05", "name": "Демонтаж светильников",
     "unit": "100 шт", "qty": 0.37, "values": [7_340.2]},
    # нулевая сумма: цену за единицу не вывести
    {"n": 5, "code": "ГЭСНм08-02-399-01", "name": "Провод в коробах",
     "unit": "100 м", "qty": 0.0, "values": [0.0]},
]}


def test_index_is_price_over_base_price():
    res = _mod().analyse(PARSED, FER)
    row = next(r for r in res["rows"] if r["n"] == 1)
    assert row["index_total"] == pytest.approx(100.0)


def test_demolition_is_its_own_row_not_an_outlier():
    """Демонтаж идёт с коэффициентом — если мерить общей медианой, он «выпадает» ложно."""
    res = _mod().analyse(PARSED, FER)
    dem = next(r for r in res["rows"] if r["n"] == 2)
    assert dem["group"] == "демонтаж"
    assert dem["flag"] == "ok"
    assert res["demolition_ratio_to_median"] == pytest.approx(0.4, abs=0.01)  # ×0,4 — нормативный коэффициент демонтажа


def test_material_and_missing_rate_are_skipped_with_reason_not_zero():
    res = _mod().analyse(PARSED, FER)
    reasons = {s["n"]: s["why"] for s in res["skipped"]}
    assert 3 in reasons and "расценки нет" in reasons[3]
    assert 4 in reasons and "ФЕР-2020" in reasons[4]
    assert 5 in reasons and "количества" in reasons[5]
    assert all(s["why"].strip() for s in res["skipped"])


def test_limits_are_stated_not_hidden():
    """Ограничение «нет поресурсной развёртки» обязано быть в отчёте, а не в голове автора."""
    res = _mod().analyse(PARSED, FER)
    text = " ".join(res["not_done"])
    assert "поресурсного" in text.lower()
    assert "не проверка правильности цены" in text


def test_markdown_has_no_empty_cells_for_flags():
    res = _mod().analyse(PARSED, FER)
    md = _mod().to_markdown(res)
    assert "| 1 | ГЭСНм08-02-390-01" in md
    assert "×0.4" in md or "×0.34" in md
    assert md.count("outlier") <= 1
