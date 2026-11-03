# -*- coding: utf-8 -*-
"""Оркестрация распознавания скана: калибровка → VL-модель (или офлайн CV) → проверки.

Ключевое: ответ модели не принимается на веру. Каждый bbox проверяется по самому
скану — есть ли под ним чернила (анти-галлюцинационная проверка), попадает ли он
в лист, есть ли уверенность. Всё, что не прошло, уходит в rejected с причиной.
"""
import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .calibrate import Calibration, CalibrationError
from .detect_offline import detect_marks_offline
from .imaging import colored_ink_mask, ink_fraction, iou, load_rgb
from .perspective import (PhotoCalibration, normalize_illumination, photo_quality,
                          rectify)
from .mock import MockVisionServer
from .prompt import PROMPT_ID, PROMPT_VERSION, SYSTEM, user_message
from .providers import (LLMCallLog, OpenAIVisionProvider, ProviderUnavailable,
                        VisionRequest, provider_info, resolve_vision_provider)
from .schema import validate_payload

MIN_INK_FRACTION = 0.004      # под bbox должно быть хоть сколько-то чернил
DEFAULT_TILE_PX = 1600
TILE_OVERLAP = 0.10


@dataclass
class VisionRun:
    image: str
    image_size: list
    engine_requested: str
    engine_used: str
    calibration: dict
    marks: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    tiles: int = 0
    provider: dict = field(default_factory=dict)
    llm_calls: int = 0
    cost_rub: float = 0.0
    warnings: list = field(default_factory=list)
    texts_source: str | None = None
    photo: dict | None = None

    def as_dict(self) -> dict:
        actionable = [m for m in self.marks
                      if m["confidence"] >= 0.7 and m["text"].strip()]
        return {
            "image": self.image,
            "image_size": self.image_size,
            "engine_requested": self.engine_requested,
            "engine_used": self.engine_used,
            "provider": self.provider,
            "calibration": self.calibration,
            "tiles": self.tiles,
            "marks": self.marks,
            "rejected": self.rejected,
            "texts_source": self.texts_source,
            "photo": self.photo,
            "stats": {
                "marks": len(self.marks),
                "actionable": len(actionable),
                "needs_human": len(self.marks) - len(actionable),
                "rejected": len(self.rejected),
                "llm_calls": self.llm_calls,
                "cost_rub": round(self.cost_rub, 4),
            },
            "warnings": self.warnings,
        }


