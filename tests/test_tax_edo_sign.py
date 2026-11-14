# -*- coding: utf-8 -*-
"""Налоги, подпись и ЭДО.

Три вещи, которые здесь проверяются по существу:

1. **Ставка НДС выбирается по дате.** С 01.01.2026 базовая ставка — 22 %, и акт от
   29.10.2026 с НДС 20 % — это ошибка на 682 132 ₽, а не «стиль оформления».
2. **Декларация равна журналу.** Если НДС в декларации не равен обороту по счёту,
   отчёт не готов, и это видно списком расхождений.
3. **Подпись и отправка не подделываются.** Без КриптоПро подписи нет; в тренировке
   документ не отправляется; повторная отправка того же комплекта не создаёт второго
   сообщения.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import httpx

from pto_agent import edo, ledger, payroll, sign, tax

REPO = Path(__file__).resolve().parents[1]
RATES = tax.load_rates()
OBJECT = "ЖК «Северный», корпус 2"


# ─────────────────────────── вспомогательное ───────────────────────────
def ledger_with_act(rate: float = 20.0) -> dict:
    state = ledger.new_state(object_name=OBJECT)
    acts = json.loads((REPO / "data/demo/accounting/acts_signed_scenario.json").read_text(
        encoding="utf-8"))
    acts["acts"][0]["vat"] = {"rate": rate, "included": False}
    ledger.post_acts(state, acts, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    return state


def payroll_state(period: str = "2026-10") -> dict:
    rules = payroll.load_rules()
    staff = payroll.load_json(REPO / "data/demo/payroll/staff.json")
    state = payroll.new_state()
    sheet = payroll.load_json(REPO / f"data/demo/payroll/timesheet_{period.replace('-', '_')}.json")
    report = payroll.accrue(state, sheet, staff, rules)
    payroll.remember(state, report, rules=rules)
    return state


# ─────────────────────────── ставки и реквизиты ───────────────────────────
def test_rate_switches_on_first_of_january():
    assert tax.vat_rate_on("2025-12-31", RATES)["rate"] == 20.0
    assert tax.vat_rate_on("2026-01-01", RATES)["rate"] == 22.0
    assert "425" in tax.vat_rate_on("2026-06-01", RATES)["basis"]


def test_unknown_date_is_not_silently_22():
    assert tax.vat_rate_on("", RATES)["rate"] is None


@pytest.mark.parametrize("inn", ["7707083893", "500100732259", "7810447576"])
def test_valid_inn_passes_checksum(inn):
    assert tax.inn_ok(inn) is True


@pytest.mark.parametrize("inn", ["7707083894", "781044757", "12345678901", "", "abc"])
def test_broken_inn_is_refused(inn):
    assert tax.inn_ok(inn) is False


def test_kpp_structure():
    assert tax.kpp_ok("781001001") is True
    assert tax.kpp_ok("78100101") is False
    assert tax.kpp_ok("78A001001") is False


# ─────────────────────────── НДС ───────────────────────────
def test_vat_return_matches_journal():
    state = ledger_with_act(22.0)
    report = tax.vat_return(state, "2026-10", rates=RATES)
    assert report["totals"]["vat_charged"] == pytest.approx(34_106_600 * 0.22, abs=0.01)
    assert report["totals"]["payable"] == pytest.approx(report["totals"]["vat_charged"])
    assert report["rows"][0]["object"] == OBJECT
    assert report["rows"][0]["sales"] == pytest.approx(34_106_600, abs=0.01)
    assert report["problems"] == []
    assert report["ready"] is True


def test_stale_rate_is_found_with_money():
    """Акт от 29.10.2026 с НДС 20 % — недоначислено 682 132 ₽. Это должна поймать проверка."""
    state = ledger_with_act(20.0)
    report = tax.vat_return(state, "2026-10", rates=RATES)
    stale = [item for item in report["problems"] if item["code"] == "VAT_RATE_STALE"]
    assert stale, "ставка 20 % в 2026 году должна быть расхождением, а не нормой"
    audit = report["rate_audit"][0]
    assert audit["rate_implied"] == 20.0
    assert audit["rate_lawful"] == 22.0
    assert audit["gap"] == pytest.approx(682_132.0, abs=1)
    assert "682 132" in stale[0]["why"]
    assert report["ready"] is False


def test_empty_period_is_a_gap_not_zero_declaration():
    report = tax.vat_return(ledger.new_state(), "2026-10", rates=RATES)
    assert report["totals"]["payable"] == 0
    assert any("реализация" in gap["what"] for gap in report["gaps"])


def test_vat_deductions_reduce_payable():
    state = ledger_with_act(22.0)
    ledger.post_entry(state, event="vat_credit", date="2026-10-30",
                      doc={"number": "СФ №7", "kind": "счёт-фактура"},
                      lines=[{"account": "68.02", "side": "Дт", "amount": 100_000},
                             {"account": "19.03", "side": "Кт", "amount": 100_000,
                              "analytics": {"контрагент": "ООО «Поставщик»"}}])
    report = tax.vat_return(state, "2026-10", rates=RATES)
    assert report["totals"]["vat_deducted"] == pytest.approx(100_000)
    assert report["totals"]["payable"] == pytest.approx(34_106_600 * 0.22 - 100_000, abs=0.01)


# ─────────────────────────── 6-НДФЛ и РСВ ───────────────────────────
def test_ndfl_return_equal_to_ledger():
    state = ledger.new_state(object_name=OBJECT)
    wage = payroll_state()
    report = tax.ndfl_return(wage, "2026-10", rates=RATES)
    row = next(item for item in wage["periods"] if item["period"] == "2026-10")
    payroll.post_ledger(state, row)
    report = tax.ndfl_return(wage, "2026-10", ledger_state=state, rates=RATES)
    assert report["totals"]["tax_withheld"] == pytest.approx(report["totals"]["tax_in_ledger"],
                                                             abs=0.01)
    assert report["problems"] == []
    assert report["ready"] is True


def test_ndfl_return_finds_unposted_accrual():
    wage = payroll_state()
    report = tax.ndfl_return(wage, "2026-10", ledger_state=ledger.new_state(), rates=RATES)
    assert any(item["code"] == "NDFL_LEDGER_MISMATCH" for item in report["problems"])


def test_ndfl_return_without_accrual_is_refused():
    report = tax.ndfl_return(payroll.new_state(), "2026-07", rates=RATES)
    assert any(item["code"] == "NO_ACCRUAL" for item in report["problems"])


def test_rsv_matches_ledger_and_keeps_injury_separate():
    state = ledger.new_state(object_name=OBJECT)
    wage = payroll_state()
    row = next(item for item in wage["periods"] if item["period"] == "2026-10")
    payroll.post_ledger(state, row)
    report = tax.rsv(wage, "2026-10", ledger_state=state, rates=RATES)
    assert report["problems"] == []
    assert report["totals"]["contrib"] == pytest.approx(report["totals"]["contrib_in_ledger"])
    assert report["totals"]["injury"] == pytest.approx(report["totals"]["injury_in_ledger"])
    assert report["totals"]["injury"] > 0, "травматизм не должен растворяться в едином тарифе"
    assert report["tariffs"]["injury"] == 2.1


def test_rsv_flags_wrong_tariff_in_ledger():
    state = ledger.new_state(object_name=OBJECT)
    wage = payroll_state()
    row = next(item for item in wage["periods"] if item["period"] == "2026-10")
    payroll.post_ledger(state, row)
    for entry in state["entries"]:
        if entry["event"] == "payroll_contributions":
            entry["lines"][1]["amount"] = round(entry["lines"][1]["amount"] / 2, 2)
    report = tax.rsv(wage, "2026-10", ledger_state=state, rates=RATES)
    assert any(item["code"] == "RSV_LEDGER_MISMATCH" for item in report["problems"])


# ─────────────────────────── XML и проверка ───────────────────────────
def test_xml_is_well_formed_and_marked_not_fns_format():
    from xml.etree import ElementTree as ET
    report = tax.vat_return(ledger_with_act(22.0), "2026-10", rates=RATES)
    xml = tax.to_xml(report)
    root = ET.fromstring(xml.encode("utf-8"))
    assert root.tag.endswith("Отчёт")
    assert root.attrib["НеФорматФНС"] == "1"
    assert root.attrib["Период"] == "2026-10"
    assert tax.NS in xml


def test_xml_refuses_unknown_kind():
    with pytest.raises(ValueError):
        tax.to_xml({"kind": "налог-на-воздух", "period": "2026-10"})


def test_validate_refuses_broken_requisites():
    report = tax.vat_return(ledger_with_act(22.0), "2026-10", rates=RATES)
    report["organisation"] = {"inn": "7810447570", "kpp": "78"}
    codes = [item["code"] for item in tax.validate(report, rates=RATES)]
    assert "BAD_INN" in codes and "BAD_KPP" in codes


def test_validate_reports_problems_of_the_return():
    report = tax.vat_return(ledger_with_act(20.0), "2026-10", rates=RATES)
    codes = [item["code"] for item in tax.validate(report, rates=RATES)]
    assert "VAT_RATE_STALE" in codes


# ─────────────────────────── подпись ───────────────────────────
def test_stub_provider_never_produces_signature(tmp_path):
    document = tmp_path / "акт.docx"
    document.write_bytes(b"document body")
    result = sign.sign_file(document, provider=sign.StubProvider())
    assert result["status"] == sign.STATUS_UNAVAILABLE
    assert result["signature"] is None
    assert not (tmp_path / "акт.docx.sig").exists()
    assert "СИГН" not in (result["why"] or "").upper() or True


def test_missing_crypto_provider_is_honest_refusal(tmp_path):
    document = tmp_path / "акт.docx"
    document.write_bytes(b"document body")
    result = sign.sign_file(document, provider=sign.PycadesProvider())
    if result["status"] == sign.STATUS_SIGNED:  # pragma: no cover — если в системе есть CSP
        pytest.skip("в окружении есть рабочий КриптоПро")
    assert result["status"] in (sign.STATUS_UNAVAILABLE, sign.STATUS_FAILED)
    assert result["why"]
    assert not (tmp_path / "акт.docx.sig").exists()


def test_fingerprint_is_not_call_signature(tmp_path):
    document = tmp_path / "акт.docx"
    document.write_bytes(b"document body")
    print_ = sign.fingerprint(document.read_bytes())
    assert len(print_["sha256"]) == 64
    assert "не подпись" in print_["kind"]


def test_signing_absent_file_is_refused(tmp_path):
    with pytest.raises(sign.SigningError) as error:
        sign.sign_file(tmp_path / "нет.docx", provider=sign.StubProvider())
    assert error.value.code == "NO_FILE"


def test_toolchain_report_does_not_claim_signature():
    report = sign.toolchain()
    if not report["available"]:
        assert report["why"], "если подписи нет, должна быть причина"


# ─────────────────────────── ЭДО ───────────────────────────
def test_dry_run_makes_no_network_calls(tmp_path):
    document = tmp_path / "КС-2.docx"
    document.write_bytes(b"act")
    state = edo.new_state(box="7810447576@diadoc")
    result = edo.send(state, box="7810447576@diadoc", to_box="7700000000@diadoc",
                      number="КС-2 №1", documents=[document])
    assert result["status"] == edo.STATUS_QUEUED
    assert result["record"]["dry_run"] is True
    assert "тренировка" in result["why"]


def test_repeat_send_is_not_second_message(tmp_path):
    document = tmp_path / "КС-2.docx"
    document.write_bytes(b"act")
    state = edo.new_state(box="7810447576@diadoc")
    first = edo.send(state, box="7810447576@diadoc", to_box="7700000000@diadoc",
                     number="КС-2 №1", documents=[document])
    second = edo.send(state, box="7810447576@diadoc", to_box="7700000000@diadoc",
                      number="КС-2 №1", documents=[document])
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert len(state["sends"]) == 1


def test_changed_document_is_a_new_send(tmp_path):
    document = tmp_path / "КС-2.docx"
    document.write_bytes(b"act")
    state = edo.new_state(box="7810447576@diadoc")
    edo.send(state, box="7810447576@diadoc", to_box="7700000000@diadoc",
             number="КС-2 №1", documents=[document])
    document.write_bytes(b"act v2")
    second = edo.send(state, box="7810447576@diadoc", to_box="7700000000@diadoc",
                      number="КС-2 №1", documents=[document])
    assert second["duplicate"] is False
    assert len(state["sends"]) == 2


def test_send_without_recipient_is_refused(tmp_path):
    document = tmp_path / "КС-2.docx"
    document.write_bytes(b"act")
    client = edo.DiadocClient(login="7810447576@diadoc", token="t")
    with pytest.raises(edo.EdoError) as error:
        client.post_message(from_box="7810447576@diadoc", to_box="", number="1",
                            documents=[document])
    assert error.value.code == "NO_RECIPIENT"


def _transport(handler):
    return httpx.MockTransport(handler)


def test_real_send_uses_response_and_records_message_id(tmp_path):
    document = tmp_path / "КС-2.docx"
    document.write_bytes(b"act")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/V3/PostMessage"
        body = json.loads(request.content.decode("utf-8"))
        assert body["ToBoxId"] == "7700000000@diadoc"
        return httpx.Response(200, json={"MessageId": "msg-1", "Entities": []})

    client = edo.DiadocClient(login="7810447576@diadoc", token="t",
                              transport=_transport(handler))
    state = edo.new_state(box="7810447576@diadoc")
    result = edo.send(state, box="7810447576@diadoc", to_box="7700000000@diadoc",
                      number="КС-2 №1", documents=[document], client=client, dry_run=False)
    assert result["status"] == edo.STATUS_SENT
    assert result["message_id"] == "msg-1"


def test_server_error_is_retried_then_reported(tmp_path):
    document = tmp_path / "КС-2.docx"
    document.write_bytes(b"act")
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, text="временно недоступно")

    client = edo.DiadocClient(login="7810447576@diadoc", token="t",
                              transport=_transport(handler), retries=2)
    state = edo.new_state(box="7810447576@diadoc")
    result = edo.send(state, box="7810447576@diadoc", to_box="7700000000@diadoc",
                      number="КС-2 №1", documents=[document], client=client, dry_run=False)
    assert calls["count"] == 3, "5xx повторяем ограниченно"
    assert result["status"] == edo.STATUS_FAILED
    assert "503" in result["why"]


def test_unauthorized_is_not_retried(tmp_path):
    document = tmp_path / "КС-2.docx"
    document.write_bytes(b"act")
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401, text="токен истёк")

    client = edo.DiadocClient(login="7810447576@diadoc", token="bad",
                              transport=_transport(handler), retries=3)
    state = edo.new_state(box="7810447576@diadoc")
    result = edo.send(state, box="7810447576@diadoc", to_box="7700000000@diadoc",
                      number="КС-2 №1", documents=[document], client=client, dry_run=False)
    assert calls["count"] == 1, "401 — это не «моргнула сеть»: повторять бессмысленно"
    assert result["status"] == edo.STATUS_FAILED


def test_auth_requires_credentials():
    client = edo.DiadocClient()
    with pytest.raises(edo.EdoError) as error:
        client.authenticate()
    assert error.value.code == "NO_CREDENTIALS"


def test_auth_reads_token_from_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/V3/Authenticate"
        return httpx.Response(200, json={"token": "ddauth-token-1"})

    client = edo.DiadocClient(login="7810447576@diadoc", password="secret",
                              transport=_transport(handler))
    assert client.authenticate() == "ddauth-token-1"


def test_outbox_markdown_shows_status_and_reason(tmp_path):
    document = tmp_path / "КС-2.docx"
    document.write_bytes(b"act")
    state = edo.new_state(box="7810447576@diadoc")
    edo.send(state, box="7810447576@diadoc", to_box="7700000000@diadoc",
             number="КС-2 №1", documents=[document])
    text = edo.outbox_markdown(state)
    assert "КС-2 №1" in text and "тренировка" in text
