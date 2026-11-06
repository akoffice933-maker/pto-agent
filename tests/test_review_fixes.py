# -*- coding: utf-8 -*-
"""Исправления по ревью main=e6de718: воспроизведённые дефекты и защита от возврата.

Каждый тест здесь — это пункт ревью. Если тест падает, значит дефект вернулся:
NameError в commit через MCP, синтаксис 3.12 в «3.11-поставке», открытый HTTP,
подтверждение машинной идентичностью, выход за корень каталога, разрыв аудита при
параллельной записи, удаление хвоста журнала, открытые работы в актах скрытых работ,
узкий классификатор.
"""
from __future__ import annotations

import ast
import json
import shutil
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DXF_V1 = ROOT / "drawings" / "korpus2_plan_v1.dxf"
TEMPLATE = ROOT / "config" / "cad_template_corp_A.yaml"


# ─────────────────── 1. commit через MCP не падает NameError ────────────────
def test_mcp_commit_writes_history_and_audit(tmp_path):
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx

    if not DXF_V1.exists():
        pytest.skip("нет демо-чертежа")
    target = tmp_path / "plan.dxf"
    shutil.copy(DXF_V1, target)
    ctx = make_ctx(tmp_path, "out/cad/audit.jsonl")

    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "apply_operations", "arguments": {
               "file_path": str(target), "mode": "commit",
               "out_path": str(tmp_path / "plan_v2.dxf"),
               "operations": [{"type": "ADD_TEXT", "layer": "ПОМЕТКИ",
                               "params": {"point": [1000, 1000], "text": "проверка",
                                          "height": 250}}],
               "confirm": {"by": "Иванов И.И.", "role": "инженер ПТО",
                           "comment": "сверил с актом"}}}}
    res = handle_rpc(req, ctx)
    payload = json.loads(res["result"]["content"][0]["text"])

    assert res["result"]["isError"] is False, payload
    assert payload["ok"] is True and payload["written"] is True
    # ошибка была именно здесь: файл и аудит создавались, а клиент получал NameError
    history = (tmp_path / "out" / "cad" / "history.jsonl").read_text(encoding="utf-8")
    entry = json.loads(history.splitlines()[-1])
    assert entry["mode"] == "commit" and entry["sha256_out"]
    assert (tmp_path / "out" / "cad" / "audit.jsonl").exists()


# ─────────────────── 2. поставка компилируется на Python 3.11 ───────────────
def test_package_parses_as_python_311():
    """Вложенные f-строки с одинаковыми кавычками — только с 3.12. CI заявляет 3.11."""
    broken = []
    for path in ROOT.rglob("*.py"):
        if any(part in {".git", "out", ".venv", "__pycache___"} for part in path.parts):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path),
                      feature_version=(3, 11))
        except SyntaxError as exc:
            broken.append(f"{path.relative_to(ROOT)}:{exc.lineno} {exc.msg}")
    assert broken == []


# ─────────────────── 3–5. HTTP-сервер: токен, localhost, CORS, песочница ────
def test_http_defaults_to_loopback():
    from pto_agent.cad import mcp_server

    text = Path(mcp_server.__file__).read_text(encoding="utf-8")
    assert '"--host", default="127.0.0.1"' in text
    assert 'default="0.0.0.0"' not in text
    assert "def serve_http(ctx, host=\"127.0.0.1\"" in text


def test_http_refuses_network_bind_without_token():
    from pto_agent.cad.mcp_server import make_ctx, serve_http

    with pytest.raises(SystemExit):
        serve_http(make_ctx(ROOT), "0.0.0.0", 8798)


def test_http_generates_token_when_missing(monkeypatch):
    from pto_agent.cad import mcp_server

    monkeypatch.delenv("PTO_MCP_TOKEN", raising=False)
    token, origin = mcp_server._resolve_http_security({}, "127.0.0.1", None)
    assert len(token) >= 16
    assert origin == ""            # CORS по умолчанию закрыт, «*» не отдаём


