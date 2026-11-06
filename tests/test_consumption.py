# -*- coding: utf-8 -*-
"""Страж-тесты норм расхода, списания, лимитов и давальческих материалов.

Правила, которые проверяются:

1. потребность = объём работ × норма (числа считаются, а не берутся «на глаз»);
2. норма без документа, пункта или редакции не применяется — строка уходит в «нет источника»;
3. норма с источником, которого нет в базе агента, даёт «справочно» и не входит в
   подтверждённый итог, пока норму не примет человек с ФИО и ролью;
4. перерасход сверх нормы без причины — ошибка; с причиной — предупреждение с суммой;
5. списание без нормы не сравнивается с нулём — это отдельная ошибка;
6. факт закупки сверх лимита — ошибка с числами; нет цены — лимит в деньгах не считается;
7. давальческие: остаток, отрицательный остаток, двойной счёт с собственными закупками;
8. `nan` и мусор в количестве не становятся нулём.
"""
import json
import subprocess
import sys
from pathlib import Path

from pto_agent.consumption import (CHECK_BAD_NUMBER, CHECK_DV_DOUBLE, CHECK_DV_NEGATIVE,
                                   CHECK_DV_REMAINDER, CHECK_NO_NORM, CHECK_NO_PRICE,
                                   CHECK_NORM_NO_SOURCE, CHECK_NORM_UNVERIFIED,
                                   CHECK_NOT_WRITTEN_OFF, CHECK_OVER_LIMIT,
                                   CHECK_OVER_NO_REASON, CHECK_OVER_WITH_REASON,
                                   CHECK_PURCHASE_NO_NEED, CHECK_WRITE_OFF_NO_NORM,
                                   build_need, build_report, check_daval, check_limits,
                                   check_writeoff, load_table, to_markdown, write)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo" / "consumption"

WORKS = [{"code": "ФЕР 06-01-001-01", "name": "Фундаментная плита", "unit": "м3", "qty": 200}]


def _norm(source=None, **overrides) -> dict:
    base = {"work_code": "ФЕР 06-01-001-01", "material_code": "Бетон B25", "unit": "м3",
            "norm": 1.02, "price": 6200,
            "source": {"doc": "ГЭСН 81-02-06-2001", "point": "06-01-001-01",
                       "edition": "ред. 2022"}}
    if source is not None:
        base["source"] = source
    base.update(overrides)
    return base


def _codes(checks) -> list[str]:
    return [c["code"] for c in checks]


# ─────────────────────────── потребность ───────────────────────────
def test_need_is_volume_times_norm():
    need = build_need(WORKS, [_norm()])
    row = need["rows"][0]
    assert row["need_qty"] == 204.0, "200 м3 × 1,02 = 204 м3"
    assert row["need_rub"] == 204 * 6200


def test_loss_coefficient_only_when_it_is_in_the_norm():
    plain = build_need(WORKS, [_norm()])["rows"][0]["need_qty"]
    with_loss = build_need(WORKS, [_norm(loss_pct=3)])["rows"][0]["need_qty"]
    assert plain == 204.0 and with_loss == 210.12


def test_norm_without_source_is_not_applied():
    for source in ({"doc": "ГЭСН 81-02-06-2001", "edition": "ред. 2022"},      # нет пункта
                   {"point": "06-01-001-01", "edition": "ред. 2022"},          # нет документа
                   {"doc": "ГЭСН 81-02-06-2001", "point": "06-01-001-01"},     # нет редакции
                   {}):
        need = build_need(WORKS, [_norm(source=source)])
        assert need["rows"] == [], f"норма без источника {source} не должна давать потребность"
        assert CHECK_NORM_NO_SOURCE in _codes(need["checks"])
        assert need["norms"]["no_source"] == 1


def test_norm_with_unknown_base_is_reference_only():
    need = build_need(WORKS, [_norm()])
    row = need["rows"][0]
    assert CHECK_NORM_UNVERIFIED in _codes(need["checks"])
    assert row["need_qty"] == 204.0, "справочная потребность считается и показывается"
    assert row["need_confirmed_qty"] == 0.0
    assert need["totals"]["need_confirmed_rub"] == 0.0
    assert need["totals"]["need_reference_rub"] > 0


