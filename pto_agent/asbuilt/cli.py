# -*- coding: utf-8 -*-
"""CLI исполнительной документации: python -m pto_agent asbuilt <команда>."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .aosr import aosr_to_markdown, build_aosr
from .registry import build_registry, check_completeness, registry_to_markdown
from .scheme import build_scheme, scheme_to_markdown


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
                          notes=args.note, with_image=not args.no_image, dpi=args.dpi)
    _write_md(Path(args.out) / "scheme.md", scheme_to_markdown(result))
    print(f"Исполнительная схема: {result['docx']}")
    print(f"  лист: {Path(result['drawing']).name} · объёмов {result['positions']} "
          f"(с объёмом {result['volumes_estimated']})")
    if result["missing_fields"]:
        print(f"  не заполнено: {', '.join(result['missing_fields'])} — подписывать нельзя",
              file=sys.stderr)
    return 0


def cmd_aosr(args) -> int:
    spec = _load_spec(args.spec)
    result = build_aosr(spec, args.out)
    _write_md(Path(args.out) / "aosr.md", aosr_to_markdown(result))
    print(f"АОСР № {result['act_number'] or '—'} от {result['act_date']}: {result['docx']}")
    print(f"  приложений ИД: {result['attachments']} · "
          f"{'готов к подписанию' if result['ready_to_sign'] else 'подписывать нельзя'}")
    if result["missing_fields"]:
        print(f"  не хватает: {', '.join(result['missing_fields'])}", file=sys.stderr)
    return 0 if result["ready_to_sign"] else 1


def cmd_registry(args) -> int:
    spec = _load_spec(args.spec)
    result = build_registry(spec, args.out)
    _write_md(Path(args.out) / "registry.md", registry_to_markdown(result))
    comp = result["completeness"]
    print(f"Реестр ИД: {result['xlsx']}")
    print(f"  документов {result['count']} · листов {result['sheets']} · {comp['verdict']}")
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
    comp = check_completeness(spec, required=required, act_ref=args.act_ref)
    print(json.dumps(comp, ensure_ascii=False, indent=2))
    return 0 if comp["complete"] else 1


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
    s.set_defaults(func=cmd_scheme)

    a = sub.add_parser("aosr", help="АОСР по форме РД-11-02-2006 из спецификации")
    a.add_argument("--spec", required=True, help="JSON/YAML с данными акта")
    a.add_argument("--out", default="out/asbuilt")
    a.set_defaults(func=cmd_aosr)

    r = sub.add_parser("registry", help="реестр ИД (XLSX) и проверка комплектности")
    r.add_argument("--spec", required=True)
    r.add_argument("--out", default="out/asbuilt")
    r.set_defaults(func=cmd_registry)

    c = sub.add_parser("check", help="только проверка комплектности (JSON в stdout)")
    c.add_argument("--spec", required=True)
    c.add_argument("--require", default="", help="обязательные типы через запятую")
    c.add_argument("--act-ref", default="")
    c.set_defaults(func=cmd_check)
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
