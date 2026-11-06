# -*- coding: utf-8 -*-
"""Реестр технологических карт объекта: объект → ТК → статус → кто проверил.

Зачем отдельный модуль, а не «список файлов в папке»:

* **папка не отвечает на вопрос «что у нас по объекту».** Черновики лежат файлами, и чтобы
  понять, какие карты проверены, кем и когда, надо открыть каждый;
* **проверка стареет молча.** Если после подтверждения кто-то поправил раздел, документ
  остаётся с подписью, но подпись относится уже к другому содержанию. Поэтому при
  подтверждении считается отпечаток содержания (`tk.content_fingerprint`), а реестр сверяет
  его с текущим: расхождение → «изменён после проверки, проверка недействительна»;
* **статус нельзя выдумывать.** «Проверено» ставится только там, где есть ФИО человека и
  роль и отпечаток совпал. Всё остальное — «черновик, не проверен», даже если файл выглядит
  законченным.

Реестр ничего не утверждает про готовность карт к применению: он показывает состояние и
того, кого ждём с данными.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .tk import content_fingerprint

SCHEMA = "pto-agent/tk-registry@1"

STATUS_DRAFT = "черновик, не проверен"
STATUS_CONFIRMED = "проверен инженером (не утверждена)"
STATUS_CHANGED = "изменён после проверки — проверка недействительна"
STATUS_NO_OWNER = "подтверждение без ФИО — не считается"


def load_drafts(paths: list[str | Path]) -> tuple[list[dict], list[str]]:
    """Собрать черновики из файлов и каталогов. Возвращает (записи, проблемы чтения)."""
    files: list[Path] = []
    problems: list[str] = []
    for raw in paths or []:
        path = Path(raw)
        if path.is_dir():
            files += sorted(p for p in path.rglob("*.json")
                            if p.name not in ("registry.json", "index.json")
                            and not p.name.endswith("_problems.json"))
        elif path.suffix.lower() == ".json":
            files.append(path)
        elif not path.exists():
            problems.append(f"нет такого файла или каталога: {path}")

    drafts: list[dict] = []
    seen: dict[str, Path] = {}
    for path in files:
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            problems.append(f"{path.name}: не читается ({exc.__class__.__name__})")
            continue
        if not isinstance(draft, dict) or "schema" not in draft or "sections" not in draft:
            continue                                    # не наш формат — молча пропускаем
        if not str(draft.get("schema", "")).startswith("pto-agent/tk-draft"):
            continue
        key = (f"{draft.get('kind')}:{draft.get('object_name')}:"
               f"{(draft.get('work_type') or {}).get('id') or '-'}")
        if key in seen:
            problems.append(f"{path.name}: тот же черновик уже есть в {seen[key].name} — "
                            f"в реестр взят первый, второй не учитывается")
            continue
        seen[key] = path
        draft["_path"] = str(path)
        draft["_file"] = path.name
        drafts.append(draft)
    return drafts, problems


def draft_status(draft: dict) -> dict:
    """Статус одного черновика: проверен ли, кем, и совпадает ли содержание с проверенным."""
    confirmed = draft.get("confirmed_by") or {}
    by = str(confirmed.get("by") or "").strip()
    role = str(confirmed.get("role") or "").strip()
    fingerprint = confirmed.get("content_fingerprint")
    current = content_fingerprint(draft)

    if not by or not role:
        status = STATUS_NO_OWNER if confirmed else STATUS_DRAFT
        valid = False
    elif fingerprint and fingerprint != current:
        status = STATUS_CHANGED
        valid = False
    else:
        status = STATUS_CONFIRMED
        valid = True

    totals = draft.get("totals") or {}
    return {
        "status": status,
        "check_valid": valid,
        "by": by or None,
        "role": role or None,
        "confirmed_at": confirmed.get("at"),
        "reason": confirmed.get("reason"),
        "filled_by_human": len(confirmed.get("filled_items") or []),
        "fingerprint_confirmed": fingerprint,
        "fingerprint_current": current,
        "changed_after_confirm": bool(fingerprint and fingerprint != current),
        "needs_input": totals.get("needs_input", 0),
        "filled_items": totals.get("filled_items", 0),
        "norms_verified": totals.get("norms_verified", 0),
        "norms_total": totals.get("norms_total", 0),
    }


def build(drafts: list[dict], *, problems: list[str] | None = None,
          today: date | None = None) -> dict:
    """Собрать реестр: по объектам, с состоянием каждой карты и списком «кого ждём»."""
    today = today or date.today()
    objects: dict[str, dict] = {}

    for draft in drafts:
        object_name = str(draft.get("object_name") or "").strip() or "объект не указан"
        work_type = draft.get("work_type") or {}
        state = draft_status(draft)
        entry = {
            "kind": draft.get("kind"),
            "title": draft.get("title"),
            "work_type": work_type.get("id"),
            "work_type_name": work_type.get("name"),
            "built_at": draft.get("built_at"),
            "file": draft.get("_file"),
            "path": draft.get("_path"),
            **state,
        }
        slot = objects.setdefault(object_name, {"object_name": object_name, "drafts": [],
                                                "needs_input": []})
        slot["drafts"].append(entry)
        for need in draft.get("needs_input") or []:
            slot["needs_input"].append({"work_type": work_type.get("id") or draft.get("kind"),
                                        "what": need.get("what"), "ask": need.get("ask")})

    for slot in objects.values():
        entries = slot["drafts"]
        slot["totals"] = {
            "drafts": len(entries),
            "tk": len([e for e in entries if e["kind"] == "tk"]),
            "ppr": len([e for e in entries if e["kind"] == "ppr"]),
            "confirmed": len([e for e in entries if e["status"] == STATUS_CONFIRMED]),
            "changed": len([e for e in entries if e["status"] == STATUS_CHANGED]),
            "not_checked": len([e for e in entries if not e["check_valid"]]),
            "needs_input": sum(e["needs_input"] for e in entries),
            "norms_verified": sum(e["norms_verified"] for e in entries),
            "norms_total": sum(e["norms_total"] for e in entries),
        }
        # «кого ждём»: адресаты запросов, сведённые по объекту
        owners: dict[str, list[str]] = {}
        for need in slot["needs_input"]:
            owner = str(need.get("ask") or "").split(" — ")[-1].strip() or "не указан"
            owners.setdefault(owner, []).append(str(need.get("what")))
        slot["waiting_for"] = [{"owner": owner, "what": sorted(set(whats))}
                               for owner, whats in sorted(owners.items(), key=lambda x: -len(x[1]))]
        # состояние объекта словами, без «готово»
        if slot["totals"]["drafts"] == 0:
            slot["state"] = "карт нет"
        elif slot["totals"]["confirmed"] == slot["totals"]["drafts"]:
            slot["state"] = "все карты проверены инженером (не утверждены)"
        elif slot["totals"]["confirmed"]:
            slot["state"] = "часть карт проверена, часть — черновики"
        else:
            slot["state"] = "ни одна карта не проверена"

    aggregated = list(objects.values())
    aggregated.sort(key=lambda s: s["object_name"])
    flat = [entry for slot in aggregated for entry in slot["drafts"]]
    return {
        "schema": SCHEMA,
        "built_at": today.isoformat(),
        "objects": aggregated,
        "problems": problems or [],
        "totals": {
            "objects": len(aggregated),
            "drafts": len(flat),
            "confirmed": len([e for e in flat if e["status"] == STATUS_CONFIRMED]),
            "changed_after_confirm": len([e for e in flat if e["status"] == STATUS_CHANGED]),
            "not_checked": len([e for e in flat if not e["check_valid"]]),
            "needs_input": sum(e["needs_input"] for e in flat),
        },
        "changed_after_confirm": [{"object_name": slot["object_name"], "file": e["file"],
                                   "work_type": e["work_type"], "by": e["by"]}
                                  for slot in aggregated for e in slot["drafts"]
                                  if e["status"] == STATUS_CHANGED],
        "not_checked": [{"object_name": slot["object_name"], "file": e["file"],
                         "kind": e["kind"], "work_type": e["work_type"],
                         "status": e["status"], "needs_input": e["needs_input"]}
                        for slot in aggregated for e in slot["drafts"] if not e["check_valid"]],
        "disclaimer": ("Реестр показывает состояние черновиков, а не право применять карты на "
                       "объекте: утверждает ТК и ППР ответственный ИТР, а не агент."),
    }


def verify(registry: dict) -> list[str]:
    """Проверка целостности реестра по файлам на диске.

    Ловит то, что нельзя увидеть в таблице: файл черновика исчез или содержимое изменилось
    после того, как реестр был собран.
    """
    problems: list[str] = []
    for slot in registry.get("objects") or []:
        for entry in slot["drafts"]:
            path = Path(entry.get("path") or "")
            if not path.exists():
                problems.append(f"{entry.get('file')}: файла черновика нет на диске")
                continue
            try:
                draft = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                problems.append(f"{entry.get('file')}: не читается ({exc.__class__.__name__})")
                continue
            current = content_fingerprint(draft)
            if entry.get("check_valid") and current != entry.get("fingerprint_current"):
                problems.append(
                    f"{entry.get('file')}: содержимое изменилось после сборки реестра — "
                    f"реестр устарел, пересоберите")
    return problems


def to_markdown(registry: dict) -> str:
    totals = registry.get("totals") or {}
    lines = ["# Реестр технологических карт объекта", "",
             f"**Собран:** {registry.get('built_at')} · **объектов:** {totals.get('objects')} · "
             f"**черновиков:** {totals.get('drafts')} · **проверено инженером:** "
             f"{totals.get('confirmed')} · **изменено после проверки:** "
             f"{totals.get('changed_after_confirm')} · **запросов данных:** "
             f"{totals.get('needs_input')}", ""]
    if registry.get("changed_after_confirm"):
        lines += ["## Внимание: проверка недействительна", ""]
        for item in registry["changed_after_confirm"]:
            lines.append(f"- {item['object_name']} · `{item['file']}` "
                         f"({item.get('work_type') or 'ППР'}): содержание поправлено после "
                         f"проверки ({item.get('by') or 'без ФИО'}) — нужна повторная проверка")
        lines.append("")

    for slot in registry.get("objects") or []:
        slot_totals = slot["totals"]
        lines += [f"## {slot['object_name']}", "",
                  f"Состояние: **{slot['state']}** · карт: {slot_totals['tk']} ТК и "
                  f"{slot_totals['ppr']} ППР · нормы сверено {slot_totals['norms_verified']} "
                  f"из {slot_totals['norms_total']}", "",
                  "| Карта | Вид | Статус | Проверил | Запросов данных |",
                  "|---|---|---|---|---|"]
        for entry in sorted(slot["drafts"], key=lambda e: (e["kind"], e["work_type"] or "")):
            title = entry["work_type_name"] or ("ППР по объекту" if entry["kind"] == "ppr"
                                                else "ТК")
            who = (f"{entry['by']}, {entry['role']} ({entry.get('confirmed_at') or '—'})"
                   if entry["by"] else "—")
            lines.append(f"| {title} (`{entry.get('work_type') or entry['kind']}`) | "
                         f"{entry['kind'].upper()} | {entry['status']} | {who} | "
                         f"{entry['needs_input']} |")
        if slot.get("waiting_for"):
            lines += ["", "**Кого ждём с данными:**", ""]
            for group in slot["waiting_for"]:
                lines.append(f"- {group['owner']}: {', '.join(group['what'][:6])}"
                             + (" …" if len(group["what"]) > 6 else ""))
        lines.append("")

    if registry.get("not_checked"):
        lines += ["## Не проверено человеком", ""]
        for item in registry["not_checked"]:
            lines.append(f"- {item['object_name']} · `{item['file']}` — {item['status']}")
        lines.append("")
    if registry.get("problems"):
        lines += ["## Проблемы чтения", ""] + [f"- {p}" for p in registry["problems"]] + [""]
    lines += [f"> {registry.get('disclaimer')}", ""]
    return "\n".join(lines)


def export_xlsx(registry: dict, path: str | Path) -> Path:
    """Реестр в XLSX: лист на объект + сводка. Статусы пишутся словами, а не цветом."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    summary = wb.active
    summary.title = "Сводка"
    summary.append(["Объект", "ТК", "ППР", "Проверено инженером", "Изменено после проверки",
                    "Запросов данных", "Состояние"])
    for cell in summary[1]:
        cell.font = Font(bold=True)
    for slot in registry.get("objects") or []:
        totals = slot["totals"]
        summary.append([slot["object_name"], totals["tk"], totals["ppr"], totals["confirmed"],
                        totals["changed"], totals["needs_input"], slot["state"]])
    for column, width in zip("ABCDEFG", (28, 8, 8, 22, 24, 18, 42)):
        summary.column_dimensions[column].width = width

    for index, slot in enumerate(registry.get("objects") or [], start=1):
        sheet = wb.create_sheet(title=f"Объект {index}")
        sheet.append(["Карта", "Тип", "Вид работ", "Статус", "Проверил", "Роль", "Дата проверки",
                      "Запросов данных", "Нормы сверено", "Норм всего", "Файл"])
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for entry in slot["drafts"]:
            sheet.append([entry["work_type_name"] or ("ППР по объекту"
                                                     if entry["kind"] == "ppr" else "ТК"),
                          entry["kind"].upper(), entry.get("work_type"),
                          entry["status"], entry["by"] or "—", entry["role"] or "—",
                          entry.get("confirmed_at") or "—", entry["needs_input"],
                          entry["norms_verified"], entry["norms_total"], entry.get("file")])
        for column, width in zip("ABCDEFGHIJK", (34, 7, 18, 38, 20, 20, 14, 18, 14, 12, 26)):
            sheet.column_dimensions[column].width = width

    wb.save(path)
    return path


def write(registry: dict, out_dir: str | Path) -> dict:
    """Сохранить реестр: JSON, Markdown, XLSX."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    (out / "registry.md").write_text(to_markdown(registry), encoding="utf-8")
    export_xlsx(registry, out / "registry.xlsx")
    return {"json": str(out / "registry.json"), "markdown": str(out / "registry.md"),
            "xlsx": str(out / "registry.xlsx")}
