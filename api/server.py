#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""
word3500 - 高考英语词汇3500 (Python + HTML 版)
依赖：仅 Python 标准库（http.server / sqlite3 / urllib），零第三方包。

运行：
    python -m api.server            # 默认 http://127.0.0.1:8000
    python -m api.server 8080       # 指定端口
    python -m api.server --no-browser   # 不自动打开浏览器

本文件只负责三件事：
  1. 组装 HTTP 处理器（把各路由模块的混入类拼起来）；
  2. 声明式路由表（`GET_ROUTES` / `POST_ROUTES`）→ 分发，替代原来的 70+ 分支 if/elif；
  3. 进程启动（建库 / 导入词表 / 监听端口）。

具体业务在：
    api/routes_auth.py      认证与用户管理
    api/routes_words.py     词表 / 搜索 / 收藏夹 / 学习进度
    api/routes_study.py     学习统计 / 事件上报 / 徽章 / 排行榜
    api/routes_together.py  一起背（学习房间）
    api/routes_admin.py     管理员报表与自愈工具
    api/routes_online.py    在线人数（SSE / 心跳）
    api/http_base.py        HTTP 公共骨架（响应 / 静态资源 / 鉴权）
    api/db.py               连接与 schema 的唯一定义处
    api/limits.py           限流与 SSE 配额
