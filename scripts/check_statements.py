#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сквозная проверка годовой отчётности — шаг CI.

Что проверяется (не «код не упал»):

1. форма и коды строк — из ФСБУ 4/2023 (приказ № 157н, приложение № 10), а не из отменённого 66н;
2. баланс сходится: 1600 = 1700, итоги разделов следуют из строк;
3. каждая заполненная строка объяснима счетами, пустая — причиной;
4. прибыль и убыток в 1370 дают сальдо;
5. ОФР: 55 000 000 − 40 000 000 = 15 000 000, и это совпадает с журналом и с реформацией;
6. прибыль из ОФР оказывается в капитале;
7. отрицательный актив, счёт вне строк, незакрытый 99 и служебный 000 ловятся проверками;
8. налог, который контур посчитал, но не провёл, назван числом (25 % от 15 000 000);
9. в отчёте и в CLI написано, чего нет: ОДДС, изменения капитала, пояснения, ПБУ 18/02.

    python3 scripts/check_statements.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent import ledger, statements, year_close                  # noqa: E402
from scripts.make_demo_year import build                              # noqa: E402

SCENARIO = ROOT / "data" / "demo" / "year_close" / "scenario_2026.json"
DECISIONS = ROOT / "data" / "demo" / "year_close" / "decisions_2026.json"

violations: list[str] = []
stats = {"checks": 0}


def check(condition: bool, message: str) -> None:
    stats["checks"] += 1
    if not condition:
        violations.append(message)


def line(report: dict, code: str) -> dict:
    return next(item for item in report["lines"] if item["code"] == code)


def check_of(report: dict, code: str) -> dict:
    return next(item for item in report["checks"] if item["code"] == code)


def _post(state: dict, *, event: str, lines: list[dict], memo: str, key: str) -> None:
    result = ledger.post_entry(state, event=event, date="2026-12-31",
                               doc={"number": key, "date": "2026-12-31"}, lines=lines,
                               memo=memo, rule=event, key=key,
                               allow_closed=True, late_reason="проба в check_statements")
    check(result["status"] == ledger.STATUS_OK,
          f"{key}: пробная проводка не прошла — {result['errors'] or result['warnings']}")


state, _ = build(SCENARIO)
closing = year_close.close_year(state, "2026", decisions=__import__("json").loads(
    DECISIONS.read_text(encoding="utf-8")))
check(closing["closed"], f"год не закрылся: {closing['problems']}")

rules = statements.load_rules()
standard = rules["standard"]
check("ФСБУ 4/2023" in standard["name"], "форма взята не из ФСБУ 4/2023")
check("157н" in standard["approved"], "не назван приказ, которым утверждён стандарт")
check("66н" in standard["supersedes"], "не сказано, что приказ 66н отменён")
codes = {item["code"] for item in rules["balance"]["lines"]}
check({"1105", "1160", "1215", "1220", "1230", "1260"} <= codes,
      f"в балансе нет строк ФСБУ 4/2023: {sorted(codes)}")
pnl_codes = {item["code"] for item in rules["pnl"]["lines"]}
check({"2300", "2410", "2411", "2412", "2420", "2500"} <= pnl_codes,
      "в ОФР нет строк ФСБУ 4/2023")
for item in rules["balance"]["lines"]:
    if not item.get("accounts"):
        check(bool(item.get("why_empty")), f"строка {item['code']} пустая без причины")

report = statements.check(state, "2026")
sheet = report["balance"]
check(report["ok"], f"проверки отчётности не прошли: "
                    f"{[item['code'] for item in report['problems']]}")
check(sheet["totals"]["1600"] == 68_400_000.0,
      f"актив {sheet['totals']['1600']}, ожидали 68 400 000")
check(sheet["totals"]["1700"] == 68_400_000.0,
      f"пассив {sheet['totals']['1700']}, ожидали 68 400 000")
check(sheet["totals"]["1100"] + sheet["totals"]["1200"] == sheet["totals"]["1600"],
      "актив не равен сумме разделов I и II")
check(sheet["totals"]["1300"] + sheet["totals"]["1400"] + sheet["totals"]["1500"]
      == sheet["totals"]["1700"], "пассив не равен сумме разделов III–V")
check(line(sheet, "1210")["amount"] == 351_500.0,
      f"запасы {line(sheet, '1210')['amount']}, ожидали 351 500 (10.01 после поступления)")
check(line(sheet, "1230")["amount"] == 67_248_500.0,
      f"дебиторка {line(sheet, '1230')['amount']}, ожидали 67 248 500 (62.01 + 73.02)")
check(line(sheet, "1250")["amount"] == 0.0,
      "денежных средств нет вовсе — в демо нет оплат, строка обязана быть пустой")
check(line(sheet, "1370")["amount"] == 15_000_000.0,
      f"нераспределённая прибыль {line(sheet, '1370')['amount']}")
check(line(sheet, "1520")["amount"] == 52_160_000.0,
      f"кредиторка {line(sheet, '1520')['amount']}, ожидали 52 160 000 (60.01 + 68.02)")
