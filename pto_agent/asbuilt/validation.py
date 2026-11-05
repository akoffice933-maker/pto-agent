# -*- coding: utf-8 -*-
"""Проверки черновика АОСР: хронология, пункты норм, проектная документация, подписи.

Это ровно те проверки, которые обещаны в `prompts/02_aosr_generation.md` («типовые
замечания, которые агент обязан проверять»). Каждая находка формулируется проверяемо —
так, чтобы её можно было предъявить подрядчику и закрыть: не «что-то не так с нормами»,
а «СП 70.13330.2012 приведён без пункта».

Уровни: `blocking` — акт нельзя передавать на подпись, `warning` — примут, но вернут с
замечанием.
"""
from __future__ import annotations

import re
from datetime import date, datetime

# пункт нормы: «п. 5.3.3», «5.3.3», «п. 6.1.2.1», «прил. А»
CLAUSE_RE = re.compile(r"(?:п\.?|пункт|пп\.?|табл\.?|прил\.?)?\s*\d+(?:\.\d+)+", re.I)
# обозначение документа с годом: СП 70.13330.2012, ГОСТ Р 50571.16-2019, ПУЭ
DOC_RE = re.compile(r"(СП|ГОСТ(?:\s+Р)?|СНиП|ПУЭ|СО|ВСН|РД)\b(?:\s*[\d.\-]+)*", re.I)
EDITION_RE = re.compile(r"(изд\.|издани|с\s+изм|ред\.|редакци|(19|20)\d{2})", re.I)
FUTURE_TOLERANCE_DAYS = 0


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _issue(code: str, text: str, severity: str = "blocking", **extra) -> dict:
    return {"code": code, "text": text, "severity": severity, **extra}


def validate_dates(spec: dict, today: date | None = None) -> list[dict]:
    """Хронология: работы, даты акта и даты приложений не должны противоречить друг другу."""
    today = today or date.today()
    issues: list[dict] = []
    act = _parse_date(spec.get("act_date"))
    start = _parse_date(spec.get("start_date"))
    end = _parse_date(spec.get("end_date"))

    if not spec.get("act_date"):
        issues.append(_issue("act_date_missing", "не указана дата освидетельствования акта"))
    if spec.get("act_date") and act is None:
        issues.append(_issue("act_date_unparsed",
                             f"дата акта не читается: {spec.get('act_date')!r}", "blocking"))
    if start and end and start > end:
        issues.append(_issue("start_after_end",
                             f"дата начала работ ({start}) позже даты окончания ({end})"))
    if act and end and act < end:
        issues.append(_issue("act_before_end",
                             f"дата акта ({act}) раньше даты окончания работ ({end})"))
    if act and (act - today).days > FUTURE_TOLERANCE_DAYS:
        issues.append(_issue("act_in_future", f"дата акта ({act}) в будущем", "warning"))
    if act and start and act < start:
        issues.append(_issue("act_before_start",
                             f"дата акта ({act}) раньше даты начала работ ({start})"))

    for doc in spec.get("attachments") or []:
        d = _parse_date(doc.get("date"))
        if d and act and d > act:
            issues.append(_issue("attachment_after_act",
                                 f"{doc.get('name') or doc.get('code')}: дата документа {d} "
                                 f"позже даты акта {act}",
                                 name=doc.get("name"), date=str(d)))
        if d and d > today:
            issues.append(_issue("attachment_in_future",
                                 f"{doc.get('name') or doc.get('code')}: дата {d} в будущем",
                                 "warning", name=doc.get("name")))
    return issues


def split_norm_string(text: str) -> tuple[str, str]:
    """«ПУЭ, 7-е изд. п. 1.8.37» → (документ, пункт). Строковая форма не должна терять пункт."""
    m = re.match(r"^(.*?)[,\s]*(?:п\.?|пункт|пп\.?)\s*([\d.]+)\s*$", str(text).strip())
    if m:
        return m.group(1).strip(" ,;"), m.group(2).strip()
    return str(text).strip(), ""


