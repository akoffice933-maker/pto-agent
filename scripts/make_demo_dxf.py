# -*- coding: utf-8 -*-
"""Генерация демо-чертежа (DXF) для CAD-модуля ИИ-агента ПТО.

Модель в 1:1 (мм), лист A1 при масштабе 1:100: 1 мм на бумаге = 100 мм в модели.
Высота текста 250 мм в модели = 2,5 мм в печати (требование шаблона).

Создаёт:
  drawings/korpus2_plan_v1.dxf — план фундаментной плиты с корпоративной рамкой
                                 и штампом, машиночитаемыми объёмами (блок ПОЗ_ОБЪЁМ)
  drawings/korpus2_plan_v1.png — предпросмотр листа
  drawings/korpus2_plan_v1_zoom.png — увеличенный фрагмент с объёмами

НАМЕРЕННО заложенные дефекты оформления (их должна поймать проверка шаблона):
  1. линия на слое 0 (защищённый слой);
  2. три текста высотой 200 мм в модели = 2,0 мм в печати при минимуме 2,5 мм;
  3. слой «МУСОР» отсутствует в шаблоне;
  4. блок «УЗЕЛ_СТАРЫЙ» отсутствует в шаблоне;
  5. текстовый стиль «ARIAL_OLD» не из шаблона;
  6. в штампе не заполнено обязательное поле «ШИФР».
"""
import sys
from pathlib import Path

import ezdxf

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "drawings"

SCALE = 100                     # 1:100
FRAME_W, FRAME_H = 841 * SCALE, 594 * SCALE     # A1 в модели
TXT = int(2.5 * SCALE)                          # 2,5 мм на бумаге


