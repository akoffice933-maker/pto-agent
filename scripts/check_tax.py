# -*- coding: utf-8 -*-
"""Сквозная проверка налогов, подписи и ЭДО — шаг CI.

Проверяется то, из-за чего декларации и отправки ломаются молча:

1. ставка НДС берётся **по дате**: 20 % до 31.12.2025, 22 % с 01.01.2026;
2. НДС начисленный = оборот по 68.02 = дебет 90.03, база = 90.01 − 90.03 (90.01 идёт с НДС);
3. акт 2026 года с НДС 20 % даёт расхождение 682 132 ₽ — и оно видно, а не «норма»;
4. ИНН/КПП проверяются по контрольной сумме и структуре, а не по длине;
5. 6-НДФЛ и РСВ сверяются с журналом; не проведённый месяц — это расхождение;
6. травматизм не растворяется в едином тарифе;
7. XML — наше пространство имён и явная пометка `НеФорматФНС="1"`;
8. подпись без провайдера — отказ с кодом, файл `.sig` не появляется;
9. ЭДО: тренировка не звонит в сеть, дубль комплекта не создаёт второго сообщения,
   5xx повторяется ограниченно, 401 — нет.
"""
from __future__ import annotations

import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx                                                                   # noqa: E402

from pto_agent import closing, edo, ledger, payroll, sign, stock, tax                          # noqa: E402

violations: list[str] = []
checks = {"done": 0}


def check(condition: bool, message: str) -> None:
    checks["done"] += 1
    if not condition:
        violations.append(message)


rates = tax.load_rates()
staff = json.loads((ROOT / "data/demo/payroll/staff.json").read_text(encoding="utf-8"))

# ── 1. ставки по дате ───────────────────────────────────────────────────────
check(tax.vat_rate_on("2025-12-31", rates)["rate"] == 20.0, "31.12.2025 ставка не 20 %")
check(tax.vat_rate_on("2026-01-01", rates)["rate"] == 22.0, "01.01.2026 ставка не 22 %")
check(tax.vat_rate_on("2026-10-29", rates)["rate"] == 22.0, "ставка на дату акта не 22 %")
check(tax.vat_rate_on("", rates)["rate"] is None, "без даты подставлена ставка — так нельзя")

# ── 4. реквизиты ────────────────────────────────────────────────────────────
organisation = rates["organisation"]
check(tax.inn_ok(organisation["inn"]), f"ИНН {organisation['inn']} не проходит контрольную сумму")
check(tax.kpp_ok(organisation["kpp"]), f"КПП {organisation['kpp']} не по структуре")
check(not tax.inn_ok("7810447570"), "ИНН с неверной контрольной суммой принят")

# ── 2–3. НДС из журнала ─────────────────────────────────────────────────────
acts = json.loads((ROOT / "data/demo/accounting/acts_signed_scenario.json").read_text(
    encoding="utf-8"))


