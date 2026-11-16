#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""访问统计 / 在线人数模块（仅 Python 标准库，零第三方依赖）。

职责：
  1. access_log：记录每一次页面浏览、登录、注册、登出与接口操作，作为报表唯一数据源。
  2. OnlineTracker：基于 SSE 连接与心跳维护在线访客集合，连接建立 / 断开即时更新。
  3. report()：按本地时区的日期范围聚合出访客数、趋势、行为分布。

时间戳统一存 SQLite `datetime('now')`（UTC，格式 YYYY-MM-DD HH:MM:SS），
报表用 `datetime(ts, '+N minutes')` 换算到浏览器本地时区后再按天分组。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlparse

from api import auth

USERS_DB_PATH = auth.USERS_DB_PATH

# 事件类型 → 中文标签（顺序即报表展示顺序）
EVENT_LABELS = {
    "view": "浏览页面",
    "operation": "功能操作",
    "login": "登录",
    "register": "注册",
    "logout": "登出",
    "failed_login": "登录/注册失败",
}

# 基础设施接口不计入「功能操作」，否则心跳、快照与报表查询会淹没真实行为。
# 注意用 "/api/online"（不带斜杠）才能同时覆盖 /api/online 与 /api/online/stream|ping|leave
_EXCLUDED_API_PREFIXES = ("/api/online", "/api/admin/report")

# 站内「游客体验」是数据库里的真实账户（见 server.api_auth_guest_login），
# 前端 app.js 的 _isGuest 也按 username 判定；在线统计必须与之保持同一口径。
GUEST_USERNAME = "guest"

