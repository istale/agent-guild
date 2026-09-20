"""Identity layer — did:key + Ed25519 signatures + delegation credentials.

This is the "who are you" half of the AGNTCY-style infrastructure, plus a
verifiable link from a *human* to the agent acting on their behalf: an
ownership credential, so a transcript or a task log can say whose agent spoke.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# multicodec prefix for ed25519-pub, as used by did:key
_ED25519_MULTICODEC = b"\xed\x01"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unb64u(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def canonical(payload: dict) -> bytes:
    """Deterministic bytes for signing: sorted keys, no whitespace, raw UTF-8.

    `ensure_ascii=False` matters for interop: Python would otherwise escape
    non-ASCII characters while JavaScript's JSON.stringify emits them raw, and
    the two sides would then sign different bytes for the same object.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def did_from_public_key(pub: Ed25519PublicKey) -> str:
    raw = pub.public_bytes_raw()
    return "did:key:z" + b64u(_ED25519_MULTICODEC + raw)


def public_key_from_did(did: str) -> Ed25519PublicKey:
    if not did.startswith("did:key:z"):
        raise ValueError(f"unsupported DID method: {did}")
    body = unb64u(did[len("did:key:z"):])
    if not body.startswith(_ED25519_MULTICODEC):
        raise ValueError("DID is not an ed25519 key")
    return Ed25519PublicKey.from_public_bytes(body[len(_ED25519_MULTICODEC):])


@dataclass
class Identity:
    """A keypair with a DID. Used for humans (owners) and for agents."""

    label: str
    _private: Ed25519PrivateKey

    @classmethod
    def generate(cls, label: str) -> "Identity":
        return cls(label=label, _private=Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, label: str, path: str | Path) -> "Identity":
        path = Path(path)
        if path.exists():
            seed = unb64u(json.loads(path.read_text())["seed"])
            return cls(label=label, _private=Ed25519PrivateKey.from_private_bytes(seed))
        ident = cls.generate(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"label": label, "seed": b64u(ident.seed)}))
        path.chmod(0o600)
        return ident

    @property
    def seed(self) -> bytes:
        return self._private.private_bytes_raw()

    @property
    def did(self) -> str:
        return did_from_public_key(self._private.public_key())

    def sign(self, payload: dict) -> str:
        return b64u(self._private.sign(canonical(payload)))


def verify(did: str, payload: dict, signature: str) -> bool:
    try:
        public_key_from_did(did).verify(unb64u(signature), canonical(payload))
        return True
    except (InvalidSignature, ValueError):
        return False


@dataclass
class DelegationCredential:
    """A W3C-VC-shaped claim: "this human owns/authorises this agent".

    `scopes` is the human's declaration of what they stood this agent up to do.
    It is published in the agent card for others to read; nothing in this repo
    enforces it.
    """

    issuer: str          # the human's DID
    subject: str         # the agent's DID
    owner_label: str
    scopes: list[str] = field(default_factory=list)
    issued_at: int = 0
    expires_at: int = 0
    proof: str = ""

    def claims(self) -> dict:
        return {
            "type": "AgentDelegationCredential",
            "issuer": self.issuer,
            "subject": self.subject,
            "owner_label": self.owner_label,
            "scopes": sorted(self.scopes),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def to_dict(self) -> dict:
        return {**self.claims(), "proof": self.proof}

    @classmethod
    def from_dict(cls, data: dict) -> "DelegationCredential":
        return cls(
            issuer=data["issuer"],
            subject=data["subject"],
            owner_label=data.get("owner_label", ""),
            scopes=list(data.get("scopes", [])),
            issued_at=int(data.get("issued_at", 0)),
            expires_at=int(data.get("expires_at", 0)),
            proof=data.get("proof", ""),
        )

    @classmethod
    def issue(
        cls,
        owner: Identity,
        agent_did: str,
        scopes: list[str],
        ttl_seconds: int = 90 * 86400,
    ) -> "DelegationCredential":
        now = int(time.time())
        cred = cls(
            issuer=owner.did,
            subject=agent_did,
            owner_label=owner.label,
            scopes=list(scopes),
            issued_at=now,
            expires_at=now + ttl_seconds,
        )
        cred.proof = owner.sign(cred.claims())
        return cred

    def validate(self, agent_did: str | None = None) -> tuple[bool, str]:
        if agent_did is not None and agent_did != self.subject:
            return False, "credential subject is not this agent"
        if self.expires_at and self.expires_at < int(time.time()):
            return False, "credential expired"
        if not verify(self.issuer, self.claims(), self.proof):
            return False, "bad issuer signature"
        return True, "ok"
