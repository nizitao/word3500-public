#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""路由：学习统计 / 事件上报 / 补签 / 徽章 / 排行榜。

所有接口的 user_id 一律取自服务端鉴权信息（`_study_uid()`），不接受请求体传入，
这样 `/api/study/*` 不存在越权读取他人进度的入口。
"""
import sys

from api import study

GET_ROUTES = {
    "/api/study/summary": ("api_study_summary", True),
    "/api/study/calendar": ("api_study_calendar", True),
    "/api/study/day": ("api_study_day", True),
    "/api/study/mastery-trend": ("api_study_mastery_trend", True),
    "/api/study/pos-stats": ("api_study_pos_stats", False),
    "/api/study/book-stats": ("api_study_book_stats", False),
    "/api/study/review": ("api_study_review", True),
    "/api/study/review/queue": ("api_study_review_queue", True),
    "/api/badges": ("api_badges", False),
    "/api/leaderboard": ("api_leaderboard", True),
}

POST_ROUTES = {
    "/api/study/events": ("api_study_events", False),
    "/api/study/makeup": ("api_study_makeup", False),
}


class StudyRoutes:
    # ---- 公共前置 ----
    def _study_uid(self) -> int:
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        return int(user["sub"]) if user else 0

    def _study_guard(self) -> bool:
        """登录校验 + 游客演示态。返回 True 表示已响应，调用方应直接 return。"""
        if not self._require_auth():
            return True
        if self._is_guest():
            self._send_json(
                {"ok": True, "guest": True,
                 "message": "游客模式不保存学习记录，注册后可累计打卡与徽章"}, 200)
            return True
        return False

    # ---- 写入 ----
    def api_study_events(self) -> None:
        """POST /api/study/events — 唯一写入口，服务端重算 master / next_review_at"""
        if not self._require_auth():
            return
        if self._is_guest():
            self._send_json(
                {"ok": False, "guest": True, "error": "guest",
                 "message": "游客学习记录不落库，注册后可保存进度"}, 403)
            return
        if self._rate_limited("study_event"):
            return
        body = self._read_json()
        try:
            tz = int(body.get("tz") or 0)
        except (TypeError, ValueError):
            tz = 0
        try:
            res = study.record_events(self._study_uid(), body.get("events"), tz)
        except Exception as e:
            # 不回传 str(e)：可能带出 SQLite 表结构与列名
            print(f"[study] record_events_failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            self._send_json({"ok": False, "error": "record_failed",
                             "message": "记录失败，请稍后重试"}, 500)
            return
        self._send_json(res, 200 if res.get("ok") else 400)

    def api_study_makeup(self) -> None:
        """POST /api/study/makeup — 补签（每月 2 次，不计入连续天数）"""
        if self._rate_limited("makeup"):
            return
        if not self._require_auth():
            return
        if self._is_guest():
            self._send_json({"ok": False, "guest": True, "error": "guest",
                             "message": "游客不能补签"}, 403)
            return
        body = self._read_json()
        try:
            tz = int(body.get("tz") or 0)
        except (TypeError, ValueError):
            tz = 0
        res = study.makeup(self._study_uid(), str(body.get("day") or ""), tz)
        self._send_json(res, 200 if res.get("ok") else 400)

    # ---- 读取 ----
    def api_study_summary(self, qs: dict[str, list[str]]) -> None:
        if self._study_guard():
            return
        self._send_json(study.summary(self._study_uid(), self._int_qs(qs, "tz")))

    def api_study_calendar(self, qs: dict[str, list[str]]) -> None:
        if self._study_guard():
            return
        month = (qs.get("month") or [""])[0]
        self._send_json(study.calendar(self._study_uid(), month,
                                       self._int_qs(qs, "tz")))

    def api_study_day(self, qs: dict[str, list[str]]) -> None:
        if self._study_guard():
            return
        day = (qs.get("day") or [""])[0]
        self._send_json(study.day_detail(self._study_uid(), day,
                                         self._int_qs(qs, "tz")))

    def api_study_mastery_trend(self, qs: dict[str, list[str]]) -> None:
        if self._study_guard():
            return
        self._send_json(study.mastery_trend(self._study_uid(),
                                            self._int_qs(qs, "days", 30),
                                            self._int_qs(qs, "tz")))

    def api_study_pos_stats(self) -> None:
        if self._study_guard():
            return
        self._send_json(study.pos_stats(self._study_uid()))

    def api_study_book_stats(self) -> None:
        if self._study_guard():
            return
        self._send_json(study.book_stats(self._study_uid()))

    def api_study_review(self, qs: dict[str, list[str]]) -> None:
        if self._study_guard():
            return
        self._send_json(study.review_panel(self._study_uid(),
                                           self._int_qs(qs, "tz")))

    def api_study_review_queue(self, qs: dict[str, list[str]]) -> None:
        if self._study_guard():
            return
        self._send_json(study.review_queue(self._study_uid(),
                                           self._int_qs(qs, "limit", 20),
                                           self._int_qs(qs, "tz")))

    def api_badges(self) -> None:
        if self._study_guard():
            return
        self._send_json(study.badges(self._study_uid()))

    def api_leaderboard(self, qs: dict[str, list[str]]) -> None:
        if self._study_guard():
            return
        self._send_json(study.leaderboard(
            (qs.get("metric") or ["words"])[0],
            (qs.get("range") or ["week"])[0],
            self._int_qs(qs, "tz"),
            self._int_qs(qs, "limit", 50),
        ))
