#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""运维脚本测试：备份一致性 / 份数保留 / 90 天明细归档 / WAL checkpoint。

为什么值得单独测：这个脚本是**唯一**会在生产库上 `DELETE` + `VACUUM` 的东西，
写错了就是永久丢数据。全部在临时目录里跑，不碰 data/。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import db as schema  # noqa: E402
from tools import db_maintenance as mt  # noqa: E402

ACCESS_LOG_DDL = """CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    path TEXT DEFAULT '',
    user_id INTEGER DEFAULT 0,
    username TEXT DEFAULT '',
    ip TEXT DEFAULT '',
    user_agent TEXT DEFAULT '',
    status INTEGER DEFAULT 200,
    success INTEGER DEFAULT 1
)"""


def _make_data_dir(root: Path) -> Path:
    """生成一个最小可用的 data/（words.db 走真实 schema，users.db 手工建 access_log）。"""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "words.db")
    try:
        schema.init_schema(conn)
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(root / "users.db")
    try:
        conn.execute(ACCESS_LOG_DDL)
        conn.commit()
    finally:
        conn.close()
    return root


def _insert_events(db_path: Path, days: list[str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO study_events (user_id,word_id,action,result,day,ts,pos,book,uid)"
            " VALUES (1, 1, 'quiz', 1, ?, ?, '', 1, ?)",
            [(day, day + " 00:00:01", f"u{day}-{i}") for i, day in enumerate(days)],
        )
        conn.commit()
    finally:
        conn.close()


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


class MaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="w3500_maint_"))
        self.data = _make_data_dir(self.tmp / "data")
        self.words = self.data / "words.db"
        self.users = self.data / "users.db"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 归档 ----
    def test_archive_moves_old_rows_only(self) -> None:
        _insert_events(self.words, ["2020-01-01", "2020-02-02", "2020-03-03",
                                    "2026-11-10", "2026-11-11"])

        conn = mt._connect(self.words)
        try:
            cutoff = mt.archive_cutoff(90, datetime(2026, 11, 15))
            result = mt.archive_table(conn, "study_events", "study_events_archive",
                                      "day", cutoff)
        finally:
            conn.close()

        self.assertEqual(result["moved"], 3, result)
        self.assertEqual(_count(self.words, "study_events"), 2)
        self.assertEqual(_count(self.words, "study_events_archive"), 3)

    def test_archive_is_idempotent(self) -> None:
        _insert_events(self.words, ["2020-01-01"])
        cutoff = mt.archive_cutoff(90, datetime(2026, 11, 15))
        for _ in range(2):
            conn = mt._connect(self.words)
            try:
                mt.archive_table(conn, "study_events", "study_events_archive",
                                 "day", cutoff)
            finally:
                conn.close()
        self.assertEqual(_count(self.words, "study_events"), 0)
        self.assertEqual(_count(self.words, "study_events_archive"), 1,
                         "重复归档不得复制出多行")

    def test_archive_dry_run_writes_nothing(self) -> None:
        _insert_events(self.words, ["2020-01-01"])
        conn = mt._connect(self.words)
        try:
            cutoff = mt.archive_cutoff(90, datetime(2026, 11, 15))
            result = mt.archive_table(conn, "study_events", "study_events_archive",
                                      "day", cutoff, dry_run=True)
        finally:
            conn.close()
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["moved"], 0)
        self.assertEqual(_count(self.words, "study_events"), 1)

    def test_access_log_archive(self) -> None:
        conn = sqlite3.connect(self.users)
        try:
            conn.executemany(
                "INSERT INTO access_log (ts, event_type, path) VALUES (?,?,?)",
                [("2020-01-01 10:00:00", "view", "/"),
                 ("2020-01-02 10:00:00", "operation", "/api/words"),
                 ("2026-11-14 10:00:00", "view", "/")],
            )
            conn.commit()
            cutoff = mt.archive_cutoff(90, datetime(2026, 11, 15))
            result = mt.archive_table(conn, "access_log", "access_log_archive",
                                      "ts", cutoff)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(result["moved"], 2)
        self.assertEqual(_count(self.users, "access_log"), 1)
        self.assertEqual(_count(self.users, "access_log_archive"), 2)

    # ---- 备份 ----
    def test_backup_is_readable_and_complete(self) -> None:
        _insert_events(self.words, ["2026-01-01"])
        backup_dir = self.data / "backups"
        target = mt.backup_database(self.words, backup_dir,
                                    now=datetime(2026, 11, 15, 3, 30, 0))
        self.assertIsNotNone(target)
        assert target is not None
        self.assertTrue(target.is_file())
        self.assertIn("words-20261115033000", target.name)
        self.assertEqual(_count(target, "study_events"), 1, "备份必须包含已提交数据")

    def test_backup_keeps_only_recent(self) -> None:
        backup_dir = self.data / "backups"
        backup_dir.mkdir(parents=True)
        for i in range(5):
            (backup_dir / f"words-2026010{i}000000.db").write_bytes(b"")
        mt.backup_database(self.words, backup_dir, keep=3,
                           now=datetime(2026, 11, 15, 3, 30, 0))
        files = sorted(backup_dir.glob("words-*.db"))
        self.assertEqual(len(files), 3, [f.name for f in files])
        self.assertTrue(files[-1].name.startswith("words-20261115"),
                        "最新一份必须保留")

    def test_backup_missing_source_is_noop(self) -> None:
        self.assertIsNone(mt.backup_database(self.data / "nope.db",
                                             self.data / "backups"))

    # ---- 全流程 ----
    def test_run_end_to_end(self) -> None:
        _insert_events(self.words, ["2020-01-01", "2026-11-14"])
        summary = mt.run(self.data, days=90, keep=2, do_vacuum=False,
                         now=datetime(2026, 11, 15, 3, 30, 0))
        self.assertTrue(summary["backup"]["words.db"])
        self.assertTrue(summary["backup"]["users.db"])
        self.assertEqual(summary["archive"]["study_events"]["moved"], 1)
        self.assertEqual(_count(self.words, "study_events"), 1)
        self.assertEqual(_count(self.words, "study_events_archive"), 1)

    def test_dry_run_does_not_touch_disk(self) -> None:
        _insert_events(self.words, ["2020-01-01"])
        summary = mt.run(self.data, days=90, dry_run=True,
                         now=datetime(2026, 11, 15, 3, 30, 0))
        self.assertFalse((self.data / "backups").exists())
        self.assertEqual(summary["archive"]["study_events"]["pending"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
