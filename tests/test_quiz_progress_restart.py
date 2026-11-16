#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""回归测试：单元背诵断点（/api/quiz/progress）在服务进程重启后仍可读取。

零第三方依赖。使用 tests/support.py 的沙箱：临时目录里复制 api/ 并自建
users.db / words.db，起停真实的 HTTP 服务进程，不读写项目生产库、不占用固定端口，
也不再依赖「本机已有 admin 账号」这种隐含前提（那是旧版只能在开发者机器上跑的根源）。

运行：
    python -m unittest discover -s tests -v
    python tests/test_quiz_progress_restart.py
"""
from __future__ import annotations

import json
import unittest

from support import BaseCase

PAYLOAD = {
    "book": "3",
    "unit": "Unit 1",
    "queue": [2099, 1757, 138, 5055],
    "quiz_pos": 2,
    "word_index": 3,
    "stats": {"answered": 3, "correct": 2},
}


class QuizProgressRestartTest(BaseCase):
    """验证「重启后断点仍在」：接口层 + SQLite 文件层各断言一次。"""

    def test_progress_survives_process_restart(self) -> None:
        name, token = self.box.new_user("quiz")
        status, saved = self.box.post("/api/quiz/progress", PAYLOAD, token=token)
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved.get("ok"), saved)

        status, before = self.box.get("/api/quiz/progress", token=token)
        self.assertEqual(status, 200, before)
        self.assertTrue(before.get("has"), before)
        self.assertEqual(before["progress"]["book"], PAYLOAD["book"])
        self.assertEqual(before["progress"]["unit"], PAYLOAD["unit"])
        self.assertEqual(before["progress"]["queue"], PAYLOAD["queue"])
        self.assertEqual(before["progress"]["quiz_pos"], PAYLOAD["quiz_pos"])
        self.assertEqual(before["progress"]["word_index"], PAYLOAD["word_index"])
        self.assertEqual(before["progress"]["stats"], PAYLOAD["stats"])

        # 模拟后端重启：杀进程 → 用同一份 data/ 重新拉起（init_db 会再跑一遍）
        self.box.restart()

        status, after = self.box.get("/api/quiz/progress", token=token)
        self.assertEqual(status, 200, after)
        self.assertTrue(after.get("has"), after)
        self.assertEqual(after["progress"], before["progress"])

        # 文件层：数据确实落在 SQLite 文件里，而非进程内存
        uid = self.box.user_id(name)
        rows = self.box.query(
            "SELECT book, unit, queue_json, quiz_pos, word_index, stats_json "
            "FROM quiz_progress WHERE user_id=?", (uid,))
        self.assertEqual(len(rows), 1, "quiz_progress 表缺少断点行")
        row = rows[0]
        self.assertEqual(row["book"], PAYLOAD["book"])
        self.assertEqual(row["unit"], PAYLOAD["unit"])
        self.assertEqual(json.loads(row["queue_json"]), PAYLOAD["queue"])
        self.assertEqual(row["quiz_pos"], PAYLOAD["quiz_pos"])
        self.assertEqual(row["word_index"], PAYLOAD["word_index"])
        self.assertEqual(json.loads(row["stats_json"]), PAYLOAD["stats"])

    def test_init_db_is_idempotent(self) -> None:
        """反复启动执行 init_db() 不会重建/清空 quiz_progress。"""
        self.box.exec_sql(
            "INSERT OR REPLACE INTO quiz_progress(user_id,book,unit) VALUES(999,'1','Unit 1')")
        out = self.box.run_python(
            "from api import server\n"
            "server.init_db()\n"
            "server.init_db()\n"
            "from api import db\n"
            "conn = db.connect()\n"
            "print(conn.execute('SELECT COUNT(*) FROM quiz_progress WHERE user_id=999')"
            ".fetchone()[0])\n"
            "conn.close()\n"
        )
        self.assertEqual(out.strip().splitlines()[-1], "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
