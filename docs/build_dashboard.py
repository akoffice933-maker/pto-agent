#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор дашборда руководителя: прозрачность, стадия проекта,
остаточные объёмы и деньги, качество и стоимость ИИ, журнал действий.

Все числа рассчитываются из массива данных ниже — чтобы на дашборде
не было арифметических противоречий между карточками и таблицами.
Данные синтетические (демонстрационные).
"""
from datetime import date

AS_OF = "02.11.2026 09:40"
OBJECT = "ЖК «Северный», корпус 2"
CONTRACT = "Д-142/2026"

# --- остаточные объёмы ------------------------------------------------------
# (код, вид работ, ед., смета, подписано КС-2, выполнено но не закрыто, цена ₽/ед., статус)
WORKS = [
    ("01", "Земляные работы", "м³", 8600, 8600, 0, 850, "done"),
    ("02", "Бетонная подготовка", "м³", 420, 420, 0, 6200, "done"),
    ("03", "Монолитные конструкции (надземная часть)", "м³", 3450, 2140, 180, 12400, "risk"),
    ("04", "Армирование", "т", 610, 402, 24, 78000, "active"),
    ("05", "Гидроизоляция фундаментов", "м²", 4200, 4200, 0, 980, "done"),
    ("06", "Кладка наружных стен", "м³", 1850, 940, 60, 8900, "active"),
    ("07", "Монтаж трубопроводов ОВ", "м", 5400, 2650, 320, 1850, "risk"),
    ("08", "Монтаж систем вентиляции", "м", 2100, 780, 130, 3400, "active"),
    ("09", "Электромонтажные работы", "м кабеля", 18600, 6300, 900, 420, "active"),
    ("10", "Кровельные работы", "м²", 3100, 590, 0, 2450, "wait"),
    ("11", "Отделочные работы", "м²", 12400, 0, 0, 1350, "wait"),
    ("12", "Благоустройство", "м²", 2800, 0, 0, 1650, "wait"),
]

STATUS_LABEL = {
    "done": ("Завершено", "ok"),
    "active": ("В работе", "active"),
    "risk": ("Отставание", "risk"),
    "wait": ("Не начато", "wait"),
}


def money(v):
    return f"{v:,.0f}".replace(",", " ")


def num(v):
    s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return s.replace(",", " ").replace(".", ",")


def pct(v):
    return f"{v:.1f}".replace(".", ",") + " %"


rows = []
t_est_qty = t_done_qty = t_wip_qty = 0
t_amount_est = t_amount_done = t_amount_rest = 0
by_status = {}
for code, name, unit, est, done, wip, price, status in WORKS:
    rest = est - done - wip
    amount_est = est * price
    amount_done = done * price
    amount_rest = rest * price
    row = dict(code=code, name=name, unit=unit, est=est, done=done, wip=wip, rest=rest,
               price=price, status=status, amount_est=amount_est,
               amount_done=amount_done, amount_rest=amount_rest,
               pct=100 * done / est, wip_pct=100 * wip / est)
    rows.append(row)
    t_est_qty += 0
    t_amount_est += amount_est
    t_amount_done += amount_done
    t_amount_rest += amount_rest
    by_status[status] = by_status.get(status, 0) + 1

# деньги по договору
vat = 0.2
closed_signed = t_amount_done                      # подписано заказчиком
submitted = 24_780_000                             # предъявлено, на проверке
received = closed_signed * 0.86                    # оплачено заказчиком
contract_with_vat = t_amount_est * (1 + vat)
remaining_to_submit = t_amount_est - closed_signed - submitted
ready_to_close = sum(r["amount_rest"] for r in rows if r["status"] == "done" and False)
work_ready_to_act = sum(r["done"] for r in rows) * 0  # заполним ниже
# «готово к предъявлению» = выполнено, но не закрыто актом
ready_wip_amount = sum(r["wip"] * r["price"] for r in rows)

overall_pct = 100 * t_amount_done / t_amount_est
overall_with_wip = 100 * (t_amount_done + ready_wip_amount) / t_amount_est

# --- стадии проекта внедрения ---------------------------------------------
STAGES = [
    ("Этап 0", "Обследование, сбор корпуса, golden set", "10.11–05.12.2026", 100,
     "Собрано 74 из 100 пакетов, размечено 12 актов КС-2, 20 норм-вопросов",
     "Разметка: 138 из 360 единиц", "ok"),
    ("MVP", "КС-2, сверка объёмов, комплектность, АОСР", "08.12.2026–27.02.2027", 72,
     "Сервис расчётов и сверки на golden set: recall ошибок 0,91 (порог 0,95)",
     "Доработка: recall 0,91 → 0,95, АОСР в работе", "active"),
    ("Пилот", "1–2 объекта, 3–5 инженеров", "02.03–10.04.2027", 0,
     "Не начат — ждёт готовности MVP", "Подготовить регламент HITL и обучение", "wait"),
    ("Версия 1.0", "1С, реестры, графики, входной контроль", "13.04–25.06.2027", 0,
     "Не начат", "Согласовать доступ к 1С (тестовый контур)", "wait"),
    ("CAD + 2.0", "Исполнительные схемы, мультиагентность, BIM", "28.06–24.12.2027", 0,
     "Не начат", "Собрать 20 redline-кейсов на этапе 0", "wait"),
]

# --- качество и стоимость ИИ ----------------------------------------------
AI = dict(calls_24h=1840, calls_month=41_260, tokens_in=61_400_000, tokens_out=8_900_000,
          cost_month=41_260 * 0.83, cost_limit=60_000,
          citation_ok=99.4, blocked=7, halluc=1, avg_latency=6.8, p95=13.2,
          human_confirm=214, auto_done=1626)

PROVIDERS = [
    ("vLLM · Qwen2.5-32B-Instruct (on-prem)", "Локально, контур КТ", 0, 0, 21_400,
     "КС-2, АОСР, документы с ПДн и КТ", "ok"),
    ("vLLM · Qwen2.5-VL-32B (on-prem)", "Локально, контур КТ", 0, 0, 6_320,
     "Сканы, redline, сертификаты", "ok"),
    ("GigaChat Pro (API, РФ-контур)", "Защищённое облако", 0.55, 1.10, 9_180,
     "Длинные рассуждения, проверка актов", "ok"),
    ("YandexGPT Pro (API, РФ-контур)", "Защищённое облако", 0.50, 1.00, 3_260,
     "Резервный провайдер (fallback)", "ok"),
    ("OpenRouter / зарубежные API", "Запрещено политикой", 0, 0, 0,
     "Только R&D без реальных данных", "blocked"),
]
provider_sum = sum(p[4] for p in PROVIDERS)
cost_by_provider = []
for name, where, pin, pout, calls, purpose, st in PROVIDERS:
    # распределение токенов по провайдеру
    share = calls / provider_sum if provider_sum else 0
    cost = share * AI["tokens_in"] / 1000 * pin + share * AI["tokens_out"] / 1000 * pout
    cost_by_provider.append((name, calls, cost, purpose, where, st))

# --- журнал действий -------------------------------------------------------
AUDIT = [
    ("09:38", "Инженер ПТО", "А. Ковалёв", "КС-2 №38 за октябрь", "Подтвердил выпуск акта",
     "сумма 4 812 400 ₽ · подпись за инженером", "prompt v1.4 · Qwen-32B", "a4f1…9c2e", "ok"),
    ("09:34", "Агент «Сметчик»", "estimator", "КС-2 №38", "Черновик: 42 позиции, 6 расхождений",
     "источники: журнал работ №10, смета v3", "prompt v1.4 · Qwen-32B", "7be2…41dd", "agent"),
    ("09:31", "Система", "cite_verify", "Нормативная справка", "Заблокирован ответ: цитата не найдена в п. 6.1.5",
     "требуется проверка источника вручную", "validator v2", "09ac…b7f3", "block"),
    ("09:12", "Инженер ПТО", "М. Гасанова", "АОСР-142-31", "Исправила ссылку на РД после замечания агента",
     "было: 142-КЖ1 л.9 → стало: л.12", "—", "5d10…2a8b", "ok"),
    ("08:55", "Агент «Чертёжник»", "cad", "ИС-142-07", "Применено 4 операции, 1 пометка → на человека",
     "redline_07.pdf · уверенность 0,42", "Qwen2.5-VL · prompt v0.9", "c3f8…6d14", "agent"),
    ("08:47", "Инженер ПТО", "А. Ковалёв", "ИС-142-07", "Подтвердил diff-изменения чертежа",
     "версия 4 → 5, предыдущая сохранена", "—", "c3f8…6d14", "ok"),
    ("08:20", "Агент «Документатор»", "documenter", "Пакет ИД: монолит", "Комплектность 6/8, блокер: протокол испытаний",
     "запрос направлен в лабораторию", "Qwen-32B · prompt v1.2", "1ab9…77c0", "agent"),
    ("07:05", "Планировщик", "scheduler", "Отчёт руководителю", "Сформирован утренний срез и отправлен",
     "получатели: 4", "—", "ee01…3f9a", "ok"),
]

ALERTS = [
    ("Критично", "Монолитные конструкции: остаток 1 130 м³ при отставании 11 дней", "Монолитные конструкции (надземная часть)", "risk"),
    ("Критично", "316 позиций КС-2 без подтверждающей ИД на 18,4 млн ₽", "Комплектность ИД", "risk"),
    ("Критично", "Сертификат на бетон В25 партии 4412: истёк 30.10.2026", "Входной контроль", "risk"),
    ("Существенно", "Расхождение объёмов ОВ: акт субподрядчика +7,4 % к смете", "Монтаж трубопроводов ОВ", "warn"),
    ("Существенно", "Кровля: подрядчик не вышел на захватку, работа не начата", "Кровельные работы", "warn"),
    ("Инфо", "5 инструментов агента вызывались с ошибкой таймаута (1С)", "Интеграции", "info"),
]

# --- SVG: помесячный план/факт -------------------------------------------
MONTHS = [("май", 6.2, 6.0), ("июн", 8.4, 7.9), ("июл", 10.1, 9.4),
          ("авг", 11.6, 10.2), ("сен", 12.2, 9.1), ("окт", 12.8, 7.6)]
CHART_W, CHART_H = 640, 210
vmax = max(max(p, f) for _, p, f in MONTHS) * 1.25
bar_group = CHART_W / len(MONTHS)
bars = []
for i, (m, plan, fact) in enumerate(MONTHS):
    x0 = i * bar_group + bar_group * 0.18
    hp = plan / vmax * (CHART_H - 40)
    hf = fact / vmax * (CHART_H - 40)
    bars.append(
        f'<rect x="{x0:.1f}" y="{CHART_H-30-hp:.1f}" width="{bar_group*0.28:.1f}" height="{hp:.1f}" fill="#8FA6C8" rx="2"/>'
        f'<rect x="{x0+bar_group*0.30:.1f}" y="{CHART_H-30-hf:.1f}" width="{bar_group*0.28:.1f}" height="{hf:.1f}" fill="#1F3864" rx="2"/>'
        f'<text x="{x0+bar_group*0.15:.1f}" y="{CHART_H-12}" font-size="11" fill="#5A5A5A" text-anchor="middle">{m}</text>'
        f'<text x="{x0+bar_group*0.30:.1f}" y="{CHART_H-30-hf-5:.1f}" font-size="10" fill="#1F3864" text-anchor="middle">{str(fact).replace(".", ",")}</text>'
    )
grid = "".join(
    f'<line x1="0" y1="{CHART_H-30-(v/vmax)*(CHART_H-40):.1f}" x2="{CHART_W}" y2="{CHART_H-30-(v/vmax)*(CHART_H-40):.1f}" stroke="#E4E9F2"/>'
    f'<text x="-6" y="{CHART_H-26-(v/vmax)*(CHART_H-40):.1f}" font-size="10" fill="#8894A6" text-anchor="end">{v}</text>'
    for v in (0, 4, 8, 12, 16)
)

# --- деньги под риском: берём из модуля impact (реальный прогон выпуска) ---
import html as _html
import json as _json
import os


def load_impact():
    """Сводка «выпуск → деньги» из последнего прогона. Нет прогона — нет блока."""
    for path in ("out/cad/batch/batch_summary.json", "out/cad/batch/impact.json",
                 "out/cad/impact/impact.json"):
        if not os.path.exists(path):
            continue
        try:
            data = _json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        impact = data.get("impact", data) if isinstance(data, dict) else None
        if impact and impact.get("totals"):
            return impact, path
    return None, None


def load_portfolio():
    """Портфель актов: деньги под риском по всем актам объекта."""
    for path in ("out/cad/portfolio/portfolio.json",):
        if not os.path.exists(path):
            continue
        try:
            data = _json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("totals"):
            return data, path
    return None, None


IMPACT, IMPACT_SRC = load_impact()
PORTFOLIO, PORTFOLIO_SRC = load_portfolio()

if PORTFOLIO:
    POSITIONS = PORTFOLIO["positions"]
    UNPRICED = PORTFOLIO["unpriced"]
elif IMPACT:
    POSITIONS = IMPACT["positions"]
    UNPRICED = IMPACT["unpriced"]
else:
    POSITIONS, UNPRICED = [], []

RISK_AT = (PORTFOLIO["totals"]["at_risk_rub"] if PORTFOLIO
           else IMPACT["totals"]["at_risk_rub"] if IMPACT else 0.0)
RISK_MARKUP = (PORTFOLIO["totals"]["markup_rub"] if PORTFOLIO
               else IMPACT["totals"]["markup_rub"] if IMPACT else 0.0)
RISK_SHARE_ACT = (PORTFOLIO["totals"].get("share_of_checked_pct") if PORTFOLIO
                  else IMPACT["totals"]["share_of_act_pct"] if IMPACT else 0.0) or 0.0
RISK_SHARE_OBJECT = 100 * RISK_AT / closed_signed if closed_signed else 0.0

if RISK_AT:
    _top = next((p for p in POSITIONS if p.get("money_release_rub")), None)
    _where = (f" — {_top['code']}"
              + (f", акты: {', '.join(_top['acts'])}" if _top.get("acts")
                 else f", листов {len(_top['sheets'])}" if _top else ""))
    ALERTS.insert(0, ("Критично",
                      f"Выпуск: споры с актом на {RISK_AT:,.0f} ₽".replace(",", " ") + _where,
                      "Деньги под риском · акты объекта", "risk"))
if PORTFOLIO and PORTFOLIO["totals"]["acts_unchecked"]:
    _t = PORTFOLIO["totals"]
    ALERTS.insert(1, ("Существенно",
                      f"{_t['acts_unchecked']} акта на "
                      f"{_t['amount_unchecked_rub']:,.0f} ₽ подписаны без проверки выпуска"
                      .replace(",", " "),
                      "Деньги под риском · покрытие проверкой", "warn"))


def plural(n: int, forms: tuple) -> str:
    """1 акт · 2 акта · 5 актов — чтобы карточки читались по-русски."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def risk_cards():
    if PORTFOLIO:
        t = PORTFOLIO["totals"]
        return f"""
  <div class="cards four">
    <div class="card alert-card">
      <div class="k">Под риском по актам</div>
      <div class="v">{money(t['at_risk_rub'])} ₽</div>
      <div class="d">{num(t['share_of_checked_pct'])} % проверенной суммы ·
      {num(t['share_of_all_pct'])} % всех актов<br>
      позиций: <b>{t['positions_priced']}</b> · листов: <b>{t['sheets_total']}</b></div>
    </div>
    <div class="card">
      <div class="k">Проверено выпуском</div>
      <div class="v">{num(t['coverage_pct'])} %</div>
      <div class="d">{money(t['amount_checked_rub'])} ₽ из {money(t['amount_rub'])} ₽<br>
      актов проверено: <b>{t['acts_checked']} из {t['acts_total']}</b></div>
    </div>
    <div class="card {'' if False else ('alert-card' if t['acts_unchecked'] else 'ok-card')}">
      <div class="k">{'Подписано без проверки' if t['acts_unchecked'] else 'Без проверки не осталось'}</div>
      <div class="v">{money(t['amount_unchecked_rub'])} ₽</div>
      <div class="d">{_unchecked_line(t)}</div>
    </div>
    <div class="card">
      <div class="k">Фон ПТО (не из выпуска)</div>
      <div class="v">{money(t['background_risk_rub'])} ₽</div>
      <div class="d">расхождения объекта, известные до сканов;<br>с выпуском <b>не складываются</b></div>
    </div>
  </div>"""
    if not IMPACT:
        return ('<div class="note">Раздел заполнится после первого прогона выпуска: '
                '<span class="mono">cad scan-batch … --summary out/summary.json</span>, '
                'затем <span class="mono">cad impact --acts data/demo/acts/acts.json</span>.</div>')
    t = IMPACT["totals"]
    return f"""
  <div class="cards four">
    <div class="card alert-card">
      <div class="k">Под риском по выпуску</div>
      <div class="v">{money(t['at_risk_rub'])} ₽</div>
      <div class="d">{num(t['share_of_act_pct'])} % акта · {num(RISK_SHARE_OBJECT)} % от закрытого<br>
      пометок со спором: <b>{t['occurrences_total']}</b> · листов: <b>{t['sheets_total']}</b></div>
    </div>
    <div class="card">
      <div class="k">Завышение / недополучено</div>
      <div class="v">{money(t['markup_rub'])} ₽</div>
      <div class="d">завышение против акта — риск отказа заказчика<br>
      не предъявлено: <b>{money(t['underclaim_rub'])} ₽</b></div>
    </div>
    <div class="card">
      <div class="k">Фон ПТО (не из выпуска)</div>
      <div class="v">{money(t['background_risk_rub'])} ₽</div>
      <div class="d">расхождения объекта, известные до сканов</div>
    </div>
    <div class="card ok-card">
      <div class="k">Не оценено в деньгах</div>
      <div class="v">{t['items_unpriced']} <span style="font-size:13px;font-weight:400;color:#6B7688">строки</span></div>
      <div class="d">без цены или объёма — с причиной</div>
    </div>
  </div>"""


