// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

/* 一起背（学习房间）—— 设计文档 §5.8
   所有成员进度来自服务端 /api/together/*，前端不做二次口径计算。
   ES5 风格、零第三方依赖；沿用 study.js 的 authFetch + content-type 校验模式。 */
(function () {
  var TZ = -new Date().getTimezoneOffset();
  var POLL_MS = 15000;

  var state = {
    rooms: [],
    scopes: null,
    code: '',
    detail: null,
    busy: false
  };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmt(n) { return Number(n || 0).toLocaleString('zh-CN'); }

  function api(path, opts) {
    if (typeof authFetch !== 'function') return Promise.reject(new Error('登录组件未加载，请刷新页面'));
    return authFetch(path, opts).then(function (r) {
      var ct = (r.headers.get('content-type') || '').toLowerCase();
      if (ct.indexOf('json') < 0) {
        if (r.status === 404) throw new Error('接口不存在（HTTP 404），请确认服务端已更新并重启');
        if (r.status === 401 || r.status === 403) throw new Error('登录状态已失效，请重新登录');
        throw new Error('服务返回了非 JSON 响应（HTTP ' + r.status + '）');
      }
      return r.json().then(function (d) {
        if (d && d.error === 'not_found') throw new Error('接口不存在（HTTP 404），请确认服务端已更新并重启');
        // 业务失败（400 + {ok:false, message}）返回给调用方按 res.ok 处理；
        // 只有鉴权失败 / 服务端异常这类非业务响应才抛错。
        if (!r.ok && d && typeof d.ok === 'boolean' && r.status < 500) return d;
        if (!r.ok && d && d.message) throw new Error(d.message);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return d;
      });
    });
  }

  function post(path, obj) {
    var body = { tz: TZ };
    var src = obj || {};
    for (var k in src) {
      if (Object.prototype.hasOwnProperty.call(src, k)) body[k] = src[k];
    }
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  }

  function showError(msg) {
    var box = $('tgError');
    if (!msg) { box.hidden = true; box.textContent = ''; return; }
    box.textContent = msg;
    box.hidden = false;
  }

  // 提示条（中性信息，与红色错误区分）。重复加入房间属于「已达成状态」，不该报错。
  var noticeTimer = null;
  function showNotice(msg, autoHideMs) {
    var box = $('tgNotice');
    if (!box) return;
    box.textContent = msg;   // 覆盖上一次提示（::before 的 ℹ 图标由 CSS 提供）
    box.hidden = false;
    if (noticeTimer) clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () { clearNotice(); }, autoHideMs || 8000);
  }

  function clearNotice() {
    var box = $('tgNotice');
    if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null; }
    if (!box) return;
    box.hidden = true;
    box.textContent = '';
  }

  // 「已在房间里」的统一回应：先刷新房间列表并打开该房间，再给出提示。
  // 重复加入不视为失败（服务端幂等），所以用中性提示而不是红色报错。
  function alreadyJoined(code, name) {
    $('tgCode').value = '';
    showError('');
    return afterMembership(code).then(function () {
      showNotice('你已经在「' + (name || code) + '」房间里了，无需重复加入，已为你打开该房间。');
    });
  }

  // -------------------------------------------------------------------------
  // 创建表单：分册 / 单元二级联动
  // -------------------------------------------------------------------------
  function fillScopeSelects(scopes) {
    var sel = $('tgBook');
    (scopes.books || []).forEach(function (b) {
      var o = document.createElement('option');
      o.value = String(b.book);
      o.textContent = b.name + '（' + fmt(b.count) + ' 词）';
      sel.appendChild(o);
    });
  }

  function fillUnits() {
    var book = $('tgBook').value;
    var sel = $('tgUnit');
    sel.innerHTML = '<option value="">整册一起背</option>';
    if (!book) { sel.disabled = true; return; }
    sel.disabled = false;
    var picked = null;
    (state.scopes.books || []).forEach(function (b) {
      if (String(b.book) === String(book)) picked = b;
    });
    (picked ? picked.units : []).forEach(function (u) {
      var o = document.createElement('option');
      o.value = u.name;
      o.textContent = u.name + '（' + fmt(u.count) + ' 词）';
      sel.appendChild(o);
    });
  }

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------
  function renderRooms() {
    var box = $('tgRoomList');
    $('tgRoomCount').textContent = state.rooms.length + ' 个';
    if (!state.rooms.length) {
      box.innerHTML = '<div class="tg-empty">还没有加入任何房间。<br>' +
        '创建一个房间，把邀请码发给同学；<br>或凭同学给的邀请码加入。</div>';
      return;
    }
    var html = '';
    state.rooms.forEach(function (r) {
      html += '<button class="tg-room' + (r.code === state.code ? ' on' : '') + '"' +
        ' data-code="' + esc(r.code) + '" type="button">' +
        '<div class="tg-room-top"><span class="tg-room-name">' + esc(r.name) + '</span>' +
        '<span class="tg-code">' + esc(r.code) + '</span></div>' +
        '<div class="tg-room-meta"><span class="tg-dot' + (r.my_checked_in ? ' ok' : '') + '"></span>' +
        esc(r.scope_label) + ' · ' + r.member_count + ' 人 · 今日 ' + r.checked_count + ' 人打卡' +
        (r.is_owner ? ' · 房主' : '') + '</div>' +
        '</button>';
    });
    box.innerHTML = html;
  }

  function kpi(label, value) {
    return '<div class="tg-kpi"><div class="tg-kpi-label">' + esc(label) + '</div>' +
      '<div class="tg-kpi-value">' + esc(value) + '</div></div>';
  }

  function memberHtml(m) {
    var goal = state.detail.goal.answered;
    var chips = '';
    if (m.is_owner) chips += '<span class="tg-chip owner">房主</span>';
    if (m.is_me) chips += '<span class="tg-chip">我</span>';
    if (m.checked_in) chips += '<span class="tg-chip checked">已打卡' + (m.makeup ? ' · 补签' : '') + '</span>';
    if (m.streak > 0) chips += '<span class="tg-chip">连续 ' + m.streak + ' 天</span>';

    var like;
    if (m.is_me) {
      like = '<span class="tg-muted">自己</span>';
    } else {
      like = '<button class="tg-like-btn' + (m.liked_by_me ? ' on' : '') + '"' +
        ' data-act="like" data-uid="' + m.user_id + '"' + (m.active ? '' : ' disabled') +
        ' type="button">' + (m.liked_by_me ? '❤️' : '👍') + ' ' + m.likes_today + '</button>';
    }
    return '<div class="tg-member' + (m.is_me ? ' me' : '') + (m.active ? '' : ' off') + '">' +
      '<div class="tg-avatar">' + esc(String(m.name || '?').slice(0, 1)) + '</div>' +
      '<div class="tg-member-main">' +
        '<div class="tg-member-top"><span class="tg-member-name">' + esc(m.name) + '</span>' + chips + '</div>' +
        '<div class="tg-bar"><i class="tg-bar-fill' + (m.checked_in ? ' done' : '') +
          '" style="width:' + m.progress + '%"></i></div>' +
        '<div class="tg-member-meta">今日答题 ' + m.answered + ' / ' + goal +
          ' · 新学 ' + m.new_words + ' 词 · 复习 ' + m.reviewed +
          ' 词 · 进度 ' + m.progress + '%</div>' +
      '</div>' +
      '<div class="tg-like">' + like +
        '<span class="tg-like-hint">累计 ' + m.likes_total + ' 赞</span></div>' +
      '</div>';
  }

  function renderDetailEmpty() {
    $('tgDetail').innerHTML = '<section class="tg-card"><div class="tg-empty">' +
      '还没有选中房间。<br>创建一个房间，或凭邀请码加入同学，<br>就能看到大家的今日进度。</div></section>';
  }

  function renderDetail() {
    var d = state.detail;
    if (!d) { renderDetailEmpty(); return; }
    var r = d.room;
    var html = '<section class="tg-hero"><div class="tg-hero-top"><div>' +
      '<h2>' + esc(r.name) + '</h2>' +
      '<div class="tg-hero-code">邀请码 <b>' + esc(r.code) + '</b>' +
      '<button class="tg-btn tg-btn-sm" data-act="copy" type="button">复制邀请链接</button></div>' +
      '</div><div class="tg-hero-acts">' +
      '<a class="tg-btn tg-btn-primary" style="width:auto" href="' + esc(r.study_url) + '">开始背这份词表</a>' +
      (r.is_owner
        ? '<button class="tg-btn tg-btn-sm tg-btn-danger" data-act="dissolve" type="button">解散房间</button>'
        : '<button class="tg-btn tg-btn-sm" data-act="leave" type="button">退出房间</button>') +
      '</div></div>' +
      '<div class="tg-kpis">' +
        kpi('成员', r.member_count + ' / ' + r.max_members) +
        kpi('今日打卡', r.checked_count + ' 人') +
        kpi('今日答题', r.answered_today + ' 题') +
        kpi('共享词表', fmt(r.word_count) + ' 词') +
      '</div>' +
      '<div class="tg-scope"><span>范围：<b>' + esc(r.scope_label) + '</b></span>' +
      '<span class="tg-muted">打卡目标：答题 ' + d.goal.answered + ' 题 或 新学 ' + d.goal.new_words + ' 词</span>' +
      '<span class="tg-muted">数据日 ' + esc(d.day) + '</span></div>' +
      '</section>';

    html += '<section class="tg-card"><div class="tg-card-head"><h2>成员今日进度</h2>' +
      '<span class="tg-muted">每 15 秒自动刷新 · 每天可为每位同学点赞 1 次</span></div>';
    d.members.forEach(function (m) { html += memberHtml(m); });
    html += '</section>';
    $('tgDetail').innerHTML = html;
  }

  // -------------------------------------------------------------------------
  // 数据加载
  // -------------------------------------------------------------------------
  function openRoom(code) {
    if (!code) { state.code = ''; state.detail = null; renderDetailEmpty(); return Promise.resolve(); }
    return api('/api/together/room?tz=' + TZ + '&code=' + encodeURIComponent(code))
      .then(function (d) {
        if (!d.ok) { showError(d.message || '打开房间失败'); return; }
        state.code = code;
        state.detail = d;
        showError('');
        renderDetail();
        renderRooms();
        try { history.replaceState(null, '', '?code=' + encodeURIComponent(code)); } catch (e) {}
      });
  }

  function refreshAll() {
    if (state.busy) return;
    state.busy = true;
    api('/api/together/rooms?tz=' + TZ).then(function (d) {
      if (d.guest) return null;
      state.rooms = d.rooms || [];
      renderRooms();
      if (!state.code) { return null; }
      return api('/api/together/room?tz=' + TZ + '&code=' + encodeURIComponent(state.code));
    }).then(function (d) {
      if (!d) return;
      if (d.ok) { state.detail = d; renderDetail(); showError(''); }
      else if (d.error === 'not_found') {
        state.code = ''; state.detail = null; renderDetailEmpty();
        try { history.replaceState(null, '', location.pathname); } catch (e) {}
      }
    }).catch(function (e) {
      showError(e.message || '刷新失败');
    }).then(function () { state.busy = false; });
  }

  function afterMembership(code) {
    return api('/api/together/rooms?tz=' + TZ).then(function (d) {
      state.rooms = d.rooms || [];
      renderRooms();
      return openRoom(code);
    });
  }

  // -------------------------------------------------------------------------
  // 交互
  // -------------------------------------------------------------------------
  function doLike(uid, btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    post('/api/together/like', { code: state.code, to_user_id: Number(uid) })
      .then(function (res) {
        if (!res.ok) { showError(res.message || '点赞失败'); btn.disabled = false; return; }
        showError('');
        return openRoom(state.code);
      })
      .catch(function (e) { showError(e.message); btn.disabled = false; });
  }

  function copyInvite() {
    var url = location.origin + '/together?code=' + encodeURIComponent(state.code);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () { showError(''); },
        function () { window.prompt('复制邀请链接发给同学：', url); });
      // 给用户一个可见反馈
      var btn = document.querySelector('[data-act="copy"]');
      if (btn) { btn.textContent = '已复制链接'; setTimeout(function () { btn.textContent = '复制邀请链接'; }, 1600); }
    } else {
      window.prompt('复制邀请链接发给同学：', url);
    }
  }

  function leaveOrDissolve(dissolve) {
    if (!window.confirm(dissolve
      ? '确定解散房间吗？所有成员关系与点赞记录都会删除，且不可恢复。'
      : '确定退出这个房间吗？')) return;
    clearNotice();
    post(dissolve ? '/api/together/dissolve' : '/api/together/leave', { code: state.code })
      .then(function (res) {
        if (!res.ok) { showError(res.message || '操作失败'); return; }
        state.code = ''; state.detail = null;
        try { history.replaceState(null, '', location.pathname); } catch (e) {}
        renderDetailEmpty();
        return refreshAll();
      })
      .catch(function (e) { showError(e.message); });
  }

  function switchTab(which) {
    var isCreate = which === 'create';
    $('tgTabCreate').className = 'tg-tab' + (isCreate ? ' on' : '');
    $('tgTabJoin').className = 'tg-tab' + (isCreate ? '' : ' on');
    $('tgCreateForm').hidden = !isCreate;
    $('tgJoinForm').hidden = isCreate;
    showError('');
    clearNotice();
  }

  function bind() {
    $('tgTabCreate').addEventListener('click', function () { switchTab('create'); });
    $('tgTabJoin').addEventListener('click', function () { switchTab('join'); });
    $('tgBook').addEventListener('change', fillUnits);
    $('tgRefresh').addEventListener('click', function () { refreshAll(); });

    $('tgCode').addEventListener('input', function () {
      this.value = this.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 6);
    });

    $('tgRoomList').addEventListener('click', function (e) {
      var t = e.target;
      while (t && t !== this && !t.getAttribute('data-code')) t = t.parentNode;
      if (!t || t === this) return;
      openRoom(t.getAttribute('data-code')).catch(function (ex) { showError(ex.message); });
    });

    $('tgDetail').addEventListener('click', function (e) {
      var t = e.target;
      while (t && t !== this && !t.getAttribute('data-act')) t = t.parentNode;
      if (!t || t === this) return;
      var act = t.getAttribute('data-act');
      if (act === 'like') doLike(t.getAttribute('data-uid'), t);
      else if (act === 'copy') copyInvite();
      else if (act === 'leave') leaveOrDissolve(false);
      else if (act === 'dissolve') leaveOrDissolve(true);
    });

    $('tgCreateForm').addEventListener('submit', function (e) {
      e.preventDefault();
      clearNotice();
      var name = $('tgName').value.trim();
      if (!name) { showError('请填写房间名称'); return; }
      var book = $('tgBook').value;
      var unit = book ? $('tgUnit').value : '';
      var btn = $('tgCreateBtn');
      btn.disabled = true;
      post('/api/together/create', { name: name, book: book || null, unit: unit || null })
        .then(function (res) {
          btn.disabled = false;
          if (!res.ok) { showError(res.message || '创建失败'); return; }
          $('tgName').value = '';
          showError('');
          return afterMembership(res.code);
        })
        .catch(function (ex) { btn.disabled = false; showError(ex.message); });
    });

    $('tgJoinForm').addEventListener('submit', function (e) {
      e.preventDefault();
      clearNotice();
      var code = $('tgCode').value.trim().toUpperCase();
      if (code.length !== 6) { showError('请输入 6 位邀请码'); return; }

      // 本地已知已加入（房间里就有这个码）：直接提醒，不必再发一次加入请求
      var mine = null;
      state.rooms.forEach(function (r) { if (r.code === code) mine = r; });
      if (mine) {
        alreadyJoined(code, mine.name).catch(function (ex) { showError(ex.message || '打开房间失败'); });
        return;
      }

      var btn = $('tgJoinBtn');
      btn.disabled = true;
      post('/api/together/join', { code: code })
        .then(function (res) {
          btn.disabled = false;
          if (!res.ok) { showError(res.message || '加入失败'); return; }
          $('tgCode').value = '';
          showError('');
          // 服务端判定已是成员（例如在别的设备/标签页加入过）→ 提醒，不静默重进
          if (res.already) return alreadyJoined(res.code, res.name);
          return afterMembership(res.code);
        })
        .catch(function (ex) { btn.disabled = false; showError(ex.message); });
    });
  }

  function boot() {
    bind();
    Promise.all([
      api('/api/together/rooms?tz=' + TZ),
      api('/api/together/scopes')
    ]).then(function (res) {
      $('tgLoading').hidden = true;
      if (res[0].guest) { $('tgGuest').hidden = false; return; }
      state.rooms = res[0].rooms || [];
      state.scopes = res[1];
      fillScopeSelects(state.scopes);
      fillUnits();
      $('tgBody').hidden = false;
      renderRooms();
      renderDetailEmpty();

      var q = (new URLSearchParams(location.search).get('code') || '').trim().toUpperCase();
      var mine = state.rooms.some(function (r) { return r.code === q; });
      if (q && !mine) {
        // 分享链接：自动尝试加入，再打开房间
        post('/api/together/join', { code: q })
          .then(function (r) {
            if (!r.ok) { showError(r.message || '加入房间失败'); return; }
            // 早已是成员（例如在别的设备加过）→ 提示一次，不当成失败
            if (r.already) return alreadyJoined(r.code, r.name);
            return afterMembership(r.code);
          })
          .catch(function (ex) { showError(ex.message); });
      } else if (q || state.rooms.length) {
        openRoom(q || state.rooms[0].code).catch(function (ex) { showError(ex.message); });
      }

      setInterval(function () { if (!document.hidden) refreshAll(); }, POLL_MS);
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) refreshAll();
      });
    }).catch(function (e) {
      $('tgLoading').hidden = true;
      showError(e.message || '加载失败');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
