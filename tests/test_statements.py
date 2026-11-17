# -*- coding: utf-8 -*-
"""Годовая отчётность: баланс и ОФР по ФСБУ 4/2023.

Что должно быть верно (а не «страница собралась»):

1. форма и коды строк берутся из действующего стандарта (ФСБУ 4/2023, приложение № 10),
   а не из отменённого с 01.01.2025 приказа № 66н;
2. каждая строка собирается из конкретных счетов, и это видно в строке;
3. баланс сходится: актив 1600 = пассив 1700, итоги разделов следуют из строк;
4. строка, которую нечем заполнить, пустая **с причиной**, а не ноль молча;
5. прибыль (84.01) и убыток (84.02) дают в 1370 сальдо, а не две суммы рядом;
6. дебиторка и кредиторка одного счёта не гасят друг друга;
7. отрицательный актив — ошибка учёта, и она ловится проверкой;
8. счёт с сальдо, не попавший в строки, называется списком с суммой;
9. цепочка ОФР пересчитывается, и отчёт сходится с оборотами 90-х счетов и реформацией;
10. прибыль из ОФР оказывается в капитале, а не «где-то ещё»;
11. налог на прибыль, который контур посчитал, но не провёл, назван числом;
12. «не сделано» (ОДДС, изменения капитала, пояснения, ПБУ 18/02) написано и в отчёте, и в CLI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pto_agent import ledger, statements, year_close                 # noqa: E402
from scripts.make_demo_year import build                             # noqa: E402

SCENARIO = REPO / "data" / "demo" / "year_close" / "scenario_2026.json"
DECISIONS = REPO / "data" / "demo" / "year_close" / "decisions_2026.json"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def closed() -> dict:
    state, _ = build(SCENARIO)
    report = year_close.close_year(state, "2026", decisions=read(DECISIONS))
    assert report["closed"], report["problems"]
    return state


def fresh(state: dict) -> dict:
    return json.loads(json.dumps(state))


def _post(state: dict, *, event: str, lines: list[dict], memo: str, key: str,
          date: str = "2026-12-31") -> dict:
    """Провести пробную операцию в закрытый декабрь — с причиной, иначе журнал её не примет."""
    result = ledger.post_entry(state, event=event, date=date,
                               doc={"number": f"проба {key}", "date": date}, lines=lines,
                               memo=memo, rule=event, key=key,
                               allow_closed=True, late_reason="проба в тесте отчётности")
    assert result["status"] == ledger.STATUS_OK, result["errors"] or result["warnings"]
    return result


def line(report: dict, code: str) -> dict:
    return next(item for item in report["lines"] if item["code"] == code)


def check_of(report: dict, code: str) -> dict:
    return next(item for item in report["checks"] if item["code"] == code)


# ─────────────────────────── форма и стандарт ───────────────────────────
def test_forms_are_taken_from_the_current_standard():
    rules = statements.load_rules()
    standard = rules["standard"]
    assert "ФСБУ 4/2023" in standard["name"]
    assert "157н" in standard["approved"]
    assert "2025" in standard["applies_from"]
    assert "66н" in standard["supersedes"] and "01.01.2025" in standard["supersedes"]
    codes = {item["code"] for item in rules["balance"]["lines"]}
    # строки, появившиеся именно в ФСБУ 4/2023: долгосрочные активы к продаже и гудвил
    assert {"1105", "1160", "1215", "1220", "1230", "1260"} <= codes
    pnl = {item["code"] for item in rules["pnl"]["lines"]}
    assert {"2300", "2410", "2411", "2412", "2420", "2500"} <= pnl
    assert any("продолжающейся деятельности" in item["title"]
               for item in rules["pnl"]["lines"] if item["code"] == "2300")


def test_every_line_has_accounts_or_a_reason():
    rules = statements.load_rules()
    for item in rules["balance"]["lines"]:
        assert item.get("accounts") is not None
        if not item["accounts"]:
            assert item.get("why_empty"), f"{item['code']}: пустая строка без причины"


# ─────────────────────────── баланс ───────────────────────────
def test_balance_ties_and_follows_from_accounts(closed):
    report = statements.check(closed, "2026")
    sheet = report["balance"]
    assert sheet["totals"]["1600"] == pytest.approx(68_400_000.0)
    assert sheet["totals"]["1700"] == pytest.approx(68_400_000.0)
    assert check_of(report, "BALANCE_TIES")["ok"]
    assert check_of(report, "SECTION_TIES")["ok"]
    assert sheet["totals"]["1100"] + sheet["totals"]["1200"] == pytest.approx(sheet["totals"]["1600"])
    assert (sheet["totals"]["1300"] + sheet["totals"]["1400"] + sheet["totals"]["1500"]
            == pytest.approx(sheet["totals"]["1700"]))


def test_lines_show_which_accounts_they_are_built_from(closed):
    sheet = statements.balance(closed, "2026")
    assert line(sheet, "1210")["amount"] == pytest.approx(351_500.0)      # 10.01 после поступления
    assert [part["account"] for part in line(sheet, "1210")["accounts"]] == ["10.01"]
    assert line(sheet, "1230")["amount"] == pytest.approx(67_248_500.0)   # 62.01 + 73.02
    assert line(sheet, "1260")["amount"] == pytest.approx(800_000.0)      # РБП 97
    # 84.01 = чистая прибыль: 15 000 000 до налога минус 3 750 000 налога
    assert line(sheet, "1370")["amount"] == pytest.approx(11_250_000.0)
    # 60.01 + 68.02 + 68.04 (налог на прибыль проведён шагом закрытия года)
    assert line(sheet, "1520")["amount"] == pytest.approx(55_910_000.0)
    assert line(sheet, "1540")["amount"] == pytest.approx(1_240_000.0)    # оценочное 96


def test_empty_lines_are_empty_with_a_reason_not_zero(closed):
    sheet = statements.balance(closed, "2026")
    assets = line(sheet, "1150")
    assert assets["amount"] == 0
    assert "01, 02" in assets["why_empty"] or "01" in assets["why_empty"]
    assert line(sheet, "1310")["why_empty"], "уставный капитал: нет счёта 80 — причина обязана быть"
    assert line(sheet, "1180")["why_empty"]                     # ОНА: ПБУ 18/02 не ведётся


def test_profit_and_loss_are_netting_in_1370(closed):
    state = fresh(closed)
    _post(state, event="demo.loss",
          lines=[{"account": "84.02", "side": ledger.DEBIT, "amount": 2_000_000},
                 {"account": "99", "side": ledger.CREDIT, "amount": 2_000_000}],
          memo="реформация убытка прошлого периода: Дт 84.02 Кт 99",
          key="demo.loss|2026")
    sheet = statements.balance(state, "2026")
    # 84.01 = 11 250 000 прибыли, 84.02 = 2 000 000 убытка → в строке сальдо 9 250 000,
    # а не «две суммы рядом» и не 13 250 000
    assert line(sheet, "1370")["amount"] == pytest.approx(9_250_000.0)


def test_year_as_number_and_as_string_is_one_year(closed):
    """Круг 44: `close_year(state, 2026)` падал TypeError — год числом должен работать."""
    as_number = statements.check(closed, 2026)
    as_string = statements.check(closed, "2026")
    assert as_number["balance"]["totals"]["1600"] == as_string["balance"]["totals"]["1600"]
    assert as_number["pnl"]["values"]["2400"] == as_string["pnl"]["values"]["2400"]


def test_thousands_are_shown_next_to_rubles(closed):
    sheet = statements.balance(closed, "2026")
    assert line(sheet, "1230")["thousand"] == pytest.approx(67_248.5)
    assert "тыс. руб." in statements.load_rules()["standard"]["unit_note"]


def test_debit_and_credit_of_one_account_are_not_netted(closed):
    """Кредиторка не уменьшается дебиторкой того же счёта: свёрнутая задолженность — это ложь."""
    state = fresh(closed)
    _post(state, event="demo.payment",
          lines=[{"account": "51", "side": ledger.DEBIT, "amount": 5_000_000},
                 {"account": "62.01", "side": ledger.CREDIT, "amount": 5_000_000,
                  "analytics": {"контрагент": "ООО «Заказчик-Инвест»",
                                "договор": "Д-142/2026"}}],
          memo="часть оплаты от заказчика: деньги появились", key="demo.payment|2026")
    _post(state, event="demo.advance",
          lines=[{"account": "60.02", "side": ledger.DEBIT, "amount": 1_000_000,
                  "analytics": {"контрагент": "ООО «Металл-База»"}},
                 {"account": "51", "side": ledger.CREDIT, "amount": 1_000_000}],
          memo="аванс выдан: 60.02 в активе, а 60.01 в пассиве остаётся", key="demo.advance|2026")
    sheet = statements.balance(state, "2026")
    # 62.01 уменьшился на 5 000 000 и вырос аванс 60.02 на 1 000 000:
    assert line(sheet, "1230")["amount"] == pytest.approx(63_248_500.0)
    assert line(sheet, "1250")["amount"] == pytest.approx(4_000_000.0)
    # а кредиторка 60.01 не уменьшилась на выданный аванс — иначе задолженность была бы свёрнута
    assert line(sheet, "1520")["amount"] == pytest.approx(55_910_000.0)


# ─────────────────────────── отчёт о финансовых результатах ───────────────────────────
def test_pnl_chain_from_revenue_to_net_profit(closed):
    report = statements.check(closed, "2026")
    values = report["pnl"]["values"]
    assert values["2110"] == pytest.approx(55_000_000.0)        # 90.01 − 90.03 (без НДС 22 %)
    assert values["2120"] == pytest.approx(40_000_000.0)
    assert values["2100"] == pytest.approx(15_000_000.0)
    assert values["2200"] == pytest.approx(15_000_000.0)
    assert values["2300"] == pytest.approx(15_000_000.0)
    assert values["2410"] == pytest.approx(3_750_000.0)        # 68.04: 25 % от 15 000 000
    assert values["2411"] == pytest.approx(3_750_000.0)        # текущий = весь: отложенного нет
    assert values["2400"] == pytest.approx(11_250_000.0)       # чистая прибыль
    assert check_of(report, "PNL_CHAIN")["ok"]


def test_pnl_lines_that_cannot_be_filled_say_why(closed):
    report = statements.check(closed, "2026")
    # 2410 теперь заполняется из 68.04, а вот отложенный налог (2412) считать нечем
    tax_line = next(item for item in report["pnl"]["lines"] if item["code"] == "2410")
    assert tax_line.get("not_calculated") is None
    assert tax_line["amount"] == pytest.approx(3_750_000.0)
    deferred = next(item for item in report["pnl"]["lines"] if item["code"] == "2412")
    assert deferred.get("not_calculated") is True
    assert "09" in deferred["why_empty"] and "ПБУ 18/02" in deferred["why_empty"]
    for code in ("2210", "2220", "2310", "2320", "2420", "2460"):
        item = next(line for line in report["pnl"]["lines"] if line["code"] == code)
        assert item.get("why_empty"), f"{code}: пустая строка без причины"


def test_pnl_matches_the_journal(closed):
    report = statements.check(closed, "2026")
    assert check_of(report, "PNL_VS_TURNOVER")["ok"]


def test_profit_from_pnl_lands_in_capital(closed):
    report = statements.check(closed, "2026")
    assert check_of(report, "RETAINED_VS_PNL")["ok"]
    assert line(report["balance"], "1370")["amount"] == pytest.approx(
        report["pnl"]["values"]["2400"])


def test_posted_profit_tax_leaves_no_gap_but_is_named(closed):
    """Налог проведён: разрыва нет, но цифра всё равно названа — и в отчёте, и в сводке."""
    report = statements.check(closed, "2026")
    assert report["gaps"] == []
    tax = report["tax"]
    assert tax["posted"] == pytest.approx(3_750_000.0)          # 25 % от 15 000 000
    assert tax["computed"] == pytest.approx(3_750_000.0)
    assert tax["difference"] == pytest.approx(0.0)
    assert "3 750 000,00" in statements.screen_text(report)
    assert check_of(report, "TAX_POSTED")["ok"]


def test_missing_tax_posting_becomes_a_gap_again(closed):
    """Убрали проводку налога — цифра не исчезает, а становится разрывом."""
    state = fresh(closed)
    state["entries"] = [entry for entry in state["entries"]
                        if entry.get("event") != "year.profit_tax"]
    report = statements.check(state, "2026")
    assert not check_of(report, "TAX_POSTED")["ok"]
    assert report["gaps"] and report["gaps"][0]["amount"] == pytest.approx(3_750_000.0)
    assert "68.04" in report["gaps"][0]["why"]


# ─────────────────────────── проверки ловят ошибки ───────────────────────────
def test_negative_asset_is_caught(closed):
    """Списать больше, чем было на складе, — ошибка: она называется, а не прячется в минус."""
    state = fresh(closed)
    _post(state, event="demo.writeoff",
          lines=[{"account": "94", "side": ledger.DEBIT, "amount": 600_000,
                  "analytics": {"склад": "Центральный склад"}},
                 {"account": "10.01", "side": ledger.CREDIT, "amount": 600_000,
                  "analytics": {"склад": "Центральный склад",
                                "номенклатура": "Арматура A500C, d 12 мм"}}],
          memo="списание сверх остатка — проверка ловит", key="demo.writeoff|2026")
    report = statements.check(state, "2026")
    item = check_of(report, "NO_NEGATIVE_LINES")
    assert not item["ok"]
    assert "1210" in item["why"] and "списали больше" in item["why"]


def test_account_outside_every_line_is_named(closed):
    state = fresh(closed)
    _post(state, event="demo.other",
          lines=[{"account": "76", "side": ledger.DEBIT, "amount": 300_000,
                  "analytics": {"контрагент": "ООО «Заказчик-Инвест»"}},
                 {"account": "91.01", "side": ledger.CREDIT, "amount": 300_000}],
          memo="прочий доход остался незакрытым: счёт 91.01 вне строк баланса",
          key="demo.other|2026")
    report = statements.check(state, "2026")
    item = check_of(report, "UNMAPPED_ACCOUNTS")
    assert not item["ok"]
    assert "91.01" in item["why"]


def test_open_year_is_not_silent(closed):
    state = fresh(closed)
    _post(state, event="demo.profit",
          lines=[{"account": "90.09", "side": ledger.DEBIT, "amount": 1_000_000},
                 {"account": "99", "side": ledger.CREDIT, "amount": 1_000_000}],
          memo="результат остался на 99 — проверка должна сказать", key="demo.profit|2026")
    report = statements.check(state, "2026")
    assert not check_of(report, "PROFIT_CLOSED")["ok"]
    assert "не закрыт" in check_of(report, "PROFIT_CLOSED")["why"]


def test_service_account_is_not_pretending_to_be_capital(closed):
    state = fresh(closed)
    _post(state, event="demo.opening", date="2026-01-15",
          lines=[{"account": "51", "side": ledger.DEBIT, "amount": 5_000_000},
                 {"account": "000", "side": ledger.CREDIT, "amount": 5_000_000}],
          memo="входящий остаток без капитала", key="demo.opening|2026")
    report = statements.check(state, "2026")
    item = check_of(report, "SERVICE_ACCOUNT")
    assert not item["ok"] and "5 000 000,00" in item["why"]


# ─────────────────────────── текст и CLI ───────────────────────────
def test_markdown_has_both_forms_and_honest_gaps(closed):
    report = statements.check(closed, "2026")
    text = statements.to_markdown(report)
    assert "Бухгалтерский баланс" in text and "Отчёт о финансовых результатах" in text
    assert "1600" in text and "2400" in text
    assert "## Не сделано (и почему)" in text
    assert "отчёт об изменениях капитала" in text.lower() or "изменениях капитала" in text.lower()
    assert "ПБУ 18/02" in text
    # разрыва по налогу больше нет (налог проведён), поэтому проверяем блок налога
    # и блок «чего не хватает» для ОДК/ОДДС — оба обязаны быть на экране и в отчёте
    assert "## Налог на прибыль" in text and "3 750 000,00" in text
    assert "## Чего не хватает" in text and "4000–4500" in text


def test_cli_reports_and_saves_markdown(closed, tmp_path):
    from pto_agent import cli

    ledger_path = tmp_path / "ledger.json"
    ledger.save(closed, ledger_path)
    out = tmp_path / "statements.md"
    code = cli.main(["statements", "check", "--year", "2026", "--ledger", str(ledger_path),
                     "--out", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "68 400 000,00" in text
    assert "нарушений 0" in text
    assert cli.main(["statements", "pnl", "--year", "2026", "--ledger", str(ledger_path)]) == 0
    assert cli.main(["statements", "balance", "--year", "2026", "--ledger", str(ledger_path)]) == 0


def _ledger_file(state: dict, tmp_path: Path) -> Path:
    path = tmp_path / "ledger_2026.json"
    ledger.save(state, path)
    return path


def test_mcp_tool_returns_same_numbers(closed, tmp_path):
    """MCP-инструмент 40 отдаёт те же цифры, что CLI: баланс 68 400 000, разрыв по налогу 3 750 000."""
    from pto_agent.cad.mcp_server import TOOLS, handle_rpc

    assert "financial_statements" in {item["name"] for item in TOOLS}
    out = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "financial_statements",
                                 "arguments": {"year": "2026",
                                               "ledger_path": str(_ledger_file(closed, tmp_path))}}},
                     {"base_dir": "."})
    payload = json.loads(out["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["balance"]["totals"]["1600"] == 68_400_000
    assert payload["pnl"]["values"]["2400"] == 11_250_000
    assert payload["tax"]["posted"] == 3_750_000
    assert payload["gaps"] == []                     # налог проведён — разрыва нет
    assert payload["readiness"] and payload["readiness"][0]["built"] is False
    assert payload["not_implemented"]
