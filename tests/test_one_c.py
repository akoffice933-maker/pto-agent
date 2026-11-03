# -*- coding: utf-8 -*-
"""Обмен с 1С / БИТ.Строительство: CSV в обе стороны.

Проверяем не «красоту файла», а контракт обмена: 1С отдаёт свои шапки и свои числа
(запятая, пробелы тысяч, cp1251) — мы обязаны это прочитать; 1С принимает `utf-8-sig` + `;` —
мы обязаны это записать.
"""
import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.integrations.one_c import (OneCError, acts_document, export_acts_csv,
                                          export_ks2_csv, import_acts_csv, import_smeta_csv,
                                          read_csv)

SUMMARY = ROOT / "out" / "summary.json"


def write_csv(path: Path, rows: list[list[str]], encoding="utf-8-sig", delimiter=";") -> Path:
    with path.open("w", encoding=encoding, newline="") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerows(rows)
    return path


@pytest.fixture()
def summary():
    if not SUMMARY.exists():
        pytest.skip("нет out/summary.json: python3 -m pto_agent run --data data/demo --out out")
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


# ───────────────────── выгрузка проверенной КС-2 в 1С ────────────────────
def test_export_ks2_has_1c_friendly_format(summary, tmp_path):
    path = tmp_path / "ks2.csv"
    res = export_ks2_csv(summary, path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                    # BOM: Excel и 1С видят кириллицу
    text = raw.decode("utf-8-sig")
    header = text.splitlines()[0].split(";")
    assert header == ["Код", "Наименование", "ЕдИзм", "КоличествоАкта", "КоличествоЖурнала",
                      "Цена", "СуммаБезНДС"]
    assert res["rows"] == len(summary["lines"])
    assert "1 000,000".replace(" ", "") in text or "1000,000" in text   # запятая, не точка


def test_export_ks2_amount_matches_claim(summary, tmp_path):
    res = export_ks2_csv(summary, tmp_path / "ks2.csv", quantity="claim")
    expected = sum((l.get("qty_claim") or 0) * (l.get("price") or 0) for l in summary["lines"])
    assert res["amount"] == pytest.approx(round(expected, 2), abs=0.01)
    assert res["amount"] == pytest.approx(summary["amount_act_safe"], abs=0.01) \
        if summary.get("amount_act_safe") is not None else True


def test_export_ks2_quantity_modes_differ(summary, tmp_path):
    claim = export_ks2_csv(summary, tmp_path / "a.csv", quantity="claim")["amount"]
    journal = export_ks2_csv(summary, tmp_path / "b.csv", quantity="journal")["amount"]
    estimate = export_ks2_csv(summary, tmp_path / "c.csv", quantity="estimate")["amount"]
    assert len({round(claim, 2), round(journal, 2), round(estimate, 2)}) >= 2


# ───────────────────────── чтение сметы из 1С ───────────────────────────
def test_import_smeta_with_1c_headers(tmp_path):
    p = write_csv(tmp_path / "smeta.csv", [
        ["Код", "Наименование работ", "ЕдИзм", "Кол-во", "Цена", "Сумма"],
        ["ФЕР 06-01-001-01", "Плита фундаментная", "м3", "1 200,50", "18 500,00", "22 209 250,00"],
        ["ФЕР 08-02-001-01", "Гидроизоляция", "м²", "1 180", "", "920 400,00"],
    ])
    res = import_smeta_csv(p)
    assert res["count"] == 2
    line = res["lines"][0]
    assert line["code"] == "ФЕР 06-01-001-01"
    assert line["estimate_qty"] == pytest.approx(1200.5)     # пробел тысяч + запятая
    assert line["price"] == pytest.approx(18500.0)
    # цена посчитана из суммы, если её не было
    assert res["lines"][1]["price"] == pytest.approx(780.0)
    assert res["lines"][1]["unit"] == "м²"


def test_import_smeta_keeps_unevaluated_position(tmp_path):
    p = write_csv(tmp_path / "smeta.csv", [
        ["Шифр", "Наименование", "Ед. изм.", "Количество", "Цена"],
        ["ФЕР 12-01-034-02", "Кровля ПВХ", "м2", "", "900"],
    ])
    res = import_smeta_csv(p)
    assert res["count"] == 1
    assert res["lines"][0]["estimate_qty"] is None            # «не оценено», а не 0
    assert any("не оценённой" in w["message"] for w in res["warnings"])


def test_import_smeta_rejects_file_without_name(tmp_path):
    p = write_csv(tmp_path / "bad.csv", [["Код", "Количество"], ["A1", "10"]])
    with pytest.raises(OneCError) as e:
        import_smeta_csv(p)
    assert "наименование" in str(e.value).lower()


# ─────────────────────── чтение реестра актов из 1С ──────────────────────
def test_import_acts_groups_by_act(tmp_path):
    p = write_csv(tmp_path / "acts.csv", [
        ["Акт", "Период", "Дата", "Статус", "Код", "Наименование", "ЕдИзм", "Количество", "Цена"],
        ["КС-2 №40", "октябрь 2026", "31.10.2026", "проверен",
         "ФЕР 06-01-001-01", "Плита", "м3", "100,5", "18 500"],
        ["КС-2 №40", "октябрь 2026", "31.10.2026", "проверен",
         "ФЕР 08-02-001-01", "Гидроизоляция", "м2", "250", "780"],
        ["КС-2 №41", "ноябрь 2026", "30.11.2026", "черновик",
         "ФЕР 12-01-034-02", "Кровля", "м2", "300", "900"],
    ])
    res = import_acts_csv(p)
    assert res["count"] == 2
    acts = {a["act_id"]: a for a in res["acts"]}
    assert acts["КС-2 №40"]["positions_count"] == 2
    assert acts["КС-2 №40"]["amount_rub"] == pytest.approx(100.5 * 18500 + 250 * 780, abs=0.01)
    assert acts["КС-2 №41"]["period"] == "ноябрь 2026"


def test_import_acts_flags_position_without_qty(tmp_path):
    p = write_csv(tmp_path / "acts.csv", [
        ["Акт", "Код", "Количество", "Цена"],
        ["КС-2 №42", "ФЕР 11-01-011-01", "", "12000"],
    ])
    res = import_acts_csv(p)
    assert res["acts"][0]["positions_count"] == 1
    assert res["acts"][0]["amount_rub"] == 0.0                 # в сумму не входит
    assert any("не оценена" in w["message"] for w in res["warnings"])


def test_acts_document_is_portfolio_compatible(tmp_path):
    p = write_csv(tmp_path / "acts.csv", [
        ["Акт", "Код", "Количество", "Цена"],
        ["КС-2 №43", "ФЕР 06-01-001-01", "10", "18500"],
    ])
    doc = acts_document(import_acts_csv(p), object_name="ЖК «Северный»")
    assert doc["object"] == "ЖК «Северный»"
    assert doc["acts"][0]["act_id"] == "КС-2 №43"

    from pto_agent.portfolio import assess_portfolio
    result = assess_portfolio(doc)
    assert len(result["acts"]) == 1                              # портфель читает импорт без правок
    assert result["acts"][0]["act_id"] == "КС-2 №43"
    assert result["totals"]["amount_rub"] == pytest.approx(185000.0, abs=0.01)


# ─────────────────────── кодировки и разделители ─────────────────────────
def test_read_csv_detects_cp1251_and_tab(tmp_path):
    p = tmp_path / "acts.csv"
    p.write_bytes("Акт\tКод\tКоличество\nКС-2 №44\tФЕР 06-01-001-01\t12\n".encode("cp1251"))
    rows, meta = read_csv(p)
    assert meta["encoding"] == "cp1251"
    assert meta["delimiter"] == "\t"
    assert rows[0]["Код"] == "ФЕР 06-01-001-01"


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(OneCError) as e:
        import_smeta_csv(tmp_path / "нет.csv")
    assert "нет файла" in str(e.value)


# ───────────────────────────── круг: туда-обратно ────────────────────────
def test_acts_roundtrip_preserves_amounts(tmp_path):
    doc = {"acts": [{"act_id": "КС-2 №35", "period": "сентябрь 2026", "act_date": "2026-09-30",
                     "status": "проверен",
                     "positions": [{"code": "ФЕР 06-01-001-01", "name": "Плита", "unit": "м3",
                                    "qty": 200, "price": 18500, "amount_rub": 3_700_000.0},
                                   {"code": "ФЕР 08-02-001-01", "name": "Гидроизоляция",
                                    "unit": "м2", "qty": 600, "price": 780,
                                    "amount_rub": 468_000.0}]}]}
    export = export_acts_csv(doc, tmp_path / "acts.csv")
    imported = import_acts_csv(tmp_path / "acts.csv")
    assert export["rows"] == 2
    assert imported["acts"][0]["amount_rub"] == pytest.approx(4_168_000.0, abs=0.01)
    assert imported["acts"][0]["positions_count"] == 2
