#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""
word3500 SSO 认证模块 —— 零依赖 OAuth 2.0 / OpenID Connect 风格实现
仅使用 Python 标准库：hmac · hashlib · secrets · json · base64 · sqlite3
"""
import os
import sys
import json
import hmac
import time
import uuid
import base64
import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from functools import wraps
from http.client import HTTPMessage
from typing import Optional, Callable, Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据库统一存放于项目根 /data 目录
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "data"))
USERS_DB_PATH = os.path.join(DATA_DIR, "users.db")
os.makedirs(DATA_DIR, exist_ok=True)  # 全新克隆时 data/ 可能不存在

# 公开导出（供 server.py 直接引用，避免访问模块私有成员告警）
sqlite3 = sqlite3
json = json

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
ACCESS_TOKEN_MINUTES = 30          # Access Token 有效期 30 分钟
REFRESH_TOKEN_DAYS = 7             # Refresh Token 有效期 7 天
SESSION_IDLE_MINUTES = 30          # 会话空闲超时 30 分钟
SESSION_MAX_HOURS = 8              # 会话绝对超时 8 小时
MAX_FAILED_ATTEMPTS = 5            # 连续失败锁定阈值
LOCKOUT_MINUTES = 15               # 锁定时间 15 分钟
BCRYPT_ROUNDS = 100_000            # PBKDF2-HMAC-SHA256 迭代次数（模拟 bcrypt 成本）

# ---------------------------------------------------------------------------
# 密钥管理
# ---------------------------------------------------------------------------
def _get_or_create_secret() -> str:
    """从 DB 读取或生成 HMAC 签名密钥"""
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS _config (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.commit()
    cur.execute("SELECT value FROM _config WHERE key='secret'")
    row = cur.fetchone()
    if row:
        secret = row[0]
    else:
        secret = secrets.token_hex(32)
        cur.execute("INSERT INTO _config(key,value) VALUES('secret',?)", (secret,))
        conn.commit()
    conn.close()
    return secret

SECRET_KEY = _get_or_create_secret()

# ---------------------------------------------------------------------------
# 数据库初始化
# ---------------------------------------------------------------------------
def init_auth_db():
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT UNIQUE NOT NULL,
        password    TEXT NOT NULL,          -- pbkdf2_hmac:iterations$salt$hash
        display_name TEXT DEFAULT '',
        email       TEXT DEFAULT '',
        role        TEXT DEFAULT 'user',    -- admin / user
        permissions TEXT DEFAULT '[]',       -- JSON 数组，额外权限
        enabled     INTEGER DEFAULT 1,
        failed_attempts INTEGER DEFAULT 0,
        locked_until   TEXT DEFAULT NULL,
        created_at  TEXT DEFAULT (datetime('now')),
        updated_at  TEXT DEFAULT (datetime('now')),
        email_verified   INTEGER DEFAULT 0,
        email_verify_token TEXT DEFAULT NULL,
        must_change_password INTEGER DEFAULT 0   -- 首启随机口令：首次登录必须改密
    )""")
    # 兼容旧版 DB：添加邮箱验证字段 + 强制改密标记
    for col, col_type in [("email_verified", "INTEGER DEFAULT 0"),
                           ("email_verify_token", "TEXT DEFAULT NULL"),
                           ("must_change_password", "INTEGER DEFAULT 0")]:
        try:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()
    cur.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id              TEXT PRIMARY KEY,     -- session_id (UUID)
        user_id         INTEGER NOT NULL,
        access_token    TEXT,
        refresh_token   TEXT,
        id_token        TEXT,
        expires_at      TEXT NOT NULL,
        refresh_expires_at TEXT,
        created_at      TEXT DEFAULT (datetime('now')),
        last_activity   TEXT DEFAULT (datetime('now')),
        ip_address      TEXT DEFAULT '',
        user_agent      TEXT DEFAULT '',
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS auth_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER,
        username    TEXT DEFAULT '',
        event_type  TEXT NOT NULL,    -- login / logout / slo / refresh / failed / lockout
        details     TEXT DEFAULT '',
        ip_address  TEXT DEFAULT '',
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")
    # 创建默认管理员：口令随机生成，绝不再硬编码 admin123。
    # 初始口令只落在 data/initial_admin_password.txt（本地文件，已 gitignore），
    # 控制台只提示文件位置，不打印明文，避免 CI/终端日志泄漏。
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        initial_pw = secrets.token_urlsafe(18)
        _create_user_internal(cur, "admin", initial_pw, "管理员", role="admin",
                              permissions=["admin:users", "admin:config", "words:read", "words:star"],
                              must_change_password=1)
        pw_file = os.path.join(DATA_DIR, "initial_admin_password.txt")
        try:
            with open(pw_file, "w", encoding="utf-8") as f:
                f.write("word3500 首次启动生成的管理员初始口令\n")
                f.write("username: admin\n")
                f.write(f"password: {initial_pw}\n")
                f.write("请登录后在「首次登录改密」页面立即修改，改完请删除本文件。\n")
            try:
                os.chmod(pw_file, 0o600)   # Windows 上尽力而为
            except OSError:
                pass
            print(f"[auth] 默认管理员已创建，初始口令见 {pw_file}（登录后必须修改）")
        except OSError as e:
            # 文件写不下去也不能留一个无人知晓口令的库：把口令打印到 stderr 便于运维救急
            print(f"[auth] 无法写入初始口令文件（{e}）；初始口令：{initial_pw}",
                  file=sys.stderr, flush=True)
    conn.commit()
    # 旧版共享「游客」账户：曾被所有访客共用同一 uid，会互相串读数据、互相顶号下线；
    # 且密码 guest123 硬编码在源码里 = 公开可登录账户。现改为「每次登录创建独立临时账户」，
    # 这里停用旧账户并随机化其口令（保留历史数据，不删除）。
    row = cur.execute("SELECT id, enabled FROM users WHERE username='guest'").fetchone()
    if row and int(row[1] or 0):
        cur.execute(
            "UPDATE users SET enabled=0, password=?, updated_at=datetime('now') "
            "WHERE username='guest'", (_hash_password(secrets.token_hex(24)),))
        conn.commit()
        print("[auth] 已停用旧版共享游客账户")
    conn.commit()
    conn.close()


