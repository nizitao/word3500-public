#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""
将 word3500.db 拆分为 users.db + words.db
- users.db: users, sessions, auth_events, _config
- words.db: words, collections, collection_words, user_stars

用法：
    python migrate_split_db.py            # 先备份，再拆分
    python migrate_split_db.py --dry      # 预览，不实际写入
"""
import os
import sys
import shutil
import sqlite3
from typing import Any, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "data"))
OLD_DB = os.path.join(DATA_DIR, "word3500.db")
USERS_DB = os.path.join(DATA_DIR, "users.db")
WORDS_DB = os.path.join(DATA_DIR, "words.db")
BACKUP_DB = os.path.join(DATA_DIR, "word3500.db.bak")

# 用户库需迁移的表
USER_TABLES = ["users", "sessions", "auth_events", "_config"]
# 单词库需迁移的表
WORD_TABLES = ["words", "collections", "collection_words", "user_stars"]


def get_schema(cur: Any, table: str) -> Optional[str]:
    """获取建表语句"""
    cur.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
    row = cur.fetchone()
    return row[0] if row else None


def copy_table(src_cur: Any, dst_cur: Any, table: str) -> int:
    """复制表结构和数据"""
    # 获取 schema
    schema = get_schema(src_cur, table)
    if not schema:
        print(f"  [SKIP] {table}: 表不存在")
        return 0
    # 建表
    dst_cur.execute(schema)
    # 获取数据
    src_cur.execute(f"SELECT * FROM {table}")
    rows = src_cur.fetchall()
    if not rows:
        dst_cur.connection.commit()
        return 0
    # 获取列名
    col_names = [desc[0] for desc in src_cur.description]
    placeholders = ",".join(["?"] * len(col_names))
    cols = ",".join(col_names)
    dst_cur.executemany(
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", rows
    )
    dst_cur.connection.commit()
    return len(rows)


def migrate(dry_run: bool = False) -> bool:
    if not os.path.isfile(OLD_DB):
        print(f"[ERROR] 未找到 {OLD_DB}")
        return False

    # 统计各表行数
    conn_old = sqlite3.connect(OLD_DB)
    cur_old = conn_old.cursor()
    stats = {}
    for table in USER_TABLES + WORD_TABLES:
        try:
            cur_old.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cur_old.fetchone()[0]
        except sqlite3.OperationalError:
            stats[table] = 0
    conn_old.close()

    print("当前 word3500.db 统计:")
    for t in USER_TABLES:
        print(f"  {t}: {stats[t]} 行")
    print("  ---")
    for t in WORD_TABLES:
        print(f"  {t}: {stats[t]} 行")

    if dry_run:
        print("\n[DRY-RUN] 预览完成，未作任何更改")
        return True

    # 备份原库
    if os.path.isfile(BACKUP_DB):
        os.remove(BACKUP_DB)
    shutil.copy2(OLD_DB, BACKUP_DB)
    print(f"\n已备份: {OLD_DB} -> {BACKUP_DB}")

    # 打开源库
    conn_old = sqlite3.connect(OLD_DB)
    cur_old = conn_old.cursor()

    # 创建 users.db
    conn_users = sqlite3.connect(USERS_DB)
    cur_users = conn_users.cursor()
    print(f"\n创建 {USERS_DB}")
    for table in USER_TABLES:
        if stats.get(table, 0) == 0:
            # 表不存在或为空，只建表结构
            schema = get_schema(cur_old, table)
            if schema:
                cur_users.execute(schema)
                conn_users.commit()
                print(f"  [EMPTY] {table}: 仅建表")
            else:
                print(f"  [SKIP] {table}: 源库无此表")
        else:
            n = copy_table(cur_old, cur_users, table)
            print(f"  [OK] {table}: {n} 行")
    conn_users.close()

    # 创建 words.db（不含 _config）
    conn_words = sqlite3.connect(WORDS_DB)
    cur_words = conn_words.cursor()
    print(f"\n创建 {WORDS_DB}")
    for table in WORD_TABLES:
        if stats.get(table, 0) == 0:
            schema = get_schema(cur_old, table)
            if schema:
                cur_words.execute(schema)
                conn_words.commit()
                print(f"  [EMPTY] {table}: 仅建表")
            else:
                print(f"  [SKIP] {table}: 源库无此表")
        else:
            n = copy_table(cur_old, cur_words, table)
            print(f"  [OK] {table}: {n} 行")
    conn_words.close()

    # 为 words.db 补充 book/unit 列（若 3500.txt 或 english_done 数据已导入）
    try:
        conn_w = sqlite3.connect(WORDS_DB)
        cur_w = conn_w.cursor()
        cur_w.execute("PRAGMA table_info(words)")
        cols = {r[1] for r in cur_w.fetchall()}
        for col, col_type in [("book", "INTEGER DEFAULT NULL"),
                               ("unit", "TEXT DEFAULT NULL")]:
            if col not in cols:
                cur_w.execute(f"ALTER TABLE words ADD COLUMN {col} {col_type}")
                conn_w.commit()
                print(f"  [SCHEMA] words 表添加列 {col}")
        conn_w.close()
    except Exception as e:
        print(f"  [INFO] 补充列跳过: {e}")

    conn_old.close()

    print(f"\n{'='*50}")
    print("拆分完成!")
    print(f"  {OLD_DB}  -> 已备份为 {BACKUP_DB}")
    print(f"  {USERS_DB}  -> 用户数据 ({sum(stats.get(t,0) for t in USER_TABLES)} 行)")
    print(f"  {WORDS_DB}  -> 单词数据 ({sum(stats.get(t,0) for t in WORD_TABLES)} 行)")
    print(f"{'='*50}")
    print("\n提示: 确认无误后可删除 word3500.db 和 word3500.db.bak")
    return True


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    migrate(dry_run=dry)
