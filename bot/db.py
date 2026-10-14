"""SQLite 存储：聊天历史 / 提醒（sqlite3 标准库，零依赖）"""
import sqlite3
import threading
import time
from pathlib import Path


class DB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        with self._lock:
            c = self._conn
            c.execute(
                """CREATE TABLE IF NOT EXISTS chat_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session TEXT NOT NULL,      -- 'group:123' / 'private:456'
                    role TEXT NOT NULL,         -- user / assistant
                    content TEXT NOT NULL,
                    ts REAL NOT NULL)"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_session ON chat_history(session, id)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS reminders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target TEXT NOT NULL,
                    fire_at REAL NOT NULL,
                    content TEXT NOT NULL,
                    owner TEXT NOT NULL)"""
            )
            c.commit()

    # ---------- 聊天历史 ----------
    def recent(self, session: str, n: int) -> list[tuple[str, str]]:
        """取某个会话最近 n 条 (role, content)，按时间正序"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM chat_history WHERE session=? "
                "ORDER BY id DESC LIMIT ?",
                (session, n),
            ).fetchall()
        return list(reversed([(r["role"], r["content"]) for r in rows]))

    def add_message(self, session: str, role: str, content: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_history(session, role, content, ts) VALUES(?,?,?,?)",
                (session, role, content, time.time()),
            )
            self._conn.commit()

    def trim(self, session: str, keep: int = 200):
        """防止某个会话的历史无限增长"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM chat_history WHERE session=? AND id NOT IN "
                "(SELECT id FROM chat_history WHERE session=? ORDER BY id DESC LIMIT ?)",
                (session, session, keep),
            )
            self._conn.commit()

    # ---------- 提醒 ----------
    def add_reminder(self, target: str, fire_at: float, content: str, owner: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders(target, fire_at, content, owner) VALUES(?,?,?,?)",
                (target, fire_at, content, owner),
            )
            self._conn.commit()
            return cur.lastrowid

    def due_reminders(self, now: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE fire_at<=? ORDER BY fire_at", (now,)
            ).fetchall()
        return [dict(r) for r in rows]

    def list_reminders(self, owner: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE owner=? ORDER BY fire_at", (owner,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_reminder(self, rid: int):
        with self._lock:
            self._conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
            self._conn.commit()
