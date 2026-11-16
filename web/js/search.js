// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

// search.html 前端逻辑（原生 JS，无依赖）+ SSO 鉴权
// 支持：模糊搜索（容忍拼写错误 / 字符顺序颠倒）、实时防抖、匹配高亮、非阻塞分片处理

// 鉴权拦截
if (typeof requireLogin === 'function') {
  requireLogin();
}
if (typeof onSessionExpired === 'function') {
  onSessionExpired(function() { if (typeof logout === 'function') logout(); });
}

// ===========================================================================
// 工具函数
// ===========================================================================
function escapeHtml(s) {
  if (!s) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// 简单防抖：等待 interval 毫秒无输入后再执行
function debounce(fn, interval) {
  var timer = null;
  return function() {
    var ctx = this, args = arguments;
    if (timer) clearTimeout(timer);
    timer = setTimeout(function() {
      timer = null;
      fn.apply(ctx, args);
    }, interval);
  };
}

// ===========================================================================
// 模糊搜索核心：Damerau–Levenshtein 距离（容忍相邻字符交换）+ 前缀/子串加权
// ===========================================================================
var Fuzzy = (function() {
  // 字符级别 Damerau–Levenshtein 距离（含 transposition）
  function editDistance(a, b, maxDist) {
    var alen = a.length, blen = b.length;
    if (Math.abs(alen - blen) > maxDist) return maxDist + 1;
    // 简化二维 DP，仅比较单词（长度通常 < 30）
    var dp = [];
    for (var i = 0; i <= alen; i++) {
      dp[i] = [];
      dp[i][0] = i;
    }
    for (var j = 0; j <= blen; j++) dp[0][j] = j;
    for (i = 1; i <= alen; i++) {
      for (j = 1; j <= blen; j++) {
        var cost = a[i - 1] === b[j - 1] ? 0 : 1;
        dp[i][j] = Math.min(
          dp[i - 1][j] + 1,       // 删除
          dp[i][j - 1] + 1,       // 插入
          dp[i - 1][j - 1] + cost // 替换
        );
        // 相邻交换
        if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
          dp[i][j] = Math.min(dp[i][j], dp[i - 2][j - 2] + 1);
        }
      }
    }
    return dp[alen][blen];
  }

  // 子串出现的位置（用于高亮）。返回索引数组，无则 null。
  function findOccurrences(haystack, needle) {
    if (!needle) return null;
    var lower = haystack.toLowerCase();
    var n = needle.toLowerCase();
    var idx = lower.indexOf(n);
    if (idx >= 0) return [[idx, idx + n.length]];
    // 子串不在，尝试每个字符逐个匹配（容忍乱序字符）
    var positions = [];
    var hi = 0, ni = 0;
    while (hi < lower.length && ni < n.length) {
      if (lower[hi] === n[ni]) { positions.push([hi, hi + 1]); ni++; }
      hi++;
    }
    if (ni === n.length) return positions;
    return null;
  }

  // 打分：越小越匹配。返回 null 表示不匹配。
  // 策略：
  //  1. 精确包含 → 得分 0
  //  2. 前缀匹配 → 高优先
  //  3. Damerau-Levenshtein 距离 ≤ 容差 → 得分 = 距离（含交换惩罚）
  //  4. 乱序子序列 → 低分匹配
  function score(word, query) {
    if (!query) return 0;
    var w = word.toLowerCase();
    var q = query.toLowerCase();
    if (w === q) return 0;
    if (w.indexOf(q) === 0) return 1;                    // 前缀
    if (w.indexOf(q) > 0) return 2;                      // 子串
    // 距离匹配（容忍拼写错误 / 交换），容差随查询长度增长
    var tolerance = Math.max(1, Math.floor(q.length / 3));
    var dist = editDistance(w, q, tolerance);
    if (dist <= tolerance) return 10 + dist;
    // 乱序子序列（字符顺序颠倒时，顺序子序列匹配）
    var occ = findOccurrences(w, q);
    if (occ) return 20 + q.length;                       // 乱序子序列
    return -1;                                           // 不匹配
  }

  // 对一整行数据（单词+释义）综合打分。
  // 支持多关键词：拆分后混合中英文关键词，策略为：
  //  - 中文关键词（硬条件）：要求全部命中释义，否则整条不匹配
  //  - 英文关键词（软加分）：不命中不否决；命中单词或释义可加分
  //  - 词性缩写（n. / v. / adj. / adv. / prep. / conj. / art. / pron. / num. / interj.）
  //    自动识别为噪音丢弃，避免污染搜索
  function rowScore(item, query) {
    // 拆分关键词（兼容半角/全角空格、顿号、逗号、分号、斜杠）
    // 第一阶段：先把词性缩写（n. v. adj. adv. ...）当成独立分隔符去除
    var stripped = query
      // 去除单词性缩写及其后的可选空格
      .replace(/\b(n|v|vi|vt|adj|adv|prep|conj|art|pron|num|interj|aux|pl|past|pp|ing|int|tr)\.\s*/gi, ' ')
      // 第二步：按常规分隔符再切分
      .split(/[\s\u3000、，,;；/]+/)
      .map(function(x) { return x.trim(); })
      .filter(function(x) { return x; });

    if (!stripped.length) return null;

    // 过滤纯词性缩写 token（防止第一步后还残留）
    var qs = stripped.filter(function(q) {
      return !/^(n|v|vi|vt|adj|adv|prep|conj|art|pron|num|interj|aux|pl|past|ing)\.$/i.test(q);
    });
    if (!qs.length) return null;

    var word = (item[1] || '').toLowerCase();
    var meaning = (item[2] || '').toLowerCase();
    var scoreTotal = 0;

    var chinese = [];
    var english = [];
    qs.forEach(function(q) {
      if (/[\u4e00-\u9fff]/.test(q)) chinese.push(q);
      else english.push(q);
    });

    // ---- 中文关键词：硬条件，所有必须命中释义----
    if (chinese.length) {
      var allHit = chinese.every(function(c) {
        return meaning.indexOf(c.toLowerCase()) >= 0;
      });
      if (!allHit) return null;
      scoreTotal = 15 - chinese.length;            // 命中越多越靠前
    }

    // ---- 英文关键词：软加分，单个对单词模糊或释义包含任一即可----
    var anyEnHit = false;
    for (var i = 0; i < english.length; i++) {
      var q = english[i].toLowerCase();
      var s = score(item[1], english[i]);
      if (s >= 0) {
        scoreTotal += 5;                           // 命中单词加分
        anyEnHit = true;
      } else if (meaning.indexOf(q) >= 0) {
        scoreTotal += 3;                           // 命中释义加分
        anyEnHit = true;
      }
    }

    // 如果只有英文关键词且没有任何一个命中，整条不匹配
    if (!chinese.length && english.length && !anyEnHit) return null;

    // 中文关键词命中（含混合），字段标为 meaning；纯英文命中则标 word
    return { score: scoreTotal, field: chinese.length ? 'meaning' : 'word', occ: null };
  }

  return { score: score, rowScore: rowScore, findOccurrences: findOccurrences };
})();

