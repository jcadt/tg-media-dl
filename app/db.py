"""SQLite database for job queue and history."""

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
                name        TEXT NOT NULL,
                session_str TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
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


# ── Session helpers ──────────────────────────────────────────

def save_session(name: str, session_str: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sessions (name, session_str, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(name) DO UPDATE SET session_str=excluded.session_str, updated_at=excluded.updated_at""",
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
               channel_name: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, query, channel_id, channel_name) VALUES (?, ?, ?, ?)",
            (kind, query, channel_id, channel_name),
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
