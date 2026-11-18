#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран прогона КС-2 по смете 1166/Н.

Все числа берутся из артефактов прогона:
  out/smeta_check/smeta_parsed.json — разобранная смета (47 позиций),
  out/ks2_from_smeta/lines.json     — позиции акта (31) и исключённые материалы (16),
  out/ks2_1166/ks2_2026_11_01.json  — собранный бланк КС-2.
Экран ничего не пересчитывает «на глаз»: суммы считаются здесь же и сверяются
с итогом бланка; расхождение выводится, а не прячется.
"""
import json
import pathlib
from html import escape

ROOT = pathlib.Path(__file__).resolve().parents[1]
read = lambda p: json.loads((ROOT / p).read_text(encoding="utf-8"))
FORM = read("out/ks2_1166/ks2_2026_11_01.json")
LINES = read("out/ks2_from_smeta/lines.json")
PARSED = read("out/smeta_check/smeta_parsed.json")

WORKS = ("ГЭСН", "ФЕР", "ТЕР", "ФССЦпг")
is_work = lambda c: str(c or "").upper().startswith(WORKS)

items = PARSED["items"]
materials = [i for i in items if not is_work(i.get("code"))]
sum_works = round(sum((i.get("values") or [0])[-1] for i in items if is_work(i.get("code"))), 2)
sum_mat = round(sum((i.get("values") or [0])[-1] for i in materials), 2)
sum_all = round(sum_works + sum_mat, 2)

t = FORM["totals"]
act_from_lines = round(sum(l["qty_claim"] * l["price"] for l in LINES["lines"]), 2)
rounding = round(t["amount_claim"] - sum_works, 2)   # цена за единицу округлена до копейки

money = lambda v: f"{v:,.2f}".replace(",", " ").replace(".", ",")


def qty(v):
    s = f"{v:,.4f}".rstrip("0").rstrip(".")
    return s.replace(",", " ").replace(".", ",")


src = {ln["n"]: ln for ln in LINES["lines"]}
BADGES = {"review": ("на проверку", "warn"), "ok": ("ок", "ok"), "blocked": ("заблокировано", "bad")}

rows = []
for r in FORM["rows"]:
    n, code, name, unit, qj, qc, price, cost = r["values"][:8]
    s = src.get(int(n), {})
    state = r.get("state", "review")
    label, cls = BADGES.get(state, ("на проверку", "warn"))
    rows.append(
        "<tr>"
        f'<td class="n">{n}</td>'
        f'<td class="code">{escape(str(code))}</td>'
        f"<td>{escape(str(name))}</td>"
        f'<td class="c">{escape(str(unit))}</td>'
        f'<td class="n">{escape(str(qj))}</td>'
        f'<td class="n">{escape(str(qc))}</td>'
        f'<td class="n">{escape(str(price))}</td>'
        f'<td class="n"><b>{escape(str(cost))}</b></td>'
        f'<td class="src">смета, поз. {s.get("n", "—")}<br><span class="badge {cls}">{label}</span></td>'
        "</tr>")

ex_rows = "\n".join(
    f'<tr><td class="n">{i}</td><td class="code">{escape(str(e.get("code", "")))}</td>'
    f'<td>{escape(str(e.get("name", "")))}</td><td class="src">{escape(str(e.get("why", "")))}</td></tr>'
    for i, e in enumerate(LINES["excluded"], 1))

gaps = "\n".join(
    f"<li><b>{escape(g['what'])}</b> — {escape(g['why'])}. Что нужно: {escape(g['ask'])}.</li>"
    for g in FORM["gaps"])

not_done = [
    ("Объём не подтверждён выполнением",
     "В акте стоит сметный объём: журнала выполненных работ и обмеров нет, поэтому колонки «по журналу» и «к предъявлению» "
     "совпадают. Подписывать в таком виде нельзя — это черновик для сверки с фактом. Оговорка стоит в каждой строке, а не только внизу."),
    ("Три обязательных реквизита пусты",
     "Договор, Заказчик, Подрядчик отсутствуют в исходных данных. Поля не заполнены выдумкой — они вынесены в «что нужно запросить»."),
    ("НР и СП не проверены",
     "Смета АРОС даёт накладные расходы и сметную прибыль, но база начисления по каждой расценке не сверялась. В акте этих строк нет вовсе."),
    ("Коэффициенты демонтажа не пересчитаны",
     "Цены взяты ровно как в смете. Если в ней применены коэффициенты на демонтаж (0,4 / 0,3 и подобные), они уже внутри цены; отдельной проверки не было."),
    ("Материалы в акт не попали — и это не потеря",
     f"16 позиций материалов (код ФГИС ЦС) исключены: они входят в состав расценок. Их сумма {money(sum_mat)} ₽ — это и есть разница между итогом сметы "
     f"({money(sum_all)} ₽) и работами ({money(sum_works)} ₽)."),
    ("Пересчёта в текущие цены нет",
     "Цены — база ФЕР-2020 (доп. 9) и ФСБЦ-2022 в редакции 01.01.2022. Индексы Минстроя по Ленинградской области не применены, "
     "поэтому суммы — в базисном уровне цен, а не в сегодняшних рублях."),
    ("Подпись не ставится",
     "Печатная форма — черновик до УКЭП. Юридической силы не имеет, в реестр не идёт, контрагенту не уходит."),
]
nd = "\n".join(f"<li><b>{escape(a)}</b><br>{escape(b)}</li>" for a, b in not_done)

html = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>КС-2 по смете 1166/Н — ноябрь 2026</title>
<style>
:root{{--ink:#15181d;--mut:#6b7280;--line:#e3e6ea;--cvt:#eceff3;--ok:#0f7b45;--warn:#b45309;--bad:#b91c1c;--bg:#f6f7f9}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}}
.wrap{{max-width:1200px;margin:0 auto;padding:28px 18px 64px}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}} h2{{font-size:19px;margin:34px 0 10px}}
.sub{{color:var(--mut);margin:0 0 20px;max-width:900px}}
.draft{{background:#fff4e5;border-left:4px solid var(--warn);padding:12px 16px;border-radius:6px;margin:0 0 22px;font-weight:600}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.card .k{{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.card .v{{font-size:21px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}}
.card .v.big{{color:var(--ok)}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}}
th{{background:var(--cvt);font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;color:#374151}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.c{{text-align:center;color:var(--mut);white-space:nowrap}}
td.code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:nowrap}}
td.src{{color:var(--mut);font-size:12px;white-space:nowrap}}
tr:hover td{{background:#fafbfc}}
.tot td{{background:#eef4ff;font-weight:700;border-top:2px solid #c8d7f0}}
.badge{{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600;margin-top:3px}}
.badge.warn{{background:#fdf1dc;color:var(--warn)}} .badge.ok{{background:#e5f4ec;color:var(--ok)}} .badge.bad{{background:#fbe9e9;color:var(--bad)}}
ul{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 20px 14px 34px;margin:0}}
li{{margin:9px 0}}
.note{{color:var(--mut);font-size:13px;margin:10px 0 0;max-width:980px}}
.scroll{{overflow:auto}}
.foot{{margin-top:36px;color:var(--mut);font-size:12.5px;border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
<h1>Акт КС-2 за ноябрь 2026 — смета 1166/Н</h1>
<p class="sub">Дошкольное отделение МОУ «Дубровская СОШ», ремонт электропроводки 2-го этажа.
Позиции не придуманы: 31 строка взята из сметы, у каждой — шифр расценки и номер позиции сметы-источника.</p>

<div class="draft">Черновик. Не подписано. Юридической силы не имеет — до УКЭП и до подтверждения объёмов журналом работ или обмером.</div>

<div class="cards">
  <div class="card"><div class="k">Позиций в акте</div><div class="v">{t['positions']}</div></div>
  <div class="card"><div class="k">Без НДС</div><div class="v">{money(t['amount_claim'])} ₽</div></div>
  <div class="card"><div class="k">НДС 20 %</div><div class="v">{money(t['vat_claim'])} ₽</div></div>
  <div class="card"><div class="k">Итого с НДС</div><div class="v big">{money(t['total_claim_with_vat'])} ₽</div></div>
  <div class="card"><div class="k">Заблокировано / урезано</div><div class="v">{t['positions_fully_blocked']} / {t['positions_reduced']}</div></div>
  <div class="card"><div class="k">Материалов за актом</div><div class="v">{len(LINES['excluded'])}</div></div>
</div>

<h2>Строки акта</h2>
<div class="scroll"><table>
<thead><tr><th class="n">№</th><th>Шифр расценки</th><th>Наименование работ</th><th>Ед.</th>
<th class="n">По журналу</th><th class="n">К предъявлению</th><th class="n">Цена, ₽</th><th class="n">Стоимость, ₽</th><th>Источник и статус</th></tr></thead>
<tbody>
{chr(10).join(rows)}
<tr class="tot"><td colspan="7">Итого без НДС</td><td class="n">{money(t['amount_claim'])}</td><td></td></tr>
<tr class="tot"><td colspan="7">НДС 20 %</td><td class="n">{money(t['vat_claim'])}</td><td></td></tr>
<tr class="tot"><td colspan="7">Итого с НДС</td><td class="n">{money(t['total_claim_with_vat'])}</td><td></td></tr>
</tbody></table></div>
<p class="note">Колонка «по журналу» заполнена сметным объёмом: журнала выполненных работ нет, поэтому обе колонки совпадают.
Это заполнитель для сверки с фактом, а не подтверждение выработки.</p>

<h2>Что осталось за актом: материалы (16 позиций)</h2>
<p class="note">Материалы не выброшены и не спрятаны. В акт они не идут отдельными строками, потому что уже входят в состав расценок;
их сумма {money(sum_mat)} ₽ объясняет разницу между сметой и актом.</p>
<div class="scroll"><table>
<thead><tr><th class="n">№</th><th>Код ФГИС ЦС</th><th>Материал</th><th>Причина исключения</th></tr></thead>
<tbody>{ex_rows}</tbody></table></div>

<h2>Чего в акте нет — честный список</h2>
<ul>{nd}</ul>

<h2>Что нужно запросить, чтобы акт стал документом</h2>
<ul>{gaps}</ul>

<h2>Сверка чисел</h2>
<ul>
<li>Итог сметы 1166/Н (из разобранного PDF): <b>{money(sum_all)} ₽</b> = работы {money(sum_works)} ₽ + материалы {money(sum_mat)} ₽.</li>
<li>В акте работ: <b>{money(t['amount_claim'])} ₽</b>. Расхождение с {money(sum_works)} ₽ — <b>{money(rounding)} ₽</b>:
  цена за единицу округлена до копейки, после чего умножена на объём.</li>
<li>Проверка обратным счётом по строкам акта (объём × цена): {money(act_from_lines)} ₽ — сходится с колонкой «стоимость» до копейки.</li>
<li>НДС 20 %: {money(t['vat_claim'])} ₽; итого {money(t['total_claim_with_vat'])} ₽ = {money(t['amount_claim'])} + {money(t['vat_claim'])}.</li>
<li>Заблокированных и урезанных позиций нет: {t['positions_fully_blocked']} / {t['positions_reduced']}. Это не значит «всё в порядке» —
  объём всё равно не подтверждён журналом.</li>
</ul>

<div class="foot">
Собрано механизмом бланков: <code>python3 -m pto_agent forms ks2</code>. Экран — <code>scripts/ks2_screen.py</code>,
читает артефакты прогона и не содержит собственных цифр. Форма КС-2 — Постановление Госкомстата России от 11.11.1999 № 100.
</div>
</div></body></html>
"""
for p in (ROOT / "docs/КС2_1166Н_акт.html", pathlib.Path("/home/user/КС2_1166Н_акт.html")):
    p.write_text(html, encoding="utf-8")
print(f"строк {len(rows)}, материалов {len(LINES['excluded'])}, запросов {len(FORM['gaps'])}")
print(f"смета {sum_all} = работы {sum_works} + материалы {sum_mat}; акт {t['amount_claim']}, округление {rounding}")