def journal_with(rate: float) -> dict:
    state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    payload = json.loads(json.dumps(acts))
    payload["acts"][0]["vat"] = {"rate": rate, "included": False}
    ledger.post_acts(state, payload, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    return state


lawful = tax.vat_return(journal_with(22.0), "2026-10", rates=rates)
legal = next((period["rate"] for period in rates["vat"]["periods"]
              if period["from"] <= "2026-10-29" and (not period["to"]
                                                     or period["to"] >= "2026-10-29")), None)
expected = round(34_106_600 * (1 + legal / 100) - 34_106_600, 2)
check(abs(lawful["totals"]["vat_charged"] - expected) < 0.05,
      f"НДС по 22 % = {lawful['totals']['vat_charged']}, ожидали {expected}")
check(not lawful["problems"], f"законный акт дал расхождения: {lawful['problems'][:1]}")
check(lawful["totals"]["sales"] == round(40_927_920 - 6_821_320, 2),
      "база НДС ≠ 90.01 − 90.03")

stale = tax.vat_return(journal_with(20.0), "2026-10", rates=rates)
gap = next((item for item in stale["problems"] if item["code"] == "VAT_RATE_STALE"), None)
check(gap is not None, "акт 2026 года с НДС 20 % не пойман как устаревшая ставка")
if gap:
    check("682 132" in gap["why"], f"сумма недоначисления не названа: {gap['why'][:80]}")
check(stale["rate_audit"][0]["rate_implied"] == 20.0, "подразумеваемая ставка не 20 %")
check(stale["rate_audit"][0]["rate_lawful"] == 22.0, "законная ставка не 22 %")

empty = tax.vat_return(ledger.new_state(), "2026-10", rates=rates)
check(empty["gaps"], "пустой период не отмечен как «нет данных», а показан нулём")

# ── 5–6. 6-НДФЛ и РСВ ──────────────────────────────────────────────────────
rules = payroll.load_rules()
wage = payroll.new_state(object_name=staff["object"])
for period in ("2026-09", "2026-10"):
    sheet = json.loads((ROOT / "data/demo/payroll"
                        / f"timesheet_{period.replace('-', '_')}.json").read_text(encoding="utf-8"))
    report = payroll.accrue(wage, sheet, staff, rules)
    payroll.remember(wage, report, rules=rules)
month = next(row for row in wage["periods"] if row["period"] == "2026-10")
books = ledger.new_state(object_name=staff["object"])
payroll.post_ledger(books, month)

ndfl = tax.ndfl_return(wage, "2026-10", ledger_state=books, rates=rates)
check(not ndfl["problems"], f"6-НДФЛ расходится с журналом: {ndfl['problems'][:1]}")
check(abs(ndfl["totals"]["tax_withheld"] - ndfl["totals"]["tax_in_ledger"]) < 0.02,
      "удержанный НДФЛ ≠ НДФЛ в журнале")
check(abs(ndfl["totals"]["tax_withheld"] - month["totals"]["ndfl"]) < 0.02,
      "6-НДФЛ ≠ ведомость")

unposted = tax.ndfl_return(wage, "2026-10", ledger_state=ledger.new_state(), rates=rates)
check(any(item["code"] == "NDFL_LEDGER_MISMATCH" for item in unposted["problems"]),
      "непроведённый месяц не отмечен как расхождение")

rsv = tax.rsv(wage, "2026-10", ledger_state=books, rates=rates)
check(not rsv["problems"], f"РСВ расходится с журналом: {rsv['problems'][:1]}")
check(abs(rsv["totals"]["contrib"] - month["totals"]["contrib"]) < 0.02, "РСВ: взносы ≠ ведомость")
check(abs(rsv["totals"]["injury"] - month["totals"]["injury"]) < 0.02,
      "РСВ: травматизм ≠ ведомость")
check(rsv["tariffs"]["injury"] == 2.1, "тариф травматизма не из справочника")
check(rsv["totals"]["base"] == month["totals"]["accrued"], "база взносов ≠ начислено")

# ── 7. XML ──────────────────────────────────────────────────────────────────
for name, payload in (("НДС", lawful), ("6-НДФЛ", ndfl), ("РСВ", rsv)):
    xml = tax.to_xml(payload)
    root = ET.fromstring(xml.encode("utf-8"))
    check(root.attrib.get("НеФорматФНС") == "1", f"{name}: XML не помечен как не-формат ФНС")
    check(tax.NS in xml, f"{name}: нет пространства имён {tax.NS}")
check([item["code"] for item in tax.validate(lawful, rates=rates)] == [],
      "проверка реквизитов отвергла верную декларацию")

# ── 8. подпись ──────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as folder:
    document = Path(folder) / "КС-2 №38.pdf"
    document.write_bytes(b"%PDF-1.4 demo")
    result = sign.sign_file(document, provider=sign.StubProvider())
    check(result["status"] == sign.STATUS_UNAVAILABLE,
          f"заглушка подписи вернула «{result['status']}»")
    check(not (Path(folder) / "КС-2 №38.pdf.sig").exists(),
          "создан файл подписи, которой нет")
    real = sign.sign_file(document)
    check(real["status"] != sign.STATUS_SIGNED or sign.toolchain()["available"],
          "подпись выдана без провайдера и сертификата")
    if not sign.toolchain()["available"]:
        check(real["why"], "нет подписи и нет причины")

    # ── 9. ЭДО ──────────────────────────────────────────────────────────────
    state = edo.new_state(box=organisation["inn"] + "@diadoc")
    first = edo.send(state, box=state["box"], to_box="7700000000@diadoc",
                     number="КС-2 №38", documents=[document])
    again = edo.send(state, box=state["box"], to_box="7700000000@diadoc",
                     number="КС-2 №38", documents=[document])
    check(first["status"] == edo.STATUS_QUEUED, "тренировка не дала статус «не отправлено»")
    check(again["duplicate"] is True and len(state["sends"]) == 1,
          "повторная отправка создала второе сообщение")
    check("тренировка" in edo.outbox_markdown(state), "в журнале нет причины «не отправлено»")

    calls = {"count": 0}

    def server_error(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, text="временно недоступно")

    client = edo.DiadocClient(login=state["box"], token="t", retries=1, sleep=0,
                              transport=httpx.MockTransport(server_error))
    failed = edo.send(edo.new_state(box=state["box"]), box=state["box"],
                      to_box="7700000000@diadoc", number="КС-2 №39",
                      documents=[document], client=client, dry_run=False)
    check(failed["status"] == edo.STATUS_FAILED, "ошибка 503 не привела к отказу")
    check(calls["count"] == 2, f"503 повторён {calls['count']} раз вместо 2")

    calls["count"] = 0

    def unauthorized(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401, text="токен истёк")

    client = edo.DiadocClient(login=state["box"], token="bad", retries=3, sleep=0,
                              transport=httpx.MockTransport(unauthorized))
    denied = edo.send(edo.new_state(box=state["box"]), box=state["box"],
                      to_box="7700000000@diadoc", number="КС-2 №40",
                      documents=[document], client=client, dry_run=False)
    check(calls["count"] == 1, f"401 повторён {calls['count']} раз — это бессмысленно")
    check(denied["status"] == edo.STATUS_FAILED, "401 не дал отказа")

    sent_ok = edo.send(edo.new_state(box=state["box"]), box=state["box"],
                       to_box="7700000000@diadoc", number="КС-2 №41", documents=[document],
                       client=edo.DiadocClient(
                           login=state["box"], token="t",
                           transport=httpx.MockTransport(
                               lambda request: httpx.Response(200, json={"MessageId": "m-1"}))),
                       dry_run=False)
    check(sent_ok["status"] == edo.STATUS_SENT and sent_ok["message_id"] == "m-1",
          "успешная отправка не сохранила идентификатор сообщения")


# ── 10. налог на прибыль ────────────────────────────────────────────────────
closed = ledger.new_state(object_name=staff["object"])
payload = json.loads(json.dumps(acts))
payload["acts"][0]["vat"] = {"rate": 22.0, "included": False}
ledger.post_acts(closed, payload, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
stock_state = stock.demo_state()
ledger.post_stock(closed, stock_state,
                  documents=json.loads((ROOT / "data/demo/stock/documents_2026_10.json")
                                       .read_text(encoding="utf-8")))
for account, amount, analytics in (
        ("25", 150_000, {"объект": staff["object"], "статья затрат": "механизмы"}),
        ("26", 50_000, {"статья затрат": "прочие"})):
    ledger.post_entry(closed, event="overhead", date="2026-10-15",
                      doc={"number": f"счёт {account}"},
                      lines=[{"account": account, "side": ledger.DEBIT, "amount": amount,
                              "analytics": analytics},
                             {"account": "60.01", "side": ledger.CREDIT, "amount": amount,
                              "analytics": {"контрагент": "ООО «Механизация»"}}])
closing.close_month(closed, "2026-10", stock_state=stock_state)

open_month = ledger.new_state(object_name=staff["object"])
ledger.post_acts(open_month, payload, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
ledger.post_stock(open_month, stock_state,
                  documents=json.loads((ROOT / "data/demo/stock/documents_2026_10.json")
                                       .read_text(encoding="utf-8")))
early = tax.profit_return(open_month, "2026-10", rates=rates)
check(any(item["code"] == "PROFIT_NOT_CLOSED" for item in early["problems"]),
      "незакрытый месяц не пойманы как PROFIT_NOT_CLOSED: прибыль посчитана до закрытия")

profit = tax.profit_return(closed, "2026-10", rates=rates)
check(not profit["problems"], f"закрытый месяц дал расхождения по прибыли: {profit['problems'][:1]}")
check(abs(profit["totals"]["revenue"] - lawful["totals"]["sales"]) < 0.05,
      "доходы по прибыли ≠ база НДС (90.01 − 90.03)")
check(abs(profit["totals"]["profit"]
          - (profit["totals"]["revenue"] - profit["totals"]["cost"])) < 0.05,
      "прибыль ≠ доходы − расходы")
check(abs(profit["totals"]["tax"]
          - (profit["totals"]["federal_tax"] + profit["totals"]["regional_tax"])) < 0.05,
      "налог ≠ федеральная + региональная части")
check(profit["totals"]["profit"] == 31_280_100.00,
      f"прибыль {profit['totals']['profit']} ≠ задокументированной 31 280 100 (docs/47): "
      f"сценарий разошёлся с демо")
check(abs(profit["totals"]["federal_tax"] - round(profit["totals"]["profit"] * 0.08, 2)) < 0.02,
      "федеральная часть не 8 %")
check(abs(profit["totals"]["regional_tax"] - round(profit["totals"]["profit"] * 0.17, 2)) < 0.02,
      "региональная часть не 17 %")
check(profit["not_included"], "прибыль заявлена полной без списка того, что не считается")
check([item["code"] for item in tax.validate(profit, rates=rates)] == [],
      "проверка реквизитов отвергла верную декларацию по прибыли")
check("<НеСчитается>" in tax.to_xml(profit),
      "XML прибыли не содержит раздела «не считается»")
check("Налог на прибыль" in tax.to_markdown(profit), "markdown прибыли без заголовка")

loss = ledger.new_state(object_name=staff["object"])
ledger.post_entry(loss, event="overhead", date="2026-10-15", doc={"number": "счёт №77"},
                  lines=[{"account": "90.02", "side": ledger.DEBIT, "amount": 100000,
                          "analytics": {"объект": staff["object"]}},
                         {"account": "60.01", "side": ledger.CREDIT, "amount": 100000,
                          "analytics": {"контрагент": "ООО «Механизация»"}}])
minus = tax.profit_return(loss, "2026-10", rates=rates)
check(any(item["code"] == "PROFIT_LOSS" for item in minus["problems"]),
      "убыток по реализации не назван убытком")
check(minus["totals"]["tax"] == 0, "с убытка начислен налог")

# ── 11. НДС с авансов ──────────────────────────────────────────────────────
adv = ledger.new_state(object_name=staff["object"])
ledger.post_acts(adv, payload, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
analytics = {"контрагент": "ООО «Заказчик-Инвест»", "договор": "Д-142/2026"}
ledger.post_entry(adv, event="receipt_payment", date="2026-10-05",
                  doc={"number": "п/п аванс 55"},
                  lines=[{"account": "51", "side": ledger.DEBIT, "amount": 12_200_000,
                          "analytics": {}},
                         {"account": "62.02", "side": ledger.CREDIT, "amount": 12_200_000,
                          "analytics": analytics}])
ledger.post_entry(adv, event="advance_offset", date="2026-10-29",
                  doc={"number": "зачёт аванса"},
                  lines=[{"account": "62.02", "side": ledger.DEBIT, "amount": 6_100_000,
                          "analytics": analytics},
                         {"account": "62.01", "side": ledger.CREDIT, "amount": 6_100_000,
                          "analytics": analytics}])
with_adv = tax.vat_return(adv, "2026-10", rates=rates)
advances = with_adv["advances"]
check(abs(advances["vat_on_received"] - 2_200_000) < 0.05,
      f"НДС с аванса 12,2 млн по 22 % = {advances['vat_on_received']}, ожидали 2 200 000")
check(abs(advances["vat_on_offset"] - 1_100_000) < 0.05,
      f"вычет с зачтённых 6,1 млн = {advances['vat_on_offset']}, ожидали 1 100 000")
check(abs(with_adv["totals"]["payable"]
          - (with_adv["totals"]["vat_charged"] - with_adv["totals"]["vat_deducted"]
             + advances["to_pay"])) < 0.05, "к уплате не учёл НДС с авансов")
check(any(item["code"] == "VAT_ADVANCE_NOT_POSTED" for item in with_adv["problems"]),
      "НДС с авансов попал в декларацию, но расхождение с журналом не названо")
check(len(with_adv["advances"]["lines"]) == 2 and all(row["verify"]
                                                      for row in with_adv["advances"]["lines"]),
      "строки авансового НДС не помечены «на проверку»")

# ── 12. проводки НДС с авансов на 76.АВ ────────────────────────────────────
posted = tax.post_advance_vat(adv, "2026-10", rates=rates)
check(posted["posted"] == 2 and not posted["errors"],
      f"НДС с авансов не проведён: {posted['errors'][:1]}")
check(abs(posted["accrued"] - 2_200_000) < 0.05 and abs(posted["deducted"] - 1_100_000) < 0.05,
      f"проведено {posted['accrued']}/{posted['deducted']}, ожидали 2 200 000/1 100 000")
after_post = tax.vat_return(adv, "2026-10", rates=rates)
check(not any(item["code"] == "VAT_ADVANCE_NOT_POSTED" for item in after_post["problems"]),
      "после проводок расхождение «декларация ≠ журнал» осталось")
check(abs(after_post["totals"]["payable"] - with_adv["totals"]["payable"]) < 0.05,
      "проводки изменили сумму к уплате: они должны только фиксировать её в журнале")
balance_76 = sum(ledger.balance_as_of(adv, "76.АВ", "2026-10-31").values())
check(abs(float(balance_76) - 1_100_000) < 0.05,
      f"сальдо 76.АВ {balance_76}, ожидали 1 100 000 (незакрытый аванс)")
check(ledger.verify(adv)["ok"], "журнал не сходится после проводок НДС с авансов")
again_post = tax.post_advance_vat(adv, "2026-10", rates=rates)
check(again_post["duplicates"] == 2 and again_post["posted"] == 0,
      "повторные проводки НДС с авансов задвоились")

# ── 13. счёт-фактура на аванс (черновик) ───────────────────────────────────
from pto_agent import forms                                                      # noqa: E402

invoice = forms.build_advance_invoice("2026-10", ledger_state=adv, rates=rates)
check(invoice["code"] == "СФ-аванс", f"бланк {invoice['code']}")
check(len(invoice["rows"]) == 1, f"строк в счёте-фактуре {len(invoice['rows'])}, ожидали 1")
check(invoice["totals"]["НДС"] == "2 200 000,00",
      f"НДС в бланке {invoice['totals']['НДС']}, ожидали 2 200 000,00")
asked = " ".join(gap["what"] for gap in invoice["gaps"])
check("номер и дата счёта-фактуры" in asked,
      "бланк не просит номер счёта-фактуры — значит делает вид, что документ есть")
check("книга продаж" in asked, "бланк не сказал, что книги продаж нет")
check("ЧЕРНОВИК" in invoice["draft_notice"], "бланк без пометки «черновик»")

print(f"прибыль 2026-10: доходы {profit['totals']['revenue']:,.2f} ₽ · расходы "
      f"{profit['totals']['cost']:,.2f} ₽ · налог {profit['totals']['tax']:,.2f} ₽ "
      f"(8 % + 17 %); НДС с авансов: начислено "
      f"{advances['vat_on_received']:,.2f} ₽, вычет {advances['vat_on_offset']:,.2f} ₽")
print(f"налоги: НДС {lawful['totals']['vat_charged']:,.2f} ₽ · 6-НДФЛ "
      f"{ndfl['totals']['tax_withheld']:,.2f} ₽ · РСВ {rsv['totals']['contrib']:,.2f} ₽ · "
      f"травматизм {rsv['totals']['injury']:,.2f} ₽")
print(f"устаревшая ставка: недоначислено {stale['rate_audit'][0]['gap']:,.2f} ₽ "
      f"(20 % вместо 22 %)")
print(f"подпись: {sign.toolchain()['why'][:70]}")
print(f"ЭДО: тренировка без сети, дубль не отправляется, 503×2, 401×1")
print(f"проверок: {checks['done']}")
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
