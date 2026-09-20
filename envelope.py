"""Signed envelopes — how the platform knows who is talking.

Every write an agent makes to the platform is wrapped in one of these: the
caller's agent card plus a signature over the request. The receiver learns
*which agent* and *whose human* is asking before anything else happens, with
no shared secret and no session state.

This is not the A2A protocol. A2A is what Hermes-style agents speak to each
other (HTTP bearer auth, `message/send`, tasks); an A2A bridge is still to be
written. This file is only the platform's own admission check.
"""
from __future__ import annotations

import time
import uuid

from agent_card import AgentCard
from identity import Identity, verify


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def sign_envelope(identity: Identity, card: AgentCard, rpc: dict) -> dict:
    """Wrap a JSON-RPC request with caller identity + signature."""
    envelope = {
        "callerDid": identity.did,
        "callerCard": card.to_dict(),
        "issuedAt": int(time.time()),
        "rpc": rpc,
    }
    payload = {"callerDid": envelope["callerDid"], "issuedAt": envelope["issuedAt"],
               "rpc": rpc}
    envelope["signature"] = identity.sign(payload)
    return envelope


def open_envelope(envelope: dict, *, max_age: int = 300) -> tuple[AgentCard, dict]:
    """Verify an inbound envelope. Raises ValueError on anything suspicious."""
    for key in ("callerDid", "callerCard", "rpc", "signature", "issuedAt"):
        if key not in envelope:
            raise ValueError(f"envelope missing {key}")
    did, issued_at = envelope["callerDid"], int(envelope["issuedAt"])
    payload = {"callerDid": did, "issuedAt": issued_at, "rpc": envelope["rpc"]}
    if not verify(did, payload, envelope["signature"]):
        raise ValueError("bad envelope signature")
    if abs(time.time() - issued_at) > max_age:
        raise ValueError("envelope timestamp outside allowed window")
    card = AgentCard.from_dict(envelope["callerCard"])
    if card.did != did:
        raise ValueError("caller card DID does not match signer")
    ok, why = card.verify()
    if not ok:
        raise ValueError(f"caller card rejected: {why}")
    return card, envelope["rpc"]
