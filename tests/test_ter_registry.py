# -*- coding: utf-8 -*-
"""Реестр ТЕР: есть реквизиты сборников — и честно нет самих расценок."""
import csv
import gzip
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "import_ter_registry", ROOT / "scripts" / "import_ter_registry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_region_of_matches_exact_subject_only():
    mod = _module()
    subjects = ["Республика Крым", "Кемеровская область - Кузбасс", "Москва"]
    assert mod.region_of("ТСЭМ 81-01-2001. Республика Крым", subjects) == "Республика Крым"
    assert mod.region_of("ТССЦпг 81-01-2001. Кемеровская область.", subjects) \
        == "Кемеровская область - Кузбасс"
    # «Республика Северная» — не регион: догадок нет
    assert mod.region_of("ТЕРп 81-05-2001. Республика Северная", subjects) == ""


def test_bundled_registry_has_records_and_reasons():
    data_dir = ROOT / "data" / "ter"
    with gzip.open(data_dir / "ter_registry.csv.gz", "rt", encoding="utf-8") as f:
        import csv
        rows = list(csv.DictReader(f))
    meta = json.loads((data_dir / "ter_registry.meta.json").read_text(encoding="utf-8"))
    assert len(rows) > 800
    assert meta["records"] == len(rows)
    # ключ честности: в мете прямо написано, чего в реестре нет
    assert "Таблиц расценок ТЕР в API нет" in meta["not_included_reason"]
    assert meta["url_check"]["with_url"] > 0
    # каждая запись — с приказом или регистрационным номером
    assert all(r["order"] or r["reg_number"] for r in rows)


def test_ter_module_finds_region_and_denies_prices():
    from pto_agent import ter
    data = ter.registry(ROOT / "data" / "ter")
    assert data["status"] == "ok"
    assert data["prices_available"] is False
    result = ter.for_region("Краснодарский край", data)
    assert result["status"] == "found"
    assert result["records"] >= 1
    # расценок нет — модуль обязан сказать это, а не отдать пустой список
    assert result["prices_available"] is False
    assert "Таблиц расценок ТЕР в API нет" in result["reason_prices"]
    assert all(item["order"] or item["reg_number"] for item in result["items"])


def test_ter_module_unknown_region_lists_what_it_has():
    from pto_agent import ter
    data = ter.registry(ROOT / "data" / "ter")
    result = ter.for_region("Чукотский автономный округ", data)
    assert result["status"] == "not_found"
    assert result["prices_available"] is False
    assert result["regions_in_registry"]
