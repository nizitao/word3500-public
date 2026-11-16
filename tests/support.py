#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""测试基座：在临时目录里起一个**真实**的 word3500 服务进程。

设计目标（这是把根目录 `_*.mjs` / `_*.py` 手工验收脚本变成可复跑套件的基础）：

* **零第三方依赖**：只用 unittest / urllib / sqlite3 / subprocess。
* **不碰生产数据**：把 `api/` 复制到临时目录，`data/` 从零生成
  （自己的 users.db + 少量合成词条），跑完即删。
* **鉴权真实开启**：不设置 `WORD3500_NO_AUTH`，所有断言都走真实 Token 链路。
* **端口随机**：绑定 `127.0.0.1:0` 拿空闲端口，可并行、不冲突。
* **可复现**：用户口令固定（同一份 PBKDF2 参数），不依赖 `initial_admin_password.txt`。

用法见 tests/test_*.py 里的 `class XxxTest(unittest.TestCase)` + `setUpClass`。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "Passw0rd_test"

# 预置账户：(用户名, 展示名, 角色, 额外权限)
SEED_USERS: tuple[tuple[str, str, str, list[str]], ...] = (
    ("admin", "管理员", "admin", ["admin:users", "admin:config", "words:read", "words:star"]),
    ("alice", "爱丽丝", "user", []),
    ("bob", "鲍勃", "user", []),
    ("carol", "卡罗", "user", []),
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Sandbox:
    """一个隔离的运行环境（临时目录 + 独立服务进程 + 独立数据库）。"""

    def __init__(self, word_count: int = 40) -> None:
        self.word_count = word_count
        self.dir = Path(tempfile.mkdtemp(prefix="w3500_test_"))
        self.port = free_port()
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self._tokens: dict[str, str] = {}
        self._prepare()

    # ---- 生命周期 ----
    def __enter__(self) -> "Sandbox":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self) -> None:
        env = dict(os.environ)
        env.pop("WORD3500_NO_AUTH", None)      # 鉴权必须开启
        env["WORD3500_PORT"] = str(self.port)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(self.dir)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "api.server", str(self.port), "--no-browser"],
            cwd=str(self.dir), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("服务进程启动即退出（见 sandbox 目录）")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/api/online", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                time.sleep(0.15)
        raise RuntimeError("服务 30 秒内未就绪")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None
        time.sleep(0.2)   # 等端口释放

    def restart(self) -> None:
        """模拟部署重启：杀掉进程后用同一份 data/ 重新拉起。"""
        self.stop()
        self.start()

    # ---- 初始化 ----
    def _prepare(self) -> None:
        (self.dir / "api").mkdir()
        (self.dir / "data").mkdir()
        for src in (ROOT / "api").glob("*.py"):
            shutil.copy2(src, self.dir / "api" / src.name)
        self._run_script(self._bootstrap_users_py())
        self._run_script(self._seed_words_py())

    def _run_script(self, code: str) -> str:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Windows 默认用 GBK 解码子进程输出，而脚本里会打印中文（UTF-8）→ 必须显式指定
        return subprocess.check_output(
            [sys.executable, "-c", code], cwd=str(self.dir), env=env,
            encoding="utf-8", errors="replace", timeout=120,
        )

    def run_python(self, code: str) -> str:
        """在沙箱目录里跑一段独立 Python（模拟「另起一个进程」的场景）。

        脚本里可以 `import api.xxx`：cwd 与 sys.path 都指向沙箱副本。
        """
        return self._run_script(f"import sys\nsys.path.insert(0, {str(self.dir)!r})\n" + code)

    def _bootstrap_users_py(self) -> str:
        users = repr([(u[0], u[1], u[2], u[3]) for u in SEED_USERS])
        return (
            "import sys\n"
            f"sys.path.insert(0, {str(self.dir)!r})\n"
            "from api import auth\n"
            f"users = {users}\n"
            f"pw = {PASSWORD!r}\n"
            # init_auth_db() 在导入时已创建随机口令的默认 admin，这里统一改成已知口令，
            # 并清掉 must_change_password，否则除改密外的接口都会 403。
            "conn = auth.sqlite3.connect(auth.USERS_DB_PATH)\n"
            "conn.execute(\"UPDATE users SET password=?, must_change_password=0 "
            "WHERE username='admin'\", (auth._hash_password(pw),))\n"
            "conn.commit(); conn.close()\n"
            "for name, display, role, perms in users:\n"
            "    if auth.authenticate_user(name, pw) is None:\n"
            "        auth.create_user(name, pw, display, role=role, permissions=perms)\n"
            "print('bootstrap ok')\n"
        )

    def _seed_words_py(self) -> str:
        count = self.word_count
        return (
            "import sys\n"
            f"sys.path.insert(0, {str(self.dir)!r})\n"
            "from api import db\n"
            "db.init_schema()\n"
            "conn = db.connect()\n"
            "rows = []\n"
            f"for i in range(1, {count} + 1):\n"
            "    book = 1 if i <= 20 else 2\n"
            "    unit = 'Unit 1' if i % 2 else 'Unit 2'\n"
            "    rows.append((i, f'word{i:03d}', f'/w{i}/', f'n. 测试词{i}', book, unit))\n"
            "conn.executemany('INSERT OR REPLACE INTO words"
            "(id,word,pronunciation,meaning,book,unit) VALUES (?,?,?,?,?,?)', rows)\n"
            "conn.commit(); conn.close()\n"
            "print('seed ok')\n"
        )

    # ---- HTTP ----
    def request(self, method: str, path: str, body: Any = None,
                token: Optional[str] = None,
                extra_headers: Optional[dict[str, str]] = None) -> tuple[int, Any]:
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers: dict[str, str] = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-Auth-Token"] = token
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            status = e.code
        try:
            return status, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return status, {"_raw": raw}

    def get(self, path: str, token: Optional[str] = None) -> tuple[int, Any]:
        return self.request("GET", path, token=token)

    def post(self, path: str, body: Any = None,
             token: Optional[str] = None) -> tuple[int, Any]:
        return self.request("POST", path, body if body is not None else {}, token=token)

    # ---- 账户 ----
    def login(self, username: str, password: str = PASSWORD) -> dict[str, Any]:
        status, data = self.post("/api/auth/login",
                                 {"username": username, "password": password})
        if status != 200:
            raise AssertionError(f"登录失败 {username}: {status} {data}")
        self._tokens[username] = data["access_token"]
        return data

    def token(self, username: str) -> str:
        """取（并缓存）某账户的 access token。"""
        cached = self._tokens.get(username)
        if cached:
            return cached
        return self.login(username)["access_token"]

    def user_id(self, username: str) -> int:
        rows = self.query_users("SELECT id FROM users WHERE username=?", (username,))
        if not rows:
            raise AssertionError(f"用户不存在: {username}")
        return int(rows[0][0])

    def new_user(self, prefix: str = "u") -> tuple[str, str]:
        """注册一个全新账户并登录，返回 (username, token)。

        只用于需要「零历史数据」的用例。注意 /api/auth/register 有每 IP 10 次/10 分钟
        的限流，单个测试类里别超过 10 次。
        """
        import secrets as _secrets

        name = f"{prefix}{_secrets.token_hex(4)}"
        status, data = self.post("/api/auth/register",
                                 {"username": name, "password": PASSWORD})
        if status != 201:
            raise AssertionError(f"注册失败: {status} {data}")
        return name, self.login(name)["access_token"]

    # ---- 数据库直查（只读断言用） ----
    def _query(self, db_name: str, sql: str,
               params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.dir / "data" / db_name, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute(sql, tuple(params)).fetchall())
        finally:
            conn.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """查 words.db。"""
        return self._query("words.db", sql, params)

    def query_users(self, sql: str,
                    params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """查 users.db。"""
        return self._query("users.db", sql, params)

    def exec_sql(self, sql: str, params: Iterable[Any] = ()) -> None:
        """直接改 words.db（模拟数据漂移，验证自愈）。"""
        self._exec("words.db", sql, params)

    def exec_users(self, sql: str, params: Iterable[Any] = ()) -> None:
        """直接改 users.db（如设置 must_change_password 标记）。"""
        self._exec("users.db", sql, params)

    def _exec(self, db_name: str, sql: str, params: Iterable[Any]) -> None:
        conn = sqlite3.connect(self.dir / "data" / db_name, timeout=10)
        try:
            conn.execute(sql, tuple(params))
            conn.commit()
        finally:
            conn.close()

    # ---- 便捷构造 ----
    def events(self, pairs: list[tuple[int, str, int]],
               tz: int = 0) -> dict[str, Any]:
        """pairs = [(word_id, action, result), ...] → 事件上报请求体。"""
        return {"tz": tz, "events": [
            {"word_id": w, "action": a, "result": r,
             "uid": f"t-{w}-{a}-{r}-{i}-{time.time_ns()}"}
            for i, (w, a, r) in enumerate(pairs)
        ]}


class BaseCase(unittest.TestCase):
    """带沙箱的测试基类：每个类一个服务进程，测试之间用不同账户/词条隔离。"""

    word_count = 40

    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = Sandbox(cls.word_count)
        cls.sandbox.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sandbox.close()

    def setUp(self) -> None:
        self.box = self.sandbox