// ===========================================================================
// 数据缓存 + 分片搜索（不阻塞主线程）
// ===========================================================================
var WordStore = (function() {
  var data = null;       // [[id, word, meaning], ...]
  var loaded = false;

  // 加载全量单词（首次调用，之后缓存）
  function load() {
    if (loaded) return Promise.resolve(data);
    var fetcher = typeof authFetch === 'function' ? authFetch : fetch;
    return fetcher('/api/words/all')
      .then(function(res) { return res.json(); })
      .then(function(json) {
        data = json.words || [];
        loaded = true;
        return data;
      });
  }

  // 分片模糊搜索：每片处理 chunkSize 条，用 setTimeout 让出主线程，避免卡顿。
  // onResult(results, done)：results 为已收集结果，done 表示是否结束。
  // 返回值：取消函数（同步返回）。原实现把取消函数写在 Promise 内部返回，
  // 调用方拿到的是 Promise，导致旧搜索永远无法中止、结果互相覆盖。
  function search(query, opts, onResult) {
    var limit = (opts && opts.limit) || 200;
    var chunkSize = (opts && opts.chunkSize) || 2000;
    var q = (query || '').trim();
    var token = { cancelled: false };
    var cancel = function() { token.cancelled = true; };
    if (!q) {
      onResult([], true);
      return cancel;
    }
    load().then(function(all) {
      var results = [];
      var index = 0;

      function processChunk() {
        if (token.cancelled) return;
        var end = Math.min(index + chunkSize, all.length);
        for (var i = index; i < end; i++) {
          var r = Fuzzy.rowScore(all[i], q);
          if (r) {
            results.push({ idx: all[i][0], word: all[i][1], meaning: all[i][2],
                           score: r.score, field: r.field });
            if (results.length >= limit) {
              onResult(results, true);
              return;
            }
          }
        }
        index = end;
        if (index >= all.length) {
          onResult(results, true);
        } else {
          // 让出主线程，下一片继续
          onResult(results, false);
          setTimeout(processChunk, 0);
        }
      }
      processChunk();
    }).catch(function() {
      if (!token.cancelled) onResult([], true);
    });
    return cancel;
  }

  return { load: load, search: search };
})();

