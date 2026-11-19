#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран разбора DDC CWICR: что проверено на наших данных, что берём, что нет.

Числа про DDC (55 719 расценок, соответствие 81,6 %, медиана отношения ×34,1)
получены прогоном по скачанному RU-треку и записаны в этот скрипт как факты
разведки — с указанием, откуда они. Числа по нашей смете (неявный индекс)
читаются из out/implicit_index.json и не дублируются вручную.
"""
import html
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
IDX = json.loads((ROOT / "out/implicit_index.json").read_text(encoding="utf-8"))

e = lambda x: html.escape(str(x))


def _delta(row):
    return "" if row["from_group_median"] is None else f"{row['from_group_median']:+.1f} %"
rows = IDX["rows"]
g, c = IDX["groups"], IDX["counts"]

tr = "\n".join(
    f"<tr class='{'warn' if r['flag'] == 'outlier' else ''}'>"
    f"<td class=n>{r['n']}</td><td class=code>{e(r['code'])}</td><td>{e(r['name'])}</td>"
    f"<td class=n>{r['unit_price']:,.2f}</td><td class=n>{r['fer_price']:,.2f}</td>"
    f"<td class=n><b>×{r['index_total']}</b></td>"
    f"<td class=n>{_delta(r)}</td>"
    f"<td class=c>{e(r['group'])}</td><td class=n>{r['fer_labour_share']:.0f} %</td>"
    f"<td class=c>{e(r['flag'])}</td></tr>"
    for r in rows).replace(",", " ")

sk = "\n".join(f"<tr><td class=n>{s['n']}</td><td class=code>{e(s['code'])}</td>"
               f"<td>{e(s['why'])}</td></tr>" for s in IDX["skipped"])
un = "\n".join(f"<li><b>{e(o['code'])}</b> ×{o['index_total']} "
               f"({o['from_group_median']:+.1f} % к своему ряду) — {e(o['name'])}</li>"
               for o in IDX["unexplained_outliers"])
nd = "\n".join(f"<li>{e(x)}</li>" for x in IDX["not_done"])

take = [
    ("Ресурсная развёртка норм", "Главное",
     "У них расценка разложена на ресурсы с количеством: разряд, электроэнергия, электроды, "
     "краны по грузоподъёмности, сварочные агрегаты — 8,2 строки на расценку. У нас в базе только "
     "итоги ФОТ/ЭММ/материалы. Без развёртки нет ни потребности в материалах по норме, ни "
     "ведомости расхода стали, которую вы просили по КР1.",
     "Импортировать поресурсную часть из нашего же ФСНБ (в XML ФГИС ЦС она есть), DDC — как образец схемы."),
    ("Проверка сметы по неявному индексу", "Сделано в этом круге",
     "Норма неизменна, цена региональна — значит цена позиции ÷ цена ФЕР-2000 даёт индекс, "
     "которым смета пересчитана. Он не напечатан в документе, но вытаскивается. На смете 1166/Н "
     "виден коэффициент демонтажа ≈ 0,33, и остаются 2 строки для проверки глазами.",
     "scripts/check_implicit_index.py — уже в репозитории, с тестами."),
    ("Типизация строк row_type", "Средний приоритет",
     "У них строка — типизированная запись: Ресурс / Состав работ / Машинист / Масса / "
     "Электричество / Абстрактный ресурс / Услуга. «Состав работ» — готовый текст для акта скрытых "
     "работ, «Масса» — для ведомости стали, «Электричество» — для мощности.",
     "Перенять типизацию при импорте ФСНБ: одна строка — один тип, а не текст в примечании."),
    ("Инвариант «нормы не меняются, меняются цены»", "Средний приоритет",
     "У них 30 треков с побайтово одинаковыми нормами и CI-тест track-parity. У нас смена базы цен "
     "могла незаметно менять норму — теста на это нет.",
     "Добавить тест: нормы ФЕР/ФСБЦ при смене версии цен не меняются."),
    ("Правило «отклонение >30 % — с обоснованием»", "Дёшево",
     "Их CONTRIBUTING требует обосновывать цену, отклоняющуюся от медианы более чем на 30 %. "
     "Это прямо то, о чём мы говорили по ФЕР/ТЕР.",
     "Вписать в чек-лист пополнения базы как формальное правило."),
]
take_html = "\n".join(
    f"<div class=card><div class=t>{e(a)} <span class=tag>{e(b)}</span></div>"
    f"<p>{e(cc)}</p><p class=act><b>Что делать:</b> {e(d)}</p></div>" for a, b, cc, d in take)

skip = [
    ("55 719 расценок и 27 672 ресурса", "CC BY-NC 4.0 — в MIT-ядро нельзя. Плюс редакция и регион не наши, уровень цен не подтверждён."),
    ("rate_code / resource_code", "Зашифрованные, не ГЭСН/ФЕР: сопоставление только по тексту, а он на 12 % пустой."),
    ("Qdrant-снапшоты (1 ГБ)", "Производная от данных → та же CC BY-NC. И 3072-мерные OpenAI-эмбеддинги нам не нужны: BM25 в norms.py работает без внешних вызовов."),
    ("n8n-workflow (Apache-2.0)", "Технически можно, но у нас свой CLI и MCP — дублировать оркестрацию незачем."),
    ("PDF-книга", "All rights reserved."),
    ("Их цены как «рыночные»", "Средняя по Петербургу на неизвестную дату — не источник для акта. Наше правило «цена с датой и версией выгрузки» строже."),
]
skip_html = "\n".join(f"<tr><td><b>{e(a)}</b></td><td>{e(b)}</td></tr>" for a, b in skip)

html_text = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DDC CWICR: что взять, что нет — проверка на наших данных</title>
<style>
:root{{--ink:#15181d;--mut:#6b7280;--line:#e3e6ea;--cvt:#eceff3;--ok:#0f7b45;--warn:#b45309;--bad:#b91c1c;--bg:#f6f7f9}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px 18px 64px}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}} h2{{font-size:19px;margin:34px 0 12px}}
.sub{{color:var(--mut);margin:0 0 20px;max-width:940px}}
.lead{{background:#fff;border:1px solid var(--line);border-left:4px solid var(--ok);border-radius:8px;padding:14px 16px;margin:0 0 22px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:8px}}
.stat{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.stat .k{{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.stat .v{{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}}
.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}}
.card .t{{font-weight:700;font-size:15px}}
.tag{{display:inline-block;margin-left:6px;padding:1px 8px;border-radius:999px;background:#e5f4ec;color:var(--ok);font-size:11px;font-weight:600;vertical-align:2px}}
.card p{{margin:6px 0 0;color:#2b3038;font-size:14px}}
.card p.act{{color:var(--mut)}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}}
th{{background:var(--cvt);font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;color:#374151}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.c{{text-align:center;color:var(--mut);white-space:nowrap}}
td.code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:nowrap}}
tr.warn td{{background:#fdf6ec}}
ul{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 20px 14px 34px;margin:0}}
li{{margin:8px 0}}
.note{{color:var(--mut);font-size:13px;margin:10px 0 0;max-width:960px}}
.scroll{{overflow:auto}}
.foot{{margin-top:34px;color:var(--mut);font-size:12.5px;border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
<h1>DDC CWICR: что взять, что нет — и проверка на нашей смете</h1>
<p class="sub">Разбор <code>datadrivenconstruction/OpenConstructionEstimate-DDC-CWICR</code>
(тег v0.4.0). Смотрел не README, а данные: скачал RU-трек и сверил с нашей базой ФЕР-2020.</p>

<div class="lead">
<b>Короткий ответ:</b> данные брать нельзя (CC BY-NC 4.0), идеи — можно и нужно.
Одну идею уже проверил на вашей смете и получил результат, которого у нас раньше не было:
неявный индекс сметы и коэффициент на демонтаж, который в документе не напечатан.
</div>

<div class="cards">
  <div class="stat"><div class="k">Расценок в их базе</div><div class="v">55 719</div></div>
  <div class="stat"><div class="k">Строк в RU-треке</div><div class="v">900 225</div></div>
  <div class="stat"><div class="k">Совпало с нашим ФЕР по названию</div><div class="v">81,6 %</div></div>
  <div class="stat"><div class="k">Медиана их цены к ФЕР-2000</div><div class="v">×34,1</div></div>
  <div class="stat"><div class="k">Их лицензия данных</div><div class="v">CC BY-NC</div></div>
</div>
<p class="note">Разброс отношения по перцентилям: p5 ×11,4 · p25 ×21,3 · медиана ×34,1 · p75 ×62,8 ·
p95 ×151,5. В «правдоподобный» диапазон индекса ×40…60 попадает только 16,7 % позиций — как база цен
это не годится.</p>

<h2>Что берём — пять вещей</h2>
{take_html}

<h2>Чего не берём</h2>
<div class="scroll"><table><thead><tr><th>Что</th><th>Почему нет</th></tr></thead><tbody>{skip_html}</tbody></table></div>

<h2>Проверка на вашей смете 1166/Н: неявный индекс</h2>
<p class="note">Цена позиции ÷ цена той же нормы в базе ФЕР-2000 (базис 01.01.2000). Метод считает,
что норма неизменна, а цена региональна — тогда отношение и есть индекс пересчёта.
Сопоставлено {c['linked']} позиций из {c['positions']}; общая медиана <b>×{IDX['median_index']}</b>.</p>
<div class="cards" style="margin-top:12px">
  <div class="stat"><div class="k">Монтаж / прочее</div><div class="v">×{g['монтаж/прочее']['median']}</div></div>
  <div class="stat"><div class="k">Демонтаж</div><div class="v">×{g['демонтаж']['median']}</div></div>
  <div class="stat"><div class="k">Коэффициент демонтажа</div><div class="v">×{IDX['demolition_ratio_to_median']}</div></div>
  <div class="stat"><div class="k">Связь доли материалов с индексом</div><div class="v">{IDX['material_vs_index_correlation']}</div></div>
</div>
<p class="note">Коэффициент демонтажа <b>×{IDX['demolition_ratio_to_median']}</b> в смете нигде не напечатан —
он виден только из отношения цен. Связь «доля ФОТ+ЭММ ↔ логарифм индекса» = {IDX['material_vs_index_correlation']}:
чем больше в норме материалов, тем ниже общий индекс. Это не дефект сметы, а следствие того, что труд
и материалы индексируются по-разному, а наша база даёт их одной суммой.</p>

<div class="scroll" style="margin-top:14px"><table>
<thead><tr><th class=n>№</th><th>Шифр</th><th>Наименование</th><th class=n>Цена сметы</th>
<th class=n>ФЕР-2000</th><th class=n>Индекс</th><th class=n>От ряда</th><th class=c>Ряд</th>
<th class=n>Доля ФОТ+ЭММ</th><th class=c>Метка</th></tr></thead>
<tbody>{tr}</tbody></table></div>

<h2>Единственное, что стоит смотреть глазами</h2>
<ul>{un}</ul>
<p class="note">Остальные «выпавшие» объясняются долей материалов — это не ошибки сметы.</p>

<h2>Без пары в базе: индекс не выводится</h2>
<div class="scroll"><table><thead><tr><th class=n>№</th><th>Шифр</th><th>Причина</th></tr></thead>
<tbody>{sk}</tbody></table></div>

<h2>Чего эта проверка не делает</h2>
<ul>{nd}</ul>

<div class="foot">
Источники: репозиторий DDC CWICR (LICENSE, NOTICE, DATA_DICTIONARY.md, CONTRIBUTING.md,
RU-трек parquet — скачан и сверен), наша база <code>data/fsnb/fer2020.csv.gz</code> (ФСНБ-2020,
доп. № 9, приказы Минстроя № 961/пр, 962/пр). Отчёт по смете — <code>out/implicit_index.md</code>,
скрипт — <code>scripts/check_implicit_index.py</code>.
</div>
</div></body></html>
"""
for p in (ROOT / "docs/DDC_CWICR_разбор.html", pathlib.Path("/home/user/DDC_CWICR_разбор.html")):
    p.write_text(html_text, encoding="utf-8")
print("ok", len(html_text))
