"""SQLite journal: the single durable record of the campaign.

Event types:
  state — a game-state snapshot captured before judging (data.turn should be set)
  jev   — one TypeSafe System One call: request, response, latency, usage
  action— one operation issued to the game (MCP tool call), with its result
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "journal.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Journal:
    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        with self._connect() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS events (
                       id   INTEGER PRIMARY KEY AUTOINCREMENT,
                       ts   TEXT NOT NULL,
                       turn INTEGER,
                       type TEXT NOT NULL,
                       data TEXT NOT NULL)"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, id)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, type: str, data: dict, turn: int | None = None) -> dict:
        """Insert one event; returns {id, ts}."""
        if turn is None:
            turn = data.get("turn")
        payload = json.dumps(data, ensure_ascii=False)
        with self._lock, self._connect() as c:
            cur = c.execute(
                "INSERT INTO events (ts, turn, type, data) VALUES (?, ?, ?, ?)",
                (_now(), turn, type, payload),
            )
            return {"id": cur.lastrowid, "ts": _now()}

    def events(
        self,
        types: list[str] | None = None,
        limit: int = 100,
        before_id: int | None = None,
    ) -> list[dict]:
        q = "SELECT id, ts, turn, type, data FROM events"
        conds, params = [], []
        if types:
            conds.append(f"type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if before_id is not None:
            conds.append("id < ?")
            params.append(before_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as c:
            rows = c.execute(q, params).fetchall()
        return [
            {"id": r["id"], "ts": r["ts"], "turn": r["turn"], "type": r["type"],
             "data": json.loads(r["data"])}
            for r in rows
        ]

    def latest(self, type: str) -> dict | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT id, ts, turn, type, data FROM events WHERE type = ? "
                "ORDER BY id DESC LIMIT 1",
                (type,),
            ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "ts": row["ts"], "turn": row["turn"],
                "type": row["type"], "data": json.loads(row["data"])}

    def stats(self) -> dict:
        with self._connect() as c:
            counts = {
                r["type"]: r["n"]
                for r in c.execute(
                    "SELECT type, COUNT(*) AS n FROM events GROUP BY type"
                ).fetchall()
            }
            tokens = c.execute(
                "SELECT data FROM events WHERE type = 'jev'"
            ).fetchall()
            gates = c.execute("SELECT data FROM events WHERE type = 'gate'").fetchall()
        tin = tout = 0
        for row in tokens:
            usage = (json.loads(row["data"]).get("response") or {}).get("usage") or {}
            tin += usage.get("input_tokens", 0) or 0
            tout += usage.get("output_tokens", 0) or 0
        skipped = sum(
            1 for g in gates
            if not json.loads(g["data"]).get("result", {}).get("should_ask")
        )
        return {
            "jev": counts.get("jev", 0),
            "actions": counts.get("action", 0),
            "states": counts.get("state", 0),
            "gate_checks": counts.get("gate", 0),
            "gate_skipped": skipped,
            "tokens_in": tin,
            "tokens_out": tout,
        }
