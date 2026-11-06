# -*- coding: utf-8 -*-
"""Комплект объекта: все реестры по одному объекту — одной командой, в одну папку, с одним вердиктом.

Зачем это нужно, когда каждый реестр уже есть по отдельности: сдаёт не реестр, а **комплект**.
Проверяющий не спрашивает «есть ли у вас журнал верификации» — он спрашивает «можно ли принять
комплект по этому объекту». Поэтому здесь считается один вердикт `can_submit`, и он honest:

* **отсутствующий раздел — это не «пройдено», а «нет данных»** (`SECTION_MISSING`): реестр,
  которого не собрали, не может быть чистым. Пропускать раздел можно только явно
  (`enabled: false` с причиной), и тогда он попадает в отчёт как «исключён человеком»;
* **любая ошибка в любом разделе блокирует сдачу** — «в целом нормально» не существует:
  комплект либо полный, либо нет;
* **сдаётся подписанное**: реестр без подписи человека с ФИО и ролью — блокер
  (`SECTION_UNSIGNED`), потому что агент не подписывает;
* **блокеры перечислены по разделам с кодами и текстами**, а не «см. отчёты»: инженер должен
  видеть, что именно мешает, не открывая восемь файлов.

Разделы собираются теми же модулями, что и в отдельных кругах (журналы, документы качества,
РД и согласование, письма, лаборатории, дни качества), поэтому противоречий между «отдельным»
и «сводным» отчётом не возникает: сводный — сумма тех же проверок.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .fingerprint import check_signature

SCHEMA = "pto-agent/object-pack@1"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

SECTION_OK = "ошибок нет"
SECTION_WARNINGS = "есть предупреждения"
SECTION_ERRORS = "есть ошибки"
SECTION_MISSING = "нет данных"
SECTION_DISABLED = "исключён человеком"

CHECK_SECTION_MISSING = "SECTION_MISSING"
CHECK_SECTION_ERRORS = "SECTION_ERRORS"
CHECK_SECTION_UNSIGNED = "SECTION_UNSIGNED"
CHECK_SECTION_EMPTY = "SECTION_EMPTY"
CHECK_DISABLED_NO_REASON = "SECTION_DISABLED_WITHOUT_REASON"
CHECK_NO_SECTIONS = "PACK_WITHOUT_SECTIONS"

# раздел → что он закрывает из обязанностей ПТО
SECTION_TITLES = {
    "journals_work": "Общий журнал работ",
    "journals_verification": "Журнал верификации закупленной продукции",
    "quality_docs": "Документы качества на материалы",
    "rd": "Выдача рабочей документации",
    "submittals": "Согласование исполнительной документации",
    "letters": "Письма и запросы",
    "involvement": "Лаборатории и геодезисты",
    "quality_days": "Авторские дни и дни качества",
}
REQUIRED_SECTIONS = tuple(SECTION_TITLES)


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_list(base: Path, value, key: str) -> tuple[list[dict], str | None]:
    """Принять и путь к файлу, и готовый список. Возвращает (данные, проблема)."""
    if isinstance(value, list):
        return value, None
    if not value:
        return [], None
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    try:
        data = _read(path)
    except (json.JSONDecodeError, OSError) as exc:
        return [], f"{path.name}: не читается ({exc.__class__.__name__})"
    items = data if isinstance(data, list) else data.get(key)
    if not isinstance(items, list):
        return [], f"{path.name}: нет списка «{key}»"
    return items, None


def _section_result(kind: str, document: dict, files: dict, base: Path) -> dict:
    errors = [c for c in document.get("checks") or [] if c["level"] == LEVEL_ERROR]
    warnings = [c for c in document.get("checks") or [] if c["level"] == LEVEL_WARNING]
    signature = check_signature(document)
    if errors:
        status = SECTION_ERRORS
    elif warnings:
        status = SECTION_WARNINGS
    else:
        status = SECTION_OK
    return {"kind": kind, "title": SECTION_TITLES.get(kind, kind), "status": status,
            "signed": bool(signature["has_signature"] and signature["valid"]),
            "signed_by": signature["by"], "signed_role": signature["role"],
            "counts": document.get("totals") or {},
            "errors": [{"code": c["code"], "message": c["message"]} for c in errors],
            "warnings": [{"code": c["code"], "message": c["message"]} for c in warnings],
            "files": {k: str(Path(v).relative_to(base)) if Path(v).is_absolute()
                      and str(v).startswith(str(base)) else str(v) for k, v in files.items()}}


def build_pack(config: dict, *, base_dir: str | Path = ".", out_dir: str | Path | None = None,
               today: date | None = None) -> dict:
    """Собрать комплект по объекту: все разделы + сводный вердикт."""
    from . import checklist as checklist_mod
    from . import journals, quality_docs
    from .correspondence import (build_involvement_registry, build_letter_registry,
                                 build_quality_days)
    from .docflow import build_rd_registry, build_submittal_registry

    base = Path(base_dir)
    out = Path(out_dir) if out_dir else base / "out" / "pack"
    out.mkdir(parents=True, exist_ok=True)
    today = today or date.today()

    from .correspondence import write as corr_write
    from .docflow import write as docflow_write
    writers: dict = {}
    writers.update({"journals_work": journals.write, "journals_verification": journals.write,
                    "quality_docs": quality_docs.write, "rd": docflow_write,
                    "submittals": docflow_write, "letters": corr_write,
                    "involvement": corr_write, "quality_days": corr_write})

    object_name = str(config.get("object_name") or config.get("object") or "").strip()
    number = str(config.get("number") or "")
    sections_cfg = config.get("sections") or {}
    checks: list[dict] = []
    sections: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})


    def prebuilt(name: str, cfg: dict):
        """Готовый реестр из конфига (`registry`): подпись проверяется, а не пересобирается."""
        value = cfg.get("registry")
        if not value:
            return None, None
        path = Path(value) if Path(value).is_absolute() else base / value
        try:
            document = _read(path)
        except (json.JSONDecodeError, OSError) as exc:
            add(CHECK_SECTION_ERRORS, LEVEL_ERROR,
                f"{SECTION_TITLES.get(name, name)}: реестр {path.name} не читается "
                f"({exc.__class__.__name__})", section=name)
            return {}, {}
        files = writers[name](document, out)
        return document, files

    def disabled(name: str) -> bool:
        cfg = sections_cfg.get(name) or {}
        return cfg.get("enabled") is False

    def missing(name: str, why: str = "источник не указан в конфиге комплекта") -> None:
        sections.append({"kind": name, "title": SECTION_TITLES[name], "status": SECTION_MISSING,
                         "signed": False, "signed_by": None, "signed_role": None,
                         "counts": {}, "errors": [], "warnings": [], "files": {},
                         "why": why})
        add(CHECK_SECTION_MISSING, LEVEL_ERROR,
            f"раздел «{SECTION_TITLES[name]}»: {why} — комплект неполный, "
            f"отсутствие раздела не равно «проверено»", section=name)

    # ─── журналы ───
    cfg = sections_cfg.get("journals_work") or {}
    if disabled("journals_work"):
        cfg = {"_off": True}
    if not cfg.get("_off"):
        document, files = prebuilt("journals_work", cfg)
        if document is not None:
            sections.append(_section_result("journals_work", document, files, base))
            cfg = {}
        entries, problem = _load_list(base, cfg.get("entries"), "entries") if cfg else ([], None)
        if problem:
            add(CHECK_SECTION_ERRORS, LEVEL_ERROR, f"общий журнал работ: {problem}",
                section="journals_work")
        if cfg is None:
            cfg = {}
        if not entries and not cfg.get("_skip") and not document:
            missing("journals_work")
        elif entries:
            acts = None
            if cfg.get("acts"):
                path = Path(cfg["acts"]) if Path(cfg["acts"]).is_absolute() else base / cfg["acts"]
                acts = _read(path)
            document = journals.build_work_journal(entries, object_name=object_name,
                                                   number=cfg.get("number") or "ОЖР-1",
                                                   acts=acts, today=today)
            files = journals.write(document, out)
            sections.append(_section_result("journals_work", document, files, base))

    cfg = sections_cfg.get("journals_verification") or {}
    if not disabled("journals_verification"):
        document, files = prebuilt("journals_verification", cfg)
        if document is not None:
            sections.append(_section_result("journals_verification", document, files, base))
            cfg = {"_done": True}
        records, problem = _load_list(base, cfg.get("records"), "records")
        if problem:
            add(CHECK_SECTION_ERRORS, LEVEL_ERROR, f"журнал верификации: {problem}",
                section="journals_verification")
        if not records and not cfg.get("_done"):
            missing("journals_verification")
        elif records:
            writeoffs: list[dict] = []
            if cfg.get("writeoffs"):
                path = Path(cfg["writeoffs"]) if Path(cfg["writeoffs"]).is_absolute() \
                    else base / cfg["writeoffs"]
                from .consumption import load_table
                writeoffs, problems = load_table(path, "writeoff")
                for item in problems:
                    add(CHECK_SECTION_ERRORS, LEVEL_ERROR, f"списание: {item}",
                        section="journals_verification")
            document = journals.build_verification_journal(records, object_name=object_name,
                                                           number=cfg.get("number") or "ВК-1",
                                                           writeoffs=writeoffs, today=today)
            files = journals.write(document, out)
            sections.append(_section_result("journals_verification", document, files, base))

    # ─── документы качества ───
    cfg = sections_cfg.get("quality_docs") or {}
    if not disabled("quality_docs"):
        document, files = prebuilt("quality_docs", cfg)
        if document is not None:
            sections.append(_section_result("quality_docs", document, files, base))
            cfg = {"_done": True}
        documents, problem = _load_list(base, cfg.get("documents"), "documents")
        if problem:
            add(CHECK_SECTION_ERRORS, LEVEL_ERROR, f"документы качества: {problem}",
                section="quality_docs")
        if not documents and not cfg.get("_done"):
            missing("quality_docs")
        elif documents:
            requirements, _ = _load_list(base, cfg.get("requirements"), "requirements")
            usage, _ = _load_list(base, cfg.get("usage"), "usage")
            verification, _ = _load_list(base, cfg.get("verification"), "records")
            registry = quality_docs.build_registry(documents, object_name=object_name,
                                                   number=cfg.get("number") or "ДК-1",
                                                   requirements=requirements, usage=usage,
                                                   verification=verification, today=today)
            files = quality_docs.write(registry, out)
            sections.append(_section_result("quality_docs", registry, files, base))

    # ─── РД и согласование ───
    cfg = sections_cfg.get("rd") or {}
    if not disabled("rd"):
        document, files = prebuilt("rd", cfg)
        if document is not None:
            sections.append(_section_result("rd", document, files, base))
            cfg = {"_done": True}
        transmittals, problem = _load_list(base, cfg.get("transmittals"), "transmittals")
        if problem:
            add(CHECK_SECTION_ERRORS, LEVEL_ERROR, f"выдача РД: {problem}", section="rd")
        if not transmittals and not cfg.get("_done"):
            missing("rd")
        elif transmittals:
            revisions, _ = _load_list(base, cfg.get("revisions"), "revisions")
            works, _ = _load_list(base, cfg.get("works"), "works")
            registry = build_rd_registry(transmittals, revisions=revisions, works=works,
                                         object_name=object_name,
                                         number=cfg.get("number") or "РД-1", today=today)
            from .docflow import write as docflow_write
            files = docflow_write(registry, out)
            sections.append(_section_result("rd", registry, files, base))

    cfg = sections_cfg.get("submittals") or {}
    if not disabled("submittals"):
        document, files = prebuilt("submittals", cfg)
        if document is not None:
            sections.append(_section_result("submittals", document, files, base))
            cfg = {"_done": True}
        submittals, problem = _load_list(base, cfg.get("submittals"), "submittals")
        if problem:
            add(CHECK_SECTION_ERRORS, LEVEL_ERROR, f"согласование ИД: {problem}",
                section="submittals")
        if not submittals and not cfg.get("_done"):
            missing("submittals")
        elif submittals:
            registry = build_submittal_registry(submittals, object_name=object_name,
                                                number=cfg.get("number") or "СГ-1", today=today)
            from .docflow import write as docflow_write
            files = docflow_write(registry, out)
            sections.append(_section_result("submittals", registry, files, base))

    # ─── переписка и участники ───
    for name, builder, key, doc_number in (
            ("letters", build_letter_registry, "letters", "ПИС-1"),
            ("involvement", build_involvement_registry, "records", "ЛАБ-1"),
            ("quality_days", build_quality_days, "events", "ДК-1")):
        cfg = sections_cfg.get(name) or {}
        if disabled(name):
            continue
        document, files = prebuilt(name, cfg)
        if document is not None:
            sections.append(_section_result(name, document, files, base))
            continue
        items, problem = _load_list(base, cfg.get(key) or cfg.get("source"), key)
        if problem:
            add(CHECK_SECTION_ERRORS, LEVEL_ERROR, f"{SECTION_TITLES[name]}: {problem}",
                section=name)
        if not items:
            missing(name)
            continue
        document = builder(items, object_name=object_name,
                           number=cfg.get("number") or doc_number, today=today)
        from .correspondence import write as corr_write
        files = corr_write(document, out)
        sections.append(_section_result(name, document, files, base))

    # ─── чек-листы (если переданы готовые) ───
    cfg = sections_cfg.get("checklists") or {}
    if cfg.get("files"):
        for index, item in enumerate(cfg["files"], start=1):
            path = Path(item) if Path(item).is_absolute() else base / item
            try:
                document = _read(path)
            except (json.JSONDecodeError, OSError) as exc:
                add(CHECK_SECTION_ERRORS, LEVEL_ERROR,
                    f"чек-лист {path.name}: не читается ({exc.__class__.__name__})",
                    section="checklists")
                continue
            files = checklist_mod.write(document, out)
            section = _section_result("checklists", document, files, base)
            section["title"] = f"Чек-лист №{document.get('number') or index}"
            sections.append(section)
    elif not disabled("checklists"):
        sections.append({"kind": "checklists", "title": "Чек-листы выполненных работ",
                         "status": SECTION_MISSING, "signed": False, "signed_by": None,
                         "signed_role": None, "counts": {}, "errors": [], "warnings": [],
                         "files": {}, "why": "чек-листы не переданы в конфиге комплекта"})
        add(CHECK_SECTION_MISSING, LEVEL_ERROR,
            "раздел «Чек-листы выполненных работ»: чек-листы не переданы в конфиге комплекта — "
            "комплект неполный", section="checklists")

    # явно исключённые разделы — с причиной или без
    for name in REQUIRED_SECTIONS + ("checklists",):
        cfg = sections_cfg.get(name) or {}
        if cfg.get("enabled") is False:
            reason = str(cfg.get("reason") or "").strip()
            sections.append({"kind": name,
                             "title": SECTION_TITLES.get(name, "Чек-листы выполненных работ"),
                             "status": SECTION_DISABLED, "signed": False, "signed_by": None,
                             "signed_role": None, "counts": {}, "errors": [], "warnings": [],
                             "files": {}, "why": reason or None})
            if not reason:
                add(CHECK_DISABLED_NO_REASON, LEVEL_ERROR,
                    f"раздел «{SECTION_TITLES.get(name, name)}» исключён без причины — так нельзя: "
                    f"исключение фиксируется основанием", section=name)

    # ─── блокеры и подписи ───
    for section in sections:
        name = section["kind"]
        if section["status"] == SECTION_DISABLED:
            continue
        if section["status"] == SECTION_MISSING:
            continue                     # уже отмечено CHECK_SECTION_MISSING
        counts = section.get("counts") or {}
        objects = counts.get("entries") or counts.get("records") or counts.get("documents") \
            or counts.get("transmittals") or counts.get("letters") or counts.get("events") \
            or counts.get("items") or 0
        if not objects and section["kind"] != "quality_docs":
            add(CHECK_SECTION_EMPTY, LEVEL_ERROR,
                f"раздел «{section['title']}» собран, но пуст — сдавать нечего", section=name)
        if section["status"] == SECTION_ERRORS:
            add(CHECK_SECTION_ERRORS, LEVEL_ERROR,
                f"раздел «{section['title']}»: ошибок {len(section['errors'])} — "
                f"{section['errors'][0]['message'][:110]}", section=name)
        if not section["signed"]:
            add(CHECK_SECTION_UNSIGNED, LEVEL_ERROR,
                f"раздел «{section['title']}» не подписан человеком с ФИО и ролью — "
                f"агент не подписывает", section=name)

    if not sections:
        add(CHECK_NO_SECTIONS, LEVEL_ERROR, "в конфиге комплекта нет ни одного раздела")

    pack = {
        "schema": SCHEMA, "kind": "object_pack",
        "title": "Комплект исполнительной документации по объекту",
        "number": number, "object_name": object_name, "built_at": today.isoformat(),
        "sections": sections, "checks": checks,
        "totals": {
            "sections": len(sections),
            "ok": len([s for s in sections if s["status"] in (SECTION_OK, SECTION_WARNINGS)]),
            "with_errors": len([s for s in sections if s["status"] == SECTION_ERRORS]),
            "missing": len([s for s in sections if s["status"] == SECTION_MISSING]),
            "disabled": len([s for s in sections if s["status"] == SECTION_DISABLED]),
            "signed": len([s for s in sections if s["signed"] and s["status"] != SECTION_DISABLED]),
            "errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
            "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING]),
            "findings": sum(len(s["errors"]) for s in sections),
        },
        "disclaimer": ("Сводный вердикт считается по разделам: отсутствующий раздел — «нет данных», "
                       "а не «пройдено»; любая ошибка блокирует сдачу; сдаётся только подписанное "
                       "человеком. Агент собирает и проверяет, решение принимает ИТР."),
    }
    pack["can_submit"] = can_submit(pack)
    files = write(pack, out)
    pack["files"] = files
    return pack


def can_submit(pack: dict) -> dict:
    """Можно ли предъявлять комплект. Причины, а не «да/нет»."""
    reasons: list[str] = []
    totals = pack.get("totals") or {}
    if totals.get("missing"):
        missing = [s["title"] for s in pack.get("sections") or [] if s["status"] == SECTION_MISSING]
        reasons.append("разделов без данных: " + ", ".join(missing))
    if totals.get("with_errors"):
        bad = [f"{s['title']} ({len(s['errors'])})" for s in pack.get("sections") or []
               if s["status"] == SECTION_ERRORS]
        reasons.append("разделов с ошибками: " + ", ".join(bad))
    unsigned = [s["title"] for s in pack.get("sections") or []
                if not s["signed"] and s["status"] not in (SECTION_DISABLED, SECTION_MISSING)]
    if unsigned:
        reasons.append("не подписано разделов: " + ", ".join(unsigned))
    no_reason = [s["title"] for s in pack.get("sections") or []
                 if s["status"] == SECTION_DISABLED and not s.get("why")]
    if no_reason:
        reasons.append("исключено без причины: " + ", ".join(no_reason))
    if not pack.get("sections"):
        reasons.append("в комплекте нет ни одного раздела")
    return {"allowed": not reasons, "reasons": reasons}


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(pack: dict) -> str:
    lines = [f"# {pack['title']}", "",
             f"**Объект:** {pack.get('object_name') or '—'} · "
             f"**комплект:** {pack.get('number') or 'б/н'} · собран: {pack.get('built_at')}", ""]
    totals = pack["totals"]
    lines += [f"Разделов: {totals['sections']} · без ошибок: {totals['ok']} · с ошибками: "
              f"{totals['with_errors']} · нет данных: {totals['missing']} · подписано: "
              f"{totals['signed']} · находок: {totals['findings']}", ""]
    close = pack.get("can_submit") or {}
    if close.get("allowed"):
        lines += ["**Комплект можно предъявлять:** все разделы собраны, ошибок нет, "
                  "все подписаны.", ""]
    else:
        lines += ["**Предъявлять нельзя:**", ""] + \
                 [f"- {r}" for r in close.get("reasons") or []] + [""]
    lines += ["## Разделы", "",
              "| Раздел | Состояние | Подписан | Находок | Файлы |", "|---|---|---|---|---|"]
    for section in pack.get("sections") or []:
        signed = (f"{section['signed_by']}, {section['signed_role']}"
                  if section.get("signed_by") else "нет")
        files = ", ".join(sorted(Path(v).name for v in (section.get("files") or {}).values()))
        lines.append(f"| {section['title']} | {section['status']} | {signed} | "
                     f"{len(section['errors'])} | {files or '—'} |")
    lines.append("")
    blockers = [c for c in pack.get("checks") or [] if c["level"] == LEVEL_ERROR]
    if blockers:
        lines += ["## Блокеры сдачи", ""] + \
                 [f"- **{c['code']}** — {c['message']}" for c in blockers] + [""]
    warnings = [c for c in pack.get("checks") or [] if c["level"] == LEVEL_WARNING]
    if warnings:
        lines += ["## Требует решения", ""] + \
                 [f"- {c['code']} — {c['message']}" for c in warnings] + [""]
    lines += ["## Что нужно подписать", ""]
    unsigned = [s for s in pack.get("sections") or [] if not s["signed"]
                and s["status"] not in (SECTION_DISABLED, SECTION_MISSING)]
    if unsigned:
        lines += [f"- {s['title']} — ФИО, роль, дата" for s in unsigned]
    else:
        lines += ["- всё подписано"]
    lines += ["", f"> {pack.get('disclaimer')}", ""]
    return "\n".join(lines)


def export_xlsx(pack: dict, path: str | Path) -> Path:
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
    sheet("Разделы",
          ["Раздел", "Состояние", "Подписан", "ФИО", "Роль", "Находок", "Предупреждений",
           "Файлы"],
          [34, 22, 10, 20, 22, 9, 15, 60],
          [[s["title"], s["status"], "да" if s["signed"] else "нет",
            s.get("signed_by") or "", s.get("signed_role") or "", len(s["errors"]),
            len(s["warnings"]),
            ", ".join(sorted(Path(v).name for v in (s.get("files") or {}).values()))]
           for s in pack.get("sections") or []])
    sheet("Блокеры", ["Уровень", "Код", "Раздел", "Что не так"], [10, 26, 30, 90],
          [[("ошибка" if c["level"] == "error" else "внимание"), c["code"],
            SECTION_TITLES.get(c.get("section") or "", c.get("section") or ""), c["message"]]
           for c in pack.get("checks") or []])
    findings = [[s["title"], "ошибка", e["code"], e["message"]]
                for s in pack.get("sections") or [] for e in s["errors"]]
    sheet("Находки", ["Раздел", "Уровень", "Код", "Что не так"], [30, 10, 26, 90], findings)
    wb.save(path)
    return path


def export_docx(pack: dict, path: str | Path) -> Path:
    """Опись комплекта: разделы, состояние, места подписей."""
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("Опись комплекта исполнительной документации", level=1)
    doc.add_paragraph(f"Объект: {pack.get('object_name') or '—'}　"
                      f"Комплект № {pack.get('number') or 'б/н'} от {pack.get('built_at')}")

    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, ["№", "Раздел комплекта", "Состояние", "Подпись"]):
        cell.text = text
    for index, section in enumerate(pack.get("sections") or [], start=1):
        cells = table.add_row().cells
        cells[0].text = str(index)
        cells[1].text = section["title"]
        cells[2].text = section["status"]
        cells[3].text = (f"{section['signed_by']}, {section['signed_role']}"
                         if section.get("signed_by") else "")
    doc.add_paragraph()
    totals = pack["totals"]
    run = doc.add_paragraph().add_run(
        f"Разделов: {totals['sections']}, с ошибками: {totals['with_errors']}, "
        f"без данных: {totals['missing']}, подписано: {totals['signed']}.")
    run.bold = True
    run.font.size = Pt(10)
    close = pack.get("can_submit") or {}
    if close.get("reasons"):
        doc.add_paragraph("Комплект предъявлять нельзя:")
        for reason in close["reasons"]:
            doc.add_paragraph(f"• {reason}", style="List Bullet")
    doc.add_paragraph()
    doc.add_paragraph("Комплект подготовил: ______________________ / ______________________")
    doc.add_paragraph("Проверил (начальник ПТО): ______________________ / ______________________")
    doc.add_paragraph("Принял (представитель заказчика): ______________________ / _____________")
    doc.add_paragraph(pack.get("disclaimer") or "").runs[0].font.size = Pt(9)
    doc.save(path)
    return path


def write(pack: dict, out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tail = (str(pack.get("number") or "").replace(" ", "_").replace("/", "-")
            or (pack.get("object_name") or "объект").replace(" ", "_").replace("/", "-")[:40]
            or "объект")
    (out / f"pack_{tail}.json").write_text(json.dumps(pack, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    (out / f"pack_{tail}.md").write_text(to_markdown(pack), encoding="utf-8")
    export_xlsx(pack, out / f"pack_{tail}.xlsx")
    export_docx(pack, out / f"pack_{tail}.docx")
    return {"json": str(out / f"pack_{tail}.json"), "markdown": str(out / f"pack_{tail}.md"),
            "xlsx": str(out / f"pack_{tail}.xlsx"), "docx": str(out / f"pack_{tail}.docx")}
