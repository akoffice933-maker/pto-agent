# -*- coding: utf-8 -*-
"""Страж-тесты чек-листов выполненных работ.

Правила:

1. пункт без источника (документ + пункт) в чек-лист не попадает;
2. чек-лист с непроверенными пунктами подписать нельзя;
3. «не соответствует» без описания не подписывается;
4. подпись только с ФИО и ролью;
5. правка результатов после подписи аннулирует подпись (отпечаток содержания);
6. `can_close` не говорит «можно предъявлять», пока есть несоответствия;
7. результат не из списка («ок», «да», пусто) не принимается.
"""
import json
import subprocess
import sys
from pathlib import Path

from pto_agent.checklist import (CHECK_DUPLICATE_ITEM, CHECK_EMPTY, CHECK_ITEM_NO_SOURCE,
                                 CHECK_ITEM_UNVERIFIED, RESULT_FAIL, RESULT_OK,
                                 RESULT_UNCHECKED, STATUS_CHANGED, STATUS_DRAFT, STATUS_FILLED,
                                 STATUS_SIGNED, STATUS_SIGNED_WITH_ISSUES, build_checklist,
                                 can_close, fill, sign, to_markdown, write)
from pto_agent.fingerprint import check_signature

ROOT = Path(__file__).resolve().parents[1]


def _items(n: int = 2) -> list[dict]:
    return [{"requirement": f"Требование {i}",
             "control": "замерить",
             "source": {"doc": "СП 45.13330.2017", "point": f"6.1.{i}", "edition": "с изм. 1"}}
            for i in range(1, n + 1)]


def _checklist(n: int = 2, **kwargs) -> dict:
    return build_checklist(object_name="Школа", number="ЧЛ-1", items=_items(n), **kwargs)


def _fill_all(checklist: dict, result: str = RESULT_OK, note: str = "") -> dict:
    return fill(checklist, [{"no": item["no"], "result": result, "note": note}
                            for item in checklist["items"]], by="Петров П.П.")


# ─────────────────────────── сборка ───────────────────────────
def test_item_without_source_is_not_created():
    items = [{"requirement": "Проверить всё", "source": {}},
             {"requirement": "Проверить ещё", "source": {"doc": "СП 45.13330.2017"}},
             {"source": {"doc": "СП 45.13330.2017", "point": "6.1.2"}}]      # без текста
    checklist = build_checklist(object_name="Школа", items=items)
    assert checklist["items"] == []
    assert checklist["totals"]["rejected"] == 3
    assert CHECK_ITEM_NO_SOURCE in [c["code"] for c in checklist["checks"]]


def test_matrix_items_come_from_norms_not_from_model():
    checklist = build_checklist(work_type_id="earthworks", object_name="Школа")
    sources = [i["source"]["doc"] for i in checklist["items"]]
    assert "СП 45.13330.2017" in sources
    for item in checklist["items"]:
        assert item["source"]["point"], "у пункта должен быть номер пункта нормы"
        assert item["source_scope"] == "matrix"


def test_unknown_sources_are_marked_not_verified():
    checklist = build_checklist(object_name="Школа", items=_items())
    assert checklist["totals"]["source_verified"] == 0
    assert CHECK_ITEM_UNVERIFIED in [c["code"] for c in checklist["checks"]]
    markdown = to_markdown(checklist)
    assert "не сверено" in markdown


def test_duplicate_requirements_are_not_added_twice():
    items = _items(1) + _items(1)
    checklist = build_checklist(object_name="Школа", items=items)
    assert checklist["totals"]["items"] == 1
    assert CHECK_DUPLICATE_ITEM in [c["code"] for c in checklist["checks"]]


def test_empty_checklist_says_so():
    checklist = build_checklist(object_name="Школа")
    assert checklist["items"] == []
    assert CHECK_EMPTY in [c["code"] for c in checklist["checks"]]
    assert checklist["can_close"]["allowed"] is False


def test_fresh_checklist_is_draft_with_all_items_unchecked():
    checklist = _checklist(3)
    assert checklist["status"] == STATUS_DRAFT
    assert checklist["totals"]["unchecked"] == 3
    assert all(i["result"] == RESULT_UNCHECKED for i in checklist["items"])


