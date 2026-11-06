# -*- coding: utf-8 -*-
"""Журналы: общий журнал работ и журнал верификации закупленной продукции.

Оба журнала входят в комплект исполнительной документации (приказ Минстроя № 344/пр), и оба
ломаются одинаково — их **ведут задним числом и подписывают не глядя**. Поэтому правила здесь
проверяемые, а не «по форме»:

**Общий журнал работ**

* запись должна содержать дату, смену, исполнителя и описание работ; иначе она не попадает в
  журнал, а уходит в список замечаний (`ENTRY_INCOMPLETE`);
* вид работ определяется классификатором матрицы по тексту записи. Не определился — запись
  остаётся, но помечается `WORK_TYPE_UNKNOWN`: такая запись не даст комплект ИД, и это видно;
* даты идут по возрастанию: запись «задним числом» в конец — ошибка `OUT_OF_ORDER` (журнал
  ведётся в хронологии, а не по мере внесения);
* для работ, чувствительных к погоде (земляные, бетонные, кровельные), температура наружного
  воздуха обязательна (`WEATHER_MISSING`) — именно она объясняет, почему бетон набирал
  прочность иначе;
* дубль записи (та же дата, смена, вид работ и объём) — предупреждение `DUPLICATE_ENTRY`;
* объёмы по виду работ сверяются с актом КС-2, если акт передан: расхождение журнала и акта —
  `JOURNAL_VS_ACT` с обеими цифрами;
* журнал не «закрыт», пока его не подписал человек с ФИО и ролью (`can_close`), и правка
  записей после подписи аннулирует подпись.

**Журнал верификации закупленной продукции (входной контроль)**

* решение «допущен» невозможно без документа качества (сертификат, декларация, паспорт) —
  `ACCEPTED_WITHOUT_DOC`; это главное правило входного контроля;
* документ качества с истёкшим на дату записи сроком не даёт допуска (`CERT_EXPIRED`);
* решение только из списка: `допущен` / `не допущен` / `допущен условно` / `не проверено`;
  «ок», «да» не принимаются;
* у записи должен быть проверяющий (ФИО и роль) — `NO_INSPECTOR`;
* материал, списанный по акту, но не прошедший верификацию, — предупреждение
  `MATERIAL_WITHOUT_VERIFICATION`: списание есть, допуска нет.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .asbuilt.requirements import classify_work, load_matrix
from .fingerprint import check_signature, content_fingerprint
from .numeric import parse_number

SCHEMA_WORK = "pto-agent/journal-work@1"
SCHEMA_VERIFICATION = "pto-agent/journal-verification@1"

STATUS_DRAFT = "черновик"
STATUS_SIGNED = "подписан"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

DECISION_OK = "допущен"
DECISION_CONDITIONAL = "допущен условно"
DECISION_REJECTED = "не допущен"
DECISION_UNCHECKED = "не проверено"
DECISIONS = (DECISION_OK, DECISION_CONDITIONAL, DECISION_REJECTED, DECISION_UNCHECKED)

CHECK_ENTRY_INCOMPLETE = "ENTRY_INCOMPLETE"
CHECK_WORK_UNKNOWN = "WORK_TYPE_UNKNOWN"
CHECK_OUT_OF_ORDER = "OUT_OF_ORDER"
CHECK_WEATHER = "WEATHER_MISSING"
CHECK_DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
CHECK_BAD_NUMBER = "BAD_NUMBER"
CHECK_VS_ACT = "JOURNAL_VS_ACT"
CHECK_NO_SIGNATURE = "JOURNAL_UNSIGNED"

CHECK_ACCEPT_NO_DOC = "ACCEPTED_WITHOUT_DOC"
CHECK_CERT_EXPIRED = "CERT_EXPIRED"
CHECK_DECISION = "DECISION_UNKNOWN"
CHECK_NO_INSPECTOR = "NO_INSPECTOR"
CHECK_DUPLICATE_BATCH = "DUPLICATE_BATCH"
CHECK_MATERIAL_UNVERIFIED = "MATERIAL_WITHOUT_VERIFICATION"

# виды работ, для которых температура воздуха объясняет поведение материала
WEATHER_SENSITIVE = ("earthworks", "concrete", "roofing", "finishing", "waterproofing")


def _rub(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _qty(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")
    return text.rstrip("0").rstrip(",") if "," in text else text


def load_json(path: str | Path, key: str) -> tuple[list[dict], list[str]]:
    """Прочитать список записей журнала из JSON."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], [f"{path.name}: не читается ({exc.__class__.__name__})"]
    if isinstance(data, dict):
        data = data.get(key) or data.get("entries") or data.get("records") or []
    if not isinstance(data, list):
        return [], [f"{path.name}: ожидался список записей (ключ «{key}»)"]
    return data, []


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip().replace("/", ".").replace("-", ".")
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ─────────────────────────── общий журнал работ ───────────────────────────
def build_work_journal(entries: list[dict], *, object_name: str = "", number: str = "",
                       acts: dict | None = None, matrix=None, folder=None,
                       today: date | None = None) -> dict:
    """Собрать общий журнал работ из записей. Ничего не додумывает: чего нет — в замечания."""
    today = today or date.today()
    matrix = matrix or load_matrix()
    checks: list[dict] = []
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen: dict[tuple, int] = {}

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    for index, raw in enumerate(entries or [], start=1):
        text = str(raw.get("work") or raw.get("description") or "").strip()
        shift = str(raw.get("shift") or "").strip()
        executor = str(raw.get("executor") or "").strip()
        day = _parse_date(raw.get("date"))
        missing = [name for name, ok in (("дату", bool(day)), ("смену", bool(shift)),
                                         ("исполнителя", bool(executor)),
                                         ("описание работ", bool(text))) if not ok]
        record = {
            "no": index, "date": day.isoformat() if day else None, "shift": shift or None,
            "work": text or None, "executor": executor or None,
            "zone": str(raw.get("zone") or "").strip() or None,
            "unit": str(raw.get("unit") or "").strip() or None,
            "qty": parse_number(raw.get("qty")),
            "weather": raw.get("weather") or None,
            "supervision": str(raw.get("supervision") or "").strip() or None,
            "note": str(raw.get("note") or "").strip() or None,
        }
        if missing:
            record["why"] = f"не указано: {', '.join(missing)}"
            rejected.append(record)
            add(CHECK_ENTRY_INCOMPLETE, LEVEL_ERROR,
                f"запись №{index}: {record['why']} — в журнал не попадает", entry=index)
            continue
        if record["qty"] is None and raw.get("qty") not in (None, ""):
            add(CHECK_BAD_NUMBER, LEVEL_WARNING,
                f"запись №{index}: объём «{raw.get('qty')}» не число — запись сохранена без "
                f"объёма, сверка с актом по ней не считается", entry=index)

        hits = classify_work(text, matrix)
        record["work_type"] = hits[0]["work_type"] if hits else None
        record["work_type_name"] = hits[0]["name"] if hits else None
        if not hits:
            add(CHECK_WORK_UNKNOWN, LEVEL_WARNING,
                f"запись №{index} ({text[:60]}): вид работ не определён классификатором матрицы "
                f"— комплект ИД по такой записи не собрать", entry=index)
        elif record["work_type"] in WEATHER_SENSITIVE and not record["weather"]:
            add(CHECK_WEATHER, LEVEL_WARNING,
                f"запись №{index} ({record['work_type']}): не указана погода (температура "
                f"воздуха) — именно она объясняет поведение материала", entry=index)

        key = (record["date"], record["shift"], record["work_type"], record["qty"])
        if key in seen:
            add(CHECK_DUPLICATE_ENTRY, LEVEL_WARNING,
                f"запись №{index} повторяет запись №{seen[key]} (та же дата, смена, вид работ и "
                f"объём) — проверьте, не внесена ли дважды", entry=index)
        else:
            seen[key] = index
        accepted.append(record)

    # хронология: журнал ведётся по датам, а не по порядку внесения
    previous = None
    for record in accepted:
        if previous and record["date"] < previous["date"]:
            add(CHECK_OUT_OF_ORDER, LEVEL_ERROR,
                f"запись №{record['no']} ({record['date']}) идёт после записи "
                f"№{previous['no']} ({previous['date']}) — журнал ведётся в хронологии, "
                f"задним числом в конец не пишут", entry=record["no"])
        else:
            previous = record

    # объёмы журнала против акта КС-2
    by_type: dict[str, dict] = {}
    for record in accepted:
        if record["qty"] is None or not record["work_type"]:
            continue
        slot = by_type.setdefault(record["work_type"], {"qty": 0.0, "unit": record["unit"],
                                                        "entries": 0})
        slot["qty"] += record["qty"]
        slot["entries"] += 1
    act_compare: list[dict] = []
    journal_amount = None
    act_amount = None
    if acts:
        act_total = 0.0
        act_by_type: dict[str, dict] = {}
        for act in acts.get("acts") or []:
            positions = act.get("positions") or []
            act_sum = parse_number(act.get("amount_rub"))
            if act_sum is None:
                act_sum = 0.0
                for position in positions:
                    amount = parse_number(position.get("amount_rub"))
                    if amount is not None:
                        act_sum += amount
            act_total += act_sum
            # построчная сверка возможна только там, где в позиции акта есть наименование работ
            for position in positions:
                name = str(position.get("name") or position.get("наименование") or "")
                qty = parse_number(position.get("qty"))
                if not name or qty is None:
                    continue
                matched = classify_work(name, matrix)
                if not matched or not matched[0]["work_type"]:
                    continue
                slot = act_by_type.setdefault(matched[0]["work_type"],
                                              {"qty": 0.0, "unit": position.get("unit"),
                                               "lines": 0})
                slot["qty"] += qty
                slot["lines"] += 1
        act_amount = round(act_total, 2)
        journal_amount = None      # в журнале объёмы, а не деньги: сумму не выдумываем
        if not (acts.get("acts") or []):
            add(CHECK_VS_ACT, LEVEL_WARNING,
                "акты переданы, но актов в реестре нет — сверка журнала с актом не считалась")
        else:
            compared = sorted(set(by_type) | set(act_by_type))
            for work_type in compared:
                j_slot = by_type.get(work_type) or {"qty": 0.0, "unit": None, "entries": 0}
                a_slot = act_by_type.get(work_type) or {"qty": 0.0, "unit": None, "lines": 0}
                row = {"work_type": work_type, "journal_qty": round(j_slot["qty"], 3),
                       "journal_entries": j_slot["entries"], "act_qty": round(a_slot["qty"], 3),
                       "act_lines": a_slot["lines"], "unit": j_slot["unit"] or a_slot["unit"]}
                if a_slot["lines"]:
                    same_unit = (not j_slot["unit"]) or (not a_slot["unit"]) or \
                        j_slot["unit"] == a_slot["unit"]
                    row["delta_qty"] = round(j_slot["qty"] - a_slot["qty"], 3)
                    row["comparable"] = bool(same_unit)
                    if same_unit and abs(row["delta_qty"]) > 0.001:
                        add(CHECK_VS_ACT, LEVEL_WARNING,
                            f"{work_type}: по журналу {row['journal_qty']} {j_slot['unit'] or ''}, "
                            f"по акту {row['act_qty']} {a_slot['unit'] or ''} — расхождение "
                            f"{row['delta_qty']}; объём в акте объясняется ведомостью объёмов, "
                            f"а не записью журнала", work_type=work_type)
                else:
                    row["comparable"] = False
                    row["delta_qty"] = None
                act_compare.append(row)
            if not act_by_type:
                add(CHECK_VS_ACT, LEVEL_WARNING,
                    f"в позициях актов нет наименований работ — построчно сверить не с чем; "
                    f"объёмы журнала по {len(by_type)} видам работ сведены, сумма по актам "
                    f"{_rub(act_amount)} ₽ остаётся справочной")
            else:
                add(CHECK_VS_ACT, LEVEL_WARNING,
                    f"построчно сопоставлено видов работ: {len(act_by_type)} из "
                    f"{len(by_type)} в журнале; сумма по актам {_rub(act_amount)} ₽ — "
                    f"деньги журнал не подтверждает")

    journal = {
        "schema": SCHEMA_WORK,
        "kind": "journal_work",
        "title": "Общий журнал работ",
        "number": number,
        "built_at": today.isoformat(),
        "object_name": object_name,
        "status": STATUS_DRAFT,
        "confirmed_by": None,
        "entries": accepted,
        "rejected": rejected,
        "checks": checks,
        "by_work_type": [{"work_type": k, **{kk: (round(vv, 3) if isinstance(vv, float) else vv)
                                             for kk, vv in v.items()}}
                         for k, v in sorted(by_type.items())],
        "vs_act": {"act_amount_rub": act_amount, "by_work_type": act_compare} if acts else None,
        "totals": {
            "entries": len(accepted),
            "rejected": len(rejected),
            "shifts": len({(r["date"], r["shift"]) for r in accepted}),
            "work_types": len(by_type),
            "with_weather": len([r for r in accepted if r["weather"]]),
            "errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
            "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING]),
        },
        "disclaimer": ("Журнал фиксирует записи по сменам и проверяет их полноту и хронологию. "
                       "Он не подтверждает выполнение работ: закрывает журнал и подписывает "
                       "ответственный ИТР."),
    }
    journal["can_close"] = can_close(journal, SCHEMA_WORK)
    return journal


