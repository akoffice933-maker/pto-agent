# -*- coding: utf-8 -*-
"""Сквозная проверка зарплаты — шаг CI.

Что проверяется (не «код не упал»):

1. НДФЛ по прогрессии 13/15/18/20/22 % на границах ступеней: повышенная ставка — только
   к превышению, а не ко всему доходу;
2. вычеты на детей: 1 400/2 800/6 000, двойной единственному родителю, отсечение на 450 000 ₽;
3. НДФЛ за месяц = налог с начала года − удержанный ранее (нарастающий итог);
4. взносы: 30 % до 2 979 000 ₽ и 15,1 % сверх, по человеку; травматизм 2,1 % без предела;
5. табель без нормы дня месяца не принимается, переработка не оплачивается молча;
6. оклад делится на норму дней: недоделанные дни уменьшают начисление;
7. проводки: 20.01 ← 70/69.01/69.11 и 70 → 68.01, журнал сходится, повтор — дубль;
8. ведомость: сумма строк = итог, начислено − НДФЛ = к выплате;
9. заявки на выплату идут через казначейство со статьёй «зарплата» и основанием Т-51.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent import ledger, payroll                                          # noqa: E402

STAFF = ROOT / "data" / "demo" / "payroll" / "staff.json"
violations: list[str] = []
stats = {"rows": 0, "months": 0, "entries": 0, "checks": 0}


def check(condition: bool, message: str) -> None:
    stats["checks"] += 1
    if not condition:
        violations.append(message)


rules = payroll.load_rules()
staff = json.loads(STAFF.read_text(encoding="utf-8"))
state = payroll.new_state(object_name=staff["object"])
ledger_state = ledger.new_state(object_name=staff["object"])

# ── 1. прогрессия ────────────────────────────────────────────────────────────
for base, expected in ((2_400_000, 312_000), (2_600_000, 342_000), (5_000_000, 702_000),
                       (20_000_000, 3_402_000), (50_000_000, 9_402_000),
                       (51_000_000, 9_622_000)):
    got = float(payroll.ndfl_scale(rules, Decimal(str(base)))["tax"])
    check(abs(got - expected) < 0.01, f"НДФЛ с {base:,} = {got}, ожидали {expected:,}")
naive = 2_600_000 * 0.15
real = float(payroll.ndfl_scale(rules, Decimal("2600000"))["tax"])
check(abs((naive - real) - 48_000) < 0.01,
      f"прогрессия посчитана «на всё»: разница {naive - real}, а должна быть 48 000")

# ── 2. вычеты ───────────────────────────────────────────────────────────────
for children, expected in ((1, 1_400), (2, 4_200), (3, 10_200), (4, 16_200)):
    got = float(payroll.deductions_of(rules, {"children": children}, Decimal("0"))["amount"])
    check(abs(got - expected) < 0.01, f"вычет на {children} детей = {got}, ожидали {expected}")
solo = float(payroll.deductions_of(rules, {"children": 1, "sole_parent": True},
                                   Decimal("0"))["amount"])
check(abs(solo - 2_800) < 0.01, f"двойной вычет единственному родителю = {solo}")
cut = payroll.deductions_of(rules, {"children": 2}, Decimal("450000"))
check(float(cut["amount"]) == 0, "вычет применён при доходе 450 000 ₽ — порог не сработал")

# ── 3. НДФЛ нарастающим итогом ──────────────────────────────────────────────
person = {"children": 0, "resident": True}
first = payroll.ndfl_for_month(rules, employee=person, income_month=Decimal("200000"),
                               income_ytd_before=Decimal("0"), withheld_ytd_before=Decimal("0"),
                               deductions_ytd_before=Decimal("0"))
second = payroll.ndfl_for_month(rules, employee=person, income_month=Decimal("200000"),
                                income_ytd_before=Decimal("2300000"),
                                withheld_ytd_before=Decimal("299000"),
                                deductions_ytd_before=Decimal("0"))
check(float(first["tax"]) == 26_000, f"НДФЛ за месяц без истории = {first['tax']}")
check(float(second["tax"]) == 28_000, f"НДФЛ в месяц перехода порога = {second['tax']}, "
                                      f"ожидали 28 000 (327 000 − 299 000)")

# ── 4. взносы ───────────────────────────────────────────────────────────────
plain = payroll.contributions_for_month(rules, income_month=Decimal("100000"),
                                        base_ytd_before=Decimal("0"))
check(float(plain["total"]) == 30_000, f"взносы с 100 000 = {plain['total']}")
edge = payroll.contributions_for_month(rules, income_month=Decimal("100000"),
                                       base_ytd_before=Decimal("2950000"))
check(abs(float(edge["total"]) - 19_421) < 0.01,
      f"взносы на границе базы = {edge['total']}, ожидали 19 421")
check(abs(float(edge["injury"]) - 2_100) < 0.01, "травматизм посчитан не со всей суммы")
over = payroll.contributions_for_month(rules, income_month=Decimal("100000"),
                                       base_ytd_before=Decimal("4000000"))
check(abs(float(over["total"]) - 15_100) < 0.01, f"сверх базы взносы = {over['total']}")

# ── 5–6. табель и начисление по двум месяцам ────────────────────────────────
for period in ("2026-09", "2026-10"):
    file = ROOT / "data" / "demo" / "payroll" / f"timesheet_{period.replace('-', '_')}.json"
    timesheet = json.loads(file.read_text(encoding="utf-8"))
    broken = dict(timesheet)
    broken["norm"] = {}
    _, errors, _ = payroll.check_timesheet(broken, staff, rules)
    check(any(item["code"] == "NO_NORM" for item in errors),
          f"{period}: табель без нормы принят")
    overtime = dict(timesheet)
    overtime["rows"] = [dict(row) for row in overtime["rows"]]
    overtime["rows"][0]["worked_days"] = 25
    _, errors, _ = payroll.check_timesheet(overtime, staff, rules)
    check(any(item["code"] == "OVER_NORM" for item in errors),
          f"{period}: переработка не отмечена")

    report = payroll.accrue(state, timesheet, staff, rules)
    check(not report["errors"], f"{period}: начисление не прошло: {report['errors'][:1]}")
    payroll.remember(state, report, rules=rules)
    stats["months"] += 1
    stats["rows"] += len(report["rows"])
    partial = next((row for row in report["rows"] if row["worked_days"] < row["norm_days"]), None)
    if partial:
        expected_pay = partial["salary"] * partial["worked_days"] / partial["norm_days"]
        check(abs(partial["accrued"] - expected_pay) < 0.02,
              f"{period}: {partial['name']} начислено {partial['accrued']} при "
              f"{partial['worked_days']}/{partial['norm_days']} дн.")
        check(partial["accrued"] < partial["salary"],
              f"{period}: недоделанные дни не уменьшили начисление")

october = payroll.accrue(state, json.loads(
    (ROOT / "data/demo/payroll/timesheet_2026_10.json").read_text(encoding="utf-8")),
    staff, rules)
result = payroll.post_ledger(ledger_state, october)
check(result["posted"] == 4 and result["rejected"] == 0,
      f"проводки по зарплате: создано {result['posted']}, отказано {result['rejected']}")
stats["entries"] = len(ledger_state["entries"])
summary = ledger.verify(ledger_state)
check(summary["ok"], f"журнал не сходится после проводок по зарплате: {summary['summary']}")


def saldo(account: str, date: str = "2026-10-31") -> Decimal:
    return sum(ledger.balance_as_of(ledger_state, account, date).values(), Decimal("0"))


totals = october["totals"]
check(abs(float(saldo("70")) + totals["net"]) < 0.02,
      f"сальдо 70 = {saldo('70')}, к выплате {totals['net']}")
check(abs(float(saldo("68.01")) + totals["ndfl"]) < 0.02, "НДФЛ не осел на 68.01")
check(abs(float(saldo("69.01")) + totals["contrib"]) < 0.02, "взносы не осели на 69.01")
check(abs(float(saldo("69.11")) + totals["injury"]) < 0.02, "травматизм не осел на 69.11")
check(abs(float(saldo("20.01")) - totals["cost"]) < 0.02,
      f"20.01 = {saldo('20.01')}, стоимость {totals['cost']}")

again = payroll.post_ledger(ledger_state, october)
check(again["posted"] == 0 and again["duplicates"] == 4, "повторное проведение создало дубль")

# ── 8. ведомость ────────────────────────────────────────────────────────────
verify = payroll.verify(state)
check(verify["ok"], f"ведомости не сходятся: {verify['summary']}")
for row in october["rows"]:
    check(abs(row["accrued"] - row["ndfl"] - row["net"]) < 0.02,
          f"{row['name']}: начислено − НДФЛ ≠ к выплате")
check(abs(totals["cost"] - (totals["accrued"] + totals["contrib"] + totals["injury"])) < 0.02,
      "стоимость для организации ≠ начислено + взносы + травматизм")

# ── 9. заявки на выплату ────────────────────────────────────────────────────
requests = payroll.payout_requests(october, date="2026-11-05")
check(len(requests) == len(october["rows"]), "заявок не по числу сотрудников")
check(all(item["article"] == "зарплата" for item in requests), "статья заявки не «зарплата»")
check(all(item["basis"]["number"].startswith("Т-51") for item in requests),
      "у заявки на зарплату нет основания Т-51")
check(abs(sum(item["amount"] for item in requests) - totals["net"]) < 0.05,
      "сумма заявок ≠ сумма к выплате")

# ── 10. выплата через казначейство: маршрут, Дт 70 Кт 51, БДР ───────────────
from pto_agent import treasury                                               # noqa: E402

cash = treasury.new_state(object_name=staff["object"],
                          opening={"date": "2026-11-01", "amount": 2_000_000,
                                   "basis": "остаток на расчётном счёте"})
for request in requests:
    added = treasury.add_request(cash, request)
    check(not added["errors"], f"{request['number']}: заявка не принята: "
                              f"{[item['code'] for item in added['errors']]}")
route = treasury.find(cash, requests[0]["number"])["route"]
check(route == ["руководитель"],
      f"маршрут зарплаты {route}: зарплату не должно согласовывать снабжение")
for request in requests:
    approved = treasury.approve(cash, request["number"], role="руководитель",
                                person="Директоров Д.Д.", date="2026-11-05")
    check(approved["status"] == "согласована",
          f"{request['number']}: согласование «{approved['status']}»")
paid_money = Decimal("0")
for request in requests:
    paid = treasury.pay(cash, request["number"], date="2026-11-05",
                        person="Бухгалтерова М.П.")
    check(not paid["errors"], f"{request['number']}: оплата не прошла: "
                              f"{[item['code'] for item in paid['errors']]}")
    paid_money += Decimal(str(request["amount"]))
check(abs(float(paid_money) - totals["net"]) < 0.05,
      f"оплачено {paid_money}, а по ведомости к выплате {totals['net']}")
check(payroll.post_ledger(ledger_state, october)["duplicates"] == 4,
      "начисление задвоилось при повторном вызове")
posted = treasury.post_to_ledger(cash, ledger_state)
check(posted["rejected"] == 0, f"выплата не проведена: {posted['errors'][:1]}")
check(abs(float(saldo("70", "2026-11-30"))) < 0.02,
      f"после выплаты 70 не закрылся: {saldo('70', '2026-11-30')} "
      f"(к выплате {totals['net']})")
check(abs(float(saldo("51", "2026-11-30")) - float(2_000_000 - paid_money)) < 0.02,
      f"на 51 осталось {saldo('51', '2026-11-30')}, ожидали {2_000_000 - paid_money}")
check(ledger.verify(ledger_state)["ok"], "журнал не сходится после выплаты зарплаты")
bdr = treasury.bdr(cash, ledger_state, "2026-10")
rows = {row["article"]: row for row in bdr["rows"]}
check("зарплата" in rows and abs(rows["зарплата"]["fact"] - totals["accrued"]) < 0.02,
      "зарплата не попала в БДР отдельной статьёй")
check("взносы" in rows and abs(rows["взносы"]["fact"] - totals["contrib"]
                              - totals["injury"]) < 0.02,
      "взносы не попали в БДР отдельной статьёй")
check(bdr["unallocated_total"] == 0,
      f"в БДР есть неразнесённые суммы: {bdr['unallocated_total']}")
check(abs(bdr["expense_fact"] - totals["cost"]) < 0.02,
      f"расход БДР {bdr['expense_fact']} ≠ стоимость зарплаты {totals['cost']}")

# ── 11. приказы: больничные, отпускные, премии ──────────────────────────────
from pto_agent import orders as orders_mod                                     # noqa: E402

parsed = orders_mod.load_orders(ROOT / "data" / "demo" / "payroll" / "orders_2026_10.json")
with_orders = payroll.new_state(object_name=staff["object"])
extra = payroll.accrue(with_orders, json.loads((ROOT / "data/demo/payroll"
                                                / "timesheet_2026_10.json")
                                                .read_text(encoding="utf-8")),
                       staff, rules, orders=parsed)
by_tab = {row["tab"]: row for row in extra["rows"]}
check(extra["totals"]["sick_employer"] == 8136.99,
      f"больничные за счёт работодателя {extra['totals']['sick_employer']}, ожидали 8 136,99")
check(extra["totals"]["sick_fund"] == 13643.81,
      f"часть СФР {extra['totals']['sick_fund']}, ожидали 13 643,81")
check(extra["totals"]["vacation"] == 56764.54,
      f"отпускные {extra['totals']['vacation']}, ожидали 56 764,54")
check(extra["totals"]["bonus"] == 45000.0, f"премия {extra['totals']['bonus']}, ожидали 45 000")
check(by_tab["004"]["contrib_base"] == 88000.0,
      "пособие попало в базу по взносам: с больничного взносы не начисляются")
check(by_tab["004"]["accrued"] == 88000 + 8136.99,
      f"начисление по 004 = {by_tab['004']['accrued']}, ожидали оклад + пособие")
check(any("П-35/2026" in item["what"] for item in extra.get("order_errors") or []),
      "приказ без заработка за два года не отмечен как недосчитанный")
check(extra["totals"]["accrued"] == round(totals["accrued"] + 8136.99 + 56764.54 + 45000
                                          + extra["totals"]["dismissal"], 2),
      f"итог с приказами {extra['totals']['accrued']} ≠ оклад + приказы")

# компенсация при увольнении: дни по стажу (п. 28 и 35 Правил № 169), а не «2,33 за месяц»
dismissal_row = next((row for row in extra["orders"]["lines"] if row["kind"] == "dismissal"),
                     None)
check(dismissal_row is not None, "приказ об увольнении не посчитан")
if dismissal_row:
    months = orders_mod.seniority_months("2026-05-05", "2026-10-30")["months"]
    check(months == 6, f"стаж по Правилам № 169 = {months} мес., ожидали 6 (излишек 25 дн.)")
    check(dismissal_row["earned_days"] == 14.0,
          f"заработано дней {dismissal_row['earned_days']}, ожидали 14 (6 × 2,3333)")
    check(round(dismissal_row["sdz"], 2) == round(552000 / 12 / 29.3, 2),
          f"СДЗ {dismissal_row['sdz']} ≠ база за 12 месяцев / 12 / 29,3")
    check(dismissal_row["amount"] == round(dismissal_row["sdz"] * 14, 2),
          "сумма компенсации ≠ дни × СДЗ")
    check(dismissal_row["contrib_base"] == dismissal_row["amount"],
          "компенсация не попала в базу по взносам: она облагается")
    check(orders_mod.seniority_months("2026-01-12", "2026-11-20")["months"] == 10,
          "излишек меньше 15 дней не отброшен")
    check(orders_mod.seniority_months("2026-01-12", "2026-11-27")["months"] == 11,
          "излишек 15+ дней не округлён до полного месяца")
    eleven = orders_mod.dismissal({"number": "П-99", "kind": "dismissal", "tab": "001",
                                   "dismissal_date": "2026-11-27", "hired": "2025-12-20",
                                   "vacation_days_taken": 0, "base_12m": 1200000},
                                  {"hired": "2025-12-20"}, rules, period="2026-11")
    check(eleven["months"] == 11 and eleven["earned_days"] == 28.0,
          f"11 месяцев стажа не дали полную компенсацию: {eleven['months']} мес., "
          f"{eleven['earned_days']} дн.")
    short = orders_mod.dismissal({"number": "П-98", "kind": "dismissal", "tab": "001",
                                  "dismissal_date": "2026-03-10", "hired": "2026-01-10",
                                  "vacation_days_taken": 10, "base_12m": 1200000},
                                 {"hired": "2026-01-10"}, rules, period="2026-03")
    check(any(item["code"] == "DISMISSAL_OVERPAID" for item in short["errors"]),
          "отгулянные сверх заработанного дни не отмечены как переплата")

undefined = orders_mod.report(parsed, staff, rules, period="2026-11")
check(undefined["lines"] == [], "приказы октября посчитаны в ноябре")
check(all(gap["why"] == "относится к другому месяцу" for gap in undefined["gaps"]),
      "перенос приказа в другой месяц не назван причиной")
check(orders_mod.report([], staff, rules, period="2026-10")["gaps"][0]["what"] == "приказы за месяц",
      "пустой файл приказов не отмечен как «нет данных»")

# ── 12. ноябрь: декрет и увольнение по сокращению ──────────────────────────
nov_state = payroll.new_state(object_name=staff["object"])
nov_ts = json.loads((ROOT / "data/demo/payroll" / "timesheet_2026_11.json").read_text(encoding="utf-8"))
nov_orders = orders_mod.load_orders(ROOT / "data/demo/payroll" / "orders_2026_11.json")
nov = payroll.accrue(nov_state, nov_ts, staff, rules, period="2026-11", orders=nov_orders)
nov_rows = {row["tab"]: row for row in nov["rows"]}
stats["months"] += 1
check(not nov["errors"] and not nov.get("order_errors"),
      f"ноябрь не посчитан: {nov['errors'][:1]} {nov.get('order_errors', [])[:1]}")
check(nov["totals"]["maternity_fund"] == 390422.2,
      f"декретные {nov['totals']['maternity_fund']}, ожидали 390 422,20")
check(nov_rows["006"]["extra"] == 0.0 and nov_rows["006"]["ndfl"] == 0.0,
      "декретные попали в доход для НДФЛ: п. 1 ст. 217 НК РФ говорит обратное")
check(nov_rows["006"]["contrib"] == 0.0, "с декретных начислены взносы")
check(nov_rows["006"]["net"] == 0.0, "сотрудница в декрете получила «к выплате» из начисления")
check(nov_rows["006"]["maternity_fund"] == 390422.2,
      "декретные не показаны как сумма СФР")
check(nov["totals"]["severance"] == 104975.0,
      f"выходное пособие {nov['totals']['severance']}, ожидали 104 975,00")
check(nov_rows["003"]["tax_free"] == 104975.0,
      "выходное пособие в пределах 3× попало в облагаемый доход")
check(nov_rows["003"]["extra"] == 101535.7,
      f"в доход для НДФЛ попало {nov_rows['003']['extra']}, ожидали только компенсацию "
      f"за отпуск 101 535,70 без выходного пособия")
check(abs(nov_rows["003"]["net"] - (nov_rows["003"]["accrued"] - nov_rows["003"]["ndfl"])) < 0.02,
      "выходное пособие не вошло в сумму к выплате")
check(nov["totals"]["tax_free"] == 104975.0, "необлагаемая часть не вынесена в итоги")
check(payroll.verify({"periods": [{"period": "2026-11", "rows": nov["rows"],
                                   "totals": nov["totals"]}]})["ok"],
      "ноябрьская ведомость не сходится со строками")

mat_line = next(row for row in nov["orders"]["lines"] if row["kind"] == "maternity")
check(mat_line["days"] == 140, f"дни декрета {mat_line['days']}, ожидали 140")
check(abs(mat_line["sdz"] - 1980000 / 710) < 0.01,
      f"СДЗ декрета {mat_line['sdz']}: замена лет и исключённые дни не применены")
check("замена" in mat_line["sdz_source"], "источник СДЗ не называет замену лет")
sev_line = next(row for row in nov["orders"]["lines"] if row["kind"] == "severance")
check(abs(sev_line["monthly"] - 5100 * 247 / 12) < 0.02,
      f"средний месячный заработок {sev_line['monthly']} ≠ СДЗ × 247/12")
check(abs(sev_line["amount"] - 5100 * 20 * 1) > 0.5,
      "выходное пособие посчитано по дням месяца, а не по среднему за год")
dis_line = next(row for row in nov["orders"]["lines"] if row["kind"] == "dismissal")
check(dis_line["months"] == 21 and dis_line["days"] == 35.0,
      f"стаж {dis_line['months']} мес. / дни {dis_line['days']}, ожидали 21 мес. и 35 дн.")

fund = payroll.fund_report(state, rules=rules)
print(f"зарплата: месяцев {stats['months']} · строк {stats['rows']} · проводок "
      f"{stats['entries']} · проверок {stats['checks']}")
print(f"2026-10: начислено {totals['accrued']:,.2f} ₽ · НДФЛ {totals['ndfl']:,.2f} ₽ · "
      f"взносы {totals['contrib']:,.2f} ₽ · травматизм {totals['injury']:,.2f} ₽ · "
      f"к выплате {totals['net']:,.2f} ₽")
print(f"фонд по месяцам: {[row['period'] + ' ' + format(row['accrued'], ',.0f') for row in fund['rows']]}")
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
