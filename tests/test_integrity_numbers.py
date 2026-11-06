# -*- coding: utf-8 -*-
"""Целостность цифр: `kr_pdf`, `takeoff`, `kr_schedules`, база цен.

Общий корень всех дефектов этого круга — `float()` в Python принимает `nan` и `inf`:

* строка «nan» в выгрузке обмера проходила как количество → объём `nan` → сумма `nan`;
* «Итого: nan» в ведомости стали **проходило проверку арифметики**: `abs(nan - x) > доп`
  всегда `False`, то есть сверка «Итого» с суммой по диаметрам молчала;
* неразобранный диаметр превращался в `0.0`, и сумма «сходилась» с испорченной цифрой.

Отдельно — потеря и удвоение объёма: дедупликация бетона сливала разные конструкции с
одинаковым количеством, а одинаковые строки из склеенных листов удваивали итог.
"""
from __future__ import annotations

from datetime import date

import pytest

from pto_agent import kr_pdf, kr_schedules, takeoff
from pto_agent.numeric import parse_number, positive
from pto_agent.prices import import_prices, read_rows

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
PRICES_KR = ROOT / "data" / "demo" / "prices" / "fgis_cs_2025q3_kr.csv"


@pytest.fixture()
def prices_dir(tmp_path):
    out = tmp_path / "prices"
    import_prices(PRICES_KR, source="ФГИС ЦС (КР)", price_date="2025-09-01", region="47",
                  out_dir=out)
    return out


def _line(description, qty, unit="m3", confidence="HIGH", source="КР-1"):
    return {"code": "", "description": description, "unit": unit, "unit_native": unit,
            "qty": qty, "qty_native": qty, "confidence": confidence, "source": source,
            "trade": "", "file": "test.csv"}


# ─────────────────────────── разбор чисел ──────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("nan", None), ("NaN", None), ("inf", None), ("-inf", None),
    ("1 234,56", 1234.56), ("1\u00a0234.56", 1234.56),
    ("−12,5", -12.5),                      # юникодный минус из PDF
    ("(1 234,56)", -1234.56),              # бухгалтерские скобки
    ("12,5 м3", None),                     # единица в ячейке — не число, а не «12,5»
    ("десять", None), ("", None), (None, None), (0, 0.0), (3, 3.0),
])
def test_parse_number(value, expected):
    assert parse_number(value) == expected


def test_positive_rejects_zero_and_negative():
    assert positive("0,00") is None and positive("-300") is None
    assert positive("300") == 300.0


# ─────────────────────────── kr_pdf ───────────────────────────────────
def _facts(gabarits, thickness="300"):
    return {"by_name": {"габариты здания": {"value": gabarits, "page": 3},
                        "толщина фундаментной плиты": {"value": thickness, "page": 4}}}


def test_kr_pdf_nan_size_gives_no_volume():
    """«12,0 х nan» раньше давало объём nan — то есть строку с суммой nan."""
    result = kr_pdf.derive_boq(_facts("12,0 х nan"))
    assert result["lines"] == []
    assert any("не разобраны" in n["why"] for n in result["needs_input"])


def test_kr_pdf_zero_size_is_question_not_zero_volume():
    result = kr_pdf.derive_boq(_facts("12,0 х 0,0"))
    assert result["lines"] == []
    need = next(n for n in result["needs_input"] if n["what"].startswith("площадь"))
    assert "габариты в томе есть" in need["why"] and "0,0" in need["why"]


def test_kr_pdf_still_computes_on_good_numbers():
    result = kr_pdf.derive_boq(_facts("72,0 х 42,0"))
    plate = result["lines"][0]
    assert plate["qty"] == pytest.approx(72 * 42 * 300 / 1000, abs=0.1)
    assert "×" in plate["formula"] and plate["confidence"] == "medium"


def test_kr_pdf_nan_thickness_is_question():
    """Толщина «nan» не должна дать плиту: раньше `if area and t_plate` пропускал nan."""
    result = kr_pdf.derive_boq(_facts("72,0 х 42,0", thickness="nan"))
    assert not [l for l in result["lines"] if "фундаментная плита" in l["description"]]


# ─────────────────────────── kr_schedules ─────────────────────────────
def _doc(steel=None, concrete=None, name="Ведомость расхода стали на плиту", rows=None):
    table = {"name": name}
    if steel is not None:
        table["steel"] = steel
    if concrete is not None:
        table["concrete"] = concrete
    if rows is not None:
        table["rows"] = rows
    return {"sheets": [{"sheet": 1, "file": "f.xlsx", "tables": [table]}]}


