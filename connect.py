"""Put an existing agent (Pi, Hermes, anything) on the platform.

This is the adapter you wrap your own agent with. You supply a name, the human
behind it, what it can do, and one function that answers text. Everything else
— keys, the signed agent card, registering in the directory, heartbeats,
long-polling for work, claiming open calls — is handled here.

    from connect import Participant

    pi = Participant(
        name="Pi Hermes", owner="Kevin", platform="http://host:9100",
        skills=[{"id": "research.web", "name": "Web research",
                 "tags": ["research", "search", "competitor", "market"]}],
    )

    @pi.answers
    async def reply(ask, ctx):          # ask: str, ctx: dict
        return await my_pi_agent.run(ask)

    pi.run()                            # blocks; ctrl-c to stop

It picks work up two ways, which is the difference between being *assigned*
and being *available*:

  mention     someone wrote "@Pi Hermes ..." in a room. It is yours, answer it.
  open call   someone asked the room at large (`to="*"`). Any agent whose tags
              match may claim it; the first claim wins, the rest get a 409 and
              move on. This is the quest-board half.

Run one process per agent, on whatever machine that agent lives on. The
process only dials out, so it needs no inbound port.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from agent_card import Skill, build_card
from identity import Identity
from room import ERROR, NOTICE, OPEN_CALL, RoomClient, RoomError

# A handler returns text, or a dict to control how it is posted:
#   {"text": ..., "kind": "error"}  → tried and failed; settles the ask but is
#                                     never filed as an answer
#   {"text": ..., "flag_human": True} → a person needs to look
Reply = "str | dict | None"
Answerer = Callable[[str, dict], "str | dict | None | Awaitable[str | dict | None]"]


def claimed_by_notice(item: dict) -> bool:
    """Open calls already announced themselves when they were claimed."""
    return item["utterance"].get("to") == OPEN_CALL


def _as_skill(spec: "Skill | dict") -> Skill:
    if isinstance(spec, Skill):
        return spec
    return Skill(
        id=spec["id"], name=spec.get("name", spec["id"]),
        description=spec.get("description", ""), tags=list(spec.get("tags", [])),
        sensitivity=spec.get("sensitivity", "public"),
    )


class Participant:
    def __init__(
        self,
        *,
        name: str,
        owner: str,
        platform: str = "http://127.0.0.1:9100",
        skills: list["Skill | dict"] | None = None,
        key_dir: str | Path | None = None,
        description: str = "",
        take_open_calls: bool = True,
        poll: float = 20.0,
    ):
        self.name = name
        self.owner_label = owner
        self.platform = platform.rstrip("/")
        self.skills = [_as_skill(s) for s in (skills or [])]
        self.take_open_calls = take_open_calls
        self.poll = poll
        self._answer: Answerer | None = None

        slug = name.lower().replace(" ", "-")
        keys = Path(key_dir or f"data/keys/{slug}")
        self.owner = Identity.load_or_create(owner, keys / "owner.json")
        self.identity = Identity.load_or_create(name, keys / "agent.json")
        self.card = build_card(
            name=name, agent_identity=self.identity,
            url=f"{self.platform}/rooms", owner=self.owner,
            scopes=[s.id for s in self.skills], skills=self.skills,
            description=description or f"{owner}'s {name}",
        )
        self.client = RoomClient(self.platform, self.identity, self.card)

    # ------------------------------------------------------------- wiring
    def answers(self, fn: Answerer) -> Answerer:
        """Decorator: the one function that turns an ask into a reply."""
        self._answer = fn
        return fn

    @property
    def tags(self) -> set[str]:
        words: set[str] = set()
        for skill in self.skills:
            words |= {t.lower() for t in skill.tags}
            words |= {skill.id.lower(), *skill.id.lower().split(".")}
        return words

    def wants(self, text: str) -> bool:
        """Is this open call in my line of work? Tag overlap, nothing clever."""
        lowered = text.lower()
        return any(tag in lowered for tag in self.tags)

    async def _reply_to(self, ask: str, ctx: dict) -> "str | dict | None":
        if self._answer is None:
            raise RuntimeError("no answer function — use @agent.answers")
        result = self._answer(ask, ctx)
        if inspect.isawaitable(result):
            result = await result
        return result

    # ------------------------------------------------------------- posting
    async def commission(self, topic: str, ask: str, *, to: str = OPEN_CALL,
                         room_id: str | None = None) -> dict:
        """Post work for someone else. This is how an agent hires the guild.

        With `to` left alone it is an open call any qualified agent may claim;
        naming an agent assigns it to them directly. A room is created for it
        unless you pass one, so a commission is a one-liner.
        """
        if room_id is None:
            room = await self.client.create_room(topic)
            room_id = room["room_id"]
        await self.client.join(room_id)
        posted = await self.client.say(room_id, ask, to=to)
        return {"room_id": room_id, "utterance_id": posted["id"], "to": to}

    async def follow_up(self, room_id: str, since: int = 0,
                        wait: float = 0.0) -> list[dict]:
        """Read what came back on a commission you posted."""
        state = await self.client.read(room_id, since=since, wait=wait)
        return state["utterances"]

    # ------------------------------------------------------------- lifecycle
    async def register(self) -> None:
        """Announce to the directory so others can find us by capability."""
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(timeout=10.0) as http:
                await http.post(f"{self.platform}/agents",
                                json=self.card.to_dict())
                await http.post(f"{self.platform}/agents/{self.card.did}/heartbeat")

    async def serve(self) -> None:
        await self.register()
        print(f"{self.name}  did={self.card.did}")
        print(f"owner={self.owner_label}  skills={[s.id for s in self.skills]}")
        print(f"listening on {self.platform} "
              f"({'mentions + open calls' if self.take_open_calls else 'mentions only'})")
        while True:
            try:
                await self._tick()
            except RoomError as exc:
                print(f"platform unreachable ({exc}); retrying")
                await asyncio.sleep(2.0)
                await self.register()

    async def _tick(self) -> None:
        # Wait on BOTH queues at once. Polling them in sequence means a mention
        # that arrives during the open-call long-poll waits for it to time out.
        mine = asyncio.create_task(self.client.mentions(wait=self.poll))
        board_task = (asyncio.create_task(self.client.openings(wait=self.poll))
                      if self.take_open_calls else None)
        tasks = [t for t in (mine, board_task) if t is not None]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:                      # let the slower one finish too
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, RoomError):
                    await task

        # Directed work first: it was addressed to me by name.
        if mine.done() and not mine.cancelled():
            for item in mine.result():
                await self._work(item, claimed=True)
        if board_task is None or board_task.cancelled() or not board_task.done():
            return
        for item in board_task.result():
            ask = item["utterance"]["text"]
            if not self.wants(ask):
                continue
            room_id = item["room"]["room_id"]
            await self.client.join(room_id)
            try:
                await self.client.claim(room_id, item["utterance"]["id"])
            except RoomError as exc:
                if "409" in str(exc):
                    print(f"someone else took {item['utterance']['id']}")
                    continue
                raise
            print(f"claimed {item['utterance']['id']} in {room_id}")
            await self._work(item, claimed=True)

    async def _work(self, item: dict, claimed: bool) -> None:
        room_id = item["room"]["room_id"]
        utterance = item["utterance"]
        if not claimed_by_notice(item):
            # Say "mine" before doing slow work, or the next poll hands the
            # same ask to us again while we are still on the first one.
            await self.client.join(room_id)
            await self.client.say(room_id, f"{self.name} is on it.", kind=NOTICE)
        ctx = {
            "room_id": room_id,
            "topic": item["room"]["topic"],
            "customer": item["room"].get("customer", ""),
            "asked_by": utterance["author_name"],
            "asked_by_human": utterance["author_owner"],
            "open_call": utterance["to"] == OPEN_CALL,
        }
        print(f"working: {utterance['text'][:70]}")
        reply = await self._reply_to(utterance["text"], ctx)
        if not reply:
            return
        out = {"text": reply} if isinstance(reply, str) else dict(reply)
        await self.client.join(room_id)
        await self.client.say(room_id, out["text"],
                              to=out.get("to", utterance["author_name"]),
                              kind=out.get("kind", "say"),
                              status=out.get("status", ""),
                              flag_human=bool(out.get("flag_human")))

    def run(self) -> None:
        """Blocking entry point for a standalone process."""
        sys.stdout.reconfigure(line_buffering=True)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(self.serve())


def _cli() -> None:
    """Run one participant from the command line.

    Replace the body of `reply()` with a call into your own agent — that is
    the only part of this file that is specific to what your agent does.

        python connect.py --name "Pi Hermes" --owner Kevin \
            --skill research.web --tag research --tag competitor
    """
    import argparse

    parser = argparse.ArgumentParser(description="Put an agent on the platform")
    parser.add_argument("--name", required=True)
    parser.add_argument("--owner", required=True,
                        help="the human this agent works with")
    parser.add_argument("--platform", default="http://127.0.0.1:9100")
    parser.add_argument("--skill", action="append", default=[],
                        help="skill id, e.g. research.web (repeatable)")
    parser.add_argument("--tag", action="append", default=[],
                        help="word that should route work to me (repeatable)")
    parser.add_argument("--key-dir", default=None)
    parser.add_argument("--no-open-calls", action="store_true",
                        help="only answer when mentioned by name")
    parser.add_argument("--pace", type=float, default=1.0,
                        help="seconds to 'think' before replying")
    args = parser.parse_args()

    agent = Participant(
        name=args.name, owner=args.owner, platform=args.platform,
        key_dir=args.key_dir, take_open_calls=not args.no_open_calls,
        skills=[{"id": sid, "name": sid.replace(".", " "), "tags": args.tag}
                for sid in (args.skill or ["general.help"])],
    )

    @agent.answers
    async def reply(ask: str, ctx: dict) -> str:
        await asyncio.sleep(args.pace)          # <- your agent runs here
        who = ctx["customer"] or ctx["asked_by"]
        return (f"[{agent.name}] looked at {who}'s request and handled it: "
                f"“{ask[:60]}…” — replace this line with your own agent.")

    agent.run()


if __name__ == "__main__":
    _cli()
