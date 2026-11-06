# -*- coding: utf-8 -*-
"""Движение документации: выдача РД «В производство работ» и согласование ИД до подписи.

Две обязанности ПТО, которые обычно тонут в переписке и «папке на подпись»:

**Выдача рабочей документации.** Работать можно только по той редакции РД, которая передана со
штампом «В производство работ». Поэтому реестр выдачи отвечает на вопросы: какая редакция
передана, кому, когда, есть ли подпись получившего и не заменена ли эта редакция более поздней.
Если в акте или журнале работ сослались на шифр РД, которой нет в реестре выдачи или которая
выдана без штампа, — это ошибка (`WORK_WITHOUT_RD`): работы выполнены по документу, право
работать по которому не подтверждено.

**Согласование ИД.** Комплект живёт по циклу «передан → замечания → устранено → передан снова →
согласовано/подписано». Агент считает, что комплекс с открытыми замечаниями подписан быть не
может (`SIGNED_WITH_OPEN_REMARKS`), что подпись без ФИО и роли — не подпись
(`SIGNED_ANONYMOUS`), что повторная передача без отметки об устранении — это та же передача
(`RESUBMIT_WITHOUT_FIX`) и что просроченное замечание — это срок, а не «в работе».

Правила общие для проекта: статус ставит человек, агент проверяет полноту и сроки; ничего не
додумывается — нет подписи получившего, значит её нет.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .fingerprint import check_signature, content_fingerprint

SCHEMA_RD = "pto-agent/rd-issue@1"
SCHEMA_SUBMITTAL = "pto-agent/submittals@1"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

STAMP_OK = "в производство работ"
STAMP_ABSENT = "без штампа"

RD_IN_WORK = "в производстве"
RD_REPLACED = "заменена"
RD_CANCELLED = "аннулирована"
RD_NOT_ISSUED = "не выдана"
RD_UNKNOWN = "состояние не определено"

SUB_TRANSFERRED = "передан на согласование"
SUB_REMARKED = "возвращён с замечаниями"
SUB_FIXED = "замечания устранены, передан повторно"
SUB_SIGNED = "подписан"
SUB_SIGNED_WITH_REMARKS = "подписан с открытыми замечаниями — подпись недействительна"
SUB_CHANGED = "изменён после подписи — подпись недействительна"

DECISION_AGREED = "согласовано"
DECISION_WITH_REMARKS = "согласовано с замечаниями"
DECISION_RETURNED = "возвращено на доработку"

CHECK_RD_NO_DATE = "RD_NO_DATE"
CHECK_RD_NO_RECEIPT = "RD_WITHOUT_RECEIPT"
CHECK_RD_NO_STAMP = "RD_WITHOUT_STAMP"
CHECK_RD_STAMP_NO_BASIS = "RD_STAMP_WITHOUT_TRANSMITTAL"
CHECK_RD_SUPERSEDED = "RD_REPLACED_IN_WORK"
CHECK_RD_UNKNOWN_REV = "RD_UNKNOWN_REVISION"
CHECK_RD_DUPLICATE = "RD_DUPLICATE_TRANSMITTAL"
CHECK_RD_EMPTY = "RD_REGISTRY_EMPTY"
CHECK_WORK_WITHOUT_RD = "WORK_WITHOUT_RD"

CHECK_SUB_NO_RECEIPT = "SUB_WITHOUT_RECEIPT"
CHECK_SUB_STATUS = "SUB_STATUS_UNKNOWN"
CHECK_SUB_SIGNED_OPEN = "SIGNED_WITH_OPEN_REMARKS"
CHECK_SUB_SIGNED_ANON = "SIGNED_ANONYMOUS"
CHECK_SUB_RESUBMIT_NO_FIX = "RESUBMIT_WITHOUT_FIX"
CHECK_SUB_OVERDUE = "REMARK_OVERDUE"
CHECK_SUB_REMARK_NO_ANSWER = "REMARK_WITHOUT_ANSWER"
CHECK_SUB_REMARK_NO_DUE = "REMARK_WITHOUT_DUE"
CHECK_SUB_EMPTY = "SUBMITTALS_EMPTY"
CHECK_SUB_OPEN_AT_SIGN = "OPEN_REMARKS_AT_SIGNATURE"

SUB_STATUSES = (SUB_TRANSFERRED, SUB_REMARKED, SUB_FIXED, SUB_SIGNED)


def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y.%m.%d", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _clean(value) -> str:
    return " ".join(str(value or "").replace("ё", "е").lower().split())


def load_list(path: str | Path, key: str) -> tuple[list[dict], list[str]]:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], [f"{path.name}: не читается ({exc.__class__.__name__})"]
    items = data if isinstance(data, list) else data.get(key)
    if not isinstance(items, list):
        return [], [f"{path.name}: нет списка «{key}»"]
    return items, []


# ─────────────────────────── выдача РД ───────────────────────────
def build_rd_registry(transmittals: list[dict], *, revisions: list[dict] | None = None,
                      works: list[dict] | None = None, object_name: str = "",
                      number: str = "", today: date | None = None) -> dict:
    """Реестр выдачи РД: какая редакция, кому, когда, со штампом и с подписью получившего."""
    today = today or date.today()
    checks: list[dict] = []
    issued: list[dict] = []
    rejected: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    # реестр редакций: какие редакции существуют и какая из них действующая
    rev_map: dict[str, dict] = {}
    for raw in revisions or []:
        code = _clean(raw.get("doc") or raw.get("code"))
        if not code:
            continue
        rev = str(raw.get("revision") or raw.get("rev") or "").strip()
        slot = rev_map.setdefault(code, {"doc": raw.get("doc") or raw.get("code"),
                                         "title": raw.get("title"), "current": None,
                                         "revisions": []})
        slot["revisions"].append(rev or "б/н")
        if raw.get("current") is True or _clean(raw.get("status")) in ("действующая", "в работе"):
            slot["current"] = rev or "б/н"
    for slot in rev_map.values():
        if slot["current"] is None and slot["revisions"]:
            slot["current"] = slot["revisions"][-1]

    seen: dict[tuple, int] = {}
    for index, raw in enumerate(transmittals or [], start=1):
        doc = str(raw.get("doc") or raw.get("code") or "").strip()
        title = str(raw.get("title") or "").strip()
        revision = str(raw.get("revision") or raw.get("rev") or "").strip()
        day = _parse_date(raw.get("date") or raw.get("sent_at"))
        stamp = _clean(raw.get("stamp") or raw.get("status"))
        recipient = str(raw.get("recipient") or raw.get("to") or "").strip()
        copies = raw.get("copies", raw.get("qty"))
        receipt = raw.get("receipt") or raw.get("received_by") or {}
        receipt_by = str((receipt or {}).get("by") or "").strip() if isinstance(receipt, dict) \
            else str(receipt or "").strip()
        receipt_date = _parse_date((receipt or {}).get("at")) if isinstance(receipt, dict) else None

        record = {"no": index, "doc": doc or None, "title": title or None,
                  "revision": revision or None, "date": day.isoformat() if day else None,
                  "recipient": recipient or None, "copies": copies,
                  "stamp": STAMP_OK if stamp in (STAMP_OK, "в производство работ", "в производство")
                  else (STAMP_ABSENT if stamp else None),
                  "receipt": ({"by": receipt_by, "at": receipt_date.isoformat() if receipt_date
                               else None} if receipt_by else None),
                  "purpose": str(raw.get("purpose") or "").strip() or None}

        missing = [name for name, ok in (("шифр документа", bool(doc)), ("дату передачи", bool(day)),
                                        ("получателя", bool(recipient))) if not ok]
        if missing:
            record["why"] = f"не указано: {', '.join(missing)}"
            rejected.append(record)
            add(CHECK_RD_NO_DATE if not day else CHECK_RD_NO_RECEIPT, LEVEL_ERROR,
                f"передача №{index} ({doc or 'документ не указан'}): {record['why']} — "
                f"в реестр выдачи не попадает", transmittal=index)
            continue

        key = (_clean(doc), revision, _clean(recipient), record["date"])
        if key in seen:
            add(CHECK_RD_DUPLICATE, LEVEL_WARNING,
                f"передача №{index} ({doc}, ред. {revision or 'б/н'}): та же дата, редакция и "
                f"получатель, что в №{seen[key]} — проверьте дубль", transmittal=index)
        else:
            seen[key] = index

        if not record["stamp"]:
            add(CHECK_RD_NO_STAMP, LEVEL_WARNING,
                f"передача №{index} ({doc}): штамп не указан — по какой редакции разрешено "
                f"работать, из реестра не видно", transmittal=index)
        elif record["stamp"] == STAMP_ABSENT:
            add(CHECK_RD_NO_STAMP, LEVEL_WARNING,
                f"передача №{index} ({doc}, ред. {revision or 'б/н'}): передан без штампа "
                f"«В производство работ» — работать по этой передаче нельзя", transmittal=index)
        if not record["receipt"]:
            add(CHECK_RD_NO_RECEIPT, LEVEL_ERROR,
                f"передача №{index} ({doc}): нет подписи получившего — передача не подтверждена",
                transmittal=index)

        slot = rev_map.get(_clean(doc))
        if slot and revision and revision not in slot["revisions"]:
            add(CHECK_RD_UNKNOWN_REV, LEVEL_ERROR,
                f"передача №{index} ({doc}): редакция «{revision}» в реестре редакций "
                f"отсутствует (есть: {', '.join(slot['revisions'])})", transmittal=index)
        current = (slot or {}).get("current")
        record["current_revision"] = current
        if slot and current and revision and revision != current and record["stamp"] == STAMP_OK:
            add(CHECK_RD_SUPERSEDED, LEVEL_ERROR,
                f"передача №{index} ({doc}): в производство выдана редакция {revision}, "
                f"а действующая — {current}; по заменённой редакции работать нельзя",
                transmittal=index)
        issued.append(record)

    # стык с работами: ссылка на РД, которой нет в выдаче или она без штампа
    by_doc: dict[str, list[dict]] = {}
    for record in issued:
        by_doc.setdefault(_clean(record["doc"]), []).append(record)
    for index, work in enumerate(works or [], start=1):
        doc = str(work.get("rd") or work.get("doc") or work.get("rd_code") or "").strip()
        if not doc:
            continue
        name = str(work.get("work") or work.get("title") or f"работа №{index}")
        rows = by_doc.get(_clean(doc)) or []
        if not rows:
            add(CHECK_WORK_WITHOUT_RD, LEVEL_ERROR,
                f"работа «{name[:70]}» ссылается на РД «{doc}», которой нет в реестре выдачи — "
                f"право работать по ней не подтверждено", work=name)
            continue
        stamped = [r for r in rows if r["stamp"] == STAMP_OK]
        if not stamped:
            add(CHECK_WORK_WITHOUT_RD, LEVEL_ERROR,
                f"работа «{name[:70]}» ссылается на РД «{doc}», переданную без штампа "
                f"«В производство работ»", work=name)

    # состояние по документу
    documents = []
    for code, rows in sorted(by_doc.items()):
        last = sorted(rows, key=lambda r: (r["date"] or "", r["no"]))[-1]
        current = (rev_map.get(code) or {}).get("current")
        if last["stamp"] != STAMP_OK:
            status = RD_NOT_ISSUED
        elif current and last["revision"] and last["revision"] != current:
            status = RD_REPLACED
        elif current is None and last.get("current_revision") is None and not rev_map:
            status = RD_IN_WORK if last["stamp"] == STAMP_OK else RD_NOT_ISSUED
        else:
            status = RD_IN_WORK
        documents.append({"doc": last["doc"], "title": last.get("title"),
                          "current_revision": current, "last_revision": last["revision"],
                          "issued_at": last["date"], "recipient": last["recipient"],
                          "stamp": last["stamp"], "status": status,
                          "transmittals": len(rows)})

    if not transmittals:
        add(CHECK_RD_EMPTY, LEVEL_ERROR, "в реестре нет ни одной передачи РД")

    registry = {
        "schema": SCHEMA_RD, "kind": "rd_issue",
        "title": "Реестр выдачи рабочей документации",
        "number": number, "built_at": today.isoformat(), "object_name": object_name,
        "status": "черновик", "confirmed_by": None,
        "transmittals": issued, "rejected": rejected, "documents": documents,
        "revisions": [{"doc": v["doc"], "title": v["title"], "current": v["current"],
                       "revisions": v["revisions"]} for _, v in sorted(rev_map.items())],
        "checks": checks,
        "totals": {"transmittals": len(issued), "rejected": len(rejected),
                   "documents": len(documents),
                   "with_stamp": len([r for r in issued if r["stamp"] == STAMP_OK]),
                   "without_receipt": len([r for r in issued if not r["receipt"]]),
                   "works_checked": len(works or []),
                   "errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
                   "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING])},
        "disclaimer": ("Реестр выдачи показывает, какая редакция РД передана, кому и со штампом ли "
                       "«В производство работ». Разрешение работать даёт заказчик передачей "
                       "документации; агент лишь сверяет записи и ссылки на РД в работах."),
    }
    registry["can_work"] = can_work(registry)
    return registry


def can_work(registry: dict) -> dict:
    """Можно ли считать, что по объекту есть документация для производства работ."""
    reasons: list[str] = []
    errors = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_ERROR]
    if errors:
        reasons.append(f"ошибок: {len(errors)} — сначала разобрать выдачу")
    if not registry.get("transmittals"):
        reasons.append("в реестре нет ни одной передачи РД")
    no_stamp = [d["doc"] for d in registry.get("documents") or []
                if d["status"] in (RD_NOT_ISSUED, RD_REPLACED)]
    if no_stamp:
        reasons.append("документов без действующей выдачи «В производство работ»: "
                       + ", ".join(str(x) for x in no_stamp))
    return {"allowed": not reasons, "reasons": reasons}


# ─────────────────────────── согласование ИД ───────────────────────────
def build_submittal_registry(submittals: list[dict], *, object_name: str = "", number: str = "",
                             today: date | None = None) -> dict:
    """Реестр передачи ИД на согласование: замечания, устранение, повторная передача, подпись."""
    today = today or date.today()
    checks: list[dict] = []
    items: list[dict] = []
    rejected: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    for index, raw in enumerate(submittals or [], start=1):
        title = str(raw.get("title") or raw.get("name") or "").strip()
        act_ref = str(raw.get("act_ref") or raw.get("act") or "").strip()
        sent = _parse_date(raw.get("sent_at") or raw.get("date"))
        recipient = str(raw.get("recipient") or raw.get("to") or "").strip()
        receipt = raw.get("receipt") or {}
        receipt_by = str((receipt or {}).get("by") or "").strip() if isinstance(receipt, dict) else ""
        remarks_raw = raw.get("remarks") or []
        signature = raw.get("signature") or raw.get("signed") or {}
        status_raw = _clean(raw.get("status"))

        item = {"no": index, "title": title or None, "act_ref": act_ref or None,
                "sent_at": sent.isoformat() if sent else None, "recipient": recipient or None,
                "receipt": {"by": receipt_by} if receipt_by else None,
                "remarks": [], "signature": None, "status": None}

        missing = [name for name, ok in (("состав комплекта", bool(title)),
                                        ("дату передачи", bool(sent)),
                                        ("получателя", bool(recipient))) if not ok]
        if missing:
            item["why"] = f"не указано: {', '.join(missing)}"
            rejected.append(item)
            add(CHECK_SUB_NO_RECEIPT, LEVEL_ERROR,
                f"комплект №{index}: {item['why']} — в реестр согласования не попадает",
                submittal=index)
            continue

        if not receipt_by:
            add(CHECK_SUB_NO_RECEIPT, LEVEL_WARNING,
                f"комплект №{index} ({title[:60]}): нет отметки о получении — передача "
                f"не подтверждена", submittal=index)

        open_remarks = 0
        for remark_no, remark in enumerate(remarks_raw, start=1):
            text = str(remark.get("text") or "").strip()
            due = _parse_date(remark.get("due"))
            fixed_at = _parse_date(remark.get("fixed_at"))
            answer = str(remark.get("answer") or "").strip()
            row = {"no": remark_no, "text": text or None,
                   "issued_at": (_parse_date(remark.get("issued_at")) or sent).isoformat()
                   if (_parse_date(remark.get("issued_at")) or sent) else None,
                   "due": due.isoformat() if due else None,
                   "fixed_at": fixed_at.isoformat() if fixed_at else None,
                   "answer": answer or None,
                   "status": "устранено" if fixed_at else "открыто"}
            if not text:
                add(CHECK_SUB_REMARK_NO_ANSWER, LEVEL_WARNING,
                    f"комплект №{index}, замечание {remark_no}: текст замечания не записан — "
                    f"устранять нечего", submittal=index, remark=remark_no)
            if not due:
                add(CHECK_SUB_REMARK_NO_DUE, LEVEL_WARNING,
                    f"комплект №{index}, замечание {remark_no}: не указан срок устранения — "
                    f"срок не проверить", submittal=index, remark=remark_no)
            elif not fixed_at and due < today:
                add(CHECK_SUB_OVERDUE, LEVEL_ERROR,
                    f"комплект №{index}, замечание {remark_no}: срок устранения "
                    f"{due.isoformat()} прошёл — замечание просрочено", submittal=index,
                    remark=remark_no)
            if not fixed_at:
                open_remarks += 1
            item["remarks"].append(row)

        status = SUB_TRANSFERRED
        if status_raw in ("подписан", "подписано", "согласовано", SUB_SIGNED):
            status = SUB_SIGNED
        elif raw.get("resubmitted_at"):
            status = SUB_FIXED
            if not any(r["fixed_at"] for r in item["remarks"]):
                add(CHECK_SUB_RESUBMIT_NO_FIX, LEVEL_ERROR,
                    f"комплект №{index} ({title[:60]}): передан повторно, но ни одно замечание "
                    f"не отмечено устранённым — это та же передача", submittal=index)
        elif item["remarks"]:
            status = SUB_REMARKED
        elif status_raw and status_raw not in ("передан", "передано", "на согласовании"):
            add(CHECK_SUB_STATUS, LEVEL_WARNING,
                f"комплект №{index}: статус «{raw.get('status')}» не из списка — состояние "
                f"определено по данным (замечания, подпись)", submittal=index)

        if signature:
            by = str((signature or {}).get("by") or "").strip()
            role = str((signature or {}).get("role") or "").strip()
            decision = str(signature.get("decision") or "").strip()
            at = _parse_date(signature.get("at"))
            item["signature"] = {"by": by or None, "role": role or None,
                                 "decision": decision or None,
                                 "at": at.isoformat() if at else None}
            if not (by and role):
                add(CHECK_SUB_SIGNED_ANON, LEVEL_ERROR,
                    f"комплект №{index} ({title[:60]}): подпись без ФИО и роли — подписью "
                    f"не является", submittal=index)
                item["signature"]["valid"] = False
            if decision and decision not in (DECISION_AGREED, DECISION_WITH_REMARKS,
                                             DECISION_RETURNED):
                add(CHECK_SUB_STATUS, LEVEL_WARNING,
                    f"комплект №{index}: решение «{decision}» не из списка "
                    f"({DECISION_AGREED} / {DECISION_WITH_REMARKS} / {DECISION_RETURNED})",
                    submittal=index)
            if open_remarks:
                add(CHECK_SUB_SIGNED_OPEN, LEVEL_ERROR,
                    f"комплект №{index} ({title[:60]}): подписан, а открытых замечаний "
                    f"{open_remarks} — подпись при невыполненных замечаниях недействительна",
                    submittal=index)
                status = SUB_SIGNED_WITH_REMARKS
                item["signature"]["valid"] = False
            elif status != SUB_SIGNED_WITH_REMARKS and item["signature"].get("valid") is not False:
                status = SUB_SIGNED
                item["signature"]["valid"] = True

        item["status"] = status
        item["open_remarks"] = open_remarks
        item["remarks_total"] = len(item["remarks"])
        items.append(item)

    if not submittals:
        add(CHECK_SUB_EMPTY, LEVEL_ERROR, "в реестре нет ни одной передачи ИД на согласование")

    registry = {
        "schema": SCHEMA_SUBMITTAL, "kind": "submittals",
        "title": "Реестр согласования исполнительной документации",
        "number": number, "built_at": today.isoformat(), "object_name": object_name,
        "status": "черновик", "confirmed_by": None,
        "submittals": items, "rejected": rejected, "checks": checks,
        "totals": {"submittals": len(items), "rejected": len(rejected),
                   "signed": len([i for i in items if i["status"] == SUB_SIGNED]),
                   "with_open_remarks": len([i for i in items if i["open_remarks"]]),
                   "remarks": sum(len(i["remarks"]) for i in items),
                   "overdue": len([c for c in checks if c["code"] == CHECK_SUB_OVERDUE]),
                   "errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
                   "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING])},
        "disclaimer": ("Реестр показывает, на какой стадии комплект ИД: передан, замечания, "
                       "устранение, повторная передача, подпись. Согласование и подпись оформляет "
                       "человек; агент проверяет, что подпись не появилась раньше замечаний."),
    }
    registry["can_close"] = can_close_submittals(registry)
    return registry


def can_close_submittals(registry: dict) -> dict:
    reasons: list[str] = []
    errors = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_ERROR]
    if errors:
        reasons.append(f"ошибок: {len(errors)} — устранить до сдачи комплекта")
    unsigned = [i["title"] or f"комплект №{i['no']}" for i in registry.get("submittals") or []
                if i["status"] != SUB_SIGNED]
    if unsigned:
        reasons.append("комплектов без подписи: " + ", ".join(str(x)[:40] for x in unsigned))
    open_remarks = sum(i.get("open_remarks") or 0 for i in registry.get("submittals") or [])
    if open_remarks:
        reasons.append(f"открытых замечаний: {open_remarks}")
    if not registry.get("submittals"):
        reasons.append("в реестре нет ни одного комплекта")
    return {"allowed": not reasons, "reasons": reasons}


def sign(document: dict, *, by: str, role: str, decision: str = "", reason: str = "",
         audit_log=None, today: date | None = None) -> dict:
    """Подписать реестр (РД или согласование). Без ФИО и роли, с ошибками — отказ."""
    today = today or date.today()
    document = json.loads(json.dumps(document))
    problems: list[str] = []
    if not str(by or "").strip():
        problems.append("анонимная подпись: не указано ФИО ответственного")
    if not str(role or "").strip():
        problems.append("не указана роль (должность) ответственного")
    errors = [c for c in document.get("checks") or [] if c["level"] == LEVEL_ERROR]
    if errors:
        problems.append(f"ошибок в реестре: {len(errors)} — {errors[0]['message'][:120]}")
    if document.get("schema") == SCHEMA_SUBMITTAL:
        open_remarks = sum(i.get("open_remarks") or 0
                           for i in document.get("submittals") or [])
        if open_remarks:
            problems.append(f"открытых замечаний: {open_remarks} — комплект не подписывается")
    if problems:
        document["sign_problems"] = problems
        return document
    document["confirmed_by"] = {"by": str(by).strip(), "role": str(role).strip(),
                                "decision": str(decision or ""), "reason": str(reason or ""),
                                "at": today.isoformat()}
    document["confirmed_by"]["content_fingerprint"] = content_fingerprint(document)
    document.pop("sign_problems", None)
    document["status"] = "подписан"
    if document.get("schema") == SCHEMA_RD:
        document["can_work"] = can_work(document)
    else:
        document["can_close"] = can_close_submittals(document)
    if audit_log is not None:
        audit_log.append("agent:docflow", "agent", "docflow.signed",
                         f"{document.get('schema')}:{document.get('number') or 'б/н'}",
                         {"by": document["confirmed_by"]["by"],
                          "role": document["confirmed_by"]["role"]})
    return document


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(document: dict) -> str:
    if document.get("schema") == SCHEMA_SUBMITTAL:
        return _submittals_markdown(document)
    return _rd_markdown(document)


def _rd_markdown(registry: dict) -> str:
    lines = [f"# {registry['title']}", "",
             f"**Объект:** {registry.get('object_name') or '—'} · "
             f"**реестр:** {registry.get('number') or 'б/н'} · собран: {registry.get('built_at')}",
             ""]
    totals = registry["totals"]
    lines += [f"Передач: {totals['transmittals']} · отклонено: {totals['rejected']} · "
              f"документов: {totals['documents']} · со штампом «В производство работ»: "
              f"{totals['with_stamp']} · без подписи получившего: {totals['without_receipt']}", "",
              f"**Статус:** {registry.get('status')}", ""]
    close = registry.get("can_work") or {}
    if close.get("allowed"):
        lines += ["Документация для производства работ есть: все передачи со штампом и подписью, "
                  "редакции действующие.", ""]
    else:
        lines += ["**Работать по этой документации нельзя:**", ""] + \
                 [f"- {r}" for r in close.get("reasons") or []] + [""]
    errors = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_ERROR]
    warnings = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_WARNING]
    if errors:
        lines += ["## Ошибки", ""] + [f"- **{c['code']}** — {c['message']}" for c in errors] + [""]
    if warnings:
        lines += ["## Требует решения", ""] + [f"- {c['code']} — {c['message']}"
                                               for c in warnings] + [""]
    lines += ["## Передачи", "",
              "| № | Документ | Редакция | Дата | Получатель | Штамп | Подпись получившего |",
              "|---|---|---|---|---|---|---|"]
    for row in registry.get("transmittals") or []:
        receipt = row.get("receipt") or {}
        lines.append(f"| {row['no']} | {row.get('doc')} — {row.get('title') or ''} | "
                     f"{row.get('revision') or 'б/н'} | {row.get('date')} | "
                     f"{row.get('recipient')} | {row.get('stamp') or 'не указан'} | "
                     f"{(receipt.get('by') or 'нет')}"
                     f"{' от ' + receipt['at'] if receipt.get('at') else ''} |")
    lines.append("")
    if registry.get("documents"):
        lines += ["## Состояние документов", "",
                  "| Документ | Действующая редакция | Выдана | Статус |", "|---|---|---|---|"]
        for doc in registry["documents"]:
            lines.append(f"| {doc['doc']} | {doc.get('current_revision') or '—'} | "
                         f"{doc.get('issued_at') or '—'} | {doc['status']} |")
        lines.append("")
    if registry.get("rejected"):
        lines += ["## Передачи, не принятые в реестр", ""]
        lines += [f"- №{r['no']}: {r.get('why')}" for r in registry["rejected"]] + [""]
    if registry.get("confirmed_by"):
        who = registry["confirmed_by"]
        lines += [f"Подписал: {who.get('by')}, {who.get('role')} ({who.get('at')}).", ""]
    if registry.get("sign_problems"):
        lines += ["**Подпись не принята:**", ""] + [f"- {p}" for p in registry["sign_problems"]] + [""]
    lines += [f"> {registry.get('disclaimer')}", ""]
    return "\n".join(lines)


def _submittals_markdown(registry: dict) -> str:
    lines = [f"# {registry['title']}", "",
             f"**Объект:** {registry.get('object_name') or '—'} · "
             f"**реестр:** {registry.get('number') or 'б/н'} · собран: {registry.get('built_at')}",
             ""]
    totals = registry["totals"]
    lines += [f"Комплектов: {totals['submittals']} · подписано: {totals['signed']} · "
              f"с открытыми замечаниями: {totals['with_open_remarks']} · замечаний: "
              f"{totals['remarks']} (просрочено: {totals['overdue']})", "",
              f"**Статус:** {registry.get('status')}", ""]
    close = registry.get("can_close") or {}
    if close.get("allowed"):
        lines += ["Комплект можно сдавать: все подписаны, открытых замечаний нет.", ""]
    else:
        lines += ["**Сдавать нельзя:**", ""] + [f"- {r}" for r in close.get("reasons") or []] + [""]
    errors = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_ERROR]
    warnings = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_WARNING]
    if errors:
        lines += ["## Ошибки", ""] + [f"- **{c['code']}** — {c['message']}" for c in errors] + [""]
    if warnings:
        lines += ["## Требует решения", ""] + [f"- {c['code']} — {c['message']}"
                                               for c in warnings] + [""]
    lines += ["## Комплекты", "",
              "| № | Комплект | Акт | Передан | Получатель | Замечаний (открыто) | Подпись | Статус |",
              "|---|---|---|---|---|---|---|---|"]
    for item in registry.get("submittals") or []:
        signature = item.get("signature") or {}
        lines.append(f"| {item['no']} | {item.get('title')} | {item.get('act_ref') or '—'} | "
                     f"{item.get('sent_at')} | {item.get('recipient')} | "
                     f"{item['remarks_total']} ({item['open_remarks']}) | "
                     f"{(signature.get('by') or 'нет')}"
                     f"{' — ' + signature['decision'] if signature.get('decision') else ''} | "
                     f"{item['status']} |")
    lines.append("")
    for item in registry.get("submittals") or []:
        if not item.get("remarks"):
            continue
        lines += [f"### Замечания по комплекту №{item['no']} ({item.get('title')})", ""]
        for remark in item["remarks"]:
            lines.append(f"- {remark['no']}. {remark.get('text') or 'текст не записан'} · "
                         f"срок: {remark.get('due') or 'не указан'} · "
                         f"устранено: {remark.get('fixed_at') or 'нет'}")
        lines.append("")
    if registry.get("confirmed_by"):
        who = registry["confirmed_by"]
        lines += [f"Подписал: {who.get('by')}, {who.get('role')} ({who.get('at')}).", ""]
    if registry.get("sign_problems"):
        lines += ["**Подпись не принята:**", ""] + [f"- {p}" for p in registry["sign_problems"]] + [""]
    lines += [f"> {registry.get('disclaimer')}", ""]
    return "\n".join(lines)


def export_xlsx(document: dict, path: str | Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    fill = PatternFill("solid", fgColor="1F4E79")
    font = Font(color="FFFFFF", bold=True)

    def sheet(title, headers, widths, rows):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        for row in rows:
            ws.append(row)
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width
        ws.freeze_panes = "A2"

    wb.remove(wb.active)
    if document.get("schema") == SCHEMA_SUBMITTAL:
        sheet("Комплекты",
              ["№", "Комплект", "Акт", "Передан", "Получатель", "Замечаний", "Открыто",
               "Подпись", "Решение", "Статус"],
              [5, 34, 14, 12, 22, 11, 9, 20, 22, 34],
              [[i["no"], i.get("title") or "", i.get("act_ref") or "", i.get("sent_at") or "",
                i.get("recipient") or "", i["remarks_total"], i["open_remarks"],
                (i.get("signature") or {}).get("by") or "",
                (i.get("signature") or {}).get("decision") or "", i["status"]]
               for i in document.get("submittals") or []])
        sheet("Замечания", ["Комплект", "№", "Замечание", "Срок", "Устранено", "Статус"],
              [30, 5, 70, 12, 12, 12],
              [[i.get("title") or f"комплект {i['no']}", r["no"], r.get("text") or "",
                r.get("due") or "", r.get("fixed_at") or "", r["status"]]
               for i in document.get("submittals") or [] for r in i.get("remarks") or []])
    else:
        sheet("Передачи", ["№", "Документ", "Редакция", "Дата", "Получатель", "Штамп",
                           "Подпись получившего"],
              [5, 40, 10, 12, 24, 20, 24],
              [[r["no"], f"{r.get('doc')} — {r.get('title') or ''}", r.get("revision") or "",
                r.get("date") or "", r.get("recipient") or "", r.get("stamp") or "",
                (r.get("receipt") or {}).get("by") or ""]
               for r in document.get("transmittals") or []])
        sheet("Документы", ["Документ", "Действующая редакция", "Выдана", "Статус"],
              [40, 20, 14, 24],
              [[d["doc"], d.get("current_revision") or "", d.get("issued_at") or "", d["status"]]
               for d in document.get("documents") or []])
    sheet("Проверки", ["Уровень", "Код", "Что не так"],
          [10, 30, 90],
          [[("ошибка" if c["level"] == "error" else "внимание"), c["code"], c["message"]]
           for c in document.get("checks") or []])
    wb.save(path)
    return path


def write(document: dict, out_dir: str | Path, *, stem: str | None = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    kind = "submittals" if document.get("schema") == SCHEMA_SUBMITTAL else "rd"
    tail = stem or (str(document.get("number") or "").replace(" ", "_").replace("/", "-")
                    or "объект")
    base = out / f"{kind}_{tail}"
    (out / f"{kind}_{tail}.json").write_text(json.dumps(document, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    (out / f"{kind}_{tail}.md").write_text(to_markdown(document), encoding="utf-8")
    export_xlsx(document, out / f"{kind}_{tail}.xlsx")
    return {"json": str(base) + ".json", "markdown": str(base) + ".md",
            "xlsx": str(base) + ".xlsx"}
