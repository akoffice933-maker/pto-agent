# -*- coding: utf-8 -*-
"""Страж-тесты движения документации: выдача РД и согласование ИД.

Правила выдачи РД:

1. передача без шифра, даты или получателя в реестр не попадает;
2. работать можно только по редакции со штампом «В производство работ»;
3. передача без подписи получившего — не передача;
4. выдача заменённой редакции в производство — ошибка;
5. ссылка в работах на РД, которой нет в выдаче или она без штампа, — ошибка;
6. реестр подписывает человек с ФИО и ролью.

Правила согласования ИД:

7. комплект с открытыми замечаниями подписан быть не может;
8. подпись без ФИО и роли подписью не является;
9. просроченное замечание — ошибка, замечание без срока — предупреждение;
10. повторная передача без отметки об устранении — та же передача;
11. правка после подписи аннулирует подпись.
"""
import json
import subprocess
import sys
from pathlib import Path

from pto_agent.docflow import (CHECK_RD_DUPLICATE, CHECK_RD_NO_DATE, CHECK_RD_NO_RECEIPT,
                               CHECK_RD_NO_STAMP, CHECK_RD_SUPERSEDED, CHECK_RD_UNKNOWN_REV,
                               CHECK_SUB_NO_RECEIPT, CHECK_SUB_OVERDUE, CHECK_SUB_RESUBMIT_NO_FIX,
                               CHECK_SUB_REMARK_NO_DUE, CHECK_SUB_SIGNED_ANON,
                               CHECK_SUB_SIGNED_OPEN, CHECK_WORK_WITHOUT_RD, RD_IN_WORK,
                               RD_NOT_ISSUED, SCHEMA_RD, SCHEMA_SUBMITTAL, SUB_SIGNED,
                               SUB_SIGNED_WITH_REMARKS, build_rd_registry,
                               build_submittal_registry, sign, to_markdown, write)
from pto_agent.fingerprint import check_signature

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo" / "docflow"

REVISIONS = [{"doc": "КЖ-1", "revision": "изм. 0", "current": False},
             {"doc": "КЖ-1", "revision": "изм. 1", "current": True}]


def _tr(**overrides) -> dict:
    base = {"doc": "КЖ-1", "title": "Фундаментная плита", "revision": "изм. 1",
            "date": "2026-09-26", "recipient": "ООО «Монолит»", "copies": 2,
            "stamp": "в производство работ", "receipt": {"by": "Соколов И.И.", "at": "2026-09-26"}}
    base.update(overrides)
    return base


def _sub(**overrides) -> dict:
    base = {"title": "Комплект ИД: земляные работы", "act_ref": "АОСР-1",
            "sent_at": "2026-10-06", "recipient": "технадзор", "receipt": {"by": "Кузнецов В.В."},
            "remarks": [], "signature": {"by": "Кузнецов В.В.", "role": "инженер технадзора",
                                         "decision": "согласовано", "at": "2026-10-08"}}
    base.update(overrides)
    return base


def _codes(document) -> list[str]:
    return [c["code"] for c in document["checks"]]


# ─────────────────────────── выдача РД ───────────────────────────
def test_transmittal_without_required_fields_is_rejected():
    registry = build_rd_registry([_tr(), _tr(date=""), _tr(recipient=""), _tr(doc="")],
                                 revisions=REVISIONS, object_name="Школа")
    assert registry["totals"]["transmittals"] == 1
    assert registry["totals"]["rejected"] == 3
    assert CHECK_RD_NO_DATE in _codes(registry)
    assert all(r["why"] for r in registry["rejected"])


def test_stamp_in_production_is_required_to_work():
    registry = build_rd_registry([_tr(stamp="без штампа")], revisions=REVISIONS, object_name="Школа")
    assert CHECK_RD_NO_STAMP in _codes(registry)
    assert registry["documents"][0]["status"] == RD_NOT_ISSUED
    assert registry["can_work"]["allowed"] is False

    ok = build_rd_registry([_tr()], revisions=REVISIONS, object_name="Школа")
    assert ok["documents"][0]["status"] == RD_IN_WORK


def test_transmittal_needs_receipt_signature():
    registry = build_rd_registry([_tr(receipt={})], revisions=REVISIONS, object_name="Школа")
    assert CHECK_RD_NO_RECEIPT in _codes(registry)
    assert registry["totals"]["without_receipt"] == 1


def test_superseded_revision_in_production_is_error():
    registry = build_rd_registry([_tr(revision="изм. 0")], revisions=REVISIONS, object_name="Школа")
    assert CHECK_RD_SUPERSEDED in _codes(registry)
    assert registry["can_work"]["allowed"] is False


def test_unknown_revision_is_error():
    registry = build_rd_registry([_tr(revision="изм. 9")], revisions=REVISIONS, object_name="Школа")
    assert CHECK_RD_UNKNOWN_REV in _codes(registry)


