# -*- coding: utf-8 -*-
"""Сквозная проверка разделов 4–6, 12 и дополнительных листов книг.

Вопросы, на которые скрипт отвечает числом:

1. подтверждённая в периоде операция идёт в раздел 4, подтверждённая раньше — в раздел 5 и
   только один раз;
2. пока 180 дней не истекли, начисления нет; после — налог считается по ставке на дату отгрузки;
3. код операции не подставляется «по памяти»: строка помечается, сумма при этом считается;
4. раздел 12 требует ИНН с верной контрольной суммой и сходящийся НДС;
5. дополнительный лист книги — две записи «до» и «после», без причины и без исходного
   документа не создаётся, повтор дубля не даёт;
6. приложение № 1 к разделу 8/9 переносит итог дополнительного листа, а не всей книги;
7. чего в контуре нет — написано.

Запуск: `python3 scripts/check_vat_declaration.py`. Код возврата 1, если есть нарушения.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import invoices, tax, vat_declaration  # noqa: E402

checks = {"done": 0}
violations: list[str] = []


def check(condition: bool, message: str) -> None:
    checks["done"] += 1
    if not condition:
        violations.append(message)


EXPORT = {"document": "ГТД 10005030/150726/0012345", "date": "2026-07-15",
          "amount": 4_800_000, "code": "1010401", "vat_input": 620_000,
          "confirmed": True, "confirmed_date": "2026-10-20"}

# ── 1. справочник ────────────────────────────────────────────────────────────
rules = vat_declaration.load_rules()
check(rules.get("schema") == vat_declaration.SCHEMA,
      f"схема справочника {rules.get('schema')} вместо {vat_declaration.SCHEMA}")
sections = rules.get("sections") or {}
for key in ("4", "5", "6", "12", "appendix8", "appendix9"):
    check(key in sections and bool((sections.get(key) or {}).get("rows")),
          f"в справочнике нет строк раздела {key}")
check((rules.get("thresholds") or {}).get("zero_rate_days") == 180,
      "срок подтверждения нулевой ставки не 180 дней")
check(bool(rules.get("not_implemented")), "список «не сделано» пуст")
check((rules.get("additional_sheets") or {}).get("knd_verify") is True,
      "номер КНД дополнительного листа не помечен «на проверку»")

# ── 2. раздел 4 ──────────────────────────────────────────────────────────────
report = vat_declaration.zero_rate("2026-10", exports=[dict(EXPORT)])
check(report["totals"]["base4"] == 4_800_000, "подтверждённая операция не попала в раздел 4")
check(report["totals"]["base6"] == 0, "подтверждённая операция попала в раздел 6")
check(report["problems"] == [], f"расхождения раздела 4: {report['problems']}")
check(all(item["status"] == vat_declaration.OK for item in report["checks"]),
      "контрольные соотношения раздела 4 не сходятся")

# ── 3. срок 180 дней ─────────────────────────────────────────────────────────
pending = vat_declaration.zero_rate(
    "2026-10", exports=[dict(EXPORT, confirmed=False, confirmed_date="", date="2026-09-01")])
check(pending["totals"]["accrued6"] == 0, "налог начислен до истечения 180 дней")
check(any("истекает" in gap["why"] for gap in pending["gaps"]), "нет запроса по неистёкшему сроку")

expired = vat_declaration.zero_rate(
    "2026-10", exports=[dict(EXPORT, confirmed=False, confirmed_date="", date="2026-03-10",
                             amount=1_200_000, vat_input=180_000)])
row = expired["sections"]["6"]["rows"][0]
lawful = float(tax.vat_rate_on("2026-03-10", tax.load_rates())["rate"])
check(row["rate"] == lawful, f"ставка в разделе 6 {row['rate']} вместо {lawful}")
check(abs(float(expired["totals"]["accrued6"]) - 1_200_000 * lawful / 100) < 0.01,
      "налог раздела 6 не равен базе × ставке")

# ── 4. раздел 5 без двойного счёта ───────────────────────────────────────────
earlier = vat_declaration.zero_rate("2026-10",
                                    exports=[dict(EXPORT, confirmed_date="2026-08-20")])
check(earlier["totals"]["base5"] == 4_800_000, "ранее подтверждённая операция не ушла в раздел 5")
claimed = vat_declaration.zero_rate(
    "2026-10", exports=[dict(EXPORT, confirmed_date="2026-08-20", claimed=True)])
check(claimed["totals"]["base5"] == 0, "уже заявленный вычет посчитан второй раз")

# ── 5. код и документ ────────────────────────────────────────────────────────
no_code = vat_declaration.zero_rate("2026-10", exports=[dict(EXPORT, code="")])
check(no_code["totals"]["base4"] == 4_800_000, "сумма потеряна из-за отсутствия кода")
check(no_code["sections"]["4"]["rows"][0]["verify"] is True,
      "строка без кода не помечена «на проверку»")
check(any("код операции" in gap["what"] for gap in no_code["gaps"]), "нет запроса кода операции")
no_doc = vat_declaration.zero_rate("2026-10", exports=[{"amount": 1_000}])
check(no_doc["totals"]["base4"] == 0, "операция без документа попала в раздел 4")
empty = vat_declaration.zero_rate("2026-10", exports=[])
check(any("разделы 4–6 не заполняются" in gap["why"] for gap in empty["gaps"]),
      "пустой реестр не объяснён")

# ── 6. раздел 12 ────────────────────────────────────────────────────────────
ROW12 = {"number": "1", "date": "2026-10-12", "buyer": "ООО «Ромашка»",
         "buyer_inn": "7810447576", "buyer_kpp": "781001001",
         "net": 500_000, "vat": 110_000, "code": "01"}
s12 = vat_declaration.section12("2026-10", invoices=[dict(ROW12)])
check(s12["ready"] is True and s12["totals"]["vat"] == 110_000, "раздел 12 не собрался")
check(s12["totals"]["to_line_030"] == s12["totals"]["vat"],
      "сумма раздела 12 не переносится в строку 030")
bad = vat_declaration.section12("2026-10", invoices=[dict(ROW12, buyer_inn="1234567890")])
check([p["code"] for p in bad["problems"]] == ["SECTION12_BAD_INN"], "битый ИНН не пойман")
wrong_vat = vat_declaration.section12("2026-10", invoices=[dict(ROW12, vat=100_000)])
check([p["code"] for p in wrong_vat["problems"]] == ["SECTION12_VAT"], "НДС раздела 12 не сверен")
empty12 = vat_declaration.section12("2026-10")
check(any("п. 5 ст. 173" in gap["why"] for gap in empty12["gaps"]), "пустой раздел 12 не объяснён")

# ── 7. дополнительный лист книги ────────────────────────────────────────────
payload = json.loads((ROOT / "data/demo/accounting/book_fix_2026_10.json").read_text("utf-8"))
doc = payload["invoice"]
state = invoices.new_state(object_name="ЖК «Северный», корпус 2")
invoices.register_purchase(state, number=doc["number"], date=doc["date"],
                           base_number=doc["base_number"], counterparty=doc["counterparty"],
                           inn=doc["inn"], net=doc["net"], rate=doc["rate"])
base_fix = {"sheet": "purchases", "base_period": "2026-09", "number": doc["number"],
            "date": doc["date"], "net_before": 900_000, "vat_before": 198_000,
            "net_after": 820_000, "vat_after": 180_400,
            "reason": "неверная стоимость по акту сверки", "entry_date": "2026-10-25"}
no_reason = vat_declaration.register_book_fix(state, **dict(base_fix, reason=""))
check(no_reason["status"] == "отклонён" and no_reason["gaps"],
      "исправление книги без основания не отклонено")
future = vat_declaration.register_book_fix(state, **dict(base_fix, base_period="2026-10"))
check([e["code"] for e in future["errors"]] == ["BOOK_FIX_NOT_PAST"],
      "исправление текущего периода принято как доп. лист")
absent = vat_declaration.register_book_fix(state, **dict(base_fix, number="СФ №999"))
check([e["code"] for e in absent["errors"]] == ["BOOK_FIX_NO_DOCUMENT"],
      "исправление несуществующего документа принято")
same = vat_declaration.register_book_fix(state, **dict(base_fix, net_after=900_000,
                                                       vat_after=198_000))
check([e["code"] for e in same["errors"]] == ["BOOK_FIX_SAME"], "пустое исправление принято")

first = vat_declaration.register_book_fix(state, **base_fix)
check(first["status"] == "записано", f"исправление книги не записано: {first['status']}")
second = vat_declaration.register_book_fix(state, **base_fix)
check(second["status"] == "уже записано", "повторное исправление создало дубль")
check(len(state.get("book_fixes") or []) == 1, "в реестре больше одной записи исправления")

sheet = vat_declaration.additional_sheets(state, "2026-10", sheet="purchases",
                                          book_total_vat=335_280)
check([r["kind"] for r in sheet["rows"]] == ["аннулируемая", "новая"],
      "доп. лист состоит не из двух записей")
check(abs(sheet["totals"]["delta_vat"] + 17_600) < 0.01,
      f"изменение НДС {sheet['totals']['delta_vat']} вместо −17 600")
appendix = {row["line"]: row["value"] for row in sheet["appendix"]["rows"]}
check(abs(float(appendix["190"]) + 17_600) < 0.01,
      "строка 190 приложения не равна изменению по доп. листу")
check(abs(float(appendix["005"]) - 335_280) < 0.01,
      "строка 005 приложения не равна итогу книги")
check(sheet["knd_verify"] is True, "номер КНД доп. листа не помечен «на проверку»")
check("Приложение № 1 к разделу 8" in vat_declaration.to_markdown(sheet),
      "в выгрузке нет заголовка приложения к разделу 8")
empty_sheet = vat_declaration.additional_sheets(invoices.new_state(), "2026-10")
check(any("лист не формируется" in gap["why"] for gap in empty_sheet["gaps"]),
      "отсутствие доп. листа не объяснено")

print(f"проверок: {checks['done']}, нарушений: {len(violations)}")
for item in violations:
    print(f"  ✗ {item}")
sys.exit(1 if violations else 0)
