#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран «Harness · Eval · Loop»: три слоя на одном листе, с числами из прогонов.

Все числа читаются из артефактов (out/harness/manifest.json, out/eval/report.json,
out/loop/run.json). Если артефакта нет — раздел честно говорит «не прогонялось»,
а не показывает ноль как результат.
"""
import html
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
e = lambda x: html.escape(str(x))


def load(rel):
    p = ROOT / rel
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


MAN = load("out/harness/manifest.json")
REP = load("out/eval/report.json")
RUN = load("out/loop/run.json")

LEVEL = {"auto": ("auto", "#e5f4ec", "#0f7b45"),
         "confirm": ("confirm", "#eef2fb", "#2e5c9a"),
         "human": ("human", "#fdf1dc", "#b45309"),
         "never": ("never", "#fbe9e9", "#b91c1c")}

tools_rows = ""
if MAN:
    for t in MAN["tools"]:
        name, bg, fg = LEVEL.get(t["level"], (t["level"], "#eee", "#333"))
        tools_rows += (f'<tr><td><b>{e(t["name"])}</b></td>'
                       f'<td><span class=p style="background:{bg};color:{fg}">{e(name)}</span></td>'
                       f'<td>{e(t["proof"])}</td><td class=mut>{e(t["why"])}</td></tr>')

bnd_rows = "\n".join(
    f'<tr><td>{e(b["what"])}</td><td class=code>{e(b["enforced_in"])}</td>'
    f'<td class=code>{e(b["test"])}</td></tr>' for b in (MAN or {}).get("boundaries", []))

STAT_ICON = {"pass": ("✓", "#0f7b45"), "fail": ("✗", "#b91c1c"), "skip": ("·", "#6b7280")}
eval_rows = ""
if REP:
    for r in REP["results"]:
        icon, color = STAT_ICON.get(r["status"], ("?", "#333"))
        eval_rows += (f'<tr><td>{e(r["title"])}</td>'
                      f'<td class=c style="color:{color};font-weight:700">{icon} {e(r["status"])}</td>'
                      f'<td>{e(r["why"])}</td></tr>')

not_measured = "".join(f"<li>{e(x)}</li>" for x in (REP or {}).get("not_measured", []))

loop_rows = ""
if RUN:
    for h in RUN["history"]:
        loop_rows += (f'<tr><td class=code>{e(h["cycle"])}</td><td class=n>{h["score"]}</td>'
                      f'<td class=n>{len(h["failures"])}</td><td class=n>{h["nodes_added"]}</td>'
                      f'<td class=n>{len(h["nodes_closed"])}</td><td class=n>{h["open_nodes"]}</td></tr>')

limits = "".join(f"<li>{e(x)}</li>" for x in (RUN or {}).get("limits_kept", []))

c = (REP or {}).get("counts", {})
man_c = (MAN or {}).get("counts", {})
score = (REP or {}).get("score")
stop = (RUN or {}).get("stop_reason", "не прогонялось")

html_text = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Harness · Eval · Loop — что внедрено</title>
<style>
:root{{--ink:#15181d;--mut:#6b7280;--line:#e3e6ea;--cvt:#eceff3;--ok:#0f7b45;--warn:#b45309;--bad:#b91c1c;--bg:#f6f7f9}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}}
.wrap{{max-width:1140px;margin:0 auto;padding:28px 18px 64px}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}} h2{{font-size:19px;margin:34px 0 10px}}
.sub{{color:var(--mut);margin:0 0 20px;max-width:900px}}
.corr{{background:#fff;border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:8px;padding:13px 16px;margin:0 0 22px}}
.corr b{{color:var(--warn)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.card .k{{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.card .v{{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}}
.card .v.ok{{color:var(--ok)}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}}
th{{background:var(--cvt);font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;color:#374151}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.c{{text-align:center;white-space:nowrap}}
td.code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:nowrap}}
td.mut{{color:var(--mut);font-size:13px}}
.p{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;font-weight:700}}
ul{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 20px 14px 34px;margin:0}}
li{{margin:8px 0}}
.note{{color:var(--mut);font-size:13px;margin:10px 0 0;max-width:960px}}
.foot{{margin-top:34px;color:var(--mut);font-size:12.5px;border-top:1px solid var(--line);padding-top:14px}}
.layer{{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin-bottom:2px}}
</style></head><body><div class="wrap">
<h1>Harness · Eval · Loop — три слоя, как они выглядят в нашем коде</h1>
<p class="sub">Не пересказ курса, а то, что проверяемо: границы агента лежат данными, поведение
измеряется прогоном, провалы осаждаются в граф и возвращаются следующим кругом.</p>

<div class="corr">
<b>Сначала про источник.</b> Курса «от Google» про graph engineering не существует: посты с
разными таймингами и содержанием не ссылаются на страницу Google, а проверка от 11.08.2026
не нашла такого курса ни на одном ресурсе Google. У Google есть фреймворк (ADK 2.0 с графовыми
workflow) — то есть они это делают, а не преподают. Философия рабочая, атрибуция — нет.
</div>

<div class="cards">
  <div class="card"><div class="k">Инструментов в манифесте</div><div class="v">{man_c.get('tools', '—')}</div></div>
  <div class="card"><div class="k">Постоянных запретов</div><div class="v">{man_c.get('boundaries', '—')}</div></div>
  <div class="card"><div class="k">Балл eval</div><div class="v ok">{score if score is not None else '—'}</div></div>
  <div class="card"><div class="k">Проверок pass / fail / skip</div>
    <div class="v">{c.get('pass', 0)} / {c.get('fail', 0)} / {c.get('skip', 0)}</div></div>
  <div class="card"><div class="k">Кругов цикла</div><div class="v">{(RUN or {}).get('cycles_run', '—')}</div></div>
</div>

<h2><span class=layer>Слой 1</span><br>Harness: границы как данные, а не абзац в промпте</h2>
<p class="note">У каждого инструмента — уровень автономии и обязательное доказательство, которое
агент предъявляет вместе с результатом. Манифест проверяется кодом: если в нём появится
несуществующий модуль или тест, <code>eval</code> падает.</p>
<table style="margin-top:12px">
<thead><tr><th>Инструмент</th><th>Уровень</th><th>Что агент обязан предъявить</th><th>Почему так</th></tr></thead>
<tbody>{tools_rows}</tbody></table>

<h2>Что запрещено всегда — и где это реально проверяется</h2>
<table><thead><tr><th>Запрет</th><th>Где в коде</th><th>Чем доказано</th></tr></thead>
<tbody>{bnd_rows}</tbody></table>
<p class="note">Ни один запрет не держится «на честном слове»: у каждого есть модуль и тест.
Подпись — только человек: проверка разбирает код через <code>ast</code> и требует, чтобы
параметры «кто» и «роль» нельзя было не передать.</p>

<h2><span class=layer>Слой 2</span><br>Eval: «проверено», а не «выглядит правильно»</h2>
<table style="margin-top:10px">
<thead><tr><th>Проверка поведения</th><th class=c>Статус</th><th>Что установлено</th></tr></thead>
<tbody>{eval_rows or '<tr><td colspan=3>прогон eval не выполнялся</td></tr>'}</tbody></table>
<p class="note"><b>skip — это не «хорошо».</b> Он значит «проверять было нечем», и так и
печатается. Отдельная проверка ловит случай, когда все проверки пропущены, а вердикт стоит
«ready».</p>

<h2><span class=layer>Слой 3</span><br>Loop: цикл с тормозом, а не «7×24 без ограничений»</h2>
<p class="note">Остановка: <b>{e(stop)}</b>. Четыре независимых тормоза — бюджет кругов, отсутствие
роста балла, повтор тех же провалов и узел, который не закрылся за отведённые попытки.</p>
<table style="margin-top:10px">
<thead><tr><th>Круг</th><th class=n>Балл</th><th class=n>Провалов</th><th class=n>Новых узлов</th>
<th class=n>Закрыто</th><th class=n>Открытых узлов</th></tr></thead>
<tbody>{loop_rows or '<tr><td colspan=6>цикл не прогонялся</td></tr>'}</tbody></table>
<p class="note">Проверено на живом сбое: испорченная сумма акта (+1 000 ₽) → круг осадил узел,
потратил попытки, перевёл его в «нужен человек» и остановился. После починки данных узел
закрылся <b>прошедшей проверкой</b>, а не словом «сделано».</p>

<h2>Чего нет — и это решение, а не недоделка</h2>
<ul>{limits or '<li>цикл не прогонялся</li>'}</ul>
<p class="note">Агент не правит собственный код. «Самоэволюция» в буквальном смысле — правка
собственной логики без человека — в бухгалтерском документе стоит дороже сэкономленного
времени. Цикл закрывает то, что закрывается проверкой; всё остальное уходит человеку с
доказательством.</p>

<h2>Что eval не измеряет</h2>
<ul>{not_measured or '<li>прогон не выполнялся</li>'}</ul>

<div class="foot">
Артефакты: <code>out/harness/manifest.json</code>, <code>out/eval/report.md</code>,
<code>out/loop/run.md</code>, <code>out/loop/runs.jsonl</code>.
Команды: <code>pto_agent harness</code> · <code>pto_agent eval</code> ·
<code>pto_agent backlog</code> · <code>pto_agent loop</code>.
</div>
</div></body></html>
"""
for p in (ROOT / "docs/Harness_Eval_Loop.html", pathlib.Path("/home/user/Harness_Eval_Loop.html")):
    p.write_text(html_text, encoding="utf-8")
print("ok", len(html_text))
