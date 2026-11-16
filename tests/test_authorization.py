#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""越权（IDOR / 权限提升）回归：所有 user_id 必须来自服务端会话。

历史事故见 `git log`：`/api/collections/words?cid=` 曾只要猜对 cid 就能读他人收藏夹。
这里把「跨用户读写」逐条钉住，避免以后为了「顺手」把 user_id 改成请求参数。
"""
from __future__ import annotations

import unittest

from support import BaseCase


class AuthorizationTest(BaseCase):
    # ---- 收藏夹 ----
    def _new_collection(self, token: str, name: str) -> int:
        status, data = self.box.post(
            "/api/collections", {"action": "create", "name": name}, token=token)
        self.assertEqual(status, 200, data)
        return int(data["id"])

    def test_cannot_read_other_users_collection(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        cid = self._new_collection(alice, "爱丽丝的收藏夹")
        self.box.post("/api/collections/toggle", {"wid": 21, "cids": [cid]},
                      token=alice)

        status, data = self.box.get(f"/api/collections/words?cid={cid}", token=alice)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["total"], 1)

        status, data = self.box.get(f"/api/collections/words?cid={cid}", token=bob)
        self.assertEqual(status, 403, f"他人收藏夹不应可读: {status} {data}")

    def test_cannot_delete_or_rename_other_users_collection(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        cid = self._new_collection(alice, "别动我")

        status, data = self.box.post(
            "/api/collections", {"action": "delete", "id": cid}, token=bob)
        self.assertEqual(status, 404, data)
        rows = self.box.query("SELECT name FROM collections WHERE id=?", (cid,))
        self.assertEqual(len(rows), 1, "他人收藏夹不得被删除")

        status, _ = self.box.post(
            "/api/collections", {"action": "rename", "id": cid, "name": "被改名"},
            token=bob)
        rows = self.box.query("SELECT name FROM collections WHERE id=?", (cid,))
        self.assertEqual(rows[0]["name"], "别动我", "他人收藏夹不得被改名")

    def test_toggle_ignores_foreign_collection_ids(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        cid = self._new_collection(alice, "爱丽丝私有")

        status, data = self.box.post(
            "/api/collections/toggle", {"wid": 22, "cids": [cid, 999999]}, token=bob)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["cids"], [], "不属于自己的收藏夹 ID 必须被丢弃")
        rows = self.box.query(
            "SELECT 1 FROM collection_words WHERE collection_id=?", (cid,))
        self.assertEqual(rows, [], "不得向他人收藏夹写入单词")

    def test_word_status_scoped_to_user(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        cid = self._new_collection(alice, "状态检查")
        self.box.post("/api/collections/toggle", {"wid": 23, "cids": [cid]},
                      token=alice)

        status, data = self.box.get("/api/collections/word-status?wid=23", token=alice)
        self.assertEqual(status, 200, data)
        self.assertIn(cid, data["ids"])
        status, data = self.box.get("/api/collections/word-status?wid=23", token=bob)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["ids"], [])

    def test_collections_list_only_own(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        cid = self._new_collection(alice, "列表隔离")
        status, data = self.box.get("/api/collections", token=bob)
        self.assertEqual(status, 200, data)
        self.assertNotIn(cid, [c["id"] for c in data["collections"]])

    # ---- 学习数据 ----
    def test_learn_progress_isolated(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        self.box.post("/api/study/events", {
            "events": [{"word_id": 24, "action": "quiz", "result": 1, "uid": "iso-a"}]},
            token=alice)

        status, data = self.box.get("/api/learn/progress", token=alice)
        self.assertEqual(status, 200, data)
        self.assertIn(24, [p["word_id"] for p in data["progress"]])

        status, data = self.box.get("/api/learn/progress", token=bob)
        self.assertEqual(status, 200, data)
        self.assertNotIn(24, [p["word_id"] for p in data["progress"]])

    def test_event_body_user_id_is_ignored(self) -> None:
        alice = self.box.token("alice")
        bob_id = self.box.user_id("bob")
        status, data = self.box.post("/api/study/events", {
            "user_id": bob_id,
            "events": [{"word_id": 25, "action": "quiz", "result": 1,
                        "uid": "spoof-1", "user_id": bob_id}]},
            token=alice)
        self.assertEqual(status, 200, data)
        rows = self.box.query(
            "SELECT user_id FROM study_events WHERE word_id=25 AND uid='spoof-1'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["user_id"]), self.box.user_id("alice"),
                         "事件必须落在当前登录账户名下")

    def test_quiz_progress_isolated(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        status, data = self.box.post("/api/quiz/progress", {
            "book": "1", "unit": "Unit 1", "queue": [1, 2], "quiz_pos": 1,
            "word_index": 1, "stats": {"answered": 1, "correct": 1}}, token=alice)
        self.assertEqual(status, 200, data)
        status, data = self.box.get("/api/quiz/progress", token=alice)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["has"])
        status, data = self.box.get("/api/quiz/progress", token=bob)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["has"], "断点不得跨账户可见")

    # ---- 管理接口 ----
    def test_admin_user_detail_rejects_normal_user(self) -> None:
        bob_id = self.box.user_id("bob")
        status, data = self.box.get(f"/api/auth/users/{bob_id}",
                                    token=self.box.token("bob"))
        self.assertEqual(status, 403, data)

    def test_admin_user_detail_allows_admin(self) -> None:
        bob_id = self.box.user_id("bob")
        status, data = self.box.get(f"/api/auth/users/{bob_id}",
                                    token=self.box.token("admin"))
        self.assertEqual(status, 200, data)
        self.assertEqual(data["user"]["username"], "bob")
        self.assertNotIn("password", data["user"], "用户详情不得回传口令哈希")

    def test_normal_user_cannot_trigger_rebuild(self) -> None:
        status, data = self.box.post("/api/admin/rebuild-progress",
                                     {"user_id": self.box.user_id("alice")},
                                     token=self.box.token("bob"))
        self.assertEqual(status, 403, data)

    def test_report_rejects_normal_user(self) -> None:
        status, data = self.box.get("/api/admin/report", token=self.box.token("carol"))
        self.assertEqual(status, 403, data)

    def test_online_snapshot_hides_usernames_from_anonymous(self) -> None:
        status, data = self.box.get("/api/online")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["users"], [], "匿名访问不得看到在线用户名")


if __name__ == "__main__":
    unittest.main(verbosity=2)
