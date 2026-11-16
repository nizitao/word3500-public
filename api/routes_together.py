#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""路由：「一起背」学习房间（读列表/详情 + 写房间）。

房间成员关系、点赞权限等业务规则在 api/together.py；这里只做鉴权、
限流与「异常转 JSON」的传输层收敛。
"""
import sys
from typing import Any, Callable

from api import together

GET_ROUTES = {
    "/api/together/rooms": ("api_together_rooms", True),
    "/api/together/room": ("api_together_room", True),
    "/api/together/scopes": ("api_together_scopes", False),
}

POST_ROUTES = {
    "/api/together/create": ("api_together_create", False),
    "/api/together/join": ("api_together_join", False),
    "/api/together/leave": ("api_together_leave", False),
    "/api/together/like": ("api_together_like", False),
    "/api/together/dissolve": ("api_together_dissolve", False),
}


class TogetherRoutes:
    def _together_guard(self, write: bool = False) -> bool:
        """登录校验 + 游客拦截。返回 True 表示已响应，调用方应直接 return。"""
        if not self._require_auth():
            return True
        if self._is_guest():
            if write:
                self._send_json(
                    {"ok": False, "guest": True, "error": "guest",
                     "message": "游客不能参与房间，注册后即可组队学习"}, 403)
            else:
                self._send_json(
                    {"ok": True, "guest": True, "rooms": [],
                     "message": "游客模式不保存学习记录，注册后可加入房间"}, 200)
            return True
        return False

    def _together_body(self) -> Any:
        """写接口统一前置：鉴权 → 限流 → 请求体。返回 None 表示已响应。"""
        if self._together_guard(write=True):
            return None
        if self._rate_limited("together"):
            return None
        return self._read_json()

    def _together_call(self, fn: Callable[..., Any], *args: Any) -> None:
        """数据层异常统一转成 JSON 500，避免 HTML 错误页破坏前端解析。"""
        try:
            res = fn(*args)
        except Exception as e:
            # str(e) 会带出 SQLite 列名 / 约束名 / 表结构，只写服务端日志
            print(f"[together] handler_failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            self._send_json({"ok": False, "error": "together_failed",
                             "message": "操作失败，请稍后重试"}, 500)
            return
        self._send_json(res, 200 if res.get("ok") else 400)

    def _together_tz(self, body: dict[str, Any]) -> int:
        try:
            return int(body.get("tz") or 0)
        except (TypeError, ValueError):
            return 0

    def api_together_rooms(self, qs: dict[str, list[str]]) -> None:
        if self._together_guard():
            return
        self._together_call(together.list_rooms, self._study_uid(),
                            self._int_qs(qs, "tz"))

    def api_together_room(self, qs: dict[str, list[str]]) -> None:
        if self._together_guard():
            return
        self._together_call(together.room_detail, self._study_uid(),
                            (qs.get("code") or [""])[0], self._int_qs(qs, "tz"))

    def api_together_scopes(self) -> None:
        if self._together_guard():
            return
        self._together_call(together.scope_options)

    def api_together_create(self) -> None:
        body = self._together_body()
        if body is None:
            return
        self._together_call(together.create_room, self._study_uid(),
                            body.get("name"), body.get("book"), body.get("unit"))

    def api_together_join(self) -> None:
        body = self._together_body()
        if body is None:
            return
        self._together_call(together.join_room, self._study_uid(), body.get("code"))

    def api_together_leave(self) -> None:
        body = self._together_body()
        if body is None:
            return
        self._together_call(together.leave_room, self._study_uid(), body.get("code"))

    def api_together_like(self) -> None:
        body = self._together_body()
        if body is None:
            return
        self._together_call(together.like_member, self._study_uid(),
                            body.get("code"), body.get("to_user_id"),
                            self._together_tz(body))

    def api_together_dissolve(self) -> None:
        body = self._together_body()
        if body is None:
            return
        self._together_call(together.dissolve_room, self._study_uid(),
                            body.get("code"))