def _unchecked_line(t: dict) -> str:
    if t["acts_unchecked"]:
        return (f"{t['acts_unchecked']} {plural(t['acts_unchecked'], ('акт', 'акта', 'актов'))}: "
                f"это <b>не «0 ₽ риска»</b>, а отсутствие проверки<br>"
                f"следующий выпуск — по ним")
    return (f"все {t['acts_total']} {plural(t['acts_total'], ('акт', 'акта', 'актов'))} "
            f"прошли выпуск, включая ретро<br>непроверенных актов на объекте не осталось")


def acts_months_tables():
    """Таблицы «по актам» и «по месяцам» — одна страница вместо трёх отчётов."""
    if not PORTFOLIO:
        return ""
    acts_rows = ""
    for act in PORTFOLIO["acts"]:
        checked = act["status"] == "проверен"
        risk = money(act["at_risk_rub"]) if checked else "не оценено"
        share = num(act["share_pct"]) + " %" if act.get("share_pct") is not None else "—"
        badge = ('<span class="badge b-risk">споры</span>' if act["at_risk_rub"]
                 else '<span class="badge b-ok">проверен</span>' if checked
                 else '<span class="badge b-warn">не проверялся</span>')
        acts_rows += f"""
    <tr{' class="row-risk"' if act['at_risk_rub'] else ''}>
      <td class="mono nowrap">{_html.escape(act['act_id'])}</td>
      <td class="small">{_html.escape(act['month_label'])}</td>
      <td class="num">{money(act['amount_rub'])}</td>
      <td class="num strong">{risk}</td>
      <td class="num">{share}</td>
      <td>{badge}</td>
    </tr>"""
    months_rows = ""
    for m in PORTFOLIO["months"]:
        assessed = m.get("risk_assessed", True)
        risk = money(m["at_risk_rub"]) if assessed else "не оценено"
        share = num(m["share_pct"]) + " %" if m.get("share_pct") is not None else "—"
        months_rows += f"""
    <tr>
      <td class="small nowrap">{_html.escape(m['label'])}</td>
      <td class="num">{len(m['acts'])}</td>
      <td class="num">{money(m['amount_rub'])}</td>
      <td class="num strong">{risk}</td>
      <td class="num">{share}</td>
    </tr>"""
    t = PORTFOLIO["totals"]
    return f"""
  <div class="two" style="margin-top:16px">
    <div class="panel">
      <h3>Акты объекта: что проверено и что под вопросом</h3>
      <table class="inner">
        <thead><tr><th>Акт</th><th>Месяц</th><th class="num">Сумма, ₽</th>
              <th class="num">Под риском</th><th class="num">Доля</th><th>Статус</th></tr></thead>
        <tbody>{acts_rows}</tbody>
        <tfoot><tr><td colspan="2">Итого {t['acts_total']} акта</td>
          <td class="num">{money(t['amount_rub'])}</td>
          <td class="num">{money(t['at_risk_rub'])}</td>
          <td class="num">{num(t['share_of_all_pct'])} %</td>
          <td class="small mut">покрытие {num(t['coverage_pct'])} %</td></tr></tfoot>
      </table>
      <p class="hint" style="margin:10px 0 0">Под риском считается только по проверенным актам:
      у актов без распознанного выпуска стоит «не оценено», а не ноль.</p>
    </div>
    <div class="panel">
      <h3>По месяцам: сколько предъявлено и сколько под вопросом</h3>
      <table class="inner">
        <thead><tr><th>Месяц</th><th class="num">Актов</th><th class="num">Сумма, ₽</th>
              <th class="num">Под риском</th><th class="num">Доля</th></tr></thead>
        <tbody>{months_rows}</tbody>
      </table>
      <p class="hint" style="margin:10px 0 0">График финансирования: деньги под риском
      уменьшают то, что можно уверенно планировать к поступлению в этом месяце.</p>
    </div>
  </div>"""


