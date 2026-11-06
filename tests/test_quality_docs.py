# -*- coding: utf-8 -*-
"""Страж-тесты документов качества на материалы.

Правила, которые проверяются:

1. документ без типа, номера, даты выдачи или материала в реестр не принимается;
2. тип вне рабочего перечня (товарный чек, счёт) документом качества не является;
3. документ проверяется на **тот** материал: марка и фракция входят в наименование;
4. документ на другую партию не закрывает применённую партию;
5. срок действия сверяется с датой применения, а не с сегодняшним днём;
6. сертификат без срока действия — предупреждение, а не «всё в порядке»;
7. копия без отметки «копия верна» (ФИО и роль) в комплект не идёт;
8. состав требуемых документов берётся из файла требований; нет файла — «требование
   не заявлено», а не «комплект есть»;
9. дубль документа в описи не удваивается;
10. стык с журналом входного контроля: допуск по документу, которого нет в реестре, — ошибка;
    документ без входного контроля — предупреждение;
11. комплект передаётся только подписанным человеком с ФИО и ролью и без ошибок;
    правка после подписи аннулирует подпись.
"""
import json
import subprocess
import sys
from pathlib import Path

from pto_agent.fingerprint import check_signature
from pto_agent.quality_docs import (CHECK_BASIS_MISSING, CHECK_BATCH_MISMATCH, CHECK_BATCH_UNUSED,
                                    CHECK_DOC_INCOMPLETE,
                                    CHECK_DOC_NO_VERIF, CHECK_DUPLICATE, CHECK_EXPIRED,
                                    CHECK_MATERIAL_NO_DOC, CHECK_NOT_ATTESTED, CHECK_NO_FILE,
                                    CHECK_NO_VALIDITY, CHECK_REQ_UNDECLARED, CHECK_TYPE_UNKNOWN,
                                    CHECK_VERIF_NO_DOC, COVER_FULL, COVER_INVALID, COVER_MISSING,
                                    COVER_UNDECLARED, SCHEMA, STATUS_SIGNED, build_registry,
                                    can_transfer, load_types, normalize_kind, sign, to_markdown,
                                    write)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo" / "quality_docs"

USAGE = [{"material": "Бетон B25", "batch": "партия 14", "qty": 200, "unit": "м3",
          "applied_from": "2026-10-04"}]
REQ = [{"material": "Бетон B25", "required": ["паспорт качества", "протокол испытаний"],
        "basis": "договор Д-142/2026, прил. 3, п. 4.12"}]


def _doc(**overrides) -> dict:
    base = {"kind": "паспорт качества", "number": "П-1", "issue_date": "2026-09-28",
            "material": "Бетон B25", "batch": "партия 14", "manufacturer": "ООО «БетонСервис»",
            "standard": "ГОСТ 7473-2010", "file": "quality/beton_1.pdf",
            "attested_by": {"by": "Иванов И.И.", "role": "инженер ПТО", "at": "2026-10-01"}}
    base.update(overrides)
    return base


def _registry(documents=None, **kwargs):
    return build_registry(documents if documents is not None else [_doc()],
                          object_name="Школа", **kwargs)


def _codes(registry) -> list[str]:
    return [c["code"] for c in registry["checks"]]


def _demo():
    load = lambda name: json.loads((DEMO / name).read_text(encoding="utf-8"))
    return (load("documents.json"), load("requirements.json"), load("usage.json"))


# ─────────────────────────── реквизиты и тип ───────────────────────────
def test_types_reference_is_honest():
    types = load_types()
    assert types["schema"] == "pto-agent/quality-doc-types@1"
    assert "не нормативный первоисточник" in types["disclaimer"]
    assert types["types"]["сертификат соответствия"]["validity"] == "required"


def test_kind_normalisation_and_aliases():
    assert normalize_kind("сертификат")[0] == "сертификат соответствия"
    assert normalize_kind("Паспорт качества партии № 5")[0] == "паспорт качества"
    kind, why = normalize_kind("товарный чек")
    assert kind is None and "не документ качества" in why


