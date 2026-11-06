# -*- coding: utf-8 -*-
"""Документы качества на материалы: реестр, привязка к партии, срок действия, заверение копий.

Материал без документа качества — это материал, про который неизвестно, что это вообще такое.
Паспорт, сертификат, декларация, протокол испытаний отвечают на три вопроса: **тот ли материал**
(наименование, марка, фракция, типоразмер), **та ли партия** (номер партии в документе и в
накладной), **действует ли документ на дату применения**. Поэтому модуль проверяет именно эти
три вещи, а не «есть ли слово сертификат в реестре»:

* **документ без реквизитов не опознаётся** — нет типа, номера, даты или изготовителя, значит
  приложить к комплекту нечего (`DOC_INCOMPLETE`, ошибка);
* **документ на другой материал или на другую партию — не документ на этот материал**
  (`DOC_MATERIAL_MISMATCH`, `DOC_BATCH_UNUSED`, ошибки/предупреждения). Совпадение проверяется
  по нормализованному наименованию, включая марку и фракцию: «Керамзит фр. 10–20» не закрывает
  партию «Керамзит фр. 5–10»;
* **срок действия сверяется с датой применения, а не с сегодняшним днём** (`DOC_EXPIRED`):
  сертификат, истёкший до укладки, на дату укладки не действовал;
* **тип документа, которого нет в рабочем перечне, — не документ качества** (`DOC_TYPE_UNKNOWN`).
  Товарный чек и счёт на оплату качество материала не подтверждают;
* **копия без отметки «копия верна» в комплект не идёт** (`DOC_NOT_ATTESTED`): кто и когда
  сверил копию с оригиналом — часть документа;
* **состав требуемых документов агент не выдумывает.** Требование берётся из файла требований
  (проектная документация, договор, ТУ изготовителя). Нет файла — материал показывается как
  «требование не заявлено», а не как «всё в порядке» (`REQUIREMENT_UNDECLARED`);
* **стык с журналом входного контроля** (круг 7): допуск по документу, которого нет в реестре —
  ошибка `VERIFICATION_WITHOUT_DOC`; документ в реестре без входного контроля — предупреждение
  `DOC_WITHOUT_VERIFICATION`.

Комплект закрывает человек: `sign()` требует ФИО и роль и отказывает при ошибках; правка после
подписи аннулирует подпись (отпечаток содержания, общий с ТК, АТН, чек-листами и журналами).
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .fingerprint import check_signature, content_fingerprint

SCHEMA = "pto-agent/quality-docs@1"
TYPES_SCHEMA = "pto-agent/quality-doc-types@1"

DEFAULT_TYPES = Path(__file__).resolve().parents[1] / "data" / "reference" / "quality_docs.json"

STATUS_DRAFT = "черновик"
STATUS_SIGNED = "подписан"
STATUS_CHANGED = "изменён после подписи — подпись недействительна"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

# состояние комплекта по материалу
COVER_FULL = "комплект есть"
COVER_INVALID = "документ недействителен"
COVER_PARTIAL = "неполный комплект"
COVER_MISSING = "документов нет"
COVER_UNDECLARED = "требование не заявлено"

CHECK_DOC_INCOMPLETE = "DOC_INCOMPLETE"
CHECK_TYPE_UNKNOWN = "DOC_TYPE_UNKNOWN"
CHECK_MATERIAL_MISMATCH = "DOC_MATERIAL_MISMATCH"
CHECK_BATCH_MISMATCH = "DOC_BATCH_MISMATCH"
CHECK_BATCH_UNUSED = "DOC_BATCH_UNUSED"
CHECK_EXPIRED = "DOC_EXPIRED"
CHECK_NO_VALIDITY = "DOC_NO_VALIDITY"
CHECK_BASIS_MISSING = "DOC_BASIS_MISSING"
CHECK_NO_FILE = "DOC_NO_SOURCE_FILE"
CHECK_NOT_ATTESTED = "DOC_NOT_ATTESTED"
CHECK_DUPLICATE = "DOC_DUPLICATE"
CHECK_MATERIAL_NO_DOC = "MATERIAL_WITHOUT_DOC"
CHECK_REQ_UNDECLARED = "REQUIREMENT_UNDECLARED"
CHECK_REQ_NO_BASIS = "REQUIREMENT_WITHOUT_BASIS"
CHECK_VERIF_NO_DOC = "VERIFICATION_WITHOUT_DOC"
CHECK_DOC_NO_VERIF = "DOC_WITHOUT_VERIFICATION"
CHECK_EMPTY = "REGISTRY_EMPTY"

_CLEAN_RE = re.compile(r"[^0-9a-zа-яё]+")


def _clean(text) -> str:
    """Нормализация наименования: регистр, ё, пунктуация, пробелы."""
    value = str(text or "").lower().replace("ё", "е")
    value = _CLEAN_RE.sub(" ", value)
    return " ".join(value.split())


def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y.%m.%d", "%d.%m.%y"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def load_types(path: str | Path | None = None) -> dict:
    """Рабочий перечень типов документов качества (не нормативный первоисточник)."""
    path = Path(path) if path else DEFAULT_TYPES
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_kind(kind, types: dict | None = None) -> tuple[str | None, str | None]:
    """Вернуть (канонический тип, как было) или (None, причина отказа)."""
    types = types or load_types()
    raw = str(kind or "").strip()
    if not raw:
        return None, "тип документа не указан"
    canon = {_clean(k): k for k in (types.get("types") or {})}
    for alias, target in (types.get("aliases") or {}).items():
        canon[_clean(alias)] = target
    key = _clean(raw)
    if key in canon:
        return canon[key], None
    # «сертификат соответствия № …», «паспорт качества партии» — ищем тип внутри строки
    for candidate, target in sorted(canon.items(), key=lambda item: -len(item[0])):
        if candidate and candidate in key:
            return target, None
    return None, f"«{raw}» — не документ качества из рабочего перечня"


def load_json(path: str | Path, key: str) -> tuple[list[dict], list[str]]:
    """Прочитать список из JSON-файла: `{key: [...]}` или просто список."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], [f"{path.name}: не читается ({exc.__class__.__name__})"]
    items = data if isinstance(data, list) else data.get(key)
    if not isinstance(items, list):
        return [], [f"{path.name}: нет списка «{key}»"]
    problems = [] if isinstance(data, list) else list(data.get(f"{key}_problems") or [])
    return items, problems


