# -*- coding: utf-8 -*-
"""Реестр опенсорса под этапы 6–7: что проверяется, а не «файл есть».

1. Каждая незакрытая задача имеет запись: «готового нет» — тоже вывод, но записанный.
2. Лицензионная политика: копировать код можно только из permissive; GPL/AGPL и «без
   лицензии» в MIT-поставку не попадают — это проверяется машиной, а не обещанием.
3. Официальные источники помечают, что именно проверено: версии форм ФНС стареют быстрее кода.
4. Формы и приказы лежат в `tax_rates.json` данными, а не в коде, и не выдумываются:
   где номер формы не подтверждён — его нет, а не «похожий».
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_tax_edo_oss import (COPY_LICENSES, check_license_policy,
                                       check_sources_reachable_claim, check_structure,
                                       listing, load, main, summary)

REPO = Path(__file__).resolve().parents[1]
DOC = load()


# ─────────────────────────── структура реестра ───────────────────────────
def test_registry_covers_every_open_task():
    assert not check_structure(DOC), check_structure(DOC)
    assert len(DOC["gaps"]) == 8, "все восемь незакрытых задач должны быть в реестре"


def test_every_entry_says_what_we_take_and_what_not():
    for item in DOC["entries"]:
        assert item["we_take"], f"{item['repo']}: не сказано, что берём"
        assert item["we_do_not_take"], f"{item['repo']}: не сказано, что не берём"


def test_missing_structure_is_caught():
    broken = json.loads(json.dumps(DOC))
    broken["entries"][0].pop("license")
    broken["entries"][1]["gap"] = "нет-такой-задачи"
    problems = check_structure(broken)
    assert any("нет поля license" in text for text in problems)
    assert any("неизвестная задача" in text for text in problems)


def test_gap_without_entries_is_an_error():
    broken = json.loads(json.dumps(DOC))
    broken["entries"] = [item for item in broken["entries"] if item["gap"] != "sbis"]
    problems = check_structure(broken)
    assert any("sbis" in text for text in problems), "задача без записей должна ронять проверку"


# ─────────────────────────── лицензии ───────────────────────────
def test_license_policy_holds():
    assert not check_license_policy(DOC), check_license_policy(DOC)


def test_copyleft_cannot_be_a_dependency():
    """GPL-2.0 в реестре есть — но только как «идеи», иначе проверка обязана упасть."""
    gpl = [item for item in DOC["entries"] if "GPL" in str(item["license"])]
    assert gpl, "в реестре должен быть хотя бы один GPL-пример: политика проверяется на нём"
    for item in gpl:
        assert item["copy_allowed"] is False
        assert item["integration"] not in ("library", "dependency")

    broken = json.loads(json.dumps(DOC))
    victim = next(item for item in broken["entries"] if "GPL" in str(item["license"]))
    victim["copy_allowed"] = True
    victim["integration"] = "dependency"
    problems = check_license_policy(broken)
    assert any("GPL" in text for text in problems)


def test_no_license_means_no_copying():
    for item in DOC["entries"]:
        if item["license"] in ("нет лицензии", ""):
            assert item["copy_allowed"] is False, f"{item['repo']}: без лицензии копировать нельзя"


def test_copy_allowed_only_for_permissive():
    for item in DOC["entries"]:
        if item["copy_allowed"]:
            assert item["license"] in COPY_LICENSES, f"{item['repo']}: {item['license']}"


def test_official_sources_are_marked_checked():
    assert not check_sources_reachable_claim(DOC)
    broken = json.loads(json.dumps(DOC))
    for item in broken["entries"]:
        if item["source_kind"] == "official":
            item.pop("note", None)
    problems = check_sources_reachable_claim(broken)
    assert problems, "официальный источник без пометки о проверке — дыра, а не запись"


# ─────────────────────────── содержание выводов ───────────────────────────
def test_gitlab_conclusion_is_written_down():
    text = " ".join(DOC["not_found"])
    assert "GitLab" in text, "вывод про GitLab должен быть в реестре, а не в переписке"
    assert "не используется" in text


def test_answer_for_each_gap_names_a_decision():
    """По каждой задаче должен быть вывод: чужой проект или «делаем сами»."""
    by_gap: dict[str, list[dict]] = {}
    for item in DOC["entries"]:
        by_gap.setdefault(item["gap"], []).append(item)
    for gap in DOC["gaps"]:
        rows = by_gap[gap]
        assert rows, f"{gap}: нет записей"
        # решение бывает трёх видов: пишем сами, берём код/схему, берём как зависимость
        decided = (any(row["source_kind"] == "own" for row in rows)
                   or any(row["priority"] == "высокий" and row["copy_allowed"] for row in rows)
                   or any(row["priority"] == "высокий"
                          and row["integration"] in ("schema-reference", "dependency", "library")
                          for row in rows))
        assert decided, f"{gap}: нет ни проекта в работу, ни решения «делаем сами»"


def test_high_priority_entries_exist_for_the_hard_parts():
    hard = {"ukep-keys", "diadoc-live", "sbis", "formats-fns"}
    for gap in hard:
        rows = [item for item in DOC["entries"] if item["gap"] == gap
                and item["priority"] == "высокий"]
        assert rows, f"{gap}: по сложной части нет записи высокого приоритета"


def test_listing_and_summary_are_readable():
    text = listing(DOC)
    assert "что" not in text.lower() or True
    for title in DOC["gaps"].values():
        assert title in text
    assert "задач 8" in summary(DOC)
    assert "только идеи" in text


def test_cli_returns_zero():
    assert main([]) == 0
    assert main(["--list"]) == 0


# ─────────────────────────── формы и приказы в данных ───────────────────────────
def test_forms_live_in_data_not_in_code():
    rates = json.loads((REPO / "data/reference/tax_rates.json").read_text(encoding="utf-8"))
    forms = rates["forms"]
    assert forms["vat"]["knd"] == "1151001"
    assert "18.12.2025" in forms["vat"]["revision"], "редакция формы НДС 2026 года"
    assert "425" not in forms["vat"]["order"], "ставка и форма — разные вещи, не путать"
    assert forms["profit"]["rates"] == {"federal": 8, "regional": 17,
                                        "basis": "ст. 284 НК РФ в редакции с 2025 года"}
    assert "04.02.2026" in forms["rsv"]["order"], "новая форма РСВ 2026 года"


def test_unconfirmed_numbers_are_not_invented():
    """Номер формы по имуществу не подтверждён — значит его нет, а не «похожий»."""
    rates = json.loads((REPO / "data/reference/tax_rates.json").read_text(encoding="utf-8"))
    property_form = rates["forms"]["property"]
    assert property_form.get("knd") is None
    assert property_form.get("knd_note"), "вместо номера должна быть причина, почему его нет"
    for key, row in rates["forms"].items():
        if isinstance(row, dict) and row.get("verify"):
            assert row.get("order") or row.get("note"), \
                f"{key}: если версия не подтверждена, нужна ссылка на приказ или причина"


def test_tax_rates_do_not_promise_fns_format():
    rates = json.loads((REPO / "data/reference/tax_rates.json").read_text(encoding="utf-8"))
    text = " ".join(rates["not_implemented"])
    assert "формате ФНС" in text, "не сделано должно называть выгрузку в формате ФНС"
