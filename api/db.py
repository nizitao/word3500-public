#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""数据库连接与 schema 的**唯一定义处**（零第三方依赖）。

历史问题：`learn_progress` 的建表语句在 `server.py` 与 `study.py` 各写了一份，
`BOOK_NAMES` 在 `server.py` / `study.py` / `together.py` / `web/js/quiz.js` 重复了四份，
改一处漏一处就会造成口径漂移（例如 study 里补了 `next_review_at` 列，server 那份没有）。

现在：所有 words.db 的表、列、索引都只在 `SCHEMA_TABLES` / `SCHEMA_INDEXES` /
`COLUMN_MIGRATIONS` 里定义一次，`init_schema()` 幂等执行，任何模块都调用它。

`users.db` 的表结构仍归 `api/auth.py` 所有（认证模块自管理），这里只提供路径。
"""
import os
import sqlite3
from typing import Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
WORDS_DB_PATH = os.path.join(DATA_DIR, "words.db")
USERS_DB_PATH = os.path.join(DATA_DIR, "users.db")

# 词表分页大小（浏览接口与「按 id 定位页码」共用同一口径）
PAGE_SIZE = 50

# ---------------------------------------------------------------------------
# 教材分册口径（前端 web/js/quiz.js 的 BOOK_NAMES 需与这里保持一致）
# ---------------------------------------------------------------------------
BOOK_NAMES: dict[int, str] = {
    1: "必修第一册", 2: "必修第二册", 3: "必修第三册",
    4: "选择性必修第一册", 5: "选择性必修第二册",
    6: "选择性必修第三册", 7: "选择性必修第四册",
}

# 导入分册目录名 → 册号（data/english_done/b1、xb1 ...）
BOOK_CODES: dict[str, int] = {
    "b1": 1, "b2": 2, "b3": 3,
    "xb1": 4, "xb2": 5, "xb3": 6, "xb4": 7,
}


def book_name(book: Any, fallback: str = "") -> str:
    """册号 → 展示名；未知册号回退到「第 N 册」。"""
    try:
        num = int(book)
    except (TypeError, ValueError):
        return fallback
    return BOOK_NAMES.get(num, fallback or f"第{num}册")


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def connect(readonly: bool = False, path: str = "") -> sqlite3.Connection:
    """打开 words.db 连接。只读连接用 URI mode=ro，避免误写。"""
    target = path or WORDS_DB_PATH
    if readonly and os.path.isfile(target):
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.OperationalError:
        pass
    return conn


def write_conn(path: str = "") -> sqlite3.Connection:
    """写连接：关闭隐式事务，由调用方显式 BEGIN IMMEDIATE 控制边界。"""
    conn = connect(path=path)
    conn.isolation_level = None
    return conn


# ---------------------------------------------------------------------------
# schema（唯一定义处）
# ---------------------------------------------------------------------------
SCHEMA_TABLES = (
    """CREATE TABLE IF NOT EXISTS words (
        id INTEGER PRIMARY KEY,
        word TEXT,
        pronunciation TEXT,
        meaning TEXT,
        star INTEGER DEFAULT 0,
        book INTEGER DEFAULT NULL,
        unit TEXT DEFAULT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS collection_words (
        collection_id INTEGER NOT NULL,
        word_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (collection_id, word_id)
    )""",
    """CREATE TABLE IF NOT EXISTS user_stars (
        user_id INTEGER NOT NULL,
        word_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, word_id)
    )""",
    """CREATE TABLE IF NOT EXISTS learn_progress (
        user_id INTEGER NOT NULL,
        word_id INTEGER NOT NULL,
        seen INTEGER DEFAULT 0,
        correct INTEGER DEFAULT 0,
        master INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, word_id)
    )""",
    """CREATE TABLE IF NOT EXISTS quiz_progress (
        user_id INTEGER PRIMARY KEY, book TEXT, unit TEXT, queue_json TEXT,
        quiz_pos INTEGER DEFAULT 0, word_index INTEGER DEFAULT 0,
        stats_json TEXT, updated_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS study_events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id  INTEGER NOT NULL,
        word_id  INTEGER NOT NULL,
        action   TEXT    NOT NULL,
        result   INTEGER DEFAULT 0,
        day      TEXT    NOT NULL,
        ts       TEXT    NOT NULL,
        pos      TEXT    DEFAULT '',
        book     INTEGER DEFAULT NULL,
        uid      TEXT    NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS daily_stats (
        user_id        INTEGER NOT NULL,
        day            TEXT    NOT NULL,
        new_words      INTEGER DEFAULT 0,
        reviewed       INTEGER DEFAULT 0,
        answered       INTEGER DEFAULT 0,
        correct        INTEGER DEFAULT 0,
        learned_total  INTEGER DEFAULT 0,
        mastered_total INTEGER DEFAULT 0,
        checked_in     INTEGER DEFAULT 0,
        makeup         INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, day)
    )""",
    """CREATE TABLE IF NOT EXISTS user_badges (
        user_id     INTEGER NOT NULL,
        badge_id    TEXT    NOT NULL,
        unlocked_at TEXT    DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, badge_id)
    )""",
    """CREATE TABLE IF NOT EXISTS points_ledger (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        delta   INTEGER NOT NULL,
        reason  TEXT,
        day     TEXT,
        ts      TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS study_rooms (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        code          TEXT    NOT NULL UNIQUE,
        name          TEXT    NOT NULL,
        owner_user_id INTEGER NOT NULL,
        scope_book    INTEGER DEFAULT NULL,
        scope_unit    TEXT    DEFAULT NULL,
        max_members   INTEGER DEFAULT 30,
        created_at    TEXT    DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS room_members (
        room_id   INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        joined_at TEXT    DEFAULT (datetime('now')),
        PRIMARY KEY (room_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS room_likes (
        room_id      INTEGER NOT NULL,
        from_user_id INTEGER NOT NULL,
        to_user_id   INTEGER NOT NULL,
        day          TEXT    NOT NULL,
        ts           TEXT    DEFAULT (datetime('now')),
        PRIMARY KEY (room_id, from_user_id, to_user_id, day)
    )""",
    # 归档表：维护脚本把超过保留期的明细搬到这里，主表保持精简
    """CREATE TABLE IF NOT EXISTS study_events_archive (
        id       INTEGER PRIMARY KEY,
        user_id  INTEGER NOT NULL,
        word_id  INTEGER NOT NULL,
        action   TEXT    NOT NULL,
        result   INTEGER DEFAULT 0,
        day      TEXT    NOT NULL,
        ts       TEXT    NOT NULL,
        pos      TEXT    DEFAULT '',
        book     INTEGER DEFAULT NULL,
        uid      TEXT    NOT NULL
    )""",
)

SCHEMA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_words_book_unit ON words(book, unit)",
    "CREATE INDEX IF NOT EXISTS idx_words_pos ON words(pos)",
    "CREATE INDEX IF NOT EXISTS idx_cw_word ON collection_words(word_id)",
    "CREATE INDEX IF NOT EXISTS idx_coll_user ON collections(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_lp_user ON learn_progress(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_lp_review ON learn_progress(user_id, next_review_at)",
    "CREATE INDEX IF NOT EXISTS idx_se_user_day ON study_events(user_id, day)",
    "CREATE INDEX IF NOT EXISTS idx_se_user_word ON study_events(user_id, word_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_se_uid ON study_events(user_id, uid)",
    "CREATE INDEX IF NOT EXISTS idx_pl_user ON points_ledger(user_id, ts)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pl_once ON points_ledger(user_id, reason, day)",
    "CREATE INDEX IF NOT EXISTS idx_rm_user ON room_members(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_rl_to ON room_likes(room_id, to_user_id, day)",
)

# 纯新增列（老库升级用）：(表, 列, 类型定义)
COLUMN_MIGRATIONS = (
    ("words", "book", "INTEGER DEFAULT NULL"),
    ("words", "unit", "TEXT DEFAULT NULL"),
    ("words", "pos", "TEXT DEFAULT ''"),
    ("learn_progress", "next_review_at", "TEXT DEFAULT NULL"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """幂等建表 / 加列 / 建索引。可重复执行，不改变既有数据。

    传入 conn 时复用调用方连接（不负责关闭）；否则自带连接并关闭。
    """
    own = conn is None
    if own:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = connect()
    assert conn is not None
    try:
        for ddl in SCHEMA_TABLES:
            conn.execute(ddl)
        for table, column, decl in COLUMN_MIGRATIONS:
            if column not in _columns(conn, table):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                except sqlite3.OperationalError:
                    pass
        conn.commit()
        # 加列之后再建索引（idx_words_pos 依赖 words.pos）
        for ddl in SCHEMA_INDEXES:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        if own:
            conn.close()
