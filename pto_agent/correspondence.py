# -*- coding: utf-8 -*-
"""Внешние участники и переписка: письма и запросы, лаборатории и геодезисты, авторские дни.

Три обязанности ПТО, которые проверяются одинаково — **наличие основания, адресата и ответа**:

* **Письмо или запрос без основания — это просьба «сделайте хорошо».** Поэтому у письма
  обязательны адресат, тема и основание: пункт договора, пункт нормы или другое письмо. Нет
  основания — `LETTER_NO_BASIS` (ошибка): предъявлять требование нечем. Нет ФИО и роли
  подписавшего — письмо не отправлено (`LETTER_NO_SIGNER`). Требование ответа ставит срок, и
  просроченный ответ — ошибка (`ANSWER_OVERDUE`), а не «ждём».
* **Лаборатория и геодезист работают в области своей аккредитации.** Поэтому у организации
  проверяются аттестат (номер, область, срок действия) и дата испытания: результат по
  просроченному аттестату на дату испытания силы не имеет (`ACCREDITATION_EXPIRED`), результат
  без привязки к партии или работе в комплект не ложится (`RESULT_UNLINKED`).
* **Авторский надзор и день качества — это протокол с замечаниями и сроками.** Мероприятие без
  участников — не мероприятие (`EVENT_NO_PARTICIPANTS`), замечание без ответственного и срока
  не исполняется (`EVENT_REMARK_NO_RESPONSIBLE`, `EVENT_REMARK_NO_DUE`), просроченное замечание
  — находка (`EVENT_REMARK_OVERDUE`).

Подпись реестра — только человеком с ФИО и ролью; правка после подписи её аннулирует.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .fingerprint import check_signature, content_fingerprint

SCHEMA_LETTERS = "pto-agent/letters@1"
SCHEMA_INVOLVEMENT = "pto-agent/involvement@1"
SCHEMA_QUALITY_DAYS = "pto-agent/quality-days@1"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

LETTER_KINDS = ("письмо", "запрос", "уведомление", "претензия", "согласование")
INVOLVEMENT_KINDS = ("лаборатория", "геодезист")
EVENT_KINDS = ("авторский надзор", "день качества")

CHECK_LETTER_NO_NUMBER = "LETTER_NO_NUMBER"
CHECK_LETTER_NO_DATE = "LETTER_NO_DATE"
CHECK_LETTER_NO_ADDRESSEE = "LETTER_NO_ADDRESSEE"
CHECK_LETTER_NO_SUBJECT = "LETTER_NO_SUBJECT"
CHECK_LETTER_NO_BASIS = "LETTER_NO_BASIS"
CHECK_LETTER_NO_SIGNER = "LETTER_NO_SIGNER"
CHECK_LETTER_KIND = "LETTER_KIND_UNKNOWN"
CHECK_LETTER_DUPLICATE = "LETTER_DUPLICATE_NUMBER"
CHECK_ANSWER_MISSING = "ANSWER_MISSING"
CHECK_ANSWER_OVERDUE = "ANSWER_OVERDUE"
CHECK_ANSWER_NO_REF = "ANSWER_WITHOUT_REFERENCE"
CHECK_LETTERS_EMPTY = "LETTERS_EMPTY"

CHECK_ORG_NO_ACCREDITATION = "NO_ACCREDITATION"
CHECK_ORG_ACCREDITATION_EXPIRED = "ACCREDITATION_EXPIRED"
CHECK_ORG_SCOPE_MISSING = "SCOPE_NOT_STATED"
CHECK_REQUEST_NO_RESULT = "RESULT_MISSING"
CHECK_REQUEST_OVERDUE = "RESULT_OVERDUE"
CHECK_RESULT_UNLINKED = "RESULT_UNLINKED"
CHECK_REQUEST_DUPLICATE = "DUPLICATE_REQUEST"
CHECK_INVOLVEMENT_EMPTY = "INVOLVEMENT_EMPTY"

CHECK_EVENT_NO_PARTICIPANTS = "EVENT_NO_PARTICIPANTS"
CHECK_EVENT_NO_PROTOCOL = "EVENT_NO_PROTOCOL"
CHECK_EVENT_KIND = "EVENT_KIND_UNKNOWN"
CHECK_EVENT_REMARK_NO_RESPONSIBLE = "EVENT_REMARK_NO_RESPONSIBLE"
CHECK_EVENT_REMARK_NO_DUE = "EVENT_REMARK_NO_DUE"
CHECK_EVENT_REMARK_OVERDUE = "EVENT_REMARK_OVERDUE"
CHECK_EVENTS_EMPTY = "QUALITY_DAYS_EMPTY"


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


def _envelope(schema: str, kind: str, title: str, *, object_name: str, number: str,
              today: date, checks: list[dict], disclaimer: str) -> dict:
    return {"schema": schema, "kind": kind, "title": title, "number": number,
            "built_at": today.isoformat(), "object_name": object_name,
            "status": "черновик", "confirmed_by": None, "checks": checks,
            "totals": {"errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
                       "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING])},
            "disclaimer": disclaimer}


# ─────────────────────────── письма и запросы ───────────────────────────
def build_letter_registry(letters: list[dict], *, object_name: str = "", number: str = "",
                          today: date | None = None) -> dict:
    """Реестр исходящих писем и запросов: адресат, основание, подпись, срок ответа."""
    today = today or date.today()
    checks: list[dict] = []
    items: list[dict] = []
    rejected: list[dict] = []
    seen: dict[str, int] = {}

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    for index, raw in enumerate(letters or [], start=1):
        letter_no = str(raw.get("number") or raw.get("no") or "").strip()
        day = _parse_date(raw.get("date"))
        kind = _clean(raw.get("kind")) or "письмо"
        to = raw.get("to") or {}
        addressee = str((to or {}).get("org") or (to or {}).get("organization")
                        or to if isinstance(to, str) else (to or {}).get("org") or "").strip()
        subject = str(raw.get("subject") or raw.get("title") or "").strip()
        basis = raw.get("basis")
        basis_text = ""
        if isinstance(basis, dict):
            basis_text = " ".join(str(basis.get(k) or "") for k in ("doc", "clause", "ref", "text")
                                  ).strip()
        else:
            basis_text = str(basis or "").strip()
        signer = raw.get("signed_by") or raw.get("signer") or {}
        signer_by = str((signer or {}).get("by") or "").strip()
        signer_role = str((signer or {}).get("role") or "").strip()
        requires = bool(raw.get("requires_answer"))
        due = _parse_date(raw.get("due_date") or raw.get("answer_due"))
        answer = raw.get("answer") or {}

        item = {"no": index, "number": letter_no or None, "date": day.isoformat() if day else None,
                "kind": kind, "addressee": addressee or None, "subject": subject or None,
                "basis": basis_text or None, "requires_answer": requires,
                "due_date": due.isoformat() if due else None,
                "signed_by": ({"by": signer_by, "role": signer_role}
                              if signer_by or signer_role else None),
                "answer": None, "status": None}

        missing = [name for name, ok in (("номер", bool(letter_no)), ("дату", bool(day)),
                                        ("адресата", bool(addressee))) if not ok]
        if missing:
            item["why"] = f"не указано: {', '.join(missing)}"
            rejected.append(item)
            add(CHECK_LETTER_NO_NUMBER if not letter_no else
                (CHECK_LETTER_NO_DATE if not day else CHECK_LETTER_NO_ADDRESSEE), LEVEL_ERROR,
                f"письмо №{index}: {item['why']} — в реестр не попадает", letter=index)
            continue

        if kind not in LETTER_KINDS:
            add(CHECK_LETTER_KIND, LEVEL_WARNING,
                f"{letter_no} от {item['date']}: вид «{raw.get('kind')}» не из списка "
                f"({', '.join(LETTER_KINDS)}) — учтено как письмо", letter=index)
            item["kind"] = "письмо"
        if not subject:
            add(CHECK_LETTER_NO_SUBJECT, LEVEL_WARNING,
                f"{letter_no}: тема не указана — по реестру не понять, о чём письмо", letter=index)
        if not basis_text:
            add(CHECK_LETTER_NO_BASIS, LEVEL_ERROR,
                f"{letter_no} от {item['date']}: нет основания (пункт договора, норма, номер "
                f"письма) — требование предъявлять нечем", letter=index)
        if not (signer_by and signer_role):
            add(CHECK_LETTER_NO_SIGNER, LEVEL_ERROR,
                f"{letter_no} от {item['date']}: нет ФИО и роли подписавшего — письмо не "
                f"считается отправленным", letter=index)
        key = _clean(letter_no)
        if key in seen:
            add(CHECK_LETTER_DUPLICATE, LEVEL_WARNING,
                f"{letter_no}: номер уже есть у письма №{seen[key]} — два письма под одним "
                f"номером", letter=index)
        else:
            seen[key] = index

        if answer:
            answer_no = str((answer or {}).get("number") or "").strip()
            answer_day = _parse_date(answer.get("date"))
            item["answer"] = {"number": answer_no or None,
                              "date": answer_day.isoformat() if answer_day else None,
                              "summary": str(answer.get("summary") or "").strip() or None}
            if not (answer_no and answer_day):
                add(CHECK_ANSWER_NO_REF, LEVEL_WARNING,
                    f"{letter_no}: ответ без номера или даты — сослаться на него нельзя",
                    letter=index)
        if requires:
            if not due:
                add(CHECK_ANSWER_MISSING, LEVEL_WARNING,
                    f"{letter_no}: требуется ответ, но срок ответа не указан", letter=index)
            if not answer:
                if due and due < today:
                    add(CHECK_ANSWER_OVERDUE, LEVEL_ERROR,
                        f"{letter_no} ({item['kind']}, {subject[:50]}): срок ответа "
                        f"{due.isoformat()} прошёл, ответа нет", letter=index)
                else:
                    add(CHECK_ANSWER_MISSING, LEVEL_WARNING,
                        f"{letter_no} ({item['kind']}, {subject[:50]}): ответа пока нет"
                        + (f", срок {due.isoformat()}" if due else ""), letter=index)
        item["status"] = ("ответ получен" if item["answer"] else
                          ("ждём ответа" if requires else "исходящее, ответ не требуется"))
        items.append(item)

    if not letters:
        add(CHECK_LETTERS_EMPTY, LEVEL_ERROR, "в реестре нет ни одного письма")

    registry = _envelope(SCHEMA_LETTERS, "letters", "Реестр писем и запросов",
                         object_name=object_name, number=number, today=today, checks=checks,
                         disclaimer=("Реестр показывает, на какое основание опирается каждое "
                                     "письмо, кто его подписал и есть ли ответ в срок. "
                                     "Письма готовит и подписывает человек."))
    registry.update({"letters": items, "rejected": rejected})
    registry["totals"].update({
        "letters": len(items), "rejected": len(rejected),
        "require_answer": len([i for i in items if i["requires_answer"]]),
        "answered": len([i for i in items if i["answer"]]),
        "awaiting": len([i for i in items if i["requires_answer"] and not i["answer"]]),
        "without_basis": len([i for i in items if not i["basis"]]),
    })
    registry["open_requests"] = [
        {"letter": i["number"], "subject": i["subject"], "due_date": i["due_date"],
         "overdue": bool(i["due_date"] and _parse_date(i["due_date"]) < today)}
        for i in items if i["requires_answer"] and not i["answer"]]
    return registry


# ─────────────────────────── лаборатории и геодезисты ───────────────────────────
def build_involvement_registry(records: list[dict], *, object_name: str = "", number: str = "",
                               today: date | None = None) -> dict:
    """Заявки в лаборатории и геодезистам: аккредитация, срок действия, привязка результата."""
    today = today or date.today()
    checks: list[dict] = []
    items: list[dict] = []
    rejected: list[dict] = []
    seen: dict[tuple, int] = {}

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    for index, raw in enumerate(records or [], start=1):
        kind = _clean(raw.get("kind"))
        org = str(raw.get("organization") or raw.get("org") or "").strip()
        attestation = raw.get("attestation") or raw.get("accreditation") or {}
        att_no = str((attestation or {}).get("number") or "").strip()
        att_scope = str((attestation or {}).get("scope") or "").strip()
        att_until = _parse_date((attestation or {}).get("valid_until"))
        request = raw.get("request") or {}
        req_no = str((request or {}).get("number") or "").strip()
        req_day = _parse_date((request or {}).get("date"))
        req_scope = str((request or {}).get("scope") or "").strip()
        req_due = _parse_date((request or {}).get("due"))
        result = raw.get("result") or {}
        res_no = str((result or {}).get("number") or "").strip()
        res_day = _parse_date((result or {}).get("date"))
        res_link = str((result or {}).get("linked_to") or raw.get("linked_to") or "").strip()

        item = {"no": index, "kind": kind or None, "organization": org or None,
                "attestation": {"number": att_no or None, "scope": att_scope or None,
                                "valid_until": att_until.isoformat() if att_until else None},
                "request": {"number": req_no or None,
                            "date": req_day.isoformat() if req_day else None,
                            "scope": req_scope or None,
                            "due": req_due.isoformat() if req_due else None},
                "result": None, "status": None}

        missing = [name for name, ok in (("вид работ (лаборатория/геодезист)", bool(kind)),
                                        ("организацию", bool(org)),
                                        ("номер заявки", bool(req_no))) if not ok]
        if missing:
            item["why"] = f"не указано: {', '.join(missing)}"
            rejected.append(item)
            add(CHECK_REQUEST_NO_RESULT, LEVEL_ERROR,
                f"заявка №{index} ({org or 'организация не указана'}): {item['why']} — "
                f"в реестр не попадает", record=index)
            continue
        if kind not in INVOLVEMENT_KINDS:
            add(CHECK_EVENT_KIND, LEVEL_WARNING,
                f"заявка №{index} ({req_no}): вид «{raw.get('kind')}» не из списка "
                f"(лаборатория, геодезист)", record=index)
        if not att_no:
            add(CHECK_ORG_NO_ACCREDITATION, LEVEL_ERROR,
                f"{org} (заявка {req_no}): не указан аттестат аккредитации — результат "
                f"без аккредитации в комплект ИД не принимается", record=index)
        if not att_scope:
            add(CHECK_ORG_SCOPE_MISSING, LEVEL_WARNING,
                f"{org} (заявка {req_no}): область аккредитации не указана — проверить, "
                f"входит ли работа «{req_scope or 'не указана'}» в область", record=index)

        key = (_clean(org), _clean(req_no))
        if key in seen:
            add(CHECK_REQUEST_DUPLICATE, LEVEL_WARNING,
                f"{org}: заявка {req_no} уже есть под №{seen[key]} — дубль заявки", record=index)
        else:
            seen[key] = index

        if result:
            item["result"] = {"number": res_no or None,
                              "date": res_day.isoformat() if res_day else None,
                              "kind": str((result or {}).get("kind") or "").strip() or None,
                              "linked_to": res_link or None}
            check_day = res_day or req_day
            if att_until and check_day and att_until < check_day:
                add(CHECK_ORG_ACCREDITATION_EXPIRED, LEVEL_ERROR,
                    f"{org} (заявка {req_no}): аттестат {att_no} действовал до "
                    f"{att_until.isoformat()}, а работа выполнена {check_day.isoformat()} — "
                    f"результат на дату работы силы не имеет", record=index)
                item["result"]["valid"] = False
            elif att_until:
                item["result"]["valid"] = True
            if not res_link:
                add(CHECK_RESULT_UNLINKED, LEVEL_WARNING,
                    f"{org} (заявка {req_no}): результат {res_no or 'без номера'} не привязан "
                    f"к партии, работе или акту — в комплект его не положить", record=index)
            item["status"] = "результат получен"
        else:
            if req_due and req_due < today:
                add(CHECK_REQUEST_OVERDUE, LEVEL_ERROR,
                    f"{org} (заявка {req_no}): срок {req_due.isoformat()} прошёл, результата нет",
                    record=index)
                item["status"] = "просрочено"
            else:
                add(CHECK_REQUEST_NO_RESULT, LEVEL_WARNING,
                    f"{org} (заявка {req_no}): результат ещё не получен"
                    + (f", срок {req_due.isoformat()}" if req_due else ""), record=index)
                item["status"] = "в работе"
        items.append(item)

    if not records:
        add(CHECK_INVOLVEMENT_EMPTY, LEVEL_ERROR, "в реестре нет ни одной заявки")

    registry = _envelope(SCHEMA_INVOLVEMENT, "involvement",
                         "Реестр привлечения лабораторий и геодезистов",
                         object_name=object_name, number=number, today=today, checks=checks,
                         disclaimer=("Реестр показывает, по какому аттестату и в какой области "
                                     "работала лаборатория или геодезист и к чему привязан "
                                     "результат. Привлечение оформляет человек."))
    registry.update({"records": items, "rejected": rejected})
    registry["totals"].update({
        "records": len(items), "rejected": len(rejected),
        "laboratories": len([i for i in items if i["kind"] == "лаборатория"]),
        "geodesists": len([i for i in items if i["kind"] == "геодезист"]),
        "with_result": len([i for i in items if i["result"]]),
        "without_accreditation": len([c for c in checks
                                      if c["code"] == CHECK_ORG_NO_ACCREDITATION]),
        "expired_accreditation": len([c for c in checks
                                      if c["code"] == CHECK_ORG_ACCREDITATION_EXPIRED]),
    })
    registry["can_close"] = can_close(registry)
    return registry


# ─────────────────────────── авторские дни и дни качества ───────────────────────────
def build_quality_days(events: list[dict], *, object_name: str = "", number: str = "",
                       today: date | None = None) -> dict:
    """Авторский надзор и дни качества: участники, протокол, замечания со сроками."""
    today = today or date.today()
    checks: list[dict] = []
    items: list[dict] = []
    rejected: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    for index, raw in enumerate(events or [], start=1):
        kind = _clean(raw.get("kind"))
        day = _parse_date(raw.get("date"))
        participants = raw.get("participants") or []
        protocol = raw.get("protocol") or {}
        proto_no = str((protocol or {}).get("number") or "").strip()
        proto_day = _parse_date((protocol or {}).get("date"))

        item = {"no": index, "kind": kind or None, "date": day.isoformat() if day else None,
                "participants": [], "protocol": None, "remarks": [], "open_remarks": 0,
                "status": None}

        missing = [name for name, ok in (("вид мероприятия", bool(kind)), ("дату", bool(day)),
                                        ("участников", bool(participants))) if not ok]
        if missing:
            item["why"] = f"не указано: {', '.join(missing)}"
            rejected.append(item)
            add(CHECK_EVENT_NO_PARTICIPANTS, LEVEL_ERROR,
                f"мероприятие №{index}: {item['why']} — в реестр не попадает", event=index)
            continue

        if kind not in EVENT_KINDS:
            add(CHECK_EVENT_KIND, LEVEL_WARNING,
                f"мероприятие №{index} от {item['date']}: вид «{raw.get('kind')}» не из списка "
                f"({', '.join(EVENT_KINDS)})", event=index)
        for participant in participants:
            org = str((participant or {}).get("org") or "").strip()
            by = str((participant or {}).get("by") or "").strip()
            role = str((participant or {}).get("role") or "").strip()
            item["participants"].append({"org": org or None, "by": by or None,
                                        "role": role or None})
            if not (by and role):
                add(CHECK_EVENT_REMARK_NO_RESPONSIBLE, LEVEL_WARNING,
                    f"мероприятие №{index}: участник {org or 'без организации'} без ФИО и роли — "
                    f"участие не подтверждено", event=index)

        if proto_no or proto_day:
            item["protocol"] = {"number": proto_no or None,
                                "date": proto_day.isoformat() if proto_day else None}
        else:
            add(CHECK_EVENT_NO_PROTOCOL, LEVEL_WARNING,
                f"мероприятие №{index} ({kind}) от {item['date']}: протокола нет — замечания "
                f"и решения не зафиксированы", event=index)

        for remark_no, remark in enumerate(raw.get("remarks") or [], start=1):
            text = str((remark or {}).get("text") or "").strip()
            responsible = str((remark or {}).get("responsible") or "").strip()
            due = _parse_date((remark or {}).get("due"))
            closed = _parse_date((remark or {}).get("closed_at"))
            item["remarks"].append({"no": remark_no, "text": text or None,
                                    "responsible": responsible or None,
                                    "due": due.isoformat() if due else None,
                                    "closed_at": closed.isoformat() if closed else None,
                                    "status": "закрыто" if closed else "открыто"})
            if not responsible:
                add(CHECK_EVENT_REMARK_NO_RESPONSIBLE, LEVEL_WARNING,
                    f"мероприятие №{index}, замечание {remark_no}: не указан ответственный — "
                    f"исполнять некому", event=index, remark=remark_no)
            if not due:
                add(CHECK_EVENT_REMARK_NO_DUE, LEVEL_WARNING,
                    f"мероприятие №{index}, замечание {remark_no}: срок не указан — "
                    f"просрочку не увидеть", event=index, remark=remark_no)
            elif not closed and due < today:
                add(CHECK_EVENT_REMARK_OVERDUE, LEVEL_ERROR,
                    f"мероприятие №{index}, замечание {remark_no}: срок {due.isoformat()} "
                    f"прошёл, замечание не закрыто", event=index, remark=remark_no)
            if not closed:
                item["open_remarks"] += 1
        item["status"] = ("замечания закрыты" if item["remarks"] and not item["open_remarks"]
                          else ("есть открытые замечания" if item["open_remarks"] else "без замечаний"))
        items.append(item)

    if not events:
        add(CHECK_EVENTS_EMPTY, LEVEL_ERROR, "в реестре нет ни одного мероприятия")

    registry = _envelope(SCHEMA_QUALITY_DAYS, "quality_days",
                         "Авторские дни и дни качества",
                         object_name=object_name, number=number, today=today, checks=checks,
                         disclaimer=("Реестр показывает, что зафиксировано протоколом авторского "
                                     "надзора или дня качества, кто отвечает за замечание и "
                                     "закрыто ли оно в срок."))
    registry.update({"events": items, "rejected": rejected})
    registry["totals"].update({
        "events": len(items), "rejected": len(rejected),
        "author_supervision": len([i for i in items if i["kind"] == "авторский надзор"]),
        "quality_days": len([i for i in items if i["kind"] == "день качества"]),
        "remarks": sum(len(i["remarks"]) for i in items),
        "open_remarks": sum(i["open_remarks"] for i in items),
        "overdue": len([c for c in checks if c["code"] == CHECK_EVENT_REMARK_OVERDUE]),
    })
    registry["can_close"] = can_close(registry)
    return registry


def can_close(registry: dict) -> dict:
    """Можно ли считать направление закрытым (для реестров заявок и мероприятий)."""
    reasons: list[str] = []
    errors = [c for c in registry.get("checks") or [] if c["level"] == LEVEL_ERROR]
    if errors:
        reasons.append(f"ошибок: {len(errors)} — сначала устранить")
    if registry.get("schema") == SCHEMA_INVOLVEMENT:
        pending = [i for i in registry.get("records") or [] if not i.get("result")]
        if pending:
            reasons.append(f"заявок без результата: {len(pending)}")
    if registry.get("schema") == SCHEMA_QUALITY_DAYS:
        open_remarks = sum(i.get("open_remarks") or 0 for i in registry.get("events") or [])
        if open_remarks:
            reasons.append(f"открытых замечаний: {open_remarks}")
    return {"allowed": not reasons, "reasons": reasons}


def sign(document: dict, *, by: str, role: str, reason: str = "", audit_log=None,
         today: date | None = None) -> dict:
    """Подписать реестр. Без ФИО и роли, с ошибками — отказ."""
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
    if problems:
        document["sign_problems"] = problems
        return document
    document["confirmed_by"] = {"by": str(by).strip(), "role": str(role).strip(),
                                "reason": str(reason or ""), "at": today.isoformat()}
    document["confirmed_by"]["content_fingerprint"] = content_fingerprint(document)
    document.pop("sign_problems", None)
    document["status"] = "подписан"
    document["can_close"] = can_close(document)
    if audit_log is not None:
        audit_log.append("agent:correspondence", "agent", "correspondence.signed",
                         f"{document.get('schema')}:{document.get('number') or 'б/н'}",
                         {"by": document["confirmed_by"]["by"],
                          "role": document["confirmed_by"]["role"]})
    return document


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(document: dict) -> str:
    lines = [f"# {document['title']}", "",
             f"**Объект:** {document.get('object_name') or '—'} · "
             f"**реестр:** {document.get('number') or 'б/н'} · собран: {document.get('built_at')}",
             ""]
    totals = document["totals"]
    if document["schema"] == SCHEMA_LETTERS:
        lines += [f"Писем: {totals['letters']} · отклонено: {totals['rejected']} · требуют "
                  f"ответа: {totals['require_answer']} · получено ответов: {totals['answered']} · "
                  f"ждём: {totals['awaiting']} · без основания: {totals['without_basis']}", ""]
    elif document["schema"] == SCHEMA_INVOLVEMENT:
        lines += [f"Заявок: {totals['records']} · лабораторий: {totals['laboratories']} · "
                  f"геодезистов: {totals['geodesists']} · с результатом: "
                  f"{totals['with_result']} · без аккредитации: "
                  f"{totals['without_accreditation']}", ""]
    else:
        lines += [f"Мероприятий: {totals['events']} · авторских дней: "
                  f"{totals['author_supervision']} · дней качества: {totals['quality_days']} · "
                  f"замечаний: {totals['remarks']} (открыто: {totals['open_remarks']}, "
                  f"просрочено: {totals['overdue']})", ""]
    lines += [f"**Статус:** {document.get('status')}", ""]
    close = document.get("can_close") or {}
    if close.get("reasons"):
        lines += ["**Закрывать нельзя:**", ""] + [f"- {r}" for r in close["reasons"]] + [""]

    errors = [c for c in document.get("checks") or [] if c["level"] == LEVEL_ERROR]
    warnings = [c for c in document.get("checks") or [] if c["level"] == LEVEL_WARNING]
    if errors:
        lines += ["## Ошибки", ""] + [f"- **{c['code']}** — {c['message']}" for c in errors] + [""]
    if warnings:
        lines += ["## Требует решения", ""] + [f"- {c['code']} — {c['message']}"
                                               for c in warnings] + [""]

    if document["schema"] == SCHEMA_LETTERS:
        lines += ["## Письма", "",
                  "| № | Номер | Дата | Вид | Адресат | Основание | Подписал | Ответ | Статус |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for item in document.get("letters") or []:
            signer = item.get("signed_by") or {}
            answer = item.get("answer") or {}
            lines.append(f"| {item['no']} | {item.get('number')} | {item.get('date')} | "
                         f"{item.get('kind')} | {item.get('addressee')} | "
                         f"{(item.get('basis') or 'нет')[:60]} | "
                         f"{signer.get('by') or '—'}, {signer.get('role') or '—'} | "
                         f"{answer.get('number') or '—'} | {item.get('status')} |")
        lines.append("")
        if document.get("open_requests"):
            lines += ["## Ждём ответа", ""]
            for row in document["open_requests"]:
                mark = " (просрочено)" if row["overdue"] else ""
                lines.append(f"- {row['letter']}: {row.get('subject') or '—'} · "
                             f"срок {row.get('due_date') or 'не указан'}{mark}")
            lines.append("")
    elif document["schema"] == SCHEMA_INVOLVEMENT:
        lines += ["## Заявки", "",
                  "| № | Вид | Организация | Аттестат | Действует до | Заявка | Результат | "
                  "Привязан к | Статус |", "|---|---|---|---|---|---|---|---|---|"]
        for item in document.get("records") or []:
            att = item.get("attestation") or {}
            request = item.get("request") or {}
            result = item.get("result") or {}
            lines.append(f"| {item['no']} | {item.get('kind')} | {item.get('organization')} | "
                         f"{att.get('number') or 'нет'} | {att.get('valid_until') or '—'} | "
                         f"{request.get('number')} от {request.get('date')} | "
                         f"{result.get('number') or 'нет'} | {result.get('linked_to') or '—'} | "
                         f"{item.get('status')} |")
        lines.append("")
    else:
        lines += ["## Мероприятия", "",
                  "| № | Вид | Дата | Участники | Протокол | Замечаний (открыто) | Статус |",
                  "|---|---|---|---|---|---|---|"]
        for item in document.get("events") or []:
            protocol = item.get("protocol") or {}
            who = ", ".join(str(p.get("org") or p.get("by") or "—")
                            for p in item.get("participants") or [])
            lines.append(f"| {item['no']} | {item.get('kind')} | {item.get('date')} | {who} | "
                         f"{protocol.get('number') or 'нет'} | {len(item['remarks'])} "
                         f"({item['open_remarks']}) | {item.get('status')} |")
        lines.append("")
        for item in document.get("events") or []:
            if not item.get("remarks"):
                continue
            lines += [f"### Замечания: {item.get('kind')} от {item.get('date')}", ""]
            for remark in item["remarks"]:
                lines.append(f"- {remark['no']}. {remark.get('text') or 'текст не записан'} · "
                             f"ответственный: {remark.get('responsible') or 'не указан'} · "
                             f"срок: {remark.get('due') or 'не указан'} · "
                             f"закрыто: {remark.get('closed_at') or 'нет'}")
            lines.append("")

    if document.get("rejected"):
        lines += ["## Не принято в реестр", ""]
        lines += [f"- №{r['no']}: {r.get('why')}" for r in document["rejected"]] + [""]
    if document.get("confirmed_by"):
        who = document["confirmed_by"]
        lines += [f"Подписал: {who.get('by')}, {who.get('role')} ({who.get('at')}).", ""]
    if document.get("sign_problems"):
        lines += ["**Подпись не принята:**", ""] + [f"- {p}" for p in document["sign_problems"]] + [""]
    lines += [f"> {document.get('disclaimer')}", ""]
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
    if document["schema"] == SCHEMA_LETTERS:
        sheet("Письма",
              ["№", "Номер", "Дата", "Вид", "Адресат", "Тема", "Основание", "Подписал", "Роль",
               "Требует ответа", "Срок", "Ответ", "Статус"],
              [5, 12, 12, 14, 24, 40, 34, 18, 20, 13, 12, 14, 24],
              [[i["no"], i.get("number"), i.get("date"), i.get("kind"), i.get("addressee"),
                i.get("subject") or "", i.get("basis") or "",
                (i.get("signed_by") or {}).get("by") or "",
                (i.get("signed_by") or {}).get("role") or "",
                "да" if i["requires_answer"] else "нет", i.get("due_date") or "",
                (i.get("answer") or {}).get("number") or "", i.get("status")]
               for i in document.get("letters") or []])
    elif document["schema"] == SCHEMA_INVOLVEMENT:
        sheet("Заявки",
              ["№", "Вид", "Организация", "Аттестат", "Область", "Действует до", "Заявка",
               "Дата заявки", "Срок", "Результат", "Дата результата", "Привязан к", "Статус"],
              [5, 14, 26, 18, 34, 12, 12, 12, 12, 14, 14, 26, 20],
              [[i["no"], i.get("kind"), i.get("organization"),
                (i.get("attestation") or {}).get("number") or "",
                (i.get("attestation") or {}).get("scope") or "",
                (i.get("attestation") or {}).get("valid_until") or "",
                (i.get("request") or {}).get("number") or "",
                (i.get("request") or {}).get("date") or "",
                (i.get("request") or {}).get("due") or "",
                (i.get("result") or {}).get("number") or "",
                (i.get("result") or {}).get("date") or "",
                (i.get("result") or {}).get("linked_to") or "", i.get("status")]
               for i in document.get("records") or []])
    else:
        sheet("Мероприятия",
              ["№", "Вид", "Дата", "Протокол", "Участники", "Замечаний", "Открыто", "Статус"],
              [5, 18, 12, 14, 40, 11, 9, 24],
              [[i["no"], i.get("kind"), i.get("date"), (i.get("protocol") or {}).get("number") or "",
                ", ".join(f"{p.get('org') or ''} {p.get('by') or ''}".strip()
                          for p in i.get("participants") or []),
                len(i["remarks"]), i["open_remarks"], i.get("status")]
               for i in document.get("events") or []])
        sheet("Замечания", ["Мероприятие", "№", "Замечание", "Ответственный", "Срок", "Закрыто",
                            "Статус"],
              [26, 5, 60, 22, 12, 12, 12],
              [[f"{i.get('kind')} от {i.get('date')}", r["no"], r.get("text") or "",
                r.get("responsible") or "", r.get("due") or "", r.get("closed_at") or "",
                r["status"]]
               for i in document.get("events") or [] for r in i.get("remarks") or []])
    sheet("Проверки", ["Уровень", "Код", "Что не так"], [10, 32, 90],
          [[("ошибка" if c["level"] == "error" else "внимание"), c["code"], c["message"]]
           for c in document.get("checks") or []])
    wb.save(path)
    return path


def write(document: dict, out_dir: str | Path, *, stem: str | None = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    kind = {SCHEMA_LETTERS: "letters", SCHEMA_INVOLVEMENT: "involvement",
            SCHEMA_QUALITY_DAYS: "quality_days"}[document["schema"]]
    tail = stem or (str(document.get("number") or "").replace(" ", "_").replace("/", "-")
                    or "объект")
    base = out / f"{kind}_{tail}"
    (out / f"{kind}_{tail}.json").write_text(json.dumps(document, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    (out / f"{kind}_{tail}.md").write_text(to_markdown(document), encoding="utf-8")
    export_xlsx(document, out / f"{kind}_{tail}.xlsx")
    return {"json": str(base) + ".json", "markdown": str(base) + ".md",
            "xlsx": str(base) + ".xlsx"}
