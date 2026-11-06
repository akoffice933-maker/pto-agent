# -*- coding: utf-8 -*-
"""Страж-тесты накопительной ведомости и АТН: деньги без выдуманных цифр.

Правила ведомости:

1. сумма акта и сумма позиций показываются обе; расхождение — ошибка, а не «возьмём удобное»;
2. акт без позиций не даёт построчного подтверждения — это видно, а не растворяется в итоге;
3. дубль акта не удваивает сумму; одна позиция в двух актах одного периода — предупреждение;
4. разные единицы под одним кодом не складываются; сторно учитывается как корректировка;
5. `nan` и мусор в количестве не превращаются в ноль — позиция не входит в итог;
6. позиции вне сметы и превышение сметы помечаются как кандидаты в АТН.

Правила АТН:

7. позиция без основания (вид + ссылка) в акт не попадает;
8. без цены — сумма не считается (не ноль рублей);
9. повтор внутри акта не считается дважды, количество ≤ 0 отклоняется;
10. подтверждение только с ФИО и ролью; правка после проверки делает подпись недействительной.
"""
import json
import subprocess
import sys
from pathlib import Path

from pto_agent import atn as atn_mod
from pto_agent.cumulative import (CHECK_ACT_SUM, CHECK_BAD_NUMBER, CHECK_DUPLICATE_ACT,
                                  CHECK_NO_POSITIONS, CHECK_NOT_IN_ESTIMATE, CHECK_NEGATIVE,
                                  CHECK_OVER_ESTIMATE, CHECK_PERIOD_DOUBLE, CHECK_UNIT,
                                  build_ledger)
from pto_agent.fingerprint import check_signature

ROOT = Path(__file__).resolve().parents[1]

ESTIMATE = {
    "ФЕР 06-01-001-01": {"name": "Фундаментная плита", "unit": "м3", "qty": 100.0,
                         "price": 18500.0},
    "ФЕР 08-02-001-01": {"name": "Гидроизоляция", "unit": "м2", "qty": 500.0, "price": 780.0},
}


def _acts(*acts) -> dict:
    return {"object": "Школа", "contract": "Д-1", "acts": list(acts)}


def _act(act_id, period, positions, amount=None, **extra) -> dict:
    total = amount if amount is not None else round(
        sum(p.get("amount_rub", (p.get("qty") or 0) * (p.get("price") or 0)) for p in positions), 2)
    return {"act_id": act_id, "period": period, "amount_rub": total, "positions": positions,
            **extra}


def _codes(ledger) -> list[str]:
    return [c["code"] for c in ledger["checks"]]


# ─────────────────────────── ведомость ───────────────────────────
def test_ledger_totals_by_period_and_cumulative():
    data = _acts(
        _act("КС-2 №1", "сентябрь 2026",
             [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 100, "price": 18500}]),
        _act("КС-2 №2", "октябрь 2026",
             [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 150, "price": 18500}]),
    )
    ledger = build_ledger(data, estimate=ESTIMATE)
    row = ledger["rows"][0]
    assert ledger["periods"] == ["сентябрь 2026", "октябрь 2026"]
    assert row["qty_total"] == 250 and row["amount_total"] == 250 * 18500
    assert row["by_period"]["октябрь 2026"]["qty"] == 150
    assert row["remainder_qty"] == -150, "смета 100, предъявлено 250 — остаток отрицательный"
    assert CHECK_OVER_ESTIMATE in _codes(ledger)


def test_act_sum_vs_positions_mismatch_is_an_error():
    data = _acts(_act("КС-2 №1", "сентябрь 2026",
                      [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 1000}],
                      amount=999999))
    ledger = build_ledger(data, estimate=ESTIMATE)
    check = next(c for c in ledger["checks"] if c["code"] == CHECK_ACT_SUM)
    assert check["level"] == "error"
    assert "10 000,00" in check["message"] and "999 999,00" in check["message"]
    assert ledger["totals"]["difference_rub"] == 999999 - 10000


def test_act_without_positions_is_flagged_not_hidden():
    data = _acts(_act("КС-2 №38", "октябрь 2026", [], amount=34106600))
    ledger = build_ledger(data, estimate=ESTIMATE)
    assert CHECK_NO_POSITIONS in _codes(ledger)
    assert ledger["totals"]["amount_positions_rub"] == 0
    assert ledger["totals"]["difference_rub"] == 34106600


def test_duplicate_act_is_not_summed_twice():
    act = _act("КС-2 №1", "сентябрь 2026",
               [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 1000}])
    ledger = build_ledger(_acts(act, dict(act)), estimate=ESTIMATE)
    assert CHECK_DUPLICATE_ACT in _codes(ledger)
    assert ledger["totals"]["acts"] == 1
    assert ledger["rows"][0]["qty_total"] == 10