# ─────────────────────────── журнал верификации ───────────────────────────
def build_verification_journal(records: list[dict], *, object_name: str = "",
                               number: str = "", writeoffs: list[dict] | None = None,
                               acts: dict | None = None, today: date | None = None) -> dict:
    """Журнал входного контроля: партия → документ качества → решение → кто проверил."""
    today = today or date.today()
    checks: list[dict] = []
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen: dict[tuple, int] = {}

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    for index, raw in enumerate(records or [], start=1):
        material = str(raw.get("material") or raw.get("name") or "").strip()
        unit = str(raw.get("unit") or "").strip()
        qty = parse_number(raw.get("qty"))
        day = _parse_date(raw.get("date"))
        doc = raw.get("quality_doc") or {}
        doc_no = str(doc.get("number") or "").strip()
        doc_kind = str(doc.get("kind") or "").strip()
        doc_until = _parse_date(doc.get("valid_until"))
        decision = str(raw.get("decision") or "").strip().lower()
        inspector = str(raw.get("inspected_by") or "").strip()
        role = str(raw.get("role") or "").strip()

        record = {
            "no": index, "date": day.isoformat() if day else None, "material": material or None,
            "unit": unit or None, "qty": qty, "batch": str(raw.get("batch") or "").strip() or None,
            "supplier": str(raw.get("supplier") or "").strip() or None,
            "quality_doc": {"kind": doc_kind or None, "number": doc_no or None,
                            "valid_until": doc_until.isoformat() if doc_until else None},
            "decision": decision or None,
            "inspected_by": inspector or None, "role": role or None,
            "note": str(raw.get("note") or "").strip() or None,
        }
        missing = [name for name, ok in (("материал", bool(material)), ("дату", bool(day)),
                                         ("количество", qty is not None)) if not ok]
        if missing:
            record["why"] = f"не указано: {', '.join(missing)}"
            rejected.append(record)
            add(CHECK_ENTRY_INCOMPLETE, LEVEL_ERROR,
                f"запись №{index} ({material or 'материал не указан'}): {record['why']} — "
                f"во входной контроль не попадает", entry=index)
            continue

        key = (material.lower(), record["batch"], doc_no)
        if key in seen:
            add(CHECK_DUPLICATE_BATCH, LEVEL_WARNING,
                f"запись №{index}: партия {material} {record['batch'] or ''} с документом "
                f"{doc_no or '—'} уже проверялась (№{seen[key]}) — проверьте повтор", entry=index)
        else:
            seen[key] = index

        if decision not in DECISIONS:
            add(CHECK_DECISION, LEVEL_ERROR,
                f"запись №{index}: решение «{raw.get('decision')}» не из списка "
                f"{DECISIONS} — не принято", entry=index)
            record["decision"] = None

        if not inspector or not role:
            add(CHECK_NO_INSPECTOR, LEVEL_ERROR,
                f"запись №{index} ({material}): не указан проверяющий (ФИО и роль) — "
                f"входной контроль без ответственного не считается выполненным", entry=index)

        has_doc = bool(doc_no)
        if record["decision"] in (DECISION_OK, DECISION_CONDITIONAL) and not has_doc:
            record["acceptable"] = False
            add(CHECK_ACCEPT_NO_DOC, LEVEL_ERROR,
                f"запись №{index} ({material}): решение «{record['decision']}» без документа "
                f"качества — допуск невозможен: нет сертификата, декларации или паспорта",
                entry=index)
        elif record["decision"] in (DECISION_OK, DECISION_CONDITIONAL) and day and doc_until \
                and doc_until < day:
            record["acceptable"] = False
            add(CHECK_CERT_EXPIRED, LEVEL_ERROR,
                f"запись №{index} ({material}): документ качества {doc_no} действовал до "
                f"{doc_until.isoformat()}, а материал проверялся {day.isoformat()} — допуск по "
                f"просроченному документу недействителен", entry=index)
        elif record["decision"] == DECISION_OK and not doc_until:
            record["acceptable"] = True
            add(CHECK_CERT_EXPIRED, LEVEL_WARNING,
                f"запись №{index} ({material}): у документа качества {doc_no} не указан срок "
                f"действия — проверить по первоисточнику", entry=index)
        else:
            record["acceptable"] = record["decision"] in (DECISION_OK, DECISION_CONDITIONAL)
        accepted.append(record)

    # материал списан, а верификации нет
    verified = {str(r["material"] or "").strip().lower() for r in accepted
                if r.get("acceptable")}
    unverified: list[dict] = []
    for item in writeoffs or []:
        name = str(item.get("code") or item.get("name") or "").strip()
        if name and name.lower() not in verified:
            unverified.append({"material": name, "qty": item.get("qty"),
                               "unit": item.get("unit"), "document": item.get("document")})
            add(CHECK_MATERIAL_UNVERIFIED, LEVEL_WARNING,
                f"{name}: списание {_qty(item.get('qty'))} {item.get('unit') or ''} без записи "
                f"входного контроля — материал применён, а допуск не оформлен",
                material=name)

    journal = {
        "schema": SCHEMA_VERIFICATION,
        "kind": "journal_verification",
        "title": "Журнал верификации закупленной продукции (входной контроль)",
        "number": number,
        "built_at": today.isoformat(),
        "object_name": object_name,
        "status": STATUS_DRAFT,
        "confirmed_by": None,
        "records": accepted,
        "rejected": rejected,
        "checks": checks,
        "unverified_writeoffs": unverified,
        "totals": {
            "records": len(accepted),
            "rejected": len(rejected),
            "accepted": len([r for r in accepted if r.get("acceptable")]),
            "rejected_decision": len([r for r in accepted
                                      if r["decision"] == DECISION_REJECTED]),
            "unchecked": len([r for r in accepted if r["decision"] == DECISION_UNCHECKED]),
            "without_doc": len([r for r in accepted
                                if not (r["quality_doc"] or {}).get("number")]),
            "unverified_writeoffs": len(unverified),
            "errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
            "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING]),
        },
        "disclaimer": ("Журнал входного контроля показывает, по какому документу качества "
                       "материал допущен в работу. Допуск оформляет человек; отсутствие "
                       "документа не заменяется отметкой «допущен»."),
    }
    journal["can_close"] = can_close(journal, SCHEMA_VERIFICATION)
    return journal


