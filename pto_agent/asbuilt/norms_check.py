# -*- coding: utf-8 -*-
"""Проверка нормативных ссылок по нормативной базе агента (а не «по памяти модели»).

Матрица видов работ — это профиль, а не первоисточник: в ней стоят обозначения и пункты,
которые кто-то когда-то внёс. Поэтому каждая ссылка из черновика акта сверяется с базой
`data/demo/norms` (front-matter + пункты `## 5.3.3`): есть ли такой документ, есть ли в нём
такой пункт, совпадает ли редакция.

Три разных ответа, которые нельзя смешивать:

* `verified` — документ и пункт найдены в базе;
* `clause_missing` — документ есть, пункта в базе нет (опечатка или другой пункт);
* `doc_missing` — документа в базе нет вообще. Это **не** значит «ссылка неверна»:
  значит «мы её не проверили». Так и пишем, и не выдаём за подтверждённую.

Плюс дословная проверка цитаты (`cite_verify` из `pto_agent.norms`) — чтобы в акт не попал
пересказ вместо текста пункта.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..norms import cite_verify, load_norms

_ROOT = Path(__file__).resolve().parents[2]
# рабочая база + демонстрационная: документ, которого нет в рабочей, ищется в демо
DEFAULT_NORMS_DIRS = (_ROOT / "data" / "norms", _ROOT / "data" / "demo" / "norms")
DEFAULT_NORMS_DIR = DEFAULT_NORMS_DIRS[0]


def _norm_designation(text: str) -> str:
    """Нормализовать обозначение: «СП 70.13330.2012» == «сп 70.13330.2012 »."""
    text = str(text or "").lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text).strip(" .,")
    text = text.replace("№", "").strip()
    text = re.sub(r"\s*,\s*", " ", text)
    return text


def _norm_clause(text: str) -> str:
    text = str(text or "").lower().replace("ё", "е").strip()
    text = re.sub(r"^(п\.?|пункт|пп\.?|табл\.?|прил\.?)\s*", "", text)
    return text.strip(" .;")


def _folders(folder) -> list[Path]:
    if not folder:
        return [d for d in DEFAULT_NORMS_DIRS if d.exists()]
    if isinstance(folder, (list, tuple)):
        return [Path(f) for f in folder if Path(f).exists()]
    return [Path(folder)]


def load_base(folder=None) -> dict:
    """Индекс базы: обозначение → {clauses, meta}. Пустая база — валидный случай.

    `folder` — каталог или список каталогов; по умолчанию `data/norms` + `data/demo/norms`.
    """
    folders = _folders(folder)
    base = {"dir": ", ".join(str(f) for f in folders) if folders else str(DEFAULT_NORMS_DIR),
            "exists": bool(folders), "docs": {}}
    for folder in folders:
        _load_into(base, folder)
    return base


def _load_into(base: dict, folder: Path) -> None:
    for clause in load_norms(folder):
        if clause.doc.designation.lower().startswith(("readme", "index")):
            continue                       # README — инструкция, а не документ базы
        doc = clause.doc
        key = _norm_designation(doc.designation)
        slot = base["docs"].setdefault(key, {"designation": doc.designation,
                                             "edition": doc.edition,
                                             "status": doc.status,
                                             "file": doc.file,
                                             "partial": False,
                                             "clauses": set()})
        slot["clauses"].add(_norm_clause(clause.clause))
        slot["partial"] = str(doc.status or "").lower() in {"fragment", "partial"}



def verify_norms(norms, folder: str | Path | None = None, base: dict | None = None) -> dict:
    """Сверить перечень норм с базой. Ничего не «додумывает»."""
    base = base or load_base(folder)
    results = []
    for norm in norms or []:
        if isinstance(norm, str):
            doc, clause = norm, ""
        else:
            doc = str(norm.get("doc") or norm.get("document") or "")
            clause = str(norm.get("clause") or norm.get("item") or "")
        key = _norm_designation(doc)
        found = base["docs"].get(key)
        if not clause:
            status, note = "no_clause", "пункт не указан — проверять нечего"
        elif not base["exists"]:
            status, note = "base_absent", f"нормативная база не найдена: {base['dir']}"
        elif found is None:
            status, note = "doc_missing", ("документа нет в базе агента — ссылка не проверена; "
                                           "добавить документ в базу или подтвердить вручную")
        elif _norm_clause(clause) not in {_norm_clause(c) for c in found["clauses"]}:
            status, note = "clause_missing", (
                f"в базе есть {found['designation']} ({found['edition'] or 'редакция не указана'}), "
                f"но пункта {clause} нет; в базе: {', '.join(sorted(found['clauses'])[:6])}")
        elif found.get("partial"):
            # документ в базе есть, но загружен фрагментом: пункт найден, полного текста нет
            status, note = "verified_partial", (
                f"пункт есть в базе фрагментом ({found['file']}), полный текст документа "
                f"не загружен — цитату проверять по первоисточнику")
        else:
            status, note = "verified", f"подтверждено базой ({found['file']})"
        trust = "matrix" if isinstance(norm, dict) and norm.get("source") == "matrix" else "spec"
        results.append({"doc": doc, "clause": clause, "status": status, "note": note,
                        "source": trust, "in_base": found is not None})
    verified = [r for r in results if r["status"] in {"verified", "verified_partial"}]
    unverified = [r for r in results if r["status"] not in {"verified", "verified_partial"}]
    partial = [r for r in results if r["status"] == "verified_partial"]
    return {
        "base": {"dir": base["dir"], "exists": base["exists"],
                 "documents": sorted(d["designation"] for d in base["docs"].values())},
        "results": results,
        "total": len(results),
        "verified": len(verified),
        "verified_partial": len(partial),
        "unverified": len(unverified),
        "doc_missing": sorted({r["doc"] for r in results if r["status"] == "doc_missing"}),
        "clause_missing": [{"doc": r["doc"], "clause": r["clause"]}
                           for r in results if r["status"] == "clause_missing"],
        "verified_share": round(len(verified) / len(results), 3) if results else 0.0,
        "verdict": ("все ссылки подтверждены базой" if results and not unverified and not partial else
                    f"подтверждено базой: {len(verified)} из {len(results)}"
                    f" (фрагментами: {len(partial)}); не проверено: {len(unverified)}"
                    if results and unverified else
                    f"подтверждено фрагментами: {len(partial)} из {len(results)}" if results and partial else
                    f"не подтверждено базой: {len(unverified)} из {len(results)}"
                    if results else "ссылок нет"),
    }


def verify_quote(quote: str, doc: str, clause: str,
                 folder: str | Path | None = None) -> dict:
    """Дословная проверка цитаты по тексту пункта из базы (а не по памяти модели)."""
    folder = Path(folder) if folder else DEFAULT_NORMS_DIR
    key_doc, key_clause = _norm_designation(doc), _norm_clause(clause)
    for c in load_norms(folder):
        if _norm_designation(c.doc.designation) == key_doc and _norm_clause(c.clause) == key_clause:
            ok = cite_verify(quote, c.body)
            return {"ok": ok, "doc": doc, "clause": clause, "file": c.doc.file,
                    "reason": None if ok else "цитата не найдена в тексте пункта дословно",
                    "body_chars": len(c.body)}
    return {"ok": False, "doc": doc, "clause": clause,
            "reason": "пункт не найден в нормативной базе", "body_chars": 0}


def norms_to_markdown(report: dict) -> str:
    lines = ["# Проверка нормативных ссылок", "",
             f"- база: `{report['base']['dir']}` "
             f"({'найдена' if report['base']['exists'] else 'НЕ найдена'}), "
             f"документов: {len(report['base']['documents'])}",
             f"- ссылок: {report['total']} · подтверждено: {report['verified']} · "
             f"не подтверждено: {report['unverified']}",
             f"- вердикт: **{report['verdict']}**", "",
             "| Документ | Пункт | Статус | Комментарий |", "|---|---|---|---|"]
    for r in report["results"]:
        lines.append(f"| {r['doc']} | {r['clause'] or '—'} | {r['status']} | {r['note']} |")
    if report["doc_missing"]:
        lines += ["", "**Документов нет в базе агента:** " + "; ".join(report["doc_missing"]),
                  "", "Это не значит, что ссылка неверна — значит, что она не проверена. "
                  "Документ нужно добавить в `data/norms` (front-matter + пункты) или "
                  "подтвердить вручную."]
    return "\n".join(lines) + "\n"
