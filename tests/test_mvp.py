# -*- coding: utf-8 -*-
"""Тесты MVP «ИИ-агент ПТО». Запуск: python -m pytest tests -q"""
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.audit import AuditLog                       # noqa: E402
from pto_agent.export import export_ks2                     # noqa: E402
from pto_agent.llm import route                             # noqa: E402
from pto_agent.norms import NormIndex, cite_verify, load_norms  # noqa: E402
from pto_agent.parsers import (norm_unit, read_closed,      # noqa: E402
                               read_estimate, read_journal)
from pto_agent.reconcile import reconcile                   # noqa: E402

DATA = ROOT / "data" / "demo"


@pytest.fixture(scope="module")
def demo():
    items = read_estimate(DATA / "smeta_v1.xlsx")
    entries = read_journal(DATA / "journal_2026_10.xlsx")
    closed = read_closed(DATA / "acts_previous.xlsx")
    rec = reconcile(items, entries, closed,
                    object_name="ЖК «Северный», корпус 2",
                    period=(min(e.date for e in entries), max(e.date for e in entries)))
    return rec, items, entries, closed


@pytest.fixture(scope="module")
def expected():
    return json.loads((DATA / "expected.json").read_text(encoding="utf-8"))


# ── 1. Полнота выявления: агент обязан найти каждое заложенное расхождение ───
def test_finds_all_expected_discrepancies(demo, expected):
    rec, *_ = demo
    found = {(d.type, d.code) for d in rec.discrepancies}
    missed = [(e["type"], e["code"]) for e in expected["expected_discrepancies"]
              if (e["type"], e["code"]) not in found]
    assert not missed, f"не найдены расхождения: {missed}"
    assert len(rec.discrepancies) == len(expected["expected_discrepancies"])


def test_no_false_positives_on_clean_positions(demo):
    rec, *_ = demo
    flagged = {d.code for d in rec.discrepancies}
    # гидроизоляция и кровля оформлены корректно — замечаний быть не должно
    assert "ФЕР 08-02-001-01" not in flagged
    assert "ФЕР 12-01-034-02" not in flagged


# ── 2. Дедупликация ────────────────────────────────────────────────────────
def test_duplicate_row_excluded_from_volume(demo):
    rec, *_ = demo
    line = next(l for l in rec.lines if l.item.code == "ФЕР 06-01-001-01")
    assert line.duplicates == 1
    assert line.qty_journal == pytest.approx(600 + 480 + 160)
    dup = next(d for d in rec.discrepancies if d.type == "DUPLICATE")
    assert dup.values["qty_after_dedup"] == pytest.approx(1240)


# ── 3. Единицы измерения ───────────────────────────────────────────────────
def test_unit_mismatch_blocks_act_line(demo):
    rec, *_ = demo
    line = next(l for l in rec.lines if l.item.code == "ФЕР 11-01-011-01")
    assert line.unit_mismatch
    assert line not in rec.lines_with_qty
    assert rec.has_type("UNIT_MISMATCH", "ФЕР 11-01-011-01")


def test_unit_normalization():
    assert norm_unit("м³") == norm_unit("м3") == "м3"
    assert norm_unit("м2 ") == "м2"
    assert norm_unit("п.м.") == "м"


# ── 4. Арифметика акта (кодом, а не моделью) ───────────────────────────────
def test_act_amounts_are_computed_from_quantities(demo):
    rec, *_ = demo
    manual = sum(l.qty_journal * l.item.price for l in rec.lines_with_qty)
    assert rec.amount_act == pytest.approx(round(manual, 2))
    assert rec.amount_act_safe <= rec.amount_act


def test_remainder_formula(demo):
    rec, *_ = demo
    for line in rec.lines:
        assert line.qty_remainder == pytest.approx(
            line.item.qty - line.qty_prev_closed - line.qty_journal, abs=1e-4)
    # позиция с повторным закрытием уходит в минус — это видно в отчёте
    pipes = next(l for l in rec.lines if l.item.code == "ФЕР 16-02-003-01")
    assert pipes.qty_remainder < 0


