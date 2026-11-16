// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

/*!
 * 管理员数据报表页逻辑（原生 JS / 纯 SVG 图表，零第三方依赖）
 *
 * 数据源：GET /api/admin/report?from=&to=&tz=&all=
 * 时间统一由服务端按 UTC 存储，报表按浏览器本地日期（-getTimezoneOffset）聚合。
 * 图表随容器宽度重绘，桌面/移动端共用一份代码。
 */
(function () {
  'use strict';

  if (typeof requireLogin !== 'function' || !requireLogin()) return;
  if (typeof onSessionExpired === 'function') onSessionExpired(function () { logout(); });

  var NS = 'http://www.w3.org/2000/svg';
  var SERIES = [
    { key: 'views', label: '浏览量', color: 'var(--primary,#2f6fed)' },
    { key: 'operations', label: '功能操作', color: '#14b8a6' },
    { key: 'visitors', label: '独立访客', color: '#f59e0b' }
  ];
  var BEHAVIOR_COLORS = {
    view: 'var(--primary,#2f6fed)', operation: '#14b8a6', login: '#8b5cf6',
    register: '#f59e0b', logout: '#64748b', failed_login: '#ef4444'
  };

  var state = {
    data: null,
    series: { views: true, operations: true, visitors: true },
    all: false,
    preset: '7'
  };

  function $(id) { return document.getElementById(id); }
  function n(v) { return Number(v) || 0; }
  function fmtNum(v) { return n(v).toLocaleString('zh-CN'); }
  function pad(v) { return v < 10 ? '0' + v : String(v); }
  function ymd(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function dayOffset(delta) {
    var d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() + delta); return d;
  }
  function escText(v) { return String(v == null ? '' : v); }

  function svg(tag, attrs, text) {
    var el = document.createElementNS(NS, tag);
    if (attrs) {
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k)) el.setAttribute(k, String(attrs[k]));
      }
    }
    if (text != null) el.textContent = String(text);
    return el;
  }

  function setHost(host, svgEl) {
    host.textContent = '';
    host.appendChild(svgEl);
  }

  function niceMax(v) {
    if (v <= 5) return 5;
    var exp = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
    var f = v / exp;
    var nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return nf * exp;
  }

  function axisNum(v) {
    v = n(v);
    if (v >= 10000) return (v / 1000).toFixed(v % 1000 === 0 ? 0 : 1) + 'k';
    return String(Math.round(v));
  }

  // -------------------------------------------------------------------------
  // 鉴权 / 初始化
  // -------------------------------------------------------------------------
  function boot() {
    var user = typeof getCurrentUser === 'function' ? getCurrentUser() : null;
    var bar = $('userBar'), name = $('userName'), out = $('logoutBtn');
    if (bar && user) {
      if (name) name.textContent = user.display_name || user.username || '';
      bar.style.display = 'inline-flex';
    }
    if (out) out.onclick = function () { if (confirm('确定退出？')) logout(); };

    if (!user || user.role !== 'admin') {
      showError('无权访问：仅管理员可以查看数据报表。', false);
      $('rptLoading').hidden = true;
      $('rptContent').hidden = true;
      return;
    }

    applyPreset('7');
    $('rptApply').addEventListener('click', function () {
      state.all = false;
      state.preset = '';
      markPreset('');
      state.from = $('rptFrom').value;
      state.to = $('rptTo').value;
      load();
    });
    $('rptCsv').addEventListener('click', exportCsv);
    $('rptPresets').addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('button[data-range]') : null;
      if (!btn) return;
      applyPreset(btn.getAttribute('data-range'));
      load();
    });
    $('rptFrom').addEventListener('change', function () { state.preset = ''; markPreset(''); state.all = false; });
    $('rptTo').addEventListener('change', function () { state.preset = ''; markPreset(''); state.all = false; });

    buildLegend();
    var t = null;
    window.addEventListener('resize', function () {
      clearTimeout(t);
      t = setTimeout(function () { drawTrend(); drawHourly(); }, 160);
    });

    if (window.word3500Online && typeof window.word3500Online.subscribe === 'function') {
      window.word3500Online.subscribe(function (s) {
        if ($('rptOnlineNum')) $('rptOnlineNum').textContent = String(s.online);
      });
    }
    load();
  }

  function markPreset(key) {
    var btns = $('rptPresets').querySelectorAll('button[data-range]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle('active', btns[i].getAttribute('data-range') === key);
    }
  }

  function applyPreset(key) {
    state.preset = key;
    state.all = key === 'all';
    var from = $('rptFrom'), to = $('rptTo');
    from.disabled = state.all;
    to.disabled = state.all;
    if (key === 'today') { from.value = ymd(dayOffset(0)); to.value = ymd(dayOffset(0)); }
    else if (key === '7') { from.value = ymd(dayOffset(-6)); to.value = ymd(dayOffset(0)); }
    else if (key === '30') { from.value = ymd(dayOffset(-29)); to.value = ymd(dayOffset(0)); }
    else if (key === 'all') { from.value = ''; to.value = ''; }
    state.from = from.value;
    state.to = to.value;
    markPreset(key);
  }

  // -------------------------------------------------------------------------
  // 数据加载
  // -------------------------------------------------------------------------
  function showError(msg, keepContent) {
    var el = $('rptError');
    el.textContent = msg;
    el.hidden = false;
    if (!keepContent) return;
    $('rptLoading').hidden = true;
  }

  function showLoading() {
    $('rptError').hidden = true;
    $('rptLoading').hidden = false;
    $('rptLoading').textContent = '正在加载报表数据…';
    $('rptApply').disabled = true;
  }

  async function load() {
    showLoading();
    var q = new URLSearchParams();
    // 服务端时间是 UTC，用本地时区偏移换算本地自然日
    q.set('tz', String(-new Date().getTimezoneOffset()));
    if (state.all) q.set('all', '1');
    else {
      if (state.from) q.set('from', state.from);
      if (state.to) q.set('to', state.to);
    }
    var fetcher = typeof authFetch === 'function' ? authFetch : fetch;
    try {
      var res = await fetcher('/api/admin/report?' + q.toString());
      var data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.message || data.error || ('请求失败 HTTP ' + res.status));
      }
      state.data = data;
      render();
    } catch (err) {
      $('rptLoading').hidden = true;
      $('rptApply').disabled = false;
      showError('报表加载失败：' + (err && err.message ? err.message : '未知错误'), false);
    } finally {
      $('rptApply').disabled = false;
    }
  }

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------
  function render() {
    var d = state.data;
    $('rptLoading').hidden = true;
    $('rptError').hidden = true;
    $('rptContent').hidden = false;

    var r = d.range, s = d.summary;
    $('rptRangeText').textContent = r.from + ' 至 ' + r.to + '（共 ' + r.days + ' 天，' + tzLabel(r.tz) + '）';
    $('rptOnlineNum').textContent = String(s.online_now);
    $('rptTotalEvents').textContent = '共 ' + fmtNum(s.total_events) + ' 条事件';
    $('rptFoot').textContent = '统计口径：页面浏览 / 登录 / 注册 / 登出 / 接口操作；生成于 ' + d.generated_at + '（UTC）';
    $('rptNote').textContent = '数据来自服务端访问日志 access_log，按浏览器本地日期（' + tzLabel(r.tz) +
      '）统计；在线心跳、报表查询等基础设施请求不计入，时间范围最长 365 天。';

    renderCards(s);
    renderBehaviors(d.behaviors || []);
    renderTopPages(d.top_pages || []);
    drawTrend();
    drawHourly();
  }

  function tzLabel(minutes) {
    var m = n(minutes);
    if (m === 0) return 'UTC';
    var sign = m > 0 ? '+' : '-';
    var a = Math.abs(m);
    return 'UTC' + sign + Math.floor(a / 60) + (a % 60 ? ':' + pad(a % 60) : '');
  }

  function renderCards(s) {
    var cards = [
      { label: '来访人数', value: s.visitors, hint: '区间内独立 IP', accent: true },
      { label: '页面浏览量', value: s.views, hint: '日均 ' + s.avg_daily_views },
      { label: '功能操作', value: s.operations, hint: '接口调用次数' },
      { label: '当前在线', value: s.online_now, hint: '实时连接数', accent: true },
      { label: '登录次数', value: s.logins, hint: '失败 ' + fmtNum(s.failed_logins) },
      { label: '注册人数', value: s.registers, hint: '登出 ' + fmtNum(s.logouts) },
      { label: '活跃用户', value: s.active_users, hint: '有行为的登录用户' },
      { label: '注册用户总数', value: s.total_users, hint: '系统累计' }
    ];
    var host = $('rptCards');
    host.textContent = '';
    cards.forEach(function (c) {
      var box = document.createElement('div'); box.className = 'rpt-kpi';
      var l = document.createElement('div'); l.className = 'rpt-kpi-label'; l.textContent = c.label;
      var v = document.createElement('div'); v.className = 'rpt-kpi-value' + (c.accent ? ' accent' : '');
      v.textContent = fmtNum(c.value);
      var h = document.createElement('div'); h.className = 'rpt-kpi-hint'; h.textContent = c.hint;
      box.appendChild(l); box.appendChild(v); box.appendChild(h);
      host.appendChild(box);
    });
  }

  function buildLegend() {
    var host = $('rptLegend');
    host.textContent = '';
    SERIES.forEach(function (item) {
      var b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('data-key', item.key);
      var i = document.createElement('i'); i.style.background = item.color;
      b.appendChild(i);
      b.appendChild(document.createTextNode(item.label));
      b.addEventListener('click', function () {
        var key = item.key;
        var onCount = SERIES.filter(function (x) { return state.series[x.key]; }).length;
        if (state.series[key] && onCount === 1) return;   // 至少保留一条曲线
        state.series[key] = !state.series[key];
        syncLegend();
        drawTrend();
      });
      host.appendChild(b);
    });
    syncLegend();
  }

  function syncLegend() {
    var btns = $('rptLegend').querySelectorAll('button[data-key]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle('off', !state.series[btns[i].getAttribute('data-key')]);
    }
  }

  function renderBehaviors(list) {
    var host = $('rptBehaviors');
    host.textContent = '';
    if (!list.length) { empty(host, '所选时间范围内暂无行为数据'); return; }
    list.forEach(function (b) {
      var row = document.createElement('div'); row.className = 'rpt-beh-row';
      var top = document.createElement('div'); top.className = 'rpt-beh-top';
      var nameEl = document.createElement('span'); nameEl.className = 'rpt-beh-name';
      nameEl.textContent = escText(b.label);
      var num = document.createElement('span'); num.className = 'rpt-beh-num';
      num.textContent = fmtNum(b.count);
      var pct = document.createElement('em'); pct.textContent = b.percent + '%';
      num.appendChild(pct);
      top.appendChild(nameEl); top.appendChild(num);
      var track = document.createElement('div'); track.className = 'rpt-beh-track';
      var fill = document.createElement('div'); fill.className = 'rpt-beh-fill';
      fill.style.width = n(b.count) > 0 ? Math.max(n(b.percent), 1.5) + '%' : '0%';
      fill.style.background = BEHAVIOR_COLORS[b.type] || 'var(--primary,#2f6fed)';
      track.appendChild(fill);
      row.appendChild(top); row.appendChild(track);
      host.appendChild(row);
    });
  }

  function renderTopPages(list) {
    var host = $('rptTopPages');
    host.textContent = '';
    if (!list.length) { empty(host, '所选时间范围内暂无页面浏览记录'); return; }
    var max = 1;
    list.forEach(function (p) { max = Math.max(max, n(p.count)); });
    list.forEach(function (p) {
      var row = document.createElement('div'); row.className = 'rpt-page-row';
      var left = document.createElement('div');
      var path = document.createElement('div'); path.className = 'rpt-page-path';
      path.textContent = escText(p.path); path.title = escText(p.path);
      var bar = document.createElement('div'); bar.className = 'rpt-page-bar';
      bar.style.width = (n(p.count) / max * 100) + '%';
      left.appendChild(path); left.appendChild(bar);
      var cnt = document.createElement('div'); cnt.className = 'rpt-page-count';
      cnt.textContent = fmtNum(p.count);
      row.appendChild(left); row.appendChild(cnt);
      host.appendChild(row);
    });
  }

  function empty(host, text) {
    var p = document.createElement('div'); p.className = 'rpt-empty'; p.textContent = text;
    host.appendChild(p);
  }

  // -------------------------------------------------------------------------
  // SVG 图表
  // -------------------------------------------------------------------------
  function drawTrend() {
    var host = $('rptTrendChart');
    if (!host || !state.data) return;
    var rows = state.data.trend || [];
    var W = Math.max(300, Math.round(host.clientWidth || 640));
    var H = 248;
    var P = { l: 42, r: 12, t: 12, b: 28 };
    var iw = W - P.l - P.r, ih = H - P.t - P.b;
    var maxV = 1;
    rows.forEach(function (r) {
      SERIES.forEach(function (s) {
        if (state.series[s.key]) maxV = Math.max(maxV, n(r[s.key]));
      });
    });
    maxV = niceMax(maxV);
    var xf = function (i) { return rows.length <= 1 ? P.l + iw / 2 : P.l + i * iw / (rows.length - 1); };
    var yf = function (v) { return P.t + ih - n(v) / maxV * ih; };
    var root = svg('svg', {
      viewBox: '0 0 ' + W + ' ' + H, width: W, height: H,
      role: 'img', 'aria-label': '按日访问趋势折线图'
    });

    for (var g = 0; g <= 4; g++) {
      var val = maxV * g / 4, y = yf(val);
      root.appendChild(svg('line', { x1: P.l, y1: y, x2: W - P.r, y2: y, 'class': 'rpt-gridline' }));
      root.appendChild(svg('text', {
        x: P.l - 7, y: y + 3, 'text-anchor': 'end', 'class': 'rpt-axis-text'
      }, axisNum(val)));
    }

    if (!rows.length) {
      root.appendChild(svg('text', {
        x: W / 2, y: H / 2, 'text-anchor': 'middle', 'class': 'rpt-axis-text'
      }, '暂无趋势数据'));
      setHost(host, root);
      return;
    }

    var step = Math.max(1, Math.ceil(rows.length / 8));
    rows.forEach(function (r, i) {
      if (i % step !== 0 && i !== rows.length - 1) return;
      var anchor = i === 0 ? 'start' : (i === rows.length - 1 ? 'end' : 'middle');
      root.appendChild(svg('text', {
        x: xf(i), y: H - 8, 'text-anchor': anchor, 'class': 'rpt-axis-text'
      }, String(r.date).slice(5)));
    });

    // 逆序绘制，浏览量在最上层
    SERIES.slice().reverse().forEach(function (s) {
      if (!state.series[s.key]) return;
      var d = '';
      rows.forEach(function (r, i) {
        d += (i ? ' L' : 'M') + xf(i).toFixed(1) + ' ' + yf(r[s.key]).toFixed(1);
      });
      if (rows.length > 1) {
        root.appendChild(svg('path', {
          d: d + ' L' + xf(rows.length - 1).toFixed(1) + ' ' + (P.t + ih) +
             ' L' + xf(0).toFixed(1) + ' ' + (P.t + ih) + ' Z',
          'class': 'rpt-area-' + s.key
        }));
      }
      root.appendChild(svg('path', { d: d, 'class': 'rpt-line rpt-line-' + s.key }));
      if (rows.length <= 20) {
        rows.forEach(function (r, i) {
          var c = svg('circle', {
            cx: xf(i), cy: yf(r[s.key]), r: 3, 'class': 'rpt-point-' + s.key
          });
          c.appendChild(svg('title', null, r.date + ' · ' + s.label + ' ' + fmtNum(r[s.key])));
          root.appendChild(c);
        });
      }
    });
    setHost(host, root);
  }

  function drawHourly() {
    var host = $('rptHourly');
    if (!host || !state.data) return;
    var hourly = state.data.hourly || [];
    var W = Math.max(300, Math.round(host.clientWidth || 420));
    var H = 180;
    var P = { l: 34, r: 8, t: 10, b: 24 };
    var iw = W - P.l - P.r, ih = H - P.t - P.b;
    var maxV = niceMax(Math.max.apply(null, hourly.concat([1])));
    var root = svg('svg', {
      viewBox: '0 0 ' + W + ' ' + H, width: W, height: H,
      role: 'img', 'aria-label': '24 小时事件分布柱状图'
    });
    for (var g = 0; g <= 2; g++) {
      var val = maxV * g / 2, y = P.t + ih - val / maxV * ih;
      root.appendChild(svg('line', { x1: P.l, y1: y, x2: W - P.r, y2: y, 'class': 'rpt-gridline' }));
      root.appendChild(svg('text', {
        x: P.l - 6, y: y + 3, 'text-anchor': 'end', 'class': 'rpt-axis-text'
      }, axisNum(val)));
    }
    var slot = iw / 24;
    var bw = Math.max(3, Math.min(18, slot - 4));
    for (var h = 0; h < 24; h++) {
      var v = n(hourly[h]);
      var bh = v / maxV * ih;
      var x = P.l + h * slot + (slot - bw) / 2;
      var bar = svg('rect', {
        x: x.toFixed(1), y: (P.t + ih - bh).toFixed(1), width: bw.toFixed(1),
        height: Math.max(v > 0 ? 2 : 0, bh).toFixed(1), rx: 2, 'class': 'rpt-hour-bar'
      });
      bar.appendChild(svg('title', null, pad(h) + ':00–' + pad(h) + ':59 · ' + fmtNum(v) + ' 次'));
      root.appendChild(bar);
      if (h % 3 === 0) {
        root.appendChild(svg('text', {
          x: (P.l + h * slot + slot / 2).toFixed(1), y: H - 7,
          'text-anchor': 'middle', 'class': 'rpt-axis-text'
        }, pad(h)));
      }
    }
    setHost(host, root);
  }

  // -------------------------------------------------------------------------
  // CSV 导出
  // -------------------------------------------------------------------------
  function csvCell(v) {
    var s = String(v == null ? '' : v);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  function exportCsv() {
    if (!state.data) return;
    var d = state.data;
    var lines = [['日期', '浏览量', '功能操作', '登录', '注册', '独立访客', '总事件']];
    (d.trend || []).forEach(function (r) {
      lines.push([r.date, r.views, r.operations, r.logins, r.registers, r.visitors, r.events]);
    });
    lines.push([]);
    lines.push(['行为类型', '数量', '占比(%)']);
    (d.behaviors || []).forEach(function (b) { lines.push([b.label, b.count, b.percent]); });
    var text = '\ufeff' + lines.map(function (row) {
      return row.map(csvCell).join(',');
    }).join('\r\n');
    var blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'word3500-report-' + d.range.from + '_' + d.range.to + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
