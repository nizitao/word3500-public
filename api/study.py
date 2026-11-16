#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""学习统计与激励体系 —— 数据层 + 业务聚合（零第三方依赖）。

设计依据：docs/学习核心功能设计方案.md（v1.2）

核心原则：单一事实来源。所有统计模块都从追加型事件流 `study_events` 派生，
`learn_progress` 只由服务端写入（master / seen / correct / next_review_at 均服务端权威），
`daily_stats` 是读优化层，不各自记账。

**唯一派生函数**：`_load_word_states()` 从事件流算出每个词的权威状态，
`_apply_word_states()` 落库。热路径（`record_events`）、兼容导入（`import_progress`）、
自愈（`rebuild_progress`）全部走这两个函数 + `_refresh_daily` / `_rebuild_daily_history`，
不再各处手写 UPDATE 语句，避免「自愈重算的口径」和「实时写入的口径」漂移。

本模块只被 api/server.py 调用；不反向导入 server，避免循环依赖。
"""
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from api import auth, db

DATA_DIR = db.DATA_DIR
WORDS_DB_PATH = db.WORDS_DB_PATH

# 计入「答题」的动作：只有这些动作会累加 correct / 影响调度；'learn' 只算浏览
ANSWER_ACTIONS = ("quiz", "review", "selfcheck")

# ---- 业务常量（设计文档 §5.1 / §5.2 / §5.6）------------------------------
ACTIONS = ("learn", "quiz", "review", "selfcheck")
CHECKIN_ANSWERED = 10        # 当日答题 ≥10 视为打卡
CHECKIN_NEW_WORDS = 5        # 或当日新学 ≥5 词
MAKEUP_PER_MONTH = 2         # 每月补签次数
MAX_EVENTS_PER_BATCH = 200   # 单批上限，防超大请求

STREAK_BADGES = (3, 7, 14, 30, 60, 100, 365)
MASTER_BADGES = (10, 30, 100, 500, 1000)
HOUR_BADGES = (10, 50, 100)
SECONDS_PER_ANSWER = 30      # 学时估算：每题按 30 秒计

# 词库总量（词库覆盖率参考基准，实际值运行时读取）
WORD_BOOK_TOTAL = 5093


# ---------------------------------------------------------------------------
# 连接（统一走 api/db.py，保持同一套 WAL / busy_timeout / Row 设置）
# ---------------------------------------------------------------------------
def connect(readonly: bool = False) -> sqlite3.Connection:
    return db.connect(readonly=readonly)


def _write_conn() -> sqlite3.Connection:
    """写连接：关闭隐式事务，由调用方显式 BEGIN IMMEDIATE 控制边界。"""
    return db.write_conn()


# ---------------------------------------------------------------------------
# 词性解析（设计文档 §14）
# ---------------------------------------------------------------------------
LEAD_NOISE = re.compile(r"^[\s\*\-·•\.,;:、，。]+")
PAREN_HEAD = re.compile(r"^[\(（][^\)）]{0,20}[\)）]\s*")
TOKEN_RE = re.compile(
    r"(?:(?:n|vt|vi|v|adj|adv|prep|conj|pron|art|num|interj|int|aux|abbr|phr)\s*\.)"
    r"|(?:短语\s*\.?)", re.I)
HEAD_RE = re.compile(
    r"^(?:(?:(?:n|vt|vi|v|adj|adv|prep|conj|pron|art|num|interj|int|aux|abbr|phr)\s*\.)"
    r"|(?:短语\s*\.?)|[\s&/,、；;]|和)+", re.I)

MERGE = {"vt": "v", "vi": "v", "int": "interj"}
PRIORITY = ["phr", "n", "v", "adj", "adv", "pron", "prep",
            "conj", "num", "interj", "art", "aux", "abbr"]

# 图表展示归并：10 类（§4.3）
POS_DISPLAY = [
    ("n", "名词"), ("adj", "形容词"), ("v", "动词"), ("other", "其他"),
    ("adv", "副词"), ("phr", "短语"), ("pron", "代词"), ("prep", "介词"),
    ("num", "数词"), ("conj", "连词"),
]
POS_OTHER_MEMBERS = ("other", "abbr", "interj", "art", "aux", "")


def parse_pos(meaning: str) -> str:
    """从中文释义前缀解析主词性；解析失败返回 other（绝不返回空串）。"""
    s = LEAD_NOISE.sub("", meaning or "")
    s = PAREN_HEAD.sub("", s)
    s = LEAD_NOISE.sub("", s)
    head = HEAD_RE.match(s)
    if not head or not head.group(0).strip():
        return "other"
    toks = [t.lower().rstrip(".").strip() for t in TOKEN_RE.findall(head.group(0))]
    toks = ["phr" if t.startswith("短语") else t for t in toks]
    toks = [MERGE.get(t, t) for t in toks if t]
    if not toks:
        return "other"
    return next((p for p in PRIORITY if p in toks), "other")


# ---------------------------------------------------------------------------
# 建表 / 迁移（幂等）—— DDL 唯一定义在 api/db.py
# ---------------------------------------------------------------------------
def init() -> None:
    """建表 + 加列 + 词性回填 + 当日基线。可重复执行，幂等。

    本函数在模块导入时执行一次。`api/db.init_schema()` 自己会建 `words` 表，
    所以全新克隆首次启动也不再有「表还不存在」的顺序问题。
    """
    conn = connect()
    try:
        db.init_schema(conn)
        has_words = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='words'"
        ).fetchone())
        if has_words:
            backfill_pos(conn)
        baseline_today(conn)
        conn.commit()
    finally:
        conn.close()


def backfill_pos(conn: sqlite3.Connection) -> int:
    """回填 words.pos；只处理空值，重复执行不覆盖已有结果。"""
    rows = conn.execute(
        "SELECT id, meaning FROM words WHERE pos IS NULL OR pos=''").fetchall()
    if not rows:
        return 0
    conn.executemany(
        "UPDATE words SET pos=? WHERE id=?",
        [(parse_pos(r["meaning"]), r["id"]) for r in rows],
    )
    conn.commit()
    return len(rows)


def baseline_today(conn: sqlite3.Connection) -> int:
    """为已有学习记录的用户写入当日基线，避免折线历史段为空。"""
    day = local_day(0)
    users = [r[0] for r in conn.execute(
        "SELECT DISTINCT user_id FROM learn_progress WHERE seen>0")]
    written = 0
    for uid in users:
        learned, mastered = _totals(conn, int(uid))
        cur = conn.execute(
            """INSERT INTO daily_stats (user_id,day,learned_total,mastered_total)
               VALUES (?,?,?,?) ON CONFLICT(user_id,day) DO NOTHING""",
            (uid, day, learned, mastered))
        written += cur.rowcount or 0
    return written


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def local_day(tz_minutes: Any = 0, offset_days: int = 0) -> str:
    """按客户端时区偏移取本地日（YYYY-MM-DD）。"""
    try:
        tz = max(-840, min(840, int(tz_minutes)))
    except (TypeError, ValueError):
        tz = 0
    return (datetime.now(timezone.utc) + timedelta(minutes=tz, days=offset_days)).strftime("%Y-%m-%d")


def _utc_bound(local_day_str: str, tz_minutes: Any = 0) -> str:
    """本地日 00:00 对应的 UTC 字符串，用于与 next_review_at 比较。"""
    try:
        tz = max(-840, min(840, int(tz_minutes)))
    except (TypeError, ValueError):
        tz = 0
    try:
        d = datetime.strptime(local_day_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        d = datetime.now(timezone.utc)
    return (d - timedelta(minutes=tz)).strftime("%Y-%m-%d %H:%M:%S")


def compute_master(seen: int, correct: int) -> int:
    """与前端既有规则完全一致：≥3 次答对且正确率 ≥80%。"""
    if seen <= 0:
        return 0
    return 1 if (correct >= 3 and correct / seen >= 0.8) else 0


def _totals(conn: sqlite3.Connection, user_id: int) -> tuple[int, int]:
    row = conn.execute(
        "SELECT SUM(CASE WHEN seen>0 THEN 1 ELSE 0 END) AS learned,"
        "       SUM(CASE WHEN master=1 THEN 1 ELSE 0 END) AS mastered "
        "FROM learn_progress WHERE user_id=?", (user_id,)).fetchone()
    return int(row["learned"] or 0), int(row["mastered"] or 0)


def _checked_days(conn: sqlite3.Connection, user_id: int,
                  real_only: bool = False) -> list[str]:
    sql = "SELECT day FROM daily_stats WHERE user_id=? AND checked_in=1"
    if real_only:
        sql += " AND makeup=0"
    sql += " ORDER BY day"
    return [r[0] for r in conn.execute(sql, (user_id,))]


def calc_streak(days: list[str], today: str) -> int:
    """连续天数：今天未打卡不清零（当天 24:00 前有效），跨日未打卡才断。"""
    if not days:
        return 0
    have = set(days)
    try:
        t = datetime.strptime(today, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 0
    if t.isoformat() in have:
        cur = t
    elif (t - timedelta(days=1)).isoformat() in have:
        cur = t - timedelta(days=1)
    else:
        return 0
    n = 0
    while cur.isoformat() in have:
        n += 1
        cur -= timedelta(days=1)
    return n


def calc_best_streak(days: list[str]) -> int:
    if not days:
        return 0
    best = run = 1
    prev = None
    for d in sorted(set(days)):
        try:
            cur = datetime.strptime(d, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if prev is not None and (cur - prev).days == 1:
            run += 1
        else:
            run = 1
        best = max(best, run)
        prev = cur
    return best


# ---------------------------------------------------------------------------
# 写入：事件流（唯一入口）
# ---------------------------------------------------------------------------
def _trailing_streak(results: list[int]) -> int:
    n = 0
    for r in results:
        if r == 1:
            n += 1
        else:
            break
    return n


def _review_days(streak: int, wrong_last: bool) -> int:
    """简化艾宾浩斯：1 次 +1 天 / 2 次 +3 天 / ≥3 次 +7 天；答错重置 +1 天。"""
    if wrong_last or streak < 2:
        return 1
    return 3 if streak == 2 else 7


def _next_review_from(base_ts: Any, streak: int, wrong_last: bool) -> str:
    """以**最后一次事件时间**为基准推算下次复习时间。

    用事件时间而非 `now()` 作基准，自愈重算才是幂等的：同一批事件无论重算多少次，
    得到的 `next_review_at` 完全一致；否则每次 rebuild 都会把到期日整体往后推。
    """
    text = str(base_ts or "")[:19]
    try:
        base = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        base = datetime.now(timezone.utc).replace(tzinfo=None)
    return (base + timedelta(days=_review_days(streak, wrong_last))).strftime("%Y-%m-%d %H:%M:%S")


def _next_review(streak: int, wrong_last: bool) -> str:
    """（兼容保留）以当前时间为基准的下次复习时间。"""
    return _next_review_from(
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), streak, wrong_last)


# ---------------------------------------------------------------------------
# 派生：事件流 → learn_progress（唯一口径）
# ---------------------------------------------------------------------------
def _load_word_states(conn: sqlite3.Connection, user_id: int,
                      word_ids: Optional[Iterable[int]] = None,
                      ) -> dict[int, dict[str, Any]]:
    """按事件流重建每个词的权威状态（seen / correct / master / next_review_at）。

    `word_ids=None` 表示该用户全部有事件的词（自愈用）；给定集合时只算这些词
    （热路径用，代价与命中词数成正比而非与总事件数成正比）。

    master 规则：最近一次答题答错 → 直接 0；否则 compute_master(seen, correct)。
    """
    where = "WHERE user_id=?"
    args: list[Any] = [user_id]
    if word_ids is not None:
        ids = sorted({int(w) for w in word_ids})
        if not ids:
            return {}
        where += " AND word_id IN (" + ",".join("?" * len(ids)) + ")"
        args.extend(ids)
    state: dict[int, dict[str, Any]] = {}
    for r in conn.execute(
            f"SELECT word_id, action, result, ts FROM study_events {where} ORDER BY id", args):
        wid = int(r["word_id"])
        st = state.get(wid)
        if st is None:
            st = {"seen": 0, "correct": 0, "answers": [], "last_ts": r["ts"]}
            state[wid] = st
        st["seen"] += 1
        action = str(r["action"] or "")
        result = int(r["result"] or 0)
        if action in ANSWER_ACTIONS:
            if result == 1:
                st["correct"] += 1
            answers: list[int] = st["answers"]
            answers.append(result)
            if len(answers) > 5:
                del answers[:-5]
        st["last_ts"] = r["ts"]
    for st in state.values():
        tail = list(reversed(st["answers"]))
        wrong_last = bool(tail) and tail[0] != 1
        st["master"] = 0 if wrong_last else compute_master(int(st["seen"]), int(st["correct"]))
        st["next_review_at"] = _next_review_from(
            st["last_ts"], _trailing_streak(tail), wrong_last)
    return state


def _apply_word_states(conn: sqlite3.Connection, user_id: int,
                       states: dict[int, dict[str, Any]]) -> None:
    """把 `_load_word_states()` 的结果写回 learn_progress（**覆盖**而非累加）。"""
    for wid, st in states.items():
        conn.execute(
            """INSERT INTO learn_progress
                 (user_id, word_id, seen, correct, master, next_review_at, updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(user_id, word_id) DO UPDATE SET
                 seen=excluded.seen,
                 correct=excluded.correct,
                 master=excluded.master,
                 next_review_at=excluded.next_review_at,
                 updated_at=datetime('now')""",
            (user_id, wid, int(st["seen"]), int(st["correct"]),
             int(st["master"]), st.get("next_review_at")),
        )


def _day_aggregate(conn: sqlite3.Connection, user_id: int, day: str) -> dict[str, int]:
    """单日事件聚合：答题数 / 答对数 / 复习词数 / 当日新学词数。"""
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN action IN ('quiz','review','selfcheck') AND result IN (0,1)
                      THEN 1 ELSE 0 END) AS answered,
             SUM(CASE WHEN action IN ('quiz','review','selfcheck') AND result=1
                      THEN 1 ELSE 0 END) AS correct,
             COUNT(DISTINCT CASE WHEN action='review' THEN word_id END) AS reviewed
           FROM study_events WHERE user_id=? AND day=?""", (user_id, day)).fetchone()
    new_words = int(conn.execute(
        "SELECT COUNT(*) FROM (SELECT word_id, MIN(day) AS f FROM study_events"
        " WHERE user_id=? GROUP BY word_id) WHERE f=?", (user_id, day)).fetchone()[0] or 0)
    return {
        "answered": int(row["answered"] or 0),
        "correct": int(row["correct"] or 0),
        "reviewed": int(row["reviewed"] or 0),
        "new_words": new_words,
    }


