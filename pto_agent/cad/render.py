# -*- coding: utf-8 -*-
"""Растровый предпросмотр чертежа (png): человек подтверждает изменения глазами."""
import shutil
import tempfile
from pathlib import Path

# Шрифты, которых нет в контейнере, подменяются при рендере (в DXF не меняются).
FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def _prepare_doc(drawing, substitute_fonts: bool = True):
    """Вернуть документ для рендера: при необходимости — с подменёнными шрифтами."""
    if not substitute_fonts:
        return drawing.doc, None
    tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
    tmp.close()
    drawing.doc.saveas(tmp.name)
    import ezdxf
    doc = ezdxf.readfile(tmp.name)
    # цвет 7 в AutoCAD означает «чёрный на белой бумаге», но библиотека рендера
    # отдаёт его как белый. Для предпросмотра подменяем на тёмно-серый (ACI 250);
    # сам DXF не меняется.
    for layer in doc.layers:
        if layer.dxf.color == 7:
            layer.dxf.color = 250
    for e in doc.modelspace():
        if e.dxf.get("color", 256) == 7:
            e.dxf.color = 250

    fallback = next((f for f in FALLBACK_FONTS if Path(f).exists()), None)
    changed = []
    if fallback:
        for style in doc.styles:
            font = style.dxf.get("font", "")
            if font and not _font_available(font):
                style.dxf.font = Path(fallback).name
                changed.append({"style": style.dxf.name, "font_in_file": font,
                                "render_font": Path(fallback).name})
    return doc, {"tmp": tmp.name, "substituted": changed}


def _font_available(font: str) -> bool:
    try:
        from ezdxf.fonts import fonts as efonts
        manager = efonts.font_manager
        return bool(manager.find_font_face(font))
    except Exception:
        path = Path(font)
        return path.exists()


def render_png(drawing, out_png: str | Path, width_in: float = 16.0, height_in: float = 11.0,
               dpi: int = 110, bg: str = "#FFFFFF", layout=None, zoom=None,
               substitute_fonts: bool = True) -> Path:
    """Отрисовать лист или его фрагмент. zoom = (x0, y0, x1, y1) в единицах модели."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)

    doc, meta = _prepare_doc(drawing, substitute_fonts)
    fig = plt.figure(figsize=(width_in, height_in), dpi=dpi)
    fig.patch.set_facecolor(bg)
    ax = fig.add_axes([0.005, 0.005, 0.99, 0.99])
    ax.set_facecolor(bg)
    ax.set_axis_off()

    ctx = RenderContext(doc)
    try:
        ctx.current_layout_properties.set_colors(bg)
    except Exception:
        pass
    Frontend(ctx, MatplotlibBackend(ax)).draw_layout(layout or doc.modelspace(), finalize=True)
    fig.set_size_inches(width_in, height_in)   # ezdxf-бэкенд подгоняет размер — возвращаем свой
    if zoom:
        x0, y0, x1, y1 = zoom
        ax.set_aspect("auto")      # иначе matplotlib игнорирует заданные пределы
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
    fig.savefig(out_png, dpi=dpi, facecolor=bg)
    plt.close(fig)

    if meta and meta.get("tmp"):
        Path(meta["tmp"]).unlink(missing_ok=True)
    if meta and meta["substituted"]:
        fonts = sorted({c["font_in_file"] for c in meta["substituted"]})
        shown = ", ".join(fonts[:4]) + (" …" if len(fonts) > 4 else "")
        print(f"  [рендер] шрифтов подменено: {len(fonts)} ({shown}) → "
              f"{meta['substituted'][0]['render_font']}; в самом DXF шрифты не менялись")
    return out_png


def render_two_panels(drawing, out_png: str | Path, zoom, width_in: float = 15.0,
                      height_in: float = 9.0, dpi: int = 110) -> Path:
    """Лист целиком + увеличенный фрагмент в одном файле (для просмотра человеком)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    doc, meta = _prepare_doc(drawing, True)

    fig = plt.figure(figsize=(width_in, height_in), dpi=dpi)
    fig.patch.set_facecolor("#FFFFFF")
    ax1 = fig.add_axes([0.01, 0.03, 0.47, 0.86])
    ax2 = fig.add_axes([0.52, 0.03, 0.46, 0.86])
    for ax, title, lim in ((ax1, "Лист целиком", None), (ax2, "Фрагмент: объёмы и пометки", zoom)):
        ax.set_facecolor("#FFFFFF")
        ax.set_axis_off()
        ctx = RenderContext(doc)
        try:
            ctx.current_layout_properties.set_colors("#FFFFFF")
        except Exception:
            pass
        Frontend(ctx, MatplotlibBackend(ax)).draw_layout(doc.modelspace(), finalize=True)
        fig.set_size_inches(width_in, height_in)
        if lim:
            ax.set_aspect("auto")
            ax.set_xlim(lim[0], lim[2])
            ax.set_ylim(lim[1], lim[3])
        ax.set_title(title, fontsize=10, color="#1F3864", pad=10)
    fig.savefig(out_png, dpi=dpi, facecolor="#FFFFFF")
    plt.close(fig)
    if meta and meta.get("tmp"):
        Path(meta["tmp"]).unlink(missing_ok=True)
    return out_png
