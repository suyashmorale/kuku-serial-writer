"""SQLite canon store — the single source of truth for the story.

LangGraph checkpoints only hold *where a run is paused*. Everything that is *true in the
story* lives here, versioned by episode, so we can answer "what was true at ep N?" and
"which later episodes depended on facts ep N introduced?" — the basis for retroactive edits.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY,
    premise TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',      -- human style/setting brief for planning
    status TEXT NOT NULL DEFAULT 'planning',   -- planning | plan_review | writing | done
    bible TEXT,                                -- JSON Bible
    outline TEXT,                              -- JSON Outline (acts, arcs, character arcs)
    story_so_far TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_episodes (
    story_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    arc INTEGER NOT NULL,
    title TEXT NOT NULL,
    logline TEXT NOT NULL,
    purpose TEXT NOT NULL,
    characters TEXT NOT NULL,     -- JSON list
    threads TEXT NOT NULL,        -- JSON list of thread codes
    turning_point INTEGER NOT NULL DEFAULT 0,
    hook_idea TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (story_id, number)
);

CREATE TABLE IF NOT EXISTS plan_history (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    old TEXT NOT NULL,            -- JSON of the replaced plan row
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    story_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    status TEXT NOT NULL,         -- in_progress | committed
    thread_id TEXT,               -- LangGraph thread for the in-flight run
    title TEXT,
    text TEXT,
    word_count INTEGER,
    summary TEXT,
    hook TEXT,
    hook_type TEXT,
    in_story_time TEXT,
    key_beats TEXT,               -- JSON list
    beats TEXT,                   -- JSON BeatSheet used to write it
    critic TEXT,                  -- JSON CriticReport of the final draft
    human_edited INTEGER NOT NULL DEFAULT 0,
    stale INTEGER NOT NULL DEFAULT 0,
    stale_reason TEXT,
    committed_at REAL,
    PRIMARY KEY (story_id, number)
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    first_ep INTEGER NOT NULL,
    UNIQUE (story_id, name)
);

-- Versioned facts: an attribute's value holds for valid_from_ep..valid_to_ep (NULL = still true).
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    valid_from_ep INTEGER NOT NULL,
    valid_to_ep INTEGER,
    source_ep INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_by_entity ON facts (story_id, entity_id, attribute);

CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'planned',   -- planned | open | resolved
    planned_payoff_ep INTEGER,
    introduced_ep INTEGER,
    last_touched_ep INTEGER,
    resolved_ep INTEGER,
    from_bible INTEGER NOT NULL DEFAULT 0,
    UNIQUE (story_id, code)
);

CREATE TABLE IF NOT EXISTS thread_events (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    ep INTEGER NOT NULL,
    action TEXT NOT NULL,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL,
    ep INTEGER NOT NULL,
    in_story_time TEXT NOT NULL,
    event TEXT NOT NULL
);

-- Dependency index: which entities / threads each episode touched.
CREATE TABLE IF NOT EXISTS episode_refs (
    story_id INTEGER NOT NULL,
    ep INTEGER NOT NULL,
    kind TEXT NOT NULL,           -- entity | thread
    ref_id INTEGER NOT NULL,
    PRIMARY KEY (story_id, ep, kind, ref_id)
);

CREATE TABLE IF NOT EXISTS arc_summaries (
    story_id INTEGER NOT NULL,
    arc INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (story_id, arc)
);

CREATE TABLE IF NOT EXISTS directives (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL,
    rule TEXT NOT NULL,
    scope TEXT NOT NULL,
    from_ep INTEGER NOT NULL,
    until_ep INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    source_feedback TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- Observability: one row per LLM call.
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    story_id INTEGER,
    ep INTEGER,
    step TEXT NOT NULL,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,         -- ok | error | refusal | invalid_json
    error TEXT,
    trace_file TEXT,
    created_at REAL NOT NULL
);

-- Observability: decisions (critic verdicts, routing, human actions, budget stops).
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    story_id INTEGER,
    ep INTEGER,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()  # one connection shared by planner worker threads
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(stories)")}
        if "brief" not in cols:  # migrate DBs created before the brief column existed
            self.conn.execute("ALTER TABLE stories ADD COLUMN brief TEXT NOT NULL DEFAULT ''")
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def q1(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    # ---------- stories ----------

    def create_story(self, premise: str, brief: str = "") -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO stories (premise, brief, created_at) VALUES (?, ?, ?)",
                            (premise, brief, time.time()))
            return cur.lastrowid

    def latest_story_id(self) -> int | None:
        r = self.q1("SELECT id FROM stories ORDER BY id DESC LIMIT 1")
        return r["id"] if r else None

    def story(self, sid: int) -> sqlite3.Row:
        r = self.q1("SELECT * FROM stories WHERE id=?", sid)
        if r is None:
            raise KeyError(f"story {sid} not found")
        return r

    def set_story(self, sid: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE stories SET {cols} WHERE id=?", (*fields.values(), sid))

    def bible(self, sid: int) -> dict:
        return json.loads(self.story(sid)["bible"] or "{}")

    def outline(self, sid: int) -> dict:
        return json.loads(self.story(sid)["outline"] or "{}")

    # ---------- plan ----------

    def save_plan_episodes(self, sid: int, arc: int, episodes: list[dict]) -> None:
        with self.tx() as c:
            for e in episodes:
                c.execute(
                    """INSERT OR REPLACE INTO plan_episodes
                       (story_id, number, arc, title, logline, purpose, characters, threads, turning_point, hook_idea)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (sid, e["number"], arc, e["title"], e["logline"], e["purpose"], _j(e["characters"]),
                     _j(e["threads"]), int(e.get("turning_point", False)), e.get("hook_idea", "")),
                )

    def plan_episode(self, sid: int, n: int) -> dict | None:
        r = self.q1("SELECT * FROM plan_episodes WHERE story_id=? AND number=?", sid, n)
        return _plan_row(r) if r else None

    def plan_episodes(self, sid: int, start: int = 1, end: int = 10_000) -> list[dict]:
        rows = self.q("SELECT * FROM plan_episodes WHERE story_id=? AND number BETWEEN ? AND ? ORDER BY number",
                      sid, start, end)
        return [_plan_row(r) for r in rows]

    def update_plan_episode(self, sid: int, n: int, reason: str, **fields: Any) -> None:
        old = self.plan_episode(sid, n)
        if old is None:
            return
        for k in ("characters", "threads"):
            if k in fields:
                fields[k] = _j(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute("INSERT INTO plan_history (story_id, number, old, reason, created_at) VALUES (?,?,?,?,?)",
                      (sid, n, _j(old), reason, time.time()))
            c.execute(f"UPDATE plan_episodes SET {cols}, version=version+1 WHERE story_id=? AND number=?",
                      (*fields.values(), sid, n))

    # ---------- episodes ----------

    def episode(self, sid: int, n: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM episodes WHERE story_id=? AND number=?", sid, n)

    def committed_episodes(self, sid: int, start: int = 1, end: int = 10_000) -> list[sqlite3.Row]:
        return self.q("""SELECT * FROM episodes WHERE story_id=? AND status='committed'
                         AND number BETWEEN ? AND ? ORDER BY number""", sid, start, end)

    def last_committed(self, sid: int) -> int:
        r = self.q1("SELECT MAX(number) m FROM episodes WHERE story_id=? AND status='committed'", sid)
        return r["m"] or 0

    def start_episode(self, sid: int, n: int, thread_id: str) -> None:
        with self.tx() as c:
            c.execute("""INSERT INTO episodes (story_id, number, status, thread_id) VALUES (?,?, 'in_progress', ?)
                         ON CONFLICT(story_id, number) DO UPDATE SET thread_id=excluded.thread_id
                         WHERE status='in_progress'""", (sid, n, thread_id))

    def set_episode(self, sid: int, n: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE episodes SET {cols} WHERE story_id=? AND number=?", (*fields.values(), sid, n))

    # ---------- entities & versioned facts ----------

    def entity_by_name(self, sid: int, name: str) -> sqlite3.Row | None:
        """Resolve a name to one entity: exact match, then alias match ("Ira" / "Inspector Ritu" ->
        "Ira Sood" / "Ritu Chauhan"). Ambiguous short names resolve to nothing rather than guessing."""
        name = canonical_name(name)
        if not name:
            return None
        row = self.q1("SELECT * FROM entities WHERE story_id=? AND lower(name)=lower(?)", sid, name)
        if row:
            return row
        want = name_tokens(name)
        hits = [r for r in self.q("SELECT * FROM entities WHERE story_id=?", sid)
                if want and (want <= name_tokens(r["name"]) or name_tokens(r["name"]) <= want)]
        return hits[0] if len(hits) == 1 else None

    def ensure_entity(self, sid: int, name: str, kind: str, description: str, ep: int, c=None) -> int:
        row = self.entity_by_name(sid, name)
        if row:
            return row["id"]
        conn = c or self.conn
        cur = conn.execute("INSERT INTO entities (story_id, name, kind, description, first_ep) VALUES (?,?,?,?,?)",
                           (sid, canonical_name(name), kind, description, ep))
        return cur.lastrowid

    def set_fact(self, sid: int, entity_id: int, attribute: str, value: str, ep: int, c=None) -> bool:
        """Record attribute=value from episode `ep` onward. Returns True if canon changed."""
        conn = c or self.conn
        attribute = attribute.strip().lower()
        cur = conn.execute("""SELECT id, value FROM facts WHERE story_id=? AND entity_id=? AND attribute=?
                              AND valid_from_ep<=? AND (valid_to_ep IS NULL OR valid_to_ep>=?)
                              ORDER BY valid_from_ep DESC LIMIT 1""",
                           (sid, entity_id, attribute, ep, ep)).fetchone()
        if cur and cur["value"].strip().lower() == value.strip().lower():
            return False
        conn.execute("""INSERT INTO facts (story_id, entity_id, attribute, value, valid_from_ep, source_ep)
                        VALUES (?,?,?,?,?,?)""", (sid, entity_id, attribute, value.strip(), ep, ep))
        self._rechain(sid, entity_id, attribute, conn)
        return True

    def _rechain(self, sid: int, entity_id: int, attribute: str, conn) -> None:
        """Recompute validity windows so each version ends where the next begins."""
        rows = conn.execute("""SELECT id, valid_from_ep FROM facts WHERE story_id=? AND entity_id=? AND attribute=?
                               ORDER BY valid_from_ep, id""", (sid, entity_id, attribute)).fetchall()
        for i, r in enumerate(rows):
            nxt = rows[i + 1]["valid_from_ep"] - 1 if i + 1 < len(rows) else None
            if nxt is not None and nxt < r["valid_from_ep"]:  # two versions from the same ep: later id wins
                nxt = r["valid_from_ep"] - 1
            conn.execute("UPDATE facts SET valid_to_ep=? WHERE id=?", (nxt, r["id"]))

    def facts_as_of(self, sid: int, entity_id: int, ep: int) -> dict[str, str]:
        rows = self.q("""SELECT attribute, value FROM facts WHERE story_id=? AND entity_id=?
                         AND valid_from_ep<=? AND (valid_to_ep IS NULL OR valid_to_ep>=?)
                         ORDER BY valid_from_ep, id""", sid, entity_id, ep, ep)
        return {r["attribute"]: r["value"] for r in rows}

    def canon_snapshot(self, sid: int, ep: int) -> dict[int, dict[str, str]]:
        return {e["id"]: self.facts_as_of(sid, e["id"], ep)
                for e in self.q("SELECT id FROM entities WHERE story_id=?", sid)}

    # ---------- threads ----------

    def thread(self, sid: int, code: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM threads WHERE story_id=? AND code=?", sid, code)

    def upsert_thread(self, sid: int, code: str, title: str, description: str = "",
                      payoff: int | None = None, from_bible: bool = False, c=None) -> int:
        conn = c or self.conn
        row = conn.execute("SELECT id FROM threads WHERE story_id=? AND code=?", (sid, code)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute("""INSERT INTO threads (story_id, code, title, description, planned_payoff_ep, from_bible)
                              VALUES (?,?,?,?,?,?)""", (sid, code, title, description, payoff, int(from_bible)))
        return cur.lastrowid

    def next_thread_code(self, sid: int, c=None) -> str:
        conn = c or self.conn
        n = conn.execute("SELECT COUNT(*) n FROM threads WHERE story_id=?", (sid,)).fetchone()["n"]
        return f"T{n + 1:02d}"

    def recompute_thread(self, sid: int, thread_id: int, conn=None) -> None:
        conn = conn or self.conn
        evs = conn.execute("SELECT ep, action FROM thread_events WHERE story_id=? AND thread_id=? ORDER BY ep, id",
                           (sid, thread_id)).fetchall()
        status, introduced, last, resolved = "planned", None, None, None
        for e in evs:
            introduced = introduced or e["ep"]
            last = e["ep"]
            if e["action"] == "resolve":
                status, resolved = "resolved", e["ep"]
            elif status != "resolved":
                status = "open"
        conn.execute("""UPDATE threads SET status=?, introduced_ep=?, last_touched_ep=?, resolved_ep=?
                        WHERE id=?""", (status, introduced, last, resolved, thread_id))

    def threads(self, sid: int, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.q("SELECT * FROM threads WHERE story_id=? AND status=? ORDER BY code", sid, status)
        return self.q("SELECT * FROM threads WHERE story_id=? ORDER BY code", sid)

    # ---------- directives ----------

    def add_directive(self, sid: int, rule: str, scope: str, from_ep: int, until_ep: int, source: str) -> int:
        with self.tx() as c:
            cur = c.execute("""INSERT INTO directives (story_id, rule, scope, from_ep, until_ep, source_feedback, created_at)
                               VALUES (?,?,?,?,?,?,?)""", (sid, rule, scope, from_ep, until_ep, source, time.time()))
            return cur.lastrowid

    def active_directives(self, sid: int, ep: int) -> list[sqlite3.Row]:
        return self.q("""SELECT * FROM directives WHERE story_id=? AND active=1 AND from_ep<=?
                         AND (until_ep=0 OR until_ep>=?) ORDER BY id""", sid, ep, ep)

    def set_directive_active(self, sid: int, did: int, active: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE directives SET active=? WHERE story_id=? AND id=?", (int(active), sid, did))

    # ---------- episode effects (commit / revert) ----------

    def revert_episode_effects(self, sid: int, ep: int, c) -> None:
        """Remove everything episode `ep` contributed to canon, then rechain what's left."""
        affected = c.execute("SELECT DISTINCT entity_id, attribute FROM facts WHERE story_id=? AND source_ep=?",
                             (sid, ep)).fetchall()
        c.execute("DELETE FROM facts WHERE story_id=? AND source_ep=?", (sid, ep))
        for a in affected:
            self._rechain(sid, a["entity_id"], a["attribute"], c)
        tids = [r["thread_id"] for r in c.execute(
            "SELECT DISTINCT thread_id FROM thread_events WHERE story_id=? AND ep=?", (sid, ep)).fetchall()]
        c.execute("DELETE FROM thread_events WHERE story_id=? AND ep=?", (sid, ep))
        for t in tids:
            self.recompute_thread(sid, t, c)
        c.execute("DELETE FROM timeline WHERE story_id=? AND ep=?", (sid, ep))
        c.execute("DELETE FROM episode_refs WHERE story_id=? AND ep=?", (sid, ep))

    def dependents(self, sid: int, after_ep: int, entity_ids: set[int], thread_ids: set[int]) -> list[int]:
        eps: set[int] = set()
        for kind, ids in (("entity", entity_ids), ("thread", thread_ids)):
            if not ids:
                continue
            marks = ",".join("?" * len(ids))
            for r in self.q(f"""SELECT DISTINCT ep FROM episode_refs WHERE story_id=? AND ep>? AND kind=?
                                AND ref_id IN ({marks})""", sid, after_ep, kind, *ids):
                eps.add(r["ep"])
        return sorted(eps)

    # ---------- summaries ----------

    def set_arc_summary(self, sid: int, arc: int, text: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO arc_summaries (story_id, arc, text) VALUES (?,?,?)", (sid, arc, text))

    def arc_summaries(self, sid: int, before_arc: int | None = None) -> list[sqlite3.Row]:
        if before_arc is None:
            return self.q("SELECT * FROM arc_summaries WHERE story_id=? ORDER BY arc", sid)
        return self.q("SELECT * FROM arc_summaries WHERE story_id=? AND arc<? ORDER BY arc", sid, before_arc)

    # ---------- observability ----------

    def log_call(self, **row: Any) -> None:
        row.setdefault("created_at", time.time())
        cols = ", ".join(row)
        with self.tx() as c:
            c.execute(f"INSERT INTO llm_calls ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))

    def log_event(self, sid: int | None, ep: int | None, kind: str, payload: Any) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events (story_id, ep, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (sid, ep, kind, _j(payload), time.time()))

    def episode_cost(self, sid: int, ep: int) -> float:
        return self.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM llm_calls WHERE story_id=? AND ep=?", sid, ep)["c"]

    def story_cost(self, sid: int) -> float:
        return self.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM llm_calls WHERE story_id=?", sid)["c"]


TITLES = {"inspector", "mr", "mrs", "ms", "dr", "old", "young", "uncle", "aunty", "auntie", "ji", "sir", "madam",
          "constable", "si", "the"}


def canonical_name(name: str) -> str:
    """'Ira Sood, née Sharma' -> 'Ira Sood'; 'Mohan (bus-stand tea seller)' -> 'Mohan'."""
    name = re.sub(r"\(.*?\)", "", name.split(",")[0]).replace("’s", "'s").strip()
    return re.sub(r"\s+", " ", name)


def name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", canonical_name(name).lower()) if t not in TITLES}


def _plan_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["characters"] = json.loads(d["characters"])
    d["threads"] = json.loads(d["threads"])
    d["turning_point"] = bool(d["turning_point"])
    return d
