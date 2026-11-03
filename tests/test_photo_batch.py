# -*- coding: utf-8 -*-
"""Тесты съёмки с телефона (перспектива по 4 точкам) и пакетной обработки выпуска.

Проверяется: математика гомографии, отказ на плохих опорных точках, выпрямление
фото и равенство результата «фото» и «ровный скан», контроль качества съёмки,
а также пакет: манифест, кеш, изоляция ошибок листа, сводка и отчёты XLSX/Markdown.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pto_agent.cad.drawing import Drawing                                   # noqa: E402
from pto_agent.cad.template import load_template                           # noqa: E402
from pto_agent.cad.vision import analyze_scan, plan_from_scan              # noqa: E402
from pto_agent.cad.vision.batch import (aggregate, load_manifest,          # noqa: E402
                                        manifest_from_dir, process_sheet,
                                        run_batch, write_markdown, write_xlsx)
from pto_agent.cad.vision.imaging import load_rgb                          # noqa: E402
from pto_agent.cad.vision.perspective import (PerspectiveError,            # noqa: E402
                                             PhotoCalibration,
                                             apply_homography,
                                             homography_from_points,
                                             photo_quality, rectify)

SCAN = ROOT / "drawings" / "redline_scan.png"
PHOTO = ROOT / "drawings" / "redline_photo.jpg"
ANCHORS = ROOT / "data" / "demo" / "photo_anchors.json"
PHOTO_TRUTH = ROOT / "data" / "demo" / "photo_truth.json"
TEXTS = ROOT / "data" / "demo" / "redline_texts.json"
DXF = ROOT / "drawings" / "korpus2_plan_v1.dxf"
TEMPLATE = ROOT / "config" / "cad_template_corp_A.yaml"
SUMMARY = ROOT / "out" / "summary.json"
MANIFEST = ROOT / "data" / "demo" / "batch" / "manifest.json"


@pytest.fixture(scope="module")
def drawing():
    return Drawing.open(DXF)


@pytest.fixture(scope="module")
def anchors():
    return json.loads(ANCHORS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def summary():
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def calib(anchors):
    return PhotoCalibration.from_dict(anchors)


# ── гомография ───────────────────────────────────────────────────────────
def test_homography_maps_four_points_exactly():
    src = [[0, 0], [100, 0], [100, 50], [0, 50]]
    dst = [[10, 10], [210, 25], [205, 120], [15, 100]]
    H = homography_from_points(src, dst)
    got = apply_homography(H, src)
    assert np.allclose(got, np.array(dst, dtype=float), atol=1e-6)


def test_homography_identity_for_square():
    src = [[0, 0], [10, 0], [10, 10], [0, 10]]
    H = homography_from_points(src, src)
    assert np.allclose(H / H[2, 2], np.eye(3), atol=1e-9)


def test_homography_requires_four_points():
    with pytest.raises(PerspectiveError):
        homography_from_points([[0, 0], [10, 0], [10, 10]], [[0, 0], [1, 0], [1, 1]])


def test_homography_rejects_degenerate_points():
    pts = [[0, 0], [10, 0], [20, 0], [30, 0]]        # все на одной прямой
    with pytest.raises(PerspectiveError):
        homography_from_points(pts, [[0, 0], [1, 0], [2, 0], [3, 0]])


# ── калибровка фото ──────────────────────────────────────────────────────
def test_photo_calibration_from_anchors(calib, anchors):
    assert calib.rms < 1.0, "на точных углах остаток должен быть близок к нулю"
    assert calib.rectified_calibration().as_dict()["mm_per_px"] == pytest.approx(29.05, abs=0.1)
    # точки фото переводятся в модель без ошибки
    for a in anchors["anchors"]:
        got = calib.to_model_point(*a["px"])
        assert abs(got[0] - a["model"][0]) < 0.5 and abs(got[1] - a["model"][1]) < 0.5


def test_photo_calibration_rejects_non_convex_quad(anchors):
    pts = [a["px"] for a in anchors["anchors"]]
    pts[1], pts[2] = pts[2], pts[1]                  # «бабочка»: точки переставлены
    with pytest.raises(PerspectiveError):
        PhotoCalibration.from_anchors(pts, [a["model"] for a in anchors["anchors"]],
                                      anchors["image_size"])


def test_photo_calibration_needs_four_points(anchors):
    with pytest.raises(PerspectiveError):
        PhotoCalibration.from_anchors([a["px"] for a in anchors["anchors"]][:3],
                                      [a["model"] for a in anchors["anchors"]][:3],
                                      anchors["image_size"])


def test_photo_calibration_dict_roundtrip(calib):
    again = PhotoCalibration.from_dict(calib.as_dict())
    p1 = calib.to_model_point(500, 400)
    p2 = again.to_model_point(500, 400)
    assert np.allclose(p1, p2, atol=0.5)


# ── выпрямление ──────────────────────────────────────────────────────────
def test_rectify_identity_reproduces_image(anchors):
    """Если «фото» — это сам скан, выпрямление обязано вернуть его же."""
    scan = load_rgb(SCAN)
    h, w = scan.shape[:2]
    size = [w, h]
    rect_model = anchors["rect_model"]
    pixels = [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]]
    model = [[rect_model[0], rect_model[3]], [rect_model[2], rect_model[3]],
             [rect_model[2], rect_model[1]], [rect_model[0], rect_model[1]]]
    calib = PhotoCalibration.from_anchors(pixels, model, size, rect_size=size,
                                          rect_model=rect_model)
    out = rectify(scan, calib)
    assert out.shape == scan.shape
    diff = np.abs(out.astype(np.float32) - scan.astype(np.float32)).mean()
    assert diff < 4.0, f"среднее расхождение {diff:.2f}"


def test_rectify_photo_to_sheet_size(calib, anchors):
    photo = load_rgb(PHOTO)
    out = rectify(photo, calib)
    assert list(out.shape[1::-1]) == anchors["rect_size"]


def test_photo_quality_detects_bad_shots():
    good = load_rgb(SCAN)
    assert photo_quality(good)["warnings"] == []

    dark = (good.astype(np.float32) * 0.25).astype(np.uint8)
    assert any("PHOTO_DARK" in w for w in photo_quality(dark)["warnings"])

    blurred = np.asarray(Image.fromarray(good).filter(__import__("PIL.ImageFilter",
                                                                fromlist=["GaussianBlur"])
                                                      .GaussianBlur(6)))
    assert any("PHOTO_BLURRY" in w for w in photo_quality(blurred)["warnings"])


# ── конвейер на фото ─────────────────────────────────────────────────────
def test_photo_matches_flat_scan(drawing, anchors, summary):
    """Фото под углом даёт тот же результат, что ровный скан."""
    tpl = load_template(TEMPLATE)
    flat = analyze_scan(SCAN, drawing=drawing, engine="offline",
                        texts_path=TEXTS)
    photo = analyze_scan(PHOTO, drawing=drawing, engine="offline", photo=anchors,
                         texts_path=TEXTS)
    assert len(photo["marks"]) == len(flat["marks"]) == 5
    p_flat = plan_from_scan(flat, drawing, tpl, summary)["summary"]
    p_photo = plan_from_scan(photo, drawing, tpl, summary)["summary"]
    assert p_photo["operations"] == p_flat["operations"] == 4
    assert p_photo["conflicts"] == p_flat["conflicts"] == 2
    assert p_photo["needs_human_decision"] == p_flat["needs_human_decision"] == 3


def test_photo_run_reports_geometry_and_quality(drawing, anchors):
    run = analyze_scan(PHOTO, drawing=drawing, engine="offline", photo=anchors)
    info = run["photo"]
    assert info["photo_size"] == [2600, 1950]
    assert info["rectified_size"] == [3000, 2119]
    assert info["illumination_normalized"] is True
    assert info["quality"]["megapixels"] == pytest.approx(5.07, abs=0.1)
    assert run["calibration"]["method"] == "perspective_rectified"


def test_photo_marks_carry_source_bbox_on_photo(drawing, anchors):
    run = analyze_scan(PHOTO, drawing=drawing, engine="offline", photo=anchors)
    for m in run["marks"]:
        assert m["source"]["type"] == "photo"
        x0, y0, x1, y1 = m["bbox_photo"]
        assert 0 <= x0 < x1 <= 2600 and 0 <= y0 < y1 <= 1950


def test_photo_truth_alignment(drawing, anchors):
    """bbox из фото-прогона совпадает с эталоном скана (та же сетка после выпрямления)."""
    run = analyze_scan(PHOTO, drawing=drawing, engine="mock", photo=anchors,
                       truth_path=PHOTO_TRUTH)
    truth = json.loads(PHOTO_TRUTH.read_text(encoding="utf-8"))["marks"]
    assert len(run["marks"]) == len(truth) == 5
    from pto_agent.cad.vision.imaging import iou
    for t in truth:
        best = max(iou(m["bbox_px"], t["bbox_px"]) for m in run["marks"])
        assert best > 0.95


# ── манифест и пакет ─────────────────────────────────────────────────────
def test_manifest_loads_release():
    sheets = load_manifest(MANIFEST)
    assert len(sheets) == 40
    assert len({s["sheet_id"] for s in sheets}) == 40
    assert all(Path(s["scan"]).exists() for s in sheets)


def test_manifest_from_directory(tmp_path):
    for name in ("a.png", "b.png"):
        Image.new("RGB", (40, 30), "white").save(tmp_path / name)
    sheets = manifest_from_dir(tmp_path, default_drawing=str(DXF))
    assert [s["sheet_id"] for s in sheets] == ["a", "b"]
    assert all(s["drawing"] == str(DXF) for s in sheets)


def test_manifest_rejects_bad_entry(tmp_path):
    bad = tmp_path / "manifest.json"
    bad.write_text(json.dumps({"sheets": [{"sheet_id": "x"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(bad)


def test_process_sheet_and_cache(tmp_path):
    sheets = load_manifest(MANIFEST)[:1]
    rec = process_sheet(sheets[0], engine="mock", template_path=str(TEMPLATE),
                        summary=json.loads(SUMMARY.read_text(encoding="utf-8")),
                        out_dir=tmp_path)
    assert rec["ok"] is True
    assert rec["marks"] >= 4
    assert rec["calibration"]["confirmed"] is False      # рамка без подтверждения

    again = process_sheet(sheets[0], engine="mock", template_path=str(TEMPLATE),
                          out_dir=tmp_path)
    assert again.get("from_cache") is True


def test_failed_sheet_does_not_break_release(tmp_path):
    sheets = load_manifest(MANIFEST)[:2]
    broken = dict(sheets[1], sheet_id="broken", drawing=str(tmp_path / "нет-такого.dxf"))
    summary = run_batch([sheets[0], broken], engine="mock", out_dir=tmp_path,
                        template_path=str(TEMPLATE),
                        summary=json.loads(SUMMARY.read_text(encoding="utf-8")))
    assert summary["totals"]["sheets"] == 2
    assert summary["totals"]["ok"] == 1
    assert summary["totals"]["failed"] == 1
    assert summary["failed_sheets"][0]["sheet_id"] == "broken"
    assert "No such file" in summary["failed_sheets"][0]["error"] or \
           "не найден" in summary["failed_sheets"][0]["error"].lower()


def test_offline_batch_forces_single_thread(tmp_path):
    sheets = load_manifest(MANIFEST)[:1]
    summary = run_batch(sheets, engine="offline", workers=4, out_dir=tmp_path,
                        template_path=str(TEMPLATE))
    assert summary["totals"]["ok"] == 1
    assert any("потоки отключены" in n for n in summary["notes"])


def test_aggregate_counts_conflicts_and_decisions():
    records = [
        {"sheet_id": "s1", "ok": True, "marks": 5, "actionable": 4, "needs_human": 1,
         "rejected": 0, "operations": 4, "calibration": {"confirmed": False},
         "mark_list": [{"mark_id": "m1", "type": "circle"}],
         "conflicts": [{"type": "CONFLICT_QUANTITY", "code": "ФЕР 1", "mark_id": "m2"}],
         "decisions": [{"mark_id": "m3", "reason": "нет текста"}]},
        {"sheet_id": "s2", "ok": True, "marks": 3, "actionable": 3, "needs_human": 0,
         "rejected": 1, "operations": 2, "calibration": {"confirmed": True},
         "mark_list": [{"mark_id": "m1", "type": "arrow"}],
         "conflicts": [{"type": "CONFLICT_APPROVAL", "code": "ФЕР 2", "mark_id": "m1"}],
         "decisions": []},
        {"sheet_id": "s3", "ok": False, "error": "boom"},
    ]
    summary = aggregate(records, engine="mock", started=0.0)
    t = summary["totals"]
    assert (t["sheets"], t["ok"], t["failed"]) == (3, 2, 1)
    assert t["marks"] == 8 and t["operations"] == 6
    assert t["conflicts"] == 2 and t["decisions"] == 1
    assert t["unconfirmed_calibration"] == 1
    assert summary["conflicts_by_type"] == {"CONFLICT_QUANTITY": 1, "CONFLICT_APPROVAL": 1}
    assert summary["marks_by_type"] == {"circle": 1, "arrow": 1}
    assert summary["failed_sheets"][0]["error"] == "boom"


def test_reports_are_written(tmp_path):
    sheets = load_manifest(MANIFEST)[:2]
    summary = run_batch(sheets, engine="mock", out_dir=tmp_path,
                        template_path=str(TEMPLATE),
                        summary=json.loads(SUMMARY.read_text(encoding="utf-8")))
    md = write_markdown(summary, tmp_path / "summary.md")
    xlsx = write_xlsx(summary, tmp_path / "summary.xlsx")
    text = md.read_text(encoding="utf-8")
    assert "Сводка по выпуску" in text and "| Лист |" in text

    from openpyxl import load_workbook
    wb = load_workbook(xlsx)
    assert {"Итог", "Листы", "Конфликты", "Вопросы человеку"} <= set(wb.sheetnames)
    assert wb["Листы"].max_row == 3          # шапка + 2 листа


def test_cli_scan_batch(tmp_path):
    sheets = load_manifest(MANIFEST)[:2]
    manifest = tmp_path / "mini.json"
    manifest.write_text(json.dumps({"sheets": sheets}, ensure_ascii=False),
                        encoding="utf-8")
    out = tmp_path / "out"
    res = subprocess.run([sys.executable, "-m", "pto_agent", "cad", "scan-batch",
                          "--manifest", str(manifest), "--engine", "mock",
                          "--out", str(out), "--summary", str(SUMMARY)],
                         cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "Итог: листов 2" in res.stdout
    assert (out / "batch_summary.xlsx").exists()
    assert (out / "batch_summary.md").exists()