// ===========================================================================
// 渲染
// ===========================================================================
function renderLoading() {
  document.getElementById('searchResults').innerHTML =
    '<div class="search-loading"><div class="spinner"></div><span>正在搜索...</span></div>';
}

function renderEmpty(isFirst) {
  if (isFirst) {
    document.getElementById('searchResults').innerHTML =
      '<div class="search-empty">' +
      '<svg class="empty-icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M8 11h6"/></svg>' +
      '<div class="empty-text">输入关键词开始搜索</div>' +
      '<div class="empty-sub">模糊模式支持拼写错误与字符顺序颠倒，匹配文本高亮</div></div>';
  } else {
    document.getElementById('searchResults').innerHTML =
      '<div class="search-empty">' +
      '<svg class="empty-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>' +
      '<div class="empty-text">未找到匹配结果</div>' +
      '<div class="empty-sub">请尝试放宽关键词或切换匹配模式</div></div>';
  }
}

// 高亮匹配文本：对 word 字段按 Fuzzy 距离策略高亮最接近的子序列
function highlightWord(word, query) {
  var q = (query || '').trim();
  if (!q) return escapeHtml(word);
  var w = word.toLowerCase();
  var ql = q.toLowerCase();
  var idx = w.indexOf(ql);
  var ranges;
  if (idx >= 0) {
    ranges = [[idx, idx + ql.length]];
  } else {
    ranges = Fuzzy.findOccurrences(w, ql);
  }
  if (!ranges) return escapeHtml(word);
  // 合并重叠区间，按起始排序
  ranges.sort(function(a, b) { return a[0] - b[0]; });
  var merged = [ranges[0]];
  for (var i = 1; i < ranges.length; i++) {
    var last = merged[merged.length - 1];
    if (ranges[i][0] <= last[1]) {
      last[1] = Math.max(last[1], ranges[i][1]);
    } else {
      merged.push(ranges[i]);
    }
  }
  var out = '';
  var prev = 0;
  for (var j = 0; j < merged.length; j++) {
    out += escapeHtml(word.slice(prev, merged[j][0]));
    out += '<mark>' + escapeHtml(word.slice(merged[j][0], merged[j][1])) + '</mark>';
    prev = merged[j][1];
  }
  out += escapeHtml(word.slice(prev));
  return out;
}

// 在中文释义中高亮多个关键词
function highlightMeaning(meaning, query) {
  // 复用 rowScore 中的词性缩写过滤逻辑
  var qs = (query || '')
    .replace(/\b(n|v|vi|vt|adj|adv|prep|conj|art|pron|num|interj|aux|pl|past|pp|ing|int|tr)\.\s*/gi, ' ')
    .split(/[\s\u3000、，,;；/]+/)
    .map(function(x) { return x.trim(); })
    .filter(function(x) { return x; });
  if (!qs.length || !meaning) return escapeHtml(meaning);
  var text = meaning;
  var ranges = [];
  qs.forEach(function(q) {
    if (!/[\u4e00-\u9fff]/.test(q)) return;   // 只处理中文关键词
    var lower = text.toLowerCase();
    var ql = q.toLowerCase();
    var idx = lower.indexOf(ql);
    if (idx >= 0) ranges.push([idx, idx + ql.length]);
  });
  if (!ranges.length) return escapeHtml(meaning);
  ranges.sort(function(a, b) { return a[0] - b[0]; });
  var merged = [ranges[0]];
  for (var i = 1; i < ranges.length; i++) {
    var last = merged[merged.length - 1];
    if (ranges[i][0] <= last[1]) last[1] = Math.max(last[1], ranges[i][1]);
    else merged.push(ranges[i]);
  }
  var out = '', prev = 0;
  for (var j = 0; j < merged.length; j++) {
    out += escapeHtml(text.slice(prev, merged[j][0]));
    out += '<mark>' + escapeHtml(text.slice(merged[j][0], merged[j][1])) + '</mark>';
    prev = merged[j][1];
  }
  out += escapeHtml(text.slice(prev));
  return out;
}