def risk_rows():
    if not POSITIONS:
        return ""
    out = ""
    for pos in POSITIONS:
        release = pos.get("money_release_rub") or 0
        background = pos.get("money_background_rub") or 0
        if not (release or background):
            continue
        risks = list(dict.fromkeys(r["risk_title"] for r in pos["risks"]))
        unique = []
        for r in pos["risks"]:
            formula = r.get("formula") or ""
            formula = formula.split(": ", 1)[-1] if ":" in formula else formula
            if formula and formula not in unique:
                unique.append(formula)
        formulas = "; ".join(unique)[:160]
        where = len(pos.get("acts") or []) or len(pos.get("sheets") or [])
        out += f"""
    <tr{' class="row-risk"' if release else ''}>
      <td class="mono nowrap">{_html.escape(pos['code'] or '—')}</td>
      <td>{_html.escape(pos['name'][:62])}<div class="sub">{_html.escape(', '.join(risks))}</div></td>
      <td class="num strong">{money(release) if release else '—'}</td>
      <td class="num">{money(background) if background else '—'}</td>
      <td class="small mut">{_html.escape(formulas) or '—'}</td>
      <td class="num">{where or '—'}</td>
    </tr>"""
    return out


def risk_chart_svg():
    """Полосы «где деньги»: тёмная часть — пометки выпуска, серая — фон ПТО."""
    positions = [p for p in POSITIONS if p["money_rub"]][:6]
    if not positions:
        return ""
    bar_w, row_h, label_w = 430, 34, 210
    height = row_h * len(positions) + 34
    peak = max(p["money_rub"] for p in positions) or 1
    parts = [f'<svg viewBox="0 0 {label_w + bar_w + 130} {height}" '
             f'style="width:100%;height:auto" role="img" aria-label="Суммы споров по позициям">']
    for i, pos in enumerate(positions):
        y = i * row_h + 10
        release = pos.get("money_release_rub") or 0
        background = pos.get("money_background_rub") or 0
        w_r = release / peak * bar_w
        w_b = background / peak * bar_w
        label = pos["code"] or "—"
        parts.append(f'<text x="{label_w - 10}" y="{y + 13}" font-size="12" fill="#1E2430" '
                     f'text-anchor="end">{_html.escape(label)}</text>')
        if release:
            parts.append(f'<rect x="{label_w}" y="{y}" width="{w_r:.1f}" height="18" '
                         f'fill="#C0502A" rx="2"/>')
            parts.append(f'<text x="{label_w + w_r / 2:.1f}" y="{y + 13}" font-size="11" '
                         f'fill="#fff" text-anchor="middle">{money(release / 1000)}</text>')
        if background:
            parts.append(f'<rect x="{label_w + w_r:.1f}" y="{y}" width="{w_b:.1f}" height="18" '
                         f'fill="#C3C9D4" rx="2"/>')
            parts.append(f'<text x="{label_w + w_r + w_b + 6:.1f}" y="{y + 13}" font-size="11" '
                         f'fill="#6B7688">{money(background / 1000)}</text>')
    parts.append(f'<text x="{label_w}" y="{height - 6}" font-size="11" fill="#6B7688">'
                 f'тыс. ₽ спора · тёмное — пометки выпуска, серое — фон ПТО</text>')
    parts.append("</svg>")
    return "".join(parts)


