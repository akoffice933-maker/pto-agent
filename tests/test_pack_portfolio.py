# -*- coding: utf-8 -*-
"""Портфель комплектов: где можно предъявлять и сколько денег держит непредъявление.

Правила:

1. сумма акта берётся из акта КС-2 с источником; нет суммы — «не оценено», а не 0 ₽;
2. объект без комплекта — «не оценено», а не «риска нет»;
3. заблокированный объект держит свои акты, предъявляемый — нет;
4. акт с одинаковым номером считается один раз (иначе риск удваивается);
5. разрез «раздел → деньги» строится только по заявленным актам (`covered_by`) и не
   складывается с итогом;
6. деньги разложены по действию: подписи, ошибки, данные — и «только подпись» отделён
   от «и данные, и ошибки, и подписи»;
7. в файлах нет «всё под контролем» и «риска нет» — вердикт называет, что мешает.
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from pto_agent import correspondence as corr
from pto_agent import docflow, journals, quality_docs
from pto_agent.pack_portfolio import (ACT_AT_RISK, ACT_NOT_ESTIMATED, ACT_RELEASABLE,
                                      FIX_ERRORS_SIGN,
                                      CHECK_ACT_DOUBLE_COUNTED, CHECK_ACT_SECTION_UNKNOWN,
                                      CHECK_ACT_WITHOUT_AMOUNT, CHECK_OBJECT_WITHOUT_PACK,
                                      CHECK_PORTFOLIO_EMPTY, FIX_MIXED, FIX_SIGN, SCHEMA,
                                      build_portfolio, to_markdown, verdict, write)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
TODAY = date(2026, 11, 6)
BY, ROLE = "Иванов И.И.", "начальник ПТО"


def _codes(portfolio) -> list[str]:
    return [c["code"] for c in portfolio["checks"]]


def _act(act_id: str, amount, **extra) -> dict:
    row = {"act_id": act_id, "period": "октябрь 2026", "covered_by": ["journals_work"]}
    if amount is not None:
        row["amount_rub"] = amount
    row.update(extra)
    return row


def _clean_pack(tmp_path: Path, sign_work: bool = True) -> dict:
    """Конфиг комплекта из восьми собранных реестров — подписанных (или нет)."""
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


def _portfolio(tmp_path: Path, objects: list[dict]) -> dict:
    return build_portfolio({"portfolio": "проба", "objects": objects},
                           base_dir=tmp_path, out_dir=tmp_path / "pf", today=TODAY)


# ─────────────────────────── деньги и честность ───────────────────────────
def test_object_without_pack_is_not_estimated(tmp_path):
    portfolio = _portfolio(tmp_path, [{"object_name": "Сад", "acts": [_act("КС-2 №1", 1_870_000)]}])
    obj = portfolio["objects"][0]
    assert CHECK_OBJECT_WITHOUT_PACK in _codes(portfolio)
    assert obj["at_risk_rub"] is None, "нет комплекта — это «не оценено», а не 0 ₽"
    assert portfolio["money"]["at_risk_rub"] == 0.0
    assert portfolio["money"]["unestimated_acts"] == 1
    assert portfolio["money"]["unestimated"][0]["why"] == "комплект объекта не собран"
    assert obj["acts"][0]["status"] == ACT_NOT_ESTIMATED


def test_act_without_amount_is_not_zero(tmp_path):
    pack = _clean_pack(tmp_path)
    portfolio = _portfolio(tmp_path, [{"object_name": "Школа", "pack": pack,
                                       "acts": [_act("КС-2 №3", None)]}])
    assert CHECK_ACT_WITHOUT_AMOUNT in _codes(portfolio)
    assert portfolio["money"]["at_risk_rub"] == 0.0
    assert portfolio["objects"][0]["acts"][0]["amount_rub"] is None
    assert portfolio["objects"][0]["acts"][0]["amount_status"] == "не оценена"


def test_amounts_are_read_honestly(tmp_path):
    pack = _clean_pack(tmp_path, sign_work=False)
    acts = [{"act_id": "КС-2 №1", "amount_rub": "2 300 000,50"},
            {"act_id": "КС-2 №2", "amount_rub": "nan"},
            {"act_id": "КС-2 №3", "amount_rub": -5},
            {"act_id": "КС-2 №4", "amount_rub.num": 1}]
    portfolio = _portfolio(tmp_path, [{"object_name": "Школа", "pack": pack, "acts": acts}])
    by_id = {a["act_id"]: a for a in portfolio["objects"][0]["acts"]}
    assert by_id["КС-2 №1"]["amount_rub"] == 2_300_000.5, "строка с пробелами и запятой читается"
    for act_id in ("КС-2 №2", "КС-2 №3", "КС-2 №4"):
        assert by_id[act_id]["amount_rub"] is None, act_id
    assert _codes(portfolio).count(CHECK_ACT_WITHOUT_AMOUNT) == 3


def test_blocked_object_holds_money(tmp_path):
    config = json.loads((DEMO / "pack" / "pack.json").read_text(encoding="utf-8"))
    config["sections"] = {k: (str(DEMO / v) if False else v) for k, v in config["sections"].items()}
    portfolio = build_portfolio({"objects": [{"object_name": "К-38", "pack": config,
                                              "acts": [_act("КС-2 №38", 34_106_600),
                                                       _act("КС-2 №37", 248_000,
                                                            covered_by=["rd"])]}]},
                                base_dir=ROOT, out_dir=tmp_path / "pf", today=TODAY)
    obj = portfolio["objects"][0]
    assert obj["can_submit"] is False
    assert portfolio["totals"]["acts_at_risk"] == 2
    assert portfolio["money"]["at_risk_rub"] == 34_354_600.0
    assert obj["acts"][0]["status"] == ACT_AT_RISK
    assert obj["acts"][0]["attributed_sections"], "акт заявил разделы — они видны"


def test_allowed_object_releases_money(tmp_path):
    pack = _clean_pack(tmp_path)
    portfolio = _portfolio(tmp_path, [{"object_name": "Школа", "pack": pack,
                                       "acts": [_act("КС-2 №1", 5_000_000)]}])
    obj = portfolio["objects"][0]
    assert obj["can_submit"] is True
    assert obj["acts"][0]["status"] == ACT_RELEASABLE
    assert portfolio["money"]["released_rub"] == 5_000_000.0
    assert portfolio["money"]["at_risk_rub"] == 0.0
    assert portfolio["verdict"]["any_releasable"] is True


def test_signature_only_money_is_separated(tmp_path):
    pack = _clean_pack(tmp_path, sign_work=False)     # всё собрано, один раздел не подписан
    portfolio = _portfolio(tmp_path, [{"object_name": "Школа", "pack": pack,
                                       "acts": [_act("КС-2 №1", 5_000_000)]}])
    obj = portfolio["objects"][0]
    assert obj["can_submit"] is False
    assert obj["totals"]["with_errors"] == 0 and obj["totals"]["missing"] == 0
    assert obj["fix"] == FIX_SIGN, obj["fix"]
    assert [row["fix"] for row in portfolio["money"]["by_fix"]] == [FIX_SIGN]
    assert portfolio["money"]["by_fix"][0]["rub"] == 5_000_000.0


def test_mixed_blockers_are_not_sold_as_signature(tmp_path):
    config = json.loads((DEMO / "pack" / "pack.json").read_text(encoding="utf-8"))
    portfolio = build_portfolio({"objects": [{"object_name": "К-38", "pack": config,
                                              "acts": [_act("КС-2 №38", 1_000_000)]}]},
                                base_dir=ROOT, out_dir=tmp_path / "pf", today=TODAY)
    assert portfolio["objects"][0]["fix"] == FIX_ERRORS_SIGN, "данных хватает, но есть ошибки"
    assert portfolio["money"]["by_fix"][0]["fix"] == FIX_ERRORS_SIGN, \
        "деньги не подписаны как «снимается подписью», пока в разделах ошибки"


def test_double_counted_act_counts_once(tmp_path):
    pack = _clean_pack(tmp_path, sign_work=False)
    duplicated = _act("КС-2 №38", 10_000_000)
    portfolio = _portfolio(tmp_path, [
        {"object_name": "Школа", "pack": pack, "acts": [duplicated]},
        {"object_name": "Сад", "pack": pack, "acts": [dict(duplicated)]}])
    assert CHECK_ACT_DOUBLE_COUNTED in _codes(portfolio)
    assert portfolio["totals"]["acts"] == 2
    assert portfolio["money"]["at_risk_rub"] == 10_000_000.0, "риск не удваивается"
    second = portfolio["objects"][1]["acts"][0]
    assert second.get("double_counted") is True
    assert second["at_risk_rub"] is None


def test_money_by_sections_uses_covered_by_only(tmp_path):
    config = json.loads((DEMO / "pack" / "pack.json").read_text(encoding="utf-8"))
    portfolio = build_portfolio({"objects": [{"object_name": "К-38", "pack": config,
                                              "acts": [_act("КС-2 №38", 1_000_000,
                                                            covered_by=["submittals", "letters"])]}]},
                                base_dir=ROOT, out_dir=tmp_path / "pf", today=TODAY)
    rows = {row["section"]: row for row in portfolio["objects"][0]["sections"]}
    assert rows["submittals"]["rub"] == 1_000_000.0
    assert rows["letters"]["rub"] == 1_000_000.0
    assert rows["rd"]["rub"] == 0.0, "раздел, который акт не заявлял, денег не получает"
    assert "не заявляли" in rows["rd"]["basis"]
    section_sum = sum(row["rub"] for row in rows.values())
    assert section_sum > portfolio["money"]["at_risk_rub"], \
        "разрез по разделам не складывается с итогом — это сказано в отчёте"
    assert "не складывается" in to_markdown(portfolio)


def test_unknown_section_in_covered_by(tmp_path):
    pack = _clean_pack(tmp_path, sign_work=False)
    portfolio = _portfolio(tmp_path, [{"object_name": "Школа", "pack": pack,
                                       "acts": [_act("КС-2 №1", 100, covered_by=["нет такого"])]}])
    assert CHECK_ACT_SECTION_UNKNOWN in _codes(portfolio)
    assert portfolio["objects"][0]["acts"][0]["covered_by"] == []


def test_empty_portfolio_is_an_error(tmp_path):
    portfolio = _portfolio(tmp_path, [])
    assert CHECK_PORTFOLIO_EMPTY in _codes(portfolio)
    assert portfolio["objects"] == []
    assert portfolio["verdict"]["any_releasable"] is False


# ─────────────────────────── демо, файлы, CLI, MCP ───────────────────────────
def test_demo_portfolio(tmp_path):
    config = json.loads((DEMO / "pack_portfolio" / "portfolio.json").read_text(encoding="utf-8"))
    portfolio = build_portfolio(config, base_dir=ROOT, out_dir=tmp_path / "pf", today=TODAY)
    totals, money = portfolio["totals"], portfolio["money"]
    assert portfolio["schema"] == SCHEMA
    assert (totals["objects"], totals["allowed"], totals["blocked"],
            totals["without_pack"]) == (3, 0, 2, 1)
    assert totals["acts"] == 5 and totals["acts_at_risk"] == 3
    assert money["at_risk_rub"] == 36_654_600.0
    assert money["unestimated_acts"] == 2
    assert verdict(portfolio)["summary"] == portfolio["verdict"]["summary"]
    assert {row["fix"] for row in money["by_fix"]} == {FIX_ERRORS_SIGN, FIX_MIXED}
    assert (tmp_path / "pf" / "portfolio_Портфель_ПТО-_ноябрь_2026.docx").exists()


def test_files_and_honest_words(tmp_path):
    from docx import Document
    from openpyxl import load_workbook

    config = json.loads((DEMO / "pack_portfolio" / "portfolio.json").read_text(encoding="utf-8"))
    portfolio = build_portfolio(config, base_dir=ROOT, out_dir=tmp_path / "pf", today=TODAY)
    files = write(portfolio, tmp_path / "pf2")
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Объекты", "Акты", "Разделы и деньги", "Блокеры"]
    document = Document(files["docx"])
    text = "\n".join(p.text for p in document.paragraphs)
    assert "Комплект объекта подготовил" not in text
    assert "Решение о предъявлении принял" in text
    markdown = to_markdown(portfolio).lower()
    for forbidden in ("всё под контролем", "риска нет", "документация в полном объёме"):
        assert forbidden not in markdown
    assert "не оценено" in markdown


def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_portfolio(tmp_path):
    run = _cli("pack", "--out", str(tmp_path / "pf"), "--base-dir", str(ROOT), "portfolio",
               "--config", str(DEMO / "pack_portfolio" / "portfolio.json"))
    assert run.returncode == 2, "портфель с блокерами — код 2"
    assert "объектов: 3" in run.stdout
    assert "ПОД РИСКОМ НЕПРЕДЪЯВЛЕНИЯ: 36 654 600.00 ₽" in run.stdout
    assert "не оценено: КС-2 №3" in run.stdout
    assert "комплекта нет" in run.stdout


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


def test_mcp_pack_portfolio_is_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "pack_portfolio")
    assert "config" in tool["inputSchema"]["properties"]
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "pack_portfolio")
    assert schema["version"] == "2.7.0"
    assert any("не оценено" in g for g in designed["guardrails"])


def test_mcp_portfolio_returns_money(tmp_path):
    result = _mcp_call("pack_portfolio",
                       {"config_path": str(DEMO / "pack_portfolio" / "portfolio.json"),
                        "base_dir": str(ROOT), "out_dir": str(tmp_path / "pf")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["kind"] == SCHEMA
    assert payload["ok"] is False
    assert payload["totals"]["objects"] == 3
    assert payload["money"]["at_risk_rub"] == 36_654_600.0
    assert Path(payload["files"]["xlsx"]).exists()
