# -*- coding: utf-8 -*-
"""Круг 44: налог на прибыль проведён по-настоящему, год принимает число, ОДК/ОДДС — по данным.

Что должно быть верно (а не «шаг появился в списке»):

1. `close_year(state, 2026)` (год **числом**) работает так же, как со строкой: раньше падал
   `TypeError` на `year + "-12"`;
2. налог на прибыль проводится одной проводкой **Дт 99 Кт 68.04** на 31 декабря;
3. проводка идёт **до реформации**: иначе сальдо 99 станет ненулевым, а чистая прибыль в
   отчёте разойдётся с остатком на 84;
4. сумма = 25 % (8 % федеральный + 17 % региональный) от прибыли от реализации за год
   = 3 750 000; база — та же, что 2300 в ОФР;
5. после налога на 84 уходит 11 250 000, а не 15 000 000;
6. при убытке налог не начисляется, и это сказано словами, а не нулём;
7. повторный прогон не проводит налог второй раз (ключ идемпотентности);
8. если месяц не закрыт, налог не проводится: шаг говорит «сначала закрытие», а не считает
   от завышенной прибыли;
9. налог считается **за год целиком**, а не суммой месяцев; расхождение, если оно есть,
   называется, а не замалчивается;
10. ОДК и ОДДС не строятся, пока нет данных, и называют, каких именно данных нет;
11. когда данные появляются (движение по 51), это видно в том же блоке — «нет данных» меняется.
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

from pto_agent import ledger, statements, tax, year_close        # noqa: E402
from scripts.make_demo_year import build                        # noqa: E402

SCENARIO = REPO / "data" / "demo" / "year_close" / "scenario_2026.json"
DECISIONS = REPO / "data" / "demo" / "year_close" / "decisions_2026.json"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def closed() -> dict:
    state, _ = build(SCENARIO)
    report = year_close.close_year(state, "2026", decisions=read(DECISIONS))
    assert report["closed"], report["problems"]
    return state


def fresh(state: dict) -> dict:
    return json.loads(json.dumps(state))


# ─────────────────────── 1. год числом ───────────────────────
def test_year_as_number_closes_the_same_way():
    state, _ = build(SCENARIO)
    report = year_close.close_year(state, 2026, decisions=read(DECISIONS))
    assert report["closed"], report["problems"]
    assert report["year"] == "2026"
    assert year_close.account_balance(state, "84", "2026-12-31") == Decimal("-11250000.00")


def test_bad_year_is_rejected_not_crashed():
    state, _ = build(SCENARIO)
    report = year_close.close_year(state, 26)
    assert not report["closed"]
    assert report["problems"][0]["code"] == "BAD_YEAR"


# ─────────────────────── 2–5. проводка налога ───────────────────────
def test_tax_posting_is_debit_99_credit_68_04(closed):
    entries = [row for row in closed["entries"] if row.get("event") == "year.profit_tax"]
    assert len(entries) == 1
    entry = entries[0]
    lines = {(line["account"], line["side"]): line["amount"] for line in entry["lines"]}
    assert lines[("99", ledger.DEBIT)] == pytest.approx(3_750_000.0)
    assert lines[("68.04", ledger.CREDIT)] == pytest.approx(3_750_000.0)
    assert entry["date"] == "2026-12-31"
    assert entry["late"]["reason"], "проводка в закрытом декабре обязана иметь причину"


def test_tax_goes_before_reformation(closed):
    order = [row["id"] for row in year_close.close_year(fresh(closed), "2026",
                                                        decisions=read(DECISIONS))["steps"]]
    assert order.index("profit_tax") < order.index("reformation")
    assert year_close.account_balance(closed, "99", "2026-12-31") == Decimal("0.00")


def test_tax_is_a_quarter_of_accounting_profit(closed):
    report = statements.check(closed, "2026")
    tax_info = report["tax"]
    assert tax_info["posted"] == pytest.approx(3_750_000.0)
    assert tax_info["computed"] == pytest.approx(3_750_000.0)
    assert tax_info["base"] == pytest.approx(15_000_000.0)
    assert tax_info["rates"] == {"federal": 8.0, "regional": 17.0}


def test_net_profit_and_retained_agree_after_tax(closed):
    report = statements.check(closed, "2026")
    values = report["pnl"]["values"]
    assert values["2300"] == pytest.approx(15_000_000.0)
    assert values["2410"] == pytest.approx(3_750_000.0)
    assert values["2400"] == pytest.approx(11_250_000.0)
    assert year_close.account_balance(closed, "84.01", "2026-12-31") == Decimal("-11250000.00")


# ─────────────────────── 6. убыток ───────────────────────
def test_loss_means_no_tax_and_says_why(tmp_path):
    payload = read(SCENARIO)
    for row in payload["months"]:
        row["cost"] = row["net"] + 200_000
    scenario = tmp_path / "loss.json"
    scenario.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    state, _ = build(scenario)
    report = year_close.close_year(state, "2026")
    assert report["closed"], report["problems"]
    step = next(row for row in report["steps"] if row["id"] == "profit_tax")
    assert step["status"] == year_close.NOT_NEEDED
    assert "прибыли" in step["why"]
    assert not [row for row in state["entries"] if row.get("event") == "year.profit_tax"]
    assert year_close.account_balance(state, "68.04", "2026-12-31") == Decimal("0.00")


# ─────────────────────── 7. идемпотентность ───────────────────────
def test_second_close_does_not_post_tax_twice(closed):
    state = fresh(closed)
    before = len(state["entries"])
    year_close.close_year(state, "2026", decisions=read(DECISIONS))
    assert len(state["entries"]) == before
    taxes = [row for row in state["entries"] if row.get("event") == "year.profit_tax"]
    assert len(taxes) == 1
    assert year_close.account_balance(state, "68.04", "2026-12-31") == Decimal("-3750000.00")


# ─────────────────────── 8. незакрытый месяц ───────────────────────
def test_open_month_blocks_tax_not_silently_wrong():
    state, _ = build(SCENARIO)
    state["periods"] = [row for row in state["periods"] if row["period"] != "2026-11"]
    report = year_close.close_year(state, "2026", decisions=read(DECISIONS))
    assert not report["closed"]
    assert any(item["code"] == "MONTH_NOT_CLOSED" for item in report["problems"])
    assert not [row for row in state["entries"] if row.get("event") == "year.profit_tax"]


def test_tax_step_refuses_when_cost_is_not_closed():
    """Если 90.02 пуст, а затраты на 20.01 есть — налог от завышенной прибыли не проводится.

    Это не гипотетика: так выглядит незакрытый месяц. Налог «от выручки» был бы больше
    настоящего, поэтому шаг отказывается и говорит, что сначала делать.
    """
    state, _ = build(SCENARIO)
    for period in ("2026-11", "2026-12"):
        state["entries"] = [row for row in state["entries"]
                            if not (row.get("event") == "close.costs"
                                    and row.get("period") == period)]
    step = year_close.step_profit_tax(state, "2026", mode=ledger.MODE_POST,
                                      chart=ledger.load_chart(),
                                      rules=year_close.load_rules())
    assert step["status"] == year_close.NOT_DONE
    assert "не закрыт" in step["why"] or "90.02" in step["why"]
    assert step["amount"] is not None and step["amount"] > 0, \
        "сумма названа, но не проведена — иначе ошибку не видно"
    assert not [row for row in state["entries"] if row.get("event") == "year.profit_tax"]


def test_already_posted_tax_is_not_posted_again(closed):
    """Ключ идемпотентности: повтор шага не начисляет налог второй раз и не молчит об этом."""
    state = fresh(closed)
    step = year_close.step_profit_tax(state, "2026", mode=ledger.MODE_POST,
                                      chart=ledger.load_chart(),
                                      rules=year_close.load_rules())
    assert step["status"] == year_close.DONE
    assert "уже проведена" in step["why"]
    taxes = [row for row in state["entries"] if row.get("event") == "year.profit_tax"]
    assert len(taxes) == 1


# ─────────────────────── 9. год, а не сумма месяцев ───────────────────────
def test_year_period_is_supported_by_tax_module(closed):
    year_report = tax.profit_return(closed, "2026")
    assert year_report["totals"]["revenue"] == pytest.approx(55_000_000.0)
    assert year_report["totals"]["profit"] == pytest.approx(15_000_000.0)
    assert year_report["totals"]["tax"] == pytest.approx(3_750_000.0)
    assert tax.in_period("2026-05", "2026") and not tax.in_period("2025-12", "2026")
    assert tax.in_period("2026-05", "2026-05") and not tax.in_period("2026-06", "2026-05")


def test_months_sum_difference_is_named_not_hidden(closed):
    info = statements.profit_tax(closed, "2026")
    if abs(info["months_sum"] - info["computed"]) > 0.01:
        assert info["why_diff"], "расхождение суммы месяцев и года обязано быть объяснено"
    else:
        assert info["why_diff"] == ""


# ─────────────────────── 10–11. ОДК и ОДДС по данным ───────────────────────
def test_odk_and_odds_are_not_built_and_say_what_is_missing(closed):
    forms = {item["short"]: item for item in statements.readiness(closed, "2026")}
    assert set(forms) == {"ОДК", "ОДДС"}
    for item in forms.values():
        assert item["built"] is False
        assert item["missing"] and item["need"]
    assert any("80" in miss or "81" in miss or "82" in miss for miss in forms["ОДК"]["missing"])
    assert any("51" in miss for miss in forms["ОДДС"]["missing"])


def test_odds_readiness_changes_when_money_appears(closed):
    state = fresh(closed)
    result = ledger.post_entry(
        state, event="probe.money", date="2026-12-31",
        doc={"number": "выписка-проба", "date": "2026-12-31"},
        lines=[{"account": "51", "side": ledger.DEBIT, "amount": 1_000_000, "analytics": {}},
               {"account": "62.01", "side": ledger.CREDIT, "amount": 1_000_000,
                "analytics": {"контрагент": "ООО «Заказчик-Инвест»",
                              "договор": "Д-142/2026"}}],
        memo="проба: движение по расчётному счёту", rule="probe.money", key="probe.money",
        allow_closed=True, late_reason="проба в тесте: движение по 51")
    assert result["status"] == ledger.STATUS_OK, result["errors"]
    odds = {item["short"]: item for item in statements.readiness(state, "2026")}["ОДДС"]
    assert odds["built"] is False, "форма не реализована — и это не меняется от одной проводки"
    assert not any("оборотов за 2026 нет: 50, 51" in miss for miss in odds["missing"]), \
        "появилось движение по 51, а блок этого не заметил"