class _MockProvider(OpenAIVisionProvider):
    """Провайдер-заглушка: тот же HTTP-путь, вместо модели — mock-сервер."""

    def __init__(self, server: MockVisionServer, tile_meta: dict | None = None):
        super().__init__({"id": "mock-vl", "model": "mock-vl-redline",
                          "base_url": server.base_url}, timeout=30, max_retries=0)
        self._tile_meta = tile_meta

    def analyze(self, request: VisionRequest, tile_meta: dict | None = None):
        import urllib.request
        b64 = __import__("base64").b64encode(request.image_bytes).decode("ascii")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": [
                    {"type": "text", "text": request.user},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{request.mime};base64,{b64}"}},
                ]},
            ],
            "tile": tile_meta or self._tile_meta or {},
        }
        req = urllib.request.Request(self.base_url + "/v1/chat/completions",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        import time
        started = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        from .providers import VisionResponse
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        return VisionResponse(text=text, json=parsed, model="mock-vl-redline",
                              provider="mock-vl",
                              latency_ms=int((time.time() - started) * 1000),
                              tokens_in=int(usage.get("prompt_tokens") or 0),
                              tokens_out=int(usage.get("completion_tokens") or 0),
                              cost_rub=0.0, error=None if parsed else "не JSON")


def analyze_scan(scan_path: str | Path, *, drawing=None, model_extents: dict | None = None,
                 engine: str = "auto", calibration: dict | Calibration | None = None,
                 texts_path: str | Path | None = None, truth_path: str | Path | None = None,
                 photo: dict | PhotoCalibration | None = None,
                 rectified_path: str | Path | None = None,
                 tile_px: int = DEFAULT_TILE_PX, provider_id: str | None = None,
                 log_path: str | Path = "out/cad/llm_calls.jsonl",
                 min_ink: float = MIN_INK_FRACTION, confirm_calibration: bool = False) -> dict:
    if engine not in ("auto", "vlm", "offline", "mock"):
        raise ValueError(f"неизвестный движок: {engine}")

    scan_path = Path(scan_path)
    rgb = load_rgb(scan_path)

    # ── фото с телефона: сначала выпрямляем, потом работаем как со сканом ──
    photo_calib, photo_info = None, None
    if photo is not None:
        photo_calib = (photo if isinstance(photo, PhotoCalibration)
                       else PhotoCalibration.from_dict(photo))
        quality = photo_quality(rgb)
        rgb = rectify(rgb, photo_calib)
        rgb = normalize_illumination(rgb)
        if rectified_path:
            Path(rectified_path).parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(rgb).save(rectified_path)
        photo_info = {
            "photo_size": photo_calib.image_size,
            "rectified_size": list(rgb.shape[1::-1]),
            "rect_model": photo_calib.rect_model,
            "rms_mm": photo_calib.rms,
            "quality": quality,
            "anchors": photo_calib.anchors,
            "method": photo_calib.method,
            "illumination_normalized": True,
            "confirmed": photo_calib.confirmed,
            "rectified_path": str(rectified_path) if rectified_path else None,
        }
        calibration = photo_calib.rectified_calibration()

    height, width = rgb.shape[:2]
    ink_mask = colored_ink_mask(rgb)

    # ── калибровка ────────────────────────────────────────────────────────
    if isinstance(calibration, Calibration):
        calib = calibration
    elif isinstance(calibration, dict):
        calib = Calibration(**{k: v for k, v in calibration.items()
                               if k in Calibration.__dataclass_fields__})
    else:
        extents = model_extents
        if extents is None and drawing is not None:
            extents = drawing.inventory().get("extents")
        if not extents:
            raise CalibrationError(
                "нет габаритов чертежа для калибровки: передайте drawing или model_extents")
        calib = Calibration.auto(scan_path, extents, confirm=confirm_calibration)

    run = VisionRun(image=str(scan_path), image_size=[width, height],
                    engine_requested=engine, engine_used="",
                    calibration=calib.as_dict())
    if not calib.confirmed:
        run.warnings.append("CALIBRATION_NOT_CONFIRMED")
    run.warnings.extend(calib.notes)
    if photo_info:
        run.photo = photo_info
        run.warnings.extend(photo_info["quality"]["warnings"])
        if photo_info["rms_mm"] > 20:
            run.warnings.append(
                f"PERSPECTIVE_RMS_HIGH: {photo_info['rms_mm']:.1f} мм — опорные точки "
                "поставлены неточно, проверьте углы рамки")

    log = LLMCallLog(log_path)
    marks, rejected = [], []

    # ── выбор движка ──────────────────────────────────────────────────────
    provider = None
    mock_server = None
    settings = {}
    if engine in ("vlm", "auto", "mock"):
        if engine == "mock":
            truth = truth_path or os.getenv("PTO_VL_MOCK_TRUTH")
            if not truth:
                raise ValueError("для --engine mock нужен эталонный словарь (--truth)")
            mock_server = MockVisionServer(truth).start()
            provider = _MockProvider(mock_server)
            settings = {"id": "mock-vl", "model": "mock-vl-redline",
                        "base_url": mock_server.base_url, "kind": "mock",
                        "residency": "local_test"}
            run.warnings.append(
                "MOCK_PROVIDER: ответы формирует локальная заглушка (не модель). "
                "Боевой режим: vLLM + Qwen2.5-VL, PTO_VL_BASE_URL")
        else:
            try:
                settings = resolve_vision_provider(provider_id)
                provider = OpenAIVisionProvider(settings)
            except ProviderUnavailable as exc:
                if engine == "vlm":
                    if mock_server:
                        mock_server.stop()
                    raise
                run.warnings.append(f"VLM_UNAVAILABLE: {exc}")
                provider = None

    try:
        if provider is not None:
            run.engine_used = ("mock_vlm" if engine == "mock" else "vlm")
            run.provider = provider_info(settings)
            tiles = _make_tiles(width, height, tile_px)
            run.tiles = len(tiles)
            for i, (ox, oy, tw, th) in enumerate(tiles, 1):
                crop = rgb[oy:oy + th, ox:ox + tw]
                buf = io.BytesIO()
                Image.fromarray(crop).save(buf, format="PNG")
                image_bytes = buf.getvalue()
                request = VisionRequest(image_bytes=image_bytes, system=SYSTEM,
                                        user=user_message(i, len(tiles), tw, th),
                                        prompt_id=PROMPT_ID, prompt_version=PROMPT_VERSION)
                try:
                    meta = {"index": i, "origin": [ox, oy], "size": [tw, th]}
                    response = (provider.analyze(request, meta) if isinstance(provider, _MockProvider)
                                else provider.analyze(request))
                except Exception as exc:  # noqa: BLE001
                    run.warnings.append(f"TILE_{i}_FAILED: {exc}")
                    log.append(provider=settings.get("id", "vlm"), model=settings.get("model", ""),
                               prompt_id=PROMPT_ID, prompt_version=PROMPT_VERSION,
                               image_bytes=image_bytes, response=None, validated=False,
                               error=str(exc))
                    continue
                run.llm_calls += 1
                run.cost_rub += response.cost_rub or 0.0

                accepted, tile_rejected = validate_payload(response.json or {}, tile_index=i)
                for r in tile_rejected:
                    r["tile"] = i
                rejected.extend(tile_rejected)

                crop_ink = ink_mask[oy:oy + th, ox:ox + tw]
                for a in accepted:
                    tpx = _norm_to_px(a["bbox"], tw, th)
                    frac = ink_fraction(crop_ink, tpx)
                    if frac < min_ink:
                        rejected.append({
                            "reason": "NO_INK_IN_BBOX", "ref": a["ref"], "tile": i,
                            "ink_fraction": round(frac, 5),
                            "bbox": a["bbox"],
                            "message": "модель указала область без чернил — пометка отклонена"})
                        continue
                    gpx = [tpx[0] + ox, tpx[1] + oy, tpx[2] + ox, tpx[3] + oy]
                    marks.append(_make_mark(a, gpx, width, height, calib,
                                            engine=run.engine_used,
                                            model=settings.get("model", ""),
                                            ink=frac, tile=i))

                log.append(provider=settings.get("id", "vlm"), model=settings.get("model", ""),
                           prompt_id=PROMPT_ID, prompt_version=PROMPT_VERSION,
                           image_bytes=image_bytes, response=response,
                           validated=response.json is not None,
                           marks_accepted=len(accepted),
                           marks_rejected=len(tile_rejected) + sum(
                               1 for r in rejected if r.get("reason") == "NO_INK_IN_BBOX"
                               and r.get("tile") == i),
                           error=response.error, extra={"tile": i})
            marks = _dedupe(marks)
        else:
            run.engine_used = "offline_cv"
            detected = detect_marks_offline(scan_path, image=rgb, drawing=drawing,
                                           calibration=calib)
            run.tiles = 1
            run.warnings.append("DETECTOR:" + detected.get("detection_method", ""))
            for m in detected["marks"]:
                marks.append({
                    "mark_id": m["mark_id"], "type": m["type"],
                    "bbox_px": m["bbox_px"],
                    "bbox_norm": m["bbox_norm"],
                    "coords_model": calib.to_model_bbox(m["bbox_px"]),
                    "text": m["text"], "confidence": m["confidence"],
                    "engine": "offline_cv", "model": "", "tile": None,
                    "ink_fraction": m["ink_fraction"],
                    "source": {"type": "photo" if photo_calib else "scan",
                               "ref": f"{scan_path.name}#{m['mark_id']}",
                               "detector": m.get("detector")},
                    "warnings": list(m.get("warnings") or []),
                    "requires_confirmation": True,
                })
    finally:
        if mock_server:
            mock_server.stop()

    # ── тексты, введённые человеком ───────────────────────────────────────
    if texts_path:
        run.texts_source = str(texts_path)
        _apply_human_texts(marks, texts_path, width, height, calib, run)

    if rejected:
        counters: dict = {}
        for r in rejected:
            counters[r.get("reason", "?")] = counters.get(r.get("reason", "?"), 0) + 1
        summary = ", ".join(f"{k}×{v}" for k, v in sorted(counters.items()))
        run.warnings.append(f"REJECTED_BY_CHECKS: {len(rejected)} ({summary})")

    if photo_calib is not None:
        m_rect = photo_calib.rectified_to_photo_matrix()
        for m in marks:
            m["bbox_photo"] = _rect_bbox_to_photo(m["bbox_px"], m_rect)

    marks.sort(key=lambda m: (m["bbox_px"][1], m["bbox_px"][0]))
    for i, m in enumerate(marks, 1):
        m["mark_id"] = m.get("mark_id") or f"m{i}"
        m["requires_confirmation"] = m["confidence"] < 0.7 or not m["text"].strip()
    run.marks = marks
    run.rejected = rejected
    return run.as_dict()


# ── вспомогательное ──────────────────────────────────────────────────────
def _make_tiles(width: int, height: int, tile_px: int) -> list:
    if width <= tile_px and height <= tile_px:
        return [(0, 0, width, height)]
    step = int(tile_px * (1 - TILE_OVERLAP))
    tiles = []
    y = 0
    while y < height:
        x = 0
        while x < width:
            tiles.append((x, y, min(tile_px, width - x), min(tile_px, height - y)))
            if x + tile_px >= width:
                break
            x += step
        if y + tile_px >= height:
            break
        y += step
    return tiles


def _norm_to_px(bbox_norm, w, h) -> list:
    return [bbox_norm[0] * w, bbox_norm[1] * h, bbox_norm[2] * w, bbox_norm[3] * h]


def _make_mark(a: dict, bbox_px, width, height, calib: Calibration, *, engine, model,
               ink, tile) -> dict:
    return {
        "mark_id": None,
        "type": a["type"],
        "bbox_px": [round(v, 1) for v in bbox_px],
        "bbox_norm": [round(bbox_px[0] / width, 5), round(bbox_px[1] / height, 5),
                      round(bbox_px[2] / width, 5), round(bbox_px[3] / height, 5)],
        "coords_model": calib.to_model_bbox(bbox_px),
        "text": a["text"],
        "confidence": a["confidence"],
        "engine": engine,
        "model": model,
        "tile": tile,
        "ink_fraction": round(ink, 4),
        "source": {"type": "scan", "ref": a.get("ref"), "tile": tile},
        "warnings": list(a.get("warnings") or []),
        "requires_confirmation": a["confidence"] < 0.7 or not a["text"].strip(),
    }


def _merge_duplicates(a: dict, b: dict) -> dict:
    """Склейка двух наблюдений одной пометки: объединение bbox, лучший текст."""
    box = [min(a["bbox_px"][0], b["bbox_px"][0]), min(a["bbox_px"][1], b["bbox_px"][1]),
           max(a["bbox_px"][2], b["bbox_px"][2]), max(a["bbox_px"][3], b["bbox_px"][3])]
    text = a.get("text") or ""
    if len(b.get("text") or "") > len(text):
        text = b["text"]
        a = {**a, "engine": b.get("engine", a.get("engine"))}
    return {**a, "bbox_px": box, "text": text,
            "confidence": max(a.get("confidence", 0), b.get("confidence", 0)),
            "coords_model": _box_to_model(box, a) or a.get("coords_model")}


def _box_to_model(box_px, mark: dict):
    """Пересчёт объединённого bbox в координаты чертежа (по масштабу пометки)."""
    old = mark.get("bbox_px") or box_px
    model = mark.get("coords_model")
    if not model:
        return None
    w_px = max(old[2] - old[0], 1e-9)
    h_px = max(old[3] - old[1], 1e-9)
    sx = (model[2] - model[0]) / w_px
    sy = (model[3] - model[1]) / h_px
    x0 = model[0] + (box_px[0] - old[0]) * sx
    y0 = model[1] + (box_px[1] - old[1]) * sy
    x1 = model[0] + (box_px[2] - old[0]) * sx
    y1 = model[1] + (box_px[3] - old[1]) * sy
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]


