"""Small local state that is NOT knowledge: file claims and reserved ids.

Kept in SQLite under data/ rather than in Qdrant because it needs things a
vector store does not give: atomic check-and-set across several processes
(each MCP client launches its own server, so parallel agents are parallel
processes) and cheap expiry. BEGIN IMMEDIATE takes SQLite's write lock up
front, which makes "is it free? then take it" one indivisible step -- the
whole point of a claim or a reservation.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "rememory-local.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    project   TEXT NOT NULL,
    path      TEXT NOT NULL,
    agent     TEXT NOT NULL,
    note      TEXT NOT NULL DEFAULT '',
    claimed_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (project, path)
);
CREATE TABLE IF NOT EXISTS reservations (
    project   TEXT NOT NULL,
    sequence  TEXT NOT NULL,
    number    INTEGER NOT NULL,
    agent     TEXT NOT NULL DEFAULT '',
    note      TEXT NOT NULL DEFAULT '',
    reserved_at REAL NOT NULL,
    PRIMARY KEY (project, sequence, number)
);
"""


@contextmanager
def connect(path: Path | None = None):
    db = Path(path or DB_PATH)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=15, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        yield conn
    finally:
        conn.close()


@contextmanager
def write_txn(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def norm_path(p: str) -> str:
    """Claims compare repo-relative POSIX paths; accept what agents type."""
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


# ------------------------------------------------------------------- claims
def claim(project: str, agent: str, paths: list[str], *, note: str = "",
          ttl_minutes: int = 120, force: bool = False, db: Path | None = None) -> dict:
    """Claim paths for `agent`. Paths held (unexpired) by another agent are
    reported as conflicts and NOT taken unless force=True. Re-claiming your
    own path just extends it. A claimed directory covers everything under it."""
    now = time.time()
    expires = now + max(1, ttl_minutes) * 60
    wanted = [norm_path(p) for p in paths if p and p.strip()]
    granted, conflicts = [], []
    with connect(db) as conn, write_txn(conn):
        conn.execute("DELETE FROM claims WHERE expires_at < ?", (now,))
        rows = conn.execute(
            "SELECT path, agent, note, expires_at FROM claims WHERE project = ?", (project,)
        ).fetchall()
        for path in wanted:
            clash = next((r for r in rows if r[1] != agent and _overlaps(r[0], path)), None)
            if clash and not force:
                conflicts.append({"path": path, "held_by": clash[1], "held_path": clash[0],
                                  "note": clash[2],
                                  "minutes_left": round((clash[3] - now) / 60)})
                continue
            conn.execute(
                "INSERT INTO claims (project, path, agent, note, claimed_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(project, path) DO UPDATE SET "
                "agent=excluded.agent, note=excluded.note, claimed_at=excluded.claimed_at, "
                "expires_at=excluded.expires_at",
                (project, path, agent, note, now, expires),
            )
            granted.append(path)
    return {"granted": granted, "conflicts": conflicts,
            "expires_in_minutes": max(1, ttl_minutes)}


def release(project: str, agent: str, paths: list[str] | None = None,
            db: Path | None = None) -> int:
    """Release this agent's claims (all of them when paths is empty)."""
    with connect(db) as conn, write_txn(conn):
        if paths:
            n = 0
            for p in paths:
                n += conn.execute(
                    "DELETE FROM claims WHERE project=? AND agent=? AND path=?",
                    (project, agent, norm_path(p)),
                ).rowcount
            return n
        return conn.execute(
            "DELETE FROM claims WHERE project=? AND agent=?", (project, agent)
        ).rowcount


def list_claims(project: str, paths: list[str] | None = None,
                db: Path | None = None) -> list[dict]:
    now = time.time()
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT path, agent, note, claimed_at, expires_at FROM claims "
            "WHERE project = ? AND expires_at >= ? ORDER BY claimed_at",
            (project, now),
        ).fetchall()
    wanted = [norm_path(p) for p in paths or []]
    out = []
    for path, agent, note, claimed, expires in rows:
        if wanted and not any(_overlaps(path, w) for w in wanted):
            continue
        out.append({"path": path, "agent": agent, "note": note,
                    "claimed_minutes_ago": round((now - claimed) / 60),
                    "minutes_left": round((expires - now) / 60)})
    return out


def _overlaps(a: str, b: str) -> bool:
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


# ------------------------------------------------------------- reservations
def reserve(project: str, sequence: str, floor: int, *, agent: str = "", note: str = "",
            db: Path | None = None) -> int:
    """Atomically reserve the next number above BOTH `floor` (the highest one
    already used in the repo) and every earlier reservation."""
    with connect(db) as conn, write_txn(conn):
        row = conn.execute(
            "SELECT MAX(number) FROM reservations WHERE project=? AND sequence=?",
            (project, sequence),
        ).fetchone()
        number = max(int(floor), int(row[0] or 0)) + 1
        conn.execute(
            "INSERT INTO reservations (project, sequence, number, agent, note, reserved_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project, sequence, number, agent, note, time.time()),
        )
    return number


def reservations(project: str, sequence: str, above: int = 0,
                 db: Path | None = None) -> list[dict]:
    """Reservations not yet visible in the repo (number > `above`)."""
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT number, agent, note, reserved_at FROM reservations "
            "WHERE project=? AND sequence=? AND number > ? ORDER BY number",
            (project, sequence, above),
        ).fetchall()
    return [{"number": n, "agent": a, "note": t,
             "reserved_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r))}
            for n, a, t, r in rows]
