// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

/* 学习统计页 —— 打卡 / 日历 / 掌握率 / 词类 / 复习 / 分册 / 排行榜 / 徽章
   数据全部来自服务端聚合接口（api/study.py），前端不做二次口径计算。 */
(function () {
  var TZ = -new Date().getTimezoneOffset();
  var WORD_TOTAL = 5093;

  var state = {
    month: '',
    trendDays: 30,
    lbMetric: 'words',
    lbRange: 'week',
    summary: null,
    calendar: {},
    me: null
  };

  var BADGES = [
    { group: '连续学习', items: [
      { id: 'streak_3', label: '连续 3 天', ico: '🔥' },
      { id: 'streak_7', label: '连续 7 天', ico: '🔥' },
      { id: 'streak_14', label: '连续 14 天', ico: '⚡' },
      { id: 'streak_30', label: '连续 30 天', ico: '⚡' },
      { id: 'streak_60', label: '连续 60 天', ico: '🌟' },
      { id: 'streak_100', label: '连续 100 天', ico: '🌟' },
      { id: 'streak_365', label: '连续 365 天', ico: '👑' }
    ]},
    { group: '掌握词数', items: [
      { id: 'master_10', label: '掌握 10 词', ico: '📗' },
      { id: 'master_30', label: '掌握 30 词', ico: '📘' },
      { id: 'master_100', label: '掌握 100 词', ico: '📚' },
      { id: 'master_500', label: '掌握 500 词', ico: '📚' },
      { id: 'master_1000', label: '掌握 1000 词', ico: '🏆' }
    ]},
    { group: '累计学时', items: [
      { id: 'hours_10', label: '学习 10 小时', ico: '⏱' },
      { id: 'hours_50', label: '学习 50 小时', ico: '⏱' },
      { id: 'hours_100', label: '学习 100 小时', ico: '⏳' }
    ]}
  ];

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function todayStr() {
    var d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  function fmtNum(n) { return Number(n || 0).toLocaleString('zh-CN'); }

  function api(path, opts) {
    if (typeof authFetch !== 'function') return Promise.reject(new Error('no auth'));
    return authFetch(path, opts).then(function (r) {
      // 先验 content-type 再解析：服务端未部署新接口 / 会话过期被重定向时，
      // 拿到的是 HTML 页面，直接 r.json() 只会抛 "Unexpected token '<'"，
      // 让人误以为是数据格式问题，实际是路由或登录态问题。
      var ct = (r.headers.get('content-type') || '').toLowerCase();
      if (ct.indexOf('json') < 0) {
        if (r.status === 404) throw new Error('接口不存在（HTTP 404），请确认服务端已更新并重启');
        if (r.status === 401 || r.status === 403) throw new Error('登录状态已失效，请重新登录');
        throw new Error('服务返回了非 JSON 响应（HTTP ' + r.status + '）');
      }
      return r.json().then(function (d) {
        if (d && d.error === 'not_found') throw new Error('接口不存在（HTTP 404），请确认服务端已更新并重启');
        if (!r.ok && d && d.message) throw new Error(d.message);
        if (!r.ok && r.status >= 500) throw new Error('HTTP ' + r.status);
        return d;
      });
    });
  }

  function showError(msg) {
    var box = $('stError');
    box.textContent = msg;
    box.hidden = false;
  }

  // ---------------- 打卡 hero ----------------
  function renderHero(s) {
    var hero = $('stHero');
    var done = !!s.checked_in;
    hero.className = 'st-hero' + (done ? ' done' : '');
    $('stCheckBadge').textContent = done ? (s.makeup ? '今日已打卡（补签）' : '今日已打卡') : '今日未打卡';
    $('stStreak').textContent = s.streak || 0;

    var needAns = Math.max(0, s.goal.answered - s.answered);
    var needNew = Math.max(0, s.goal.new_words - s.new_words);
    $('stCheckHint').textContent = done
      ? '保持节奏，明天继续'
      : (needAns <= needNew
          ? '再答 ' + needAns + ' 题即可打卡'
          : '再新学 ' + needNew + ' 个词即可打卡');

    $('stHeroNums').innerHTML =
      numBox('今日新学', s.new_words) +
      numBox('今日复习', s.reviewed) +
      numBox('今日答题', s.answered) +
      numBox('答对', s.correct);

    var pct = Math.max(s.answered / s.goal.answered, s.new_words / s.goal.new_words);
    pct = Math.min(1, pct || 0);
    $('stGoalFill').style.width = (pct * 100).toFixed(1) + '%';
    $('stGoalText').textContent = done
      ? '今日目标已达成 · 累计积分 ' + fmtNum(s.points)
      : '今日目标进度 ' + Math.round(pct * 100) + '%（答题 ' + s.answered + '/' + s.goal.answered +
        ' · 新学 ' + s.new_words + '/' + s.goal.new_words + '）';
  }

  function numBox(label, v) {
    return '<div class="st-hero-num">' + esc(label) + '<b>' + fmtNum(v) + '</b></div>';
  }

  function renderKpis(s) {
    var rate = s.learned_total ? (s.mastered_total / s.learned_total * 100) : 0;
    var cover = WORD_TOTAL ? (s.learned_total / WORD_TOTAL * 100) : 0;
    var items = [
      { label: '已学词数', value: fmtNum(s.learned_total), hint: '词库覆盖 ' + cover.toFixed(1) + '%', accent: true },
      { label: '已掌握', value: fmtNum(s.mastered_total), hint: '答对 ≥3 次且正确率 ≥80%' },
      { label: '掌握率', value: s.learned_total < 20 ? '样本不足' : rate.toFixed(1) + '%',
        hint: s.learned_total < 20 ? '已学 ' + s.learned_total + ' 词' : '掌握 ÷ 已学' },
      { label: '待复习', value: fmtNum(s.due_review || 0), hint: '已学未掌握且到期' },
      { label: '积分', value: fmtNum(s.points), hint: '打卡 / 首学累积' }
    ];
    $('stKpis').innerHTML = items.map(function (it) {
      return '<div class="st-kpi">' +
        '<div class="st-kpi-label">' + esc(it.label) + '</div>' +
        '<div class="st-kpi-value' + (it.accent ? ' accent' : '') + '">' + esc(it.value) + '</div>' +
        '<div class="st-kpi-hint">' + esc(it.hint) + '</div></div>';
    }).join('');
  }

  // ---------------- 日历 ----------------
  function loadCalendar() {
    var month = state.month || todayStr().slice(0, 7);
    return api('/api/study/calendar?tz=' + TZ + '&month=' + month).then(function (d) {
      if (d.guest) return;
      state.month = d.month;
      state.calendar = {};
      (d.days || []).forEach(function (x) { state.calendar[x.day] = x; });
      renderCalendar(d.today);
    });
  }

  function renderCalendar(today) {
    var month = state.month;
    var y = parseInt(month.slice(0, 4), 10);
    var m = parseInt(month.slice(5, 7), 10);
    $('stCalTitle').textContent = y + ' 年 ' + m + ' 月';
    $('stCalNext').disabled = month >= today.slice(0, 7);

    var first = new Date(y, m - 1, 1);
    var lead = (first.getDay() + 6) % 7;              // 周一为一周起点
    var days = new Date(y, m, 0).getDate();
    var html = '';
    for (var i = 0; i < lead; i++) html += '<div class="st-cal-cell empty"></div>';
    for (var d = 1; d <= days; d++) {
      var key = month + '-' + pad(d);
      var st = state.calendar[key];
      var total = st ? (st.new_words || 0) + (st.reviewed || 0) : 0;
      // 四档色阶：0 / 1–10 / 11–30 / 30+
      var lv = 0;
      if (total > 30) lv = 4;
      else if (total > 10) lv = 2;
      else if (total > 0) lv = 1;
      var cls = 'st-cal-cell' + (lv ? ' lv' + lv : '');
      if (st && st.makeup) cls += ' makeup';
      if (key === today) cls += ' today';
      if (key > today) cls += ' future';
      html += '<div class="' + cls + '" data-day="' + key + '" title="' +
        esc(key + ' · 新学 ' + (st ? st.new_words : 0) + ' · 复习 ' + (st ? st.reviewed : 0)) + '">' + d + '</div>';
    }
    $('stCalGrid').innerHTML = html;
  }

  function openDay(day) {
    openDrawer(day + ' 学习明细', '<div class="st-empty">加载中…</div>');
    api('/api/study/day?tz=' + TZ + '&day=' + day).then(function (d) {
      if (!d.ok) { $('stDrawerBody').innerHTML = '<div class="st-empty">加载失败</div>'; return; }
      var st = d.stat || {};
      var html = '<div class="st-day-stats">' +
        statBox('新学', st.new_words) + statBox('复习', st.reviewed) +
        statBox('答题', st.answered) + statBox('答对', st.correct) +
        statBox('已学', st.learned_total) + statBox('已掌握', st.mastered_total) + '</div>';

      var canMakeup = day < todayStr() && !st.checked_in && day.slice(0, 7) === todayStr().slice(0, 7);
      if (st.checked_in) {
        html += '<p class="st-note">' + (st.makeup ? '该日通过补签打卡（不计入连续天数）' : '该日已打卡') + '</p>';
      } else if (canMakeup) {
        html += '<p class="st-note">该日未打卡，可补签（每月 2 次，补签不计入连续天数）</p>' +
          '<button class="st-btn" id="stMakeupBtn" style="margin:6px 0 14px">补签这一天</button>';
      } else {
        html += '<p class="st-note">该日未打卡</p>';
      }

      if (d.events && d.events.length) {
        html += '<div class="st-badge-title">学习记录（最近 ' + d.events.length + ' / 共 ' + d.total + ' 条）</div>' +
          d.events.map(function (e) {
            var tag = e.action === 'review' ? '复习' : (e.action === 'learn' ? '学习' : '自检');
            var cls = e.result === 1 ? ' ok' : (e.result === 0 ? ' bad' : '');
            return '<div class="st-event">' +
              '<span class="st-event-tag' + cls + '">' + tag + '</span>' +
              '<span class="st-event-word">' + esc(e.word || ('#' + e.word_id)) + '</span>' +
              '<span class="st-event-mean">' + esc(e.meaning || '') + '</span></div>';
          }).join('');
      } else {
        html += '<div class="st-empty">这一天还没有学习记录</div>';
      }
      $('stDrawerBody').innerHTML = html;

      var mk = $('stMakeupBtn');
      if (mk) mk.onclick = function () { doMakeup(day, mk); };
    }).catch(function () {
      $('stDrawerBody').innerHTML = '<div class="st-empty">加载失败</div>';
    });
  }

  function statBox(label, v) {
    return '<div>' + esc(label) + '<b>' + fmtNum(v) + '</b></div>';
  }

  function doMakeup(day, btn) {
    btn.disabled = true;
    btn.textContent = '提交中…';
    api('/api/study/makeup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ day: day, tz: TZ })
    }).then(function (d) {
      if (!d.ok) {
        btn.textContent = d.message || '补签失败';
        return;
      }
      btn.textContent = '已补签';
      refreshAll();
      setTimeout(closeDrawer, 700);
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = '补签失败，重试';
    });
  }

  // ---------------- 掌握率折线 ----------------
  function loadTrend() {
    return api('/api/study/mastery-trend?tz=' + TZ + '&days=' + state.trendDays).then(function (d) {
      if (d.guest) return;
      renderTrend(d.points || []);
    });
  }

  function renderTrend(points) {
    var host = $('stTrendChart');
    var bars = $('stNewBars');
    var note = $('stTrendNote');

    if (!points.length) {
      host.innerHTML = '<div class="st-empty">还没有学习记录<br>完成几道题后，这里会出现掌握率曲线</div>';
      bars.innerHTML = '';
      $('stTrendFrom').textContent = '';
      $('stTrendTo').textContent = '';
      note.textContent = '掌握率 = 累计已掌握 ÷ 累计已学；数据从你第一次学习的那天开始记录。';
      return;
    }
    if (points.length < 2) {
      var p0 = points[0];
      host.innerHTML = '<div class="st-empty">今天是记录的第 1 天：已学 ' + p0.learned +
        ' 词 · 掌握 ' + p0.mastered + ' 词（' + p0.rate + '%）<br>' +
        '再坚持 1 天就能看到掌握率曲线</div>';
      bars.innerHTML = '';
      $('stTrendFrom').textContent = p0.day;
      $('stTrendTo').textContent = p0.day;
      note.textContent = '掌握率 = 累计已掌握 ÷ 累计已学（' + p0.mastered + ' / ' + p0.learned + '）' +
        '　· 词库覆盖率 ' + (p0.learned / WORD_TOTAL * 100).toFixed(1) + '%';
      return;
    }
    var last = points[points.length - 1];
    var labels = points.map(function (p) { return p.day.slice(5).replace('-', '/'); });
    var rates = points.map(function (p) { return p.rate; });

    $('stTrendFrom').textContent = points[0].day;
    $('stTrendTo').textContent = last.day;

    if (window.W35Charts && typeof window.W35Charts.line === 'function') {
      window.W35Charts.line(host, {
        labels: labels,
        series: [{ key: 'rate', label: '掌握率', color: '#2f6fed', values: rates }],
        height: 220,
        yMax: 100,
        yFormat: function (v) { return v + '%'; },
        tooltip: function (i) {
          var p = points[i];
          return p.day + '　掌握 ' + p.mastered + ' / 已学 ' + p.learned +
            '（' + p.rate + '%）' + (p.new_words ? '　新增 ' + p.new_words + ' 词' : '');
        },
        emptyText: '暂无数据'
      });
    } else {
      host.innerHTML = '<div class="st-empty">图表组件未加载</div>';
    }

    // 每日新增词柱状（CSS 实现，避免两条不同量纲的线挤在一张图里）
    var maxNew = 0;
    points.forEach(function (p) { if (p.new_words > maxNew) maxNew = p.new_words; });
    bars.innerHTML = points.map(function (p) {
      var h = maxNew > 0 ? Math.round(p.new_words / maxNew * 100) : 0;
      return '<div class="st-newbar' + (p.new_words ? '' : ' zero') + '" style="height:' +
        Math.max(h, p.new_words ? 4 : 2) + '%" title="' +
        esc(p.day + ' 新增 ' + p.new_words + ' 词') + '"></div>';
    }).join('');

    var lowSample = last.learned < 20;
    note.textContent = '掌握率 = 累计已掌握 ÷ 累计已学（' + last.mastered + ' / ' + last.learned + '）' +
      (lowSample ? '　· 已学不足 20 词，样本不足仅供参考' : '') +
      '　· 词库覆盖率 ' + (last.learned / WORD_TOTAL * 100).toFixed(1) + '%' +
      '　· 下方柱状为每日新增词数';
  }

  // ---------------- 词类 ----------------
  function loadPos() {
    return api('/api/study/pos-stats').then(function (d) {
      if (d.guest) return;
      renderPos(d.items || []);
    });
  }

  function renderPos(items) {
    var host = $('stPosList');
    if (!items.length) { host.innerHTML = '<div class="st-empty">暂无数据</div>'; return; }

    // 用 DOM 行而非 SVG 柱状：设计文档要求并排展示「该类总词数 / 已学数」作为样本量参考，
    // SVG 柱状图的行内空间放不下这组数字，且低样本行需要单独置灰。
    host.innerHTML = items.map(function (it) {
      return '<div class="st-pos-row' + (it.low_sample ? ' low' : '') + '" data-pos="' + esc(it.pos) + '"' +
        ' title="' + esc(it.label + ' · 已学 ' + it.learned + ' / 共 ' + it.total + ' 词' +
                          (it.low_sample ? ' · 样本不足' : '')) + '">' +
        '<span class="st-pos-name">' + esc(it.label) + '</span>' +
        '<span class="st-pos-track"><span class="st-pos-fill" style="width:' + it.rate + '%"></span></span>' +
        '<span class="st-pos-num"><b>' + it.rate + '%</b> · ' + it.learned + '/' + it.total + '</span></div>';
    }).join('');

    // 洞察：样本足够（已学 ≥5）里掌握率最低的一类
    var candidates = items.filter(function (x) { return !x.low_sample && x.learned >= 5; });
    var box = $('stPosInsight');
    if (!candidates.length) {
      box.hidden = true;
      return;
    }
    var weakest = candidates.slice().sort(function (a, b) { return a.rate - b.rate; })[0];
    var learnedAll = items.reduce(function (s, x) { return s + x.learned; }, 0);
    var masteredAll = items.reduce(function (s, x) { return s + x.mastered; }, 0);
    var overall = learnedAll ? masteredAll / learnedAll * 100 : 0;
    box.hidden = false;
    if (weakest.rate + 5 < overall) {
      box.textContent = '你的「' + weakest.label + '」掌握率 ' + weakest.rate + '%，低于整体 ' +
        overall.toFixed(1) + '%，建议加入复习队列重点巩固（已学 ' + weakest.learned + ' 词）。';
    } else {
      box.textContent = '各类词性掌握较均衡，整体掌握率 ' + overall.toFixed(1) + '%。继续保持。';
    }
  }

  function openPosWords(pos, label) {
    openDrawer(label + ' · 词表', '<div class="st-empty">加载中…</div>');
    api('/api/words?pos=' + encodeURIComponent(pos) + '&page=1').then(function (d) {
      var rows = d.words || [];
      if (!rows.length) { $('stDrawerBody').innerHTML = '<div class="st-empty">该类暂无单词</div>'; return; }
      $('stDrawerBody').innerHTML =
        '<p class="st-note">共 ' + fmtNum(d.total) + ' 词，显示前 ' + rows.length + ' 个</p>' +
        '<div class="st-wordlist">' + rows.map(function (w) {
          return '<div class="st-wordrow"><b>' + esc(w.word) + '</b><span>' + esc(w.meaning) + '</span></div>';
        }).join('') + '</div>';
    }).catch(function () {
      $('stDrawerBody').innerHTML = '<div class="st-empty">加载失败</div>';
    });
  }

  // ---------------- 复习 ----------------
  function loadReview() {
    return api('/api/study/review?tz=' + TZ).then(function (d) {
      if (d.guest) return;
      renderReview(d);
    });
  }

  function renderReview(d) {
    var b = d.buckets || { overdue: 0, today: 0, week: 0, total: 0 };
    $('stReviewBoxes').innerHTML =
      '<div class="st-review-box overdue"><b>' + fmtNum(b.overdue) + '</b><span>已逾期</span></div>' +
      '<div class="st-review-box today"><b>' + fmtNum(b.today) + '</b><span>今日到期</span></div>' +
      '<div class="st-review-box week"><b>' + fmtNum(b.week) + '</b><span>未来 7 天</span></div>';

    var top = d.overdue_top || [];
    $('stOverdue').innerHTML = top.length
      ? '<div class="st-badge-title">逾期最久</div>' + top.map(function (w) {
          var days = '';
          if (w.next_review_at) {
            var t = Date.parse(w.next_review_at.replace(' ', 'T') + 'Z');
            if (!isNaN(t)) {
              var diff = Math.floor((Date.now() - t) / 86400000);
              days = diff > 0 ? diff + ' 天' : '今日';
            }
          }
          return '<div class="st-overdue-item">' +
            '<span class="st-overdue-word">' + esc(w.word) + '</span>' +
            '<span class="st-overdue-mean">' + esc(w.meaning) + '</span>' +
            '<span class="st-overdue-days">' + days + '</span></div>';
        }).join('')
      : '<div class="st-empty" style="padding:14px 0">暂无逾期单词</div>';

    var btn = $('stReviewBtn');
    btn.disabled = !b.total;
    btn.textContent = b.total ? '开始复习（' + b.total + ' 词）' : '暂无需复习';
    $('stReviewHint').textContent = b.total
      ? '按遗忘曲线排序，答错会重置为 1 天后再次出现'
      : '继续学习新词，到期后会自动进入队列';
  }

  // ---------------- 分册 ----------------
  function loadBooks() {
    return api('/api/study/book-stats').then(function (d) {
      if (d.guest) return;
      renderBooks(d.books || []);
    });
  }

  function renderBooks(books) {
    var host = $('stBooks');
    if (!books.length) { host.innerHTML = '<div class="st-empty">暂无分册数据</div>'; return; }
    var total = books.reduce(function (s, b) { return s + b.total; }, 0);
    $('stBookTotal').textContent = '共 ' + fmtNum(total) + ' 词';

    host.innerHTML = books.map(function (b, i) {
      var units = (b.units || []).map(function (u) {
        return '<div class="st-unit">' +
          '<span>' + esc(u.name) + '<span class="st-unit-count">' + u.total + ' 词</span></span>' +
          '<span class="st-book-track"><span class="st-book-fill" style="width:' + u.rate + '%"></span></span>' +
          '<span class="st-book-rate">' + u.rate + '%</span></div>';
      }).join('');
      return '<div class="st-book' + (i === 0 ? ' open' : '') + '" data-idx="' + i + '">' +
        '<div class="st-book-head">' +
          '<span class="st-book-name"><i>▸</i>' + esc(b.name) +
            '<span class="st-unit-count">' + b.total + ' 词 · 已学 ' + b.learned + ' · 掌握 ' + b.mastered + '</span></span>' +
          '<span class="st-book-track"><span class="st-book-fill" style="width:' + b.rate + '%"></span></span>' +
          '<span class="st-book-rate">' + b.rate + '%</span>' +
        '</div>' +
        '<div class="st-units">' + units + '</div></div>';
    }).join('');
  }

  // ---------------- 排行榜 ----------------
  function loadLeaderboard() {
    return api('/api/leaderboard?metric=' + state.lbMetric + '&range=' + state.lbRange + '&tz=' + TZ)
      .then(function (d) {
        if (d.guest) return;
        renderLeaderboard(d);
      });
  }

  function renderLeaderboard(d) {
    var list = $('stLbList');
    var entries = d.entries || [];
    var unit = d.metric === 'words' ? '词' : (d.metric === 'streak' ? '天' : '词');
    var meId = state.me && (state.me.id || state.me.sub);

    if (entries.length < 3) {
      list.innerHTML = '<div class="st-empty">已有 ' + entries.length +
        ' 位同学开始学习。<br>去邀请同班同学一起背，榜单满 3 人后显示排名。</div>';
    } else {
      list.innerHTML = entries.map(function (e) {
        var cls = 'st-lb-row';
        if (e.rank === 1) cls += ' top1';
        else if (e.rank === 2) cls += ' top2';
        else if (e.rank === 3) cls += ' top3';
        if (meId && String(e.user_id) === String(meId)) cls += ' me';
        return '<div class="' + cls + '">' +
          '<span class="st-lb-rank">' + e.rank + '</span>' +
          '<span class="st-lb-name">' + esc(e.name) + '</span>' +
          '<span class="st-lb-score">' + fmtNum(e.score) + '<em>' + unit + '</em></span></div>';
      }).join('');
    }

    var mine = null;
    for (var i = 0; i < entries.length; i++) {
      if (meId && String(entries[i].user_id) === String(meId)) { mine = entries[i]; break; }
    }
    var rangeText = d.range === 'week' ? '本周' : '总榜';
    var metricText = d.metric === 'words' ? '学习词数' : (d.metric === 'streak' ? '连续天数' : '掌握总数');
    $('stLbNote').textContent = '榜单口径：' + rangeText + ' · ' + metricText +
      '；仅统计有效账号，游客不计入。' + (mine ? '　我：第 ' + mine.rank + ' 名（' + fmtNum(mine.score) + ' ' + unit + '）' : '');
  }

  // ---------------- 徽章 ----------------
  function loadBadges() {
    return api('/api/badges').then(function (d) {
      if (d.guest) return;
      renderBadges(d.unlocked || []);
    });
  }

  function renderBadges(unlocked) {
    var map = {};
    unlocked.forEach(function (u) { map[u.badge_id] = u.unlocked_at || ''; });
    var got = 0;
    var html = BADGES.map(function (g) {
      return '<div class="st-badge-group"><div class="st-badge-title">' + esc(g.group) + '</div>' +
        '<div class="st-badges">' + g.items.map(function (b) {
          var on = !!map[b.id];
          if (on) got++;
          var when = '';
          if (on && map[b.id]) when = '<span class="st-badge-time">' + esc(String(map[b.id]).slice(0, 10)) + '</span>';
          return '<span class="st-badge' + (on ? ' on' : '') + '" title="' +
            esc(on ? '已解锁' : '尚未解锁') + '">' +
            '<span class="st-badge-ico">' + b.ico + '</span>' + esc(b.label) + when + '</span>';
        }).join('') + '</div></div>';
    }).join('');
    $('stBadges').innerHTML = html;
    var totalBadges = BADGES.reduce(function (s, g) { return s + g.items.length; }, 0);
    $('stBadgeCount').textContent = '已解锁 ' + got + ' / ' + totalBadges;
  }

  // ---------------- 抽屉 ----------------
  function openDrawer(title, html) {
    $('stDrawerTitle').textContent = title;
    $('stDrawerBody').innerHTML = html;
    $('stMask').classList.add('show');
    $('stDrawer').classList.add('show');
  }
  function closeDrawer() {
    $('stMask').classList.remove('show');
    $('stDrawer').classList.remove('show');
  }

  // ---------------- 装配 ----------------
  function loadSummary() {
    return api('/api/study/summary?tz=' + TZ).then(function (d) {
      if (d.guest) {
        $('stLoading').hidden = true;
        $('stGuest').hidden = false;
        $('stBody').hidden = true;
        return false;
      }
      state.summary = d.summary;
      $('stLoading').hidden = true;
      $('stBody').hidden = false;
      renderHero(d.summary);
      renderKpis(d.summary);
      return true;
    });
  }

  function loadAll() {
    return Promise.all([
      loadCalendar(), loadTrend(), loadPos(), loadReview(), loadBooks(), loadBadges(), loadLeaderboard()
    ]);
  }

  function refreshAll() {
    loadSummary().then(function (ok) {
      if (ok) loadAll();
    }).catch(function (e) { showError('加载失败：' + e.message); });
  }

  function bind() {
    $('stCalPrev').onclick = function () {
      var y = parseInt(state.month.slice(0, 4), 10);
      var m = parseInt(state.month.slice(5, 7), 10) - 1;
      if (m < 1) { m = 12; y--; }
      state.month = y + '-' + pad(m);
      loadCalendar();
    };
    $('stCalNext').onclick = function () {
      var y = parseInt(state.month.slice(0, 4), 10);
      var m = parseInt(state.month.slice(5, 7), 10) + 1;
      if (m > 12) { m = 1; y++; }
      state.month = y + '-' + pad(m);
      loadCalendar();
    };
    $('stCalGrid').onclick = function (ev) {
      var cell = ev.target.closest ? ev.target.closest('.st-cal-cell') : null;
      if (!cell || cell.classList.contains('empty') || cell.classList.contains('future')) return;
      var day = cell.getAttribute('data-day');
      if (day) openDay(day);
    };

    $('stTrendTabs').onclick = function (ev) {
      var btn = ev.target.closest ? ev.target.closest('button[data-days]') : null;
      if (!btn) return;
      var days = parseInt(btn.getAttribute('data-days'), 10);
      if (!days || days === state.trendDays) return;
      state.trendDays = days;
      Array.prototype.forEach.call($('stTrendTabs').children, function (b) {
        b.classList.toggle('active', b === btn);
      });
      loadTrend();
    };

    $('stPosList').onclick = function (ev) {
      var row = ev.target.closest ? ev.target.closest('.st-pos-row') : null;
      if (!row) return;
      openPosWords(row.getAttribute('data-pos'), row.querySelector('.st-pos-name').textContent);
    };

    $('stLbTabs').onclick = function (ev) {
      var btn = ev.target.closest ? ev.target.closest('button') : null;
      if (!btn) return;
      var metric = btn.getAttribute('data-metric');
      var range = btn.getAttribute('data-range');
      if (metric) {
        state.lbMetric = metric;
        Array.prototype.forEach.call($('stLbTabs').children, function (b) {
          if (b.hasAttribute('data-metric')) b.classList.toggle('active', b === btn);
        });
      } else if (range) {
        state.lbRange = (state.lbRange === 'all') ? 'week' : 'all';
        btn.classList.toggle('active', state.lbRange === 'all');
        btn.textContent = state.lbRange === 'all' ? '本周' : '总榜';
      }
      loadLeaderboard();
    };

    $('stBooks').onclick = function (ev) {
      var head = ev.target.closest ? ev.target.closest('.st-book-head') : null;
      if (!head) return;
      head.parentNode.classList.toggle('open');
    };

    $('stReviewBtn').onclick = function () {
      location.href = '/quiz?review=1&limit=20';
    };

    $('stMask').onclick = closeDrawer;
    $('stDrawerClose').onclick = closeDrawer;
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeDrawer();
    });

    // 切回页面时静默刷新（在另一个标签页答题后回来能看到最新数据）
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible' && state.summary) {
        loadSummary().then(function (ok) { if (ok) loadAll(); }).catch(function () {});
      }
    });
  }

  function boot() {
    state.month = todayStr().slice(0, 7);
    state.me = (typeof getCurrentUser === 'function') ? getCurrentUser() : null;
    var sub = $('stSub');
    if (sub) sub.textContent = '打卡、日历、掌握率与复习计划 · 数据按浏览器本地时区（UTC' +
      (TZ >= 0 ? '+' : '') + (TZ / 60) + '）统计';
    bind();
    loadSummary().then(function (ok) {
      if (!ok) return;
      return loadAll().then(function () {
        // 内容异步渲染，锚点跳转需等渲染完再滚（/me 的「我的徽章」用了 /study#badges）
        if (location.hash === '#badges') {
          var target = document.getElementById('badges');
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    }).catch(function (e) {
      $('stLoading').hidden = true;
      var msg = (e && e.message) || '未知错误';
      // 未登录 / 会话失效：直接引导登录，而不是把 401 文案摊在页面上
      if (!(typeof isAuthenticated === 'function' && isAuthenticated())) {
        showError('请先登录后查看学习统计');
        var tip = document.createElement('a');
        tip.href = '/login?redirect=' + encodeURIComponent(location.pathname);
        tip.textContent = '去登录 →';
        tip.style.cssText = 'display:inline-block;margin-left:8px;font-weight:600';
        $('stError').appendChild(tip);
        return;
      }
      showError('加载失败：' + msg);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
