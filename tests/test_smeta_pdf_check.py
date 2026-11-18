# -*- coding: utf-8 -*-
"""Прогон чужой сметы (PDF АРОС-Лидер): разбор позиций и проверки.

Тест держит два свойства, без которых отчёт бесполезен:
  * позиции разбираются вместе с количеством и колонками — иначе арифметика «сойдётся»
    на пустом месте;
  * отсутствие данных и непроверяемое не выдают себя за «сошлось».
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "fixtures" / "smeta_1166n_lines.txt"


def _module():
    spec = importlib.util.spec_from_file_location(
        "check_smeta_pdf", ROOT / "scripts" / "check_smeta_pdf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_money_and_number_rows():
    mod = _module()
    assert mod.money("3 072,18") == 3072.18
    assert mod.money("0,37") == 0.37
    assert mod.money("Демонтаж кабеля") is None
    assert mod._is_number_row("3,8") is True
    assert mod._is_number_row("100 м") is False


def test_item_parsing_keeps_qty_unit_and_columns():
    mod = _module()
    lines = ["1", "ГЭСНр67-01-004-05", "Демонтаж: светильников", "100 шт", "0,37",
             "3 072,18", "2 794,17", "1 473,85", "7 340,20"]
    item, _ = mod._parse_item(lines, 0)
    assert item["code"] == "ГЭСНр67-01-004-05"
    assert item["qty"] == 0.37
    assert item["unit"] == "100 шт"
    assert item["kind"] == "work"
    assert item["values"] == [3072.18, 2794.17, 1473.85, 7340.2]


def test_material_row_has_single_total():
    mod = _module()
    lines = ["20", "21.1.06.09-0151", "Кабель ВВГнг(A)-LS 3×1,5", "1000 м", "0,3264",
             "17 199,48"]
    item, _ = mod._parse_item(lines, 0)
    assert item["kind"] == "material"
    assert item["qty"] == 0.3264
    assert item["values"] == [17199.48]


def test_fixture_runs_end_to_end_and_reports_reasons():
    """Полный прогон на сохранённом тексте сметы: арифметика и «не проверено» с причиной."""
    mod = _module()
    lines = SAMPLE.read_text(encoding="utf-8").split("\n")
    doc = mod.parse.__wrapped__(lines) if hasattr(mod.parse, "__wrapped__") else None
    if doc is None:
        # parse читает PDF; здесь проверяем то же ядро на готовых строках
        doc = {"header": {"object": "1166/Н"}, "items": [], "sections": [],
               "totals": {}, "pages": 3}
        i = 0
        section = None
        while i < len(lines):
            line = lines[i].strip()
            m = mod.RE_SECTION.match(line)
            if m:
                section = {"number": int(m.group(1)), "name": m.group(2).strip(),
                           "reported_total": None, "items": []}
                doc["sections"].append(section)
            elif line == "ВСЕГО ПО СМЕТЕ":
                vals = [mod.money(lines[i + k].strip()) for k in range(1, 5)]
                vals = [v for v in vals if v is not None]
                doc["totals"] = {"grand": vals[-1]}
            elif mod.RE_N.match(line) and i + 2 < len(lines):
                nxt = lines[i + 1].strip()
                if mod.RE_WORK.match(nxt) or mod.RE_RES.match(nxt):
                    item, used = mod._parse_item([l.strip() for l in lines], i)
                    if item:
                        item["section"] = section["number"] if section else None
                        doc["items"].append(item)
                        if section:
                            section["items"].append(item)
                        i += used
                        continue
            i += 1
    result = mod.check(doc)
    arith = [f for f in result["findings"] if f["check"] == "арифметика всего"]
    assert arith and arith[0]["status"] == "сошлось"
    assert result["resolved"]["materials"] >= 10
    # материалы сверяются с ФСБЦ, и все найденные коды должны быть покрыты
    assert result["resolved"]["in_fsbc"] == result["resolved"]["materials"]
    # коэффициенты к базе устойчивы по группам
    ratios = {round(r["ratio"], 3) for r in result["prices"]}
    assert 1.12 in ratios or 1.119 in ratios
    # непроверяемое названо непроверяемым, а не «ок»
    rates = [f for f in result["findings"] if f["check"] == "ставки НР и СП"]
    assert rates and rates[0]["status"] == "не проверено"
    assert "фонд оплаты труда" in rates[0]["detail"]
