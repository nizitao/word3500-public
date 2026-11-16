#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""旧接口 `POST /api/learn/progress` 必须收敛到事件流。

设计文档承诺「study_events 是唯一事实来源」，但这个降级接口历史上直接
UPDATE learn_progress：不写事件、不写 daily_stats，导致这些进度无法自愈、
打卡与日历永远是空的。这里钉住收敛后的行为。
"""
from __future__ import annotations

import unittest

from support import BaseCase


class LegacyProgressTest(BaseCase):
    def _save(self, token: str, items: list[dict], tz: int = 0):
        return self.box.post("/api/learn/progress",
                             {"progress": items, "tz": tz}, token=token)

    def test_progress_is_translated_into_events(self) -> None:
        name, token = self.box.new_user("leg")
        uid = self.box.user_id(name)

        status, data = self._save(token, [
            {"word_id": 33, "seen": 5, "correct": 5, "master": 1}])
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertTrue(data["deprecated"])
        self.assertEqual(data["saved"], 1)
        self.assertEqual(data["events"], 5, "seen 的增量要补成 5 条事件")

        row = self.box.query(
            "SELECT seen, correct, master FROM learn_progress"
            " WHERE user_id=? AND word_id=33", (uid,))[0]
        self.assertEqual(int(row["seen"]), 5)
        self.assertEqual(int(row["correct"]), 5)
        self.assertEqual(int(row["master"]), 1)

        events = self.box.query(
            "SELECT action, result, uid FROM study_events"
            " WHERE user_id=? AND word_id=33 ORDER BY id", (uid,))
        self.assertEqual(len(events), 5, "旧接口必须留下事件，才能被自愈重建")
        self.assertTrue(all(str(e["uid"]).startswith("legacy:33:") for e in events))

        stat = self.box.query(
            "SELECT answered, correct, new_words FROM daily_stats WHERE user_id=?",
            (uid,))
        self.assertEqual(len(stat), 1, "旧接口必须同时派生 daily_stats")
        self.assertEqual(int(stat[0]["answered"]), 5)
        self.assertEqual(int(stat[0]["correct"]), 5)
        self.assertEqual(int(stat[0]["new_words"]), 1)

        status, summary = self.box.get("/api/study/summary", token=token)
        self.assertEqual(status, 200, summary)
        self.assertEqual(summary["summary"]["learned_total"], 1)
        self.assertEqual(summary["summary"]["mastered_total"], 1)

    def test_replay_is_idempotent(self) -> None:
        name, token = self.box.new_user("legdup")
        uid = self.box.user_id(name)
        payload = [{"word_id": 34, "seen": 4, "correct": 2}]

        status, first = self._save(token, payload)
        self.assertEqual(status, 200, first)
        self.assertEqual(first["saved"], 1)

        status, second = self._save(token, payload)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["saved"], 0, "同一份进度重放不应重复写事件")
        self.assertEqual(second["events"], 0)

        count = self.box.query(
            "SELECT COUNT(*) FROM study_events WHERE user_id=? AND word_id=34", (uid,))
        self.assertEqual(int(count[0][0]), 4, "重放不得让进度翻倍")

    def test_client_master_is_never_trusted(self) -> None:
        name, token = self.box.new_user("legforge")
        uid = self.box.user_id(name)
        status, data = self._save(token, [
            {"word_id": 35, "seen": 1, "correct": 0, "master": 1}])
        self.assertEqual(status, 200, data)
        row = self.box.query(
            "SELECT master FROM learn_progress WHERE user_id=? AND word_id=35", (uid,))[0]
        self.assertEqual(int(row["master"]), 0, "master 只能由服务端按规则计算")

    def test_only_increases_and_skips_bad_rows(self) -> None:
        name, token = self.box.new_user("legmin")
        uid = self.box.user_id(name)
        status, data = self._save(token, [
            {"word_id": 36, "seen": 4, "correct": 2},
            {"word_id": 999999, "seen": 9, "correct": 9},
            {"word_id": "abc", "seen": 1},
            {"seen": 3},
        ])
        self.assertEqual(status, 200, data)
        self.assertEqual(data["saved"], 1, "只应处理真实存在且格式正确的词")

        status, data = self._save(token, [
            {"word_id": 36, "seen": 1, "correct": 1}])
        self.assertEqual(status, 200, data)
        self.assertEqual(data["saved"], 0, "客户端传小值不得回退服务端进度")
        row = self.box.query(
            "SELECT seen, correct FROM learn_progress WHERE user_id=? AND word_id=36",
            (uid,))[0]
        self.assertEqual(int(row["seen"]), 4)
        self.assertEqual(int(row["correct"]), 2)

    def test_guest_rejected(self) -> None:
        status, session = self.box.post("/api/auth/guest-login", {})
        self.assertEqual(status, 200, session)
        status, data = self._save(session["access_token"],
                                  [{"word_id": 37, "seen": 1, "correct": 1}])
        self.assertEqual(status, 403, data)

    def test_requires_login(self) -> None:
        status, data = self.box.post("/api/learn/progress",
                                     {"progress": [{"word_id": 37, "seen": 1}]})
        self.assertEqual(status, 401, data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
