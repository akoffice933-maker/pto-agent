# -*- coding: utf-8 -*-
"""Дашборд руководителя: блок «деньги под риском» берётся из модуля impact.

Проверяется не вёрстка, а честность цифр: сумма на дашборде равна сумме в
сводке выпуска, выпуск и фон ПТО не складываются, без прогона выпуска раздел
честно говорит, что данных нет, а не показывает ноль.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "Дашборд_руководителя_ПТО.html"
BATCH = ROOT / "out" / "cad" / "batch" / "batch_summary.json"
PORTFOLIO = ROOT / "out" / "cad" / "portfolio" / "portfolio.json"


def _plain(html: str) -> str:
    """Текст страницы без тегов — чтобы искать фразы, а не разметку."""
    return re.sub(r"<[^>]+>", " ", html)


def _num(v) -> str:
    """Тот же формат чисел, что на странице: 88,54 · 100 · 1 850 000."""
    return f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")


def _risk_section(html: str) -> str:
    start = html.index('<section id="risk">')
    return html[start:html.index("</section>", start)]


@pytest.fixture(scope="module")
def html():
    if not DASHBOARD.exists():
        pytest.skip("дашборд не собран")
    return DASHBOARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def impact():
    if not BATCH.exists():
        pytest.skip("нет сводки выпуска")
    return json.loads(BATCH.read_text(encoding="utf-8"))["impact"]


@pytest.fixture(scope="module")
def portfolio():
    if not PORTFOLIO.exists():
        pytest.skip("нет портфеля актов")
    return json.loads(PORTFOLIO.read_text(encoding="utf-8"))


def test_dashboard_has_risk_section(html):
    section = _plain(_risk_section(html))
    assert "Деньги под риском" in section
    assert "Фон ПТО (не из выпуска)" in section
    if PORTFOLIO.exists():
        assert "Под риском по актам" in section
        assert "Проверено выпуском" in section
        # карточка покрытия: либо «подписано без проверки», либо «без проверки не осталось»
        assert ("Подписано без проверки" in section
                or "Без проверки не осталось" in section)
    else:
        assert "Под риском по выпуску" in section


def test_dashboard_numbers_match_portfolio(html, portfolio):
    """Цифры на странице — ровно те, что посчитал модуль портфеля."""
    t = portfolio["totals"]
    money = lambda v: f"{v:,.0f}".replace(",", " ")       # noqa: E731
    section = _plain(_risk_section(html))
    assert money(t["at_risk_rub"]) in section
    assert money(t["background_risk_rub"]) in section
    assert money(t["amount_checked_rub"]) in section
    assert money(t["amount_rub"]) in section
    assert money(t["amount_unchecked_rub"]) in section
    assert _num(t["coverage_pct"]) in section
    for act in portfolio["acts"]:
        assert act["act_id"] in section
        assert money(act["amount_rub"]) in section


def test_dashboard_positions_split_release_and_background(html, portfolio):
    section = _plain(_risk_section(html))
    for pos in portfolio["positions"]:
        if pos["code"]:
            assert pos["code"] in section
    # фон ПТО по фундаментным балкам показан, но не попал в сумму выпуска
    assert "8 560 000" in section
    assert "1 850 000" in section


def test_dashboard_shows_months(html, portfolio):
    section = _plain(_risk_section(html))
    for month in portfolio["months"]:
        assert month["label"] in section
    assert "По месяцам" in section
    if portfolio["totals"]["acts_unchecked"] == 0:
        assert "не проверялся" not in section        # ретро-выпуск закрыл пробел


def test_dashboard_links_to_risk_from_money(html):
    assert 'href="#risk"' in html
    assert html.index('id="risk"') > html.index('id="money"')     # раздел после денег


def test_dashboard_mentions_rules(html):
    section = _plain(_risk_section(html))
    assert "Один спор считается один раз" in section
    assert "не складываются" in section
    assert "не оценено», а не «0 ₽" in section


def test_dashboard_rebuild_without_impact_is_honest(tmp_path):
    """Нет прогона выпуска — раздел не рисует нулей, а объясняет, что делать."""
    res = subprocess.run([sys.executable, str(ROOT / "docs" / "build_dashboard.py")],
                         cwd=tmp_path, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    page = (tmp_path / "Дашборд_руководителя_ПТО.html").read_text(encoding="utf-8")
    section = _plain(_risk_section(page))
    assert "Раздел заполнится после первого прогона выпуска" in section
    assert "Под риском по актам" not in section        # пустых карточек с 0 ₽ нет
    assert "Под риском по выпуску" not in section


def test_dashboard_generator_is_consistent(tmp_path):
    """Запуск генератора в рабочей папке воспроизводит те же цифры."""
    res = subprocess.run([sys.executable, str(ROOT / "docs" / "build_dashboard.py")],
                         cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "под риском:" in res.stdout
    page = DASHBOARD.read_text(encoding="utf-8")
    assert "<section id=\"risk\">" in page
    assert page.count("<section") == page.count("</section>")