def _refresh_daily(conn: sqlite3.Connection, user_id: int, day: str) -> dict[str, Any]:
    """按事件流重算某一天的 daily_stats（保留补签标记与已达成打卡）。

    只用于「今天」的热路径：learned_total / mastered_total 取当前累计值，
    等价于「截至今天」的快照。历史日快照由 `_rebuild_daily_history` 精确重算。
    """
    agg = _day_aggregate(conn, user_id, day)
    learned, mastered = _totals(conn, user_id)
    prev = conn.execute(
        "SELECT checked_in, makeup FROM daily_stats WHERE user_id=? AND day=?",
        (user_id, day)).fetchone()
    makeup = int(prev["makeup"] or 0) if prev else 0
    hit = (agg["answered"] >= CHECKIN_ANSWERED
           or agg["new_words"] >= CHECKIN_NEW_WORDS)
    was_checked = bool(prev and prev["checked_in"])
    checked_in = 1 if (hit or makeup or was_checked) else 0
    conn.execute(
        """INSERT INTO daily_stats (user_id,day,new_words,reviewed,answered,correct,
                                    learned_total,mastered_total,checked_in,makeup)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(user_id,day) DO UPDATE SET
             new_words=excluded.new_words, reviewed=excluded.reviewed,
             answered=excluded.answered, correct=excluded.correct,
             learned_total=excluded.learned_total, mastered_total=excluded.mastered_total,
             checked_in=excluded.checked_in""",
        (user_id, day, agg["new_words"], agg["reviewed"], agg["answered"],
         agg["correct"], learned, mastered, checked_in, makeup),
    )
    return {**agg, "learned": learned, "mastered": mastered,
            "checked_in": checked_in, "was_checked": was_checked}


