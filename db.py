"""Durable storage — SQLite behind the in-memory stores.

The guild keeps rooms and the answer log in memory while it runs, because
long-polling readers and the board sweep both want cheap access to the whole
state. This module is the durable mirror: every mutation is written through
immediately, and on startup the stores read themselves back.

What is *not* here is the directory. Agents re-register and heartbeat on
reconnect, so a lost directory heals itself within a poll; a lost transcript
does not.

    HUB_DB=data/guild.db    # default; set to "" or ":memory:" to run without
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id     TEXT PRIMARY KEY,
    topic       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'normal',
    needs_human INTEGER NOT NULL DEFAULT 0,
    customer    TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    guest_token TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS utterances (
    utterance_id TEXT PRIMARY KEY,
    room_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    author_did   TEXT NOT NULL DEFAULT '',
    author_name  TEXT NOT NULL DEFAULT '',
    author_owner TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL,
    to_name      TEXT NOT NULL DEFAULT '',
    needs_input  INTEGER NOT NULL DEFAULT 0,
    text         TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS utterances_by_room ON utterances (room_id, seq);

CREATE TABLE IF NOT EXISTS participants (
    room_id   TEXT NOT NULL,
    did       TEXT NOT NULL,
    name      TEXT NOT NULL,
    owner     TEXT NOT NULL DEFAULT '',
    joined_at REAL NOT NULL,
    PRIMARY KEY (room_id, did)
);

CREATE TABLE IF NOT EXISTS claims (
    utterance_id TEXT PRIMARY KEY,
    room_id      TEXT NOT NULL,
    agent        TEXT NOT NULL,
    claimed_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge (
    entry_id   TEXT PRIMARY KEY,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    by_agent   TEXT NOT NULL DEFAULT '',
    by_human   TEXT NOT NULL DEFAULT '',
    room_id    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    """A tiny write-through store. Every method is safe to call from the
    request threads FastAPI runs handlers on."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    # ------------------------------------------------------------- rooms
    def save_room(self, room: dict) -> None:
        self._write(
            """INSERT INTO rooms (room_id, topic, kind, status, priority,
                                  needs_human, customer, created_by, created_at,
                                  guest_token)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(room_id) DO UPDATE SET
                 topic=excluded.topic, kind=excluded.kind,
                 status=excluded.status, priority=excluded.priority,
                 needs_human=excluded.needs_human, customer=excluded.customer""",
            (room["room_id"], room["topic"], room["kind"], room["status"],
             room["priority"], int(room["needs_human"]), room["customer"],
             room["created_by"], room["created_at"], room["guest_token"]))

    def save_utterance(self, room_id: str, u: dict) -> None:
        self._write(
            """INSERT OR REPLACE INTO utterances
               (utterance_id, room_id, seq, author_did, author_name,
                author_owner, kind, to_name, needs_input, text, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (u["utterance_id"], room_id, u["seq"], u["author_did"],
             u["author_name"], u["author_owner"], u["kind"], u["to"],
             int(u["needs_input"]), u["text"], u["created_at"]))

    def save_participant(self, room_id: str, p: dict) -> None:
        self._write(
            """INSERT OR REPLACE INTO participants
               (room_id, did, name, owner, joined_at) VALUES (?,?,?,?,?)""",
            (room_id, p["did"], p["name"], p["owner"], p["joined_at"]))

    def save_claim(self, room_id: str, utterance_id: str, agent: str,
                   at: float) -> None:
        self._write(
            """INSERT OR REPLACE INTO claims
               (utterance_id, room_id, agent, claimed_at) VALUES (?,?,?,?)""",
            (utterance_id, room_id, agent, at))

    def drop_claim(self, utterance_id: str) -> None:
        self._write("DELETE FROM claims WHERE utterance_id=?", (utterance_id,))

    def load_rooms(self) -> list[dict]:
        rooms = []
        for row in self._read("SELECT * FROM rooms ORDER BY created_at"):
            room = dict(row)
            room["needs_human"] = bool(room["needs_human"])
            rid = room["room_id"]
            room["utterances"] = [
                {**dict(u), "needs_input": bool(u["needs_input"])}
                for u in self._read(
                    "SELECT * FROM utterances WHERE room_id=? ORDER BY seq",
                    (rid,))]
            room["participants"] = [dict(p) for p in self._read(
                "SELECT * FROM participants WHERE room_id=?", (rid,))]
            room["claims"] = {c["utterance_id"]: {"agent": c["agent"],
                                                  "at": c["claimed_at"]}
                              for c in self._read(
                                  "SELECT * FROM claims WHERE room_id=?", (rid,))}
            rooms.append(room)
        return rooms

    # ------------------------------------------------------------- knowledge
    def save_entry(self, entry: dict) -> None:
        self._write(
            """INSERT INTO knowledge (entry_id, question, answer, by_agent,
                                      by_human, room_id, created_at, used)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(entry_id) DO UPDATE SET used=excluded.used""",
            (entry["entry_id"], entry["question"], entry["answer"],
             entry["by_agent"], entry["by_human"], entry["room_id"],
             entry["created_at"], entry["used"]))

    def load_entries(self) -> list[dict]:
        return [dict(r) for r in
                self._read("SELECT * FROM knowledge ORDER BY created_at")]

    def counts(self) -> dict:
        return {name: self._read(f"SELECT COUNT(*) AS n FROM {name}")[0]["n"]
                for name in ("rooms", "utterances", "claims", "knowledge")}


def open_default() -> Database | None:
    """The database the platform uses, or None when persistence is off."""
    path = os.environ.get("HUB_DB", "data/guild.db").strip()
    if not path or path == ":memory:":
        return None
    return Database(path)
