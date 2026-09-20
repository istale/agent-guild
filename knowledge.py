"""The guild's loot: answers that internal agents already worked out.

Every time an internal agent answers an ask that was directed at it, the
platform files the pair away. The front-line agent checks here *before*
escalating, so the second customer with the same problem is answered from the
log and nobody's domain expert is interrupted twice for the same thing.

Entries are written by the platform, never by an agent claiming "this is
knowledge", and each one keeps the name of the agent and the human it came
from — so an answer in the log is still attributable, and a wrong one is
traceable to whoever produced it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException

from directory import tokenize
from envelope import new_id

# Words that carry no meaning for matching a support question.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "is", "are", "was", "were",
    "i", "my", "me", "you", "your", "it", "this", "that", "for", "to", "of",
    "in", "on", "at", "with", "please", "have", "has", "had", "do", "does",
    "can", "could", "would", "will", "not", "no", "am", "be", "been", "get",
    "got", "just", "there", "they", "them", "customer", "says", "look", "take",
}


CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,}")


def stem(word: str) -> str:
    """Crudest useful stemmer: "charged", "charges" and "charge" must match.

    Not linguistics — just enough that a customer writing "duplicate charge"
    hits an answer filed under "charged twice".
    """
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def keywords(text: str) -> set[str]:
    """Latin word stems plus CJK character bigrams.

    `tokenize` only sees ASCII-ish words, so Chinese would contribute nothing
    at all. Bigrams give it something to match on without needing a segmenter.
    """
    words = {stem(t) for t in tokenize(text)
             if t not in STOPWORDS and len(t) > 2}
    for run in CJK_RUN.findall(text):
        words |= {run[i:i + 2] for i in range(len(run) - 1)}
    return words


@dataclass
class Entry:
    question: str
    answer: str
    by_agent: str
    by_human: str
    room_id: str
    entry_id: str = field(default_factory=lambda: new_id("kb"))
    created_at: float = field(default_factory=time.time)
    used: int = 0

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id, "question": self.question,
            "answer": self.answer, "by_agent": self.by_agent,
            "by_human": self.by_human, "room_id": self.room_id,
            "created_at": self.created_at, "used": self.used,
        }


@dataclass
class Match:
    entry: Entry
    score: float
    overlap: list[str]

    def to_dict(self) -> dict:
        return {**self.entry.to_dict(), "score": round(self.score, 4),
                "overlap": self.overlap}


class KnowledgeBase:
    def __init__(self, *, min_overlap: int = 2):
        self.entries: dict[str, Entry] = {}
        # One shared word ("invoice") is a coincidence; two is a lead.
        self.min_overlap = min_overlap

    def record(self, *, question: str, answer: str, by_agent: str,
               by_human: str, room_id: str) -> Entry | None:
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            return None
        for existing in self.entries.values():
            if existing.question == question and existing.answer == answer:
                return None                      # already filed
        entry = Entry(question=question, answer=answer, by_agent=by_agent,
                      by_human=by_human, room_id=room_id)
        self.entries[entry.entry_id] = entry
        return entry

    def search(self, query: str, limit: int = 5) -> list[Match]:
        wanted = keywords(query)
        if not wanted:
            return []
        matches: list[Match] = []
        for entry in self.entries.values():
            known = keywords(entry.question) | keywords(entry.answer)
            shared = wanted & known
            if len(shared) < self.min_overlap:
                continue
            score = len(shared) / len(wanted)
            matches.append(Match(entry=entry, score=score,
                                 overlap=sorted(shared)))
        matches.sort(key=lambda m: (-m.score, -m.entry.used))
        return matches[:limit]

    def mark_used(self, entry_id: str) -> Entry:
        entry = self.entries[entry_id]
        entry.used += 1
        return entry

    def list(self) -> list[Entry]:
        return sorted(self.entries.values(), key=lambda e: -e.created_at)


store = KnowledgeBase()
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("")
def search(q: str = "", limit: int = 5) -> dict:
    """With a query, the best matches; without one, everything on file."""
    if not q:
        return {"entries": [e.to_dict() for e in store.list()],
                "count": len(store.entries)}
    return {"query": q, "count": len(store.entries),
            "matches": [m.to_dict() for m in store.search(q, limit=limit)]}


@router.post("/{entry_id}/used")
def mark_used(entry_id: str) -> dict:
    """An agent reused this answer. Counted, so /ops can show what pays off."""
    try:
        return store.mark_used(entry_id).to_dict()
    except KeyError:
        raise HTTPException(404, "no such entry") from None