def test_same_position_in_two_acts_of_one_period_warns():
    data = _acts(
        _act("КС-2 №1", "сентябрь 2026",
             [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 1000}]),
        _act("КС-2 №2", "сентябрь 2026",
             [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 5, "price": 1000}]),
    )
    ledger = build_ledger(data, estimate=ESTIMATE)
    check = next(c for c in ledger["checks"] if c["code"] == CHECK_PERIOD_DOUBLE)
    assert "возможен двойной счёт" in check["message"]
    # сумма при этом видна как есть, а не «исправлена» молча
    assert ledger["rows"][0]["qty_total"] == 15


def test_same_code_different_units_are_not_added():
    data = _acts(_act("КС-2 №1", "сентябрь 2026",
                      [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 1000},
                       {"code": "ФЕР 06-01-001-01", "unit": "м2", "qty": 4, "price": 100}]))
    ledger = build_ledger(data, estimate=ESTIMATE)
    assert CHECK_UNIT in _codes(ledger)
    units = sorted(r["unit"] for r in ledger["rows"])
    assert units == ["м2", "м3"]
    assert all(r["qty_total"] in (10.0, 4.0) for r in ledger["rows"])


def test_nan_quantity_is_not_zero():
    data = _acts(_act("КС-2 №1", "сентябрь 2026",
                      [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": "nan",
                        "amount_rub": "nan"}], amount=0))
    ledger = build_ledger(data, estimate=ESTIMATE)
    assert CHECK_BAD_NUMBER in _codes(ledger)
    assert ledger["rows"] == [], "позиция с nan не должна попадать в ведомость как 0"
    assert ledger["totals"]["amount_positions_rub"] == 0


def test_negative_quantity_is_a_reversal_with_warning():
    data = _acts(_act("КС-2 №3", "октябрь 2026",
                      [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": -20,
                        "price": 18500}]))
    ledger = build_ledger(data, estimate=ESTIMATE)
    assert CHECK_NEGATIVE in _codes(ledger)
    assert ledger["rows"][0]["qty_total"] == -20
    assert ledger["rows"][0]["amount_total"] == -370000


def test_position_outside_estimate_is_candidate_for_atn():
    data = _acts(_act("КС-2 №1", "сентябрь 2026",
                      [{"code": "ДОП-1", "name": "Усиление проёма", "unit": "шт", "qty": 2,
                        "price": 50000}]))
    ledger = build_ledger(data, estimate=ESTIMATE)
    assert CHECK_NOT_IN_ESTIMATE in _codes(ledger)
    assert ledger["rows"][0]["source"] == "вне сметы"
    assert ledger["totals"]["positions_without_estimate"] == 1


def test_ledger_writes_three_files(tmp_path):
    from openpyxl import load_workbook

    from pto_agent.cumulative import write

    data = _acts(_act("КС-2 №1", "сентябрь 2026",
                      [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 1000}]))
    files = write(build_ledger(data, estimate=ESTIMATE), tmp_path / "led")
    assert Path(files["json"]).exists() and Path(files["markdown"]).exists()
    wb = load_workbook(files["xlsx"])
    assert wb.sheetnames == ["Ведомость", "Акты", "Проверки"]
    markdown = Path(files["markdown"]).read_text(encoding="utf-8")
    assert "Накопительная ведомость" in markdown
    assert "не подтверждает объёмы" in markdown


# ─────────────────────────── АТН ───────────────────────────
def _items(**overrides) -> list[dict]:
    base = {"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10,
            "basis": {"kind": "лист РД", "ref": "КР1, лист 12"}}
    return [{**base, **overrides}]


def test_atn_without_basis_is_not_in_the_act():
    items = _items(basis=None, price=1000)
    atn = atn_mod.build_atn(items, estimate=ESTIMATE)
    assert atn["totals"]["included"] == 0
    assert atn["totals"]["to_claim_rub"] == 0
    assert atn["totals"]["needs_basis"] == 1
    assert atn["needs_basis"][0]["amount_rub"] == 10000, "сумма видна, но в акт не входит"


def test_atn_basis_needs_both_kind_and_reference():
    for basis in ({"kind": "лист РД"}, {"ref": "КР1"}, {"kind": "", "ref": "КР1"}):
        atn = atn_mod.build_atn(_items(basis=basis, price=1000), estimate=ESTIMATE)
        assert atn["totals"]["needs_basis"] == 1, f"основание {basis} не должно считаться"


