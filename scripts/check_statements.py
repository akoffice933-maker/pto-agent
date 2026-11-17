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
8. налог на прибыль проведён шагом закрытия года (Дт 99 Кт 68.04): 3 750 000 = 25 % от
   15 000 000, 2410 заполнена из журнала, чистая прибыль 11 250 000, и она же в 1370;
9. расхождение «проведено против посчитано» равно нулю, а не «объявлено нулём»;
10. в отчёте и в CLI написано, чего нет: ОДДС, изменения капитала, пояснения, ПБУ 18/02,
    и названы данные, которых не хватает для ОДК/ОДДС;
11. сравнительные показатели (круг 45): в журнале два закрытых года, графа «на 31 декабря
    предыдущего года» заполняется из того же журнала, входящее сальдо 2026 равно исходящему
    2025, 1370 прошлого года + чистая прибыль года = 1370 текущего, цепочка ОФР прошлого
    года пересчитывается; отсутствие прошлого года ловится как нарушение, а самый первый
    отчётный год — нет (там сравнивать не с чем, и это сказано словами).

    python3 scripts/check_statements.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent import ledger, statements, year_close                  # noqa: E402
from scripts.make_demo_year import build, build_chain                 # noqa: E402

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


def _load(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding="utf-8"))


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
check(line(sheet, "1370")["amount"] == 11_250_000.0,
      f"нераспределённая прибыль {line(sheet, '1370')['amount']}, ожидали 11 250 000 "
      f"(15 000 000 прибыли минус 3 750 000 налога)")
check(line(sheet, "1520")["amount"] == 55_910_000.0,
      f"кредиторка {line(sheet, '1520')['amount']}, ожидали 55 910 000 "
      f"(60.01 + 68.02 + 68.04 после проведения налога)")
check(line(sheet, "1540")["amount"] == 1_240_000.0,
      f"оценочное обязательство {line(sheet, '1540')['amount']}, ожидали 1 240 000")
check(line(sheet, "1150")["amount"] == 0 and line(sheet, "1150")["why_empty"],
      "основные средства: сумма 0 обязана идти с причиной")

values = report["pnl"]["values"]
check(values["2110"] == 55_000_000.0, f"выручка {values['2110']}, ожидали 55 000 000 без НДС")
check(values["2120"] == 40_000_000.0, f"себестоимость {values['2120']}")
check(values["2100"] == 15_000_000.0 and values["2200"] == 15_000_000.0,
      "валовая прибыль и прибыль от продаж расходятся")
check(values["2300"] == 15_000_000.0,
      f"результат до налога {values['2300']}, ожидали 15 000 000")
check(values["2410"] == 3_750_000.0,
      f"налог в ОФР {values['2410']}, ожидали 3 750 000 — строка 2410 берётся из 68.04")
check(values["2411"] == 3_750_000.0,
      f"текущий налог {values['2411']}, ожидали 3 750 000 (отложенного нет)")
check(values["2400"] == 11_250_000.0,
      f"чистая прибыль {values['2400']}, ожидали 11 250 000 (15 000 000 − налог)")
tax_line = next(item for item in report["pnl"]["lines"] if item["code"] == "2412")
check(tax_line.get("not_calculated") and "09" in (tax_line["why_empty"] or ""),
      "строка 2412 (отложенный налог) не названа причиной")
check(check_of(report, "PNL_VS_TURNOVER")["ok"], "ОФР не сошёлся с оборотами журнала")
check(check_of(report, "RETAINED_VS_PNL")["ok"], "прибыль из ОФР не оказалась в капитале")
check(report["gaps"] == [],
      f"разрыв по налогу остался: {report['gaps']} — налог проведён, разрыва быть не должно")
tax = report["tax"]
check(tax["posted"] == 3_750_000.0 and tax["computed"] == 3_750_000.0,
      f"проведено {tax['posted']}, посчитано {tax['computed']}")
check(abs(tax["difference"]) < 0.01,
      f"расхождение «проведено против посчитано» {tax['difference']}")
check("3 750 000,00" in statements.screen_text(report),
      "налог не назван в сводке — цифра должна быть видна в терминале")
check(check_of(report, "TAX_POSTED")["ok"], "проверка TAX_POSTED не прошла")
check(check_of(report, "NO_SILENT_ZEROS")["ok"], "есть строки-нули без причины")

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

# налог не проведён: разрыв обязан появиться цифрой, а не исчезнуть
untaxed = __import__("json").loads(__import__("json").dumps(state))
untaxed["entries"] = [entry for entry in untaxed["entries"]
                      if entry.get("event") != "year.profit_tax"]
lost = statements.check(untaxed, "2026")
check(not check_of(lost, "TAX_POSTED")["ok"], "исчезнувший налог не поймали: TAX_POSTED молчит")
check(lost["gaps"] and lost["gaps"][0]["amount"] == 3_750_000.0,
      f"без проводки налога разрыв {lost['gaps']}, ожидали 3 750 000")

# ОДК/ОДДС: формы не строятся, и названо, каких данных нет
forms = {item["short"]: item for item in report["readiness"]}
check(not forms["ОДДС"]["built"] and forms["ОДДС"]["missing"],
      "ОДДС построился без данных")
check(not forms["ОДК"]["built"] and forms["ОДК"]["missing"],
      "ОДК построилась без данных")