# ─────────────────────────── сборка реестра ───────────────────────────
def build_registry(documents: list[dict], *, object_name: str = "", number: str = "",
                   requirements: list[dict] | None = None, usage: list[dict] | None = None,
                   verification: list[dict] | None = None, types: dict | None = None,
                   today: date | None = None) -> dict:
    """Собрать реестр документов качества и проверить его против применения материалов."""
    today = today or date.today()
    typebook = types or load_types()
    type_rules = typebook.get("types") or {}
    checks: list[dict] = []
    accepted: list[dict] = []
    rejected: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    # применение материалов: (материал, партия) → когда применяли
    use_map: dict[tuple[str, str], dict] = {}
    for row in usage or []:
        material = _clean(row.get("material") or row.get("code"))
        batch = _clean(row.get("batch"))
        if not material:
            continue
        slot = use_map.setdefault((material, batch), {"material": str(row.get("material") or ""),
                                                      "batch": str(row.get("batch") or "") or None,
                                                      "unit": row.get("unit"),
                                                      "qty": 0.0, "from": None, "to": None})
        try:
            slot["qty"] += float(row.get("qty") or 0)
        except (TypeError, ValueError):
            pass
        for key, value in (("from", row.get("applied_from") or row.get("date")),
                           ("to", row.get("applied_to") or row.get("date"))):
            day = _parse_date(value)
            if day and (slot[key] is None or (key == "from" and day < slot[key])
                        or (key == "to" and day > slot[key])):
                slot[key] = day

    # требования к составу документов по материалам
    req_map: dict[str, dict] = {}
    for row in requirements or []:
        material = _clean(row.get("material"))
        if not material:
            continue
        kinds = []
        for kind in row.get("required") or []:
            canon, why = normalize_kind(kind, typebook)
            if canon:
                kinds.append(canon)
            else:
                add(CHECK_REQ_NO_BASIS, LEVEL_WARNING,
                    f"требование «{row.get('material')}»: {why} — требование не учтено",
                    material=row.get("material"))
        req_map[material] = {"material": str(row.get("material") or ""), "required": kinds,
                             "basis": str(row.get("basis") or "").strip() or None,
                             "basis_source": row.get("basis_source")}
        if not req_map[material]["basis"]:
            add(CHECK_REQ_NO_BASIS, LEVEL_WARNING,
                f"{row.get('material')}: требование к документам без основания "
                f"(проект, договор, ТУ, ГОСТ) — состав считать справочным", material=row.get("material"))

    seen: dict[tuple, int] = {}
    for index, raw in enumerate(documents or [], start=1):
        kind_raw = raw.get("kind") or raw.get("type")
        kind, why = normalize_kind(kind_raw, typebook)
        doc_no = str(raw.get("number") or "").strip()
        issue = _parse_date(raw.get("issue_date") or raw.get("date"))
        until = _parse_date(raw.get("valid_until"))
        material = str(raw.get("material") or raw.get("material_name") or "").strip()
        batch = str(raw.get("batch") or "").strip()
        manufacturer = str(raw.get("manufacturer") or raw.get("issuer") or "").strip()
        standard = str(raw.get("standard") or raw.get("basis") or "").strip()
        file_ref = str(raw.get("file") or raw.get("scan") or "").strip()
        attested = raw.get("attested_by") or {}
        attested_by = str((attested or {}).get("by") or "").strip()
        attested_role = str((attested or {}).get("role") or "").strip()

        doc = {"no": index, "kind": kind, "kind_raw": str(kind_raw or "").strip() or None,
               "number": doc_no or None, "issue_date": issue.isoformat() if issue else None,
               "valid_until": until.isoformat() if until else None,
               "material": material or None, "batch": batch or None,
               "manufacturer": manufacturer or None, "standard": standard or None,
               "pages": raw.get("pages"), "file": file_ref or None,
               "attested_by": ({"by": attested_by, "role": attested_role,
                                "at": (attested or {}).get("at")} if attested_by else None),
               "note": str(raw.get("note") or "").strip() or None}

        missing = [name for name, ok in (("тип документа", bool(kind)),
                                        ("номер", bool(doc_no)),
                                        ("дату выдачи", bool(issue)),
                                        ("материал", bool(material))) if not ok]
        if missing:
            doc["why"] = f"не указано: {', '.join(missing)}"
            if why:
                doc["why"] += f" ({why})"
            rejected.append(doc)
            add(CHECK_DOC_INCOMPLETE, LEVEL_ERROR,
                f"документ №{index} ({material or 'материал не указан'}): {doc['why']} — "
                f"к комплекту не принимается", document=index)
            if raw.get("kind") and not kind:
                add(CHECK_TYPE_UNKNOWN, LEVEL_ERROR,
                    f"документ №{index}: {why}", document=index)
            continue

        key = (kind, _clean(doc_no), _clean(material))
        if key in seen:
            add(CHECK_DUPLICATE, LEVEL_WARNING,
                f"документ №{index} ({kind} {doc_no}): уже есть под №{seen[key]} — "
                f"в описи будет одна строка, проверьте, не копия ли это", document=index)
        else:
            seen[key] = index

        rules = type_rules.get(kind) or {}
        # ссылка на основание соответствия (ТР ТС, ГОСТ, ТУ)
        if rules.get("basis_required", True) and not standard:
            add(CHECK_BASIS_MISSING, LEVEL_WARNING,
                f"документ №{index} ({kind} {doc_no}): не указано основание соответствия "
                f"(ТР ТС, ГОСТ, ТУ) — по какому документу выдан, непонятно", document=index)
        # срок действия
        if rules.get("validity") == "required":
            if until is None:
                add(CHECK_NO_VALIDITY, LEVEL_WARNING,
                    f"документ №{index} ({kind} {doc_no}): срок действия не указан — "
                    f"проверить, действовал ли он на дату применения", document=index)
        # без файла реестр — это список названий, а не документы
        if not file_ref:
            add(CHECK_NO_FILE, LEVEL_WARNING,
                f"документ №{index} ({kind} {doc_no}): нет файла или скана — "
                f"в комплект приложить нечего", document=index)
        # заверение копии
        if file_ref and not (attested_by and attested_role):
            add(CHECK_NOT_ATTESTED, LEVEL_WARNING,
                f"документ №{index} ({kind} {doc_no}): копия без отметки «копия верна» "
                f"(ФИО и роль того, кто сверил с оригиналом)", document=index)

        # привязка к партии и материалу
        doc_material = _clean(material)
        doc_batch = _clean(batch)
        slot = use_map.get((doc_material, doc_batch))
        if slot is None:
            # материал есть, а партии такой в применении нет — возможно, перепутана партия
            same_material = [(k, v) for k, v in use_map.items() if k[0] == doc_material]
            if same_material:
                add(CHECK_BATCH_MISMATCH, LEVEL_ERROR,
                    f"документ №{index} ({kind} {doc_no}) — на партию «{batch or 'не указана'}», "
                    f"а в работе партия(и) "
                    f"{', '.join(sorted(str(v['batch'] or 'б/н') for _, v in same_material))}: "
                    f"документ прикрывает не ту партию", document=index)
            else:
                add(CHECK_BATCH_UNUSED, LEVEL_WARNING,
                    f"документ №{index} ({kind} {doc_no}): материал «{material}» "
                    f"в применении не значится — документ к делу не привязан", document=index)
            doc["used"] = False
        else:
            doc["used"] = True
            doc["used_qty"] = round(slot["qty"], 3)
            doc["used_unit"] = slot["unit"]
            doc["applied_from"] = slot["from"].isoformat() if slot["from"] else None
            doc["applied_to"] = slot["to"].isoformat() if slot["to"] else None
            check_day = slot["from"] or today
            if rules.get("validity") == "required" or until is not None:
                if until is not None and until < check_day:
                    add(CHECK_EXPIRED, LEVEL_ERROR,
                        f"документ №{index} ({kind} {doc_no}, {material}, партия {batch or 'б/н'}): "
                        f"действовал до {until.isoformat()}, а материал применялся "
                        f"{check_day.isoformat()} — документ на дату применения не действовал",
                        document=index)
                    doc["valid_on_use"] = False
                else:
                    doc["valid_on_use"] = None if until is None else True
        accepted.append(doc)

    # материал «в работе», но документа на его партию нет — ошибка
    for (material_key, batch_key), slot in sorted(use_map.items()):
        covered = [d for d in accepted
                   if _clean(d.get("material")) == material_key
                   and _clean(d.get("batch")) == batch_key]
        if covered:
            continue
        req = req_map.get(material_key)
        required = (req or {}).get("required") or []
        add(CHECK_MATERIAL_NO_DOC, LEVEL_ERROR,
            f"{slot['material'] or material_key}"
            f"{' партия ' + str(slot['batch']) if slot['batch'] else ''}: материал применён"
            f"{' (' + str(round(slot['qty'], 3)) + ' ' + str(slot.get('unit') or '') + ')' if slot['qty'] else ''}"
            f", а документа качества на эту партию нет"
            + (f" — требуется: {', '.join(required)}" if required else ""),
            material=slot["material"] or material_key)

    # ─── карточка по материалам ───
    materials: dict[str, dict] = {}
    for (material_key, batch_key), slot in use_map.items():
        req = req_map.get(material_key)
        docs = [d for d in accepted if _clean(d.get("material")) == material_key]
        have_kinds = {d["kind"] for d in docs if d.get("kind")}
        required = (req or {}).get("required") or []
        missing = [k for k in required if k not in have_kinds]
        covered_batch = [d for d in docs if _clean(d.get("batch")) == batch_key]
        covered_batch = [d for d in docs if _clean(d.get("batch")) == batch_key]
        batch_valid = (any(d.get("valid_on_use") is not False for d in covered_batch)
                       if covered_batch else None)
        if not req:
            status = COVER_UNDECLARED
            add(CHECK_REQ_UNDECLARED, LEVEL_WARNING,
                f"{slot['material'] or material_key}: требование к составу документов не заявлено — "
                f"комплектность проверить не с чем (нужен файл требований: проект, договор, ТУ)",
                material=slot["material"] or material_key)
        elif not docs:
            status = COVER_MISSING
        elif not covered_batch:
            status = COVER_PARTIAL
        elif batch_valid is False:
            # документ на партию есть, но на дату применения не действовал
            status = COVER_INVALID
        elif missing:
            status = COVER_PARTIAL
        else:
            status = COVER_FULL
        card = materials.setdefault(material_key, {
            "material": slot["material"] or material_key, "status": status,
            "required": required, "basis": (req or {}).get("basis"),
            "documents": 0, "batches": [], "missing": missing,
        })
        if status == COVER_INVALID:
            card["status"] = COVER_INVALID
        elif status == COVER_UNDECLARED and card["status"] != COVER_UNDECLARED:
            card["status"] = status
        card["documents"] = len(docs)
        card["batches"].append({"batch": slot["batch"], "qty": round(slot["qty"], 3),
                                "unit": slot["unit"],
                                "documented": bool(covered_batch),
                                "valid_on_use": batch_valid,
                                "applied_from": slot["from"].isoformat() if slot["from"] else None})

    # документы на материалы, которых нет в применении и в требованиях
    for material_key, req in req_map.items():
        if material_key in materials:
            continue
        docs = [d for d in accepted if _clean(d.get("material")) == material_key]
        materials[material_key] = {
            "material": req["material"], "status": COVER_FULL if docs else COVER_MISSING,
            "required": req["required"], "basis": req["basis"], "documents": len(docs),
            "batches": [], "missing": [k for k in req["required"]
                                       if k not in {d["kind"] for d in docs}],
        }

    # ─── стык с журналом входного контроля (круг 7) ───
    registry_keys = {(d["kind"], _clean(d.get("number")), _clean(d.get("material")))
                     for d in accepted}
    registry_numbers = {_clean(d.get("number")) for d in accepted if d.get("number")}
    for index, record in enumerate(verification or [], start=1):
        material = str(record.get("material") or "").strip()
        decision = str(record.get("decision") or "").strip().lower()
        doc = record.get("quality_doc") or {}
        doc_no = _clean(doc.get("number"))
        if decision in ("допущен", "допущен условно") and doc_no and \
                doc_no not in registry_numbers:
            add(CHECK_VERIF_NO_DOC, LEVEL_ERROR,
                f"входной контроль (запись №{index}, {material}): допуск по документу "
                f"«{doc.get('number')}», которого нет в реестре документов качества — "
                f"допуск подтвердить нечем", material=material)
        if decision in ("допущен", "допущен условно") and not doc_no:
            add(CHECK_VERIF_NO_DOC, LEVEL_ERROR,
                f"входной контроль (запись №{index}, {material}): материал допущен без "
                f"документа качества и без ссылки на реестр", material=material)

    verified_keeps = {(_clean(r.get("material")), _clean(r.get("batch")))
                      for r in verification or [] if r.get("decision")}
    for doc in accepted:
        key = (_clean(doc.get("material")), _clean(doc.get("batch")))
        if doc.get("used") and key not in verified_keeps:
            add(CHECK_DOC_NO_VERIF, LEVEL_WARNING,
                f"документ №{doc['no']} ({doc.get('kind')} {doc.get('number')}): материал "
                f"применён, документ в реестре есть, а записи входного контроля по партии "
                f"нет — контроль не оформлен", document=doc["no"])

    if not documents:
        add(CHECK_EMPTY, LEVEL_ERROR, "в реестре нет ни одного документа качества")

    registry = {
        "schema": SCHEMA,
        "kind": "quality_docs",
        "title": "Реестр документов качества на материалы и оборудование",
        "number": number,
        "built_at": today.isoformat(),
        "object_name": object_name,
        "status": STATUS_DRAFT,
        "confirmed_by": None,
        "types_profile": {k: typebook.get(k) for k in ("schema", "profile", "checked_at",
                                                       "disclaimer") if typebook.get(k)},
        "documents": accepted,
        "rejected": rejected,
        "materials": sorted(materials.values(), key=lambda m: m["material"]),
        "usage": [{"material": v["material"], "batch": v["batch"], "qty": round(v["qty"], 3),
                   "unit": v["unit"],
                   "applied_from": v["from"].isoformat() if v["from"] else None,
                   "applied_to": v["to"].isoformat() if v["to"] else None}
                  for v in use_map.values()],
        "checks": checks,
        "totals": {
            "documents": len(accepted),
            "rejected": len(rejected),
            "materials": len(materials),
            "batches": len(use_map),
            "covered_batches": sum(1 for m in materials.values()
                                   for b in m["batches"]
                                   if b["documented"] and b.get("valid_on_use") is not False),
            "attested": len([d for d in accepted if d.get("attested_by")]),
            "errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
            "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING]),
        },
        "disclaimer": ("Реестр показывает, по какому документу качества материал допущен в работу "
                       "и на какую партию он распространяется. Состав требуемых документов берётся "
                       "из файла требований (проект, договор, ТУ): агент его не выдумывает. "
                       "Комплект закрывает человек — инженер ПТО, подписью с ФИО и ролью."),
    }
    registry["can_transfer"] = can_transfer(registry)
    return registry


