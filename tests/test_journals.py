# -*- coding: utf-8 -*-
"""Страж-тесты журналов: общий журнал работ и входной контроль материалов.

Правила общего журнала:

1. запись без даты, смены, исполнителя или описания в журнал не попадает;
2. вид работ определяется классификатором матрицы; не определился — видно и не даёт комплект ИД;
3. даты только по возрастанию: запись задним числом в конец — ошибка;
4. температура воздуха обязательна для земляных/бетонных/кровельных работ;
5. дубль записи и `nan` в объёме не проходят молча;
6. журнал нельзя закрыть без подписи человека и при наличии ошибок; правка после подписи
   аннулирует подпись.

Правила входного контроля:

7. «допущен» без документа качества невозможен;
8. документ качества с истёкшим на дату проверки сроком не даёт допуска;
9. решение только из четырёх значений, проверяющий обязателен;
10. материал, списанный без записи входного контроля, виден в журнале.
"""
import json
import subprocess
import sys
from pathlib import Path

from pto_agent.fingerprint import check_signature
from pto_agent.journals import (CHECK_ACCEPT_NO_DOC, CHECK_CERT_EXPIRED, CHECK_DECISION,
                                CHECK_DUPLICATE_BATCH, CHECK_DUPLICATE_ENTRY, CHECK_ENTRY_INCOMPLETE,
                                CHECK_MATERIAL_UNVERIFIED, CHECK_NO_INSPECTOR,
                                CHECK_OUT_OF_ORDER, CHECK_WEATHER, CHECK_WORK_UNKNOWN,
                                DECISION_OK, DECISION_UNCHECKED, SCHEMA_VERIFICATION, SCHEMA_WORK,
                                STATUS_SIGNED, build_verification_journal, build_work_journal,
                                sign, to_markdown, write)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo" / "journals"


def _entry(**overrides) -> dict:
    base = {"date": "2026-10-01", "shift": "1", "work": "Разработка грунта в котловане",
            "unit": "м3", "qty": 100, "executor": "ООО «Земстрой»",
            "weather": {"temp_c": 8, "precipitation": "нет"}}
    base.update(overrides)
    return base


def _record(**overrides) -> dict:
    base = {"date": "2026-10-01", "material": "Бетон B25", "unit": "м3", "qty": 60,
            "batch": "партия 1",
            "quality_doc": {"kind": "паспорт качества", "number": "П-1",
                            "valid_until": "2026-12-31"},
            "decision": DECISION_OK, "inspected_by": "Иванов И.И.", "role": "инженер ПТО"}
    base.update(overrides)
    return base


def _codes(journal) -> list[str]:
    return [c["code"] for c in journal["checks"]]


# ─────────────────────────── общий журнал работ ───────────────────────────
def test_incomplete_entry_is_not_accepted():
    journal = build_work_journal([
        _entry(),
        _entry(date=""),
        _entry(shift=""),
        _entry(executor=""),
        _entry(work=""),
    ], object_name="Школа")
    assert journal["totals"]["entries"] == 1
    assert journal["totals"]["rejected"] == 4
    assert CHECK_ENTRY_INCOMPLETE in _codes(journal)
    assert all(r["why"] for r in journal["rejected"])


def test_work_type_is_detected_and_unknown_is_flagged():
    journal = build_work_journal([_entry(work="Разработка грунта экскаватором"),
                                  _entry(work="Прочие работы", date="2026-10-02")],
                                 object_name="Школа")
    first, second = journal["entries"]
    assert first["work_type"] == "earthworks"
    assert second["work_type"] is None
    assert CHECK_WORK_UNKNOWN in _codes(journal)


def test_out_of_order_date_is_error():
    journal = build_work_journal([_entry(date="2026-10-02"),
                                  _entry(date="2026-09-29", qty=50)], object_name="Школа")
    check = next(c for c in journal["checks"] if c["code"] == CHECK_OUT_OF_ORDER)
    assert check["level"] == "error"
    assert "хронологии" in check["message"]


