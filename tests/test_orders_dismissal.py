# -*- coding: utf-8 -*-
"""Увольнение: компенсация за неиспользованный отпуск по Правилам № 169.

Стаж считается месяцами, а не «на глазок»: излишек меньше 15 дней отбрасывается, 15 и
более — округляются (п. 35), 11 месяцев в рабочем году дают полные 28 дней (п. 28).
Каждая граница проверяется отдельным тестом, потому что именно на границе такие расчёты
и ломаются — а цена ошибки в компенсации выше, чем кажется.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pto_agent import orders, payroll

REPO = Path(__file__).resolve().parents[1]
RULES = payroll.load_rules()
STAFF = payroll.load_json(REPO / "data/demo/payroll/staff.json")
EMPLOYEE = {"tab": "900", "name": "Тестов Тест", "position": "тест", "hired": "2026-01-12"}


def dismissal(**extra) -> dict:
    payload = {"number": "П-1", "kind": "dismissal", "tab": "900",
               "dismissal_date": "2026-03-09", "hired": "2026-01-12",
               "vacation_days_taken": 0, "base_12m": 1200000}
    payload.update(extra)
    return orders.dismissal(payload, EMPLOYEE, RULES, period="2026-03")


# ─────────────────────────── стаж ───────────────────────────
@pytest.mark.parametrize("hired,dismissal_date,months", [
    ("2026-01-12", "2026-02-11", 1),    # месяц от 12.01 по 11.02: излишек 30 дней округлён
    ("2026-01-12", "2026-02-12", 1),    # ровно месяц
    ("2026-01-12", "2026-02-26", 1),    # излишек 14 дней — отбрасывается
    ("2026-01-12", "2026-02-27", 2),    # излишек 15 дней — округляется
    ("2026-01-12", "2026-11-20", 10),
    ("2026-01-12", "2026-11-27", 11),
    ("2026-01-12", "2027-01-12", 12),
])
def test_seniority_months_follow_rule_35(hired, dismissal_date, months):
    assert orders.seniority_months(hired, dismissal_date)["months"] == months


def test_days_outside_seniority_are_subtracted():
    """Отпуск без сохранения зарплаты свыше 14 дней не входит в стаж (ст. 121 ТК РФ)."""
    plain = orders.seniority_months("2026-01-12", "2026-05-20")["months"]
    reduced = orders.seniority_months("2026-01-12", "2026-05-20", excluded_days=60)["months"]
    assert plain == 4
    assert reduced == 2


def test_dismissal_before_hire_gives_no_seniority():
    info = orders.seniority_months("2026-05-05", "2026-04-01")
    assert info["months"] == 0


# ─────────────────────────── дни и деньги ───────────────────────────
def test_six_months_give_fourteen_days():
    row = dismissal(dismissal_date="2026-07-20")
    assert row["months"] == 6
    assert row["earned_days"] == 14.0     # 6 × 2,3333


def test_eleven_months_give_full_twenty_eight_days():
    row = dismissal(dismissal_date="2026-12-15")
    assert row["months"] == 11
    assert row["earned_days"] == 28.0
    assert "п. 28" in row["days_rule"]


def test_taken_days_reduce_compensation():
    row = dismissal(dismissal_date="2026-07-20", vacation_days_taken=14)
    assert row["earned_days"] == 14.0
    assert row["days"] == 0.0
    assert row["amount"] == 0.0


def test_more_taken_than_earned_is_overpayment_not_zero():
    row = dismissal(dismissal_date="2026-03-09", vacation_days_taken=10)
    assert any(item["code"] == "DISMISSAL_OVERPAID" for item in row["errors"])
    assert row["amount"] == 0.0
    assert row.get("not_calculated")


def test_sdz_is_vacation_formula():
    row = dismissal(dismissal_date="2026-07-20")
    assert row["sdz"] == pytest.approx(1200000 / 12 / 29.3, abs=0.01)
    assert row["amount"] == pytest.approx(row["sdz"] * 14, abs=0.01)


def test_sdz_can_be_given_directly():
    row = dismissal(dismissal_date="2026-07-20", sdz=2000, base_12m=None)
    assert row["sdz"] == 2000.0
    assert row["amount"] == pytest.approx(28000.0, abs=0.01)


def test_sdz_is_raised_to_mrot_floor():
    row = dismissal(dismissal_date="2026-07-20", base_12m=50000)
    floor = RULES["mrot"] / 29.3
    assert row["sdz"] == pytest.approx(floor, abs=0.01)
    assert row["raised_to_min"]


def test_missing_base_is_refused_not_guessed():
    row = dismissal(base_12m=None)
    assert any(item["code"] == "DISMISSAL_NO_BASE" for item in row["errors"])


def test_missing_hire_date_is_refused():
    row = orders.dismissal({"number": "П-2", "kind": "dismissal", "tab": "900",
                            "dismissal_date": "2026-03-09", "base_12m": 1000000},
                           {"tab": "900"}, RULES, period="2026-03")
    assert any(item["code"] == "DISMISSAL_NO_HIRED" for item in row["errors"])


def test_unknown_taken_days_are_flagged_for_review():
    row = dismissal(dismissal_date="2026-07-20", vacation_days_taken=None)
    assert row["verify"]
    assert any("отгуляно" in text for text in row["warnings"])


def test_compensation_is_taxable_and_insurable():
    row = dismissal(dismissal_date="2026-07-20")
    assert row["taxable"] == row["amount"]
    assert row["contrib_base"] == row["amount"], "компенсация входит в базу по взносам"


def test_kind_is_registered():
    assert "dismissal" in orders.KINDS


# ─────────────────────────── в составе приказов месяца ───────────────────────────
def test_dismissal_goes_through_month_report():
    parsed = [{"number": "П-3", "date": "2026-07-20", "kind": "dismissal", "tab": "005",
               "dismissal_date": "2026-07-20", "hired": "2026-05-05",
               "vacation_days_taken": 0, "base_12m": 184000}]
    result = orders.report(parsed, STAFF, RULES, period="2026-07")
    assert result["totals"]["dismissal"] > 0
    assert result["lines"][0]["kind"] == "dismissal"


def test_demo_order_is_calculated():
    parsed = orders.load_orders(REPO / "data/demo/payroll/orders_2026_10.json")
    result = orders.report(parsed, STAFF, RULES, period="2026-10")
    line = next(item for item in result["lines"] if item["kind"] == "dismissal")
    assert line["months"] == 6
    assert line["amount"] > 0
    # ошибка есть только у намеренно неполного больничного П-35; у увольнения — нет
    assert not [item for item in result["errors"] if "П-36" in str(item.get("what"))]


def test_not_included_no_longer_claims_compensation_is_missing():
    """«Не включено» не должно перечислять то, что уже считается.

    Круг 37 держал здесь «декретные» как незакрытый пункт. В круге 42 добавлено пособие
    по уходу до 1,5 лет, поэтому список переписан: проверяем обратное — что реализованное
    больше не выдаётся за отсутствующее (иначе экран врёт про собственные возможности).
    """
    texts = " ".join(item["what"] for item in orders.NOT_INCLUDED).lower()
    assert "компенсация за неиспользованный отпуск" not in texts
    assert "декретные" not in texts
    assert "уход" not in texts or "единое пособие" in texts
    # а то, что действительно не сделано, остаётся в списке с причиной
    assert "районный коэффициент" in texts
    kinds = set(orders.KINDS)
    assert {"childcare", "retention", "maternity", "severance"} <= kinds
