#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""
导入分册分单元单词表到 word3500 数据库。
新增 book（册编号）、unit（单元名）字段，
保留已有单词，仅追加新数据，不破坏现有收藏/用户。

用法：
    python import_books.py          # 导入 textbook 单词并更新 DB 结构
    python import_books.py --dry    # 预览，不实际写入
"""
import os
import re
import sys
import sqlite3
from typing import Any, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "data"))
WORDS_DB_PATH = os.path.join(DATA_DIR, "words.db")
WORDS_DIR = os.path.join(DATA_DIR, "english_done")

# 目录名 → 册编号/显示名
BOOK_MAP = {
    "b1":  (1,  "必修第一册"),
    "b2":  (2,  "必修第二册"),
    "b3":  (3,  "必修第三册"),
    "xb1": (4,  "选择性必修第一册"),
    "xb2": (5,  "选择性必修第二册"),
    "xb3": (6,  "选择性必修第三册"),
    "xb4": (7,  "选择性必修第四册"),
}


def extract_unit(filename: str) -> str:
    """从文件名提取单元名，如 '1普通高中教科书·英语必修 第一册 单词表 Unit 1.txt' → 'Unit 1'"""
    # 去掉扩展名
    name = os.path.splitext(filename)[0]
    # 提取 "单词表 " 之后的部分
    m = re.search(r'单词表\s+(.+)', name)
    if m:
        return m.group(1).strip()
    return name


def find_words_dir() -> Optional[str]:
    """定位单词目录，支持多种可能路径"""
    for p in [WORDS_DIR, os.path.join(BASE_DIR, "english_done")]:
        if os.path.isdir(p):
            return p
    return None


def scan_files(words_dir: str) -> list[tuple[int, str, str]]:
    """扫描所有 unit 文件，返回 [(book, unit, filepath), ...]"""
    files: list[tuple[int, str, str]] = []
    for entry in sorted(os.listdir(words_dir)):
        child = os.path.join(words_dir, entry)
        if not os.path.isdir(child):
            continue
        map_entry = BOOK_MAP.get(entry)
        if not map_entry:
            continue
        book_num, _ = map_entry
        for fname in sorted(os.listdir(child)):
            if not fname.endswith(".txt"):
                continue
            unit = extract_unit(fname)
            files.append((book_num, unit, os.path.join(child, fname)))
    return files


def parse_words(filepath: str) -> list[tuple[str, str, str]]:
    """解析 3 行一组的单词文件，返回 [(word, pronunciation, meaning), ...]"""
    results: list[tuple[str, str, str]] = []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        lines = [ln.rstrip("\n\r") for ln in f]
    i, n = 0, len(lines)
    while i + 2 < n:
        w = lines[i].strip()
        p = lines[i + 1].strip()
        m = lines[i + 2].strip()
        if w == "" and p == "" and m == "":
            i += 3
            continue
        results.append((w, p, m))
        i += 3
    return results


def ensure_columns(cur: Any) -> None:
    """确保 words 表有 book / unit 列"""
    cur.execute("PRAGMA table_info(words)")
    cols = {row[1] for row in cur.fetchall()}
    for col, col_def in [("book", "INTEGER DEFAULT NULL"),
                         ("unit", "TEXT DEFAULT NULL")]:
        if col not in cols:
            cur.execute(f"ALTER TABLE words ADD COLUMN {col} {col_def}")
            print(f"  [schema] 添加列 {col}")


def import_all(dry_run: bool = False) -> bool:
    """主导入逻辑"""
    words_dir = find_words_dir()
    if not words_dir:
        print("[ERROR] 未找到 english_done/ 目录")
        return False

    files = scan_files(words_dir)
    if not files:
        print("[ERROR] 未找到任何单词文件")
        return False

    print(f"在 {words_dir} 找到 {len(files)} 个单词文件\n")

    conn = sqlite3.connect(WORDS_DB_PATH)
    cur = conn.cursor()

    # 确保列存在
    ensure_columns(cur)
    conn.commit()

    total_inserted = 0
    total_updated = 0

    for book_num, unit, fpath in files:
        entries = parse_words(fpath)
        if not entries:
            print(f"  [SKIP] {os.path.basename(fpath)} 空文件，跳过")
            continue

        if dry_run:
            print(f"  [DRY] {os.path.basename(fpath)} -> {entries[0][0]}... ({len(entries)} 词)")
            total_inserted += len(entries)
            continue

        for w, p, m in entries:
            # 查是否已存在该（word, book, unit）组合
            cur.execute(
                "SELECT id FROM words WHERE word=? AND book=? AND unit=?",
                (w, book_num, unit),
            )
            if cur.fetchone():
                # 已存在相同组合，跳过（幂等）
                continue

            # 看是否有同 word 但不同 book/unit 的现有记录
            cur.execute(
                "SELECT id FROM words WHERE word=? AND book IS NULL AND unit IS NULL",
                (w,),
            )
            row = cur.fetchone()
            if row:
                # 更新现有记录的 book/unit（避免数据分裂）
                cur.execute(
                    "UPDATE words SET book=?, unit=? WHERE id=?",
                    (book_num, unit, row[0]),
                )
                total_updated += 1
            else:
                # 全新插入（其他册/单元已有则 INSERT 新行）
                cur.execute(
                    "INSERT INTO words (word, pronunciation, meaning, book, unit) VALUES (?,?,?,?,?)",
                    (w, p, m, book_num, unit),
                )
                total_inserted += 1

        cur_count = cur.execute(
            "SELECT COUNT(*) FROM words WHERE book=? AND unit=?",
            (book_num, unit),
        ).fetchone()[0]
        print(f"  [OK] Book {book_num} {unit} -- {cur_count} 词")

    conn.commit()
    conn.close()

    if not dry_run:
        # 重新统计全部
        conn2 = sqlite3.connect(WORDS_DB_PATH)
        total_words = conn2.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        with_book = conn2.execute(
            "SELECT COUNT(*) FROM words WHERE book IS NOT NULL"
        ).fetchone()[0]
        conn2.close()
        print(f"\n{'='*50}")
        print("导入完成:")
        print(f"  新增插入: {total_inserted} 词")
        print(f"  更新标记: {total_updated} 词")
        print(f"  数据库总计: {total_words} 词")
        print(f"  已标记册/单元: {with_book} 词")
        print(f"{'='*50}")
    else:
        print(f"\n[DRY-RUN] 预览模式：将导入约 {total_inserted} 词")
    return True


if __name__ == "__main__":
    dry_run = "--dry" in sys.argv
    import_all(dry_run=dry_run)
