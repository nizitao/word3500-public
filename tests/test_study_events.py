#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""学习事件流：幂等、服务端权威判定、当日聚合与打卡、补签。

这些是「事件流是唯一事实来源」的地基，此前只能靠浏览器脚本手测。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from support import BaseCase


def local_day(tz_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(minutes=tz_minutes)).strftime("%Y-%m-%d")


class StudyEventsTest(BaseCase):
    # 注册接口有「每 IP 10 次/10 分钟」限流：只需要一个干净账户的用例共用它
    _misc: tuple[str, str] | None = None

    def misc_user(self) -> tuple[str, str]:
        if StudyEventsTest._misc is None:
            StudyEventsTest._misc = self.box.new_user("misc")
        return StudyEventsTest._misc

    def _post(self, token: str, events: list[dict], tz: int = 0):
        return self.box.post("/api/study/events", {"tz": tz, "events": events},
                             token=token)

    def test_requires_login(self) -> None:
        status, data = self.box.post("/api/study/events",
                                     {"events": [{"word_id": 1, "action": "learn"}]})
        self.assertEqual(status, 401, data)

    def test_guest_is_rejected(self) -> None:
        status, session = self.box.post("/api/auth/guest-login", {})
        self.assertEqual(status, 200, session)
        token = session["access_token"]
        status, data = self._post(token, [
            {"word_id": 1, "action": "learn", "result": -1, "uid": "g1"}])
        self.assertEqual(status, 403, data)
        self.assertTrue(data.get("guest"))

        status, data = self.box.get("/api/study/summary", token=token)
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("guest"), "游客应得到演示态而不是真实统计")

    def test_event_writes_progress_and_daily_stats(self) -> None:
        name, token = self.box.new_user("ev")
        status, data = self._post(token, [
            {"word_id": 1, "action": "learn", "result": -1, "uid": "e1"},
            {"word_id": 1, "action": "quiz", "result": 1, "uid": "e2"},
        ])
        self.assertEqual(status, 200, data)
        self.assertEqual(data["accepted"], 2, data)
        self.assertEqual(data["progress"]["1"]["seen"], 2)
        self.assertEqual(data["progress"]["1"]["correct"], 1)

        uid = self.box.user_id(name)
        row = self.box.query(
            "SELECT seen, correct, master, next_review_at FROM learn_progress"
            " WHERE user_id=? AND word_id=1", (uid,))[0]
        self.assertEqual(int(row["seen"]), 2)
        self.assertEqual(int(row["correct"]), 1)
        self.assertEqual(int(row["master"]), 0, "只答对 1 次不应掌握")
        self.assertIsNotNone(row["next_review_at"], "必须写入复习调度时间")

        stat = self.box.query(
            "SELECT answered, correct, new_words, learned_total FROM daily_stats"
            " WHERE user_id=? AND day=?", (uid, data["day"]))[0]
        self.assertEqual(int(stat["answered"]), 1, "只有 quiz 计入答题")
        self.assertEqual(int(stat["correct"]), 1)
        self.assertEqual(int(stat["new_words"]), 1)
        self.assertEqual(int(stat["learned_total"]), 1)

    def test_duplicate_batch_is_idempotent(self) -> None:
        name, token = self.box.new_user("dup")
        batch = [
            {"word_id": 2, "action": "quiz", "result": 1, "uid": "same-batch-1"},
            {"word_id": 3, "action": "quiz", "result": 0, "uid": "same-batch-2"},
        ]
        status, first = self._post(token, batch)
        self.assertEqual(status, 200, first)
        self.assertEqual(first["accepted"], 2)

        status, second = self._post(token, batch)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["accepted"], 0, "重放同一批次不应再计数")
        self.assertEqual(second["duplicates"], 2)
        # 重复批次仍应回传权威进度，供客户端对齐本地缓存
        self.assertEqual(second["progress"]["2"]["seen"], 1)

        uid = self.box.user_id(name)
        row = self.box.query(
            "SELECT seen FROM learn_progress WHERE user_id=? AND word_id=2", (uid,))[0]
        self.assertEqual(int(row["seen"]), 1, "重复上报不得让进度翻倍")
        n = self.box.query(
            "SELECT COUNT(*) FROM study_events WHERE user_id=? AND word_id=2", (uid,))[0][0]
        self.assertEqual(int(n), 1)

    def test_master_rule_is_server_side(self) -> None:
        name, token = self.box.new_user("mst")
        status, data = self._post(token, [
            {"word_id": 4, "action": "quiz", "result": 1, "uid": "m1"},
            {"word_id": 4, "action": "quiz", "result": 1, "uid": "m2"},
            {"word_id": 4, "action": "quiz", "result": 1, "uid": "m3"},
        ])
        self.assertEqual(status, 200, data)
        self.assertEqual(data["progress"]["4"]["master"], 1,
                         "3 次全对（≥3 且 100%）应判定掌握")

        # 客户端即使塞 master=1 也不被采信，只能由事件推导
        status, data = self._post(token, [
            {"word_id": 5, "action": "quiz", "result": 0, "uid": "m4",
             "master": 1, "seen": 999, "correct": 999},
        ])
        self.assertEqual(status, 200, data)
        self.assertEqual(data["progress"]["5"]["master"], 0)
        self.assertEqual(data["progress"]["5"]["seen"], 1, "客户端 seen 必须被忽略")

        # 已掌握的词再答错 → master 回落
        status, data = self._post(token, [
            {"word_id": 4, "action": "quiz", "result": 0, "uid": "m5"}])
        self.assertEqual(status, 200, data)
        self.assertEqual(data["progress"]["4"]["master"], 0,
                         "最近一次答错后 master 必须回落为 0")

    def test_invalid_events_are_rejected_not_fatal(self) -> None:
        _name, token = self.misc_user()
        status, data = self._post(token, [
            {"word_id": 999999, "action": "quiz", "result": 1, "uid": "b1"},
            {"word_id": 6, "action": "hack", "result": 1, "uid": "b2"},
            {"word_id": 6, "action": "quiz", "result": 9, "uid": "b3"},
            {"word_id": "abc", "action": "quiz", "result": 1, "uid": "b4"},
            {"word_id": 6, "action": "quiz", "result": 1, "uid": "b5"},
        ])
        self.assertEqual(status, 200, data)
        self.assertEqual(data["accepted"], 1)
        self.assertEqual(data["rejected"], 4)

    def test_empty_events_rejected(self) -> None:
        _name, token = self.misc_user()
        status, data = self._post(token, [])
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "bad_request")

    def test_day_follows_client_timezone(self) -> None:
        _name, token = self.misc_user()
        tz = 480  # UTC+8
        before = local_day(tz)
        status, data = self._post(token, [
            {"word_id": 7, "action": "learn", "result": -1, "uid": "tz1"}], tz=tz)
        after = local_day(tz)
        self.assertEqual(status, 200, data)
        self.assertIn(data["day"], (before, after),
                      "day 必须按客户端时区偏移计算，而不是直接采信请求体")

    def test_checkin_after_ten_answers(self) -> None:
        name, token = self.box.new_user("chk")
        events = [{"word_id": w, "action": "quiz", "result": 1, "uid": f"c{w}"}
                  for w in range(1, 11)]
        status, data = self._post(token, events)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["just_checked_in"], data)
        self.assertTrue(data["summary"]["checked_in"], data)
        self.assertEqual(data["summary"]["streak"], 1)
        self.assertGreater(data["points_delta"], 0, "打卡应加分")

        uid = self.box.user_id(name)
        stat = self.box.query(
            "SELECT answered, checked_in FROM daily_stats WHERE user_id=? AND day=?",
            (uid, data["day"]))[0]
        self.assertEqual(int(stat["answered"]), 10)
        self.assertEqual(int(stat["checked_in"]), 1)

        # 保持打卡：同日再上报不应重复发打卡分
        status, again = self._post(token, [
            {"word_id": 11, "action": "quiz", "result": 1, "uid": "c11"}])
        self.assertEqual(status, 200, again)
        self.assertFalse(again["just_checked_in"])

    def test_summary_and_calendar_are_per_user(self) -> None:
        _n1, t1 = self.box.new_user("s1")
        _n2, t2 = self.box.new_user("s2")
        status, data = self._post(t1, [
            {"word_id": 12, "action": "quiz", "result": 1, "uid": "s1a"}])
        self.assertEqual(status, 200, data)
        day = data["day"]

        status, mine = self.box.get("/api/study/summary", token=t1)
        self.assertEqual(status, 200, mine)
        self.assertEqual(mine["summary"]["learned_total"], 1)
        self.assertEqual(mine["summary"]["answered"], 1)

        status, other = self.box.get("/api/study/summary", token=t2)
        self.assertEqual(status, 200, other)
        self.assertEqual(other["summary"]["learned_total"], 0,
                         "统计只能看自己的数据")

        status, cal = self.box.get(f"/api/study/calendar?month={day[:7]}", token=t1)
        self.assertEqual(status, 200, cal)
        self.assertEqual(len(cal["days"]), 1)
        self.assertEqual(cal["days"][0]["day"], day)

        status, detail = self.box.get(f"/api/study/day?day={day}", token=t1)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["total"], 1)
        self.assertEqual(detail["events"][0]["word_id"], 12)

    def test_makeup_rules(self) -> None:
        _name, token = self.box.new_user("mk")
        today = local_day(0)
        status, data = self.box.post("/api/study/makeup", {"day": today}, token=token)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"], data)

        status, data = self.box.post("/api/study/makeup", {"day": today}, token=token)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "already")

        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        status, data = self.box.post("/api/study/makeup", {"day": tomorrow}, token=token)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "future")

        status, data = self.box.post("/api/study/makeup", {"day": "1999-01-01"},
                                     token=token)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "other_month")

    def test_leaderboard_excludes_guests(self) -> None:
        status, session = self.box.post("/api/auth/guest-login", {})
        self.assertEqual(status, 200, session)
        status, data = self.box.get("/api/leaderboard?metric=words&range=all",
                                    token=self.box.token("alice"))
        self.assertEqual(status, 200, data)
        names = [e["name"] for e in data["entries"]]
        self.assertFalse(any(str(n).startswith("guest") for n in names),
                         f"排行榜不得出现游客: {names}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
