#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""一起背（学习房间）：建房 / 加入幂等 / 成员与房主权限 / 点赞 / 解散。

此前只有 `_tg_http.py` / `_tg_browser.mjs` 这类依赖本机端口的手工脚本，
CI 里完全没有覆盖。这里用真实 HTTP 把权限矩阵钉住。
"""
from __future__ import annotations

import unittest

from support import BaseCase


class TogetherTest(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self._rooms: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        for token, code in self._rooms:
            self.box.post("/api/together/dissolve", {"code": code}, token=token)
        super().tearDown()

    # ---- 辅助 ----
    def make_room(self, token: str, name: str = "测试房间",
                  book: object = None, unit: object = None) -> str:
        status, data = self.box.post(
            "/api/together/create", {"name": name, "book": book, "unit": unit},
            token=token)
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)
        code = data["code"]
        self._rooms.append((token, code))
        return code

    # ---- 建房 ----
    def test_create_room_and_list(self) -> None:
        alice = self.box.token("alice")
        code = self.make_room(alice, "早读小队", 1, "Unit 1")
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isalnum())

        status, data = self.box.get("/api/together/rooms", token=alice)
        self.assertEqual(status, 200, data)
        room = next(r for r in data["rooms"] if r["code"] == code)
        self.assertTrue(room["is_owner"])
        self.assertEqual(room["member_count"], 1)
        self.assertIn("必修第一册", room["scope_label"])
        self.assertIn("/quiz", room["study_url"])

    def test_create_validates_scope(self) -> None:
        alice = self.box.token("alice")
        status, data = self.box.post(
            "/api/together/create", {"name": "坏册号", "book": 9}, token=alice)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "bad_scope")

        status, data = self.box.post(
            "/api/together/create", {"name": "只有单元", "unit": "Unit 1"}, token=alice)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "bad_scope")

        status, data = self.box.post(
            "/api/together/create", {"name": "", "book": 1}, token=alice)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "bad_name")

    def test_scopes_listing(self) -> None:
        status, data = self.box.get("/api/together/scopes", token=self.box.token("alice"))
        self.assertEqual(status, 200, data)
        books = {b["book"]: b for b in data["books"]}
        self.assertIn(1, books)
        self.assertIn(2, books)
        self.assertEqual(sum(u["count"] for u in books[1]["units"]), 20)

    # ---- 加入 ----
    def test_join_is_idempotent(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        code = self.make_room(alice, "加入测试")

        status, data = self.box.post("/api/together/join", {"code": code}, token=bob)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["already"])

        status, data = self.box.post("/api/together/join", {"code": code.lower()},
                                     token=bob)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["already"], "重复加入应幂等，并显式标记 already")

        status, data = self.box.get(f"/api/together/room?code={code}", token=alice)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["room"]["member_count"], 2)

    def test_join_unknown_code(self) -> None:
        status, data = self.box.post("/api/together/join", {"code": "ZZZZZZ"},
                                     token=self.box.token("bob"))
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "not_found")

    def test_non_member_cannot_view_room(self) -> None:
        alice = self.box.token("alice")
        carol = self.box.token("carol")
        code = self.make_room(alice, "私密房间")
        status, data = self.box.get(f"/api/together/room?code={code}", token=carol)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "not_member")

    # ---- 点赞 ----
    def test_like_rules(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        carol = self.box.token("carol")
        code = self.make_room(alice, "点赞房")
        self.box.post("/api/together/join", {"code": code}, token=bob)

        alice_id = self.box.user_id("alice")
        status, data = self.box.post(
            "/api/together/like", {"code": code, "to_user_id": alice_id}, token=bob)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["likes_today"], 1)
        self.assertFalse(data["already"])

        status, data = self.box.post(
            "/api/together/like", {"code": code, "to_user_id": alice_id}, token=bob)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["already"], "同一人同一天重复点赞应幂等")
        self.assertEqual(data["likes_today"], 1)

        status, data = self.box.post(
            "/api/together/like", {"code": code, "to_user_id": alice_id}, token=alice)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "self_like")

        status, data = self.box.post(
            "/api/together/like", {"code": code, "to_user_id": alice_id}, token=carol)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "not_member")

    def test_room_detail_shows_member_progress(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        code = self.make_room(alice, "进度房")
        self.box.post("/api/together/join", {"code": code}, token=bob)
        self.box.post("/api/study/events", {
            "events": [{"word_id": 31, "action": "quiz", "result": 1, "uid": "tg-31"}]},
            token=bob)

        status, data = self.box.get(f"/api/together/room?code={code}", token=alice)
        self.assertEqual(status, 200, data)
        members = {m["user_id"]: m for m in data["members"]}
        bob_row = members[self.box.user_id("bob")]
        self.assertEqual(bob_row["answered"], 1)
        self.assertFalse(bob_row["is_owner"])
        self.assertTrue(members[self.box.user_id("alice")]["is_owner"])

    # ---- 退出 / 解散 ----
    def test_dissolve_requires_owner(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        code = self.make_room(alice, "解散测试")
        self.box.post("/api/together/join", {"code": code}, token=bob)

        status, data = self.box.post("/api/together/dissolve", {"code": code},
                                     token=bob)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "forbidden")

        status, data = self.box.post("/api/together/dissolve", {"code": code},
                                     token=alice)
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"))
        status, data = self.box.get(f"/api/together/room?code={code}", token=alice)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "not_found")

    def test_leave_transfers_owner_then_dissolves(self) -> None:
        alice = self.box.token("alice")
        bob = self.box.token("bob")
        code = self.make_room(alice, "转让测试")
        self.box.post("/api/together/join", {"code": code}, token=bob)

        status, data = self.box.post("/api/together/leave", {"code": code}, token=alice)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["dissolved"])

        status, data = self.box.get(f"/api/together/room?code={code}", token=bob)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["room"]["is_owner"], "房主退出后应转让给最早加入的成员")

        status, data = self.box.post("/api/together/leave", {"code": code}, token=bob)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["dissolved"], "最后一人退出应解散房间")

    def test_leave_when_not_member(self) -> None:
        alice = self.box.token("alice")
        carol = self.box.token("carol")
        code = self.make_room(alice, "非成员退出")
        status, data = self.box.post("/api/together/leave", {"code": code}, token=carol)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "not_member")

    def test_max_rooms_per_user(self) -> None:
        carol = self.box.token("carol")
        for i in range(5):
            self.make_room(carol, f"占位{i}")
        status, data = self.box.post(
            "/api/together/create", {"name": "第六个"}, token=carol)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "too_many_rooms")

    # ---- 鉴权 / 游客 ----
    def test_guest_blocked_from_writing(self) -> None:
        status, session = self.box.post("/api/auth/guest-login", {})
        self.assertEqual(status, 200, session)
        token = session["access_token"]
        status, data = self.box.post("/api/together/create", {"name": "游客房"},
                                     token=token)
        self.assertEqual(status, 403, data)
        self.assertTrue(data.get("guest"))

        status, data = self.box.get("/api/together/rooms", token=token)
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("guest"))
        self.assertEqual(data["rooms"], [])

    def test_requires_login(self) -> None:
        status, data = self.box.post("/api/together/create", {"name": "匿名"})
        self.assertEqual(status, 401, data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