def test_incomplete_document_is_not_accepted():
    registry = _registry([_doc(number=""), _doc(kind="", number="Х-1"),
                          _doc(issue_date=""), _doc(material="")])
    assert registry["totals"]["documents"] == 0
    assert registry["totals"]["rejected"] == 4
    assert CHECK_DOC_INCOMPLETE in _codes(registry)
    assert all(d["why"] for d in registry["rejected"])


def test_payment_papers_are_not_quality_documents():
    registry = _registry([_doc(kind="товарный чек", number="Ч-5")])
    assert CHECK_TYPE_UNKNOWN in _codes(registry)
    assert registry["totals"]["documents"] == 0


def test_document_without_basis_and_validity_is_flagged():
    registry = _registry([_doc(kind="сертификат соответствия", standard="")])
    codes = _codes(registry)
    assert CHECK_BASIS_MISSING in codes, "основание соответствия не указано"
    assert CHECK_NO_VALIDITY in codes, "у сертификата нет срока действия"


def test_copy_without_attestation_is_not_ready():
    registry = _registry([_doc(attested_by=None)])
    assert CHECK_NOT_ATTESTED in _codes(registry)
    assert registry["can_transfer"]["allowed"] is False
    assert any("копия верна" in r for r in registry["can_transfer"]["reasons"])


def test_document_without_file_is_only_a_name():
    registry = _registry([_doc(file="")])
    assert CHECK_NO_FILE in _codes(registry)


def test_duplicate_number_is_not_doubled():
    registry = _registry([_doc(), _doc(file="quality/beton_1_copy.pdf")])
    assert CHECK_DUPLICATE in _codes(registry)
    assert registry["totals"]["documents"] == 2, "оба документа видны, но в описи это дубль"


# ─────────────────────────── партия и материал ───────────────────────────
def test_document_for_other_batch_does_not_close_the_used_batch():
    registry = _registry([_doc(batch="партия 15")], usage=USAGE, requirements=REQ)
    codes = _codes(registry)
    assert CHECK_MATERIAL_NO_DOC in codes, "применённая партия 14 без документа"
    assert CHECK_BATCH_MISMATCH in codes, "документ на партию 15 прикрывает не ту партию"


def test_material_mark_and_fraction_are_part_of_the_name():
    usage = [{"material": "Керамзит фр. 5-10", "batch": "п-11", "qty": 12, "unit": "м3",
              "applied_from": "2026-10-04"}]
    registry = build_registry([_doc(material="Керамзит фр. 10-20", batch="п-11")],
                              object_name="Школа", usage=usage,
                              requirements=[{"material": "Керамзит фр. 5-10",
                                             "required": ["паспорт качества"]}])
    assert registry["totals"]["documents"] == 1
    assert CHECK_MATERIAL_NO_DOC in _codes(registry), "документ на другую фракцию — другой материал"


def test_expired_document_does_not_cover_the_application_date():
    usage = [{"material": "Мастика", "batch": "п-3", "qty": 200, "unit": "кг",
              "applied_from": "2026-10-03"}]
    registry = build_registry(
        [_doc(kind="сертификат соответствия", material="Мастика", batch="п-3",
              issue_date="2025-09-01", valid_until="2026-09-01")],
        object_name="Школа", usage=usage,
        requirements=[{"material": "Мастика", "required": ["сертификат соответствия"]}])
    assert CHECK_EXPIRED in _codes(registry)
    assert registry["materials"][0]["status"] == COVER_INVALID
    assert registry["documents"][0]["valid_on_use"] is False


def test_validity_is_checked_against_use_date_not_today():
    usage = [{"material": "Мастика", "batch": "п-3", "qty": 200, "unit": "кг",
              "applied_from": "2026-10-03"}]
    registry = build_registry(
        [_doc(kind="сертификат соответствия", material="Мастика", batch="п-3",
              issue_date="2026-05-01", valid_until="2026-10-10")],
        object_name="Школа", usage=usage, today=__import__("datetime").date(2026, 11, 6))
    assert CHECK_EXPIRED not in _codes(registry), "документ действовал на дату применения"
    assert registry["documents"][0]["valid_on_use"] is True


