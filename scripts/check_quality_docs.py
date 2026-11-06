# -*- coding: utf-8 -*-
"""Сквозная проверка документов качества на демо-объекте — шаг CI.

Правила, которые проверяются:

1. документ без типа, номера, даты или материала в реестр не принимается;
2. товарный чек документом качества не считается;
3. документ на другую партию не закрывает применённую партию;
4. срок действия сверяется с датой применения, а не с сегодняшним днём;
5. материал, применённый без документа на свою партию, — ошибка;
6. состав документов берётся из файла требований; нет требования — «не заявлено»;
7. копия без отметки «копия верна» не даёт передать комплект;
8. стык с журналом входного контроля: допуск по документу вне реестра — ошибка;
9. комплект передаётся только подписанным и без ошибок, правка после подписи аннулирует подпись;
10. ни в одном файле нет формулировок о подтверждённом качестве материалов.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent.fingerprint import check_signature                       # noqa: E402
from pto_agent.quality_docs import (COVER_FULL, COVER_INVALID, COVER_MISSING,  # noqa: E402
                                    COVER_UNDECLARED, build_registry, can_transfer, sign, write)

FORBIDDEN = ("качество подтверждено", "материалы приняты", "допущен без замечаний")
violations: list[str] = []
stats = {"rules": 0, "documents": 0, "rejected": 0, "materials": 0, "batches": 0,
         "covered": 0, "errors": 0}


def rule(ok: bool, message: str) -> None:
    stats["rules"] += 1
    if not ok:
        violations.append(message)


def codes(registry) -> list[str]:
    return [c["code"] for c in registry["checks"]]


demo = ROOT / "data" / "demo" / "quality_docs"
load = lambda name: json.loads((demo / name).read_text(encoding="utf-8"))
documents, requirements, usa = load("documents.json"), load("requirements.json"), load("usage.json")
verification = json.loads((ROOT / "data" / "demo" / "journals" / "verification.json")
                          .read_text(encoding="utf-8"))

registry = build_registry(documents["documents"], object_name=documents["object"], number="ДК-1",
                          requirements=requirements["requirements"], usage=usa["usage"],
                          verification=verification["records"])
totals = registry["totals"]
stats.update({"documents": totals["documents"], "rejected": totals["rejected"],
              "materials": totals["materials"], "batches": totals["batches"],
              "covered": totals["covered_batches"], "errors": totals["errors"]})

# 1–2: реквизиты и тип документа
rule(totals["rejected"] == 2, f"отклонено документов {totals['rejected']}, ожидалось 2 — "
                              f"паспорт без номера и товарный чек")
rule("DOC_INCOMPLETE" in codes(registry), "документ без номера принят в реестр")
rule("DOC_TYPE_UNKNOWN" in codes(registry), "товарный чек посчитан документом качества")

# 3: партия
rule("DOC_BATCH_MISMATCH" in codes(registry), "протокол на партию п-11 закрыл применённую п-12")

# 4: срок действия против даты применения
rule("DOC_EXPIRED" in codes(registry), "просроченный сертификат мастики прошёл без находки")
fresh = build_registry(
    [{"kind": "сертификат соответствия", "number": "С-1", "issue_date": "2026-09-01",
      "valid_until": "2026-12-31", "material": "Мастика", "batch": "п-1",
      "file": "x.pdf", "attested_by": {"by": "Иванов И.И.", "role": "инженер ПТО"}}],
    object_name="проба", usage=[{"material": "Мастика", "batch": "п-1", "qty": 10, "unit": "кг",
                                 "applied_from": "2026-10-03"}],
    requirements=[{"material": "Мастика", "required": ["сертификат соответствия"],
                   "basis": "договор"}],
    today=date(2027, 1, 1))
rule("DOC_EXPIRED" not in codes(fresh),
     "документ, действовавший на дату применения, признан просроченным только потому, что сегодня позже")

# 5: материал без документа на свою партию
rule("MATERIAL_WITHOUT_DOC" in codes(registry), "песок применён партией п-12 без документа")
statuses = {m["material"]: m["status"] for m in registry["materials"]}
rule(statuses.get("Песок строительный") != COVER_FULL, "песок показан как укомплектованный")
rule(statuses.get("Мастика битумная") == COVER_INVALID,
     f"мастика со сторнированным сертификатом: {statuses.get('Мастика битумная')}")
rule(statuses.get("Бетон B25") == COVER_FULL, "бетон с паспортом и протоколом не признан комплектным")

# 6: требование не заявлено
undeclared = build_registry(
    [{"kind": "паспорт качества", "number": "П-1", "issue_date": "2026-09-01",
      "material": "Грунтовка", "batch": "п-1", "file": "g.pdf",
      "attested_by": {"by": "Иванов И.И.", "role": "инженер ПТО"}}],
    object_name="проба", usage=[{"material": "Грунтовка", "batch": "п-1", "qty": 5, "unit": "л",
                                 "applied_from": "2026-10-01"}])
rule(undeclared["materials"][0]["status"] == COVER_UNDECLARED,
     "без файла требований материал показан как укомплектованный")
rule("REQUIREMENT_UNDECLARED" in codes(undeclared), "отсутствие требований не отмечено")
rule(build_registry([], object_name="проба", usage=[{"material": "Песок", "batch": "п-9",
                                                    "qty": 1, "unit": "м3"}],
                    requirements=[{"material": "Песок", "required": ["протокол испытаний"],
                                   "basis": "проект, общие указания"}])
     ["materials"][0]["status"] == COVER_MISSING, "материал без единого документа не отмечен")

# 7: заверение копий
rule("DOC_NOT_ATTESTED" in codes(registry), "копия без отметки «копия верна» не отмечена")
rule(registry["can_transfer"]["allowed"] is False, "неподписанный комплект разрешён к передаче")

# 8: стык с журналом входного контроля
rule("VERIFICATION_WITHOUT_DOC" in codes(registry),
     "допуск арматуры без документа в реестре не отмечен")
rule("DOC_WITHOUT_VERIFICATION" in codes(registry),
     "документ без записи входного контроля не отмечен")

# 9: подпись
anonymous = sign(registry, by="", role="")
rule(bool(anonymous["sign_problems"]), "анонимная подпись принята")
refused = sign(registry, by="Иванов И.И.", role="начальник ПТО")
rule(bool(refused["sign_problems"]), "комплект с ошибками подписан")
clean = build_registry(
    [{"kind": "паспорт качества", "number": "П-11", "issue_date": "2026-09-01",
      "material": "Бетон B25", "batch": "партия 14", "standard": "ГОСТ 7473-2010",
      "file": "b.pdf", "attested_by": {"by": "Иванов И.И.", "role": "инженер ПТО"}},
     {"kind": "протокол испытаний", "number": "ПИ-11", "issue_date": "2026-10-02",
      "material": "Бетон B25", "batch": "партия 14", "file": "pi.pdf",
      "attested_by": {"by": "Иванов И.И.", "role": "инженер ПТО"}}],
    object_name="проба", number="ДК-2",
    usage=[{"material": "Бетон B25", "batch": "партия 14", "qty": 200, "unit": "м3",
            "applied_from": "2026-10-04"}],
    requirements=[{"material": "Бетон B25", "required": ["паспорт качества", "протокол испытаний"],
                   "basis": "договор Д-142/2026, прил. 3, п. 4.12"}],
    verification=[{"material": "Бетон B25", "batch": "партия 14", "decision": "допущен",
                   "quality_doc": {"kind": "паспорт качества", "number": "П-11"}}])
rule(clean["totals"]["errors"] == 0, "чистый комплект дал ошибки: "
     + "; ".join(c["message"] for c in clean["checks"] if c["level"] == "error"))
signed = sign(clean, by="Иванов И.И.", role="начальник ПТО", reason="сверил с партиями")
rule(signed["can_transfer"]["allowed"] is True, "подписанный комплект без ошибок не передаётся: "
     + "; ".join(signed["can_transfer"]["reasons"]))
rule(check_signature(signed)["valid"] is True, "подпись недействительна сразу после подписания")
edited = json.loads(json.dumps(signed))
edited["documents"][0]["valid_until"] = "2030-01-01"
rule(check_signature(edited)["changed_after_confirm"] is True, "правка после подписи не видна")
rule(can_transfer(edited)["allowed"] is False, "изменённый комплект разрешён к передаче")

# 10: файлы и формулировки
out = ROOT / "out" / "quality_check"
write(signed, out)
write(registry, out)
markdown = (out / "quality_ДК-2.md").read_text(encoding="utf-8").lower()
for phrase in FORBIDDEN:
    rule(phrase not in markdown, f"quality_ДК-2.md: формулировка «{phrase}» — качество не подтверждается файлом")
registry_json = json.loads((out / "quality_ДК-2.json").read_text(encoding="utf-8"))
rule(bool((registry_json.get("types_profile") or {}).get("disclaimer")),
     "в файле нет оговорки о том, что перечень типов — рабочий, а не нормативный")

print(f"документы качества: правил {stats['rules']} · документов {stats['documents']} · "
      f"отклонено {stats['rejected']} · материалов {stats['materials']} · "
      f"партий с документом {stats['covered']} из {stats['batches']} · "
      f"ошибок в реестре {stats['errors']} · подпись: "
      f"{signed['confirmed_by']['by'] if signed.get('confirmed_by') else '—'}")
if violations:
    print("НАРУШЕНИЯ:")
    for violation in violations:
        print(" ✗ " + violation)
    sys.exit(1)
print("нарушений: 0")
