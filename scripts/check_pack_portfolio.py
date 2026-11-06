# -*- coding: utf-8 -*-
"""Сквозная проверка портфеля комплектов — шаг CI.

Правила:

1. портфель собирается по конфигу, каждый объект — тем же `build_pack`, что и в папке объекта;
2. объект без комплекта — «не оценено», а не «риска нет»;
3. акт без суммы — «не оценено», а не 0 ₽;
4. заблокированный объект держит деньги своих актов, предъявляемый — нет;
5. повторённый акт считается один раз;
6. деньги разложены по действию, и «снимается подписью» не выдаётся за объект с ошибками;
7. разрез «раздел → деньги» строится только по заявленным актам;
8. чистый подписанный комплект даёт «можно предъявлять» и освобождает деньги;
9. в файлах нет «всё под контролем» и «риска нет».
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
from pto_agent.pack_portfolio import (ACT_AT_RISK, ACT_NOT_ESTIMATED,       # noqa: E402
                                      ACT_RELEASABLE, CHECK_ACT_DOUBLE_COUNTED,
                                      CHECK_ACT_SECTION_UNKNOWN, CHECK_ACT_WITHOUT_AMOUNT,
                                      CHECK_OBJECT_WITHOUT_PACK, FIX_SIGN, build_portfolio,
                                      to_markdown, write)

TODAY = date(2026, 11, 6)
BY, ROLE = "Иванов И.И.", "начальник ПТО"
violations: list[str] = []
stats: dict = {"rules": 0}


def rule(ok: bool, message: str) -> None:
    stats["rules"] += 1
    if not ok:
        violations.append(message)


def codes(portfolio) -> list[str]:
    return [c["code"] for c in portfolio["checks"]]


demo_out = ROOT / "out" / "pack_portfolio_check"

# ─── 1–4, 6, 7: демо-портфель ───
config = json.loads((ROOT / "data/demo/pack_portfolio/portfolio.json").read_text(encoding="utf-8"))
portfolio = build_portfolio(config, base_dir=ROOT, out_dir=demo_out, today=TODAY)
totals, money = portfolio["totals"], portfolio["money"]
objects = {obj["object_name"]: obj for obj in portfolio["objects"]}

rule(totals["objects"] == 3, f"объектов {totals['objects']}, ожидалось 3")
rule(totals["blocked"] == 2 and totals["without_pack"] == 1,
     f"раскладка объектов не та: с блокерами {totals['blocked']}, без комплекта "
     f"{totals['without_pack']}")
rule(money["at_risk_rub"] == 36_654_600.0,
     f"риск {money['at_risk_rub']} вместо 36 654 600 (суммы актов КС-2)")

pupil = objects.get("Детский сад, Колпино") or {}
rule(CHECK_OBJECT_WITHOUT_PACK in codes(portfolio), "объект без комплекта прошёл молча")
rule(pupil.get("at_risk_rub") is None, "объект без комплекта показан нулём риска")
rule(any(a["status"] == ACT_NOT_ESTIMATED for a in pupil.get("acts") or []),
     "акты объекта без комплекта не попали в «не оценено»")
rule(CHECK_ACT_WITHOUT_AMOUNT in codes(portfolio), "акт без суммы не отмечен")
rule(all(a["amount_rub"] is None for a in pupil.get("acts") or [] if a["act_id"] == "КС-2 №3"),
     "акт без суммы получил число")

blocked = objects.get("ЖК «Северный», корпус 2") or {}
rule(blocked.get("at_risk_rub") == 34_354_600.0,
     f"риск по К-38 {blocked.get('at_risk_rub')} вместо 34 354 600")
rule(all(a["status"] == ACT_AT_RISK for a in blocked.get("acts") or []),
     "акты заблокированного объекта не под риском")
rule(any(row["fix"] == FIX_SIGN for row in money["by_fix"]) is False,
     "в демо есть объект с ошибками, подписанный как «снимается подписью»")
rows = {row["section"]: row for row in blocked.get("sections") or []}
rule(rows.get("submittals", {}).get("rub"),
     "деньги не разнесены по заявленным разделам")
rule(rows.get("journals_work", {}).get("rub") == 0,
     "раздел, который акты не заявляли, получил деньги")

# ─── 5: повтор акта ───
duplicated = json.loads(json.dumps(config))
duplicated["objects"][1]["acts"][0]["act_id"] = "КС-2 №38"
dup = build_portfolio(duplicated, base_dir=ROOT, out_dir=demo_out / "dup", today=TODAY)
rule(CHECK_ACT_DOUBLE_COUNTED in codes(dup), "повтор акта не отмечен")
rule(dup["money"]["at_risk_rub"] < money["at_risk_rub"],
     f"повтор акта не снял деньги: {dup['money']['at_risk_rub']} против {money['at_risk_rub']} "
     f"— сумма повторённого акта не должна входить второй раз")
rule(dup["money"]["at_risk_rub"] == 34_354_600.0,
     f"риск с повтором {dup['money']['at_risk_rub']} вместо 34 354 600 (без второй копии)")

# ─── 8: чистый подписанный комплект освобождает деньги ───
built = demo_out / "built"
built.mkdir(parents=True, exist_ok=True)
work = journals.build_work_journal(
    [{"date": "2026-10-01", "shift": "1", "work": "Разработка грунта экскаватором",
      "unit": "м3", "qty": 100, "executor": "ООО «Земстрой»", "weather": {"temp_c": 8}}],
    object_name="проба", number="ОЖР-1", today=TODAY)
# тот же журнал, но без подписи — в отдельную папку, иначе подписанный файл его перезапишет
built_unsigned = demo_out / "built_unsigned"
built_unsigned.mkdir(parents=True, exist_ok=True)
unsigned_work = journals.write(work, built_unsigned)["json"]
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

sections = {
    "journals_work": {"registry": journals.write(work, built)["json"]},
    "journals_verification": {"registry": journals.write(verification, built)["json"]},
    "quality_docs": {"registry": quality_docs.write(quality, built)["json"]},
    "rd": {"registry": docflow.write(rd, built)["json"]},
    "submittals": {"registry": docflow.write(submittals, built)["json"]},
    "letters": {"registry": corr.write(letters, built)["json"]},
    "involvement": {"registry": corr.write(involvement, built)["json"]},
    "quality_days": {"registry": corr.write(days, built)["json"]},
    "checklists": {"enabled": False, "reason": "в этом акте чек-листы не выпускаются"},
}
clean_pack = {"object_name": "проба", "number": "К-Ч", "sections": sections}
unsigned_pack = {"object_name": "проба", "number": "К-Ч",
                 "sections": {**sections, "journals_work": {"registry": unsigned_work}}}

allowed = build_portfolio({"objects": [
    {"object_name": "чистый", "pack": clean_pack, "acts": [
        {"act_id": "КС-2 №90", "amount_rub": 5_000_000, "covered_by": ["journals_work"]}]},
    {"object_name": "без подписи", "pack": unsigned_pack, "acts": [
        {"act_id": "КС-2 №91", "amount_rub": 2_000_000, "covered_by": ["journals_work"]}]},
]}, base_dir=demo_out, out_dir=demo_out / "clean", today=TODAY)
clean, unsigned = allowed["objects"][0], allowed["objects"][1]
rule(clean["can_submit"] is True, "чистый подписанный комплект не предъявляется")
rule(clean["at_risk_rub"] == 0.0 and clean["acts"][0]["status"] == ACT_RELEASABLE,
     "деньги чистого объекта не освобождены")
rule(allowed["money"]["released_rub"] == 5_000_000.0,
     f"освобождено {allowed['money']['released_rub']} вместо 5 000 000")
rule(unsigned["fix"] == FIX_SIGN, f"комплект без подписи назван «{unsigned['fix']}»")
rule([row["fix"] for row in allowed["money"]["by_fix"]] == [FIX_SIGN],
     "деньги, которые держит только подпись, не отделены")
rule(allowed["money"]["by_fix"][0]["rub"] == 2_000_000.0,
     f"под риском подписи {allowed['money']['by_fix'][0]['rub']} вместо 2 000 000")
rule(allowed["verdict"]["any_releasable"] is True, "вердикт не увидел предъявляемый объект")

# ─── 4, 9: файлы и формулировки ───
files = write(portfolio, demo_out)
markdown = to_markdown(portfolio).lower()
for phrase in ("всё под контролем", "риска нет"):
    rule(phrase not in markdown, f"portfolio.md: формулировка «{phrase}»")
rule("не оценено" in markdown, "в сводке нет пометки «не оценено»")
rule(Path(files["xlsx"]).exists() and Path(files["docx"]).exists(),
     "сводка не собрана в XLSX/DOCX")

print(f"портфель комплектов: правил {stats['rules']} · объектов {totals['objects']} · "
      f"актов {totals['acts']} · под риском непредъявления {money['at_risk_rub']:,.2f} ₽ "
      f"· не оценено {money['unestimated_acts']} · чистый объект: "
      f"{'предъявляется' if clean['can_submit'] else 'не предъявляется'}".replace(",", " "))
if violations:
    print("НАРУШЕНИЯ:")
    for violation in violations:
        print(" ✗ " + violation)
    sys.exit(1)
print("нарушений: 0")
