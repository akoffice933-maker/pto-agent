# -*- coding: utf-8 -*-
"""Сквозная проверка комплекта объекта — шаг CI.

Правила:

1. комплект собирается по конфигу одной командой и содержит все разделы;
2. отсутствующий раздел — «нет данных» и блокер, а не «пройдено»;
3. раздел, исключённый человеком, виден в описи, а исключение без причины — блокер;
4. ошибка в любом разделе блокирует сдачу комплекта;
5. не подписанный раздел блокирует сдачу — агент не подписывает;
6. подписанный реестр читается по ссылке и проверяется: правка после подписи его «разваливает»;
7. сводный раздел не может быть чище отдельного реестра (те же коды находок);
8. чистый подписанный комплект предъявляется (проверяется на собранных в прогоне реестрах);
9. в файлах нет «комплект готов к сдаче» без оснований.
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
from pto_agent.pack import (CHECK_DISABLED_NO_REASON, CHECK_SECTION_ERRORS,  # noqa: E402
                            CHECK_SECTION_MISSING, CHECK_SECTION_UNSIGNED, SECTION_DISABLED,
                            SECTION_MISSING, build_pack, to_markdown, write)

TODAY = date(2026, 11, 6)
BY, ROLE = "Иванов И.И.", "начальник ПТО"
violations: list[str] = []
stats = {"rules": 0, "sections": 0, "findings": 0, "signed": 0, "clean_sections": 0}


def rule(ok: bool, message: str) -> None:
    stats["rules"] += 1
    if not ok:
        violations.append(message)


def codes(pack) -> list[str]:
    return [c["code"] for c in pack["checks"]]


demo_config = json.loads((ROOT / "data" / "demo" / "pack" / "pack.json").read_text(encoding="utf-8"))
demo_out = ROOT / "out" / "pack_check"
pack = build_pack(demo_config, base_dir=ROOT, out_dir=demo_out, today=TODAY)
stats.update({"sections": pack["totals"]["sections"], "findings": pack["totals"]["findings"]})

# 1–3: состав комплекта
rule(pack["totals"]["sections"] == 9, f"разделов {pack['totals']['sections']}, ожидалось 9")
kinds = {s["kind"] for s in pack["sections"]}
rule({"journals_work", "journals_verification", "quality_docs", "rd", "submittals", "letters",
      "involvement", "quality_days"} <= kinds, "не все обязательные разделы попали в комплект")
rule(pack["totals"]["disabled"] == 1, "исключённый раздел не показан в описи")
rule(any(s["status"] == SECTION_DISABLED and s.get("why") for s in pack["sections"]),
     "у исключённого раздела нет причины")
no_reason = build_pack({"object_name": "проба", "sections": {"letters": {"enabled": False}}},
                       base_dir=ROOT, out_dir=demo_out / "no_reason", today=TODAY)
rule(CHECK_DISABLED_NO_REASON in codes(no_reason), "исключение без причины прошло молча")

empty_cfg = build_pack({"object_name": "проба", "sections": {}},
                       base_dir=ROOT, out_dir=demo_out / "empty", today=TODAY)
rule(CHECK_SECTION_MISSING in codes(empty_cfg), "пустой конфиг не дал «нет данных»")
rule(empty_cfg["totals"]["missing"] >= 8, "разделы без данных не перечислены")
rule(any(s["status"] == SECTION_MISSING for s in empty_cfg["sections"]),
     "нет ни одного раздела со статусом «нет данных»")

# 4–5: ошибки и подписи блокируют
rule(CHECK_SECTION_ERRORS in codes(pack), "ошибки разделов не попали в блокеры")
rule(CHECK_SECTION_UNSIGNED in codes(pack), "неподписанные разделы не попали в блокеры")
rule(pack["can_submit"]["allowed"] is False, "демо-комплект разрешён к предъявлению")
rule(any("без данных" in r or "с ошибками" in r for r in pack["can_submit"]["reasons"]),
     "причины отказа не названы")

# 7: сводный раздел = отдельный реестр
demo_quality = next(s for a in pack["sections"] if a["kind"] == "quality_docs" and a["errors"]
                     for s in [a])
alone = quality_docs.build_registry(
    json.loads((ROOT / "data/demo/quality_docs/documents.json").read_text(encoding="utf-8"))["documents"],
    object_name=demo_config["object_name"], number="ДК-1",
    requirements=json.loads((ROOT / "data/demo/quality_docs/requirements.json")
                            .read_text(encoding="utf-8"))["requirements"],
    usage=json.loads((ROOT / "data/demo/quality_docs/usage.json").read_text(encoding="utf-8"))["usage"],
    verification=json.loads((ROOT / "data/demo/journals/verification.json")
                            .read_text(encoding="utf-8"))["records"],
    today=TODAY)
rule([e["code"] for e in demo_quality["errors"]]
     == [c["code"] for c in alone["checks"] if c["level"] == "error"],
     "сводный раздел расходится с отдельным реестром документов качества")

# 6 и 8: чистый подписанный комплект
built = demo_out / "built"
built.mkdir(parents=True, exist_ok=True)
work = journals.sign(journals.build_work_journal(
    [{"date": "2026-10-01", "shift": "1", "work": "Разработка грунта экскаватором",
      "unit": "м3", "qty": 100, "executor": "ООО «Земстрой»", "weather": {"temp_c": 8}}],
    object_name="проба", number="ОЖР-1", today=TODAY), by=BY, role=ROLE, today=TODAY)
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
clean = build_pack({"object_name": "проба", "number": "К-Ч", "sections": sections},
                   base_dir=demo_out, out_dir=demo_out / "clean", today=TODAY)
stats["signed"] = clean["totals"]["signed"]
stats["clean_sections"] = clean["totals"]["ok"]
rule(clean["totals"]["with_errors"] == 0,
     "чистый комплект с ошибками: " + "; ".join(e["message"] for s in clean["sections"]
                                                for e in s["errors"]))
rule(clean["totals"]["missing"] == 0, "в чистом комплекте есть разделы без данных")
rule(clean["totals"]["signed"] == 8, f"подписанных разделов {clean['totals']['signed']}, ожидалось 8")
rule(clean["can_submit"]["allowed"] is True,
     "чистый подписанный комплект не предъявляется: " + "; ".join(clean["can_submit"]["reasons"]))

# 6: подпись проверяется — правка после подписи ломает раздел
tampered = json.loads(Path(sections["journals_work"]["registry"]).read_text(encoding="utf-8"))
tampered["entries"][0]["qty"] = 999
broken_path = built / "work_подправлен.json"
broken_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
broken = build_pack({"object_name": "проба",
                     "sections": {**sections, "journals_work": {"registry": str(broken_path)}}},
                    base_dir=demo_out, out_dir=demo_out / "broken", today=TODAY)
work_section = next(s for s in broken["sections"] if s["kind"] == "journals_work")
rule(work_section["signed"] is False, "правка после подписи не отменила подпись раздела")
rule(CHECK_SECTION_UNSIGNED in codes(broken), "неподписанный после правки раздел не в блокерах")

# 9: файлы и формулировки
files = write(pack, demo_out)
markdown = to_markdown(pack).lower()
for phrase in ("комплект готов к сдаче", "документация в полном объёме"):
    rule(phrase not in markdown, f"pack.md: формулировка «{phrase}» — вердикт ставит ИТР")
rule(Path(files["xlsx"]).exists() and Path(files["docx"]).exists(),
     "опись комплекта не собрана в XLSX/DOCX")

print(f"комплект объекта: правил {stats['rules']} · разделов {stats['sections']} · "
      f"находок в демо-комплекте {stats['findings']} · чистый комплект: разделов без ошибок "
      f"{stats['clean_sections']}, подписано {stats['signed']} · вердикт демо-комплекта: "
      f"{'можно' if pack['can_submit']['allowed'] else 'нельзя'}")
if violations:
    print("НАРУШЕНИЯ:")
    for violation in violations:
        print(" ✗ " + violation)
    sys.exit(1)
print("нарушений: 0")