def risk_unpriced():
    if not UNPRICED:
        return '<p class="small mut">Все споры получили цену — неоценённых строк нет.</p>'
    out = ""
    for u in UNPRICED:
        out += (f'<div class="kv"><span class="k">{_html.escape(u.get("code") or "—")} · '
                f'{_html.escape(u.get("conflict_type") or "")}</span>'
                f'<span class="v small">{_html.escape(u.get("note") or "")}'
                f'<span class="mut"> · повторов {u.get("occurrences", 1)}</span></span></div>')
    return out


if PORTFOLIO:
    _t = PORTFOLIO["totals"]
    STRIP_SCOPE = f"{_t['acts_total']} акта объекта"
    STRIP_DETAIL = (f"это {num(_t['share_of_checked_pct'])} % проверенной суммы и "
                    f"{num(_t['share_of_all_pct'])} % всех актов · "
                    f"проверено {num(_t['coverage_pct'])} % предъявленного"
                    + (f" · без проверки {money(_t['amount_unchecked_rub'])} ₽"
                       if _t["acts_unchecked"] else ""))
elif IMPACT:
    STRIP_SCOPE = "выпуск, акт №38"
    STRIP_DETAIL = (f"это {num(RISK_SHARE_ACT)} % акта и {num(RISK_SHARE_OBJECT)} % "
                    f"от закрытого по объекту · завышение {money(RISK_MARKUP)} ₽")
else:
    STRIP_SCOPE, STRIP_DETAIL = "деньги под риском", "прогон выпуска ещё не выполнялся"

_SOURCE = PORTFOLIO_SRC or IMPACT_SRC
_STAMP = ""
if _SOURCE:
    _gen = (PORTFOLIO or IMPACT or {}).get("generated_at") or ""
    _STAMP = (" · расчёт от " + ".".join(reversed(_gen[:10].split("-"))) if _gen else "")