def test_safe_path_blocks_escape(tmp_path):
    from pto_agent.cad.mcp_server import safe_path

    inside = safe_path("out/x.json", tmp_path, write=True)
    assert str(inside).startswith(str(tmp_path.resolve()))
    with pytest.raises(ValueError):
        safe_path("../../etc/passwd", tmp_path)
    with pytest.raises(ValueError):
        safe_path("/etc/passwd", tmp_path)


def test_check_paths_enforces_write_sandbox(tmp_path):
    from pto_agent.cad.mcp_server import check_paths

    ctx = {"base_dir": str(tmp_path), "sandbox_writes": True, "sandbox_reads": True}
    check_paths({"out_path": "out/ok.dxf"}, ctx)                       # внутри — можно
    with pytest.raises(ValueError):
        check_paths({"out_path": str(tmp_path.parent / "evil.dxf")}, ctx)
    with pytest.raises(ValueError):
        check_paths({"file_path": "/etc/hostname"}, ctx)
    # без песочницы (stdio, локальный пользователь) чтение вне корня не блокируется
    check_paths({"file_path": "/etc/hostname"}, {"base_dir": str(tmp_path)})


def test_public_message_hides_paths():
    from pto_agent.cad.mcp_server import _public_message

    text = _public_message(FileNotFoundError("/home/user/secret/project/plan.dxf"))
    assert "/home/user" not in text and "secret" not in text
    assert "<путь>" in text


# ─────────────────── 4. подтверждение человеком ─────────────────────────────
@pytest.mark.parametrize("by", ["ai-agent", "bot", "agent:cad", "gpt-4", "system",
                               "авто", "робот", "ИИ", "x1", ""])
def test_machine_or_empty_identity_is_rejected(by):
    from pto_agent.cad.ops import check_approval

    result = check_approval({"by": by, "role": "инженер ПТО"})
    assert result["ok"] is False
    assert any(i["severity"] == "blocking" for i in result["issues"])


def test_human_identity_accepted_but_marked_unverified():
    from pto_agent.cad.ops import check_approval

    result = check_approval({"by": "Иванов И.И.", "role": "инженер ПТО"})
    assert result["ok"] is True
    # честно: подпись проверена только по формату, к учётной записи не привязана
    assert result["identity_verified"] is False
    assert "не привязана к учётной записи" in result["identity_note"]


def test_commit_with_machine_identity_is_blocked(tmp_path):
    from pto_agent.cad.drawing import Drawing
    from pto_agent.cad.ops import apply_operations
    from pto_agent.cad.template import load_template

    if not DXF_V1.exists():
        pytest.skip("нет демо-чертежа")
    target = tmp_path / "plan.dxf"
    shutil.copy(DXF_V1, target)
    drawing = Drawing.open(target)
    tpl = load_template(TEMPLATE)
    res = apply_operations(drawing, [{"type": "ADD_TEXT", "layer": "ПОМЕТКИ",
                                      "params": {"point": [1000, 1000], "text": "x",
                                                 "height": 250}}],
                           tpl, mode="commit", out_path=str(tmp_path / "v2.dxf"),
                           approval={"by": "ai-agent", "role": "агент"})
    assert res["ok"] is False
    assert any(i["code"] == "APPROVAL_NOT_HUMAN" for i in res["issues"])
    assert not (tmp_path / "v2.dxf").exists()


# ─────────────────── 6. аудит: параллельная запись и якорь ──────────────────
def test_audit_chain_survives_parallel_writes(tmp_path):
    from pto_agent.audit import AuditLog

    log = AuditLog(tmp_path / "audit.jsonl")

    def worker(n):
        for i in range(15):
            log.append(f"u{n}", "agent", "test.parallel", f"e{i}", {"n": n})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok, count, msg = log.verify()
    assert ok is True, msg
    assert count == 120


def test_anchor_detects_truncated_tail(tmp_path):
    from pto_agent.audit import AuditLog

    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(6):
        log.append("agent", "agent", "test.entry", f"e{i}")
    log.anchor()

    # хэш-цепочка сама по себе удаление последних записей не замечает
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    (tmp_path / "audit.jsonl").write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")
    assert log.verify()[0] is True
    ok, count, msg = log.verify_anchored()
    assert ok is False and count == 4 and "удалены" in msg