def test_kr_schedules_nan_total_is_caught():
    """«Итого: nan» проходило сверку: разница с nan никогда не «больше допуска»."""
    doc = _doc(steel={"grade": "А500С", "total_kg": "nan",
                      "diameters": {"12": "1200", "16": "800"}},
               concrete={"mark": "В25", "qty": 100})
    validation = kr_schedules.validate_schedules(doc)
    assert validation["ok"] is False
    assert any("не число" in f["detail"] for f in validation["sum_mismatches"])


def test_kr_schedules_nan_total_does_not_become_quantity():
    doc = _doc(steel={"grade": "А500С", "total_kg": "nan",
                      "diameters": {"12": "1200", "16": "800"}},
               concrete={"mark": "В25", "qty": 100})
    validation = kr_schedules.validate_schedules(doc)
    boq = kr_schedules.to_boq(doc, validation)
    rebar = next(l for l in boq["lines"] if l["description"].startswith("Арматура"))
    assert rebar["qty"] == pytest.approx(2.0)          # 2 000 кг из диаметров, не nan
    assert rebar["confidence"] == "low" and "не число" in rebar["caveat"]


def test_kr_schedules_unparsed_diameter_is_finding_not_zero():
    doc = _doc(steel={"grade": "А500С", "total_kg": "2000",
                      "diameters": {"12": "1200", "16": "б/н"}})
    validation = kr_schedules.validate_schedules(doc)
    unparsed = [f for f in validation["findings"] if f["type"] == "VALUE_UNPARSED"]
    assert unparsed and "16" in unparsed[0]["detail"] and "б/н" in unparsed[0]["detail"]
    # сумма по диаметрам считается без испорченного значения, а не с нулём вместо него
    assert validation["findings"][0]["expected"] != "0"


def test_kr_schedules_zero_concrete_is_finding():
    validation = kr_schedules.validate_schedules(_doc(concrete={"mark": "В25", "qty": 0}))
    assert any("0 м3" in f["detail"] for f in validation["findings"])


def test_kr_schedules_row_without_quantity_is_reported():
    doc = _doc(steel={"grade": "А500С", "total_kg": "1000", "diameters": {"12": "1000"}},
               rows=[{"name": "Колонна К-1", "unit": "шт", "qty": "nan"}])
    validation = kr_schedules.validate_schedules(doc)
    boq = kr_schedules.to_boq(doc, validation)
    assert not [l for l in boq["lines"] if "Колонна" in l["description"]]
    assert any("Колонна К-1" in f["detail"] for f in boq["findings"]
               if f["type"] == "QTY_MISSING")


def test_dedupe_keeps_different_elements_with_same_quantity():
    """Плита 100 м3 и стены подвала 100 м3 — 200 м3, а не «дубль»."""
    lines = [{"description": "Бетон В25 — плита", "unit": "м3", "qty": 100.0},
             {"description": "Бетон В25 — стены подвала", "unit": "м3", "qty": 100.0}]
    kept = kr_schedules._dedupe_concrete(lines)
    assert len(kept) == 2 and sum(l["qty"] for l in kept) == pytest.approx(200.0)
    assert any("другая конструкция" in (l.get("note") or "") for l in kept)


def test_dedupe_still_merges_spec_and_schedule():
    """Спецификация без конструкции и ведомость с конструкцией — один и тот же объём."""
    lines = [{"description": "Бетон В25 W6 F50", "unit": "м3", "qty": 100.0},
             {"description": "Бетон В25 W6 F150 — Плита", "unit": "м3", "qty": 100.0}]
    kept = kr_schedules._dedupe_concrete(lines)
    assert len(kept) == 1 and kept[0]["qty"] == pytest.approx(100.0)


# ─────────────────────────── takeoff ──────────────────────────────────
def test_takeoff_nan_quantity_is_not_a_number(tmp_path):
    path = tmp_path / "boq.csv"
    path.write_text("description,unit,qty,confidence\nБетон В25,m3,nan,HIGH\n", encoding="utf-8")
    lines, problems = takeoff.read_boq(path)
    assert lines[0]["qty"] is None
    assert any("не число" in p for p in problems)


