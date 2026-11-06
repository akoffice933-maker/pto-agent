# -*- coding: utf-8 -*-
"""Месячный план: работы месяца, требования к документам, что готово к предъявлению.

Правила:

1. требования берутся из матрицы вида работ, а не из головы: набор документов в отчёте равен
   набору в матрице;
2. позиция без опознанного вида работ — «требование не заявлено», а не «документы не нужны»;
3. документ есть, но без ФИО и роли — работа не готова;
4. вид работ без требований в матрице (вывоз мусора) — «требований не заявлено», это не ошибка
   и не «всё в порядке»;
5. сумма позиций сверяется с суммой акта; нет цены — «не оценено», а не 0 ₽;
6. срок из графика: нет срока — не считается, прошёл — просрочка, близко — предупреждение.
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from pto_agent.asbuilt.requirements import load_matrix, work_type_requirements
from pto_agent.monthly import (CHECK_ACTS_EMPTY, CHECK_ACT_SUM_MISMATCH,
                               CHECK_ACT_WITHOUT_POSITIONS, CHECK_DEADLINE_MISSED,
                               CHECK_DEADLINE_NOT_STATED, CHECK_DEADLINE_SOON,
                               CHECK_DOC_MISSING, CHECK_DOC_UNKNOWN_KIND, CHECK_DOC_UNSIGNED,
                               CHECK_MATRIX_MISSING, CHECK_NO_REQUIREMENTS,
                               CHECK_POSITION_WITHOUT_PRICE, CHECK_WORK_NOT_READY,
                               CHECK_WORK_TYPE_NOT_STATED, SCHEMA, WORK_NO_REQUIREMENTS,
                               WORK_NOT_READY, WORK_READY, build_plan, to_markdown, verdict,
                               write)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
TODAY = date(2026, 11, 6)
MATRIX = ROOT / "data" / "reference" / "work_type_requirements.json"

CONCRETE = "Бетонирование монолитных стен подвала"
EARTH = "Разработка грунта экскаватором в котловане"


def _codes(plan) -> list[str]:
    return [c["code"] for c in plan["checks"]]


def _pos(name: str, amount: float, **extra) -> dict:
    row = {"code": "ФЕР 00-00-000-00", "name": name, "qty": 1, "unit": "шт",
           "price": amount, "amount_rub": amount}
    row.update(extra)
    return row


def _doc(kind: str, number: str = "Д-1", signed: bool = True, **extra) -> dict:
    row = {"kind": kind, "number": number}
    if signed:
        row.update({"signed_by": "Иванов И.И.", "role": "начальник ПТО", "date": "2026-11-01"})
    row.update(extra)
    return row


def _plan(tmp_path: Path, acts: list[dict], **config) -> dict:
    base = {"name": "проба", "month": "2026-11", "requirements": str(MATRIX), "acts": acts}
    base.update(config)
    return build_plan(base, base_dir=tmp_path, out_dir=tmp_path / "mon", today=TODAY)


def _all_docs_for(work_type: str) -> list[dict]:
    """Полный комплект по матрице для вида работ — чтобы проверять «готово»."""
    req = work_type_requirements(work_type, load_matrix(MATRIX))
    docs = [_doc(kind, f"{kind}-1") for kind in req["required"]]
    docs += [_doc("протокол", f"ПИ-{i}", requirement=protocol)
             for i, protocol in enumerate(req["protocols"], start=1)]
    return docs


# ─────────────────────────── требования из матрицы ───────────────────────────
def test_requirements_come_from_matrix(tmp_path):
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1_000_000,
                             "positions": [_pos(EARTH, 1_000_000)]}])
    work = plan["acts"][0]["works"][0]
    matrix = load_matrix(MATRIX)
    assert work["required"] == matrix["work_types"][0]["required"]
    assert work["required"] == ["аоср", "схема", "журнал"]
    assert work["protocols"], "протоколы тоже из матрицы"
    assert plan["matrix"]["profile"]["name"] == "pto-agent/default"


def test_ready_work_when_everything_present_and_signed(tmp_path):
    docs = _all_docs_for("earthworks")
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1_000_000,
                             "positions": [_pos(EARTH, 1_000_000)], "documents": docs}])
    work = plan["acts"][0]["works"][0]
    assert work["status"] == WORK_READY, [m["what"] for m in work["missing"]]
    assert plan["totals"]["money_ready_rub"] == 1_000_000.0
    assert plan["verdict"]["ready_to_invoice"] is True


def test_missing_document_blocks_and_makes_action(tmp_path):
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1_000_000,
                             "positions": [_pos(EARTH, 1_000_000)],
                             "documents": [_doc("журнал")]}])
    work = plan["acts"][0]["works"][0]
    assert work["status"] == WORK_NOT_READY
    assert {m["kind"] for m in work["missing"]} <= {"аоср", "схема", "протокол"}
    assert CHECK_DOC_MISSING in _codes(plan) and CHECK_WORK_NOT_READY in _codes(plan)
    assert any(a["what"] == "получить" for a in plan["actions"])
    assert plan["totals"]["money_waiting_rub"] == 1_000_000.0


def test_unsigned_document_is_not_a_document(tmp_path):
    docs = _all_docs_for("earthworks")
    docs[0] = _doc(docs[0]["kind"], "схема-без-подписи", signed=False)
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1_000_000,
                             "positions": [_pos(EARTH, 1_000_000)], "documents": docs}])
    work = plan["acts"][0]["works"][0]
    assert work["status"] == WORK_NOT_READY
    assert len(work["unsigned"]) == 1
    assert CHECK_DOC_UNSIGNED in _codes(plan)
    assert any(a["what"] == "подписать" for a in plan["actions"])


def test_unknown_work_type_is_not_stated_requirement(tmp_path):
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 120_000,
                             "positions": [_pos("Устройство временного ограждения площадки",
                                                120_000)]}])
    work = plan["acts"][0]["works"][0]
    assert work["work_type"] is None
    assert work["status"] == WORK_NOT_READY
    assert CHECK_WORK_TYPE_NOT_STATED in _codes(plan)
    assert plan["totals"]["money_ready_rub"] == 0.0, "деньги спорной позиции не «готовы»"
    assert any("не опознан" in c["message"] for c in plan["checks"])


def test_work_type_without_requirements_is_flagged(tmp_path):
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 120_000,
                             "positions": [_pos("Погрузка и вывоз строительного мусора",
                                                120_000)]}])
    work = plan["acts"][0]["works"][0]
    assert work["status"] == WORK_NO_REQUIREMENTS
    assert CHECK_NO_REQUIREMENTS in _codes(plan)
    assert "NO_REQUIREMENTS" not in [c["code"] for c in plan["checks"] if c["level"] == "error"]


def test_unknown_document_kind(tmp_path):
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1_000_000,
                             "positions": [_pos(EARTH, 1_000_000)],
                             "documents": [_doc("товарный чек")]}])
    assert CHECK_DOC_UNKNOWN_KIND in _codes(plan)


# ─────────────────────────── деньги и сверки ───────────────────────────
def test_act_sum_mismatch_is_an_error(tmp_path):
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1_000_000,
                             "positions": [_pos(EARTH, 900_000)]}])
    assert CHECK_ACT_SUM_MISMATCH in _codes(plan)


def test_position_without_price_is_not_zero(tmp_path):
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1",
                             "positions": [{"code": "ФЕР 1", "name": EARTH, "qty": 10,
                                            "unit": "м3"}]}])
    assert CHECK_POSITION_WITHOUT_PRICE in _codes(plan)
    work = plan["acts"][0]["works"][0]
    assert work["amount_rub"] == 0.0 and work["amount_unknown"] is True
    assert plan["totals"]["money_unknown_works"] == 1


def test_act_without_positions(tmp_path):
    plan = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1_000_000}])
    assert CHECK_ACT_WITHOUT_POSITIONS in _codes(plan)


def test_empty_month(tmp_path):
    plan = _plan(tmp_path, [])
    assert CHECK_ACTS_EMPTY in _codes(plan)
    assert plan["verdict"]["ready_to_invoice"] is False


def test_matrix_missing(tmp_path):
    plan = build_plan({"name": "проба", "month": "2026-11", "requirements": "нет/такого.json",
                       "acts": [{"act_id": "КС-2 №1", "amount_rub": 1000,
                                 "positions": [_pos(EARTH, 1000)]}]},
                      base_dir=tmp_path, out_dir=tmp_path / "mon", today=TODAY)
    assert CHECK_MATRIX_MISSING in _codes(plan)


# ─────────────────────────── сроки ───────────────────────────
def test_deadlines(tmp_path):
    missing = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1000,
                                "positions": [_pos(EARTH, 1000)]}])
    assert CHECK_DEADLINE_NOT_STATED in _codes(missing)
    assert missing["acts"][0]["days_to_deadline"] is None

    soon = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1000,
                             "submission_deadline": "2026-11-09",
                             "positions": [_pos(EARTH, 1000)]}])
    assert CHECK_DEADLINE_SOON in _codes(soon)

    missed = _plan(tmp_path, [{"act_id": "КС-2 №1", "amount_rub": 1000,
                               "submission_deadline": "2026-10-30",
                               "positions": [_pos(EARTH, 1000)]}])
    assert CHECK_DEADLINE_MISSED in _codes(missed)
    assert missed["acts"][0]["days_to_deadline"] == -7


# ─────────────────────────── демо, файлы, CLI, MCP ───────────────────────────
def test_demo_plan(tmp_path):
    config = json.loads((DEMO / "monthly" / "plan.json").read_text(encoding="utf-8"))
    plan = build_plan(config, base_dir=ROOT, out_dir=tmp_path / "mon", today=TODAY)
    totals = plan["totals"]
    assert plan["schema"] == SCHEMA
    assert (totals["acts"], totals["works"], totals["ready"], totals["not_ready"],
            totals["no_requirements"]) == (2, 10, 1, 8, 1)
    assert totals["money_ready_rub"] == 230_000.0
    assert totals["money_waiting_rub"] == 36_056_600.0
    assert totals["docs_missing"] == 31 and totals["docs_unsigned"] == 6
    assert totals["unclassified_works"] == 1
    assert verdict(plan)["ready_to_invoice"] is False
    assert "230 000.00 ₽ можно предъявлять" in plan["verdict"]["summary"]
    assert (tmp_path / "mon" / "monthly_План_ПТО-_ноябрь_2026-_ЖК_-Северный.docx").exists()
    ready = [w for act in plan["acts"] for w in act["works"] if w["status"] == WORK_READY]
    assert [w["name"] for w in ready] == ["Гидроизоляция и пароизоляция"]


def test_files_and_honest_words(tmp_path):
    from openpyxl import load_workbook

    config = json.loads((DEMO / "monthly" / "plan.json").read_text(encoding="utf-8"))
    plan = build_plan(config, base_dir=ROOT, out_dir=tmp_path / "mon", today=TODAY)
    files = write(plan, tmp_path / "mon2")
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Работы", "Что сделать", "Документы", "Блокеры"]
    markdown = to_markdown(plan).lower()
    for forbidden in ("все документы в наличии", "комплект полный", "замечаний нет"):
        assert forbidden not in markdown
    assert "не заявлено" in markdown or "требований в матрице нет" in markdown


def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_monthly(tmp_path):
    run = _cli("monthly", "--out", str(tmp_path / "mon"), "--base-dir", str(ROOT), "plan",
               "--config", str(DEMO / "monthly" / "plan.json"))
    assert run.returncode == 2, "в демо не все работы готовы"
    assert "видов работ: 10" in run.stdout
    assert "ГОТОВО К ПРЕДЪЯВЛЕНИЮ: 230 000.00 ₽" in run.stdout
    assert "держит документ: 36 056 600.00 ₽" in run.stdout
    assert "Что сделать" not in run.stdout or "что сделать в первую очередь" in run.stdout


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


def test_mcp_monthly_plan_is_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "monthly_plan")
    assert "config" in tool["inputSchema"]["properties"]
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "monthly_plan")
    assert schema["version"] == "2.7.0"
    assert any("матриц" in g for g in designed["guardrails"])


def test_mcp_monthly_returns_works(tmp_path):
    result = _mcp_call("monthly_plan",
                       {"config_path": str(DEMO / "monthly" / "plan.json"),
                        "base_dir": str(ROOT), "out_dir": str(tmp_path / "mon")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["kind"] == SCHEMA
    assert payload["ok"] is False
    assert payload["totals"]["money_waiting_rub"] == 36_056_600.0
    assert len(payload["actions"]) > 0
    assert Path(payload["files"]["xlsx"]).exists()