def test_verify_anchored_without_anchor_says_so(tmp_path):
    from pto_agent.audit import AuditLog

    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("agent", "agent", "test.entry", "e1")
    ok, _count, msg = log.verify_anchored()
    assert ok is True and "якоря нет" in msg


# ─────────────────── 7. открытые работы не идут в акты ─────────────────────
def test_open_work_detected():
    from pto_agent.asbuilt.requirements import is_open_work

    assert is_open_work("Розетка неутопленного типа при открытой проводке") is True
    assert is_open_work("Установка короба по поверхности стен") is True
    assert is_open_work("Прокладка кабеля открыто по стенам") is True
    assert is_open_work("Прокладка кабеля в гофре в штробе") is False
    assert is_open_work("Заделка штроб после прокладки кабеля") is False


def test_plan_acts_excludes_open_works():
    from pto_agent.asbuilt.acts_plan import plan_acts

    positions = [
        {"n": 1, "code": "ФЕРм 10-01", "name": "Розетка неутопленного типа при открытой "
                                             "проводке", "unit": "шт", "qty": 10, "kind": "work"},
        {"n": 2, "code": "ФЕРм 10-02", "name": "Прокладка кабеля в гофре в штробе",
         "unit": "м", "qty": 120, "kind": "work"},
        {"n": 3, "code": "ФЕРм 10-03", "name": "Установка короба по поверхности стен",
         "unit": "м", "qty": 30, "kind": "work"},
    ]
    plan = plan_acts(positions, object_name="тест")
    in_acts = [p["name"] for act in plan["acts"] for p in act["positions"]]
    assert any("штробе" in name for name in in_acts)
    assert not any("открытой проводке" in name for name in in_acts)
    reasons = " ".join(e["reason"] for e in plan["excluded"])
    assert "открытые работы" in reasons


# ─────────────────── 8–9. классификатор: примеры из ревью ──────────────────
@pytest.mark.parametrize("text,expected", [
    ("Устройство кровель из рулонных материалов", "roofing"),
    ("Утепление покрытия минераловатными плитами", "roofing"),
    ("Устройство бетонной подготовки", "concrete"),
    ("Устройство монолитной фундаментной плиты", "concrete"),
    ("Установка унитазов", "water_supply_sewer"),
    ("Прокладка водопровода из полипропиленовых труб", "water_supply_sewer"),
    ("Гидравлическое испытание системы отопления", "hvac"),
    ("Промывка системы отопления", "hvac"),
    ("Установка радиаторов отопления", "hvac"),
    ("Установка приёмно-контрольного прибора", "fire_alarm"),
    ("Монтаж видеокамеры", "low_voltage"),
    ("Пусконаладочные работы системы видеонаблюдения", "low_voltage"),
    ("Улучшенная штукатурка стен", "finishing"),
    ("Окраска потолков водоэмульсионной краской", "finishing"),
    ("Устройство покрытий из линолеума", "finishing"),
    ("Установка дверных блоков", "finishing"),
])
def test_classifier_recognises_review_examples(text, expected):
    from pto_agent.asbuilt.requirements import classify_work

    hits = classify_work(text)
    assert hits, f"позиция не распознана: {text}"
    assert hits[0]["work_type"] == expected


def test_multiword_key_tolerates_inflection():
    """«бетонн подготовк» должно ловить «бетонной подготовки» — иначе теряется половина."""
    from pto_agent.asbuilt.requirements import classify_work

    hits = classify_work("Устройство бетонной подготовки под фундаменты")
    assert any(h["work_type"] == "concrete" for h in hits)


def test_price_base_not_read_as_metal_structure():
    """«ФЕРм» в шифре расценки не должен давать вид работ «металлоконструкции»."""
    from pto_agent.asbuilt.requirements import classify_work

    hits = classify_work("Прокладка кабеля по лотку ФЕРм 21-02-012-01")
    assert [h["work_type"] for h in hits] == ["electrical"]