risk_section = f"""
<section id="risk">
  <h2>Деньги под риском: чего стоит спор с актом</h2>
  <p class="hint">Выпуск распознан, каждая пометка инженера сверена с КС-2 и сметой, спор переведён в рубли
  по цене позиции и разложен по актам объекта. Источник: <span class="mono">{_SOURCE or '—'}</span>{_STAMP}.</p>
  {risk_cards()}
  {acts_months_tables()}
  {('<div class="two" style="margin-top:16px"><div class="panel"><h3>Где лежат деньги: сумма спора по позициям</h3>' + risk_chart_svg() + '</div><div class="panel"><h3>Что не удалось оценить в деньгах</h3>' + risk_unpriced() + '</div></div>') if POSITIONS else ''}
  {('<table class="inner" style="margin-top:16px"><thead><tr><th style="width:170px">Позиция</th><th>Наименование и вид спора</th>'
    '<th class="num" style="width:130px">По выпуску, ₽</th><th class="num" style="width:120px">Фон ПТО, ₽</th>'
    '<th style="width:230px">Формула</th><th class="num" style="width:90px">Актов/листов</th></tr></thead>'
    '<tbody>' + risk_rows() + '</tbody></table>') if POSITIONS else ''}
  <div class="note" style="margin-top:16px">
    <b>Четыре правила, без которых цифра врёт.</b> (1) Один спор считается один раз: пометка «1 300 м³» стоит
    на каждой итерации листа, но деньги по ней — одни; повторы и листы показываются отдельно.
    (2) Пометки выпуска и фон ПТО не складываются: второе было известно до сканов.
    (3) Непроверенный акт — это «не оценено», а не «0 ₽»: ноль читался бы как «там всё чисто».
    (4) Нет цены или объёма — нет суммы: строка уходит в «не оценено» с причиной.
    Сумма «под риском» — не «потеряно»: чем закончится спор, решают документы и заказчик.
  </div>
</section>
"""

# ------------------------------------------------------------------ HTML ---
def status_badge(status):
    label, cls = STATUS_LABEL[status]
    return f'<span class="badge b-{cls}">{label}</span>'


def bar(p, wip=0.0, color="#2E5C9A"):
    wip_html = f'<span class="bar-wip" style="width:{min(wip, 100-p):.1f}%"></span>' if wip else ""
    return (f'<span class="bar"><span class="bar-done" style="width:{p:.1f}%;background:{color}"></span>{wip_html}</span>')


work_rows = ""
for r in rows:
    rest_amount = r["amount_rest"]
    color = {"done": "#2E7D5B", "active": "#2E5C9A", "risk": "#C0502A", "wait": "#9AA6B8"}[r["status"]]
    work_rows += f"""
    <tr class="{'row-risk' if r['status']=='risk' else ''}">
      <td class="mono">{r['code']}</td>
      <td>{r['name']}</td>
      <td class="num">{num(r['est'])}</td>
      <td class="num ok-t">{num(r['done'])}</td>
      <td class="num wip-t">{num(r['wip'])}</td>
      <td class="num strong">{num(r['rest'])}</td>
      <td class="num">{money(rest_amount)}</td>
      <td class="bar-cell">{bar(r['pct'], r['wip_pct'], color)}<span class="pct">{pct(r['pct'])}</span></td>
      <td>{status_badge(r['status'])}</td>
    </tr>"""

stage_rows = ""
for name, scope, dates, prog, fact, nxt, cls in STAGES:
    stage_rows += f"""
    <tr>
      <td><b>{prog} %</b></td>
      <td><b>{name}</b><div class="sub">{scope}</div></td>
      <td class="mono nowrap">{dates}</td>
      <td><div class="prog"><div class="prog-fill" style="width:{prog}%;background:{'#2E7D5B' if prog==100 else '#2E5C9A' if prog else '#C9D2E0'}"></div></div></td>
      <td class="small">{fact}</td>
      <td class="small mut">{nxt}</td>
    </tr>"""

audit_rows = ""
for t, actor, aid, obj, action, detail, model, h, kind in AUDIT:
    icon = {"ok": "✓", "agent": "◆", "block": "!"}[kind]
    audit_rows += f"""
    <tr>
      <td class="mono nowrap">{t}</td>
      <td><span class="dot d-{kind}">{icon}</span> <b>{actor}</b><div class="sub mono">{aid}</div></td>
      <td>{obj}</td>
      <td>{action}<div class="sub">{detail}</div></td>
      <td class="small mut nowrap">{model}</td>
      <td class="mono small mut">{h}</td>
    </tr>"""

prov_rows = ""
for name, calls, cost, purpose, where, st in cost_by_provider:
    share = 100 * calls / provider_sum if provider_sum else 0
    label = {"ok": '<span class="badge b-ok">активен</span>',
             "blocked": '<span class="badge b-risk">запрещён</span>'}[st]
    prov_rows += f"""
    <tr>
      <td><b>{name}</b><div class="sub">{where}</div></td>
      <td class="num">{calls if calls else '—'}</td>
      <td class="num">{money(cost) + ' ₽' if cost else '—'}</td>
      <td class="bar-cell">{bar(share if calls else 0, 0, '#2E5C9A')}<span class="pct">{pct(share) if calls else '—'}</span></td>
      <td class="small">{purpose}</td>
      <td>{label}</td>
    </tr>"""

alert_rows = ""
for sev, text, scope, cls in ALERTS:
    badge_cls = {"risk": "b-risk", "warn": "b-warn", "info": "b-info"}[cls]
    alert_rows += f"""
    <div class="alert a-{cls}">
      <span class="badge {badge_cls}">{sev}</span>
      <div><div>{text}</div><div class="sub">{scope}</div></div>
      <button class="btn-ghost" onclick="alert('Демонстрационный режим: карточка решения откроется в системе')">Решить</button>
    </div>"""

