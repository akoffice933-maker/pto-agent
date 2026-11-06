# -*- coding: utf-8 -*-
"""Страж-тесты переписки и внешних участников: письма, лаборатории, геодезисты, дни качества.

Правила писем:

1. письмо без номера, даты или адресата в реестр не попадает;
2. письмо без основания (пункт договора, норма, номер письма) не предъявляется;
3. письмо без ФИО и роли подписавшего не считается отправленным;
4. требуешь ответа — ставь срок; срок прошёл и ответа нет — ошибка, а не «ждём»;
5. ответ без номера и даты — на него нельзя сослаться;
6. два письма под одним номером видны.

Правила лабораторий и геодезистов:

7. организация без аттестата аккредитации результат не даёт;
8. аттестат, истёкший до даты работы, силу результата не подтверждает;
9. область аккредитации не указана — проверить нечем;
10. результат без привязки к партии/работе в комплект не ложится;
11. заявка без результата: в срок — предупреждение, после срока — ошибка.

Правила авторских дней и дней качества:

12. мероприятие без участников не проводится;
13. мероприятие без протокола — замечания не зафиксированы;
14. замечание без ответственного не исполняется, без срока — просрочку не увидеть;
15. просроченное замечание — ошибка.

Общее: реестр подписывает человек с ФИО и ролью; правка после подписи аннулирует подпись.
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from pto_agent.correspondence import (CHECK_ANSWER_MISSING, CHECK_ANSWER_NO_REF,
                                      CHECK_ANSWER_OVERDUE, CHECK_EVENT_NO_PARTICIPANTS,
                                      CHECK_EVENT_NO_PROTOCOL, CHECK_EVENT_REMARK_NO_DUE,
                                      CHECK_EVENT_REMARK_NO_RESPONSIBLE,
                                      CHECK_EVENT_REMARK_OVERDUE, CHECK_LETTER_DUPLICATE,
                                      CHECK_LETTER_NO_ADDRESSEE, CHECK_LETTER_NO_BASIS,
                                      CHECK_LETTER_NO_SIGNER, CHECK_ORG_ACCREDITATION_EXPIRED,
                                      CHECK_ORG_NO_ACCREDITATION, CHECK_ORG_SCOPE_MISSING,
                                      CHECK_REQUEST_NO_RESULT, CHECK_REQUEST_OVERDUE,
                                      CHECK_RESULT_UNLINKED, SCHEMA_QUALITY_DAYS,
                                      build_involvement_registry, build_letter_registry,
                                      build_quality_days, sign, to_markdown, write)
from pto_agent.fingerprint import check_signature

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo" / "correspondence"
TODAY = date(2026, 11, 6)


def _letter(**overrides) -> dict:
    base = {"number": "231", "date": "2026-10-02", "kind": "запрос",
            "to": {"org": "ООО «Монолит»"}, "subject": "Документы качества на арматуру",
            "basis": {"doc": "договор Д-142/2026", "clause": "прил. 3, п. 4.12"},
            "requires_answer": True, "due_date": "2026-11-20",
            "signed_by": {"by": "Иванов И.И.", "role": "начальник ПТО"}}
    base.update(overrides)
    return base


def _record(**overrides) -> dict:
    base = {"kind": "лаборатория", "organization": "Лаборатория «СтройТест»",
            "attestation": {"number": "RA.RU.21АБ12", "scope": "испытания бетона",
                            "valid_until": "2027-04-01"},
            "request": {"number": "З-14", "date": "2026-10-02", "scope": "испытание бетона",
                        "due": "2026-11-20"},
            "result": {"number": "ПИ-14", "date": "2026-10-05", "linked_to": "партия 14"}}
    base.update(overrides)
    return base


def _event(**overrides) -> dict:
    base = {"kind": "авторский надзор", "date": "2026-10-02",
            "participants": [{"org": "ООО «Проект-С»", "by": "Орлова М.С.",
                              "role": "главный специалист"}],
            "protocol": {"number": "АН-4", "date": "2026-10-02"},
            "remarks": [{"text": "уточнить армирование", "responsible": "ООО «Монолит»",
                         "due": "2026-11-20"}]}
    base.update(overrides)
    return base


def _codes(document) -> list[str]:
    return [c["code"] for c in document["checks"]]


# ─────────────────────────── письма ───────────────────────────
def test_letter_without_addressee_is_rejected():
    registry = build_letter_registry([_letter(), _letter(to={}), _letter(date="")],
                                     object_name="Школа", today=TODAY)
    assert registry["totals"]["letters"] == 1
    assert registry["totals"]["rejected"] == 2
    assert CHECK_LETTER_NO_ADDRESSEE in _codes(registry)


def test_letter_without_basis_is_not_a_claim():
    registry = build_letter_registry([_letter(basis="")], object_name="Школа", today=TODAY)
    assert CHECK_LETTER_NO_BASIS in _codes(registry)
    assert registry["totals"]["without_basis"] == 1


def test_letter_without_signer_is_not_sent():
    registry = build_letter_registry([_letter(signed_by={"by": "", "role": ""})],
                                     object_name="Школа", today=TODAY)
    assert CHECK_LETTER_NO_SIGNER in _codes(registry)


def test_answer_deadline_missing_and_overdue():
    overdue = build_letter_registry([_letter(due_date="2026-10-09")], object_name="Школа",
                                    today=TODAY)
    assert CHECK_ANSWER_OVERDUE in _codes(overdue)
    assert overdue["open_requests"][0]["overdue"] is True

    pending = build_letter_registry([_letter(due_date="2026-12-01")], object_name="Школа",
                                    today=TODAY)
    assert CHECK_ANSWER_MISSING in _codes(pending)
    assert CHECK_ANSWER_OVERDUE not in _codes(pending)

    answered = build_letter_registry(
        [_letter(due_date="2026-12-01", answer={"number": "412", "date": "2026-10-26"})],
        object_name="Школа", today=TODAY)
    assert not [c for c in answered["checks"] if c["level"] == "error"]


def test_answer_without_reference_is_flagged():
    registry = build_letter_registry(
        [_letter(answer={"summary": "переоформлено"})], object_name="Школа", today=TODAY)
    assert CHECK_ANSWER_NO_REF in _codes(registry)


def test_duplicate_letter_number():
    registry = build_letter_registry([_letter(), _letter(subject="другое")],
                                     object_name="Школа", today=TODAY)
    assert CHECK_LETTER_DUPLICATE in _codes(registry)


# ─────────────────────────── лаборатории и геодезисты ───────────────────────────
def test_organization_without_accreditation_gives_no_result():
    registry = build_involvement_registry([_record(attestation={})], object_name="Школа",
                                          today=TODAY)
    assert CHECK_ORG_NO_ACCREDITATION in _codes(registry)


def test_expired_accreditation_on_work_date_is_error():
    registry = build_involvement_registry(
        [_record(attestation={"number": "RA.RU.1", "scope": "бетон",
                              "valid_until": "2026-09-01"})],
        object_name="Школа", today=TODAY)
    assert CHECK_ORG_ACCREDITATION_EXPIRED in _codes(registry)
    assert registry["records"][0]["result"]["valid"] is False

    valid = build_involvement_registry([_record()], object_name="Школа", today=TODAY)
    assert registry["totals"]["expired_accreditation"] == 1
    assert valid["records"][0]["result"]["valid"] is True


def test_scope_and_result_linkage():
    registry = build_involvement_registry(
        [_record(attestation={"number": "RA.RU.1", "valid_until": "2027-01-01"},
                 result={"number": "ИС-7", "date": "2026-10-11"})],
        object_name="Школа", today=TODAY)
    codes = _codes(registry)
    assert CHECK_ORG_SCOPE_MISSING in codes
    assert CHECK_RESULT_UNLINKED in codes, "результат без привязки в комплект не ложится"


def test_request_without_result_in_time_and_overdue():
    pending = build_involvement_registry([_record(result={}, request={
        "number": "Г-11", "date": "2026-10-20", "due": "2026-12-01"})],
        object_name="Школа", today=TODAY)
    assert CHECK_REQUEST_NO_RESULT in _codes(pending)
    assert pending["records"][0]["status"] == "в работе"

    overdue = build_involvement_registry([_record(result={}, request={
        "number": "Г-11", "date": "2026-10-20", "due": "2026-10-27"})],
        object_name="Школа", today=TODAY)
    assert CHECK_REQUEST_OVERDUE in _codes(overdue)
    assert overdue["can_close"]["allowed"] is False


def test_involvement_signature(tmp_path):
    registry = build_involvement_registry([_record()], object_name="Школа", number="ЛАБ-1")
    assert registry["can_close"]["allowed"] is True
    refused = sign(registry, by="", role="")
    assert refused["sign_problems"]
    signed = sign(registry, by="Иванов И.И.", role="начальник ПТО")
    assert signed["status"] == "подписан" and check_signature(signed)["valid"] is True
    files = write(signed, tmp_path / "c")
    assert Path(files["json"]).name == "involvement_ЛАБ-1.json"


# ─────────────────────────── дни качества ───────────────────────────
def test_event_without_participants_is_not_held():
    registry = build_quality_days([_event(), _event(participants=[])], object_name="Школа",
                                  today=TODAY)
    assert CHECK_EVENT_NO_PARTICIPANTS in _codes(registry)
    assert registry["totals"]["rejected"] == 1


def test_event_without_protocol_is_flagged():
    registry = build_quality_days([_event(protocol={})], object_name="Школа", today=TODAY)
    assert CHECK_EVENT_NO_PROTOCOL in _codes(registry)


def test_remark_needs_responsible_due_and_closing():
    registry = build_quality_days(
        [_event(remarks=[{"text": "накрыть бетон", "responsible": "", "due": "2026-10-20"}])],
        object_name="Школа", today=TODAY)
    codes = _codes(registry)
    assert CHECK_EVENT_REMARK_NO_RESPONSIBLE in codes
    assert CHECK_EVENT_REMARK_OVERDUE in codes, "срок прошёл, замечание не закрыто"

    no_due = build_quality_days(
        [_event(remarks=[{"text": "накрыть бетон", "responsible": "ООО «Монолит»"}])],
        object_name="Школа", today=TODAY)
    assert CHECK_EVENT_REMARK_NO_DUE in _codes(no_due)

    closed = build_quality_days(
        [_event(remarks=[{"text": "накрыть бетон", "responsible": "ООО «Монолит»",
                          "due": "2026-10-20", "closed_at": "2026-10-18"}])],
        object_name="Школа", today=TODAY)
    assert not [c for c in closed["checks"] if c["level"] == "error"]
    assert closed["events"][0]["status"] == "замечания закрыты"


def test_quality_days_files_and_words(tmp_path):
    from openpyxl import load_workbook

    registry = build_quality_days([_event()], object_name="Школа", number="ДК-1")
    assert registry["schema"] == SCHEMA_QUALITY_DAYS
    files = write(registry, tmp_path / "c")
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Мероприятия", "Замечания", "Проверки"]
    markdown = to_markdown(registry).lower()
    assert "авторский надзор" in markdown
    for forbidden in ("замечаний нет", "мероприятие проведено в полном объёме"):
        assert forbidden not in markdown


def test_demo_correspondence_keeps_findings():
    load = lambda name: json.loads((DEMO / name).read_text(encoding="utf-8"))
    letters, involvement, days = (load("letters.json"), load("involvement.json"),
                                  load("quality_days.json"))
    letter_registry = build_letter_registry(letters["letters"], object_name=letters["object"],
                                            number="ПИС-1", today=TODAY)
    assert CHECK_LETTER_NO_BASIS in _codes(letter_registry), "письмо 232 без основания"
    assert CHECK_ANSWER_OVERDUE in _codes(letter_registry), "ответ на 231 просрочен"
    assert CHECK_LETTER_NO_SIGNER in _codes(letter_registry), "письмо 241 без подписи"

    lab_registry = build_involvement_registry(involvement["records"],
                                              object_name=involvement["object"],
                                              number="ЛАБ-1", today=TODAY)
    assert CHECK_ORG_ACCREDITATION_EXPIRED in _codes(lab_registry)
    assert CHECK_RESULT_UNLINKED in _codes(lab_registry)

    days_registry = build_quality_days(days["events"], object_name=days["object"],
                                       number="ДК-1", today=TODAY)
    assert CHECK_EVENT_REMARK_OVERDUE in _codes(days_registry)
    assert CHECK_EVENT_REMARK_NO_RESPONSIBLE in _codes(days_registry)
    assert CHECK_EVENT_NO_PROTOCOL in _codes(days_registry)


# ─────────────────────────── CLI и MCP ───────────────────────────
def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_letters_and_refused_signature(tmp_path):
    out = tmp_path / "c"
    built = _cli("correspondence", "--out", str(out), "letters",
                 "--source", str(DEMO / "letters.json"), "--number", "ПИС-1")
    assert built.returncode == 2
    assert built.stdout.startswith("Реестр писем и запросов"), built.stdout[:80]
    assert "писем: 6" in built.stdout and "ждём: 2" in built.stdout
    assert "LETTER_NO_BASIS" in built.stdout and "ANSWER_OVERDUE" in built.stdout
    anonymous = _cli("correspondence", "--out", str(out), "sign", "--registry",
                     str(out / "letters_ПИС-1.json"), "--by", " ", "--role", " ")
    assert anonymous.returncode == 2 and "ПОДПИСЬ НЕ ПРИНЯТА" in anonymous.stdout


def test_cli_involvement_and_days(tmp_path):
    out = tmp_path / "c"
    lab = _cli("correspondence", "--out", str(out), "involvement",
               "--source", str(DEMO / "involvement.json"), "--number", "ЛАБ-1")
    assert lab.returncode == 2 and "ACCREDITATION_EXPIRED" in lab.stdout
    days = _cli("correspondence", "--out", str(out), "days",
                "--source", str(DEMO / "quality_days.json"), "--number", "ДК-1")
    assert days.returncode == 2 and "EVENT_REMARK_OVERDUE" in days.stdout


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


def test_mcp_correspondence_tool_is_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "correspondence")
    assert set(tool["inputSchema"]["properties"]["action"]["enum"]) == {
        "letters", "involvement", "days", "sign"}
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "correspondence")
    assert any("аккредитаци" in g for g in designed["guardrails"])


def test_mcp_letters_and_labs(tmp_path):
    letters = _mcp_call("correspondence", {"action": "letters",
                                           "letters": [_letter(basis="")],
                                           "object_name": "Школа", "number": "П-1",
                                           "out_dir": str(tmp_path / "c")})
    payload = json.loads(letters["content"][0]["text"])
    assert payload["ok"] is False
    assert CHECK_LETTER_NO_BASIS in [c["code"] for c in payload["checks"]]
    assert Path(payload["files"]["xlsx"]).exists()

    labs = _mcp_call("correspondence", {"action": "involvement",
                                        "records": [_record(attestation={})],
                                        "object_name": "Школа", "number": "Л-1",
                                        "out_dir": str(tmp_path / "c")})
    lab_payload = json.loads(labs["content"][0]["text"])
    assert CHECK_ORG_NO_ACCREDITATION in [c["code"] for c in lab_payload["checks"]]
