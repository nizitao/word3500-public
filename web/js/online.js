// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

/*!
 * word3500 在线人数组件（原生 JS，零依赖）
 *
 * - client_id 存 localStorage，同一浏览器多个标签页算一位访客。
 * - 优先 EventSource SSE：服务端在连接建立 / 断开 / 人数变化时主动推送。
 * - SSE 不可用或连续失败时自动降级为 /api/online/ping 轮询心跳。
 * - pagehide 用 sendBeacon 显式 leave；服务端带 2 秒宽限避免站内跳转闪断。
 * - 页面从 bfcache 恢复、从后台切回前台时立即重新上报。
 *
 * 对外暴露 window.word3500Online：{ cid, snapshot, subscribe }，报表页可复用。
 */
(function () {
  'use strict';

  var CID_KEY = 'word3500_client_id';
  var TAB_KEY = 'word3500_online_tabs';   // 同源标签页计数：只有最后一个标签关闭才下线
  var STREAM = '/api/online/stream';
  var PING_URL = '/api/online/ping';
  var LEAVE_URL = '/api/online/leave';
  var PING_MS = 20000;          // 轮询模式心跳间隔，须小于服务端 TTL(60s)
  var SUBSCRIBERS = [];

  var CSS = [
    '.w35-online-badge{position:fixed;right:18px;bottom:18px;z-index:9998;display:flex;align-items:center;gap:7px;',
      'padding:7px 13px;border-radius:999px;border:1px solid var(--border-default,#cfd6e0);',
      'background:var(--bg-card,#fff);color:var(--text-primary,#222);',
      'box-shadow:var(--shadow-card,0 4px 24px rgba(0,0,0,.08));',
      'font:13px/1.2 "Microsoft YaHei","PingFang SC",Arial,sans-serif;cursor:pointer;',
      'user-select:none;-webkit-tap-highlight-color:transparent;transition:box-shadow .2s,transform .2s}',
    '.w35-online-badge:hover{transform:translateY(-1px);box-shadow:0 8px 28px rgba(0,0,0,.14)}',
    '.w35-online-dot{width:8px;height:8px;border-radius:50%;background:#22c55e;flex:none;',
      'box-shadow:0 0 0 3px rgba(34,197,94,.16);animation:w35-pulse 2s infinite}',
    '.w35-online-badge.w35-offline .w35-online-dot{background:#94a3b8;box-shadow:none;animation:none}',
    '@keyframes w35-pulse{0%,100%{box-shadow:0 0 0 3px rgba(34,197,94,.16)}',
      '50%{box-shadow:0 0 0 6px rgba(34,197,94,.05)}}',
    '@media (prefers-reduced-motion:reduce){.w35-online-dot{animation:none}',
      '.w35-online-badge{transition:none}}',
    '.w35-online-num{font-size:16px;font-weight:700;color:var(--primary,#2f6fed);font-variant-numeric:tabular-nums;min-width:1ch;text-align:center}',
    '.w35-online-label,.w35-online-unit{color:var(--text-secondary,#475569)}',
    '.w35-online-panel{position:absolute;right:0;bottom:calc(100% + 10px);width:216px;',
      'padding:12px 14px;border-radius:12px;border:1px solid var(--border-light,#e3e8ef);',
      'background:var(--bg-card,#fff);box-shadow:var(--shadow-card,0 4px 24px rgba(0,0,0,.08));',
      'cursor:default;white-space:normal}',
    '.w35-online-panel[hidden]{display:none}',
    '.w35-online-panel h4{margin:0 0 8px;font-size:13px;font-weight:600;color:var(--text-primary,#222)}',
    '.w35-online-row{display:flex;justify-content:space-between;font-size:12px;',
      'color:var(--text-secondary,#475569);padding:3px 0}',
    '.w35-online-row b{color:var(--text-primary,#222);font-variant-numeric:tabular-nums}',
    '.w35-online-users{margin-top:8px;padding-top:8px;border-top:1px solid var(--border-lighter,#eef1f5);',
      'font-size:11px;color:var(--text-tertiary,#64748b);word-break:break-all;line-height:1.6}',
    '.w35-online-tip{margin-top:6px;font-size:11px;color:var(--text-muted,#94a3b8)}',
    '@media(max-width:640px){.w35-online-badge{right:10px;bottom:10px;padding:6px 11px;font-size:12px;gap:5px}',
      '.w35-online-num{font-size:14px}.w35-online-panel{width:min(216px,calc(100vw - 24px))}}'
  ].join('');

  function uuid() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
      }
      if (window.crypto && window.crypto.getRandomValues) {
        var b = new Uint8Array(16);
        window.crypto.getRandomValues(b);
        b[6] = (b[6] & 15) | 64;
        b[8] = (b[8] & 63) | 128;
        var h = [];
        for (var i = 0; i < 16; i++) h.push(('0' + b[i].toString(16)).slice(-2));
        return h.join('');
      }
    } catch (e) { /* 降级 */ }
    return 'c-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
  }

  function getCid() {
    try {
      var v = localStorage.getItem(CID_KEY);
      if (!v) { v = uuid(); localStorage.setItem(CID_KEY, v); }
      return v;
    } catch (e) { return uuid(); }
  }

  var cid = getCid();

  // 同一浏览器多标签共用一个 cid（算一位访客）。关闭其中任意一标签时不能下线，
  // 用 localStorage 里的标签计数决定「是否最后一个标签」，只有最后一个才发 leave。
  function tabsAdd() {
    try {
      var v = parseInt(localStorage.getItem(TAB_KEY) || '0', 10) || 0;
      localStorage.setItem(TAB_KEY, String(Math.max(0, v) + 1));
    } catch (e) { /* 隐私模式：退化为单标签语义 */ }
  }
  function tabsRemove() {
    try {
      var v = parseInt(localStorage.getItem(TAB_KEY) || '0', 10) || 0;
      if (v > 1) { localStorage.setItem(TAB_KEY, String(v - 1)); return false; }
      localStorage.removeItem(TAB_KEY);
      return true;
    } catch (e) { return true; }
  }

  function authToken() {
    // 与服务端约定：身份只由 Token 决定，前端不再自报 uid/用户名
    try {
      var raw = localStorage.getItem('word3500_auth');
      if (!raw) return '';
      var st = JSON.parse(raw);
      return (st && st.access_token) || '';
    } catch (e) { return ''; }
  }

  function meta() {
    return { cid: cid };
  }

  function queryString() {
    // SSE 只上报 cid；身份由 /api/online/ping 携带 Token 校验后写入
    return 'cid=' + encodeURIComponent(cid);
  }

  var badge, numEl, panelEl, loggedEl, guestEl, usersEl, tipEl;
  var state = null;
  var es = null;
  var pollTimer = null;
  var retryTimer = null;
  var errors = 0;
  var mode = '';            // 'sse' | 'poll'
  var connectedUid = null;
  var leaving = false;

  function build() {
    if (document.getElementById('w35OnlineBadge')) return false;
    var st = document.createElement('style');
    st.id = 'w35OnlineStyle';
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);

    badge = document.createElement('div');
    badge.id = 'w35OnlineBadge';
    badge.className = 'w35-online-badge w35-offline';
    badge.setAttribute('role', 'status');
    badge.setAttribute('aria-live', 'polite');
    badge.title = '正在连接在线人数…';

    var dot = document.createElement('span');
    dot.className = 'w35-online-dot';
    var label = document.createElement('span');
    label.className = 'w35-online-label';
    label.textContent = '在线';
    numEl = document.createElement('strong');
    numEl.className = 'w35-online-num';
    numEl.textContent = '–';
    var unit = document.createElement('span');
    unit.className = 'w35-online-unit';
    unit.textContent = '人';

    panelEl = document.createElement('div');
    panelEl.className = 'w35-online-panel';
    panelEl.hidden = true;
    var h4 = document.createElement('h4');
    h4.textContent = '实时在线';
    panelEl.appendChild(h4);
    loggedEl = row(panelEl, '已登录用户');
    guestEl = row(panelEl, '游客');
    usersEl = document.createElement('div');
    usersEl.className = 'w35-online-users';
    usersEl.hidden = true;
    panelEl.appendChild(usersEl);
    tipEl = document.createElement('div');
    tipEl.className = 'w35-online-tip';
    tipEl.textContent = '连接断开或关闭页面后会自动移除';
    panelEl.appendChild(tipEl);

    badge.appendChild(dot);
    badge.appendChild(label);
    badge.appendChild(numEl);
    badge.appendChild(unit);
    badge.appendChild(panelEl);
    badge.addEventListener('click', function (ev) {
      if (ev.target === panelEl || panelEl.contains(ev.target)) return;
      panelEl.hidden = !panelEl.hidden;
    });
    document.addEventListener('click', function (ev) {
      if (panelEl && !panelEl.hidden && badge && !badge.contains(ev.target)) panelEl.hidden = true;
    });
    (document.body || document.documentElement).appendChild(badge);
    return true;
  }

  function row(parent, text) {
    var r = document.createElement('div');
    r.className = 'w35-online-row';
    var s = document.createElement('span');
    s.textContent = text;
    var b = document.createElement('b');
    b.textContent = '0';
    r.appendChild(s); r.appendChild(b); parent.appendChild(r);
    return b;
  }

  function apply(snap) {
    if (!snap || typeof snap.online !== 'number') return;
    state = snap;
    if (badge) {
      badge.classList.remove('w35-offline');
      numEl.textContent = String(snap.online);
      badge.title = '当前在线 ' + snap.online + ' 人（点击查看详情）';
      loggedEl.textContent = String(snap.logged_in != null ? snap.logged_in : 0);
      guestEl.textContent = String(snap.guests != null ? snap.guests : 0);
      var names = Array.isArray(snap.users) ? snap.users : [];
      usersEl.hidden = names.length === 0;
      usersEl.textContent = names.length ? '在线：' + names.join('、') : '';
      tipEl.textContent = mode === 'poll' ? '轮询模式 · ' + PING_MS / 1000 + ' 秒更新' : '实时推送 · 断开自动移除';
    }
    for (var i = 0; i < SUBSCRIBERS.length; i++) {
      try { SUBSCRIBERS[i](snap); } catch (e) { /* 订阅者异常不影响组件 */ }
    }
  }

  function markOffline() {
    if (badge) badge.classList.add('w35-offline');
  }

  function startSSE() {
    if (leaving || typeof window.EventSource !== 'function') { startPolling(); return; }
    if (es) { try { es.close(); } catch (e) {} es = null; }
    mode = 'sse';
    connectedUid = authToken();
    // SSE 只带 cid，身份需靠一次带 Token 的 ping 写入服务端
    ping();
    try {
      es = new EventSource(STREAM + '?' + queryString());
    } catch (e) { startPolling(); return; }
    es.addEventListener('online', function (ev) {
      errors = 0;
      try { apply(JSON.parse(ev.data)); } catch (e) { /* 忽略坏帧 */ }
      // 同页登录 / 退出后 Token 变化，用新身份重新上报并重连
      if (authToken() !== connectedUid) startSSE();
    });
    es.onopen = function () { errors = 0; if (badge) badge.classList.remove('w35-offline'); };
    es.onerror = function () {
      if (es) { try { es.close(); } catch (e) {} es = null; }
      markOffline();
      errors += 1;
      if (errors >= 3) { startPolling(); return; }   // 服务端重启 / 代理禁用 SSE
      clearTimeout(retryTimer);
      retryTimer = setTimeout(startSSE, Math.min(15000, 800 * Math.pow(2, errors)));
    };
  }

  function startPolling() {
    if (mode === 'poll') return;
    mode = 'poll';
    if (es) { try { es.close(); } catch (e) {} es = null; }
    ping();
    clearInterval(pollTimer);
    pollTimer = setInterval(ping, PING_MS);
  }

  function ping() {
    if (leaving) return;
    var headers = { 'Content-Type': 'application/json' };
    var tk = authToken();
    if (tk) headers['X-Auth-Token'] = tk;   // 服务端据此校验身份
    fetch(PING_URL, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(meta()),
      cache: 'no-store',
      keepalive: true
    }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) apply(d); else markOffline(); })
      .catch(markOffline);
  }

  function leave() {
    if (leaving) return;
    leaving = true;
    if (es) { try { es.close(); } catch (e) {} es = null; }
    clearInterval(pollTimer);
    clearTimeout(retryTimer);
    // 还有其它标签页开着：只释放本标签的 SSE 线程，不把整位访客下线
    if (!tabsRemove()) return;
    var body = JSON.stringify({ cid: cid });
    try {
      if (navigator.sendBeacon) {
        var blob = new Blob([body], { type: 'application/json' });
        if (navigator.sendBeacon(LEAVE_URL, blob)) return;
      }
    } catch (e) { /* 降级到 keepalive fetch */ }
    try {
      fetch(LEAVE_URL, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: body, keepalive: true, cache: 'no-store'
      });
    } catch (e) { /* 页面正在卸载，尽力而为 */ }
  }

  window.addEventListener('pagehide', leave);
  window.addEventListener('pageshow', function (ev) {
    if (!ev.persisted) return;
    leaving = false;                       // bfcache 恢复：重新计数并上线
    tabsAdd();
    if (mode === 'poll') ping(); else startSSE();
  });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || leaving) return;
    if (mode === 'poll') ping();
    else if (!es) { clearTimeout(retryTimer); startSSE(); }
  });

  function boot() { build(); tabsAdd(); startSSE(); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }

  window.word3500Online = {
    get cid() { return cid; },
    get snapshot() { return state; },
    subscribe: function (fn) {
      if (typeof fn !== 'function') return function () {};
      SUBSCRIBERS.push(fn);
      if (state) { try { fn(state); } catch (e) {} }
      return function () {
        var i = SUBSCRIBERS.indexOf(fn);
        if (i >= 0) SUBSCRIBERS.splice(i, 1);
      };
    }
  };
})();
