# -*- coding: utf-8 -*-
"""Закрытие года: реформация 99 → 84, недостачи 94, оценочные обязательства 96, РБП 97.

Что должно быть верно (а не «код не упал»):

1. год не закрывается, пока не закрыты все 12 месяцев — и об этом сказано, какие открыты;
2. реформация переносит результат на 84: прибыль на 84.01, убыток на 84.02, и 99 после
   неё нулевой (проверяется, а не предполагается);
3. реформация одна на год: повторный вызов не создаёт вторую проводку и не удваивает 84;
4. недостача без решения остаётся на 94 и называется цифрой — программа не решает, кто виноват;
5. решение «на виновного» без ФИО и без основания не проводится;
6. оценочное обязательство не списывается в доходы «до нуля»: остаток переходит, а списание
   в 91.01 возможно только по прекращённому обязательству с основанием;
7. РБП списывается частично, остаток переходит на следующий год;
8. операции года попадают в закрытый декабрь только с причиной, и причина остаётся в записи;
9. расходы года доходят до результата года (перекрытие декабря), а не висят на 26/20.01/91.02.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pto_agent import ledger, year_close                                # noqa: E402
from scripts.make_demo_year import build                               # noqa: E402

SCENARIO = REPO / "data" / "demo" / "year_close" / "scenario_2026.json"
DECISIONS = REPO / "data" / "demo" / "year_close" / "decisions_2026.json"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def year_state() -> dict:
    state, _ = build(SCENARIO)
    return state


def fresh(state: dict) -> dict:
    return json.loads(json.dumps(state))


def decisions() -> dict:
    return read(DECISIONS)


def codes(step_result: dict) -> list[str]:
    return [item["code"] for item in step_result.get("detail") or []]


def balance(state: dict, account: str, date: str = "2026-12-31") -> Decimal:
    return year_close.account_balance(state, account, date)


# ─────────────────────────── предпосылки ───────────────────────────
def test_year_is_not_closed_while_months_are_open():
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    report = year_close.close_year(state, "2026")
    assert not report["closed"]
    assert any(item["code"] == "MONTH_NOT_CLOSED" for item in report["problems"])
    assert "2026-01" in report["problems"][0]["what"]
    assert state["entries"] == []            # ни одной проводки «на всякий случай»


def test_precheck_names_exactly_the_open_months(year_state):
    state = fresh(year_state)
    state["periods"] = [row for row in state["periods"] if row["period"] != "2026-12"]
    report = year_close.precheck(state, "2026")
    assert not report["ok"]
    assert report["open_months"] == ["2026-12"]
    assert len(report["closed_months"]) == 11


def test_bad_year_is_refused_with_the_format():
    report = year_close.close_year(ledger.new_state(), "26")
    assert not report["closed"]
    assert report["problems"][0]["code"] == "BAD_YEAR"


# ─────────────────────────── реформация ───────────────────────────
def test_reformation_moves_profit_to_retained_and_zeroes_99(year_state):
    state = fresh(year_state)
    report = year_close.close_year(state, "2026", decisions=decisions())
    assert report["closed"], report["problems"]
    assert balance(state, "99") == Decimal("0.00")
    # на 84 уходит чистая прибыль: 15 000 000 до налога − 3 750 000 налога (круг 44)
    assert balance(state, "84") == Decimal("-11250000.00")   # кредитовое сальдо — прибыль
    assert balance(state, "84.01") == Decimal("-11250000.00")
    assert balance(state, "84.02") == Decimal("0.00")
    reform = [row for row in state["entries"] if row["event"] == "year.reformation"]
    assert len(reform) == 1
    lines = {(line["account"], line["side"]): line["amount"] for line in reform[0]["lines"]}
    assert lines[("99", ledger.DEBIT)] == 11250000.0
    assert lines[("84.01", ledger.CREDIT)] == 11250000.0


def test_reformation_does_not_double_on_second_run(year_state):
    state = fresh(year_state)
    year_close.close_year(state, "2026", decisions=decisions())
    entries_before = len(state["entries"])
    second = year_close.close_year(state, "2026", decisions=decisions())
    assert len(state["entries"]) == entries_before, "повторный прогон создал проводки"
    assert balance(state, "84") == Decimal("-11250000.00")
    reform_step = next(row for row in second["steps"] if row["id"] == "reformation")
    assert reform_step["status"] == year_close.NOT_NEEDED


def test_loss_goes_to_84_02_not_to_negative_profit(tmp_path):
    payload = read(SCENARIO)
    for row in payload["months"]:
        row["cost"] = row["net"] + 200_000          # расходы больше выручки каждый месяц
    scenario = tmp_path / "loss.json"
    scenario.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    state, summary = build(scenario)
    assert all(row["closed"] for row in summary["months"])
    report = year_close.close_year(state, "2026")
    assert report["closed"], report["problems"]
    # убыток 12 × 200 000 от субподряда + 1 240 000 оценочного обязательства = 3 640 000
    assert balance(state, "84.02") == Decimal("3640000.00")     # дебет — убыток
    assert balance(state, "84.01") == Decimal("0.00")
    assert balance(state, "99") == Decimal("0.00")
    assert report["result"]["profit"] < 0


# ─────────────────────────── недостачи 94 ───────────────────────────
def test_shortage_without_decision_stays_on_94_and_is_named(year_state):
    state = fresh(year_state)
    step = year_close.step_shortages(state, "2026", decisions={}, mode=ledger.MODE_POST,
                                     chart=ledger.load_chart(), rules=year_close.load_rules())
    assert step["status"] == year_close.NEEDS_DECISION
    assert step["amount"] == 148500.0
    assert any("решение по недостаче" in item["what"] for item in step["requests"])
    assert balance(state, "94") == Decimal("148500.00")


def test_shortage_to_victim_requires_person_and_basis(year_state):
    chart = ledger.load_chart()
    rules = year_close.load_rules()
    state = fresh(year_state)
    without_person = {"shortages": [{"склад": "Центральный склад", "decision": "victim",
                                     "basis": {"number": "приказ №1", "date": "2026-12-20"}}]}
    step = year_close.step_shortages(state, "2026", decisions=without_person,
                                     mode=ledger.MODE_POST, chart=chart, rules=rules)
    assert step["status"] == year_close.NOT_DONE
    assert any("сотрудник" in item["why"] for item in step["problems"])
    assert balance(state, "94") == Decimal("148500.00")

    without_basis = {"shortages": [{"склад": "Центральный склад", "decision": "victim",
                                    "сотрудник": "Гусев А.В."}]}
    step2 = year_close.step_shortages(fresh(year_state), "2026", decisions=without_basis,
                                      mode=ledger.MODE_POST, chart=chart, rules=rules)
    assert step2["status"] == year_close.NOT_DONE
    assert any("основани" in item["why"] for item in step2["problems"])
    assert balance(state, "94") == Decimal("148500.00")


def test_shortage_more_than_balance_is_refused(year_state):
    state = fresh(year_state)
    decision = {"shortages": [{"склад": "Центральный склад", "decision": "other", "amount": 200000,
                               "basis": {"number": "приказ №1", "date": "2026-12-20"}}]}
    step = year_close.step_shortages(state, "2026", decisions=decision,
                                     mode=ledger.MODE_POST, chart=ledger.load_chart(),
                                     rules=year_close.load_rules())
    assert step["status"] == year_close.NOT_DONE
    assert "200 000,00" in step["why"] and "148 500,00" in step["why"]
    assert balance(state, "94") == Decimal("148500.00")


def test_shortage_decision_reaches_73_02(year_state):
    state = fresh(year_state)
    year_close.close_year(state, "2026", decisions=decisions())
    assert balance(state, "94") == Decimal("0.00")
    assert balance(state, "73.02") == Decimal("148500.00")
    entry = next(row for row in state["entries"] if row["event"] == "year.shortages")
    analyst = {line["account"]: line["analytics"] for line in entry["lines"]}
    assert analyst["73.02"]["сотрудник"] == "Гусев А.В."


# ─────────────────── оценочные обязательства 96 ───────────────────
def test_provision_is_not_written_off_to_income_by_default(year_state):
    state = fresh(year_state)
    report = year_close.close_year(state, "2026", decisions={})
    assert report["closed"]
    assert balance(state, "96") == Decimal("-1240000.00")      # обязательство переходит
    assert not [row for row in state["entries"] if row["event"] == "year.provisions"]
    assert year_close.account_balance(state, "91.01", "2026-12-31") == Decimal("0.00")
    step = next(row for row in report["steps"] if row["id"] == "provisions")
    assert step["status"] == year_close.NEEDS_DECISION


def test_released_provision_goes_to_income_only_with_basis(year_state):
    chart, rules = ledger.load_chart(), year_close.load_rules()
    no_basis = {"provisions": [{"вид": "резерв на оплату отпусков", "status": "released"}]}
    state = fresh(year_state)
    step = year_close.step_provisions(state, "2026", decisions=no_basis,
                                      mode=ledger.MODE_POST, chart=chart, rules=rules)
    assert step["status"] == year_close.NOT_DONE
    assert balance(state, "96") == Decimal("-1240000.00")

    with_basis = {"provisions": [{"вид": "резерв на оплату отпусков", "status": "released",
                                  "basis": {"number": "акт сверки №9", "date": "2026-12-31"}}]}
    state2 = fresh(year_state)
    step2 = year_close.step_provisions(state2, "2026", decisions=with_basis,
                                       mode=ledger.MODE_POST, chart=chart, rules=rules)
    assert step2["status"] == year_close.DONE
    assert balance(state2, "96") == Decimal("0.00")
    entry = next(row for row in state2["entries"] if row["event"] == "year.provisions")
    lines = {(line["account"], line["side"]) for line in entry["lines"]}
    assert ("96", ledger.DEBIT) in lines and ("91.01", ledger.CREDIT) in lines


# ─────────────────── расходы будущих периодов 97 ───────────────────
def test_future_costs_partial_write_off_leaves_the_rest_for_next_year(year_state):
    state = fresh(year_state)
    year_close.close_year(state, "2026", decisions=decisions())
    assert balance(state, "97") == Decimal("800000.00")
    step = next(row for row in year_close.close_year(fresh(year_state), "2026",
                                                     decisions=decisions())["steps"]
                if row["id"] == "future_costs")
    assert step["status"] == year_close.PARTIAL
    assert "переходит на 2027" in step["why"]


def test_future_cost_without_schedule_is_not_written_off(year_state):
    state = fresh(year_state)
    report = year_close.close_year(state, "2026", decisions={})
    assert report["closed"]
    assert balance(state, "97") == Decimal("960000.00")
    step = next(row for row in report["steps"] if row["id"] == "future_costs")
    assert step["status"] == year_close.NEEDS_DECISION


# ─────────── расходы года доходят до результата (перекрытие) ───────────
def test_year_adjustments_reach_the_result_and_do_not_hang_on_26(year_state):
    with_rbp = fresh(year_state)
    year_close.close_year(with_rbp, "2026", decisions=decisions())
    without_rbp = fresh(year_state)
    no_future = decisions()
    no_future["future_costs"] = [{"вид": "страхование СМР", "amount": 0}]
    year_close.close_year(without_rbp, "2026", decisions=no_future)
    assert balance(with_rbp, "26") == Decimal("0.00"), "26 остался с сальдо"
    assert balance(with_rbp, "20.01") == Decimal("0.00"), "20.01 остался с сальдо"
    before_with = Decimal(str(year_close._result_block(with_rbp, "2026",
                                                       ledger.load_chart())["profit_before_tax"]))
    before_without = Decimal(str(year_close._result_block(without_rbp, "2026",
                                                          ledger.load_chart())["profit_before_tax"]))
    assert before_without - before_with == Decimal("160000.00"), "РБП не дошло до прибыли до налога"
    # а до чистой прибыли доходит 120 000: 160 000 расхода снимают 40 000 налога
    net_with = Decimal(str(year_close._result_block(with_rbp, "2026",
                                                    ledger.load_chart())["profit"]))
    net_without = Decimal(str(year_close._result_block(without_rbp, "2026",
                                                       ledger.load_chart())["profit"]))
    assert net_without - net_with == Decimal("120000.00")


def test_year_end_entries_are_late_entries_with_reason(year_state):
    state = fresh(year_state)
    year_close.close_year(state, "2026", decisions=decisions())
    late = [row for row in state["entries"] if row.get("late")]
    assert late, "операции года не помечены как проводки в закрытом периоде"
    for entry in late:
        assert entry["late"]["period"] == "2026-12"
        assert entry["late"]["reason"]
    assert ledger.verify(state)["ok"], ledger.verify(state)["problems"][:3]


def test_late_entry_without_reason_is_refused_by_the_journal(year_state):
    state = fresh(year_state)
    result = ledger.post_entry(
        state, event="year.reformation", date="2026-12-31",
        doc={"number": "проба", "date": "2026-12-31", "kind": "closing"},
        lines=[{"account": "99", "side": ledger.DEBIT, "amount": 1, "analytics": {}},
               {"account": "84.01", "side": ledger.CREDIT, "amount": 1, "analytics": {}}],
        rule="year.reformation", key="проба|1", allow_closed=True)
    assert result["status"] == ledger.STATUS_REJECTED
    assert result["errors"][0]["code"] == "LATE_WITHOUT_REASON"


# ─────────────────────────── вывод ───────────────────────────
def test_screen_and_markdown_say_what_is_not_done(year_state):
    state = fresh(year_state)
    report = year_close.close_year(state, "2026", decisions=decisions())
    text = year_close.screen_text(report)
    assert "реформация" in text.lower()
    assert "не сделано" in text.lower()
    assert "налог на прибыль" in text.lower()
    markdown = year_close.to_markdown(report)
    assert "| Шаг | Статус |" in markdown
    assert "Реформация баланса" in markdown
    assert "Не сделано" in markdown


def test_result_block_counts_revenue_vat_and_cost(year_state):
    state = fresh(year_state)
    report = year_close.close_year(state, "2026", decisions=decisions())
    result = report["result"]
    assert result["revenue"] == pytest.approx(67_100_000.0)
    assert result["vat_out"] == pytest.approx(12_100_000.0)
    assert result["cost"] == pytest.approx(40_000_000.0)
    assert result["profit_before_tax"] == pytest.approx(15_000_000.0)
    assert result["profit_tax"] == pytest.approx(3_750_000.0)
    assert result["profit"] == pytest.approx(11_250_000.0)