check(any("51" in item for item in forms["ОДДС"]["missing"]),
      "не сказано, что по 51 нет движения денег")
check(any("80" in item for item in forms["ОДК"]["missing"]),
      "не сказано, что счетов капитала 80/81/82/83 нет в плане")

# год: число и строка — одно и то же (круг 44)
probe_year = statements.check(state, 2026)
check(probe_year["ok"] and probe_year["balance"]["totals"]["1600"] == 68_400_000.0,
      "год числом не принимается или даёт другой баланс")

text = statements.to_markdown(report)
check("Бухгалтерский баланс" in text and "Отчёт о финансовых результатах" in text,
      "в markdown нет одной из форм")
check("## Не сделано (и почему)" in text, "в отчёте нет честного списка «не сделано»")
check("изменениях капитала" in text and "ПБУ 18/02" in text,
      "в «не сделано» нет ОДДС/изменений капитала или ПБУ 18/02")
check("## Налог на прибыль" in text and "## Чего не хватает" in text,
      "в отчёте нет блока налога или блока «чего не хватает»")

# ── два года: сравнительные показатели (круг 45) ──────────────────────────────
PRIOR = ROOT / "data" / "demo" / "year_close"
state2, chain = build_chain(PRIOR / "scenario_2025.json", PRIOR / "decisions_2025.json",
                            SCENARIO, DECISIONS)
check(chain["closed"] == ["2025", "2026"], f"закрыты не оба года: {chain['closed']}")
report2 = statements.check(state2, 2026)
check(report2["ok"], f"два года подряд дают нарушения: {[c['code'] for c in report2['problems']]}")
prior = report2["balance"]["prior"]
check(prior["available"] and prior["closed"] and prior["year"] == "2025",
      f"графа прошлого года не заполнена: {prior}")
check(prior["totals"]["1600"] == 54_100_000.0 and prior["totals"]["1700"] == 54_100_000.0,
      f"прошлый год в балансе {prior['totals']}, ожидали 54 100 000")
row = line(report2["balance"], "1370")
check(row["prior"] == 8_790_000.0 and row["amount"] == 20_040_000.0,
      f"1370: прошлый {row['prior']}, текущий {row['amount']}")
row = line(report2["balance"], "1210")
check(row["prior"] == 360_000.0 and row["amount"] == 711_500.0,
      f"1210: прошлый {row['prior']}, текущий {row['amount']}")
check(report2["pnl"]["prior_values"]["2400"] == 8_790_000.0,
      f"чистая прибыль 2025 в ОФР {report2['pnl']['prior_values'].get('2400')}")
check(report2["pnl"]["prior_values"]["2110"] == 44_000_000.0,
      f"выручка 2025 в ОФР {report2['pnl']['prior_values'].get('2110')}")
check(report2["pnl"]["values"]["2400"] == 11_250_000.0,
      f"чистая прибыль 2026 в ОФР {report2['pnl']['values'].get('2400')}")
check(check_of(report2, "OPENING_MATCHES_PRIOR")["ok"],
      "входящее сальдо 2026 не равно исходящему 2025")
check(check_of(report2, "RETAINED_CONTINUITY")["ok"], "1370 не сходится с прошлым годом")
check(check_of(report2, "PNL_PRIOR_CHAIN")["ok"], "цепочка ОФР прошлого года не пересчитывается")
text2 = statements.to_markdown(report2)
check("на 31.12.2025" in text2 and "8 790 000,00" in text2,
      "в markdown нет графы прошлого года с суммой 8 790 000")
check("Годом ранее" in text2, "в markdown не сказано, каким был баланс годом ранее")

# прошлый год есть в журнале, но не закрыт — это нарушение, а не «нет данных»
state3, _ = build(PRIOR / "scenario_2025.json")
state3, _ = build(SCENARIO, state=state3)
year_close.close_year(state3, 2026, decisions=_load(DECISIONS))
probe = statements.check(state3, 2026)
closed_check = check_of(probe, "PRIOR_CLOSED")
check(not closed_check["ok"], "незакрытый прошлый год не поймали: сравнительные повисли в воздухе")

# первый отчётный год: графа пустая, но с причиной — и это не нарушение
first = statements.check(state, 2026)
check(first["ok"], "первый отчётный год теперь ругается на отсутствие сравнений")
check(not check_of(first, "PRIOR_COMPARATIVES")["first_year"] is False,
      "не отмечено, что это первый отчётный год")
check(first["balance"]["prior"]["reason"] and "первый отчётный год" in
      first["balance"]["prior"]["reason"], "причина пустой графы не сказана словами")

fmt = statements.fmt
print(f"два года: 2025 чистая {fmt(report2['pnl']['prior_values']['2400'])} ₽, "
      f"2026 чистая {fmt(report2['pnl']['values']['2400'])} ₽ · баланс годом ранее "
      f"{fmt(prior['totals']['1600'])} ₽ → сейчас {fmt(report2['balance']['totals']['1600'])} ₽")
print(f"отчётность 2026: актив {fmt(sheet['totals']['1600'])} ₽ = пассив "
      f"{fmt(sheet['totals']['1700'])} ₽ · выручка {fmt(values['2110'])} ₽ · "
      f"налог {fmt(tax['posted'])} ₽ · чистая прибыль {fmt(values['2400'])} ₽ · "
      f"разрыв по налогу {fmt(tax['difference'])} ₽")
print(f"проверок: {stats['checks']}")
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
