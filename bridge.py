"""Bring an A2A agent (a Hermes) into the guild.

Hermes-style agents already speak A2A: enable their `a2a` platform plugin and
they serve an agent card at /.well-known/agent-card.json and accept JSON-RPC
`message/send`. What they cannot do is *pull* — A2A has no notion of watching
a board and taking work. So this process stands between the two:

    guild room ──mention/open call──► bridge ──A2A message/send──► Hermes :9900
                                        │                             │
                                        └──── posts the answer ◄──────┘

    python bridge.py --peer http://mei-laptop:9900 --owner Mei \\
        --token "$A2A_TOKEN" --platform http://host:9100

For each peer it registers a proxy participant in the directory, carrying the
skills the remote agent advertises, so `/search` can route to it and its asks
show up on /ops like everyone else's. The proxy is named after the remote
agent and says in its description that it is a bridge — nothing pretends to be
the remote agent itself.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import uuid
from pathlib import Path

import httpx

from agent_card import Skill
from connect import Participant
from knowledge import looks_like_question


class A2APeer:
    """The remote agent, as seen over A2A."""

    def __init__(self, url: str, token: str = "", timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.rpc_url = self.url
        self.token = token
        self.timeout = timeout
        self.card: dict = {}

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def fetch_card(self) -> dict:
        """A2A v1.0 publishes the card at a well-known path; older peers used
        agent.json, and Hermes answers both."""
        async with httpx.AsyncClient(timeout=20.0) as http:
            for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
                with contextlib.suppress(httpx.HTTPError):
                    resp = await http.get(f"{self.url}{path}", headers=self.headers)
                    if resp.status_code < 400:
                        self.card = resp.json()
                        self.rpc_url = self._rpc_endpoint()
                        return self.card
        raise RuntimeError(f"no agent card at {self.url}")

    def _rpc_endpoint(self) -> str:
        """An A2A card names its own JSON-RPC endpoint. Honour it only when it
        points at the same host we were told to dial — a card published for the
        outside world can carry a URL we cannot reach from here."""
        declared = str(self.card.get("url") or "").rstrip("/")
        if not declared.startswith(("http://", "https://")):
            return self.url
        mine = httpx.URL(self.url)
        theirs = httpx.URL(declared)
        same_host = (theirs.host, theirs.port) == (mine.host, mine.port)
        return declared if same_host else self.url

    def skills(self, override: list[Skill] | None = None) -> list[Skill]:
        """What to advertise on the guild's behalf.

        A Hermes card lists one entry per *toolset* (browser, spotify,
        terminal, …) with tool names as tags. Registering that verbatim makes
        the proxy match almost every open call on the board and pulls
        unrelated tickets towards it, so an operator can name the domain
        skills this peer should actually be offered.
        """
        if override:
            return override
        out = []
        for raw in self.card.get("skills", []):
            out.append(Skill(
                id=raw.get("id") or raw.get("name", "skill"),
                name=raw.get("name", raw.get("id", "skill")),
                description=raw.get("description", ""),
                tags=list(raw.get("tags", [])),
                sensitivity="public",
            ))
        return out or [Skill("a2a.general", "General help",
                             "an A2A agent that did not list skills")]

    async def ask(self, text: str, context_id: str | None = None) -> dict:
        """Send one A2A message. Returns {"text", "needs_input"}."""
        rpc = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex[:12],
            "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "messageId": uuid.uuid4().hex,
                **({"contextId": context_id} if context_id else {}),
            }},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            resp = await http.post(self.rpc_url, json=rpc, headers=self.headers)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise RuntimeError(
                f"A2A error {err.get('code')}: {err.get('message', err)}")
        result = body.get("result", {})
        state = task_state(result)
        text = extract_text(result)
        if state in FAILED_STATES:
            # A failed remote task is not an answer: let the caller turn it
            # into an error in the room instead of filing it as knowledge.
            raise RuntimeError(f"remote task {state}: {text[:300]}")
        # Hermes returns `completed` even when its reply is a list of
        # questions, so the state alone is not enough to tell the two apart.
        needs_input = state in ASK_STATES or looks_like_question(text)
        return {"text": text, "needs_input": needs_input}


# Hermes answers with protobuf-shaped JSON: the state is an enum name and a
# text part carries `text` + `mediaType` with no `kind` discriminator at all.
# Requiring kind == "text" (as the A2A docs' examples show) finds nothing.
FAILED_STATES = {"failed", "rejected", "canceled", "cancelled", "unknown"}
# A2A's own way of saying "I need more from you first".
ASK_STATES = {"input_required", "auth_required"}


def task_state(result: dict) -> str:
    """`TASK_STATE_COMPLETED` and `completed` both mean completed."""
    raw = str((result.get("status") or {}).get("state") or "").lower()
    return raw.removeprefix("task_state_").replace("-", "_")


def parts_text(parts: list | None) -> list[str]:
    """Text out of A2A parts, whether or not they declare a kind."""
    out = []
    for part in parts or []:
        if part.get("kind") in (None, "text") and part.get("text"):
            out.append(str(part["text"]))
    return out


def extract_text(result: dict) -> str:
    """Pull the readable answer out of an A2A Task or Message.

    Three places hold it, in order of preference: the artifacts, the status
    message, and the last turn of the history. The same text often appears in
    more than one, so the first that yields anything wins instead of all of
    them being concatenated.
    """
    for artifact in result.get("artifacts") or []:
        if chunks := parts_text(artifact.get("parts")):
            return "\n".join(chunks).strip()

    status_message = (result.get("status") or {}).get("message")
    if isinstance(status_message, dict):
        if chunks := parts_text(status_message.get("parts")):
            return "\n".join(chunks).strip()
    elif isinstance(status_message, str) and status_message.strip():
        return status_message.strip()

    if chunks := parts_text(result.get("parts")):      # a bare Message reply
        return "\n".join(chunks).strip()
    for message in reversed(result.get("history") or []):
        if chunks := parts_text(message.get("parts")):
            return "\n".join(chunks).strip()
    return "(the agent returned no text)"


def parse_skill(spec: str) -> Skill:
    """`billing.support:billing,invoice,refund` → a Skill with those tags."""
    skill_id, _, tags = spec.partition(":")
    words = [t.strip() for t in tags.split(",") if t.strip()]
    return Skill(id=skill_id.strip() or "a2a.general",
                 name=skill_id.strip().replace(".", " ") or "general",
                 description=f"handled by the remote agent ({skill_id.strip()})",
                 tags=words, sensitivity="public")


async def run_bridge(args: argparse.Namespace) -> None:
    sys.stdout.reconfigure(line_buffering=True)
    peer = A2APeer(args.peer, args.token)
    card = await peer.fetch_card()
    remote_name = args.name or card.get("name") or "A2A agent"
    override = [parse_skill(spec) for spec in args.skill] if args.skill else None
    offered = peer.skills(override)
    print(f"peer {peer.url} → {remote_name}  (rpc: {peer.rpc_url})")
    if override:
        print(f"advertising (override): {[s.id for s in offered]}")
        print(f"peer's own card listed {len(peer.skills())} skill(s), ignored")
    else:
        print(f"advertising (from card): {[s.id for s in offered]}")
        if len(offered) > 5:
            print(f"!! {len(offered)} skills with "
                  f"{sum(len(s.tags) for s in offered)} tags — this proxy will "
                  "match a lot of open calls; consider --skill to narrow it")

    proxy = Participant(
        name=remote_name,
        owner=args.owner,
        platform=args.platform,
        key_dir=args.key_dir,
        skills=offered,
        description=f"{args.owner}'s {remote_name}, reached over A2A by a bridge",
        poll=args.poll,
        take_open_calls=not args.no_open_calls,
    )

    @proxy.answers
    async def forward(ask: str, ctx: dict) -> "str | dict":
        # The room's id doubles as the A2A context id, so the remote agent
        # keeps one conversation per ticket instead of a pile of one-offs.
        prefix = (f"[guild] {ctx['asked_by']} asks, about "
                  f"{ctx['topic']!r}"
                  + (f" for customer {ctx['customer']}" if ctx["customer"] else "")
                  + ":\n")
        try:
            answer = await peer.ask(prefix + ask, context_id=ctx["room_id"])
            if answer["needs_input"]:
                print("remote is asking for more information")
            return answer
        except (httpx.HTTPError, RuntimeError) as exc:
            print(f"peer failed: {exc}")
            # Report it as an error, not as an answer: it settles the ask (no
            # retry storm), stays out of the answer log, is invisible to the
            # customer, and puts the room in front of a human.
            return {"text": f"{remote_name} could not be reached: {exc}",
                    "kind": "error", "flag_human": True}

    await proxy.serve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--peer", required=True,
                        help="the A2A agent's base URL, e.g. http://host:9900")
    parser.add_argument("--owner", required=True,
                        help="the human that remote agent works with")
    parser.add_argument("--platform", default="http://127.0.0.1:9100")
    parser.add_argument("--token", default="",
                        help="bearer token the peer expects (A2A_PEER_TOKENS)")
    parser.add_argument("--name", default=None,
                        help="override the name from the peer's agent card")
    parser.add_argument("--key-dir", default=None)
    parser.add_argument("--skill", action="append", default=[],
                        help="advertise this instead of the peer's own list: "
                             "\"billing.support:billing,invoice,refund\" "
                             "(repeatable)")
    parser.add_argument("--poll", type=float, default=20.0)
    parser.add_argument("--no-open-calls", action="store_true",
                        help="only forward asks addressed to it by name")
    args = parser.parse_args()
    args.key_dir = args.key_dir or (
        f"data/keys/bridge-{(args.name or args.peer).lower().replace(' ', '-')}")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_bridge(args))


if __name__ == "__main__":
    main()
