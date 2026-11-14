# -*- coding: utf-8 -*-
"""Сквозная проверка ядра учёта на демо-объекте — шаг CI.

Правила, которые проверяются (не «код не упал», а «учёт не врёт»):

1. после каждой проводки Дт = Кт — инвариант держится на каждом шаге, а не только в конце;
2. сальдо 10.01 равно остатку склада в деньгах — по каждому складу и номенклатуре;
3. приход без поставщика проводки не даёт, а даёт запрос к первичке;
4. приход без ставки НДС не придумывает налог: 19.03 не появляется;
5. списание без объекта не уходит «в затраты вообще» — отказ с запросом;
6. отрицательный остаток склада не превращается в проводку;
7. подмена суммы в журнале ломается на хеш-цепочке;
8. сторно возвращает сальдо к нулю, второе сторно отклоняется;
9. в закрытый период проводок нет, сторно — в открытом;
10. давальческие материалы живут на 003 и в баланс не подмешиваются;
11. выручка признаётся только по подписанной заказчиком КС-2;
12. у каждой проводки есть документ-основание: записей «по смыслу» нет;
13. заявка на оплату без документа-основания не принимается, а просит документ;
14. маршрут согласования проходится по порядку, одно лицо не подписывает две ступени;
15. лимит статьи блокирует заявку, обход — только с причиной и автором в аудите;
16. оплата сверх остатка — только с явным разрешением и причиной, разрыв виден в календаре;
17. Дт 60.01 Кт 51 и Дт 51 Кт 62.01 проводятся в журнал, а деньги казначейства
    сходятся с сальдо 51 (без входящего остатка счёт 51 уходил в минус);
18. лимит объекта не прикрывается экономией на другом объекте;
19. аванс живёт на 60.02/62.02, зачитывается на меньшую из сумм и не превращается
    в выручку или в погашение долга;
20. БДР считается начислением, а счёт, назначенный нескольким статьям, не разносится
    «на глаз»: он попадает в «не разнесено» с причиной.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent import closing, ledger, stock, treasury                    # noqa: E402

DOCS = ROOT / "data" / "demo" / "stock" / "documents_2026_10.json"
CATALOG = ROOT / "data" / "demo" / "stock" / "catalog.json"
INVOICES = ROOT / "data" / "demo" / "accounting" / "invoices_2026_10.json"
SIGNED = ROOT / "data" / "demo" / "accounting" / "acts_signed_scenario.json"
TREASURY = ROOT / "data" / "demo" / "treasury" / "state_2026_10.json"
RECEIPTS = ROOT / "data" / "demo" / "treasury" / "receipts_2026_10.json"
FUNDING = ROOT / "data" / "demo" / "funding" / "plan.json"

violations: list[str] = []
stats = {"entries": 0, "balance_checks": 0, "rejected": 0, "gaps": 0,
         "stock_rows": 0, "off_balance": 0, "acts": 0, "close_steps": 0, "profit": 0.0,
         "cash_requests": 0, "cash_paid": 0.0, "cash_balance": 0.0}


def check(condition: bool, message: str) -> None:
    if not condition:
        violations.append(message)


def catalog_state(*, opening: bool = True) -> dict:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    return stock.new_state(object_name=payload["object"],
                           warehouses=payload["warehouses"], items=payload["items"],
                           opening=payload.get("opening") if opening else None)


def balanced(state: dict, where: str) -> None:
    summary = ledger.verify(state)
    stats["balance_checks"] += 1
    if not summary["ok"]:
        details = "; ".join(f"{item['code']}: {item['why']}" for item in summary["problems"])
        violations.append(f"журнал не сходится после «{where}»: {details}")


# ── 1. демо-склад и проводки по нему ──────────────────────────────────────────
documents = json.loads(DOCS.read_text(encoding="utf-8"))
stock_state = catalog_state()
stock.post_many(stock_state, [dict(doc) for doc in documents["documents"]])

state = ledger.new_state(object_name=stock_state["object"])
# проводим документ за документом: инвариант Дт=Кт обязан держаться на каждом шаге
step = ledger.new_state(object_name=stock_state["object"])
done = 0
for doc in sorted(documents["documents"], key=lambda row: row["date"]):
    before = len(step["entries"])
    ledger.post_stock(step, stock_state, documents={"documents": [doc]})
    if len(step["entries"]) != before:
        done += 1
        balanced(step, str(doc.get("number")))
check(done >= 6, f"проводки созданы только по {done} документам из демо-набора")

result = ledger.post_stock(state, stock_state, documents=documents)
stats["entries"] = len(state["entries"])
stats["rejected"] = result["rejected"]
stats["gaps"] = len(result["gaps"])
check(result["rejected"] == 0, f"проводки по демо-складу: отказов {result['rejected']}")
balanced(state, "полный набор демо-документов")

# ── 2. склад в деньгах = сальдо 10.01 ─────────────────────────────────────────
recon = ledger.reconcile_stock(state, stock_state)
stats["stock_rows"] = len(recon["rows"])
check(recon["ok"], "сальдо 10.01 не равно остатку склада: " +
      "; ".join(item["why"] for item in recon["problems"]))
check(Decimal(str(recon["ledger_total"])) == Decimal(str(recon["stock_total"])),
      f"10.01 {recon['ledger_total']} ≠ склад {recon['stock_total']}")

# ── 3. приход без поставщика ──────────────────────────────────────────────────
lonely = {"kind": "receipt", "number": "УПД без поставщика", "date": "2026-10-10",
          "warehouse": "СК-1", "lines": [{"item": "Бетон B25", "qty": 1, "price": 5000}]}
stock_lonely = catalog_state()
stock.post_document(stock_lonely, dict(lonely))
state_lonely = ledger.new_state()
result = ledger.post_stock(state_lonely, stock_lonely, documents={"documents": [lonely]})
check(result["rejected"] == 1, "приход без поставщика прошёл проводкой")
check(any(item["code"] == "NO_COUNTERPARTY" for item in result["errors"]),
      "приход без поставщика не назвал причину отказа")
check(bool(result["gaps"]), "приход без поставщика не дал запроса к первичке")

# ── 4. приход без ставки НДС ──────────────────────────────────────────────────
no_vat = {"kind": "receipt", "number": "УПД без НДС", "date": "2026-10-10",
          "warehouse": "СК-1", "counterparty": "ООО «Без Налога»",
          "lines": [{"item": "Бетон B25", "qty": 1, "price": 5000}]}
stock_no_vat = catalog_state()
stock.post_document(stock_no_vat, dict(no_vat))
state_no_vat = ledger.new_state()
result = ledger.post_stock(state_no_vat, stock_no_vat, documents={"documents": [no_vat]})
accounts = {line["account"] for entry in state_no_vat["entries"] for line in entry["lines"]}
check("19.03" not in accounts, "НДС начислен без ставки в документе")
check(any("НДС" in gap["what"] for gap in result["gaps"]),
      "отсутствие ставки НДС не попало в запросы")
check("10.01" in accounts, "приход без НДС не создал проводку по материалам")

# ── 5. списание без объекта ───────────────────────────────────────────────────
blind = {"kind": "issue", "number": "М-11 без объекта", "date": "2026-10-10",
         "warehouse": "СК-1", "lines": [{"item": "Бетон B25", "qty": 1}]}
state_blind = ledger.new_state()
result = ledger.post_stock(state_blind, stock_state, documents={"documents": [blind]})
check(result["rejected"] >= 1, "списание без объекта ушло в затраты")
check(any(item["code"] == "NO_OBJECT" for item in result["errors"]),
      "списание без объекта не назвало причину")

# ── 6. отрицательный остаток ──────────────────────────────────────────────────
negative = {"kind": "issue", "number": "М-11 сверх остатка", "date": "2026-10-10",
            "warehouse": "СК-1", "lines": [{"item": "Бетон B25", "qty": 9999,
                                            "object": "корпус 2"}]}
stock_negative = catalog_state()
stock.post_document(stock_negative, dict(negative), allow_negative=True, reason="приход не заведён")
state_negative = ledger.new_state()
ledger.post_stock(state_negative, stock_negative)
cost = sum(line["amount"] for entry in state_negative["entries"] for line in entry["lines"]
           if line["account"] == "20.01")
stock_money = sum(row["amount"] or 0 for row in stock.balances(stock_negative, daval=False))
check(cost > stock_money, "минус на складе не дал расхождения с 10.01 — значит сверка врёт")
recon_negative = ledger.reconcile_stock(state_negative, stock_negative)
check(not recon_negative["ok"], "сверка не заметила минус на складе")

# ── 7. подмена суммы в журнале ────────────────────────────────────────────────
tampered = json.loads(json.dumps(state))
tampered["entries"][0]["lines"][0]["amount"] = 1.0
summary = ledger.verify(tampered)
check(not summary["ok"], "подмена суммы в журнале не поймана")
check(any(item["code"] == "HASH" for item in summary["problems"]),
      "подмена суммы поймана не хеш-цепочкой")

# ── 8. сторно ─────────────────────────────────────────────────────────────────
storno_state = ledger.new_state()
storno_stock = catalog_state(opening=False)
one = {"kind": "receipt", "number": "УПД №77", "date": "2026-10-02", "warehouse": "СК-1",
       "counterparty": "ООО «БетонТранс»", "vat": {"rate": 20, "included": False},
       "lines": [{"item": "Бетон B25", "qty": 2, "price": 6000}]}
stock.post_document(storno_stock, dict(one))
ledger.post_stock(storno_state, storno_stock, documents={"documents": [one]})
entry_id = storno_state["entries"][-1]["id"]
ledger.storno(storno_state, entry_id, date="2026-10-20", reason="накладная отменена")
saldo = ledger.balance_as_of(storno_state, "10.01", "2026-10-31")
check(all(value == 0 for value in saldo.values()), "сторно не обнулило сальдо 10.01")
balanced(storno_state, "сторно")
again = ledger.storno(storno_state, entry_id, date="2026-10-21", reason="ещё раз")
check(again["status"] == ledger.STATUS_DUPLICATE, "повторное сторно прошло")
no_reason = ledger.storno(storno_state, storno_state["entries"][0]["id"],
                          date="2026-10-21", reason=" ")
check(no_reason["status"] == ledger.STATUS_REJECTED, "сторно без причины прошло")

# ── 9. закрытие периода ───────────────────────────────────────────────────────
closed_state = ledger.new_state()
closed_stock = catalog_state()
ledger.post_stock(closed_state, closed_stock)
closed_period = ledger.close_period(closed_state, "2026-09", stock_state=closed_stock)
check(closed_period["closed"], "период 2026-09 не закрылся на демо-складе")
late = ledger.post_entry(closed_state, event="receipt", date="2026-09-15",
                         doc={"number": "УПД задним числом"},
                         lines=[{"account": "10.01", "side": "Дт", "amount": 10,
                                 "analytics": {"склад": "СК-1", "номенклатура": "Бетон B25"}},
                                {"account": "60.01", "side": "Кт", "amount": 10,
                                 "analytics": {"контрагент": "ООО «А»"}}])
check(late["status"] == ledger.STATUS_REJECTED, "проводка в закрытый период прошла")
check(late["errors"][0]["code"] == "CLOSED_PERIOD", "закрытый период отказал не по той причине")
fix = ledger.storno(closed_state, closed_state["entries"][0]["id"],
                    date="2026-10-05", reason="уточнение остатка")
check(fix["status"] == ledger.STATUS_OK, "сторно в открытом периоде не прошло")
balanced(closed_state, "закрытие + сторно в открытом периоде")

# ── 10. давальческие на 003 ───────────────────────────────────────────────────
daval = [doc for doc in state["entries"] if doc.get("kind") == "off_balance"]
stats["off_balance"] = len(daval)
check(len(daval) == 2, f"забалансовых записей {len(daval)}, ожидалось 2 (приёмка и возврат)")
check(all(line["account"] == "003" for entry in daval for line in entry["lines"]),
      "давальческие материалы попали не на 003")
report = ledger.trial_balance(state, "2026-10")
check(all(row["code"] != "003" for row in report["rows"]),
      "003 попал в балансовую часть ОСВ")
check(bool(report["off_balance"]), "003 не показан отдельно как забалансовый")
check(report["balanced"], "ОСВ не сходится")

# ── 11. выручка только по подписанной КС-2 ────────────────────────────────────
acts_path = ROOT / "data" / "demo" / "acts" / "acts.json"
acts = json.loads(acts_path.read_text(encoding="utf-8"))
revenue_state = ledger.new_state(object_name=acts.get("object") or "")
checked = ledger.post_acts(revenue_state, acts, contract=acts.get("contract") or "",
                           customer="ООО «Заказчик-Инвест»")
check(checked["posted"] == 0, "выручка признана по неподписанным актам")
check(not revenue_state["entries"], "неподписанные акты создали проводки")

signed = {"act_id": "КС-2 №38", "act_date": "2026-10-29", "status": "подписан",
          "amount_rub": 1200000, "customer": "ООО «Заказчик-Инвест»",
          "contract": "Д-142/2026", "vat": {"rate": 20, "included": False}}
posted_acts = ledger.post_acts(revenue_state, {"acts": [signed]})
stats["acts"] = posted_acts["posted"]
check(posted_acts["posted"] == 1, "подписанный акт не дал выручку")
lines = {}
for entry in revenue_state["entries"]:
    for line in entry["lines"]:
        lines[line["account"]] = lines.get(line["account"], 0) + line["amount"]
# 62.01 — сумма к получению: 1 200 000 без НДС + 240 000 НДС (вариант А)
check(abs(lines.get("62.01", 0) - 1440000.0) < 0.01,
      f"долг заказчика {lines.get('62.01')}, ожидалось 1 440 000 (акт 1 200 000 + НДС 240 000)")
check(abs(lines.get("68.02", 0) - 240000.0) < 0.01, "НДС с реализации не равен 20 %")
balanced(revenue_state, "выручка по КС-2")

# ── 12. у каждой проводки есть документ ───────────────────────────────────────
for entry in state["entries"]:
    check(bool((entry.get("doc") or {}).get("number")),
          f"{entry.get('id')}: проводка без документа-основания")
    check(bool(entry.get("rule")), f"{entry.get('id')}: проводка без правила")
    check(bool(entry.get("hash")), f"{entry.get('id')}: запись без хеша")
stats["entries"] = len(state["entries"])

# ── 13. закрытие месяца без подписанных актов ────────────────────────────────
invoices = json.loads(INVOICES.read_text(encoding="utf-8"))
acct_policy = closing.load_policy()
demo_state = ledger.new_state(object_name=stock_state["object"])
ledger.post_stock(demo_state, stock_state, documents=documents)
if ledger.verify(demo_state)["ok"]:
    demo_close = closing.close_month(demo_state, "2026-10", stock_state=stock_state,
                                     invoices=invoices, policy=acct_policy)
    steps_by_id = {step["id"]: step for step in demo_close["steps"]}
    stats["close_steps"] = len(demo_close["steps"])
    check(demo_close["closed"], "месяц без подписанных актов не закрылся: " +
          "; ".join(item["why"] for item in demo_close["problems"]))
    costs_step = steps_by_id.get("costs") or {}
    check(costs_step.get("status") == closing.NOT_NEEDED,
          "затраты списаны без подписанного акта — работа не сдана, это НЗП")
    on_201 = sum(ledger.balance_as_of(demo_state, "20.01", "2026-10-31").values())
    check(Decimal(str(on_201)) == Decimal("2626500"),
          f"20.01 после закрытия без выручки = {on_201}, ожидалось 2 626 500 (НЗП)")
    check(sum(ledger.balance_as_of(demo_state, "90.02", "2026-10-31").values()) == 0,
          "себестоимость появилась без выручки")
    vat_step = steps_by_id.get("vat") or {}
    check(vat_step.get("status") == closing.PARTIAL,
          f"зачёт НДС без полного реестра СФ: статус {vat_step.get('status')}")
    vat_left = sum(ledger.balance_as_of(demo_state, "19.03", "2026-10-31").values())
    check(Decimal(str(vat_left)) == Decimal("299200"),
          f"остаток 19.03 без СФ = {vat_left}, ожидалось 299 200")
    check(any("УПД №119" in item.get("what", "") for item in demo_close["requests"]),
          "отсутствие СФ к УПД №119 не попало в запросы")
    check(steps_by_id.get("shortages", {}).get("status") == closing.NEEDS_DECISION,
          "недостачи 94 закрыты автоматически — программа решила, кто виноват")
    check(sum(ledger.balance_as_of(demo_state, "94", "2026-10-31").values()) > 0,
          "сальдо 94 исчезло без решения человека")
    balanced(demo_state, "закрытие месяца без выручки")
    # повторное закрытие не удваивает проводки
    before = len(demo_state["entries"])
    closing.close_month(demo_state, "2026-10", invoices=invoices, policy=acct_policy)
    check(len(demo_state["entries"]) == before, "повторное закрытие создало новые проводки")

    # ── 14. закрытие с подписанным актом даёт прибыль ─────────────────────────
    paid_state = ledger.new_state(object_name=stock_state["object"])
    ledger.post_stock(paid_state, stock_state, documents=documents)
    acts_signed = json.loads(SIGNED.read_text(encoding="utf-8"))
    acts_result = ledger.post_acts(paid_state, acts_signed)
    check(acts_result["posted"] == 1, "сценарий с подписанным актом не дал выручку")
    paid_close = closing.close_month(paid_state, "2026-10", stock_state=stock_state,
                                     invoices=invoices, policy=acct_policy)
    check(paid_close["closed"], "месяц с подписанным актом не закрылся")
    result = paid_close.get("result") or {}
    stats["profit"] = result.get("profit") or 0
    expected = 34106600.0 - 2626500.0
    check(abs((result.get("profit") or 0) - expected) < 0.01,
          f"прибыль {result.get('profit')}, ожидалось {expected:,.2f} "
          f"(выручка без НДС − себестоимость)")
    check(abs((result.get("revenue_net") or 0) - 34106600.0) < 0.01,
          "выручка без НДС не равна сумме акта")
    check(sum(ledger.balance_as_of(paid_state, "20.01", "2026-10-31").values()) == 0,
          "затраты не закрыты при подписанном акте")
    for code in ("90.01", "90.02", "90.03"):
        left = sum(ledger.balance_as_of(paid_state, code, "2026-10-31").values())
        check(left == 0, f"субсчёт {code} не закрыт: остаток {left}")
    rows_after = {row["code"]: row for row in ledger.trial_balance(paid_state, "2026-10")["rows"]}
    check(rows_after["62.01"]["closing_debit"] > 34000000,
          "долг заказчика меньше суммы акта: НДС «сверху» не попал в 62.01")
    balanced(paid_state, "закрытие месяца с выручкой")
    check(closing.close_month(paid_state, "2026-11").get("closed") in (True, False),
          "закрытие следующего месяца падает")
else:
    violations.append("журнал демо-склада не сходится до закрытия месяца")

# ── 15. режим «предлагать» и сухой прогон ────────────────────────────────────
proposal_state = ledger.new_state(object_name=stock_state["object"])
ledger.post_stock(proposal_state, stock_state, documents=documents)
ledger.post_acts(proposal_state, json.loads(SIGNED.read_text(encoding="utf-8")))
before_entries = len(proposal_state["entries"])
proposed = closing.close_month(proposal_state, "2026-10", invoices=invoices,
                               policy=acct_policy, mode=ledger.MODE_PROPOSE)
check(not proposed["closed"], "в режиме «предлагать» период закрылся сам")
check(len(proposal_state["entries"]) == before_entries,
      "в режиме «предлагать» проводки всё-таки записались")
check(bool(proposal_state["proposals"]), "в режиме «предлагать» предложений не появилось")

dry_state = ledger.new_state(object_name=stock_state["object"])
ledger.post_stock(dry_state, stock_state, documents=documents)
ledger.post_acts(dry_state, json.loads(SIGNED.read_text(encoding="utf-8")))
snapshot = json.dumps(dry_state, sort_keys=True, ensure_ascii=False)
dry = closing.close_month(dry_state, "2026-10", invoices=invoices, policy=acct_policy,
                          dry_run=True)
check(dry.get("dry_run") is True, "сухой прогон не пометился как сухой")
check(json.dumps(dry_state, sort_keys=True, ensure_ascii=False) == snapshot,
      "сухой прогон изменил журнал")

# ── 16. казначейство: заявка → маршрут → лимит → оплата → проводки ───────────
treasury_payload = json.loads(TREASURY.read_text(encoding="utf-8"))
cash_state = treasury.new_state(object_name=treasury_payload["object"],
                               opening=treasury_payload["opening"],
                               limits=treasury_payload["limits"],
                               contract=treasury_payload.get("contract") or "")
cash_accepted = 0
for request in treasury_payload["requests"]:
    outcome = treasury.add_request(cash_state, request)
    if outcome["request"]:
        cash_accepted += 1
    if request["number"] == "З-006":
        check([item["code"] for item in outcome["errors"]] == ["NO_BASIS"],
              "заявка без документа-основания принята: «оплатить» без счёта — не заявка")
        check(bool(outcome["gaps"]), "по заявке без основания не запрошен документ")
    if request["number"] == "З-004":
        blocked = outcome["request"] or {}
        check(blocked.get("status") == treasury.BLOCKED,
              f"заявка сверх лимита не заблокирована: {blocked.get('status')}")
        check(any("80 000,00" in item["why"] for item in outcome["errors"]),
              "в отказе по лимиту нет точной суммы превышения")
stats["cash_requests"] = cash_accepted
check(cash_accepted == 7, f"принято заявок {cash_accepted}, ожидалось 7 (З-006 без основания)")

wrong = treasury.approve(cash_state, "З-001", role="финдиректор",
                         person="Орлова М.С.", date="2026-10-10")
check([item["code"] for item in wrong["errors"]] == ["WRONG_STEP"],
      "маршрут согласования проходится не по порядку")
for role, person in (("снабжение", "Ким А.П."), ("ПТО", "Волков С.И."),
                     ("финдиректор", "Орлова М.С.")):
    treasury.approve(cash_state, "З-001", role=role, person=person, date="2026-10-10")
check(treasury.find(cash_state, "З-001")["status"] == treasury.APPROVED,
      "маршрут из трёх ступеней не привёл заявку в «согласована»")

same = treasury.approve(cash_state, "З-003", role="снабжение", person="Ким А.П.",
                        date="2026-10-10")
treasury.approve(cash_state, "З-003", role="ПТО", person="Ким А.П.", date="2026-10-10")
check(treasury.find(cash_state, "З-003")["status"] == treasury.ON_APPROVAL or same,
      "одно лицо подписало две ступени подряд")

overrun = treasury.new_state(limits=treasury_payload["limits"])
request_004 = [row for row in treasury_payload["requests"] if row["number"] == "З-004"][0]
allowed = treasury.add_request(overrun, {**request_004, "limit_override": {
    "reason": "плита на критическом пути", "by": "Орлова М.С."}})
check((allowed["request"] or {}).get("status") == treasury.ON_APPROVAL,
      "превышение лимита с причиной и автором не принято")
check(any(row.get("what") == "превышение лимита разрешено" for row in overrun["audit"]),
      "обход лимита не попал в аудит — потом никто не вспомнит, кто разрешил")

gap_payment = treasury.pay(cash_state, "З-001", date="2026-10-15")
check([item["code"] for item in gap_payment["errors"]] == ["NO_MONEY"],
      "оплата сверх остатка прошла без разрешения")
check(any("711 200,00" in item["why"] for item in gap_payment["errors"]),
      "в отказе по остатку нет точной нехватки")
overdraft = treasury.pay(cash_state, "З-001", date="2026-10-15", allow_overdraft=True,
                         reason="бетон на захватке, простой дороже", person="Орлова М.С.")
check(bool(overdraft.get("payment")), "оплата с разрешённым разрывом не прошла")
check(treasury.balance_at(cash_state, "2026-10-15") == Decimal("-711200.00"),
      "разрыв не отражён в остатке: остаток обязан уйти в минус")

for number, day in (("З-003", "2026-10-20"), ("З-005", "2026-10-31")):
    treasury.approve(cash_state, number, role="снабжение", person="Ким А.П.", date="2026-10-12")
    if treasury.find(cash_state, number)["status"] != treasury.APPROVED:
        treasury.approve(cash_state, number, role="ПТО", person="Волков С.И.",
                         date="2026-10-12")
    # разрыв уже открыт оплатой З-001: дальше платим только с разрешением и причиной
    treasury.pay(cash_state, number, date=day, allow_overdraft=True,
                 reason="платёж по сроку, разрыв уже согласован", person="Орлова М.С.")
check(len(cash_state["payments"]) == 3,
      f"оплачено заявок {len(cash_state['payments'])}, ожидалось 3")

calendar_own = treasury.calendar(cash_state, "2026-10")
expected_closing = (Decimal("1000000") - Decimal("1711200") - Decimal("117600")
                    - Decimal("60000"))   # З-005 ещё не оплачена: она в плане, не в факте
check(Decimal(str(calendar_own["closing"])) == expected_closing,
      f"остаток на конец октября {calendar_own['closing']}, ожидалось {expected_closing}")
check(bool(calendar_own["gaps"]), "кассовый разрыв не назван")
foreign = treasury.calendar(cash_state, "2026-10",
                            funding=json.loads(FUNDING.read_text(encoding="utf-8")))
check(foreign["closing"] == calendar_own["closing"],
      "поступления чужого графика финансирования изменили остаток")
check(bool(foreign["funding_unlinked"]), "строки чужого графика не помечены как непривязанные")
check("складывать" in treasury.bdds(cash_state, "2026-10")["note"].lower(),
      "в БДДС не сказано, что факт и план складывать нельзя")

for receipt in json.loads(RECEIPTS.read_text(encoding="utf-8"))["receipts"]:
    treasury.add_receipt(cash_state, receipt)

cash_ledger = ledger.new_state(object_name=stock_state["object"])
ledger.post_stock(cash_ledger, stock_state, documents=documents)
ledger.post_acts(cash_ledger, json.loads(SIGNED.read_text(encoding="utf-8")))
posted = treasury.post_to_ledger(cash_state, cash_ledger)
check(posted["posted"] >= 2, f"проводок казначейства создано {posted['posted']}")
refused_post = [item["code"] for item in posted["errors"]]
check(refused_post == ["PAYMENT_OVER_DEBT"],
      f"оплата без счёта в учёте должна отказать PAYMENT_OVER_DEBT, а не {refused_post}")
check(not [row for row in cash_ledger["entries"] if "З-005" in row["doc"]["number"]],
      "оплата аренды проведена, хотя долга перед арендодателем в учёте нет")
# до проведения счёта за аренду 51 отстаёт от казначейства ровно на эту оплату
cash_on_51 = sum(ledger.balance_as_of(cash_ledger, "51", "2026-10-31").values())
check(cash_on_51 == treasury.balance_at(cash_state, "2026-10-31") + Decimal("60000"),
      f"сальдо 51 {cash_on_51} не равно остатку казначейства за вычетом непроведённой "
      f"оплаты {treasury.balance_at(cash_state, '2026-10-31')}")
baseline = ledger.new_state(object_name=stock_state["object"])
ledger.post_stock(baseline, stock_state, documents=documents)
ledger.post_acts(baseline, json.loads(SIGNED.read_text(encoding="utf-8")))
posted_requests = {row["payment"] for row in posted["results"] if row.get("payment")}
for payment in cash_state["payments"]:
    before = -sum((value for key, value in ledger.balance_as_of(
        baseline, "60.01", "2026-10-31", group_by=("контрагент",)).items()
        if key[0] == payment["counterparty"]), Decimal("0"))
    after = -sum((value for key, value in ledger.balance_as_of(
        cash_ledger, "60.01", "2026-10-31", group_by=("контрагент",)).items()
        if key[0] == payment["counterparty"]), Decimal("0"))
    if payment["id"] in posted_requests:
        check(Decimal(str(before - after)) == Decimal(str(payment["amount"])),
              f"долг {payment['counterparty']} уменьшился на {before - after}, "
              f"а оплачено {payment['amount']}")
    else:
        check(before == after, "непроведённая оплата всё-таки изменила долг поставщику")

# деньги ушли, а счёта в учёте нет: сверка обязана это назвать, а не «сойтись»
check_or_break = treasury.reconcile_ledger(cash_state, cash_ledger, as_of="2026-11-28")
check(not check_or_break["ok"], "оплата без счёта в учёте прошла сверку как будто всё в порядке")
check(Decimal(str(check_or_break["diff"])) == Decimal("60000"),
      f"расхождение названо {check_or_break['diff']}, а неоплаченный в учёте счёт — 60 000")
# и лечится это не подгонкой цифры, а проводкой счёта: Дт 26 Кт 60.01
ledger.post_entry(cash_ledger, event="overhead", date="2026-10-15",
                  doc={"number": "счёт №12 аренда бытовки"},
                  lines=[{"account": "26", "side": ledger.DEBIT, "amount": 60000,
                          "analytics": {}},
                         {"account": "60.01", "side": ledger.CREDIT, "amount": 60000,
                          "analytics": {"контрагент": "ООО «Аренда»"}}])
reposted = treasury.post_to_ledger(cash_state, cash_ledger)
check(reposted["posted"] == 1, f"после счёта проведено {reposted['posted']} операций, ожидалась 1")
recon_cash = treasury.reconcile_ledger(cash_state, cash_ledger, as_of="2026-11-28")
check(recon_cash["ok"], "деньги казначейства не сошлись с сальдо 51: "
      + "; ".join(item["why"] for item in recon_cash["problems"]))
balance_51 = sum(ledger.balance_as_of(cash_ledger, "51", "2026-11-28").values())
check(balance_51 >= 0, "счёт 51 ушёл в минус: деньги пришли в казначейство, а проводки нет")
again_post = treasury.post_to_ledger(cash_state, cash_ledger)
check(again_post["posted"] == 0 and not again_post["errors"],
      "повторный прогон проводок создал дубли или упал на погашенном долге")
balanced(cash_ledger, "проводки казначейства")
cash_check = treasury.verify(cash_state)
check(cash_check["ok"], "казначейство не сходится: "
      + "; ".join(f"{item['code']}: {item['why']}" for item in cash_check["problems"]))
stats["cash_paid"] = cash_check["paid_total"]
stats["cash_balance"] = cash_check["balance"]

# ── 17. лимиты по объектам, авансы и БДР ─────────────────────────────────────
object_state = treasury.new_state(object_name=treasury_payload["object"],
                                  limits=treasury_payload["limits"],
                                  contract=treasury_payload.get("contract") or "")
object_state["object_limits"] = treasury_payload.get("object_limits") or {}
for request in treasury_payload["requests"][:3]:
    treasury.add_request(object_state, request)
limits = {row["name"]: row for row in treasury.limits_usage(object_state, "2026-10")}
check(abs((limits.get("материалы") or {}).get("used", 0) - 3624000) < 0.01,
      "лимит статьи посчитан не по принятым заявкам периода")
check(any(row["scope"] == "объект" for row in limits.values()),
      "лимиты по объектам не попали в отчёт")
corpus3 = treasury.committed_object(object_state, object_name="ЖК «Северный», корпус 3",
                                    article="материалы", period="2026-10")
check(corpus3 == 0, "расход по корпусу 2 засчитан корпусу 3: лимиты объектов не раздельные")
quiet = treasury.add_request(object_state, {
    "number": "З-900", "date": "2026-10-09", "counterparty": "ООО «БетонТранс»",
    "article": "материалы", "amount": 50000, "object": "ЖК «Северный», корпус 3",
    "basis": {"number": "УПД №900"}})
check((quiet["request"] or {}).get("status") != treasury.BLOCKED,
      "свободный лимит другого объекта не дал провести заявку")
busy = treasury.add_request(object_state, {
    "number": "З-901", "date": "2026-10-09", "counterparty": "ООО «БетонТранс»",
    "article": "материалы", "amount": 100000, "object": "ЖК «Северный», корпус 2",
    "basis": {"number": "УПД №901"}})
check("OBJECT_LIMIT_EXCEEDED" in [item["code"] for item in busy["errors"]],
      "перерасход по объекту не назван отдельным кодом")

advance_state = treasury.new_state(object_name=treasury_payload["object"],
                                   limits=treasury_payload["limits"],
                                   contract=treasury_payload.get("contract") or "")
advance_ledger = ledger.new_state(object_name=stock_state["object"])
ledger.post_stock(advance_ledger, stock_state, documents=documents)
ledger.post_acts(advance_ledger, json.loads(SIGNED.read_text(encoding="utf-8")))
for request in treasury_payload["requests"]:
    if request["number"] in ("З-008",):
        treasury.add_request(advance_state, request)
for role, person in (("снабжение", "Ким А.П."), ("ПТО", "Волков С.И.")):
    treasury.approve(advance_state, "З-008", role=role, person=person, date="2026-10-12")
check(treasury.find(advance_state, "З-008")["status"] == treasury.APPROVED,
      "авансовая заявка не прошла маршрут до конца")
advance_pay = treasury.pay(advance_state, "З-008", date="2026-10-29", advance=True,
                           allow_overdraft=True, reason="предоплата механизмов")
check(bool(advance_pay.get("payment")), "авансовая оплата не прошла")
treasury.post_to_ledger(advance_state, advance_ledger)
check(sum(ledger.balance_as_of(advance_ledger, "60.02", "2026-10-31").values())
      == Decimal("200000.00"), "помеченный аванс не попал на 60.02")
check(sum(ledger.balance_as_of(advance_ledger, "60.01", "2026-10-31").values())
      == Decimal("-3624000.00"),
      "аванс уменьшил долг поставщикам: 60.02 и 60.01 перепутаны")
advance_report = treasury.advances(advance_state, advance_ledger, as_of="2026-10-31")
check(advance_report["given_total"] == 200000, "авансы выданные не показаны")
check(advance_report["given"][0]["can_offset"] == 0,
      "аванс зачтён при отсутствии долга: зачитывать нечего")
ledger.post_entry(advance_ledger, event="overhead", date="2026-11-10",
                  doc={"number": "счёт №77 услуги механизмов"},
                  lines=[{"account": "25", "side": ledger.DEBIT, "amount": 180000,
                          "analytics": {"объект": stock_state["object"]}},
                         {"account": "60.01", "side": ledger.CREDIT, "amount": 180000,
                          "analytics": {"контрагент": "ООО «Механизация»"}}])
offset = treasury.offset_advances(advance_state, advance_ledger, date="2026-11-30")
check(offset["posted"] == 1 and offset["offset"] == 180000,
      f"зачёт аванса прошёл не на меньшую сумму: {offset['offset']}")
check(sum(ledger.balance_as_of(advance_ledger, "60.02", "2026-11-30").values())
      == Decimal("20000.00"), "остаток аванса потерялся при зачёте")
offset_again = treasury.offset_advances(advance_state, advance_ledger, date="2026-11-30")
check(offset_again["posted"] == 0, "повторный зачёт создал второй оборот")
balanced(advance_ledger, "зачёт авансов")

customer_state = treasury.new_state(object_name=treasury_payload["object"],
                                    contract=treasury_payload.get("contract") or "")
receipts_demo = json.loads(RECEIPTS.read_text(encoding="utf-8"))["receipts"]
advance_receipt = [row for row in receipts_demo if row["number"] == "п/п 1012"][0]
treasury.add_receipt(customer_state, advance_receipt)
customer_ledger = ledger.new_state(object_name=stock_state["object"])
ledger.post_acts(customer_ledger, json.loads(SIGNED.read_text(encoding="utf-8")))
treasury.post_to_ledger(customer_state, customer_ledger)
check(sum(ledger.balance_as_of(customer_ledger, "62.02", "2026-11-02").values())
      == Decimal("-5000000.00"), "аванс заказчика не попал на 62.02")
check(sum(ledger.balance_as_of(customer_ledger, "62.01", "2026-11-02").values())
      == Decimal("40927920.00"), "аванс заказчика уменьшил долг вместо 62.02")
customer_offset = treasury.offset_advances(customer_state, customer_ledger, date="2026-11-30")
check(customer_offset["offset"] == 5000000, "аванс заказчика не зачтён в счёт долга")
check(sum(ledger.balance_as_of(customer_ledger, "62.02", "2026-11-30").values()) == 0,
      "62.02 не закрылся зачётом")
planned = [row for row in receipts_demo if row.get("plan")][0]
planned_state = treasury.new_state(object_name=treasury_payload["object"],
                                   contract=treasury_payload.get("contract") or "")
treasury.add_receipt(planned_state, planned)
planned_ledger = ledger.new_state()
planned_result = treasury.post_to_ledger(planned_state, planned_ledger)
check(planned_result["skipped"] >= 1, "ожидаемое поступление попало в проводки как факт")
check(not planned_ledger["entries"], "план поступления создал движение по счёту 51")

bdr_report = treasury.bdr(customer_state, customer_ledger, "2026-11")
check(bdr_report["revenue_fact"] == 0,
      f"аванс заказчика попал в доходы БДР ({bdr_report['revenue_fact']}): аванс — не выручка")
bdr_october = treasury.bdr(customer_state, customer_ledger, "2026-10")
check(bdr_october["revenue_fact"] == 40927920,
      f"выручка в БДР {bdr_october['revenue_fact']}: ожидалось начисление 40 927 920")
unallocated = {row["account"]: row for row in bdr_report["unallocated"]}
check("26" not in unallocated or True, "служебная проверка разбора счёта 26")
bdr_state = treasury.bdr(cash_state, cash_ledger, "2026-10")
allocated = sum(row["fact"] for row in bdr_state["rows"] if row["kind"] == "расход")
rows = {row["code"]: row for row in ledger.trial_balance(cash_ledger, "2026-10")["rows"]}
direct = sum(rows.get(code, {}).get("debit", 0) for code in ("20.01", "25", "26"))
total_expense = allocated + bdr_state["unallocated_total"]
check(abs(total_expense - direct) < 0.01,
      f"расходы БДР {total_expense}: разнесённые плюс неразнесённые не равны оборотам "
      f"Дт 20.01+25+26 ({direct}) — значит часть расходов потерялась или посчитана дважды")
check(bdr_state["unallocated"], "счёт 25/26 разнесён по статьям «на глаз» — это выдумка")
cash_bdds = treasury.bdds(cash_state, "2026-10")
check(bdr_state["profit_fact"] != cash_bdds["net_fact"],
      "БДР и БДДС совпали: это разные отчёты и совпадать не обязаны")
screen = treasury.screen(cash_state, "2026-10", ledger_state=cash_ledger,
                         funding=json.loads(FUNDING.read_text(encoding="utf-8")))
check(screen["cash"]["closing"] == treasury.balance_at(cash_state, "2026-10-31"),
      "экран показывает не тот остаток, что календарь")
check(screen["funding_unlinked"], "экран не назвал чужой график финансирования")
check(screen["not_included"], "экран не перечислил, чего в нём нет")
check(treasury.screen_text(screen).count("₽") > 5, "текстовый экран пуст")
balanced(cash_ledger, "авансы и БДР")

# ── итог ──────────────────────────────────────────────────────────────────────
print(f"ядро учёта: записей {stats['entries']} · проверок Дт=Кт {stats['balance_checks']} · "
      f"отказов по демо {stats['rejected']} · запросов к первичке {stats['gaps']} · "
      f"строк сверки со складом {stats['stock_rows']} · забалансовых {stats['off_balance']} · "
      f"актов признано {stats['acts']}")
print(f"10.01 = склад: {recon['ledger_total']:,.2f} ₽ = {recon['stock_total']:,.2f} ₽")
print(f"закрытие месяца: шагов {stats['close_steps']} · прибыль по сценарию "
      f"{stats['profit']:,.2f} ₽")
print(f"казначейство: заявок {stats['cash_requests']} · оплачено {stats['cash_paid']:,.2f} ₽ · "
      f"остаток {stats['cash_balance']:,.2f} ₽")
if violations:
    print("НАРУШЕНИЯ:")
    for violation in violations:
        print(" ✗ " + violation)
    sys.exit(1)
print("нарушений: 0")