def can_transfer(registry: dict) -> dict:
    """Можно ли передавать комплект документов качества заказчику. Причины, а не «нет»."""
    reasons: list[str] = []
    signature = check_signature(registry)
    if not signature["has_signature"]:
        reasons.append("комплект не подписан ответственным (нет ФИО и роли)")
    elif signature["changed_after_confirm"]:
        reasons.append("реестр изменён после подписи — подпись недействительна")
    errors = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_ERROR]
    if errors:
        reasons.append(f"ошибок: {len(errors)} — устранить до передачи комплекта")
    if not registry.get("documents"):
        reasons.append("в реестре нет ни одного документа качества")
    missing = [m["material"] for m in registry.get("materials") or []
               if m["status"] == COVER_MISSING]
    if missing:
        reasons.append("материалов без единого документа: " + ", ".join(missing))
    unattested = [d for d in registry.get("documents") or []
                  if d.get("file") and not d.get("attested_by")]
    if unattested:
        reasons.append(f"копий без отметки «копия верна»: {len(unattested)}")
    undeclared = [m["material"] for m in registry.get("materials") or []
                  if m["status"] == COVER_UNDECLARED]
    if undeclared:
        reasons.append("материалов без заявленного состава документов: " + ", ".join(undeclared))
    return {"allowed": not reasons, "reasons": reasons}


