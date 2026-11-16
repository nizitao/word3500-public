#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""路由：SSO 认证 / 用户管理 / 安全日志 / 注册。

令牌签发、会话校验、权限模型都在 api/auth.py；这里只做 HTTP 语义
（参数校验、限流、状态码、Cookie/页面跳转）。
"""
from urllib.parse import quote, urlparse

from api import auth

GET_ROUTES = {
    "/api/auth/me": ("api_auth_me", False),
    "/api/auth/userinfo": ("api_auth_userinfo", False),
    "/api/auth/.well-known/openid-configuration": ("api_oidc_discovery", False),
    "/api/auth/check-username": ("api_auth_check_username", True),
    "/api/auth/authorize": ("api_auth_authorize", False),
    "/api/auth/guest-login": ("api_auth_guest_login_get", False),
    "/api/auth/users": ("api_auth_users", False),
    "/api/auth/events": ("api_auth_events", False),
}

POST_ROUTES = {
    "/api/auth/login": ("api_auth_login", False),
    "/api/auth/logout": ("api_auth_logout", False),
    "/api/auth/refresh": ("api_auth_refresh", False),
    "/api/auth/slo": ("api_auth_slo", False),
    "/api/auth/guest-login": ("api_auth_guest_login", False),
    "/api/auth/token": ("api_auth_token", False),
    "/api/auth/change-password": ("api_auth_change_password", False),
    "/api/auth/register": ("api_auth_register", False),
    "/api/auth/users": ("api_auth_users_post", False),
}

# 带路径参数的路由前缀（/api/auth/users/<id>）
USER_DETAIL_PREFIX = "/api/auth/users/"


class AuthRoutes:
    # ---- 登录 / 登出 ----
    def api_auth_login(self) -> None:
        if self._rate_limited("login"):
            return
        body = self._read_json()
        username = (body.get("username") or "").strip()
        password = body.get("password", "")
        ip = self._get_client_ip()
        ua = self.headers.get("User-Agent", "")  # pyright: ignore[reportAttributeAccessIssue]

        if not username or not password:
            self._send_json({"error": "bad_request", "message": "用户名和密码不能为空"}, 400)
            return

        user = auth.authenticate_user(username, password)
        if not user:
            auth._log_event(0, username, "failed",
                            f"登录失败（IP: {ip}）", ip)
            self._send_json({"error": "auth_failed", "message": "用户名或密码错误"}, 401)
            return

        # 互踢：登录前先撤销该用户所有旧会话
        revoked = auth.revoke_all_sessions(user["id"])
        if revoked:
            auth._log_event(user["id"], username, "kickout",
                            f"新设备登录，踢掉 {revoked} 个旧会话（IP: {ip}）", ip)

        session = auth.create_session(user, ip=ip, ua=ua)
        self._send_json(session)

    def api_auth_logout(self) -> None:
        if self._rate_limited("logout"):
            return
        xtoken = self.headers.get("X-Auth-Token", "")  # pyright: ignore[reportAttributeAccessIssue]
        token = auth.extract_bearer_token(self.headers) or xtoken  # pyright: ignore[reportAttributeAccessIssue]
        if token:
            auth.revoke_session(token)
        self._send_json({"ok": True, "message": "已登出"})

    def api_auth_guest_login(self) -> None:
        """游客体验登录：每次发放独立临时账户（不再共享 uid、不再互相顶号）"""
        if self._rate_limited("guest"):
            return
        auth.cleanup_guests()
        user = auth.create_guest_user()
        if not user:
            self._send_json({"error": "internal", "message": "游客账户不可用"}, 500)
            return
        ip = self._get_client_ip()
        ua = self.headers.get("User-Agent", "")  # pyright: ignore[reportAttributeAccessIssue]
        session = auth.create_session(user, ip=ip, ua=ua)
        self._send_json(session)

    def api_auth_guest_login_get(self) -> None:
        """页面跳转方式创建游客会话：每次发放独立临时账户"""
        if self._rate_limited("guest"):
            return
        auth.cleanup_guests()
        user = auth.create_guest_user()
        if not user:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        ip = self._get_client_ip()
        ua = self.headers.get("User-Agent", "")  # pyright: ignore[reportAttributeAccessIssue]
        session = auth.create_session(user, ip=ip, ua=ua)
        # 返回一个微型页面，用 JS 写入 localStorage 后跳转首页
        from api.auth import json as _json  # 安全序列化
        user_json = _json.dumps(session["user"], ensure_ascii=False)
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>游客登录中…</title></head><body>
<script>
localStorage.setItem('word3500_auth', JSON.stringify({{
    access_token: {_json.dumps(session['access_token'])},
    refresh_token: {_json.dumps(session['refresh_token'])},
    user: {user_json},
    expires_at: Date.now() + {session['expires_in']} * 1000
}}));
var params = new URLSearchParams(location.search);
var r = params.get('redirect') || '/';
// 只允许站内相对路径：防止 ?redirect=javascript:... / //evil.com
location.href = (r.charAt(0) === '/' && r.charAt(1) !== '/' && r.charAt(1) !== '\\\\') ? r : '/';
</script>
<p style="font-family:sans-serif;text-align:center;margin-top:40vh;color:#888;">进入中…</p>
</body></html>"""
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)  # pyright: ignore[reportAttributeAccessIssue]

    def api_auth_refresh(self) -> None:
        if self._rate_limited("refresh"):
            return
        body = self._read_json()
        refresh_token = body.get("refresh_token", "")
        if not refresh_token:
            self._send_json({"error": "bad_request", "message": "缺少 refresh_token"}, 400)
            return
        result = auth.refresh_session(refresh_token, ip=self._get_client_ip())
        if not result:
            self._send_json({"error": "invalid_token",
                             "message": "Refresh Token 无效或已过期"}, 401)
            return
        self._send_json(result)

    def api_auth_slo(self) -> None:
        if self._rate_limited("logout"):
            return
        if not self._require_auth():
            return
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        assert user is not None
        user_id: int = user["sub"]
        count = auth.revoke_all_sessions(user_id)
        self._send_json({"ok": True, "message": f"已撤销 {count} 个会话",
                         "sessions_revoked": count})

    # ---- 当前用户 ----
    def api_auth_me(self) -> None:
        if not self._require_auth():
            return
        u = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        assert u is not None
        self._send_json({
            "id": u["sub"],
            "username": u["username"],
            "display_name": u.get("display_name", ""),
            "role": u["role"],
            "permissions": auth.get_effective_permissions(dict(u)),
        })

    def api_auth_userinfo(self) -> None:
        if not self._require_auth():
            return
        u = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        assert u is not None
        user = auth.get_user_by_id(u["sub"])
        self._send_json({
            "sub": str(u["sub"]),
            "preferred_username": u["username"],
            "name": u.get("display_name", "") or u["username"],
            "role": u["role"],
            "email": (user or {}).get("email", ""),
        })

    def api_oidc_discovery(self) -> None:
        self._send_json(auth.get_oidc_configuration(self._base_url))

    def api_auth_authorize(self) -> None:
        parsed = urlparse(self.path)  # pyright: ignore[reportAttributeAccessIssue]
        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query)
        redirect_uri = (qs.get("redirect_uri") or [""])[0]
        # SSO 前端会处理；此处返回登录页重定向
        if redirect_uri:
            self.send_response(302)
            self.send_header("Location", f"/login?redirect={quote(redirect_uri, safe='')}")
            self.end_headers()
        else:
            self._send_json({"message": "请访问 /login 进行登录"})

    def api_auth_token(self) -> None:
        if self._rate_limited("refresh"):
            return
        body = self._read_json()
        grant_type = body.get("grant_type", "")

        if grant_type == "password":
            username = (body.get("username") or "").strip()
            password = body.get("password", "")
            if not username or not password:
                self._send_json({"error": "invalid_request"}, 400)
                return
            user = auth.authenticate_user(username, password)
            if not user:
                self._send_json({"error": "invalid_grant"}, 401)
                return
            auth.revoke_all_sessions(user["id"])
            session = auth.create_session(
                user, ip=self._get_client_ip(),
                ua=self.headers.get("User-Agent", ""))  # pyright: ignore[reportAttributeAccessIssue]
            self._send_json(session)

        elif grant_type == "refresh_token":
            refresh_token = body.get("refresh_token", "")
            result = auth.refresh_session(refresh_token, ip=self._get_client_ip())
            if not result:
                self._send_json({"error": "invalid_grant"}, 401)
                return
            self._send_json(result)

        else:
            self._send_json({"error": "unsupported_grant_type"}, 400)

    def api_auth_change_password(self) -> None:
        if self._rate_limited("change_password"):
            return
        if not self._require_auth():
            return
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        assert user is not None
        body = self._read_json()
        old_pw = body.get("old_password", "")
        new_pw = body.get("new_password", "")
        if not old_pw or not new_pw:
            self._send_json({"error": "bad_request", "message": "缺少密码"}, 400)
            return
        if len(new_pw) < 6:
            self._send_json({"error": "weak_password", "message": "新密码至少 6 位"}, 400)
            return
        ok = auth.update_user_password(user["sub"], old_pw, new_pw)
        if ok:
            self._send_json({"ok": True, "message": "密码已修改"})
        else:
            self._send_json({"error": "wrong_password", "message": "原密码错误"}, 400)

    # ---- 用户管理（管理员）----
    def api_auth_users(self) -> None:
        if not self._require_permission("admin:users"):
            return
        self._send_json({"users": auth.list_users()})

    def api_auth_events(self) -> None:
        if not self._require_permission("admin:config"):
            return
        self._send_json({"events": auth.get_recent_events(50)})

    def api_auth_user_detail(self, user_id: int) -> None:
        if not self._require_permission("admin:users"):
            return
        user = auth.get_user_by_id(user_id)
        if not user:
            self._send_json({"error": "not_found", "message": "用户不存在"}, 404)
            return
        self._send_json({"user": user})

    def api_auth_users_post(self) -> None:
        if not self._require_permission("admin:users"):
            return
        me = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        assert me is not None
        body = self._read_json()
        action = body.get("action", "create")

        if action == "create":
            username = (body.get("username") or "").strip()
            # 不给缺省口令：省略 password 时若静默填 admin123，会创建出
            # 「公开文档里写着密码」的账号。之前依赖 admin.js 前端校验，
            # 接口层没兜住，脚本/CSRF 调用可直接绕过。
            password = body.get("password")
            if not isinstance(password, str) or not password:
                self._send_json({"error": "bad_request", "message": "请提供初始密码"}, 400)
                return
            display_name = body.get("display_name", "")
            email = (body.get("email") or "").strip()
            role = body.get("role", "user")
            permissions = body.get("permissions", [])
            if not username:
                self._send_json({"error": "bad_request", "message": "用户名不能为空"}, 400)
                return
            if len(password) < 6:
                self._send_json({"error": "weak_password", "message": "密码至少 6 位"}, 400)
                return
            ok = auth.create_user(username, password, display_name, role, permissions,
                                  email=email)
            if ok:
                self._send_json({"ok": True, "message": f"用户 {username} 已创建"})
                auth._log_event(me["sub"], me["username"], "user_created",
                                f"创建用户: {username}", self._get_client_ip())
            else:
                self._send_json({"error": "conflict", "message": "用户名已存在"}, 409)

        elif action == "delete":
            raw_uid = body.get("id")
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                self._send_json({"error": "bad_request", "message": "用户 ID 无效"}, 400)
                return
            if uid == int(me["sub"]):
                self._send_json({"error": "bad_request",
                                 "message": "不能删除当前登录账户"}, 400)
                return
            ok = auth.delete_user(uid)
            if ok:
                self._send_json({"ok": True, "message": f"用户 {uid} 已删除"})
            else:
                self._send_json({"error": "not_found", "message": "用户不存在"}, 404)

        elif action == "update":
            try:
                uid = int(body.get("id"))
            except (TypeError, ValueError):
                self._send_json({"error": "bad_request", "message": "用户 ID 无效"}, 400)
                return
            display_name = body.get("display_name")
            email = body.get("email")
            role = body.get("role")
            enabled = body.get("enabled")
            new_password = body.get("new_password")
            if new_password and len(str(new_password)) < 6:
                self._send_json({"error": "weak_password", "message": "密码至少 6 位"}, 400)
                return
            conn = auth.sqlite3.connect(auth.USERS_DB_PATH)
            updates = []
            params: list[object] = []
            if display_name is not None:
                updates.append("display_name=?")
                params.append(display_name)
            if email is not None:
                updates.append("email=?")
                params.append(email)
            if role is not None:
                updates.append("role=?")
                params.append(role)
            if enabled is not None:
                updates.append("enabled=?")
                params.append(1 if enabled else 0)
            if updates:
                updates.append("updated_at=datetime('now')")
                params.append(uid)
                conn.execute(f"UPDATE users SET {','.join(updates)} WHERE id=?", params)
                conn.commit()
            conn.close()
            # 密码重置走同一条请求（前端以前发两次请求，第二次可能因网络失败而漏改密码）
            if new_password:
                auth.admin_reset_password(int(uid), str(new_password))
            self._send_json({"ok": True, "message": f"用户 {uid} 已更新"})

        elif action == "reset_password":
            try:
                uid = int(body.get("id"))
            except (TypeError, ValueError):
                self._send_json({"error": "bad_request", "message": "用户 ID 无效"}, 400)
                return
            new_pw = body.get("new_password")
            # 同上：重置密码不能回落到 admin123，必须由调用方显式给出新口令
            if not isinstance(new_pw, str) or not new_pw:
                self._send_json({"error": "bad_request", "message": "请提供新密码"}, 400)
                return
            if len(new_pw) < 6:
                self._send_json({"error": "weak_password", "message": "密码至少 6 位"}, 400)
                return
            ok = auth.admin_reset_password(uid, new_pw)
            if ok:
                self._send_json({"ok": True, "message": f"用户 {uid} 密码已重置"})
            else:
                self._send_json({"error": "not_found", "message": "用户不存在"}, 404)

        else:
            self._send_json({"error": "bad_request", "message": f"未知操作: {action}"}, 400)

    # ---- 注册 / 用户名探测 ----
    def api_auth_check_username(self, qs: dict[str, list[str]]) -> None:
        if self._rate_limited("checkuser"):
            return
        username = (qs.get("username") or [""])[0].strip().lower()
        if not username or len(username) < 2:
            self._send_json({"available": False, "reason": "too_short"})
            return
        conn = auth.sqlite3.connect(auth.USERS_DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE LOWER(username)=? LIMIT 1", (username,))
        row = cur.fetchone()
        conn.close()
        self._send_json({"available": row is None})

    def api_auth_register(self) -> None:
        if self._rate_limited("register"):
            return
        body = self._read_json()
        username = (body.get("username") or "").strip()
        password = body.get("password", "")
        display_name = body.get("display_name", "") or username
        email = (body.get("email") or "").strip()

        if not username:
            self._send_json({"error": "bad_request", "message": "用户名不能为空"}, 400)
            return
        if len(username) < 2:
            self._send_json({"error": "bad_request", "message": "用户名至少 2 个字符"}, 400)
            return
        if len(password) < 6:
            self._send_json({"error": "weak_password", "message": "密码至少 6 位"}, 400)
            return
        # 简单邮箱校验
        if email and ("@" not in email or "." not in email):
            self._send_json({"error": "bad_request", "message": "邮箱格式不正确"}, 400)
            return

        ok = auth.create_user(username, password, display_name, role="user",
                              permissions=None)
        if not ok:
            self._send_json({"error": "conflict", "message": "用户名已存在"}, 409)
            return

        # 更新邮箱并直接设为已验证
        conn = auth.sqlite3.connect(auth.USERS_DB_PATH)
        if email:
            conn.execute("UPDATE users SET email=?, email_verified=1, "
                         "email_verify_token=NULL WHERE username=?", (email, username))
        else:
            conn.execute("UPDATE users SET email_verified=1 WHERE username=?", (username,))
        conn.commit()
        conn.close()

        auth._log_event(0, username, "register", f"新用户注册: {username}",
                        self._get_client_ip())
        self._send_json({"ok": True, "message": f"注册成功，欢迎 {username}！"}, 201)

    # ---- 路径参数路由 ----
    def route_user_detail(self, path: str) -> bool:
        """处理 /api/auth/users/<id>；返回 False 表示不是该路由。"""
        if not path.startswith(USER_DETAIL_PREFIX):
            return False
        tail = path[len(USER_DETAIL_PREFIX):]
        try:
            uid = int(tail)
        except ValueError:
            self.send_error(404)
            return True
        self.api_auth_user_detail(uid)
        return True
