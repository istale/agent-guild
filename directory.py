"""Directory — AGNTCY-style discovery over signed agent cards.

Registration is authenticated by the card itself: the card carries the agent's
signature and its owner's delegation credential, so the directory can reject
anything unsigned or impersonated without holding a single shared secret.

Search is capability-based (OASF skills), with an optional owner filter, so
"who can book a meeting for a human I trust" is one query.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_card import AgentCard

_WORD = re.compile(r"[a-z0-9_.]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


@dataclass
class Record:
    card: AgentCard
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    health: str = "unknown"          # unknown | healthy | unreachable

    def to_dict(self) -> dict:
        return {
            "card": self.card.to_dict(),
            "registered_at": self.registered_at,
            "last_seen": self.last_seen,
            "health": self.health,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        return cls(card=AgentCard.from_dict(d["card"]),
                   registered_at=d.get("registered_at", time.time()),
                   last_seen=d.get("last_seen", time.time()),
                   health=d.get("health", "unknown"))


@dataclass
class Match:
    card: AgentCard
    score: float
    matched_skills: list[str]
    health: str


class Directory:
    def __init__(self, store: str | Path | None = None):
        self.records: dict[str, Record] = {}
        self.store = Path(store) if store else None
        if self.store and self.store.exists():
            for raw in json.loads(self.store.read_text()).get("records", []):
                rec = Record.from_dict(raw)
                self.records[rec.card.did] = rec

    # ---------------------------------------------------------- write side
    def register(self, card: AgentCard) -> Record:
        ok, why = card.verify()
        if not ok:
            raise ValueError(f"refusing to register card: {why}")
        existing = self.records.get(card.did)
        rec = Record(
            card=card,
            registered_at=existing.registered_at if existing else time.time(),
        )
        self.records[card.did] = rec
        self._persist()
        return rec

    def deregister(self, did: str) -> bool:
        gone = self.records.pop(did, None) is not None
        if gone:
            self._persist()
        return gone

    def heartbeat(self, did: str, health: str = "healthy") -> Record:
        rec = self.records[did]
        rec.health, rec.last_seen = health, time.time()
        self._persist()
        return rec

    def mark_stale(self, max_silence: int = 120) -> list[str]:
        cutoff, stale = time.time() - max_silence, []
        for did, rec in self.records.items():
            if rec.last_seen < cutoff and rec.health != "unreachable":
                rec.health, stale = "unreachable", stale + [did]
        if stale:
            self._persist()
        return stale

    # ---------------------------------------------------------- read side
    def get(self, did: str) -> Record | None:
        return self.records.get(did)

    def resolve(self, name_or_did: str) -> Record | None:
        if rec := self.records.get(name_or_did):
            return rec
        lowered = name_or_did.lower()
        return next((r for r in self.records.values()
                     if r.card.name.lower() == lowered), None)

    def list(self) -> list[Record]:
        return sorted(self.records.values(), key=lambda r: r.card.name)

    def search(self, query: str, *, owner_did: str | None = None,
               skill_id: str | None = None, limit: int = 10,
               include_unreachable: bool = True) -> list[Match]:
        """Score cards by keyword overlap with their OASF skill descriptions."""
        terms = set(tokenize(query))
        results: list[Match] = []
        for rec in self.records.values():
            if owner_did and (rec.card.owner or {}).get("did") != owner_did:
                continue
            if not include_unreachable and rec.health == "unreachable":
                continue
            best: list[tuple[float, str]] = []
            for skill in rec.card.skills:
                if skill_id and skill.id != skill_id:
                    continue
                if skill_id and skill.id == skill_id:
                    best.append((1.0, skill.id))
                    continue
                tokens = set(tokenize(skill.search_text()))
                hits = terms & tokens
                if hits:
                    best.append((len(hits) / max(len(terms), 1), skill.id))
            if not best:
                continue
            best.sort(reverse=True)
            score = best[0][0] + 0.05 * (len(best) - 1)
            if rec.health == "healthy":
                score += 0.1
            results.append(Match(card=rec.card, score=round(score, 4),
                                 matched_skills=[s for _, s in best],
                                 health=rec.health))
        results.sort(key=lambda m: (-m.score, m.card.name))
        return results[:limit]

    # ---------------------------------------------------------- persistence
    def _persist(self) -> None:
        if not self.store:
            return
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps(
            {"records": [r.to_dict() for r in self.records.values()]}, indent=2))
