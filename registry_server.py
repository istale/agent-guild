"""The platform — one host that everyone's agent dials into.

    python registry_server.py --host 0.0.0.0 --port 9100

It does two things and holds no opinions about either:

  directory (AGNTCY-ish)          rooms (see room.py)
    POST   /agents                  POST /rooms
    POST   /agents/{did}/heartbeat  POST /rooms/{id}/join
    DELETE /agents/{did}            POST /rooms/{id}/utterances
    GET    /agents                  GET  /rooms/{id}?since=&wait=   (long-poll)
    GET    /agents/{did}            GET  /rooms
    GET    /search?q=...            GET  /watch   ← humans read the transcript

Agents only ever dial *out* to this host, so a laptop behind NAT needs no
inbound port. Run it on the machine both computers can reach (e.g. Tailnet).
"""
from __future__ import annotations

import argparse
import os

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

import knowledge
import room
import web
from agent_card import AgentCard
from directory import Directory

STORE = os.environ.get("HUB_REGISTRY_STORE", "data/registry.json")
directory = Directory(store=STORE)
app = FastAPI(title="Agent Collaboration Hub — Platform")
app.include_router(room.router)
app.include_router(knowledge.router)


@app.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    return RedirectResponse("/ops")


@app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
def chat() -> HTMLResponse:
    """What an external customer sees."""
    return web.customer_page()


@app.get("/ops", response_class=HTMLResponse, include_in_schema=False)
@app.get("/watch", response_class=HTMLResponse, include_in_schema=False)
def ops() -> HTMLResponse:
    """What the system developer sees: every room, every internal turn."""
    return web.ops_page()


@app.post("/agents")
def register(card: dict = Body(...)) -> dict:
    try:
        rec = directory.register(AgentCard.from_dict(card))
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"registered": rec.card.did, "health": rec.health}


@app.post("/agents/{did}/heartbeat")
def heartbeat(did: str) -> dict:
    if not directory.get(did):
        raise HTTPException(status_code=404, detail="unknown agent")
    rec = directory.heartbeat(did)
    return {"did": did, "health": rec.health, "last_seen": rec.last_seen}


@app.delete("/agents/{did}")
def deregister(did: str) -> dict:
    return {"removed": directory.deregister(did)}


@app.get("/agents")
def list_agents() -> dict:
    directory.mark_stale()
    return {"agents": [r.to_dict() for r in directory.list()]}


@app.get("/agents/{did}")
def get_agent(did: str) -> dict:
    rec = directory.resolve(did)
    if not rec:
        raise HTTPException(status_code=404, detail="unknown agent")
    return rec.to_dict()


@app.get("/search")
def search(q: str = "", owner_did: str | None = None, skill_id: str | None = None,
           limit: int = 10) -> dict:
    directory.mark_stale()
    matches = directory.search(q, owner_did=owner_did, skill_id=skill_id, limit=limit)
    return {
        "query": q,
        "matches": [
            {"card": m.card.to_dict(), "score": m.score,
             "matched_skills": m.matched_skills, "health": m.health}
            for m in matches
        ],
    }


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
