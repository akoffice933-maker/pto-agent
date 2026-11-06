# -*- coding: utf-8 -*-
"""Сквозная проверка движения документации (РД и согласование ИД) — шаг CI.

Правила:

1. передача без даты или получателя в реестр не попадает;
2. работать можно только по редакции со штампом «В производство работ»;
3. передача без подписи получившего не подтверждена;
4. выдача заменённой редакции в производство — ошибка;
5. ссылка в работах на невыданную РД — ошибка, по выданной — нет;
6. комплект с открытыми замечаниями не подписывается;
7. повторная передача без устранения — та же передача;
8. подпись без ФИО и роли отклоняется, правка после подписи аннулирует подпись;
9. ни в одном файле нет формулировок «работы разрешены» / «документация в полном объёме».
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent.docflow import (build_rd_registry, build_submittal_registry,  # noqa: E402
                               sign, write)
from pto_agent.fingerprint import check_signature                            # noqa: E402

FORBIDDEN = ("работы разрешены", "документация выдана в полном объёме", "согласовано без замечаний")
violations: list[str] = []
stats = {"rules": 0, "transmittals": 0, "rejected": 0, "documents": 0, "submittals": 0,
         "open_remarks": 0, "errors": 0}
TODAY = date(2026, 11, 6)


def rule(ok: bool, message: str) -> None:
    stats["rules"] += 1
    if not ok:
        violations.append(message)


def codes(document) -> list[str]:
    return [c["code"] for c in document["checks"]]


demo = ROOT / "data" / "demo" / "docflow"
load = lambda name: json.loads((demo / name).read_text(encoding="utf-8"))
tr, rev, works = load("transmittals.json"), load("revisions.json"), load("works_rd.json")
submittals = load("submittals.json")

registry = build_rd_registry(tr["transmittals"], revisions=rev["revisions"],
                             works=works["works"], object_name=tr["object"], number="РД-1")
stats.update({"transmittals": registry["totals"]["transmittals"],
              "rejected": registry["totals"]["rejected"],
              "documents": registry["totals"]["documents"],
              "errors": registry["totals"]["errors"]})

# 1–2: реквизиты передачи и штамп
rule(registry["totals"]["rejected"] == 1, "передача без даты и получателя не отклонена")
rule("RD_NO_DATE" in codes(registry), "передача без даты принята в реестр")
rule("RD_WITHOUT_STAMP" in codes(registry), "передача без штампа не отмечена")
rule(any(d["status"] == "не выдана" for d in registry["documents"]),
     "документ без действующей выдачи не отмечен")

# 3–4: подпись получившего и редакция
rule("RD_WITHOUT_RECEIPT" in codes(registry), "передача без подписи получившего не отмечена")
rule("RD_REPLACED_IN_WORK" in codes(registry), "выдача заменённой редакции прошла без ошибки")
fresh = build_rd_registry(
    [{"doc": "КЖ-1", "revision": "изм. 1", "date": "2026-09-26", "recipient": "ООО «Монолит»",
      "stamp": "в производство работ", "receipt": {"by": "Соколов И.И."}}],
    revisions=[{"doc": "КЖ-1", "revision": "изм. 1", "current": True}],
    works=[{"work": "Плита", "rd": "КЖ-1"}], object_name="проба")
rule(fresh["totals"]["errors"] == 0, "корректная выдача дала ошибки: "
     + "; ".join(c["message"] for c in fresh["checks"] if c["level"] == "error"))
rule(fresh["can_work"]["allowed"] is True, "по корректной выдаче нельзя работать")

# 5: ссылки работ на РД
messages = [c["message"] for c in registry["checks"] if c["code"] == "WORK_WITHOUT_RD"]
rule(any("2026-14-КЖ2" in m for m in messages), "работа по РД без штампа не отмечена")
rule(any("2026-14-ОВ" in m for m in messages), "работа по невыданной РД не отмечена")
rule(not any("2026-14-КЖ " in m or "«2026-14-КЖ»" in m for m in messages),
     "работа по действующей РД признана ошибкой")

# 6–8: согласование
sub = build_submittal_registry(submittals["submittals"], object_name=submittals["object"],
                               number="СГ-1", today=TODAY)
stats.update({"submittals": sub["totals"]["submittals"],
              "open_remarks": sub["totals"]["with_open_remarks"]})
rule("SIGNED_WITH_OPEN_REMARKS" in codes(sub),
     "комплект, подписанный при открытых замечаниях, не отмечен")
rule("REMARK_OVERDUE" in codes(sub), "просроченные замечания не отмечены")
rule("RESUBMIT_WITHOUT_FIX" in codes(sub), "повторная передача без устранения не отмечена")
rule(sub["can_close"]["allowed"] is False, "реестр с открытыми замечаниями закрыт")
anonymous = sign(sub, by="", role="")
rule(bool(anonymous["sign_problems"]), "анонимная подпись принята")
refused = sign(sub, by="Иванов И.И.", role="начальник ПТО")
rule(bool(refused["sign_problems"]), "реестр с открытыми замечаниями подписан")

clean = build_submittal_registry(
    [{"title": "Комплект ИД: земляные работы", "act_ref": "АОСР-1", "sent_at": "2026-10-06",
      "recipient": "технадзор", "receipt": {"by": "Кузнецов В.В."}, "remarks": [],
      "signature": {"by": "Кузнецов В.В.", "role": "инженер технадзора",
                    "decision": "согласовано", "at": "2026-10-08"}}],
    object_name="проба", number="СГ-2")
rule(clean["totals"]["errors"] == 0, "чистый комплект дал ошибки: "
     + "; ".join(c["message"] for c in clean["checks"] if c["level"] == "error"))
signed = sign(clean, by="Иванов И.И.", role="начальник ПТО", reason="сверено с замечаниями")
rule(signed["can_close"]["allowed"] is True, "подписанный чистый реестр не закрывается: "
     + "; ".join(signed["can_close"]["reasons"]))
rule(check_signature(signed)["valid"] is True, "подпись недействительна сразу после подписания")
edited = json.loads(json.dumps(signed))
edited["submittals"][0]["remarks"] = [{"text": "добавлено после подписи"}]
rule(check_signature(edited)["changed_after_confirm"] is True, "правка после подписи не видна")

# 9: файлы и формулировки
out = ROOT / "out" / "docflow_check"
write(registry, out)
write(signed, out)
for name in ("rd_РД-1", "submittals_СГ-2"):
    markdown = (out / f"{name}.md").read_text(encoding="utf-8").lower()
    for phrase in FORBIDDEN:
        rule(phrase not in markdown, f"{name}.md: формулировка «{phrase}» — документация не подтверждается файлом")

print(f"движение документации: правил {stats['rules']} · передач РД {stats['transmittals']} "
      f"(отклонено {stats['rejected']}, документов {stats['documents']}) · комплектов ИД "
      f"{stats['submittals']} (с открытыми замечаниями {stats['open_remarks']}) · "
      f"ошибок в РД {stats['errors']} · подпись: "
      f"{signed['confirmed_by']['by'] if signed.get('confirmed_by') else '—'}")
if violations:
    print("НАРУШЕНИЯ:")
    for violation in violations:
        print(" ✗ " + violation)
    sys.exit(1)
print("нарушений: 0")
