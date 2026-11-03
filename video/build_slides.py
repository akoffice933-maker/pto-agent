#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Рендер слайдов видео-презентации (1920×1080) по narration.json."""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
NAVY = (31, 56, 100)
NAVY_D = (22, 40, 72)
BLUE = (46, 92, 154)
ACCENT = (192, 80, 42)
OK = (46, 125, 91)
WARN = (183, 121, 31)
BG = (244, 246, 250)
CARD = (255, 255, 255)
BORDER = (220, 227, 238)
TEXT = (30, 36, 48)
MUT = (107, 118, 136)
LIGHT_TXT = (220, 229, 242)

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
OUT = Path("video/slides")
OUT.mkdir(parents=True, exist_ok=True)
_cache = {}


def F(size, bold=False):
    key = (size, bold)
    if key not in _cache:
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        _cache[key] = ImageFont.truetype(str(FONT_DIR / name), size)
    return _cache[key]


def wrap(text, font, max_w, draw):
    out = []
    for raw in text.split("\n"):
        if not raw:
            out.append("")
            continue
        words, line = raw.split(" "), ""
        for w in words:
            probe = (line + " " + w).strip()
            if draw.textlength(probe, font=font) <= max_w or not line:
                line = probe
            else:
                out.append(line)
                line = w
        out.append(line)
    return out


def block(draw, xy, text, font, fill, max_w, lh=None):
    x, y = xy
    lh = lh or int(font.size * 1.32)
    for ln in wrap(text, font, max_w, draw):
        draw.text((x, y), ln, font=font, fill=fill)
        y += lh
    return y


def rrect(draw, box, r, fill, outline=None, width=2):
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def chip(draw, x, y, text, font, fg, bg, pad_x=16, pad_y=9, r=18):
    w = draw.textlength(text, font=font) + pad_x * 2
    h = font.size + pad_y * 2
    rrect(draw, (x, y, x + w, y + h), r, bg)
    draw.text((x + pad_x, y + pad_y - 2), text, font=font, fill=fg)
    return w


def header(draw, s, dark=False):
    k_font, t_font, l_font = F(24, True), F(58, True), F(28)
    kicker_col = ACCENT if not dark else (232, 145, 110)
    title_col = NAVY if not dark else (255, 255, 255)
    lead_col = MUT if not dark else LIGHT_TXT
    draw.text((110, 84), s["kicker"].upper(), font=k_font, fill=kicker_col)
    y = block(draw, (110, 122), s["title"], t_font, title_col, 1700, lh=int(58 * 1.16))
    if s.get("lead"):
        block(draw, (110, y + 10), s["lead"], l_font, lead_col, 1640, lh=38)
    return 350 if not s.get("lead") else max(360, y + 70)


def footer(draw, n, total, dark=False):
    col = MUT if not dark else (120, 135, 165)
    draw.line([(110, 986), (1810, 986)], fill=BORDER if not dark else (52, 72, 108), width=2)
    draw.text((110, 1002), "ИИ-агент ПТО  ·  «Цифровой инженер ПТО»  ·  02.11.2026",
              font=F(20), fill=col)
    draw.text((1810 - 52, 1002), f"{n} / {total}", font=F(20, True), fill=col)
    seg_w, gap = 150, 12
    x = 110
    for i in range(total):
        c = ACCENT if i == n - 1 else (BORDER if not dark else (52, 72, 108))
        draw.line([(x, 1050), (x + seg_w - gap, 1050)], fill=c, width=5)
        x += seg_w


def bullet_list(draw, x, y, items, max_w, size=29, gap=16, col=TEXT, dot=BLUE):
    for it in items:
        draw.ellipse((x, y + size * 0.36, x + 12, y + size * 0.36 + 12), fill=dot)
        y = block(draw, (x + 34, y), it, F(size), col, max_w - 34, lh=int(size * 1.34)) + gap
    return y


def kpi_card(draw, x, y, w, h, value, label, sub, accent=ACCENT):
    rrect(draw, (x, y, x + w, y + h), 18, CARD, BORDER, 2)
    draw.rectangle((x, y + 18, x + 7, y + h - 18), fill=accent)
    draw.text((x + 36, y + 26), value, font=F(54, True), fill=NAVY)
    draw.text((x + 36, y + 92), label, font=F(24, True), fill=TEXT)
    block(draw, (x + 36, y + 128), sub, F(20), MUT, w - 70)


def panel(draw, x, y, w, h, title, body, size=21):
    rrect(draw, (x, y, x + w, y + h), 16, CARD, BORDER, 2)
    draw.text((x + 28, y + 24), title, font=F(23, True), fill=NAVY)
    block(draw, (x + 28, y + 66), body, F(size), MUT, w - 56, lh=30)