html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ПТО-контроль · дашборд руководителя</title>
<style>
  :root {{
    --navy:#1F3864; --blue:#2E5C9A; --accent:#C0502A; --ok:#2E7D5B;
    --warn:#B7791F; --info:#4A6FA5; --bg:#F4F6FA; --card:#FFFFFF;
    --border:#DCE3EE; --text:#1E2430; --mut:#6B7688;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
        font:15px/1.5 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; }}
  .topbar {{ background:var(--navy); color:#fff; padding:18px 28px 16px; }}
  .topbar h1 {{ margin:0; font-size:22px; letter-spacing:.2px; }}
  .topbar .crumbs {{ font-size:13px; color:#B8C7DF; margin-top:4px; }}
  .topbar .right {{ float:right; text-align:right; font-size:12.5px; color:#B8C7DF; }}
  .badge-demo {{ display:inline-block; background:#C0502A; color:#fff; font-size:11px;
                 font-weight:700; padding:2px 8px; border-radius:10px; letter-spacing:.4px; }}
  .wrap {{ max-width:1240px; margin:0 auto; padding:0 28px 60px; }}
  nav.tabs {{ position:sticky; top:0; z-index:5; background:var(--bg); padding:12px 0 10px;
             border-bottom:1px solid var(--border); margin-bottom:20px; }}
  nav.tabs a {{ display:inline-block; margin-right:18px; color:var(--mut); text-decoration:none;
               font-size:13.5px; padding:4px 0; border-bottom:2px solid transparent; }}
  nav.tabs a:hover {{ color:var(--navy); border-bottom-color:var(--navy); }}
  section {{ margin-bottom:34px; }}
  h2 {{ font-size:18px; margin:0 0 4px; color:var(--navy); }}
  .hint {{ font-size:12.5px; color:var(--mut); margin:0 0 14px; }}
  .cards {{ display:grid; grid-template-columns:repeat(5,1fr); gap:14px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }}
  .card .k {{ font-size:12px; color:var(--mut); text-transform:uppercase; letter-spacing:.5px; }}
  .card .v {{ font-size:26px; font-weight:700; color:var(--navy); margin:6px 0 2px; }}
  .card .d {{ font-size:12.5px; color:var(--mut); }}
  .card .d b {{ color:var(--text); }}
  .cards.four {{ grid-template-columns:repeat(4,1fr); }}
  .card.alert-card {{ border-left:4px solid var(--accent); }}
  .strip {{ display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
            background:linear-gradient(90deg,#FDF1EC,#FFFFFF); border:1px solid #F0D3C6;
            border-left:4px solid var(--accent); border-radius:10px; padding:13px 16px; }}
  .strip-k {{ font-size:12.5px; color:var(--mut); text-transform:uppercase; letter-spacing:.4px; }}
  .strip-v {{ font-size:22px; font-weight:700; color:var(--accent); }}
  .strip-d {{ font-size:13px; color:var(--text); }}
  .strip a {{ margin-left:auto; text-decoration:none; }}
  .card.ok-card {{ border-left:4px solid var(--ok); }}
  table.inner {{ font-size:12.5px; }}
  table.inner th {{ padding:7px 8px; font-size:11px; }}
  table.inner td {{ padding:7px 8px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card);
           border:1px solid var(--border); border-radius:10px; overflow:hidden; font-size:13.5px; }}
  th {{ background:#EDF1F8; color:var(--navy); text-align:left; font-weight:700;
        padding:9px 10px; font-size:12px; text-transform:uppercase; letter-spacing:.3px;
        border-bottom:1px solid var(--border); }}
  td {{ padding:9px 10px; border-bottom:1px solid #EEF1F7; vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  tfoot td {{ background:#F7F9FC; font-weight:700; border-top:2px solid var(--border); }}
  tr.row-risk td {{ background:#FDF6F3; }}
  .num {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
  .strong {{ font-weight:700; color:var(--navy); }}
  .mono {{ font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace; font-size:12.5px; }}
  .small {{ font-size:12.5px; }}
  .mut {{ color:var(--mut); }}
  .sub {{ font-size:12px; color:var(--mut); margin-top:2px; }}
  .nowrap {{ white-space:nowrap; }}
  .ok-t {{ color:var(--ok); }}
  .wip-t {{ color:var(--warn); }}
  .badge {{ display:inline-block; font-size:11px; font-weight:700; padding:2px 8px;
            border-radius:10px; white-space:nowrap; }}
  .b-ok {{ background:#E4F2EB; color:#1F6347; }}
  .b-active {{ background:#E6EDF8; color:#22467B; }}
  .b-risk {{ background:#FBE7E0; color:#9C3A1B; }}
  .b-wait {{ background:#EDF0F5; color:#5D6878; }}
  .b-warn {{ background:#FBF0DA; color:#8A5A10; }}
  .b-info {{ background:#E9EFF8; color:#3B5876; }}
  .bar {{ display:inline-block; width:120px; height:9px; background:#E8EDF5;
          border-radius:5px; overflow:hidden; vertical-align:middle; position:relative; }}
  .bar-done {{ display:block; height:100%; border-radius:5px 0 0 5px; }}
  .bar-wip {{ display:block; height:100%; background:repeating-linear-gradient(
              45deg,#C9A227,#C9A227 3px,#E4D08A 3px,#E4D08A 6px); }}
  .bar-cell {{ white-space:nowrap; }}
  .pct {{ font-size:12px; color:var(--mut); margin-left:8px; }}
  .prog {{ width:110px; height:8px; background:#E8EDF5; border-radius:5px; overflow:hidden; }}
  .prog-fill {{ height:100%; }}
  .two {{ display:grid; grid-template-columns:1.25fr 1fr; gap:18px; }}
  .three {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
  .panel {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }}
  .panel h3 {{ margin:0 0 10px; font-size:14.5px; color:var(--navy); }}
  .kv {{ display:flex; justify-content:space-between; padding:7px 0; border-bottom:1px dashed #E6EBF3; font-size:13.5px; }}
  .kv:last-child {{ border-bottom:none; }}
  .kv .k {{ color:var(--mut); padding-right:12px; }}
  .kv .v {{ font-weight:700; }}
  .kv .v.small {{ font-weight:400; text-align:right; max-width:62%; }}
  .alert {{ display:grid; grid-template-columns:96px 1fr auto; gap:12px; align-items:center;
            background:var(--card); border:1px solid var(--border); border-left-width:4px;
            border-radius:8px; padding:10px 12px; margin-bottom:9px; }}
  .a-risk {{ border-left-color:var(--accent); }}
  .a-warn {{ border-left-color:var(--warn); }}
  .a-info {{ border-left-color:var(--info); }}
  .btn-ghost {{ background:#fff; border:1px solid var(--border); color:var(--navy);
                border-radius:7px; padding:6px 12px; font-size:12.5px; cursor:pointer; }}
  .btn-ghost:hover {{ background:#EDF1F8; }}
  .dot {{ display:inline-block; width:18px; height:18px; line-height:18px; text-align:center;
          border-radius:50%; font-size:10.5px; color:#fff; font-weight:700; margin-right:4px; }}
  .d-ok {{ background:var(--ok); }}
  .d-agent {{ background:var(--blue); }}
  .d-block {{ background:var(--accent); }}
  .note {{ background:#FFF8E9; border:1px solid #F0DDAE; border-radius:8px; padding:12px 14px;
           font-size:13px; color:#6B5622; }}
  footer {{ border-top:1px solid var(--border); margin-top:26px; padding-top:16px;
            font-size:12px; color:var(--mut); }}
  @media (max-width:1000px) {{ .cards, .cards.four {{ grid-template-columns:repeat(2,1fr); }}
    .two, .three {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>

<div class="topbar">
  <div class="right">
    Данные на {AS_OF}<br>
    Объект: <b style="color:#fff">{OBJECT}</b> · договор {CONTRACT}<br>
    <span class="badge-demo">ДЕМО-ДАННЫЕ</span>
  </div>
  <h1>ПТО-контроль — прозрачность и остатки</h1>
  <div class="crumbs">Срез для руководителя · обновляется автоматически при каждом действии агента и инженера</div>
</div>

<div class="wrap">
<nav class="tabs">
  <a href="#stage">Где мы сейчас</a>
  <a href="#remainder">Остатки по работам</a>
  <a href="#money">Деньги по договору</a>
  <a href="#risk">Деньги под риском</a>
  <a href="#ai">Прозрачность ИИ</a>
  <a href="#audit">Журнал действий</a>
  <a href="#alerts">Требует решения</a>
</nav>

<!-- ================== КАРТОЧКИ ================== -->
<section>
  <div class="cards">
    <div class="card">
      <div class="k">Освоено по объёмам</div>
      <div class="v">{pct(overall_pct)}</div>
      <div class="d">с учётом выполненных,<br>но не закрытых: <b>{pct(overall_with_wip)}</b></div>
    </div>
    <div class="card alert-card">
      <div class="k">Остаток по смете</div>
      <div class="v">{money(t_amount_rest)} ₽</div>
      <div class="d">по {by_status.get('active',0)+by_status.get('risk',0)+by_status.get('wait',0)} незавершённым видам работ</div>
    </div>
    <div class="card">
      <div class="k">Закрыто актами КС-2</div>
      <div class="v">{money(closed_signed)} ₽</div>
      <div class="d">подписано заказчиком<br>предъявлено на проверке: <b>{money(submitted)} ₽</b></div>
    </div>
    <div class="card ok-card">
      <div class="k">Готово к предъявлению</div>
      <div class="v">{money(ready_wip_amount)} ₽</div>
      <div class="d">выполнено, но не закрыто актом —<br>не ждёт работ, ждёт документов</div>
    </div>
    <div class="card alert-card">
      <div class="k">Критичных расхождений</div>
      <div class="v">6</div>
      <div class="d">из них без ИД: <b>316 позиций</b><br>на сумму 18,4 млн ₽</div>
    </div>
  </div>
</section>

<section>
  <div class="strip">
    <span class="strip-k">Сколько под вопросом ({STRIP_SCOPE})</span>
    <span class="strip-v">{money(RISK_AT)} ₽</span>
    <span class="strip-d">{STRIP_DETAIL}</span>
    <a class="btn-ghost" href="#risk">Раскрыть по позициям</a>
  </div>
</section>

<!-- ================== ЭТАПЫ ================== -->
<section id="stage">
  <h2>Где мы сейчас: стадии проекта и стадии объекта</h2>
  <p class="hint">Одна и та же страница отвечает на два вопроса: на каком этапе внедрение системы и в каком состоянии каждый вид работ на объекте.</p>
  <table>
    <thead>
      <tr><th style="width:74px">Готовность</th><th>Этап</th><th style="width:150px">Сроки</th>
          <th style="width:130px">Прогресс</th><th>Факт (что реально сделано)</th><th>Что дальше</th></tr>
    </thead>
    <tbody>{stage_rows}</tbody>
  </table>
</section>

<!-- ================== ОСТАТКИ ================== -->
<section id="remainder">
  <h2>Остаточное количество работ — полный вывод</h2>
  <p class="hint">Остаток = смета − подписано КС-2 − выполнено, но не закрыто актом. Жёлтая штриховка — работы выполнены на объекте, но ещё не оформлены документально.</p>
  <table>
    <thead>
      <tr>
        <th style="width:44px">№</th><th>Вид работ</th>
        <th class="num">Смета</th><th class="num">Подписано</th><th class="num">Выполнено,<br>не закрыто</th>
        <th class="num">Остаток</th><th class="num">Остаток, ₽</th>
        <th style="width:210px">Выполнение</th><th style="width:120px">Статус</th>
      </tr>
    </thead>
    <tbody>{work_rows}</tbody>
    <tfoot>
      <tr>
        <td colspan="2">Итого по объекту, ₽</td>
        <td class="num">{money(t_amount_est)}</td>
        <td class="num">{money(t_amount_done)}</td>
        <td class="num">{money(ready_wip_amount)}</td>
        <td class="num">—</td>
        <td class="num">{money(t_amount_rest)}</td>
        <td class="num">{pct(overall_pct)}</td>
        <td class="small mut">{by_status.get('done',0)} из 12<br>завершено</td>
      </tr>
    </tfoot>
  </table>
</section>

<!-- ================== ДЕНЬГИ ================== -->
<section id="money">
  <h2>Деньги по договору</h2>
  <p class="hint">Сумма в ₽ без НДС, если не указано иное. «Получено» — фактическое поступление от заказчика.</p>
  <div class="two">
    <div class="panel">
      <h3>План / факт освоения по месяцам, млн ₽</h3>
      <svg viewBox="-40 0 {CHART_W+50} {CHART_H}" width="100%" height="230" role="img"
           aria-label="Столбчатая диаграмма план/факт по месяцам">
        {grid}{''.join(bars)}
        <rect x="{CHART_W-150}" y="2" width="10" height="10" fill="#8FA6C8" rx="2"/>
        <text x="{CHART_W-134}" y="11" font-size="11" fill="#5A5A5A">план</text>
        <rect x="{CHART_W-90}" y="2" width="10" height="10" fill="#1F3864" rx="2"/>
        <text x="{CHART_W-74}" y="11" font-size="11" fill="#5A5A5A">факт</text>
      </svg>
    </div>
    <div class="panel">
      <h3>Позиция по договору {CONTRACT}</h3>
      <div class="kv"><span class="k">Сумма договора с НДС</span><span class="v">{money(contract_with_vat)} ₽</span></div>
      <div class="kv"><span class="k">Сметная стоимость работ</span><span class="v">{money(t_amount_est)} ₽</span></div>
      <div class="kv"><span class="k">Закрыто КС-2 (подписано)</span><span class="v">{money(closed_signed)} ₽</span></div>
      <div class="kv"><span class="k">Предъявлено, на проверке</span><span class="v">{money(submitted)} ₽</span></div>
      <div class="kv"><span class="k">Остаток к предъявлению</span><span class="v">{money(remaining_to_submit)} ₽</span></div>
      <div class="kv"><span class="k">Оплачено заказчиком</span><span class="v" style="color:#2E7D5B">{money(received)} ₽</span></div>
      <div class="kv"><span class="k">Дебиторка (закрыто − оплачено)</span><span class="v" style="color:#C0502A">{money(closed_signed - received)} ₽</span></div>
      <div class="kv"><span class="k">НДС к уплате с закрытых</span><span class="v">{money(closed_signed * vat)} ₽</span></div>
      <div class="kv"><span class="k">Под риском по актам объекта</span><span class="v" style="color:#C0502A">{money(RISK_AT)} ₽</span></div>
      <p class="hint" style="margin:12px 0 0">Риск: {money(ready_wip_amount)} ₽ выполнено, но не может быть предъявлено из-за неполной ИД — это прямая задержка выручки.
      Сколько именно стоит каждый спор с актом и по каким актам — в разделе <a href="#risk">«Деньги под риском»</a>.</p>
    </div>
  </div>
</section>

{risk_section}

<!-- ================== ИИ ================== -->
<section id="ai">
  <h2>Прозрачность ИИ: что делает модель, чем платим, как проверяем</h2>
  <p class="hint">Подключение моделей через API — сменное: локальные модели в контуре заказчика, РФ-облако как второй контур, зарубежные API запрещены политикой для реальных данных.</p>
  <div class="three" style="margin-bottom:16px">
    <div class="card">
      <div class="k">Вызовов моделей за месяц</div>
      <div class="v">{money(AI['calls_month'])}</div>
      <div class="d">за последние 24 ч: <b>{money(AI['calls_24h'])}</b><br>
      с подтверждением человека: <b>{money(AI['human_confirm'])}</b> · без участия: <b>{money(AI['auto_done'])}</b></div>
    </div>
    <div class="card">
      <div class="k">Стоимость ИИ за месяц</div>
      <div class="v">{money(AI['cost_month'])} ₽</div>
      <div class="d">лимит: {money(AI['cost_limit'])} ₽ · использовано <b>{pct(100*AI['cost_month']/AI['cost_limit'])}</b><br>
      токенов: {f"{AI['tokens_in']/1e6:.1f}".replace(".", ",")} млн вход / {f"{AI['tokens_out']/1e6:.1f}".replace(".", ",")} млн выход</div>
    </div>
    <div class="card ok-card">
      <div class="k">Качество ответов</div>
      <div class="v">{str(AI['citation_ok']).replace('.', ',')} %</div>
      <div class="d">ответов с проверенной цитатой<br>
      заблокировано cite_verify: <b>{AI['blocked']}</b> · выдуманных ссылок: <b>{AI['halluc']}</b></div>
    </div>
  </div>
  <table>
    <thead>
      <tr><th>Модель / провайдер</th><th class="num">Вызовов</th><th class="num">Стоимость</th>
          <th style="width:200px">Доля</th><th>Задачи</th><th style="width:110px">Статус</th></tr>
    </thead>
    <tbody>{prov_rows}</tbody>
  </table>
  <div class="three" style="margin-top:14px">
    <div class="panel"><h3>Задержка ответа</h3>
      <div class="kv"><span class="k">Средняя</span><span class="v">{str(AI['avg_latency']).replace('.', ',')} с</span></div>
      <div class="kv"><span class="k">p95</span><span class="v">{str(AI['p95']).replace('.', ',')} с</span></div>
      <div class="kv"><span class="k">Цель по ТЗ</span><span class="v">≤ 15 с</span></div>
    </div>
    <div class="panel"><h3>Контроль и правила</h3>
      <div class="kv"><span class="k">Запросов вне контура</span><span class="v" style="color:#2E7D5B">0</span></div>
      <div class="kv"><span class="k">Промптов с версией</span><span class="v">100 %</span></div>
      <div class="kv"><span class="k">Задач с фиксацией модели</span><span class="v">100 %</span></div>
    </div>
    <div class="panel"><h3>Экономия затрат</h3>
      <div class="kv"><span class="k">Доля рутины на локальных</span><span class="v">67 %</span></div>
      <div class="kv"><span class="k">Маршрутизация моделей</span><span class="v">−41 % стоимости</span></div>
      <div class="kv"><span class="k">Кэш повторных запросов</span><span class="v">18 % попаданий</span></div>
    </div>
  </div>
</section>

<!-- ================== ЖУРНАЛ ================== -->
<section id="audit">
  <h2>Журнал действий: кто, что, на основании чего</h2>
  <p class="hint">Неизменяемая запись (append-only, хэш-цепочка). Каждое действие агента и человека прослеживается до документа-источника и до подписи.</p>
  <table>
    <thead>
      <tr><th style="width:64px">Время</th><th style="width:190px">Кто</th><th style="width:170px">Объект</th>
          <th>Действие и основание</th><th style="width:190px">Модель / промпт</th><th style="width:110px">Хэш</th></tr>
    </thead>
    <tbody>{audit_rows}</tbody>
  </table>
</section>

<!-- ================== АЛЕРТЫ ================== -->
<section id="alerts">
  <h2>Требует решения руководителя</h2>
  <p class="hint">Каждый пункт связан с конкретным видом работ, суммой и ответственным. Нажатие «Решить» открывает карточку с историей и вариантами.</p>
  {alert_rows}
  <div class="note" style="margin-top:14px">
    <b>Как считаются остатки.</b> Остаток по виду работ = объём по смете − объём, закрытый подписанными актами КС-2 − объём, подтверждённый журналами работ, но ещё не оформленный актом.
    Работы, выполненные без подтверждающих документов, в остаток не списываются: физически они сделаны, но предъявить их нельзя. Поэтому на дашборде две разные цифры — «выполнено» и «закрыто».
    Источники: смета v3 (утверждена 12.08.2026), журналы работ №10–12, подписанные КС-2 №29–38, выгрузка 1С на {AS_OF}.
  </div>
</section>

<footer>
  Дашборд руководителя · сформирован автоматически {AS_OF} · данные синтетические (демонстрация структуры отчёта)<br>
  Прозрачность обеспечивается журналом аудита с хэш-цепочкой; любая цифра на этой странице раскрывается до документа-источника.
  Версия отчёта 1.0 · методика расчёта зафиксирована в ТЗ, раздел 18.
</footer>
</div>
</body>
</html>
"""

with open("Дашборд_руководителя_ПТО.html", "w", encoding="utf-8") as f:
    f.write(html)

print("saved: Дашборд_руководителя_ПТО.html")
print(f"смета: {t_amount_est:,.0f} ₽ | закрыто: {closed_signed:,.0f} ₽ | "
      f"остаток: {t_amount_rest:,.0f} ₽ | освоено {overall_pct:.1f} %")
_scope = (f"{PORTFOLIO['totals']['acts_checked']} из {PORTFOLIO['totals']['acts_total']} актов, "
          f"покрытие {PORTFOLIO['totals']['coverage_pct']} %" if PORTFOLIO else f"{RISK_SHARE_ACT} % акта")
print(f"под риском: {RISK_AT:,.0f} ₽ ({_scope}) · источник: {PORTFOLIO_SRC or IMPACT_SRC}")