# ─────────────────────────── заполнение ───────────────────────────
def test_fill_marks_filled_state():
    filled = _fill_all(_checklist(2))
    assert filled["status"] == STATUS_FILLED
    assert filled["totals"]["checked"] == 2 and filled["totals"]["ok"] == 2
    assert filled["items"][0]["result_by"] == "Петров П.П."


def test_result_outside_the_list_is_refused():
    filled = fill(_checklist(1), [{"no": 1, "result": "ок"}])
    assert filled["items"][0]["result"] == RESULT_UNCHECKED
    assert any(c["code"] == "BAD_RESULT" for c in filled["checks"])


def test_fill_unknown_item_number_is_reported():
    filled = fill(_checklist(2), [{"no": 9, "result": RESULT_OK}])
    assert any("пункта №9" in p for p in filled["problems"])


# ─────────────────────────── подпись ───────────────────────────
def test_sign_refused_without_name_or_role():
    filled = _fill_all(_checklist(2))
    for by, role in (("", "инженер ПТО"), ("Иванов И.И.", ""), (" ", " ")):
        refused = sign(filled, by=by, role=role)
        assert refused.get("sign_problems"), (by, role)
        assert refused["status"] == STATUS_FILLED


def test_sign_refused_while_items_unchecked():
    checklist = fill(_checklist(3), [{"no": 1, "result": RESULT_OK}])
    refused = sign(checklist, by="Иванов И.И.", role="начальник ПТО")
    assert any("не проверено пунктов: 2" in p for p in refused["sign_problems"])


def test_nonconformity_without_note_is_not_signed():
    filled = _fill_all(_checklist(2), result=RESULT_FAIL, note="")
    refused = sign(filled, by="Иванов И.И.", role="начальник ПТО")
    assert any("несоответствия без описания" in p for p in refused["sign_problems"])


def test_signed_checklist_without_issues_allows_work():
    signed = sign(_fill_all(_checklist(2)), by="Иванов И.И.", role="начальник ПТО")
    assert signed["status"] == STATUS_SIGNED
    assert signed["can_close"]["allowed"] is True
    assert check_signature(signed)["valid"] is True


def test_signed_with_nonconformity_blocks_work():
    filled = _fill_all(_checklist(2), result=RESULT_FAIL, note="зазор 12 мм при допуске 5 мм")
    signed = sign(filled, by="Иванов И.И.", role="начальник ПТО")
    assert signed["status"] == STATUS_SIGNED_WITH_ISSUES
    assert signed["can_close"]["allowed"] is False
    assert any("несоответствий: 2" in r for r in signed["can_close"]["reasons"])
    assert "работы предъявлять нельзя" in to_markdown(signed).lower()


def test_edit_after_sign_invalidates_signature():
    signed = sign(_fill_all(_checklist(2)), by="Иванов И.И.", role="начальник ПТО")
    signed["items"][0]["result"] = RESULT_FAIL
    assert check_signature(signed)["changed_after_confirm"] is True
    assert can_close(signed)["allowed"] is False
    recounted = fill(signed, [], by="Иванов И.И.")
    assert recounted["status"] == STATUS_CHANGED


def test_signed_empty_checklist_is_refused():
    empty = build_checklist(object_name="Школа")
    refused = sign(empty, by="Иванов И.И.", role="начальник ПТО")
    assert any("нет ни одного пункта" in p for p in refused["sign_problems"])


# ─────────────────────────── файлы и формулировки ───────────────────────────
def test_docx_has_sources_and_signature_places(tmp_path):
    from docx import Document

    signed = sign(_fill_all(_checklist(2)), by="Иванов И.И.", role="начальник ПТО")
    files = write(signed, tmp_path / "cl")
    assert Path(files["json"]).name == "checklist_ЧЛ-1.json"
    doc = Document(files["docx"])
    text = "\n".join(p.text for p in doc.paragraphs)
    text += "\n" + "\n".join(cell.text for table in doc.tables
                             for row in table.rows for cell in row.cells)
    assert "СП 45.13330.2017, п. 6.1.1" in text
    assert "Проверил (инженер ПТО)" in text and "Принял (представитель заказчика)" in text
    assert "работы принимает человек" in text.lower() or "Работы принимает человек" in text