def test_takeoff_unit_mismatch_amount_leaves_total(prices_dir):
    """100 шт по цене за м2 — 125 000 ₽, которых в «итого» быть не должно."""
    draft = takeoff.build_draft([_line("Бетон В25 — плита", 42.5),
                                 _line("Опалубка щитовая — стены", 100, unit="шт")],
                                mapping=takeoff.load_map(), prices_dir=prices_dir,
                                as_of="2025-12-01")
    mismatch = next(l for l in draft["lines"] if l["element"].startswith("Опалубка"))
    assert mismatch["amount"] is None and mismatch["amount_wrong_unit"] == pytest.approx(125000.0)
    assert mismatch["status"] == takeoff.STATUS_REVIEW
    assert draft["totals"]["wrong_unit_lines"] == 1
    assert draft["totals"]["amount_rub"] == pytest.approx(6200 * 42.5)


def test_takeoff_negative_quantity_not_in_total(prices_dir):
    draft = takeoff.build_draft([_line("Бетон В25 — плита", -10)], mapping=takeoff.load_map(),
                                prices_dir=prices_dir, as_of="2025-12-01")
    line = draft["lines"][0]
    assert line["amount"] is None and line["status"] == takeoff.STATUS_REVIEW
    assert "отрицательный объём" in line["reason"]
    assert draft["totals"]["amount_rub"] is None and draft["totals"]["negative_lines"] == 1


def test_takeoff_duplicate_rows_are_flagged_and_kept_out_of_high_total(prices_dir):
    draft = takeoff.build_draft([_line("Бетон В25 — балки", 5), _line("Бетон В25 — балки", 5)],
                                mapping=takeoff.load_map(), prices_dir=prices_dir,
                                as_of="2025-12-01")
    assert draft["totals"]["duplicate_lines"] == 1
    assert all(l["duplicate_of"] == 2 for l in draft["lines"])
    assert draft["totals"]["amount_high_rub"] == 0.0
    assert draft["totals"]["amount_duplicates_rub"] == pytest.approx(2 * 5 * 6200)


def test_takeoff_markdown_shows_doubtful_totals(prices_dir):
    draft = takeoff.build_draft([_line("Опалубка щитовая — стены", 100, unit="шт")],
                                mapping=takeoff.load_map(), prices_dir=prices_dir,
                                as_of="2025-12-01")
    text = takeoff.draft_to_markdown(draft)
    assert "не совпадает с единицей цены" in text


# ─────────────────────────── база цен ─────────────────────────────────
def test_price_rows_reject_nan_and_non_positive(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text("Код ресурса,Наименование,Ед. изм.,Цена\n"
                    "01.1,Бетон,м3,nan\n02.2,Арматура,т,-5\n03.3,Опалубка,м2,1 250\n",
                    encoding="utf-8")
    items, problems = read_rows(path)
    assert [i["code"] for i in items] == ["03.3"] and items[0]["price"] == 1250.0
    assert any("nan" in p for p in problems) and any("≤ 0" in p for p in problems)


def test_price_lookup_skips_broken_price(tmp_path):
    """Испорченная цена в снимке версии не должна дойти до сметы."""
    import json

    from pto_agent.prices import lookup

    out = tmp_path / "prices"
    out.mkdir()
    (out / "prices_v1.json").write_text(json.dumps({
        "version": "v1", "source": "тест", "price_date": "2025-09-01",
        "items": [{"code": "01.1", "name": "Бетон", "unit": "м3", "price": "nan"}]},
        ensure_ascii=False), encoding="utf-8")
    (out / "index.json").write_text(json.dumps({"schema": "pto-agent/prices@1", "versions": [
        {"version": "v1", "source": "тест", "price_date": "2025-09-01",
         "file": "prices_v1.json", "items": 1}]}, ensure_ascii=False), encoding="utf-8")
    result = lookup(code="01.1", out_dir=out)
    assert result["found"] is False


def test_demo_price_version_digest_is_stable(prices_dir):
    """Снимок версии не меняется от прогона к прогону — иначе цены «плывут»."""
    from pto_agent.prices import load_index

    versions = load_index(prices_dir)["versions"]
    assert len(versions) == 1 and versions[0]["digest"]
    assert versions[0]["price_date"] == "2025-09-01"


def test_numeric_helper_used_by_all_modules():
    """Один разбор чисел на проект: «nan» отвергается везде одинаково."""
    for module in (takeoff, kr_pdf, kr_schedules):
        assert module._num("nan") is None
        assert module._num("1 234,5") == pytest.approx(1234.5)
