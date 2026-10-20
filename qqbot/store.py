"""SQLite 持久化：消息流水 + 审计日志 + 键值状态。

同步 sqlite3 + 线程锁。调用方在事件循环里直接调（单次操作微秒级），
量大时再考虑丢线程池。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    time        INTEGER NOT NULL,
    message_id  INTEGER,
    message_type TEXT,
    sub_type    TEXT,
    group_id    INTEGER,
    user_id     INTEGER,
    sender_name TEXT,
    sender_card TEXT,
    direction   TEXT NOT NULL DEFAULT 'in',
    raw_message TEXT,
    segments    TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_group_time ON messages(group_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_user_time  ON messages(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_mid        ON messages(message_id);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,
    actor   TEXT,
    action  TEXT NOT NULL,
    params  TEXT,
    result  TEXT,
    ok      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(id DESC);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'fact',
    user_id INTEGER,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_at ON memories(id DESC);

-- 情节记忆：一轮对话/一天里发生过什么，带情绪与显著度
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          INTEGER NOT NULL,
    user_id     INTEGER,
    kind        TEXT NOT NULL DEFAULT 'chat',
    summary     TEXT NOT NULL,
    emotion     TEXT,
    salience    REAL NOT NULL DEFAULT 0.5,
    source      TEXT,
    source_msg_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_at     ON events(id DESC);
CREATE INDEX IF NOT EXISTS idx_events_user   ON events(user_id, id DESC);

-- 自我叙事：她自己的时间线（日程、发生的小事、念头）
CREATE TABLE IF NOT EXISTS life_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,
    day     TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'event',
    content TEXT NOT NULL,
    mood    TEXT,
    tags    TEXT
);
CREATE INDEX IF NOT EXISTS idx_life_day ON life_log(day, id DESC);

-- 每日小结
CREATE TABLE IF NOT EXISTS day_summary (
    day        TEXT PRIMARY KEY,
    at         INTEGER NOT NULL,
    summary    TEXT NOT NULL,
    mood       TEXT,
    highlights TEXT
);

CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER NOT NULL,
    digest     TEXT UNIQUE,
    path       TEXT,
    kind       TEXT,
    summary    TEXT,
    sender_id  INTEGER,
    message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_images_at ON images(id DESC);


CREATE TABLE IF NOT EXISTS stickers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           INTEGER NOT NULL,
    digest       TEXT UNIQUE,
    path         TEXT,
    summary      TEXT,
    tags         TEXT,
    note         TEXT,
    sender_id    INTEGER,
    message_id   INTEGER,
    rating       REAL NOT NULL DEFAULT 0,
    used_count   INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER,
    persona      TEXT
);
CREATE INDEX IF NOT EXISTS idx_stickers_at ON stickers(id DESC);


CREATE TABLE IF NOT EXISTS qzone_feeds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            INTEGER NOT NULL,
    tid           TEXT UNIQUE,
    uin           INTEGER,
    nickname      TEXT,
    content       TEXT,
    created_time  INTEGER,
    comment_count INTEGER,
    like_count    INTEGER,
    pic_count     INTEGER,
    persona       TEXT
);
CREATE INDEX IF NOT EXISTS idx_qzone_feeds_at ON qzone_feeds(id DESC);


CREATE TABLE IF NOT EXISTS day_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER NOT NULL,
    day        TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    summary    TEXT,
    mood       TEXT,
    highlights TEXT,
    persona    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_day_notes_day_user ON day_notes(day, user_id);

CREATE TABLE IF NOT EXISTS day_memo (
    day          TEXT NOT NULL,
    user_id      INTEGER NOT NULL,
    persona      TEXT NOT NULL DEFAULT 'shenzhiwei',
    memo         TEXT NOT NULL DEFAULT '',
    last_msg_id  INTEGER NOT NULL DEFAULT 0,
    msg_count    INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id, persona)
);

CREATE TABLE IF NOT EXISTS trait_marks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,
    persona TEXT NOT NULL DEFAULT 'xiaojingyu',
    kind    TEXT NOT NULL DEFAULT 'event',
    source  TEXT,
    note    TEXT,
    dims    TEXT,
    before  TEXT,
    after   TEXT
);
CREATE INDEX IF NOT EXISTS idx_trait_marks_at ON trait_marks(id DESC);
CREATE INDEX IF NOT EXISTS idx_day_notes_user ON day_notes(user_id, id DESC);

"""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """老库平滑升级：后加的列。"""
        cols = {row["name"] for row in self._db.execute("PRAGMA table_info(memories)")}
        for name, ddl in (
            ("source", "TEXT"),
            ("salience", "REAL NOT NULL DEFAULT 0.5"),
            ("last_used_at", "INTEGER"),
            ("weight", "REAL NOT NULL DEFAULT 1.0"),
        ):
            if name not in cols:
                self._db.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl}")
        cols = {row["name"] for row in self._db.execute("PRAGMA table_info(events)")}
        for name, ddl in (("mood_delta", "TEXT"),):
            if name not in cols:
                self._db.execute(f"ALTER TABLE events ADD COLUMN {name} {ddl}")
        # 语音转写 / 识图描述：存一份「人话」版本，重建历史时优先用它
        cols = {row["name"] for row in self._db.execute("PRAGMA table_info(messages)")}
        if "text" not in cols:
            self._db.execute("ALTER TABLE messages ADD COLUMN text TEXT")
        # 多角色：记忆 / 情节 / 生活线 按角色隔离，老数据归到第一张卡
        cols = {row["name"] for row in self._db.execute("PRAGMA table_info(qzone_feeds)")}
        for name, ddl in (
            ("liked", "INTEGER NOT NULL DEFAULT 0"),
            ("comment_text", "TEXT"),
            ("interacted_at", "INTEGER"),
            ("interest", "REAL"),
            ("verdict_reason", "TEXT"),
            ("verdict_at", "INTEGER"),
        ):
            if name not in cols:
                self._db.execute(f"ALTER TABLE qzone_feeds ADD COLUMN {name} {ddl}")
        cols = {row["name"] for row in self._db.execute("PRAGMA table_info(images)")}
        if "summary_version" not in cols:
            self._db.execute(
                "ALTER TABLE images ADD COLUMN summary_version INTEGER NOT NULL DEFAULT 0"
            )
        for table in ("memories", "events", "life_log", "day_summary"):
            cols = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
            if "persona" not in cols:
                self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN persona TEXT NOT NULL DEFAULT 'shenzhiwei'"
                )

    # ---------- 消息 ----------

    def add_message(
        self,
        *,
        time_: int,
        message_id: int | None,
        message_type: str | None,
        sub_type: str | None = None,
        group_id: int | None = None,
        user_id: int | None = None,
        sender_name: str | None = None,
        sender_card: str | None = None,
        direction: str = "in",
        raw_message: str | None = None,
        segments: Any = None,
    ) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (time, message_id, message_type, sub_type, group_id,"
                " user_id, sender_name, sender_card, direction, raw_message, segments, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time_,
                    message_id,
                    message_type,
                    sub_type,
                    group_id,
                    user_id,
                    sender_name,
                    sender_card,
                    direction,
                    raw_message,
                    _json(segments) if segments is not None else None,
                    int(time.time()),
                ),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def set_message_text(self, message_id: int | None, text: str, *, direction: str = "in") -> None:
        """把语音转写 / 图片描述回写到消息上，重建历史时就不用再看 [语音]/[表情包]。"""
        if not message_id or not text:
            return
        with self._lock:
            self._db.execute(
                "UPDATE messages SET text = ? WHERE message_id = ? AND direction = ?",
                (text, int(message_id), direction),
            )
            self._db.commit()

    def messages(
        self,
        *,
        group_id: int | None = None,
        user_id: int | None = None,
        direction: str | None = None,
        limit: int = 50,
        before_id: int | None = None,
        after_id: int | None = None,
        keyword: str | None = None,
        oldest_first: bool = False,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses: list[str] = []
        args: list[Any] = []
        if group_id is not None:
            clauses.append("group_id = ?")
            args.append(group_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            args.append(user_id)
        if direction:
            clauses.append("direction = ?")
            args.append(direction)
        if before_id:
            clauses.append("id < ?")
            args.append(before_id)
        if after_id:
            clauses.append("id > ?")
            args.append(after_id)
        if since is not None:
            clauses.append("CAST(time AS REAL) >= ?")
            args.append(float(since))
        if keyword:
            clauses.append("raw_message LIKE ?")
            args.append(f"%{keyword}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ASC" if oldest_first else "DESC"
        sql = f"SELECT * FROM messages {where} ORDER BY id {order} LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            if item.get("segments"):
                try:
                    item["segments"] = json.loads(item["segments"])
                except Exception:
                    pass
            out.append(item)
        if not oldest_first:
            out.reverse()
        return out

    def last_message_at(self, *, user_id: int | None = None, direction: str | None = None) -> float | None:
        """最近一条消息的时间戳（用于判断「他多久没说话了」）。"""
        clauses: list[str] = []
        args: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            args.append(user_id)
        if direction:
            clauses.append("direction = ?")
            args.append(direction)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._db.execute(
                f"SELECT time FROM messages {where} ORDER BY id DESC LIMIT 1", args
            ).fetchone()
        if row is None:
            return None
        try:
            return float(row["time"])
        except (TypeError, ValueError):
            return None

    # ---------- 审计 ----------

    def audit(self, actor: str, action: str, params: Any, result: Any, ok: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO audit (at, actor, action, params, result, ok) VALUES (?,?,?,?,?,?)",
                (int(time.time()), actor, action, _json(params), _json(result), 1 if ok else 0),
            )
            self._db.commit()

    def audits(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- kv ----------

    def set_kv(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, _json(value)),
            )
            self._db.commit()

    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except Exception:
            return default

    # ---------- 长期记忆 ----------

    def add_memory(
        self,
        content: str,
        *,
        kind: str = "fact",
        user_id: int | None = None,
        source: str = "chat",
        salience: float = 0.5,
        persona: str | None = None,
    ) -> int:
        content = " ".join(str(content).split()).strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO memories (at, kind, user_id, content, source, salience, persona)"
                " VALUES (?,?,?,?,?,?,COALESCE(?, 'shenzhiwei'))",
                (
                    int(time.time()),
                    kind,
                    user_id,
                    content[:1000],
                    source,
                    float(salience),
                    persona,
                ),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def memories(
        self,
        *,
        limit: int = 50,
        user_id: int | None = None,
        persona: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = ["1=1"]
        args: list[Any] = []
        if user_id is not None:
            clauses.append("(user_id = ? OR user_id IS NULL)")
            args.append(user_id)
        if persona:
            clauses.append("persona = ?")
            args.append(persona)
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        sql = f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def search_memories(
        self,
        *,
        user_id: int | None = None,
        terms: list[str] | None = None,
        limit: int = 12,
        max_age_days: int = 365,
        persona: str | None = None,
    ) -> list[dict[str, Any]]:
        """关键词 + 时间衰减 + 显著度的粗检索（无向量库时的够用版）。"""
        limit = max(1, min(int(limit), 100))
        since = int(time.time()) - max_age_days * 86400
        sql = "SELECT * FROM memories WHERE at >= ?"
        args: list[Any] = [since]
        if user_id is not None:
            sql += " AND (user_id = ? OR user_id IS NULL)"
            args.append(user_id)
        if persona:
            sql += " AND persona = ?"
            args.append(persona)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        terms = [t.lower() for t in (terms or []) if len(t) >= 1]

        def score(row: dict[str, Any]) -> float:
            text = str(row.get("content") or "").lower()
            hit = sum(1.0 for t in terms if t in text)
            age_days = max(0.0, (time.time() - (row.get("at") or 0)) / 86400)
            recency = 0.5 ** (age_days / 30.0)
            salience = float(row.get("salience") or 0.5)
            return hit * 2.0 + recency * 1.0 + salience * 0.5

        items = [dict(row) for row in rows]
        items.sort(key=score, reverse=True)
        return items[:limit]

    def touch_memories(self, ids: list[int]) -> None:
        if not ids:
            return
        now = int(time.time())
        with self._lock:
            self._db.executemany(
                "UPDATE memories SET last_used_at = ? WHERE id = ?",
                [(now, int(i)) for i in ids],
            )
            self._db.commit()

    def delete_memory(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
            self._db.commit()
            return cur.rowcount > 0

    # ---------- 情节记忆 ----------

    def add_event(
        self,
        summary: str,
        *,
        user_id: int | None = None,
        kind: str = "chat",
        emotion: str | None = None,
        salience: float = 0.5,
        source: str | None = None,
        source_msg_id: int | None = None,
        mood_delta: Any = None,
        persona: str | None = None,
    ) -> int:
        summary = " ".join(str(summary).split()).strip()
        if not summary:
            raise ValueError("事件内容不能为空")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (at, user_id, kind, summary, emotion, salience, source,"
                " source_msg_id, mood_delta, persona) VALUES (?,?,?,?,?,?,?,?,?,COALESCE(?, 'shenzhiwei'))",
                (
                    int(time.time()),
                    user_id,
                    kind,
                    summary[:1200],
                    emotion,
                    float(salience),
                    source,
                    source_msg_id,
                    _json(mood_delta) if mood_delta is not None else None,
                    persona,
                ),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def events(
        self,
        *,
        user_id: int | None = None,
        limit: int = 20,
        kind: str | None = None,
        since: int | None = None,
        persona: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = ["1=1"]
        args: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            args.append(user_id)
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        if since:
            clauses.append("at >= ?")
            args.append(int(since))
        if persona:
            clauses.append("persona = ?")
            args.append(persona)
        sql = f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def delete_event(self, event_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM events WHERE id = ?", (int(event_id),))
            self._db.commit()
            return cur.rowcount > 0

    # ---------- 自我叙事 ----------

    def add_life(
        self,
        content: str,
        *,
        day: str,
        kind: str = "event",
        mood: str | None = None,
        tags: list[str] | None = None,
        persona: str | None = None,
    ) -> int:
        content = " ".join(str(content).split()).strip()
        if not content:
            raise ValueError("生活线内容不能为空")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO life_log (at, day, kind, content, mood, tags, persona)"
                " VALUES (?,?,?,?,?,?,COALESCE(?, 'shenzhiwei'))",
                (
                    int(time.time()),
                    day,
                    kind,
                    content[:1200],
                    mood,
                    _json(tags or []),
                    persona,
                ),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def life_log(
        self,
        *,
        day: str | None = None,
        kind: str | None = None,
        limit: int = 20,
        persona: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = ["1=1"]
        args: list[Any] = []
        if day:
            clauses.append("day = ?")
            args.append(day)
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        if persona:
            clauses.append("persona = ?")
            args.append(persona)
        sql = f"SELECT * FROM life_log WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = [dict(row) for row in rows]
        for item in out:
            if item.get("tags"):
                try:
                    item["tags"] = json.loads(item["tags"])
                except Exception:
                    pass
        out.reverse()
        return out

    def latest_life_of_kind(
        self, day: str, kind: str, *, persona: str | None = None
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM life_log WHERE day = ? AND kind = ?"
        args: list[Any] = [day, kind]
        if persona:
            sql += " AND persona = ?"
            args.append(persona)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def upsert_day_summary(
        self,
        day: str,
        summary: str,
        *,
        mood: str | None = None,
        highlights: Any = None,
        persona: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO day_summary (day, at, summary, mood, highlights, persona)"
                " VALUES (?,?,?,?,?,COALESCE(?, 'shenzhiwei'))"
                " ON CONFLICT(day) DO UPDATE SET at=excluded.at, summary=excluded.summary,"
                " mood=excluded.mood, highlights=excluded.highlights, persona=excluded.persona",
                (day, int(time.time()), summary, mood, _json(highlights or []), persona),
            )
            self._db.commit()

    def day_summaries(self, limit: int = 30, *, persona: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 365))
        sql = "SELECT * FROM day_summary"
        args: list[Any] = []
        if persona:
            sql += " WHERE persona = ?"
            args.append(persona)
        sql += " ORDER BY day DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def get_day_summary(self, day: str, *, persona: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM day_summary WHERE day = ?"
        args: list[Any] = [day]
        if persona:
            sql += " AND persona = ?"
            args.append(persona)
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return dict(row) if row else None

    # ---------- 统计 ----------

    def add_image(
        self,
        *,
        digest: str,
        path: str,
        kind: str,
        summary: str,
        sender_id: int | None = None,
        message_id: int | None = None,
        version: int = 0,
    ) -> int:
        now = int(time.time())
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO images (at, digest, path, kind, summary, sender_id, message_id, summary_version) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(digest) DO UPDATE SET summary = excluded.summary, path = excluded.path, "
                "summary_version = excluded.summary_version",
                (now, digest, path, kind, summary, sender_id, message_id, int(version)),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def image_by_digest(self, digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM images WHERE digest = ?", (digest,)).fetchone()
        return dict(row) if row else None

    def images(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM images ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 她收藏的表情包 ----------
    def add_sticker(
        self,
        *,
        digest: str,
        path: str,
        summary: str,
        tags: str = "",
        note: str = "",
        rating: float = 0.0,
        sender_id: int | None = None,
        message_id: int | None = None,
        persona: str | None = None,
    ) -> int:
        now = int(time.time())
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO stickers (at, digest, path, summary, tags, note, sender_id, message_id, rating, persona) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(digest) DO UPDATE SET summary = excluded.summary, path = excluded.path, "
                "tags = excluded.tags, note = excluded.note, rating = excluded.rating",
                (
                    now,
                    digest,
                    path,
                    summary,
                    tags,
                    note,
                    sender_id,
                    message_id,
                    float(rating),
                    persona,
                ),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def sticker_by_digest(self, digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM stickers WHERE digest = ?", (digest,)).fetchone()
        return dict(row) if row else None

    def stickers(self, limit: int = 100, persona: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM stickers"
        args: list[Any] = []
        if persona is not None:
            sql += " WHERE persona IS NULL OR persona = ?"
            args.append(persona)
        sql += " ORDER BY rating DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, tuple(args)).fetchall()
        return [dict(row) for row in rows]

    def touch_sticker(self, sticker_id: int) -> None:
        now = int(time.time())
        with self._lock:
            self._db.execute(
                "UPDATE stickers SET used_count = used_count + 1, last_used_at = ? WHERE id = ?",
                (now, int(sticker_id)),
            )
            self._db.commit()

    def delete_sticker(self, sticker_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM stickers WHERE id = ?", (int(sticker_id),))
            self._db.commit()
            return cur.rowcount > 0

    def sticker_stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(used_count), 0) AS used, "
                "MAX(last_used_at) AS last_used_at FROM stickers"
            ).fetchone()
        return dict(row) if row else {"total": 0, "used": 0, "last_used_at": None}

    # ---------- 空间动态（她刷到的东西） ----------
    def add_feed(
        self,
        *,
        tid: str,
        uin: int | None = None,
        nickname: str = "",
        content: str = "",
        created_time: int | None = None,
        comment_count: int = 0,
        like_count: int = 0,
        pic_count: int = 0,
        persona: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO qzone_feeds"
                " (at, tid, uin, nickname, content, created_time, comment_count, like_count, pic_count, persona)"
                " VALUES (?,?,?,?,?,?,?,?,?,COALESCE(?, 'shenzhiwei'))",
                (
                    int(time.time()),
                    tid,
                    uin,
                    (nickname or "")[:40],
                    (content or "")[:500],
                    created_time,
                    int(comment_count or 0),
                    int(like_count or 0),
                    int(pic_count or 0),
                    persona,
                ),
            )
            self._db.commit()
            if cur.rowcount:
                return int(cur.lastrowid or 0)
            row = self._db.execute("SELECT id FROM qzone_feeds WHERE tid = ?", (tid,)).fetchone()
            return int(row["id"]) if row else 0

    def set_verdict(
        self,
        tid: str,
        *,
        interest: float,
        like: bool,
        comment_text: str = "",
        reason: str = "",
    ) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE qzone_feeds SET interest = ?, verdict_reason = ?, verdict_at = ? WHERE tid = ?",
                (float(interest), (reason or "")[:200], int(time.time()), tid),
            )
            if comment_text:
                self._db.execute(
                    "UPDATE qzone_feeds SET comment_text = ? WHERE tid = ?",
                    (comment_text[:300], tid),
                )
            self._db.commit()

    def mark_feed(self, tid: str, *, liked: bool | None = None, comment_text: str | None = None) -> None:
        sets: list[str] = []
        args: list[Any] = []
        if liked is not None:
            sets.append("liked = ?")
            args.append(1 if liked else 0)
        if comment_text is not None:
            sets.append("comment_text = ?")
            args.append(comment_text[:300])
        if not sets:
            return
        sets.append("interacted_at = ?")
        args.append(int(time.time()))
        args.append(tid)
        with self._lock:
            self._db.execute(f"UPDATE qzone_feeds SET {', '.join(sets)} WHERE tid = ?", args)
            self._db.commit()

    def feed_by_tid(self, tid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM qzone_feeds WHERE tid = ?", (tid,)).fetchone()
        return dict(row) if row else None

    def feeds(self, limit: int = 20, persona: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        sql = "SELECT * FROM qzone_feeds"
        args: list[Any] = []
        if persona:
            sql += " WHERE persona = ?"
            args.append(persona)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def upsert_day_note(
        self,
        day: str,
        user_id: int,
        summary: str,
        *,
        mood: str = "",
        highlights: Any = None,
        persona: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO day_notes (at, day, user_id, summary, mood, highlights, persona)"
                " VALUES (?,?,?,?,?,?,COALESCE(?, 'shenzhiwei'))"
                " ON CONFLICT(day, user_id) DO UPDATE SET"
                " at = excluded.at, summary = excluded.summary,"
                " mood = excluded.mood, highlights = excluded.highlights",
                (
                    int(time.time()),
                    day,
                    int(user_id),
                    (summary or "")[:2000],
                    (mood or "")[:20],
                    _json(highlights or []),
                    persona,
                ),
            )
            self._db.commit()

    # ------------------------------------------------------------ 性格年轮
    def add_trait_mark(
        self,
        *,
        persona: str | None,
        kind: str,
        source: str,
        note: str,
        dims: dict[str, float],
        before: dict[str, float],
        after: dict[str, float],
    ) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO trait_marks (at, persona, kind, source, note, dims, before, after)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    int(time.time()),
                    persona or "xiaojingyu",
                    kind,
                    source,
                    note,
                    json.dumps(dims, ensure_ascii=False),
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def trait_marks(self, *, limit: int = 20, persona: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        sql = "SELECT * FROM trait_marks"
        args: list[Any] = []
        if persona:
            sql += " WHERE persona = ?"
            args.append(persona)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ("dims", "before", "after"):
                try:
                    item[key] = json.loads(item.get(key) or "{}")
                except Exception:
                    item[key] = {}
            out.append(item)
        return out

    def get_trait_mark(self, mark_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM trait_marks WHERE id = ?", (int(mark_id),)).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("dims", "before", "after"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except Exception:
                item[key] = {}
        return item

    def delete_trait_mark(self, mark_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM trait_marks WHERE id = ?", (int(mark_id),))
            return cur.rowcount > 0

    def upsert_day_memo(
        self,
        user_id: int,
        memo: str,
        *,
        day: str,
        last_msg_id: int = 0,
        msg_count: int = 0,
        persona: str | None = None,
    ) -> None:
        """当天聊天备忘：增量滚动更新（同一天同一个人只有一条）。"""
        with self._lock:
            self._db.execute(
                "INSERT INTO day_memo (day, user_id, persona, memo, last_msg_id, msg_count, updated_at)"
                " VALUES (?,?,COALESCE(?, 'shenzhiwei'),?,?,?,?)"
                " ON CONFLICT(day, user_id, persona) DO UPDATE SET"
                " memo = excluded.memo,"
                " last_msg_id = excluded.last_msg_id,"
                " msg_count = excluded.msg_count,"
                " updated_at = excluded.updated_at",
                (day, int(user_id), persona, memo, int(last_msg_id), int(msg_count), time.time()),
            )

    def day_memo(
        self,
        user_id: int,
        *,
        day: str | None = None,
        persona: str | None = None,
    ) -> dict[str, Any] | None:
        day = day or time.strftime("%Y-%m-%d")
        sql = "SELECT * FROM day_memo WHERE day = ? AND user_id = ?"
        args: list[Any] = [day, int(user_id)]
        if persona:
            sql += " AND persona = ?"
            args.append(persona)
        sql += " LIMIT 1"
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def day_notes(self, user_id: int, *, limit: int = 3, persona: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 30))
        sql = "SELECT * FROM day_notes WHERE user_id = ?"
        args: list[Any] = [int(user_id)]
        if persona:
            sql += " AND persona = ?"
            args.append(persona)
        sql += " ORDER BY day DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def active_user_ids(self, day: str) -> list[int]:
        """那一天聊过天的 QQ 号（给日结按人分开用）。"""
        since = int(datetime.strptime(day, "%Y-%m-%d").timestamp())
        until = since + 86400
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT user_id FROM messages WHERE time >= ? AND time < ?"
                " AND user_id IS NOT NULL AND user_id >= 1000000",
                (since, until),
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            inbound = self._db.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE direction='in'"
            ).fetchone()["c"]
            outbound = total - inbound
            audits = self._db.execute("SELECT COUNT(*) AS c FROM audit").fetchone()["c"]
            memories = self._db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
            events = self._db.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            lives = self._db.execute("SELECT COUNT(*) AS c FROM life_log").fetchone()["c"]
            days = self._db.execute("SELECT COUNT(*) AS c FROM day_summary").fetchone()["c"]
        return {
            "messages": total,
            "inbound": inbound,
            "outbound": outbound,
            "audits": audits,
            "memories": memories,
            "events": events,
            "life": lives,
            "days": days,
        }
