# -*- coding: utf-8 -*-
"""Складской контур: движения, партии, остатки и правила, которые не дают учёту врать.

Проверяем не «функция вернула список», а правила:
1. отрицательный остаток отклоняется с точным «сколько есть и сколько не хватает»;
2. отрицательный остаток можно провести только с причиной, и это попадает в журнал;
3. единица документа сверяется со справочником до всякой арифметики;
4. повторное проведение не удваивает приход;
5. FIFO: списывается раньше пришедшее, цена уходит вместе с материалом;
6. перемещение не меняет общий остаток и не попадает в М-29 ни приходом, ни расходом;
7. давальческие (003) — отдельный контур, в собственные остатки не подмешиваются;
8. инвентаризация — это движения (излишек/недостача), остаток = сумма движений;
9. закрытый период не принимает проведение;
10. М-29 из склада балансируется: остаток на начало + приход − расход = остаток на конец.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import forms, stock

DEMO_DOCS = ROOT / "data" / "demo" / "stock" / "documents_2026_10.json"
DEMO_INV = ROOT / "data" / "demo" / "stock" / "inventory_inv3.csv"
DEMO_CATALOG = ROOT / "data" / "demo" / "stock" / "catalog.json"


def catalog_state(**kwargs) -> dict:
    payload = json.loads(DEMO_CATALOG.read_text(encoding="utf-8"))
    return stock.new_state(object_name=payload["object"],
                           warehouses=payload["warehouses"], items=payload["items"],
                           opening=payload.get("opening") if kwargs.get("opening", True) else None)


def receipt(number, date, item, qty, price, warehouse="СК-1"):
    return {"kind": "receipt", "number": number, "date": date, "warehouse": warehouse,
            "lines": [{"item": item, "qty": qty, "price": price}]}


def issue(number, date, item, qty, warehouse="СК-1", **extra):
    line = {"item": item, "qty": qty, **extra}
    return {"kind": "issue", "number": number, "date": date, "warehouse": warehouse,
            "lines": [line]}


# ─────────────────────────── остатки и партии ───────────────────────────
def test_empty_state_has_no_balances():
    state = stock.new_state()
    assert stock.balances(state) == []
    assert stock.verify(state)["ok"] is True


def test_opening_balance_is_moves_too():
    state = catalog_state()
    assert sum(b["qty"] for b in stock.balances(state)) == pytest.approx(15 + 8 + 60)
    assert stock.verify(state)["ok"] is True


def test_receipt_creates_batch_and_balance():
    state = stock.new_state(warehouses=[{"code": "СК-1"}],
                            items=[{"code": "Бетон", "unit": "м3"}])
    result = stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон", 100, 5000))
    assert result["status"] == stock.STATUS_OK
    assert len(result["moves"]) == 1
    row = stock.balances(state)[0]
    assert row["qty"] == 100 and row["price"] == 5000 and row["amount"] == 500000


def test_fifo_writes_off_earlier_batch_first():
    state = stock.new_state(warehouses=[{"code": "СК-1"}],
                            items=[{"code": "Бетон", "unit": "м3"}])
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон", 100, 5000))
    stock.post_document(state, receipt("УПД №2", "2026-10-02", "Бетон", 100, 6000))
    stock.post_document(state, issue("М-11 №1", "2026-10-03", "Бетон", 150))
    batches = stock.batches(state, item="Бетон")
    assert batches[0]["batch"].startswith("УПД №1") and batches[0]["left"] == 0
    assert batches[1]["left"] == 50
    # остаток 50 по цене второй партии: списано 100 × 5000 + 50 × 6000
    assert stock.balances(state)[0]["amount"] == 300000
    assert stock.balances(state)[0]["price"] == 6000


def test_issue_document_is_split_by_batches():
    state = stock.new_state(warehouses=[{"code": "СК-1"}],
                            items=[{"code": "Бетон", "unit": "м3"}])
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон", 100, 5000))
    stock.post_document(state, receipt("УПД №2", "2026-10-02", "Бетон", 100, 6000))
    result = stock.post_document(state, issue("М-11 №1", "2026-10-03", "Бетон", 150))
    assert len(result["moves"]) == 2  # из двух партий
    assert sum(m["qty"] for m in result["moves"]) == 150


# ─────────────────────────── правила-предохранители ───────────────────────────
def test_negative_stock_is_rejected_with_exact_shortage():
    state = stock.new_state(warehouses=[{"code": "СК-1"}], items=[{"code": "Бетон", "unit": "м3"}])
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон", 10, 5000))
    result = stock.post_document(state, issue("М-11 №1", "2026-10-02", "Бетон", 25))
    assert result["status"] == stock.STATUS_REJECTED
    error = result["errors"][0]
    assert error["code"] == "NEGATIVE_STOCK" and error["short"] == 15.0
    assert any("не хватает 15" in gap["what"] for gap in result["gaps"])
    assert stock.balances(state)[0]["qty"] == 10  # остаток не тронут


def test_negative_without_reason_is_still_rejected():
    state = stock.new_state(warehouses=[{"code": "СК-1"}], items=[{"code": "Бетон", "unit": "м3"}])
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон", 10, 5000))
    result = stock.post_document(state, issue("М-11 №1", "2026-10-02", "Бетон", 25),
                                 allow_negative=True)
    assert result["status"] == stock.STATUS_REJECTED
    assert result["errors"][0]["code"] == "NEGATIVE_NEEDS_REASON"


def test_negative_with_reason_goes_to_audit():
    state = stock.new_state(warehouses=[{"code": "СК-1"}], items=[{"code": "Бетон", "unit": "м3"}])
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон", 10, 5000))
    result = stock.post_document(state, issue("М-11 №1", "2026-10-02", "Бетон", 25),
                                 allow_negative=True, reason="аварийная заливка")
    assert result["status"] == stock.STATUS_OK
    assert any("отрицательный остаток" in entry["what"] for entry in state["audit"])
    assert stock.balances(state)[0]["qty"] == -15
    assert stock.verify(state)["ok"] is False  # минус виден контролю, а не спрятан


def test_unit_mismatch_blocks_before_arithmetic():
    state = stock.new_state(warehouses=[{"code": "СК-1"}],
                            items=[{"code": "Арматура", "unit": "т"}])
    doc = {"kind": "receipt", "number": "УПД №1", "date": "2026-10-01", "warehouse": "СК-1",
           "lines": [{"item": "Арматура", "qty": 100, "price": 68000, "unit": "кг"}]}
    result = stock.post_document(state, doc)
    assert result["status"] == stock.STATUS_REJECTED
    assert result["errors"][0]["code"] == "UNIT_MISMATCH"
    assert stock.balances(state) == []


def test_unknown_item_or_warehouse_is_reported():
    state = stock.new_state(warehouses=[{"code": "СК-1"}], items=[{"code": "Бетон", "unit": "м3"}])
    result = stock.post_document(state, receipt("УПД №1", "2026-10-01", "Кирпич", 10, 100))
    assert result["status"] == stock.STATUS_REJECTED
    assert any("Кирпич" in error["what"] for error in result["errors"])


def test_receipt_without_price_is_rejected():
    state = stock.new_state(warehouses=[{"code": "СК-1"}], items=[{"code": "Бетон", "unit": "м3"}])
    doc = {"kind": "receipt", "number": "УПД №1", "date": "2026-10-01", "warehouse": "СК-1",
           "lines": [{"item": "Бетон", "qty": 10}]}
    result = stock.post_document(state, doc)
    assert result["errors"][0]["code"] == "NO_PRICE"


def test_double_posting_is_idempotent():
    state = stock.new_state(warehouses=[{"code": "СК-1"}], items=[{"code": "Бетон", "unit": "м3"}])
    doc = receipt("УПД №1", "2026-10-01", "Бетон", 10, 5000)
    stock.post_document(state, doc)
    second = stock.post_document(state, doc)
    assert second["status"] == stock.STATUS_DUPLICATE
    assert stock.balances(state)[0]["qty"] == 10  # не 20


def test_closed_period_rejects_posting():
    state = stock.new_state(warehouses=[{"code": "СК-1"}], items=[{"code": "Бетон", "unit": "м3"}])
    result = stock.post_document(state, receipt("УПД №1", "2026-09-15", "Бетон", 10, 5000),
                                 as_of_closed="2026-09-30")
    assert result["errors"][0]["code"] == "CLOSED_PERIOD"


def test_issue_without_object_is_a_gap():
    state = stock.new_state(warehouses=[{"code": "СК-1"}], items=[{"code": "Бетон", "unit": "м3"}])
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон", 10, 5000))
    result = stock.post_document(state, issue("М-11 №1", "2026-10-02", "Бетон", 5))
    assert any("объект списания" in gap["what"] for gap in result["gaps"])


# ─────────────────────────── перемещение и давальческие ───────────────────────────
def test_transfer_keeps_total_and_moves_price():
    state = catalog_state(opening=False)
    state["warehouses"].append({"code": "ПР-2", "title": "Приобъектный"})
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Труба ПВХ 110", 100, 310))
    doc = {"kind": "transfer", "number": "М-15 №1", "date": "2026-10-02", "warehouse": "СК-1",
           "warehouse_to": "ПР-2", "lines": [{"item": "Труба ПВХ 110", "qty": 40}]}
    result = stock.post_document(state, doc)
    assert result["status"] == stock.STATUS_OK
    rows = {row["warehouse"]: row for row in stock.balances(state)}
    assert rows["СК-1"]["qty"] == 60 and rows["ПР-2"]["qty"] == 40
    assert rows["ПР-2"]["price"] == 310          # цена переехала вместе с материалом
    total = sum(row["amount"] for row in stock.balances(state))
    assert total == 31000                        # 100 м по 310 ₽, ничего не потеряно


def test_transfer_needs_target_warehouse():
    state = catalog_state(opening=False)
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Труба ПВХ 110", 10, 310))
    doc = {"kind": "transfer", "number": "М-15 №1", "date": "2026-10-02", "warehouse": "СК-1",
           "lines": [{"item": "Труба ПВХ 110", "qty": 5}]}
    result = stock.post_document(state, doc)
    assert result["errors"][0]["code"] == "NO_TARGET"


def test_daval_is_separate_contour_and_not_in_own_balances():
    state = catalog_state(opening=False)
    doc = {"kind": "daval_receipt", "number": "М-35 №1", "date": "2026-10-01", "warehouse": "ПР-2",
           "lines": [{"item": "Труба ПВХ 110", "qty": 300}]}
    stock.post_document(state, doc)
    assert stock.balances(state) == []                     # собственных нет
    daval = stock.balances(state, daval=True)
    assert daval[0]["qty"] == 300 and daval[0]["amount"] == 0


def test_daval_cannot_be_written_off_as_own():
    state = catalog_state(opening=False)
    stock.post_document(state, {"kind": "daval_receipt", "number": "М-35 №1", "date": "2026-10-01",
                                "warehouse": "ПР-2",
                                "lines": [{"item": "Труба ПВХ 110", "qty": 300}]})
    result = stock.post_document(state, issue("М-11 №1", "2026-10-02", "Труба ПВХ 110", 10,
                                              warehouse="ПР-2"))
    assert result["status"] == stock.STATUS_REJECTED  # давальческое не списывается как своё


def test_daval_return_reduces_daval_balance():
    state = catalog_state(opening=False)
    stock.post_document(state, {"kind": "daval_receipt", "number": "М-35 №1", "date": "2026-10-01",
                                "warehouse": "ПР-2",
                                "lines": [{"item": "Труба ПВХ 110", "qty": 300}]})
    stock.post_document(state, {"kind": "daval_return", "number": "М-35 №2", "date": "2026-10-05",
                                "warehouse": "ПР-2",
                                "lines": [{"item": "Труба ПВХ 110", "qty": 120}]})
    assert stock.balances(state, daval=True)[0]["qty"] == 180


# ─────────────────────────── инвентаризация ───────────────────────────
def test_inventory_makes_surplus_and_shortage_as_moves():
    state = catalog_state(opening=False)
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон B25", 100, 6200))
    doc = {"kind": "inventory", "number": "ИНВ-3 №1", "date": "2026-10-31", "warehouse": "СК-1",
           "lines": [{"item": "Бетон B25", "qty": 95, "price": 6200}]}
    result = stock.post_document(state, doc)
    assert result["status"] == stock.STATUS_OK
    assert stock.balances(state)[0]["qty"] == 95
    assert any("недостача" in w for w in result["warnings"])
    assert stock.verify(state)["ok"] is True  # недостача списана из партии, а не «из воздуха»


def test_inventory_surplus_creates_price_tagged_batch():
    state = catalog_state(opening=False)
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон B25", 100, 6200))
    stock.post_document(state, {"kind": "inventory", "number": "ИНВ-3 №1", "date": "2026-10-31",
                                "warehouse": "СК-1",
                                "lines": [{"item": "Бетон B25", "qty": 104, "price": 6200}]})
    batch = [b for b in stock.batches(state, item="Бетон B25") if "излишек" in b["batch"]]
    assert batch and batch[0]["left"] == 4


def test_inventory_without_difference_makes_no_moves():
    state = catalog_state(opening=False)
    stock.post_document(state, receipt("УПД №1", "2026-10-01", "Бетон B25", 100, 6200))
    result = stock.post_document(state, {"kind": "inventory", "number": "ИНВ-3 №1",
                                         "date": "2026-10-31", "warehouse": "СК-1",
                                         "lines": [{"item": "Бетон B25", "qty": 100,
                                                    "price": 6200}]})
    assert result["moves"] == [] and any("совпал" in w for w in result["warnings"])


def test_inventory_file_reader_reports_broken_rows(tmp_path):
    path = tmp_path / "inv.csv"
    path.write_text("код;количество\nБетон B25;36\n;10\nАрматура A500;много\n",
                    encoding="utf-8")
    rows, problems = stock.read_inventory_file(path)
    assert len(rows) == 1 and rows[0]["item"] == "Бетон B25"
    assert len(problems) == 2


# ─────────────────────────── М-29 из склада ───────────────────────────
def test_m29_from_stock_balances():
    state = stock.load_demo() if hasattr(stock, "load_demo") else stock.demo_state()
    rows = stock.m29_rows(state, period="2026-10")
    form = forms.build_m29(rows["purchases"], rows["writeoffs"], period="2026-10",
                           opening=rows["opening"])
    assert form["totals"]["balance_defined"] is True
    qty = {row["cells"][0]: row for row in form["rows"]}
    # бетон: 15 на начало + 230 приход − 210 расход = 35 на конец
    bеton = next(row for name, row in qty.items() if name.startswith("Бетон"))
    assert bеton["cells"][2] == "15" and bеton["cells"][7] == "35"


def test_m29_excludes_internal_transfers():
    state = stock.load_demo() if hasattr(stock, "load_demo") else stock.demo_state()
    rows = stock.m29_rows(state, period="2026-10")
    pipe_receipt = sum(float(row["количество"]) for row in rows["purchases"]
                       if row["код"] == "Труба ПВХ 110")
    assert pipe_receipt == 200  # приход 200, а не 320: перемещение не приход


def test_m29_expense_matches_writeoffs():
    state = stock.load_demo() if hasattr(stock, "load_demo") else stock.demo_state()
    rows = stock.m29_rows(state, period="2026-10")
    expense = sum(float(row["количество"]) for row in rows["writeoffs"]
                  if row["код"] == "Бетон B25")
    assert expense == 210


# ─────────────────────────── демо-данные и общий контроль ───────────────────────────
@pytest.mark.skipif(not DEMO_DOCS.exists(), reason="демо-документы склада отсутствуют")
def test_demo_documents_post_and_verify():
    payload = json.loads(DEMO_DOCS.read_text(encoding="utf-8"))
    state = catalog_state()
    result = stock.post_many(state, payload["documents"])
    assert result["posted"] >= 7
    # последнее требование (5 т арматуры при остатке 2,5 т) отклонено — это правильно
    assert result["rejected"] == 1
    assert any(error["code"] == "NEGATIVE_STOCK" for error in result["errors"])
    assert stock.verify(state)["ok"] is True


@pytest.mark.skipif(not DEMO_INV.exists(), reason="демо ИНВ-3 отсутствует")
def test_demo_inventory_reconciles_stock():
    state = stock.demo_state()
    rows, problems = stock.read_inventory_file(DEMO_INV)
    assert not problems and len(rows) == 3
    stock.post_document(state, {"kind": "inventory", "number": "ИНВ-3 от 2026-10-31",
                                "date": "2026-10-31", "warehouse": "СК-1", "lines": rows})
    assert stock.verify(state)["ok"] is True
    balance = {row["item"]: row["qty"] for row in stock.balances(state, warehouse="СК-1")}
    assert balance["Бетон B25"] == 36 and balance["Керамзит"] == 30


def test_state_roundtrip(tmp_path):
    state = stock.demo_state()
    path = stock.save(state, tmp_path / "state.json")
    again = stock.load(path)
    assert len(again["moves"]) == len(state["moves"])
    assert stock.verify(again)["ok"] is True


def test_wrong_schema_is_an_error(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema": "other@0"}), encoding="utf-8")
    with pytest.raises(ValueError):
        stock.load(path)


def test_unknown_document_kind_is_rejected():
    state = catalog_state(opening=False)
    result = stock.post_document(state, {"kind": "пересорт", "number": "X",
                                         "date": "2026-10-01", "warehouse": "СК-1",
                                         "lines": []})
    assert result["errors"][0]["code"] == "UNKNOWN_KIND"


def test_markdown_shows_negative_flag_and_contours():
    state = stock.demo_state()
    text = stock.to_markdown(state)
    assert "Остатки" in text and "контроль" in text