def _rebuild_daily_history(conn: sqlite3.Connection, user_id: int) -> int:
    """自愈用：按天重放事件流，精确重建每一天的统计与累计快照。

    不能直接用「今天的 totals」回填历史（会把历史曲线拉平到当前值）。
    单次遍历，内存里维护每个词的 seen/correct/最近答题，按天快照 learned/mastered。
    """
    rows = conn.execute(
        "SELECT word_id, action, result, day FROM study_events"
        " WHERE user_id=? ORDER BY day, id", (user_id,)).fetchall()
    if not rows:
        return 0
    per_day: dict[str, dict[str, int]] = {}
    reviewed: dict[str, set[int]] = {}
    seen: dict[int, int] = {}
    correct: dict[int, int] = {}
    answers: dict[int, list[int]] = {}
    mastered_words: set[int] = set()
    first_day: dict[int, str] = {}
    for r in rows:
        day = str(r["day"])
        agg = per_day.setdefault(
            day, {"answered": 0, "correct": 0, "new_words": 0})
        wid = int(r["word_id"])
        action = str(r["action"] or "")
        result = int(r["result"] or 0)
        seen[wid] = seen.get(wid, 0) + 1
        if wid not in first_day:
            first_day[wid] = day
            agg["new_words"] += 1
        if action in ANSWER_ACTIONS and result in (0, 1):
            agg["answered"] += 1
            if result == 1:
                correct[wid] = correct.get(wid, 0) + 1
                agg["correct"] += 1
            lst = answers.setdefault(wid, [])
            lst.append(result)
            if len(lst) > 5:
                del lst[:-5]
            tail = list(reversed(lst))
            ok = (not tail or tail[0] == 1) and compute_master(
                seen[wid], correct.get(wid, 0)) == 1
            if ok:
                mastered_words.add(wid)
            else:
                mastered_words.discard(wid)
        if action == "review":
            reviewed.setdefault(day, set()).add(wid)
        # 事件按天升序处理，当天最后一条事件处理完的值就是「当天收盘快照」
        agg["learned_total"] = len(seen)
        agg["mastered_total"] = len(mastered_words)

    existing = {r["day"]: r for r in conn.execute(
        "SELECT day, checked_in, makeup FROM daily_stats WHERE user_id=?", (user_id,))}
    for day in sorted(per_day):
        agg = per_day[day]
        learned = agg["learned_total"]
        mastered = agg["mastered_total"]
        prev = existing.get(day)
        makeup = int(prev["makeup"] or 0) if prev else 0
        was_checked = bool(prev and prev["checked_in"])
        hit = (agg["answered"] >= CHECKIN_ANSWERED
               or agg["new_words"] >= CHECKIN_NEW_WORDS)
        checked_in = 1 if (hit or makeup or was_checked) else 0
        conn.execute(
            """INSERT INTO daily_stats (user_id,day,new_words,reviewed,answered,correct,
                                        learned_total,mastered_total,checked_in,makeup)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id,day) DO UPDATE SET
                 new_words=excluded.new_words, reviewed=excluded.reviewed,
                 answered=excluded.answered, correct=excluded.correct,
                 learned_total=excluded.learned_total,
                 mastered_total=excluded.mastered_total,
                 checked_in=excluded.checked_in""",
            (user_id, day, agg["new_words"], len(reviewed.get(day, ())),
             agg["answered"], agg["correct"], learned, mastered, checked_in, makeup),
        )
    return len(per_day)


