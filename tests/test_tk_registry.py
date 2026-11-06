# -*- coding: utf-8 -*-
"""Страж-тесты реестра ТК: статус нельзя выдумать, подпись не переживает правку.

Правила, которые проверяются:

1. черновик без проверки человека — «не проверен», даже если файл выглядит законченным;
2. «проверено» ставится только при ФИО и роли;
3. правка разделов после проверки аннулирует проверку (отпечаток содержания), а не сохраняет
   подпись на другом тексте;
4. две карты в одном каталоге не затирают друг друга (имя файла привязано к виду работ);
5. реестр не пишет «готово»/«утверждено» и группирует по объектам.
"""
import json
import subprocess
import sys
from pathlib import Path

from pto_agent import tk
from pto_agent.tk_registry import (STATUS_CHANGED, STATUS_CONFIRMED, STATUS_DRAFT,
                                   STATUS_NO_OWNER, build, load_drafts, to_markdown, verify,
                                   write)

ROOT = Path(__file__).resolve().parents[1]


def _drafts_dir(tmp_path: Path, object_name: str = "Школа на 550 мест",
                work_types=("concrete", "earthworks")) -> Path:
    out = tmp_path / "tk"
    for work_type in work_types:
        tk.write(tk.build_tk_draft(work_type_id=work_type, object_name=object_name), out)
    return out


def _confirm(out: Path, stem: str, by: str = "Иванов И.И.",
             role: str = "главный инженер") -> dict:
    path = out / f"{stem}.json"
    draft = tk.confirm(json.loads(path.read_text(encoding="utf-8")), by=by, role=role,
                       reason="проверил вручную")
    tk.write(draft, out, stem=stem)
    return draft


# ─────────────────────────── имена файлов ───────────────────────────
def test_two_work_types_do_not_overwrite_each_other(tmp_path):
    """Раньше все карты писались в tk_draft.json: вторая карта в каталоге затирала первую."""
    out = _drafts_dir(tmp_path)
    names = sorted(p.name for p in out.glob("*.json"))
    assert "tk_concrete.json" in names and "tk_earthworks.json" in names
    drafts, _ = load_drafts([out])
    assert len(drafts) == 2
    assert {d["work_type"]["id"] for d in drafts} == {"concrete", "earthworks"}


def test_ppr_file_name_refers_to_object(tmp_path):
    out = tmp_path / "tk"
    tk.write(tk.build_ppr_draft(object_name="Детский сад на 220 мест"), out)
    assert (out / "ppr_детский-сад-на-220-мест.json").exists()


# ─────────────────────────── статусы ───────────────────────────
def test_fresh_draft_is_not_checked(tmp_path):
    out = _drafts_dir(tmp_path)
    drafts, _ = load_drafts([out])
    registry = build(drafts)
    assert registry["totals"]["confirmed"] == 0
    assert registry["totals"]["not_checked"] == 2
    for slot in registry["objects"]:
        for entry in slot["drafts"]:
            assert entry["status"] == STATUS_DRAFT
            assert entry["check_valid"] is False
            assert entry["by"] is None


def test_confirmed_draft_has_name_role_date(tmp_path):
    out = _drafts_dir(tmp_path)
    _confirm(out, "tk_concrete")
    drafts, _ = load_drafts([out])
    entry = next(e for slot in build(drafts)["objects"] for e in slot["drafts"]
                 if e["file"] == "tk_concrete.json")
    assert entry["status"] == STATUS_CONFIRMED
    assert entry["check_valid"] is True
    assert entry["by"] == "Иванов И.И." and entry["role"] == "главный инженер"
    assert entry["confirmed_at"], "дата проверки должна попадать в реестр"


def test_edit_after_confirm_invalidates_check(tmp_path):
    """Подпись не переживает правку: иначе в реестре «проверено» стояло бы на другом тексте."""
    out = _drafts_dir(tmp_path)
    draft = _confirm(out, "tk_earthworks")
    draft["sections"][2]["items"].append({"text": "правка после проверки", "source": "кто-то"})
    tk.write(draft, out, stem="tk_earthworks")

    drafts, _ = load_drafts([out])
    registry = build(drafts)
    entry = next(e for slot in registry["objects"] for e in slot["drafts"]
                 if e["file"] == "tk_earthworks.json")
    assert entry["status"] == STATUS_CHANGED
    assert entry["changed_after_confirm"] is True
    assert entry["check_valid"] is False
    assert registry["changed_after_confirm"], "такая карта должна попадать в раздел «внимание»"
    assert entry["by"] == "Иванов И.И.", "ФИО сохраняется: видно, чья проверка аннулирована"


