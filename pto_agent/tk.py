# -*- coding: utf-8 -*-
"""Черновик технологической карты (ТК) и проекта производства работ (ППР).

Готового OSS на ТК/ППР нет (см. `docs/26_ТК_и_ППР_открытый_код`), поэтому структура здесь
своя, а содержание берётся только из того, что у агента есть как **факт**:

* вид работ и требуемые документы — матрица `data/reference/work_type_requirements.json`;
* нормы — оттуда же, но каждая норма проверяется по загруженной нормативной базе
  (`data/norms`): если документа или пункта в базе нет, норма помечается `verified: false`
  и в документ идёт с явной оговоркой, а не как утверждение;
* позиции сметы, если она передана, — «что учтено сметой», со ссылкой на источник.

Чего здесь **нет и не будет** (иначе это уже не черновик): состава операций с допусками,
перечня машин и механизмов, калькуляции трудозатрат, схем расстановки техники, мероприятий
по охране труда под конкретный объект. По каждому такому разделу — `needs_input` с адресатом
запроса: кто и что должен дать.

Документ никогда не называется «готовым»: статус `draft_unapproved`, пока человек не
подтвердит (`confirm`), и даже после подтверждения это «черновик, подписанный инженером»,
а не утверждённая ТК.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .asbuilt.requirements import (classify_work, get_work_type, hidden_work,
                                   work_type_requirements)
from .norms import load_norms

SCHEMA = "pto-agent/tk-draft@1"
DEFAULT_MATRIX_NORMS_DIR = Path(__file__).resolve().parents[1] / "data" / "norms"

STATUS_DRAFT = "draft_unapproved"
STATUS_CONFIRMED = "draft_confirmed_by_human"

DISCLAIMER = (
    "Это черновик, собранный агентом из матрицы требований и нормативной базы. "
    "Он не является утверждённой технологической картой (проектом производства работ) и "
    "не может применяться на объекте без проверки и подписи: состав операций, допуски, "
    "машины и механизмы, охрана труда и калькуляция трудозатрат заполняет инженер."
)

# Разделы ТК: (ключ, заголовок, кто отвечает за содержание)
TK_SECTIONS = (
    ("scope", "1. Область применения и границы работ", "инженер ПТО"),
    ("norms", "2. Нормативные ссылки", "инженер ПТО"),
    ("sequence", "3. Состав и последовательность технологических операций", "технолог / главный инженер"),
    ("materials", "4. Материалы, изделия и оборудование", "инженер ПТО / снабжение"),
    ("machines", "5. Машины, механизмы, инструмент и приспособления", "механик участка"),
    ("quality", "6. Контроль качества и исполнительная документация", "инженер ПТО"),
    ("safety", "7. Требования охраны труда, пожарной и экологической безопасности", "специалист по ОТиПБ"),
    ("labour", "8. Калькуляция трудозатрат", "сметчик"),
)

# что именно запросить по разделу ППР — формулировки разные, «нужны данные» без адреса бесполезно
PPR_HINTS = {
    "schedule": "график производства работ с датами и объёмами (из проекта организации "
                "строительства или договора)",
    "siteplan": "стройгенплана нет: нужен план площадки с механизмами, складами и временными "
                "сетями (из ПОС/ППР проектировщика)",
    "machines": "схема механизации: какие краны/экскаваторы, где стоят, чем обоснованы",
    "safety": "перечень опасных производственных факторов по объекту и мероприятия с "
              "пунктами нормативных документов",
    "fire": "мероприятия по пожарной безопасности площадки с пунктами и схемой расстановки",
    "utilities": "расчёт потребности в электроэнергии и воде, точки подключения",
    "ecology": "мероприятия по обращению с отходами и охране окружающей среды по объекту",
}

PPR_SECTIONS = (
    ("general", "1. Общие данные и обоснование", "инженер ПТО"),
    ("schedule", "2. Календарный план и график производства работ", "инженер ПТО"),
    ("siteplan", "3. Строительный генеральный план", "инженер ПТО / проектировщик"),
    ("technology", "4. Технология производства работ (перечень ТК)", "инженер ПТО"),
    ("machines", "5. Механизация, машины и механизмы", "механик участка"),
    ("safety", "6. Охрана труда и промышленная безопасность", "специалист по ОТиПБ"),
    ("fire", "7. Пожарная безопасность", "специалист по ОТиПБ"),
    ("utilities", "8. Электроснабжение и водоснабжение площадки", "энергетик / инженер ПТО"),
    ("ecology", "9. Мероприятия по охране окружающей среды", "специалист по экологии"),
    ("attachments", "10. Перечень технологических карт и приложений", "инженер ПТО"),
)


# ─────────────────────────── нормативная база ───────────────────────────
def load_norm_clauses(norms_dir: str | Path | None = None):
    """Пункты нормативной базы; нет каталога — пустой список (тогда всё непроверено)."""
    folder = Path(norms_dir or DEFAULT_MATRIX_NORMS_DIR)
    if not folder.exists():
        return []
    try:
        return load_norms(folder)
    except Exception:                       # битая база не должна ронять черновик
        return []


def verify_norms(norms: list[dict], clauses: list) -> list[dict]:
    """Сверить нормы из матрицы с загруженной базой.

    Три исхода, и все они видны в документе:

    * `verified: true` — документ и пункт есть в базе, приведена цитата;
    * `verified: false`, `reason: "пункта нет в базе"` — документ есть, пункта нет;
    * `verified: false`, `reason: "документа нет в базе"` — проверить нечем.
    """
    by_doc: dict[str, list] = {}
    for clause in clauses:
        by_doc.setdefault(_norm_key(clause.doc.designation), []).append(clause)

    out: list[dict] = []
    for norm in norms:
        item = {"doc": norm.get("doc"), "clause": norm.get("clause"),
                "edition": norm.get("edition"), "topic": norm.get("topic"),
                "verified": False, "reason": "", "quote": None, "page": None,
                "edition_in_base": None}
        found_doc = by_doc.get(_norm_key(norm.get("doc")))
        if not found_doc:
            item["reason"] = "документа нет в загруженной нормативной базе — проверить"
        else:
            item["edition_in_base"] = found_doc[0].doc.edition
            match = next((c for c in found_doc if str(c.clause).strip() == str(norm.get("clause")).strip()),
                         None)
            if match is None:
                item["reason"] = (f"документ в базе есть (ред. {found_doc[0].doc.edition}), "
                                  f"пункта {norm.get('clause')} в ней нет — проверить")
            else:
                item["verified"] = True
                item["quote"] = match.body.strip()
                item["page"] = getattr(match, "page", None)
        out.append(item)
    return out


def _norm_key(text) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


# ─────────────────────────── сборка черновика ───────────────────────────
def build_tk_draft(*, work_type_id: str | None = None, work_text: str | None = None,
                   object_name: str = "", positions: list[dict] | None = None,
                   norms_dir: str | Path | None = None, matrix: dict | None = None,
                   today: date | None = None) -> dict:
    """Черновик ТК по виду работ.

    Вид работ задаётся либо явно (`work_type_id`), либо текстом (`work_text` — тогда
    используется тот же классификатор, что и для актов, и нераспознанное не выдумывается).
    """
    today = today or date.today()
    matrix_norms = load_norm_clauses(norms_dir)
    needs: list[dict] = []
    sections: list[dict] = []

    resolved = None
    if work_type_id:
        wt = get_work_type(work_type_id, matrix)
        if wt is not None:
            resolved = wt
    if resolved is None and work_text:
        hits = classify_work(work_text, matrix)
        if hits:
            resolved = get_work_type(hits[0]["work_type"], matrix)

    if resolved is None:
        needs.append({"what": "вид работ", "why": "не удалось определить вид работ по тексту",
                      "ask": "укажите вид работ (id из матрицы) — их "
                             f"{len((matrix or {}).get('work_types') or [])} в справочнике"})
        requirements = {}
    else:
        requirements = work_type_requirements(resolved["id"], matrix)

    work_type = None if resolved is None else {
        "id": resolved["id"], "name": resolved["name"],
        "hidden_work": hidden_work(resolved["id"], matrix),
    }

    # 1. Область применения — только то, что известно
    scope_items = []
    if object_name:
        scope_items.append({"text": f"Объект: {object_name}.", "source": "задано в запросе"})
    if work_type:
        scope_items.append({"text": f"Вид работ: {work_type['name']}.", "source": "матрица требований"})
        if work_type["hidden_work"]:
            scope_items.append({"text": "Работы скрываемые: по завершении требуется акт "
                                        "освидетельствования скрытых работ.",
                                "source": "матрица требований (hidden_work)"})
    if not scope_items:
        needs.append({"what": "объект и вид работ", "why": "не заданы",
                      "ask": "наименование объекта и вид работ"})
    scope_needs = _pop_needs(needs, "объект и вид работ") + _pop_needs(needs, "вид работ")
    sections.append({"key": "scope", "title": TK_SECTIONS[0][1], "owner": TK_SECTIONS[0][2],
                     "items": scope_items, "needs_input": scope_needs})

    # 2. Нормативные ссылки — с проверкой по базе
    norms = verify_norms(requirements.get("norms") or [], matrix_norms)
    if not norms and resolved is not None:
        needs.append({"what": "нормативные ссылки",
                      "why": "в матрице у этого вида работ нормы не заполнены",
                      "ask": "перечень СП/ГОСТ с пунктами — инженер ПТО"})
    sections.append({"key": "norms", "title": TK_SECTIONS[1][1], "owner": TK_SECTIONS[1][2],
                     "items": [_norm_text(n) for n in norms],
                     "needs_input": _pop_needs(needs, "нормативные ссылки")})

    # 3. Технология — у агента нет состава операций
    needs.append({"what": "состав и последовательность операций",
                  "why": "в матрице и нормативной базе нет состава операций с допусками; "
                         "придумывать последовательность работ и допуски нельзя",
                  "ask": "перечень операций с допусками и схемами — технолог / главный инженер"})
    sections.append({"key": "sequence", "title": TK_SECTIONS[2][1], "owner": TK_SECTIONS[2][2],
                     "items": [], "needs_input": _pop_needs(needs, "состав и последовательность операций")})

    # 4. Материалы: если есть смета — перечислить её позиции как «учтено сметой»
    material_items = []
    for pos in positions or []:
        name = str(pos.get("name") or pos.get("Наименование") or "").strip()
        if not name:
            continue
        qty = pos.get("qty") if pos.get("qty") is not None else pos.get("Количество")
        unit = pos.get("unit") or pos.get("Ед") or pos.get("ЕдИзм") or ""
        material_items.append({
            "text": f"{name} — {qty if qty is not None else 'объём не указан'} {unit}".rstrip(),
            "source": pos.get("source") or "смета (позиция передана в запросе)",
            "note": "перенесено из сметы: проверить, что материал относится к этому виду работ",
        })
    if not material_items:
        needs.append({"what": "материалы и изделия",
                      "why": "смета или спецификация не переданы",
                      "ask": "спецификация материалов / ведомость ресурсов — инженер ПТО"})
    sections.append({"key": "materials", "title": TK_SECTIONS[3][1], "owner": TK_SECTIONS[3][2],
                     "items": material_items,
                     "needs_input": _pop_needs(needs, "материалы и изделия")})

    # 5. Машины и механизмы
    needs.append({"what": "машины, механизмы и инструмент",
                  "why": "потребность в технике зависит от объекта и схемы работ — данных нет",
                  "ask": "перечень машин и механизмов с обоснованием — механик участка"})
    sections.append({"key": "machines", "title": TK_SECTIONS[4][1], "owner": TK_SECTIONS[4][2],
                     "items": [], "needs_input": _pop_needs(needs, "машины, механизмы и инструмент")})

    # 6. Контроль качества и ИД — берём из матрицы: это наш сильный раздел
    quality_items = []
    for kind in requirements.get("required_descriptions") or []:
        quality_items.append({"text": f"Требуется документ: {kind['what']}",
                              "source": "матрица требований"})
    for protocol in requirements.get("protocols") or []:
        quality_items.append({"text": f"Требуется протокол: {protocol}",
                              "source": "матрица требований"})
    for extra in requirements.get("extra_docs") or []:
        quality_items.append({"text": f"Дополнительно: {extra}", "source": "матрица требований"})
    if not quality_items:
        needs.append({"what": "контроль качества и исполнительная документация",
                      "why": "вид работ не определён — комплект документов не выведен",
                      "ask": "вид работ, затем комплект по матрице"})
    sections.append({"key": "quality", "title": TK_SECTIONS[5][1], "owner": TK_SECTIONS[5][2],
                     "items": quality_items, "needs_input": []})

    # 7. Охрана труда
    needs.append({"what": "требования охраны труда, пожарной и экологической безопасности",
                  "why": "опасные факторы определяются по объекту и проекту, а не по шаблону",
                  "ask": "раздел ОТиПБ под объект (с ссылками на пункты) — специалист по ОТиПБ"})
    sections.append({"key": "safety", "title": TK_SECTIONS[6][1], "owner": TK_SECTIONS[6][2],
                     "items": [], "needs_input": _pop_needs(needs, "требования охраны труда, "
                                                                  "пожарной и экологической безопасности")})

    # 8. Трудозатраты
    needs.append({"what": "калькуляция трудозатрат",
                  "why": "нормы времени в базе агента отсутствуют; расчёт «по смыслу» недопустим",
                  "ask": "калькуляция по ГЭСН/ФЕР или хронометражу — сметчик"})
    sections.append({"key": "labour", "title": TK_SECTIONS[7][1], "owner": TK_SECTIONS[7][2],
                     "items": [], "needs_input": _pop_needs(needs, "калькуляция трудозатрат")})

    return _finish(kind="tk", object_name=object_name, work_type=work_type,
                   sections=sections, norms=norms, today=today,
                   extra={"documents": [_document_line(k) for k in
                                        requirements.get("required_descriptions") or []],
                          "protocols": requirements.get("protocols") or []})


def build_ppr_draft(*, object_name: str = "", work_text: str | None = None,
                    positions: list[dict] | None = None, matrix: dict | None = None,
                    today: date | None = None) -> dict:
    """Черновик ППР: перечень разделов с состоянием «есть / нужны данные».

    ППР — документ по объекту, а не по виду работ: у агента нет ни стройгенплана, ни
    календарного плана, поэтому заполняются только общие данные и перечень ТК.
    """
    today = today or date.today()
    needs: list[dict] = []
    sections: list[dict] = []

    work_types_found: list[dict] = []
    for pos in positions or []:
        for hit in classify_work(str(pos.get("name") or ""), matrix):
            if not any(w["id"] == hit["work_type"] for w in work_types_found):
                work_types_found.append({"id": hit["work_type"], "name": hit["name"]})

    general_items = []
    if object_name:
        general_items.append({"text": f"Объект: {object_name}.", "source": "задано в запросе"})
    if work_types_found:
        general_items.append({
            "text": "В смете распознаны виды работ: "
                    + ", ".join(w["name"] for w in work_types_found) + ".",
            "source": "классификация позиций сметы"})
    if not general_items:
        needs.append({"what": "объект и состав работ", "why": "не заданы",
                      "ask": "наименование объекта, смета или перечень работ"})
    sections.append({"key": "general", "title": PPR_SECTIONS[0][1], "owner": PPR_SECTIONS[0][2],
                     "items": general_items, "needs_input": _pop_needs(needs, "объект и состав работ")})

    for key, title, owner in PPR_SECTIONS[1:]:
        if key == "attachments":
            items = [{"text": f"ТК на вид работ: {w['name']} (черновик формируется "
                              f"`tk draft --work-type {w['id']}`)",
                      "source": "классификация позиций сметы"} for w in work_types_found]
            if not items:
                needs.append({"what": "перечень ТК", "why": "виды работ не определены",
                              "ask": "смета или перечень работ"})
            sections.append({"key": key, "title": title, "owner": owner, "items": items,
                             "needs_input": _pop_needs(needs, "перечень ТК")})
            continue
        if key == "technology":
            items = [{"text": "Технология производства работ раскрывается технологическими "
                              "картами (раздел 10) — в ППР она не пересказывается.",
                      "source": "структура ППР"}]
            sections.append({"key": key, "title": title, "owner": owner, "items": items,
                             "needs_input": []})
            continue
        items = []
        hint = PPR_HINTS.get(key, "раздел под объект, с ссылками на пункты")
        needs.append({"what": title.split(". ", 1)[-1].lower(),
                      "why": f"раздел «{title.split('. ', 1)[-1]}» агент не заполняет: "
                             f"у него нет данных объекта, а «по смыслу» это писать нельзя",
                      "ask": f"{owner}: {hint}"})
        sections.append({"key": key, "title": title, "owner": owner, "items": items,
                         "needs_input": _pop_needs(needs, title.split(". ", 1)[-1].lower())})

    return _finish(kind="ppr", object_name=object_name, work_type=None, sections=sections,
                   norms=[], today=today,
                   extra={"work_types": work_types_found})


def _pop_needs(needs: list[dict], what: str) -> list[dict]:
    """Вынуть из общего списка то, что относится к разделу (список `needs` общий на документ)."""
    taken = [n for n in needs if n.get("what") == what]
    for item in taken:
        needs.remove(item)
    return taken


def _document_line(kind: dict) -> dict:
    return {"kind": kind.get("kind"), "what": kind.get("what")}


def _norm_text(norm: dict) -> dict:
    text = f"{norm['doc']}, п. {norm['clause']}"
    if norm.get("edition"):
        text += f" (ред. {norm['edition']})"
    text += f" — {norm.get('topic') or 'без указания темы'}"
    if norm["verified"]:
        return {"text": text, "source": f"нормативная база: {norm['doc']}",
                "quote": norm["quote"], "page": norm.get("page"),
                "note": "пункт сверен с загруженной базой"}
    return {"text": text, "source": "матрица требований (не проверено по базе)",
            "note": norm["reason"], "unverified": True}


def _finish(*, kind: str, object_name: str, work_type: dict | None, sections: list[dict],
            norms: list[dict], today: date, extra: dict) -> dict:
    aggregated = [{"section": s["title"].split(". ", 1)[-1], "owner": n["ask"], **n}
                  for s in sections for n in s["needs_input"]]
    filled = sum(len(s["items"]) for s in sections)
    draft = {
        "schema": SCHEMA,
        "kind": kind,
        "title": ("Черновик технологической карты" if kind == "tk"
                  else "Черновик проекта производства работ"),
        "object_name": object_name or None,
        "work_type": work_type,
        "built_at": today.isoformat(),
        "status": STATUS_DRAFT,
        "disclaimer": DISCLAIMER,
        "sections": sections,
        "norms": norms,
        "needs_input": aggregated,
        "confirmed_by": None,
        "totals": {
            "sections": len(sections),
            "filled_items": filled,
            "needs_input": len(aggregated),
            "norms_total": len(norms),
            "norms_verified": len([n for n in norms if n["verified"]]),
            "norms_unverified": len([n for n in norms if not n["verified"]]),
        },
    }
    draft.update(extra)
    return draft


# ─────────────────────────── подтверждение человеком ───────────────────────────
def confirm(draft: dict, *, by: str, role: str, filled: dict | None = None,
            reason: str = "", audit_log=None, today: date | None = None) -> dict:
    """Отметить, что инженер проверил черновик и заполнил разделы.

    `filled` — {ключ раздела: [текст, …]} — то, что инженер вписал руками (состав операций,
    машины, ОТиПБ, калькуляция). Каждый внесённый пункт получает `by` и пометку «внесено
    человеком»: агент эти данные не придумывает и не переписывает.
    """
    today = today or date.today()
    problems: list[str] = []
    if not str(by or "").strip():
        problems.append("не указано, кто подтверждает: анонимное подтверждение не принимается")
    if not str(role or "").strip():
        problems.append("не указана роль подтверждающего")
    applied: list[dict] = []

    for key, texts in (filled or {}).items():
        section = next((s for s in draft.get("sections") or [] if s["key"] == key), None)
        if section is None:
            problems.append(f"раздел «{key}» в черновике не найден — не применено")
            continue
        for text in texts if isinstance(texts, list) else [texts]:
            value = str(text or "").strip()
            if not value:
                continue
            if not section["items"]:
                section["needs_input"] = []      # раздел перестал быть «нужны данные»
            section["items"].append({"text": value, "source": f"внесено человеком ({by})"})
            applied.append({"section": section["title"], "text": value})

    if problems:
        draft["confirm_problems"] = problems
        return draft

    remaining = [n for s in draft.get("sections") or [] for n in s["needs_input"]]
    draft["needs_input"] = [{"section": s["title"].split(". ", 1)[-1], **n}
                            for s in draft.get("sections") or [] for n in s["needs_input"]]
    draft["status"] = STATUS_CONFIRMED
    draft["confirmed_by"] = {"by": by, "role": role, "reason": reason,
                             "reason_missing": not reason, "at": today.isoformat(),
                             "filled_items": applied,
                             "sections_without_data": [n["what"] for n in remaining]}
    draft["totals"]["filled_items"] = sum(len(s["items"]) for s in draft["sections"])
    draft["totals"]["needs_input"] = len(draft["needs_input"])
    if audit_log is not None:
        audit_log.append(by, "human", f"{draft.get('kind')}.confirmed",
                         f"{draft.get('kind')}:{draft.get('object_name') or 'без объекта'}",
                         {"filled_items": len(applied), "role": role,
                          "reason": reason or "не указана",
                          "remaining_needs_input": draft["totals"]["needs_input"],
                          "status": draft["status"]})
    return draft


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(draft: dict) -> str:
    totals = draft.get("totals") or {}
    work_type = draft.get("work_type") or {}
    lines = [f"# {draft.get('title')}: {draft.get('object_name') or 'объект не указан'}", ""]
    if work_type:
        lines.append(f"- вид работ: **{work_type.get('name')}** "
                     f"(`{work_type.get('id')}`, скрываемые: "
                     f"{'да' if work_type.get('hidden_work') else 'нет'})")
    lines += [f"- собран: {draft.get('built_at')} · статус: **{_status_ru(draft.get('status'))}**",
              f"- разделов: {totals.get('sections')} · заполнено пунктов: "
              f"{totals.get('filled_items')} · требуют данных: {totals.get('needs_input')}",
              f"- нормы: проверено {totals.get('norms_verified')} из {totals.get('norms_total')}",
              "", f"> {draft.get('disclaimer')}", ""]

    for section in draft.get("sections") or []:
        lines += [f"## {section['title']}", ""]
        for item in section["items"]:
            tail = f" — _{item.get('source')}_" if item.get("source") else ""
            lines.append(f"- {item['text']}{tail}")
            if item.get("quote"):
                lines += ["", f"  > «{item['quote']}»"
                          + (f" (стр. {item['page']})" if item.get("page") else ""), ""]
            if item.get("note"):
                lines.append(f"  - примечание: {item['note']}")
            if item.get("unverified"):
                lines.append("  - **не проверено по нормативной базе**")
        for need in section.get("needs_input") or []:
            lines.append(f"- ⚠ **нужны данные:** {need['what']} — {need['why']}. "
                         f"Запросить: {need['ask']}")
        if not section["items"] and not section.get("needs_input"):
            lines.append("- (раздел пуст: данных нет, и заполнять его «по смыслу» нельзя)")
        lines.append("")

    if draft.get("documents"):
        lines += ["## Комплект исполнительной документации по виду работ", ""]
        for doc in draft["documents"]:
            lines.append(f"- {doc.get('what')}")
        lines.append("")
    if draft.get("protocols"):
        lines += ["## Протоколы испытаний", ""] + [f"- {p}" for p in draft["protocols"]] + [""]
    if draft.get("work_types"):
        lines += ["## Виды работ по смете", ""] + [
            f"- {w['name']} (`{w['id']}`)" for w in draft["work_types"]] + [""]

    if draft.get("needs_input") and draft.get("status") == STATUS_CONFIRMED:
        lines += ["## Что осталось запросить после проверки", ""] + [
            f"- {n['what']} ({n['ask']})" for n in draft["needs_input"]] + [""]

    confirmed = draft.get("confirmed_by")
    if confirmed:
        lines += [f"## Проверено человеком: {confirmed['by']}, {confirmed['role']}", "",
                  f"- причина проверки: {confirmed.get('reason') or '— (не указана)'}",
                  f"- внесено инженером пунктов: {len(confirmed.get('filled_items') or [])}"]
        for item in (confirmed.get("filled_items") or [])[:20]:
            lines.append(f"  - {item['section']}: {item['text'][:120]}")
        if confirmed.get("sections_without_data"):
            lines.append("- разделы без данных: "
                         + ", ".join(confirmed["sections_without_data"][:8]))
    else:
        lines += ["", "_Черновик не проверен человеком: состав работ, допуски, техника, "
                      "охрана труда и трудозатраты не заполнены._"]

    lines += ["", "_Документ не является утверждённой технологической картой (ППР): "
                  "подписывает ответственный ИТР после проверки. Нормы без пометки о сверке "
                  "с нормативной базой — не подтверждены._"]
    return "\n".join(lines) + "\n"


def _status_ru(status: str | None) -> str:
    return {STATUS_DRAFT: "черновик, не проверен человеком",
            STATUS_CONFIRMED: "черновик проверен инженером (не утверждённая ТК)"}.get(
                status or "", str(status))


def export_docx(draft: dict, path: str | Path) -> Path:
    """DOCX черновика: разделы, пометки «нужны данные», таблица норм с отметкой проверки."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(11)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(f"{draft.get('title')}\n{draft.get('object_name') or 'объект не указан'}")
    run.bold = True
    run.font.size = Pt(14)

    warn = doc.add_paragraph()
    warn_run = warn.add_run(draft.get("disclaimer") or DISCLAIMER)
    warn_run.italic = True
    warn_run.font.size = Pt(9)

    totals = draft.get("totals") or {}
    doc.add_paragraph(
        f"Собран: {draft.get('built_at')} · статус: {_status_ru(draft.get('status'))} · "
        f"разделов: {totals.get('sections')} · пунктов: {totals.get('filled_items')} · "
        f"требуют данных: {totals.get('needs_input')}")
    work_type = draft.get("work_type")
    if work_type:
        doc.add_paragraph(f"Вид работ: {work_type.get('name')} ({work_type.get('id')})")

    for section in draft.get("sections") or []:
        doc.add_heading(section["title"], level=2)
        for item in section["items"]:
            paragraph = doc.add_paragraph(item["text"], style="List Bullet")
            if item.get("source"):
                note = paragraph.add_run(f" — {item['source']}")
                note.italic = True
                note.font.size = Pt(9)
            if item.get("quote"):
                quote = doc.add_paragraph(f"«{item['quote']}»"
                                         + (f" (стр. {item['page']})" if item.get("page") else ""))
                quote.paragraph_format.left_indent = Pt(18)
                quote.runs[0].italic = True
            if item.get("unverified"):
                flag = doc.add_paragraph("не проверено по нормативной базе")
                flag.runs[0].bold = True
                flag.paragraph_format.left_indent = Pt(18)
        for need in section.get("needs_input") or []:
            paragraph = doc.add_paragraph(style="List Bullet")
            flag = paragraph.add_run("НУЖНЫ ДАННЫЕ: ")
            flag.bold = True
            paragraph.add_run(f"{need['what']} — {need['why']}. Запросить: {need['ask']}")

    if draft.get("norms"):
        doc.add_heading("Нормативные ссылки: состояние проверки", level=2)
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        for cell, text in zip(table.rows[0].cells,
                              ("Документ", "Пункт", "Редакция", "Сверено с базой")):
            cell.text = text
            cell.paragraphs[0].runs[0].bold = True
        for norm in draft["norms"]:
            row = table.add_row().cells
            row[0].text = str(norm.get("doc") or "—")
            row[1].text = str(norm.get("clause") or "—")
            row[2].text = str(norm.get("edition") or "—")
            row[3].text = "да" if norm["verified"] else f"нет — {norm['reason']}"

    confirmed = draft.get("confirmed_by")
    doc.add_heading("Проверка документа", level=2)
    if confirmed:
        doc.add_paragraph(f"Проверил: {confirmed['by']}, {confirmed['role']} "
                          f"({confirmed.get('at')}).")
        if confirmed.get("reason"):
            doc.add_paragraph(f"Основание: {confirmed['reason']}")
        doc.add_paragraph(f"Внесено инженером пунктов: "
                          f"{len(confirmed.get('filled_items') or [])}")
        if confirmed.get("sections_without_data"):
            doc.add_paragraph("Разделы без данных: "
                              + ", ".join(confirmed["sections_without_data"]))
    else:
        doc.add_paragraph("Черновик не проверен человеком. Подпись: ______________ / "
                          "______________________ / дата «___» __________ 20___ г.")

    doc.save(path)
    return path


def write(draft: dict, out_dir: str | Path) -> dict:
    """Сохранить черновик: JSON (машинный), Markdown (чтение), DOCX (для инженера)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{draft.get('kind') or 'tk'}_draft"
    (out / f"{stem}.json").write_text(json.dumps(draft, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    (out / f"{stem}.md").write_text(to_markdown(draft), encoding="utf-8")
    export_docx(draft, out / f"{stem}.docx")
    return {"json": str(out / f"{stem}.json"), "markdown": str(out / f"{stem}.md"),
            "docx": str(out / f"{stem}.docx")}