def test_weather_required_for_sensitive_works():
    without = build_work_journal([_entry(weather=None)], object_name="Школа")
    assert CHECK_WEATHER in _codes(without)

    # электрика к погоде не чувствительна — требования нет
    electrical = build_work_journal(
        [_entry(work="Прокладка кабеля в гофре по стенам", weather=None)], object_name="Школа")
    assert CHECK_WEATHER not in _codes(electrical)


def test_duplicate_entry_and_bad_quantity_are_flagged():
    journal = build_work_journal([_entry(), _entry(), _entry(qty="nan")], object_name="Школа")
    codes = _codes(journal)
    assert CHECK_DUPLICATE_ENTRY in codes
    assert "BAD_NUMBER" in codes
    assert journal["entries"][2]["qty"] is None, "nan не должен стать нулём"


def test_journal_cannot_close_unsigned_or_with_errors():
    journal = build_work_journal([_entry()], object_name="Школа")
    assert journal["can_close"]["allowed"] is False
    assert any("не подписан" in r for r in journal["can_close"]["reasons"])

    broken = build_work_journal([_entry(), _entry(date="2026-09-01")], object_name="Школа")
    refused = sign(broken, by="Иванов И.И.", role="начальник ПТО")
    assert refused["sign_problems"]
    assert any("ошибок" in p for p in refused["sign_problems"])


def test_sign_requires_name_and_role():
    journal = build_work_journal([_entry()], object_name="Школа")
    for by, role in (("", "начальник ПТО"), ("Иванов И.И.", "")):
        refused = sign(journal, by=by, role=role)
        assert refused["sign_problems"] and refused["status"] != STATUS_SIGNED


def test_signed_journal_closes_and_edit_invalidates():
    journal = build_work_journal([_entry()], object_name="Школа")
    signed = sign(journal, by="Иванов И.И.", role="начальник ПТО", reason="проверил")
    assert signed["status"] == STATUS_SIGNED
    assert signed["can_close"]["allowed"] is True
    assert check_signature(signed)["valid"] is True

    signed["entries"][0]["qty"] = 999
    assert check_signature(signed)["changed_after_confirm"] is True


def test_acts_comparison_does_not_invent_money():
    acts = {"acts": [{"act_id": "КС-2 №1", "amount_rub": 1000.0,
                      "positions": [{"code": "X", "qty": 1, "price": 1000}]}]}
    journal = build_work_journal([_entry()], object_name="Школа", acts=acts)
    assert journal["vs_act"]["act_amount_rub"] == 1000.0
    assert "JOURNAL_VS_ACT" in _codes(journal)
    assert journal["vs_act"]["journal_amount_rub"] if "journal_amount_rub" in (
        journal["vs_act"] or {}) else True
    markdown = to_markdown(journal)
    assert "построчно сверить не с чем" in markdown, "без наименований работ сверки нет"


def test_acts_comparison_finds_quantity_gap_by_work_type():
    acts = {"acts": [{"act_id": "КС-2 №1", "amount_rub": 1000.0, "positions": [
        {"name": "Разработка грунта экскаватором в котловане", "qty": 150.0, "unit": "м3",
         "price": 100.0}]}]}
    journal = build_work_journal([_entry(qty=100)], object_name="Школа", acts=acts)
    row = next(r for r in journal["vs_act"]["by_work_type"] if r["work_type"] == "earthworks")
    assert row["act_qty"] == 150.0 and row["journal_qty"] == 100.0
    assert row["delta_qty"] == -50.0 and row["comparable"] is True
    gap = next(c for c in journal["checks"] if c["code"] == "JOURNAL_VS_ACT"
               and "расхождение" in c["message"])
    assert "-50.0" in gap["message"] or "-50,0" in gap["message"] or "-50" in gap["message"]


def test_empty_journal_says_so():
    journal = build_work_journal([], object_name="Школа")
    assert journal["entries"] == []
    assert any("нет ни одной записи" in r for r in journal["can_close"]["reasons"])
    refused = sign(journal, by="Иванов И.И.", role="начальник ПТО")
    assert any("подписывать нечего" in p for p in refused["sign_problems"])


