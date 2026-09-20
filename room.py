"""Rooms — one room per conversation, watchable by humans.

A room is the shared space everything happens in. Two kinds:

  ticket      an external customer's conversation. The customer talks in it
              through a guest token (no keys, no DID — they are a stranger),
              the customer-service agent answers, and internal agents get
              pulled in by name when the CS agent needs help.
  discussion  agents (and their humans) talking among themselves.

The platform hosts the room; agents dial *out* to it and long-poll, so an
agent on a laptop behind NAT needs no inbound port — only the platform does.
Agent posts are identity-checked with the same signed envelope as A2A; guest
posts are checked against the token the platform handed that customer.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from dataclasses import dataclass, field

import httpx
from fastapi import APIRouter, Body, HTTPException

import knowledge
from envelope import new_id, open_envelope, sign_envelope
from agent_card import AgentCard
from identity import Identity

# Utterance kinds a watcher can tell apart
SAY = "say"          # ordinary turn
JOIN = "join"        # an agent entered the room
NOTICE = "notice"    # something the platform or an agent wants humans to see
ERROR = "error"      # an agent tried and could not: closes the ask without
                     # becoming an answer, and never enters the answer log

# Kinds that settle an ask, so the same work is not harded out twice.
SETTLES = {SAY, NOTICE, ERROR}

# An ask addressed to ANYONE qualified, instead of to one named agent: the
# quest board. First agent to claim it owns it.
OPEN_CALL = "*"

# Room status, driven by the agents as they work
OPEN = "open"                        # customer is being served
WAITING_INTERNAL = "waiting"         # blocked on an internal agent
AWAITING_CUSTOMER = "awaiting_customer"  # blocked on the customer answering
RESOLVED = "resolved"

SINGLE_WORD_MENTION = re.compile(r"@([A-Za-z0-9_.\-]{2,40})")


def mentions_in(text: str, known: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Find @mentions. Agent names contain spaces ("Billing Hermes"), which no
    regex can delimit reliably, so match the names the room actually knows and
    only fall back to a single-word guess when none of them appear."""
    lowered = text.lower()
    hits = [name for name in known if f"@{name.lower()}" in lowered]
    return hits or SINGLE_WORD_MENTION.findall(text)


@dataclass
class Utterance:
    seq: int
    author_did: str
    author_name: str
    author_owner: str
    text: str
    kind: str = SAY
    to: str = ""                 # optional: name of the agent being addressed
    # "I cannot answer yet, I need more from whoever asked." Declared by the
    # agent, because guessing from the text only works some of the time.
    needs_input: bool = False
    created_at: float = field(default_factory=time.time)
    utterance_id: str = field(default_factory=lambda: new_id("utt"))

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "id": self.utterance_id, "kind": self.kind,
            "author_did": self.author_did, "author_name": self.author_name,
            "author_owner": self.author_owner, "to": self.to,
            "needs_input": self.needs_input,
            "text": self.text, "created_at": self.created_at,
        }


@dataclass
class Room:
    room_id: str
    topic: str
    kind: str = "discussion"
    status: str = OPEN
    priority: str = "normal"       # raised when an open call goes unanswered
    needs_human: bool = False      # nobody took it; a person has to look
    customer: str = ""
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    guest_token: str = ""
    participants: dict[str, dict] = field(default_factory=dict)  # did -> summary
    utterances: list[Utterance] = field(default_factory=list)
    # utterance id -> {"agent": name, "at": ts}
    claims: dict[str, dict] = field(default_factory=dict)

    def summary(self) -> dict:
        last = self.utterances[-1] if self.utterances else None
        said = [u for u in self.utterances if u.kind == SAY]
        return {
            "room_id": self.room_id, "topic": self.topic, "kind": self.kind,
            "status": self.status, "priority": self.priority,
            "needs_human": self.needs_human, "customer": self.customer,
            "created_by": self.created_by, "created_at": self.created_at,
            "participants": list(self.participants.values()),
            "utterance_count": len(self.utterances),
            "said_count": len(said),
            "last_at": last.created_at if last else self.created_at,
            "last_by": last.author_name if last else "",
            "waiting_on": self._waiting_on(),
        }

    def _waiting_on(self) -> str:
        """Who owes the room a reply: the last addressee who has not spoken."""
        for u in reversed([x for x in self.utterances if x.kind == SAY]):
            if not u.to:
                continue
            if u.to == OPEN_CALL:
                claim = self.claims.get(u.utterance_id)
                taker = claim["agent"] if claim else ""
                if not taker:
                    return "anyone"
                return "" if any(x.seq > u.seq and x.author_name == taker
                                 for x in self.utterances) else taker
            after = [x for x in self.utterances
                     if x.seq > u.seq and x.author_name == u.to]
            return "" if after else u.to
        return ""

    def to_dict(self, since: int = 0, view: str = "full") -> dict:
        return {
            **self.summary(),
            "utterances": [u.to_dict() for u in self.visible(view)
                           if u.seq > since],
        }

    def visible(self, view: str) -> list[Utterance]:
        """What a given audience may read.

        `customer` sees their own messages and the agent turns addressed to the
        room at large. Anything an agent directs at another agent (it carries
        `to`) is internal chatter and never leaves the platform — the filter
        lives here, not in the page, so a UI bug cannot leak it.
        """
        if view != "customer":
            return self.utterances
        return [u for u in self.utterances
                if u.kind == SAY and (u.author_owner == "external" or not u.to)]


