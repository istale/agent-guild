"""Agent description — an A2A Agent Card whose skills are OASF-shaped.

The card is the only thing an agent publishes to the world: its DID, where to
reach it, what it can do, and (this is the human-centric part) *who owns it*,
proven by the delegation credential from identity.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from identity import DelegationCredential, Identity, verify


@dataclass
class Skill:
    """OASF-style capability description."""

    id: str
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    # A hint to whoever calls (and to the humans watching) about how touchy
    # the data behind this skill is. Nothing in this repo enforces it: the
    # skill handler decides what it hands back.
    #   public / personal / private
    sensitivity: str = "personal"
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "sensitivity": self.sensitivity,
            "examples": list(self.examples),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            description=d.get("description", ""),
            tags=list(d.get("tags", [])),
            sensitivity=d.get("sensitivity", "personal"),
            examples=list(d.get("examples", [])),
        )

    def search_text(self) -> str:
        return " ".join(
            [self.id, self.name, self.description, *self.tags, *self.examples]
        ).lower()


@dataclass
class AgentCard:
    name: str
    did: str
    url: str                      # A2A JSON-RPC endpoint
    description: str = ""
    version: str = "0.1.0"
    protocol_version: str = "0.2"
    transports: list[str] = field(default_factory=lambda: ["jsonrpc-http"])
    skills: list[Skill] = field(default_factory=list)
    owner: dict | None = None     # {"label", "did"} — the human behind the agent
    delegation: dict | None = None  # DelegationCredential.to_dict()
    proof: str = ""               # agent's own signature over the claims

    # ---------- serialisation ----------
    def claims(self) -> dict:
        return {
            "name": self.name,
            "did": self.did,
            "url": self.url,
            "description": self.description,
            "version": self.version,
            "protocol_version": self.protocol_version,
            "transports": sorted(self.transports),
            "skills": [s.to_dict() for s in sorted(self.skills, key=lambda s: s.id)],
            "owner": self.owner,
            "delegation": self.delegation,
        }

    def to_dict(self) -> dict:
        return {**self.claims(), "proof": self.proof}

    @classmethod
    def from_dict(cls, d: dict) -> "AgentCard":
        return cls(
            name=d["name"],
            did=d["did"],
            url=d["url"],
            description=d.get("description", ""),
            version=d.get("version", "0.1.0"),
            protocol_version=d.get("protocol_version", "0.2"),
            transports=list(d.get("transports", ["jsonrpc-http"])),
            skills=[Skill.from_dict(s) for s in d.get("skills", [])],
            owner=d.get("owner"),
            delegation=d.get("delegation"),
            proof=d.get("proof", ""),
        )

    # ---------- signing / verification ----------
    def sign(self, agent_identity: Identity) -> "AgentCard":
        if agent_identity.did != self.did:
            raise ValueError("card DID does not match the signing key")
        self.proof = agent_identity.sign(self.claims())
        return self

    def verify(self) -> tuple[bool, str]:
        """Self-contained check: card signature + owner delegation chain."""
        if not verify(self.did, self.claims(), self.proof):
            return False, "bad agent card signature"
        if self.delegation is None:
            return True, "ok (unowned agent)"
        cred = DelegationCredential.from_dict(self.delegation)
        ok, why = cred.validate(self.did)
        if not ok:
            return False, f"delegation: {why}"
        if self.owner and self.owner.get("did") != cred.issuer:
            return False, "declared owner does not match credential issuer"
        return True, "ok"

    def skill(self, skill_id: str) -> Skill | None:
        return next((s for s in self.skills if s.id == skill_id), None)

    def owner_scopes(self) -> list[str]:
        if not self.delegation:
            return []
        return list(self.delegation.get("scopes", []))


def build_card(
    *,
    name: str,
    agent_identity: Identity,
    url: str,
    owner: Identity,
    scopes: list[str],
    skills: list[Skill],
    description: str = "",
) -> AgentCard:
    """Make a fully-signed, owned agent card in one call."""
    cred = DelegationCredential.issue(owner, agent_identity.did, scopes)
    card = AgentCard(
        name=name,
        did=agent_identity.did,
        url=url,
        description=description,
        skills=skills,
        owner={"label": owner.label, "did": owner.did},
        delegation=cred.to_dict(),
    )
    return card.sign(agent_identity)