def test_confirmation_without_name_does_not_count(tmp_path):
    """Черновик с отметкой без ФИО (старые файлы, чужая запись) не считается проверенным."""
    out = _drafts_dir(tmp_path)
    path = out / "tk_concrete.json"
    draft = json.loads(path.read_text(encoding="utf-8"))
    draft["status"] = tk.STATUS_CONFIRMED
    draft["confirmed_by"] = {"by": "", "role": "", "at": "2026-01-01"}
    path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    drafts, _ = load_drafts([out])
    entry = next(e for slot in build(drafts)["objects"] for e in slot["drafts"]
                 if e["file"] == "tk_concrete.json")
    assert entry["status"] == STATUS_NO_OWNER
    assert entry["check_valid"] is False


def test_confirm_without_human_is_refused_by_module(tmp_path):
    """Уровнем ниже: модуль ТК не принимает анонимное подтверждение."""
    draft = tk.build_tk_draft(work_type_id="earthworks", object_name="Школа")
    result = tk.confirm(draft, by=" ", role=" ")
    assert result["confirm_problems"], "анонимное подтверждение должно быть отклонено"
    assert result["status"] != tk.STATUS_CONFIRMED


# ─────────────────────────── группировка и смысл ───────────────────────────
def test_registry_groups_by_object_and_counts(tmp_path):
    first = _drafts_dir(tmp_path, "Школа на 550 мест")
    second = _drafts_dir(tmp_path / "сад", "Детский сад на 220 мест",
                         work_types=("electrical",))
    _confirm(first, "tk_concrete")
    drafts, problems = load_drafts([first, second])
    registry = build(drafts, problems=problems)

    assert not problems
    assert registry["totals"]["objects"] == 2
    assert registry["totals"]["drafts"] == 3
    assert registry["totals"]["confirmed"] == 1
    school = next(s for s in registry["objects"] if s["object_name"] == "Школа на 550 мест")
    assert school["totals"]["tk"] == 2 and school["totals"]["ppr"] == 0
    assert school["state"] == "часть карт проверена, часть — черновики"
    kindergarten = next(s for s in registry["objects"]
                        if s["object_name"] == "Детский сад на 220 мест")
    assert kindergarten["state"] == "ни одна карта не проверена"
    # у каждого объекта есть «кого ждём с данными» — без выдуманных исполнителей
    assert school["waiting_for"] and all(g["owner"] for g in school["waiting_for"])


def test_registry_never_says_ready_or_approved(tmp_path):
    out = _drafts_dir(tmp_path)
    _confirm(out, "tk_concrete")
    drafts, _ = load_drafts([out])
    text = to_markdown(build(drafts)).lower()
    # «не утверждена» — законная оговорка; запрещены именно утверждения о готовности
    claim = text.replace("не утверждена", "")
    for forbidden in ("готова к применению", "готов к применению", "утверждена", "согласована"):
        assert forbidden not in claim, f"реестр не должен утверждать «{forbidden}»"
    assert "не право применять" in text


def test_duplicate_draft_is_reported_not_double_counted(tmp_path):
    out = _drafts_dir(tmp_path)
    copy_dir = tmp_path / "копия"
    copy_dir.mkdir()
    (copy_dir / "tk_concrete_копия.json").write_text(
        (out / "tk_concrete.json").read_text(encoding="utf-8"), encoding="utf-8")
    drafts, problems = load_drafts([out, copy_dir])
    assert len(drafts) == 2, "копия того же черновика не должна считаться второй картой"
    assert any("тот же черновик" in p for p in problems)


def test_missing_file_is_reported_by_verify(tmp_path):
    out = _drafts_dir(tmp_path)
    drafts, _ = load_drafts([out])
    registry = build(drafts)
    (out / "tk_earthworks.json").unlink()
    problems = verify(registry)
    assert problems and "нет на диске" in problems[0]


