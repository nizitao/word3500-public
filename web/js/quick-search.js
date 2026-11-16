// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

/* 首页页内即时搜索
   - 原先工具栏的「搜索」按钮要跳到独立页 search.html，多一次整页加载；
     这里改成就地输入、就地出结果，清空关键词即恢复词表。
   - 搜索时把词表相关控件（分页、册/单元筛选、朗读）一起让位，
     避免留下「能点但点下去没意义」的按钮。
   - 样式随脚本自注入，理由同 nav.js：index.html 不必额外维护 CSS 引用。 */
(function () {
  var input = document.getElementById('quickSearchInput');
  var box = document.getElementById('searchResults');
  if (!input || !box) return;

  var DEBOUNCE_MS = 250;
  var SERVER_LIMIT = 200;   // 服务端 LIMIT 200，命中上限时要提示收窄关键词

  // 搜索态下应当隐藏的词表专属控件；#topBtn 保留（结果列表长了仍有用）
  var TABLE_ONLY = [
    '#wordTable', '.book-filter', '#homeBtn', '#prevBtn', '#pageInfo',
    '#nextBtn', '#pageInput', '#goPageBtn', '#playBtn', 'label.accent'
  ];

  var hideNodes = (function () {
    var list = [];
    TABLE_ONLY.forEach(function (sel) {
      var found = document.querySelectorAll(sel);
      for (var i = 0; i < found.length; i++) list.push(found[i]);
    });
    // 「一遍 / 循环 / 顺序」只作用于表格行朗读，没有标签可选中，按内部 radio 反查
    var labels = document.querySelectorAll('#controlsBar label');
    for (var j = 0; j < labels.length; j++) {
      if (labels[j].querySelector('input[name="control"]')) list.push(labels[j]);
    }
    return list;
  })();

  var searching = false;
  var composing = false;    // 中文输入法拼写过程中不触发搜索，否则会拿拼音片段去查
  var timer = null;
  var token = 0;            // 请求序号：只有最后一次关键词的响应才允许渲染

  // ------------------------------------------------------------------
  // 样式（亮/暗主题都只走 style.css 的变量，不写死颜色）
  // ------------------------------------------------------------------
  var style = document.createElement('style');
  style.textContent = [
    '.controls .quick-search{position:relative;z-index:2;display:inline-flex;align-items:center;gap:.4rem;',
    'min-height:44px;padding:0 .6rem;border:1px solid var(--border-default);border-radius:12px;',
    'background:color-mix(in srgb,var(--bg-input) 88%,transparent);cursor:text;',
    'transition:border-color .2s,box-shadow .2s,background .2s}',
    '.controls .quick-search:hover{border-color:color-mix(in srgb,var(--primary) 40%,var(--border-default))}',
    '.controls .quick-search:focus-within{border-color:var(--primary);box-shadow:0 0 0 3px var(--focus-ring)}',
    '.controls .quick-search .icon{width:1.05rem;height:1.05rem;stroke:var(--text-muted);flex:none}',
    '.controls .quick-search-input{width:11rem;min-width:0;border:0;outline:0;background:transparent;',
    'color:var(--text-primary);font-size:.9375rem;font-family:inherit;line-height:1.6;text-overflow:ellipsis}',
    '.controls .quick-search-input::placeholder{color:var(--text-muted)}',
    // style.css 在窄屏把 .controls>a 拉成 flex:1（那是给导航类链接的），这里收回成固定图标按钮
    '.controls #searchPageBtn{position:relative;z-index:2;display:inline-flex;align-items:center;justify-content:center;',
    'flex:0 0 auto;max-width:none;min-width:44px;min-height:44px;border:1px solid var(--border-default);border-radius:12px;',
    'background:color-mix(in srgb,var(--bg-btn-ghost) 85%,transparent);color:var(--text-secondary);',
    'text-decoration:none;transition:background .2s,border-color .2s,color .2s}',
    '.controls #searchPageBtn:hover{background:var(--bg-hover);color:var(--primary);',
    'border-color:color-mix(in srgb,var(--primary) 45%,var(--border-default))}',
    '.controls #searchPageBtn:focus-visible{outline:2px solid var(--primary);outline-offset:2px}',

    '#searchResults{margin:12px 0 16px;padding:0;background:transparent;border:0;color:var(--text-primary)}',
    '#searchResults:empty{display:none}',
    '.qr-hd{font-size:13px;color:var(--text-tertiary);margin-bottom:10px;word-break:break-word}',
    '.qr-hd strong{color:var(--text-primary)}',
    '.qr-hd .qr-more{color:var(--text-muted);margin-left:4px}',
    '#searchResults a.qr{display:flex;align-items:flex-start;gap:12px;margin:0 0 8px;padding:10px 12px;',
    'border:1px solid var(--border-lighter);border-radius:12px;background:var(--bg-card);',
    'text-decoration:none;color:inherit;font-size:inherit;transition:background .15s,border-color .15s}',
    '#searchResults a.qr:hover{background:var(--bg-hover);border-color:var(--border-default)}',
    '#searchResults a.qr:focus-visible{outline:2px solid var(--primary);outline-offset:2px}',
    '#searchResults .qr-id{flex:none;min-width:2.6rem;height:1.6rem;padding:0 .4rem;border-radius:8px;',
    'background:var(--bg-btn-ghost);color:var(--text-tertiary);font-size:12px;',
    'display:inline-flex;align-items:center;justify-content:center;font-variant-numeric:tabular-nums}',
    '#searchResults .qr-body{flex:1;min-width:0}',
    '#searchResults .qr-word{font-size:1.0625rem;font-weight:700;color:var(--text-primary);word-break:break-word}',
    '#searchResults .qr-pron{font-size:.8125rem;color:var(--text-muted);margin-left:.5rem}',
    '#searchResults .qr-mean{font-size:.875rem;color:var(--text-secondary);margin-top:3px;line-height:1.45;word-break:break-word}',
    '#searchResults .qr-note{padding:16px 4px;font-size:.875rem;color:var(--text-muted);text-align:center}',
    '#searchResults mark{background:#fff3bf;color:#a6791a;border-radius:3px;padding:0 1px}',
    '[data-theme="dark"] #searchResults mark{background:#78350f;color:#fde68a}',
    '@media (max-width:768px){.controls .quick-search-input{width:9rem}}',
    '@media (max-width:480px){.controls .quick-search{padding:0 .45rem}',
    '.controls .quick-search-input{width:8.5rem;font-size:.8125rem}}'
  ].join('');
  document.head.appendChild(style);

  // ------------------------------------------------------------------
  // 视图切换
  // ------------------------------------------------------------------
  function setTableChrome(visible) {
    for (var i = 0; i < hideNodes.length; i++) {
      hideNodes[i].style.display = visible ? '' : 'none';
    }
  }

  function enterSearch() {
    if (searching) return;
    searching = true;
    setTableChrome(false);
  }

  // 恢复表格视图：清空结果、作废进行中的请求
  function exitSearch() {
    if (timer) { clearTimeout(timer); timer = null; }
    token++;
    if (box.firstChild) box.innerHTML = '';
    if (!searching) return;
    searching = false;
    setTableChrome(true);
  }

  // ------------------------------------------------------------------
  // 渲染
  // ------------------------------------------------------------------
  // 关键词高亮用 DOM 拼装而不是 innerHTML：词库里的释义含 HTML 敏感字符时不会被当成标签
  function appendHighlighted(parent, text, kw) {
    text = text == null ? '' : String(text);
    kw = kw || '';
    if (!kw) { parent.appendChild(document.createTextNode(text)); return; }
    var lower = text.toLowerCase();
    var needle = kw.toLowerCase();
    if (!needle) { parent.appendChild(document.createTextNode(text)); return; }
    var from = 0;
    var idx = lower.indexOf(needle, from);
    while (idx >= 0) {
      if (idx > from) parent.appendChild(document.createTextNode(text.slice(from, idx)));
      var mark = document.createElement('mark');
      mark.textContent = text.slice(idx, idx + needle.length);
      parent.appendChild(mark);
      from = idx + needle.length;
      idx = lower.indexOf(needle, from);
    }
    if (from < text.length) parent.appendChild(document.createTextNode(text.slice(from)));
  }

  function buildCard(item, kw) {
    var a = document.createElement('a');
    a.className = 'qr';
    a.href = '/#' + item.id;

    var idEl = document.createElement('span');
    idEl.className = 'qr-id';
    idEl.textContent = item.id;

    var body = document.createElement('div');
    body.className = 'qr-body';

    var word = document.createElement('span');
    word.className = 'qr-word';
    appendHighlighted(word, item.word, kw);
    body.appendChild(word);

    if (item.pronunciation) {
      var pron = document.createElement('span');
      pron.className = 'qr-pron';
      pron.textContent = item.pronunciation;
      body.appendChild(pron);
    }
    if (item.meaning) {
      var mean = document.createElement('div');
      mean.className = 'qr-mean';
      appendHighlighted(mean, item.meaning, kw);
      body.appendChild(mean);
    }

    a.appendChild(idEl);
    a.appendChild(body);
    a.addEventListener('click', function (e) {
      // 允许 Ctrl/⌘/中键交给浏览器新开标签页
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      goWord(item.id);
    });
    return a;
  }

  function renderNote(text) {
    box.innerHTML = '';
    var div = document.createElement('div');
    div.className = 'qr-note';
    div.textContent = text;
    box.appendChild(div);
  }

  function renderLoading(kw) {
    renderNote('正在搜索「' + kw + '」…');
  }

  function renderResults(data, kw) {
    var list = data.results || [];
    if (!list.length) { renderNote('没有找到「' + kw + '」，试试更短的关键词'); return; }
    box.innerHTML = '';
    var hd = document.createElement('div');
    hd.className = 'qr-hd';
    hd.appendChild(document.createTextNode('「'));
    var q = document.createElement('strong');
    q.textContent = data.search || kw;
    hd.appendChild(q);
    hd.appendChild(document.createTextNode('」匹配到 ' + list.length + ' 个'));
    if (list.length >= SERVER_LIMIT) {
      var more = document.createElement('span');
      more.className = 'qr-more';
      more.textContent = '（只显示前 ' + SERVER_LIMIT + ' 个，可再补充关键词）';
      hd.appendChild(more);
    }
    box.appendChild(hd);
    for (var i = 0; i < list.length; i++) box.appendChild(buildCard(list[i], kw));
  }

  // 纯数字是「按编号定位」，卡片形式给出，回车则直接跳
  function renderIdHit(data, kw) {
    box.innerHTML = '';
    var card = buildCard({ id: data.id, word: '跳转到编号 ' + data.id, pronunciation: '', meaning: '第 ' + data.page + ' 页' }, '');
    box.appendChild(card);
  }

  // ------------------------------------------------------------------
  // 跳词：恢复词表并定位到该词所在页
  // ------------------------------------------------------------------
  // 只改 location.href 的 hash 不会重跑 app.js 的 start()（没有 hashchange 监听），
  // 所以这里复用 app.js 暴露的 loadPage/PAGE_SIZE 就地跳页；拿不到时退回整页跳转。
  function goWord(id) {
    if (timer) { clearTimeout(timer); timer = null; }
    input.value = '';
    exitSearch();
    if (typeof loadPage === 'function' && typeof PAGE_SIZE === 'number') {
      if (window.history && history.replaceState) history.replaceState(null, '', '/#' + id);
      loadPage(Math.floor((id - 1) / PAGE_SIZE) + 1).then(function () {
        var row = document.getElementById('row-' + id);
        if (!row) {
          // 有册/单元筛选时分页范围变了，该词可能不在这一页；整页重载会复位筛选，
          // 再由 app.js 的 start() 按 #<id> 重新定位
          location.reload();
          return;
        }
        row.classList.add('active');
        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
      return;
    }
    location.href = '/#' + id;
    location.reload();
  }

  // ------------------------------------------------------------------
  // 搜索
  // ------------------------------------------------------------------
  function runSearch(immediate) {
    var kw = input.value.trim();
    if (!kw) { exitSearch(); return; }
    enterSearch();

    // 纯数字：服务端只回编号与页码，交给点击/回车决定是否跳
    if (/^\d+$/.test(kw)) {
      var myId = ++token;
      var fetcher0 = typeof authFetch === 'function' ? authFetch : fetch;
      if (!box.firstChild) renderLoading(kw);
      fetcher0('/api/search?pattern=contain&search=' + encodeURIComponent(kw))
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (myId !== token) return;
          if (data.type === 'id') {
            if (immediate) { goWord(data.id); return; }
            renderIdHit(data, kw);
          } else {
            renderNote('编号 ' + kw + ' 不存在');
          }
        })
        .catch(function () {
          if (myId !== token) return;
          renderNote('搜索失败，请稍后重试');
        });
      return;
    }

    var my = ++token;
    var fetcher = typeof authFetch === 'function' ? authFetch : fetch;
    if (!box.firstChild) renderLoading(kw);
    fetcher('/api/search?pattern=contain&search=' + encodeURIComponent(kw))
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (my !== token) return;      // 已有更新的关键词，丢弃过期响应
        // 服务端有结果时回 type:"list"（旧文档写的是 "results"），两种都认
        if (data.results) { renderResults(data, kw); return; }
        if (data.type === 'empty') { exitSearch(); return; }
        renderNote('没有找到「' + kw + '」，中文可直接搜释义');
      })
      .catch(function () {
        if (my !== token) return;
        renderNote('搜索失败，请检查网络后重试');
      });
  }

  function schedule() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (!input.value.trim()) { exitSearch(); return; }
    timer = setTimeout(function () { timer = null; runSearch(false); }, DEBOUNCE_MS);
  }

  input.addEventListener('input', function () {
    if (composing) return;   // 拼音未上屏，等 compositionend
    schedule();
  });
  input.addEventListener('compositionstart', function () { composing = true; });
  input.addEventListener('compositionend', function () { composing = false; schedule(); });

  input.addEventListener('keydown', function (e) {
    if (e.isComposing) return;   // 输入法确认候选词时的 Enter/Esc 不算本页快捷键
    if (e.key === 'Enter') {
      e.preventDefault();
      if (timer) { clearTimeout(timer); timer = null; }
      runSearch(true);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      input.value = '';
      exitSearch();
    }
  });
})();