def sign(registry: dict, *, by: str, role: str, reason: str = "", audit_log=None,
         today: date | None = None) -> dict:
    """Подписать комплект. Без ФИО и роли, с ошибками — отказ."""
    today = today or date.today()
    registry = json.loads(json.dumps(registry))
    problems: list[str] = []
    if not str(by or "").strip():
        problems.append("анонимная подпись: не указано ФИО ответственного")
    if not str(role or "").strip():
        problems.append("не указана роль (должность) ответственного")
    errors = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_ERROR]
    if errors:
        problems.append(f"ошибок в реестре: {len(errors)} — {errors[0]['message'][:120]}")
    if not registry.get("documents"):
        problems.append("в реестре нет ни одного документа качества — подписывать нечего")
    if problems:
        registry["sign_problems"] = problems
        return registry

    registry["confirmed_by"] = {"by": str(by).strip(), "role": str(role).strip(),
                                "reason": str(reason or ""), "at": today.isoformat()}
    registry["confirmed_by"]["content_fingerprint"] = content_fingerprint(registry)
    registry.pop("sign_problems", None)
    registry["status"] = STATUS_SIGNED
    registry["can_transfer"] = can_transfer(registry)
    if audit_log is not None:
        audit_log.append("agent:quality", "agent", "quality.signed",
                         f"quality:{registry.get('number') or 'б/н'}",
                         {"by": registry["confirmed_by"]["by"],
                          "role": registry["confirmed_by"]["role"],
                          "documents": len(registry.get("documents") or [])})
    return registry


