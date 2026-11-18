# -*- coding: utf-8 -*-
"""Тесты импортёров нормативки и расценок (круг 48).

Держат три правила:

* импортёр **не выдумывает** то, чего нет в источнике (нет цены/пункта → пропуск с причиной);
* нумерация пунктов и цитаты должны совпадать с тем, что видит человек в документе
  (вложенные пункты 5.1.1 не должны «схлопываться» в 5.1 — иначе ссылка не проверяется);
* статус документа не выдаётся за свой: `unknown` в источнике не превращается в `active`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import import_fsnb            # noqa: E402
import import_norms           # noqa: E402
import import_pue             # noqa: E402


# ─────────────────────────── пункты и вложенная нумерация ───────────────────────────
def test_nested_points_are_split_so_clause_links_can_be_checked():
    """«**5.1.1** текст» внутри пункта 5.1 — это отдельный пункт, на него ссылаются акты."""
    body = ("5.1 Общие требования к объёмно-планировочным решениям зданий\n"
            "**5.1.1** Высота помещений должна быть не менее 2,2 м.\n"
            "**5.1.2** Высота проезда — не менее 4,2 м.\n")
    points, _ = import_norms.split_points(body)
    expanded = []
    for number, text in points:
        expanded += import_norms.split_nested(number, text)
    numbers = [n for n, _ in expanded]
    assert numbers == ["5.1", "5.1.1", "5.1.2"]
    assert expanded[1][1].startswith("Высота помещений")


def test_import_does_not_invent_status():
    """Статус из источника переносится как есть: `unknown` не превращается в `active`."""
    meta = {"designation": "СП 99.99999.2099", "title": "Тест", "status": "unknown"}
    text, _ = import_norms.build_document(meta, "## 1.1\nТестовый пункт с текстом.\n",
                                          repo=Path("/tmp"), commit="0" * 40)
    assert "status: unknown" in text
    assert "source_status: unknown" in text


# ─────────────────────────── ПУЭ: колонтитулы и переносы ───────────────────────────
def test_pue_clean_page_removes_running_heads_and_joins_hyphenation():
    page = "ПУЭ108\nмероприя-\nтий по испытанию\n"
    cleaned = import_pue.clean_page(page)
    assert "ПУЭ108" not in cleaned
    assert "мероприятий" in cleaned


def test_pue_point_regex_catches_numbers_from_the_book():
    match = import_pue.POINT.match("1.7.126. Заземлители и заземляющие проводники")
    assert match and match.group(1) == "1.7.126"


# ─────────────────────────── ФЕР: имя, база, цена ───────────────────────────
def test_fer_full_name_glues_group_and_work():
    """Без заголовка группы в CSV остаётся «15 м3, группа грунтов 1» — расценку не найти."""
    work = type("W", (), {"get": staticmethod(lambda key: {"EndName": "15 м3, группа грунтов 1"}
                                              .get(key))})
    name = import_fsnb._full_name(work, "Разработка грунта в отвал экскаваторами:")
    assert name.startswith("Разработка грунта в отвал экскаваторами")
    assert name.endswith("15 м3, группа грунтов 1")


def test_prices_accept_gzip_export(tmp_path):
    """Выгрузка ФСНБ весит десятки мегабайт — в репозитории она лежит как .csv.gz."""
    import gzip
    from pto_agent.prices import read_rows
    path = tmp_path / "prices.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        handle.write("code,name,unit,price\nФЕР/01-01-001-01,Работа,1000 м3,2968.03\n")
    items, problems = read_rows(path)
    assert not problems and len(items) == 1 and items[0]["price"] == 2968.03


def test_data_files_exist_and_carry_source():
    """Импортированные данные лежат в репозитории и у них есть источник."""
    import json
    meta = json.loads((ROOT / "data" / "fsnb" / "fer2020.meta.json").read_text(encoding="utf-8"))
    assert "961/пр" in meta["source"] and meta["price_level"] == "01.01.2000"
    assert meta["written"] > 50_000
    assert (ROOT / "data" / "fsnb" / "fsbc2022.csv.gz").exists()


def test_pue_full_text_is_loaded_as_active_document():
    """Полный ПУЭ загружен и его пункты ищутся: это закрывает «нет документа по электромонтажу»."""
    from pto_agent.norms import load_norms
    clauses = load_norms(ROOT / "data" / "norms")
    pue = [c for c in clauses if c.doc.file == "pue_7_full.md"]
    assert len(pue) > 2000
    assert pue[0].doc.status == "active"
    assert any(c.clause == "1.8.37" for c in pue)