def test_works_must_refer_to_issued_documentation():
    works = [{"work": "Фундаментная плита", "rd": "КЖ-1"},
             {"work": "Стены подвала", "rd": "КЖ-2"},
             {"work": "Кровля", "rd": "АР-1"}]
    registry = build_rd_registry(
        [_tr(), _tr(doc="АР-1", revision="изм. 0", stamp="без штампа")],
        revisions=REVISIONS + [{"doc": "АР-1", "revision": "изм. 0", "current": True}],
        works=works, object_name="Школа")
    messages = [c["message"] for c in registry["checks"] if c["code"] == CHECK_WORK_WITHOUT_RD]
    assert any("КЖ-2" in m for m in messages), "РД нет в выдаче"
    assert any("АР-1" in m for m in messages), "РД передана без штампа"
    assert not any("КЖ-1" in m for m in messages), "по выданной РД претензий нет"


def test_duplicate_transmittal_and_files(tmp_path):
    from openpyxl import load_workbook

    registry = build_rd_registry([_tr(), _tr()], revisions=REVISIONS, object_name="Школа",
                                 number="РД-1")
    assert CHECK_RD_DUPLICATE in _codes(registry)
    files = write(registry, tmp_path / "d")
    assert Path(files["json"]).name == "rd_РД-1.json"
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Передачи", "Документы", "Проверки"]
    markdown = to_markdown(registry).lower()
    for forbidden in ("документация выдана в полном объёме", "работы разрешены"):
        assert forbidden not in markdown
    assert "право работать по ней не подтверждено" in to_markdown(
        build_rd_registry([_tr()], revisions=REVISIONS, works=[{"work": "X", "rd": "НЕТ"}]))


def test_rd_registry_signature_needs_name_and_role():
    registry = build_rd_registry([_tr()], revisions=REVISIONS, object_name="Школа")
    for by, role in (("", "начальник ПТО"), ("Иванов И.И.", "")):
        refused = sign(registry, by=by, role=role)
        assert refused["sign_problems"]
    signed = sign(registry, by="Иванов И.И.", role="начальник ПТО")
    assert signed["status"] == "подписан"
    assert signed["can_work"]["allowed"] is True
    assert signed["schema"] == SCHEMA_RD
    assert check_signature(signed)["valid"] is True


def test_demo_rd_registry_keeps_findings():
    load = lambda name: json.loads((DEMO / name).read_text(encoding="utf-8"))
    tr, rev, works = load("transmittals.json"), load("revisions.json"), load("works_rd.json")
    registry = build_rd_registry(tr["transmittals"], revisions=rev["revisions"],
                                 works=works["works"], object_name=tr["object"], number="РД-1")
    codes = _codes(registry)
    assert CHECK_RD_SUPERSEDED in codes, "изм. 0 при действующей изм. 1"
    assert CHECK_RD_NO_STAMP in codes, "передача без штампа"
    assert CHECK_RD_NO_RECEIPT in codes, "передача без подписи получившего"
    assert CHECK_WORK_WITHOUT_RD in codes, "работа по невыданной РД"
    assert registry["can_work"]["allowed"] is False


# ─────────────────────────── согласование ИД ───────────────────────────
def test_signed_with_open_remarks_is_invalid():
    registry = build_submittal_registry(
        [_sub(remarks=[{"text": "нет протокола испытаний", "due": "2026-12-01",
                        "issued_at": "2026-10-09"}])],
        object_name="Школа", today=__import__("datetime").date(2026, 10, 20))
    assert CHECK_SUB_SIGNED_OPEN in _codes(registry)
    assert registry["submittals"][0]["status"] == SUB_SIGNED_WITH_REMARKS
    assert registry["submittals"][0]["signature"]["valid"] is False
    assert registry["can_close"]["allowed"] is False


def test_anonymous_signature_is_not_a_signature():
    registry = build_submittal_registry(
        [_sub(signature={"by": "", "role": "", "decision": "согласовано"})], object_name="Школа")
    assert CHECK_SUB_SIGNED_ANON in _codes(registry)
    assert registry["submittals"][0]["status"] != SUB_SIGNED


def test_overdue_remark_is_error_and_missing_due_is_warning():
    today = __import__("datetime").date(2026, 11, 6)
    overdue = build_submittal_registry(
        [_sub(signature=None, remarks=[{"text": "нет схемы", "due": "2026-10-14"}])],
        object_name="Школа", today=today)
    assert CHECK_SUB_OVERDUE in _codes(overdue)

    no_due = build_submittal_registry(
        [_sub(signature=None, remarks=[{"text": "нет схемы"}])], object_name="Школа", today=today)
    assert CHECK_SUB_REMARK_NO_DUE in _codes(no_due)
    assert CHECK_SUB_OVERDUE not in _codes(no_due)