def test_xlsx_and_markdown_are_written(tmp_path):
    from openpyxl import load_workbook

    out = _drafts_dir(tmp_path)
    _confirm(out, "tk_concrete")
    drafts, _ = load_drafts([out])
    files = write(build(drafts), tmp_path / "report")

    wb = load_workbook(files["xlsx"])
    assert "Сводка" in wb.sheetnames
    header = [c.value for c in wb["Сводка"][1]]
    assert header[:5] == ["Объект", "ТК", "ППР", "Проверено инженером",
                          "Изменено после проверки"]
    markdown = Path(files["markdown"]).read_text(encoding="utf-8")
    assert "Кого ждём с данными" in markdown
    assert "Иванов И.И." in markdown


# ─────────────────────────── CLI ───────────────────────────
def _cli(*argv: str) -> subprocess.CompletedProcess:
    import os
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_cli_registry_reports_state_and_exit_code(tmp_path):
    out = _drafts_dir(tmp_path)
    run = _cli("tk", "--out", str(out), "draft", "--work-type", "concrete",
               "--object-name", "Школа")
    assert run.returncode == 0, run.stderr

    run = _cli("tk", "--out", str(out), "registry")
    assert run.returncode == 0, run.stderr
    assert "проверено инженером 0" in run.stdout
    assert (out / "registry.xlsx").exists() and (out / "registry.md").exists()

    run = _cli("tk", "--out", str(out), "registry", "--strict")
    assert run.returncode == 2, "--strict: непроверенные карты — это провал"
    assert "--strict" in run.stdout


def test_cli_registry_flags_edit_after_confirm(tmp_path):
    out = _drafts_dir(tmp_path)
    _confirm(out, "tk_concrete")
    draft = json.loads((out / "tk_concrete.json").read_text(encoding="utf-8"))
    draft["sections"][1]["items"].append({"text": "правка", "source": "кто-то"})
    tk.write(draft, out, stem="tk_concrete")

    run = _cli("tk", "--out", str(out), "registry")
    assert run.returncode == 2
    assert "проверка недействительна" in run.stdout


def test_cli_registry_empty_dir_is_not_an_error(tmp_path):
    out = tmp_path / "пусто"
    out.mkdir()
    run = _cli("tk", "--out", str(out), "registry")
    assert run.returncode == 0, run.stderr
    assert "объектов 0" in run.stdout


# ─────────────────────────── MCP-инструмент ───────────────────────────
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


def test_mcp_tk_registry_tool_is_registered():
    from pto_agent.cad.mcp_server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "tk_registry")
    assert tool["inputSchema"]["type"] == "object"
    assert "strict" in tool["inputSchema"]["properties"]
    # реестр только читает черновики и пишет отчёт: без ФИО он не может ничего подтвердить
    assert "by" not in tool["inputSchema"]["properties"]


def test_mcp_tk_registry_returns_objects_and_states(tmp_path):
    from pto_agent import tk as tk_mod

    for work_type in ("concrete", "earthworks"):
        tk_mod.write(tk_mod.build_tk_draft(work_type_id=work_type, object_name="Школа"),
                     tmp_path / "tk")
    result = _mcp_call("tk_registry", {"paths": [str(tmp_path / "tk")],
                                       "out_dir": str(tmp_path / "report")})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["totals"]["objects"] == 1
    assert payload["totals"]["confirmed"] == 0
    assert payload["totals"]["not_checked"] == 2
    slot = payload["objects"][0]
    assert slot["object_name"] == "Школа"
    assert slot["state"] == "ни одна карта не проверена"
    assert slot["waiting_for"], "должно быть видно, кого ждём с данными"
    assert Path(payload["files"]["xlsx"]).exists()


def test_mcp_tk_registry_strict_fails_on_unchecked(tmp_path):
    tk.write(tk.build_tk_draft(work_type_id="earthworks", object_name="Школа"), tmp_path / "tk")
    result = _mcp_call("tk_registry", {"paths": [str(tmp_path / "tk")],
                                       "out_dir": str(tmp_path / "report"), "strict": True})
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False and payload["strict_failed"] is True
