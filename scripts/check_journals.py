# -*- coding: utf-8 -*-
"""Сквозная проверка журналов на демо-объекте — шаг CI.

Правила, которые проверяются:

1. общий журнал собирается из записей смен и не теряет записи молча;
2. запись без даты/смены/исполнителя/описания в журнал не попадает, и это видно;
3. даты идут по возрастанию, температура обязательна там, где она объясняет материал;
4. журнал нельзя закрыть без подписи человека с ФИО и ролью;
5. подпись возможна только при отсутствии ошибок, а правка после подписи её аннулирует;
6. входной контроль: «допущен» без документа качества и по просроченному документу невозможен;
7. списание материала без записи входного контроля показывается отдельно;
8. ни в одном файле журнала нет формулировок о приёмке работ.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent.cumulative import load_acts                                    # noqa: E402
from pto_agent.fingerprint import check_signature                            # noqa: E402
from pto_agent.journals import (build_verification_journal, build_work_journal,  # noqa: E402
                                sign, write)

FORBIDDEN = ("выполнение подтверждено", "работы приняты", "готова к сдаче")
violations: list[str] = []
stats = {"rules": 0, "entries": 0, "records": 0, "blocked": 0, "unverified": 0}


def rule(ok: bool, message: str) -> None:
    stats["rules"] += 1
    if not ok:
        violations.append(message)


demo = ROOT / "data" / "demo" / "journals"
entries = json.loads((demo / "work_entries.json").read_text(encoding="utf-8"))
records = json.loads((demo / "verification.json").read_text(encoding="utf-8"))
acts, act_problems = load_acts(ROOT / "data" / "demo" / "acts" / "acts.json")
for problem in act_problems:
    violations.append(f"акты: {problem}")

work = build_work_journal(entries["entries"], object_name=entries["object"], number="ОЖР-1",
                          acts=acts or None)
stats["entries"] = work["totals"]["entries"]

# 1–3: полнота, хронология, погода
rule(work["totals"]["entries"] == len(entries["entries"]),
     f"часть записей потерялась: {work['totals']['entries']} из {len(entries['entries'])}")
rule(work["totals"]["rejected"] == 0, f"в демо-журнале отклонённых записей: {work['totals']['rejected']}")
rule(work["totals"]["errors"] == 0,
     "ошибки в демо-журнале: " + "; ".join(c["message"] for c in work["checks"]
                                            if c["level"] == "error"))
rule(all(e["work_type"] for e in work["entries"]),
     "вид работ не определён: " + ", ".join(e["date"] for e in work["entries"] if not e["work_type"]))
rule(work["totals"]["with_weather"] == work["totals"]["entries"],
     "не у всех записей указана погода")

# неполная запись не проходит молча
gap = build_work_journal([{"date": "2026-10-01", "shift": "1", "qty": 10}], object_name="проба")
rule(gap["totals"]["entries"] == 0 and gap["totals"]["rejected"] == 1,
     "запись без исполнителя и описания попала в журнал")

# дата меньше предыдущей — ошибка
back = build_work_journal([{"date": "2026-10-05", "shift": "1", "work": "Разработка грунта",
                            "executor": "ООО «Земстрой»", "weather": {"temp_c": 5}},
                           {"date": "2026-09-30", "shift": "1", "work": "Разработка грунта",
                            "executor": "ООО «Земстрой»", "weather": {"temp_c": 5}}],
                          object_name="проба")
rule(any(c["code"] == "OUT_OF_ORDER" and c["level"] == "error" for c in back["checks"]),
     "запись задним числом в конец журнала прошла без ошибки")

# 4–5: подпись и закрытие
rule(work["can_close"]["allowed"] is False, "неподписанный журнал считается закрытым")
anonymous = sign(work, by="", role="")
rule(bool(anonymous["sign_problems"]), "анонимная подпись принята")
refused = sign(back, by="Иванов И.И.", role="начальник ПТО")
rule(bool(refused["sign_problems"]), "журнал с ошибками подписан")
signed = sign(work, by="Иванов И.И.", role="начальник ПТО", reason="сверено с актами")
rule(signed["can_close"]["allowed"] is True, "подписанный журнал без ошибок не закрывается: "
     + "; ".join(signed["can_close"]["reasons"]))
rule(check_signature(signed)["valid"] is True, "подпись недействительна сразу после подписания")
edited = json.loads(json.dumps(signed))
edited["entries"][0]["qty"] = 999
rule(check_signature(edited)["changed_after_confirm"] is True, "правка после подписи не видна")

# 6–7: входной контроль
control = build_verification_journal(records["records"], object_name=records["object"],
                                     number="ВК-1",
                                     writeoffs=[{"code": "Керамзит", "qty": 12, "unit": "м3"},
                                                {"code": "Бетон B25", "qty": 60, "unit": "м3"}])
stats["records"] = control["totals"]["records"]
codes = [c["code"] for c in control["checks"]]
rule("ACCEPTED_WITHOUT_DOC" in codes, "допуск арматуры без документа качества не отмечен")
rule("CERT_EXPIRED" in codes, "просроченный сертификат мастики не отмечен")
rule(control["totals"]["accepted"] == 2,
     f"допущенных партий {control['totals']['accepted']}, ожидалось 2")
rule(control["totals"]["accepted"] <= control["totals"]["records"],
     "допущено партий больше, чем проверено")
stats["blocked"] = control["totals"]["errors"]
stats["unverified"] = len(control["unverified_writeoffs"])
rule([u["material"] for u in control["unverified_writeoffs"]] == ["Керамзит"],
     "списание без входного контроля показано неверно")
rule(control["can_close"]["allowed"] is False, "журнал с ошибками закрывается")

# 8: файлы и формулировки
out = ROOT / "out" / "journals_check"
files = write(signed, out)
files.update(write(control, out))
_ = files
for name in ("journal_work_ОЖР-1", "journal_verification_ВК-1"):
    markdown = (out / f"{name}.md").read_text(encoding="utf-8").lower()
    for phrase in FORBIDDEN:
        rule(phrase not in markdown, f"{name}.md: формулировка «{phrase}» — журнал не принимает работы")

print(f"журналы: правил {stats['rules']} · записей журнала {stats['entries']} · "
      f"проверок партий {stats['records']} · ошибок в контроле {stats['blocked']} · "
      f"списано без контроля {stats['unverified']} · подпись: "
      f"{signed['confirmed_by']['by'] if signed.get('confirmed_by') else '—'}")
if violations:
    print("НАРУШЕНИЯ:")
    for v in violations:
        print(" ✗ " + v)
    sys.exit(1)
print("нарушений: 0")