# ─────────────────────────── общее: подпись и вердикт ───────────────────────────
def can_close(journal: dict, kind: str | None = None) -> dict:
    """Можно ли считать журнал закрытым. Причины, а не «да/нет»."""
    kind = kind or journal.get("schema")
    reasons: list[str] = []
    signature = check_signature(journal)
    if not signature["has_signature"]:
        reasons.append("журнал не подписан ответственным (нет ФИО и роли)")
    elif signature["changed_after_confirm"]:
        reasons.append("записи изменены после подписи — подпись недействительна")
    errors = [c for c in journal.get("checks") or [] if c["level"] == LEVEL_ERROR]
    if errors:
        reasons.append(f"ошибок в журнале: {len(errors)} — сначала устранить, потом закрывать")
    if kind == SCHEMA_WORK:
        if not journal.get("entries"):
            reasons.append("в журнале нет ни одной записи")
        unknown = [e for e in journal.get("entries") or [] if not e.get("work_type")]
        if unknown:
            reasons.append(f"записей без определённого вида работ: {len(unknown)}")
    if kind == SCHEMA_VERIFICATION:
        if not journal.get("records"):
            reasons.append("в журнале нет ни одной проверки партии")
        unverified = journal.get("unverified_writeoffs") or []
        if unverified:
            reasons.append(f"материалов без входного контроля, но уже списанных: "
                           f"{len(unverified)}")
    return {"allowed": not reasons, "reasons": reasons}