function renderWordCard(r, idx) {
  var card = document.createElement('a');
  card.className = 'word-card';
  card.href = 'index.html#' + r.idx;
  var wordHtml = highlightWord(r.word, r.query);
  var meaningHtml = r.meaning
    ? highlightMeaning(r.meaning, r.query)
    : '';
  card.innerHTML =
    '<span class="idx">' + idx + '</span>' +
    '<div class="body">' +
      '<span class="word">' + wordHtml + '</span>' +
      (meaningHtml ? '<span class="mean">' + meaningHtml + '</span>' : '') +
    '</div>' +
    '<span class="arrow"><svg viewBox="0 0 24 24"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg></span>';
  return card;
}

function renderResults(results, query, box) {
  var html = '<div class="result-count">“<strong>' + escapeHtml(query) +
             '</strong>” 的模糊匹配结果（共 <strong>' + results.length +
             '</strong> 个）</div>';
  box.innerHTML = html;
  results.forEach(function(r, i) {
    r.query = query;
    box.appendChild(renderWordCard(r, i + 1));
  });
}

// ===========================================================================
// 后端精确搜索（contain / begin / end / meaning / 编号）
// ===========================================================================
async function doBackendSearch(pattern, search) {
  var box = document.getElementById('searchResults');
  renderLoading();
  try {
    var fetcher = typeof authFetch === 'function' ? authFetch : fetch;
    var res = await fetcher('/api/search?pattern=' + pattern + '&search=' + encodeURIComponent(search));
    var data = await res.json();
    if (data.type === 'empty' || data.type === 'none') { renderEmpty(false); return; }
    if (data.type === 'id') { location.href = 'index.html#' + data.id; return; }
    var html = '<div class="result-count">“<strong>' + escapeHtml(data.search) +
               '</strong>” 的搜索结果（共 <strong>' + data.results.length +
               '</strong> 个）</div>';
    box.innerHTML = html;
    data.results.forEach(function(r, i) {
      box.appendChild(renderWordCard({ idx: r.id, word: r.word, meaning: r.meaning }, i + 1));
    });
  } catch (err) {
    box.innerHTML =
      '<div class="search-empty">' +
      '<svg class="empty-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>' +
      '<div class="empty-text">搜索失败，请稍后重试</div>' +
      '<div class="empty-sub">' + (err.message || '网络异常') + '</div></div>';
  }
}

// ===========================================================================
// 搜索入口（按模式分发）
// ===========================================================================
var _currentCanceller = null;   // 用于中止旧的模糊搜索

function doSearch() {
  var form = document.getElementById('searchForm');
  var pattern = form.pattern.value;
  var search = form.search.value.trim();
  var box = document.getElementById('searchResults');

  if (!search) {
    // 中止进行中的模糊搜索
    if (_currentCanceller) { _currentCanceller(); _currentCanceller = null; }
    renderEmpty(true);
    return;
  }

  if (pattern === 'fuzzy') {
    // 中止上一个模糊搜索
    if (_currentCanceller) { _currentCanceller(); _currentCanceller = null; }
    renderLoading();
    _currentCanceller = WordStore.search(search, { limit: 200 }, function(results, done) {
      if (done) {
        if (!results.length) renderEmpty(false);
        else renderResults(results, search, box);
      }
      // done === false 时暂不渲染中间结果，避免闪烁（可选：可在此做渐进渲染）
    });
  } else {
    if (_currentCanceller) { _currentCanceller(); _currentCanceller = null; }
    doBackendSearch(pattern, search);
  }
}

// ===========================================================================
// 事件绑定：回车 + 实时搜索（防抖）
// ===========================================================================
var _debouncedSearch = debounce(doSearch, 300);

// 输入实时搜索（仅英文模糊模式实时；中文等用后端，稍作防抖）
document.getElementById('searchForm').search.addEventListener('input', function() {
  _debouncedSearch();
});

// 回车立即搜索
document.getElementById('searchForm').addEventListener('submit', function(e) {
  e.preventDefault();
  doSearch();
});
