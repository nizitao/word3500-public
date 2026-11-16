#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""HTTP 处理器的公共骨架：静态文件、响应封装、鉴权与限流辅助。

从 `server.py` 拆出，供各路由模块（`routes_*.py`）以混入类方式复用。
这些方法都不关心具体业务，只提供「一个请求能安全地读写、鉴权、回 JSON」的能力。
"""
import json
import os
from datetime import timezone
from email.utils import formatdate, parsedate_to_datetime
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from api import analytics, auth, limits

# 静态资源扩展名白名单 → MIME 类型
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".mp3": "audio/mpeg",
}


class HttpMixin:
    """混入 BaseHTTPRequestHandler 的公共能力。

    约定：子类（Handler）在 `__init__` 里初始化 `_auth_user` 与 `_resp_status`，
    并提供 `WEB_DIR` / `AUTH_ENABLED` / `_SETUP_ALLOWED_PATHS` 等模块级配置的引用。
    """

    # 由 server.Handler 在类体里覆盖
    WEB_DIR: str = ""
    AUTH_ENABLED: bool = True
    SETUP_ALLOWED_PATHS: frozenset[str] = frozenset()

    # ---- 响应 ----
    def send_response(self, code: int, message: Optional[str] = None) -> None:
        """统一捕获状态码并落访问日志。

        所有响应（_send_json / _send_raw / _serve_file / send_error）都经过这里，
        因此一次请求恰好一条日志，且能拿到真实的成功/失败状态。
        """
        self._resp_status = code  # pyright: ignore[reportAttributeAccessIssue]
        super().send_response(code, message)  # pyright: ignore[reportArgumentType,reportCallIssue]
        try:
            analytics.record(self, code)
        except Exception:
            pass  # 统计异常绝不影响响应

    def log_message(self, format: str, *args: Any) -> None:  # 关闭默认访问日志
        pass

    def _send_security_headers(self) -> None:
        """基础安全响应头（同源应用，无需 CORS）"""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")

    def _send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        _ = self.wfile.write(data)  # pyright: ignore[reportAttributeAccessIssue]

    def _send_raw(self, payload: bytes, ctype: str, cache_control: str = "no-store",
                  status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self._send_security_headers()
        self.end_headers()
        _ = self.wfile.write(payload)  # pyright: ignore[reportAttributeAccessIssue]

    # ---- 请求 ----
    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))  # pyright: ignore[reportAttributeAccessIssue]
        except (TypeError, ValueError):
            return b""
        if length <= 0:
            return b""
        if length > 4 * 1024 * 1024:  # 4 MB 上限，防止超大请求打爆内存
            # 不读取请求体却复用 keep-alive 连接，会让残留字节被当成下一个请求解析。
            # 显式关闭连接，避免协议错乱。
            self.close_connection = True  # pyright: ignore[reportAttributeAccessIssue]
            return b""
        return self.rfile.read(length)  # pyright: ignore[reportAttributeAccessIssue]

    def _read_json(self) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny,reportAny]
        body = self._read_body()
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _get_client_ip(self) -> str:
        return auth.extract_client_ip(self)

    @property
    def _base_url(self) -> str:
        host = self.headers.get("Host", "localhost")  # pyright: ignore[reportAttributeAccessIssue]
        scheme = ("https" if self.headers.get("X-Forwarded-Proto") == "https"  # pyright: ignore[reportAttributeAccessIssue]
                  else "http")
        return f"{scheme}://{host}"

    # ---- 静态资源 ----
    def _serve_file(self, rel_path: str, ctype: str) -> None:
        full = os.path.normpath(os.path.join(self.WEB_DIR, rel_path))
        # 目录穿越防护：必须严格位于 WEB_DIR 之内
        try:
            inside = os.path.commonpath([full, self.WEB_DIR]) == self.WEB_DIR
        except ValueError:  # 跨盘符
            inside = False
        if not inside or not os.path.isfile(full):
            self.send_error(404)
            return
        try:
            st = os.stat(full)
        except OSError:
            self.send_error(404)
            return
        # 协商缓存：未改动直接 304，页面刷新不再重复传输 CSS/JS
        ims = self.headers.get("If-Modified-Since", "")  # pyright: ignore[reportAttributeAccessIssue]
        if ims:
            try:
                since = parsedate_to_datetime(ims)
                if since is not None:
                    if since.tzinfo is None:
                        since = since.replace(tzinfo=timezone.utc)
                    if int(st.st_mtime) <= int(since.timestamp()):
                        self.send_response(304)
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        return
            except (TypeError, ValueError):
                pass
        # 图片/字体/音频必须按字节原样输出，否则 open(..., encoding='utf-8')
        # 会在二进制数据上抛 UnicodeDecodeError，连接被直接掐断（浏览器拿到空响应）。
        text_exts = (".html", ".js", ".css", ".json", ".txt", ".svg")
        if rel_path.lower().endswith(text_exts):
            with open(full, "r", encoding="utf-8") as f:
                payload = f.read().encode("utf-8")
        else:
            with open(full, "rb") as f:
                payload = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self._send_security_headers()
        self.end_headers()
        _ = self.wfile.write(payload)  # pyright: ignore[reportAttributeAccessIssue]

    def _serve_static(self, url_path: str) -> bool:
        """通用静态资源服务：/foo → web/foo.html，/js/x.js → web/js/x.js。

        返回 True 表示已处理（含 404），False 表示不是静态资源请求。
        使用扩展名白名单，避免任意文件读取。
        """
        rel = url_path.lstrip("/")
        if not rel:
            rel = "index.html"
        elif "." not in rel.rsplit("/", 1)[-1]:
            # 无扩展名 → 尝试同名 .html（/login → login.html）
            rel += ".html"
        ext = os.path.splitext(rel)[1].lower()
        ctype = STATIC_TYPES.get(ext)
        if ctype is None:
            return False
        parts = rel.split("/")
        if any(p in ("..", "") for p in parts):
            self.send_error(404)
            return True
        self._serve_file(rel, ctype)
        return True

    # ---- 鉴权 ----
    def _check_auth(self) -> bool:
        """内部鉴权：AUTH_ENABLED=False 时跳过"""
        if not self.AUTH_ENABLED:
            # 跳过鉴权不等于「没有用户」：多个端点会断言 _auth_user 非空，
            # 无认证模式下会直接 500。注入一个虚拟管理员即可。
            self._auth_user = {  # pyright: ignore[reportAttributeAccessIssue]
                "sub": 0, "username": "anonymous", "display_name": "本地模式",
                "role": "admin",
                "permissions": ["admin:users", "admin:config", "words:read", "words:star"],
                "session_id": "no-auth",
            }
            return True
        # 先检查 X-Auth-Token（绕过 Cloudflare 对 Authorization 的剥除）
        xtoken = self.headers.get("X-Auth-Token", "")  # pyright: ignore[reportAttributeAccessIssue]
        token = auth.extract_bearer_token(self.headers) or xtoken  # pyright: ignore[reportAttributeAccessIssue]
        if token:
            user = auth.validate_session(token)
            if user:
                self._auth_user = user  # pyright: ignore[reportAttributeAccessIssue]
                return True
        return False

    def _require_auth(self) -> bool:
        """检查认证并 401 响应（首启未改密时 403 拦截其它接口）"""
        if self._check_auth():
            user: Mapping[str, Any] = self._auth_user or {}  # pyright: ignore[reportAttributeAccessIssue]
            if int(user.get("must_change_password") or 0):
                if urlparse(self.path).path not in self.SETUP_ALLOWED_PATHS:  # pyright: ignore[reportAttributeAccessIssue]
                    self._send_json(
                        {"error": "password_change_required",
                         "message": "首次登录请先修改初始口令"},
                        status=403,
                    )
                    return False
            return True
        self._send_json(
            {"error": "unauthorized", "message": "请先登录"},
            status=401,
        )
        return False

    def _require_permission(self, perm: str) -> bool:
        """检查指定权限并 403 响应"""
        if not self._require_auth():
            return False
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        assert user is not None
        if not auth.check_permission(dict(user), perm):
            self._send_json(
                {"error": "forbidden", "message": f"缺少权限: {perm}"},
                status=403,
            )
            return False
        return True

    def _is_guest(self) -> bool:
        """检查当前用户是否为游客（含历史共享账户 guest 与新版临时账户 guest_xxx）"""
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        return bool(user and auth.is_guest_username(user.get("username")))

    def _rate_limited(self, bucket: str) -> bool:
        """命中限流则直接返回 429，调用方应立即 return。"""
        rule = limits.RATE_RULES.get(bucket)
        if not rule:
            return False
        limit, window = rule
        if limits.RATE.allow(bucket, self._get_client_ip() or "unknown", limit, window):
            return False
        self._send_json(
            {"error": "rate_limited",
             "message": f"操作过于频繁，请 {int(window / 60) or 1} 分钟后再试"},
            429,
        )
        return True

    # ---- 查询参数 ----
    @staticmethod
    def _int_qs(qs: dict[str, list[str]], key: str, default: int = 0) -> int:
        try:
            return int((qs.get(key) or [str(default)])[0])
        except (TypeError, ValueError):
            return default
