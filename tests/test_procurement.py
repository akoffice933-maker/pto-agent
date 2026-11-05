# -*- coding: utf-8 -*-
"""Цены, закупка и аудит-лог: «цифра с источником», «КП без реквизитов не сравнивается»."""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent.audit import AuditLog, diff, summarize_changes
from pto_agent.prices import (import_prices, load_index, lookup, price_history,
                              prices_to_markdown, read_rows)
from pto_agent.procurement import (build_request, compare_quotes, decide, load_quotes,
                                   procurement_to_markdown, request_from_estimate,
                                   validate_quote)

PRICES_Q3 = ROOT / "data" / "demo" / "prices" / "fgis_cs_2025q3.csv"
PRICES_Q4 = ROOT / "data" / "demo" / "prices" / "fgis_cs_2025q4.csv"
QUOTES = ROOT / "data" / "demo" / "quotes" / "quotes_1166n.csv"
SMETA = ROOT / "uploads" / "smeta_1166N_AROS.md"
CABLE = "21.1.06.09-0151"
AUTOMAT = "62.1.01.09-1101"


@pytest.fixture()
def price_dir(tmp_path):
    out = tmp_path / "prices"
    import_prices(PRICES_Q3, source="ФГИС ЦС", price_date="2025-09-01", region="47", zone="1",
                  out_dir=out)
    import_prices(PRICES_Q4, source="ФГИС ЦС", price_date="2025-12-01", region="47", zone="1",
                  out_dir=out)
    return out


# ─────────────────────────────── цены ───────────────────────────────
def test_price_import_creates_versions(price_dir):
    index = load_index(price_dir)
    assert [v["version"] for v in index["versions"]] == ["2025-09-01-фгис-цс-47",
                                                         "2025-12-01-фгис-цс-47"]
    assert all(v["items"] == 9 for v in index["versions"])
    assert len({v["digest"] for v in index["versions"]}) == 2      # снимки разные


def test_price_lookup_respects_date(price_dir):
    """Запрос «цена на 1 октября» не должен получить декабрьскую выгрузку."""
    october = lookup(CABLE, as_of="2025-10-01", out_dir=price_dir)
    december = lookup(CABLE, as_of="2025-12-31", out_dir=price_dir)
    assert october["found"] and october["price_date"] == "2025-09-01"
    assert december["price_date"] == "2025-12-01"
    assert december["price"] > october["price"]


def test_price_has_source_and_version(price_dir):
    result = lookup(CABLE, out_dir=price_dir)
    assert result["source"] == "ФГИС ЦС" and result["version"].startswith("2025-12-01")
    assert result["region"] == "47" and result["verified"] is True


def test_missing_price_is_reported_not_substituted(price_dir):
    result = lookup("00.0.00.00-0000", out_dir=price_dir)
    assert result["found"] is False
    assert "не найден" in result["reason"]
    assert result["searched"]                      # перечислено, где смотрели
    assert "похожий" in prices_to_markdown(lookup_result=result)


def test_price_history_shows_change_percent(price_dir):
    history = price_history(AUTOMAT, out_dir=price_dir)
    assert len(history["points"]) == 2
    change = history["changes"][0]
    assert change["delta"] == pytest.approx(49.0)
    assert change["percent"] == pytest.approx(11.95, abs=0.01)   # 410 → 459