def test_human_can_accept_norms_and_stays_responsible():
    need = build_need(WORKS, [_norm()], confirmed_by="Иванов И.И.", role="инженер ПТО",
                      confirm_norms=True)
    assert need["rows"][0]["need_confirmed_qty"] == 204.0
    assert need["totals"]["need_confirmed_rub"] == 204 * 6200
    assert need["confirmed_by"] == {"by": "Иванов И.И.", "role": "инженер ПТО"}


def test_work_without_norm_says_so_does_not_guess():
    need = build_need([{"code": "ФЕР 99-99-999-99", "unit": "м", "qty": 100}], [_norm()])
    assert CHECK_NO_NORM in _codes(need["checks"])
    assert need["rows"] == []


def test_bad_norm_value_is_error_not_zero():
    need = build_need(WORKS, [_norm(norm="nan")])
    assert CHECK_BAD_NUMBER in _codes(need["checks"])
    assert need["rows"] == []


def test_no_price_means_no_money_not_zero():
    need = build_need(WORKS, [_norm(price=None)])
    assert need["rows"][0]["need_rub"] is None
    assert CHECK_NO_PRICE in _codes(need["checks"])


# ─────────────────────────── списание ───────────────────────────
def test_over_norm_without_reason_is_error():
    need = build_need(WORKS, [_norm()])
    result = check_writeoff(need, [{"code": "Бетон B25", "unit": "м3", "qty": 210,
                                    "document": "М-29 №4"}])
    check = next(c for c in result["checks"] if c["code"] == CHECK_OVER_NO_REASON)
    assert check["level"] == "error"
    assert "37 200,00" in check["message"], "перерасход 6 м3 × 6200 ₽"
    assert result["rows"][0]["status"] == "перерасход без причины"


def test_over_norm_with_reason_is_warning_with_money():
    need = build_need(WORKS, [_norm()])
    result = check_writeoff(need, [{"code": "Бетон B25", "unit": "м3", "qty": 210,
                                    "document": "М-29 №4", "reason": "потери при подаче"}])
    check = next(c for c in result["checks"] if c["code"] == CHECK_OVER_WITH_REASON)
    assert check["level"] == "warning" and "потери при подаче" in check["message"]
    assert result["rows"][0]["deviation_rub"] == 6 * 6200


def test_writeoff_without_norm_is_not_compared_with_zero():
    need = build_need(WORKS, [_norm()])
    result = check_writeoff(need, [{"code": "Керамзит", "unit": "м3", "qty": 12,
                                    "document": "М-29 №4"}])
    check = next(c for c in result["checks"] if c["code"] == CHECK_WRITE_OFF_NO_NORM)
    assert check["level"] == "error"
    assert "проверить нечем" in check["message"]


def test_missing_writeoff_is_flagged():
    need = build_need(WORKS, [_norm()])
    result = check_writeoff(need, [])
    assert CHECK_NOT_WRITTEN_OFF in _codes(result["checks"])
    assert result["rows"][0]["status"] == "не списано"


def test_nan_writeoff_quantity_is_error():
    need = build_need(WORKS, [_norm()])
    result = check_writeoff(need, [{"code": "Бетон B25", "unit": "м3", "qty": None}])
    assert CHECK_BAD_NUMBER in _codes(result["checks"])


# ─────────────────────────── лимит ───────────────────────────
def test_purchase_over_limit_is_error_with_numbers():
    need = build_need(WORKS, [_norm()])
    result = check_limits(need, [{"code": "Бетон B25", "unit": "м3", "qty": 230,
                                  "price": 6200, "document": "УПД №118"}])
    check = next(c for c in result["checks"] if c["code"] == CHECK_OVER_LIMIT)
    assert check["level"] == "error"
    assert "превышение 26" in check["message"] and "161 200,00" in check["message"]
    assert result["rows"][0]["over_limit_qty"] == 26


