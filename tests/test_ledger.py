# -*- coding: utf-8 -*-
"""Ядро учёта: журнал, правила, ОСВ. Проверяем правила, а не «функция что-то вернула».

1. Дт ≠ Кт — проводки нет, с точной разницей в отказе;
2. аналитика обязательна: счёт без объекта/контрагента не закрывается;
3. журнал неизменяем: подмена суммы ломается на хеш-цепочке;
4. сторно — единственный способ исправить; дважды сторнировать нельзя;
5. НДС берётся из документа; нет ставки — нет проводки на 19, а не «20 % по умолчанию»;
6. приход: Дт 10.01 (без НДС) + Дт 19.03 = Кт 60.01 (с НДС), до копейки;
7. списание: себестоимость партий по FIFO, а не цена из требования;
8. перемещение не меняет сальдо 10.01 — только оборот;
9. давальческие — забалансовый 003, количество, в баланс не подмешиваются;
10. сальдо 10.01 равно остатку склада в деньгах (по складу и номенклатуре);
11. закрытый период не принимает проводок, но принимает сторно в открытом;
12. выручка — только по подписанной КС-2, «проверен» выручки не даёт;
13. ОСВ сходится на каждом шаге: Дт = Кт, и это проверяется, а не предполагается.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import ledger, stock

DEMO_DOCS = ROOT / "data" / "demo" / "stock" / "documents_2026_10.json"
DEMO_CATALOG = ROOT / "data" / "demo" / "stock" / "catalog.json"


# ─────────────────────────── помощники ───────────────────────────
def chart() -> dict:
    return ledger.load_chart()


def rules() -> dict:
    return ledger.load_rules()


def catalog_state(**kwargs) -> dict:
    payload = json.loads(DEMO_CATALOG.read_text(encoding="utf-8"))
    return stock.new_state(object_name=payload["object"],
                           warehouses=payload["warehouses"], items=payload["items"],
                           opening=payload.get("opening") if kwargs.get("opening", True) else None)


def receipt(number="УПД №1", date="2026-10-01", item="Бетон B25", qty=10, price=6200,
            warehouse="СК-1", **extra):
    return {"kind": "receipt", "number": number, "date": date, "warehouse": warehouse,
            "lines": [{"item": item, "qty": qty, "price": price}], **extra}


def issue(number="М-11 №1", date="2026-10-05", item="Бетон B25", qty=5, warehouse="СК-1",
          object_name="корпус 2, плита"):
    line = {"item": item, "qty": qty}
    if object_name:
        line["object"] = object_name
    return {"kind": "issue", "number": number, "date": date, "warehouse": warehouse,
            "lines": [line]}


SUPPLIER = {"counterparty": "ООО «БетонТранс»", "vat": {"rate": 20, "included": False}}


def meta(*documents) -> dict:
    return {"documents": list(documents)}


def posted_stock(*documents, opening=None) -> dict:
    state = catalog_state(opening=opening if opening is not None else False)
    stock.post_many(state, [dict(doc) for doc in documents])
    return state


def lines_of(state: dict, index: int = -1) -> dict:
    entry = state["entries"][index]
    out: dict[str, float] = {}
    for line in entry["lines"]:
        out[f"{line['side']} {line['account']}"] = out.get(f"{line['side']} {line['account']}",
                                                          0) + line["amount"]
    return out


# ─────────────────────────── план счетов и правила ───────────────────────────
def test_chart_loads_and_is_consistent():
    data = chart()
    assert len(data["accounts"]) >= 30
    assert ledger.check_chart(data) == []
    codes = {row["code"] for row in data["accounts"]}
    for needed in ("10.01", "19.03", "20.01", "60.01", "62.01", "90.01", "68.02", "94", "003"):
        assert needed in codes


def test_chart_marks_off_balance_and_service_accounts():
    index = ledger.accounts_index(chart())
    assert index["003"]["off_balance"] is True
    assert index["000"]["service"] is True
    assert index["10.01"]["off_balance"] is False


def test_chart_says_where_subaccounts_come_from():
    index = ledger.accounts_index(chart())
    assert index["10.01"]["subaccount_source"] == "94н"
    assert index["20.01"]["subaccount_source"] == "организация"


def test_rules_have_source_and_ask_for_missing_data():
    data = rules()
    assert len(data["rules"]) >= 10
    for rule in data["rules"]:
        assert rule["source"], rule["id"]
        assert rule["date_from"], rule["id"]


def test_rule_for_picks_latest_edition_by_date():
    data = rules()
    assert ledger.rule_for(data, "receipt", "2026-10-01")["id"] == "stock.receipt"
    assert ledger.rule_for(data, "нет.такого", "2026-10-01") is None
    # правило с более поздней датой действия не применяется к прошлым операциям
    edited = {"rules": [{"id": "старое", "event": "receipt", "date_from": "2000-01-01"},
                        {"id": "новое", "event": "receipt", "date_from": "2027-01-01"}]}
    assert ledger.rule_for(edited, "receipt", "2026-10-01")["id"] == "старое"
    assert ledger.rule_for(edited, "receipt", "2027-02-01")["id"] == "новое"


# ─────────────────────────── двойная запись ───────────────────────────
def test_unbalanced_entry_is_rejected_with_exact_difference():
    state = ledger.new_state()
    result = ledger.post_entry(state, event="test", date="2026-10-01",
                               doc={"number": "Т-1"}, rule="test",
                               lines=[{"account": "10.01", "side": "Дт", "amount": 100,
                                       "analytics": {"склад": "СК-1", "номенклатура": "Бетон B25"}},
                                      {"account": "60.01", "side": "Кт", "amount": 90,
                                       "analytics": {"контрагент": "ООО «А»"}}])
    assert result["status"] == ledger.STATUS_REJECTED
    assert result["errors"][0]["code"] == "UNBALANCED"
    assert "10,00" in result["errors"][0]["why"]
    assert state["entries"] == []


def test_entry_without_analytics_is_rejected_with_request():
    state = ledger.new_state()
    result = ledger.post_entry(state, event="test", date="2026-10-01",
                               doc={"number": "Т-2"},
                               lines=[{"account": "20.01", "side": "Дт", "amount": 100},
                                      {"account": "10.01", "side": "Кт", "amount": 100,
                                       "analytics": {"склад": "СК-1", "номенклатура": "Бетон B25"}}])
    assert result["status"] == ledger.STATUS_REJECTED
    assert result["errors"][0]["code"] == "ANALYTICS_MISSING"
    assert any("объект" in gap["what"] for gap in result["gaps"])


def test_account_outside_chart_is_rejected():
    state = ledger.new_state()
    result = ledger.post_entry(state, event="test", date="2026-10-01", doc={"number": "Т-3"},
                               lines=[{"account": "77", "side": "Дт", "amount": 1},
                                      {"account": "60.01", "side": "Кт", "amount": 1,
                                       "analytics": {"контрагент": "ООО «А»"}}])
    assert result["status"] == ledger.STATUS_REJECTED
    assert result["errors"][0]["code"] == "NO_ACCOUNT"


def test_duplicate_posting_does_not_double_the_journal():
    state = ledger.new_state()
    line = [{"account": "10.01", "side": "Дт", "amount": 120,
             "analytics": {"склад": "СК-1", "номенклатура": "Бетон B25"}},
            {"account": "60.01", "side": "Кт", "amount": 120,
             "analytics": {"контрагент": "ООО «А»"}}]
    first = ledger.post_entry(state, event="receipt", date="2026-10-01",
                              doc={"number": "УПД №9"}, lines=line, key="dup-key")
    second = ledger.post_entry(state, event="receipt", date="2026-10-01",
                               doc={"number": "УПД №9"}, lines=line, key="dup-key")
    assert first["status"] == ledger.STATUS_OK
    assert second["status"] == ledger.STATUS_DUPLICATE
    assert len(state["entries"]) == 1


# ─────────────────────────── неизменяемость и сторно ───────────────────────────
def test_journal_tampering_breaks_hash_chain():
    state = ledger.new_state()
    ledger.post_entry(state, event="receipt", date="2026-10-01", doc={"number": "УПД №1"},
                      lines=[{"account": "10.01", "side": "Дт", "amount": 100,
                              "analytics": {"склад": "СК-1", "номенклатура": "Бетон B25"}},
                             {"account": "60.01", "side": "Кт", "amount": 100,
                              "analytics": {"контрагент": "ООО «А»"}}])
    assert ledger.verify(state)["ok"] is True
    state["entries"][0]["lines"][0]["amount"] = 999.99
    summary = ledger.verify(state)
    assert summary["ok"] is False
    assert any(problem["code"] == "HASH" for problem in summary["problems"])


def test_storno_returns_balance_to_zero():
    stock_state = posted_stock(receipt(**SUPPLIER))
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER)))
    entry_id = state["entries"][-1]["id"]
    before = ledger.balance_as_of(state, "10.01", "2026-10-31")
    result = ledger.storno(state, entry_id, date="2026-10-15", reason="накладная отменена")
    assert result["status"] == ledger.STATUS_OK
    after = ledger.balance_as_of(state, "10.01", "2026-10-31")
    assert all(value == 0 for value in after.values())
    assert before != after


def test_storno_requires_reason():
    stock_state = posted_stock(receipt(**SUPPLIER))
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER)))
    result = ledger.storno(state, state["entries"][-1]["id"], date="2026-10-15", reason=" ")
    assert result["status"] == ledger.STATUS_REJECTED
    assert result["errors"][0]["code"] == "NO_REASON"


def test_storno_twice_is_refused():
    stock_state = posted_stock(receipt(**SUPPLIER))
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER)))
    entry_id = state["entries"][-1]["id"]
    ledger.storno(state, entry_id, date="2026-10-15", reason="ошибка")
    again = ledger.storno(state, entry_id, date="2026-10-16", reason="ещё раз")
    assert again["status"] == ledger.STATUS_DUPLICATE


# ─────────────────────────── НДС и суммы ───────────────────────────
def test_vat_included_split_is_exact_to_kopeck():
    net, vat, memo = ledger.split_vat(1426000, 20, True)
    assert net + vat == ledger.kopeck(1426000)
    assert net == ledger.kopeck(1188333.33)
    assert "в том числе" in memo


def test_vat_on_top_is_added_to_debt():
    net, vat, _ = ledger.split_vat(1000, 20, False)
    assert (net, vat) == (ledger.kopeck(1000), ledger.kopeck(200))


def test_vat_without_rate_is_zero_and_says_so():
    net, vat, memo = ledger.split_vat(1000, None, True)
    assert (net, vat) == (ledger.kopeck(1000), ledger.kopeck(0))
    assert "не указана" in memo


def test_rounding_of_odd_amount_stays_at_kopeck():
    net, vat, _ = ledger.split_vat(0.03, 20, True)
    assert net + vat == ledger.kopeck(0.03)


# ─────────────────────────── приход ───────────────────────────
def test_receipt_posts_materials_vat_and_debt():
    stock_state = posted_stock(receipt(**SUPPLIER))
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER)))
    assert result["rejected"] == 0
    lines = lines_of(state)
    assert lines["Дт 10.01"] == pytest.approx(62000.0)      # 10 × 6 200 без НДС
    assert lines["Дт 19.03"] == pytest.approx(12400.0)      # 20 %
    assert lines["Кт 60.01"] == pytest.approx(74400.0)      # с НДС
    assert ledger.verify(state)["ok"] is True


def test_receipt_without_supplier_is_rejected_not_guessed():
    doc = receipt()
    stock_state = posted_stock(doc)
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state, documents=meta(doc))
    assert result["rejected"] == 1
    assert result["errors"][0]["code"] == "NO_COUNTERPARTY"
    assert result["gaps"][0]["ask"].startswith("поставщик")


def test_receipt_without_vat_rate_books_materials_and_asks_for_vat():
    doc = receipt(counterparty="ООО «БетонТранс»")  # без ставки
    stock_state = posted_stock(doc)
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state, documents=meta(doc))
    lines = lines_of(state)
    assert "Дт 19.03" not in lines
    assert lines["Дт 10.01"] == lines["Кт 60.01"]
    assert any("НДС" in gap["what"] for gap in result["gaps"])


# ─────────────────────────── списание и перемещение ───────────────────────────
def test_issue_goes_to_object_at_fifo_cost():
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(receipt(**SUPPLIER)), dict(issue())])
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER), issue()))
    assert result["rejected"] == 0
    issue_entry = [entry for entry in state["entries"] if entry["event"] == "issue"][0]
    debit = [line for line in issue_entry["lines"] if line["side"] == "Дт"][0]
    assert debit["account"] == "20.01"
    assert debit["analytics"]["объект"] == "корпус 2, плита"
    # 5 м³: 5 из остатка 15 м³ по 6 100 (сальдо), а не по цене новой накладной
    assert debit["amount"] == pytest.approx(30500.0)


def test_issue_without_object_is_rejected_with_request():
    doc = issue(object_name="")
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(doc)])
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state, documents=meta(doc))
    assert result["rejected"] == 1
    assert result["errors"][0]["code"] == "NO_OBJECT"


def test_transfer_changes_warehouse_but_not_account_total():
    transfer = {"kind": "transfer", "number": "М-15 №1", "date": "2026-10-06",
                "warehouse": "СК-1", "warehouse_to": "ПР-2",
                "lines": [{"item": "Бетон B25", "qty": 5}]}
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(transfer)])
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state)
    assert result["rejected"] == 0
    after = ledger.balance_as_of(state, "10.01", "2026-10-31", group_by=("склад", "номенклатура"))
    # материал переехал со СК-1 на ПР-2, но общая стоимость не изменилась
    assert sum(after.values()) == ledger.kopeck(116840)
    assert after[("ПР-2", "Бетон B25")] > 0 and after[("СК-1", "Бетон B25")] > 0
    row = next(item for item in ledger.trial_balance(state, "2026-10")["rows"]
               if item["code"] == "10.01")
    assert row["debit"] == row["credit"]


def test_writeoff_goes_to_94_not_to_cost():
    doc = {"kind": "writeoff", "number": "акт №1", "date": "2026-10-20", "warehouse": "СК-1",
           "lines": [{"item": "Бетон B25", "qty": 1, "note": "потери"}]}
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(doc)])
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(doc))
    lines = lines_of(state)
    assert lines["Дт 94"] == pytest.approx(6100.0)
    assert "Дт 20.01" not in lines


def test_inventory_surplus_and_shortage_use_two_rules():
    inventory = {"kind": "inventory", "number": "ИНВ-3 №1", "date": "2026-10-31",
                 "warehouse": "СК-1",
                 "lines": [{"item": "Бетон B25", "qty": 20, "price": 6100},   # +5 → излишек
                           {"item": "Керамзит", "qty": 6, "price": 880}]}     # −2 → недостача
    stock_state = catalog_state()
    stock.post_document(stock_state, dict(inventory))
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state, documents=meta(inventory))
    assert result["rejected"] == 0
    events = {entry["event"] for entry in state["entries"]}
    assert {"inventory_surplus", "inventory_shortage"} <= events
    surplus = [entry for entry in state["entries"] if entry["event"] == "inventory_surplus"][0]
    assert any(line["account"] == "91.01" for line in surplus["lines"])


# ─────────────────────────── давальческие ───────────────────────────
def test_daval_is_off_balance_and_does_not_touch_balance():
    doc = {"kind": "daval_receipt", "number": "М-35 №1", "date": "2026-10-07",
           "warehouse": "ПР-2", "counterparty": "ООО «Заказчик-Инвест»",
           "lines": [{"item": "Труба ПВХ 110", "qty": 300}]}
    stock_state = catalog_state()
    stock.post_document(stock_state, dict(doc))
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state, documents=meta(doc))
    assert result["rejected"] == 0
    entry = state["entries"][-1]
    assert entry["kind"] == "off_balance"
    assert entry["lines"][0]["account"] == "003"
    assert entry["lines"][0]["qty"] == 300
    summary = ledger.verify(state)
    assert summary["ok"] is True
    assert summary["off_balance_qty"] == 300
    report = ledger.trial_balance(state, "2026-10")
    assert all(row["code"] != "003" for row in report["rows"])   # в балансе 003 нет
    assert report["off_balance"][0]["qty_closing"] == 300


def test_daval_return_more_than_received_is_refused():
    given = {"kind": "daval_receipt", "number": "М-35 №1", "date": "2026-10-07",
             "warehouse": "ПР-2", "counterparty": "ООО «Заказчик-Инвест»",
             "lines": [{"item": "Труба ПВХ 110", "qty": 100}]}
    back = {"kind": "daval_return", "number": "М-35 №2", "date": "2026-10-20",
            "warehouse": "ПР-2", "counterparty": "ООО «Заказчик-Инвест»",
            "lines": [{"item": "Труба ПВХ 110", "qty": 150}]}
    stock_state = catalog_state()
    stock.post_document(stock_state, dict(given))
    # склад пропускаем с разрешения (как если бы минус подтвердили): проверяем именно учёт
    stock.post_document(stock_state, dict(back), allow_negative=True, reason="проверка правила")
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state, documents=meta(given, back))
    assert result["rejected"] == 1
    assert result["errors"][0]["code"] == "DAVAL_OVER_RETURN"


# ─────────────────────────── входящие остатки и сверка со складом ───────────────────────────
def test_opening_balance_comes_with_service_account():
    stock_state = catalog_state()
    state = ledger.new_state()
    result = ledger.post_stock(state, stock_state)
    assert result["rejected"] == 0
    lines = lines_of(state)
    assert lines["Дт 10.01"] == pytest.approx(116840.0)
    assert lines["Кт 000"] == pytest.approx(116840.0)


def test_account_10_equals_stock_money():
    documents = json.loads(DEMO_DOCS.read_text(encoding="utf-8"))
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(doc) for doc in documents["documents"]])
    state = ledger.new_state(object_name=stock_state["object"])
    ledger.post_stock(state, stock_state, documents=documents)
    result = ledger.reconcile_stock(state, stock_state)
    assert result["ok"] is True, result["problems"]
    assert result["ledger_total"] == pytest.approx(result["stock_total"])
    assert result["ledger_total"] == pytest.approx(499700.0)


def test_reconcile_catches_vat_basis_mismatch():
    included = {"counterparty": "ООО «БетонТранс»", "vat": {"rate": 20, "included": True}}
    doc = receipt(**included)
    stock_state = posted_stock(doc)
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(doc))
    result = ledger.reconcile_stock(state, stock_state)
    assert result["ok"] is False
    assert result["problems"][0]["code"] == "STOCK_10_MISMATCH"


# ─────────────────────────── ОСВ ───────────────────────────
def test_trial_balance_is_balanced_and_has_openings():
    documents = json.loads(DEMO_DOCS.read_text(encoding="utf-8"))
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(doc) for doc in documents["documents"]])
    state = ledger.new_state(object_name=stock_state["object"])
    ledger.post_stock(state, stock_state, documents=documents)
    report = ledger.trial_balance(state, "2026-10")
    assert report["balanced"] is True
    assert report["totals"]["debit"] == report["totals"]["credit"]
    row = next(item for item in report["rows"] if item["code"] == "10.01")
    assert row["opening_debit"] == pytest.approx(116840.0)
    assert row["closing_debit"] == pytest.approx(499700.0)


def test_trial_balance_for_next_month_has_opening_from_previous():
    documents = json.loads(DEMO_DOCS.read_text(encoding="utf-8"))
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(doc) for doc in documents["documents"]])
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=documents)
    report = ledger.trial_balance(state, "2026-11")
    row = next(item for item in report["rows"] if item["code"] == "10.01")
    assert row["debit"] == 0
    assert row["opening_debit"] == pytest.approx(499700.0)
    assert report["balanced"] is True


def test_register_splits_by_object():
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(receipt(**SUPPLIER)), dict(issue())])
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER), issue()))
    report = ledger.register(state, "20.01", "2026-10")
    assert report["debit"] == pytest.approx(30500.0)
    assert report["rows"][0]["analytics"]["объект"] == "корпус 2, плита"


def test_markdown_and_xlsx_are_produced(tmp_path):
    documents = json.loads(DEMO_DOCS.read_text(encoding="utf-8"))
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(doc) for doc in documents["documents"]])
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=documents)
    text = ledger.to_markdown(state, "2026-10")
    assert "10.01" in text and "Сходится: да" in text
    path = ledger.to_xlsx(ledger.trial_balance(state, "2026-10"), tmp_path / "osv.xlsx")
    assert path.exists() and path.stat().st_size > 0
    journal = ledger.entries_markdown(state, period="2026-10")
    assert "Журнал проводок" in journal and "М-11 №4" in journal
    everything = ledger.entries_markdown(state)
    assert "сальдо начальное" in everything


# ─────────────────────────── выручка по КС-2 ───────────────────────────
def signed_act(**extra):
    act = {"act_id": "КС-2 №38", "act_date": "2026-10-29", "status": "подписан",
           "amount_rub": 1200000, "customer": "ООО «Заказчик-Инвест»",
           "contract": "Д-142/2026", "vat": {"rate": 20, "included": False}}
    act.update(extra)
    return act


def test_signed_act_books_revenue_and_vat():
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    result = ledger.post_acts(state, {"acts": [signed_act()]})
    assert result["posted"] == 1
    lines = lines_of(state)
    # долг заказчика — сумма к получению: 1 200 000 без НДС + 240 000 НДС (вариант А)
    assert lines["Дт 62.01"] == pytest.approx(1440000.0)
    assert lines["Кт 90.01"] == pytest.approx(1440000.0)
    assert lines["Дт 90.03"] == pytest.approx(240000.0)
    assert lines["Кт 68.02"] == pytest.approx(240000.0)
    assert ledger.verify(state)["ok"] is True


def test_checked_but_unsigned_act_gives_no_revenue():
    state = ledger.new_state()
    act = signed_act(status="проверен")
    act.pop("customer")
    result = ledger.post_acts(state, {"acts": [act]})
    assert result["posted"] == 0
    assert state["entries"] == []
    assert "заказчик" in result["gaps"][0]["ask"]


def test_signed_act_without_customer_is_rejected():
    state = ledger.new_state()
    act = signed_act(customer="", contract="")
    result = ledger.post_acts(state, {"acts": [act]})
    assert result["rejected"] == 1
    assert result["errors"][0]["code"] == "ANALYTICS_MISSING"


def test_revenue_plus_stock_gives_balanced_trial_balance():
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(receipt(**SUPPLIER)), dict(issue())])
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER), issue()))
    ledger.post_acts(state, {"acts": [signed_act()]})
    report = ledger.trial_balance(state, "2026-10")
    assert report["balanced"] is True
    assert report["totals"]["debit"] == pytest.approx(report["totals"]["credit"])


# ─────────────────────────── закрытие периода ───────────────────────────
def test_close_period_blocks_later_postings():
    stock_state = posted_stock(receipt(**SUPPLIER))
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER)))
    closed = ledger.close_period(state, "2026-10")
    assert closed["closed"] is True
    assert closed["not_done"], "закрытие обязано перечислить, что ещё не сделано"
    late = ledger.post_entry(state, event="receipt", date="2026-10-20",
                             doc={"number": "УПД №2"},
                             lines=[{"account": "10.01", "side": "Дт", "amount": 10,
                                     "analytics": {"склад": "СК-1", "номенклатура": "Бетон B25"}},
                                    {"account": "60.01", "side": "Кт", "amount": 10,
                                     "analytics": {"контрагент": "ООО «А»"}}])
    assert late["status"] == ledger.STATUS_REJECTED
    assert late["errors"][0]["code"] == "CLOSED_PERIOD"


def test_storno_into_open_period_works_after_close():
    stock_state = posted_stock(receipt(**SUPPLIER))
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER)))
    entry_id = state["entries"][-1]["id"]
    ledger.close_period(state, "2026-10")
    result = ledger.storno(state, entry_id, date="2026-11-05", reason="накладная отменена")
    assert result["status"] == ledger.STATUS_OK
    assert result["entry"]["period"] == "2026-11"


def test_close_twice_does_not_create_second_closing():
    stock_state = posted_stock(receipt(**SUPPLIER))
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER)))
    ledger.close_period(state, "2026-10")
    again = ledger.close_period(state, "2026-10")
    assert again["closed"] is True
    assert len(state["periods"]) == 1


def test_close_is_refused_with_negative_stock():
    negative = {"kind": "issue", "number": "М-11 №9", "date": "2026-10-05", "warehouse": "СК-1",
                "lines": [{"item": "Бетон B25", "qty": 100, "object": "корпус 2"}]}
    stock_state = catalog_state(opening=False)
    stock.post_document(stock_state, dict(negative), allow_negative=True, reason="приход не заведён")
    state = ledger.new_state()
    ledger.post_stock(state, stock_state)
    result = ledger.close_period(state, "2026-10", stock_state=stock_state)
    assert result["closed"] is False
    assert result["problems"][0]["code"] == "NEGATIVE_STOCK"


def test_close_is_refused_with_broken_journal():
    stock_state = posted_stock(receipt(**SUPPLIER))
    state = ledger.new_state()
    ledger.post_stock(state, stock_state, documents=meta(receipt(**SUPPLIER)))
    state["entries"][0]["lines"][0]["amount"] = 1.0
    result = ledger.close_period(state, "2026-10")
    assert result["closed"] is False


def test_close_rejects_bad_period_format():
    state = ledger.new_state()
    result = ledger.close_period(state, "октябрь")
    assert result["closed"] is False
    assert result["problems"][0]["code"] == "BAD_PERIOD"


# ─────────────────────────── демо целиком ───────────────────────────
def test_signed_act_receivable_includes_vat_when_vat_is_on_top():
    """Регресс: при НДС «сверху» 62.01 должен быть суммой к получению, а не суммой без НДС."""
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    ledger.post_acts(state, {"acts": [signed_act()]})
    lines = lines_of(state)
    assert lines["Дт 62.01"] - lines["Дт 90.03"] == pytest.approx(1200000.0)
    assert lines["Кт 68.02"] == pytest.approx(240000.0)


def test_whole_demo_chain_is_consistent():
    documents = json.loads(DEMO_DOCS.read_text(encoding="utf-8"))
    stock_state = catalog_state()
    stock.post_many(stock_state, [dict(doc) for doc in documents["documents"]])
    state = ledger.new_state(object_name=stock_state["object"])
    posted = ledger.post_stock(state, stock_state, documents=documents)
    assert posted["rejected"] == 0
    summary = ledger.verify(state)
    assert summary["ok"] is True
    assert summary["debits"] == summary["credits"]
    assert summary["off_balance_qty"] == 180
    assert ledger.reconcile_stock(state, stock_state)["ok"] is True