# ─────────────────────────── требования и комплектность ───────────────────────────
def test_requirements_come_from_file_not_from_air():
    registry = _registry(usage=USAGE)                 # файла требований нет
    card = registry["materials"][0]
    assert card["status"] == COVER_UNDECLARED
    assert CHECK_REQ_UNDECLARED in _codes(registry)
    assert registry["can_transfer"]["allowed"] is False


def test_missing_required_document_makes_set_incomplete():
    registry = _registry([_doc()], usage=USAGE, requirements=REQ)
    card = registry["materials"][0]
    assert card["status"] != COVER_FULL
    assert card["missing"] == ["протокол испытаний"]


def test_full_set_is_recognised():
    registry = _registry([_doc(), _doc(kind="протокол испытаний", number="ПИ-14")],
                         usage=USAGE, requirements=REQ)
    assert registry["materials"][0]["status"] == COVER_FULL
    assert registry["totals"]["covered_batches"] == 1


def test_material_without_any_document_is_an_error():
    registry = _registry([], usage=USAGE, requirements=REQ)
    assert CHECK_MATERIAL_NO_DOC in _codes(registry)
    assert registry["materials"][0]["status"] == COVER_MISSING


# ─────────────────────────── стык с журналом входного контроля ───────────────────────────
def test_verification_acceptance_needs_document_in_registry():
    verification = [{"material": "Арматура", "batch": "п-7", "decision": "допущен",
                     "quality_doc": {"kind": "сертификат", "number": "С-999"}}]
    registry = _registry([_doc()], usage=USAGE, requirements=REQ, verification=verification)
    assert CHECK_VERIF_NO_DOC in _codes(registry)
    assert any("С-999" in c["message"] for c in registry["checks"]
               if c["code"] == CHECK_VERIF_NO_DOC)


def test_document_without_incoming_control_is_flagged():
    verification = [{"material": "Бетон B25", "batch": "партия 14", "decision": "допущен",
                     "quality_doc": {"kind": "паспорт качества", "number": "П-1"}}]
    registry = _registry([_doc()], usage=USAGE, requirements=REQ, verification=verification)
    assert CHECK_DOC_NO_VERIF not in _codes(registry), "контроль оформлен — замечания нет"

    silent = _registry([_doc()], usage=USAGE, requirements=REQ, verification=[])
    assert CHECK_DOC_NO_VERIF in _codes(silent)


# ─────────────────────────── подпись и передача ───────────────────────────
def test_transfer_needs_signature_and_no_errors():
    registry = _registry([_doc(), _doc(kind="протокол испытаний", number="ПИ-14")],
                         usage=USAGE, requirements=REQ)
    assert registry["can_transfer"]["allowed"] is False
    assert any("не подписан" in r for r in registry["can_transfer"]["reasons"])

    for by, role in (("", "начальник ПТО"), ("Иванов И.И.", "")):
        refused = sign(registry, by=by, role=role)
        assert refused["sign_problems"] and refused["status"] != STATUS_SIGNED

    signed = sign(registry, by="Иванов И.И.", role="начальник ПТО", reason="сверил с партиями")
    assert signed["status"] == STATUS_SIGNED
    assert signed["can_transfer"]["allowed"] is True
    assert check_signature(signed)["valid"] is True

    broken = _registry([_doc(batch="партия 15"), _doc(kind="протокол испытаний",
                                                      number="ПИ-14", batch="партия 15")],
                       usage=USAGE, requirements=REQ)
    refused = sign(broken, by="Иванов И.И.", role="начальник ПТО")
    assert refused["sign_problems"], "комплект с ошибками не подписывается"


def test_edit_after_signature_invalidates_it():
    registry = _registry([_doc(), _doc(kind="протокол испытаний", number="ПИ-14")],
                         usage=USAGE, requirements=REQ)
    signed = sign(registry, by="Иванов И.И.", role="начальник ПТО")
    signed["documents"][0]["valid_until"] = "2030-01-01"
    assert check_signature(signed)["changed_after_confirm"] is True
    assert can_transfer(signed)["allowed"] is False


