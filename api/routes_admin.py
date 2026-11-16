#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""路由：管理员报表与运维工具。"""
import sys

from api import analytics, study

GET_ROUTES = {
    "/api/admin/report": ("api_admin_report", True),
}

POST_ROUTES = {
    "/api/admin/rebuild-progress": ("api_admin_rebuild_progress", False),
}


class AdminRoutes:
    def api_admin_report(self, qs: dict[str, list[str]]) -> None:
        """GET /api/admin/report — 访客、趋势、行为分布（按本地日期范围）"""
        if not self._require_permission("admin:users"):
            return
        from_str = (qs.get("from") or [""])[0]
        to_str = (qs.get("to") or [""])[0]
        all_time = (qs.get("all") or [""])[0] in ("1", "true", "yes")
        try:
            tz = int((qs.get("tz") or ["0"])[0])
        except (TypeError, ValueError):
            tz = 0
        try:
            self._send_json(analytics.report(from_str, to_str, tz, all_time))
        except Exception as e:
            print(f"[admin] report_failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            self._send_json({"ok": False, "error": "report_failed",
                             "message": "报表生成失败，请稍后重试"}, 500)

    def api_admin_rebuild_progress(self) -> None:
        """POST /api/admin/rebuild-progress — 从事件流全量重算（自愈工具）

        重算 learn_progress(含 next_review_at) / daily_stats / 徽章，
        与实时写入共用同一套派生函数，详见 api/study.rebuild_progress。
        """
        if self._rate_limited("rebuild"):
            return
        if not self._require_permission("admin:users"):
            return
        body = self._read_json()
        uid = body.get("user_id")
        try:
            target = int(uid) if uid not in (None, "", 0) else None
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "bad_user_id"}, 400)
            return
        try:
            self._send_json(study.rebuild_progress(target))
        except Exception as e:
            print(f"[admin] rebuild_failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            self._send_json({"ok": False, "error": "rebuild_failed",
                             "message": "重算失败，请查看服务端日志"}, 500)
