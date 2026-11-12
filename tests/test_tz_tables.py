# -*- coding: utf-8 -*-
"""Таблицы ТЗ → объёмы: разбор таблиц, перечней и прозы, проверки цифр, стык с ценами."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.prices import import_prices
from pto_agent.takeoff import STATUS_EXCLUDED, build_draft, load_map, read_boq
from pto_agent.tz_tables import (cable_context, doc_unit, extract, parse_bullet_quantities,
                                 parse_cabinet_table, parse_distribution, parse_route_table,
                                 read_lines, report_to_markdown, to_boq_rows, write_boq_csv)

PRICES = ROOT / "data" / "demo" / "prices_skc_adler.csv"
MAP_TZ = ROOT / "data" / "reference" / "takeoff_map_tz.json"
REAL_TZ = os.environ.get("PTO_TZ_PDF", "")
HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
no_pypdf = pytest.mark.skipif(not HAS_PYPDF, reason="нет pypdf (pip install -r requirements-pto.txt)")

CABINET_BLOCK = [
    "ШТ. Кабинет 414 ГК, пом. 414, 4 этаж 42U напольный 1",
    "ШТ. Отм.21.",
    "Лестница",
    "ГК, отм. +21.0,",
    "лестничная клетка",
    "33U напольный 1",
    "ШТ.КРУЭ КРУЭ, коридор, 3",
    "этаж",
    "42U напольный 1",
    "ШТ.ЦТП  ЦТП, помещение настенный 1",
]
OPT_ROUTES = [
    "Маршрут Длина, м Кол-во линий",
    "Каб. 414 → ШТ. Лестница 80 1",
    "Каб. 414 → ЦТП 580 2",
    "Итого 1 240 м. 3",
]
POWER_ROUTES = [
    "Маршрут Марка / сечение Длина, м Кол-во линий",
    "ГК Секция 1 ПТ-10,",
    "шкаф 7, яч. 8А → Каб.",
    "414",
    "ВВГЭнг(А)-LS 3×6 190 1",
    "Итого:  190 1",
]

TZ_PAGES = [
    "Техническое задание на монтаж СКС\n"
    "1. Общие сведения\n"
    "Заказчик: ПАО «ОГК-2»\n"
    "5.2 Монтаж новых телекоммуникационных шкафов\n"
    "Установить и закрепить 2 новых шкафа (количество уточнено по проекту):\n"
    "Шкаф Место установки Типоразмер Кол-во\n"
    "ШТ. Кабинет 414 ГК, пом. 414, 4 этаж 42U напольный 1\n"
    "ШТ. Отм.21.\n"
    "Лестница\n"
    "ГК, отм. +21.0,\n"
    "лестничная клетка\n"
    "33U напольный 1\n"
    "Остальные шкафы (АБК, КПП1) – существующие, подлежат дооснащению (п. 5.6.5).\n",

    "5.3 Монтаж кабельнесущих систем\n"
    "· Гофрированная ПВХ-труба ∅32 мм – 150 м.\n"
    "· Проволочный лоток 100×50×3000 мм – 1 шт. (склад/ЦТП).\n"
    "5.4 Земляные работы\n"
    "· Разработка траншеи длиной 60 м, ориентировочный объём выемки 45,8 м³ (ширина ~0,8 м,\n"
    "глубина ~1,0 м – уточнить по стройгенплану).\n"
    "· Укладка на дно двух ПНД-труб ∅32 мм для оптического кабеля.\n"
    "· Планировка, вывоз излишков грунта (при необходимости).\n",

    "5.5 Прокладка кабелей\n"
    "Оптические кабели (ТОД-нг(А)-HF-8У-8кН, 8 волокон):\n"
    "Маршрут Длина, м Кол-во линий\n"
    "Каб. 414 → ШТ. Лестница 80 1\n"
    "Каб. 414 → ЦТП 580 1\n"
    "Итого 660 м. 2\n"
    "\n"
    "Силовые кабели:\n"
    "Маршрут Марка / сечение Длина, м Кол-во линий\n"
    "ГК Секция 1 ПТ-10,\n"
    "шкаф 7, яч. 8А → Каб.\n"
    "414\n"
    "ВВГЭнг(А)-LS 3×6 190 1\n"
    "Итого:  190 1\n"
    "\n"
    "Распределение по прокладке:\n"
    "Кабель Общая\n"
    "длинна\n"
    "Лоток\n"
    "существующ\n"
    "ий\n"
    "Опт. 660 500 160\n"
    "Силов\n"
    "ые\n"
    "190 190 0\n",

    "5.6.1 Шкаф «Кабинет 414» (42U, новый):\n"
    "· Оптические кроссы 19\" 24×SC – 6 шт. \n"
    "· Коммутаторы доступа Eltex MES2324P – 2 шт. (исправлено с 5 до 6 по проекту)\n"
    "· Патч-корды: F/UTP Cat.5e 1м – 48 шт., LC-LC 1м – 2 шт., LC-SC 2м – 20 шт.\n"
    "5.6.5 Дооснащение существующих шкафов:\n"
    "· АБК (00CYA04, 42U):\n"
    "  · Коммутаторы Eltex MES2348P – 4 шт.\n"
    "  · Переподключение ~192 существующих патч-кордов.\n"
    "  · Доукомплектация двух существующих кроссов (каждый 24 порта).\n"
    "5.7 Разделка и подключение кабелей\n"
    "· Ввод и зачистка оптических кабелей в кроссах (2 концов).\n"
    "· Подключение силовых кабелей в щитах ПТ-10 (секции 1 и 2) – 2 конца.\n"
    "5.8 Сварка оптических волокон\n"
    "· Сварка 32 одномодовых волокон (пигтейл – магистральное волокно).\n"
    "5.9 Измерения и тестирование\n"
    "· Рефлектометрические измерения на 2 оптических участках (с двух сторон).\n"
    "5.10 Пусконаладочные работы\n"
    "· Маркировка кабелей, кроссов, портов, оборудования.\n"
    "6 Исполнительная документация\n"
    "В состав Исполнительной документации должны входить:\n"
    "- пояснительная записка;\n"
    "- структурная схема СКС;\n"
    "- протоколы тестирования медных линий (при наличии);\n"
    "- протоколы измерения оптических параметров смонтированных оптических\n"
    "кабелей;\n"
    "- комплект протоколов и актов предварительных испытаний.\n",
]


def make_doc(pages=TZ_PAGES):
    return {"file": "tz_test.pdf", "pages_total": len(pages), "pages_with_text": len(pages),
            "pages": [{"page": i + 1, "text": text, "has_text": True}
                      for i, text in enumerate(pages)]}


def block(lines):
    return [{"page": 1, "line": i, "text": text} for i, text in enumerate(lines)]


@pytest.fixture(scope="module")
def report():
    return extract(make_doc())


@pytest.fixture()
def prices_dir(tmp_path):
    out = tmp_path / "prices"
    import_prices(PRICES, source="ДЕМО (тест)", price_date="2026-11-01", region="23",
                  out_dir=out)
    return out


# ─────────────────────────── таблица шкафов ───────────────────────────
def test_cabinet_table_splits_rows_wrapped_in_text_layer():
    rows, problems = parse_cabinet_table(block(CABINET_BLOCK))
    assert not problems
    assert [r["qty"] for r in rows] == [1, 1, 1, 1]


def test_cabinet_table_keeps_place_and_type():
    rows, _ = parse_cabinet_table(block(CABINET_BLOCK))
    assert rows[0]["description"].endswith("Кабинет 414")
    assert rows[0]["detail"]["место"].startswith("ГК, пом. 414")
    assert rows[0]["detail"]["типоразмер"] == "42U напольный"
    assert rows[3]["detail"]["типоразмер"] == "настенный"
    assert rows[2]["description"].endswith("КРУЭ")


def test_cabinet_table_reports_missing_qty_instead_of_zero():
    rows, problems = parse_cabinet_table(block(["ШТ. Тест ГК, пом. 1 42U напольный"]))
    assert rows and rows[0]["qty"] is None
    assert rows[0]["confidence"] == "low"
    assert problems and "количество" in problems[0]


def test_cabinet_table_reports_text_before_first_row():
    _, problems = parse_cabinet_table(block([
        "Какой-то текст перед таблицей, который не является строкой шкафа вовсе",
        "ШТ. Кабинет 1 ГК, пом. 1 42U напольный 1"]))
    assert any("не похожий на строку таблицы" in p for p in problems)


# ─────────────────────────── таблица маршрутов ───────────────────────────
def test_route_table_single_line_rows_multiply_length_by_lines():
    rows, totals, problems = parse_route_table(block(OPT_ROUTES), mark="ТОД", is_opt=True)
    assert not problems
    assert [r["qty"] for r in rows] == [80, 1160]       # 580 м × 2 линии
    assert rows[1]["formula"] == "580 м × 2 лин. = 1160 м"
    assert totals and totals[0]["numbers"][0] == 1240


def test_route_table_multiline_power_route_glues_route_and_mark():
    rows, _, problems = parse_route_table(block(POWER_ROUTES), mark="ВВГЭ", is_opt=False)
    assert not problems and len(rows) == 1
    assert rows[0]["detail"]["марка"].startswith("ВВГЭнг")
    assert "шкаф 7, яч. 8А → Каб. 414" in rows[0]["detail"]["маршрут"]
    assert rows[0]["qty"] == 190


def test_route_table_unparsed_tail_is_reported_not_dropped():
    _, _, problems = parse_route_table(block(["Маршрут Длина, м", "Каб. 414 → ЦТП без чисел"]),
                                       mark="ТОД", is_opt=True)
    assert problems and "не разобраны строки" in problems[0]


def test_cable_context_takes_mark_from_subtitle():
    lines = [{"page": 2, "line": 0, "text": "Оптические кабели (ТОД-нг(А)-HF-8У-8кН, 8 волокон):"},
             {"page": 2, "line": 1, "text": "Маршрут Длина, м Кол-во линий"}]
    is_opt, mark = cable_context(lines, 1)
    assert is_opt and mark.startswith("ТОД")


# ─────────────────────────── перечни «– N ед.» ───────────────────────────
def test_bullet_quantities_split_enumeration_and_keep_group():
    items = parse_bullet_quantities(
        "· Патч-корды: F/UTP Cat.5e 1м – 48 шт., LC-LC 1м – 2 шт., LC-SC 2м – 20 шт.")
    assert [i["qty"] for i in items] == [48, 2, 20]
    assert items[1]["description"] == "Патч-корды: LC-LC 1м"
    assert items[2]["description"] == "Патч-корды: LC-SC 2м"


def test_bullet_quantities_group_of_breakers():
    items = parse_bullet_quantities(
        "· Автоматические выключатели: 2P 40A – 2 шт., 2P 10A – 2 шт.")
    assert [i["description"] for i in items] == ["Автоматические выключатели: 2P 40A",
                                                 "Автоматические выключатели: 2P 10A"]


def test_bullet_quantities_normalizes_units():
    items = parse_bullet_quantities("· Труба – 150 м. ")
    assert items[0]["unit"] == "м" and items[0]["qty"] == 150


def test_bullet_quantities_ignores_items_without_number():
    assert parse_bullet_quantities("· Маркировка кабелей, кроссов, портов") == []


# ─────────────────────────── документ целиком ───────────────────────────
def test_extract_finds_all_tables(report):
    assert report["tables_found"] == {"cabinet": 1, "routes": 2, "distribution": 1}
    assert report["unparsed"] == []


def test_extract_cabinet_count_check(report):
    check = next(c for c in report["checks"] if "новых шкаф" in c["name"])
    assert check["result"] == "сходится" and "2" in check["formula"]


def test_extract_route_totals_match(report):
    route_checks = [c for c in report["checks"] if "итог таблицы маршрутов" in c["name"]]
    assert route_checks and all(c["result"] == "сходится" for c in route_checks)


def test_extract_distribution_mismatch_is_detected(report):
    """500 + 160 = 660 при общей длине 660 — сходится; подменяем на 500 + 100 и ждём «расходится»."""
    broken = make_doc([TZ_PAGES[2].replace("Опт. 660 500 160", "Опт. 660 500 100")])
    result = extract(broken)
    check = next(c for c in result["checks"] if "способов прокладки" in c["name"])
    assert check["result"] == "расходится"
    assert "500 + 100 = 600" in check["formula"] and "660" in check["formula"]


def test_extract_amendment_contradiction_detected(report):
    check = next(c for c in report["checks"] if "исправлено" in c["name"])
    assert check["result"] == "расходится"
    assert "5" in check["formula"] and "6" in check["formula"]


def test_extract_welding_cross_check(report):
    check = next(c for c in report["checks"] if "сварка волокон" in c["name"].lower())
    assert check["result"] == "сходится"
    assert "2 лин. × 8 волокон × 2 = 32" in check["formula"]


def test_extract_never_invents_zero_volume(report):
    """Позиции без количества — в «не оценено» с причиной, а не строкой с 0."""
    assert all(l["qty"] != 0 for l in report["lines"])
    markings = [c for c in report["not_estimated"] if "Маркировка" in c["description"]]
    assert markings and "не нормирован" in markings[0]["why"]
    assert markings[0]["source"].startswith("стр.")


def test_extract_conditional_item_is_not_estimated(report):
    med = [l for l in report["lines"] if "медных линий" in l["description"]]
    assert med and med[0]["confidence"] == "low" and med[0]["qty"] is None


def test_extract_ground_volume_and_arithmetic_note(report):
    trench = next(l for l in report["lines"] if "объём выемки" in l["description"])
    assert trench["qty"] == 45.8 and trench["confidence"] == "medium"
    assert "45,8" in trench["formula"] and "48" in trench["formula"].replace(",", ".")
    pipes = next(l for l in report["lines"] if "ПНД-труб" in l["description"]
                 and l["unit"] == "м")
    assert pipes["qty"] == 120 and "2 трубы" in pipes["formula"]


def test_extract_underrated_reconnection_stays_out_of_money(report):
    assert any("Переподключение" in c["description"] and "оценочное" in c["why"]
               for c in report["not_estimated"])


def test_extract_documents_get_units_by_type():
    assert doc_unit("пояснительная записка") == "книга"
    assert doc_unit("структурная схема СКС") == "лист"
    assert doc_unit("протоколы измерения") == "протокол"
    assert doc_unit("комплект протоколов и актов") == "компл"
    assert doc_unit("спецификация оборудования") == "док"


def test_extract_document_continuation_is_joined(report):
    item = next(l for l in report["lines"] if "оптических параметров" in l["description"])
    assert item["description"].endswith("оптических кабелей")


def test_extract_scan_notes_are_kept(report):
    """У каждой строки есть источник и пункт ТЗ — иначе строки нет."""
    for line in report["lines"]:
        assert line["source"].startswith("стр.")
        assert line["clause"]


# ─────────────────────────── стык с ценами ───────────────────────────
def test_boq_csv_roundtrip_through_takeoff_reader(report, tmp_path):
    path = write_boq_csv(report, tmp_path / "boq.csv")
    lines, problems = read_boq(path)
    # строка «при наличии» уходит без количества — ридер честно помечает её, а не читает как 0
    assert all("нет количества" in p for p in problems), problems
    assert len(lines) == len(report["lines"])
    no_qty = [l for l in lines if l["qty"] is None]
    assert len(no_qty) == sum(1 for l in report["lines"] if l["qty"] is None)
    assert lines[0]["qty"] == report["lines"][0]["qty"]


def test_pipeline_prices_every_parsed_line(report, prices_dir):
    draft = build_draft(to_boq_rows(report), mapping=load_map(MAP_TZ),
                        prices_dir=prices_dir, as_of="2026-12-01",
                        object_name="Тест", section="ТЗ")
    assert draft["totals"]["no_price_lines"] == 0
    assert draft["totals"]["wrong_unit_lines"] == 0
    assert draft["totals"]["amount_rub"] > 0
    assert draft["totals"]["amount_high_rub"] <= draft["totals"]["amount_rub"]


def test_pipeline_keeps_conditional_document_out_of_total(report, prices_dir):
    draft = build_draft(to_boq_rows(report), mapping=load_map(MAP_TZ),
                        prices_dir=prices_dir, as_of="2026-12-01")
    excluded = [l for l in draft["excluded"] if "медных линий" in l["element"]]
    assert excluded and excluded[0]["status"] == STATUS_EXCLUDED
    assert excluded[0]["amount"] is None


def test_report_markdown_lists_checks_and_gaps(report):
    text = report_to_markdown(report)
    assert "## Проверки цифр ТЗ" in text and "## Не оценено" in text
    assert "расходится" in text


def test_map_tz_covers_every_rule_with_price():
    mapping = json.loads(MAP_TZ.read_text(encoding="utf-8"))
    codes = {line.split(",")[0] for line in PRICES.read_text(encoding="utf-8").splitlines()[1:]
             if line.strip()}
    missing = [r["code"] for r in mapping["rules"] if r["code"] not in codes]
    assert not missing, f"в правилах есть коды без цены: {missing}"


# ─────────────────────────── CLI ───────────────────────────
@no_pypdf
def test_cli_tz_no_draft(tmp_path):
    if not REAL_TZ or not Path(REAL_TZ).exists():
        pytest.skip("нет реального PDF ТЗ (PTO_TZ_PDF)")
    result = subprocess.run(
        [sys.executable, "-m", "pto_agent", "takeoff", "--out", str(tmp_path), "tz",
         "--file", REAL_TZ, "--no-draft"],
        capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert "объёмов:" in result.stdout
    assert (tmp_path / "boq.csv").exists() and (tmp_path / "tz.json").exists()


# ─────────────────────────── реальный документ ───────────────────────────
@no_pypdf
@pytest.mark.skipif(not (REAL_TZ and Path(REAL_TZ).exists()),
                    reason="реальный PDF ТЗ не передан (PTO_TZ_PDF)")
def test_real_tz_document_volumes():
    from pto_agent.tz_tables import extract_file
    real = extract_file(REAL_TZ)
    assert real["pages_total"] >= 8
    assert real["unparsed"] == []
    lengths = sum(l["qty"] for l in real["lines"] if "Прокладка оптического" in l["description"])
    assert lengths == 8270
    assert any(c["result"] == "расходится" for c in real["checks"])
