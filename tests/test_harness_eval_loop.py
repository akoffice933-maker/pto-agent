# -*- coding: utf-8 -*-
"""Harness, eval, loop: три слоя, которые нельзя проверить «на глаз».

Тесты держат ровно те свойства, ради которых слои заведены:
манифест не врёт, eval честно различает pass / fail / skip,
провал становится узлом графа один раз, узел закрывается только проверкой,
цикл останавливается по понятной причине.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pto_agent import backlog as bl
from pto_agent import eval as ev
from pto_agent import harness
from pto_agent import loop

ROOT = Path(__file__).resolve().parents[1]


# ─────────────────────────── harness ───────────────────────────
def test_manifest_paths_exist():
    """Каждый модуль и каждый тест из манифеста существует: манифест — не декларация."""
    man = harness.manifest()
    missing = [t["module"] for t in man["tools"]
               if t["module"] != "—" and not (ROOT / t["module"]).exists()]
    missing += [b["enforced_in"] for b in man["boundaries"]
                if not (ROOT / b["enforced_in"]).exists()]
    missing += [b["test"] for b in man["boundaries"] if not (ROOT / b["test"]).exists()]
    assert not missing, f"в манифесте пути, которых нет: {missing}"


def test_every_tool_has_level_proof_and_reason():
    for t in harness.manifest()["tools"]:
        assert t["level"] in {"auto", "confirm", "human", "never"}
        assert t["proof"].strip(), t["name"]
        assert t["why"].strip(), t["name"]


def test_dangerous_actions_are_not_auto():
    """Подпись, проводки и внешняя отправка не могут быть «auto»."""
    by_name = {t["name"]: t["level"] for t in harness.manifest()["tools"]}
    assert by_name["подпись документа"] == "human"
    assert by_name["отправка контрагенту"] == "never"
    assert by_name["проводка в учёте"] == "confirm"


def test_digest_is_stable_and_counts_match_body():
    """Дайджест не «плавает» между вызовами, а счётчики совпадают с телом манифеста."""
    a, b = harness.manifest(), harness.manifest()
    assert a["digest"] == b["digest"]
    assert a["counts"]["tools"] == len(a["tools"])
    assert a["counts"]["boundaries"] == len(a["boundaries"])
    assert sum(a["counts"]["by_level"].values()) == len(a["tools"])


# ─────────────────────────── eval ───────────────────────────
def test_eval_arithmetic_catches_wrong_sum():
    bad = {"lines": [{"qty_claim": 2, "price": 100}], "amount_act_safe": 999,
           "vat_rate": 20, "vat_claimable": 0}
    res = ev.check_ks2_arithmetic(bad)
    assert res["status"] == ev.FAIL
    assert "≠" in res["why"]


def test_eval_arithmetic_passes_on_consistent_data():
    good = {"lines": [{"qty_claim": 2, "price": 100}], "amount_act_safe": 200,
            "vat_rate": 20, "vat_claimable": 40}
    assert ev.check_ks2_arithmetic(good)["status"] == ev.PASS


def test_eval_marks_missing_input_as_skip_not_pass():
    """Нет данных — это skip. Превращать его в «ок» значит врать в отчёте."""
    assert ev.check_ks2_arithmetic({})["status"] == ev.SKIP
    assert ev.check_honesty_no_invented_numbers(None)["status"] == ev.SKIP


def test_honesty_fails_when_skipped_check_is_dressed_as_ready():
    review = {"verdict": {"status": "ready"},
              "checked": [{"id": "ks2", "status": "skipped", "money": {}}]}
    assert ev.check_honesty_no_invented_numbers(review)["status"] == ev.FAIL


def test_refusals_are_real():
    """Агент обязан отказать там, где отказ заявлен, а не только в документации."""
    assert ev.check_refusals()["status"] == ev.PASS


def test_signature_requires_person_and_role():
    res = ev.check_signature_needs_human()
    assert res["status"] == ev.PASS
    assert "кто" in res["why"] and "роль" in res["why"]


def test_eval_report_lists_what_is_not_measured():
    report = ev.run_all(out_dir="/tmp/eval_test")
    assert report["not_measured"], "отчёт обязан перечислять, что не измерено"
    assert {"pass", "fail", "skip", "total"} <= set(report["counts"])
    assert (Path("/tmp/eval_test") / "report.json").exists()


# ─────────────────────────── backlog ───────────────────────────
def test_same_failure_twice_is_one_node_with_counter(tmp_path):
    b = bl.Backlog(tmp_path / "b.json")
    one = {"results": [{"code": "prices", "status": "fail", "why": "без даты: [v1]"}]}
    two = {"results": [{"code": "prices", "status": "fail", "why": "без даты: [v1, v2]"}]}
    assert b.ingest(one, cycle="к1") == 1
    assert b.ingest(two, cycle="к2") == 0
    node = b.open_nodes()[0]
    assert node["seen"] == 2, "повтор провала должен копиться в счётчике, а не в новых узлах"


def test_new_nodes_per_cycle_are_capped(tmp_path):
    b = bl.Backlog(tmp_path / "b.json")
    many = {"results": [{"code": f"c{i}", "status": "fail", "why": "x"} for i in range(12)]}
    assert b.ingest(many, cycle="к1") <= bl.MAX_NEW_PER_CYCLE


def test_node_closes_only_when_its_check_passes(tmp_path):
    b = bl.Backlog(tmp_path / "b.json")
    b.ingest({"results": [{"code": "norms", "status": "fail", "why": "нет цитаты"}]}, cycle="к1")
    passed = {"results": [{"code": "norms", "status": "pass", "why": "цитата есть"}]}
    b.ingest(passed, cycle="к2")
    assert b.data["nodes"][0]["status"] == bl.CLOSED
    assert "прошла" in b.data["nodes"][0]["closed_note"]


def test_child_cannot_be_closed_before_parent(tmp_path):
    b = bl.Backlog(tmp_path / "b.json")
    parent = b.add(code="a", title="родитель", observed="o", evidence="e", closed_by="c")
    child = b.add(code="b", title="ребёнок", observed="o", evidence="e", closed_by="c",
                  blocked_by=[parent["id"]])
    assert b.close(child["id"], note="закрываю")["status"] == bl.BLOCKED
    b.close(parent["id"], note="прошло")
    assert b.close(child["id"], note="прошло")["status"] == bl.CLOSED


def test_graph_shows_ready_and_blocked(tmp_path):
    b = bl.Backlog(tmp_path / "b.json")
    p = b.add(code="a", title="p", observed="o", evidence="e", closed_by="c")
    b.add(code="b", title="c", observed="o", evidence="e", closed_by="c",
          blocked_by=[p["id"]])
    g = b.graph()
    assert g["ready"] == [p["id"]] and len(g["blocked"]) == 1


# ─────────────────────────── loop ───────────────────────────
def test_loop_stops_with_a_named_reason(tmp_path):
    res = loop.run(cycles=2, out_dir=tmp_path / "loop")
    assert res["stop_reason"]
    assert res["cycles_run"] >= 1
    assert (tmp_path / "loop" / "runs.jsonl").exists()


def test_loop_does_not_edit_own_code():
    """Границы цикла записаны в результат — чтобы «самоэволюция» не превращалась в миф."""
    res = loop.run(cycles=1, out_dir="/tmp/loop_test")
    text = " ".join(res["limits_kept"]).lower()
    assert "не правит собственный код" in text
    assert "прошедшей проверкой" in text


# ─────────────── цикл целиком: провал → узел → человек → закрытие ───────────────
def test_loop_full_cycle_flow(tmp_path):
    """Провал не «исправляется сам»: он уходит человеку, а после починки закрывается
    прошедшей проверкой. Это и есть разница между «самоулучшается» и «самоотмечается»."""
    summary = json.loads((ROOT / "out" / "demo_eval" / "summary.json").read_text(encoding="utf-8")) \
        if (ROOT / "out" / "demo_eval" / "summary.json").exists() else None
    if summary is None:
        pytest.skip("нет артефакта прогона — сравнивать нечего")
    broken = dict(summary)
    broken["amount_act_safe"] = broken["amount_act_safe"] + 1000
    bad_path = tmp_path / "summary.json"
    bad_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

    res = loop.run(cycles=3, out_dir=tmp_path / "loop", summary_path=bad_path,
                   max_attempts=2)
    assert res["stop_reason"] == loop.STOP_STUCK
    nodes = json.loads((tmp_path / "loop" / "backlog.json").read_text(encoding="utf-8"))["nodes"]
    assert nodes[0]["status"] == bl.BLOCKED
    assert "нужен человек" in nodes[0]["closed_note"]

    good_path = tmp_path / "fixed.json"
    good_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    loop.run(cycles=1, out_dir=tmp_path / "loop", summary_path=good_path)
    nodes = json.loads((tmp_path / "loop" / "backlog.json").read_text(encoding="utf-8"))["nodes"]
    assert nodes[0]["status"] == bl.CLOSED
    assert "прошла" in nodes[0]["closed_note"]
