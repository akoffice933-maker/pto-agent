# -*- coding: utf-8 -*-
"""Страж-тесты комплекта объекта: один вердикт на все разделы.

Правила:

1. отсутствующий раздел — «нет данных», а не «пройдено»; он блокирует сдачу;
2. исключить раздел можно только с основанием — «enabled: false без причины» блокирует сдачу;
3. ошибка в любом разделе блокирует сдачу комплекта;
4. не подписанный человеком раздел блокирует сдачу (агент не подписывает);
5. готовый подписанный реестр читается по ссылке `registry`, подпись проверяется, а не
   пересобирается;
6. разделы сводного отчёта — те же проверки, что у отдельных реестров (один и тот же код
   находки), поэтому сводка не может быть «чище» разделов;
7. пустой раздел (собран, но записей нет) — находка;
8. файлы: JSON, Markdown, XLSX «Разделы»/«Блокеры»/«Находки», DOCX-опись с местами подписей;
9. ни в одном файле нет «комплект готов к сдаче» без оснований.
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from pto_agent import correspondence as corr
from pto_agent import docflow, journals, quality_docs
from pto_agent.pack import (CHECK_DISABLED_NO_REASON, CHECK_SECTION_ERRORS, CHECK_SECTION_MISSING,
                            CHECK_SECTION_UNSIGNED, SCHEMA, SECTION_DISABLED, SECTION_ERRORS,
                            SECTION_MISSING, SECTION_OK, build_pack, can_submit, to_markdown,
                            write)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
TODAY = date(2026, 11, 6)
BY, ROLE = "Иванов И.И.", "начальник ПТО"


def _cfg(**sections) -> dict:
    return {"object_name": "Школа", "number": "К-1", "sections": sections}


def _journal_section(**extra) -> dict:
    section = {"entries": [{"date": "2026-10-01", "shift": "1",
                            "work": "Разработка грунта экскаватором в котловане",
                            "unit": "м3", "qty": 100, "executor": "ООО «Земстрой»",
                            "weather": {"temp_c": 8}}]}
    section.update(extra)
    return section


def _codes(pack) -> list[str]:
    return [c["code"] for c in pack["checks"]]


def _signed_sections(tmp_path: Path) -> dict:
    """Собрать и подписать по одному чистому реестру каждого раздела — как это делает человек."""
    out = tmp_path / "built"
    out.mkdir(parents=True, exist_ok=True)
    work = journals.build_work_journal(
        [{"date": "2026-10-01", "shift": "1", "work": "Разработка грунта экскаватором",
          "unit": "м3", "qty": 100, "executor": "ООО «Земстрой»", "weather": {"temp_c": 8}}],
        object_name="Школа", number="ОЖР-1", today=TODAY)
    work = journals.sign(work, by=BY, role=ROLE, today=TODAY)
    work_file = journals.write(work, out)["json"]

    verification = journals.build_verification_journal(
        [{"date": "2026-10-01", "material": "Бетон B25", "unit": "м3", "qty": 60,
          "batch": "партия 1", "decision": "допущен", "inspected_by": BY, "role": "инженер ПТО",
          "quality_doc": {"kind": "паспорт качества", "number": "П-1",
                          "valid_until": "2026-12-31"}}],
        object_name="Школа", number="ВК-1", today=TODAY)
    verification = journals.sign(verification, by=BY, role=ROLE, today=TODAY)
    verification_file = journals.write(verification, out)["json"]

    quality = quality_docs.build_registry(
        [{"kind": "паспорт качества", "number": "П-1", "issue_date": "2026-09-28",
          "material": "Бетон B25", "batch": "партия 1", "standard": "ГОСТ 7473-2010",
          "file": "quality/b.pdf", "attested_by": {"by": BY, "role": "инженер ПТО"}}],
        object_name="Школа", number="ДК-1",
        requirements=[{"material": "Бетон B25", "required": ["паспорт качества"],
                       "basis": "договор, прил. 3"}],
        usage=[{"material": "Бетон B25", "batch": "партия 1", "qty": 60, "unit": "м3",
                "applied_from": "2026-10-04"}],
        verification=[{"material": "Бетон B25", "batch": "партия 1", "decision": "допущен",
                       "quality_doc": {"kind": "паспорт качества", "number": "П-1"}}],
        today=TODAY)
    quality = quality_docs.sign(quality, by=BY, role=ROLE, today=TODAY)
    quality_file = quality_docs.write(quality, out)["json"]

    rd = docflow.build_rd_registry(
        [{"doc": "КЖ-1", "revision": "изм. 1", "date": "2026-09-26", "recipient": "ООО «М»",
          "stamp": "в производство работ", "receipt": {"by": "Соколов И.И."}}],
        revisions=[{"doc": "КЖ-1", "revision": "изм. 1", "current": True}],
        works=[{"work": "Плита", "rd": "КЖ-1"}], object_name="Школа", number="РД-1", today=TODAY)
    rd = docflow.sign(rd, by=BY, role=ROLE, today=TODAY)
    rd_file = docflow.write(rd, out)["json"]

    submittals = docflow.build_submittal_registry(
        [{"title": "Комплект ИД: земляные работы", "act_ref": "АОСР-1", "sent_at": "2026-10-06",
          "recipient": "технадзор", "receipt": {"by": "Кузнецов В.В."}, "remarks": [],
          "signature": {"by": "Кузнецов В.В.", "role": "инженер технадзора",
                        "decision": "согласовано", "at": "2026-10-08"}}],
        object_name="Школа", number="СГ-1", today=TODAY)
    submittals = docflow.sign(submittals, by=BY, role=ROLE, today=TODAY)
    submittals_file = docflow.write(submittals, out)["json"]

    letters = corr.build_letter_registry(
        [{"number": "1", "date": "2026-10-02", "kind": "запрос", "to": {"org": "ООО «М»"},
          "subject": "документы", "basis": {"doc": "договор", "clause": "п. 4.12"},
          "signed_by": {"by": BY, "role": ROLE}}], object_name="Школа", number="ПИС-1", today=TODAY)
    letters = corr.sign(letters, by=BY, role=ROLE, today=TODAY)
    letters_file = corr.write(letters, out)["json"]

    involvement = corr.build_involvement_registry(
        [{"kind": "лаборатория", "organization": "Лаборатория «СтройТест»",
          "attestation": {"number": "RA.RU.1", "scope": "бетон", "valid_until": "2027-04-01"},
          "request": {"number": "З-1", "date": "2026-10-02", "due": "2026-10-09"},
          "result": {"number": "ПИ-1", "date": "2026-10-05", "linked_to": "партия 1"}}],
        object_name="Школа", number="ЛАБ-1", today=TODAY)
    involvement = corr.sign(involvement, by=BY, role=ROLE, today=TODAY)
    involvement_file = corr.write(involvement, out)["json"]

    days = corr.build_quality_days(
        [{"kind": "день качества", "date": "2026-10-09",
          "participants": [{"org": "ООО «М»", "by": "Соколов И.И.", "role": "прораб"}],
          "protocol": {"number": "ДК-2", "date": "2026-10-09"},
          "remarks": [{"text": "накрыть бетон", "responsible": "ООО «М»", "due": "2026-10-20",
                       "closed_at": "2026-10-18"}]}],
        object_name="Школа", number="ДК-1", today=TODAY)
    days = corr.sign(days, by=BY, role=ROLE, today=TODAY)
    days_file = corr.write(days, out)["json"]

    return {"journals_work": {"registry": work_file},
            "journals_verification": {"registry": verification_file},
            "quality_docs": {"registry": quality_file},
            "rd": {"registry": rd_file},
            "submittals": {"registry": submittals_file},
            "letters": {"registry": letters_file},
            "involvement": {"registry": involvement_file},
            "quality_days": {"registry": days_file},
            "checklists": {"enabled": False, "reason": "по этому акту чек-листы не выпускаются"}}


# ─────────────────────────── вердикт ───────────────────────────
def test_missing_section_is_not_passed(tmp_path):
    pack = build_pack(_cfg(journals_work=_journal_section()), base_dir=tmp_path,
                      out_dir=tmp_path / "p", today=TODAY)
    statuses = {s["kind"]: s["status"] for s in pack["sections"]}
    assert statuses["journals_work"] != SECTION_MISSING
    assert statuses["letters"] == SECTION_MISSING
    assert pack["totals"]["missing"] >= 7
    assert CHECK_SECTION_MISSING in _codes(pack)
    assert pack["can_submit"]["allowed"] is False
    assert any("без данных" in r for r in pack["can_submit"]["reasons"])


def test_disabled_section_requires_reason(tmp_path):
    cfg = _cfg(journals_work={"enabled": False})
    pack = build_pack(cfg, base_dir=tmp_path, out_dir=tmp_path / "p", today=TODAY)
    statuses = {s["kind"]: s["status"] for s in pack["sections"]}
    assert statuses["journals_work"] == SECTION_DISABLED
    assert CHECK_DISABLED_NO_REASON in _codes(pack)

    with_reason = _cfg(journals_work={"enabled": False, "reason": "журнал ведёт субподрядчик"})
    pack2 = build_pack(with_reason, base_dir=tmp_path, out_dir=tmp_path / "p2", today=TODAY)
    assert CHECK_DISABLED_NO_REASON not in _codes(pack2)
    assert any("исключено без причины" not in r for r in pack2["can_submit"]["reasons"])


def test_errors_in_section_block_submission(tmp_path):
    section = {"documents": str(DEMO / "quality_docs" / "documents.json"),
               "requirements": str(DEMO / "quality_docs" / "requirements.json"),
               "usage": str(DEMO / "quality_docs" / "usage.json")}
    pack = build_pack({"object_name": "Школа", "sections": {"quality_docs": section}},
                      base_dir=ROOT, out_dir=tmp_path / "p", today=TODAY)
    quality = next(s for s in pack["sections"] if s["kind"] == "quality_docs")
    assert quality["status"] == SECTION_ERRORS and quality["errors"]
    assert "DOC_EXPIRED" in [e["code"] for e in quality["errors"]]
    assert CHECK_SECTION_ERRORS in _codes(pack)
    assert pack["can_submit"]["allowed"] is False
    assert any("с ошибками" in r for r in pack["can_submit"]["reasons"])


def test_unsigned_section_blocks_submission(tmp_path):
    pack = build_pack(_cfg(journals_work=_journal_section()), base_dir=tmp_path,
                      out_dir=tmp_path / "p", today=TODAY)
    work = next(s for s in pack["sections"] if s["kind"] == "journals_work")
    assert work["signed"] is False
    assert CHECK_SECTION_UNSIGNED in _codes(pack)
    assert any("не подписано" in r for r in pack["can_submit"]["reasons"])


def test_signed_complete_pack_can_be_submitted(tmp_path):
    sections = _signed_sections(tmp_path)
    pack = build_pack({"object_name": "Школа", "number": "К-1", "sections": sections},
                      base_dir=tmp_path, out_dir=tmp_path / "p", today=TODAY)
    assert pack["totals"]["with_errors"] == 0, [s for s in pack["sections"] if s["errors"]]
    assert pack["totals"]["missing"] == 0
    assert pack["totals"]["signed"] == 8
    assert pack["can_submit"]["allowed"] is True, pack["can_submit"]["reasons"]
    assert pack["schema"] == SCHEMA


def test_signed_registry_is_verified_not_rebuilt(tmp_path):
    sections = _signed_sections(tmp_path)
    path = Path(sections["journals_work"]["registry"])
    document = json.loads(path.read_text(encoding="utf-8"))
    document["entries"][0]["qty"] = 999            # подпись должна «отвалиться»
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    pack = build_pack({"object_name": "Школа", "sections": sections}, base_dir=tmp_path,
                      out_dir=tmp_path / "p", today=TODAY)
    work = next(s for s in pack["sections"] if s["kind"] == "journals_work")
    assert work["signed"] is False, "правка после подписи делает подпись недействительной"


def test_empty_section_is_a_finding(tmp_path):
    pack = build_pack(
        {"object_name": "Школа", "sections": {
            "letters": {"letters": [{"number": "1", "date": "2026-10-01", "to": {"org": "X"},
                                     "subject": "s"}]}}},
        base_dir=tmp_path, out_dir=tmp_path / "p", today=TODAY)
    letters = next(s for s in pack["sections"] if s["kind"] == "letters")
    assert letters["errors"], "письмо без основания и подписи — находка"
    assert letters["counts"]["errors"] >= 2


def test_section_findings_match_single_registry(tmp_path):
    section = {"documents": str(DEMO / "quality_docs" / "documents.json"),
               "requirements": str(DEMO / "quality_docs" / "requirements.json"),
               "usage": str(DEMO / "quality_docs" / "usage.json")}
    pack = build_pack({"object_name": "Школа", "sections": {"quality_docs": section}},
                      base_dir=ROOT, out_dir=tmp_path / "p", today=TODAY)
    alone = quality_docs.build_registry(
        json.loads((DEMO / "quality_docs" / "documents.json").read_text(encoding="utf-8"))["documents"],
        object_name="Школа", number="ДК-1",
        requirements=json.loads((DEMO / "quality_docs" / "requirements.json")
                                .read_text(encoding="utf-8"))["requirements"],
        usage=json.loads((DEMO / "quality_docs" / "usage.json").read_text(encoding="utf-8"))["usage"],
        today=TODAY)
    quality = next(s for s in pack["sections"] if s["kind"] == "quality_docs")
    assert [e["code"] for e in quality["errors"]] == [c["code"] for c in alone["checks"]
                                                      if c["level"] == "error"], \
        "сводный раздел не может быть чище отдельного реестра"


def test_files_and_honest_words(tmp_path):
    from openpyxl import load_workbook

    pack = build_pack({"object_name": "Школа", "number": "К-1",
                       "sections": {"journals_work": _journal_section()}},
                      base_dir=tmp_path, out_dir=tmp_path / "p", today=TODAY)
    files = write(pack, tmp_path / "p2")
    assert Path(files["json"]).name == "pack_К-1.json"
    assert Path(files["docx"]).exists()
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Разделы", "Блокеры", "Находки"]
    markdown = to_markdown(pack).lower()
    for forbidden in ("комплект готов к сдаче", "документация в полном объёме", "замечаний нет"):
        assert forbidden not in markdown
    assert "нет данных" in markdown


def test_demo_pack_reports_all_blockers(tmp_path):
    config = json.loads((DEMO / "pack" / "pack.json").read_text(encoding="utf-8"))
    pack = build_pack(config, base_dir=ROOT, out_dir=tmp_path / "p", today=TODAY)
    assert pack["totals"]["sections"] == 9
    assert pack["totals"]["disabled"] == 1, "чек-листы исключены с причиной — это видно"
    assert pack["totals"]["findings"] >= 25
    assert pack["can_submit"]["allowed"] is False
    assert can_submit(pack)["reasons"] == pack["can_submit"]["reasons"]
    titles = {s["kind"] for s in pack["sections"]}
    assert {"journals_work", "journals_verification", "quality_docs", "rd", "submittals",
            "letters", "involvement", "quality_days", "checklists"} <= titles
    assert (tmp_path / "p" / "pack_К-38.docx").exists()


# ─────────────────────────── CLI и MCP ───────────────────────────
def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_pack(tmp_path):
    run = _cli("pack", "--out", str(tmp_path / "p"), "--base-dir", str(ROOT), "build",
               "--config", str(DEMO / "pack" / "pack.json"))
    assert run.returncode == 2, "демо-комплект предъявлять нельзя"
    assert "разделов: 9" in run.stdout
    assert "предъявлять нельзя" in run.stdout
    assert "подписать (ФИО, роль, дата)" in run.stdout
    assert (tmp_path / "p" / "pack_К-38.xlsx").exists()


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


def test_mcp_object_pack_tool_is_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "object_pack")
    assert "config" in tool["inputSchema"]["properties"]
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "object_pack")
    assert any("нет данных" in g for g in designed["guardrails"])


def test_mcp_pack_returns_verdict(tmp_path):
    result = _mcp_call("object_pack", {"config_path": str(DEMO / "pack" / "pack.json"),
                                       "base_dir": str(ROOT),
                                       "out_dir": str(tmp_path / "p")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["kind"] == SCHEMA
    assert payload["ok"] is False
    assert payload["totals"]["sections"] == 9
    assert payload["can_submit"]["reasons"]
    assert Path(payload["files"]["docx"]).exists()