def _create_user_internal(cur, username: str, password: str, display_name: str = "",
                          role: str = "user", permissions: list[str] | None = None,
                          must_change_password: int = 0):
    pwd = _hash_password(password)
    perms = json.dumps(permissions or [], ensure_ascii=False)
    cur.execute(
        "INSERT INTO users(username, password, display_name, role, permissions, "
        "must_change_password) VALUES(?,?,?,?,?,?)",
        (username, pwd, display_name, role, perms, must_change_password),
    )


# ---------------------------------------------------------------------------
# 密码哈希（PBKDF2-HMAC-SHA256，无第三方依赖）
# ---------------------------------------------------------------------------
def _load_permissions(raw: Any) -> list[str]:
    """把 users.permissions 字段（JSON 字符串 / 列表）安全解析为列表"""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def _hash_password(password: str) -> str:
    """格式: pbkdf2_sha256$iterations$salt$hash"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"),
                              BCRYPT_ROUNDS)
    return f"pbkdf2_sha256${BCRYPT_ROUNDS}${salt}${dk.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt, stored_hash = encoded.split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                  salt.encode("utf-8"), int(iterations))
        return hmac.compare_digest(dk.hex(), stored_hash)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Token 工具（JWT 风格，HMAC-SHA256 签名）
# ---------------------------------------------------------------------------
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    s += "=" * (4 - len(s) % 4)  # 补齐 padding
    return base64.urlsafe_b64decode(s)


def create_token(payload: dict[str, Any], expires_seconds: int) -> str:
    """创建签名的 JWT 风格 Token"""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload["iat"] = now
    payload["exp"] = now + expires_seconds
    payload["jti"] = secrets.token_hex(8)
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}"
    sig = hmac.new(SECRET_KEY.encode(), signing_input.encode(), "sha256").digest()
    sig_b64 = _b64url_encode(sig)
    return f"{signing_input}.{sig_b64}"


def verify_token(token: str) -> dict[str, Any] | None:
    """验证 Token 签名与过期时间，返回 payload 或 None"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(SECRET_KEY.encode(), signing_input.encode(),
                                "sha256").digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def decode_token_unsafe(token: str) -> dict[str, Any] | None:
    """不验证签名，仅解码 payload（用于调试或提取 sub）"""
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            return json.loads(_b64url_decode(parts[1]))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 用户管理
# ---------------------------------------------------------------------------
def authenticate_user(username: str, password: str) -> dict[str, Any] | None:
    """验证用户名密码，返回用户信息或 None"""
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=? AND enabled=1", (username,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    user = dict(row)
    # 检查锁定
    if user.get("locked_until"):
        locked = _parse_dt(user["locked_until"])
        if locked > _utcnow():
            conn.close()
            return None  # 已锁定
    # 验证密码
    if _verify_password(password, user["password"]):
        # 重置失败计数
        cur.execute("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?",
                    (user["id"],))
        conn.commit()
        conn.close()
        user["permissions"] = _load_permissions(user.get("permissions", "[]"))
        return user
    else:
        # 增加失败计数
        attempts = (user.get("failed_attempts") or 0) + 1
        if attempts >= MAX_FAILED_ATTEMPTS:
            lock_until = (_utcnow() +
                          timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
            cur.execute(
                "UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                (attempts, lock_until, user["id"])
            )
        else:
            cur.execute("UPDATE users SET failed_attempts=? WHERE id=?",
                        (attempts, user["id"]))
        conn.commit()
        conn.close()
        return None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, username, display_name, email, role, permissions, enabled, email_verified FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        u = dict(row)
        u["permissions"] = _load_permissions(u.get("permissions", "[]"))
        return u
    return None


def list_users() -> list[dict[str, Any]]:
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, username, display_name, email, role, permissions, enabled, email_verified FROM users ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    users = []
    for r in rows:
        u = dict(r)
        u["permissions"] = _load_permissions(u.get("permissions", "[]"))
        users.append(u)
    return users


def update_user_password(user_id: int, old_password: str, new_password: str) -> bool:
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT password FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    if not row or not _verify_password(old_password, row["password"]):
        conn.close()
        return False
    cur.execute("UPDATE users SET password=?, must_change_password=0, "
                "updated_at=datetime('now') WHERE id=?",
                (_hash_password(new_password), user_id))
    conn.commit()
    conn.close()
    return True


def admin_reset_password(user_id: int, new_password: str) -> bool:
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET password=?, updated_at=datetime('now') WHERE id=?",
                (_hash_password(new_password), user_id))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def create_user(username: str, password: str, display_name: str = "",
                role: str = "user", permissions: list[str] | None = None,
                email: str = "") -> bool:
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    try:
        _create_user_internal(cur, username, password, display_name, role, permissions)
        if email:
            cur.execute("UPDATE users SET email=? WHERE username=?", (email, username))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def delete_user(user_id: int) -> bool:
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    ok = cur.rowcount > 0
    if ok:
        cur.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return ok