def sign(journal: dict, *, by: str, role: str, reason: str = "", audit_log=None,
         today: date | None = None) -> dict:
    """Подписать журнал. Без ФИО и роли, с ошибками — отказ."""
    today = today or date.today()
    journal = json.loads(json.dumps(journal))
    problems: list[str] = []
    if not str(by or "").strip():
        problems.append("анонимная подпись: не указано ФИО ответственного")
    if not str(role or "").strip():
        problems.append("не указана роль (должность) ответственного")
    errors = [c for c in journal.get("checks") or [] if c["level"] == LEVEL_ERROR]
    if errors:
        problems.append(f"в журнале ошибок: {len(errors)} — "
                        f"{errors[0]['message'][:120]}")
    if journal.get("schema") == SCHEMA_WORK and not journal.get("entries"):
        problems.append("в журнале нет ни одной записи — подписывать нечего")
    if journal.get("schema") == SCHEMA_VERIFICATION and not journal.get("records"):
        problems.append("в журнале нет ни одной проверки партии — подписывать нечего")
    if problems:
        journal["sign_problems"] = problems
        return journal

    journal["confirmed_by"] = {"by": str(by).strip(), "role": str(role).strip(),
                               "reason": str(reason or ""), "at": today.isoformat()}
    journal["confirmed_by"]["content_fingerprint"] = content_fingerprint(journal)
    journal.pop("sign_problems", None)
    journal["status"] = STATUS_SIGNED
    journal["can_close"] = can_close(journal)
    if audit_log is not None:
        audit_log.append("agent:journal", "agent", "journal.signed",
                         f"journal:{journal.get('number') or 'б/н'}",
                         {"kind": journal.get("schema"), "by": journal["confirmed_by"]["by"],
                          "role": journal["confirmed_by"]["role"],
                          "records": len(journal.get("entries") or journal.get("records") or [])})
    return journal


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(journal: dict) -> str:
    totals = journal.get("totals") or {}
    is_work = journal.get("schema") == SCHEMA_WORK
    lines = [f"# {journal.get('title')}", "",
             f"**Объект:** {journal.get('object_name') or 'не указан'} · "
             f"**журнал:** {journal.get('number') or 'б/н'} · "
             f"**собран:** {journal.get('built_at')}", ""]
    if is_work:
        lines += [f"Записей: {totals.get('entries')} · смен: {totals.get('shifts')} · видов "
                  f"работ: {totals.get('work_types')} · с погодой: {totals.get('with_weather')} "
                  f"· отклонено: {totals.get('rejected')}", ""]
    else:
        lines += [f"Проверок партий: {totals.get('records')} · допущено: "
                  f"{totals.get('accepted')} · не допущено: {totals.get('rejected_decision')} · "
                  f"не проверено: {totals.get('unchecked')} · без документа качества: "
                  f"{totals.get('without_doc')} · списано без контроля: "
                  f"{totals.get('unverified_writeoffs')}", ""]
    lines += [f"**Статус:** {journal.get('status')}", ""]

    close = journal.get("can_close") or {}
    if close.get("allowed"):
        lines += ["Журнал можно закрывать: подписан, ошибок нет.", ""]
    else:
        lines += ["**Закрывать нельзя:**", ""]
        lines += [f"- {reason}" for reason in close.get("reasons") or []] + [""]

    errors = [c for c in journal.get("checks") or [] if c["level"] == LEVEL_ERROR]
    warnings = [c for c in journal.get("checks") or [] if c["level"] == LEVEL_WARNING]
    if errors:
        lines += ["## Ошибки", ""] + [f"- **{c['code']}** — {c['message']}" for c in errors] + [""]
    if warnings:
        lines += ["## Требует решения", ""]
        lines += [f"- {c['code']} — {c['message']}" for c in warnings] + [""]

    if is_work and journal.get("entries"):
        lines += ["## Записи журнала", "",
                  "| № | Дата | Смена | Работы | Вид работ | Объём | Погода | Исполнитель |",
                  "|---|---|---|---|---|---|---|---|"]
        for entry in journal["entries"]:
            weather = entry.get("weather") or {}
            weather_text = (f"{weather.get('temp_c', '—')} °C, {weather.get('precipitation', '—')}"
                            if isinstance(weather, dict) else str(weather))
            lines.append(f"| {entry['no']} | {entry['date']} | {entry['shift']} | "
                         f"{(entry['work'] or '')[:70]} | {entry.get('work_type_name') or '?'} | "
                         f"{_qty(entry.get('qty'))} {entry.get('unit') or ''} | {weather_text} | "
                         f"{entry['executor']} |")
        lines.append("")
    if not is_work and journal.get("records"):
        lines += ["## Входной контроль", "",
                  "| № | Дата | Материал | Партия | Документ качества | Годен до | Решение | "
                  "Проверил |", "|---|---|---|---|---|---|---|---|"]
        for record in journal["records"]:
            doc = record.get("quality_doc") or {}
            doc_text = " ".join(x for x in (doc.get("kind"), doc.get("number")) if x) or "нет"
            decision = record.get("decision") or "—"
            if record.get("acceptable") is False and record.get("decision"):
                # решение записано, но допуском не стало — так и показываем
                decision += " · не подтверждён"
            lines.append(f"| {record['no']} | {record['date']} | {record['material']} | "
                         f"{record.get('batch') or '—'} | {doc_text} | "
                         f"{doc.get('valid_until') or '—'} | {decision} | "
                         f"{record.get('inspected_by') or '—'}, {record.get('role') or '—'} |")
        lines.append("")

    for key, title in (("rejected", "Записи, не попавшие в журнал"),):
        if journal.get(key):
            lines += [f"## {title}", ""]
            lines += [f"- №{r.get('no')}: {r.get('why')}" for r in journal[key]] + [""]
    if journal.get("confirmed_by"):
        who = journal["confirmed_by"]
        lines += [f"Подписал: {who.get('by')}, {who.get('role')} ({who.get('at')}).", ""]
    if journal.get("sign_problems"):
        lines += ["**Подпись не принята:**", ""]
        lines += [f"- {p}" for p in journal["sign_problems"]] + [""]
    lines += [f"> {journal.get('disclaimer')}", ""]
    return "\n".join(lines)


