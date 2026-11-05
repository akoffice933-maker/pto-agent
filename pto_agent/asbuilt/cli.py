# -*- coding: utf-8 -*-
"""CLI исполнительной документации: python -m pto_agent asbuilt <команда>."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .acts_plan import plan_acts, plan_to_markdown, plan_to_specs
from .aosr import aosr_to_markdown, build_aosr
from .norms_check import DEFAULT_NORMS_DIR, norms_to_markdown, verify_norms, verify_quote
from .requirements import classify_positions
from .forms import forms_catalog
from .registry import build_registry, check_completeness, registry_to_markdown
from .requirements import matrix_summary, required_kinds, work_type_requirements
from .scheme import build_scheme, scheme_to_markdown
from .spec_from_estimate import (load_positions, spec_from_estimate, spec_to_markdown)


def _load_spec(path: str | Path | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"нет файла спецификации: {p}")
    raw = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(raw) or {}
    return json.loads(raw)


def _write_md(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def cmd_scheme(args) -> int:
    result = build_scheme(args.file, args.out, object_name=args.object_name, work=args.work,
                          code=args.code, act_ref=args.act_ref, executor=args.executor,
                          reviewer=args.reviewer, organization=args.organization,
                          drawing_no=args.drawing_no, scheme_date=args.date,
                          notes=args.note, with_image=not args.no_image, dpi=args.dpi,
                          template_path=args.template)
    _write_md(Path(args.out) / "scheme.md", scheme_to_markdown(result))
    print(f"Исполнительная схема: {result['docx']}")
    print(f"  лист: {Path(result['drawing']).name} · объёмов {result['positions']} "
          f"(с объёмом {result['volumes_estimated']})")
    if result.get("template_blocking"):
        print(f"  ✗ оформление листа: {result['template_blocking']} блокирующих замечаний",
              file=sys.stderr)
    if result["missing_fields"]:
        print(f"  не заполнено: {', '.join(result['missing_fields'])} — подписывать нельзя",
              file=sys.stderr)
    return 0


def cmd_aosr(args) -> int:
    spec = _load_spec(args.spec)
    if args.work_type:
        spec["work_types"] = [w for w in args.work_type.split(",") if w]
    result = build_aosr(spec, args.out, form=args.form, norms_dir=args.norms_dir)
    _write_md(Path(args.out) / "aosr.md", aosr_to_markdown(result))
    print(f"ЧЕРНОВИК АОСР № {result['act_number'] or '—'} от {result['act_date']}: {result['docx']}")
    print(f"  форма: {result['form']['short']}")
    print(f"  приложений ИД: {result['attachments']} · статус: {result['status']} "
          f"({'можно передавать на подпись' if result['can_transfer'] else 'есть блокеры'})")
    if result["form"]["warning"]:
        print(f"  ВНИМАНИЕ: {result['form']['warning']}", file=sys.stderr)
    report = (result["validation"] or {}).get("norms_report")
    if report and report.get("unverified"):
        print(f"  нормы: подтверждено базой {report['verified']} из {report['total']} "
              f"(нет в базе: {', '.join(report['doc_missing']) or '—'})", file=sys.stderr)
    for issue in result["validation"]["blocking"]:
        print(f"  ✗ {issue['text']}", file=sys.stderr)
    for issue in result["validation"]["warnings"]:
        print(f"  ! {issue['text']}", file=sys.stderr)
    return 0 if result["can_transfer"] else 1


def cmd_registry(args) -> int:
    spec = _load_spec(args.spec)
    if args.work_type:
        spec["work_types"] = [w for w in args.work_type.split(",") if w]
    result = build_registry(spec, args.out)
    _write_md(Path(args.out) / "registry.md", registry_to_markdown(result))
    comp = result["completeness"]
    print(f"Реестр ИД: {result['xlsx']}")
    print(f"  документов {result['count']} · листов {result['sheets']} · {comp['verdict']}")
    print(f"  профиль: {comp['profile']['name']}/v{comp['profile']['version']} · "
          f"требования: {comp['required_source']}")
    for proto in comp.get("missing_protocols") or []:
        print(f"  ✗ нет протокола: {proto}", file=sys.stderr)
    if comp["missing"]:
        for m in comp["missing"]:
            print(f"  ✗ нет документа: {m['kind']} — {m['what']}", file=sys.stderr)
    if comp["foreign_act"]:
        for d in comp["foreign_act"]:
            print(f"  ✗ другой акт: {d['name']} ({d['act_ref']})", file=sys.stderr)
    return 0 if comp["complete"] else 1


def cmd_check(args) -> int:
    spec = _load_spec(args.spec)
    required = [r for r in (args.require or "").split(",") if r] or None
    work_types = [w for w in (args.work_type or "").split(",") if w] or None
    comp = check_completeness(spec, required=required, act_ref=args.act_ref,
                              work_types=work_types)
    print(json.dumps(comp, ensure_ascii=False, indent=2))
    return 0 if comp["complete"] else 1


def cmd_requirements(args) -> int:
    if args.work_type:
        payload = work_type_requirements(args.work_type)
    else:
        payload = {"matrix": matrix_summary(), "profile": load_matrix_profile()}
    if (args.format or "json") == "markdown":
        print(payload.get("matrix") or matrix_summary(), end="")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok", True) else 1


def load_matrix_profile() -> dict:
    from .requirements import load_matrix
    return load_matrix().get("profile")


def cmd_forms(args) -> int:
    print(json.dumps(forms_catalog(), ensure_ascii=False, indent=2))
    return 0


def cmd_spec(args) -> int:
    matrix = None
    positions, source = load_positions(smeta=args.smeta, summary_path=args.summary)
    spec = spec_from_estimate(positions, object_name=args.object_name,
                              act_number=args.act_number or "", act_date=args.date or "",
                              start_date=args.start or "", end_date=args.end or "",
                              work=args.work or "",
                              work_types=[w for w in (args.work_type or "").split(",") if w] or None,
                              matrix=matrix, source=source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / (args.name or "aosr_spec.json")
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_md(out / (path.stem + ".md"), spec_to_markdown(spec))
    print(f"Черновик спецификации АОСР: {path}")
    print(f"  позиций: {spec['source']['positions_total']} "
          f"(опознано {spec['source']['positions_classified']}) · "
          f"виды работ: {', '.join(spec['work_types']) or '—'}")
    print(f"  норм с пунктами: {len(spec['norms'])} · шаблон приложений: {len(spec['attachments'])}")
    if spec["needs_input"]:
        print(f"  заполнить вручную: {', '.join(spec['needs_input'])}", file=sys.stderr)
    return 0


def cmd_smeta(args) -> int:
    """Разбор сметы как есть: АРОС (md/html) или xlsx — что агент в ней увидел."""
    path = Path(args.smeta)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload: dict = {"file": path.name}
    if path.suffix.lower() in {".md", ".markdown", ".html", ".htm"}:
        from .aros import aros_to_markdown, read_aros, section_summary
        doc = read_aros(path)
        payload.update({"format": "АРОС (html/markdown-выгрузка)",
                        "totals": doc["totals"], "sections": section_summary(doc),
                        "works": len(doc["works"]), "materials": len(doc["materials"]),
                        "positions": [{k: p[k] for k in ("n", "code", "name", "unit", "qty",
                                                          "section", "kind")}
                                      for p in doc["positions"]]})
        md = aros_to_markdown(doc)
    else:
        payload["format"] = "xlsx"
        md = f"# Смета {path.name}\n\nразбор xlsx: позиции читаются как работы\n"
    positions, source = load_positions(smeta=path)
    classified = classify_positions(positions)
    payload["source"] = source
    payload["classification"] = {
        "total": classified["total"], "classified": classified["classified"],
        "work_types": [{"work_type": w["work_type"], "name": w["name"],
                        "positions": len(w["positions"]), "share": w.get("share")}
                       for w in classified["work_types"]],
        "candidates": [{"work_type": w["work_type"], "positions": len(w["positions"])}
                       for w in classified["candidates"]],
        "unknown": classified["unknown"], "merged": classified.get("merged") or []}
    (out / "smeta_parse.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    lines = [md.rstrip(), "", "## Классификация работ", "",
             f"- работ: {classified['total']} · распознано: {classified['classified']}",
             "", "| Вид работ | Позиций | Доля |", "|---|---|---|"]
    for w in classified["work_types"]:
        lines.append(f"| {w['name']} | {len(w['positions'])} | {w.get('share')} |")
    for w in classified["candidates"]:
        lines.append(f"| {w['name']} (кандидат, в комплект не идёт) | {len(w['positions'])} | "
                     f"{w.get('share')} |")
    if classified["unknown"]:
        lines += ["", "**Не распознано:**"] + [f"- {u['name']}" for u in classified["unknown"]]
    _write_md(out / "smeta_parse.md", "\n".join(lines) + "\n")
    print(f"Разбор сметы: {out / 'smeta_parse.json'}")
    print(f"  работ {classified['total']}: распознано {classified['classified']} · "
          f"видов работ {len(classified['work_types'])}")
    for w in classified["work_types"]:
        print(f"  {w['work_type']}: позиций {len(w['positions'])} (доля {w.get('share')})")
    if classified["unknown"]:
        print(f"  не распознано: {len(classified['unknown'])} — "
              f"{'; '.join(str(u['name'])[:40] for u in classified['unknown'][:3])}",
              file=sys.stderr)
    return 0


def cmd_acts(args) -> int:
    positions, source = load_positions(smeta=args.smeta, summary_path=args.summary)
    plan = plan_acts(positions,
                     work_types=[w for w in (args.work_type or "").split(",") if w] or None,
                     act_prefix=args.act_prefix, object_name=args.object_name or "",
                     source=source, act_date=args.date)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "acts_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    _write_md(out / "acts_plan.md", plan_to_markdown(plan))
    print(f"План актов: {out / 'acts_plan.json'}")
    print(f"  позиций {plan['positions_total']}: в актах {plan['positions_in_acts']}, "
          f"вне актов {plan['positions_excluded']} · актов {len(plan['acts'])}")
    for act in plan["acts"]:
        print(f"  {act['code']}: {act['group_name']} — позиций {act['positions_count']}")
    if args.write_specs:
        specs_dir = out / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)
        for spec in plan_to_specs(plan):
            name = str(spec["act_number"]).replace("/", "-") or "act"
            (specs_dir / f"aosr_{name}.json").write_text(
                json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  спецификации актов: {specs_dir}")
    if plan.get("warning"):
        print(f"  вне актов: {plan['warning']}", file=sys.stderr)
    return 0


def cmd_verify_norms(args) -> int:
    norms_dir = args.norms_dir or None      # None → рабочая база + демо-база
    if args.quote:
        result = verify_quote(args.quote, args.doc or "", args.clause or "", norms_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    spec = _load_spec(args.spec) if args.spec else {}
    norms = spec.get("norms") or []
    report = verify_norms(norms, norms_dir)
    out = Path(args.out) if args.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
        _write_md(out / "norms_check.md", norms_to_markdown(report))
    if (args.format or "json") == "markdown":
        print(norms_to_markdown(report), end="")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["unverified"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pto_agent asbuilt",
                                description="Исполнительная документация и схемы")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scheme", help="исполнительная схема из DXF (DOCX + PNG)")
    s.add_argument("--file", required=True, help="DXF чертежа")
    s.add_argument("--out", default="out/asbuilt")
    s.add_argument("--object-name", required=True)
    s.add_argument("--work", required=True, help="вид работ на схеме")
    s.add_argument("--code", default="", help="шифр схемы")
    s.add_argument("--act-ref", default="", help="акт КС-2, к которому относится схема")
    s.add_argument("--executor", default="")
    s.add_argument("--reviewer", default="")
    s.add_argument("--organization", default="")
    s.add_argument("--drawing-no", default="")
    s.add_argument("--date", default=None)
    s.add_argument("--note", default="")
    s.add_argument("--dpi", type=int, default=110)
    s.add_argument("--no-image", action="store_true", help="без растрового листа в DOCX")
    s.add_argument("--template", default=None,
                   help="шаблон оформления листа (по умолчанию корпоративный corp-A)")
    s.set_defaults(func=cmd_scheme)

    a = sub.add_parser("aosr", help="АОСР по форме РД-11-02-2006 из спецификации")
    a.add_argument("--spec", required=True, help="JSON/YAML с данными акта")
    a.add_argument("--out", default="out/asbuilt")
    a.add_argument("--form", default=None,
                   choices=["344pr", "rd-11-02-2006"],
                   help="форма акта: 344pr (действует с 01.09.2023) или устаревшая РД-11-02-2006")
    a.add_argument("--work-type", default="",
                   help="виды работ через запятую (earthworks, concrete, electrical …)")
    a.add_argument("--norms-dir", default=None,
                   help="нормативная база для сверки ссылок (по умолчанию data/demo/norms)")
    a.set_defaults(func=cmd_aosr)

    r = sub.add_parser("registry", help="реестр ИД (XLSX) и проверка комплектности")
    r.add_argument("--spec", required=True)
    r.add_argument("--out", default="out/asbuilt")
    r.add_argument("--work-type", default="",
                   help="виды работ через запятую — включает матрицу требований")
    r.set_defaults(func=cmd_registry)

    c = sub.add_parser("check", help="только проверка комплектности (JSON в stdout)")
    c.add_argument("--spec", required=True)
    c.add_argument("--require", default="", help="обязательные типы через запятую")
    c.add_argument("--act-ref", default="")
    c.add_argument("--work-type", default="")
    c.set_defaults(func=cmd_check)

    q = sub.add_parser("requirements", help="матрица требований по видам работ")
    q.add_argument("--work-type", default="", help="один вид работ; без него — вся матрица")
    q.add_argument("--format", default="json", choices=["json", "markdown"])
    q.set_defaults(func=cmd_requirements)

    f = sub.add_parser("forms", help="какие формы актов действуют и какие устарели")
    f.set_defaults(func=cmd_forms)

    sm = sub.add_parser("smeta", help="разбор сметы (АРОС md/html или xlsx): что увидел агент")
    sm.add_argument("--smeta", required=True, help="смета: выгрузка АРОС (.md/.html) или .xlsx")
    sm.add_argument("--out", default="out/asbuilt/smeta")
    sm.set_defaults(func=cmd_smeta)

    ac = sub.add_parser("acts", help="план отдельных АОСР по скрытым работам из сметы/КС-2")
    acsrc = ac.add_mutually_exclusive_group(required=True)
    acsrc.add_argument("--smeta", help="смета XLSX")
    acsrc.add_argument("--summary", help="out/summary.json")
    ac.add_argument("--work-type", default="", help="виды работ через запятую")
    ac.add_argument("--act-prefix", default="АОСР", help="префикс нумерации актов")
    ac.add_argument("--object-name", default="")
    ac.add_argument("--date", default=None)
    ac.add_argument("--write-specs", action="store_true",
                    help="дописать по спецификации на каждый акт (для asbuilt aosr)")
    ac.add_argument("--out", default="out/asbuilt/acts")
    ac.set_defaults(func=cmd_acts)

    vn = sub.add_parser("verify-norms", help="сверить нормативные ссылки с нормативной базой")
    vn.add_argument("--spec", default=None, help="спецификация АОСР с перечнем norms")
    vn.add_argument("--norms-dir", default=None)
    vn.add_argument("--quote", default=None, help="дословная цитата для cite_verify")
    vn.add_argument("--doc", default=None)
    vn.add_argument("--clause", default=None)
    vn.add_argument("--format", default="json", choices=["json", "markdown"])
    vn.add_argument("--out", default=None)
    vn.set_defaults(func=cmd_verify_norms)

    sp = sub.add_parser("spec", help="черновик спецификации АОСР из сметы или КС-2")
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--smeta", help="смета XLSX")
    src.add_argument("--summary", help="out/summary.json (сверка КС-2)")
    sp.add_argument("--object-name", default="")
    sp.add_argument("--act-number", default="")
    sp.add_argument("--date", default="", help="дата акта")
    sp.add_argument("--start", default="")
    sp.add_argument("--end", default="")
    sp.add_argument("--work", default="", help="наименование работ (иначе — из позиций)")
    sp.add_argument("--work-type", default="")
    sp.add_argument("--name", default="aosr_spec.json")
    sp.add_argument("--out", default="out/asbuilt")
    sp.set_defaults(func=cmd_spec)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