def test_bad_price_row_is_skipped_with_reason(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("code,name,unit,price\n01-1,Кабель,м,\n,Без кода,м,100\n02-2,Ок,м,250\n",
                   encoding="utf-8")
    items, problems = read_rows(bad)
    assert [i["code"] for i in items] == ["02-2"]
    assert len(problems) == 2


# ─────────────────────────────── закупка ───────────────────────────────
def test_request_takes_materials_only_and_sums_codes():
    request = build_request([
        {"kind": "work", "code": "ГЭСН", "name": "Прокладка кабеля", "qty": 10, "unit": "м"},
        {"kind": "material", "code": CABLE, "name": "Кабель ВВГнг", "qty": 0.3, "unit": "1000 м"},
        {"kind": "material", "code": CABLE, "name": "Кабель ВВГнг", "qty": 0.07, "unit": "1000 м"},
    ], object_name="Объект", request_no="ЗМ-1")
    assert request["items_count"] == 1
    assert request["items"][0]["qty"] == pytest.approx(0.37)
    assert request["items"][0]["lines"] == 2


def test_quote_without_requisites_is_not_comparable():
    quote = {"supplier": "ООО «А»", "code": CABLE, "price": 100, "unit": "1000 м",
             "delivery_days": None, "valid_until": "", "quote_date": "", "quote_no": ""}
    verdict = validate_quote(quote)
    assert verdict["comparable"] is False
    assert "срок поставки" in "; ".join(verdict["notes"]) + "срок поставки"


def test_expired_quote_is_rejected():
    quote = {"supplier": "ООО «А»", "code": CABLE, "price": 100, "unit": "1000 м",
             "delivery_days": 5, "valid_until": "2026-01-01", "quote_date": "2025-12-01",
             "quote_no": "КП-1"}
    verdict = validate_quote(quote, today=date(2026, 11, 5))
    assert verdict["comparable"] is False
    assert "истёк" in verdict["notes"][0]


def test_comparison_brings_prices_to_one_basis():
    """У одного КП цена без НДС, у другого с НДС — сравнение по приведённой цене."""
    request = build_request([{"kind": "material", "code": CABLE, "name": "Кабель",
                              "qty": 1.0, "unit": "1000 м"}], request_no="ЗМ-1")
    quotes = [
        {"supplier": "Без НДС", "code": CABLE, "price": 100000, "unit": "1000 м", "vat_rate": 0,
         "delivery_days": 10, "valid_until": "2027-01-01", "quote_date": "2026-11-01",
         "quote_no": "КП-1"},
        {"supplier": "С НДС", "code": CABLE, "price": 90000, "unit": "1000 м", "vat_rate": 20,
         "delivery_days": 10, "valid_until": "2027-01-01", "quote_date": "2026-11-01",
         "quote_no": "КП-2"},
    ]
    comparison = compare_quotes(request, quotes, today=date(2026, 11, 5))
    line = comparison["lines"][0]
    assert line["winner"]["supplier"] == "Без НДС"        # 100 000 < 108 000
    assert line["winner"]["price_vat"] == pytest.approx(100000)
    assert "без НДС" in line["winner"]["price_basis"]


def test_comparison_excludes_bad_quotes_and_says_why():
    request = build_request([{"kind": "material", "code": CABLE, "name": "Кабель",
                              "qty": 1.0, "unit": "1000 м"}], request_no="ЗМ-1")
    quotes, _ = load_quotes(QUOTES)
    comparison = compare_quotes(request, quotes, today=date(2026, 11, 5))
    assert comparison["lines"], "ожидалась хотя бы одна сравнимая позиция"
    excluded = " ".join(" ".join(item["reasons"]) for item in comparison["not_comparable"])
    assert "СнабМонтаж" in " ".join(item["supplier"] for item in comparison["not_comparable"])
    assert "срок действия цены" in excluded or "срок поставки" in excluded


def test_positions_without_quotes_are_listed():
    request = build_request([{"kind": "material", "code": "99.9.99.99-9999", "name": "Нет КП",
                              "qty": 1, "unit": "шт"}], request_no="ЗМ-1")
    comparison = compare_quotes(request, [])
    assert comparison["status"] == "no_data"
    assert comparison["no_quotes"][0]["reason"] == "предложений по позиции нет"


def test_decision_is_written_to_audit_log(tmp_path):
    """Решение человека: автор, причина, ссылка на сравнение и на сколько дороже лучшего."""
    request = build_request([{"kind": "material", "code": CABLE, "name": "Кабель",
                              "qty": 1.0, "unit": "1000 м"}], request_no="ЗМ-7")
    quotes, _ = load_quotes(QUOTES)
    comparison = compare_quotes(request, quotes, today=date(2026, 11, 5))
    log = AuditLog(tmp_path / "audit.jsonl")
    decision = decide(comparison, {CABLE: "ООО «Кабель-СПб»"}, by="Петров П.П.",
                      reason="срок поставки 10 дней вместо 14", audit_log=log)
    assert decision["decisions"] and decision["over_best"] > 0
    ok, count, message = log.verify()
    assert ok and count == 1
    record = log.history(f"procurement:ЗМ-7:{CABLE}")[0]
    assert record["actor"] == "Петров П.П." and record["actor_type"] == "human"
    assert record["payload"]["reason"] == "срок поставки 10 дней вместо 14"
    assert record["payload"]["delta"]["supplier"]["to"] == "ООО «Кабель-СПб»"


def test_decision_rejects_supplier_without_eligible_quote(tmp_path):
    request = build_request([{"kind": "material", "code": CABLE, "name": "Кабель",
                              "qty": 1.0, "unit": "1000 м"}], request_no="ЗМ-8")
    quotes, _ = load_quotes(QUOTES)
    comparison = compare_quotes(request, quotes, today=date(2026, 11, 5))
    decision = decide(comparison, {CABLE: "ООО «Неизвестный»"}, by="Петров П.П.")
    assert decision["decisions"] == []
    assert "не подавал пригодного КП" in decision["problems"][0]


def test_decision_without_reason_is_flagged(tmp_path):
    request = build_request([{"kind": "material", "code": CABLE, "name": "Кабель",
                              "qty": 1.0, "unit": "1000 м"}], request_no="ЗМ-9")
    quotes, _ = load_quotes(QUOTES)
    comparison = compare_quotes(request, quotes, today=date(2026, 11, 5))
    log = AuditLog(tmp_path / "audit.jsonl")
    decision = decide(comparison, {CABLE: "ООО «Кабель-СПб»"}, by="Петров П.П.", audit_log=log)
    assert decision["reason_missing"] is True
    assert log.history()[0]["payload"]["reason_missing"] is True


def test_comparison_markdown_explains_choice():
    request = build_request([{"kind": "material", "code": CABLE, "name": "Кабель",
                              "qty": 1.0, "unit": "1000 м"}], request_no="ЗМ-1")
    quotes, _ = load_quotes(QUOTES)
    comparison = compare_quotes(request, quotes, today=date(2026, 11, 5))
    text = procurement_to_markdown(comparison, request=request)
    assert "Обоснование" in text and "минимальная приведённая цена" in text
    assert "Не участвовали в сравнении" in text or "Без пригодных предложений" in text


# ─────────────────────────────── аудит-лог ───────────────────────────────
def test_diff_reports_only_changed_fields():
    assert diff({"a": 1, "b": 2}, {"a": 1, "b": 3}) == {"b": {"from": 2, "to": 3}}


def test_change_record_keeps_snapshot_and_chain(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append_change("agent:procurement", "agent", "entity.changed", "kp:1",
                      {"price": 100}, {"price": 95}, reason="пересчёт", ref="КП-2")
    record = log.history()[0]
    assert record["payload"]["before"] == {"price": 100}
    assert record["payload"]["after"] == {"price": 95}
    assert record["payload"]["changed_fields"] == ["price"]
    assert log.verify()[0] is True


def test_tampered_record_breaks_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("agent:x", "agent", "test", "entity", {"a": 1})
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["a"] = 2
    path.write_text(json.dumps(tampered, ensure_ascii=False) + "\n", encoding="utf-8")
    ok, _, message = AuditLog(path).verify()
    assert ok is False and "хэш не совпадает" in message


def test_summarize_changes_is_flat(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append_change("Иванов И.И.", "human", "entity.changed", "act:1", {}, {"status": "ok"},
                      reason="проверено")
    summary = summarize_changes(log.history())
    assert summary[0]["actor"] == "Иванов И.И."
    assert summary[0]["fields"] == ["status"] and summary[0]["reason"] == "проверено"


def test_find_by_hash_prefix(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    record = log.append("agent", "agent", "test", "entity", {})
    assert log.find(record["hash"][:10])["hash"] == record["hash"]
    assert log.find("нет-такого") is None


# ─────────────────────────────────── CLI ───────────────────────────────────
def test_cli_prices_roundtrip(tmp_path):
    out = tmp_path / "prices"
    res = subprocess.run([sys.executable, "-m", "pto_agent", "prices", "--out", str(out),
                          "import", "--file", str(PRICES_Q3), "--source", "ФГИС ЦС",
                          "--date", "2025-09-01", "--region", "47"],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    res = subprocess.run([sys.executable, "-m", "pto_agent", "prices", "--out", str(out),
                          "lookup", "--code", CABLE], capture_output=True, text=True,
                         timeout=180, cwd=ROOT)
    assert res.returncode == 0 and "ФГИС ЦС" in res.stdout


def test_cli_procure_compare_and_decide(tmp_path):
    out = tmp_path / "proc"
    res = subprocess.run([sys.executable, "-m", "pto_agent", "procure", "--out", str(out),
                          "compare", "--request", str(_request_file(tmp_path)),
                          "--quotes", str(QUOTES)],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    comparison = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert comparison["lines_count"] >= 1
    assert (out / "comparison.xlsx").exists()

    code = comparison["lines"][0]["code"]
    supplier = comparison["lines"][0]["winner"]["supplier"]
    res = subprocess.run([sys.executable, "-m", "pto_agent", "procure", "--out", str(out),
                          "--audit", str(out / "audit.jsonl"), "decide",
                          "--request", str(_request_file(tmp_path)), "--quotes", str(QUOTES),
                          "--choose", f"{code}={supplier}", "--by", "Иванов И.И.",
                          "--reason", "минимум цены при сроке 14 дней"],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    audit = AuditLog(out / "audit.jsonl")
    assert audit.history()[0]["payload"]["reason"] == "минимум цены при сроке 14 дней"

    res = subprocess.run([sys.executable, "-m", "pto_agent", "audit", "--path",
                          str(out / "audit.jsonl"), "list"],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0 and "procurement.decided" in res.stdout


def _request_file(tmp_path: Path) -> Path:
    if SMETA.exists():
        request = request_from_estimate(SMETA, request_no="ЗМ-ТЕСТ")
    else:
        request = build_request([{"kind": "material", "code": CABLE, "name": "Кабель",
                                  "qty": 1.0, "unit": "1000 м"}], request_no="ЗМ-ТЕСТ")
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_audit_export_xlsx(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append_change("Иванов И.И.", "human", "entity.changed", "act:1", {}, {"a": 1},
                      reason="проверка")
    res = subprocess.run([sys.executable, "-m", "pto_agent", "audit", "--path",
                          str(tmp_path / "audit.jsonl"), "export", "--output",
                          str(tmp_path / "audit.xlsx")],
                         capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "audit.xlsx").exists() and "цепочка целостна" in res.stdout


# ─────────────────────────────────── MCP ───────────────────────────────────
def test_mcp_prices_and_procurement_tools(tmp_path):
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx
    ctx = make_ctx(str(ROOT))

    def call(name, arguments):
        res = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}}, ctx)
        return json.loads(res["result"]["content"][0]["text"])

    imported = call("prices", {"action": "import", "file_path": str(PRICES_Q3),
                               "source": "ФГИС ЦС", "price_date": "2025-09-01",
                               "region": "47", "prices_dir": str(tmp_path / "prices")})
    assert imported["ok"] and imported["items"] == 9
    found = call("prices", {"action": "lookup", "code": CABLE,
                            "prices_dir": str(tmp_path / "prices")})
    assert found["price"]["found"] is True

    request = call("procurement", {"action": "request",
                                   "smeta_path": str(SMETA if SMETA.exists() else QUOTES)})
    assert request["ok"]
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request["request"], ensure_ascii=False), encoding="utf-8")
    comparison = call("procurement", {"action": "compare", "request_path": str(path),
                                      "quotes_paths": [str(QUOTES)]})
    assert comparison["ok"] and comparison["comparison"]["status"] in {"complete", "partial"}


def test_mcp_audit_trail_verifies_chain(tmp_path):
    from pto_agent.cad.mcp_server import handle_rpc, make_ctx
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append_change("Иванов И.И.", "human", "entity.changed", "act:1", {}, {"a": 1},
                      reason="проверка")
    ctx = make_ctx(str(ROOT))
    res = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "audit_trail",
                                 "arguments": {"action": "verify",
                                               "audit_path": str(tmp_path / "audit.jsonl")}}},
                     ctx)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["ok"] is True and payload["records"] == 1
