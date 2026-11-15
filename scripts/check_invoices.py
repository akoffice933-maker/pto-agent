# -*- coding: utf-8 -*-
"""Проверки счетов-фактур, книги продаж и вычета НДС с выданных авансов.

Скрипт отвечает на вопросы, из-за которых декларация и книга расходятся:

1. нумерация сквозная с 1 января и номер не переиспользуется;
2. счёт-фактура не может быть датирован раньше основания, а срок 5 дней проверяется числом;
3. НДС в счёте-фактуре считается из базы и ставки, а не «по памяти»;
4. книга продаж строится из документов и сходится с начислением в журнале;
5. отсутствие счёта-фактуры — это `SALES_BOOK_MISMATCH` с суммой, а не тишина;
6. вычет НДС с аванса выданного попадает в декларацию (`vat_return`), а не живёт только
   в журнале.

Запуск: `python3 scripts/check_invoices.py`. Код возврата 1, если есть нарушения.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import invoices, ledger, tax, treasury  # noqa: E402

checks = {"done": 0}
violations: list[str] = []


def check(condition: bool, message: str) -> None:
    checks["done"] += 1
    if not condition:
        violations.append(message)


def journal_from_acts(rate: float = 22.0) -> dict:
    """Журнал из сценария подписанных актов: та же первичка, что в check_tax."""
    acts = json.loads((ROOT / "data/demo/accounting/acts_signed_scenario.json")
                      .read_text(encoding="utf-8"))
    payload = json.loads(json.dumps(acts))
    payload["acts"][0]["vat"] = {"rate": rate, "included": False}
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    ledger.post_acts(state, payload, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    return state


# ── 1. нумерация ───────────────────────────────────────────────────────────
rules = invoices.load_rules()
state = invoices.new_state(object_name="ЖК «Северный», корпус 2")
check(invoices.next_number(state, "2026-10-29", rules=rules) == "1",
      "первый номер в году не 1")
check(invoices.next_number(state, "2026-10-29", kind="correction", rules=rules) == "К1",
      "корректировочный счёт-фактура не получил префикс «К»")

first = invoices.register(state, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                          base_number="КС-2 №38", counterparty="ООО «Заказчик-Инвест»",
                          contract="Д-142/2026", net=34_106_600, rate=22, rules=rules)
check(first["status"] == invoices.STATUS_OK, f"первый СФ не выставлен: {first['status']}")
check(first["invoice"]["number"] == "1", f"номер первого СФ {first['invoice']['number']}")
check(first["invoice"]["vat"] == 7_503_452.0,
      f"НДС по 22 % = {first['invoice']['vat']}, ожидали 7 503 452,00")
check(invoices.next_number(state, "2026-12-31", rules=rules) == "2",
      "второй номер не 2: нумерация не сквозная")
check(invoices.next_number(state, "2027-01-05", rules=rules) == "1",
      "с 1 января нумерация не начинается заново")

# номер не переиспользуется даже после «аннулирования»
dupe = invoices.register(state, kind="shipped", date="2026-10-29", base_date="2026-10-29",
                         base_number="КС-2 №38", counterparty="ООО «Заказчик-Инвест»",
                         net=34_106_600, rate=22, rules=rules)
check(dupe["status"] == invoices.STATUS_DUPLICATE,
      "повторная регистрация того же основания не распознана как дубль")
check(len(state["issued"]) == 1, "дубль попал в реестр")

taken = invoices.register(state, kind="shipped", date="2026-10-30", base_date="2026-10-30",
                          base_number="КС-2 №39", counterparty="ООО «Заказчик-Инвест»",
                          net=1000, rate=22, number="1", rules=rules)
check(taken["status"] == invoices.STATUS_REJECTED and
      any(item["code"] == "INVOICE_NUMBER_TAKEN" for item in taken["errors"]),
      "занятый номер принят: нумерация переиспользована")

# ── 2. даты и срок ─────────────────────────────────────────────────────────
before = invoices.register(state, kind="shipped", date="2026-10-20", base_date="2026-10-29",
                           base_number="КС-2 №40", counterparty="ООО «Заказчик-Инвест»",
                           net=1000, rate=22, rules=rules)
check(before["status"] == invoices.STATUS_REJECTED and
      any(item["code"] == "INVOICE_DATE_BEFORE_BASE" for item in before["errors"]),
      "счёт-фактура датирован раньше основания и это не отказ")

late = invoices.register(state, kind="shipped", date="2026-11-10", base_date="2026-11-01",
                         base_number="КС-2 №41", counterparty="ООО «Заказчик-Инвест»",
                         net=1000, rate=22, rules=rules)
check(late["status"] == invoices.STATUS_OK, f"СФ через 9 дней не выставлен: {late['status']}")
check(late["invoice"]["late"] is True and late["invoice"]["late_days"] == 9,
      f"просрочка не посчитана: {late['invoice'].get('late_days')}")
check(any("168" in text for text in late["warnings"]),
      "предупреждение о сроке не ссылается на п. 3 ст. 168 НК РФ")

no_rate = invoices.register(state, kind="shipped", date="2026-11-11", base_date="2026-11-11",
                            base_number="КС-2 №42", counterparty="ООО «Заказчик-Инвест»",
                            net=1000, rules=rules)
check(no_rate["status"] == invoices.STATUS_REJECTED and
      any("ставка" in gap["what"] for gap in no_rate["gaps"]),
      "счёт-фактура без ставки зарегистрирован: НДС был бы выдуман")

# ── 3. книга продаж и сверка с журналом ────────────────────────────────────
journal = journal_from_acts(22.0)
registry = invoices.new_state(object_name="ЖК «Северный», корпус 2")
issued = invoices.auto_issue(registry, journal, "2026-10", rules=rules)
check(issued["posted"] == 1, f"по акту не выставлен СФ: {issued['posted']}")
check(issued["duplicates"] == 0 and issued["rejected"] == 0,
      f"auto_issue: дубли {issued['duplicates']}, отказы {issued['rejected']}")

book = invoices.sales_book(registry, journal, "2026-10", rules=rules)
check(book["totals"]["invoices"] == 1, f"в книге {book['totals']['invoices']} счетов-фактур")
check(book["totals"]["difference"] == 0,
      f"книга не сошлась с журналом: разница {book['totals']['difference']}")
check(book["totals"]["vat"] == book["totals"]["journal_vat"],
      "НДС по книге ≠ начислено в журнале")
check(not any(item["code"] == "SALES_BOOK_MISMATCH" for item in book["problems"]),
      "ложное расхождение книги с журналом на согласованном сценарии")

# без счетов-фактур книга меньше начисленного — это должно быть числом
empty_book = invoices.sales_book(invoices.new_state(), journal, "2026-10", rules=rules)
mismatch = next((item for item in empty_book["problems"]
                 if item["code"] == "SALES_BOOK_MISMATCH"), None)
check(mismatch is not None, "пустая книга не дала SALES_BOOK_MISMATCH")
if mismatch:
    check("0,00 ₽" in mismatch["why"],
          f"в расхождении не названы суммы: {mismatch['why'][:80]}")
check(not empty_book["ready"], "пустая книга признана готовой к сдаче")

# предпросмотр не занимает номера
preview_registry = invoices.new_state()
preview = invoices.auto_issue(preview_registry, journal, "2026-10", rules=rules,
                              mode="propose")
check(preview["posted"] == 0 and preview["planned"] == 1,
      f"предпросмотр: posted {preview['posted']}, planned {preview['planned']}")
check(not preview_registry["issued"], "предпросмотр записал счёт-фактуру в реестр")

# ── 4. вычет НДС с выданных авансов в декларации ───────────────────────────
def issued_advance_journal() -> dict:
    """Аванс, выданный поставщику: 60.02 + ставка и договор в документе."""
    state = ledger.new_state()
    cash = treasury.new_state(opening=50_000_000)
    treasury.add_request(cash, {"number": "З-501", "date": "2026-10-01", "article": "материалы",
                                "amount": 12_200_000, "counterparty": "ООО «Бетон-Сервис»",
                                "requested_by": "snab", "vat": {"rate": 22},
                                "basis": {"number": "СЧ-908", "date": "2026-09-30",
                                          "kind": "счёт", "contract": "Д-11/2025"}},
                          rules=treasury.load_rules())
    for role, person in (("снабжение", "Прохоров"), ("ПТО", "Кузнецов"),
                         ("финдиректор", "Смирнов")):
        treasury.approve(cash, "З-501", role=role, person=person, date="2026-10-02")
    treasury.pay(cash, "З-501", date="2026-10-05", advance=True)
    treasury.post_to_ledger(cash, state)
    return state


with_advance = issued_advance_journal()
posted = tax.post_advance_vat(with_advance, "2026-10")
check(posted["issued_deducted"] == 2_200_000,
      f"вычет с выданного аванса {posted['issued_deducted']}, ожидали 2 200 000")

advances = tax.advances_vat(with_advance, "2026-10")
check(advances["issued"]["in_ledger"] == 2_200_000,
      f"блок issued показывает вычет {advances['issued']['in_ledger']} вместо 2 200 000 "
      f"(76.ВА: вычет — кредит счёта)")
check(advances["issued"]["restored"] == 0,
      f"блок issued показывает восстановление {advances['issued']['restored']} вместо 0")

declaration = tax.vat_return(with_advance, "2026-10")
check(declaration["totals"]["vat_on_issued_advances"] == 2_200_000,
      "вычет по выданным авансам не попал в строку декларации")
check(declaration["totals"]["payable"] == -2_200_000,
      f"к уплате {declaration['totals']['payable']}: вычет не уменьшил налог")
check(any("счёт-фактура" in text.lower() for text in declaration["warnings"]),
      "вычет заявлен без счёта-фактуры и об этом не сказано")

lines = {row["what"]: row for row in advances["lines"]}
check(any("выданных" in name for name in lines),
      "в строках НДС с авансов нет строки по выданным авансам")
issued_line = next((row for name, row in lines.items() if "выданных" in name
                    and "восстанов" not in name), None)
check(issued_line is not None and issued_line["amount"] == 2_200_000,
      "строка вычета по выданным авансам пустая или с чужой суммой")
check(issued_line is not None and issued_line.get("verify") is True,
      "номер строки вычета по выданным авансам не помечен «на проверку»")

# зачёт аванса: восстановление возвращает налог
ledger.post_entry(with_advance, event="advance_offset", date="2026-11-20",
                  doc={"number": "зачёт аванса Д-11/2025", "date": "2026-11-20"},
                  lines=[{"account": "60.01", "side": ledger.DEBIT, "amount": 6_100_000,
                          "analytics": {"контрагент": "ООО «Бетон-Сервис»",
                                        "договор": "Д-11/2025"}},
                         {"account": "60.02", "side": ledger.CREDIT, "amount": 6_100_000,
                          "analytics": {"контрагент": "ООО «Бетон-Сервис»",
                                        "договор": "Д-11/2025"}}],
                  memo="зачёт половины аванса", rule="treasury.advance_offset",
                  chart=ledger.load_chart(), rules=ledger.load_rules())
restored = tax.post_advance_vat(with_advance, "2026-11")
check(restored["issued_restored"] == 1_100_000,
      f"восстановление при зачёте {restored['issued_restored']}, ожидали 1 100 000")
november = tax.vat_return(with_advance, "2026-11")
check(november["totals"]["vat_on_issued_restored"] == 1_100_000,
      "восстановление не попало в декларацию")
check(ledger.verify(with_advance)["ok"], "журнал не сходится после проводок 76.ВА")

print(f"счета-фактуры: нумерация с 1 января, префикс «К», срок "
      f"{rules['issue']['deadline_days']} дн.; занятый номер — отказ, просрочка — числом")
print(f"книга продаж 2026-10: СФ {book['totals']['invoices']} · НДС "
      f"{book['totals']['vat']:,.2f} ₽ = начислено в журнале · разница "
      f"{book['totals']['difference']:,.2f} ₽")
print(f"вычет с выданного аванса в декларации: {declaration['totals']['vat_on_issued_advances']:,.2f} ₽ "
      f"(к уплате {declaration['totals']['payable']:,.2f} ₽); восстановлено при зачёте "
      f"{november['totals']['vat_on_issued_restored']:,.2f} ₽")
print(f"проверок: {checks['done']}")
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