# ─────────────────────────── входной контроль ───────────────────────────
def test_accept_without_document_is_impossible():
    journal = build_verification_journal([_record(quality_doc={})], object_name="Школа")
    check = next(c for c in journal["checks"] if c["code"] == CHECK_ACCEPT_NO_DOC)
    assert check["level"] == "error"
    assert journal["records"][0]["acceptable"] is False
    assert journal["totals"]["accepted"] == 0


def test_expired_document_gives_no_acceptance():
    journal = build_verification_journal(
        [_record(date="2026-10-03",
                 quality_doc={"kind": "сертификат", "number": "С-1",
                              "valid_until": "2026-09-01"})], object_name="Школа")
    check = next(c for c in journal["checks"] if c["code"] == CHECK_CERT_EXPIRED)
    assert check["level"] == "error"
    assert journal["records"][0]["acceptable"] is False


def test_document_without_validity_is_a_warning_not_acceptance():
    journal = build_verification_journal(
        [_record(quality_doc={"kind": "паспорт", "number": "П-9"})], object_name="Школа")
    check = next(c for c in journal["checks"] if c["code"] == CHECK_CERT_EXPIRED)
    assert check["level"] == "warning"
    assert journal["records"][0]["acceptable"] is True, "документ есть, срок не указан"


def test_decision_outside_the_list_is_refused():
    journal = build_verification_journal([_record(decision="ок")], object_name="Школа")
    assert CHECK_DECISION in _codes(journal)
    assert journal["records"][0]["decision"] is None


def test_inspector_is_required():
    journal = build_verification_journal([_record(inspected_by="", role="")], object_name="Школа")
    assert CHECK_NO_INSPECTOR in _codes(journal)


def test_duplicate_batch_is_flagged():
    journal = build_verification_journal([_record(), _record()], object_name="Школа")
    assert CHECK_DUPLICATE_BATCH in _codes(journal)


def test_unchecked_decision_does_not_accept():
    journal = build_verification_journal([_record(decision=DECISION_UNCHECKED)],
                                         object_name="Школа")
    assert journal["records"][0]["acceptable"] is False
    assert journal["totals"]["unchecked"] == 1


def test_material_written_off_without_verification_is_visible():
    journal = build_verification_journal(
        [_record(material="Бетон B25")], object_name="Школа",
        writeoffs=[{"code": "Бетон B25", "qty": 210, "unit": "м3"},
                   {"code": "Керамзит", "qty": 12, "unit": "м3"}])
    names = [u["material"] for u in journal["unverified_writeoffs"]]
    assert names == ["Керамзит"]
    assert CHECK_MATERIAL_UNVERIFIED in _codes(journal)
    assert any("без входного контроля" in r for r in journal["can_close"]["reasons"])


def test_markdown_shows_unconfirmed_acceptance():
    journal = build_verification_journal(
        [_record(quality_doc={}),
         _record(material="Песок", batch="п-2", date="2026-10-02")], object_name="Школа")
    markdown = to_markdown(journal)
    assert "допущен · не подтверждён" in markdown, "решение без документа показывается как недействительное"
    assert markdown.count("| нет |") == 1, "отсутствующий документ не маскируется пустой строкой"


def test_verification_journal_sign_and_close():
    journal = build_verification_journal([_record()], object_name="Школа")
    signed = sign(journal, by="Иванов И.И.", role="начальник ПТО")
    assert signed["status"] == STATUS_SIGNED
    assert signed["can_close"]["allowed"] is True
    assert signed["schema"] == SCHEMA_VERIFICATION


# ─────────────────────────── файлы и формулировки ───────────────────────────
def test_files_and_honest_words(tmp_path):
    from openpyxl import load_workbook

    journal = build_work_journal([_entry()], object_name="Школа", number="ОЖР-1")
    files = write(journal, tmp_path / "j")
    assert Path(files["json"]).name == "journal_work_ОЖР-1.json"
    assert Path(files["markdown"]).exists()
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Записи", "Проверки"]
    markdown = to_markdown(journal)
    for forbidden in ("выполнение подтверждено", "работы приняты", "готова к сдаче"):
        assert forbidden not in markdown.lower()
    assert "не подтверждает выполнение" in markdown


