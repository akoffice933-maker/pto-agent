# -*- coding: utf-8 -*-
"""Мини-RAG по нормативной базе: BM25-поиск + обязательная проверка цитаты.

Принцип: ответ без дословной цитаты, найденной в указанном пункте указанной
редакции, не выдаётся. Это техническая защита от галлюцинаций.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

TOKEN = re.compile(r"[0-9a-zа-я]+")
MIN_COVERAGE = 0.34   # минимум совпавших слов запроса, иначе честное «не найдено»


@dataclass
class NormDoc:
    designation: str
    title: str
    kind: str
    edition: str
    status: str
    effective_from: date | None
    file: str


@dataclass
class Clause:
    doc: NormDoc
    clause: str
    body: str
    tokens: list = field(default_factory=list)


@dataclass
class NormAnswer:
    answer_type: str                  # answer | not_found | ambiguous
    conclusion: str
    citations: list
    verified: bool
    confidence: str
    as_of: date
    searched: list

    def as_dict(self) -> dict:
        return {
            "answer_type": self.answer_type,
            "conclusion": self.conclusion,
            "citations": self.citations,
            "cite_verified": self.verified,
            "confidence": self.confidence,
            "as_of_date_used": self.as_of.isoformat(),
            "searched": self.searched,
        }


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[\u00a0\s]+", " ", s)
    return s.strip()


STOP = {"и", "в", "во", "на", "не", "с", "со", "для", "при", "по", "из", "от", "до",
        "или", "а", "но", "что", "как", "это", "к", "ко", "о", "об", "the", "of"}
ENDINGS = ("иями", "ями", "ами", "ыми", "ими", "ого", "его", "ому", "ему", "ых", "их",
           "ая", "ое", "ые", "ий", "ый", "ой", "ей", "ия", "ию", "ии", "ов", "ев",
           "ам", "ям", "ах", "ях", "ом", "ем", "ую", "юю", "а", "я", "ы", "и", "е",
           "у", "о", "ь")


def stem(word: str) -> str:
    """Очень лёгкий стеммер: срезает типовые русские окончания у длинных слов."""
    if len(word) <= 4:
        return word
    for end in ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 4:
            return word[: -len(end)]
    return word


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in TOKEN.findall(_norm(text)) if t not in STOP]


def _parse_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, parts[2]


def load_norms(folder: Path) -> list[Clause]:
    clauses = []
    for path in sorted(folder.glob("*.md")):
        meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
        eff = None
        if meta.get("effective_from"):
            y, m, d = meta["effective_from"].split("-")
            eff = date(int(y), int(m), int(d))
        doc = NormDoc(
            designation=meta.get("designation", path.stem),
            title=meta.get("title", ""),
            kind=meta.get("kind", "СП"),
            edition=meta.get("edition", ""),
            status=meta.get("status", "active"),
            effective_from=eff,
            file=path.name,
        )
        current, buf = None, []
        for line in body.splitlines():
            m = re.match(r"^##\s+(.+)$", line.strip())
            if m:
                if current:
                    clauses.append(Clause(doc, current, "\n".join(buf).strip()))
                current, buf = m.group(1).strip(), []
            elif current is not None:
                buf.append(line)
        if current:
            clauses.append(Clause(doc, current, "\n".join(buf).strip()))
    for c in clauses:
        c.tokens = tokenize(c.clause + " " + c.body)
    return clauses


def cite_verify(quote: str, source_text: str) -> bool:
    """Дословная проверка цитаты в тексте пункта (с нормализацией пробелов)."""
    q, s = _norm(quote), _norm(source_text)
    if len(q) < 20:
        return False
    return q in s


class NormIndex:
    def __init__(self, clauses: list[Clause], k1: float = 1.5, b: float = 0.75):
        self.clauses = clauses
        self.k1, self.b = k1, b
        self.avg_len = (sum(len(c.tokens) for c in clauses) / len(clauses)) if clauses else 0
        self.df: dict[str, int] = {}
        for c in clauses:
            for t in set(c.tokens):
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(clauses)

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, clause: Clause, query_tokens: list[str]) -> float:
        tf: dict[str, int] = {}
        for t in clause.tokens:
            tf[t] = tf.get(t, 0) + 1
        dl = len(clause.tokens) or 1
        total = 0.0
        for t in query_tokens:
            if t not in tf:
                continue
            f = tf[t]
            total += self._idf(t) * (f * (self.k1 + 1)) / (
                f + self.k1 * (1 - self.b + self.b * dl / self.avg_len))
        return total

    def coverage(self, clause: "Clause", query_tokens: list[str]) -> float:
        """Доля слов запроса, реально присутствующих в пункте (защита от «похожих» тем)."""
        if not query_tokens:
            return 0.0
        doc_tokens = set(clause.tokens)
        return sum(1 for t in set(query_tokens) if t in doc_tokens) / len(set(query_tokens))

    def search(self, query: str, top_k: int = 3, as_of: date | None = None) -> list[tuple[Clause, float]]:
        qt = tokenize(query)
        if not qt:
            return []
        as_of = as_of or date.today()
        pool = [c for c in self.clauses
                if c.doc.status == "active"
                and (c.doc.effective_from is None or c.doc.effective_from <= as_of)]
        scored = [(c, self.score(c, qt)) for c in pool]
        scored = [x for x in scored if x[1] > 0 and self.coverage(x[0], qt) >= MIN_COVERAGE]
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def ask(self, query: str, as_of: date | None = None, top_k: int = 3) -> NormAnswer:
        as_of = as_of or date.today()
        hits = self.search(query, top_k=top_k, as_of=as_of)
        if not hits:
            return NormAnswer("not_found",
                              "В загруженной нормативной базе ответ не найден.",
                              [], False, "low", as_of,
                              [f"запрос: {query}"])
        best, second = hits[0], (hits[1] if len(hits) > 1 else (None, 0.0))
        ambiguous = second[0] is not None and second[1] > 0 and best[1] - second[1] < 0.15 * best[1]

        citations = []
        for clause, score in hits[:2]:
            body = clause.body.strip()
            quote = body if len(body) <= 400 else body[:400].rsplit(" ", 1)[0] + "…"
            citations.append({
                "doc": clause.doc.designation,
                "title": clause.doc.title,
                "clause": clause.clause,
                "quote": quote,
                "edition": clause.doc.edition,
                "status": clause.doc.status,
                "actual_as_of": as_of.isoformat(),
                "file": f"{clause.doc.file}#{clause.clause}",
                "score": round(score, 3),
                "verified": cite_verify(quote, clause.body),
            })

        verified = all(c["verified"] for c in citations) and bool(citations)
        conclusion = (
            f"Наиболее применимый пункт: {citations[0]['doc']}, п. {citations[0]['clause']} "
            f"(редакция: {citations[0]['edition'] or 'действующая'}, "
            f"проверено на {as_of:%d.%m.%Y})."
        )
        if not verified:
            return NormAnswer("not_found",
                              "Цитата не прошла проверку дословности — ответ заблокирован.",
                              citations, False, "low", as_of,
                              [f"{c['doc']} п. {c['clause']}" for c, _ in hits])
        return NormAnswer(
            "ambiguous" if ambiguous else "answer",
            conclusion,
            citations,
            verified,
            "medium" if ambiguous else "high",
            as_of,
            [f"{c.doc.designation} п. {c.clause}" for c, _ in hits],
        )
