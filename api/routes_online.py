#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""路由：在线人数（SSE 长连接 / 心跳 / 显式离开）+ 公开快照。"""
import json
import secrets

from api import analytics, auth, limits

GET_ROUTES = {
    "/api/online": ("api_online_snapshot", True),
    "/api/online/stream": ("api_online_stream", True),
}

POST_ROUTES = {
    "/api/online/ping": ("api_online_ping", False),
    "/api/online/leave": ("api_online_leave", False),
}


class OnlineRoutes:
    def api_online_snapshot(self, qs: dict[str, list[str]]) -> None:
        """GET /api/online — 在线人数快照（公开）。

        该接口公开可访问（未登录也能看在线人数），因此默认不返回在线用户名；
        管理员携带 Token 访问时才附带名单，避免用户名对任意人可见。
        """
        is_admin = False
        if self._check_auth() and self._auth_user:  # pyright: ignore[reportAttributeAccessIssue]
            is_admin = auth.check_permission(
                dict(self._auth_user), "admin:users")  # pyright: ignore[reportAttributeAccessIssue]
        self._send_json(analytics.ONLINE.snapshot(include_users=is_admin))

    def api_online_stream(self, qs: dict[str, list[str]]) -> None:
        """GET /api/online/stream — SSE 长连接，连接建立/断开即时更新在线集合。

        浏览器不支持 EventSource 时，客户端会自动退化为 /api/online/ping 轮询。

        安全说明：EventSource 无法自定义请求头，因此这里**只接受 cid**，
        不接受任何客户端声称的 uid / 用户名——身份一律由带 Token 的
        /api/online/ping 经服务端会话校验后写入，避免伪造「admin 在线」。
        """
        meta = {
            "cid": (qs.get("cid") or [""])[0],
            "uid": 0,
            "username": "",
        }
        ip = self._get_client_ip() or "unknown"
        if not limits.sse_acquire(ip):
            self._send_json({"error": "too_many_streams",
                             "message": "在线连接数过多，请稍后再试"}, 429)
            return
        conn_id = secrets.token_hex(8)
        cid = analytics.ONLINE.connect(meta, ip, conn_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._send_security_headers()
        self.end_headers()
        # 流结束后让服务器主动关闭连接，避免 handle_one_request 继续解析下一条请求
        self.close_connection = True  # pyright: ignore[reportAttributeAccessIssue]
        last_sent = -1
        try:
            while True:
                snap = analytics.ONLINE.snapshot()
                if snap["version"] != last_sent:
                    payload = ("event: online\ndata: " +
                               json.dumps(snap, ensure_ascii=False) + "\n\n").encode("utf-8")
                    last_sent = snap["version"]
                else:
                    # SSE 注释心跳：不触发前端更新，但能穿透代理缓冲，并让服务端
                    # 在数秒内发现对端已关闭（BrokenPipe → finally: disconnect）。
                    payload = b": keepalive\n\n"
                self.wfile.write(payload)  # pyright: ignore[reportAttributeAccessIssue]
                self.wfile.flush()  # pyright: ignore[reportAttributeAccessIssue]
                version = snap["version"]
                analytics.ONLINE.wait_for_change(version, 2.0)
                # 保活：不触发版本变化，避免推送自激循环；客户端已离开则退出
                if not analytics.ONLINE.touch(cid):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass  # 浏览器关闭 / 网络断开，属正常结束
        finally:
            analytics.ONLINE.disconnect(cid, conn_id)
            limits.sse_release(ip)   # 无论正常断开还是异常，配额都要归还

    def api_online_ping(self) -> None:
        """POST /api/online/ping — 心跳（SSE 不可用时的兜底）

        身份来自服务端会话校验（X-Auth-Token / Bearer），不采信请求体里的
        uid / username，防止任何人伪造「admin 在线」。
        """
        if self._rate_limited("ping"):
            return
        body = self._read_json()
        user = self._auth_user if self._check_auth() else None  # pyright: ignore[reportAttributeAccessIssue]
        meta = {
            "cid": str(body.get("cid") or "")[:64],
            "uid": (user or {}).get("sub") or 0,
            "username": (user or {}).get("username") or "",
        }
        self._send_json(analytics.ONLINE.ping(meta, self._get_client_ip()))

    def api_online_leave(self) -> None:
        """POST /api/online/leave — 页面卸载时显式下线"""
        body = self._read_json()
        self._send_json(analytics.ONLINE.leave(body))