def validate_norms(norms, materials=None) -> list[dict]:
    """Нормы: ссылка без пункта недопустима; обозначение без года/редакции — замечание."""
    issues: list[dict] = []
    if not norms:
        return [_issue("norms_missing", "не приведены нормативные документы, по которым "
                                        "выполнены работы")]
    for norm in norms:
        if isinstance(norm, str):
            doc, clause = split_norm_string(norm)
            edition = ""
        else:
            doc = str(norm.get("doc") or norm.get("document") or "").strip()
            clause = str(norm.get("clause") or norm.get("item") or "").strip()
            edition = str(norm.get("edition") or "").strip()
        if not doc:
            issues.append(_issue("norm_empty", "пустая ссылка в перечне нормативных документов"))
            continue
        if not clause:
            issues.append(_issue("norm_without_clause",
                                 f"{doc}: нормативный документ приведён без указания пункта",
                                 doc=doc))
        elif not CLAUSE_RE.search(clause):
            issues.append(_issue("norm_clause_unparsed",
                                 f"{doc}: пункт {clause!r} не похож на номер пункта",
                                 "warning", doc=doc))
        if not DOC_RE.search(doc):
            issues.append(_issue("norm_unrecognized",
                                 f"{doc}: не распознано обозначение документа (ожидается СП/ГОСТ/"
                                 f"СНиП/ПУЭ с номером)", "warning", doc=doc))
        elif not EDITION_RE.search(f"{doc} {edition}"):
            issues.append(_issue("norm_without_edition",
                                 f"{doc}: не указана редакция (год или «с изм. N»)", "warning",
                                 doc=doc))
        actual = norm.get("actual_as_of") if isinstance(norm, dict) else None
        if actual and _parse_date(actual) is None:
            issues.append(_issue("norm_actual_unparsed",
                                 f"{doc}: дата актуальности не читается: {actual!r}", "warning"))
    return issues


def validate_norms_against_base(norms, norms_dir=None, base: dict | None = None) -> list[dict]:
    """Ссылки, которые база не подтверждает: это не «неверно», это «не проверено»."""
    from .norms_check import verify_norms
    report = verify_norms(norms, norms_dir, base=base)
    issues: list[dict] = []
    if not report["base"]["exists"]:
        return [_issue("norm_base_absent",
                       f"нормативная база не найдена ({report['base']['dir']}): ссылки не проверены",
                       "warning")]
    for r in report["results"]:
        if r["status"] == "doc_missing":
            issues.append(_issue("norm_not_in_base",
                                 f"{r['doc']} п. {r['clause']}: документа нет в нормативной базе "
                                 f"агента — ссылка не подтверждена", "warning", doc=r["doc"]))
        elif r["status"] == "verified_partial":
            issues.append(_issue("norm_base_fragment",
                                 f"{r['doc']} п. {r['clause']}: в базе только фрагмент "
                                 f"документа — цитату сверять по первоисточнику", "info",
                                 doc=r["doc"], clause=r["clause"]))
        elif r["status"] == "clause_missing":
            issues.append(_issue("norm_clause_not_in_base",
                                 f"{r['doc']}: пункта {r['clause']} нет в базе — {r['note']}",
                                 "warning", doc=r["doc"], clause=r["clause"]))
    return issues


def validate_project_docs(spec: dict) -> list[dict]:
    """Ссылка на проектную/рабочую документацию обязательна (п. 2 формы)."""
    docs = spec.get("project_docs") or []
    if not docs:
        return [_issue("project_docs_missing",
                       "отсутствует ссылка на чертежи рабочей документации")]
    issues = []
    for doc in docs:
        if isinstance(doc, str):
            if not doc.strip():
                issues.append(_issue("project_doc_empty", "пустая ссылка на проектную документацию"))
            continue
        if not (doc.get("doc") or doc.get("name")):
            issues.append(_issue("project_doc_empty", "ссылка на проектную документацию без шифра"))
    return issues


def validate_materials(spec: dict) -> list[dict]:
    """Материалы: без реквизитов сертификата акт не подтверждает качество."""
    materials = spec.get("materials") or []
    if not materials:
        return [_issue("materials_missing",
                       "не указаны материалы и реквизиты документов об их качестве")]
    issues = []
    for mat in materials:
        if isinstance(mat, dict) and mat.get("resource_role_unverified"):
            issues.append(_issue("resource_role_unverified",
                                 f"{mat.get('name') or 'Ресурс'}: назначение вложенного "
                                 "ресурса в составе акта не подтверждено"))
        if isinstance(mat, str):
            if not re.search(r"(сертификат|паспорт|декларац|№)", mat, re.I):
                issues.append(_issue("material_without_docs",
                                     f"{mat}: не указаны реквизиты документа о качестве",
                                     "warning", material=mat))
            continue
        if not (mat.get("certificate") or mat.get("doc")):
            issues.append(_issue("material_without_docs",
                                 f"{mat.get('name') or 'материал'}: не указаны реквизиты "
                                 f"документа о качестве", "warning", material=mat.get("name")))
    return issues