class RoomStore:
    def __init__(self, *, claim_ttl: float = 120.0, escalate_after: float = 60.0,
                 human_after: float = 180.0) -> None:
        self.rooms: dict[str, Room] = {}
        # A claim that produces no answer within claim_ttl goes back on the
        # board; an open call nobody takes gets escalated, then flagged for a
        # human. Seconds.
        self.claim_ttl = claim_ttl
        self.escalate_after = escalate_after
        self.human_after = human_after
        self._changed = asyncio.Event()

    def create(self, topic: str, *, room_id: str | None = None,
               created_by: str = "", kind: str = "discussion",
               customer: str = "") -> Room:
        room_id = room_id or new_id("room")
        if room_id in self.rooms:
            return self.rooms[room_id]
        room = Room(room_id=room_id, topic=topic, created_by=created_by,
                    kind=kind, customer=customer)
        if kind == "ticket":
            room.guest_token = secrets.token_urlsafe(16)
        self.rooms[room_id] = room
        self._wake()
        return room

    def get(self, room_id: str) -> Room:
        room = self.rooms.get(room_id)
        if room is None:
            raise KeyError(room_id)
        return room

    def join(self, room_id: str, card: AgentCard) -> Room:
        room = self.get(room_id)
        first_time = card.did not in room.participants
        room.participants[card.did] = {
            "did": card.did, "name": card.name,
            "owner": (card.owner or {}).get("label", "unknown"),
            "joined_at": time.time(),
        }
        if first_time:
            self.post(room_id, card, f"{card.name} joined the room.", kind=JOIN)
        return room

    def post(self, room_id: str, card: AgentCard, text: str, *, kind: str = SAY,
             to: str = "", status: str = "", flag_human: bool = False,
             needs_input: bool = False) -> Utterance:
        return self._append(
            room_id, text, kind=kind, to=to, status=status,
            flag_human=flag_human, needs_input=needs_input,
            author_did=card.did, author_name=card.name,
            author_owner=(card.owner or {}).get("label", "unknown"))

    def post_as_guest(self, room_id: str, token: str, text: str) -> Utterance:
        room = self.get(room_id)
        if not room.guest_token or not secrets.compare_digest(token,
                                                              room.guest_token):
            raise PermissionError("bad guest token")
        return self._append(room_id, text, author_did="guest",
                            author_name=room.customer or "Customer",
                            author_owner="external")

    def _append(self, room_id: str, text: str, *, author_did: str,
                author_name: str, author_owner: str, kind: str = SAY,
                to: str = "", status: str = "",
                flag_human: bool = False, needs_input: bool = False) -> Utterance:
        room = self.get(room_id)
        # Only a human's @mention is parsed out of the text. An agent must name
        # its addressee explicitly, because agents quote each other: a relayed
        # answer that repeats "@Some Agent" from the original ask would re-fire
        # as a fresh assignment and the two agents would bounce it forever.
        if not to and kind == SAY and author_owner == "external":
            found = mentions_in(text, [p["name"] for p in room.participants.values()])
            to = next((n for n in found if n != author_name), "")
        utterance = Utterance(
            seq=len(room.utterances) + 1, author_did=author_did,
            author_name=author_name, author_owner=author_owner,
            text=text, kind=kind, to=to, needs_input=needs_input,
        )
        room.utterances.append(utterance)
        if status:
            room.status = status
        if flag_human:
            room.needs_human, room.priority = True, "high"
        self._file_knowledge(room, utterance)
        self._wake()
        return utterance

    def _file_knowledge(self, room: Room, utterance: Utterance) -> None:
        """An internal agent just answered a directed ask — keep the pair.

        The question filed is the customer's own words when there are any, not
        the ask the front-line agent composed, so the log matches how the next
        customer will phrase it.
        """
        if (utterance.kind != SAY or not utterance.to
                or utterance.author_owner in ("external", "platform")):
            return
        if utterance.needs_input:
            return                      # a follow-up question, not an answer
        ask = next((u for u in reversed(room.utterances[:-1])
                    if u.kind == SAY and u.author_name != utterance.author_name
                    and (u.to == utterance.author_name
                         or (room.claims.get(u.utterance_id) or {}).get("agent")
                         == utterance.author_name)), None)
        if ask is None:
            return
        customer_words = next((u.text for u in reversed(room.utterances)
                               if u.author_owner == "external"
                               and u.seq < utterance.seq), "")
        knowledge.store.record(
            question=customer_words or ask.text, answer=utterance.text,
            by_agent=utterance.author_name, by_human=utterance.author_owner,
            room_id=room.room_id)

    def claim(self, room_id: str, utterance_id: str, card: AgentCard) -> dict:
        """Take an open call. First caller wins; everyone else gets a 409."""
        room = self.get(room_id)
        target = next((u for u in room.utterances
                       if u.utterance_id == utterance_id), None)
        if target is None or target.to != OPEN_CALL:
            raise KeyError(utterance_id)
        held = room.claims.get(utterance_id)
        taken_by = held["agent"] if held else ""
        if taken_by and taken_by != card.name:
            raise PermissionError(f"already claimed by {taken_by}")
        room.claims[utterance_id] = {"agent": card.name, "at": time.time()}
        if not taken_by:
            self.post(room_id, card, f"{card.name} took this on.", kind=NOTICE)
        return {"room_id": room_id, "utterance_id": utterance_id,
                "claimed_by": card.name}

    def sweep(self) -> None:
        """Housekeeping the board needs to stay honest, done lazily on read.

        Two things rot without it: a claim by an agent that then died (the
        request would be owned forever by nobody), and an open call nobody
        ever takes (it would sit there silently). Both are visible events, so
        they are posted into the room rather than fixed quietly.
        """
        now = time.time()
        for room in self.rooms.values():
            notices: list[str] = []
            for u in [x for x in room.utterances if x.to == OPEN_CALL]:
                claim = room.claims.get(u.utterance_id)
                if claim:
                    answered = any(x.seq > u.seq and x.kind == SAY
                                   and x.author_name == claim["agent"]
                                   for x in room.utterances)
                    if not answered and now - claim["at"] > self.claim_ttl:
                        del room.claims[u.utterance_id]
                        notices.append(
                            f"{claim['agent']} claimed this "
                            f"{int(now - claim['at'])}s ago and never answered — "
                            "back on the board.")
                    continue
                age = now - u.created_at
                if age > self.human_after and not room.needs_human:
                    room.needs_human, room.priority = True, "high"
                    notices.append(f"No agent has taken this in {int(age)}s — "
                                   "a human needs to look at it.")
                elif age > self.escalate_after and room.priority == "normal":
                    room.priority = "high"
                    notices.append(f"Still unclaimed after {int(age)}s — "
                                   "priority raised.")
            for text in notices:   # appended after the scan, not during it
                self._append(room.room_id, text, kind=NOTICE,
                             author_did="platform", author_name="Platform",
                             author_owner="platform")

    def openings(self) -> list[dict]:
        """Unclaimed open calls — the quest board."""
        self.sweep()
        board = []
        for room in self.rooms.values():
            for u in room.utterances:
                if u.to == OPEN_CALL and u.utterance_id not in room.claims:
                    board.append({"room": room.summary(), "utterance": u.to_dict()})
        return sorted(board, key=lambda o: o["utterance"]["created_at"])

    def mentions_for(self, name: str) -> list[dict]:
        """Open asks addressed to `name` that it has not answered yet."""
        self.sweep()
        pending = []
        for room in self.rooms.values():
            for u in reversed([x for x in room.utterances if x.kind == SAY]):
                claim = room.claims.get(u.utterance_id) or {}
                if u.to != name and claim.get("agent") != name:
                    continue
                # A notice ("X is on it") settles the ask too, so a slow agent
                # is not handed the same work again on the next poll.
                settled = any(x.seq > u.seq and x.author_name == name
                              and x.kind in SETTLES for x in room.utterances)
                if not settled:
                    pending.append({"room": room.summary(),
                                    "utterance": u.to_dict()})
                break
        return sorted(pending, key=lambda p: p["utterance"]["created_at"])

    def _wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait_for_change(self, timeout: float) -> None:
        waiter = self._changed
        try:
            await asyncio.wait_for(waiter.wait(), timeout=max(timeout, 0.0))
        except asyncio.TimeoutError:
            pass


