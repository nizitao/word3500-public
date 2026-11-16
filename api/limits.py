#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""限流 / 配额：进程内滑动窗口 + SSE 长连接配额（零第三方依赖）。

从 `server.py` 拆出的独立模块，供 HTTP 骨架与各路由模块共用，
避免路由模块为了限流再反向 import 服务器主模块。
"""
import threading
import time


class RateLimiter:
    """按「类别 + 客户端 IP」限制单位时间内的请求次数。

    用于注册、用户名探测、在线心跳、游客登录等无认证或可被刷的端点。
    Cloudflare 后取的是 CF-Connecting-IP，即真实访客 IP，不会被轻易伪造。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, bucket: str, key: str, limit: int, window: float) -> bool:
        now = time.time()
        full_key = bucket + "|" + key
        with self._lock:
            arr = self._hits.get(full_key)
            if arr is None:
                arr = []
                self._hits[full_key] = arr
            cutoff = now - window
            if arr and arr[0] < cutoff:
                del arr[: sum(1 for t in arr if t < cutoff)]
            if len(arr) >= limit:
                return False
            arr.append(now)
            # 防止字典无限膨胀：桶数量过多时清掉已空的
            if len(self._hits) > 20000:
                for k in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(k, None)
            return True

    def reset(self) -> None:
        """清空所有计数（测试或多环境切换用）。"""
        with self._lock:
            self._hits.clear()


RATE = RateLimiter()

# 各类别的 (次数上限, 时间窗秒数)
# 阈值需容忍「校园网 / 机房 NAT 多人共享同一出口 IP」：一个班 30-50 人同时操作
# 会共用 CF-Connecting-IP，限得太紧会误伤整班。因此取「足以阻断脚本化批量攻击、
# 又不影响正常上课」的宽档位；单账户维度另有登录失败锁定兜底。
RATE_RULES = {
    "register": (10, 600),       # 注册：每 IP 10 分钟 10 次
    "checkuser": (60, 60),       # 用户名探测：每分钟 60 次（防批量枚举）
    "ping": (600, 60),           # 在线心跳：每分钟 600 次（NAT 下几十人轮询仍够用）
    "guest": (30, 300),          # 游客登录：5 分钟 30 次
    "login": (60, 300),          # 登录：5 分钟 60 次（配合账户级锁定防爆破）
    "study_event": (120, 60),    # 学习事件上报：每分钟 120 次（兼容自然答题节奏）
    "together": (60, 60),        # 一起背房间写操作：每分钟 60 次（轮询走 GET 不占此桶）
    # 其余写接口：以前只有上面 7 个桶，logout/refresh/改密/收藏夹/补签/rebuild
    # 都能被脚本无限刷。阈值同样按校园网 NAT 共享出口 IP 放宽。
    "logout": (120, 60),         # 登出 / 单点注销
    "refresh": (180, 60),        # Token 续期（前端每 25 分钟一次，留足余量）
    "change_password": (20, 300),  # 改密 / 密码模式换 Token
    "collections": (180, 60),    # 收藏夹：建/改名/删/勾选
    "makeup": (20, 300),         # 补签（业务本身每月限 2 次）
    "rebuild": (5, 600),         # 全量重算：最贵，10 分钟 5 次
}

# 首启随机口令未改之前，只允许这些接口通过鉴权（前端据此把用户留在改密页）
SETUP_ALLOWED_PATHS = frozenset({
    "/api/auth/change-password",
    "/api/auth/logout",
    "/api/auth/refresh",
    "/api/auth/me",
    "/api/auth/userinfo",
    "/api/auth/slo",
})

# SSE 长连接配额：/api/online/stream 是公开端点且每条连接常驻一个线程，
# 不设上限时脚本批量建连能把线程池打满。按 IP + 全局双重限额。
SSE_PER_IP = 4
SSE_GLOBAL = 200
_SSE_LOCK = threading.Lock()
_SSE_BY_IP: dict[str, int] = {}
_SSE_TOTAL = 0


def sse_acquire(ip: str) -> bool:
    global _SSE_TOTAL
    with _SSE_LOCK:
        if _SSE_TOTAL >= SSE_GLOBAL or _SSE_BY_IP.get(ip, 0) >= SSE_PER_IP:
            return False
        _SSE_BY_IP[ip] = _SSE_BY_IP.get(ip, 0) + 1
        _SSE_TOTAL += 1
        return True


def sse_release(ip: str) -> None:
    global _SSE_TOTAL
    with _SSE_LOCK:
        left = _SSE_BY_IP.get(ip, 0) - 1
        if left > 0:
            _SSE_BY_IP[ip] = left
        else:
            _SSE_BY_IP.pop(ip, None)
        _SSE_TOTAL = max(0, _SSE_TOTAL - 1)
