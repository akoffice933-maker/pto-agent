# -*- coding: utf-8 -*-
"""Сквозная проверка месячного плана — шаг CI.

Правила:

1. требования к документам берутся из матрицы вида работ и совпадают с ней один в один;
2. позиция без опознанного вида работ — «требование не заявлено», деньги не в «готово»;
3. документ без ФИО и роли не закрывает работу;
4. вид работ без требований в матрице (вывоз мусора) помечается отдельно и не считается ошибкой;
5. полный подписанный комплект по матрице даёт «готово к предъявлению» и освобождает деньги;
6. сумма позиций сверяется с суммой акта; нет цены — «не оценено»;
7. строки «что сделать» отсортированы по деньгам — сверху то, что держит больше;
8. сроки: нет срока — «не заявлено», прошёл — просрочка.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent.asbuilt.requirements import load_matrix, work_type_requirements   # noqa: E402
from pto_agent.monthly import (CHECK_ACT_SUM_MISMATCH, CHECK_DEADLINE_MISSED,    # noqa: E402
                               CHECK_DEADLINE_NOT_STATED, CHECK_DOC_MISSING,
                               CHECK_DOC_UNSIGNED, CHECK_NO_REQUIREMENTS,
                               CHECK_POSITION_WITHOUT_PRICE, CHECK_WORK_TYPE_NOT_STATED,
                               WORK_NO_REQUIREMENTS, WORK_READY, build_plan)

TODAY = date(2026, 11, 6)
MATRIX = ROOT / "data" / "reference" / "work_type_requirements.json"
violations: list[str] = []
stats = {"rules": 0}


def rule(ok: bool, message: str) -> None:
    stats["rules"] += 1
    if not ok:
        violations.append(message)


def codes(plan) -> list[str]:
    return [c["code"] for c in plan["checks"]]


demo_out = ROOT / "out" / "monthly_check"

# ─── демо-план ───
config = json.loads((ROOT / "data/demo/monthly/plan.json").read_text(encoding="utf-8"))
plan = build_plan(config, base_dir=ROOT, out_dir=demo_out, today=TODAY)
totals = plan["totals"]
works = [w for act in plan["acts"] for w in act["works"]]
ready = [w for w in works if w["status"] == WORK_READY]

rule(totals["acts"] == 2 and totals["works"] == 10,
     f"актов {totals['acts']}, видов работ {totals['works']}")
rule(totals["ready"] == 1 and [w["name"] for w in ready] == ["Гидроизоляция и пароизоляция"],
     "готовой работой оказалась не гидроизоляция")
rule(totals["money_ready_rub"] == 230_000.0, f"готово на {totals['money_ready_rub']}")
rule(totals["money_waiting_rub"] == 36_056_600.0, f"держит документ {totals['money_waiting_rub']}")
rule(totals["no_requirements"] == 1, "вид работ без требований в матрице не отделён")
rule(CHECK_NO_REQUIREMENTS in codes(plan), "«требований не заявлено» не показано")
rule(not [c for c in plan["checks"]
          if c["code"] == CHECK_NO_REQUIREMENTS and c["level"] == "error"],
     "«требований в матрице нет» показано ошибкой — это не ошибка инженера")
rule(totals["unclassified_works"] == 1 and CHECK_WORK_TYPE_NOT_STATED in codes(plan),
     "неопознанная позиция не отмечена")
rule(all(w["status"] != WORK_READY for w in works if w["work_type"] is None),
     "работа без вида работ попала в «готово»")
rule(all(w["status"] != WORK_READY for w in works if w["missing"] or w["unsigned"]),
     "работа с недобором документов попала в «готово»")
rule(CHECK_DOC_MISSING in codes(plan) and CHECK_DOC_UNSIGNED in codes(plan),
     "нет/не подписан документ не отмечены")
rule(plan["actions"], "нет списка «что сделать»")
money_order = [a["rub"] or 0 for a in plan["actions"]]
rule(money_order == sorted(money_order, reverse=True),
     "«что сделать» не отсортировано по деньгам")
rule(max(money_order or [0]) == 18_846_100.0,
     f"самое дорогое действие {max(money_order or [0])} вместо 18 846 100")

# ─── требования = матрица ───
matrix = load_matrix(MATRIX)
for work in works:
    if not work["work_type"]:
        continue
    req = work_type_requirements(work["work_type"], matrix)
    rule(work["required"] == req["required"],
         f"{work['work_type']}: документы в отчёте не совпадают с матрицей")
    rule(work["protocols"] == (req.get("protocols") or []),
         f"{work['work_type']}: протоколы не совпадают с матрицей")

# ─── сценарии ───
def scenario(acts: list[dict], **extra) -> dict:
    base = {"name": "проба", "month": "2026-11", "requirements": str(MATRIX), "acts": acts}
    base.update(extra)
    return build_plan(base, base_dir=demo_out, out_dir=demo_out / "cases", today=TODAY)


def pos(name: str, amount: float) -> dict:
    return {"code": "ФЕР 0", "name": name, "qty": 1, "unit": "шт", "price": amount,
            "amount_rub": amount}


EARTH = "Разработка грунта экскаватором в котловане"
earth_req = work_type_requirements("earthworks", matrix)
full_docs = ([{"kind": kind, "number": f"{kind}-1", "signed_by": "Иванов И.И.",
               "role": "начальник ПТО", "date": "2026-11-01"} for kind in earth_req["required"]]
             + [{"kind": "протокол", "requirement": protocol, "number": f"ПИ-{i}",
                 "signed_by": "Кузнецов В.В.", "role": "инженер лаборатории",
                 "date": "2026-11-02"} for i, protocol in enumerate(earth_req["protocols"], 1)])

full = scenario([{"act_id": "КС-2 №90", "amount_rub": 1_000_000,
                  "submission_deadline": "2026-12-01",
                  "positions": [pos(EARTH, 1_000_000)], "documents": full_docs}])
rule(full["acts"][0]["works"][0]["status"] == WORK_READY,
     "полный подписанный комплект не дал готовности")
rule(full["totals"]["money_ready_rub"] == 1_000_000.0, "деньги готовой работы не освобождены")
rule(full["verdict"]["ready_to_invoice"] is True, "месяц не признан готовым при полном комплекте")

unsigned = scenario([{"act_id": "КС-2 №91", "amount_rub": 1_000_000,
                      "positions": [pos(EARTH, 1_000_000)],
                      "documents": [{**d, "signed_by": "", "role": ""} for d in full_docs]}])
rule(unsigned["acts"][0]["works"][0]["status"] != WORK_READY,
     "неподписанные документы закрыли работу")
rule(CHECK_DOC_UNSIGNED in codes(unsigned), "неподписанные документы не отмечены")

mismatch = scenario([{"act_id": "КС-2 №92", "amount_rub": 1_000_000,
                      "positions": [pos(EARTH, 900_000)], "documents": full_docs}])
rule(CHECK_ACT_SUM_MISMATCH in codes(mismatch), "сумма позиций не сверена с актом")

no_price = scenario([{"act_id": "КС-2 №93",
                      "positions": [{"code": "ФЕР 1", "name": EARTH, "qty": 10, "unit": "м3"}]}])
rule(CHECK_POSITION_WITHOUT_PRICE in codes(no_price), "позиция без цены прошла как 0 ₽")
rule(no_price["totals"]["money_ready_rub"] == 0.0, "позиция без цены попала в «готово»")

no_deadline = scenario([{"act_id": "КС-2 №94", "amount_rub": 1000,
                         "positions": [pos(EARTH, 1000)]}])
rule(CHECK_DEADLINE_NOT_STATED in codes(no_deadline), "срок предъявления выдуман")
missed = scenario([{"act_id": "КС-2 №95", "amount_rub": 1000,
                    "submission_deadline": "2026-10-30",
                    "positions": [pos(EARTH, 1000)]}])
rule(CHECK_DEADLINE_MISSED in codes(missed), "просроченный срок не отмечен")

trash = scenario([{"act_id": "КС-2 №96", "amount_rub": 120_000,
                   "positions": [pos("Погрузка и вывоз строительного мусора", 120_000)]}])
rule(trash["acts"][0]["works"][0]["status"] == WORK_NO_REQUIREMENTS,
     "вид работ без требований показан как обычная работа")

print(f"месячный план: правил {stats['rules']} · актов {totals['acts']} · видов работ "
      f"{totals['works']} · готово {totals['ready']} · держит документ "
      f"{totals['money_waiting_rub']:,.2f} ₽ · полный комплект: "
      f"{'готово' if full['verdict']['ready_to_invoice'] else 'не готово'}".replace(",", " "))
if violations:
    print("НАРУШЕНИЯ:")
    for violation in violations:
        print(" ✗ " + violation)
    sys.exit(1)
print("нарушений: 0")
