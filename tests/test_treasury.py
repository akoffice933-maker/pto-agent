# -*- coding: utf-8 -*-
"""Казначейство: заявка, маршрут, лимит, календарь, БДДС, проводки в учёт.

Проверяется поведение при отказах, а не «функция вернула словарь»:

1. заявка без документа-основания — не заявка, а запрос основания;
2. маршрут идёт по порядку: подпись не той ступени — отказ с указанием очереди;
3. одно лицо не согласует две ступени подряд;
4. лимит проверяется до согласования, превышение — блокировка с точной разницей;
5. обход лимита возможен только с причиной и автором, и это видно в аудите;
6. оплата — только по согласованной заявке; переплата невозможна;
7. оплата сверх остатка — только с явным «разрешаю разрыв» и причиной;
8. заблокированная заявка лимит не занимает (иначе ложное превышение);
9. календарь: поступления чужого договора не прячут кассовый разрыв;
10. БДДС: факт и план раздельно, «складывать нельзя» сказано словами;
11. проводка оплаты — Дт 60.01 Кт 51, поступления — Дт 51 Кт 62.01, до копейки;
12. оплата больше долга поставщику не проводится (аванс не выдумывается);
13. деньги в казначействе сходятся с сальдо 51 — иначе это разрыв, а не «мелочь»;
14. verify ловит подделку: подпись без маршрута, дубль платежа.
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import ledger, stock, treasury

DEMO_STATE = ROOT / "data" / "demo" / "treasury" / "state_2026_10.json"
DEMO_RECEIPTS = ROOT / "data" / "demo" / "treasury" / "receipts_2026_10.json"
DEMO_DOCS = ROOT / "data" / "demo" / "stock" / "documents_2026_10.json"
ACTS = ROOT / "data" / "demo" / "accounting" / "acts_signed_scenario.json"

TEAM = {"снабжение": "Ким А.П.", "ПТО": "Волков С.И.", "финдиректор": "Орлова М.С."}


# ─────────────────────────── помощники ───────────────────────────
def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def demo_state() -> dict:
    payload = read(DEMO_STATE)
    state = treasury.new_state(object_name=payload["object"], opening=payload["opening"],
                               limits=payload["limits"], contract=payload.get("contract") or "")
    state["object_limits"] = payload.get("object_limits") or {}
    for request in payload["requests"]:
        treasury.add_request(state, request)
    return state


def codes(result: dict) -> list[str]:
    return [item["code"] for item in result["errors"]]


def approve_all(state: dict, number: str, date: str = "2026-10-10") -> dict:
    request = treasury.find(state, number)
    result = {}
    for role in request["route"]:
        result = treasury.approve(state, number, role=role, person=TEAM[role], date=date)
    return result


def accounting_state(services: bool = True) -> dict:
    """Учёт на октябрь: склад, подписанная КС-2 и счета за услуги — долги под заявки."""
    st = stock.demo_state()
    state = ledger.new_state(object_name=st["object"])
    ledger.post_stock(state, st, documents=read(DEMO_DOCS))
    ledger.post_acts(state, read(ACTS))
    bills = ([("счёт №77", 180000, "25", "ООО «Механизация»"),
              ("счёт №12", 60000, "26", "ООО «Аренда»")] if services else [])
    for number, amount, account, counterparty in bills:
        ledger.post_entry(state, event="overhead", date="2026-10-15",
                          doc={"number": number},
                          lines=[{"account": account, "side": ledger.DEBIT, "amount": amount,
                                  "analytics": ({"объект": "ЖК «Северный», корпус 2"}
                                                if account == "25" else {})},
                                 {"account": "60.01", "side": ledger.CREDIT, "amount": amount,
                                  "analytics": {"контрагент": counterparty}}])
    return state


# ─────────────────────────── 1. основание ───────────────────────────
def test_request_without_basis_is_not_a_request():
    state = demo_state()
    request = treasury.find(state, "З-006")
    assert request is None, "заявка без основания не должна попадать в реестр"
    assert not [row for row in state["requests"] if row["number"] == "З-006"]


def test_no_basis_answer_explains_what_to_bring():
    payload = read(DEMO_STATE)
    request = [row for row in payload["requests"] if row["number"] == "З-006"][0]
    result = treasury.add_request(treasury.new_state(), request)
    assert codes(result) == ["NO_BASIS"]
    assert "счёт, УПД, акт или договор" in result["gaps"][0]["ask"]


def test_unknown_article_is_refused_with_list_of_known():
    result = treasury.add_request(treasury.new_state(), {
        "number": "З-100", "counterparty": "ООО «X»", "article": "командировочные",
        "amount": 1000, "basis": {"number": "счёт 1"}})
    assert codes(result) == ["NO_ARTICLE"]
    assert "материалы" in result["errors"][0]["why"]


def test_receipt_article_cannot_be_used_for_payment_request():
    result = treasury.add_request(treasury.new_state(), {
        "number": "З-101", "counterparty": "ООО «X»", "article": "оплата КС-2",
        "amount": 1000, "basis": {"number": "счёт 1"}})
    assert codes(result) == ["NOT_EXPENSE"]


# ─────────────────────────── 2. маршрут ───────────────────────────
def test_route_grows_with_amount():
    rules = treasury.load_rules()
    assert treasury.route_for(rules, 50000)["steps"] == ["снабжение"]
    assert treasury.route_for(rules, 150000)["steps"] == ["снабжение", "ПТО"]
    assert treasury.route_for(rules, 100000)["steps"] == ["снабжение"], "порог включается"
    assert treasury.route_for(rules, 1711200)["steps"] == ["снабжение", "ПТО", "финдиректор"]
    assert treasury.route_for(rules, 9500000)["steps"] == ["снабжение", "ПТО", "финдиректор"]


def test_step_order_is_enforced():
    state = demo_state()
    result = treasury.approve(state, "З-001", role="финдиректор",
                              person=TEAM["финдиректор"], date="2026-10-10")
    assert codes(result) == ["WRONG_STEP"]
    assert "снабжение" in result["errors"][0]["why"]


def test_route_cannot_be_finished_and_then_reused():
    state = demo_state()
    approve_all(state, "З-001")
    request = treasury.find(state, "З-001")
    assert request["status"] == treasury.APPROVED
    steps = len(request["approvals"])
    again = treasury.approve(state, "З-001", role="финдиректор",
                             person=TEAM["финдиректор"], date="2026-10-11")
    assert again["warnings"], "повторная подпись не должна проходить молча"
    assert len(treasury.find(state, "З-001")["approvals"]) == steps, \
        "пятая подпись в маршруте из трёх ступеней не появляется"


def test_one_person_cannot_sign_two_steps_in_a_row():
    state = demo_state()
    treasury.approve(state, "З-001", role="снабжение", person="Ким А.П.", date="2026-10-10")
    result = treasury.approve(state, "З-001", role="ПТО", person="Ким А.П.", date="2026-10-10")
    assert codes(result) == ["SAME_PERSON"]


# ─────────────────────────── 3. лимиты ───────────────────────────
def test_limit_blocks_request_with_exact_overrun():
    payload = read(DEMO_STATE)
    request = [row for row in payload["requests"] if row["number"] == "З-004"][0]
    state = treasury.new_state(limits=payload["limits"])
    result = treasury.add_request(state, request)
    assert result["request"]["status"] == treasury.BLOCKED
    assert result["errors"][0]["code"] == "LIMIT_EXCEEDED"
    assert "80 000,00" in result["errors"][0]["why"]


def test_blocked_request_does_not_consume_limit():
    state = demo_state()
    committed = treasury.committed(state, article="механизмы", period="2026-10")
    assert committed == Decimal("0.00"), \
        "заблокированная заявка не занимает лимит, иначе следующая получит ложное превышение"


def test_overrun_is_allowed_only_with_reason_and_author():
    payload = read(DEMO_STATE)
    request = [row for row in payload["requests"] if row["number"] == "З-004"][0]
    state = treasury.new_state(limits=payload["limits"])
    no_reason = treasury.add_request(state, dict(request))
    assert no_reason["request"]["status"] == treasury.BLOCKED
    no_author = treasury.add_request(treasury.new_state(limits=payload["limits"]), {
        **request, "limit_override": {"reason": "плита на критическом пути"}})
    assert no_author["request"]["status"] == treasury.BLOCKED, \
        "причина без автора не разрешение: спросить будет некого"
    fresh = treasury.new_state(limits=payload["limits"])
    allowed = treasury.add_request(fresh, {
        **request, "limit_override": {"reason": "плита на критическом пути, перенос даёт простой",
                                      "by": "Орлова М.С."}})
    assert allowed["request"]["status"] == treasury.ON_APPROVAL
    audit = [row for row in fresh["audit"] if row.get("what") == "превышение лимита разрешено"]
    assert audit and "критическом пути" in audit[0]["reason"]
    assert audit[0]["excess"] == 80000


def test_limit_counts_submitted_requests_and_frees_on_reject():
    state = demo_state()
    assert treasury.committed(state, article="материалы", period="2026-10") \
        == Decimal("3624000.00"), "лимит занимают все поданные заявки периода"
    treasury.reject(state, "З-003", role="снабжение", person=TEAM["снабжение"],
                    date="2026-10-08", reason="поставка отложена на ноябрь")
    assert treasury.committed(state, article="материалы", period="2026-10") \
        == Decimal("3506400.00"), "отклонённая заявка лимит не занимает"


# ─────────────────────────── 4. оплата ───────────────────────────
def test_payment_requires_approval():
    state = demo_state()
    result = treasury.pay(state, "З-003", date="2026-10-20")
    assert codes(result) == ["NOT_APPROVED"]


def test_overpayment_is_impossible():
    state = demo_state()
    approve_all(state, "З-003")
    result = treasury.pay(state, "З-003", date="2026-10-20", amount=200000)
    assert result["status"] == "переплата"
    assert codes(result) == ["OVERPAY"]


def test_payment_over_balance_needs_explicit_permission():
    state = demo_state()
    approve_all(state, "З-001")
    refused = treasury.pay(state, "З-001", date="2026-10-15")
    assert codes(refused) == ["NO_MONEY"]
    assert "711 200,00" in refused["errors"][0]["why"]
    no_reason = treasury.pay(state, "З-001", date="2026-10-15", allow_overdraft=True)
    assert codes(no_reason) == ["OVERDRAFT_NO_REASON"], "разрыв без причины не оплачивается"
    paid = treasury.pay(state, "З-001", date="2026-10-15", allow_overdraft=True,
                        reason="бетон на захватке, остановка дороже", person="Орлова М.С.")
    assert paid["payment"]["amount"] == 1711200
    assert treasury.balance_at(state, "2026-10-15") == Decimal("-711200.00")
    audit = [row for row in state["audit"] if row.get("what") == "оплата с кассовым разрывом"]
    assert audit and audit[0]["gap"] == 711200 and audit[0]["by"] == "Орлова М.С."


def test_partial_payments_close_request_only_at_full_amount():
    state = demo_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20", amount=100000)
    assert treasury.find(state, "З-003")["status"] == treasury.PARTIALLY_PAID
    treasury.pay(state, "З-003", date="2026-10-21", amount=17600)
    assert treasury.find(state, "З-003")["status"] == treasury.PAID


def test_late_payment_records_the_lag():
    state = demo_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-25")
    payment = state["payments"][0]
    assert payment["late_days"] == 5, "оплата позже срока — это 5 дней просрочки, а не «вовремя»"


def test_unpaid_overdue_request_stays_in_the_list():
    state = demo_state()
    approve_all(state, "З-001")          # срок 2026-10-15
    approve_all(state, "З-003")          # срок 2026-10-20
    treasury.pay(state, "З-003", date="2026-10-20")
    report = treasury.calendar(state, "2026-10")
    numbers = [row["number"] for row in report["overdue"]]
    assert "З-001" in numbers, "срок прошёл, деньги не ушли — заявка остаётся в просрочке"
    assert "З-003" not in numbers, "оплаченная заявка в просрочке не висит"
    assert "З-002" not in numbers, "срок в ноябре — в октябрьской просрочке его нет"


# ─────────────────────────── 5. календарь и БДДС ───────────────────────────
def test_calendar_shows_gap_when_money_runs_out():
    state = demo_state()
    approve_all(state, "З-001")
    treasury.pay(state, "З-001", date="2026-10-15", allow_overdraft=True,
                 reason="бетон на захватке", person="Орлова М.С.")
    report = treasury.calendar(state, "2026-10")
    assert report["closing"] == Decimal("-711200.00")
    assert report["gaps"] and report["gaps"][0]["date"] == "2026-10-15"
    assert report["gaps"][0]["amount"] == Decimal("711200.00")


def test_foreign_funding_schedule_does_not_hide_the_gap():
    state = demo_state()
    approve_all(state, "З-001")
    treasury.pay(state, "З-001", date="2026-10-15", allow_overdraft=True,
                 reason="бетон на захватке", person="Орлова М.С.")
    plan = read(ROOT / "data" / "demo" / "funding" / "plan.json")
    report = treasury.calendar(state, "2026-10", funding=plan)
    assert report["closing"] == Decimal("-711200.00"), \
        "поступления другого договора не могут делать остаток положительным"
    assert report["funding_unlinked"], "чужие строки графика обязаны быть названы"


def test_own_funding_schedule_is_taken_into_account():
    state = demo_state()
    approve_all(state, "З-001")
    treasury.pay(state, "З-001", date="2026-10-15", allow_overdraft=True,
                 reason="бетон на захватке", person="Орлова М.С.")
    plan = {"contract": {"number": state["contract"]}, "payments": [
        {"act_id": "КС-2 №38", "payment_date": "2026-10-15", "amount_rub": 1000000}]}
    report = treasury.calendar(state, "2026-10", funding=plan)
    assert report["closing"] == Decimal("288800.00"), \
        "поступление по своему договору обязано закрывать разрыв"
    assert not report["gaps"]
    assert report["funding_rows"]


def test_bdds_keeps_fact_and_plan_apart():
    state = demo_state()
    approve_all(state, "З-001")
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    report = treasury.bdds(state, "2026-10")
    materials = [row for row in report["rows"] if row["article"] == "материалы"][0]
    assert materials["out_fact"] == 117600
    assert materials["out_plan"] == 1711200
    assert "в одно «итого» нельзя" in report["note"]


def test_receipts_are_not_payments():
    state = demo_state()
    for row in read(DEMO_RECEIPTS)["receipts"]:
        treasury.add_receipt(state, row)
    report = treasury.bdds(state, "2026-11")
    assert report["out_fact"] == 0 and report["out_plan"] == 0, \
        "поступления не превращаются в расходы"
    assert report["in_plan"] > 0, "ноябрьские п/п ещё не пришли — это план"
    assert report["in_fact"] == 5000000, \
        "в факте только аванс п/п 1012 (он уже пришёл), ожидаемые 40,9 млн — план"


# ─────────────────────────── 6. проводки в учёт ───────────────────────────
def test_payment_becomes_debit_60_credit_51():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    result = treasury.post_to_ledger(state, accounting)
    entries = [row["entry"] for row in result["results"]
               if row.get("entry") and "З-003" in row["entry"]["doc"]["number"]]
    assert len(entries) == 1
    entry = entries[0]
    assert {line["account"] for line in entry["lines"]} == {"60.01", "51"}
    debit = [line for line in entry["lines"] if line["account"] == "60.01"][0]
    credit = [line for line in entry["lines"] if line["account"] == "51"][0]
    assert debit["side"] == ledger.DEBIT and debit["amount"] == 117600
    assert credit["side"] == ledger.CREDIT and credit["amount"] == 117600
    assert debit["analytics"]["контрагент"] == "ООО «СтройБаза»"


def test_receipt_becomes_debit_51_credit_62():
    state = demo_state()
    accounting = accounting_state()
    receipt = {"number": "п/п 9901", "date": "2026-11-05", "amount": 1000000,
               "counterparty": "ООО «Заказчик-Инвест»", "contract": "Д-142/2026",
               "article": "оплата КС-2", "basis": "частичная оплата КС-2 №38"}
    treasury.add_receipt(state, receipt)
    treasury.post_to_ledger(state, accounting)
    entry = [row for row in accounting["entries"]
             if "п/п 9901" in str(row["doc"].get("number", ""))][0]
    lines = {line["account"]: line for line in entry["lines"]}
    assert lines["51"]["side"] == ledger.DEBIT and lines["51"]["amount"] == 1000000
    assert lines["62.01"]["side"] == ledger.CREDIT


def test_payment_above_debt_is_refused_not_turned_into_advance():
    state = demo_state()
    accounting = accounting_state(services=False)   # долга перед арендодателем нет
    approve_all(state, "З-005")
    treasury.pay(state, "З-005", date="2026-10-20")
    result = treasury.post_to_ledger(state, accounting)
    refused = [row for row in result["results"] if not row.get("entry")]
    assert refused and refused[0]["errors"][0]["code"] == "PAYMENT_OVER_DEBT"
    assert "аванс" in refused[0]["errors"][0]["why"]


def test_posting_twice_creates_no_duplicates():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    treasury.post_to_ledger(state, accounting)
    entries = len(accounting["entries"])
    second = treasury.post_to_ledger(state, accounting)
    assert len(accounting["entries"]) == entries
    assert second["posted"] == 0 and second["duplicates"] == 2, \
        "повторный прогон ничего не создаёт: и остаток, и оплата уже проведены"
    assert not second["errors"], \
        "повтор не должен упираться в «долг погашен»: проводка уже есть, это не отказ"


def test_opening_balance_keeps_51_out_of_the_red():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    treasury.post_to_ledger(state, accounting)
    balance = sum(ledger.balance_as_of(accounting, "51", "2026-10-31").values())
    assert balance == Decimal("882400.00"), \
        "без проводки входящего остатка оплаты уводят счёт 51 в минус"


def test_cash_matches_account_51():
    state = demo_state()
    for row in read(DEMO_RECEIPTS)["receipts"]:
        treasury.add_receipt(state, row)
    accounting = accounting_state()
    approve_all(state, "З-003")
    approve_all(state, "З-005")
    treasury.pay(state, "З-003", date="2026-10-20")
    treasury.pay(state, "З-005", date="2026-10-31")
    treasury.post_to_ledger(state, accounting)
    check = treasury.reconcile_ledger(state, accounting, as_of="2026-10-31")
    assert check["ok"], check["problems"]
    assert check["cash"] == 822400 and check["ledger"] == 822400


def test_cash_mismatch_is_named_not_hidden():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    check = treasury.reconcile_ledger(state, accounting, as_of="2026-10-31")
    assert not check["ok"]
    assert check["problems"][0]["code"] == "CASH_51_MISMATCH"
    assert "расхождение" in check["problems"][0]["why"]
    assert check["diff"] == -882400, "счёт 51 пуст, а в казначействе уже заплачено"


# ─────────────────────────── 7. лимиты по объектам ───────────────────────────
def test_object_limit_blocks_only_its_own_object():
    state = demo_state()
    request = {"number": "З-900", "date": "2026-10-09", "counterparty": "ООО «БетонТранс»",
               "article": "материалы", "amount": 50000,
               "object": "ЖК «Северный», корпус 3", "basis": {"number": "УПД №900"}}
    result = treasury.add_request(state, request)
    assert result["request"]["status"] != treasury.BLOCKED, \
        "на корпусе 3 лимит 1,2 млн и он свободен — экономия одного объекта не блокирует другой"
    busy = treasury.add_request(state, {**request, "number": "З-901",
                                        "object": "ЖК «Северный», корпус 2"})
    assert busy["request"]["status"] == treasury.BLOCKED
    assert "OBJECT_LIMIT_EXCEEDED" in [item["code"] for item in busy["errors"]], \
        "перерасход на корпусе 2 обязан назвать именно лимит объекта"


def test_object_limit_reports_exact_overrun():
    payload = read(DEMO_STATE)
    request = [row for row in payload["requests"] if row["number"] == "З-007"][0]
    state = treasury.new_state(object_name=payload["object"], limits=payload["limits"])
    state["object_limits"] = payload["object_limits"]
    for row in payload["requests"][:3]:
        treasury.add_request(state, row)
    result = treasury.add_request(state, request)
    assert result["request"]["status"] == treasury.BLOCKED
    object_error = [item for item in result["errors"]
                    if item["code"] == "OBJECT_LIMIT_EXCEEDED"]
    assert object_error and "600 000,00" in object_error[0]["why"], \
        "в отказе по объекту нет точной суммы превышения"


def test_object_limit_counts_requests_of_that_object_only():
    state = demo_state()
    used = treasury.committed_object(state, object_name="ЖК «Северный», корпус 2",
                                     article="материалы", period="2026-10")
    assert used == Decimal("3624000.00")
    assert treasury.committed_object(state, object_name="ЖК «Северный», корпус 3",
                                     article="материалы", period="2026-10") == Decimal("0")


def test_object_limit_override_needs_reason_and_author():
    payload = read(DEMO_STATE)
    request = [row for row in payload["requests"] if row["number"] == "З-007"][0]
    state = treasury.new_state(object_name=payload["object"], limits=payload["limits"])
    state["object_limits"] = payload["object_limits"]
    for row in payload["requests"][:3]:
        treasury.add_request(state, row)
    result = treasury.add_request(state, {**request, "limit_override": {
        "reason": "бетон на захватке, иначе простой", "by": "Орлова М.С."}})
    assert result["request"]["status"] != treasury.BLOCKED
    assert [row for row in state["audit"]
            if row.get("what") == "превышение лимита объекта разрешено"]


def test_limits_usage_shows_articles_and_objects_together():
    state = demo_state()
    rows = treasury.limits_usage(state, "2026-10")
    scopes = {row["scope"] for row in rows}
    assert scopes == {"статья", "объект"}
    materials = [row for row in rows if row["name"] == "материалы"][0]
    assert materials["used"] == 3624000 and materials["limit"] == 3700000
    assert materials["pct"] == 97.9


# ─────────────────────────── 8. авансы ───────────────────────────
def test_tagged_advance_goes_to_60_02_entirely():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-008")
    treasury.pay(state, "З-008", date="2026-10-29", advance=True)
    treasury.post_to_ledger(state, accounting)
    six_two = sum(ledger.balance_as_of(accounting, "60.02", "2026-10-31").values())
    six_one = sum(ledger.balance_as_of(accounting, "60.01", "2026-10-31").values())
    assert six_two == Decimal("200000.00"), "аванс обязан жить на 60.02, а не растворятся в долге"
    assert six_one == Decimal("-3864000.00"), "аванс не должен уменьшать долг поставщикам"


def test_advance_keeps_payment_valid_even_without_debt():
    state = demo_state()
    accounting = accounting_state(services=False)
    approve_all(state, "З-008")
    treasury.pay(state, "З-008", date="2026-10-29", advance=True)
    result = treasury.post_to_ledger(state, accounting)
    assert not result["errors"], "помеченный аванс не должен упираться в PAYMENT_OVER_DEBT"


def test_untagged_payment_above_debt_still_refused():
    state = demo_state()
    accounting = accounting_state(services=False)
    treasury.find(state, "З-008")["advance"] = False   # снимаем пометку аванса
    approve_all(state, "З-008")
    treasury.pay(state, "З-008", date="2026-10-29")
    result = treasury.post_to_ledger(state, accounting)
    assert [item["code"] for item in result["errors"]] == ["PAYMENT_OVER_DEBT"]


def test_customer_advance_goes_to_62_02_not_revenue():
    state = demo_state()
    accounting = accounting_state()
    receipt = [row for row in read(DEMO_RECEIPTS)["receipts"] if row["number"] == "п/п 1012"][0]
    treasury.add_receipt(state, receipt)
    treasury.post_to_ledger(state, accounting)
    assert sum(ledger.balance_as_of(accounting, "62.02", "2026-11-02").values()) \
        == Decimal("-5000000.00")
    receiving = [row for row in accounting["entries"]
                 if "п/п аванс п/п 1012" in str(row["doc"]["number"])][0]
    accounts = {line["account"] for line in receiving["lines"]}
    assert accounts == {"51", "62.02"}, "аванс заказчика не идёт в выручку и не гасит долг"


def test_planned_receipt_is_not_posted():
    state = demo_state()
    accounting = accounting_state()
    planned = [row for row in read(DEMO_RECEIPTS)["receipts"] if row.get("plan")][0]
    treasury.add_receipt(state, planned)
    result = treasury.post_to_ledger(state, accounting)
    assert result["skipped"] >= 1
    assert not [row for row in accounting["entries"]
                if planned["number"] in str(row["doc"]["number"])]


def test_advances_report_shows_offsettable_amounts():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-008")
    treasury.pay(state, "З-008", date="2026-10-29", advance=True)
    treasury.add_receipt(state, [row for row in read(DEMO_RECEIPTS)["receipts"]
                                 if row["number"] == "п/п 1012"][0])
    treasury.post_to_ledger(state, accounting)
    report = treasury.advances(state, accounting, as_of="2026-11-02")
    assert report["given_total"] == 200000 and report["received_total"] == 5000000
    given = report["given"][0]
    assert given["counterparty"] == "ООО «Механизация»"
    assert given["can_offset"] == 180000, "зачитывается долг (180 000), а не весь аванс (200 000)"
    received = report["received"][0]
    assert received["can_offset"] == 5000000, "аванс заказчика закрывается долгом по договору"


def test_offset_closes_advance_and_debt_together():
    state = demo_state()
    accounting = accounting_state()
    treasury.add_receipt(state, [row for row in read(DEMO_RECEIPTS)["receipts"]
                                 if row["number"] == "п/п 1012"][0])
    treasury.post_to_ledger(state, accounting)
    result = treasury.offset_advances(state, accounting, date="2026-11-30")
    assert result["posted"] == 1 and result["offset"] == 5000000
    assert sum(ledger.balance_as_of(accounting, "62.02", "2026-11-30").values()) == 0
    assert sum(ledger.balance_as_of(accounting, "62.01", "2026-11-30").values()) \
        == Decimal("35927920.00")


def test_offset_twice_does_nothing():
    state = demo_state()
    accounting = accounting_state()
    treasury.add_receipt(state, [row for row in read(DEMO_RECEIPTS)["receipts"]
                                 if row["number"] == "п/п 1012"][0])
    treasury.post_to_ledger(state, accounting)
    treasury.offset_advances(state, accounting, date="2026-11-30")
    entries = len(accounting["entries"])
    again = treasury.offset_advances(state, accounting, date="2026-11-30")
    assert again["posted"] == 0 and len(accounting["entries"]) == entries


def test_supplier_advance_offsets_only_when_debt_appears():
    state = demo_state()
    accounting = accounting_state(services=False)
    approve_all(state, "З-008")
    treasury.pay(state, "З-008", date="2026-10-29", advance=True)
    treasury.post_to_ledger(state, accounting)
    empty = treasury.offset_advances(state, accounting, date="2026-10-31")
    assert empty["posted"] == 0, "без долга зачитывать нечего"
    ledger.post_entry(accounting, event="overhead", date="2026-11-10",
                      doc={"number": "счёт №77 услуги механизмов"},
                      lines=[{"account": "25", "side": ledger.DEBIT, "amount": 180000,
                              "analytics": {"объект": "ЖК «Северный», корпус 2"}},
                             {"account": "60.01", "side": ledger.CREDIT, "amount": 180000,
                              "analytics": {"контрагент": "ООО «Механизация»"}}])
    result = treasury.offset_advances(state, accounting, date="2026-11-30")
    assert result["offset"] == 180000, "зачитывается меньшая из сумм: долг, а не весь аванс"
    assert sum(ledger.balance_as_of(accounting, "60.02", "2026-11-30").values()) \
        == Decimal("20000.00"), "остаток аванса обязан остаться на 60.02"


# ─────────────────────────── 9. БДР ───────────────────────────
def test_bdr_counts_accrual_not_payments():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    treasury.post_to_ledger(state, accounting)
    report = treasury.bdr(state, accounting, "2026-10")
    assert report["revenue_fact"] == 40927920, "выручка в БДР — начисление по 90.01"
    assert report["profit_fact"] == report["revenue_fact"] - report["expense_fact"]
    materials = [row for row in report["rows"] if row["article"] == "материалы"][0]
    assert materials["source_fact"] == "обороты Дт 20.01"


def test_bdr_does_not_split_account_that_is_in_several_articles():
    state = demo_state()
    accounting = accounting_state()
    report = treasury.bdr(state, accounting, "2026-10")
    unallocated = {row["account"]: row for row in report["unallocated"]}
    assert "26" in unallocated, "счёт 26 висит в пяти статьях — разнести нельзя, это надо сказать"
    assert "аналитик" in unallocated["26"]["why"]
    assert report["unallocated_total"] == 240000, "25 (180 000) и 26 (60 000) разнести нечем"
    allocated = sum(row["fact"] for row in report["rows"] if row["kind"] == "расход")
    assert allocated == 2626500, "разнесённая часть — только прямые затраты"


def test_bdr_plan_comes_from_requests_and_excludes_blocked():
    state = demo_state()
    accounting = accounting_state()
    report = treasury.bdr(state, accounting, "2026-10")
    materials = [row for row in report["rows"] if row["article"] == "материалы"][0]
    assert materials["plan"] == 3624000, \
        "в план идут принятые заявки, а заблокированная З-007 (600 000) — нет"


def test_bdr_and_bdds_are_different_reports():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    treasury.post_to_ledger(state, accounting)
    bdr_report = treasury.bdr(state, accounting, "2026-10")
    bdds_report = treasury.bdds(state, "2026-10")
    assert bdr_report["profit_fact"] != bdds_report["net_fact"], \
        "прибыль по начислению и денежный поток — разные величины, и совпасть случайно могут"
    assert "разные методы" in bdr_report["note"]


# ─────────────────────────── 10. один экран ───────────────────────────
def test_screen_puts_money_first_and_names_what_is_not_included():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-001")
    treasury.pay(state, "З-001", date="2026-10-15", allow_overdraft=True,
                 reason="бетон на захватке", person="Орлова М.С.")
    treasury.post_to_ledger(state, accounting)
    report = treasury.screen(state, "2026-10", funding=read(ROOT / "data" / "demo" / "funding" / "plan.json"),
                             ledger_state=accounting)
    assert report["cash"]["closing"] == -711200
    assert report["cash"]["gaps"], "разрыв обязан быть на экране"
    assert report["funding_unlinked"], "чужой график назван, а не спрятан"
    assert report["bdds"] and report["bdr"] and report["advances"] is not None
    assert report["not_included"], "«чего нет» перечислено словами"
    text = treasury.screen_text(report)
    assert "разрыв" in text.lower() and "БДР" in text


def test_screen_funding_of_own_contract_enters_the_balance():
    state = demo_state()
    accounting = accounting_state()
    report = treasury.screen(state, "2026-10",
                             funding={"contract": {"number": state["contract"]},
                                      "payments": [{"act_id": "КС-2 №38",
                                                    "payment_date": "2026-10-15",
                                                    "amount_rub": 1000000}]},
                             ledger_state=accounting)
    assert report["cash"]["in_plan"] == 1000000
    assert not report["funding_unlinked"]


# ─────────────────────────── 11. verify ───────────────────────────
def test_verify_passes_on_clean_state():
    state = demo_state()
    for row in read(DEMO_RECEIPTS)["receipts"]:
        treasury.add_receipt(state, row)
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    assert treasury.verify(state)["ok"]


def test_verify_catches_signature_without_route_step():
    state = demo_state()
    approve_all(state, "З-001")
    request = treasury.find(state, "З-001")
    request["route"] = request["route"] + ["главный инженер"]   # в маршруте 4 ступени, подписей 3
    report = treasury.verify(state)
    assert not report["ok"]
    assert any(problem["code"] == "ROUTE_INCOMPLETE" for problem in report["problems"])


def test_verify_catches_duplicated_payment():
    state = demo_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    state["payments"].append(dict(state["payments"][0]))
    report = treasury.verify(state)
    assert not report["ok"]
    assert {problem["code"] for problem in report["problems"]} & {
        "OVERPAID", "PAID_SUM_MISMATCH"}, "задвоенная оплата обязана всплыть в verify"


def test_ledger_verifies_after_treasury_posting():
    state = demo_state()
    accounting = accounting_state()
    approve_all(state, "З-003")
    treasury.pay(state, "З-003", date="2026-10-20")
    treasury.post_to_ledger(state, accounting)
    assert ledger.verify(accounting)["ok"]
    trial = ledger.trial_balance(accounting, "2026-10")
    assert trial["balanced"], trial.get("diff")
    debit = sum(row["debit"] for row in trial["rows"])
    credit = sum(row["credit"] for row in trial["rows"])
    assert round(debit, 2) == round(credit, 2)
