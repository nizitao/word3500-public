#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""自愈工具 `POST /api/admin/rebuild-progress` 的完整口径。

旧实现的缺陷：只重算 seen/correct/master，不重建 `next_review_at`、`daily_stats`
与徽章。于是「自愈」会静默丢掉复习调度、日历与打卡记录——越修越少。
本测试先人为把这三块数据打坏，再验证自愈能完整还原。
"""
from __future__ import annotations

import unittest

from support import BaseCase


class RebuildTest(BaseCase):
    def _seed_mastered(self, token: str, word_ids: list[int]) -> dict:
        events = []
        for wid in word_ids:
            for i in range(3):
                events.append({"word_id": wid, "action": "quiz", "result": 1,
                               "uid": f"rb-{wid}-{i}"})
        status, data = self.box.post("/api/study/events", {"events": events}, token=token)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["accepted"], len(events))
        return data

    def test_rebuild_restores_schedule_stats_and_badges(self) -> None:
        name, token = self.box.new_user("rb")
        uid = self.box.user_id(name)
        words = list(range(6, 16))          # 10 个词 × 3 次答对 → 触发 master_10
        seeded = self._seed_mastered(token, words)
        day = seeded["day"]

        before = self.box.query(
            "SELECT word_id, seen, correct, master, next_review_at FROM learn_progress"
            " WHERE user_id=? ORDER BY word_id", (uid,))
        self.assertEqual(len(before), 10)
        self.assertTrue(all(int(r["master"]) == 1 for r in before))
        self.assertTrue(all(r["next_review_at"] for r in before),
                        "首次写入就必须有复习调度时间")
        schedule = {int(r["word_id"]): r["next_review_at"] for r in before}

        # ---- 人为破坏：调度时间清空、master 归零、当日统计与徽章全删 ----
        self.box.exec_sql("UPDATE learn_progress SET master=0, next_review_at=NULL"
                          " WHERE user_id=?", (uid,))
        self.box.exec_sql("DELETE FROM daily_stats WHERE user_id=?", (uid,))
        self.box.exec_sql("DELETE FROM user_badges WHERE user_id=?", (uid,))

        damaged = self.box.query(
            "SELECT COUNT(*) AS c FROM daily_stats WHERE user_id=?", (uid,))[0]
        self.assertEqual(int(damaged["c"]), 0)

        # ---- 自愈 ----
        status, data = self.box.post("/api/admin/rebuild-progress", {"user_id": uid},
                                     token=self.box.token("admin"))
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["updated"], 10)
        self.assertEqual(data["users"], 1)
        self.assertEqual(data["days"], 1)

        after = self.box.query(
            "SELECT word_id, seen, correct, master, next_review_at FROM learn_progress"
            " WHERE user_id=? ORDER BY word_id", (uid,))
        self.assertEqual(len(after), 10)
        for row in after:
            wid = int(row["word_id"])
            self.assertEqual(int(row["seen"]), 3)
            self.assertEqual(int(row["correct"]), 3)
            self.assertEqual(int(row["master"]), 1, "自愈必须重算 master")
            self.assertEqual(row["next_review_at"], schedule[wid],
                             "自愈必须以事件时间为基准还原 next_review_at")

        stat = self.box.query(
            "SELECT answered, correct, new_words, learned_total, mastered_total,"
            "       checked_in FROM daily_stats WHERE user_id=? AND day=?", (uid, day))
        self.assertEqual(len(stat), 1, "自愈必须重建 daily_stats")
        self.assertEqual(int(stat[0]["answered"]), 30)
        self.assertEqual(int(stat[0]["correct"]), 30)
        self.assertEqual(int(stat[0]["new_words"]), 10)
        self.assertEqual(int(stat[0]["learned_total"]), 10)
        self.assertEqual(int(stat[0]["mastered_total"]), 10)
        self.assertEqual(int(stat[0]["checked_in"]), 1)

        badges = [r["badge_id"] for r in self.box.query(
            "SELECT badge_id FROM user_badges WHERE user_id=?", (uid,))]
        self.assertIn("master_10", badges, "自愈必须补齐徽章")

        # ---- 幂等：再跑一次结果不变 ----
        status, data = self.box.post("/api/admin/rebuild-progress", {"user_id": uid},
                                     token=self.box.token("admin"))
        self.assertEqual(status, 200, data)
        again = self.box.query(
            "SELECT next_review_at FROM learn_progress WHERE user_id=? ORDER BY word_id",
            (uid,))
        self.assertEqual([r["next_review_at"] for r in again],
                         [schedule[w] for w in words],
                         "重复自愈不得把复习到期日整体往后推")

    def test_rebuild_keeps_rows_without_events(self) -> None:
        name, token = self.box.new_user("rbkeep")
        uid = self.box.user_id(name)
        self._seed_mastered(token, [17])
        # 没有事件的历史进度（旧接口遗留 / 手工补录）：自愈不得清零
        self.box.exec_sql(
            "INSERT OR REPLACE INTO learn_progress"
            " (user_id, word_id, seen, correct, master, next_review_at)"
            " VALUES (?, 40, 7, 7, 1, '2030-01-01 00:00:00')", (uid,))

        status, data = self.box.post("/api/admin/rebuild-progress", {"user_id": uid},
                                     token=self.box.token("admin"))
        self.assertEqual(status, 200, data)

        rows = self.box.query(
            "SELECT seen, master, next_review_at FROM learn_progress"
            " WHERE user_id=? AND word_id=40", (uid,))
        self.assertEqual(len(rows), 1, "无事件的词不能被删除")
        self.assertEqual(int(rows[0]["seen"]), 7)
        self.assertEqual(int(rows[0]["master"]), 1)
        self.assertEqual(rows[0]["next_review_at"], "2030-01-01 00:00:00")

    def test_rebuild_requires_admin(self) -> None:
        status, data = self.box.post("/api/admin/rebuild-progress", {},
                                     token=self.box.token("alice"))
        self.assertEqual(status, 403, data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