def test_atn_without_price_does_not_count_as_zero():
    atn = atn_mod.build_atn(_items(code="МОНТАЖ-Х", unit="шт", qty=5),
                            estimate=ESTIMATE)
    assert atn["totals"]["to_claim_rub"] == 0
    assert atn["totals"]["no_price"] == 1
    assert atn["no_price"][0]["amount_rub"] is None


def test_atn_price_taken_from_estimate_by_code():
    atn = atn_mod.build_atn(_items(qty=10), estimate=ESTIMATE, ledger={"rows": []})
    assert atn["positions"][0]["price"] == 18500
    assert atn["positions"][0]["price_source"] == "смета по коду"
    assert atn["totals"]["to_claim_rub"] == 185000


def test_atn_repeat_inside_act_is_not_double_counted():
    items = _items(price=1000) + _items(price=1000)
    atn = atn_mod.build_atn(items, estimate=ESTIMATE)
    assert atn["totals"]["included"] == 1
    assert atn["totals"]["rejected"] == 1
    assert "уже есть в этом акте" in atn["rejected"][0]["why"]


def test_atn_rejects_non_positive_and_non_numeric_qty():
    items = _items(price=1000, qty=0) + _items(price=1000, qty=-5) + \
            _items(code="X", price=1000, qty="nan")
    atn = atn_mod.build_atn(items, estimate=ESTIMATE)
    assert atn["totals"]["to_claim_rub"] == 0
    assert atn["totals"]["rejected"] == 3


def test_atn_flags_excess_over_estimate():
    ledger = {"rows": [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty_total": 95,
                        "estimate_qty": 100, "remainder_qty": 5, "acts": ["КС-2 №1"]}]}
    atn = atn_mod.build_atn(_items(qty=20, price=1000), estimate=ESTIMATE, ledger=ledger)
    position = atn["positions"][0]
    assert position["excess_qty"] == 15
    assert "превышает остаток по смете" in position["note"]
    assert atn["totals"]["excess_vs_estimate_rub"] == 15000


def test_atn_confirm_requires_name_and_role():
    atn = atn_mod.build_atn(_items(price=1000), estimate=ESTIMATE, number="АТН-1")
    assert atn["status"] == atn_mod.STATUS_DRAFT
    for by, role in (("", "инженер ПТО"), ("Иванов И.И.", ""), (" ", " ")):
        refused = atn_mod.confirm(atn, by=by, role=role)
        assert refused["confirm_problems"], (by, role)
        assert refused["status"] == atn_mod.STATUS_DRAFT


def test_atn_confirm_marks_status_and_fingerprint():
    atn = atn_mod.build_atn(_items(price=1000), estimate=ESTIMATE, number="АТН-1")
    confirmed = atn_mod.confirm(atn, by="Иванов И.И.", role="инженер ПТО")
    assert confirmed["status"] == atn_mod.STATUS_CONFIRMED
    assert confirmed["confirmed_by"]["by"] == "Иванов И.И."
    assert check_signature(confirmed)["valid"] is True

    confirmed["positions"][0]["qty"] = 999                    # правка после проверки
    check = check_signature(confirmed)
    assert check["changed_after_confirm"] is True and check["valid"] is False


def test_atn_confirm_empty_act_is_refused():
    atn = atn_mod.build_atn(_items(basis=None), estimate=ESTIMATE)
    refused = atn_mod.confirm(atn, by="Иванов И.И.", role="инженер ПТО")
    assert any("нет ни одной позиции" in p for p in refused["confirm_problems"])


def test_atn_writes_docx_with_basis_and_signature_place(tmp_path):
    from docx import Document

    atn = atn_mod.build_atn(_items(price=1000), estimate=ESTIMATE, number="АТН-1",
                            customer="ООО «Заказчик»")
    files = atn_mod.write(atn, tmp_path / "atn")
    assert Path(files["json"]).name == "atn_АТН-1.json"
    doc = Document(files["docx"])
    text = "\n".join(p.text for p in doc.paragraphs)
    text += "\n" + "\n".join(cell.text for table in doc.tables
                              for row in table.rows for cell in row.cells)
    assert "КР1, лист 12" in text, "основание должно быть в документе"
    assert "Согласовано (заказчик)" in text
    assert "не согласовывает" in text


# ─────────────────────────── CLI ───────────────────────────
def _cli(*argv):
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def _acts_file(tmp_path) -> Path:
    path = tmp_path / "acts.json"
    path.write_text(json.dumps(_acts(
        _act("КС-2 №35", "сентябрь 2026",
             [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 100, "price": 18500}]),
        _act("КС-2 №38", "октябрь 2026", [], amount=34106600)), ensure_ascii=False),
        encoding="utf-8")
    return path