# ---------------------------------------------------------------------------
# 游客体验：每次登录创建独立临时账户
# ---------------------------------------------------------------------------
GUEST_PREFIX = "guest_"
GUEST_TTL_HOURS = 24          # 临时游客账户保留时长（超期且无活跃会话即清理）


def is_guest_username(username: Any) -> bool:
    """统一游客判定：历史共享账户 guest 与新版临时账户 guest_xxx 都算游客。"""
    name = str(username or "").strip().lower()
    return name == "guest" or name.startswith(GUEST_PREFIX)


def create_guest_user() -> dict[str, Any] | None:
    """创建一个独立的临时游客账户。

    旧实现让所有访客共用同一个 guest 账户（同一 uid），导致：
      1. 游客互相读到对方的收藏/进度数据；
      2. 任一人登录都会 revoke_all_sessions 把其他游客全部顶下线；
      3. 口令 guest123 硬编码在源码中，等于公开账户。
    改为每次发放独立账户 + 随机口令后，三个问题同时消除。
    """
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    try:
        for _ in range(5):                       # 极小概率重名，重试若干次
            username = GUEST_PREFIX + secrets.token_hex(6)
            password = secrets.token_hex(18)
            try:
                _create_user_internal(cur, username, password, "游客",
                                      role="user",
                                      permissions=["words:read"])
                conn.commit()
                break
            except sqlite3.IntegrityError:
                continue
        else:
            return None
    finally:
        conn.close()
    return authenticate_user(username, password)


def cleanup_guests() -> int:
    """清理超期且无有效会话的临时游客账户，避免 users 表无限增长。"""
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM users WHERE username LIKE ? ESCAPE '\\' "
            "AND created_at < datetime('now', ?) "
            "AND id NOT IN (SELECT user_id FROM sessions WHERE expires_at > datetime('now'))",
            (GUEST_PREFIX.replace("_", "\\_") + "%", f"-{GUEST_TTL_HOURS} hours"),
        )
        ids = [r[0] for r in cur.fetchall()]
        for uid in ids:
            cur.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            cur.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        return len(ids)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 邮箱验证