# ─────────────────────────── вывод ───────────────────────────
def _doc_rows(registry: dict) -> list[list]:
    rows = []
    for doc in registry.get("documents") or []:
        rows.append([doc["no"], doc.get("kind") or "—", doc.get("number") or "—",
                     doc.get("issue_date") or "—", doc.get("valid_until") or "—",
                     doc.get("material") or "—", doc.get("batch") or "б/н",
                     doc.get("standard") or "не указано",
                     "копия заверена" if doc.get("attested_by") else
                     ("не заверена" if doc.get("file") else "нет файла"),
                     "да" if doc.get("used") else "нет"])
    return rows


DOC_HEADERS = ["№", "Тип документа", "Номер", "Выдан", "Годен до", "Материал", "Партия",
               "Основание (ТР ТС/ГОСТ/ТУ)", "Копия", "Партия в работе"]


def to_markdown(registry: dict) -> str:
    lines = [f"# {registry['title']}", "",
             f"**Объект:** {registry.get('object_name') or '—'} · "
             f"**реестр:** {registry.get('number') or 'б/н'} · "
             f"**собран:** {registry.get('built_at')}", ""]
    totals = registry["totals"]
    lines += [f"Документов: {totals['documents']} · отклонено: {totals['rejected']} · "
              f"материалов: {totals['materials']} · партий: {totals['batches']} · "
              f"партий с документом: {totals['covered_batches']} · "
              f"копий заверено: {totals['attested']}", "",
              f"**Статус:** {registry.get('status')}", ""]
    close = registry.get("can_transfer") or {}
    if close.get("allowed"):
        lines += ["Комплект можно передавать: подписан, ошибок нет, все копии заверены.", ""]
    elif close.get("reasons"):
        lines += ["**Передавать нельзя:**", ""] + [f"- {r}" for r in close["reasons"]] + [""]

    errors = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_ERROR]
    warnings = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_WARNING]
    if errors:
        lines += ["## Ошибки", ""] + [f"- **{c['code']}** — {c['message']}" for c in errors] + [""]
    if warnings:
        lines += ["## Требует решения", ""] + [f"- {c['code']} — {c['message']}"
                                               for c in warnings] + [""]

    lines += ["## Документы качества", "",
              "| " + " | ".join(DOC_HEADERS) + " |", "|" + "---|" * len(DOC_HEADERS)]
    for row in _doc_rows(registry):
        lines.append("| " + " | ".join(str(x) for x in row) + " |")
    lines.append("")

    if registry.get("materials"):
        lines += ["## Материалы", "",
                  "| Материал | Состояние комплекта | Документов | Партии | Чего не хватает | Основание требования |",
                  "|---|---|---|---|---|---|"]
        for card in registry["materials"]:
            batches = ", ".join(f"{b['batch'] or 'б/н'}"
                                f"{'' if b['documented'] else ' ✗'}" for b in card["batches"]) or "—"
            lines.append(f"| {card['material']} | {card['status']} | {card['documents']} | "
                         f"{batches} | {', '.join(card['missing']) or '—'} | "
                         f"{card.get('basis') or 'не заявлено'} |")
        lines.append("")

    if registry.get("rejected"):
        lines += ["## Документы, не принятые в реестр", ""]
        lines += [f"- №{d['no']}: {d.get('why')}" for d in registry["rejected"]] + [""]
    if registry.get("confirmed_by"):
        who = registry["confirmed_by"]
        lines += [f"Подписал: {who.get('by')}, {who.get('role')} ({who.get('at')}).", ""]
    if registry.get("sign_problems"):
        lines += ["**Подпись не принята:**", ""] + [f"- {p}" for p in registry["sign_problems"]] + [""]
    profile = registry.get("types_profile") or {}
    if profile.get("disclaimer"):
        lines += [f"> {profile['disclaimer']}", ""]
    lines += [f"> {registry.get('disclaimer')}", ""]
    return "\n".join(lines)


