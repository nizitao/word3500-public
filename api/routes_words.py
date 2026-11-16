#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""路由：词表浏览 / 搜索 / 收藏夹 / 学习进度 / 单元背诵断点。

所有需要登录的接口在函数内自行调用 `_require_auth()`（不再由分发器代劳），
这样路由表只声明「路径 → 处理函数」，鉴权策略跟着处理函数走，不会漏。
"""
import json
import sys
import time
from typing import Optional

from api import db, study

GET_ROUTES = {
    "/api/words": ("api_words", True),
    "/api/words/all": ("api_words_all", True),
    "/api/books": ("api_books", False),
    "/api/search": ("api_search", True),
    "/api/collections": ("api_collections", True),
    "/api/collections/words": ("api_collections_words", True),
    "/api/collections/word-status": ("api_collections_word_status", True),
    "/api/learn/progress": ("api_learn_progress", False),
    "/api/quiz/progress": ("api_quiz_progress_get", False),
}

POST_ROUTES = {
    "/api/collections": ("api_collections_post", False),
    "/api/collections/toggle": ("api_collections_toggle", False),
    "/api/learn/progress": ("api_learn_progress_save", False),
    "/api/quiz/progress": ("api_quiz_progress_save", False),
}

# 全量词表缓存：(构建时间戳, 行数, 已序列化字节)
# 紧凑版给前端模糊搜索（WordStore 依赖 [id, word, meaning] 的三元数组），
# full 版给「全部单词」词表视图（多一列音标），两份缓存互不污染。
_words_all_cache: Optional[tuple[float, int, bytes]] = None
_words_all_full_cache: Optional[tuple[float, int, bytes]] = None


class WordsRoutes:
    # ---- 词表 ----
    def api_words(self, qs: dict[str, list[str]]) -> None:
        if not self._require_auth():
            return
        try:
            page = int((qs.get("page") or ["1"])[0])
        except ValueError:
            page = 1
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        user_id = user["sub"] if user else 0
        # 册/单元过滤
        book = qs.get("book", [None])[0]
        unit = qs.get("unit", [None])[0]
        # 词性过滤（词类分析下钻）；other 是图表归并后的「其他」桶
        pos = (qs.get("pos") or [""])[0].strip().lower()
        where_clauses = []
        params: list[object] = []
        if pos:
            if pos == "other":
                where_clauses.append(
                    "COALESCE(pos,'') IN ('other','abbr','interj','art','aux','')")
            elif pos in ("n", "v", "adj", "adv", "pron", "prep", "num", "conj", "phr"):
                where_clauses.append("pos=?")
                params.append(pos)
        if book is not None and book.isdigit():
            where_clauses.append("book=?")
            params.append(int(book))
            if unit:
                where_clauses.append("unit=?")
                params.append(unit)
        where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        conn = db.connect()
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS c FROM words{where_sql}", params)
        total = cur.fetchone()["c"]
        total_pages = max(1, (total + db.PAGE_SIZE - 1) // db.PAGE_SIZE)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * db.PAGE_SIZE
        cur.execute(
            f"SELECT * FROM words{where_sql} ORDER BY id LIMIT ? OFFSET ?",
            params + [db.PAGE_SIZE, offset],
        )
        rows = [dict(r) for r in cur.fetchall()]
        # 标记当前用户的收藏状态（返回收藏夹ID列表）
        if user_id and rows:
            cur.execute(
                ("SELECT cw.word_id, GROUP_CONCAT(cw.collection_id) FROM collection_words cw "
                 "INNER JOIN collections c ON cw.collection_id = c.id AND c.user_id = ? "
                 "WHERE cw.word_id IN ({}) GROUP BY cw.word_id").format(
                    ",".join("?" * len(rows))
                ),
                [user_id] + [r["id"] for r in rows],
            )
            coll_map = {r[0]: [int(x) for x in str(r[1]).split(",") if x]
                        for r in cur.fetchall()}
            for r in rows:
                r["collection_ids"] = coll_map.get(r["id"], [])
                r["star"] = 1 if r["collection_ids"] else 0
        else:
            for r in rows:
                r["collection_ids"] = []
                r["star"] = 0
        conn.close()
        self._send_json(
            {"page": page, "total_pages": total_pages, "total": total,
             "words": rows, "some": False,
             "book": int(book) if book and book.isdigit() else None,
             "unit": unit}
        )

    def api_books(self) -> None:
        """GET /api/books — 返回所有有数据的册和单元"""
        if not self._require_auth():
            return
        conn = db.connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT book, unit, COUNT(*) AS cnt FROM words "
            "WHERE book IS NOT NULL GROUP BY book, unit ORDER BY book, unit"
        )
        rows = cur.fetchall()
        books: dict[int, dict[str, object]] = {}
        for r in rows:
            b = int(r["book"])
            if b not in books:
                books[b] = {"book": b, "name": db.book_name(b), "units": []}
            units = books[b]["units"]
            assert isinstance(units, list)
            units.append({"name": r["unit"], "count": r["cnt"]})
        conn.close()
        self._send_json({"books": [books[k] for k in sorted(books)]})

    def api_words_all(self, qs: dict[str, list[str]]) -> None:
        """GET /api/words/all — 返回全部单词

        默认紧凑列表 [id, word, meaning]，供前端本地模糊搜索（search.js 的 WordStore
        按下标取词），必须保持三元结构不变。
        ?full=1 增加音标列：[id, word, pronunciation, meaning]，供收藏夹「全部单词」视图。

        两种形态分别做进程内缓存（按行数失效）+ 浏览器 5 分钟私有缓存。
        """
        global _words_all_cache, _words_all_full_cache
        if not self._require_auth():
            return
        full = (qs.get("full") or [""])[0] == "1"
        cache = _words_all_full_cache if full else _words_all_cache
        conn = db.connect()
        try:
            cur_count = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        finally:
            conn.close()
        now = time.time()
        if cache is not None:
            built_at, cached_count, payload = cache
            if cached_count == cur_count and now - built_at < 600:
                self._send_raw(payload, "application/json; charset=utf-8",
                               cache_control="private, max-age=300")
                return
        conn = db.connect()
        cur = conn.cursor()
        if full:
            cur.execute("SELECT id, word, pronunciation, meaning FROM words ORDER BY id")
            data = [[r["id"], r["word"], r["pronunciation"] or "", r["meaning"]]
                    for r in cur.fetchall()]
        else:
            cur.execute("SELECT id, word, meaning FROM words ORDER BY id")
            data = [[r["id"], r["word"], r["meaning"]] for r in cur.fetchall()]
        conn.close()
        payload = json.dumps({"count": len(data), "words": data},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if full:
            _words_all_full_cache = (now, cur_count, payload)
        else:
            _words_all_cache = (now, cur_count, payload)
        self._send_raw(payload, "application/json; charset=utf-8",
                       cache_control="private, max-age=300")

    # ---- 搜索 ----
    @staticmethod
    def _has_chinese(s: str) -> bool:
        """检测字符串是否包含中文字符"""
        for ch in s:
            if ('\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f'
                    or '\uff00' <= ch <= '\uffef'):
                return True
        return False

    @staticmethod
    def _like_escape(s: str) -> str:
        """转义 LIKE 通配符：用户输入 '%' / '_' 不应变成「匹配全部」。"""
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def api_search(self, qs: dict[str, list[str]]) -> None:
        if not self._require_auth():
            return
        search = (qs.get("search") or [""])[0].strip()
        pattern = (qs.get("pattern") or ["contain"])[0]
        if search == "":
            self._send_json({"type": "empty"})
            return
        conn = db.connect()
        cur = conn.cursor()
        if search.isdigit():
            wid = int(search)
            cur.execute("SELECT id FROM words WHERE id=?", (wid,))
            row = cur.fetchone()
            conn.close()
            if row:
                self._send_json(
                    {"type": "id", "id": wid,
                     "page": (wid - 1) // db.PAGE_SIZE + 1}
                )
            else:
                self._send_json({"type": "none"})
        else:
            keyword = self._like_escape(search)
            if pattern == "begin":
                like = f"{keyword}%"
            elif pattern == "end":
                like = f"%{keyword}"
            else:  # contain / meaning / 未知模式
                like = f"%{keyword}%"

            if pattern == "meaning":
                cur.execute(
                    "SELECT id, word, pronunciation, meaning FROM words WHERE meaning LIKE ? ESCAPE '\\' "
                    "ORDER BY id LIMIT 200",
                    (like,),
                )
            else:
                cur.execute(
                    "SELECT id, word, pronunciation, meaning FROM words WHERE word LIKE ? ESCAPE '\\' "
                    "ORDER BY id LIMIT 200",
                    (like,),
                )
            rows = [dict(r) for r in cur.fetchall()]
            # 如为英文搜索且无结果，自动 fallback 到中文释义搜索（始终用 %keyword%）
            if not rows and pattern != "meaning" and self._has_chinese(search):
                fallback_like = f"%{keyword}%"
                cur.execute(
                    "SELECT id, word, pronunciation, meaning FROM words WHERE meaning LIKE ? ESCAPE '\\' "
                    "ORDER BY id LIMIT 200",
                    (fallback_like,),
                )
                rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            if not rows:
                self._send_json({"type": "none"})
                return
            # 判断实际匹配字段（检测是否命中 meaning 列）
            matched_meaning = (pattern == "meaning") or (
                pattern != "meaning" and self._has_chinese(search)
            )
            match_field = "meaning" if matched_meaning else "word"
            for r in rows:
                r["match"] = match_field
            self._send_json(
                {"type": "list", "pattern": pattern,
                 "search": search, "results": rows}
            )

    # ---- 收藏夹 ----
    def api_collections(self, _qs: dict[str, list[str]]) -> None:
        """GET /api/collections — 列出当前用户的收藏夹（含单词数）"""
        if not self._require_auth():
            return
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        user_id = user["sub"] if user else 0
        conn = db.connect()
        cur = conn.cursor()
        cur.execute(
            """SELECT c.id, c.name, c.created_at,
                      (SELECT COUNT(*) FROM collection_words WHERE collection_id=c.id) AS cnt
               FROM collections c WHERE c.user_id=? ORDER BY c.id""",
            (user_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        self._send_json({"collections": rows})

    def api_collections_post(self) -> None:
        """POST /api/collections — 创建或重命名/删除收藏夹
           body: {action:'create', name:'xxx'}
                 {action:'rename', id:1, name:'xxx'}
                 {action:'delete', id:1}"""
        if not self._require_auth():
            return
        if self._is_guest():
            self._send_json({"ok": False, "error": "游客不允许操作收藏夹"}, 403)
            return
        if self._rate_limited("collections"):
            return
        body = self._read_json()
        action = body.get("action", "")
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        user_id = user["sub"] if user else 0
        conn = db.connect()
        try:
            if action == "create":
                name = (body.get("name") or "").strip()
                if not name:
                    self._send_json({"ok": False, "error": "名称不能为空"}, 400)
                    return
                conn.execute("INSERT INTO collections (user_id, name) VALUES (?, ?)",
                             (user_id, name))
                cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.commit()
                self._send_json({"ok": True, "id": cid, "name": name})
            elif action == "rename":
                cid = body.get("id")
                name = (body.get("name") or "").strip()
                if not cid or not name:
                    self._send_json({"ok": False, "error": "缺少 id 或名称"}, 400)
                    return
                conn.execute("UPDATE collections SET name=? WHERE id=? AND user_id=?",
                             (name, cid, user_id))
                conn.commit()
                self._send_json({"ok": True})
            elif action == "delete":
                cid = body.get("id")
                if not cid:
                    self._send_json({"ok": False, "error": "缺少 id"}, 400)
                    return
                # 先确认该收藏夹属于当前用户，避免越权清空他人收藏
                owned = conn.execute(
                    "SELECT 1 FROM collections WHERE id=? AND user_id=?", (cid, user_id)
                ).fetchone()
                if not owned:
                    self._send_json({"ok": False, "error": "收藏夹不存在"}, 404)
                    return
                conn.execute("DELETE FROM collection_words WHERE collection_id=?", (cid,))
                conn.execute("DELETE FROM collections WHERE id=? AND user_id=?",
                             (cid, user_id))
                conn.commit()
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "unknown action"}, 400)
        finally:
            conn.close()

    def api_collections_words(self, qs: dict[str, list[str]]) -> None:
        """GET /api/collections/words?cid=X — 获取收藏夹内单词（仅限本人收藏夹）"""
        if not self._require_auth():
            return
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        user_id = user["sub"] if user else 0
        try:
            cid = int(qs.get("cid", [0])[0])
        except (TypeError, ValueError):
            self._send_json({"words": [], "total": 0})
            return
        conn = db.connect()
        cur = conn.cursor()
        # 归属校验：此前只要猜 cid 就能读取他人收藏夹
        owner = cur.execute(
            "SELECT user_id FROM collections WHERE id=?", (cid,)
        ).fetchone()
        if not owner or int(owner[0]) != int(user_id):
            conn.close()
            self._send_json({"error": "forbidden", "message": "收藏夹不存在或无权限"}, 403)
            return
        cur.execute(
            """SELECT w.id, w.word, w.pronunciation, w.meaning,
                      w.book, w.unit
               FROM words w INNER JOIN collection_words cw ON w.id=cw.word_id
               WHERE cw.collection_id=? ORDER BY w.book, w.unit, w.id""",
            (cid,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        self._send_json({"words": rows, "total": len(rows)})

    def api_collections_word_status(self, qs: dict[str, list[str]]) -> None:
        """GET /api/collections/word-status?wid=X — 查单词在哪些收藏夹中"""
        if not self._require_auth():
            return
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        user_id = user["sub"] if user else 0
        try:
            wid = int(qs.get("wid", [0])[0])
        except (TypeError, ValueError):
            self._send_json({"ids": []})
            return
        conn = db.connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT cw.collection_id FROM collection_words cw "
            "INNER JOIN collections c ON cw.collection_id=c.id AND c.user_id=? "
            "WHERE cw.word_id=?",
            (user_id, wid),
        )
        ids = [r[0] for r in cur.fetchall()]
        conn.close()
        self._send_json({"ids": ids})

    def api_collections_toggle(self) -> None:
        """POST /api/collections/toggle — {wid, cids:[...]} 设置单词的收藏夹"""
        if not self._require_auth():
            return
        if self._is_guest():
            self._send_json({"ok": False, "error": "游客不允许收藏单词"}, 403)
            return
        if self._rate_limited("collections"):
            return
        body = self._read_json()
        wid = body.get("wid")
        cids = body.get("cids", [])
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        user_id = user["sub"] if user else 0
        if not isinstance(cids, list):
            self._send_json({"ok": False, "error": "cids 必须是数组"}, 400)
            return
        try:
            wid = int(wid)
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "wid 无效"}, 400)
            return
        conn = db.connect()
        try:
            # 只接受属于当前用户的收藏夹 ID，防止向他人收藏夹写数据。
            # 前端/第三方可能传字符串 ID，统一转 int 再比对，否则会被静默丢弃。
            owned = {r[0] for r in conn.execute(
                "SELECT id FROM collections WHERE user_id=?", (user_id,)
            ).fetchall()}
            try:
                wanted = {int(c) for c in cids}
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "cids 含无效值"}, 400)
                return
            safe_cids = [c for c in wanted if c in owned]
            conn.execute(
                "DELETE FROM collection_words WHERE word_id=? AND collection_id IN "
                "(SELECT id FROM collections WHERE user_id=?)",
                (wid, user_id),
            )
            for cid in safe_cids:
                conn.execute(
                    "INSERT OR IGNORE INTO collection_words (collection_id, word_id) VALUES (?, ?)",
                    (cid, wid),
                )
            conn.commit()
            self._send_json({"ok": True, "wid": wid, "cids": safe_cids})
        finally:
            conn.close()

    # ---- 学习进度 ----
    def api_learn_progress(self) -> None:
        """GET /api/learn/progress — 返回当前用户的全部学习进度"""
        if not self._require_auth():
            return
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        user_id = user["sub"] if user else 0
        conn = db.connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT word_id, seen, correct, master FROM learn_progress WHERE user_id=?",
            (user_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        self._send_json({"progress": rows})

    def api_learn_progress_save(self) -> None:
        """POST /api/learn/progress — 【已降级】兼容升级瞬间的本地缓存

        设计文档 §4.5：`master` 判定已下沉服务端，本接口忽略客户端 `master`，
        `seen`/`correct` 取 max(服务端值, 客户端值)，客户端无法靠传大值虚增进度。

        与设计文档的差异曾经是真实缺陷：本接口原先直接 UPDATE `learn_progress`，
        既不写 `study_events` 也不写 `daily_stats`，「事件流是唯一事实来源」被破坏，
        这些进度永远无法被自愈重建、不产生打卡/日历/徽章。
        现在统一交给 study.import_progress()，把增量翻译成事件后走同一套派生。
        """
        if not self._require_auth():
            return
        if self._is_guest():
            self._send_json({"ok": False, "error": "游客进度不保存"}, 403)
            return
        if self._rate_limited("study_event"):
            return
        body = self._read_json()
        items = body.get("progress") or []
        if not isinstance(items, list):
            self._send_json({"ok": False, "error": "progress 必须是数组"}, 400)
            return
        try:
            tz = int(body.get("tz") or 0)
        except (TypeError, ValueError):
            tz = 0
        try:
            res = study.import_progress(self._study_uid(), items, tz)
        except Exception as e:
            # 不回传 str(e)：可能带出 SQLite 表结构与列名
            print(f"[words] import_progress_failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            self._send_json({"ok": False, "error": "save_failed",
                             "message": "保存失败，请稍后重试"}, 500)
            return
        self._send_json(res, 200 if res.get("ok") else 400)

    # ---- 单元背诵断点 ----
    def api_quiz_progress_save(self) -> None:
        """POST /api/quiz/progress — 保存/清除单元背诵断点（每用户一条）"""
        if not self._require_auth():
            return
        if self._is_guest():
            self._send_json({"ok": False, "error": "guest"}, 403)
            return
        body = self._read_json()
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        user_id = user["sub"] if user else 0
        conn = db.connect()
        try:
            if body.get("clear"):
                conn.execute("DELETE FROM quiz_progress WHERE user_id=?", (user_id,))
                conn.commit()
                self._send_json({"ok": True, "cleared": True})
                return
            book = body.get("book")
            unit = body.get("unit")
            if not book or not unit:
                self._send_json({"ok": False, "error": "bad_request"}, 400)
                return
            queue = json.dumps(body.get("queue") or [], ensure_ascii=False)
            stats = json.dumps(body.get("stats") or {"answered": 0, "correct": 0},
                                ensure_ascii=False)
            try:
                qp = max(0, int(body.get("quiz_pos") or 0))
                wi = max(0, int(body.get("word_index") or 0))
            except (TypeError, ValueError):
                qp = wi = 0
            conn.execute(
                "INSERT INTO quiz_progress (user_id,book,unit,queue_json,quiz_pos,word_index,"
                "stats_json,updated_at) VALUES (?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(user_id) DO UPDATE SET book=excluded.book, unit=excluded.unit, "
                "queue_json=excluded.queue_json, quiz_pos=excluded.quiz_pos, "
                "word_index=excluded.word_index, stats_json=excluded.stats_json, "
                "updated_at=datetime('now')",
                (user_id, book, unit, queue, qp, wi, stats),
            )
            conn.commit()
            self._send_json({"ok": True})
        finally:
            conn.close()

    def api_quiz_progress_get(self) -> None:
        """GET /api/quiz/progress — 读取单元背诵断点"""
        if not self._require_auth():
            return
        user = self._auth_user  # pyright: ignore[reportAttributeAccessIssue]
        user_id = user["sub"] if user else 0
        conn = db.connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT book,unit,queue_json,quiz_pos,word_index,stats_json "
            "FROM quiz_progress WHERE user_id=?",
            (user_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            self._send_json({"has": False})
            return
        try:
            queue = json.loads(row["queue_json"]) if row["queue_json"] else []
            if not isinstance(queue, list):
                queue = []
        except (json.JSONDecodeError, TypeError):
            queue = []
        try:
            stats = json.loads(row["stats_json"]) if row["stats_json"] else {
                "answered": 0, "correct": 0}
            if not isinstance(stats, dict):
                stats = {"answered": 0, "correct": 0}
        except (json.JSONDecodeError, TypeError):
            stats = {"answered": 0, "correct": 0}
        self._send_json({"has": True, "progress": {
            "book": row["book"], "unit": row["unit"], "queue": queue,
            "quiz_pos": row["quiz_pos"], "word_index": row["word_index"],
            "stats": stats,
        }})
