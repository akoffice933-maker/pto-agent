# -*- coding: utf-8 -*-
"""Закрытие месяца: что происходит с затратами, НДС и результатом — и что не происходит.

Проверяем правила, а не «функция вернула словарь»:

1. 25 собирается по объектам и переносится как есть; 26 без аналитики распределяется
   по базе прямых затрат, и доли сходятся до копейки на итог;
2. нет базы распределения — шаг не выполнен с причиной, а не «разложили поровну»;
3. затраты закрываются только по объектам с признанной выручкой; остальное — НЗП,
   и это названо, а не списано молча;
4. несведённая аналитика (захватка без объекта) — отказ с запросом, а не догадка;
5. НДС зачитывается только по зарегистрированным СФ и только если сумма сходится;
   остаток остаётся на 19.03 и попадает в запросы, а не «списывается в ноль»;
6. отсутствие СФ не блокирует закрытие месяца — зачёт это операция налогового периода;
7. закрытие субсчетов 90/91 сохраняет аналитику и не даёт проводок с минусом;
8. прибыль и убыток — разные стороны проводки, а не «минусовая прибыль»;
9. 94 (недостачи) не закрывается автоматически: программа не решает, кто виноват;
10. закрытый период не принимает проводок, повторное закрытие не удваивает результат;
11. режим «предлагать»: проводки становятся предложениями, период не закрывается,
    пока человек не примет их; отклонение оставляет след;
12. сухой прогон не меняет журнал, но считает цепочкой (распределение влияет на следующую цифру).
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import closing, ledger, stock

DEMO_DOCS = ROOT / "data" / "demo" / "stock" / "documents_2026_10.json"
DEMO_CATALOG = ROOT / "data" / "demo" / "stock" / "catalog.json"
DEMO_INVOICES = ROOT / "data" / "demo" / "accounting" / "invoices_2026_10.json"
SIGNED_ACTS = ROOT / "data" / "demo" / "accounting" / "acts_signed_scenario.json"


# ─────────────────────────── помощники ───────────────────────────
def policy() -> dict:
    return closing.load_policy()


def invoices() -> dict:
    return json.loads(DEMO_INVOICES.read_text(encoding="utf-8"))


def stock_state() -> dict:
    payload = json.loads(DEMO_CATALOG.read_text(encoding="utf-8"))
    state = stock.new_state(object_name=payload["object"],
                            warehouses=payload["warehouses"], items=payload["items"],
                            opening=payload.get("opening"))
    documents = json.loads(DEMO_DOCS.read_text(encoding="utf-8"))
    stock.post_many(state, [dict(doc) for doc in documents["documents"]])
    return state


def book(*, with_stock: bool = True, with_revenue: bool = False,
         company: str = "ЖК «Северный», корпус 2") -> dict:
    """Журнал с демо-складом; по желанию — с признанной выручкой."""
    state = ledger.new_state(object_name=company)
    if with_stock:
        documents = json.loads(DEMO_DOCS.read_text(encoding="utf-8"))
        ledger.post_stock(state, stock_state(), documents=documents)
    if with_revenue:
        acts = json.loads(SIGNED_ACTS.read_text(encoding="utf-8"))
        ledger.post_acts(state, acts)
    return state


def add_overheads(state: dict, *, account: str = "26", amount: float = 50000,
                  object_name: str = "", article: str = "аренда") -> dict:
    analytics = {"статья затрат": article}
    if object_name:
        analytics["объект"] = object_name
    return ledger.post_entry(state, event="overhead", date="2026-10-31",
                             doc={"number": f"счёт за услуги по {account}"}, rule="demo",
                             lines=[{"account": account, "side": "Дт", "amount": amount,
                                     "analytics": analytics},
                                    {"account": "60.01", "side": "Кт", "amount": amount,
                                     "analytics": {"контрагент": "ООО «Услуги»"}}])


def steps(result: dict) -> dict:
    return {step["id"]: step for step in result["steps"]}


def balance(state: dict, account: str, date: str = "2026-10-31") -> float:
    return float(sum(ledger.balance_as_of(state, account, date).values()))


# ─────────────────────────── политика ───────────────────────────
def test_policy_loads_and_says_what_it_does_not_do():
    data = policy()
    assert data["schema"] == "pto-agent/policy@1"
    assert data["mode"]["value"] in ("проводить", "предлагать")
    assert data["not_implemented"], "политика обязана перечислять, чего в ней нет"


def test_rollup_maps_capture_to_object():
    data = policy()
    assert closing.rollup_of(data, "корпус 2, плита") == "ЖК «Северный», корпус 2"
    assert closing.rollup_of(data, "ЖК «Северный», корпус 2") == "ЖК «Северный», корпус 2"
    assert closing.rollup_of(data, "неизвестная захватка") == "неизвестная захватка"


def test_unrolled_lists_names_without_mapping():
    data = policy()
    assert closing.unrolled(data, {"корпус 2, плита", "чужая захватка"}) == ["чужая захватка"]


def test_policy_markdown_is_honest():
    text = closing.policy_markdown(policy())
    assert "Чего в политике нет" in text
    assert "реформация" in text.lower()


# ─────────────────────────── косвенные расходы ───────────────────────────
def test_overhead_without_analytics_is_allocated_by_direct_costs():
    state = book(with_revenue=True)
    add_overheads(state, account="26", amount=50000)
    result = closing.close_month(state, "2026-10", invoices=invoices())
    step = steps(result)["overheads"]
    assert step["status"] == closing.DONE
    assert step["amount"] == pytest.approx(50000.0)
    # база — прямые затраты, поэтому доли записаны в проводку
    entry = next(e for e in state["entries"] if e["event"] == "close.overheads")
    assert entry["bases"], "распределение без базы в проводке недоказуемо"
    total = sum(line["amount"] for line in entry["lines"] if line["side"] == "Дт")
    assert total == pytest.approx(50000.0)


def test_overhead_with_object_analytics_is_carried_not_reallocated():
    state = book(with_revenue=True)
    add_overheads(state, account="25", amount=150000, object_name="корпус 2, плита")
    result = closing.close_month(state, "2026-10", invoices=invoices())
    step = steps(result)["overheads"]
    assert step["status"] == closing.DONE
    entry = next(e for e in state["entries"] if e["event"] == "close.overheads")
    debit = [line for line in entry["lines"] if line["side"] == "Дт"]
    assert all(line["analytics"]["объект"] == "корпус 2, плита" for line in debit)


def test_overhead_without_base_is_refused_with_reason():
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    add_overheads(state, account="26", amount=50000)      # прямых затрат нет вовсе
    result = closing.close_month(state, "2026-10")
    step = steps(result)["overheads"]
    assert step["status"] == closing.NOT_DONE
    assert "базы распределения" in step["why"]
    assert step["requests"], "без базы распределения нужен запрос, а не тишина"
    assert result["closed"] is False


def test_overhead_allocation_shares_sum_to_total():
    state = book(with_revenue=True)
    add_overheads(state, account="26", amount=1000)
    closing.step_overheads(state, "2026-10", policy=policy(), mode=ledger.MODE_POST,
                           chart=ledger.load_chart(), rules=ledger.load_rules(),
                           date="2026-10-31")
    entry = next(e for e in state["entries"] if e["event"] == "close.overheads")
    debit = sum(line["amount"] for line in entry["lines"] if line["side"] == "Дт")
    credit = sum(line["amount"] for line in entry["lines"] if line["side"] == "Кт")
    assert debit == credit == pytest.approx(1000.0)


# ─────────────────────────── затраты и НЗП ───────────────────────────
def test_costs_stay_in_progress_without_signed_acts():
    state = book()
    result = closing.close_month(state, "2026-10", invoices=invoices())
    step = steps(result)["costs"]
    assert step["status"] == closing.NOT_NEEDED
    assert "НЗП" in step["why"]
    assert balance(state, "20.01") == pytest.approx(2626500.0), "затраты не должны исчезнуть"
    assert balance(state, "90.02") == 0


def test_costs_close_to_cost_of_sales_when_act_is_signed():
    state = book(with_revenue=True)
    result = closing.close_month(state, "2026-10", invoices=invoices())
    step = steps(result)["costs"]
    assert step["status"] == closing.DONE
    assert step["amount"] == pytest.approx(2626500.0)
    assert balance(state, "20.01") == 0
    assert balance(state, "90.02") == 0        # закрыт на 90.09 в том же закрытии
    profit = step["amount"]


def test_costs_close_after_overheads_so_cost_includes_them():
    state = book(with_revenue=True)
    add_overheads(state, account="26", amount=50000)
    result = closing.close_month(state, "2026-10", invoices=invoices())
    assert steps(result)["costs"]["amount"] == pytest.approx(2676500.0)


def test_costs_refuse_when_analytics_is_not_rolled_up():
    state = book(with_revenue=True)
    # затраты на захватку, которой нет в свёртке: закрывать нечего, но объект выручки есть
    ledger.post_entry(state, event="issue", date="2026-10-30",
                      doc={"number": "М-11 вне свёртки"},
                      lines=[{"account": "20.01", "side": "Дт", "amount": 1000,
                              "analytics": {"объект": "чужая захватка"}},
                             {"account": "60.01", "side": "Кт", "amount": 1000,
                              "analytics": {"контрагент": "ООО «Услуги»"}}])
    result = closing.close_month(state, "2026-10", invoices=invoices())
    step = steps(result)["costs"]
    assert step["status"] == closing.NOT_DONE
    assert "чужая захватка" in step["why"]
    assert any("rollup" in gap["ask"] for gap in step["requests"])
    assert result["closed"] is False


# ─────────────────────────── НДС ───────────────────────────
def test_vat_not_offset_without_invoices_and_does_not_block_closing():
    state = book()
    result = closing.close_month(state, "2026-10")
    step = steps(result)["vat"]
    assert step["status"] == closing.PENDING
    assert balance(state, "19.03") == pytest.approx(604000.0)
    assert result["closed"] is True, "отсутствие СФ — не повод не закрывать месяц"


def test_vat_offset_is_partial_when_invoices_are_incomplete():
    state = book()
    result = closing.close_month(state, "2026-10", invoices=invoices())
    step = steps(result)["vat"]
    assert step["status"] == closing.PARTIAL
    assert step["amount"] == pytest.approx(304800.0)
    assert balance(state, "19.03") == pytest.approx(299200.0)
    assert any("УПД №119" in gap["what"] for gap in step["requests"])


def test_vat_offset_posts_to_68_02_by_counterparty():
    state = book()
    closing.close_month(state, "2026-10", invoices=invoices())
    entry = next(e for e in state["entries"] if e["event"] == "close.vat")
    assert any(line["account"] == "68.02" and line["side"] == "Дт" for line in entry["lines"])
    credits = [line for line in entry["lines"] if line["account"] == "19.03"]
    assert all(line["analytics"].get("контрагент") for line in credits)


def test_invoice_with_bigger_vat_is_not_offset():
    invoices_bad = {"invoices": [{"number": "СФ №1", "date": "2026-10-01",
                                  "document": "УПД №118", "counterparty": "ООО «БетонТранс»",
                                  "net": 2000000, "vat": 400000}]}
    state = book()
    result = closing.close_month(state, "2026-10", invoices=invoices_bad)
    step = steps(result)["vat"]
    assert step["amount"] in (0.0, None)
    assert any("расходится" in gap["why"] or "больше" in gap["why"]
               for gap in step["requests"]) or step["detail"]
    assert balance(state, "19.03") == pytest.approx(604000.0)


def test_invoice_for_unknown_document_is_reported():
    invoices_bad = {"invoices": [{"number": "СФ №9", "date": "2026-10-01",
                                  "document": "УПД №999", "vat": 1000}]}
    state = book()
    result = closing.close_month(state, "2026-10", invoices=invoices_bad)
    step = steps(result)["vat"]
    assert step["status"] == closing.PENDING
    assert any(item["code"] == "NO_BASE_DOC" for item in step["detail"])


# ─────────────────────────── закрытие 90/91 и результат ───────────────────────────
def test_sales_subaccounts_close_with_analytics_kept():
    state = book(with_revenue=True)
    closing.close_month(state, "2026-10", invoices=invoices())
    for account in ("90.01", "90.02", "90.03"):
        assert balance(state, account) == 0, f"{account} не закрыт"
    entry = next(e for e in state["entries"] if e["event"] == "close.sales")
    revenue = [line for line in entry["lines"] if line["account"] == "90.01"]
    assert revenue[0]["analytics"]["объект"] == "ЖК «Северный», корпус 2"
    assert revenue[0]["analytics"]["договор"] == "Д-142/2026"


def test_profit_appears_on_credit_of_99():
    state = book(with_revenue=True)
    result = closing.close_month(state, "2026-10", invoices=invoices())
    profit = result["result"]["profit"]
    assert profit == pytest.approx(34106600.0 - 2626500.0)
    rows = {row["code"]: row for row in ledger.trial_balance(state, "2026-10")["rows"]}
    assert rows["99"]["closing_credit"] == pytest.approx(profit)
    assert rows["99"]["closing_debit"] == 0


def test_loss_appears_on_debit_of_99():
    state = book(with_revenue=True)
    # делаем себестоимость больше выручки: доначисляем косвенные в 40 млн
    add_overheads(state, account="26", amount=40000000)
    result = closing.close_month(state, "2026-10", invoices=invoices())
    assert result["result"]["profit"] < 0
    rows = {row["code"]: row for row in ledger.trial_balance(state, "2026-10")["rows"]}
    assert rows["99"]["closing_debit"] > 0
    assert rows["99"]["closing_credit"] == 0


def test_other_income_closes_through_91_09():
    state = book(with_revenue=True)
    ledger.post_entry(state, event="inventory_surplus", date="2026-10-30",
                      doc={"number": "ИНВ-3 излишек"},
                      lines=[{"account": "10.01", "side": "Дт", "amount": 5000,
                              "analytics": {"склад": "СК-1", "номенклатура": "Керамзит"}},
                             {"account": "91.01", "side": "Кт", "amount": 5000,
                              "analytics": {}}])
    result = closing.close_month(state, "2026-10", invoices=invoices())
    assert steps(result)["other"]["status"] == closing.DONE
    assert balance(state, "91.01") == 0
    assert balance(state, "91.09") == 0


# ─────────────────────────── недостачи ───────────────────────────
def test_shortages_require_decision_and_stay_on_94():
    state = book()
    result = closing.close_month(state, "2026-10", invoices=invoices())
    step = steps(result)["shortages"]
    assert step["status"] == closing.NEEDS_DECISION
    assert step["blocking"] is False
    assert balance(state, "94") == pytest.approx(10640.0)
    assert any("только решением" in gap["why"] for gap in step["requests"])


# ─────────────────────────── период ───────────────────────────
def test_close_records_period_and_blocks_new_entries():
    state = book(with_revenue=True)
    result = closing.close_month(state, "2026-10", invoices=invoices())
    assert result["closed"] is True
    assert state["periods"][0]["period"] == "2026-10"
    assert state["periods"][0]["steps"], "закрытие обязано записать, что было сделано"
    late = ledger.post_entry(state, event="receipt", date="2026-10-15", doc={"number": "УПД поздно"},
                             lines=[{"account": "10.01", "side": "Дт", "amount": 10,
                                     "analytics": {"склад": "СК-1", "номенклатура": "Бетон B25"}},
                                    {"account": "60.01", "side": "Кт", "amount": 10,
                                     "analytics": {"контрагент": "ООО «А»"}}])
    assert late["status"] == ledger.STATUS_REJECTED
    assert late["errors"][0]["code"] == "CLOSED_PERIOD"


def test_second_close_does_not_double_entries():
    state = book(with_revenue=True)
    closing.close_month(state, "2026-10", invoices=invoices())
    count = len(state["entries"])
    again = closing.close_month(state, "2026-10", invoices=invoices())
    assert again["closed"] is True
    assert len(state["entries"]) == count
    assert again["warnings"]


def test_close_refuses_broken_journal():
    state = book()
    state["entries"][0]["lines"][0]["amount"] = 1.0
    result = closing.close_month(state, "2026-10")
    assert result["closed"] is False
    assert result["problems"][0]["code"] == "JOURNAL_BROKEN"


def test_close_refuses_negative_stock():
    documents = {"documents": [{"kind": "issue", "number": "М-11 №9", "date": "2026-10-05",
                                "warehouse": "СК-1",
                                "lines": [{"item": "Бетон B25", "qty": 500,
                                           "object": "корпус 2"}]}]}
    st = stock.new_state(object_name="объект")
    payload = json.loads(DEMO_CATALOG.read_text(encoding="utf-8"))
    st["warehouses"] = payload["warehouses"]
    st["items"] = payload["items"]
    stock.post_many(st, [dict(doc) for doc in documents["documents"]], allow_negative=True,
                    reason="проверка")
    state = ledger.new_state(object_name="объект")
    ledger.post_stock(state, st)
    result = closing.close_month(state, "2026-10", stock_state=st)
    assert result["closed"] is False
    assert result["problems"][0]["code"] == "NEGATIVE_STOCK"


def test_close_rejects_bad_period():
    result = closing.close_month(book(), "октябрь")
    assert result["closed"] is False
    assert result["problems"][0]["code"] == "BAD_PERIOD"


# ─────────────────────────── режим «предлагать» ───────────────────────────
def test_propose_mode_creates_proposals_not_entries():
    state = book(with_revenue=True)
    before = len(state["entries"])
    result = closing.close_month(state, "2026-10", invoices=invoices(),
                                 mode=ledger.MODE_PROPOSE)
    assert result["closed"] is False, "в режиме «предлагать» период не закрывается"
    assert len(state["entries"]) == before
    assert state["proposals"], "предложения должны появиться"
    statuses = {step["status"] for step in result["steps"]}
    assert closing.PROPOSED in statuses


def test_accepted_proposal_becomes_entry():
    state = book(with_revenue=True)
    closing.close_month(state, "2026-10", invoices=invoices(), mode=ledger.MODE_PROPOSE)
    proposal = state["proposals"][0]["id"]
    result = ledger.accept_proposal(state, proposal)
    assert result["status"] == ledger.STATUS_OK
    assert all(row["id"] != proposal for row in state["proposals"])


def test_rejected_proposal_leaves_trace():
    state = book(with_revenue=True)
    closing.close_month(state, "2026-10", invoices=invoices(), mode=ledger.MODE_PROPOSE)
    proposal = state["proposals"][0]["id"]
    ledger.reject_proposal(state, proposal, reason="не в этом месяце")
    assert all(row["id"] != proposal for row in state["proposals"])
    assert state["rejections"][-1]["reason"] == "не в этом месяце"


def test_proposals_do_not_balance_journal():
    state = book(with_revenue=True)
    closing.close_month(state, "2026-10", invoices=invoices(), mode=ledger.MODE_PROPOSE)
    assert ledger.verify(state)["ok"] is True


# ─────────────────────────── сухой прогон ───────────────────────────
def test_dry_run_does_not_touch_journal_but_chains_steps():
    state = book(with_revenue=True)
    add_overheads(state, account="26", amount=50000)
    before = json.dumps(state, sort_keys=True, ensure_ascii=False)
    result = closing.close_month(state, "2026-10", invoices=invoices(), dry_run=True)
    assert result.get("dry_run") is True
    assert json.dumps(state, sort_keys=True, ensure_ascii=False) == before
    step = steps(result)["costs"]
    # распределение 50 000 учтено в следующем шаге: 2 626 500 + 50 000
    assert step["amount"] == pytest.approx(2676500.0)


# ─────────────────────────── весь цикл ───────────────────────────
def test_full_cycle_is_balanced_and_result_adds_up():
    state = book(with_revenue=True)
    add_overheads(state, account="26", amount=50000)
    result = closing.close_month(state, "2026-10", invoices=invoices())
    assert result["closed"] is True
    report = ledger.trial_balance(state, "2026-10")
    assert report["balanced"] is True
    res = result["result"]
    assert res["revenue_net"] == pytest.approx(34106600.0)
    assert res["cost"] == pytest.approx(2676500.0)
    assert res["profit"] == pytest.approx(res["revenue_net"] - res["cost"])
    assert ledger.verify(state)["ok"] is True


def test_stock_money_matches_account_10_after_closing():
    state = book(with_revenue=True)
    st = stock_state()
    closing.close_month(state, "2026-10", invoices=invoices())
    reconcile = ledger.reconcile_stock(state, st)
    assert reconcile["ok"] is True