# auth_events → access_log 的历史数据回填映射
_BACKFILL_MAP = {
    "login": "login",
    "register": "register",
    "logout": "logout",
    "slo": "logout",
    "failed": "failed_login",
    "lockout": "failed_login",
    "kickout": "operation",
    "refresh": "operation",
    "user_created": "operation",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(USERS_DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init() -> None:
    """建表 + 索引 + 首次历史回填。可重复调用，幂等。"""
    conn = _connect()
    try:
        # WAL 让「每请求一条日志」与会话读写并发时不易锁冲突
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            """CREATE TABLE IF NOT EXISTS access_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,          -- UTC
                event_type  TEXT NOT NULL,          -- view/operation/login/register/logout/failed_login
                path        TEXT DEFAULT '',
                user_id     INTEGER DEFAULT 0,
                username    TEXT DEFAULT '',
                ip          TEXT DEFAULT '',
                user_agent  TEXT DEFAULT '',
                status      INTEGER DEFAULT 200,
                success     INTEGER DEFAULT 1
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_access_ts ON access_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_access_type_ts ON access_log(event_type, ts)")
        conn.commit()

        # 老版本只有 auth_events（安全日志）。首次升级时回填，使报表有历史登录/注册数据。
        count = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        if count == 0:
            try:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(auth_events)")}
                if {"created_at", "event_type", "user_id", "username", "ip_address"} <= columns:
                    case_sql = " ".join(
                        f"WHEN event_type='{k}' THEN '{v}'" for k, v in _BACKFILL_MAP.items()
                    )
                    conn.execute(
                        "INSERT INTO access_log(ts, event_type, path, user_id, username, ip, status, success) "
                        "SELECT created_at, CASE " + case_sql + " ELSE 'operation' END, "
                        "'', COALESCE(user_id,0), COALESCE(username,''), COALESCE(ip_address,''), 200, 1 "
                        "FROM auth_events WHERE created_at IS NOT NULL"
                    )
                    conn.commit()
            except sqlite3.OperationalError:
                pass  # auth_events 不存在（全新安装）→ 无历史可回填
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 请求日志：由 Handler.send_response() 统一调用，一次响应恰好一条
# ---------------------------------------------------------------------------
def _identity(handler: Any) -> tuple[int, str]:
    user = getattr(handler, "_auth_user", None)
    if user:
        try:
            return int(user.get("sub") or 0), str(user.get("username") or "")
        except (TypeError, ValueError):
            return 0, ""
    token = handler.headers.get("X-Auth-Token") or auth.extract_bearer_token(handler.headers) or ""
    payload = auth.decode_token_unsafe(token) if token else None
    if payload:
        try:
            return int(payload.get("sub") or 0), str(payload.get("username") or "")
        except (TypeError, ValueError):
            return 0, ""
    return 0, ""


def _classify(path: str, status: int) -> Optional[str]:
    ok = 200 <= status < 300
    if path in ("/api/auth/login", "/api/auth/guest-login"):
        return "login" if ok else "failed_login"
    if path == "/api/auth/register":
        return "register" if ok else "failed_login"
    if path in ("/api/auth/logout", "/api/auth/slo"):
        return "logout" if ok else "operation"
    return "operation"


def record(handler: Any, status: int) -> None:
    """记录一次请求。任何异常都不能影响正常响应。"""
    try:
        path = urlparse(handler.path).path
        if path.startswith(_EXCLUDED_API_PREFIXES):
            return
        if not path.startswith("/api/"):
            # 只统计 HTML 页面（含无扩展名路由），CSS/JS/图片/字体不算一次浏览。
            # 304 是浏览器对 HTML 的协商缓存重验证，用户确实又访问了一次页面，
            # 必须计入浏览，否则刷新页面会被漏统计。
            ext = os.path.splitext(path)[1].lower()
            if ext not in ("", ".html") or status not in (200, 304):
                return
            event_type = "view"
        else:
            event_type = _classify(path, status) or "operation"
        user_id, username = _identity(handler)
        ip = auth.extract_client_ip(handler)
        ua = (handler.headers.get("User-Agent") or "")[:200]
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO access_log(ts, event_type, path, user_id, username, ip, user_agent, status, success) "
                "VALUES (datetime('now'),?,?,?,?,?,?,?,?)",
                (event_type, path, user_id, username, ip, ua, int(status),
                 1 if 200 <= status < 300 else 0),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # 统计失败绝不影响业务


# ---------------------------------------------------------------------------
# 在线人数：SSE 连接建立 / 断开 + 心跳兜底 + 显式离开
# ---------------------------------------------------------------------------
class OnlineTracker:
    """一个 client_id（浏览器 localStorage 中的 UUID）= 一位在线访客。

    - connect/disconnect：SSE 长连接建立 / 断开时即时维护连接引用计数。
    - ping：不支持 SSE 或代理缓冲时的轮询心跳；也用于页面重新可见时立刻上线。
    - leave：pagehide/beforeunload 用 keepalive 显式下线，断开即时生效。
    - 超时清理：浏览器崩溃、断网等无法发送 leave 的场景，由 TTL 兜底。
    """

    TTL_SECONDS = 60       # 无 SSE、仅靠 ping 保活的客户端超时
    GRACE_SECONDS = 6      # SSE 断线后的宽限期（EventSource 默认约 3s 自动重连）
    LEAVE_GRACE = 2.0      # 显式 leave 后的移除延迟（秒）

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        # cid -> {user_id, username, ip, conns:set, last_seen, since}
        self._clients: dict[str, dict[str, Any]] = {}
        self._version = 0
        self._reaper_started = False

    def start_reaper(self) -> None:
        """后台清理线程：每秒跑一次 snapshot()，使 TTL / leave 到期能被及时发现。

        否则 _prune() 只在有人查询时才执行，SSE 客户端可能要等 15 秒心跳周期
        才看到「离线」变化。
        """
        with self._lock:
            if self._reaper_started:
                return
            self._reaper_started = True
        threading.Thread(target=self._reaper_loop, name="w3500-online-reaper",
                         daemon=True).start()

    def _reaper_loop(self) -> None:
        while True:
            time.sleep(1.0)
            try:
                self.snapshot()
            except Exception:
                pass

    def _notify(self) -> None:
        self._version += 1
        self._cond.notify_all()

    @staticmethod
    def _clean(meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "cid": str(meta.get("cid") or "")[:64],
            "uid": _safe_int(meta.get("uid")),
            "username": str(meta.get("username") or "")[:64],
        }

    def _upsert(self, meta: dict[str, Any], ip: str = "") -> str:
        info = self._clean(meta)
        cid = info["cid"]
        if not cid:
            return ""
        now = time.time()
        with self._cond:
            entry = self._clients.get(cid)
            if entry is None:
                entry = {"conns": set(), "since": now, "ip": ip, "leave_at": 0.0}
                self._clients[cid] = entry
            # 匿名事件（如 SSE 建连）不带身份，不能用它把已认证的身份覆盖掉；
            # 身份只由服务端校验过会话的 ping 写入。
            if info["uid"] <= 0 and _safe_int(entry.get("uid")) > 0:
                info = {"cid": cid, "uid": entry["uid"], "username": entry.get("username", "")}
            # 重新连接 / 心跳会取消此前排队的 leave，保证站内跳转人数不闪断
            entry.update({**info, "last_seen": now, "leave_at": 0.0})
            if ip:
                entry["ip"] = ip
            self._notify()
        return cid

    def connect(self, meta: dict[str, Any], ip: str, conn_id: str) -> str:
        cid = self._upsert(meta, ip)
        if cid:
            with self._cond:
                self._clients[cid]["conns"].add(conn_id)
                self._notify()
        return cid

    def disconnect(self, cid: str, conn_id: str) -> None:
        if not cid:
            return
        with self._cond:
            entry = self._clients.get(cid)
            if not entry:
                return
            entry["conns"].discard(conn_id)
            if entry["conns"]:
                entry["last_seen"] = time.time()
            else:
                # 最后一个 SSE 连接断开：把 last_seen 回拨，使该访客在 GRACE_SECONDS
                # 后由 _prune 清除。既给跨页跳转/自动重连留出宽限（人数不闪断），
                # 又不会像原来的 60s 那样让已关闭的标签页长期虚占在线人数。
                entry["last_seen"] = time.time() - (self.TTL_SECONDS - self.GRACE_SECONDS)
            self._notify()

    def ping(self, meta: dict[str, Any], ip: str = "") -> dict[str, Any]:
        self._upsert(meta, ip)
        return self.snapshot()

    def leave(self, meta: dict[str, Any]) -> dict[str, Any]:
        cid = self._clean(meta)["cid"]
        if cid:
            with self._cond:
                entry = self._clients.get(cid)
                if entry is not None:
                    if len(entry["conns"]) > 1:
                        # 同一浏览器多个标签页共用 cid：只关掉其中一个标签，
                        # 不能把整位访客下线；剩余连接由 disconnect 自然回收。
                        entry["last_seen"] = time.time()
                        entry["leave_at"] = 0.0
                    else:
                        # 不立即 pop：pagehide 在同站跳转时也会触发，若立刻删除会出现
                        # 「掉 1 再涨 1」的闪断。排队 2 秒移除，期间新页面用同一 cid
                        # 重连会取消移除；真正关闭标签页则 2 秒后由 _prune 清除。
                        entry["conns"] = set()
                        entry["leave_at"] = time.time() + self.LEAVE_GRACE
                    self._notify()
        return self.snapshot()

    def touch(self, cid: str) -> bool:
        """SSE 心跳保活，不触发版本变化，避免推送自激循环。"""
        if not cid:
            return False
        with self._lock:
            entry = self._clients.get(cid)
            if not entry:
                return False
            # 仍有活跃 SSE 连接时取消待执行的 leave（例如同站跳转的旧页面
            # 发出了 leave，但新页面已用同一 cid 连上）。
            if entry.get("leave_at", 0.0) and entry["conns"]:
                entry["leave_at"] = 0.0
            entry["last_seen"] = time.time()
            return True

    def _prune(self) -> None:
        now = time.time()
        # TTL 对所有访客生效（不只无连接者）：SSE 线程靠每 2 秒 touch() 续期，
        # 轮询客户端靠 ping() 续期。若某个 SSE 线程卡死 / 对端静默消失且没有触发
        # BrokenPipe，最多 60 秒也会被清除，不会出现「永久在线」的幽灵访客。
        stale = [cid for cid, e in self._clients.items()
                 if (e.get("leave_at", 0.0) and now >= e["leave_at"])
                 or (now - e["last_seen"] > self.TTL_SECONDS)]
        for cid in stale:
            self._clients.pop(cid, None)
        if stale:
            self._notify()

    @staticmethod
    def _is_guest(entry: dict[str, Any]) -> bool:
        """游客 = 未登录(uid<=0) 或站内「游客体验」账户。

        游客体验账户在数据库里有真实 uid，若只按 uid>0 判定会被算成
        「已登录用户」，导致面板游客数偏小、users 里还错误列出 guest。
        """
        if _safe_int(entry.get("uid")) <= 0:
            return True
        return auth.is_guest_username(entry.get("username"))

    def snapshot(self, include_users: bool = False) -> dict[str, Any]:
        """在线快照。

        include_users=False（默认）时不返回在线用户名——该接口是公开的，
        任何未登录访客都能轮询；泄露「谁在什么时候在线」属于用户隐私泄露。
        仅管理员视角才带上用户名列表。
        """
        with self._cond:
            self._prune()
            clients = list(self._clients.values())
            online = len(clients)
            guests = sum(1 for e in clients if self._is_guest(e))
            logged_clients = online - guests
            snap = {
                "online": online,
                # logged_in 与 guests 同为「访客(client)数」口径，二者恒等于 online，
                # 避免面板出现「已登录 + 游客 ≠ 在线总数」。
                "logged_in": logged_clients,
                # 按 uid 去重的真实账号数（同一账号多浏览器只算 1），供报表等场景使用
                "logged_users": len({e["uid"] for e in clients if not self._is_guest(e)}),
                "guests": guests,
                "users": [],
                "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + "Z",
                "ttl": self.TTL_SECONDS,
                "version": self._version,
            }
            if include_users:
                snap["users"] = sorted({
                    str(e["username"]) for e in clients
                    if not self._is_guest(e) and e.get("username")
                })[:10]
            return snap

    def wait_for_change(self, last_version: int, timeout: float = 15.0) -> int:
        with self._cond:
            if self._version == last_version:
                self._cond.wait(timeout)
            return self._version


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


ONLINE = OnlineTracker()

# 模块导入即初始化（与 auth.init_auth_db() 一致），保证任何响应进来前表已存在
init()
# 启动超时清理，保证连接断开 / 页面关闭后在线人数及时回落
ONLINE.start_reaper()


# ---------------------------------------------------------------------------
# 报表聚合
# ---------------------------------------------------------------------------
def _local_modifier(tz_minutes: int) -> str:
    """SQLite 时间修饰符：UTC → 浏览器本地时间。tz=-getTimezoneOffset()，中国为 +480。"""
    try:
        tz = max(-840, min(840, int(tz_minutes)))
    except (TypeError, ValueError):
        tz = 0
    return f"{tz:+d} minutes"


def _utc_bound(local_day: date, tz_minutes: int) -> str:
    """本地日期 00:00:00 → UTC 时间字符串（半开区间用）。"""
    local = datetime(local_day.year, local_day.month, local_day.day)
    utc = local - timedelta(minutes=tz_minutes)
    return utc.strftime("%Y-%m-%d %H:%M:%S")


def _default_range(tz_minutes: int) -> tuple[date, date]:
    today_local = datetime.utcnow() + timedelta(minutes=tz_minutes)
    return today_local.date() - timedelta(days=6), today_local.date()


def report(from_str: str = "", to_str: str = "", tz_minutes: int = 0,
           all_time: bool = False) -> dict[str, Any]:
    """按本地日期范围（含首尾）聚合报表数据。"""
    try:
        tz = max(-840, min(840, int(tz_minutes)))
    except (TypeError, ValueError):
        tz = 0

    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        modifier = _local_modifier(tz)
        if all_time:
            row = conn.execute(
                "SELECT MIN(date(ts, ?)) AS d FROM access_log", (modifier,)
            ).fetchone()
            first = _parse_date(row["d"] if row else None)
            today = (datetime.utcnow() + timedelta(minutes=tz)).date()
            start = first or today
            end = today
        else:
            start = _parse_date(from_str) or _default_range(tz)[0]
            end = _parse_date(to_str) or _default_range(tz)[1]
        if start > end:
            start, end = end, start
        # 防止一次拉出过长区间拖垮页面
        if (end - start).days > 365:
            start = end - timedelta(days=365)

        start_ts = _utc_bound(start, tz)
        end_ts = _utc_bound(end + timedelta(days=1), tz)

        total = conn.execute(
            "SELECT COUNT(*) AS c, COUNT(DISTINCT NULLIF(ip,'')) AS uv, "
            "COUNT(DISTINCT CASE WHEN user_id>0 THEN user_id END) AS au "
            "FROM access_log WHERE ts>=? AND ts<?", (start_ts, end_ts)
        ).fetchone()

        by_type = {r["event_type"]: r["c"] for r in conn.execute(
            "SELECT event_type, COUNT(*) AS c FROM access_log "
            "WHERE ts>=? AND ts<? GROUP BY event_type", (start_ts, end_ts)
        )}
        views = by_type.get("view", 0)
        operations = by_type.get("operation", 0)

        # 每日趋势（本地日）
        daily_rows = conn.execute(
            "SELECT date(ts, ?) AS d, "
            "SUM(CASE WHEN event_type='view' THEN 1 ELSE 0 END) AS views, "
            "SUM(CASE WHEN event_type='operation' THEN 1 ELSE 0 END) AS operations, "
            "SUM(CASE WHEN event_type='login' THEN 1 ELSE 0 END) AS logins, "
            "SUM(CASE WHEN event_type='register' THEN 1 ELSE 0 END) AS registers, "
            "COUNT(DISTINCT NULLIF(ip,'')) AS visitors, "
            "COUNT(*) AS events "
            "FROM access_log WHERE ts>=? AND ts<? GROUP BY d",
            (modifier, start_ts, end_ts),
        ).fetchall()
        daily = {r["d"]: dict(r) for r in daily_rows}

        trend = []
        cursor = start
        while cursor <= end:
            key = cursor.isoformat()
            row = daily.get(key, {})
            trend.append({
                "date": key,
                "views": int(row.get("views") or 0),
                "operations": int(row.get("operations") or 0),
                "logins": int(row.get("logins") or 0),
                "registers": int(row.get("registers") or 0),
                "visitors": int(row.get("visitors") or 0),
                "events": int(row.get("events") or 0),
            })
            cursor += timedelta(days=1)

        # 行为分布（固定 6 类，0 也展示，避免图表误导）
        total_events = int(total["c"] or 0)
        behaviors = []
        for key, label in EVENT_LABELS.items():
            cnt = int(by_type.get(key, 0))
            behaviors.append({
                "type": key, "label": label, "count": cnt,
                "percent": round(cnt * 100.0 / total_events, 1) if total_events else 0.0,
            })

        top_pages = [dict(r) for r in conn.execute(
            "SELECT path, COUNT(*) AS count FROM access_log "
            "WHERE ts>=? AND ts<? AND event_type='view' AND path<>'' "
            "GROUP BY path ORDER BY count DESC LIMIT 8", (start_ts, end_ts)
        )]

        hourly = [0] * 24
        for r in conn.execute(
            "SELECT CAST(strftime('%H', datetime(ts, ?)) AS INTEGER) AS h, COUNT(*) AS c "
            "FROM access_log WHERE ts>=? AND ts<? GROUP BY h",
            (modifier, start_ts, end_ts),
        ):
            h = r["h"]
            if h is not None and 0 <= h < 24:
                hourly[h] = int(r["c"])

        user_total = 0
        try:
            user_total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        except sqlite3.OperationalError:
            pass

        snap = ONLINE.snapshot()
        return {
            "ok": True,
            "range": {
                "from": start.isoformat(), "to": end.isoformat(),
                "tz": tz, "start_utc": start_ts, "end_utc": end_ts,
                "days": len(trend),
            },
            "summary": {
                "visitors": int(total["uv"] or 0),
                "views": views,
                "operations": operations,
                "logins": int(by_type.get("login", 0)),
                "registers": int(by_type.get("register", 0)),
                "logouts": int(by_type.get("logout", 0)),
                "failed_logins": int(by_type.get("failed_login", 0)),
                "active_users": int(total["au"] or 0),
                "total_users": int(user_total),
                "total_events": total_events,
                "online_now": snap["online"],
                "avg_daily_views": round(views / len(trend), 1) if trend else 0,
            },
            "trend": trend,
            "behaviors": behaviors,
            "top_pages": top_pages,
            "hourly": hourly,
            "generated_at": snap["updated_at"],
        }
    finally:
        conn.close()


def _parse_date(value: Any) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
