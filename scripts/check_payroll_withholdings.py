# -*- coding: utf-8 -*-
"""Сквозная проверка удержаний из зарплаты — шаг CI.

Что проверяется (числами, а не «код не упал»):

1. лимиты ст. 138 ТК РФ считаются от суммы **после НДФЛ**: 20 % — распоряжение работодателя,
   50 % — исполнительный лист, 70 % — алименты на несовершеннолетних и вред здоровью;
2. алименты в долях: 1/4, 1/3, 1/2 по числу детей (ст. 81 СК РФ);
3. документ, который не влезает в предел, удерживается на предел, остаток переносится числом;
4. несколько документов идут по очерёдности (ст. 111 № 229-ФЗ), внутри очереди — пропорционально;
5. документ без номера или без взыскателя не исполняется и попадает в отчёт, а не в ноль молча;
6. удержания вписаны в начисление: net = начислено − НДФЛ − удержания, повторный расчёт
   не удерживает алименты второй раз (база — `accrued_net`);
7. проводки: Дт 70 Кт 76 по каждому взыскателю (76 требует контрагента), затем 70 закрывается
   до суммы к выплате; журнал сходится;
8. заявки: зарплатные — на сумму после удержаний, отдельные — на перечисление взыскателям
   (Дт 76 Кт 51).
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent import ledger, orders as orders_mod, payroll, withholdings  # noqa: E402

DEMO = ROOT / "data" / "demo" / "payroll"
violations: list[str] = []
stats = {"checks": 0, "documents": 0, "entries": 0}


def check(condition: bool, message: str) -> None:
    stats["checks"] += 1
    if not condition:
        violations.append(message)


rules = payroll.load_rules()
wh_rules = withholdings.load_rules()
staff = json.loads((DEMO / "staff.json").read_text(encoding="utf-8"))
timesheet = json.loads((DEMO / "timesheet_2026_10.json").read_text(encoding="utf-8"))
orders = orders_mod.load_orders(DEMO / "orders_2026_10.json")
documents = json.loads((DEMO / "withholdings_2026_10.json").read_text(encoding="utf-8"))["documents"]
stats["documents"] = len(documents)

state = payroll.new_state(object_name=staff["object"])
report = payroll.accrue(state, timesheet, staff, rules, orders=orders, withholdings=documents)
payroll.remember(state, report, rules=rules)
rows = {row["tab"]: row for row in report["rows"]}
totals = report["totals"]
limits = wh_rules["limits"]

# ── 1. пределы: от суммы после НДФЛ ─────────────────────────────────────────
check(limits["employer"]["percent"] == 20 and limits["enforcement"]["percent"] == 50
      and limits["priority"]["percent"] == 70, "лимиты ст. 138 ТК РФ не 20/50/70")

prokhorov = rows["001"]
check(prokhorov["withheld"] == 12000.0,
      f"распоряжение работодателя удержано {prokhorov['withheld']}, ожидали 12 000")
check(prokhorov["net"] == round(prokhorov["accrued_net"] - 12000, 2),
      "к выплате ≠ начислено после НДФЛ − удержание")

kovalev = rows["002"]
share = round(kovalev["accrued_net"] / 2, 2)
check(abs(kovalev["withheld"] - share) <= 0.01,
      f"алименты на трёх детей {kovalev['withheld']} ≠ 1/2 от {kovalev['accrued_net']}")
check(kovalev["withholdings"][0]["kind"] == "alimony_share", "вид удержания подменён")
check(withholdings.share_of(1, wh_rules)[0] == Decimal("0.25")
      and withholdings.share_of(2, wh_rules)[0] == Decimal("0.3333333333333333333333333333")
      and withholdings.share_of(5, wh_rules)[0] == Decimal("0.5"),
      "доли алиментов не 1/4, 1/3, 1/2 (ст. 81 СК РФ)")

# ── 2. предел 50 % и перенос остатка ────────────────────────────────────────
gusev = rows["004"]
cap = round(gusev["accrued_net"] * 0.5, 2)
check(abs(gusev["withheld"] - cap) <= 0.01,
      f"исполнительный лист удержан {gusev['withheld']}, предел 50 % = {cap}")
check(abs(gusev["withheld"] + gusev["withheld_carried"] - 60000) <= 0.01,
      "удержано + перенесено ≠ сумма по документу")
check(any("остаток удержания" in (gap.get("what") or "") for gap in report["gaps"]),
      "перенос остатка не назван в «чего не хватает»")

# ── 3. документ без номера и без взыскателя ─────────────────────────────────
smetchikova = rows["006"]
check(smetchikova["withheld"] == 0.0, "документ без номера всё-таки удержан")
check(any(item["code"] == "WITHHOLDING_NO_NUMBER" for item in report["withholding_errors"]),
      "документ без номера не отмечен как неисполнимый")
bad = withholdings.apply({"period": "2026-10", "rows": [{"tab": "900", "net": 10000}]},
                         [{"tab": "900", "kind": "writ", "number": "ИЛ-1", "amount": 5000},
                          {"tab": "900", "kind": "writ", "number": "ИЛ-2", "amount": 5000}])
check(any(item["code"] == "WITHHOLDING_NO_PAYEE" for item in bad["errors"]),
      "документ без взыскателя не отмечен")
check(not bad["ready"], "отчёт с ошибками в документах признан готовым")

# ── 4. очерёдность и пропорция внутри очереди ───────────────────────────────
many = withholdings.apply({"period": "2026-10", "rows": [{"tab": "900", "name": "Тест",
                                                         "net": 10000}]},
                          [{"tab": "900", "kind": "writ", "number": "ИЛ-1", "amount": 6000,
                            "payee": "Банк А"},
                           {"tab": "900", "kind": "writ", "number": "ИЛ-2", "amount": 6000,
                            "payee": "Банк Б"}])
doc_rows = many["rows"][0]["documents"]
check(abs(many["totals"]["withheld"] - 5000) <= 0.01,
      f"при двух листах удержано {many['totals']['withheld']}, предел 50 % = 5 000")
check(all(abs(item["withheld"] - 2500) <= 0.01 for item in doc_rows),
      "внутри одной очереди суммы поделены не пропорционально")

priority = withholdings.apply({"period": "2026-10", "rows": [{"tab": "900", "name": "Тест",
                                                             "net": 10000}]},
                              [{"tab": "900", "kind": "writ", "number": "ИЛ-3", "amount": 4000,
                                "payee": "Банк"},
                               {"tab": "900", "kind": "alimony_fixed", "number": "ИЛ-4",
                                "amount": 7000, "payee": "Алименты"}])
by_number = {item["number"]: item for item in priority["rows"][0]["documents"]}
check(by_number["ИЛ-4"]["withheld"] == 7000.0,
      "алименты первой очереди не удовлетворены раньше кредита")
check(abs(by_number["ИЛ-3"]["withheld"]) <= 0.01,
      "кредит удержан после исчерпания предела алиментами")

# ── 5. повторный расчёт не удерживает дважды ────────────────────────────────
again = withholdings.apply({"period": "2026-10", "rows": [kovalev]}, documents)
check(abs(again["rows"][0]["withheld"] - kovalev["withheld"]) <= 0.01,
      "повторный расчёт удержал алименты второй раз: база взята из net, а не из accrued_net")

# ── 6. итоги и сходимость ───────────────────────────────────────────────────
withheld_total = round(sum(row.get("withheld") or 0 for row in report["rows"]), 2)
check(abs(totals["withheld"] - withheld_total) <= 0.01, "итог удержаний ≠ сумма по строкам")
check(abs(totals["net"] - (totals["net_accrued"] - totals["withheld"])) <= 0.01,
      "к выплате после удержаний ≠ к выплате до − удержано")
check(payroll.verify(state)["ok"], "ведомость с удержаниями не сходится")

# ── 7. проводки: 70 закрывается, 76 получает контрагента ────────────────────
ledger_state = ledger.new_state(object_name=staff["object"])
posted = payroll.post_ledger(ledger_state, state["periods"][0])
stats["entries"] = len(ledger_state.get("entries") or [])
check(posted["rejected"] == 0, f"проводки с удержаниями отказаны: {posted['errors'][:2]}")
check(payroll.post_ledger(ledger_state, state["periods"][0])["duplicates"]
      == posted["posted"], "повторное проведение создало вторые проводки")
by_event: dict[str, float] = {}
for entry in ledger_state.get("entries") or []:
    lines = entry.get("lines") or []
    if entry.get("event") == "payroll_withheld":
        for line in lines:
            if str(line.get("account")) == "76":
                by_event[str((line.get("analytics") or {}).get("контрагент"))] = float(
                    line.get("amount"))
check(len(by_event) == 4, f"проводок Дт 70 Кт 76 по взыскателям {len(by_event)}, ожидали 4")
check(all(name not in (None, "", "None") for name in by_event),
      "в проводке 76 нет контрагента: удержание не привязано к взыскателю")
check(abs(sum(by_event.values()) - totals["withheld"]) <= 0.01,
      "сумма проводок 76 ≠ удержано по документам")

balances = ledger.balances(ledger_state) if hasattr(ledger, "balances") else {}
credit70 = 0.0
for entry in ledger_state.get("entries") or []:
    for line in entry.get("lines") or []:
        if str(line.get("account")) != "70":
            continue
        amount = float(line.get("amount") or 0)
        credit70 += amount if line.get("side") == ledger.CREDIT else -amount
check(abs(credit70 - totals["net"]) <= 0.01,
      f"остаток по 70 = {credit70:,.2f} ≠ к выплате {totals['net']:,.2f}: "
      f"удержания не закрыли счёт 70")
check(ledger.verify(ledger_state)["ok"], "журнал с удержаниями не сходится")

# ── 8. заявки ───────────────────────────────────────────────────────────────
requests = payroll.payout_requests(state["periods"][0], date="2026-11-05")
check(all(abs(request["amount"] - rows[request["number"][-2:] and
                                      next(row["tab"] for row in report["rows"]
                                           if row["name"] == request["counterparty"])]["net"])
          <= 0.01 for request in requests),
      "заявка на зарплату не равна сумме после удержаний")
check(any("за вычетом удержаний" in request["purpose"] for request in requests),
      "в назначении платежа не сказано про удержания")
transfer = payroll.withholding_requests(state["periods"][0], date="2026-11-05")
check(len(transfer) == 4, f"заявок на перечисление взыскателям {len(transfer)}, ожидали 4")
check(abs(sum(item["amount"] for item in transfer) - totals["withheld"]) <= 0.01,
      "заявки на перечисление удержаний не равны удержанной сумме")
check(all(item["article"] == "удержания по исполнительным" for item in transfer),
      "у заявки взыскателю не своя статья платежа")

# ── 9. без реестра удержаний сумма не притворяется верной ───────────────────
empty = payroll.accrue(payroll.new_state(object_name=staff["object"]), timesheet, staff, rules,
                       orders=orders)
check(any("удержания" in (item["what"] or "") for item in empty["not_included"]),
      "без реестра документов отсутствие удержаний не названо вслух")
check("withheld" not in empty["totals"], "без реестра появился итог удержаний")

print(f"удержания: документов {stats['documents']} · проводок {stats['entries']} · "
      f"проверок {stats['checks']}")
print(f"2026-10: удержано {totals['withheld']:,.2f} ₽ · перенесено {totals['carried']:,.2f} ₽ · "
      f"к выплате до удержаний {totals['net_accrued']:,.2f} ₽ · после {totals['net']:,.2f} ₽")
print("по взыскателям: " + "; ".join(f"{name} {amount:,.2f}" for name, amount in by_event.items()))
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
