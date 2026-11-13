# -*- coding: utf-8 -*-
"""Профили разбора PDF: ЛОЖНЫЙ ФАКТ этажности и профиль вместо КР-хардкода.

Что проверяем (по мотивам реальных ошибок):
1. «пом. 414, 4 этаж» — это адрес кабинета, а не этажность. Раньше правило этажности
   вытаскивало из этой строки факт «этажность 4».
2. «этажность блоков — 4», «4-этажное здание» — это этажность, факт нужен.
3. Профиль sks не задаёт вопрос про арматуру, зато задаёт про кабельный журнал.
4. Неизвестный профиль — ошибка, а не тихий откат к КР.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.kr_pdf import derive_boq, extract_facts, find_schedules
from pto_agent.pdf_profiles import (ProfileError, fact_rules, get_profile, list_profiles,
                                    load_profiles, schedule_markers)


def doc_from(pages: dict, total: int | None = None) -> dict:
    return {"file": "test.pdf", "pages_total": total or max(pages),
            "pages": [{"page": n, "text": text} for n, text in sorted(pages.items())],
            "pages_with_text": len(pages), "pages_without_text": [], "read_errors": []}


def storey(pages: dict) -> dict | None:
    facts = extract_facts(doc_from(pages), rules=fact_rules(get_profile("kr")))
    return next((f for f in facts["facts"] if f["name"] == "этажность здания"), None)


# ─────────────────────────── факты: ложное срабатывание ───────────────────────────
def test_room_number_is_not_a_storey_count():
    """«пом. 414, 4 этаж» — адрес помещения; факта этажности быть не должно."""
    assert storey({2: "Кабинет 414 ГК, пом. 414, 4 этаж. Типоразмер 42U напольный"}) is None


def test_room_number_before_real_storey_keeps_the_real_one():
    """Отсев действует на своё совпадение, а не на всё правило."""
    fact = storey({3: "пом. 414, 4 этаж. Этажность здания — 3 этажа"})
    assert fact is not None and fact["value"] == "3"


@pytest.mark.parametrize("text,value", [
    ("Этажность блоков — 4", 4),
    ("Здание 4-этажное", 4),
    ("этажность здания: 2", 2),
])
def test_real_storey_count_is_a_fact(text, value):
    fact = storey({5: text})
    assert fact is not None, text
    assert fact["value"] == str(value)


def test_every_fact_has_quote_and_page():
    fact = storey({5: "Этажность здания: 2"})
    assert fact["page"] == 5 and fact["quote"]


# ─────────────────────────── профили: структура ───────────────────────────
def test_profiles_file_has_default_and_three_types():
    payload = load_profiles()
    assert payload["default"] == "kr"
    assert {"kr", "sks", "ovik"} <= set(payload["profiles"])


def test_list_profiles_marks_unvalidated():
    rows = {row["name"]: row for row in list_profiles()}
    assert rows["kr"]["validated_on"]
    assert rows["sks"]["validated_on"]
    assert rows["ovik"]["validated_on"] is None


def test_unknown_profile_is_an_error_not_a_fallback():
    with pytest.raises(ProfileError):
        get_profile("arh")


def test_broken_profile_file_is_reported(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"profiles": {"x": {"title": "X", "schedules": {},
                                                  "facts": [{"name": "n", "pattern": "("}]}}}))
    with pytest.raises(ProfileError):
        load_profiles(bad)


# ─────────────────────────── профили: поведение ───────────────────────────
def test_sks_profile_asks_for_cable_journal_not_rebar():
    profile = get_profile("sks")
    derived = derive_boq({"facts": [], "by_name": {}}, profile=profile)
    asks = " ".join(n["what"] for n in derived["needs_input"])
    assert "кабельный журнал" in asks
    assert "арматура" not in asks


def test_sks_profile_does_not_derive_concrete():
    profile = get_profile("sks")
    gab = {"name": "габариты здания", "value": "138,4×76", "unit": "м", "page": 1,
           "quote": "138,4х76"}
    plate = {"name": "толщина фундаментной плиты", "value": "500", "unit": "мм", "page": 1,
             "quote": "Толщина плиты – 500 мм"}
    facts = {"facts": [gab, plate],
             "by_name": {"габариты здания": gab, "толщина фундаментной плиты": plate}}
    derived = derive_boq(facts, profile=profile)
    assert derived["lines"] == []
    assert any("бетон" in n["what"] for n in derived["needs_input"])


def test_kr_profile_still_derives_concrete():
    profile = get_profile("kr")
    gab = {"name": "габариты здания", "value": "138,4×76", "unit": "м", "page": 11,
           "quote": "138,4х76"}
    plate = {"name": "толщина фундаментной плиты", "value": "500", "unit": "мм", "page": 11,
             "quote": "Толщина плиты – 500 мм"}
    facts = {"facts": [gab, plate], "by_name": {"габариты здания": gab,
                                                "толщина фундаментной плиты": plate}}
    derived = derive_boq(facts, profile=profile)
    assert derived["lines"] and derived["lines"][0]["qty"] > 0


def test_profile_markers_change_what_schedules_look_for():
    doc = doc_from({1: "Кабельный журнал. Участок, марка, длина"})
    kr = find_schedules(doc, markers=schedule_markers(get_profile("kr")))
    sks = find_schedules(doc, markers=schedule_markers(get_profile("sks")))
    assert "кабельный журнал" not in kr["found"]
    assert sks["found"]["кабельный журнал"] == [1]


def test_ovik_profile_is_marked_as_draft():
    profile = get_profile("ovik")
    assert profile["validated"] is False
    asks = " ".join(a if isinstance(a, str) else a.get("what", "")
                    for a in profile["always_ask"]).lower()
    assert "воздуховод" in asks
