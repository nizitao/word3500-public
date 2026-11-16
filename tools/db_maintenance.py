#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""数据库运维：在线备份 + 明细归档 + WAL checkpoint + 空间回收。

为什么需要它
------------
此前只有若干个手工 `.bak` 文件，全靠人记得；而 `access_log`（每次请求一行）与
`study_events`（每次答题一行）只增不删，长期运行后表会持续膨胀，
`words.db-wal` 也不会自己回收。一次误操作就只能靠运气。

本工具做三件事，全部可重复执行（幂等）：

1. **备份**：用 SQLite 官方在线备份 API（`Connection.backup`）生成一致性快照，
   服务运行中也能用；不要用 `cp`（WAL 模式下会拷到半个事务）。
2. **归档**：把超过保留期的明细搬到 `*_archive` 表，主表只留热数据。
   `study_events` 默认保留 90 天，`access_log` 默认保留 90 天。
   归档只搬不删逻辑：先 `INSERT OR IGNORE` 再 `DELETE`，重复执行不会丢行。
3. **回收**：`PRAGMA wal_checkpoint(TRUNCATE)` 把 WAL 收缩回主库；
   可选 `VACUUM` 真正释放文件空间（需要额外磁盘，默认开启）。

用法
----
    python -m tools.db_maintenance                 # 备份 + 归档 + 回收
    python -m tools.db_maintenance --dry-run       # 只看会搬多少行
    python -m tools.db_maintenance --backup-only   # 只备份（最安全的日常钩子）
    python -m tools.db_maintenance --days 180 --keep 30
    python -m tools.db_maintenance --data-dir /root/word3500/data

建议：systemd timer 每天 03:30 跑一次（见 deploy/systemd/），
或 cron: `30 3 * * * cd /root/word3500 && python -m tools.db_maintenance`。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# (数据库文件名, 主表, 归档表, 时间列, 时间格式)
ARCHIVE_JOBS: tuple[tuple[str, str, str, str], ...] = (
    ("words.db", "study_events", "study_events_archive", "day"),
    ("users.db", "access_log", "access_log_archive", "ts"),
)

DEFAULT_KEEP_BACKUPS = 14
DEFAULT_RETENTION_DAYS = 90


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.isolation_level = None          # 显式控制事务
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def backup_database(src: Path, backup_dir: Path, keep: int = DEFAULT_KEEP_BACKUPS,
                    now: Optional[datetime] = None) -> Optional[Path]:
    """在线一致性备份；返回备份文件路径（源库不存在时返回 None）。"""
    if not src.is_file():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d%H%M%S")
    stem = src.stem
    target = backup_dir / f"{stem}-{stamp}.db"
    tmp = backup_dir / f".{stem}-{stamp}.part"
    if tmp.exists():
        tmp.unlink()
    # 源库只读打开：备份期间不会写生产库；WAL 中未 checkpoint 的内容也会被包含
    source = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    dest = sqlite3.connect(str(tmp))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    os.replace(tmp, target)
    prune_backups(backup_dir, stem, keep)
    return target


def prune_backups(backup_dir: Path, stem: str, keep: int) -> list[Path]:
    """只保留最近 keep 份备份，返回被删除的路径。"""
    if keep <= 0:
        return []
    files = sorted(backup_dir.glob(f"{stem}-*.db"))
    surplus = files[:-keep] if len(files) > keep else []
    for old in surplus:
        try:
            old.unlink()
        except OSError:
            pass
    return surplus


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------
def _ensure_archive_table(conn: sqlite3.Connection, table: str,
                          archive: str) -> None:
    """按主表结构建同名归档表（列顺序/类型自动保持一致）。"""
    if _table_exists(conn, archive) or not _table_exists(conn, table):
        return
    conn.execute(f"CREATE TABLE {archive} AS SELECT * FROM {table} WHERE 0")


def archive_table(conn: sqlite3.Connection, table: str, archive: str,
                  time_col: str, cutoff: str, dry_run: bool = False) -> dict[str, int]:
    """把 `time_col < cutoff` 的行搬进归档表；返回 {moved, pending}。"""
    if not _table_exists(conn, table):
        return {"moved": 0, "pending": 0}
    # `day` 列只存 YYYY-MM-DD：用日期粒度比较，避免把「今天」整天的行算进去
    bound = cutoff[:10] if time_col == "day" else cutoff
    pending = int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {time_col} < ?", (bound,)
    ).fetchone()[0] or 0)
    if dry_run or pending == 0:
        return {"moved": 0, "pending": pending}
    _ensure_archive_table(conn, table, archive)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            f"INSERT OR IGNORE INTO {archive} SELECT * FROM {table} WHERE {time_col} < ?",
            (bound,),
        )
        cur = conn.execute(f"DELETE FROM {table} WHERE {time_col} < ?", (bound,))
        moved = int(cur.rowcount or 0)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"moved": moved, "pending": pending}