def test_resubmission_without_fix_is_same_transmittal():
    registry = build_submittal_registry(
        [_sub(signature=None, resubmitted_at="2026-10-20",
              remarks=[{"text": "не приложен сертификат", "due": "2026-11-20"}])],
        object_name="Школа", today=__import__("datetime").date(2026, 10, 21))
    assert CHECK_SUB_RESUBMIT_NO_FIX in _codes(registry)

    fixed = build_submittal_registry(
        [_sub(signature=None, resubmitted_at="2026-10-20",
              remarks=[{"text": "не приложен сертификат", "due": "2026-11-20",
                        "fixed_at": "2026-10-19"}])],
        object_name="Школа", today=__import__("datetime").date(2026, 10, 21))
    assert CHECK_SUB_RESUBMIT_NO_FIX not in _codes(fixed)


def test_missing_receipt_and_rejected_submittal():
    registry = build_submittal_registry([_sub(receipt={}), _sub(title="", act_ref="АОСР-2")],
                                        object_name="Школа")
    assert CHECK_SUB_NO_RECEIPT in _codes(registry)
    assert registry["totals"]["rejected"] == 1


def test_clean_submittal_closes(tmp_path):
    registry = build_submittal_registry([_sub()], object_name="Школа", number="СГ-1")
    assert registry["totals"]["errors"] == 0
    assert registry["can_close"]["allowed"] is True
    assert registry["submittals"][0]["status"] == SUB_SIGNED
    files = write(registry, tmp_path / "d")
    assert Path(files["json"]).name == "submittals_СГ-1.json"
    assert registry["schema"] == SCHEMA_SUBMITTAL


def test_submittal_sign_refused_with_open_remarks_and_edit_invalidates():
    open_remarks = build_submittal_registry(
        [_sub(signature=None, remarks=[{"text": "нет схемы", "due": "2026-12-01"}])],
        object_name="Школа", today=__import__("datetime").date(2026, 10, 20))
    refused = sign(open_remarks, by="Иванов И.И.", role="начальник ПТО")
    assert refused["sign_problems"] and any("замечани" in p for p in refused["sign_problems"])

    clean = build_submittal_registry([_sub()], object_name="Школа")
    signed = sign(clean, by="Иванов И.И.", role="начальник ПТО")
    signed["submittals"][0]["remarks"] = [{"text": "добавлено после подписи"}]
    assert check_signature(signed)["changed_after_confirm"] is True


def test_demo_submittals_keep_findings():
    load = lambda name: json.loads((DEMO / name).read_text(encoding="utf-8"))
    sub = load("submittals.json")
    registry = build_submittal_registry(sub["submittals"], object_name=sub["object"],
                                        number="СГ-1")
    codes = _codes(registry)
    assert CHECK_SUB_SIGNED_OPEN in codes
    assert CHECK_SUB_OVERDUE in codes
    assert CHECK_SUB_RESUBMIT_NO_FIX in codes


# ─────────────────────────── CLI и MCP ───────────────────────────
def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_rd_and_sign(tmp_path):
    out = tmp_path / "d"
    built = _cli("docflow", "--out", str(out), "rd",
                 "--transmittals", str(DEMO / "transmittals.json"),
                 "--revisions", str(DEMO / "revisions.json"),
                 "--works", str(DEMO / "works_rd.json"),
                 "--object-name", "ЖК", "--number", "РД-1")
    assert built.returncode == 2, "в демо-реестре есть ошибки"
    assert "передач: 4" in built.stdout and "RD_REPLACED_IN_WORK" in built.stdout
    assert (out / "rd_РД-1.xlsx").exists()

    anonymous = _cli("docflow", "--out", str(out), "sign", "--registry",
                     str(out / "rd_РД-1.json"), "--by", " ", "--role", " ")
    assert anonymous.returncode == 2 and "ПОДПИСЬ НЕ ПРИНЯТА" in anonymous.stdout


def test_cli_submittals(tmp_path):
    out = tmp_path / "d"
    built = _cli("docflow", "--out", str(out), "submittals",
                 "--submittals", str(DEMO / "submittals.json"), "--number", "СГ-1")
    assert built.returncode == 2
    assert "SIGNED_WITH_OPEN_REMARKS" in built.stdout


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


def test_mcp_docflow_tools_are_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "docflow")
    assert set(tool["inputSchema"]["properties"]["action"]["enum"]) == {"rd", "submittals", "sign"}
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "docflow")
    assert any("В производство работ" in g for g in designed["guardrails"])


def test_mcp_rd_returns_findings(tmp_path):
    result = _mcp_call("docflow", {"action": "rd",
                                   "transmittals": [_tr(revision="изм. 0")],
                                   "revisions": REVISIONS,
                                   "works": [{"work": "Плита", "rd": "КЖ-9"}],
                                   "object_name": "Школа", "number": "РД-9",
                                   "out_dir": str(tmp_path / "d")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    codes = [c["code"] for c in payload["checks"]]
    assert CHECK_RD_SUPERSEDED in codes and CHECK_WORK_WITHOUT_RD in codes
    assert Path(payload["files"]["xlsx"]).exists()