# ─────────────────────────── файлы, формулировки, демо ───────────────────────────
def test_files_and_honest_words(tmp_path):
    from openpyxl import load_workbook

    registry = _registry([_doc(), _doc(kind="протокол испытаний", number="ПИ-14")],
                         usage=USAGE, requirements=REQ, number="ДК-1")
    files = write(registry, tmp_path / "q")
    assert Path(files["json"]).name == "quality_ДК-1.json"
    assert Path(files["docx"]).exists()
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Документы", "Материалы", "Проверки"]
    markdown = to_markdown(registry).lower()
    for forbidden in ("качество подтверждено", "материалы приняты", "допущен без замечаний"):
        assert forbidden not in markdown
    assert "агент его не выдумывает" in markdown


def test_demo_registry_keeps_its_findings():
    documents, requirements, usage = _demo()
    registry = build_registry(documents["documents"], object_name=documents["object"],
                              number="ДК-1", requirements=requirements["requirements"],
                              usage=usage["usage"])
    codes = _codes(registry)
    assert CHECK_EXPIRED in codes, "сертификат мастики истёк до применения"
    assert CHECK_TYPE_UNKNOWN in codes, "товарный чек — не документ качества"
    assert CHECK_MATERIAL_NO_DOC in codes, "песок применён партией без документа"
    assert CHECK_DUPLICATE in codes, "две копии одного сертификата"
    statuses = {m["material"]: m["status"] for m in registry["materials"]}
    assert statuses["Мастика битумная"] == COVER_INVALID
    assert statuses["Бетон B25"] == COVER_FULL
    assert registry["totals"]["errors"] >= 6


# ─────────────────────────── CLI и MCP ───────────────────────────
def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_builds_registry_and_refuses_anonymous_signature(tmp_path):
    out = tmp_path / "q"
    built = _cli("quality", "--out", str(out), "build",
                 "--documents", str(DEMO / "documents.json"),
                 "--requirements", str(DEMO / "requirements.json"),
                 "--usage", str(DEMO / "usage.json"),
                 "--verification", str(ROOT / "data" / "demo" / "journals" / "verification.json"),
                 "--object-name", "ЖК", "--number", "ДК-1")
    assert built.returncode == 2, "в демо-реестре есть ошибки"
    assert "документов: 7" in built.stdout and "Мастика битумная: документ недействителен" in built.stdout
    assert (out / "quality_ДК-1.docx").exists()

    anonymous = _cli("quality", "--out", str(out), "sign", "--registry",
                     str(out / "quality_ДК-1.json"), "--by", " ", "--role", " ")
    assert anonymous.returncode == 2 and "ПОДПИСЬ НЕ ПРИНЯТА" in anonymous.stdout


def test_mcp_quality_tool_is_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "quality_docs")
    assert set(tool["inputSchema"]["properties"]["action"]["enum"]) == {"build", "sign"}
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "quality_docs")
    assert len(designed["guardrails"]) >= 6
    assert any("копия верна" in g for g in designed["guardrails"])


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


def test_mcp_quality_returns_findings_and_files(tmp_path):
    result = _mcp_call("quality_docs", {"documents": [_doc(batch="партия 15")], "usage": USAGE,
                                        "requirements": REQ, "object_name": "Школа",
                                        "number": "ДК-9", "out_dir": str(tmp_path / "q")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["kind"] == SCHEMA and payload["ok"] is False
    codes = [c["code"] for c in payload["checks"]]
    assert CHECK_MATERIAL_NO_DOC in codes
    assert Path(payload["files"]["xlsx"]).exists()

    anonymous = _mcp_call("quality_docs", {"action": "sign", "registry_path":
                                           payload["files"]["json"], "by": "", "role": "",
                                           "out_dir": str(tmp_path / "q")})
    body = json.loads(anonymous["content"][0]["text"])
    assert body["ok"] is False and body["problems"]