def build_drawing() -> ezdxf.document.Drawing:
    doc = ezdxf.new("R2010", setup=True)
    doc.units = 4                       # миллиметры
    doc.header["$INSUNITS"] = 4

    # ── стили и размеры ──────────────────────────────────────────────────
    doc.styles.add("СПДС", font="isocpeur.ttf")
    doc.styles.add("GOST_TYPE_A", font="gost type a.ttf")
    doc.styles.add("ARIAL_OLD", font="arial.ttf")           # дефект №5
    doc.dimstyles.add("СПДС_2.5")

    # ── слои ─────────────────────────────────────────────────────────────
    layers = {
        "ОС_РАМКА": (7, "CONTINUOUS"), "ОС_ШТАМП": (7, "CONTINUOUS"),
        "ПЛ_ФУНДАМЕНТ": (1, "CONTINUOUS"), "ИЗ_ГИДРОИЗОЛЯЦИЯ": (3, "CONTINUOUS"),
        "КО_КАРКАС": (2, "CONTINUOUS"), "ВК_ВОДОСНАБЖЕНИЕ": (4, "CONTINUOUS"),
        "КР_КРОВЛЯ": (6, "DASHED"), "ТЕКСТ": (7, "CONTINUOUS"),
        "РАЗМЕРЫ": (5, "CONTINUOUS"), "ПОМЕТКИ": (30, "CONTINUOUS"),
        "МУСОР": (9, "CONTINUOUS"),                         # дефект №3
    }
    for name, (color, lt) in layers.items():
        doc.layers.add(name, color=color, linetype=lt)

    # ── блок оформления: рамка ───────────────────────────────────────────
    frame = doc.blocks.new("РАМКА_A1")
    frame.add_lwpolyline([(0, 0), (FRAME_W, 0), (FRAME_W, FRAME_H), (0, FRAME_H)], close=True)
    inset = 20 * SCALE
    frame.add_lwpolyline([(inset, inset / 2), (FRAME_W - inset / 2, inset / 2),
                          (FRAME_W - inset / 2, FRAME_H - inset / 2), (inset, FRAME_H - inset / 2)],
                         close=True)

    # ── блок оформления: штамп (185×55 мм на бумаге) ─────────────────────
    stamp = doc.blocks.new("ШТАМП_КОРП_A")
    sw, sh = 185 * SCALE, 55 * SCALE
    stamp.add_lwpolyline([(0, 0), (sw, 0), (sw, sh), (0, sh)], close=True)
    fields = [("ОБЪЕКТ", 0.80), ("ШИФР", 0.66), ("ВИД_РАБОТ", 0.52),
              ("ДАТА", 0.38), ("ФИО", 0.24), ("МАСШТАБ", 0.10)]
    for tag, k in fields:
        y = sh * k
        stamp.add_text(f"{tag}:", height=TXT, dxfattribs={"insert": (5 * SCALE, y),
                                                          "style": "СПДС"})
        stamp.add_attdef(tag, (55 * SCALE, y), dxfattribs={"height": TXT, "style": "СПДС"})

    # ── блок машиночитаемого объёма: связь чертежа с КС-2 ────────────────
    poz = doc.blocks.new("ПОЗ_ОБЪЁМ")
    pw, ph = 34 * SCALE, 17 * SCALE
    poz.add_lwpolyline([(0, 0), (pw, 0), (pw, ph), (0, ph)], close=True)
    for tag, k in (("PTO_CODE", 0.78), ("PTO_QTY", 0.55), ("PTO_UNIT", 0.34),
                   ("PTO_NAME", 0.12)):
        poz.add_attdef(tag, (2 * SCALE, ph * k), dxfattribs={"height": TXT * 0.8,
                                                             "style": "СПДС"})

    col = doc.blocks.new("КОЛОННА_400")
    col.add_lwpolyline([(-200, -200), (200, -200), (200, 200), (-200, 200)], close=True)
    col.add_circle((0, 0), 90)

    old = doc.blocks.new("УЗЕЛ_СТАРЫЙ")                     # дефект №4
    old.add_lwpolyline([(0, 0), (7000, 0), (7000, 4000), (0, 4000)], close=True)

    msp = doc.modelspace()

    # ── рамка и штамп ────────────────────────────────────────────────────
    msp.add_blockref("РАМКА_A1", (0, 0), dxfattribs={"layer": "ОС_РАМКА"})
    tb = msp.add_blockref("ШТАМП_КОРП_A", (FRAME_W - sw - 40 * SCALE, 40 * SCALE),
                          dxfattribs={"layer": "ОС_ШТАМП"})
    tb.add_auto_attribs({
        "ОБЪЕКТ": "ЖК «Северный», корпус 2",
        "ШИФР": "",                                         # дефект №6
        "ВИД_РАБОТ": "Монолитные работы. Фундаментная плита",
        "ДАТА": "29.10.2026",
        "ФИО": "",
        "МАСШТАБ": "1:100",
    })

    # ── рабочая часть: плита 30 × 24 м ───────────────────────────────────
    msp.add_lwpolyline([(12000, 15000), (42000, 15000)], dxfattribs={"layer": "ПЛ_ФУНДАМЕНТ"})
    plate = [(12000, 15000), (42000, 15000), (42000, 39000), (12000, 39000)]
    msp.add_lwpolyline(plate, close=True, dxfattribs={"layer": "ПЛ_ФУНДАМЕНТ"})
    msp.add_lwpolyline([(13500, 16500), (40500, 16500), (40500, 37500), (13500, 37500)],
                       close=True, dxfattribs={"layer": "ИЗ_ГИДРОИЗОЛЯЦИЯ"})
    msp.add_lwpolyline([(20000, 21000), (35000, 21000), (35000, 34000), (20000, 34000)],
                       close=True, dxfattribs={"layer": "КР_КРОВЛЯ"})

    for x in (18000, 27000, 36000):
        for y in (20000, 34000):
            msp.add_blockref("КОЛОННА_400", (x, y), dxfattribs={"layer": "КО_КАРКАС"})

    msp.add_lwpolyline([(20000, 43000), (40000, 43000)], dxfattribs={"layer": "ВК_ВОДОСНАБЖЕНИЕ"})
    msp.add_lwpolyline([(20000, 43000), (20000, 39500)], dxfattribs={"layer": "ВК_ВОДОСНАБЖЕНИЕ"})

    msp.add_linear_dim(base=(12000, 13000), p1=(12000, 15000), p2=(42000, 15000),
                       dimstyle="СПДС_2.5", dxfattribs={"layer": "РАЗМЕРЫ"}).render()

    # подписи
    msp.add_text("План фундаментной плиты. Схема исполнительная",
                 height=7 * SCALE, dxfattribs={"layer": "ТЕКСТ", "style": "СПДС",
                                               "insert": (12000, 46000)})
    labels = [
        ("Плита h=600, B25", 25000, 27000, TXT, "СПДС"),
        ("Гидроизоляция обмазочная", 15000, 17500, TXT, "СПДС"),
        ("Каркас: колонны 400×400", 22000, 36000, TXT, "GOST_TYPE_A"),
        ("Ввод водопровода Ду50", 30000, 44000, 200, "СПДС"),        # дефект №2
        ("Ось 1", 18000, 13500, 200, "СПДС"),                        # дефект №2
        ("Отм. -3.300", 36000, 38000, 200, "ARIAL_OLD"),             # дефекты №2 и №5
    ]
    for text, x, y, h, style in labels:
        msp.add_text(text, height=h, dxfattribs={"layer": "ТЕКСТ", "style": style,
                                                 "insert": (x, y)})

    # ── машиночитаемые объёмы (связь с КС-2) ────────────────────────────
    positions = [
        ("ФЕР 06-01-001-01", "1240", "м3", "Плита фундаментная B25", (8000, 32000)),
        ("ФЕР 08-02-001-01", "1180", "м2", "Гидроизоляция обмазочная", (8000, 26000)),
        ("ФЕР 12-01-034-02", "900", "м2", "Кровля ПВХ-мембрана", (44000, 26000)),
        ("ФЕР 11-01-011-01", "210", "м3", "Стяжка полов (бетон)", (44000, 32000)),
        ("ФЕР 16-02-003-01", "440", "м", "Водопровод Ду50", (44000, 43000)),
        ("", "340", "м2", "Устройство отмостки вокруг здания", (8000, 20000)),
    ]
    for code, qty, unit, name, point in positions:
        ref = msp.add_blockref("ПОЗ_ОБЪЁМ", point, dxfattribs={"layer": "ТЕКСТ"})
        ref.add_auto_attribs({"PTO_CODE": code, "PTO_QTY": qty,
                              "PTO_UNIT": unit, "PTO_NAME": name})

    # ── дефекты оформления ───────────────────────────────────────────────
    msp.add_line((8000, 8000), (8000, 12000), dxfattribs={"layer": "0"})          # дефект №1
    msp.add_circle((70000, 50000), 1200, dxfattribs={"layer": "МУСОР"})           # дефект №3
    msp.add_blockref("УЗЕЛ_СТАРЫЙ", (64000, 44000), dxfattribs={"layer": "ТЕКСТ"})  # дефект №4

    return doc


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    dxf_path = OUT / "korpus2_plan_v1.dxf"
    build_drawing().saveas(str(dxf_path))
    print(f"чертёж: {dxf_path}")

    from pto_agent.cad.drawing import Drawing
    from pto_agent.cad.render import render_png

    d = Drawing.open(dxf_path)
    render_png(d, OUT / "korpus2_plan_v1.png", width_in=14.5, height_in=10.3)
    render_png(d, OUT / "korpus2_plan_v1_zoom.png", width_in=12, height_in=8,
               zoom=(6500, 17500, 22500, 35000))
    print(f"предпросмотр: {OUT / 'korpus2_plan_v1.png'}")
    print(f"фрагмент:     {OUT / 'korpus2_plan_v1_zoom.png'}")
    print(f"объектов в модели: {len(d.msp)}")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