def _seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Tunable without touching the code: shorten them to watch the board work.
store = RoomStore(
    claim_ttl=_seconds("HUB_CLAIM_TTL", 120.0),
    escalate_after=_seconds("HUB_ESCALATE_AFTER", 60.0),
    human_after=_seconds("HUB_HUMAN_AFTER", 180.0),
)
router = APIRouter(tags=["rooms"])


def _verified(envelope: dict) -> tuple[AgentCard, dict]:
    try:
        card, rpc = open_envelope(envelope)
    except ValueError as exc:
        raise HTTPException(401, f"envelope rejected: {exc}") from None
    return card, rpc.get("params") or {}


# ------------------------------------------------------- customer (no keys)
@router.post("/tickets")
def open_ticket(body: dict = Body(...)) -> dict:
    """A customer starts a conversation. They get a token, not an identity."""
    customer = (body.get("customer") or "Customer").strip()[:40]
    topic = (body.get("topic") or "Support request").strip()[:120]
    room = store.create(topic, created_by=customer, kind="ticket",
                        customer=customer)
    if body.get("text"):
        store.post_as_guest(room.room_id, room.guest_token, body["text"][:2000])
    return {"room_id": room.room_id, "guest_token": room.guest_token,
            "topic": room.topic}


@router.post("/rooms/{room_id}/guest-utterances")
def guest_say(room_id: str, body: dict = Body(...)) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    try:
        return store.post_as_guest(room_id, body.get("token", ""),
                                   text[:2000]).to_dict()
    except KeyError:
        raise HTTPException(404, "no such room") from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