def test_purchase_within_limit_is_fine():
    need = build_need(WORKS, [_norm()])
    result = check_limits(need, [{"code": "Бетон B25", "unit": "м3", "qty": 200, "price": 6200}])
    assert CHECK_OVER_LIMIT not in _codes(result["checks"])


def test_purchase_without_need_is_flagged():
    need = build_need(WORKS, [_norm()])
    result = check_limits(need, [{"code": "Песок", "unit": "м3", "qty": 40, "price": 900}])
    assert CHECK_PURCHASE_NO_NEED in _codes(result["checks"])


def test_limit_in_money_is_not_zero_without_price():
    need = build_need(WORKS, [_norm(price=None)])
    result = check_limits(need, [{"code": "Бетон B25", "unit": "м3", "qty": 10}])
    assert result["rows"][0]["limit_rub"] is None
    assert CHECK_NO_PRICE in _codes(result["checks"])


# ─────────────────────────── давальческие ───────────────────────────
def test_daval_remainder_and_negative_balance():
    result = check_daval([
        {"code": "Арматура A500", "unit": "т", "received": 5, "used": 3, "returned": 0},
        {"code": "Труба Д50", "unit": "м", "received": 100, "used": 120, "returned": 0},
    ])
    first, second = result["rows"]
    assert first["remainder"] == 2 and first["status"] == "остаток не возвращён"
    assert second["remainder"] == -20 and second["status"] == "расход больше прихода"
    assert CHECK_DV_REMAINDER in _codes(result["checks"])
    assert CHECK_DV_NEGATIVE in _codes(result["checks"])


def test_daval_returned_remainder_is_fine():
    result = check_daval([{"code": "Арматура A500", "unit": "т", "received": 5, "used": 3,
                           "returned": 2}])
    assert result["rows"][0]["remainder"] == 0
    assert result["rows"][0]["status"] == "сходится"
    assert CHECK_DV_REMAINDER not in _codes(result["checks"])


def test_daval_material_in_own_purchases_is_double_count():
    result = check_daval([{"code": "Арматура A500", "unit": "т", "received": 5, "used": 5,
                           "returned": 0}],
                         purchases=[{"code": "Арматура A500", "unit": "т", "qty": 16}])
    check = next(c for c in result["checks"] if c["code"] == CHECK_DV_DOUBLE)
    assert check["level"] == "error" and "двойной счёт" in check["message"]
    assert result["rows"][0]["status"] == "двойной счёт"


def test_daval_without_numbers_is_not_a_balance():
    result = check_daval([{"code": "Арматура A500", "unit": "т", "received": None, "used": 3}])
    assert CHECK_BAD_NUMBER in _codes(result["checks"])
    assert result["rows"][0]["status"] == "нет данных"
    assert "remainder" not in result["rows"][0]


# ─────────────────────────── отчёт и файлы ───────────────────────────
def test_report_totals_and_checks_are_deduplicated():
    report = build_report(
        WORKS, [_norm(price=None)],
        writeoffs=[{"code": "Бетон B25", "unit": "м3", "qty": 210}],
        purchases=[{"code": "Бетон B25", "unit": "м3", "qty": 230}],
        daval=[{"code": "Арматура A500", "unit": "т", "received": 5, "used": 3}])
    codes = _codes(report["checks"])
    pairs = [(c["code"], c["message"]) for c in report["checks"]]
    assert len(pairs) == len(set(pairs)), "одинаковые находки не дублируются в отчёте"
    assert codes.count(CHECK_NO_PRICE) == 2, ("цена отсутствует у потребности и у лимита — "
                                             "это две разные находки с разным текстом")
    assert CHECK_OVER_NO_REASON in codes and CHECK_OVER_LIMIT in codes
    assert report["totals"]["errors"] >= 2
    assert report["totals"]["over_norms"] == 1
    assert report["totals"]["daval_remainder"] == 2


