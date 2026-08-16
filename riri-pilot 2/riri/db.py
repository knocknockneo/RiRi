"""SQLite layer. One file DB, no ORM — pilot scale (4 sections, ~90 students).

v2:
- conn() is a real context manager: transaction on the way in, close() on the
  way out (v1 leaked a connection per call and never set a busy timeout).
- WAL + busy_timeout so a whole section hitting /api/chat at once doesn't
  throw 'database is locked'.
- init() runs additive migrations: an existing v1 riri.db upgrades in place
  (gate flag columns, indexes). Never destructive.
"""
import os, sqlite3, hashlib, uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("RIRI_DB", "riri.db")
BUSY_TIMEOUT_MS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens(
  token_hash TEXT PRIMARY KEY,
  label      TEXT,                 -- cohort tag only; never a name
  created_at TEXT NOT NULL,
  disabled   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions(
  id          TEXT PRIMARY KEY,
  token_hash  TEXT NOT NULL REFERENCES tokens(token_hash),
  mode        TEXT NOT NULL CHECK(mode IN ('artifact','assignment')),
  author_slug TEXT,                -- artifact mode only
  stage       TEXT,                -- assignment mode: invention|development|polishing
  voice_brief TEXT,               -- session-scoped; student holds the canonical .md
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  role       TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_submissions(   -- the 'documented student input'
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  from_stage  TEXT NOT NULL,
  to_stage    TEXT NOT NULL,
  submission  TEXT NOT NULL,
  flagged     INTEGER NOT NULL DEFAULT 0,      -- instructor-review flag, not a rejection
  flag_reason TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage(              -- per-token daily budget
  token_hash TEXT NOT NULL,
  day        TEXT NOT NULL,
  est_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(token_hash, day)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token   ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_gates_session    ON gate_submissions(session_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def conn():
    """Yield a connection inside a transaction; commit/rollback then CLOSE."""
    c = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    try:
        with c:          # transaction scope: commits on success, rolls back on error
            yield c
    finally:
        c.close()


def init():
    # journal_mode must change outside a transaction; use a raw connection.
    raw = sqlite3.connect(DB_PATH)
    try:
        raw.execute("PRAGMA journal_mode=WAL")
    finally:
        raw.close()
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c: sqlite3.Connection):
    """Additive-only upgrades for DBs created by older schema versions."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(gate_submissions)")}
    if "flagged" not in cols:
        c.execute("ALTER TABLE gate_submissions ADD COLUMN flagged INTEGER NOT NULL DEFAULT 0")
    if "flag_reason" not in cols:
        c.execute("ALTER TABLE gate_submissions ADD COLUMN flag_reason TEXT NOT NULL DEFAULT ''")


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.strip().upper().encode()).hexdigest()


def token_valid(raw: str) -> str | None:
    h = hash_token(raw)
    with conn() as c:
        row = c.execute(
            "SELECT token_hash FROM tokens WHERE token_hash=? AND disabled=0", (h,)
        ).fetchone()
    return h if row else None


def create_session(token_hash, mode, author_slug, stage, voice_brief) -> str:
    sid = uuid.uuid4().hex
    with conn() as c:
        c.execute(
            "INSERT INTO sessions(id,token_hash,mode,author_slug,stage,voice_brief,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, token_hash, mode, author_slug, stage, voice_brief, now()),
        )
    return sid


def get_session(sid, token_hash):
    with conn() as c:
        return c.execute(
            "SELECT * FROM sessions WHERE id=? AND token_hash=?", (sid, token_hash)
        ).fetchone()


def log_message(sid, role, content):
    with conn() as c:
        c.execute(
            "INSERT INTO messages(session_id,role,content,created_at) VALUES (?,?,?,?)",
            (sid, role, content, now()),
        )


def get_messages(sid, limit=40):
    with conn() as c:
        rows = c.execute(
            "SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (sid, limit),
        ).fetchall()
    msgs = [dict(r) for r in reversed(rows)]
    # Providers require history to start with a user turn. v1 relied on the
    # limit being even and messages logging in pairs; make it explicit so a
    # future odd limit or extra role can't silently break the API contract.
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def assistant_texts(sid, limit=40) -> list[str]:
    """Assistant replies in this session — for gate-submission overlap checks."""
    with conn() as c:
        rows = c.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='assistant'"
            " ORDER BY id DESC LIMIT ?",
            (sid, limit),
        ).fetchall()
    return [r["content"] for r in rows]


def log_gate(sid, from_stage, to_stage, submission, flagged=0, flag_reason=""):
    with conn() as c:
        c.execute(
            "INSERT INTO gate_submissions"
            "(session_id,from_stage,to_stage,submission,flagged,flag_reason,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, from_stage, to_stage, submission, int(flagged), flag_reason, now()),
        )
        c.execute("UPDATE sessions SET stage=? WHERE id=?", (to_stage, sid))


def add_usage(token_hash, est_tokens):
    day = now()[:10]
    with conn() as c:
        c.execute(
            "INSERT INTO usage(token_hash,day,est_tokens) VALUES (?,?,?) "
            "ON CONFLICT(token_hash,day) DO UPDATE SET est_tokens=est_tokens+excluded.est_tokens",
            (token_hash, day, est_tokens),
        )


def usage_today(token_hash) -> int:
    day = now()[:10]
    with conn() as c:
        row = c.execute(
            "SELECT est_tokens FROM usage WHERE token_hash=? AND day=?", (token_hash, day)
        ).fetchone()
    return row["est_tokens"] if row else 0
