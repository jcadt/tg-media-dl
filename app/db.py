"""SQLite database for job queue, history, users, and requests."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

DB_PATH = settings.DATA_DIR / "tgmediadl.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                session_str TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                email       TEXT NOT NULL UNIQUE,
                name        TEXT NOT NULL DEFAULT '',
                role        TEXT NOT NULL DEFAULT 'user'
                            CHECK(role IN ('admin', 'user')),
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                last_login  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL REFERENCES users(id),
                user_email  TEXT NOT NULL,
                user_name   TEXT NOT NULL DEFAULT '',
                kind        TEXT NOT NULL CHECK(kind IN ('series', 'movie')),
                query       TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending', 'approved', 'rejected', 'error')),
                message     TEXT DEFAULT '',
                job_id      INTEGER REFERENCES jobs(id),
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id  INTEGER REFERENCES requests(id),
                kind        TEXT NOT NULL CHECK(kind IN ('series','movie')),
                query       TEXT NOT NULL,
                channel_id  INTEGER,
                channel_name TEXT,
                status      TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','scanning','downloading','done','error')),
                progress    TEXT DEFAULT '{}',
                total_files INTEGER DEFAULT 0,
                completed_files INTEGER DEFAULT 0,
                error_msg   TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS downloads (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                msg_id      INTEGER DEFAULT 0,
                file_name   TEXT NOT NULL,
                file_size   INTEGER DEFAULT 0,
                downloaded_path TEXT,
                media_path      TEXT,
                status      TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','downloading','done','skipped','error')),
                error_msg   TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)


# ── User helpers ───────────────────────────────────────────

def upsert_user(user_id: str, email: str, name: str, role: str = "user") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO users (id, email, name, role, last_login)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 email=excluded.email,
                 name=excluded.name,
                 last_login=excluded.last_login""",
            (user_id, email, name, role),
        )


def get_user(user_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()
    return dict(row) if row else None


# ── Request helpers ─────────────────────────────────────────

def create_request(user_id: str, user_email: str, user_name: str,
                   kind: str, query: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO requests (user_id, user_email, user_name, kind, query)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, user_email, user_name, kind, query),
        )
        return cur.lastrowid


def update_request(req_id: int, **kwargs: Any) -> None:
    fields = [f"{k}=?" for k in kwargs]
    vals = list(kwargs.values())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE requests SET {', '.join(fields)}, updated_at=datetime('now') WHERE id=?",
            (*vals, req_id),
        )


def get_request(req_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    return dict(row) if row else None


def list_requests(limit: int = 50, status: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM requests WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM requests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def list_pending_requests(limit: int = 50) -> list[dict]:
    return list_requests(limit=limit, status="pending")


# ── Session helpers ──────────────────────────────────────────

def save_session(name: str, session_str: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sessions (name, session_str, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(name) DO UPDATE SET
                 session_str=excluded.session_str,
                 updated_at=excluded.updated_at""",
            (name, session_str),
        )


def load_session(name: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_str FROM sessions WHERE name = ?", (name,)
        ).fetchone()
    return row["session_str"] if row else None


# ── Job helpers ──────────────────────────────────────────────

def create_job(kind: str, query: str, channel_id: int | None = None,
               channel_name: str | None = None,
               request_id: int | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO jobs (kind, query, channel_id, channel_name, request_id)
               VALUES (?, ?, ?, ?, ?)""",
            (kind, query, channel_id, channel_name, request_id),
        )
        return cur.lastrowid


def update_job(job_id: int, **kwargs: Any) -> None:
    fields = []
    vals: list = []
    for k, v in kwargs.items():
        fields.append(f"{k}=?")
        if isinstance(v, (dict, list)):
            vals.append(json.dumps(v))
        else:
            vals.append(v)
    vals.append(datetime.now(timezone.utc).isoformat())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE jobs SET {', '.join(fields)}, updated_at=? WHERE id=?",
            (*vals, job_id),
        )


def get_job(job_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Download helpers ─────────────────────────────────────────

def create_download(job_id: int, file_name: str, file_size: int = 0,
                    msg_id: int = 0) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO downloads (job_id, file_name, file_size, msg_id) VALUES (?, ?, ?, ?)",
            (job_id, file_name, file_size, msg_id),
        )
        return cur.lastrowid


def update_download(dl_id: int, **kwargs: Any) -> None:
    fields = [f"{k}=?" for k in kwargs]
    vals = list(kwargs.values())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE downloads SET {', '.join(fields)} WHERE id=?",
            (*vals, dl_id),
        )


def get_job_downloads(job_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
    return [dict(r) for r in rows]
