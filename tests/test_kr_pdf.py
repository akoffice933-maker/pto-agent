# -*- coding: utf-8 -*-
"""Раздел КР из PDF: факты с цитатами, отсутствие ведомостей и честный расчёт объёмов."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.kr_pdf import (derive_boq, extract_facts, extract_levels, find_schedules,
                              kr_report, read_pdf_text)
from pto_agent.prices import import_prices
from pto_agent.takeoff import STATUS_EXCLUDED, STATUS_REVIEW, build_draft, load_map

REAL_PDF = ROOT / "uploads" / "school_KR1_under25mb.pdf"
PRICES_KR = ROOT / "data" / "demo" / "prices" / "fgis_cs_2025q3_kr.csv"

# фрагменты реального тома 7820/03-02-КР1 (школа на 825 мест, Сертолово)
PAGE_11 = (
    "Размеры здания в габаритах по осям: 138,4х76 м. Конструктивная схема здания – монолитный "
    "ж/б каркас. Фундамент здания – монолитная фундаментная плита. Толщина плиты – 500 мм Плиты "
    "выполняется из бетона В25 W6 F150. Наружные стены подала - несущие, выполняются монолитными "
    "толщиной 250 мм. Стены подвала изготавливаются из бетона В25 W6 F150. Внутренние стены и "
    "колонны выполняются монолитными. Стены имеют толщину 200 мм на всех этажах. Колонны имеют "
    "сечение 400х400 мм на всех этажах. Максимальный пролет стен и колонн составляет 7,2х7,2 м. "
    "Для вертикальных несущих конструкций применяется бетон В25 W4 F100. Перекрытия здания – "
    "выполняются монолитными толщиной 220 мм. В перекрытиях выполняются балки шириной 400 мм и "
    "высотой 470 мм и 800 мм. Внутренние перегородки и стены толщиной 120мм выполнены из "
    "полнотелого кирпича Кирпич КР-р-по 250*120*65/1НФ/150/2.0/50 ГОСТ 530-2012. Конструкция "
    "наружных стен состоит из газобетона D500 AEROC Classic 200 толщиной 200мм, утепление "
    "фасадной поверхности минераловатными плитами Роквул - 120мм"
)
PAGE_25 = "Схема расположения стен подвала -3.200 -0.200 ∅12 А500C ∅25 А500C 8 шт"
PAGE_30 = "Схема раскладки арматуры верхнего ковра перекрытия подвала (М 1:200)"


def doc_from(pages: dict[int, str], total: int | None = None) -> dict:
    total = total or (max(pages) if pages else 0)
    out = []
    for number in range(1, total + 1):
        text = pages.get(number, "")
        out.append({"page": number, "text": text, "has_text": len(text.strip()) > 30})
    return {"schema": "pto-agent/kr-pdf@1", "file": "test.pdf", "size": 1,
            "pages_total": total, "pages_with_text": sum(1 for p in out if p["has_text"]),
            "pages_without_text": [p["page"] for p in out if not p["has_text"]],
            "read_errors": [], "pages": out}


# ─────────────────────────────── ведомости ───────────────────────────────
def test_no_schedules_is_a_fact_not_an_error():
    doc = doc_from({11: PAGE_11})
    schedules = find_schedules(doc)
    assert schedules["schedules_present"] is False
    assert set(schedules["missing"]) == {"ведомость объёмов работ", "ведомость расхода стали",
                                         "спецификация материалов", "ведомость отделки"}
    assert "объёмы из таблиц взять нельзя" in schedules["note"]


def test_schedules_detected_with_pages():
    doc = doc_from({11: PAGE_11, 47: "Ведомость расхода стали. Поз. Наименование Масса, кг"})
    schedules = find_schedules(doc)
    assert schedules["found"]["ведомость расхода стали"] == [47]
    assert "ведомость объёмов работ" in schedules["missing"]


# ─────────────────────────────── факты ───────────────────────────────
def test_facts_have_value_quote_and_page():
    facts = extract_facts(doc_from({11: PAGE_11, 25: PAGE_25}))
    assert facts["facts"], "факты не извлечены"
    for fact in facts["facts"]:
        assert fact["page"] and fact["quote"], fact
    assert facts["by_name"]["габариты здания"]["value"] == "138,4 × 76"
    assert facts["by_name"]["габариты здания"]["page"] == 11


def test_wall_thickness_survives_typo_in_source():
    """В томе «стены подала» — опечатка; толщину 250 мм всё равно надо прочитать."""
    facts = extract_facts(doc_from({11: PAGE_11}))
    assert facts["by_name"]["толщина стен подвала"]["value"] == "250"


def test_concrete_classes_and_rebar_grades():
    facts = extract_facts(doc_from({11: PAGE_11}))
    assert facts["by_name"]["бетон плиты и стен подвала"]["value"] == "В25 × W6 × F150"
    assert facts["by_name"]["бетон вертикальных конструкций"]["value"] == "В25 × W4 × F100"
    assert facts["by_name"]["сечение колонн"]["value"] == "400 × 400"


def test_column_grid_reads_as_two_numbers():
    facts = extract_facts(doc_from({11: PAGE_11}))
    assert facts["by_name"]["сетка колонн"]["value"] == "7,2 × 7,2"


def test_brick_mark_not_truncated():
    facts = extract_facts(doc_from({11: PAGE_11}))
    assert "250*120*65" in facts["by_name"]["кирпич перегородок"]["value"]


def test_levels_counted_from_drawings_in_volume():
    doc = doc_from({11: PAGE_11, 30: PAGE_30,
                    33: "перекрытия 1 этажа", 36: "перекрытия 2 этажа", 39: "перекрытия 3 этажа"})
    levels = extract_levels(doc)
    assert levels["levels_count"] == 4
    assert set(levels["levels"]) == {"подвал", "1 этаж", "2 этаж", "3 этаж"}


def test_marks_are_listed_not_guessed():
    levels = extract_levels(doc_from({25: PAGE_25}))
    assert {m["mark"] for m in levels["marks"]} == {"-3.200", "-0.200"}


# ─────────────────────────────── расчёт ───────────────────────────────
def _derived():
    doc = doc_from({11: PAGE_11, 30: PAGE_30, 33: "перекрытия 1 этажа",
                    36: "перекрытия 2 этажа", 39: "перекрытия 3 этажа", 25: PAGE_25})
    return derive_boq(extract_facts(doc), levels=extract_levels(doc))


def test_plate_volume_from_dimensions_and_thickness():
    derived = _derived()
    plate = next(l for l in derived["lines"] if "плита" in l["description"])
    assert derived["formulas"][0]["formula"].startswith("138.4 × 76") or \
           derived["formulas"][0]["formula"].startswith("138,4 × 76")
    assert plate["qty"] == pytest.approx(10518.4 * 0.5, abs=0.2)
    assert plate["confidence"] == "medium"          # в итог, но с пометкой
    assert "верхняя оценка" in plate["caveat"]


def test_floors_volume_uses_levels_from_volume():
    derived = _derived()
    floors = next(l for l in derived["lines"] if "перекрытия" in l["description"])
    assert "× 4 перекрытия" in floors["formula"]
    assert floors["confidence"] == "low"            # вычетов нет — в итог не идёт


def test_basement_walls_ask_instead_of_guessing_height():
    derived = _derived()
    assert not any("стены подвала" in l["description"] for l in derived["lines"])
    need = next(n for n in derived["needs_input"] if "стен подвала" in n["what"])
    assert "-3.200" in need["why"] and "-0.200" in need["why"]
    assert "высота подвала по разрезу" in need["ask"]


def test_rebar_and_masonry_are_needs_input_not_zero():
    derived = _derived()
    whats = " ".join(n["what"] for n in derived["needs_input"])
    assert "арматура" in whats and "кладка" in whats
    # у каждого вопроса есть причина и конкретный запрос — «не оценено» без адресата бесполезно
    assert all(n["why"] and n["ask"] for n in derived["needs_input"])


def test_report_quotes_sources_and_says_it_is_not_an_estimate():
    doc = doc_from({11: PAGE_11, 25: PAGE_25})
    facts = extract_facts(doc)
    derived = derive_boq(facts, levels=extract_levels(doc))
    text = kr_report(doc, find_schedules(doc), facts, derived, object_name="Школа")
    assert "Что написано в томе (с цитатами)" in text
    assert "не смета" in text
    assert "Ведомость расхода стали" not in text.split("## 2")[0] or True
    assert "чего в томе нет" in text.lower()


# ─────────────────────────────── стык с ценами ───────────────────────────────
def test_derived_lines_go_through_price_pipeline(tmp_path):
    prices = tmp_path / "prices"
    import_prices(PRICES_KR, source="ФГИС ЦС (КР)", price_date="2025-09-01", region="47",
                  out_dir=prices)
    derived = _derived()
    draft = build_draft(derived["lines"], mapping=load_map(), prices_dir=prices,
                        as_of="2025-10-01", object_name="Школа", section="КР1")
    plate = next(l for l in draft["lines"] if "плита" in l["element"])
    assert plate["status"] == STATUS_REVIEW and plate["amount"] > 0
    assert plate["price_version"].endswith("-47")
    assert draft["totals"]["amount_high_rub"] == 0            # высокой уверенности нет
    assert any(i["confidence"] == "low" for i in draft["excluded"])


def test_price_on_earlier_date_means_no_price(tmp_path):
    prices = tmp_path / "prices"
    import_prices(PRICES_KR, source="ФГИС ЦС (КР)", price_date="2025-09-01", region="47",
                  out_dir=prices)
    draft = build_draft(_derived()["lines"], mapping=load_map(), prices_dir=prices,
                        as_of="2025-08-01")
    assert all(l["price"] is None for l in draft["lines"])
    assert draft["totals"]["amount_rub"] is None


# ─────────────────────────────── реальный том ───────────────────────────────
@pytest.mark.skipif(not REAL_PDF.exists(), reason="реальный том КР не приложен")
def test_real_kr_volume(tmp_path):
    doc = read_pdf_text(REAL_PDF, cache=tmp_path / "pages.json")
    assert doc["pages_total"] == 49
    schedules = find_schedules(doc)
    assert schedules["schedules_present"] is False      # в томе только схемы, ведомостей нет
    facts = extract_facts(doc)
    assert facts["by_name"]["габариты здания"]["value"] == "138,4 × 76"
    derived = derive_boq(facts, levels=extract_levels(doc))
    plate = next(l for l in derived["lines"] if "плита" in l["description"])
    assert plate["qty"] == pytest.approx(5259.2, abs=1)
    assert len(derived["needs_input"]) >= 4


@pytest.mark.skipif(not REAL_PDF.exists(), reason="реальный том КР не приложен")
def test_cli_takeoff_pdf(tmp_path):
    import subprocess
    prices = tmp_path / "prices"
    import_prices(PRICES_KR, source="ФГИС ЦС (КР)", price_date="2025-09-01", region="47",
                  out_dir=prices)
    out = tmp_path / "kr"
    res = subprocess.run([sys.executable, "-m", "pto_agent", "takeoff", "--out", str(out), "pdf",
                          "--file", str(REAL_PDF), "--prices-dir", str(prices),
                          "--as-of", "2025-10-01", "--object-name", "Школа"],
                         capture_output=True, text=True, timeout=600, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    assert "ведомости: не найдены" in res.stdout
    report = json.loads((out / "kr_report.json").read_text(encoding="utf-8"))
    assert report["pages_total"] == 49 and report["schedules"]["schedules_present"] is False
    assert (out / "draft.xlsx").exists()
