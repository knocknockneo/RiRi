#!/usr/bin/env python3
"""Retention enforcement: delete session data older than RIRI_RETENTION_DAYS
(default 456 = study period + 1 year per the Privacy Plan). Run via cron:
  15 3 * * * cd /opt/riri && /opt/riri/venv/bin/python scripts/purge.py

v2: also purges the usage table (v1 left per-token daily rows forever — that's
behavioral metadata keyed to the same hash, so it falls under the same window),
and VACUUMs afterward so the file actually shrinks.
"""
import os, sqlite3, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
from riri import db

DAYS = int(os.environ.get("RIRI_RETENTION_DAYS", "456"))
cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
cutoff_day = cutoff[:10]

db.init()
with db.conn() as c:
    old = [r["id"] for r in c.execute(
        "SELECT id FROM sessions WHERE created_at < ?", (cutoff,))]
    for sid in old:
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        c.execute("DELETE FROM gate_submissions WHERE session_id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))
    usage_gone = c.execute(
        "DELETE FROM usage WHERE day < ?", (cutoff_day,)).rowcount

# VACUUM cannot run inside a transaction — raw connection, after commit.
raw = sqlite3.connect(db.DB_PATH)
try:
    raw.execute("VACUUM")
finally:
    raw.close()

print(f"Purged {len(old)} sessions and {usage_gone} usage rows older than {DAYS} days.")
