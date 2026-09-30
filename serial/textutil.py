"""Cheap deterministic text checks: word counts, n-gram overlap, lexical similarity."""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z0-9']+")
STOP = set("""a an the and or but if then so of to in on at by for with from as is was were be been are
it its this that these those he she they them his her their i you we our me my your not no into out up
down over under again just than too very can could would should will shall has have had do did does""".split())


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def word_count(text: str) -> int:
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    return len(words(body))


def content_tokens(text: str) -> set[str]:
    return {w for w in words(text) if w not in STOP and len(w) > 2}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    w = words(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def overlap_ratio(draft: str, other: str, n: int = 5) -> float:
    a, b = shingles(draft, n), shingles(other, n)
    return len(a & b) / len(a) if a else 0.0


def split_title(text: str) -> tuple[str, str]:
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("#"):
        return lines[0].lstrip("# ").strip(), "\n".join(lines[1:]).strip()
    return "", text.strip()


def last_paragraph(text: str) -> str:
    paras = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    return paras[-1] if paras else ""
