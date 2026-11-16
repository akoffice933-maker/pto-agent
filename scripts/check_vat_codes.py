# -*- coding: utf-8 -*-
"""Проверки справочника кодов видов операций по НДС (приказ ФНС от 14.03.2016 № ММВ-7-3/136@).

Коды — это то, из-за чего книгу и журнал возвращают на доработку, если код не тот.
Поэтому проверяется не «код есть», а откуда он:

1. справочник переписан из приказа целиком (24 кода), у каждого — источник;
2. код для документа выводится правилом из справочника, а не «по смыслу» в коде программы;
3. для случая, которого правило не покрывает, код не выдумывается — возвращается `None` и
   причина, и документ попадает в «ПроверкаФайла» с номером;
4. код попадает и в книгу, и в журнал, и в выгрузку XML.

Запуск: `python3 scripts/check_vat_codes.py`. Код возврата 1, если есть нарушения.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import invoices  # noqa: E402

checks = {"done": 0}
violations: list[str] = []


def check(condition: bool, message: str) -> None:
    checks["done"] += 1
    if not condition:
        violations.append(message)


codes = invoices.load_operation_codes()

# ── 1. справочник ──────────────────────────────────────────────────────────
check("ММВ-7-3/136" in str(codes.get("source")),
      "источник справочника не назван приказом № ММВ-7-3/136@")
check(codes.get("loaded_from_primary_source") is True,
      "справочник не помечен как переписанный из первоисточника")
check(len(codes.get("codes") or []) == 24,
      f"в справочнике {len(codes.get('codes') or [])} кодов, в приказе — 24")
numbers = [row["code"] for row in codes["codes"]]
expected = ["01", "02", "06", "10", "13", "14", "15", "16", "17", "18", "19", "20", "21", "22",
            "23", "24", "25", "26", "27", "28", "29", "30", "31", "32"]
check(numbers == expected, f"коды в справочнике {numbers}, ожидали {expected}")
check(all(str(row.get("title") or "").strip() for row in codes["codes"]),
      "у части кодов нет наименования из приказа")
check(not any(code in ("03", "04", "05", "07", "08", "09", "11", "12") for code in numbers),
      "в справочнике остались коды старого приказа № ММВ-7-3/83@ (03, 04, 05, 07–09, 11, 12): "
      "их отменили с 01.07.2016")

# ── 2. правила вывода ──────────────────────────────────────────────────────
cases = [
    ({"kind": "shipped"}, "01", "отгрузка"),
    ({"kind": "advance"}, "02", "полученный аванс"),
    ({"kind": "purchase"}, "01", "приобретение"),
    ({"kind": "correction", "direction": "decrease"}, "18", "уменьшение"),
    ({"kind": "correction", "direction": "increase"}, "01", "увеличение"),
    ({"kind": "purchase", "agent": True}, "06", "налоговый агент"),
]
for kwargs, want, title in cases:
    answer = invoices.operation_code_for(**kwargs)
    check(answer.get("code") == want, f"{title}: код {answer.get('code')}, ожидали {want}")
    check(bool(answer.get("why")), f"{title}: правило не объяснено")
    check("ММВ-7-3/136" in str(answer.get("source")), f"{title}: у кода нет источника")
    check(answer.get("resolved") is True, f"{title}: код помечен как невыведенный")

agency = invoices.operation_code_for("purchase", agency=True)
check(agency.get("code") == "01", "посреднический СФ должен идти по коду 01")

# ── 3. чего правило не знает ───────────────────────────────────────────────
unknown = invoices.operation_code_for("невиданный-вид")
check(unknown.get("code") is None, "для неизвестного вида кода выдуман код")
check(unknown.get("resolved") is False, "невыведенный код помечен как выведенный")
check(codes.get("unresolved"), "в справочнике нет списка того, что правило не решает")

# ── 4. код в документах, книгах и выгрузке ─────────────────────────────────
state = invoices.new_state()
rules = invoices.load_rules()
issued = invoices.register(state, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                           base_number="КС-2 №38", counterparty="ООО «Заказчик-Инвест»",
                           net=34_106_600, rate=22, rules=rules)
check(issued["invoice"]["operation_code"] == "01",
      f"у выставленного СФ код {issued['invoice'].get('operation_code')}")
purchased = invoices.register_purchase(state, number="СФ №341", date="2026-10-01",
                                       base_number="УПД №118", base_date="2026-10-01",
                                       counterparty="ООО «БетонТранс»", net=1_426_000,
                                       rate=22)
check(purchased["invoice"]["operation_code"] == "01",
      f"у полученного СФ код {purchased['invoice'].get('operation_code')}")
manual = invoices.register_purchase(state, number="СФ №512", date="2026-10-03",
                                    base_number="УПД №120", base_date="2026-10-03",
                                    counterparty="ООО «СтройБаза»", net=98_000, rate=22,
                                    operation_code="13")
check(manual["invoice"]["operation_code"] == "13",
      "код, указанный человеком, не сохранился")
check("человек" in str(manual["invoice"].get("operation_code_why")),
      "не сказано, что код ввёл человек")

book = invoices.purchase_book(state, {"entries": []}, "2026-10")
check(all(row.get("operation_code") for row in book["rows"]),
      "в строках книги покупок есть строки без кода вида операции")
xml = invoices.to_fns_xml(book, rules=rules)
check("<КодВидОпер>01</КодВидОпер>" in xml, "в выгрузке книги покупок нет КодВидОпер")
check("<КодВидОпер>13</КодВидОпер>" in xml, "в выгрузке нет кода, введённого человеком")
check("КодВидОпер не определён" not in xml, "в выгрузке названы документы без кода — их быть не должно")

agency_state = invoices.new_state()
invoices.register_purchase(agency_state, number="СФ №77", date="2026-10-06",
                           base_number="УПД №130", base_date="2026-10-06",
                           counterparty="ООО «Заказчик-Инвест»", net=1_000_000, rate=22,
                           agency=True)
journal = invoices.intermediary_journal(agency_state, "2026-10")
check(journal["ready"] is True, "журнал не готов, хотя код вида операции выведен")
check(journal["part2"][0]["operation_code"] == "01",
      f"в журнале код {journal['part2'][0].get('operation_code')}")
xml_journal = invoices.to_fns_xml(journal, rules=rules)
check("<КодВидОпер>01</КодВидОпер>" in xml_journal, "в выгрузке журнала нет КодВидОпер")

print(f"справочник: {len(codes['codes'])} кодов из приказа № ММВ-7-3/136@, "
      f"правил вывода {len(codes.get('rules') or [])}, не решается правилом "
      f"{len(codes.get('unresolved') or [])}")
print(f"выведено: отгрузка → 01, аванс → 02, приобретение → 01, уменьшение → 18, "
      f"налоговый агент → 06; неизвестный вид → кода нет")
print(f"проверок: {checks['done']}")
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
