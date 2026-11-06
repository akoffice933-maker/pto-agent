# -*- coding: utf-8 -*-
"""Сквозная проверка графика финансирования — шаг CI.

Правила:

1. график сводится с портфелем: вердикт по каждому акту берётся из портфеля, а не заводится заново;
2. в платёж попадает только предъявляемый акт, остальное — перенос с причиной;
3. перенесённая строка не показывает «к оплате» — это долг, а не деньги месяца;
4. пени считаются только по заявленной в договоре ставке; нет ставки — «цена переноса
   не заявлена», а не 0 ₽;
5. удержание — только из договора; нет процента — «к оплате» равна сумме акта;
6. сроки и даты — из графика; нет срока — «не заявлено», нет подстановки «обычно к 10-му»;
7. сумма платежа сверяется с актом, платят по акту;
8. акт в двух строках графика, акт без графика, платёж без акта — видны;
9. на чистом подписанном комплекте платёж наступает и попадает в «в платёж».
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent import correspondence as corr                                # noqa: E402
from pto_agent import docflow, journals, quality_docs                       # noqa: E402
from pto_agent.funding import (CHECK_ACT_DOUBLE_IN_PAYMENTS, CHECK_ACT_NOT_IN_PORTFOLIO,  # noqa: E402
                               CHECK_ACT_WITHOUT_PAYMENT, CHECK_AMOUNT_MISMATCH,
                               CHECK_DEADLINE_MISSED, CHECK_DEADLINE_NOT_STATED,
                               CHECK_PAYMENT_BEFORE_DEADLINE, CHECK_RETENTION_NOT_STATED,
                               CHECK_TRANSFER_COST_NOT_STATED, PAY_IN_PAYMENT, PAY_NOT_LINKED,
                               PAY_TRANSFER, build_plan)

TODAY = date(2026, 11, 6)
BY, ROLE = "Иванов И.И.", "начальник ПТО"
violations: list[str] = []
stats = {"rules": 0}


def rule(ok: bool, message: str) -> None:
    stats["rules"] += 1
    if not ok:
        violations.append(message)


def codes(plan) -> list[str]:
    return [c["code"] for c in plan["checks"]]


demo_out = ROOT / "out" / "funding_check"

# ─── 1–8: демо-график ───
config = json.loads((ROOT / "data/demo/funding/plan.json").read_text(encoding="utf-8"))
plan = build_plan(config, base_dir=ROOT, out_dir=demo_out, today=TODAY)
totals = plan["totals"]
rows = {row["act_id"]: row for row in plan["payments"]}

rule(totals["payments"] == 5, f"строк графика {totals['payments']}, ожидалось 5")
rule(totals["plan_rub"] == 44_124_600.0, f"запланировано {totals['plan_rub']}")
rule(totals["transfer_rub"] == 38_524_600.0, f"перенос {totals['transfer_rub']}")
rule(totals["penalty_rub"] == 17_910.6, f"пени {totals['penalty_rub']} вместо 17 910,60")
rule(totals["missed"] == 3, f"пропущено сроков {totals['missed']}")
rule(totals["unestimated"] == 1, f"не оценено строк {totals['unestimated']}")
rule(totals["in_payment_rub"] == 0.0, "в демо есть платёж, хотя ни один комплект не сдан")

rule(rows["КС-2 №38"]["status"] == PAY_TRANSFER,
     "акт с заблокированным комплектом попал в платёж")
rule(rows["КС-2 №5"]["days_overdue"] == 27,
     f"просрочка по КС-2 №5 {rows['КС-2 №5']['days_overdue']} дн. вместо 27")
rule(rows["КС-2 №5"]["penalty_rub"] == 6_210.0,
     f"пени по КС-2 №5 {rows['КС-2 №5']['penalty_rub']} вместо 6 210,00")
rule("27 дн." in (rows["КС-2 №5"]["penalty_formula"] or ""), "в пени нет формулы")
rule("п. 9.1" in (rows["КС-2 №5"].get("penalty_basis") or ""),
     "у пеней нет пункта-основания из договора")
rule(rows["КС-2 №90"]["status"] == PAY_NOT_LINKED, "акт вне портфеля показан как платёж")
rule(CHECK_ACT_NOT_IN_PORTFOLIO in codes(plan), "акт вне портфеля не отмечен")
rule(CHECK_ACT_WITHOUT_PAYMENT in codes(plan), "акт портфеля без строки графика не отмечен")
rule(all(row["to_pay_rub"] is None for row in plan["payments"] if row["status"] == PAY_TRANSFER),
     "перенесённая строка показывает «к оплате» — это долг, а не деньги месяца")
rule(CHECK_DEADLINE_MISSED in codes(plan), "пропущенный срок не отмечен")
rule(totals["retention_rub"] > 0, "удержание из договора не применилось")

# ─── 6, 4, 5, 7, 8: отдельные сценарии ───
def scenario(**extra) -> dict:
    base = {"name": "проба",
            "contract": {"number": "Д-0", "payment_terms": {"basis": "договор п. 5.3"}},
            "payments": [{"month": "2026-11", "act_id": "КС-2 №1", "amount_rub": 5_000_000,
                          "submission_deadline": "2026-12-01", "payment_date": "2026-12-20"}],
            "objects": extra.pop("objects", [])}
    base.update(extra)
    return build_plan(base, base_dir=demo_out, out_dir=demo_out / "cases", today=TODAY)


blocked_obj = [{"object_name": "Склад", "pack": str(ROOT / "data/demo/pack_portfolio/warehouse_pack.json"),
                "acts": [{"act_id": "КС-2 №1", "amount_rub": 5_000_000,
                          "covered_by": ["journals_work"]}]}]
late = scenario(objects=blocked_obj,
                payments=[{"month": "2026-11", "act_id": "КС-2 №1", "amount_rub": 5_000_000,
                           "submission_deadline": "2026-10-01", "payment_date": "2026-11-01"}])
rule(CHECK_TRANSFER_COST_NOT_STATED in codes(late),
     "без ставки пеней цена переноса показана числом")
rule(late["payments"][0]["penalty_rub"] is None, "пени посчитаны без ставки в договоре")
rule(late["payments"][0]["penalty_note"], "нет пояснения, почему цена переноса не названа")

no_deadline = scenario(objects=blocked_obj,
                       payments=[{"month": "2026-11", "act_id": "КС-2 №1",
                                  "amount_rub": 5_000_000}])
rule(CHECK_DEADLINE_NOT_STATED in codes(no_deadline), "срок предъявления выдуман")
rule(no_deadline["payments"][0]["days_to_deadline"] is None, "дни до срока посчитаны без срока")

no_retention = scenario(objects=blocked_obj)
rule(CHECK_RETENTION_NOT_STATED in codes(no_retention), "удержание посчитано без договора")
rule(no_retention["payments"][0]["retention_rub"] is None, "удержание выдумано")

mismatch = scenario(objects=blocked_obj,
                    payments=[{"month": "2026-11", "act_id": "КС-2 №1", "amount_rub": 4_000_000,
                               "submission_deadline": "2026-12-01", "payment_date": "2026-12-20"}])
rule(CHECK_AMOUNT_MISMATCH in codes(mismatch), "расхождение графика и акта не отмечено")
rule(mismatch["payments"][0]["amount_rub"] == 5_000_000.0, "в расчёт взята сумма из графика")

before = scenario(objects=blocked_obj,
                  payments=[{"month": "2026-11", "act_id": "КС-2 №1", "amount_rub": 5_000_000,
                             "submission_deadline": "2026-12-20", "payment_date": "2026-12-01"}])
rule(CHECK_PAYMENT_BEFORE_DEADLINE in codes(before), "платёж раньше срока предъявления прошёл")

twice = scenario(objects=blocked_obj,
                 payments=[{"month": "2026-11", "act_id": "КС-2 №1", "amount_rub": 5_000_000,
                            "submission_deadline": "2026-12-01", "payment_date": "2026-12-20"},
                           {"month": "2026-12", "act_id": "КС-2 №1", "amount_rub": 5_000_000,
                            "submission_deadline": "2026-12-01", "payment_date": "2027-01-20"}])
rule(CHECK_ACT_DOUBLE_IN_PAYMENTS in codes(twice), "акт в двух строках графика прошёл молча")

# ─── 9: чистый подписанный комплект даёт платёж ───
built = demo_out / "built"
built.mkdir(parents=True, exist_ok=True)
work = journals.build_work_journal(
    [{"date": "2026-10-01", "shift": "1", "work": "Разработка грунта экскаватором",
      "unit": "м3", "qty": 100, "executor": "ООО «Земстрой»", "weather": {"temp_c": 8}}],
    object_name="проба", number="ОЖР-1", today=TODAY)
work = journals.sign(work, by=BY, role=ROLE, today=TODAY)
verification = journals.sign(journals.build_verification_journal(
    [{"date": "2026-10-01", "material": "Бетон B25", "unit": "м3", "qty": 60, "batch": "партия 1",
      "decision": "допущен", "inspected_by": BY, "role": "инженер ПТО",
      "quality_doc": {"kind": "паспорт качества", "number": "П-1",
                      "valid_until": "2026-12-31"}}],
    object_name="проба", number="ВК-1", today=TODAY), by=BY, role=ROLE, today=TODAY)
quality = quality_docs.sign(quality_docs.build_registry(
    [{"kind": "паспорт качества", "number": "П-1", "issue_date": "2026-09-28",
      "material": "Бетон B25", "batch": "партия 1", "standard": "ГОСТ 7473-2010",
      "file": "b.pdf", "attested_by": {"by": BY, "role": "инженер ПТО"}}],
    object_name="проба", number="ДК-1",
    requirements=[{"material": "Бетон B25", "required": ["паспорт качества"], "basis": "договор"}],
    usage=[{"material": "Бетон B25", "batch": "партия 1", "qty": 60, "unit": "м3",
            "applied_from": "2026-10-04"}],
    verification=[{"material": "Бетон B25", "batch": "партия 1", "decision": "допущен",
                   "quality_doc": {"kind": "паспорт качества", "number": "П-1"}}],
    today=TODAY), by=BY, role=ROLE, today=TODAY)
rd = docflow.sign(docflow.build_rd_registry(
    [{"doc": "КЖ-1", "revision": "изм. 1", "date": "2026-09-26", "recipient": "ООО «М»",
      "stamp": "в производство работ", "receipt": {"by": "Соколов И.И."}}],
    revisions=[{"doc": "КЖ-1", "revision": "изм. 1", "current": True}],
    works=[{"work": "Плита", "rd": "КЖ-1"}], object_name="проба", number="РД-1", today=TODAY),
    by=BY, role=ROLE, today=TODAY)
submittals = docflow.sign(docflow.build_submittal_registry(
    [{"title": "Комплект ИД", "act_ref": "АОСР-1", "sent_at": "2026-10-06",
      "recipient": "технадзор", "receipt": {"by": "Кузнецов В.В."}, "remarks": [],
      "signature": {"by": "Кузнецов В.В.", "role": "инженер технадзора",
                    "decision": "согласовано", "at": "2026-10-08"}}],
    object_name="проба", number="СГ-1", today=TODAY), by=BY, role=ROLE, today=TODAY)
letters = corr.sign(corr.build_letter_registry(
    [{"number": "1", "date": "2026-10-02", "kind": "запрос", "to": {"org": "ООО «М»"},
      "subject": "документы", "basis": {"doc": "договор", "clause": "п. 4.12"},
      "signed_by": {"by": BY, "role": ROLE}}], object_name="проба", number="ПИС-1", today=TODAY),
    by=BY, role=ROLE, today=TODAY)
involvement = corr.sign(corr.build_involvement_registry(
    [{"kind": "лаборатория", "organization": "Лаборатория «СтройТест»",
      "attestation": {"number": "RA.RU.1", "scope": "бетон", "valid_until": "2027-04-01"},
      "request": {"number": "З-1", "date": "2026-10-02", "due": "2026-10-09"},
      "result": {"number": "ПИ-1", "date": "2026-10-05", "linked_to": "партия 1"}}],
    object_name="проба", number="ЛАБ-1", today=TODAY), by=BY, role=ROLE, today=TODAY)
days = corr.sign(corr.build_quality_days(
    [{"kind": "день качества", "date": "2026-10-09",
      "participants": [{"org": "ООО «М»", "by": "Соколов И.И.", "role": "прораб"}],
      "protocol": {"number": "ДК-2", "date": "2026-10-09"},
      "remarks": [{"text": "накрыть бетон", "responsible": "ООО «М»", "due": "2026-10-20",
                   "closed_at": "2026-10-18"}]}],
    object_name="проба", number="ДК-1", today=TODAY), by=BY, role=ROLE, today=TODAY)

clean_pack = {"object_name": "проба", "number": "К-Ч", "sections": {
    "journals_work": {"registry": journals.write(work, built)["json"]},
    "journals_verification": {"registry": journals.write(verification, built)["json"]},
    "quality_docs": {"registry": quality_docs.write(quality, built)["json"]},
    "rd": {"registry": docflow.write(rd, built)["json"]},
    "submittals": {"registry": docflow.write(submittals, built)["json"]},
    "letters": {"registry": corr.write(letters, built)["json"]},
    "involvement": {"registry": corr.write(involvement, built)["json"]},
    "quality_days": {"registry": corr.write(days, built)["json"]},
    "checklists": {"enabled": False, "reason": "в этом акте чек-листы не выпускаются"}}}

paid = scenario(objects=[{"object_name": "чистый", "pack": clean_pack,
                          "acts": [{"act_id": "КС-2 №1", "amount_rub": 5_000_000,
                                    "covered_by": ["journals_work"]}]}],
                contract={"number": "Д-0",
                          "payment_terms": {"basis": "договор п. 5.3"},
                          "retention": {"pct": 5, "basis": "договор п. 5.7"}},
                payments=[{"month": "2026-11", "act_id": "КС-2 №1", "amount_rub": 5_000_000,
                           "submission_deadline": "2026-12-01", "payment_date": "2026-12-20"}])
rule(paid["payments"][0]["status"] == PAY_IN_PAYMENT, "чистый комплект не дал платежа")
rule(paid["payments"][0]["to_pay_rub"] == 4_750_000.0,
     f"к оплате {paid['payments'][0]['to_pay_rub']} вместо 4 750 000 после удержания 5 %")
rule(paid["totals"]["in_payment_rub"] == 4_750_000.0, "сумма «в платёж» не сложилась")
rule(paid["verdict"]["share_in_payment_pct"] == 95.0,
     f"доля в платеже {paid['verdict']['share_in_payment_pct']} вместо 95 %")

print(f"график финансирования: правил {stats['rules']} · строк {totals['payments']} · "
      f"в платёж {totals['in_payment_rub']:,.2f} ₽ · перенос {totals['transfer_rub']:,.2f} ₽ · "
      f"пени {totals['penalty_rub']:,.2f} ₽ · чистый комплект: "
      f"{'платёж наступает' if paid['payments'][0]['status'] == PAY_IN_PAYMENT else 'перенос'}"
      .replace(",", " "))
if violations:
    print("НАРУШЕНИЯ:")
    for violation in violations:
        print(" ✗ " + violation)
    sys.exit(1)
print("нарушений: 0")
