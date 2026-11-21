"""SQLite storage using only the standard library `sqlite3` module."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    system_prompt TEXT,
    created_at TEXT NOT NULL
);
"""


def parse_database_url(url: str) -> str:
    """Turn `sqlite:////data/app.db` into `/data/app.db` (and `sqlite:///x.db` into `x.db`)."""
    url = url.strip()
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
    elif url.startswith("sqlite://"):
        path = url[len("sqlite://"):]
    else:
        path = url
    if not path:
        raise ValueError("DATABASE_URL does not contain a database path")
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, database_url: str):
        self.path = parse_database_url(database_url)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def ping(self) -> bool:
        try:
            with closing(self._connect()) as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def session_exists(self, session_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (session_id,)
            ).fetchone()
            if row:
                return True
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? LIMIT 1", (session_id,)
            ).fetchone()
            return row is not None

    def get_system_prompt(self, session_id: str) -> Optional[str]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT system_prompt FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return row["system_prompt"] if row else None

    def get_history(self, session_id: str) -> list:
        """Return [{role, content}] oldest first, ready to send to the LLM."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def list_messages(self, session_id: str) -> list:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        messages = []
        for row in rows:
            item = {"role": row["role"], "content": row["content"]}
            if row["role"] == "assistant" and row["metadata"]:
                item["metadata"] = json.loads(row["metadata"])
            messages.append(item)
        return messages

    def save_exchange(
        self,
        session_id: str,
        system_prompt: Optional[str],
        user_content: str,
        assistant_content: str,
        metadata: dict,
    ) -> None:
        """Persist the user turn and the assistant turn atomically (only after success)."""
        now = _now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (session_id, system_prompt, created_at) VALUES (?, ?, ?)",
                    (session_id, system_prompt, now),
                )
                if system_prompt is not None:
                    conn.execute(
                        "UPDATE sessions SET system_prompt = ? WHERE session_id = ?",
                        (system_prompt, session_id),
                    )
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, metadata, created_at) VALUES (?, 'user', ?, NULL, ?)",
                    (session_id, user_content, now),
                )
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, metadata, created_at) VALUES (?, 'assistant', ?, ?, ?)",
                    (session_id, assistant_content, json.dumps(metadata), _now()),
                )