def archive_cutoff(days: int, now: Optional[datetime] = None) -> str:
    """保留期截止时间（UTC，与 SQLite datetime('now') 同格式）。"""
    base = (now or datetime.now(timezone.utc).replace(tzinfo=None)) - timedelta(
        days=max(1, int(days)))
    return base.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 空间回收
# ---------------------------------------------------------------------------
def checkpoint(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """WAL → 主库并截断 WAL。返回 (busy, log, checkpointed)。"""
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError:
        return (-1, -1, -1)
    values = tuple(int(x or 0) for x in (row or (0, 0, 0)))
    while len(values) < 3:
        values = values + (0,)
    return values[0], values[1], values[2]


def vacuum(conn: sqlite3.Connection) -> bool:
    """VACUUM 释放磁盘空间；失败不致命（可能磁盘不足或存在活动事务）。"""
    try:
        conn.execute("VACUUM")
        return True
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(data_dir: Path, days: int = DEFAULT_RETENTION_DAYS,
        keep: int = DEFAULT_KEEP_BACKUPS, do_backup: bool = True,
        do_archive: bool = True, do_vacuum: bool = True,
        dry_run: bool = False, now: Optional[datetime] = None) -> dict[str, Any]:
    """备份 → 归档 → checkpoint → (可选) VACUUM。返回结构化摘要。"""
    summary: dict[str, Any] = {"data_dir": str(data_dir), "dry_run": dry_run,
                              "backup": {}, "archive": {}, "checkpoint": {}}
    backup_dir = data_dir / "backups"

    if do_backup and not dry_run:
        for name in ("users.db", "words.db"):
            target = backup_database(data_dir / name, backup_dir, keep=keep, now=now)
            summary["backup"][name] = str(target) if target else None

    cutoff = archive_cutoff(days, now)
    summary["archive"]["cutoff"] = cutoff

    for db_name, table, archive, time_col in ARCHIVE_JOBS:
        path = data_dir / db_name
        if not path.is_file():
            summary["archive"][table] = {"moved": 0, "pending": 0, "skipped": True}
            continue
        conn = _connect(path)
        try:
            result = archive_table(conn, table, archive, time_col, cutoff, dry_run)
            if not dry_run and result["moved"]:
                busy, wal, done = checkpoint(conn)
                result["checkpoint"] = {"busy": busy, "wal": wal, "checkpointed": done}
            summary["archive"][table] = result
            if do_vacuum and not dry_run:
                result["vacuumed"] = vacuum(conn)
                if result["vacuumed"]:
                    summary["checkpoint"][db_name] = checkpoint(conn)
        finally:
            conn.close()

    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"[maintenance] data_dir={summary['data_dir']}"
          f"{' (dry-run)' if summary['dry_run'] else ''}")
    for name, path in summary.get("backup", {}).items():
        print(f"  backup  {name}: {path or '不存在，跳过'}")
    for table, info in summary.get("archive", {}).items():
        if table == "cutoff":
            continue
        if isinstance(info, dict):
            print(f"  archive {table}: 待归档 {info.get('pending', 0)} 行，"
                  f"已归档 {info.get('moved', 0)} 行"
                  f"{'（跳过）' if info.get('skipped') else ''}"
                  f"{'' if info.get('vacuumed') is None else '，已 VACUUM'}")
    cp = summary.get("checkpoint", {})
    for db_name, values in cp.items():
        print(f"  checkpoint {db_name}: busy={values[0]} wal={values[1]} "
              f"checkpointed={values[2]}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="word3500 数据库备份 / 明细归档 / 空间回收")
    default_data = str(Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--data-dir", default=os.environ.get("WORD3500_DATA_DIR", default_data))
    parser.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help="明细保留天数（默认 90）")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP_BACKUPS,
                        help="保留最近几份备份（默认 14）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--backup-only", action="store_true", help="只做备份")
    parser.add_argument("--no-vacuum", action="store_true", help="跳过 VACUUM")
    args = parser.parse_args(argv)

    if args.days < 1:
        parser.error("--days 必须 ≥ 1")

    summary = run(
        Path(args.data_dir), days=args.days, keep=args.keep,
        do_backup=True, do_archive=not args.backup_only,
        do_vacuum=not args.no_vacuum and not args.backup_only,
        dry_run=args.dry_run,
    )
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