def export_xlsx(registry: dict, path: str | Path) -> Path:
    """XLSX: листы «Документы», «Материалы», «Проверки»."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="1F4E79")
    head_font = Font(color="FFFFFF", bold=True)

    def sheet(title, headers, widths, rows):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        for row in rows:
            ws.append(row)
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width
        ws.freeze_panes = "A2"
        return ws

    wb.remove(wb.active)
    sheet("Документы", DOC_HEADERS, [5, 26, 16, 12, 12, 26, 12, 26, 14, 14],
          _doc_rows(registry))
    sheet("Материалы",
          ["Материал", "Состояние комплекта", "Документов", "Партии", "Чего не хватает",
           "Основание требования"],
          [30, 24, 12, 24, 30, 30],
          [[card["material"], card["status"], card["documents"],
            ", ".join(f"{b['batch'] or 'б/н'}{'' if b['documented'] else ' ✗'}"
                      for b in card["batches"]) or "—",
            ", ".join(card["missing"]) or "—", card.get("basis") or "не заявлено"]
           for card in registry.get("materials") or []])
    sheet("Проверки", ["Уровень", "Код", "Что не так", "Материал", "Документ"],
          [10, 28, 80, 24, 10],
          [[("ошибка" if c["level"] == "error" else "внимание"), c["code"], c["message"],
            c.get("material") or "", c.get("document") or ""]
           for c in registry.get("checks") or []])
    wb.save(path)
    return path


def export_docx(registry: dict, path: str | Path) -> Path:
    """Опись документов качества: таблица, состояние комплекта, места подписей."""
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("Опись документов качества на материалы и оборудование", level=1)
    doc.add_paragraph(f"Объект: {registry.get('object_name') or '—'}　"
                      f"Реестр № {registry.get('number') or 'б/н'} от {registry.get('built_at')}")

    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells,
                          ["№", "Документ (тип, номер, дата)", "Материал, партия",
                           "Основание соответствия", "Срок действия", "Копия"]):
        cell.text = text
    for item in registry.get("documents") or []:
        cells = table.add_row().cells
        cells[0].text = str(item["no"])
        cells[1].text = (f"{item.get('kind') or '—'} № {item.get('number') or 'б/н'} "
                         f"от {item.get('issue_date') or '—'}")
        cells[2].text = f"{item.get('material') or '—'}, партия {item.get('batch') or 'б/н'}"
        cells[3].text = item.get("standard") or "не указано"
        cells[4].text = item.get("valid_until") or "—"
        cells[5].text = ("копия верна" if item.get("attested_by")
                         else ("не заверена" if item.get("file") else "нет файла"))

    doc.add_paragraph()
    totals = registry["totals"]
    run = doc.add_paragraph().add_run(
        f"Статус: {registry.get('status')}. Документов: {totals['documents']}, "
        f"материалов: {totals['materials']}, партий с документом: "
        f"{totals['covered_batches']} из {totals['batches']}.")
    run.bold = True
    run.font.size = Pt(10)

    close = registry.get("can_transfer") or {}
    if close.get("reasons"):
        doc.add_paragraph("Комплект передавать нельзя:")
        for reason in close["reasons"]:
            doc.add_paragraph(f"• {reason}", style="List Bullet")

    doc.add_paragraph()
    doc.add_paragraph("Комплект подготовил: ______________________ / ______________________")
    doc.add_paragraph("Проверил (инженер ПТО): ______________________ / ______________________")
    doc.add_paragraph("Принял (представитель заказчика): ______________________ / ______________")
    doc.add_paragraph(registry.get("disclaimer") or "").runs[0].font.size = Pt(9)
    doc.save(path)
    return path


def write(registry: dict, out_dir: str | Path, *, stem: str | None = None) -> dict:
    """Сохранить реестр: JSON, Markdown, XLSX, DOCX (опись)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tail = stem or (str(registry.get("number") or "").replace(" ", "_").replace("/", "-")
                    or "объект")
    (out / f"quality_{tail}.json").write_text(json.dumps(registry, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    (out / f"quality_{tail}.md").write_text(to_markdown(registry), encoding="utf-8")
    export_xlsx(registry, out / f"quality_{tail}.xlsx")
    export_docx(registry, out / f"quality_{tail}.docx")
    return {"json": str(out / f"quality_{tail}.json"),
            "markdown": str(out / f"quality_{tail}.md"),
            "xlsx": str(out / f"quality_{tail}.xlsx"),
            "docx": str(out / f"quality_{tail}.docx")}
