# -*- coding: utf-8 -*-
"""Проверки книги покупок, корректировочных СФ, журнала посредников и выгрузки в ФНС.

Скрипт отвечает на вопросы, из-за которых вычет в декларации расходится с книгой:

1. книга покупок опирается на проводки, а не на память: НДС по строке сверяется с дебетом
   19.03 по документу-основанию, и остаток без счёта-фактуры называется числом;
2. счёт-фактура без проведённого основания даёт `PURCHASE_NO_BASE_DOC`, а не тихий вычет;
3. уменьшение в корректировочном СФ уменьшает книгу, увеличение — увеличивает, и причина
   обязательна: без неё корректировка неотличима от подгонки;
4. посреднические документы не попадают в книги и попадают в журнал; счёт-фактура на
   вознаграждение в журнал не включается;
5. выгрузка книг и журнала получается структурой ФНС, но помечена `СвереноСоСхемой="нет"`:
   XSD-сверки в песочнице нет, и файл не выдаётся за готовый к сдаче.

Запуск: `python3 scripts/check_purchases.py`. Код возврата 1, если есть нарушения.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import closing, invoices, ledger, tax  # noqa: E402

checks = {"done": 0}
violations: list[str] = []


def check(condition: bool, message: str) -> None:
    checks["done"] += 1
    if not condition:
        violations.append(message)


def demo_ledger() -> dict:
    """Журнал с приходами 19.03: три УПД, из них счёт-фактура есть только к двум."""
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    chart, rules = ledger.load_chart(), ledger.load_rules()
    for number, name, net in (("УПД №118", "ООО «БетонТранс»", 1_426_000),
                              ("УПД №119", "ООО «МеталлСнаб»", 1_496_000),
                              ("УПД №120", "ООО «СтройБаза»", 98_000)):
        vat = net * 0.20
        ledger.post_entry(state, event="receipt", date="2026-10-01",
                          doc={"number": number, "date": "2026-10-01", "kind": "upd"},
                          lines=[{"account": "20.01", "side": ledger.DEBIT, "amount": net,
                                  "analytics": {"объект": "ЖК «Северный», корпус 2"}},
                                 {"account": "19.03", "side": ledger.DEBIT, "amount": vat,
                                  "analytics": {"контрагент": name}},
                                 {"account": "60.01", "side": ledger.CREDIT,
                                  "amount": net + vat,
                                  "analytics": {"контрагент": name}}],
                          memo="приход", rule="check_purchases", chart=chart, rules=rules)
    return state


def registry() -> dict:
    return json.loads((ROOT / "data/demo/purchases/incoming_2026_10.json")
                      .read_text(encoding="utf-8"))


def registry_state(ledger_state: dict) -> dict:
    state = invoices.new_state(object_name="ЖК «Северный», корпус 2")
    result = invoices.auto_register_purchases(state, registry(), "2026-10",
                                              ledger_state=ledger_state)
    check(result["posted"] == 2, f"зарегистрировано {result['posted']} счетов-фактур, ожидали 2")
    check(result["rejected"] == 0, f"отклонено {result['rejected']} счетов-фактур")
    return state


# ── 1. книга покупок и остаток 19.03 ───────────────────────────────────────
ledger_state = demo_ledger()
state = registry_state(ledger_state)
book = invoices.purchase_book(state, ledger_state, "2026-10")
check(book["totals"]["invoices"] == 2, f"строк в книге покупок {book['totals']['invoices']}")
check(abs(book["totals"]["vat"] - 304_800) < 0.01,
      f"НДС по книге покупок {book['totals']['vat']}, ожидали 304 800")
check(abs(book["totals"]["journal_vat"] - 604_000) < 0.01,
      f"вычет в журнале {book['totals']['journal_vat']}, ожидали 604 000")
check(abs(book["totals"]["difference"] - 299_200) < 0.01,
      f"непокрытый остаток {book['totals']['difference']}, ожидали 299 200")
check(any(item["code"] == "PURCHASE_BOOK_MISMATCH" for item in book["problems"]),
      "расхождение книги с журналом не названо кодом PURCHASE_BOOK_MISMATCH")
check(any("299 200" in str(gap.get("ask")) for gap in book["gaps"]),
      "в запросе нет суммы, которую надо закрыть счетом-фактурой")

# ── 2. СФ без проведённого основания ───────────────────────────────────────
orphan = invoices.new_state()
invoices.register_purchase(orphan, number="СФ №900", date="2026-10-05",
                           base_number="УПД №999", base_date="2026-10-05",
                           counterparty="ООО «Нет-Прихода»", net=100_000, rate=22)
orphan_book = invoices.purchase_book(orphan, ledger_state, "2026-10")
check(any(item["code"] == "PURCHASE_NO_BASE_DOC" for item in orphan_book["problems"]),
      "счёт-фактура без проводки не дала PURCHASE_NO_BASE_DOC")

# счёт-фактура без номера и без основания не регистрируется — контур не выдумывает
blank = invoices.new_state()
no_number = invoices.register_purchase(blank, number="", date="2026-10-05",
                                       base_number="УПД №118", counterparty="ООО «БетонТранс»",
                                       net=1000, rate=22)
check(no_number["status"] == invoices.STATUS_REJECTED,
      f"счёт-фактура без номера получила статус {no_number['status']}")
check(any(gap["what"].startswith("номер") for gap in no_number["gaps"]),
      "отказ без номера не назвал, чего не хватает")
no_base = invoices.register_purchase(blank, number="СФ №1", date="2026-10-05",
                                     base_number="", counterparty="ООО «БетонТранс»",
                                     net=1000, rate=22)
check(no_base["status"] == invoices.STATUS_REJECTED,
      f"счёт-фактура без основания получила статус {no_base['status']}")

# ── 3. корректировочные счета-фактуры ──────────────────────────────────────
corr_state = registry_state(ledger_state)
down = invoices.register_correction(corr_state, for_kind="received", base_number="СФ №341",
                                    base_date="2026-10-01", number="К/512",
                                    date="2026-10-20", direction="decrease", delta_net=26_000,
                                    reason="сторнирование объёма бетона")
check(down["status"] == invoices.STATUS_OK, f"уменьшение не зарегистрировано: {down['status']}")
check(abs(down["correction"]["vat"] + 5_200) < 0.01,
      f"НДС уменьшения {down['correction']['vat']}, ожидали -5200")
after_down = invoices.purchase_book(corr_state, ledger_state, "2026-10")
check(abs(after_down["totals"]["vat"] - 299_600) < 0.01,
      f"книга после уменьшения {after_down['totals']['vat']}, ожидали 299 600")

up = invoices.register_correction(corr_state, for_kind="received", base_number="СФ №512",
                                  base_date="2026-10-03", number="К/513",
                                  date="2026-10-21", direction="increase", delta_net=50_000,
                                  rate=22,
                                  reason="допоставка керамзита")
check(up["status"] == invoices.STATUS_OK, f"увеличение не зарегистрировано: {up['status']}")
after_up = invoices.purchase_book(corr_state, ledger_state, "2026-10")
check(abs(after_up["totals"]["vat"] - 310_600) < 0.01,
      f"книга после увеличения {after_up['totals']['vat']}, ожидали 310 600")

no_reason = invoices.register_correction(corr_state, for_kind="received", base_number="СФ №341",
                                         base_date="2026-10-01", number="К/514",
                                         date="2026-10-22", direction="decrease",
                                         delta_net=1000, reason="  ")
check(no_reason["status"] == invoices.STATUS_REJECTED,
      "корректировка без причины не отклонена")
check(any("причин" in str(gap.get("why")).lower() for gap in no_reason["gaps"]),
      "отказ без причины не объяснён")

no_original = invoices.register_correction(corr_state, for_kind="received",
                                           base_number="СФ №000", base_date="2026-10-01",
                                           number="К/515", date="2026-10-22",
                                           direction="decrease", delta_net=1000,
                                           reason="проба")
check(no_original["status"] == invoices.STATUS_REJECTED,
      "корректировка без исходного документа не отклонена")

received_without_number = invoices.register_correction(corr_state, for_kind="received",
                                                       base_number="СФ №341",
                                                       base_date="2026-10-01",
                                                       date="2026-10-23", direction="decrease",
                                                       delta_net=1000,
                                                       reason="проба без номера")
check(received_without_number["status"] == invoices.STATUS_REJECTED,
      "полученный корректировочный СФ без номера поставщика зарегистрирован")
check(any("поставщик" in str(gap.get("why")) for gap in received_without_number["gaps"]),
      "номер входящего корректировочного СФ не запрошен у человека")

# ── 4. журнал посредников ──────────────────────────────────────────────────
agency_state = invoices.new_state()
agency = invoices.register_purchase(agency_state, number="СФ №77", date="2026-10-06",
                                    base_number="УПД №130", base_date="2026-10-06",
                                    counterparty="ООО «Заказчик-Инвест»", net=1_000_000,
                                    rate=22, agency=True)
check(agency["status"] == invoices.STATUS_OK, f"посреднический СФ не принят: {agency['status']}")
invoices.register_purchase(agency_state, number="СФ №78", date="2026-10-07",
                           base_number="УПД №131", base_date="2026-10-07",
                           counterparty="ООО «СтройБаза»", net=100_000, rate=22)
agency_book = invoices.purchase_book(agency_state, demo_ledger(), "2026-10")
check(agency_book["totals"]["invoices"] == 1,
      f"посреднический СФ попал в книгу покупок: строк {agency_book['totals']['invoices']}")
journal_report = invoices.intermediary_journal(agency_state, "2026-10")
check(len(journal_report["part2"]) == 1,
      f"в журнале полученных {len(journal_report['part2'])} строк, ожидали 1")
check(abs(journal_report["totals"]["vat2"] - 220_000) < 0.01,
      f"НДС полученных в журнале {journal_report['totals']['vat2']}, ожидали 220 000")
check(journal_report["ready"] is False,
      "журнал без кода вида операции помечен готовым")
check(any("код вида операции" in str(gap.get("what")).lower() for gap in journal_report["gaps"]),
      "отсутствие кода вида операции не названо в запросах")
empty_journal = invoices.intermediary_journal(state, "2026-10")
check(not empty_journal["part1"] and not empty_journal["part2"],
      "журнал заполнился документами, не помеченными как посреднические")

# ── 5. закрытие месяца берёт реестр входящих СФ ────────────────────────────
close_state = demo_ledger()
close_registry = registry_state(close_state)
report = closing.close_month(close_state, "2026-10",
                             invoices=invoices.for_closing(close_registry, "2026-10"),
                             dry_run=True)
vat_step = next((step for step in report["steps"] if "19.03" in str(step.get("title"))), None)
check(vat_step is not None, "в закрытии месяца нет шага зачёта НДС")
check(abs(float(vat_step.get("amount") or 0) - 304_800) < 0.01,
      f"закрытие зачло {vat_step.get('amount')} НДС, ожидали 304 800 (только по документам)")
check(vat_step.get("status") == "выполнено частично",
      f"частичный зачёт помечен как {vat_step.get('status')}")

# ── 6. выгрузка в структуре формата ФНС ────────────────────────────────────
sales = invoices.new_state()
invoices.register(sales, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                  base_number="КС-2 №38", counterparty="ООО «Заказчик-Инвест»",
                  contract="Д-142/2026", net=34_106_600, rate=22, rules=invoices.load_rules())
book_sales = invoices.sales_book(sales, {"entries": []}, "2026-10")
xml_sales = invoices.to_fns_xml(book_sales)
check('КНД="1115103"' in xml_sales, "в выгрузке книги продаж нет КНД 1115103")
check('СвереноСоСхемой="нет"' in xml_sales, "выгрузка не помечена как несверенная со схемой")
xml_purchases = invoices.to_fns_xml(book, rules=invoices.load_rules())
check('КНД="1115102"' in xml_purchases, "в выгрузке книги покупок нет КНД 1115102")
check("285200.00" in xml_purchases and "19600.00" in xml_purchases,
      "в выгрузке книги покупок нет сумм НДС по строкам")
check("ПроверкаФайла" in xml_purchases, "в выгрузке нет перечня замечаний к файлу")
xml_journal = invoices.to_fns_xml(journal_report)
check('КНД="1115104"' in xml_journal, "в выгрузке журнала нет КНД 1115104")
check("7810447576" in xml_journal, "в выгрузке нет ИНН организации из справочника")
check(not tax.load_rates().get("organisation", {}).get("inn") == "",
      "реквизиты организации не заведены — выгрузка подписана пустым ИНН?")
for payload, name in ((xml_sales, "книга продаж"), (xml_purchases, "книга покупок"),
                      (xml_journal, "журнал")):
    from xml.etree import ElementTree as ET
    try:
        ET.fromstring(payload.split("\n", 1)[1])
    except ET.ParseError as error:  # pragma: no cover - проверка самого скрипта
        check(False, f"{name}: XML не разбирается ({error})")
    else:
        check(True, name)

print(f"книга покупок 2026-10: строк {book['totals']['invoices']} · НДС "
      f"{book['totals']['vat']:,.2f} ₽ · вычет в журнале {book['totals']['journal_vat']:,.2f} ₽ "
      f"· не закрыто документами {book['totals']['difference']:,.2f} ₽")
print(f"корректировки: уменьшение уменьшает книгу до "
      f"{after_down['totals']['vat']:,.2f} ₽, увеличение возвращает до "
      f"{after_up['totals']['vat']:,.2f} ₽")
print(f"журнал посредников: выставлено {len(journal_report['part1'])}, получено "
      f"{len(journal_report['part2'])}; код вида операции не заведён — запрос человеку")
print(f"выгрузка ФНС: КНД 1115103 / 1115102 / 1115104, все помечены «СвереноСоСхемой=нет»")
print(f"проверок: {checks['done']}")
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
