# -*- coding: utf-8 -*-
"""КС-2 из сметы: бланк собирается, период и НДС не теряются, лишнего не придумывается.

Эти тесты — на конкретные дефекты, найденные при прогоне сметы 1166/Н:
  * `build_ks2` падал с TypeError, как только в шапке было заполнено хоть одно поле
    (русские ключи передавались как именованные аргументы);
  * обязательный `--period` игнорировался, и акт выходил «без периода»;
  * ставка НДС 20 превращалась в 2000 % (доля и проценты путались).
"""
import importlib.util
import json
from pathlib import Path

import pytest

from pto_agent import forms

ROOT = Path(__file__).resolve().parents[1]


def _ks2_script():
    spec = importlib.util.spec_from_file_location(
        "ks2_from_smeta", ROOT / "scripts" / "ks2_from_smeta.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PARSED = {"header": {"object": "1166/Н"},
          "totals": {"grand": 1000.0},
          "items": [
              {"n": 1, "code": "ГЭСНр67-01-004-05", "name": "Демонтаж светильников",
               "unit": "100 шт", "qty": 0.37, "kind": "work",
               "values": [3072.18, 2794.17, 1473.85, 7340.20], "section": 1},
              {"n": 2, "code": "21.1.06.09-0151", "name": "Кабель ВВГнг 3х1,5",
               "unit": "1000 м", "qty": 0.3264, "kind": "material",
               "values": [17199.48], "section": 2},
          ]}


def test_period_reaches_blank_from_cli_flag():
    """`--period` больше не игнорируется: в бланке месяц, а не пусто."""
    form = forms.build_from_files("ks2", period="2026-11",
                                  acts_path=str(ROOT / "tests" / "fixtures"
                                                / "ks2_lines.json"))
    assert form["period"] == "2026-11-01"
    assert form["period_label"] == "ноябрь 2026"


def test_vat_rate_percent_is_not_multiplied_twice():
    """20 % хранится как 0,2 и печатается как «20 %», а не «2000 %»."""
    form = forms.build_ks2([], period_from="2026-11-01", period_to="2026-11-30",
                           vat_rate=20)
    assert form["totals"]["vat_rate"] == pytest.approx(0.2)
    labels = dict(forms.totals_ru(form))
    assert labels["ставка НДС"] == "20 %"


def test_requisites_with_object_do_not_crash():
    """Раньше TypeError: «requisites() got an unexpected keyword argument 'Объект'»."""
    form = forms.build_ks2([], period_from="2026-11-01", period_to="2026-11-30",
                           requisites_dict=forms.requisites(object_name="Школа"),
                           vat_rate=20)
    assert form["requisites"]["Объект"]["value"] == "Школа"


def test_materials_go_out_of_act_with_reason():
    """Материалы не становятся строками акта: они внутри расценки."""
    lines, skipped = _ks2_script().build_lines(PARSED)
    assert len(lines) == 1
    assert lines[0]["code"] == "ГЭСНр67-01-004-05"
    assert lines[0]["price"] == pytest.approx(19838.38, abs=0.01)
    assert any("входит в расценку" in s["why"] for s in skipped)


def test_every_line_carries_the_unverified_volume_flag():
    """Объём из сметы — не подтверждение выполнения; это должно быть в каждой строке."""
    lines, _ = _ks2_script().build_lines(PARSED)
    assert lines and all("не из журнала" in r for r in lines[0]["restrictions"])


def test_bundled_lines_file_builds_act():
    payload = json.loads((ROOT / "tests" / "fixtures" / "ks2_lines.json").read_text(
        encoding="utf-8"))
    form = forms.ks2_from_summary(payload, req={"Объект": "Школа"}, vat_rate=20)
    assert form["totals"]["positions"] >= 1
    assert form["totals"]["amount_claim"] > 0
    assert form["gaps"]  # пустые реквизиты обязаны попасть в запросы, а не исчезнуть


def test_month_bounds_uses_real_last_day():
    """«2026-11-31» — несуществующая дата: последний день месяца берётся из календаря."""
    mod = _ks2_script()
    assert mod.month_bounds("2026-11") == ["2026-11-01", "2026-11-30"]
    assert mod.month_bounds("2028-02") == ["2028-02-01", "2028-02-29"]
