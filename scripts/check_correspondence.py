# -*- coding: utf-8 -*-
"""Сквозная проверка переписки и внешних участников — шаг CI.

Правила:

1. письмо без адресата в реестр не попадает;
2. письмо без основания не предъявляется; без ФИО и роли подписавшего — не отправлено;
3. просроченный ответ — ошибка, ответ в срок — нет;
4. ответ без номера и даты — сослаться нельзя;
5. лаборатория без аттестата результата не даёт; аттестат, истёкший до даты работы, — нет;
6. результат без привязки к партии/работе в комплект не ложится;
7. мероприятие без участников в реестр не попадает; без протокола — отмечено;
8. замечание без ответственного и срока не исполняется; просроченное — ошибка;
9. реестр подписывает человек с ФИО и ролью, правка после подписи аннулирует подпись.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent.correspondence import (build_involvement_registry,  # noqa: E402
                                      build_letter_registry, build_quality_days, sign, write)
from pto_agent.fingerprint import check_signature                    # noqa: E402

violations: list[str] = []
stats = {"rules": 0, "letters": 0, "awaiting": 0, "records": 0, "events": 0,
         "open_remarks": 0, "errors": 0}
TODAY = date(2026, 11, 6)


def rule(ok: bool, message: str) -> None:
    stats["rules"] += 1
    if not ok:
        violations.append(message)


def codes(document) -> list[str]:
    return [c["code"] for c in document["checks"]]


demo = ROOT / "data" / "demo" / "correspondence"
load = lambda name: json.loads((demo / name).read_text(encoding="utf-8"))
letters, involvement, days = load("letters.json"), load("involvement.json"), load("quality_days.json")

# 1–4: письма
registry = build_letter_registry(letters["letters"], object_name=letters["object"],
                                 number="ПИС-1", today=TODAY)
stats.update({"letters": registry["totals"]["letters"],
              "awaiting": registry["totals"]["awaiting"],
              "errors": registry["totals"]["errors"]})
rule(registry["totals"]["letters"] == 6, f"писем {registry['totals']['letters']}, ожидалось 6")
rule("LETTER_NO_ADDRESSEE" in codes(registry) or
     build_letter_registry([{"number": "1", "date": "2026-10-01", "subject": "x"}],
                           today=TODAY)["totals"]["rejected"] == 1,
     "письмо без адресата принято в реестр")
rule("LETTER_NO_BASIS" in codes(registry), "письмо без основания не отмечено")
rule("LETTER_NO_SIGNER" in codes(registry), "письмо без подписи не отмечено")
rule("ANSWER_OVERDUE" in codes(registry), "просроченный ответ не отмечен")
rule(registry["totals"]["awaiting"] == 2, f"ждём ответа: {registry['totals']['awaiting']}, ожидалось 2")
in_time = build_letter_registry(
    [{"number": "300", "date": "2026-10-20", "kind": "запрос", "to": {"org": "ООО «М»"},
      "subject": "документы", "basis": {"doc": "договор", "clause": "п. 4.12"},
      "requires_answer": True, "due_date": "2026-12-01",
      "signed_by": {"by": "Иванов И.И.", "role": "начальник ПТО"}}],
    object_name="проба", today=TODAY)
rule(in_time["totals"]["errors"] == 0, "корректный запрос в срок дал ошибки")
rule("ANSWER_WITHOUT_REFERENCE" in codes(registry), "ответ без номера и даты не отмечен")

# 5–6: лаборатории и геодезисты
lab = build_involvement_registry(involvement["records"], object_name=involvement["object"],
                                 number="ЛАБ-1", today=TODAY)
stats.update({"records": lab["totals"]["records"]})
rule("NO_ACCREDITATION" in codes(
    build_involvement_registry([{"kind": "лаборатория", "organization": "X",
                                 "request": {"number": "З-1", "date": "2026-10-01"},
                                 "result": {"number": "П-1", "date": "2026-10-02",
                                            "linked_to": "партия 1"}}],
                               object_name="проба", today=TODAY)),
     "лаборатория без аттестата принята")
rule("ACCREDITATION_EXPIRED" in codes(lab), "просроченный аттестат не отмечен")
rule(lab["totals"]["expired_accreditation"] == 1, "просроченных аттестатов больше или меньше одного")
rule("RESULT_UNLINKED" in codes(lab), "результат без привязки не отмечен")
rule("RESULT_MISSING" in codes(lab) or "RESULT_OVERDUE" in codes(lab),
     "заявка без результата не отмечена")

# 7–8: авторские дни и дни качества
quality = build_quality_days(days["events"], object_name=days["object"], number="ДК-1",
                             today=TODAY)
stats.update({"events": quality["totals"]["events"],
              "open_remarks": quality["totals"]["open_remarks"]})
rule(quality["totals"]["events"] == 3, f"мероприятий {quality['totals']['events']}, ожидалось 3")
rule(build_quality_days([{"kind": "день качества", "date": "2026-10-01"}],
                        object_name="проба", today=TODAY)["totals"]["rejected"] == 1,
     "мероприятие без участников принято в реестр")
rule("EVENT_NO_PROTOCOL" in codes(quality), "мероприятие без протокола не отмечено")
rule("EVENT_REMARK_NO_RESPONSIBLE" in codes(quality), "замечание без ответственного не отмечено")
rule("EVENT_REMARK_OVERDUE" in codes(quality), "просроченное замечание не отмечено")
rule(quality["totals"]["open_remarks"] == 2,
     f"открытых замечаний {quality['totals']['open_remarks']}, ожидалось 2")

# 9: подпись
anonymous = sign(lab, by="", role="")
rule(bool(anonymous["sign_problems"]), "анонимная подпись принята")
refused = sign(registry, by="Иванов И.И.", role="начальник ПТО")
rule(bool(refused["sign_problems"]), "реестр писем с ошибками подписан")
clean = build_quality_days(
    [{"kind": "день качества", "date": "2026-10-09",
      "participants": [{"org": "ООО «Монолит»", "by": "Соколов И.И.", "role": "прораб"}],
      "protocol": {"number": "ДК-2", "date": "2026-10-09"},
      "remarks": [{"text": "накрыть бетон", "responsible": "ООО «Монолит»",
                   "due": "2026-10-20", "closed_at": "2026-10-18"}]}],
    object_name="проба", number="ДК-2", today=TODAY)
rule(clean["totals"]["errors"] == 0, "чистый день качества дал ошибки: "
     + "; ".join(c["message"] for c in clean["checks"] if c["level"] == "error"))
signed = sign(clean, by="Иванов И.И.", role="начальник ПТО", reason="сверено с протоколом")
rule(signed["can_close"]["allowed"] is True, "подписанный чистый реестр не закрывается: "
     + "; ".join(signed["can_close"]["reasons"]))
rule(check_signature(signed)["valid"] is True, "подпись недействительна сразу после подписания")
edited = json.loads(json.dumps(signed))
edited["events"][0]["remarks"][0]["closed_at"] = "2026-10-25"
rule(check_signature(edited)["changed_after_confirm"] is True, "правка после подписи не видна")

out = ROOT / "out" / "correspondence_check"
for document in (registry, lab, quality, signed):
    write(document, out)
markdown = (out / "quality_days_ДК-2.md").read_text(encoding="utf-8").lower()
for phrase in ("замечаний нет", "мероприятие проведено в полном объёме"):
    rule(phrase not in markdown, f"quality_days_ДК-2.md: формулировка «{phrase}»")

print(f"переписка и участники: правил {stats['rules']} · писем {stats['letters']} "
      f"(ждём ответа {stats['awaiting']}) · заявок {stats['records']} · мероприятий "
      f"{stats['events']} (открытых замечаний {stats['open_remarks']}) · ошибок в письмах "
      f"{stats['errors']} · подпись: {signed['confirmed_by']['by'] if signed.get('confirmed_by') else '—'}")
if violations:
    print("НАРУШЕНИЯ:")
    for violation in violations:
        print(" ✗ " + violation)
    sys.exit(1)
print("нарушений: 0")