def test_cli_cumulative_reports_difference_and_exit_code(tmp_path):
    run = _cli("cumulative", "--acts", str(_acts_file(tmp_path)), "--out", str(tmp_path / "cum"))
    assert run.returncode == 0, run.stderr
    assert "расхождение" in run.stdout
    assert "без построчного подтверждения" in run.stdout
    assert (tmp_path / "cum" / "cumulative.xlsx").exists()

    # --strict превращает предупреждение в провал
    strict = _cli("cumulative", "--acts", str(_acts_file(tmp_path)), "--out",
                  str(tmp_path / "cum2"), "--strict")
    assert strict.returncode == 2


def test_cli_cumulative_error_is_exit_2(tmp_path):
    path = tmp_path / "acts.json"
    path.write_text(json.dumps(_acts(_act(
        "КС-2 №1", "сентябрь 2026",
        [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 1000}], amount=1))),
        encoding="utf-8")
    run = _cli("cumulative", "--acts", str(path), "--out", str(tmp_path / "cum"))
    assert run.returncode == 2
    assert "ACT_SUM_MISMATCH" in run.stdout


def test_cli_atn_draft_and_confirm(tmp_path):
    items = tmp_path / "items.json"
    items.write_text(json.dumps([
        {"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 18500,
         "basis": {"kind": "лист РД", "ref": "КР1, лист 12"}},
        {"code": "БЕЗ-ОСНОВАНИЯ", "unit": "шт", "qty": 3, "price": 100},
    ], ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "atn"
    run = _cli("atn", "--out", str(out), "draft", "--items", str(items), "--number", "АТН-1")
    assert run.returncode == 2, "в акте есть позиция без основания — это не «ок»"
    assert "требует основания: 1 поз." in run.stdout
    assert (out / "atn_АТН-1.docx").exists()

    anonymous = _cli("atn", "--out", str(out), "confirm", "--atn",
                     str(out / "atn_АТН-1.json"), "--by", " ", "--role", " ")
    assert anonymous.returncode == 2 and "ОСТАНОВЛЕНО" in anonymous.stdout

    ok = _cli("atn", "--out", str(out), "confirm", "--atn", str(out / "atn_АТН-1.json"),
              "--by", "Иванов И.И.", "--role", "инженер ПТО")
    assert ok.returncode == 0, ok.stderr
    saved = json.loads((out / "atn_АТН-1.json").read_text(encoding="utf-8"))
    assert saved["status"] == atn_mod.STATUS_CONFIRMED


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


def test_mcp_tools_registered_and_designed():
    from pto_agent.cad.mcp_server import TOOLS

    names = [t["name"] for t in TOOLS]
    assert "cumulative_ledger" in names and "atn_draft" in names
    schema = json.loads((ROOT / "prompts" / "tools_schema.json").read_text(encoding="utf-8"))
    designed = {t["name"]: t for t in schema["tools"]}
    for tool in ("cumulative_ledger", "atn_draft"):
        assert tool in designed, f"{tool} нет в проектной схеме"
        assert designed[tool]["guardrails"], f"у {tool} нет guardrails"


def test_mcp_cumulative_ledger_returns_checks(tmp_path):
    acts = _acts_file(tmp_path)
    result = _mcp_call("cumulative_ledger", {"acts_path": str(acts),
                                             "out_dir": str(tmp_path / "cum")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["totals"]["acts"] == 2
    assert any(c["code"] == CHECK_NO_POSITIONS for c in payload["checks"])
    assert Path(payload["files"]["xlsx"]).exists()


def test_mcp_atn_draft_keeps_unbacked_positions_out(tmp_path):
    result = _mcp_call("atn_draft", {
        "items": [
            {"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 18500,
             "basis": {"kind": "лист РД", "ref": "КР1, лист 12"}},
            {"code": "БЕЗ-ОСНОВАНИЯ", "unit": "шт", "qty": 3, "price": 100},
        ],
        "number": "АТН-7", "out_dir": str(tmp_path / "atn")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["totals"]["to_claim_rub"] == 185000
    assert payload["totals"]["needs_basis"] == 1
    assert payload["needs_basis"][0]["code"] == "БЕЗ-ОСНОВАНИЯ"
    assert Path(payload["files"]["docx"]).exists()


def test_mcp_atn_confirm_requires_human(tmp_path):
    draft = _mcp_call("atn_draft", {
        "items": [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 18500,
                   "basis": {"kind": "лист РД", "ref": "КР1"}}],
        "number": "АТН-8", "out_dir": str(tmp_path / "atn")})
    path = json.loads(draft["content"][0]["text"])["files"]["json"]
    result = _mcp_call("atn_draft", {"action": "confirm", "atn_path": path, "by": "",
                                     "role": "", "out_dir": str(tmp_path / "atn")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert any("анонимное подтверждение" in p for p in payload["problems"])