def _dedupe(marks: list) -> list:
    """Пометки, попавшие в перекрытие фрагментов, не должны дублироваться.

    Два критерия: сильное перекрытие bbox либо одинаковый текст при близких центрах
    (пометка, разрезанная границей фрагмента, приходит из двух фрагментов обрезанной).
    """
    kept = []
    for m in sorted(marks, key=lambda x: -x["ink_fraction"]):
        twin = None
        for i, k in enumerate(kept):
            # сливаем только однотипные наблюдения: обводка и подчёркивание на одном
            # месте — это две разные пометки инженера, терять одну нельзя
            if m.get("type") != k.get("type"):
                continue
            if iou(m["bbox_px"], k["bbox_px"]) > 0.5:
                twin = i
                break
            same_text = (m.get("text") or "").strip() and m["text"].strip() == k["text"].strip()
            if same_text:
                mx = (m["bbox_px"][0] + m["bbox_px"][2]) / 2
                my = (m["bbox_px"][1] + m["bbox_px"][3]) / 2
                kx = (k["bbox_px"][0] + k["bbox_px"][2]) / 2
                ky = (k["bbox_px"][1] + k["bbox_px"][3]) / 2
                size = max(m["bbox_px"][2] - m["bbox_px"][0],
                           m["bbox_px"][3] - m["bbox_px"][1], 1)
                if ((mx - kx) ** 2 + (my - ky) ** 2) ** 0.5 < size:
                    twin = i
                    break
        if twin is None:
            kept.append(m)
        else:
            kept[twin] = _merge_duplicates(kept[twin], m)
    return kept


