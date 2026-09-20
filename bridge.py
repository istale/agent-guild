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

    async def ask(self, text: str, context_id: str | None = None) -> str:
        """Send one A2A message and return the reply as text."""
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
            raise RuntimeError(f"A2A error: {body['error']}")
        return extract_text(body.get("result", {}))


def extract_text(result: dict) -> str:
    """Pull the readable answer out of an A2A Task or Message."""
    chunks: list[str] = []
    for artifact in result.get("artifacts", []) or []:
        for part in artifact.get("parts", []) or []:
            if part.get("kind") == "text" and part.get("text"):
                chunks.append(part["text"])
    if not chunks:
        history = result.get("history") or []
        messages = [result] if result.get("kind") == "message" else []
        for message in messages or history[-1:]:
            for part in message.get("parts", []) or []:
                if part.get("kind") == "text" and part.get("text"):
                    chunks.append(part["text"])
    if not chunks and result.get("status", {}).get("message"):
        chunks.append(str(result["status"]["message"]))
    return "\n".join(chunks).strip() or "(the agent returned no text)"


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
            return await peer.ask(prefix + ask, context_id=ctx["room_id"])
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
