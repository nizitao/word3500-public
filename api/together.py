#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""「一起背」学习房间 —— 数据层（零第三方依赖）。

设计依据：docs/学习核心功能设计方案.md §5.8

核心约束：
1. 成员进度**只读消费** `daily_stats`，不另建进度体系，与单人模式口径完全一致；
2. 房间表放在 `words.db`（与 `daily_stats` 同库，成员关系可直接 JOIN），
   成员名称仍从 `users.db` 经 `auth.list_users()` 在应用层内存合并（SQL 无法跨库 JOIN）；
3. 首版只做「共享词表 + 进度可见 + 互相点赞」，不做实时对战；
4. 所有 user_id 由服务端从鉴权信息取得，绝不接受客户端传入的身份。

本模块只被 api/server.py 调用；不反向导入 server，避免循环依赖。
"""
import re
import secrets
import sys
from typing import Any, Optional
from urllib.parse import quote

from api import auth, db, study

WORDS_DB_PATH = db.WORDS_DB_PATH

# ---- 业务常量 ---------------------------------------------------------------
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆的 0/O/1/I
CODE_LEN = 6
MAX_ROOMS_PER_USER = 5        # 单人最多加入的房间数，防刷
MAX_MEMBERS = 30              # 单房间人数上限（一个班的量级）
NAME_MAX = 24

# 分册名称唯一定义在 api/db.py
BOOK_NAMES = db.BOOK_NAMES


# ---------------------------------------------------------------------------
# 连接（统一走 api/db.py，保持同一套 WAL / busy_timeout / Row 设置）
# ---------------------------------------------------------------------------
def connect(readonly: bool = False):
    return db.connect(readonly=readonly)


def _write_conn():
    """写连接：关闭隐式事务，由调用方显式 BEGIN IMMEDIATE 控制边界。"""
    return db.write_conn()


def init() -> None:
    """幂等建表，模块导入即执行（表结构唯一定义在 api/db.py）。"""
    db.init_schema()


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _norm_code(code: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code or "").strip().upper())


def _norm_name(name: Any) -> str:
    n = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "").strip())
    return n[:NAME_MAX]


def _users_map() -> dict[int, dict[str, Any]]:
    return {int(u["id"]): u for u in auth.list_users()}


def _display(u: Optional[dict[str, Any]], uid: int) -> tuple[str, bool]:
    """返回 (展示名, 是否有效账号)。沿用排行榜口径：排除禁用与游客。"""
    if not u:
        return f"用户#{uid}", False
    username = str(u.get("username") or "")
    active = bool(int(u.get("enabled", 1))) and not auth.is_guest_username(username)
    name = str(u.get("display_name") or "").strip() or username or f"用户#{uid}"
    return name, active


def _scope(book: Any, unit: Any, conn) -> tuple[Optional[int], Optional[str], str]:
    """校验并归一化共享词表范围；返回 (book, unit, 错误文案)。"""
    b: Optional[int] = None
    if book not in (None, "", 0, "0"):
        try:
            b = int(book)
        except (TypeError, ValueError):
            return None, None, "分册参数无效"
        if b not in BOOK_NAMES:
            return None, None, "分册需在 1–7 之间"
    u = str(unit or "").strip()[:40] or None
    if u and b is None:
        return None, None, "选择单元前请先选择分册"
    if b is not None and u:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM words WHERE book=? AND unit=?", (b, u)
        ).fetchone()
        if not row or not row["c"]:
            return None, None, "该分册下没有这个单元"
    return b, u, ""


def _word_count(conn, book: Optional[int], unit: Optional[str]) -> int:
    if book is None:
        return int(conn.execute("SELECT COUNT(*) AS c FROM words").fetchone()["c"])
    if unit:
        return int(conn.execute(
            "SELECT COUNT(*) AS c FROM words WHERE book=? AND unit=?", (book, unit)
        ).fetchone()["c"])
    return int(conn.execute(
        "SELECT COUNT(*) AS c FROM words WHERE book=?", (book,)
    ).fetchone()["c"])


def _scope_label(book: Optional[int], unit: Optional[str]) -> str:
    if book is None:
        return "全部词库"
    base = BOOK_NAMES.get(book, f"第 {book} 册")
    return f"{base} · {unit}" if unit else base


def _study_url(book: Optional[int], unit: Optional[str]) -> str:
    """房间约定词表的一键开背链接（quiz.js 支持 book/unit 深链）。"""
    if book is None:
        return "/quiz"
    q = f"/quiz?book={book}"
    if unit:
        q += "&unit=" + quote(unit, safe="")
    return q


def _room_brief(conn, r, today: str) -> dict[str, Any]:
    """房间列表用摘要：成员数、今日打卡人数、我的今日进度。"""
    rid = int(r["id"])
    # LEFT JOIN 保留没有当日统计的成员：他们计 0，但仍计入成员数
    agg = conn.execute(
        "SELECT COUNT(*) AS member_count,"
        "       COALESCE(SUM(CASE WHEN d.checked_in=1 THEN 1 ELSE 0 END),0) AS checked,"
        "       COALESCE(SUM(d.answered),0) AS answered,"
        "       COALESCE(SUM(d.new_words),0) AS new_words "
        "FROM room_members m LEFT JOIN daily_stats d"
        "  ON d.user_id=m.user_id AND d.day=? "
        "WHERE m.room_id=?", (today, rid)
    ).fetchone()
    return {
        "id": rid,
        "code": r["code"],
        "name": r["name"],
        "scope_book": r["scope_book"],
        "scope_unit": r["scope_unit"],
        "scope_label": _scope_label(r["scope_book"], r["scope_unit"]),
        "study_url": _study_url(r["scope_book"], r["scope_unit"]),
        "is_owner": False,       # 由调用方按 user_id 覆盖
        "member_count": int(agg["member_count"] or 0),
        "checked_count": int(agg["checked"] or 0),
        "answered": int(agg["answered"] or 0),
        "new_words": int(agg["new_words"] or 0),
        "created_at": r["created_at"],
    }


def _gen_code(conn) -> str:
    for _ in range(30):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if not conn.execute(
            "SELECT 1 FROM study_rooms WHERE code=?", (code,)
        ).fetchone():
            return code
    return ""


def _find_room(conn, code: str):
    return conn.execute("SELECT * FROM study_rooms WHERE code=?", (code,)).fetchone()


def _is_member(conn, room_id: int, user_id: int) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM room_members WHERE room_id=? AND user_id=?", (room_id, user_id)
    ).fetchone())


def _err(error: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message}


def _internal_error(error: str, exc: Exception) -> dict[str, Any]:
    """内部异常只回固定文案，明细写服务端 stderr。

    str(e) 会把 SQLite 的列名、约束名、表结构直接回给客户端（信息泄漏），
    排查需要的信息在本地日志里看，不进 HTTP 响应。
    """
    print(f"[together] {error}: {type(exc).__name__}: {exc}",
          file=sys.stderr, flush=True)
    return _err(error, "操作失败，请稍后重试")


# ---------------------------------------------------------------------------
# 写入：房间 / 成员 / 点赞
# ---------------------------------------------------------------------------
def create_room(user_id: int, name: Any, book: Any, unit: Any) -> dict[str, Any]:
    """创建房间并把创建者作为第一个成员加入。"""
    n = _norm_name(name)
    if not n:
        return _err("bad_name", "请填写房间名称")
    conn = _write_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        joined = int(conn.execute(
            "SELECT COUNT(*) AS c FROM room_members WHERE user_id=?", (user_id,)
        ).fetchone()["c"])
        if joined >= MAX_ROOMS_PER_USER:
            conn.rollback()
            return _err("too_many_rooms", f"最多加入 {MAX_ROOMS_PER_USER} 个房间，请先退出一个")
        b, u, msg = _scope(book, unit, conn)
        if msg:
            conn.rollback()
            return _err("bad_scope", msg)
        code = _gen_code(conn)
        if not code:
            conn.rollback()
            return _err("code_failed", "邀请码生成失败，请重试")
        cur = conn.execute(
            "INSERT INTO study_rooms(code,name,owner_user_id,scope_book,scope_unit,max_members)"
            " VALUES(?,?,?,?,?,?)", (code, n, user_id, b, u, MAX_MEMBERS))
        rid = int(cur.lastrowid)
        conn.execute("INSERT INTO room_members(room_id,user_id) VALUES(?,?)", (rid, user_id))
        conn.commit()
        return {"ok": True, "code": code, "room_id": rid, "name": n}
    except Exception as e:
        conn.rollback()
        return _internal_error("create_failed", e)
    finally:
        conn.close()


def join_room(user_id: int, code: Any) -> dict[str, Any]:
    """凭邀请码加入房间。

    已是成员时**幂等成功**，但用 already=True 显式标记，并带上房间名，
    供前端提示「你已在该房间里，无需重复加入」——不能静默当成新加入。
    """
    c = _norm_code(code)
    if len(c) != CODE_LEN:
        return _err("bad_code", "请输入 6 位邀请码")
    conn = _write_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = _find_room(conn, c)
        if not room:
            conn.rollback()
            return _err("not_found", "邀请码不存在，请核对后重试")
        rid = int(room["id"])
        if _is_member(conn, rid, user_id):
            conn.commit()
            return {"ok": True, "code": c, "already": True,
                    "room_id": rid, "name": room["name"]}
        joined = int(conn.execute(
            "SELECT COUNT(*) AS n FROM room_members WHERE user_id=?", (user_id,)
        ).fetchone()["n"])
        if joined >= MAX_ROOMS_PER_USER:
            conn.rollback()
            return _err("too_many_rooms", f"最多加入 {MAX_ROOMS_PER_USER} 个房间，请先退出一个")
        members = int(conn.execute(
            "SELECT COUNT(*) AS n FROM room_members WHERE room_id=?", (rid,)
        ).fetchone()["n"])
        if members >= int(room["max_members"] or MAX_MEMBERS):
            conn.rollback()
            return _err("room_full", "房间已满，请联系房主")
        conn.execute("INSERT INTO room_members(room_id,user_id) VALUES(?,?)", (rid, user_id))
        conn.commit()
        return {"ok": True, "code": c, "already": False,
                "room_id": rid, "name": room["name"]}
    except Exception as e:
        conn.rollback()
        return _internal_error("join_failed", e)
    finally:
        conn.close()


def leave_room(user_id: int, code: Any) -> dict[str, Any]:
    """退出房间。房主退出时把房主转给最早加入的成员；无人则解散房间。"""
    c = _norm_code(code)
    conn = _write_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = _find_room(conn, c)
        if not room:
            conn.rollback()
            return _err("not_found", "房间不存在")
        rid = int(room["id"])
        if not _is_member(conn, rid, user_id):
            conn.rollback()
            return _err("not_member", "你不在这个房间里")
        conn.execute("DELETE FROM room_members WHERE room_id=? AND user_id=?", (rid, user_id))
        nxt = conn.execute(
            "SELECT user_id FROM room_members WHERE room_id=? ORDER BY joined_at, user_id LIMIT 1",
            (rid,)).fetchone()
        if not nxt:
            _purge_room(conn, rid)
            conn.commit()
            return {"ok": True, "dissolved": True}
        if int(room["owner_user_id"]) == user_id:
            conn.execute("UPDATE study_rooms SET owner_user_id=? WHERE id=?",
                         (int(nxt["user_id"]), rid))
        conn.commit()
        return {"ok": True, "dissolved": False}
    except Exception as e:
        conn.rollback()
        return _internal_error("leave_failed", e)
    finally:
        conn.close()


def dissolve_room(user_id: int, code: Any) -> dict[str, Any]:
    """仅房主可解散，删除房间、成员关系与点赞记录。"""
    c = _norm_code(code)
    conn = _write_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = _find_room(conn, c)
        if not room:
            conn.rollback()
            return _err("not_found", "房间不存在")
        if int(room["owner_user_id"]) != user_id:
            conn.rollback()
            return _err("forbidden", "只有房主可以解散房间")
        _purge_room(conn, int(room["id"]))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        return _internal_error("dissolve_failed", e)
    finally:
        conn.close()


def _purge_room(conn, room_id: int) -> None:
    conn.execute("DELETE FROM room_likes WHERE room_id=?", (room_id,))
    conn.execute("DELETE FROM room_members WHERE room_id=?", (room_id,))
    conn.execute("DELETE FROM study_rooms WHERE id=?", (room_id,))


def like_member(user_id: int, code: Any, to_user_id: Any,
                tz_minutes: Any = 0) -> dict[str, Any]:
    """给房间成员点赞：每人每天对同一成员限 1 次（主键幂等），不能给自己点。"""
    c = _norm_code(code)
    try:
        target = int(to_user_id)
    except (TypeError, ValueError):
        return _err("bad_target", "点赞对象无效")
    if target == user_id:
        return _err("self_like", "不能给自己点赞")
    day = study.local_day(tz_minutes)
    conn = _write_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = _find_room(conn, c)
        if not room:
            conn.rollback()
            return _err("not_found", "房间不存在")
        rid = int(room["id"])
        if not _is_member(conn, rid, user_id) or not _is_member(conn, rid, target):
            conn.rollback()
            return _err("not_member", "点赞双方须为同一房间成员")
        cur = conn.execute(
            "INSERT OR IGNORE INTO room_likes(room_id,from_user_id,to_user_id,day)"
            " VALUES(?,?,?,?)", (rid, user_id, target, day))
        conn.commit()
        today_n = int(conn.execute(
            "SELECT COUNT(*) AS n FROM room_likes WHERE room_id=? AND to_user_id=? AND day=?",
            (rid, target, day)).fetchone()["n"])
        total_n = int(conn.execute(
            "SELECT COUNT(*) AS n FROM room_likes WHERE room_id=? AND to_user_id=?",
            (rid, target)).fetchone()["n"])
        return {"ok": True, "already": cur.rowcount == 0,
                "likes_today": today_n, "likes_total": total_n}
    except Exception as e:
        conn.rollback()
        return _internal_error("like_failed", e)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 读取：房间列表 / 房间详情 / 可选词表
# ---------------------------------------------------------------------------
def list_rooms(user_id: int, tz_minutes: Any = 0) -> dict[str, Any]:
    """我的房间列表 + 每个房间的今日汇总。"""
    today = study.local_day(tz_minutes)
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT r.* FROM study_rooms r"
            " JOIN room_members m ON m.room_id=r.id"
            " WHERE m.user_id=? ORDER BY r.created_at DESC, r.id DESC",
            (user_id,)).fetchall()
        rooms = []
        for r in rows:
            brief = _room_brief(conn, r, today)
            brief["is_owner"] = int(r["owner_user_id"]) == user_id
            mine = conn.execute(
                "SELECT answered,new_words,checked_in FROM daily_stats"
                " WHERE user_id=? AND day=?", (user_id, today)).fetchone()
            brief["my_answered"] = int(mine["answered"]) if mine else 0
            brief["my_new_words"] = int(mine["new_words"]) if mine else 0
            brief["my_checked_in"] = int(mine["checked_in"]) if mine else 0
            rooms.append(brief)
    finally:
        conn.close()
    return {"ok": True, "day": today, "rooms": rooms,
            "max_rooms": MAX_ROOMS_PER_USER, "max_members": MAX_MEMBERS}


def room_detail(user_id: int, code: Any, tz_minutes: Any = 0) -> dict[str, Any]:
    """房间详情：共享词表 + 每位成员当日进度 / 打卡 / 连续天数 / 点赞。"""
    c = _norm_code(code)
    today = study.local_day(tz_minutes)
    conn = connect(readonly=True)
    try:
        room = _find_room(conn, c)
        if not room:
            return _err("not_found", "房间不存在或已解散")
        rid = int(room["id"])
        if not _is_member(conn, rid, user_id):
            return _err("not_member", "请先加入房间再查看成员进度")
        word_count = _word_count(conn, room["scope_book"], room["scope_unit"])
        member_rows = conn.execute(
            "SELECT user_id, joined_at FROM room_members WHERE room_id=?"
            " ORDER BY joined_at, user_id", (rid,)).fetchall()
        users = _users_map()

        likes_today = {int(r["to_user_id"]): int(r["n"]) for r in conn.execute(
            "SELECT to_user_id, COUNT(*) AS n FROM room_likes"
            " WHERE room_id=? AND day=? GROUP BY to_user_id", (rid, today))}
        likes_total = {int(r["to_user_id"]): int(r["n"]) for r in conn.execute(
            "SELECT to_user_id, COUNT(*) AS n FROM room_likes"
            " WHERE room_id=? GROUP BY to_user_id", (rid,))}
        liked_today = {int(r[0]) for r in conn.execute(
            "SELECT to_user_id FROM room_likes"
            " WHERE room_id=? AND from_user_id=? AND day=?", (rid, user_id, today))}

        members = []
        for m in member_rows:
            uid = int(m["user_id"])
            name, active = _display(users.get(uid), uid)
            st = conn.execute(
                "SELECT new_words,reviewed,answered,correct,checked_in,makeup"
                " FROM daily_stats WHERE user_id=? AND day=?", (uid, today)).fetchone()
            answered = int(st["answered"]) if st else 0
            new_words = int(st["new_words"]) if st else 0
            checked_in = int(st["checked_in"]) if st else 0
            days = [x[0] for x in conn.execute(
                "SELECT day FROM daily_stats WHERE user_id=? AND checked_in=1"
                " AND makeup=0 ORDER BY day", (uid,))]
            # 进度与打卡阈值同口径：答题 10 题或新学 5 词，取较大者
            pct = min(100, int(round(max(answered / study.CHECKIN_ANSWERED,
                                        new_words / study.CHECKIN_NEW_WORDS) * 100)))
            if checked_in:
                pct = 100
            members.append({
                "user_id": uid,
                "name": name,
                "active": active,
                "is_me": uid == user_id,
                "is_owner": uid == int(room["owner_user_id"]),
                "joined_at": m["joined_at"],
                "answered": answered,
                "correct": int(st["correct"]) if st else 0,
                "new_words": new_words,
                "reviewed": int(st["reviewed"]) if st else 0,
                "checked_in": checked_in,
                "makeup": int(st["makeup"]) if st else 0,
                "progress": pct,
                "streak": study.calc_streak(days, today),
                "likes_today": likes_today.get(uid, 0),
                "likes_total": likes_total.get(uid, 0),
                "liked_by_me": uid in liked_today,
            })
        members.sort(key=lambda x: (-x["progress"], -x["answered"], x["user_id"]))

        checked_count = sum(1 for x in members if x["checked_in"])
        info = {
            "id": rid,
            "code": room["code"],
            "name": room["name"],
            "is_owner": int(room["owner_user_id"]) == user_id,
            "owner_user_id": int(room["owner_user_id"]),
            "scope_book": room["scope_book"],
            "scope_unit": room["scope_unit"],
            "scope_label": _scope_label(room["scope_book"], room["scope_unit"]),
            "study_url": _study_url(room["scope_book"], room["scope_unit"]),
            "word_count": word_count,
            "max_members": int(room["max_members"] or MAX_MEMBERS),
            "member_count": len(members),
            "checked_count": checked_count,
            "answered_today": sum(x["answered"] for x in members),
            "created_at": room["created_at"],
        }
    finally:
        conn.close()
    return {"ok": True, "day": today, "room": info, "members": members,
            "goal": {"answered": study.CHECKIN_ANSWERED,
                     "new_words": study.CHECKIN_NEW_WORDS}}


def scope_options() -> dict[str, Any]:
    """创建房间时的可选词表范围：册 → 单元（含词数），以及全部词库。"""
    conn = connect(readonly=True)
    try:
        total = int(conn.execute("SELECT COUNT(*) AS c FROM words").fetchone()["c"])
        rows = conn.execute(
            "SELECT book, unit, COUNT(*) AS c FROM words"
            " WHERE book IS NOT NULL AND TRIM(book) <> '' AND unit IS NOT NULL"
            "   AND TRIM(unit) <> ''"
            " GROUP BY book, unit ORDER BY CAST(book AS INTEGER), unit").fetchall()
        books: dict[int, dict[str, Any]] = {}
        for r in rows:
            try:
                b = int(r["book"])
            except (TypeError, ValueError):
                continue
            if b not in BOOK_NAMES:
                continue
            item = books.setdefault(b, {"book": b, "name": BOOK_NAMES[b],
                                        "count": 0, "units": []})
            n = int(r["c"])
            item["count"] += n
            item["units"].append({"name": r["unit"], "count": n})
    finally:
        conn.close()
    return {"ok": True, "total": total,
            "books": [books[k] for k in sorted(books)]}


init()
