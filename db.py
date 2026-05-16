"""SQLite layer — users, presence samples, inactivity events."""
import os
import sqlite3
import threading
import time

_lock = threading.Lock()


def connect(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT,
    display_name TEXT,
    ignored INTEGER NOT NULL DEFAULT 0,
    last_active_ts REAL,
    current_state TEXT,
    current_state_since REAL,
    in_inactive_streak INTEGER NOT NULL DEFAULT 0,
    streak_started_ts REAL,
    streak_alerted INTEGER NOT NULL DEFAULT 0,
    updated_ts REAL
);

CREATE TABLE IF NOT EXISTS inactivity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    email TEXT,
    display_name TEXT,
    started_ts REAL NOT NULL,
    ended_ts REAL,
    duration_seconds REAL,
    state_during TEXT,
    alerted INTEGER NOT NULL DEFAULT 0,
    alerted_ts REAL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_started ON inactivity_events(started_ts);
CREATE INDEX IF NOT EXISTS idx_events_user ON inactivity_events(user_id);

CREATE TABLE IF NOT EXISTS poll_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    in_window INTEGER NOT NULL,
    users_polled INTEGER NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_poll_ts ON poll_log(ts);
"""


def init_db(conn):
    with _lock, conn:
        conn.executescript(SCHEMA)


def upsert_user(conn, uid, email, name):
    with _lock, conn:
        conn.execute(
            """INSERT INTO users(id, email, display_name, updated_ts)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 email=excluded.email,
                 display_name=excluded.display_name,
                 updated_ts=excluded.updated_ts""",
            (uid, email, name, time.time()),
        )


def set_user_state(conn, uid, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [uid]
    with _lock, conn:
        conn.execute(f"UPDATE users SET {cols} WHERE id=?", vals)


def set_ignored(conn, uid, ignored):
    with _lock, conn:
        conn.execute("UPDATE users SET ignored=? WHERE id=?",
                     (1 if ignored else 0, uid))


def bulk_set_ignored_by_email(conn, emails, ignored):
    if not emails:
        return
    placeholders = ",".join("?" * len(emails))
    with _lock, conn:
        conn.execute(
            f"UPDATE users SET ignored=? WHERE lower(email) IN ({placeholders})",
            [1 if ignored else 0] + [e.lower() for e in emails],
        )


def start_inactivity(conn, uid, email, name, state, started_ts):
    with _lock, conn:
        cur = conn.execute(
            """INSERT INTO inactivity_events
               (user_id, email, display_name, started_ts, state_during)
               VALUES (?, ?, ?, ?, ?)""",
            (uid, email, name, started_ts, state),
        )
        return cur.lastrowid


def end_inactivity(conn, uid, ended_ts):
    with _lock, conn:
        conn.execute(
            """UPDATE inactivity_events
               SET ended_ts=?, duration_seconds=?-started_ts
               WHERE user_id=? AND ended_ts IS NULL""",
            (ended_ts, ended_ts, uid),
        )


def mark_alerted(conn, uid, ts):
    with _lock, conn:
        conn.execute(
            """UPDATE inactivity_events
               SET alerted=1, alerted_ts=?
               WHERE user_id=? AND ended_ts IS NULL""",
            (ts, uid),
        )


def log_poll(conn, in_window, users_polled, note=None):
    with _lock, conn:
        conn.execute(
            "INSERT INTO poll_log(ts, in_window, users_polled, note) VALUES (?, ?, ?, ?)",
            (time.time(), 1 if in_window else 0, users_polled, note),
        )
        conn.execute(
            "DELETE FROM poll_log WHERE id < (SELECT MAX(id)-1000 FROM poll_log)"
        )


def all_users(conn):
    return conn.execute(
        "SELECT * FROM users ORDER BY display_name"
    ).fetchall()


def recent_events(conn, since_ts, limit=200):
    return conn.execute(
        """SELECT * FROM inactivity_events
           WHERE started_ts >= ?
           ORDER BY started_ts DESC LIMIT ?""",
        (since_ts, limit),
    ).fetchall()


def user_summary(conn, since_ts):
    return conn.execute(
        """SELECT u.id, u.email, u.display_name, u.ignored,
                  u.current_state, u.last_active_ts, u.in_inactive_streak,
                  u.streak_started_ts,
                  COUNT(e.id) AS event_count,
                  COALESCE(SUM(e.duration_seconds), 0) AS total_inactive_seconds,
                  COALESCE(SUM(e.alerted), 0) AS alert_count
           FROM users u
           LEFT JOIN inactivity_events e
             ON e.user_id=u.id AND e.started_ts >= ?
           GROUP BY u.id
           ORDER BY total_inactive_seconds DESC""",
        (since_ts,),
    ).fetchall()


def latest_poll(conn):
    row = conn.execute(
        "SELECT * FROM poll_log ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return row


def user_detail(conn, user_id, since_ts=None):
    """Get detailed information for a specific user including all events."""
    if since_ts is None:
        since_ts = 0
    
    user = conn.execute(
        "SELECT * FROM users WHERE id=?", (user_id,)
    ).fetchone()
    
    if not user:
        return None
    
    events = conn.execute(
        """SELECT * FROM inactivity_events
           WHERE user_id=? AND started_ts >= ?
           ORDER BY started_ts DESC""",
        (user_id, since_ts),
    ).fetchall()
    
    return {
        "user": dict(user),
        "events": [dict(e) for e in events],
    }


def user_daily_breakdown(conn, user_id, days=30):
    """Get daily inactivity breakdown for a user over the specified period."""
    since_ts = time.time() - days * 24 * 3600
    
    rows = conn.execute(
        """SELECT 
           DATE(started_ts, 'unixepoch') as date,
           COUNT(*) as event_count,
           COALESCE(SUM(duration_seconds), 0) as total_inactive_seconds,
           COALESCE(SUM(alerted), 0) as alert_count
           FROM inactivity_events
           WHERE user_id=? AND started_ts >= ?
           GROUP BY DATE(started_ts, 'unixepoch')
           ORDER BY date DESC""",
        (user_id, since_ts),
    ).fetchall()
    
    return [dict(r) for r in rows]


def user_trend_data(conn, user_id, days=30):
    """Get trend data for a user over the specified period."""
    since_ts = time.time() - days * 24 * 3600
    
    rows = conn.execute(
        """SELECT 
           DATE(started_ts, 'unixepoch') as date,
           COALESCE(SUM(duration_seconds), 0) as total_inactive_seconds,
           COUNT(*) as event_count
           FROM inactivity_events
           WHERE user_id=? AND started_ts >= ?
           GROUP BY DATE(started_ts, 'unixepoch')
           ORDER BY date ASC""",
        (user_id, since_ts),
    ).fetchall()
    
    return [dict(r) for r in rows]


def all_user_stats(conn, since_ts=None):
    """Get statistics for all users for comparison."""
    if since_ts is None:
        since_ts = time.time() - 7 * 24 * 3600
    
    rows = conn.execute(
        """SELECT 
           u.id, u.email, u.display_name, u.ignored,
           u.current_state, u.last_active_ts,
           COUNT(e.id) AS event_count,
           COALESCE(SUM(e.duration_seconds), 0) AS total_inactive_seconds,
           COALESCE(SUM(e.alerted), 0) AS alert_count,
           AVG(e.duration_seconds) as avg_inactive_seconds
           FROM users u
           LEFT JOIN inactivity_events e
             ON e.user_id=u.id AND e.started_ts >= ?
           GROUP BY u.id
           ORDER BY total_inactive_seconds DESC""",
        (since_ts,),
    ).fetchall()
    
    return [dict(r) for r in rows]
