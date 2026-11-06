# -*- coding: utf-8 -*-
"""Черновик ТК и ППР: чего в нём быть не должно.

Тесты держат четыре правила проекта:

* **никаких выдуманных норм** — норма либо сверена с нормативной базой (с цитатой), либо
  помечена как непроверенная с причиной;
* **никаких выдуманных данных** — состав операций, машины, охрана труда и трудозатраты
  приходят от человека, у агента для них нет источника (`needs_input` с адресатом);
* **документ не «готов»** — статус `draft_unapproved`, пока человек не подтвердит, и даже
  после подтверждения это «черновик, проверенный инженером», а не утверждённая ТК;
* **подтверждение — человеческое** — без ФИО и роли подтверждение не принимается.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import tk          # noqa: E402

FORBIDDEN = ("готова к применению", "утверждена и допускается", "согласована в надзоре")


# ─────────────────────────── нормативная часть ───────────────────────────
def _matrix_with_norms(doc: str = "ПУЭ, 7-е изд.", clause: str = "1.8.37") -> dict:
    return {"profile": "тест", "document_kinds": {"аоср": "Акт освидетельствования скрытых работ"},
            "work_types": [{
                "id": "elec", "name": "Электромонтажные работы", "keywords": [], "strong": ["кабел"],
                "qualifiers": [], "required": ["аоср"], "protocols": [], "hidden_work": True,
                "norms": [{"doc": doc, "clause": clause, "edition": "7-е изд.", "topic": "испытания"}],
            }]}


def test_norm_present_in_base_is_verified_with_quote():
    """Норма, которая есть в базе, приходит с цитатой — не «по памяти»."""
    draft = tk.build_tk_draft(work_type_id="elec", matrix=_matrix_with_norms())
    norm = draft["norms"][0]
    assert norm["verified"] is True and norm["quote"]
    assert "Электрические аппараты" in norm["quote"]
    assert draft["totals"]["norms_verified"] == 1


def test_norm_absent_from_base_is_marked_unverified():
    """СП 45.13330 в базе нет — значит, норма идёт с пометкой, а не как факт."""
    draft = tk.build_tk_draft(work_type_id="earthworks")
    assert draft["norms"], "у земляных работ нормы в матрице есть"
    for norm in draft["norms"]:
        assert norm["verified"] is False
        assert "нет в загруженной нормативной базе" in norm["reason"]
    assert draft["totals"]["norms_unverified"] == len(draft["norms"])
    assert "не проверено по нормативной базе" in tk.to_markdown(draft)


def test_norm_clause_missing_but_document_present():
    """Документ в базе есть, пункта нет — отдельная формулировка, а не «проверено»."""
    draft = tk.build_tk_draft(work_type_id="elec",
                              matrix=_matrix_with_norms(clause="4.2.1"))
    norm = draft["norms"][0]
    assert norm["verified"] is False and "пункта 4.2.1" in norm["reason"]


def test_no_norm_in_matrix_asks_for_them():
    matrix = _matrix_with_norms()
    matrix["work_types"][0]["norms"] = []
    draft = tk.build_tk_draft(work_type_id="elec", matrix=matrix)
    assert any("нормативные ссылки" in n["what"] for n in draft["needs_input"])


# ─────────────────────────── «нужны данные», а не выдумка ───────────────────────────
def test_unknown_parts_are_questions_with_addressee():
    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школа")
    needs = {n["what"]: n for n in draft["needs_input"]}
    for what in ("состав и последовательность операций", "машины, механизмы и инструмент",
                 "калькуляция трудозатрат"):
        assert what in needs, what
        assert needs[what]["ask"].strip(), "у вопроса должен быть адресат"
    assert "технолог" in needs["состав и последовательность операций"]["ask"]


def test_every_section_is_either_filled_or_asked():
    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школе")
    for section in draft["sections"]:
        assert section["items"] or section["needs_input"], \
            f"{section['title']}: пустой раздел без запроса данных"


def test_quality_section_comes_from_matrix():
    """Комплект ИД — то, что у агента есть: берётся из матрицы, а не выдумывается."""
    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школа")
    quality = next(s for s in draft["sections"] if s["key"] == "quality")
    assert quality["items"]
    assert all(item["source"] == "матрица требований" for item in quality["items"])


def test_unknown_work_type_is_question_not_guess():
    draft = tk.build_tk_draft(work_text="покраска забора")
    assert draft["work_type"] is None
    assert any(n["what"] == "вид работ" for n in draft["needs_input"])


def test_estimate_positions_are_marked_as_transferred():
    """Позиции сметы можно перечислить, но с пометкой «проверить» и источником."""
    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школа",
                              positions=[{"name": "Разработка грунта", "qty": 1200, "unit": "м3",
                                          "source": "смета smeta_v1.xlsx"}])
    materials = next(s for s in draft["sections"] if s["key"] == "materials")
    assert materials["items"][0]["text"].startswith("Разработка грунта — 1200 м3")
    assert "проверить" in materials["items"][0]["note"]


# ─────────────────────────── статус и подтверждение ───────────────────────────
def test_draft_is_never_called_ready():
    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школа")
    text = tk.to_markdown(draft)
    assert draft["status"] == tk.STATUS_DRAFT
    assert "не является утверждённой" in text
    for phrase in FORBIDDEN:
        assert phrase not in text.lower()


def test_confirm_requires_human_identity():
    draft = tk.build_tk_draft(work_type_id="earthworks")
    result = tk.confirm(draft, by="", role="")
    assert result["status"] == tk.STATUS_DRAFT
    assert result["confirm_problems"]
    assert "анонимное подтверждение не принимается" in result["confirm_problems"][0]


def test_confirm_records_what_human_filled_and_by_whom():
    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школа")
    result = tk.confirm(draft, by="Иванов И.И.", role="главный инженер",
                        filled={"sequence": ["Срезка растительного слоя; разработка грунта"],
                                "labour": ["Калькуляция по ГЭСН 01-01-030"]},
                        reason="проверено по ППР")
    sequence = next(s for s in result["sections"] if s["key"] == "sequence")
    assert result["status"] == tk.STATUS_CONFIRMED
    assert sequence["items"][0]["source"] == "внесено человеком (Иванов И.И.)"
    assert not sequence["needs_input"]
    assert result["confirmed_by"]["role"] == "главный инженер"
    assert len(result["confirmed_by"]["filled_items"]) == 2
    assert "материалы и изделия" in result["confirmed_by"]["sections_without_data"]


def test_confirm_writes_audit_entry(tmp_path):
    from pto_agent.audit import AuditLog

    log = AuditLog(tmp_path / "audit.jsonl")
    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школа")
    tk.confirm(draft, by="Петров П.П.", role="инженер ПТО",
               filled={"machines": ["Экскаватор 0,65 м3"]}, audit_log=log)
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    assert any(e.get("action") == "tk.confirmed" and e.get("actor") == "Петров П.П."
               for e in entries), entries


def test_confirm_cannot_fill_unknown_section():
    draft = tk.build_tk_draft(work_type_id="earthworks")
    result = tk.confirm(draft, by="Иванов И.И.", role="инженер ПТО",
                        filled={"narnia": ["магия"]})
    assert result["status"] == tk.STATUS_DRAFT
    assert any("не найден" in p for p in result["confirm_problems"])


# ─────────────────────────── ППР ───────────────────────────
def test_ppr_lists_work_types_from_estimate():
    draft = tk.build_ppr_draft(object_name="Школа",
                               positions=[{"name": "Разработка грунта экскаватором"},
                                          {"name": "Монтаж кабеля ВВГнг"}])
    ids = [w["id"] for w in draft["work_types"]]
    assert "earthworks" in ids and "electrical" in ids
    attachments = next(s for s in draft["sections"] if s["key"] == "attachments")
    assert len(attachments["items"]) == len(ids)


def test_ppr_sections_have_specific_asks():
    draft = tk.build_ppr_draft(object_name="Школа")
    asks = {n["what"]: n["ask"] for n in draft["needs_input"]}
    assert "стройгенплан" in asks["строительный генеральный план"].lower()
    assert "график" in asks["календарный план и график производства работ"].lower()
    assert draft["kind"] == "ppr"


# ─────────────────────────── файлы и CLI ───────────────────────────
def test_write_produces_json_markdown_docx(tmp_path):
    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школа")
    files = tk.write(draft, tmp_path)
    for key in ("json", "markdown", "docx"):
        assert Path(files[key]).exists() and Path(files[key]).stat().st_size > 0
    saved = json.loads(Path(files["json"]).read_text(encoding="utf-8"))
    assert saved["schema"] == tk.SCHEMA and saved["status"] == tk.STATUS_DRAFT


def test_docx_contains_unverified_norms_table_and_disclaimer(tmp_path):
    from docx import Document

    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школа")
    path = tk.export_docx(draft, tmp_path / "tk.docx")
    doc = Document(path)
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "не является утверждённой" in text
    assert "НУЖНЫ ДАННЫЕ" in text
    assert doc.tables, "таблица состояния норм должна быть"
    header = [c.text for c in doc.tables[0].rows[0].cells]
    assert "Сверено с базой" in header
    assert any("нет —" in row.cells[3].text for row in doc.tables[0].rows[1:])


def test_cli_draft_and_confirm(tmp_path):
    out = tmp_path / "tk"
    run = subprocess.run([sys.executable, "-m", "pto_agent", "tk", "--out", str(out), "draft",
                          "--work-type", "earthworks", "--object-name", "Школа"],
                         cwd=ROOT, capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
    assert run.returncode == 0, run.stderr
    assert "это черновик" in run.stdout
    assert (out / "tk_draft.docx").exists()

    filled = tmp_path / "filled.json"
    filled.write_text(json.dumps({"labour": ["Калькуляция по ГЭСН"]}, ensure_ascii=False),
                      encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "pto_agent", "tk", "--out", str(out), "confirm",
                          "--draft", str(out / "tk_draft.json"), "--by", "Иванов И.И.",
                          "--role", "инженер ПТО", "--filled", str(filled),
                          "--reason", "проверка"],
                         cwd=ROOT, capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
    assert run.returncode == 0, run.stderr
    saved = json.loads((out / "tk_draft.json").read_text(encoding="utf-8"))
    assert saved["status"] == tk.STATUS_CONFIRMED


def test_cli_confirm_without_human_fails(tmp_path):
    out = tmp_path / "tk"
    subprocess.run([sys.executable, "-m", "pto_agent", "tk", "--out", str(out), "ppr",
                    "--object-name", "Школа"], cwd=ROOT, check=True,
                   env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
                   capture_output=True, text=True)
    run = subprocess.run([sys.executable, "-m", "pto_agent", "tk", "--out", str(out), "confirm",
                          "--draft", str(out / "ppr_draft.json"), "--by", " ", "--role", " "],
                         cwd=ROOT, capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
    assert run.returncode == 2
    assert "ОСТАНОВЛЕНО" in run.stdout


# ─────────────────────────── MCP-инструмент ───────────────────────────
def _mcp_call(name: str, arguments: dict) -> dict:
    """Вызов инструмента через настоящий stdio-протокол, как это делает MCP-клиент."""
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


def test_mcp_tool_is_registered():
    from pto_agent.cad.mcp_server import TOOLS

    names = [t["name"] for t in TOOLS]
    assert "tk_draft" in names
    tool = next(t for t in TOOLS if t["name"] == "tk_draft")
    assert tool["inputSchema"]["type"] == "object"
    assert set(tool["inputSchema"]["required"]) <= set(tool["inputSchema"]["properties"])


def test_mcp_tk_draft_returns_draft_with_questions(tmp_path):
    result = _mcp_call("tk_draft", {"action": "draft", "work_type": "earthworks",
                                    "object_name": "Школа", "out_dir": str(tmp_path)})
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "draft_unapproved"
    assert payload["totals"]["needs_input"] >= 3
    assert payload["unverified_norms"], "у земляных работ нормы в базе не подтверждены"
    assert "не является утверждённой" in payload["disclaimer"]
    assert Path(payload["files"]["docx"]).exists()


def test_mcp_tk_confirm_without_human_is_refused(tmp_path):
    draft = _mcp_call("tk_draft", {"action": "ppr", "object_name": "Школа",
                                   "out_dir": str(tmp_path)})
    path = json.loads(draft["content"][0]["text"])["files"]["json"]
    result = _mcp_call("tk_draft", {"action": "confirm", "draft_path": path, "by": "",
                                    "role": "", "out_dir": str(tmp_path)})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert any("анонимное подтверждение" in p for p in payload["problems"])


def test_tk_tool_is_in_design_schema_and_server():
    """`prompts/tools_schema.json` — проектная схема (свои имена), сервер — реализация.

    Сверять списки один-в-один нельзя: в схеме есть проектные инструменты (`get_estimate`,
    `render_document`), которых в реализации нет. Проверяем то, что действительно важно:
    новый инструмент описан в обеих частях, и у него есть guardrails.
    """
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    from pto_agent.cad.mcp_server import TOOLS

    names = [t["name"] for t in schema["tools"]]
    assert len(names) == len(set(names)), "дубликаты в проектной схеме"
    assert "tk_draft" in names
    assert "tk_draft" in [t["name"] for t in TOOLS]
    entry = next(t for t in schema["tools"] if t["name"] == "tk_draft")
    assert entry["guardrails"] and "не проверено" in " ".join(entry["guardrails"])