def table(draw, x, y, w, rows, col_w, hdr_size=21, size=21, row_h=62):
    # header
    draw.rectangle((x, y, x + w, y + row_h - 10), fill=(237, 241, 248))
    cx = x + 18
    for i, c in enumerate(rows[0]):
        draw.text((cx, y + 16), c, font=F(hdr_size, True), fill=NAVY)
        cx += col_w[i]
    yy = y + row_h
    for ri, row in enumerate(rows[1:]):
        if ri % 2 == 1:
            draw.rectangle((x, yy - 6, x + w, yy + row_h - 14), fill=(250, 251, 253))
        cx = x + 18
        for i, c in enumerate(row):
            draw.text((cx, yy + 8), c, font=F(size, i == 0), fill=TEXT if i == 0 else MUT)
            cx += col_w[i]
        draw.line([(x, yy + row_h - 14), (x + w, yy + row_h - 14)], fill=(238, 241, 247), width=2)
        yy += row_h
    return yy


def bar(draw, x, y, w, h, pct, color=BLUE, wip=0.0):
    rrect(draw, (x, y, x + w, y + h), h // 2, (232, 237, 245))
    fw = int(w * min(pct, 100) / 100)
    if fw > 3:
        rrect(draw, (x, y, x + fw, y + h), h // 2, color)
    if wip:
        ww = int(w * min(wip, max(0, 100 - pct)) / 100)
        if ww > 3:
            rrect(draw, (x + fw, y, x + fw + ww, y + h), h // 2, (214, 178, 76))


# ------------------------------------------------------------------ слайды ---
def slide_cover(s, total):
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 10), fill=ACCENT)
    d.text((110, 150), s["kicker"].upper(), font=F(24, True), fill=(232, 145, 110))
    y = block(d, (110, 196), s["title"], F(96, True), (255, 255, 255), 1600, lh=112)
    block(d, (110, y + 34), s["lead"], F(34), LIGHT_TXT, 1450, lh=48)
    x = 110
    for c in s["chips"]:
        w = chip(d, x, 700, c, F(23), (255, 255, 255), (46, 74, 122), 20, 12, 22)
        x += w + 14
    d.line([(110, 900), (1000, 900)], fill=(52, 80, 128), width=2)
    d.text((110, 918), "Техническое задание v1.2  ·  демонстрационные данные  ·  10 слайдов  ·  ≈ 3,5 минуты",
           font=F(21), fill=(150, 168, 198))
    footer(d, s["n"], total, dark=True)
    img.save(OUT / f"slide_{s['n']:02d}.png")


def slide_problem(s, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y0 = header(d, s)
    cw, gap = 553, 22
    for i, (v, l, sub) in enumerate(s["kpis"]):
        kpi_card(d, 110 + i * (cw + gap), y0 + 10, cw, 200, v, l, sub)
    y1 = y0 + 250
    d.text((110, y1), "Почему это дорого", font=F(30, True), fill=NAVY)
    half = (1700 - 60) // 2
    bullet_list(d, 110, y1 + 56, s["bullets"][:2], half, size=25, gap=14)
    bullet_list(d, 110 + half + 60, y1 + 56, s["bullets"][2:], half, size=25, gap=14)
    rrect(d, (110, 800, 1810, 900), 14, (255, 248, 233), (240, 221, 174), 2)
    block(d, (140, 828),
          "Источник оценок — интервью с инженерами ПТО. Точные значения фиксируются "
          "хронометражем на этапе 0 — это база для метрик и для критериев остановки проекта.",
          F(22), (107, 86, 34), 1620, lh=32)
    footer(d, s["n"], total)
    img.save(OUT / f"slide_{s['n']:02d}.png")


def slide_scope(s, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y0 = header(d, s)
    cw, ch, gap = (1690 - 30) // 2, 250, 30
    for i, (t, body) in enumerate(s["cards"]):
        x = 110 + (i % 2) * (cw + gap)
        y = y0 + 14 + (i // 2) * (ch + gap)
        rrect(d, (x, y, x + cw, y + ch), 18, CARD, BORDER, 2)
        d.rectangle((x, y + 22, x + 7, y + 58), fill=ACCENT)
        d.text((x + 34, y + 26), t, font=F(30, True), fill=NAVY)
        block(d, (x + 34, y + 82), body, F(22), MUT, cw - 68, lh=32)
    footer(d, s["n"], total)
    img.save(OUT / f"slide_{s['n']:02d}.png")


def slide_pipeline(s, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y0 = header(d, s)
    n = len(s["steps"])
    gap = 16
    cw = (1690 - gap * (n - 1)) // n
    y = y0 + 20
    for i, (num, title, body) in enumerate(s["steps"]):
        x = 110 + i * (cw + gap)
        rrect(d, (x, y, x + cw, y + 200), 16, CARD, BORDER, 2)
        d.rectangle((x, y, x + cw, y + 6), fill=ACCENT)
        d.text((x + 24, y + 22), num, font=F(34, True), fill=ACCENT)
        d.text((x + 24, y + 72), title, font=F(22, True), fill=NAVY)
        block(d, (x + 24, y + 108), body, F(18), MUT, cw - 48, lh=25)
        if i < n - 1:
            d.text((x + cw + 2, y + 84), "›", font=F(30, True), fill=BLUE)
    y2 = y + 240
    d.text((110, y2), "Правила, которые делают результат проверяемым", font=F(28, True), fill=NAVY)
    table(d, 110, y2 + 52, 1690, [["Контроль", "Как реализовано"]] + s["rules"], [420, 1270],
          row_h=66, size=22)
    footer(d, s["n"], total)
    img.save(OUT / f"slide_{s['n']:02d}.png")


def slide_dashboard(s, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y0 = header(d, s)
    y = y0 + 10
    for i, (v, l) in enumerate(s["big"]):
        rrect(d, (110, y, 780, y + 128), 16, CARD, BORDER, 2)
        d.text((140, y + 26), v, font=F(46, True), fill=NAVY)
        d.text((140, y + 88), l, font=F(21), fill=MUT)
        y += 146
    block(d, (110, y + 6), s["note"], F(20), WARN, 760, lh=28)

    # мок-таблица остатков
    x0, w = 940, 870
    rrect(d, (x0 - 20, y0 - 10, 1830, 890), 18, CARD, BORDER, 2)
    d.text((x0, y0 + 14), "Остатки по видам работ", font=F(25, True), fill=NAVY)
    d.text((x0, y0 + 52), "штриховка — выполнено, но не закрыто актом", font=F(18), fill=MUT)
    ry = y0 + 96
    for name, pct, wip, rest in s["rows"]:
        d.text((x0, ry), name, font=F(21, True), fill=TEXT)
        d.text((1780 - d.textlength(rest, font=F(18)), ry + 2), rest, font=F(18), fill=MUT)
        bar(d, x0, ry + 32, 560, 14, pct, BLUE, wip)
        d.text((x0 + 580, ry + 26), f"{pct:.0f} %".replace(".", ","), font=F(18, True), fill=NAVY)
        ry += 82
    footer(d, s["n"], total)
    img.save(OUT / f"slide_{s['n']:02d}.png")


def slide_cad(s, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y0 = header(d, s)
    n = len(s["steps"])
    gap = 16
    cw = (1690 - gap * (n - 1)) // n
    y = y0 + 10
    for i, txt in enumerate(s["steps"]):
        x = 110 + i * (cw + gap)
        fill = (251, 233, 226) if i == 3 else (232, 237, 245)
        rrect(d, (x, y, x + cw, y + 118), 14, fill, BORDER, 2)
        txt = txt if isinstance(txt, str) else "\n".join(txt)
        lines = txt.split("\n")
        ty = y + 28 if len(lines) > 1 else y + 44
        for ln in lines:
            lw = d.textlength(ln, font=F(20, True))
            d.text((x + (cw - lw) / 2, ty), ln, font=F(20, True), fill=NAVY)
            ty += 28
        if i < n - 1:
            d.text((x + cw + 2, y + 44), "›", font=F(28, True), fill=BLUE)
    ty = y + 150
    d.text((110, ty), "Три способа управлять чертежом", font=F(27, True), fill=NAVY)
    table(d, 110, ty + 46, 1690, s["table"], [430, 640, 620], row_h=62, size=21)
    block(d, (110, ty + 46 + 62 * 4 + 16), s["note"], F(21, True), ACCENT, 1650, lh=30)
    footer(d, s["n"], total)
    img.save(OUT / f"slide_{s['n']:02d}.png")


def slide_quad(s, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y0 = header(d, s)
    cw, ch, gap = (1690 - 30) // 2, 250, 30
    for i, (t, body) in enumerate(s["quads"]):
        x = 110 + (i % 2) * (cw + gap)
        y = y0 + 14 + (i // 2) * (ch + gap)
        rrect(d, (x, y, x + cw, y + ch), 18, CARD, BORDER, 2)
        d.ellipse((x + 30, y + 30, x + 48, y + 48), fill=OK)
        d.text((x + 64, y + 26), t, font=F(28, True), fill=NAVY)
        block(d, (x + 30, y + 86), body, F(22), MUT, cw - 60, lh=32)
    footer(d, s["n"], total)
    img.save(OUT / f"slide_{s['n']:02d}.png")


def slide_llm(s, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y0 = header(d, s)
    y = y0 + 6
    for name, share, purpose, price in s["providers"]:
        col = BLUE if "Локально" in name else (BLUE if share else (176, 184, 196))
        d.text((110, y + 6), name, font=F(23, True), fill=TEXT if share else MUT)
        bx, bw = 700, 620
        if share:
            bar(d, bx, y + 14, bw, 18, share, col)
            d.text((bx + bw * share / 100 + 16, y + 8), f"{share} %", font=F(20, True), fill=NAVY)
        else:
            d.text((bx, y + 8), "заблокировано политикой ИБ", font=F(19, True), fill=ACCENT)
        d.text((1370, y + 8), purpose, font=F(19), fill=MUT)
        d.text((1780 - d.textlength(price, font=F(21, True)), y + 4), price, font=F(21, True),
               fill=OK if price == "0 ₽" else NAVY)
        d.line([(110, y + 54), (1810, y + 54)], fill=(234, 238, 245), width=2)
        y += 74
    # бюджет
    label, pctv = s["budget"]
    rrect(d, (110, y + 14, 1810, y + 130), 16, CARD, BORDER, 2)
    d.text((140, y + 34), label, font=F(24, True), fill=NAVY)
    d.text((140, y + 72), "при 80 % — предупреждение, при 100 % — внешние вызовы прекращаются",
           font=F(19), fill=MUT)
    bar(d, 1060, y + 46, 700, 20, pctv, WARN)
    d.text((1060 + 700 * pctv / 100 + 16, y + 40), f"{pctv:.1f} %".replace(".", ","),
           font=F(21, True), fill=WARN)
    footer(d, s["n"], total)
    img.save(OUT / f"slide_{s['n']:02d}.png")


def slide_plan(s, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y0 = header(d, s)
    n = len(s["phases"])
    gap = 14
    cw = (1690 - gap * (n - 1)) // n
    y = y0 + 6
    colors = [OK, BLUE, NAVY, BLUE, (155, 182, 221)]
    for i, (name, dates, body) in enumerate(s["phases"]):
        x = 110 + i * (cw + gap)
        rrect(d, (x, y, x + cw, y + 196), 14, CARD, BORDER, 2)
        d.rectangle((x, y, x + cw, y + 44), fill=colors[i])
        d.text((x + 18, y + 12), name, font=F(21, True), fill=(255, 255, 255))
        d.text((x + 18, y + 58), dates, font=F(17, True), fill=ACCENT)
        block(d, (x + 18, y + 90), body, F(19), MUT, cw - 36, lh=26)
    y2 = y + 232
    for i, (v, l) in enumerate(s["money"]):
        x = 110 + i * 566
        rrect(d, (x, y2, x + 546, y2 + 128), 16, CARD, BORDER, 2)
        d.text((x + 28, y2 + 22), v, font=F(40, True), fill=NAVY)
        d.text((x + 28, y2 + 80), l, font=F(20), fill=MUT)
    block(d, (110, y2 + 156), "Пилот — не более 15 % бюджета; критерии остановки зафиксированы в ТЗ.",
          F(21), MUT, 1600)
    footer(d, s["n"], total)
    img.save(OUT / f"slide_{s['n']:02d}.png")


def slide_final(s, total):
    img = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 10), fill=ACCENT)
    d.text((110, 96), s["kicker"].upper(), font=F(24, True), fill=(232, 145, 110))
    block(d, (110, 140), s["title"], F(64, True), (255, 255, 255), 1650, lh=76)
    y = 300
    for i, (t, body) in enumerate(s["asks"]):
        d.text((110, y + 4), t, font=F(26, True), fill=(155, 182, 221))
        block(d, (400, y), body, F(25), (255, 255, 255), 1400, lh=32)
        d.line([(110, y + 62), (1810, y + 62)], fill=(52, 80, 128), width=2)
        y += 92
    rrect(d, (110, y + 26, 1810, y + 132), 14, (44, 72, 120))
    d.text((140, y + 52), s["stop"], font=F(27, True), fill=(240, 200, 180))
    d.text((140, y + 94), "Бюджет пилота ограничен, решение принимается по метрикам, а не по демонстрации.",
           font=F(20), fill=(178, 194, 220))
    footer(d, s["n"], total, dark=True)
    img.save(OUT / f"slide_{s['n']:02d}.png")


BUILDERS = {"cover": slide_cover, "problem": slide_problem, "scope": slide_scope,
            "pipeline": slide_pipeline, "dashboard": slide_dashboard, "cad": slide_cad,
            "quad": slide_quad, "llm": slide_llm, "plan": slide_plan, "final": slide_final}

data = json.loads(Path("video/narration.json").read_text(encoding="utf-8"))
total = len(data["slides"])
for s in data["slides"]:
    BUILDERS[s["kind"]](s, total)
    print("rendered slide", s["n"], s["kind"])
print("slides:", total, "→", OUT)