def test_report_writes_three_files_and_honest_words(tmp_path):
    from openpyxl import load_workbook

    report = build_report(WORKS, [_norm()], object_name="Школа")
    files = write(report, tmp_path / "cons")
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Потребность", "Списание", "Лимит", "Давальческие", "Проверки"]
    markdown = Path(files["markdown"]).read_text(encoding="utf-8")
    assert "справочная" in markdown
    for forbidden in ("утверждено", "согласовано с заказчиком", "готова к применению"):
        assert forbidden not in markdown.lower()
    assert "принимает человек" in markdown


def test_demo_csv_columns_are_read(tmp_path):
    writeoffs, problems = load_table(DEMO / "writeoffs.csv", "writeoff")
    assert not problems
    assert writeoffs[0]["qty"] == 210
    assert writeoffs[0]["reason"] == "потери при подаче бетононасосом"
    daval, _ = load_table(DEMO / "daval.csv", "daval")
    assert daval[0]["received"] == 5 and daval[0]["used"] == 3


# ─────────────────────────── CLI ───────────────────────────
def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_consumption_on_demo_set(tmp_path):
    run = _cli("consumption", "--works", str(DEMO / "works.json"),
               "--norms", str(DEMO / "norms.json"),
               "--writeoffs", str(DEMO / "writeoffs.csv"),
               "--purchases", str(DEMO / "purchases.csv"),
               "--daval", str(DEMO / "daval.csv"),
               "--out", str(tmp_path / "cons"))
    assert run.returncode == 2, "в демо-наборе есть ошибки (перерасход без причины, двойной счёт)"
    assert "OVER_WITHOUT_REASON" in run.stdout
    assert "DAVAL_DOUBLE_ACCOUNT" in run.stdout
    assert (tmp_path / "cons" / "consumption.xlsx").exists()


def test_cli_confirm_norms_requires_human(tmp_path):
    works = tmp_path / "works.json"
    works.write_text(json.dumps({"works": WORKS}, ensure_ascii=False), encoding="utf-8")
    norms = tmp_path / "norms.json"
    norms.write_text(json.dumps({"norms": [_norm()]}, ensure_ascii=False), encoding="utf-8")

    anonymous = _cli("consumption", "--works", str(works), "--norms", str(norms),
                     "--confirm-norms", "--confirm-norms", "--out", str(tmp_path / "a"))
    assert anonymous.returncode == 2
    assert "норму принимает человек" in anonymous.stderr

    with_human = _cli("consumption", "--works", str(works), "--norms", str(norms),
                      "--confirm-norms", "--by", "Иванов И.И.", "--role", "инженер ПТО",
                      "--out", str(tmp_path / "b"))
    assert with_human.returncode == 0, with_human.stderr
    assert "нормы приняты для расчёта человеком: Иванов И.И." in with_human.stdout


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


def test_mcp_consumption_tool_is_designed():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "consumption_report")
    assert "confirm_norms" in tool["inputSchema"]["properties"]
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = next(t for t in schema["tools"] if t["name"] == "consumption_report")
    assert any("норма без документа" in g for g in designed["guardrails"])


def test_mcp_consumption_returns_checks_and_marks_norms(tmp_path):
    result = _mcp_call("consumption_report", {
        "works_path": str(DEMO / "works.json"), "norms_path": str(DEMO / "norms.json"),
        "daval_path": str(DEMO / "daval.csv"), "out_dir": str(tmp_path / "cons")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False, "в демо-нормах есть норма без пункта — это ошибка"
    assert payload["norms"]["no_source"] == 1
    assert payload["totals"]["need_confirmed_rub"] == 0
    assert payload["totals"]["need_reference_rub"] > 0
    assert Path(payload["files"]["xlsx"]).exists()


def test_mcp_confirm_norms_without_human_is_refused(tmp_path):
    result = _mcp_call("consumption_report", {
        "works_path": str(DEMO / "works.json"), "norms_path": str(DEMO / "norms.json"),
        "confirm_norms": True, "out_dir": str(tmp_path / "cons")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert any("норму принимает человек" in p for p in payload["problems"])
