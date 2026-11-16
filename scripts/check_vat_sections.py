# -*- coding: utf-8 -*-
"""Проверки раздельного учёта НДС и разделов декларации 3/7/8/9.

Скрипт отвечает на вопросы, из-за которых декларацию по НДС возвращают:

1. пропорция считается по отгруженной выручке, а правило 5 % — по совокупным расходам;
2. нет расходов — правило 5 % «не проверено», а не посчитано от выручки;
3. необлагаемая операция без основания или без документа в расчёт не идёт;
4. код раздела 7 не выдумывается, а противоречие «код ↔ основание» — это ошибка;
5. невычитаемый НДС списывается ровно на сумму раздела 7 и повторно не проводится;
6. разделы 8 и 9 сходятся с разделом 3, а расхождение — это `VAT_SECTION_MISMATCH`;
7. чего в контуре нет — написано, а не умолчано.

Запуск: `python3 scripts/check_vat_sections.py`. Код возврата 1, если есть нарушения.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import invoices, ledger, vat_sections  # noqa: E402

checks = {"done": 0}
violations: list[str] = []


def check(condition: bool, message: str) -> None:
    checks["done"] += 1
    if not condition:
        violations.append(message)


def states(folder: Path) -> tuple[dict, dict]:
    """Журнал и реестр на демо-первичке: приходы с НДС на 19.03 и акты за октябрь."""
    from pto_agent.web import build_demo

    build_demo(folder)
    state = ledger.load(folder / "ledger" / "state.json")
    acts = json.loads((ROOT / "data/demo/accounting/acts_signed_scenario.json")
                      .read_text(encoding="utf-8"))
    ledger.post_acts(state, acts, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    reg = invoices.new_state(object_name="ЖК «Северный», корпус 2")
    invoices.auto_issue(reg, state, "2026-10")
    incoming = json.loads((ROOT / "data/demo/purchases/incoming_2026_10.json")
                          .read_text(encoding="utf-8"))
    invoices.auto_register_purchases(reg, incoming, "2026-10", ledger_state=state)
    return state, reg


EXEMPT = [{"document": "ДКП №14/2026", "date": "2026-10-15", "amount": 6_200_000,
           "basis": "пп. 22 п. 3 ст. 149 НК РФ"}]

with tempfile.TemporaryDirectory() as folder:
    state, reg = states(Path(folder))

    # ── 1. справочник ─────────────────────────────────────────────────────
    rules = vat_sections.load_rules()
    check(rules.get("schema") == vat_sections.SCHEMA,
          f"схема справочника {rules.get('schema')} вместо {vat_sections.SCHEMA}")
    check((rules.get("thresholds") or {}).get("rule5_percent") == 5,
          "порог правила 5 % в справочнике не 5")
    codes = [row.get("code") for row in rules.get("section7_codes") or []]
    check(len(codes) >= 1 and all(len(str(code)) == 7 for code in codes),
          f"коды раздела 7 не 7-значные: {codes}")
    check((rules.get("layout") or {}).get("verify") is True,
          "номера строк разделов не помечены «на проверку»")
    check(bool(rules.get("not_implemented")), "список «не сделано» пуст")

    # ── 2. пропорция и правило 5 % ────────────────────────────────────────
    cheap = vat_sections.separate_accounting(state, "2026-10", exempt=EXEMPT,
                                             expenses_not_taxable=380_000,
                                             expenses_total=12_400_000)
    dear = vat_sections.separate_accounting(state, "2026-10", exempt=EXEMPT,
                                            expenses_not_taxable=1_000_000,
                                            expenses_total=12_400_000)
    check(cheap["rule5"]["checked"] and cheap["rule5"]["applies"] is True,
          "доля расходов 3,06 % — правило 5 % должно применяться")
    check(cheap["totals"]["vat_not_deductible"] == 0,
          "при выполненном правиле 5 % весь входной НДС должен быть к вычету")
    check(dear["rule5"]["applies"] is False and dear["totals"]["vat_not_deductible"] > 0,
          "при доле 8,06 % часть НДС обязана стать невычитаемой")
    check(dear["totals"]["input_vat"] > 0, "входной НДС по демо равен нулю: сценарий пуст")

    no_expenses = vat_sections.separate_accounting(state, "2026-10", exempt=EXEMPT)
    check(no_expenses["rule5"]["checked"] is False,
          "без расходов правило 5 % не должно считаться")
    check(no_expenses["rule5"]["share"] is None,
          "доля по расходам без расходов должна быть None, а не подставленная выручка")
    check(any("расход" in gap["what"] for gap in no_expenses["gaps"]),
          "нет запроса расходов для правила 5 %")

    # доля необлагаемых по выручке на демо
    totals = dear["totals"]
    expected = totals["exempt_revenue"] / (totals["taxable_revenue"]
                                           + totals["exempt_revenue"]) * 100
    check(abs(totals["share_exempt"] - round(expected, 2)) < 0.01,
          f"доля необлагаемых {totals['share_exempt']} против расчётной {expected:.2f}")

    # ── 3. раздел 7: основание, документ, код ────────────────────────────
    no_basis = vat_sections.separate_accounting(
        state, "2026-10", exempt=[{"document": "Акт №1", "date": "2026-10-10",
                                   "amount": 100_000}])
    check("EXEMPT_NO_BASIS" in [item["code"] for item in no_basis["problems"]],
          "операция без основания не помечена как проблема")
    check(no_basis["totals"]["exempt_revenue"] == 0,
          "операция без основания попала в пропорцию")

    no_doc = vat_sections.separate_accounting(
        state, "2026-10", exempt=[{"amount": 100_000,
                                   "basis": "пп. 22 п. 3 ст. 149 НК РФ"}])
    check(any("документ и дата" in gap["what"] for gap in no_doc["gaps"]),
          "операция без документа не попала в запросы")

    mismatch = vat_sections.separate_accounting(
        state, "2026-10", exempt=[{"document": "ДКП №14/2026", "date": "2026-10-15",
                                   "amount": 6_200_000, "code": "1010800",
                                   "basis": "пп. 22 п. 3 ст. 149 НК РФ"}])
    check("EXEMPT_CODE_BASIS_MISMATCH" in [item["code"] for item in mismatch["problems"]],
          "код 1010800 при основании по ст. 149 не распознан как противоречие")

    unknown = vat_sections.separate_accounting(
        state, "2026-10", exempt=[{"document": "ДКП №16/2026", "date": "2026-10-17",
                                   "amount": 1_000_000, "code": "1099999",
                                   "basis": "п. 2 ст. 146 НК РФ"}])
    check(any("код операции раздела 7" in gap["what"] for gap in unknown["gaps"]),
          "неизвестный код раздела 7 принят молча")
    check(unknown["rows"][0]["state"] == "код на проверку",
          "строка с неизвестным кодом не помечена «на проверку»")

    # ── 4. проводка ──────────────────────────────────────────────────────
    before = vat_sections.separate_accounting(state, "2026-10", exempt=EXEMPT,
                                              expenses_not_taxable=1_000_000,
                                              expenses_total=12_400_000)
    target = before["totals"]["vat_not_deductible"]
    posted = vat_sections.post_non_deductible(state, "2026-10", separate=before)
    check(posted.get("posted") is True, f"проводка не проведена: {posted.get('status')}")
    check(abs(float(posted.get("amount") or 0) - target) < 0.01,
          f"сумма проводки {posted.get('amount')} против раздела 7 {target}")
    again = vat_sections.post_non_deductible(state, "2026-10", separate=before)
    check(again.get("posted") is False,
          "повторный вызов создал вторую проводку по тому же ключу")
    keys = [entry.get("key") for entry in state.get("entries") or []]
    check(keys.count("vat.non_deductible|2026-10") == 1,
          "ключ vat.non_deductible|2026-10 встречается не один раз")

    # ── 5. разделы и контрольные соотношения ─────────────────────────────
    report = vat_sections.vat_sections(reg, state, "2026-10", exempt=EXEMPT,
                                       expenses_not_taxable=1_000_000,
                                       expenses_total=12_400_000)
    check(report["section8"]["totals"]["vat"] > 0, "раздел 8 пуст: книга покупок не собрана")
    check(report["section9"]["totals"]["vat"] > 0, "раздел 9 пуст: книга продаж не собрана")
    statuses = {item["what"]: item["status"] for item in report["checks"]}
    check(statuses.get("итог раздела 9 = начислено по журналу") == vat_sections.OK,
          "раздел 9 не сошёлся с начислением")
    check(statuses.get("итог раздела 8 = вычет + НДС не к вычету") == vat_sections.OK,
          "раздел 8 не сошёлся с вычетом плюс невычитаемой частью")
    check(statuses.get("итог графы 4 раздела 7 = НДС не к вычету") == vat_sections.OK,
          "графа 4 раздела 7 не сошлась с невычитаемым НДС")
    check(report["problems"] == [], f"расхождения в разделах: {report['problems']}")
    check(report["section3"]["totals"]["book_deducted"]
          == report["section8"]["totals"]["vat"],
          "раздел 3 считает вычет не по книге покупок")

    text = vat_sections.to_markdown(report)
    for fragment in ("Раздел 3", "Раздел 7", "Раздел 8", "Раздел 9",
                     "Раздельный учёт", "Контрольные соотношения", "Не сделано"):
        check(fragment in text, f"в выгрузке нет раздела «{fragment}»")

    # ── 6. пустой период ─────────────────────────────────────────────────
    empty_state = ledger.new_state(object_name="ЖК «Северный», корпус 2")
    empty_reg = invoices.new_state(object_name="ЖК «Северный», корпус 2")
    empty = vat_sections.vat_sections(empty_reg, empty_state, "2026-11")
    check(any("книга продаж" in gap["what"] for gap in empty["gaps"]),
          "пустая книга продаж не названа")
    check(empty["layout_verify"]["verify"] is True,
          "в пустом отчёте нет пометки «номера строк на проверку»")

    # ── 7. дата проводки ─────────────────────────────────────────────────
    check(vat_sections._last_day("2026-10") == "2026-10-31",
          f"последний день периода {vat_sections._last_day('2026-10')}")
    check(vat_sections._last_day("2026-02") == "2026-02-28",
          f"последний день февраля {vat_sections._last_day('2026-02')}")

print(f"проверок: {checks['done']}, нарушений: {len(violations)}")
for item in violations:
    print(f"  ✗ {item}")
sys.exit(1 if violations else 0)
