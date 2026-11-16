#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""认证与会话：登录、令牌校验、登出/互踢/单点注销、注册校验、首启改密闸门。

替代根目录 `_api_contract_check.py` 里「用户接口不再接受缺省口令」等手工断言，
并补上此前完全没覆盖的会话失效与改密闸门。
"""
from __future__ import annotations

import unittest

from support import PASSWORD, BaseCase


class AuthTest(BaseCase):
    # ---- 登录 ----
    def test_login_returns_session(self) -> None:
        data = self.box.login("alice")
        self.assertIn("access_token", data)
        self.assertEqual(data["user"]["username"], "alice")
        self.assertEqual(data["token_type"], "Bearer")

        status, me = self.box.get("/api/auth/me", token=data["access_token"])
        self.assertEqual(status, 200, me)
        self.assertEqual(me["username"], "alice")
        self.assertEqual(me["role"], "user")
        self.assertIn("words:read", me["permissions"])

    def test_login_wrong_password(self) -> None:
        status, data = self.box.post(
            "/api/auth/login", {"username": "alice", "password": "wrong-password"})
        self.assertEqual(status, 401, data)
        self.assertEqual(data.get("error"), "auth_failed")

    def test_login_missing_fields(self) -> None:
        status, data = self.box.post("/api/auth/login", {"username": "alice"})
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "bad_request")

    # ---- 令牌 ----
    def test_protected_endpoint_requires_token(self) -> None:
        for path in ("/api/words", "/api/collections", "/api/study/summary",
                     "/api/learn/progress", "/api/together/rooms"):
            status, data = self.box.get(path)
            self.assertEqual(status, 401, f"{path} 未鉴权应 401，实际 {status} {data}")
            self.assertEqual(data.get("error"), "unauthorized")

    def test_bearer_authorization_header_accepted(self) -> None:
        token = self.box.token("alice")
        status, data = self.box.request(
            "GET", "/api/auth/me",
            extra_headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["username"], "alice")

    def test_garbage_token_rejected(self) -> None:
        status, data = self.box.get("/api/auth/me", token="not-a-real-token")
        self.assertEqual(status, 401, data)

    def test_logout_invalidates_session(self) -> None:
        token = self.box.login("bob")["access_token"]
        status, _ = self.box.post("/api/auth/logout", {}, token=token)
        self.assertEqual(status, 200)
        status, _ = self.box.get("/api/auth/me", token=token)
        self.assertEqual(status, 401, "登出后旧 Token 必须失效")

    def test_refresh_issues_working_token(self) -> None:
        session = self.box.login("bob")
        status, refreshed = self.box.post(
            "/api/auth/refresh", {"refresh_token": session["refresh_token"]})
        self.assertEqual(status, 200, refreshed)
        self.assertIn("access_token", refreshed)
        status, me = self.box.get("/api/auth/me", token=refreshed["access_token"])
        self.assertEqual(status, 200, me)
        self.assertEqual(me["username"], "bob")

    def test_refresh_rejects_bad_token(self) -> None:
        status, data = self.box.post("/api/auth/refresh", {"refresh_token": "bad"})
        self.assertEqual(status, 401, data)

    def test_new_login_kicks_old_session(self) -> None:
        first = self.box.login("carol")["access_token"]
        second = self.box.login("carol")["access_token"]
        self.assertNotEqual(first, second)
        status, _ = self.box.get("/api/auth/me", token=first)
        self.assertEqual(status, 401, "新设备登录后旧会话必须被撤销")
        status, _ = self.box.get("/api/auth/me", token=second)
        self.assertEqual(status, 200)

    def test_slo_revokes_all_sessions(self) -> None:
        token = self.box.login("carol")["access_token"]
        status, data = self.box.post("/api/auth/slo", {}, token=token)
        self.assertEqual(status, 200, data)
        self.assertGreaterEqual(data.get("sessions_revoked", 0), 1)
        status, _ = self.box.get("/api/auth/me", token=token)
        self.assertEqual(status, 401)

    # ---- 注册 ----
    def test_register_validates_then_creates(self) -> None:
        status, data = self.box.post(
            "/api/auth/register", {"username": "newbie", "password": "123"})
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "weak_password")

        status, data = self.box.post(
            "/api/auth/register",
            {"username": "newbie", "password": PASSWORD, "email": "bad-email"})
        self.assertEqual(status, 400, data)

        status, data = self.box.post(
            "/api/auth/register",
            {"username": "newbie", "password": PASSWORD, "email": "newbie@example.com"})
        self.assertEqual(status, 201, data)
        self.assertTrue(data.get("ok"))

        status, data = self.box.post(
            "/api/auth/register", {"username": "newbie", "password": PASSWORD})
        self.assertEqual(status, 409, data)

        session = self.box.login("newbie")
        self.assertEqual(session["user"]["role"], "user")

    def test_check_username(self) -> None:
        status, data = self.box.get("/api/auth/check-username?username=alice")
        self.assertEqual(status, 200, data)
        self.assertFalse(data["available"])
        status, data = self.box.get("/api/auth/check-username?username=definitely_new")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["available"])

    # ---- 管理员接口的鉴权 ----
    def test_admin_endpoints_reject_normal_user(self) -> None:
        token = self.box.token("bob")
        for method, path, body in (
            ("GET", "/api/auth/users", None),
            ("GET", "/api/auth/events", None),
            ("GET", "/api/admin/report", None),
            ("POST", "/api/admin/rebuild-progress", {}),
        ):
            status, data = self.box.request(method, path, body, token=token)
            self.assertEqual(status, 403, f"{path} 普通用户应 403，实际 {status} {data}")
            self.assertEqual(data.get("error"), "forbidden")

    def test_admin_cannot_delete_self(self) -> None:
        token = self.box.token("admin")
        admin_id = self.box.user_id("admin")
        status, data = self.box.post(
            "/api/auth/users", {"action": "delete", "id": admin_id}, token=token)
        self.assertEqual(status, 400, data)
        self.assertIn("不能删除当前登录账户", data.get("message", ""))

    def test_admin_create_user_requires_explicit_password(self) -> None:
        token = self.box.token("admin")
        status, data = self.box.post(
            "/api/auth/users", {"action": "create", "username": "nopw"}, token=token)
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("error"), "bad_request")

    # ---- 首启改密闸门 ----
    def test_must_change_password_gate(self) -> None:
        self.box.exec_users("UPDATE users SET must_change_password=1 WHERE username=?",
                            ("bob",))
        session = self.box.login("bob")
        self.assertEqual(session.get("must_change_password"), 1)

        status, data = self.box.get("/api/words", token=session["access_token"])
        self.assertEqual(status, 403, data)
        self.assertEqual(data.get("error"), "password_change_required")

        status, data = self.box.post(
            "/api/auth/change-password",
            {"old_password": PASSWORD, "new_password": PASSWORD + "x"},
            token=session["access_token"])
        self.assertEqual(status, 200, data)

        status, _ = self.box.get("/api/words", token=session["access_token"])
        self.assertEqual(status, 200, "改密后应立即放行其它接口")
        # 复原口令，避免影响同类其它用例
        self.box.exec_users("UPDATE users SET must_change_password=0 WHERE username=?",
                            ("bob",))
        self.box.login("bob", PASSWORD + "x")
        self.box.post("/api/auth/change-password",
                      {"old_password": PASSWORD + "x", "new_password": PASSWORD},
                      token=self.box.token("bob"))
        self.box.login("bob")

    # ---- 错误路径与静态资源 ----
    def test_unknown_api_returns_json_404(self) -> None:
        status, data = self.box.get("/api/does-not-exist")
        self.assertEqual(status, 404, data)
        self.assertEqual(data.get("error"), "not_found")
        status, data = self.box.post("/api/does-not-exist", {})
        self.assertEqual(status, 404, data)

    def test_static_path_traversal_blocked(self) -> None:
        for path in ("/js/%2e%2e/%2e%2e/data/users.db",
                     "/%2e%2e/app/../data/users.db",
                     "/api/../data/users.db"):
            status, _ = self.box.get(path)
            self.assertIn(status, (400, 404), f"{path} 不应可读，实际 {status}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