check(line(sheet, "1540")["amount"] == 1_240_000.0,
      f"оценочное обязательство {line(sheet, '1540')['amount']}, ожидали 1 240 000")
check(line(sheet, "1150")["amount"] == 0 and line(sheet, "1150")["why_empty"],
      "основные средства: сумма 0 обязана идти с причиной")

values = report["pnl"]["values"]
check(values["2110"] == 55_000_000.0, f"выручка {values['2110']}, ожидали 55 000 000 без НДС")
check(values["2120"] == 40_000_000.0, f"себестоимость {values['2120']}")
check(values["2100"] == 15_000_000.0 and values["2200"] == 15_000_000.0,
      "валовая прибыль и прибыль от продаж расходятся")
check(values["2300"] == 15_000_000.0 and values["2400"] == 15_000_000.0,
      "результат до налога и чистая прибыль расходятся")
tax_line = next(item for item in report["pnl"]["lines"] if item["code"] == "2410")
check(tax_line.get("not_calculated") and "68.04" in tax_line["why_empty"],
      "строка 2410 не пустая с причиной: налог не проведён, это должно быть видно")
check(check_of(report, "PNL_VS_TURNOVER")["ok"], "ОФР не сошёлся с оборотами журнала")
check(check_of(report, "RETAINED_VS_PNL")["ok"], "прибыль из ОФР не оказалась в капитале")
check(report["gaps"][0]["amount"] == 3_750_000.0,
      f"разрыв по налогу {report['gaps'][0]['amount']}, ожидали 3 750 000 (25 %)")
check("3 750 000,00" in statements.screen_text(report), "разрыв по налогу не назван в сводке")

# отрицательный актив ловится
broken = __import__("json").loads(__import__("json").dumps(state))
_post(broken, event="probe.writeoff",
      lines=[{"account": "94", "side": ledger.DEBIT, "amount": 600_000,
              "analytics": {"склад": "Центральный склад"}},
             {"account": "10.01", "side": ledger.CREDIT, "amount": 600_000,
              "analytics": {"склад": "Центральный склад",
                            "номенклатура": "Арматура A500C, d 12 мм"}}],
      memo="списание сверх остатка", key="probe.writeoff")
probe = statements.check(broken, "2026")
check(not check_of(probe, "NO_NEGATIVE_LINES")["ok"],
      "отрицательный актив не поймали: минус в балансе прошёл бы молча")

# счёт вне строк ловится
stray = __import__("json").loads(__import__("json").dumps(state))
_post(stray, event="probe.stray",
      lines=[{"account": "76", "side": ledger.DEBIT, "amount": 300_000,
              "analytics": {"контрагент": "ООО «Заказчик-Инвест»"}},
             {"account": "91.01", "side": ledger.CREDIT, "amount": 300_000}],
      memo="счёт 91.01 остался с сальдо", key="probe.stray")
probe = statements.check(stray, "2026")
check(not check_of(probe, "UNMAPPED_ACCOUNTS")["ok"],
      "счёт с сальдо вне строк баланса не поймали")

# незакрытый 99 ловится
open_year = __import__("json").loads(__import__("json").dumps(state))
_post(open_year, event="probe.profit",
      lines=[{"account": "90.09", "side": ledger.DEBIT, "amount": 1_000_000},
             {"account": "99", "side": ledger.CREDIT, "amount": 1_000_000}],
      memo="результат остался на 99", key="probe.profit")
check(not check_of(statements.check(open_year, "2026"), "PROFIT_CLOSED")["ok"],
      "незакрытый 99 не поймали")

# служебный счёт 000 назван, а не выдан за капитал
service = __import__("json").loads(__import__("json").dumps(state))
_post(service, event="probe.opening",
      lines=[{"account": "51", "side": ledger.DEBIT, "amount": 5_000_000},
             {"account": "000", "side": ledger.CREDIT, "amount": 5_000_000}],
      memo="входящий остаток без капитала", key="probe.opening")
probe = statements.check(service, "2026")
check(not check_of(probe, "SERVICE_ACCOUNT")["ok"]
      and "5 000 000,00" in check_of(probe, "SERVICE_ACCOUNT")["why"],
      "служебный счёт 000 не назван числом")

text = statements.to_markdown(report)
check("Бухгалтерский баланс" in text and "Отчёт о финансовых результатах" in text,
      "в markdown нет одной из форм")
check("## Не сделано (и почему)" in text, "в отчёте нет честного списка «не сделано»")
check("изменениях капитала" in text and "ПБУ 18/02" in text,
      "в «не сделано» нет ОДДС/изменений капитала или ПБУ 18/02")

fmt = statements.fmt
print(f"отчётность 2026: актив {fmt(sheet['totals']['1600'])} ₽ = пассив "
      f"{fmt(sheet['totals']['1700'])} ₽ · выручка {fmt(values['2110'])} ₽ · "
      f"чистая прибыль {fmt(values['2400'])} ₽ (без налога: не проведён, разрыв "
      f"{fmt(report['gaps'][0]['amount'])} ₽)")
print(f"проверок: {stats['checks']}")
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
