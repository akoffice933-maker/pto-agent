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
12. у каждой проводки есть документ-основание: записей «по смыслу» нет.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent import closing, ledger, stock                              # noqa: E402

DOCS = ROOT / "data" / "demo" / "stock" / "documents_2026_10.json"
CATALOG = ROOT / "data" / "demo" / "stock" / "catalog.json"
INVOICES = ROOT / "data" / "demo" / "accounting" / "invoices_2026_10.json"
SIGNED = ROOT / "data" / "demo" / "accounting" / "acts_signed_scenario.json"

violations: list[str] = []
stats = {"entries": 0, "balance_checks": 0, "rejected": 0, "gaps": 0,
         "stock_rows": 0, "off_balance": 0, "acts": 0, "close_steps": 0, "profit": 0.0}


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

# ── итог ──────────────────────────────────────────────────────────────────────
print(f"ядро учёта: записей {stats['entries']} · проверок Дт=Кт {stats['balance_checks']} · "
      f"отказов по демо {stats['rejected']} · запросов к первичке {stats['gaps']} · "
      f"строк сверки со складом {stats['stock_rows']} · забалансовых {stats['off_balance']} · "
      f"актов признано {stats['acts']}")
print(f"10.01 = склад: {recon['ledger_total']:,.2f} ₽ = {recon['stock_total']:,.2f} ₽")
print(f"закрытие месяца: шагов {stats['close_steps']} · прибыль по сценарию "
      f"{stats['profit']:,.2f} ₽")
if violations:
    print("НАРУШЕНИЯ:")
    for violation in violations:
        print(" ✗ " + violation)
    sys.exit(1)
print("нарушений: 0")