def export_xlsx(journal: dict, path: str | Path) -> Path:
    """Журнал в XLSX: записи и проверки."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    is_work = journal.get("schema") == SCHEMA_WORK

    sheet = wb.active
    if is_work:
        sheet.title = "Записи"
        header = ["№", "Дата", "Смена", "Работы", "Вид работ", "Захватка", "Ед.", "Объём",
                  "Температура", "Осадки", "Исполнитель", "Замечания надзора"]
        rows = []
        for e in journal.get("entries") or []:
            weather = e.get("weather") if isinstance(e.get("weather"), dict) else {}
            rows.append([e["no"], e["date"], e["shift"], e["work"], e.get("work_type_name"),
                         e.get("zone"), e.get("unit"), e.get("qty"), weather.get("temp_c"),
                         weather.get("precipitation"), e["executor"], e.get("supervision")])
    else:
        sheet.title = "Входной контроль"
        header = ["№", "Дата", "Материал", "Ед.", "Кол-во", "Партия", "Поставщик",
                  "Документ", "Номер", "Годен до", "Решение", "Проверил", "Роль", "Примечание"]
        rows = []
        for r in journal.get("records") or []:
            doc = r.get("quality_doc") or {}
            rows.append([r["no"], r["date"], r["material"], r.get("unit"), r.get("qty"),
                         r.get("batch"), r.get("supplier"), doc.get("kind"), doc.get("number"),
                         doc.get("valid_until"), r.get("decision"), r.get("inspected_by"),
                         r.get("role"), r.get("note")])
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append(row)
    for column, width in zip("ABCDEFGHIJKLMNOP", (6, 12, 10, 50, 22, 12, 8, 12, 18, 16, 24, 12,
                                                  16, 18)):
        sheet.column_dimensions[column].width = width

    checks = wb.create_sheet("Проверки")
    checks.append(["Уровень", "Код", "Сообщение"])
    for cell in checks[1]:
        cell.font = Font(bold=True)
    for check in journal.get("checks") or []:
        checks.append([check["level"], check["code"], check["message"]])
    for column, width in zip("ABC", (10, 30, 120)):
        checks.column_dimensions[column].width = width
    wb.save(path)
    return path


def write(journal: dict, out_dir: str | Path, *, stem: str | None = None) -> dict:
    """Сохранить журнал: JSON, Markdown, XLSX."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    kind = "journal_work" if journal.get("schema") == SCHEMA_WORK else "journal_verification"
    tail = stem or (str(journal.get("number") or "").replace(" ", "_").replace("/", "-") or kind)
    (out / f"{kind}_{tail}.json").write_text(json.dumps(journal, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    (out / f"{kind}_{tail}.md").write_text(to_markdown(journal), encoding="utf-8")
    export_xlsx(journal, out / f"{kind}_{tail}.xlsx")
    return {"json": str(out / f"{kind}_{tail}.json"),
            "markdown": str(out / f"{kind}_{tail}.md"),
            "xlsx": str(out / f"{kind}_{tail}.xlsx")}
