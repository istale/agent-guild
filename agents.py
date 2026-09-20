"""The agents. One process per agent, run on whichever machine you like.

    # the customer-facing one
    python agents.py --role cs --platform http://<host>:9100

    # one per internal domain, each with its own human collaborator
    python agents.py --role domain --domain billing  --owner Mei   --platform ...
    python agents.py --role domain --domain shipping --owner Jun   --platform ...

The customer-service agent:
  1. watches the platform for new tickets and joins them,
  2. answers the customer itself when it can,
  3. otherwise asks the *directory* who handles this kind of thing and
     @mentions that agent in the ticket, flipping the ticket to `waiting`,
  4. relays the internal answer back to the customer in its own words.

A domain agent long-polls `/mentions` for asks addressed to it, answers in the
same room, and its human collaborator reads along on /ops.

Everything an agent says is signed with its own key, so the transcript on /ops
attributes every line to an agent *and* to the human behind it.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

import httpx

from agent_card import Skill, build_card
from identity import Identity
from room import OPEN_CALL, RoomClient, RoomError

# What each internal domain advertises (OASF-ish) and how it answers.
DOMAINS: dict[str, dict] = {
    "billing": {
        "skill": Skill("billing.support", "Billing support",
                       "invoices, double charges, refunds, payment methods",
                       tags=["billing", "invoice", "refund", "charge", "payment",
                             "money", "card"]),
        "answer": ("I see two authorisations on that card — one is a pending "
                   "hold that drops off in 3 working days, so the customer was "
                   "only charged once. I have released the hold manually."),
    },
    "shipping": {
        "skill": Skill("shipping.support", "Shipping support",
                       "deliveries, tracking, delays, lost parcels",
                       tags=["shipping", "delivery", "tracking", "parcel",
                             "late", "courier", "address"]),
        "answer": ("The parcel is sitting at the courier's depot after a failed "
                   "delivery. I have rebooked it for tomorrow and pushed a new "
                   "tracking link to the account."),
    },
    "bug": {
        "skill": Skill("product.bug", "Product bug triage",
                       "crashes, errors, broken features, login problems",
                       tags=["bug", "crash", "error", "broken", "login",
                             "500", "cannot", "fails"]),
        "answer": ("Reproduced it — the export button throws on accounts with "
                   "no default currency set. Fix is in review; workaround is to "
                   "set a currency in settings first."),
    },
}


def build_agent(name: str, owner_label: str, key_dir: Path, platform: str,
                skills: list[Skill], description: str):
    owner = Identity.load_or_create(owner_label, key_dir / "owner.json")
    agent = Identity.load_or_create(name, key_dir / "agent.json")
    card = build_card(name=name, agent_identity=agent,
                      url=f"{platform.rstrip('/')}/rooms", owner=owner,
                      scopes=[s.id for s in skills], skills=skills,
                      description=description)
    return agent, card


async def announce(platform: str, card) -> None:
    """Register in the directory so other agents can find us by capability."""
    with contextlib.suppress(httpx.HTTPError):
        async with httpx.AsyncClient(timeout=10.0) as http:
            await http.post(f"{platform}/agents", json=card.to_dict())
            await http.post(f"{platform}/agents/{card.did}/heartbeat")


async def find_helper(platform: str, text: str, exclude_did: str) -> dict | None:
    """Ask the directory who handles this — no hardcoded routing table."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.get(f"{platform}/search", params={"q": text, "limit": 5})
    if resp.status_code >= 400:
        return None
    for match in resp.json()["matches"]:
        if match["card"]["did"] != exclude_did:
            return match
    return None


# --------------------------------------------------------------- CS agent
async def run_cs(args: argparse.Namespace) -> None:
    skills = [Skill("support.frontline", "Customer service",
                    "talk to customers, triage and escalate their problems",
                    tags=["support", "customer", "service", "helpdesk"],
                    sensitivity="public")]
    agent, card = build_agent(args.agent, args.owner, Path(args.key_dir),
                              args.platform, skills,
                              "front-line customer service agent")
    client = RoomClient(args.platform, agent, card)
    await announce(args.platform, card)
    print(f"{card.name}  did={card.did}\nwatching {args.platform} for tickets")

    seen: dict[str, int] = {}
    while True:
        try:
            tickets = await client.rooms(kind="ticket")
        except RoomError as exc:
            # The platform may have restarted: forget what we joined, wait,
            # and re-announce ourselves before trying again.
            print(f"platform unreachable ({exc}); retrying")
            seen.clear()
            await asyncio.sleep(2.0)
            await announce(args.platform, card)
            continue

        try:
            await serve_tickets(client, card, tickets, seen, args)
        except RoomError as exc:
            print(f"lost the platform mid-sweep ({exc}); retrying")
            seen.clear()
            await asyncio.sleep(2.0)
        await asyncio.sleep(args.tick)


