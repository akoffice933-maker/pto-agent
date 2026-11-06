# -*- coding: utf-8 -*-
"""График финансирования и комплекты: что попадёт в платёж, что переносится, сколько стоит перенос.

Правила:

1. сроки предъявления и даты платежей берутся только из графика — нет даты, значит «не заявлено»;
2. сумма сверяется с актом; платят по акту, расхождение — ошибка;
3. в платёж попадает только акт с предъявляемым комплектом, остальное — перенос с причиной;
4. пени — только по заявленной в договоре ставке и с пунктом-основанием, иначе «цена переноса
   не заявлена», а не 0 ₽;
5. удержание — только из договора; «к оплате» есть только у платежа, который наступает;
6. акт в двух строках графика считается один раз.
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from pto_agent import correspondence as corr
from pto_agent import docflow, journals, quality_docs
from pto_agent.funding import (CHECK_ACT_DOUBLE_IN_PAYMENTS, CHECK_ACT_NOT_IN_PORTFOLIO,
                               CHECK_ACT_WITHOUT_PAYMENT, CHECK_AMOUNT_MISMATCH,
                               CHECK_DEADLINE_MISSED, CHECK_DEADLINE_NOT_STATED,
                               CHECK_PAYMENTS_EMPTY, CHECK_PAYMENT_BEFORE_DEADLINE,
                               CHECK_PAYMENT_WITHOUT_AMOUNT, CHECK_PORTFOLIO_MISSING,
                               CHECK_RETENTION_NOT_STATED, CHECK_TRANSFER_COST_NOT_STATED,
                               PAY_IN_PAYMENT, PAY_NOT_LINKED, PAY_TRANSFER, SCHEMA,
                               build_plan, verdict, write)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
TODAY = date(2026, 11, 6)
BY, ROLE = "Иванов И.И.", "начальник ПТО"


def _codes(plan) -> list[str]:
    return [c["code"] for c in plan["checks"]]


def _clean_pack(tmp_path: Path, sign_work: bool = True) -> dict:
    """Комплект из восьми собранных реестров — подписанных (или нет)."""
    out = tmp_path / "built"
    out.mkdir(parents=True, exist_ok=True)
    work = journals.build_work_journal(
        [{"date": "2026-10-01", "shift": "1", "work": "Разработка грунта экскаватором",
          "unit": "м3", "qty": 100, "executor": "ООО «Земстрой»", "weather": {"temp_c": 8}}],
        object_name="Школа", number="ОЖР-1", today=TODAY)
    if sign_work:
        work = journals.sign(work, by=BY, role=ROLE, today=TODAY)
    verification = journals.sign(journals.build_verification_journal(
        [{"date": "2026-10-01", "material": "Бетон B25", "unit": "м3", "qty": 60, "batch": "партия 1",
          "decision": "допущен", "inspected_by": BY, "role": "инженер ПТО",
          "quality_doc": {"kind": "паспорт качества", "number": "П-1",
                          "valid_until": "2026-12-31"}}],
        object_name="Школа", number="ВК-1", today=TODAY), by=BY, role=ROLE, today=TODAY)
    quality = quality_docs.sign(quality_docs.build_registry(
        [{"kind": "паспорт качества", "number": "П-1", "issue_date": "2026-09-28",
          "material": "Бетон B25", "batch": "партия 1", "standard": "ГОСТ 7473-2010",
          "file": "b.pdf", "attested_by": {"by": BY, "role": "инженер ПТО"}}],
        object_name="Школа", number="ДК-1",
        requirements=[{"material": "Бетон B25", "required": ["паспорт качества"],
                       "basis": "договор"}],
        usage=[{"material": "Бетон B25", "batch": "партия 1", "qty": 60, "unit": "м3",
                "applied_from": "2026-10-04"}],
        verification=[{"material": "Бетон B25", "batch": "партия 1", "decision": "допущен",
                       "quality_doc": {"kind": "паспорт качества", "number": "П-1"}}],
        today=TODAY), by=BY, role=ROLE, today=TODAY)
    rd = docflow.sign(docflow.build_rd_registry(
        [{"doc": "КЖ-1", "revision": "изм. 1", "date": "2026-09-26", "recipient": "ООО «М»",
          "stamp": "в производство работ", "receipt": {"by": "Соколов И.И."}}],
        revisions=[{"doc": "КЖ-1", "revision": "изм. 1", "current": True}],
        works=[{"work": "Плита", "rd": "КЖ-1"}], object_name="Школа", number="РД-1", today=TODAY),
        by=BY, role=ROLE, today=TODAY)
    submittals = docflow.sign(docflow.build_submittal_registry(
        [{"title": "Комплект ИД", "act_ref": "АОСР-1", "sent_at": "2026-10-06",
          "recipient": "технадзор", "receipt": {"by": "Кузнецов В.В."}, "remarks": [],
          "signature": {"by": "Кузнецов В.В.", "role": "инженер технадзора",
                        "decision": "согласовано", "at": "2026-10-08"}}],
        object_name="Школа", number="СГ-1", today=TODAY), by=BY, role=ROLE, today=TODAY)
    letters = corr.sign(corr.build_letter_registry(
        [{"number": "1", "date": "2026-10-02", "kind": "запрос", "to": {"org": "ООО «М»"},
          "subject": "документы", "basis": {"doc": "договор", "clause": "п. 4.12"},
          "signed_by": {"by": BY, "role": ROLE}}], object_name="Школа", number="ПИС-1", today=TODAY),
        by=BY, role=ROLE, today=TODAY)
    involvement = corr.sign(corr.build_involvement_registry(
        [{"kind": "лаборатория", "organization": "Лаборатория «СтройТест»",
          "attestation": {"number": "RA.RU.1", "scope": "бетон", "valid_until": "2027-04-01"},
          "request": {"number": "З-1", "date": "2026-10-02", "due": "2026-10-09"},
          "result": {"number": "ПИ-1", "date": "2026-10-05", "linked_to": "партия 1"}}],
        object_name="Школа", number="ЛАБ-1", today=TODAY), by=BY, role=ROLE, today=TODAY)
    days = corr.sign(corr.build_quality_days(
        [{"kind": "день качества", "date": "2026-10-09",
          "participants": [{"org": "ООО «М»", "by": "Соколов И.И.", "role": "прораб"}],
          "protocol": {"number": "ДК-2", "date": "2026-10-09"},
          "remarks": [{"text": "накрыть бетон", "responsible": "ООО «М»", "due": "2026-10-20",
                       "closed_at": "2026-10-18"}]}],
        object_name="Школа", number="ДК-1", today=TODAY), by=BY, role=ROLE, today=TODAY)
    return {"object_name": "Школа", "number": "К-Ч", "sections": {
        "journals_work": {"registry": journals.write(work, out)["json"]},
        "journals_verification": {"registry": journals.write(verification, out)["json"]},
        "quality_docs": {"registry": quality_docs.write(quality, out)["json"]},
        "rd": {"registry": docflow.write(rd, out)["json"]},
        "submittals": {"registry": docflow.write(submittals, out)["json"]},
        "letters": {"registry": corr.write(letters, out)["json"]},
        "involvement": {"registry": corr.write(involvement, out)["json"]},
        "quality_days": {"registry": corr.write(days, out)["json"]},
        "checklists": {"enabled": False, "reason": "по этому акту чек-листы не выпускаются"}}}


def _plan(tmp_path: Path, payments: list[dict], **config) -> dict:
    pack = config.pop("pack", None) or _clean_pack(tmp_path)
    act = {"act_id": "КС-2 №1", "amount_rub": 5_000_000}
    act.update(config.pop("act", {}))
    base = {"name": "проба", "contract": {"number": "Д-1"}, "payments": payments,
            "objects": [{"object_name": "Школа", "pack": pack, "acts": [act]}]}
    base.update(config)
    return build_plan(base, base_dir=tmp_path, out_dir=tmp_path / "fin", today=TODAY)


def _payment(**extra) -> dict:
    row = {"month": "2026-11", "act_id": "КС-2 №1", "amount_rub": 5_000_000,
           "submission_deadline": "2026-12-01", "payment_date": "2026-12-20"}
    row.update(extra)
    return row


# ─────────────────────────── в платёж или перенос ───────────────────────────
def test_allowed_pack_goes_to_payment(tmp_path):
    plan = _plan(tmp_path, [_payment()],
                 contract={"number": "Д-1",
                           "retention": {"pct": 5, "basis": "договор п. 5.7"},
                           "payment_terms": {"basis": "договор п. 5.3"}})
    row = plan["payments"][0]
    assert row["status"] == PAY_IN_PAYMENT
    assert row["retention_rub"] == 250_000.0
    assert row["to_pay_rub"] == 4_750_000.0
    assert plan["totals"]["in_payment_rub"] == 4_750_000.0
    assert plan["totals"]["transfer_rub"] == 0.0
    assert "в платёж попадёт 4 750 000.00 ₽ из 5 000 000.00 ₽" in verdict(plan)["summary"]


def test_blocked_pack_becomes_transfer(tmp_path):
    plan = _plan(tmp_path, [_payment()], pack=_clean_pack(tmp_path, sign_work=False))
    row = plan["payments"][0]
    assert row["status"] == PAY_TRANSFER
    assert row["to_pay_rub"] is None, "перенесённая строка — не деньги месяца"
    assert row["blocked_sections"] == ["journals_work"]
    assert plan["totals"]["transfer_rub"] == 5_000_000.0
    assert plan["totals"]["in_payment_rub"] == 0.0


def test_deadline_missed_and_penalty_by_contract(tmp_path):
    plan = _plan(tmp_path, [_payment(submission_deadline="2026-10-01", payment_date="2026-11-01")],
                 pack=_clean_pack(tmp_path, sign_work=False),
                 contract={"number": "Д-1",
                           "penalty": {"pct_per_day": 0.01, "basis": "договор п. 9.1"}})
    row = plan["payments"][0]
    assert CHECK_DEADLINE_MISSED in _codes(plan)
    assert row["days_overdue"] == 36
    assert row["penalty_rub"] == 18_000.0          # 5 000 000 × 0,01 % × 36 дн.
    assert "36 дн." in row["penalty_formula"]
    assert row["penalty_basis"] == "договор п. 9.1"
    assert plan["totals"]["penalty_rub"] == 18_000.0


def test_penalty_without_rate_is_not_zero(tmp_path):
    plan = _plan(tmp_path, [_payment(submission_deadline="2026-10-01", payment_date="2026-11-01")],
                 pack=_clean_pack(tmp_path, sign_work=False))
    row = plan["payments"][0]
    assert CHECK_TRANSFER_COST_NOT_STATED in _codes(plan)
    assert row["penalty_rub"] is None, "без ставки цена переноса не считается"
    assert "не заявлена" in row["penalty_note"]
    assert plan["totals"]["penalty_rub"] == 0.0


def test_retention_only_from_contract(tmp_path):
    plan = _plan(tmp_path, [_payment()])
    row = plan["payments"][0]
    assert CHECK_RETENTION_NOT_STATED in _codes(plan)
    assert row["retention_rub"] is None
    assert row["to_pay_rub"] == 5_000_000.0, "удержание не выдумывается"


def test_deadline_not_stated_is_not_guessed(tmp_path):
    plan = _plan(tmp_path, [_payment(submission_deadline=None, payment_date=None)])
    row = plan["payments"][0]
    assert CHECK_DEADLINE_NOT_STATED in _codes(plan)
    assert row["days_to_deadline"] is None and row["days_overdue"] is None
    assert row["status"] == PAY_IN_PAYMENT, "нет срока — не значит «просрочено»"


def test_payment_before_deadline_is_an_error(tmp_path):
    plan = _plan(tmp_path, [_payment(submission_deadline="2026-12-20", payment_date="2026-12-01")])
    assert CHECK_PAYMENT_BEFORE_DEADLINE in _codes(plan)


def test_amount_mismatch_uses_act(tmp_path):
    plan = _plan(tmp_path, [_payment(amount_rub=4_000_000)])
    row = plan["payments"][0]
    assert CHECK_AMOUNT_MISMATCH in _codes(plan)
    assert row["amount_rub"] == 5_000_000.0, "платят по акту, а не по ошибке в графике"


def test_payment_without_amount_anywhere(tmp_path):
    plan = _plan(tmp_path, [_payment(amount_rub=None)], act={"amount_rub": None})
    assert CHECK_PAYMENT_WITHOUT_AMOUNT in _codes(plan)
    assert plan["payments"][0]["amount_rub"] is None
    assert plan["totals"]["unestimated"] == 1


def test_act_not_in_portfolio_is_not_linked(tmp_path):
    plan = _plan(tmp_path, [{"month": "2026-11", "act_id": "КС-2 №77", "amount_rub": 900_000,
                             "submission_deadline": "2026-11-20", "payment_date": "2026-12-05"}])
    row = plan["payments"][0]
    assert CHECK_ACT_NOT_IN_PORTFOLIO in _codes(plan)
    assert row["status"] == PAY_NOT_LINKED
    assert row["to_pay_rub"] is None
    assert "не сверена" in plan["checks"][0]["message"] or any(
        "не сверена" in c["message"] for c in plan["checks"])


def test_act_without_payment_line(tmp_path):
    plan = _plan(tmp_path, [])
    assert CHECK_ACT_WITHOUT_PAYMENT in _codes(plan)
    assert CHECK_PAYMENTS_EMPTY in _codes(plan)


def test_double_act_in_payments(tmp_path):
    plan = _plan(tmp_path, [_payment(), _payment(month="2026-12")])
    assert CHECK_ACT_DOUBLE_IN_PAYMENTS in _codes(plan)
    assert plan["payments"][1]["double"] is True


def test_portfolio_missing(tmp_path):
    plan = build_plan({"name": "проба", "payments": [_payment()]},
                      base_dir=tmp_path, out_dir=tmp_path / "fin", today=TODAY)
    assert CHECK_PORTFOLIO_MISSING in _codes(plan)
    assert plan["payments"][0]["status"] == PAY_NOT_LINKED
    assert plan["verdict"]["in_payment_rub"] == 0.0


# ─────────────────────────── демо, файлы, CLI, MCP ───────────────────────────
def test_demo_plan(tmp_path):
    config = json.loads((DEMO / "funding" / "plan.json").read_text(encoding="utf-8"))
    plan = build_plan(config, base_dir=ROOT, out_dir=tmp_path / "fin", today=TODAY)
    totals = plan["totals"]
    assert plan["schema"] == SCHEMA
    assert (totals["payments"], totals["missed"], totals["unestimated"]) == (5, 3, 1)
    assert totals["plan_rub"] == 44_124_600.0
    assert totals["transfer_rub"] == 38_524_600.0
    assert totals["penalty_rub"] == 17_910.6
    assert totals["retention_rub"] == 1_926_230.0
    assert [m["label"] for m in plan["months"]] == ["сентябрь 2026", "октябрь 2026",
                                                    "ноябрь 2026", "декабрь 2026"]
    assert totals["in_payment_rub"] == 0.0, "в демо ни один комплект не сдан"
    assert (tmp_path / "fin" / "funding_График_финансирования-_ноябрь_2026.docx").exists()


def test_files_and_honest_words(tmp_path):
    from openpyxl import load_workbook

    config = json.loads((DEMO / "funding" / "plan.json").read_text(encoding="utf-8"))
    plan = build_plan(config, base_dir=ROOT, out_dir=tmp_path / "fin", today=TODAY)
    files = write(plan, tmp_path / "fin2")
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Месяцы", "Платежи", "Блокеры"]
    from pto_agent.funding import to_markdown
    markdown = to_markdown(plan).lower()
    for forbidden in ("платежи гарантированы", "оплата обеспечена", "риска нет"):
        assert forbidden not in markdown
    assert "перенос" in markdown and "цена переноса" in markdown


def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_finance(tmp_path):
    run = _cli("finance", "--out", str(tmp_path / "fin"), "--base-dir", str(ROOT), "plan",
               "--config", str(DEMO / "funding" / "plan.json"))
    assert run.returncode == 2, "в демо есть блокеры графика"
    assert "строк графика: 5" in run.stdout
    assert "переносится: 38 524 600.00 ₽" in run.stdout
    assert "пени: 17 910.60 ₽" in run.stdout
    assert "DEADLINE_MISSED" in run.stdout


def _mcp_call(name: str, arguments: dict) -> dict:
    payload = "\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
    ]) + "\n"
    proc = subprocess.run([sys.executable, "-m", "pto_agent.mcp_server", "--base-dir", str(ROOT)],
                          input=payload, capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    answers = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return next(a for a in answers if a.get("id") == 2)["result"]


def test_mcp_funding_plan_is_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "funding_plan")
    assert "config" in tool["inputSchema"]["properties"]
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "funding_plan")
    assert schema["version"] == "2.6.0"
    assert any("не заявлена" in g for g in designed["guardrails"])


def test_mcp_funding_plan_returns_monthly(tmp_path):
    result = _mcp_call("funding_plan",
                       {"config_path": str(DEMO / "funding" / "plan.json"),
                        "base_dir": str(ROOT), "out_dir": str(tmp_path / "fin")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["kind"] == SCHEMA
    assert payload["ok"] is False
    assert payload["totals"]["transfer_rub"] == 38_524_600.0
    assert len(payload["months"]) == 4
    assert Path(payload["files"]["docx"]).exists()