# ── 5. Нормативы: цитата обязательна и проверяется дословно ────────────────
@pytest.fixture(scope="module")
def index():
    return NormIndex(load_norms(DATA / "norms"))


def test_norm_answer_returns_verified_citation(index):
    ans = index.ask("минимальная толщина защитного слоя бетона для фундамента")
    assert ans.answer_type in ("answer", "ambiguous")
    assert ans.citations and ans.citations[0]["doc"] == "СП 63.13330.2018"
    assert ans.citations[0]["clause"].startswith("10.3")
    assert ans.verified and all(c["verified"] for c in ans.citations)


def test_norm_answer_blocks_volume_over_estimate():
    ans = NormIndex(load_norms(DATA / "norms")).ask(
        "можно ли включать в акт объёмы сверх сметы")
    assert ans.citations[0]["doc"] == "СП 48.13330.2019"
    assert ans.citations[0]["clause"] == "10.4.2"


def test_fabricated_quote_fails_verification(index):
    real = next(c for c in index.clauses if c.clause == "10.3.2")
    fake = "Минимальная толщина защитного слоя бетона принимается равной 55 мм для всех конструкций"
    assert cite_verify(fake, real.body) is False
    assert cite_verify(real.body[:120], real.body) is True


def test_norm_edition_applicability(index):
    """Редакция, вступившая в силу позже даты проверки, не должна попадать в ответ."""
    hits = index.search("толщина защитного слоя бетона", as_of=date(2018, 1, 1))
    assert all(c.doc.designation != "СП 63.13330.2018" for c, _ in hits)

    early = index.ask("толщина защитного слоя бетона", as_of=date(2010, 1, 1))
    assert early.answer_type == "not_found" and not early.citations


def test_norm_not_found_is_honest(index):
    ans = index.ask("требования к отделке фасадов керамогранитом")
    assert ans.answer_type in ("not_found", "ambiguous")


# ── 6. Журнал аудита ───────────────────────────────────────────────────────
def test_audit_chain_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("agent:parser", "agent", "documents.read", "dataset", {"n": 3})
    log.append("Иванов И.И.", "human", "act.approved", "ks2_draft", {"comment": "ок"})
    ok, count, _ = log.verify()
    assert ok and count == 2

    lines = log.path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["payload"]["n"] = 999
    lines[0] = json.dumps(rec, ensure_ascii=False)
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _, msg = log.verify()
    assert not ok and "изменена" in msg


# ── 7. Маршрутизация моделей по классу данных ──────────────────────────────
def test_commercial_secret_never_leaves_premise():
    res = route("ks2_validate", data_class="commercial_secret", complexity="high")
    assert res["provider"] == "local-qwen-32b"
    assert res["rule"] == "personal-data-local-only"


def test_internal_heavy_task_goes_to_cloud_rf():
    res = route("ks2_validate", data_class="internal", complexity="high")
    assert res["provider"] == "gigachat-pro"


def test_routine_extraction_is_local():
    assert route("doc_classify", data_class="internal")["provider"] == "local-qwen-32b"


# ── 8. Экспорт КС-2 ────────────────────────────────────────────────────────
def test_ks2_export_writes_totals(demo, tmp_path):
    """КС-2 идёт тем же механизмом бланков, что КС-3/КС-6а/М-29: суммы — числами."""
    rec, *_ = demo
    path = export_ks2(rec, tmp_path / "ks2.xlsx", vat_rate=20.0)
    ws = load_workbook(path, data_only=True)["КС-2"]
    assert "ЧЕРНОВИК" in ws["A1"].value  # бланк помечен, как и остальные формы
    assert "КС-2" in ws["A2"].value
    numbers = [ws.cell(r, 2).value for r in range(1, ws.max_row + 1)
               if isinstance(ws.cell(r, 2).value, (int, float))]
    # и полный объём по журналу, и сумма к предъявлению, и заблокированное — числами
    assert any(abs(v - rec.amount_act) < 0.01 for v in numbers)
    assert any(abs(v - rec.amount_act_safe) < 0.01 for v in numbers)
    assert any(abs(v - rec.amount_blocked) < 0.01 for v in numbers)