# ---------------------------------------------------------------------------
def generate_verify_token() -> str:
    """生成邮箱验证 Token（48 位随机 hex）"""
    return secrets.token_hex(24)


def set_verify_token(user_id: int) -> str:
    """为用户生成并存储验证 Token，返回 token 值"""
    token = generate_verify_token()
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.execute(
        "UPDATE users SET email_verify_token=? WHERE id=?",
        (token, user_id),
    )
    conn.commit()
    conn.close()
    return token


def verify_email(token: str) -> bool:
    """通过 Token 验证邮箱"""
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET email_verified=1, email_verify_token=NULL "
        "WHERE email_verify_token=? AND email_verified=0",
        (token,),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def mark_email_verified(user_id: int):
    """管理员直接标记邮箱已验证"""
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.execute(
        "UPDATE users SET email_verified=1, email_verify_token=NULL WHERE id=?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_verify_token(user_id: int) -> str | None:
    """获取用户当前的验证 Token（用于管理员查看）"""
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT email_verify_token FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------
def create_session(user: dict[str, Any], ip: str = "", ua: str = "") -> dict[str, Any]:
    """为用户创建会话，返回 tokens"""
    user_id = user["id"]
    username = user["username"]
    role = user["role"]
    permissions = user.get("permissions", [])
    display_name = user.get("display_name", "")

    session_id = uuid.uuid4().hex
    now = _utcnow()
    access_expires = now + timedelta(minutes=ACCESS_TOKEN_MINUTES)
    refresh_expires = now + timedelta(days=REFRESH_TOKEN_DAYS)

    # Access Token payload
    access_payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "permissions": permissions,
        "display_name": display_name,
        "sid": session_id,
        "type": "access",
    }
    access_token = create_token(access_payload, ACCESS_TOKEN_MINUTES * 60)

    # Refresh Token payload
    refresh_payload = {
        "sub": user_id,
        "username": username,
        "sid": session_id,
        "type": "refresh",
    }
    refresh_token = create_token(refresh_payload, REFRESH_TOKEN_DAYS * 86400)

    # ID Token payload（OpenID Connect 风格）
    id_payload = {
        "sub": str(user_id),
        "preferred_username": username,
        "name": display_name or username,
        "role": role,
        "sid": session_id,
    }
    id_token = create_token(id_payload, ACCESS_TOKEN_MINUTES * 60)

    # 持久化
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    # 顺手清理彻底过期的会话，避免 sessions 表无限增长
    cur.execute(
        "DELETE FROM sessions WHERE refresh_expires_at IS NOT NULL AND refresh_expires_at < ?",
        (now.isoformat(),),
    )
    cur.execute(
        """INSERT INTO sessions(id, user_id, access_token, refresh_token, id_token,
           expires_at, refresh_expires_at, ip_address, user_agent)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (session_id, user_id, access_token, refresh_token, id_token,
         access_expires.isoformat(), refresh_expires.isoformat(), ip, ua),
    )
    conn.commit()
    conn.close()

    _log_event(user_id, username, "login", f"会话创建: {session_id[:8]}...", ip)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_MINUTES * 60,
        "session_id": session_id,
        "must_change_password": int(user.get("must_change_password") or 0),
        "user": {
            "id": user_id,
            "username": username,
            "display_name": display_name,
            "role": role,
            "permissions": permissions,
        },
    }


def _parse_dt(dt_str: str) -> datetime:
    """解析日期时间字符串，无时区信息则视作 UTC"""
    try:
        dt = datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        # SQLite datetime('now') 存储的是 UTC 时间（无时区标记）
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _utcnow() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


def validate_session(access_token: str) -> dict[str, Any] | None:
    """验证 Access Token，返回用户信息或 None"""
    payload = verify_token(access_token)
    if not payload or payload.get("type") != "access":
        return None
    session_id = payload.get("sid")
    if not session_id:
        return None
    # 检查会话是否仍然有效
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    session = dict(row)
    # 检查过期
    expires = _parse_dt(session["expires_at"])
    now = _utcnow()
    if expires < now:
        conn.close()
        return None
    # 检查绝对超时
    created = _parse_dt(session["created_at"])
    max_life = created + timedelta(hours=SESSION_MAX_HOURS)
    if now > max_life:
        conn.close()
        return None
    # 检查空闲超时
    last_act = _parse_dt(session["last_activity"])
    idle = now - last_act
    if idle > timedelta(minutes=SESSION_IDLE_MINUTES):
        conn.close()
        return None
    # 复查账户当前状态：Token 里的 role/permissions 是签发时的快照，
    # 若管理员在有效期内禁用账号或降级角色，旧快照会让权限继续生效（最长一个
    # Access Token 周期）。这里以数据库为准，使封禁 / 降级立即生效。
    cur.execute("SELECT enabled, role, permissions, username, display_name, "
                "must_change_password FROM users WHERE id=?", (session["user_id"],))
    urow = cur.fetchone()
    if not urow or not int(urow["enabled"] or 0):
        # 账户已被删除或禁用：顺手撤销该会话
        cur.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
        conn.close()
        return None
    # 更新最后活动时间（节流：仅在距上次写入 >60s 时落库。
    # 原实现每个请求都写一次 sessions，高频轮询时会造成不必要的写锁）
    if idle > timedelta(seconds=60):
        cur.execute("UPDATE sessions SET last_activity=datetime('now') WHERE id=?",
                    (session_id,))
        conn.commit()
    conn.close()
    return {
        "sub": payload["sub"],
        "username": urow["username"],
        "role": urow["role"],
        "permissions": _load_permissions(urow["permissions"]),
        "display_name": urow["display_name"] or "",
        "session_id": session_id,
        # 首启随机口令：未改密前除改密/登出/续期外的接口一律 403
        "must_change_password": int(urow["must_change_password"] or 0),
    }


def refresh_session(refresh_token: str, ip: str = "") -> dict[str, Any] | None:
    """使用 Refresh Token 刷新 Access Token"""
    payload = verify_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        return None
    session_id = payload.get("sid")
    user_id = payload.get("sub")
    if not session_id or not user_id:
        return None
    # 验证会话
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    session = dict(row)
    refresh_exp = _parse_dt(session["refresh_expires_at"])
    if refresh_exp < _utcnow():
        conn.close()
        return None
    conn.close()
    # 获取用户信息
    user = get_user_by_id(user_id)
    if not user:
        return None
    # 签发新 Token（复用原 session_id）
    now = _utcnow()
    access_expires = now + timedelta(minutes=ACCESS_TOKEN_MINUTES)
    access_payload = {
        "sub": user_id,
        "username": user["username"],
        "role": user["role"],
        "permissions": user["permissions"],
        "display_name": user.get("display_name", ""),
        "sid": session_id,
        "type": "access",
    }
    new_access_token = create_token(access_payload, ACCESS_TOKEN_MINUTES * 60)
    # 更新 DB
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.execute(
        "UPDATE sessions SET access_token=?, expires_at=?, last_activity=datetime('now') WHERE id=?",
        (new_access_token, access_expires.isoformat(), session_id),
    )
    conn.commit()
    conn.close()
    _log_event(user_id, user["username"], "refresh",
               f"Token 已刷新: {session_id[:8]}...", ip)
    return {
        "access_token": new_access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_MINUTES * 60,
    }


def revoke_session(access_token: str) -> bool:
    """撤销当前会话（登出）"""
    payload = decode_token_unsafe(access_token)
    if not payload:
        return False
    session_id = payload.get("sid")
    if not session_id:
        return False
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT s.user_id, u.username FROM sessions s LEFT JOIN users u ON s.user_id=u.id WHERE s.id=?", (session_id,))
    row = cur.fetchone()
    cur.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    if row:
        _log_event(row[0], row[1] or "", "logout",
                   f"会话已撤销: {session_id[:8]}...", "")
    conn.close()
    return True


def revoke_all_sessions(user_id: int) -> int:
    """单点注销（SLO）：撤销用户所有会话"""
    conn = sqlite3.connect(USERS_DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    username = row[0] if row else ""
    cur.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user_id,))
    count = cur.fetchone()[0]
    cur.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    _log_event(user_id, username, "slo", f"所有会话已撤销（共 {count} 个）", "")
    return count


# ---------------------------------------------------------------------------
# 权限管理
# ---------------------------------------------------------------------------
# 角色默认权限
ROLE_PERMISSIONS = {
    "admin": ["admin:users", "admin:config", "words:read", "words:star"],
    "user":  ["words:read", "words:star"],
}


def get_effective_permissions(user: dict[str, Any]) -> list[str]:
    """合并角色权限 + 用户额外权限"""
    role = user.get("role", "user")
    base = ROLE_PERMISSIONS.get(role, [])
    extra = user.get("permissions", [])
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (json.JSONDecodeError, TypeError):
            extra = []
    return list(set(base + extra))


def check_permission(user: dict[str, Any], required: str) -> bool:
    """检查用户是否有指定权限（严格最小权限，不再因单一权限放行全部）。

    旧实现里 `"admin:users" in effective → return True`，使「用户管理」权限
    隐含了「配置管理」等其余全部权限，最小权限原则失效。role=admin 仍拥有
    全部权限，因为 ROLE_PERMISSIONS["admin"] 本身已列出所有权限项。
    """
    effective = get_effective_permissions(user)
    return required in effective


# ---------------------------------------------------------------------------
# 安全事件日志
# ---------------------------------------------------------------------------
def _log_event(user_id: Optional[int], username: str, event_type: str,
               details: str = "", ip: str = ""):
    try:
        conn = sqlite3.connect(USERS_DB_PATH)
        conn.execute(
            "INSERT INTO auth_events(user_id, username, event_type, details, ip_address) VALUES(?,?,?,?,?)",
            (user_id or 0, username, event_type, details, ip),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # 日志失败不应阻止主流程


def get_recent_events(limit: int = 50) -> list[dict[str, Any]]:
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM auth_events ORDER BY id DESC LIMIT ?", (limit,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# HTTP 鉴权中间件（用于 server.py 接入）
# ---------------------------------------------------------------------------
def extract_bearer_token(headers: HTTPMessage | dict[str, str]) -> str | None:
    """从 Authorization 头提取 Bearer Token（兼容 http.client.HTTPMessage）"""
    auth = headers.get("Authorization", "") if isinstance(headers, dict) else headers.get("Authorization", "")
    if isinstance(auth, str) and auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def extract_client_ip(request_handler: Any) -> str:
    """从请求中提取客户端 IP（Cloudflare / 反向代理场景优先取真实 IP）"""
    for header in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        val = request_handler.headers.get(header, "")
        if val:
            return val.split(",")[0].strip()
    try:
        return request_handler.client_address[0]
    except (IndexError, TypeError):
        return ""


def require_auth(optional: bool = False):
    """
    装饰器：为 API 方法添加 Bearer Token 认证。
    - 默认模式：无有效 Token 返回 401。
    - optional=True：有 Token 则解析用户挂到请求上，无 Token 也放行（公开接口）。
    用法：
        @require_auth()
        def api_xxx(self, qs): ...
    """
    def decorator(func: Callable[..., Any]):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            token = extract_bearer_token(self.headers)
            if token:
                user = validate_session(token)
                if user:
                    self._auth_user = user
                    return func(self, *args, **kwargs)
            if optional:
                self._auth_user = None
                return func(self, *args, **kwargs)
            self._send_json(
                {"error": "unauthorized", "message": "请先登录"},
                status=401,
            )
        return wrapper
    return decorator


def require_permission(permission: str):
    """装饰器：要求指定权限，需与 @require_auth() 配合使用"""
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            user = getattr(self, "_auth_user", None)
            if not user:
                self._send_json(
                    {"error": "unauthorized", "message": "请先登录"},
                    status=401,
                )
                return
            if not check_permission(user, permission):
                self._send_json(
                    {"error": "forbidden", "message": f"缺少权限: {permission}"},
                    status=403,
                )
                return
            return func(self, *args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# OIDC Discovery（标准端点发现）
# ---------------------------------------------------------------------------
def get_oidc_configuration(base_url: str) -> dict[str, Any]:
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/api/auth/authorize",
        "token_endpoint": f"{base_url}/api/auth/token",
        "userinfo_endpoint": f"{base_url}/api/auth/userinfo",
        "end_session_endpoint": f"{base_url}/api/auth/logout",
        "jwks_uri": "",
        "scopes_supported": ["openid", "profile", "offline_access"],
        "response_types_supported": ["code", "token", "id_token"],
        "grant_types_supported": ["authorization_code", "refresh_token", "password"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["HS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }


# ---------------------------------------------------------------------------
# 初始化（模块导入时执行）
# ---------------------------------------------------------------------------
init_auth_db()