def _rect_bbox_to_photo(bbox_px, matrix) -> list:
    """bbox выпрямленного растра → bbox на фото (по четырём углам)."""
    x0, y0, x1, y1 = bbox_px
    pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float)
    homo = matrix @ np.vstack([pts.T, np.ones(4)])
    w = np.where(np.abs(homo[2]) < 1e-12, 1e-12, homo[2])
    xs, ys = homo[0] / w, homo[1] / w
    return [round(float(xs.min()), 1), round(float(ys.min()), 1),
            round(float(xs.max()), 1), round(float(ys.max()), 1)]


def _containment(inner: list, outer: list) -> float:
    """Доля площади inner, попавшая в outer: пометка движка шире обводки инженера."""
    ix0, iy0 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix1, iy1 = min(inner[2], outer[2]), min(inner[3], outer[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    area = max((inner[2] - inner[0]) * (inner[3] - inner[1]), 1e-9)
    return ((ix1 - ix0) * (iy1 - iy0)) / area


def _apply_human_texts(marks: list, texts_path, width, height, calib, run: VisionRun) -> None:
    data = json.loads(Path(texts_path).read_text(encoding="utf-8"))
    entries = data.get("marks") if isinstance(data, dict) else data
    unmatched = []
    for entry in entries or []:
        # привязка по геометрии: id пометок у разных движков не совпадают
        target = None
        if entry.get("bbox_px"):
            best, best_score = None, 0.0
            for m in marks:
                score = max(iou(m["bbox_px"], entry["bbox_px"]),
                            _containment(entry["bbox_px"], m["bbox_px"]))
                if score > best_score:
                    best, best_score = m, score
            if best_score >= 0.35:
                target = best
        if target is None and entry.get("mark_id") and not entry.get("bbox_px"):
            target = next((m for m in marks if m["mark_id"] == entry["mark_id"]), None)
        if target is None:
            unmatched.append(entry)
            continue
        if entry.get("text"):
            target["text"] = str(entry["text"]).strip()
            target["engine"] = "human"
            target["confidence"] = max(float(target["confidence"]), 0.95)
            target["warnings"] = [w for w in target["warnings"] if w != "TEXT_NOT_RECOGNIZED"]
            target["source"] = {**target.get("source", {}), "text_by": "human"}
        if entry.get("type"):
            target["type"] = entry["type"]
        target["coords_model"] = calib.to_model_bbox(target["bbox_px"])
    if unmatched:
        run.warnings.append(f"UNMATCHED_HUMAN_TEXTS: {len(unmatched)}")