def test_markdown_never_claims_work_accepted_with_issues():
    filled = _fill_all(_checklist(1), result=RESULT_FAIL, note="отклонение по отметке")
    signed = sign(filled, by="Иванов И.И.", role="начальник ПТО")
    text = to_markdown(signed).lower()
    for forbidden in ("работы приняты", "готова к сдаче", "готов к сдаче"):
        assert forbidden not in text
    assert "работы предъявлять нельзя" in text


# ─────────────────────────── CLI ───────────────────────────
def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_build_fill_sign_flow(tmp_path):
    out = tmp_path / "cl"
    run = _cli("checklist", "--out", str(out), "build", "--work-type", "earthworks",
               "--object-name", "Школа", "--number", "ЧЛ-1")
    assert run.returncode == 0, run.stderr
    assert "вид работ: Земляные работы" in run.stdout
    assert (out / "checklist_ЧЛ-1.docx").exists()

    path = out / "checklist_ЧЛ-1.json"
    checklist = json.loads(path.read_text(encoding="utf-8"))
    results = tmp_path / "results.json"
    results.write_text(json.dumps([{"no": item["no"], "result": "соответствует"}
                                   for item in checklist["items"]], ensure_ascii=False),
                       encoding="utf-8")
    run = _cli("checklist", "--out", str(out), "fill", "--checklist", str(path),
               "--results", str(results), "--by", "Петров П.П.")
    assert run.returncode == 0, run.stderr
    assert "проверено 3 из 3" in run.stdout

    anonymous = _cli("checklist", "--out", str(out), "sign", "--checklist", str(path),
                     "--by", " ", "--role", " ")
    assert anonymous.returncode == 2 and "ПОДПИСЬ НЕ ПРИНЯТА" in anonymous.stdout

    signed = _cli("checklist", "--out", str(out), "sign", "--checklist", str(path),
                  "--by", "Иванов И.И.", "--role", "начальник ПТО")
    assert signed.returncode == 0, signed.stderr
    assert "можно предъявлять" in signed.stdout
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["status"] == STATUS_SIGNED


def test_cli_rejects_item_file_without_sources(tmp_path):
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"requirement": "Проверить всё"}], ensure_ascii=False),
                     encoding="utf-8")
    run = _cli("checklist", "--out", str(tmp_path / "cl"), "build", "--items", str(items))
    assert run.returncode == 2
    assert "ITEM_WITHOUT_SOURCE" in run.stdout


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


def test_mcp_checklist_tool_is_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "checklist")
    assert set(tool["inputSchema"]["properties"]["action"]["enum"]) == {"build", "fill", "sign"}
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "checklist")
    assert any("непроверенными" in g for g in designed["guardrails"])


def test_mcp_checklist_build_rejects_sourceless_items(tmp_path):
    result = _mcp_call("checklist", {"action": "build", "object_name": "Школа",
                                     "items": [{"requirement": "Проверить всё"}],
                                     "out_dir": str(tmp_path / "cl")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["totals"]["rejected"] == 1
    assert payload["can_close"]["allowed"] is False


def test_mcp_checklist_sign_requires_all_items_checked(tmp_path):
    built = _mcp_call("checklist", {"action": "build", "object_name": "Школа",
                                    "items": _items(2), "number": "ЧЛ-7",
                                    "out_dir": str(tmp_path / "cl")})
    payload = json.loads(built["content"][0]["text"])
    path = payload["files"]["json"]

    refused = _mcp_call("checklist", {"action": "sign", "checklist_path": path,
                                      "by": "Иванов И.И.", "role": "начальник ПТО",
                                      "out_dir": str(tmp_path / "cl")})
    body = json.loads(refused["content"][0]["text"])
    assert body["ok"] is False and "не проверено пунктов: 2" in " ".join(body["problems"])

    _mcp_call("checklist", {"action": "fill", "checklist_path": path,
                            "results": [{"no": 1, "result": "соответствует"},
                                        {"no": 2, "result": "соответствует"}],
                            "by": "Петров П.П.", "out_dir": str(tmp_path / "cl")})
    signed = _mcp_call("checklist", {"action": "sign", "checklist_path": path,
                                     "by": "Иванов И.И.", "role": "начальник ПТО",
                                     "out_dir": str(tmp_path / "cl")})
    body = json.loads(signed["content"][0]["text"])
    assert body["ok"] is True and body["can_close"]["allowed"] is True