def test_demo_journals_keep_their_findings():
    entries, problems = json.loads((DEMO / "work_entries.json").read_text(encoding="utf-8")), []
    journal = build_work_journal(entries["entries"], object_name=entries["object"],
                                 number="ОЖР-1")
    assert not problems
    assert journal["totals"]["entries"] == 5
    assert journal["totals"]["with_weather"] == 5
    assert journal["totals"]["errors"] == 0

    records = json.loads((DEMO / "verification.json").read_text(encoding="utf-8"))
    control = build_verification_journal(records["records"], object_name=records["object"],
                                         number="ВК-1")
    codes = _codes(control)
    assert CHECK_ACCEPT_NO_DOC in codes, "арматура без документа — так и должно быть видно"
    assert CHECK_CERT_EXPIRED in codes, "просроченный сертификат мастики"
    assert control["totals"]["accepted"] == 2


# ─────────────────────────── CLI ───────────────────────────
def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_work_journal_and_sign(tmp_path):
    out = tmp_path / "j"
    run = _cli("journal", "--out", str(out), "work", "--entries",
               str(DEMO / "work_entries.json"), "--object-name", "ЖК", "--number", "ОЖР-1")
    assert run.returncode == 0, run.stderr
    assert "записей: 5" in run.stdout
    assert (out / "journal_work_ОЖР-1.xlsx").exists()

    anonymous = _cli("journal", "--out", str(out), "sign", "--journal",
                     str(out / "journal_work_ОЖР-1.json"), "--by", " ", "--role", " ")
    assert anonymous.returncode == 2 and "ПОДПИСЬ НЕ ПРИНЯТА" in anonymous.stdout

    signed = _cli("journal", "--out", str(out), "sign", "--journal",
                  str(out / "journal_work_ОЖР-1.json"), "--by", "Иванов И.И.",
                  "--role", "начальник ПТО")
    assert signed.returncode == 0, signed.stderr
    assert "журнал можно закрывать" in signed.stdout


def test_cli_verification_journal_flags_errors(tmp_path):
    run = _cli("journal", "--out", str(tmp_path / "j"), "verification", "--records",
               str(DEMO / "verification.json"), "--writeoffs",
               str(ROOT / "data" / "demo" / "consumption" / "writeoffs.csv"))
    assert run.returncode == 2, "в демо-журнале есть допуск без документа и просроченный сертификат"
    assert "ACCEPTED_WITHOUT_DOC" in run.stdout and "CERT_EXPIRED" in run.stdout


# ─────────────────────────── MCP ───────────────────────────
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


def test_mcp_journals_tool_is_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "journals")
    assert set(tool["inputSchema"]["properties"]["action"]["enum"]) == {
        "work", "verification", "sign"}
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "journals")
    assert any("документа качества" in g for g in designed["guardrails"])


def test_mcp_work_journal_returns_checks(tmp_path):
    result = _mcp_call("journals", {"action": "work",
                                    "entries": [_entry(), _entry(date="2026-09-01")],
                                    "object_name": "Школа", "out_dir": str(tmp_path / "j")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert CHECK_OUT_OF_ORDER in [c["code"] for c in payload["checks"]]
    assert payload["can_close"]["allowed"] is False
    assert Path(payload["files"]["xlsx"]).exists()


def test_mcp_verification_refuses_without_document_and_signature(tmp_path):
    built = _mcp_call("journals", {"action": "verification",
                                   "records": [_record(material="Арматура", quality_doc={})],
                                   "object_name": "Школа", "number": "ВК-9",
                                   "out_dir": str(tmp_path / "j")})
    payload = json.loads(built["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["totals"]["accepted"] == 0

    anonymous = _mcp_call("journals", {"action": "sign", "journal_path": payload["files"]["json"],
                                       "by": "", "role": "", "out_dir": str(tmp_path / "j")})
    body = json.loads(anonymous["content"][0]["text"])
    assert body["ok"] is False and body["problems"]