# ------------------------------------------------------- agents (signed)
@router.post("/rooms")
def create_room(envelope: dict = Body(...)) -> dict:
    card, params = _verified(envelope)
    if not params.get("topic"):
        raise HTTPException(400, "topic is required")
    room = store.create(params["topic"], room_id=params.get("room_id"),
                        created_by=card.name,
                        kind=params.get("kind", "discussion"),
                        customer=params.get("customer", ""))
    return room.summary()


@router.post("/rooms/{room_id}/join")
def join_room(room_id: str, envelope: dict = Body(...)) -> dict:
    card, _ = _verified(envelope)
    try:
        return store.join(room_id, card).summary()
    except KeyError:
        raise HTTPException(404, "no such room") from None


@router.post("/rooms/{room_id}/utterances")
def post_utterance(room_id: str, envelope: dict = Body(...)) -> dict:
    card, params = _verified(envelope)
    text = (params.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    try:
        room = store.get(room_id)
    except KeyError:
        raise HTTPException(404, "no such room") from None
    if card.did not in room.participants:
        raise HTTPException(403, "join the room before speaking")
    return store.post(room_id, card, text, kind=params.get("kind", SAY),
                      to=params.get("to", ""),
                      status=params.get("status", ""),
                      flag_human=bool(params.get("flag_human")),
                      needs_input=bool(params.get("needs_input"))).to_dict()


# ------------------------------------------------------- reading
@router.get("/rooms")
def list_rooms(with_utterances: bool = False, kind: str | None = None) -> dict:
    store.sweep()
    rooms = sorted(store.rooms.values(), key=lambda r: r.created_at)
    if kind:
        rooms = [r for r in rooms if r.kind == kind]
    return {"rooms": [r.to_dict() if with_utterances else r.summary()
                      for r in rooms]}


@router.get("/rooms/{room_id}")
async def read_room(room_id: str, since: int = 0, wait: float = 0.0,
                    view: str = "full") -> dict:
    """Read the transcript. `wait` turns this into a long-poll for new turns.
    `view=customer` strips internal agent-to-agent chatter."""
    try:
        room = store.get(room_id)
    except KeyError:
        raise HTTPException(404, "no such room") from None
    deadline = time.time() + min(max(wait, 0.0), 60.0)
    while (not [u for u in room.visible(view) if u.seq > since]
           and time.time() < deadline):
        await store.wait_for_change(timeout=deadline - time.time())
    return room.to_dict(since=since, view=view)


@router.post("/rooms/{room_id}/openings/{utterance_id}/claim")
def claim_opening(room_id: str, utterance_id: str,
                  envelope: dict = Body(...)) -> dict:
    card, _ = _verified(envelope)
    try:
        room = store.get(room_id)
    except KeyError:
        raise HTTPException(404, "no such room") from None
    if card.did not in room.participants:
        raise HTTPException(403, "join the room before claiming")
    try:
        return store.claim(room_id, utterance_id, card)
    except KeyError:
        raise HTTPException(404, "no such open call") from None
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/openings")
async def read_openings(wait: float = 0.0) -> dict:
    """The quest board: asks nobody has taken yet."""
    deadline = time.time() + min(max(wait, 0.0), 60.0)
    board = store.openings()
    while not board and time.time() < deadline:
        await store.wait_for_change(timeout=deadline - time.time())
        board = store.openings()
    return {"openings": board}


@router.get("/mentions")
async def read_mentions(name: str, wait: float = 0.0) -> dict:
    """What is waiting for me. An internal agent long-polls this."""
    deadline = time.time() + min(max(wait, 0.0), 60.0)
    pending = store.mentions_for(name)
    while not pending and time.time() < deadline:
        await store.wait_for_change(timeout=deadline - time.time())
        pending = store.mentions_for(name)
    return {"name": name, "mentions": pending}


# ------------------------------------------------------------- agent client
class RoomClient:
    """What an agent on some other machine uses to take part in a room."""

    def __init__(self, platform: str, identity: Identity, card: AgentCard,
                 timeout: float = 70.0):
        self.platform = platform.rstrip("/")
        self.identity = identity
        self.card = card
        self.timeout = timeout

    def _envelope(self, method: str, params: dict) -> dict:
        return sign_envelope(self.identity, self.card, {
            "jsonrpc": "2.0", "id": new_id("rpc"), "method": method,
            "params": params,
        })

    # Every transport failure surfaces as RoomError, so an agent that handles
    # RoomError survives the platform restarting under it.
    async def _post(self, path: str, method: str, params: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                resp = await http.post(f"{self.platform}{path}",
                                       json=self._envelope(method, params))
        except httpx.HTTPError as exc:
            raise RoomError(f"{path} unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise RoomError(f"{resp.status_code}: "
                            f"{resp.json().get('detail', resp.text)}")
        return resp.json()

    async def _get(self, path: str, params: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                resp = await http.get(f"{self.platform}{path}", params=params)
        except httpx.HTTPError as exc:
            raise RoomError(f"{path} unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise RoomError(f"{resp.status_code}: {resp.text}")
        return resp.json()

    async def create_room(self, topic: str, room_id: str | None = None,
                          **params) -> dict:
        return await self._post("/rooms", "room/create",
                                {"topic": topic, "room_id": room_id, **params})

    async def join(self, room_id: str) -> dict:
        return await self._post(f"/rooms/{room_id}/join", "room/join", {})

    async def say(self, room_id: str, text: str, *, to: str = "",
                  kind: str = SAY, status: str = "",
                  flag_human: bool = False, needs_input: bool = False) -> dict:
        return await self._post(f"/rooms/{room_id}/utterances", "room/post",
                                {"text": text, "to": to, "kind": kind,
                                 "status": status, "flag_human": flag_human,
                                 "needs_input": needs_input})

    async def read(self, room_id: str, since: int = 0, wait: float = 0.0,
                   view: str = "full") -> dict:
        return await self._get(f"/rooms/{room_id}",
                               {"since": since, "wait": wait, "view": view})

    async def rooms(self, kind: str | None = None) -> list[dict]:
        params = {"kind": kind} if kind else {}
        return (await self._get("/rooms", params))["rooms"]

    async def mentions(self, wait: float = 0.0) -> list[dict]:
        return (await self._get("/mentions",
                                {"name": self.card.name, "wait": wait}))["mentions"]

    async def openings(self, wait: float = 0.0) -> list[dict]:
        return (await self._get("/openings", {"wait": wait}))["openings"]

    async def claim(self, room_id: str, utterance_id: str) -> dict:
        return await self._post(
            f"/rooms/{room_id}/openings/{utterance_id}/claim", "room/claim", {})


class RoomError(RuntimeError):
    pass
