# -*- coding: utf-8 -*-
"""Отчёт руководителю: HTML из реально посчитанных данных."""
import json
from datetime import date
from pathlib import Path

from .reconcile import Reconciliation


def _m(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " ₽"


def _q(v: float) -> str:
    s = f"{v:,.3f}".rstrip("0").rstrip(".")
    return s.replace(",", " ").replace(".", ",") or "0"


def build_report(rec: Reconciliation, audit_path: Path, out: Path,
                 vat_rate: float = 20.0, norms_last: dict | None = None) -> Path:
    sev = rec.by_severity()
    total = rec.amount_act
    vat = round(total * vat_rate / 100, 2)
    safe_vat = round(rec.amount_act_safe * vat_rate / 100, 2)
    progress = 100 * total / rec.amount_estimate if rec.amount_estimate else 0

    plan = {p["line"].item.code: p for p in rec.act_plan()}
    rows = []
    for line in rec.lines:
        rem = line.qty_remainder
        done = 100 * line.qty_journal / line.item.qty if line.item.qty else 0
        wip = 0.0
        color = "#2E7D5B" if rem <= 0.0001 else ("#C0502A" if rem < 0 else "#2E5C9A")
        rows.append(f"""
        <tr>
          <td class="mono">{line.item.code}</td>
          <td>{line.item.name}</td>
          <td class="num">{_q(line.item.qty)}</td>
          <td class="num ok">{_q(line.qty_prev_closed)}</td>
          <td class="num warn">{_q(line.qty_journal)}{' <span class="mut small">ед. изм.!</span>' if line.unit_mismatch else ''}</td>
          <td class="num strong">{_q(rem)}</td>
          <td class="num">{_m(max(rem, 0) * line.item.price)}</td>
          <td class="num">{_q(plan[line.item.code]["qty_claim"]) if line.item.code in plan else "—"}</td>
          <td class="barcell"><span class="bar"><span class="fill" style="width:{min(done,100):.1f}%;background:{color}"></span></span>
              <span class="pct">{done:.0f} %</span></td>
        </tr>""")

    disc_rows = ""
    for d in rec.discrepancies:
        cls = "risk" if d.severity == "critical" else "warn"
        vals = "; ".join(f"{k}: {v}" for k, v in d.values.items())
        disc_rows += f"""
        <div class="disc {cls}">
          <div class="disc-head"><span class="badge {cls}">{d.severity}</span> <b>{d.type}</b>
            <span class="mut">· {d.code or '—'}</span></div>
          <div>{d.name}</div>
          <div class="mut small">{vals}</div>
          <div class="small">→ {d.suggested_action}</div>
        </div>"""

    audit_rows = ""
    if audit_path and audit_path.exists():
        lines = [json.loads(x) for x in audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        for a in lines[-6:][::-1]:
            audit_rows += (f"<tr><td class='mono'>{a['ts'][11:19]}</td><td>{a['actor']}</td>"
                           f"<td>{a['action']}</td><td class='mono small mut'>{a['hash'][:12]}…</td></tr>")

    norms_block = ""
    if norms_last:
        cites = "".join(
            f"<div class='cite'><b>{c['doc']}, п. {c['clause']}</b>"
            f"<div class='small mut'>редакция: {c['edition'] or '—'} · проверено: "
            f"{'да' if c['verified'] else 'нет'}</div>"
            f"<div class='quote'>«{c['quote'][:260]}»</div></div>"
            for c in norms_last.get("citations", []))
        norms_block = f"""
        <section>
          <h2>Нормативная проверка (RAG с обязательной цитатой)</h2>
          <p class="hint">Запрос: «{norms_last.get('query', '')}» · тип ответа: {norms_last.get('answer_type')}</p>
          <div class="panel">{cites}</div>
        </section>"""

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Отчёт руководителю · {rec.object_name}</title>
<style>
  :root {{ --navy:#1F3864; --blue:#2E5C9A; --accent:#C0502A; --ok:#2E7D5B;
          --warn:#B7791F; --bg:#F4F6FA; --card:#fff; --border:#DCE3EE; --mut:#6B7688; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:#1E2430;
         font:15px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif; }}
  .top {{ background:var(--navy); color:#fff; padding:20px 28px; }}
  .top h1 {{ margin:0; font-size:21px; }}
  .top .meta {{ color:#B8C7DF; font-size:13px; margin-top:4px; }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:0 28px 60px; }}
  h2 {{ font-size:17px; color:var(--navy); margin:28px 0 6px; }}
  .hint {{ color:var(--mut); font-size:12.5px; margin:0 0 12px; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-top:20px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }}
  .card .k {{ font-size:12px; color:var(--mut); text-transform:uppercase; letter-spacing:.4px; }}
  .card .v {{ font-size:25px; font-weight:700; color:var(--navy); margin:6px 0 2px; }}
  .card .d {{ font-size:12.5px; color:var(--mut); }}
  .card.risk {{ border-left:4px solid var(--accent); }}
  .card.okc {{ border-left:4px solid var(--ok); }}
  table {{ width:100%; border-collapse:collapse; background:var(--card);
           border:1px solid var(--border); border-radius:10px; overflow:hidden; font-size:13.5px; }}
  th {{ background:#EDF1F8; color:var(--navy); font-size:11.5px; text-transform:uppercase;
        letter-spacing:.3px; text-align:left; padding:9px 10px; }}
  td {{ padding:9px 10px; border-bottom:1px solid #EEF1F7; vertical-align:top; }}
  .num {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
  .mono {{ font-family:Consolas,"SFMono-Regular",monospace; font-size:12.5px; }}
  .small {{ font-size:12.5px; }} .mut {{ color:var(--mut); }}
  .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .strong {{ font-weight:700; color:var(--navy); }}
  .bar {{ display:inline-block; width:110px; height:9px; background:#E8EDF5; border-radius:5px; overflow:hidden; }}
  .fill {{ display:block; height:100%; }}
  .pct {{ font-size:12px; color:var(--mut); margin-left:8px; }}
  .panel {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }}
  .disc {{ background:var(--card); border:1px solid var(--border); border-left-width:4px;
           border-radius:8px; padding:10px 12px; margin-bottom:9px; }}
  .disc.risk {{ border-left-color:var(--accent); }}
  .disc.warn {{ border-left-color:var(--warn); }}
  .disc-head {{ margin-bottom:3px; }}
  .badge {{ font-size:11px; font-weight:700; padding:2px 8px; border-radius:10px; }}
  .badge.risk {{ background:#FBE7E0; color:#9C3A1B; }}
  .badge.warn {{ background:#FBF0DA; color:#8A5A10; }}
  .cite {{ padding:10px 0; border-bottom:1px dashed #E6EBF3; }}
  .quote {{ background:#F7F9FC; border-left:3px solid var(--blue); padding:8px 10px;
            margin-top:6px; font-size:13px; }}
  .kpi-line {{ display:flex; gap:22px; flex-wrap:wrap; margin-top:10px; font-size:13.5px; }}
  .kpi-line b {{ color:var(--navy); }}
  .note {{ background:#FFF8E9; border:1px solid #F0DDAE; border-radius:8px; padding:12px 14px;
           font-size:12.5px; color:#6B5622; margin-top:14px; }}
  footer {{ border-top:1px solid var(--border); margin-top:26px; padding-top:14px;
            font-size:12px; color:var(--mut); }}
</style></head><body>
<div class="top">
  <h1>Отчёт руководителю · {rec.object_name}</h1>
  <div class="meta">Период: {rec.period[0]:%d.%m.%Y} — {rec.period[1]:%d.%m.%Y} ·
    сформировано {date.today():%d.%m.%Y} · агент «Сметчик» v0.1 ·
    данные синтетические (демонстрация)</div>
</div>
<div class="wrap">
  <div class="cards">
    <div class="card"><div class="k">Освоено (по журналу)</div><div class="v">{progress:.1f} %</div>
      <div class="d">от сметной стоимости {_m(rec.amount_estimate)}</div></div>
    <div class="card okc"><div class="k">Рекомендуется к предъявлению</div>
      <div class="v">{_m(rec.amount_act_safe)}</div>
      <div class="d">без НДС · НДС {vat_rate:.0f} % = {_m(safe_vat)}</div></div>
    <div class="card risk"><div class="k">Заблокировано замечаниями</div>
      <div class="v">{_m(rec.amount_blocked)}</div>
      <div class="d">из {_m(total)} по журналу работ</div></div>
    <div class="card"><div class="k">Остаток по смете</div><div class="v">{_m(rec.amount_remainder)}</div>
      <div class="d">критичных: {sev['critical']} · существенных: {sev['major']}</div></div>
  </div>

  <h2>Остаточное количество и предъявление</h2>
  <p class="hint">Остаток = смета − закрыто ранее − выполнено по журналу.
     Жёлтым — выполнено, но не закрыто актом; красным — отрицательный остаток (перебор);
     «К предъявлению» — объём, который агент рекомендует включить в акт после снятия замечаний
     («—» = позиция в текущем периоде не предъявляется, причина в блоке расхождений).</p>
  <table>
    <thead><tr><th>Шифр</th><th>Наименование работ</th><th class="num">Смета</th>
      <th class="num">Закрыто ранее</th><th class="num">Выполнено</th>
      <th class="num">Остаток</th><th class="num">Остаток, ₽</th>
      <th class="num">К предъявлению</th><th style="width:170px">Выполнение</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>

  <h2>Расхождения ({len(rec.discrepancies)})</h2>
  <p class="hint">Найдены автоматически по правилам: дубли, превышение сметы, повторное закрытие,
     единицы измерения, отсутствие подтверждающих документов, работы вне сметы.</p>
  {disc_rows or '<div class="panel">Расхождений не найдено.</div>'}

  {norms_block}

  <h2>Журнал действий (фрагмент)</h2>
  <table><thead><tr><th style="width:80px">Время</th><th style="width:220px">Кто</th>
    <th>Действие</th><th style="width:130px">Хэш</th></tr></thead>
    <tbody>{audit_rows or '<tr><td colspan="4" class="mut">нет записей</td></tr>'}</tbody></table>

  <div class="note">
    <b>Методика.</b> Вся арифметика выполняется кодом, не моделью. Остаток считается как
    смета − объём, закрытый подписанными актами, − объём по журналам, ещё не закрытый актом;
    работы без подтверждающих документов в остаток не списываются. Каждая строка связана
    с источником (журнал, строка, документ). Журнал действий защищён хэш-цепочкой.
  </div>
  <footer>ИИ-агент ПТО · MVP v0.1 · данные: смета v1, журнал работ за октябрь, ранее закрытые акты ·
  проверка целостности журнала: <code>python -m pto_agent audit-verify</code></footer>
</div></body></html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
