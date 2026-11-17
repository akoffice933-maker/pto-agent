# -*- coding: utf-8 -*-
"""Сквозная проверка закрытия года — шаг CI (круг 41).

Что проверяется (не «код не упал», а «год закрыт правильно»):

1. год не закрывается при открытом месяце — и называется, какой именно открыт;
2. 12 закрытых месяцев дают результат года: 99 → 84 одной проводкой, 99 после неё нулевой;
3. повторное закрытие не создаёт вторую реформацию и не удваивает 84;
4. убыток идёт на 84.02 отдельной стороной, а не «минусом в прибыли»;
5. недостача без решения остаётся на 94; решение «на виновного» требует ФИО и основания;
6. оценочное обязательство не списывается в доходы «до нуля»: 96 переходит на следующий год;
7. РБП списывается частично, остаток переходит;
8. расходы года доходят до результата через перекрытие декабря, а не висят на 26/20.01;
9. проводки года в закрытом декабре помечены причиной, и журнал после них сходится;
10. сумма, перенесённая на 84, равна результату года, а не «примерно похожа».
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent import ledger, year_close                                # noqa: E402
from scripts.make_demo_year import build                               # noqa: E402

violations: list[str] = []
checks = {"done": 0}


def check(condition: bool, message: str) -> None:
    checks["done"] += 1
    if not condition:
        violations.append(message)


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


SCENARIO = ROOT / "data" / "demo" / "year_close" / "scenario_2026.json"
DECISIONS = ROOT / "data" / "demo" / "year_close" / "decisions_2026.json"
chart = ledger.load_chart()
rules = year_close.load_rules()
base_state, summary = build(SCENARIO)


def fresh() -> dict:
    return json.loads(json.dumps(base_state))


# ── 1. предпосылки ────────────────────────────────────────────────────────
check(all(row["closed"] for row in summary["months"]),
      f"не все демо-месяцы закрылись: {[r for r in summary['months'] if not r['closed']]}")
check(ledger.verify(base_state)["ok"], "журнал демо-года не сходится до закрытия года")

empty = ledger.new_state()
early = year_close.close_year(empty, "2026")
check(not early["closed"], "год закрыт на пустом журнале")
check(any(item["code"] == "MONTH_NOT_CLOSED" for item in early["problems"]),
      "открытые месяцы не помешали закрытию года")
check(empty["entries"] == [], "закрытие года на пустом журнале создало проводки")

partial = fresh()
partial["periods"] = [row for row in partial["periods"] if row["period"] != "2026-11"]
check(year_close.precheck(partial, "2026")["open_months"] == ["2026-11"],
      "открытый месяц не назван по имени")

# ── 2. реформация ─────────────────────────────────────────────────────────
state = fresh()
report = year_close.close_year(state, "2026", decisions=read(DECISIONS))
check(report["closed"], f"год не закрыт: {report['problems']}")
check(report["result"]["profit"] == 15_000_000,
      f"результат года {report['result']['profit']}, ожидалось 15 000 000")
check(report["result"]["revenue"] == 67_100_000,
      f"выручка {report['result']['revenue']}, ожидалось 67 100 000 (с НДС)")
check(report["result"]["vat_out"] == 12_100_000,
      f"НДС с реализации {report['result']['vat_out']}, ожидалось 12 100 000")
check(report["result"]["cost"] == 40_000_000,
      f"себестоимость {report['result']['cost']}, ожидалось 40 000 000 "
      f"(субподряд 38 600 000 + обязательство 1 240 000 + РБП 160 000)")
check(year_close.account_balance(state, "99", "2026-12-31", chart=chart) == Decimal("0.00"),
      "сальдо 99 после реформации не нулевое")
check(year_close.account_balance(state, "84", "2026-12-31", chart=chart)
      == Decimal("-15000000.00"),
      f"на 84 {year_close.account_balance(state, '84', '2026-12-31', chart=chart)}, "
      f"ожидалось −15 000 000 (кредитовое сальдо)")
check(year_close.account_balance(state, "84.02", "2026-12-31", chart=chart)
      == Decimal("0.00"), "при прибыли открылся 84.02 (убыток)")
reform = [row for row in state["entries"] if row["event"] == "year.reformation"]
check(len(reform) == 1, f"реформаций в журнале {len(reform)}, ожидалась одна")
check(ledger.verify(state)["ok"], "журнал не сходится после закрытия года")

# ── 3. повторный прогон ───────────────────────────────────────────────────
entries_before = len(state["entries"])
second = year_close.close_year(state, "2026", decisions=read(DECISIONS))
check(len(state["entries"]) == entries_before,
      f"повторное закрытие добавило {len(state['entries']) - entries_before} проводок")
check(year_close.account_balance(state, "84", "2026-12-31", chart=chart)
      == Decimal("-15000000.00"), "повторное закрытие удвоило 84")

# ── 4. убыток ─────────────────────────────────────────────────────────────
loss_payload = read(SCENARIO)
for row in loss_payload["months"]:
    row["cost"] = row["net"] + 200_000
loss_path = ROOT / "out" / "year_close" / "scenario_loss.json"
loss_path.parent.mkdir(parents=True, exist_ok=True)
loss_path.write_text(json.dumps(loss_payload, ensure_ascii=False), encoding="utf-8")
loss_state, _ = build(loss_path)
loss_report = year_close.close_year(loss_state, "2026")
check(loss_report["closed"], f"год с убытком не закрылся: {loss_report['problems']}")
check(loss_report["result"]["profit"] < 0, "убыток показан как прибыль")
check(year_close.account_balance(loss_state, "84.02", "2026-12-31", chart=chart)
      == Decimal("3640000.00"),
      f"убыток на 84.02 {year_close.account_balance(loss_state, '84.02', '2026-12-31', chart=chart)}, "
      f"ожидалось 3 640 000")
check(year_close.account_balance(loss_state, "84.01", "2026-12-31", chart=chart)
      == Decimal("0.00"), "при убытке открылся 84.01")

# ── 5. недостачи ──────────────────────────────────────────────────────────
no_decisions = fresh()
report_none = year_close.close_year(no_decisions, "2026")
check(report_none["closed"], "год не закрылся без решений по 94/96/97")
check(year_close.account_balance(no_decisions, "94", "2026-12-31", chart=chart)
      == Decimal("148500.00"),
      "недостача списалась сама: программа решила, кто виноват")
short_step = next(row for row in report_none["steps"] if row["id"] == "shortages")
check(short_step["status"] == year_close.NEEDS_DECISION,
      f"недостача без решения получила статус {short_step['status']}")
check(short_step["requests"], "недостача без решения не дала запроса к человеку")

victim_no_person = fresh()
step = year_close.step_shortages(
    victim_no_person, "2026",
    decisions={"shortages": [{"склад": "Центральный склад", "decision": "victim",
                              "basis": {"number": "приказ №1", "date": "2026-12-20"}}]},
    mode=ledger.MODE_POST, chart=chart, rules=rules)
check(step["status"] == year_close.NOT_DONE,
      "недостача на виновного прошла без ФИО")
check(year_close.account_balance(victim_no_person, "94", "2026-12-31", chart=chart)
      == Decimal("148500.00"), "проводка по недостаче создана без виновного")

over = fresh()
step = year_close.step_shortages(
    over, "2026",
    decisions={"shortages": [{"склад": "Центральный склад", "decision": "other",
                              "amount": 200_000,
                              "basis": {"number": "приказ №1", "date": "2026-12-20"}}]},
    mode=ledger.MODE_POST, chart=chart, rules=rules)
check(step["status"] == year_close.NOT_DONE, "списание недостачи больше сальдо прошло")

check(year_close.account_balance(state, "73.02", "2026-12-31", chart=chart)
      == Decimal("148500.00"),
      "недостача по решению не встала на 73.02")
shortage_entry = next(row for row in state["entries"] if row["event"] == "year.shortages")
analytics = {line["account"]: line["analytics"] for line in shortage_entry["lines"]}
check(analytics.get("73.02", {}).get("сотрудник") == "Гусев А.В.",
      "в проводке по недостаче нет виновного")

# ── 6. оценочные обязательства ────────────────────────────────────────────
check(year_close.account_balance(no_decisions, "96", "2026-12-31", chart=chart)
      == Decimal("-1240000.00"),
      "оценочное обязательство списано в доходы без основания")
check(year_close.account_balance(no_decisions, "91.01", "2026-12-31", chart=chart)
      == Decimal("0.00"), "без основания возник прочий доход")
prov_step = next(row for row in report_none["steps"] if row["id"] == "provisions")
check(prov_step["status"] == year_close.NEEDS_DECISION,
      f"статус 96 без подтверждения — {prov_step['status']}")

released = fresh()
rel_report = year_close.close_year(
    released, "2026",
    decisions={"provisions": [{"вид": "резерв на оплату отпусков", "status": "released",
                               "basis": {"number": "акт сверки №9", "date": "2026-12-31"}}]})
check(year_close.account_balance(released, "96", "2026-12-31", chart=chart) == Decimal("0.00"),
      "прекращённое обязательство осталось на 96")
check(rel_report["result"]["profit"] - report_none["result"]["profit"] == 1_240_000,
      f"списание обязательства в доходы не изменило результат на 1 240 000: "
      f"{rel_report['result']['profit']} против {report_none['result']['profit']}")

# ── 7. РБП ────────────────────────────────────────────────────────────────
check(year_close.account_balance(state, "97", "2026-12-31", chart=chart)
      == Decimal("800000.00"),
      f"остаток 97 {year_close.account_balance(state, '97', '2026-12-31', chart=chart)}, "
      f"ожидалось 800 000 (960 000 − 160 000)")
check(year_close.account_balance(no_decisions, "97", "2026-12-31", chart=chart)
      == Decimal("960000.00"), "РБП списаны без графика")

# ── 8. перекрытие декабря ─────────────────────────────────────────────────
check(year_close.account_balance(state, "26", "2026-12-31", chart=chart) == Decimal("0.00"),
      "счёт 26 остался с сальдо после закрытия года")
check(year_close.account_balance(state, "20.01", "2026-12-31", chart=chart) == Decimal("0.00"),
      "счёт 20.01 остался с сальдо после закрытия года")
no_rbp = fresh()
decisions_no_rbp = read(DECISIONS)
decisions_no_rbp["future_costs"] = [{"вид": "страхование СМР", "amount": 0}]
no_rbp_report = year_close.close_year(no_rbp, "2026", decisions=decisions_no_rbp)
check(no_rbp_report["result"]["profit"] - report["result"]["profit"] == 160_000,
      "списание РБП не дошло до результата года")

# ── 9. проводки в закрытом декабре ────────────────────────────────────────
late = [row for row in state["entries"] if row.get("late")]
check(len(late) >= 4, f"проводок года без пометки «поздняя» — {len(late)}")
check(all(row["late"]["period"] == "2026-12" and row["late"]["reason"] for row in late),
      "у проводки в закрытом периоде нет периода или причины")
probe = fresh()
attempt = ledger.post_entry(
    probe, event="year.reformation", date="2026-12-31",
    doc={"number": "проба", "date": "2026-12-31", "kind": "closing"},
    lines=[{"account": "99", "side": ledger.DEBIT, "amount": 1, "analytics": {}},
           {"account": "84.01", "side": ledger.CREDIT, "amount": 1, "analytics": {}}],
    rule="year.reformation", key="проба|1", allow_closed=True)
check(attempt["errors"] and attempt["errors"][0]["code"] == "LATE_WITHOUT_REASON",
      "журнал принял проводку в закрытом периоде без причины")
check(ledger.verify(state)["ok"] and ledger.trial_balance(state, "2026-12")["balanced"],
      "после закрытия года ОСВ не сходится")

# ── 10. суммы ─────────────────────────────────────────────────────────────
moved = sum(ledger.kopeck(line["amount"]) for line in reform[0]["lines"]
            if line["account"] == "99")
check(moved == Decimal("15000000.00"),
      f"на 84 перенесено {moved}, а результат года 15 000 000")
check(year_close.account_balance(state, "84", "2026-12-31", chart=chart) == -moved,
      "перенос на 84 не равен результату года")
text = year_close.screen_text(report)
check("не сделано" in text.lower() and "годовая бухгалтерская отчётность" in text,
      "на экране закрытия года нет списка «чего нет»")
markdown = year_close.to_markdown(report)
check("| Шаг | Статус |" in markdown and "Реформация баланса" in markdown,
      "markdown закрытия года не содержит шагов")

# ── итог ──────────────────────────────────────────────────────────────────
print(f"закрытие года 2026: месяцев закрыто {len(summary['months'])} · "
      f"результат {report['result']['profit']:,.2f} ₽ · на 84 "
      f"{year_close.account_balance(state, '84', '2026-12-31', chart=chart):,.2f} ₽ · "
      f"96 {year_close.account_balance(state, '96', '2026-12-31', chart=chart):,.2f} ₽ · "
      f"97 {year_close.account_balance(state, '97', '2026-12-31', chart=chart):,.2f} ₽")
print(f"убыточный вариант: убыток {-loss_report['result']['profit']:,.2f} ₽ на 84.02")
print(f"проверок: {checks['done']}")
print("нарушений: 0" if not violations else "НАРУШЕНИЯ:")
for row in violations:
    print(f" ✗ {row}")
sys.exit(1 if violations else 0)