"""
import os
import sys
import threading
import webbrowser
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

# 确保项目根目录在 sys.path 中，使 `from api import auth` 在直接运行
# （python api/server.py）与作为模块导入（python -m api.server）两种方式下均可用
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# study / together 在导入时执行 init()（幂等建表），因此必须先于请求处理被导入
from api import auth, db, limits, study, together  # noqa: F401  (导入即初始化)
from api import routes_admin, routes_auth, routes_online, routes_study
from api import routes_together, routes_words
from api.http_base import HttpMixin

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（api/ 的上一级）
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, ".."))
# 前端静态资源目录
WEB_DIR = os.path.join(PROJECT_ROOT, "web")
# 数据库与数据文件统一存放于 /data
DATA_DIR = db.DATA_DIR
WORDS_DB_PATH = db.WORDS_DB_PATH
WORDS_FILE = os.path.join(DATA_DIR, "3500.txt")
PAGE_SIZE = db.PAGE_SIZE
PORT = 8000
AUTH_ENABLED = os.environ.get("WORD3500_NO_AUTH", "").lower() not in ("1", "true", "yes")

# 兼容旧引用（限流与配额已迁到 api/limits.py）
_RATE = limits.RATE
_RATE_RULES = limits.RATE_RULES
_SETUP_ALLOWED_PATHS = limits.SETUP_ALLOWED_PATHS
_sse_acquire = limits.sse_acquire
_sse_release = limits.sse_release

# ---------------------------------------------------------------------------
# 路由表（路径 → (处理方法名, 是否接收 query dict)）
# 新增接口只需在对应 routes_*.py 的 GET_ROUTES / POST_ROUTES 里加一行。
# ---------------------------------------------------------------------------
GET_ROUTES: dict[str, tuple[str, bool]] = {
    **routes_online.GET_ROUTES,
    **routes_admin.GET_ROUTES,
    **routes_study.GET_ROUTES,
    **routes_together.GET_ROUTES,
    **routes_words.GET_ROUTES,
    **routes_auth.GET_ROUTES,
}

POST_ROUTES: dict[str, tuple[str, bool]] = {
    **routes_online.POST_ROUTES,
    **routes_admin.POST_ROUTES,
    **routes_study.POST_ROUTES,
    **routes_together.POST_ROUTES,
    **routes_words.POST_ROUTES,
    **routes_auth.POST_ROUTES,
}


# ---------------------------------------------------------------------------
# 数据库初始化（连接与 schema 定义在 api/db.py）
# ---------------------------------------------------------------------------
def get_words_db():
    return db.connect()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db.init_schema()
    conn = get_words_db()
    cur = conn.cursor()
    _ = cur.execute("SELECT COUNT(*) AS c FROM words")
    if cur.fetchone()["c"] == 0:
        # 优先从 english_done/ 导入分册单词
        words_dir = os.path.join(DATA_DIR, "english_done")
        imported = 0
        if os.path.isdir(words_dir):
            imported = _import_from_books(cur, words_dir)
        if imported == 0:
            # 分册目录不存在或为空 → 回退到旧的 3500.txt 扁平格式
            # （旧实现只看目录是否存在，目录为空时会留下 0 个单词的空库）
            if os.path.isfile(WORDS_FILE):
                _import_from_flat_file(cur)
            else:
                print(f"[init] 警告：{WORDS_FILE} 不存在，单词库为空")
        conn.commit()
    conn.close()
    # words 表（含首次启动导入的词条）到这里才就绪。study 的 init() 负责
    # 词性回填 / 当日基线，幂等，老库重复执行无副作用。
    study.init()


def _import_from_books(cur: Any, words_dir: str) -> int:
    """从 english_done/ 分册目录导入单词"""
    import re
    BOOK_MAP = db.BOOK_CODES
    total = 0
    for entry in sorted(os.listdir(words_dir)):
        child = os.path.join(words_dir, entry)
        if not os.path.isdir(child):
            continue
        book_num = BOOK_MAP.get(entry)
        if not book_num:
            continue
        for fname in sorted(os.listdir(child)):
            if not fname.endswith(".txt"):
                continue
            # 提取单元名
            unit = re.sub(r'.*单词表\s+', '', os.path.splitext(fname)[0]).strip()
            fpath = os.path.join(child, fname)
            with open(fpath, "r", encoding="utf-8-sig") as f:
                lines = [ln.rstrip("\n\r") for ln in f]
            i, n = 0, len(lines)
            while i + 2 < n:
                w = lines[i].strip()
                p = lines[i + 1].strip()
                m = lines[i + 2].strip()
                if w == "" and p == "" and m == "":
                    i += 3
                    continue
                total += 1
                cur.execute(
                    "INSERT INTO words(word, pronunciation, meaning, book, unit) VALUES (?,?,?,?,?)",
                    (w, p, m, book_num, unit),
                )
                i += 3
    print(f"[init] 已从 english_done/ 导入 {total} 个单词（含册/单元信息）")
    return total


def _import_from_flat_file(cur: Any) -> int:
    """从 3500.txt 导入扁平单词表"""
    with open(WORDS_FILE, encoding="utf-8-sig") as f:
        lines = [ln.rstrip("\n") for ln in f]
    words = []
    i = 0
    n = len(lines)
    while i + 2 < n:
        w = lines[i].strip()
        p = lines[i + 1].strip()
        m = lines[i + 2].strip()
        if w == "" and p == "" and m == "":
            i += 3
            continue
        words.append((w, p, m))
        i += 3
    _ = cur.executemany(
        "INSERT INTO words(id, word, pronunciation, meaning, star) "
        "VALUES (?,?,?,?,0)",
        [(idx + 1, w, p, m) for idx, (w, p, m) in enumerate(words)],
    )
    print(f"[init] 已从 {os.path.basename(WORDS_FILE)} 导入 {len(words)} 个单词")
    return len(words)


# ---------------------------------------------------------------------------
# HTTP 处理器：路由混入类 + 分发
# ---------------------------------------------------------------------------
class Handler(
    HttpMixin,
    routes_online.OnlineRoutes,
    routes_admin.AdminRoutes,
    routes_study.StudyRoutes,
    routes_together.TogetherRoutes,
    routes_words.WordsRoutes,
    routes_auth.AuthRoutes,
    BaseHTTPRequestHandler,
):
    # HTTP/1.1 长连接：默认的 HTTP/1.0 每个请求都要重新建 TCP 连接，
    # 首页一次要拉 4 个静态资源 + 2 个接口，握手开销占比很高。
    protocol_version = "HTTP/1.1"

    WEB_DIR = WEB_DIR
    AUTH_ENABLED = AUTH_ENABLED
    SETUP_ALLOWED_PATHS = limits.SETUP_ALLOWED_PATHS

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._auth_user: Optional[dict[str, Any]] = None  # 鉴权中间件注入
        self._resp_status: int = 0                        # 本次响应状态码（供访问日志使用）
        super().__init__(*args, **kwargs)

    # ---- 分发 ----
    def _dispatch(self, table: dict[str, tuple[str, bool]], path: str,
                  qs: dict[str, list[str]]) -> bool:
        """按路由表调用处理函数；返回 False 表示没有匹配。"""
        entry = table.get(path)
        if entry is None:
            return False
        name, wants_qs = entry
        handler = getattr(self, name)
        if wants_qs:
            handler(qs)
        else:
            handler()
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 静态文件：统一走白名单静态服务。
        # 以前是硬编码路由表，新增 js 文件忘加一条就静默 404
        # （js/liquid-glass.js 就是这样一直没被服务）。
        if not path.startswith("/api/"):
            if self._serve_static(path):
                return
            self.send_error(404)
            return

        if self._dispatch(GET_ROUTES, path, qs):
            return
        if self.route_user_detail(path):   # /api/auth/users/<id>
            return

        # /api/* 下的未知路径必须返回 JSON，不能落到 send_error 的 HTML 错误页：
        # 前端拿到 <!DOCTYPE 后 r.json() 会抛 "Unexpected token '<'"，把
        # 「接口不存在」误报成解析错误，排查成本极高。
        self._send_json(
            {"error": "not_found", "message": "接口不存在: " + path}, 404
        )

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if self._dispatch(POST_ROUTES, path, qs):
            return

        # 同 do_GET：/api/* 一律 JSON 404，避免前端 .json() 解析 HTML 报错
        self._send_json(
            {"error": "not_found", "message": "接口不存在: " + path}, 404
        )


# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------
class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """HTTP/1.1 长连接下，浏览器关闭空闲连接会抛 ConnectionResetError。

    socketserver 默认会打完整堆栈，终端被刷屏；这类异常是正常现象，直接忽略。
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def main():
    init_db()
    port = PORT
    cli_port_given = False
    open_browser = True
    for arg in sys.argv[1:]:
        if arg in ("--no-browser", "-n"):
            open_browser = False
        elif arg in ("--help", "-h"):
            print("用法: python -m api.server [端口] [--no-browser]")
            return
        else:
            try:
                port = int(arg)
                cli_port_given = True
            except ValueError:
                print(f"[warn] 忽略无法识别的参数: {arg}")
    # 环境变量端口只要命令行没有显式给端口就生效。
    # 旧实现用 len(sys.argv) <= 1 判断，导致带 --no-browser 启动时 WORD3500_PORT 被静默忽略。
    env_port = os.environ.get("WORD3500_PORT", "")
    if env_port.isdigit() and not cli_port_given:
        port = int(env_port)
    try:
        server = QuietThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        print(f"[error] 无法监听端口 {port}: {e}")
        print("        端口可能已被占用，可换一个端口重试：python -m api.server 8080")
        sys.exit(1)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 50)
    print(" word3500 已启动 ->", url)
    print(f" 登录鉴权：{'已开启' if AUTH_ENABLED else '已关闭（WORD3500_NO_AUTH）'}")
    print(" 按 Ctrl+C 停止")
    print("=" * 50)

    if open_browser:
        def _open():
            try:
                webbrowser.open(url)
            except Exception:
                pass  # 服务器无桌面环境时静默跳过

        threading.Timer(1.0, _open).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