def validate_representatives(spec: dict) -> list[dict]:
    """Подписи: застройщик и подрядчик обязательны, у представителя застройщика — НРС."""
    reps = spec.get("representatives") or {}
    issues: list[dict] = []
    required = (("customer", "застройщик (технический заказчик)"),
                ("builder", "лицо, выполнившее работы"),
                ("contractor", "лицо, осуществляющее строительство"))
    for key, label in required:
        rep = reps.get(key) or {}
        if not rep.get("person"):
            issues.append(_issue("representative_missing",
                                 f"не указан представитель: {label}", role=key))
    customer = reps.get("customer") or {}
    if customer.get("person") and not customer.get("nrs"):
        issues.append(_issue("nrs_missing",
                             "у представителя застройщика не указан идентификационный номер "
                             "в национальном реестре специалистов (НРС)", "warning"))
    for key, rep in reps.items():
        if rep.get("person") and key not in {"customer", "builder", "contractor", "designer"}:
            issues.append(_issue("representative_unknown_role",
                                 f"неизвестная роль представителя: {key}", "warning"))
    return issues


def validate_attachments(spec: dict, work_types: list[str] | None = None,
                         matrix: dict | None = None) -> list[dict]:
    """Приложения: акт без приложений недействителен; матрица подсказывает, чего ждут."""
    attachments = spec.get("attachments") or []
    if not attachments:
        return [_issue("attachments_missing",
                       "перечень исполнительной документации пуст — акт недействителен")]
    issues: list[dict] = []
    if work_types:
        from .requirements import required_kinds
        req = required_kinds(work_types, matrix)
        if spec.get("required_kinds"):
            # акт на конкретную группу скрытых работ: комплект документов у него свой,
            # иначе акт на борозды требовал бы акт испытаний электроустановки
            req = {**req, "required": list(spec["required_kinds"])}
        from .registry import _kind_of
        present = {_kind_of(doc) for doc in attachments}
        for kind in req["required"]:
            if kind not in present and kind != "аоср":       # сам акт не приложение к себе
                issues.append(_issue("attachment_kind_missing",
                                     f"для вида работ {', '.join(work_types)} не приложен документ "
                                     f"типа «{kind}» ({req['document_kinds'].get(kind, kind)})",
                                     "warning", kind=kind))
    has_scheme = any(_kind_of_scheme(doc) for doc in attachments)
    if not has_scheme:
        issues.append(_issue("attachment_no_scheme",
                             "не приложена исполнительная схема на скрытые работы"))
    return issues


def _kind_of_scheme(doc: dict) -> bool:
    from .registry import _kind_of
    return _kind_of(doc) == "схема"


def validate_draft(spec: dict, *, work_types: list[str] | None = None,
                   matrix: dict | None = None, today: date | None = None,
                   norms_dir=None) -> dict:
    """Полный набор проверок черновика. Возвращает находки, блокеры и вердикт."""
    issues: list[dict] = []
    issues += validate_project_docs(spec)
    issues += validate_representatives(spec)
    issues += validate_attachments(spec, work_types, matrix)
    issues += validate_dates(spec, today)
    issues += validate_norms(spec.get("norms"))
    norms_report = None
    if norms_dir is not False:
        from .norms_check import verify_norms
        norms_report = verify_norms(spec.get("norms"), norms_dir or None)
        issues += validate_norms_against_base(spec.get("norms"), norms_dir, base=None)
    issues += validate_materials(spec)

    # обязательные текстовые поля формы
    for field, label in (("object_name", "наименование объекта"), ("work", "наименование работ"),
                         ("start_date", "дата начала работ"), ("end_date", "дата окончания работ")):
        if not spec.get(field):
            issues.append(_issue(f"{field}_missing", f"не заполнено поле: {label}", field=field))

    blocking = [i for i in issues if i["severity"] == "blocking"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    return {
        "issues": issues,
        "blocking": blocking,
        "warnings": warnings,
        "counts": {"blocking": len(blocking), "warning": len(warnings)},
        "norms_report": norms_report,
        "can_transfer": not blocking,
        "verdict": ("черновик заполнен, блокеров нет — можно передавать на подпись человеку"
                    if not blocking else
                    f"блокеров {len(blocking)}: акт нельзя передавать на подпись"),
    }