async def serve_tickets(client: RoomClient, card, tickets: list[dict],
                        seen: dict[str, int], args: argparse.Namespace) -> None:
    for ticket in tickets:
        room_id = ticket["room_id"]
        if room_id not in seen:
            await client.join(room_id)
            await client.say(room_id,
                             f"Hi {ticket['customer']}, {card.name} here — "
                             "let me take a look at this.", status="open")
            seen[room_id] = 0

        state = await client.read(room_id, since=seen[room_id])
        seen[room_id] = max([u["seq"] for u in state["utterances"]]
                            or [seen[room_id]])
        for u in state["utterances"]:
            if u["kind"] != "say":
                continue
            if u["author_owner"] == "external":
                await handle_customer(client, room_id, card, u, args)
            elif u["to"] == card.name:
                await relay_to_customer(client, room_id, u)


async def handle_customer(client: RoomClient, room_id: str, card, u: dict,
                          args: argparse.Namespace) -> None:
    text = u["text"]
    if any(word in text.lower() for word in ("thanks", "thank you", "謝謝")):
        await client.say(room_id, "Glad that helped. I will close this ticket.",
                         status="resolved")
        return
    helper = await find_helper(args.platform, text, card.did)
    if helper is None:
        # Nobody in the directory advertises this. Put it on the board instead
        # of sitting on it: any agent whose skills match can claim it.
        await client.say(
            room_id,
            f"Open call — customer {u['author_name']} asks: “{text}”. "
            "Whoever owns this, please take it.",
            to=OPEN_CALL, status="waiting")
        await client.say(room_id, "I am finding the right person for this — "
                                  "one moment.")
        return
    name = helper["card"]["name"]
    await client.join(room_id)
    await client.say(
        room_id,
        f"@{name} customer {u['author_name']} says: “{text}”. "
        f"Can you take a look?",
        to=name, status="waiting")
    await client.say(room_id, "I am checking this with the team that owns it — "
                              "one moment.")


async def relay_to_customer(client: RoomClient, room_id: str, u: dict) -> None:
    await client.say(room_id,
                     f"Thanks for waiting — here is what we found: {u['text']}",
                     status="open")


# ------------------------------------------------------------ domain agent
async def run_domain(args: argparse.Namespace) -> None:
    spec = DOMAINS[args.domain]
    agent, card = build_agent(args.agent, args.owner, Path(args.key_dir),
                              args.platform, [spec["skill"]],
                              f"internal {args.domain} agent, works with "
                              f"{args.owner}")
    client = RoomClient(args.platform, agent, card)
    await announce(args.platform, card)
    print(f"{card.name}  did={card.did}\nowner={args.owner}  "
          f"domain={args.domain}\nwaiting for mentions…")

    while True:
        try:
            pending = await client.mentions(wait=args.poll)
        except RoomError as exc:
            print(f"platform unreachable ({exc}); retrying")
            await asyncio.sleep(2.0)
            await announce(args.platform, card)
            continue
        try:
            await answer_mentions(client, spec, pending, args)
        except RoomError as exc:
            print(f"could not answer ({exc}); will pick it up again")
            await asyncio.sleep(2.0)


async def answer_mentions(client: RoomClient, spec: dict, pending: list[dict],
                          args: argparse.Namespace) -> None:
    for item in pending:
        room_id = item["room"]["room_id"]
        asked_by = item["utterance"]["author_name"]
        print(f"asked in {room_id} by {asked_by}: "
              f"{item['utterance']['text'][:70]}")
        await client.join(room_id)
        await asyncio.sleep(args.pace)      # "looking into it"
        await client.say(room_id, spec["answer"], to=asked_by)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", required=True, choices=["cs", "domain"])
    parser.add_argument("--platform", default="http://127.0.0.1:9100")
    parser.add_argument("--domain", choices=sorted(DOMAINS),
                        help="which internal domain this agent owns")
    parser.add_argument("--owner", default=None,
                        help="the human collaborator behind this agent")
    parser.add_argument("--agent", default=None, help="agent display name")
    parser.add_argument("--key-dir", default=None)
    parser.add_argument("--tick", type=float, default=1.0,
                        help="CS agent: seconds between ticket sweeps")
    parser.add_argument("--poll", type=float, default=20.0,
                        help="domain agent: long-poll seconds")
    parser.add_argument("--pace", type=float, default=2.0,
                        help="domain agent: seconds before answering")
    args = parser.parse_args()

    if args.role == "domain" and not args.domain:
        parser.error("--domain is required for --role domain")
    args.owner = args.owner or ("Support" if args.role == "cs" else "Internal")
    args.agent = args.agent or (
        "Support Hermes" if args.role == "cs"
        else f"{args.domain.capitalize()} Hermes")
    args.key_dir = args.key_dir or f"data/keys/{args.agent.lower().replace(' ', '-')}"
    args.platform = args.platform.rstrip("/")

    sys.stdout.reconfigure(line_buffering=True)
    runner = run_cs if args.role == "cs" else run_domain
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runner(args))


if __name__ == "__main__":
    main()