def record_events(user_id: int, events: Any, tz_minutes: Any = 0) -> dict[str, Any]:
    """批量写入学习事件，并在同一事务内派生所有状态。

    客户端只能上报 (word_id, action, result, uid)；day 由服务端按 tz 计算，
    seen / correct / master / next_review_at 全部服务端权威。
    """
    if not isinstance(events, list) or not events:
        return {"ok": False, "error": "bad_request", "message": "events 必须是非空数组"}
    if len(events) > MAX_EVENTS_PER_BATCH:
        events = events[:MAX_EVENTS_PER_BATCH]

    day = local_day(tz_minutes)
    conn = _write_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 词表校验 + 快照（pos / book 冗余写入，统计免 JOIN）
        ids = set()
        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                ids.add(int(ev.get("word_id") or 0))
            except (TypeError, ValueError):
                pass
        ids.discard(0)
        meta: dict[int, sqlite3.Row] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in conn.execute(
                    f"SELECT id, pos, book FROM words WHERE id IN ({marks})", list(ids)):
                meta[int(r["id"])] = r

        accepted = duplicates = rejected = 0
        inserted: list[int] = []
        dup_words: set[int] = set()
        for ev in events:
            if not isinstance(ev, dict):
                rejected += 1
                continue
            try:
                wid = int(ev.get("word_id") or 0)
                result = int(ev.get("result", -1))
            except (TypeError, ValueError):
                rejected += 1
                continue
            action = str(ev.get("action") or "").strip().lower()
            if wid <= 0 or wid not in meta or action not in ACTIONS:
                rejected += 1
                continue
            if result not in (-1, 0, 1):
                rejected += 1
                continue
            uid = str(ev.get("uid") or "").strip()[:64] or uuid.uuid4().hex
            try:
                cur = conn.execute(
                    "INSERT INTO study_events (user_id,word_id,action,result,day,ts,pos,book,uid)"
                    " VALUES (?,?,?,?,?,datetime('now'),?,?,?)",
                    (user_id, wid, action, result, day,
                     meta[wid]["pos"] or "", meta[wid]["book"], uid),
                )
                inserted.append(int(cur.lastrowid or 0))
                accepted += 1
            except sqlite3.IntegrityError:
                duplicates += 1   # (user_id, uid) 唯一索引：重试批次幂等丢弃
                dup_words.add(wid)

        if accepted == 0:
            # 全部为重复批次：仍回传这些词的权威状态，客户端重试后可对齐本地缓存
            echo: dict[str, dict[str, int]] = {}
            for wid in dup_words:
                row = conn.execute(
                    "SELECT seen, correct, master FROM learn_progress"
                    " WHERE user_id=? AND word_id=?", (user_id, wid)).fetchone()
                if row:
                    echo[str(wid)] = {"seen": int(row["seen"] or 0),
                                      "correct": int(row["correct"] or 0),
                                      "master": int(row["master"] or 0)}
            conn.commit()
            return {"ok": True, "accepted": 0, "duplicates": duplicates,
                    "rejected": rejected, "day": day, "progress": echo,
                    **_snapshot(conn, user_id, tz_minutes, day)}

        # ---- 派生 learn_progress（唯一口径：从事件流重算本批次命中的词）----
        marks_ids = ",".join("?" * len(inserted))
        touched = {int(r["word_id"]) for r in conn.execute(
            f"SELECT word_id FROM study_events WHERE id IN ({marks_ids})", inserted)}
        states = _load_word_states(conn, user_id, touched)
        _apply_word_states(conn, user_id, states)
        progress = {str(wid): {"seen": int(st["seen"]), "correct": int(st["correct"]),
                              "master": int(st["master"])}
                    for wid, st in states.items()}

        # ---- 派生 daily_stats ----
        daily = _refresh_daily(conn, user_id, day)
        answered = int(daily["answered"])
        mastered = int(daily["mastered"])
        checked_in = int(daily["checked_in"])
        was_checked = bool(daily["was_checked"])

        # ---- 积分 ----
        points = 0
        points += _award(conn, user_id, "first_study", 5, day)
        if checked_in and not was_checked:
            points += _award(conn, user_id, "checkin", 10, day)

        # ---- 徽章 ----
        streak = calc_streak(_checked_days(conn, user_id, real_only=True), day)
        new_badges = _check_badges(conn, user_id, streak, mastered, answered_total(conn, user_id))
        if streak and streak % 7 == 0:
            points += _award(conn, user_id, "streak_7", 50, day)

        conn.commit()
        snap = _snapshot(conn, user_id, tz_minutes, day)
        return {
            "ok": True, "accepted": accepted, "duplicates": duplicates,
            "rejected": rejected, "day": day, "progress": progress,
            "just_checked_in": bool(checked_in and not was_checked),
            "new_badges": new_badges, "points_delta": points,
            **snap,
        }
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _award(conn: sqlite3.Connection, user_id: int, reason: str, delta: int, day: str) -> int:
    """幂等加分：同一用户同一 reason 同一日只记一次。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO points_ledger (user_id,delta,reason,day) VALUES (?,?,?,?)",
        (user_id, delta, reason, day))
    return delta if (cur.rowcount or 0) > 0 else 0


def answered_total(conn: sqlite3.Connection, user_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM study_events WHERE user_id=?"
        " AND action IN ('quiz','review','selfcheck') AND result IN (0,1)",
        (user_id,)).fetchone()[0] or 0)


def _check_badges(conn: sqlite3.Connection, user_id: int, streak: int,
                  mastered: int, answers: int) -> list[str]:
    """阈值检查，INSERT OR IGNORE 幂等；已解锁永久保留。"""
    got: list[str] = []
    hours = answers * SECONDS_PER_ANSWER / 3600.0
    cands = ([f"streak_{n}" for n in STREAK_BADGES if streak >= n]
             + [f"master_{n}" for n in MASTER_BADGES if mastered >= n]
             + [f"hours_{n}" for n in HOUR_BADGES if hours >= n])
    for bid in cands:
        cur = conn.execute(
            "INSERT OR IGNORE INTO user_badges (user_id,badge_id) VALUES (?,?)",
            (user_id, bid))
        if (cur.rowcount or 0) > 0:
            got.append(bid)
    return got


# ---------------------------------------------------------------------------
# 读取：概览 / 日历 / 明细
# ---------------------------------------------------------------------------
def _snapshot(conn: sqlite3.Connection, user_id: int, tz_minutes: Any,
              day: Optional[str] = None) -> dict[str, Any]:
    day = day or local_day(tz_minutes)
    row = conn.execute(
        "SELECT * FROM daily_stats WHERE user_id=? AND day=?", (user_id, day)).fetchone()
    learned, mastered = _totals(conn, user_id)
    checked_days = _checked_days(conn, user_id, real_only=True)
    all_checked = _checked_days(conn, user_id)
    today = conn.execute(
        "SELECT COUNT(*) FROM study_events WHERE user_id=? AND day=?", (user_id, day)
    ).fetchone()[0]
    return {
        "summary": {
            "day": day,
            "checked_in": bool(row and row["checked_in"]),
            "makeup": bool(row and row["makeup"]),
            "new_words": int(row["new_words"] or 0) if row else 0,
            "reviewed": int(row["reviewed"] or 0) if row else 0,
            "answered": int(row["answered"] or 0) if row else 0,
            "correct": int(row["correct"] or 0) if row else 0,
            "learned_total": learned,
            "mastered_total": mastered,
            "streak": calc_streak(checked_days, day),
            "best_streak": calc_best_streak(all_checked),
            "points": points_balance(conn, user_id),
            "events_today": int(today or 0),
            "goal": {"answered": CHECKIN_ANSWERED, "new_words": CHECKIN_NEW_WORDS},
        }
    }


def points_balance(conn: sqlite3.Connection, user_id: int) -> int:
    return int(conn.execute(
        "SELECT COALESCE(SUM(delta),0) FROM points_ledger WHERE user_id=?",
        (user_id,)).fetchone()[0] or 0)


def summary(user_id: int, tz_minutes: Any = 0) -> dict[str, Any]:
    conn = connect(readonly=True)
    try:
        snap = _snapshot(conn, user_id, tz_minutes)
    finally:
        conn.close()
    day = snap["summary"]["day"]
    conn = connect(readonly=True)
    try:
        due = _due_count(conn, user_id, tz_minutes, day)
    finally:
        conn.close()
    snap["summary"]["due_review"] = due
    snap["summary"]["word_total"] = WORD_BOOK_TOTAL
    snap["ok"] = True
    return snap


def _due_count(conn: sqlite3.Connection, user_id: int, tz_minutes: Any, day: str) -> int:
    """待复习数 = 已学未掌握 且 next_review_at <= 今日结束。"""
    end = _utc_bound(day, tz_minutes)
    end = (datetime.strptime(end, "%Y-%m-%d %H:%M:%S") + timedelta(days=1)
           ).strftime("%Y-%m-%d %H:%M:%S")
    return int(conn.execute(
        "SELECT COUNT(*) FROM learn_progress WHERE user_id=? AND seen>0 AND master=0"
        " AND next_review_at IS NOT NULL AND next_review_at < ?",
        (user_id, end)).fetchone()[0] or 0)


def calendar(user_id: int, month: str = "", tz_minutes: Any = 0) -> dict[str, Any]:
    today = local_day(tz_minutes)
    month = (month or today[:7])[:7]
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        month = today[:7]
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT day,new_words,reviewed,answered,correct,learned_total,mastered_total,"
            "checked_in,makeup FROM daily_stats WHERE user_id=? AND day LIKE ? ORDER BY day",
            (user_id, month + "-%")).fetchall()
    finally:
        conn.close()
    return {"ok": True, "month": month, "today": today,
            "days": [dict(r) for r in rows]}


def day_detail(user_id: int, day: str = "", tz_minutes: Any = 0) -> dict[str, Any]:
    day = day or local_day(tz_minutes)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return {"ok": False, "error": "bad_day"}
    conn = connect(readonly=True)
    try:
        stat = conn.execute(
            "SELECT * FROM daily_stats WHERE user_id=? AND day=?", (user_id, day)).fetchone()
        rows = conn.execute(
            """SELECT e.word_id, e.action, e.result, e.ts, w.word, w.meaning
               FROM study_events e LEFT JOIN words w ON w.id=e.word_id
               WHERE e.user_id=? AND e.day=? ORDER BY e.id DESC LIMIT 50""",
            (user_id, day)).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM study_events WHERE user_id=? AND day=?",
            (user_id, day)).fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "day": day,
            "stat": dict(stat) if stat else None,
            "total": int(total or 0),
            "events": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 读取：掌握率折线
# ---------------------------------------------------------------------------
def mastery_trend(user_id: int, days: int = 30, tz_minutes: Any = 0) -> dict[str, Any]:
    try:
        days = max(1, min(365, int(days)))
    except (TypeError, ValueError):
        days = 30
    today = local_day(tz_minutes)
    start = local_day(tz_minutes, -(days - 1))
    conn = connect(readonly=True)
    try:
        rows = {r["day"]: r for r in conn.execute(
            "SELECT day,learned_total,mastered_total,new_words FROM daily_stats"
            " WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day",
            (user_id, start, today))}
    finally:
        conn.close()

    if not rows:
        # 没有任何快照 → 返回空序列，由前端展示引导文案（不要凭空画坐标系）
        return {"ok": True, "days": days, "points": []}

    out = []
    # 从**第一个有数据的日**开始，之后缺失日沿用最近一次已知快照。
    # 关键：绝不把今天的数据倒推回更早的日期——那会伪造历史曲线。
    cursor = datetime.strptime(min(rows), "%Y-%m-%d").date()
    end = datetime.strptime(today, "%Y-%m-%d").date()
    cur_l = cur_m = 0
    while cursor <= end:
        d = cursor.isoformat()
        r = rows.get(d)
        if r is not None:
            cur_l, cur_m = int(r["learned_total"] or 0), int(r["mastered_total"] or 0)
            new_words = int(r["new_words"] or 0)
        else:
            new_words = 0
        out.append({
            "day": d,
            "learned": cur_l,
            "mastered": cur_m,
            "new_words": new_words,
        })
        cursor += timedelta(days=1)
    for p in out:
        p["rate"] = round(p["mastered"] / p["learned"] * 100, 1) if p["learned"] else 0.0
        p["low_sample"] = p["learned"] < 20
    return {"ok": True, "days": days, "points": out}


# ---------------------------------------------------------------------------
# 读取：词类 / 分册
# ---------------------------------------------------------------------------
def pos_stats(user_id: int) -> dict[str, Any]:
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            """SELECT COALESCE(w.pos,'') AS pos, COUNT(*) AS total,
                      SUM(CASE WHEN p.seen>0 THEN 1 ELSE 0 END) AS learned,
                      SUM(CASE WHEN p.master=1 THEN 1 ELSE 0 END) AS mastered
               FROM words w
               LEFT JOIN learn_progress p ON p.word_id=w.id AND p.user_id=?
               GROUP BY COALESCE(w.pos,'')""", (user_id,)).fetchall()
    finally:
        conn.close()
    bucket = {r["pos"]: r for r in rows}
    out = []
    for key, label in POS_DISPLAY:
        if key == "other":
            total = learned = mastered = 0
            for m in POS_OTHER_MEMBERS:
                r = bucket.get(m)
                if r:
                    total += int(r["total"] or 0)
                    learned += int(r["learned"] or 0)
                    mastered += int(r["mastered"] or 0)
        else:
            r = bucket.get(key)
            total = int(r["total"] or 0) if r else 0
            learned = int(r["learned"] or 0) if r else 0
            mastered = int(r["mastered"] or 0) if r else 0
        out.append({
            "pos": key, "label": label, "total": total,
            "learned": learned, "mastered": mastered,
            "rate": round(mastered / learned * 100, 1) if learned else 0.0,
            "low_sample": learned < 5,
        })
    return {"ok": True, "items": out}


# 分册名称唯一定义在 api/db.py（此处保留别名，兼容既有调用方）
BOOK_NAMES = db.BOOK_NAMES


def book_stats(user_id: int) -> dict[str, Any]:
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            """SELECT w.book AS book, w.unit AS unit, COUNT(*) AS total,
                      SUM(CASE WHEN p.seen>0 THEN 1 ELSE 0 END) AS learned,
                      SUM(CASE WHEN p.master=1 THEN 1 ELSE 0 END) AS mastered
               FROM words w
               LEFT JOIN learn_progress p ON p.word_id=w.id AND p.user_id=?
               GROUP BY w.book, w.unit""", (user_id,)).fetchall()
    finally:
        conn.close()
    books: dict[Any, dict[str, Any]] = {}
    for r in rows:
        b = r["book"]
        if b is None:
            key, name, order = "unbooked", "未分册", 99
        else:
            key, name, order = int(b), BOOK_NAMES.get(int(b), f"第{b}册"), int(b)
        node = books.setdefault(key, {"key": key, "name": name, "order": order,
                                      "total": 0, "learned": 0, "mastered": 0, "units": []})
        total = int(r["total"] or 0)
        learned = int(r["learned"] or 0)
        mastered = int(r["mastered"] or 0)
        node["total"] += total
        node["learned"] += learned
        node["mastered"] += mastered
        node["units"].append({
            "name": r["unit"] or "未分单元", "total": total,
            "learned": learned, "mastered": mastered,
            "rate": round(mastered / learned * 100, 1) if learned else 0.0,
        })
    out = sorted(books.values(), key=lambda x: x["order"])
    for node in out:
        node["units"].sort(key=lambda u: (u["name"] == "未分单元", u["name"]))
        node["rate"] = round(node["mastered"] / node["learned"] * 100, 1) if node["learned"] else 0.0
    return {"ok": True, "books": out}


# ---------------------------------------------------------------------------
# 读取：复习面板 / 队列
# ---------------------------------------------------------------------------
def review_panel(user_id: int, tz_minutes: Any = 0) -> dict[str, Any]:
    day = local_day(tz_minutes)
    day_start = _utc_bound(day, tz_minutes)
    day_end = (datetime.strptime(day_start, "%Y-%m-%d %H:%M:%S") + timedelta(days=1)
               ).strftime("%Y-%m-%d %H:%M:%S")
    week_end = (datetime.strptime(day_start, "%Y-%m-%d %H:%M:%S") + timedelta(days=8)
                ).strftime("%Y-%m-%d %H:%M:%S")
    conn = connect(readonly=True)
    try:
        base = ("FROM learn_progress p JOIN words w ON w.id=p.word_id "
                "WHERE p.user_id=? AND p.seen>0 AND p.master=0 AND p.next_review_at IS NOT NULL")
        overdue = conn.execute(
            f"SELECT COUNT(*) {base} AND p.next_review_at < ?", (user_id, day_start)
        ).fetchone()[0]
        today = conn.execute(
            f"SELECT COUNT(*) {base} AND p.next_review_at >= ? AND p.next_review_at < ?",
            (user_id, day_start, day_end)).fetchone()[0]
        week = conn.execute(
            f"SELECT COUNT(*) {base} AND p.next_review_at >= ? AND p.next_review_at < ?",
            (user_id, day_end, week_end)).fetchone()[0]
        top = conn.execute(
            f"""SELECT w.id, w.word, w.meaning, p.seen, p.correct, p.next_review_at
                {base} AND p.next_review_at < ?
                ORDER BY p.next_review_at ASC LIMIT 10""",
            (user_id, day_start)).fetchall()
        total_due = conn.execute(
            f"SELECT COUNT(*) {base} AND p.next_review_at < ?", (user_id, day_end)
        ).fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "day": day,
            "buckets": {"overdue": int(overdue or 0), "today": int(today or 0),
                        "week": int(week or 0), "total": int(total_due or 0)},
            "overdue_top": [dict(r) for r in top]}


def review_queue(user_id: int, limit: int = 20, tz_minutes: Any = 0) -> dict[str, Any]:
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        limit = 20
    day = local_day(tz_minutes)
    end = _utc_bound(day, tz_minutes)
    end = (datetime.strptime(end, "%Y-%m-%d %H:%M:%S") + timedelta(days=1)
           ).strftime("%Y-%m-%d %H:%M:%S")
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            """SELECT w.id, w.word, w.pronunciation, w.meaning, w.book, w.unit,
                      p.seen, p.correct, p.next_review_at
               FROM learn_progress p JOIN words w ON w.id=p.word_id
               WHERE p.user_id=? AND p.seen>0 AND p.master=0
                 AND p.next_review_at IS NOT NULL AND p.next_review_at < ?
               ORDER BY p.next_review_at ASC LIMIT ?""",
            (user_id, end, limit)).fetchall()
    finally:
        conn.close()
    return {"ok": True, "count": len(rows), "words": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 读取：徽章
# ---------------------------------------------------------------------------
def badges(user_id: int) -> dict[str, Any]:
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT badge_id, unlocked_at FROM user_badges WHERE user_id=? ORDER BY unlocked_at",
            (user_id,)).fetchall()
    finally:
        conn.close()
    return {"ok": True, "unlocked": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 读取：排行榜（跨库内存合并）
# ---------------------------------------------------------------------------
def leaderboard(metric: str = "words", rng: str = "week", tz_minutes: Any = 0,
                limit: int = 50) -> dict[str, Any]:
    metric = metric if metric in ("words", "streak", "mastered") else "words"
    rng = rng if rng in ("week", "all") else "week"
    today = local_day(tz_minutes)
    start = local_day(tz_minutes, -6) if rng == "week" else "1970-01-01"
    conn = connect(readonly=True)
    try:
        if metric == "words":
            rows = conn.execute(
                "SELECT user_id, SUM(new_words) AS score FROM daily_stats"
                " WHERE day BETWEEN ? AND ? GROUP BY user_id HAVING score>0",
                (start, today)).fetchall()
            scores = {int(r["user_id"]): int(r["score"] or 0) for r in rows}
        elif metric == "mastered":
            rows = conn.execute(
                "SELECT user_id, MAX(mastered_total) AS score FROM daily_stats"
                " GROUP BY user_id HAVING score>0").fetchall()
            scores = {int(r["user_id"]): int(r["score"] or 0) for r in rows}
        else:  # streak
            scores = {}
            for r in conn.execute("SELECT DISTINCT user_id FROM daily_stats"):
                uid = int(r["user_id"])
                days = [x[0] for x in conn.execute(
                    "SELECT day FROM daily_stats WHERE user_id=? AND checked_in=1"
                    " AND makeup=0 ORDER BY day", (uid,))]
                s = calc_streak(days, today)
                if s > 0:
                    scores[uid] = s
    finally:
        conn.close()

    # 名称来自 users.db，SQL 无法跨库 JOIN → 应用层合并
    users = {int(u["id"]): u for u in auth.list_users()}
    entries = []
    for uid, score in scores.items():
        u = users.get(uid)
        if not u:
            continue
        if not int(u.get("enabled", 1)):
            continue
        name = str(u.get("username") or "")
        if auth.is_guest_username(name):   # 游客 role 仍是 user，只能按用户名模式排除
            continue
        display = str(u.get("display_name") or "").strip() or name or f"用户#{uid}"
        entries.append({"user_id": uid, "name": display, "score": int(score)})
    entries.sort(key=lambda e: (-e["score"], e["user_id"]))
    for i, e in enumerate(entries, 1):
        e["rank"] = i
    return {"ok": True, "metric": metric, "range": rng,
            "count": len(entries), "entries": entries[:max(1, limit)]}


# ---------------------------------------------------------------------------
# 补签
# ---------------------------------------------------------------------------
def makeup(user_id: int, day: str = "", tz_minutes: Any = 0) -> dict[str, Any]:
    today = local_day(tz_minutes)
    day = day or local_day(tz_minutes, -1)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return {"ok": False, "error": "bad_day", "message": "日期格式应为 YYYY-MM-DD"}
    if day > today:
        return {"ok": False, "error": "future", "message": "不能补签未来日期"}
    if day[:7] != today[:7]:
        return {"ok": False, "error": "other_month", "message": "只能补签本月"}
    conn = _write_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT checked_in, makeup FROM daily_stats WHERE user_id=? AND day=?",
            (user_id, day)).fetchone()
        if row and row["checked_in"]:
            conn.commit()
            return {"ok": False, "error": "already", "message": "该日已打卡"}
        used = int(conn.execute(
            "SELECT COUNT(*) FROM daily_stats WHERE user_id=? AND makeup=1 AND day LIKE ?",
            (user_id, today[:7] + "-%")).fetchone()[0] or 0)
        if used >= MAKEUP_PER_MONTH:
            conn.commit()
            return {"ok": False, "error": "quota", "message": f"本月补签已用完（{MAKEUP_PER_MONTH} 次）"}
        conn.execute(
            """INSERT INTO daily_stats (user_id,day,checked_in,makeup) VALUES (?,?,1,1)
               ON CONFLICT(user_id,day) DO UPDATE SET checked_in=1, makeup=1""",
            (user_id, day))
        conn.commit()
        return {"ok": True, "day": day, "used": used + 1, "limit": MAKEUP_PER_MONTH}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 兼容导入：旧 POST /api/learn/progress → 事件流
# ---------------------------------------------------------------------------
def import_progress(user_id: int, items: Any, tz_minutes: Any = 0) -> dict[str, Any]:
    """把旧接口的「绝对进度」翻译成事件流，再走统一派生。

    旧接口直接 UPDATE learn_progress，绕过了 study_events / daily_stats，
    于是「事件流是唯一事实来源」这句话在数据层并不成立：这类用户的进度
    无法被自愈重建，打卡/日历/徽章也永远不更新。

    现在：只把**增量**补成合成事件（`uid` 由目标状态确定，重放同一请求不会重复写入），
    再由 `_load_word_states` 重算 seen/correct/master/next_review_at、`_refresh_daily`
    重算当日统计。客户端仍然无法虚增 master（那是服务端算出来的）。
    """
    if not isinstance(items, list) or not items:
        return {"ok": False, "error": "bad_request", "message": "progress 必须是非空数组"}
    # 升级瞬间可能一次带全量进度：允许比单批事件上限更大，但仍是有限值
    if len(items) > MAX_EVENTS_PER_BATCH * 10:
        items = items[:MAX_EVENTS_PER_BATCH * 10]

    day = local_day(tz_minutes)
    conn = _write_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        wanted_ids: set[int] = set()
        parsed: list[tuple[int, int, int]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                wid = int(it.get("word_id") or 0)
                seen = max(0, int(it.get("seen", 0) or 0))
                correct = max(0, int(it.get("correct", 0) or 0))
            except (TypeError, ValueError):
                continue
            if wid <= 0:
                continue
            wanted_ids.add(wid)
            parsed.append((wid, seen, correct))
        if not parsed:
            conn.commit()
            return {"ok": True, "saved": 0, "events": 0, "deprecated": True,
                    "hint": "请改用 POST /api/study/events"}

        meta: dict[int, sqlite3.Row] = {}
        marks = ",".join("?" * len(wanted_ids))
        for r in conn.execute(
                f"SELECT id, pos, book FROM words WHERE id IN ({marks})", list(wanted_ids)):
            meta[int(r["id"])] = r

        saved = events = 0
        for wid, seen, correct in parsed:
            if wid not in meta:
                continue
            row = conn.execute(
                "SELECT seen, correct FROM learn_progress WHERE user_id=? AND word_id=?",
                (user_id, wid)).fetchone()
            cur_seen = int(row["seen"] or 0) if row else 0
            cur_correct = int(row["correct"] or 0) if row else 0
            target_seen = max(cur_seen, seen)
            target_correct = min(target_seen, max(cur_correct, correct))
            if target_seen <= cur_seen and target_correct <= cur_correct:
                continue  # 只抬不降：客户端传小值不改动服务端进度
            need_seen = target_seen - cur_seen
            need_correct = target_correct - cur_correct
            # 合成事件稳定可重放：uid 由「目标状态 + 序号」决定，同一请求重发不会翻倍
            for i in range(need_correct):
                conn.execute(
                    "INSERT INTO study_events (user_id,word_id,action,result,day,ts,pos,book,uid)"
                    " VALUES (?,?,?,?,?,datetime('now'),?,?,?)",
                    (user_id, wid, "quiz", 1, day,
                     meta[wid]["pos"] or "", meta[wid]["book"],
                     f"legacy:{wid}:{target_seen}:{target_correct}:c{i}"),
                )
                events += 1
            for i in range(need_seen - need_correct):
                conn.execute(
                    "INSERT INTO study_events (user_id,word_id,action,result,day,ts,pos,book,uid)"
                    " VALUES (?,?,?,?,?,datetime('now'),?,?,?)",
                    (user_id, wid, "learn", -1, day,
                     meta[wid]["pos"] or "", meta[wid]["book"],
                     f"legacy:{wid}:{target_seen}:{target_correct}:s{i}"),
                )
                events += 1
            saved += 1

        if saved:
            states = _load_word_states(conn, user_id, {p[0] for p in parsed})
            _apply_word_states(conn, user_id, states)
            _refresh_daily(conn, user_id, day)
        conn.commit()
        return {"ok": True, "saved": saved, "events": events, "deprecated": True,
                "hint": "请改用 POST /api/study/events"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 自愈：从事件流全量重算（learn_progress + daily_stats + 徽章）
# ---------------------------------------------------------------------------
def rebuild_progress(user_id: Optional[int] = None) -> dict[str, Any]:
    """按事件流全量重算 learn_progress / daily_stats / 徽章。

    与热路径共用 `_load_word_states` / `_apply_word_states` / `_rebuild_daily_history`，
    所以「实时写入」和「自愈重算」的结果必然一致：
      - learn_progress：seen / correct / **master** / **next_review_at**
      - daily_stats：当日答题/答对/复习/新学 + 每日累计快照 + 打卡（保留补签记录）
      - user_badges：按重算后的连续天数 / 掌握数 / 答题数补齐（只增不删）

    无事件的词保留现状（历史数据无法凭空还原），不会被清零。
    积分（points_ledger）不重算：它是幂等发放的流水，重放会改变历史收益。
    """
    conn = _write_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        where = " WHERE user_id=?" if user_id else ""
        args: tuple[Any, ...] = (user_id,) if user_id else ()
        users = [int(r[0]) for r in conn.execute(
            f"SELECT DISTINCT user_id FROM study_events{where}", args)]
        today = local_day(0)
        updated = days = 0
        for uid in users:
            states = _load_word_states(conn, uid)
            if states:
                _apply_word_states(conn, uid, states)
                updated += len(states)
            days += _rebuild_daily_history(conn, uid)
            streak = calc_streak(_checked_days(conn, uid, real_only=True), today)
            _learned, mastered = _totals(conn, uid)
            _check_badges(conn, uid, streak, mastered, answered_total(conn, uid))
        conn.commit()
        return {"ok": True, "updated": updated, "users": len(users), "days": days}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 模块导入即建表（与 auth / analytics 的初始化方式一致）
init()
