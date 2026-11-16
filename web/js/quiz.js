// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

// ===========================================================================
// 单词测试页（独立页面）
// 模式：拼写测试 / 选择释义 / 听写练习
// 背诵模式：单元背诵（按单元顺序逐个背诵，支持单元选择与进度追踪）
// 数据来源：收藏夹 API（GET /api/collections + /api/collections/words?cid=X）
// 进度存储：localStorage（与单词浏览页共享，key=word3500_learn_progress）
// ===========================================================================
(function(){
  'use strict';

  var WORDS_KEY = 'word3500_learn_words';      // [{id, word, pron, meaning, book, unit}] 快照
  var PROG_KEY  = 'word3500_learn_progress';   // {wordId: {seen, correct, master}}
  var META_KEY  = 'word3500_learn_meta';       // {cid, name}
  var MODE_KEY  = 'word3500_quiz_mode';        // 上次用的模式：spell | choice | dictate

  var words = [];
  var progress = {};
  var mode = 'spell';        // 'spell' | 'choice' | 'dictate'
  var unitMode = true;       // 是否单元背诵模式；默认开启，带 cid 从收藏夹进入时在 init 中关闭
  var activeUnit = null;     // 当前背诵单元 {book, unit, words:[]}
  var wordIndexInUnit = 0;   // 当前单元内单词下标
  var quizQueue = [];
  var quizPos = 0;
  var quizStats = { answered: 0, correct: 0 };
  var quizAnswered = false;
  var selectedChoice = null;
  // 当前题的选项顺序缓存：同一道题重绘（切模式 / 答完揭示答案）不得重新洗牌
  var choiceCache = { wid: null, opts: null };

  var el = {};
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function fetch_(url, opts) {
    opts = opts || {};
    var fetcher = typeof authFetch === 'function' ? authFetch : fetch;
    return fetcher(url, opts);
  }
  // 取当前 Access Token。auth.js 导出的名字是 getAccessToken，
  // 原代码调用了不存在的 getToken() → ReferenceError 被 try/catch 吞掉，
  // 导致单元背诵断点的保存与恢复实际从未生效。
  function getToken() {
    if (typeof getAccessToken === 'function') return getAccessToken();
    try {
      var st = JSON.parse(localStorage.getItem('word3500_auth') || 'null');
      return st ? st.access_token : null;
    } catch (e) { return null; }
  }
  // 带 token 的请求头（authFetch 自己也会加，这里保留以兼容旧调用）
  function authHeaders(extra) {
    var h = extra || {};
    var t = getToken();
    if (t) h['X-Auth-Token'] = t;
    return h;
  }
  function getQueryParam(name) {
    return new URLSearchParams(location.search).get(name);
  }

  // ---- 进度读写（本地 + 服务器同步）----
  function loadProgressLocal() {
    try { progress = JSON.parse(localStorage.getItem(PROG_KEY) || '{}'); }
    catch(e) { progress = {}; }
  }
  function saveProgressLocal() {
    localStorage.setItem(PROG_KEY, JSON.stringify(progress));
  }

  // 从服务器拉取进度并合并（服务器优先，覆盖本地）
  async function loadProgressServer() {
    try {
      var res = await fetch_('/api/learn/progress');
      var data = await res.json();
      var rows = data.progress || [];
      rows.forEach(function(r) {
        progress[r.word_id] = {
          seen: r.seen || 0,
          correct: r.correct || 0,
          master: !!(r.master)
        };
      });
      saveProgressLocal();
    } catch(e) {}
  }

  // ---- 学习事件上报（设计文档 §4.5 / §8 链路 A）----
  // 客户端只上报 (word_id, action, result, uid)；master / seen / correct 由服务端
  // 权威计算后回传，本地值仅用于即时展示（离线也能继续答题）。
  var _events = [];        // 待上报事件队列
  var _flushTimer = null;
  var _flushing = false;
  var reviewMode = false;  // ?review=1 时事件 action 记为 review

  function tzOffset() { return -new Date().getTimezoneOffset(); }
  function newEventUid() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }

  function queueEvent(wid, action, result) {
    _events.push({ word_id: Number(wid), action: action, result: result, uid: newEventUid() });
    if (_flushTimer) clearTimeout(_flushTimer);
    _flushTimer = setTimeout(flushEvents, 800);
  }

  function flushEvents() {
    if (_flushing || !_events.length) return;
    if (typeof authFetch !== 'function') return;
    var batch = _events.splice(0, 200);
    _flushing = true;
    authFetch('/api/study/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: batch, tz: tzOffset() })
    }).then(function(r) { return r.json(); }).then(function(d) {
      _flushing = false;
      if (!d || d.ok === false) throw new Error('rejected');
      if (d.progress) {
        Object.keys(d.progress).forEach(function(wid) {
          var s = d.progress[wid];
          progress[wid] = { seen: s.seen, correct: s.correct, master: !!s.master };
        });
        saveProgressLocal();
      }
      window.dispatchEvent(new CustomEvent('study:update', { detail: d }));
      if (d.just_checked_in) {
        toast('今日已打卡 · 连续 ' + (d.summary ? d.summary.streak : 1) + ' 天');
      }
    }).catch(function() {
      _flushing = false;
      // 失败回队：uid 不变，服务端 (user_id, uid) 唯一索引保证重试幂等
      _events = batch.concat(_events);
      if (_events.length < 600) {
        if (_flushTimer) clearTimeout(_flushTimer);
        _flushTimer = setTimeout(flushEvents, 3000);
      }
    });
  }

  function toast(msg) {
    var t = document.getElementById('_quizToast');
    if (!t) {
      t = document.createElement('div');
      t.id = '_quizToast';
      t.style.cssText = 'position:fixed;left:50%;bottom:88px;transform:translateX(-50%);' +
        'background:rgba(22,163,74,.95);color:#fff;padding:10px 18px;border-radius:999px;' +
        'font-size:14px;z-index:9999;box-shadow:0 6px 24px rgba(0,0,0,.25);' +
        'opacity:0;transition:opacity .3s';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = '1';
    clearTimeout(t._timer);
    t._timer = setTimeout(function() { t.style.opacity = '0'; }, 2600);
  }

  function recordAnswer(w, correct) {
    var p = progress[w.id] || { seen: 0, correct: 0, master: false };
    p.seen = (p.seen || 0) + 1;
    if (correct) p.correct = (p.correct || 0) + 1;
    // 掌握判定：≥3 次答对且正确率 ≥80%；答错后正确率下降要能取消「已掌握」
    // （本地值仅用于即时展示，服务端会在回包中覆盖为权威结果）
    p.master = p.correct >= 3 && p.correct / p.seen >= 0.8;
    progress[w.id] = p;
    saveProgressLocal();
    queueEvent(w.id, reviewMode ? 'review' : 'quiz', correct ? 1 : 0);
  }

  // ---- 册/单元数据（来自主应用 /api/books，与首页完全一致）----
  var booksData = [];   // [{book, name, units:[{name, count}]}]
  var activeUnitWords = [];  // 当前选中单元的收藏夹单词

  // Fisher-Yates 洗牌：随机打乱数组顺序（返回新数组）
  function shuffle(arr) {
    var a = arr.slice();
    for (var i = a.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var t = a[i]; a[i] = a[j]; a[j] = t;
    }
    return a;
  }

  // ---- 加载收藏夹单词 ----
  async function loadCollection(cid) {
    el.quizArea.innerHTML = '<div class="empty-tip">加载中...</div>';
    try {
      // 获取收藏夹名
      var collRes = await fetch_('/api/collections');
      var collData = await collRes.json();
      var coll = (collData.collections || []).find(function(c) { return String(c.id) === String(cid); });
      var name = coll ? coll.name : '收藏夹 #' + cid;
      el.quizMeta.innerHTML = '当前词库：<strong>' + esc(name) + '</strong>' +
        (coll ? ' · ' + coll.cnt + ' 词' : '');

      var res = await fetch_('/api/collections/words?cid=' + encodeURIComponent(cid));
      var data = await res.json();
      words = (data.words || []).map(function(w) {
        return { id: w.id, word: w.word, pron: w.pronunciation, meaning: w.meaning,
                 book: w.book, unit: w.unit };
      });

      // 保存快照供浏览页同步
      localStorage.setItem(WORDS_KEY, JSON.stringify(words));
      localStorage.setItem(META_KEY, JSON.stringify({ cid: cid, name: name }));
    } catch(e) {
      words = [];
      el.quizMeta.innerHTML = '加载失败：' + esc(e.message || e);
    }
    if (!words.length) {
      el.quizArea.innerHTML =
        '<div class="empty-tip">该收藏夹没有单词。<br><a href="/favorites">去收藏夹挑一个词表</a></div>';
      return;
    }
    initUnitPanel();
    refreshQuiz();
  }

  // ---- 单元面板：从主应用 /api/books 拉取权威单元列表（册→单元二级联动）----
  async function initUnitPanel() {
    el.unitPanel.style.display = 'block';
    el.unitModeCheck.checked = unitMode;
    el.unitSelectWrap.style.display = unitMode ? 'block' : 'none';
    try {
      var res = await fetch_('/api/books');
      var data = await res.json();
      booksData = data.books || [];
    } catch(e) {
      booksData = [];
    }
    var bookSel = el.bookSelect;
    var sel = el.unitSelect;
    // 册下拉：与主应用首页一致，显示所有册（不按收藏夹过滤）
    bookSel.innerHTML = '';
    var usedBooks = {};
    words.forEach(function(w) {
      if (w.book != null && w.book !== '') usedBooks[w.book] = true;
    });
    booksData.forEach(function(b) {
      var opt = document.createElement('option');
      opt.value = b.book;
      opt.textContent = b.name;
      bookSel.appendChild(opt);
    });
    if (!bookSel.options.length) {
      // 词库没有册/单元信息（例如仅导入 3500.txt 扁平词表）：
      // 只能关闭单元背诵面板，绝不能清空答题区——原实现会把随机测试/
      // 引导文案一起覆盖，造成「整个测试页只剩一句错误提示」。
      bookSel.innerHTML = '<option value="">无册信息</option>';
      sel.innerHTML = '<option value="">--</option>';
      unitMode = false;
      el.unitModeCheck.checked = false;
      el.unitModeCheck.disabled = true;
      el.unitSelectWrap.style.display = 'none';
      el.unitPanel.style.display = 'none';
      return;
    }
    el.unitModeCheck.disabled = false;
    // 默认选中收藏夹有单词的第一个册（若无则选第一册）
    var firstUsed = null;
    for (var i = 0; i < bookSel.options.length; i++) {
      if (usedBooks[bookSel.options[i].value]) { firstUsed = bookSel.options[i].value; break; }
    }
    bookSel.value = firstUsed || bookSel.options[0].value;
    bookSel.onchange = function() { fillUnitsForBook(bookSel.value); };
    fillUnitsForBook(bookSel.value);
    if (unitMode) {
      // 优先恢复上次单元测试进度（断点续背）
      var restored = await tryRestoreUnitProgress();
      if (restored) return;
      startUnitRecite();
    }
  }

  // 填充所选册下的单元列表（与首页单元下拉一致：单元名 + 词数）
  function fillUnitsForBook(book) {
    var sel = el.unitSelect;
    sel.innerHTML = '';
    var b = booksData.find(function(x) { return String(x.book) === String(book); });
    var unitsOfBook = b ? b.units : [];
    if (!unitsOfBook.length) {
      sel.innerHTML = '<option value="">--</option>';
      return;
    }
    unitsOfBook.forEach(function(u, idx) {
      var opt = document.createElement('option');
      opt.value = u.name;
      opt.textContent = u.name + ' (' + u.count + ')';
      sel.appendChild(opt);
    });
    sel.onchange = function() { selectUnit(book, sel.value); };
    // 仅单元背诵模式开启时才自动选中第一个单元并出题；
    // 未开启时只填充选项，避免覆盖随机模式界面
    if (unitMode) {
      selectUnit(book, unitsOfBook[0].name);
    }
  }

  // 选中单元：从主应用 /api/words 拉取该单元全部单词（与首页单元筛选一致）
  async function selectUnit(book, unit, restore) {
    clearAutoJump();
    el.quizArea.innerHTML = '<div class="empty-tip">加载单元单词...</div>';
    var list = [];
    try {
      // 分页拉取该单元全部单词（每页 50）
      var page = 1, totalPages = 1;
      do {
        var res = await fetch_('/api/words?page=' + page + '&book=' + encodeURIComponent(book) + '&unit=' + encodeURIComponent(unit));
        var data = await res.json();
        (data.words || []).forEach(function(w) {
          list.push({ id: w.id, word: w.word, pron: w.pronunciation, meaning: w.meaning,
                      book: w.book, unit: w.unit });
        });
        totalPages = data.total_pages || 1;
        page++;
      } while (page <= totalPages);
    } catch(e) {
      list = [];
    }
    activeUnitWords = list;
    if (!list.length) {
      el.quizArea.innerHTML =
        '<div class="empty-tip">该单元暂无单词。<br>请尝试选择其他单元。</div>';
      return;
    }
    // 记录当前背诵单元（book + unit）供进度/完成提示
    activeUnit = { book: book, unit: unit, words: list };
    resetSessions();   // 换单元 → 三个模式各自的进度一起作废
    if (restore && Array.isArray(restore.queue) && restore.queue.length) {
      // 恢复断点：复用上次保存的洗牌顺序与位置（过滤已不存在单词，补齐新增单词）
      var existIds = list.map(function(w) { return w.id; });
      var q = restore.queue.filter(function(id) { return existIds.indexOf(id) >= 0; });
      existIds.forEach(function(id) { if (q.indexOf(id) < 0) q.push(id); });
      quizQueue = q;
      quizPos = Math.min(restore.quiz_pos || 0, Math.max(0, q.length - 1));
      wordIndexInUnit = restore.word_index || 0;
      quizStats = restore.stats || { answered: 0, correct: 0 };
    } else {
      // 随机顺序出题
      quizQueue = unitQueueIds();
      quizPos = 0;
      wordIndexInUnit = 0;
      quizStats = { answered: 0, correct: 0 };
    }
    quizAnswered = false;
    selectedChoice = null;
    clearChoiceCache();
    updateUnitProgress();
    renderQuiz();
    saveUnitProgress();
  }

  // ---- 单元测试进度持久化（断点续背）----
  function saveUnitProgress() {
    // 仅在单元背诵模式且有当前单元时保存；游客由服务端拒绝（403），前端忽略即可
    if (!unitMode || !activeUnit || !activeUnit.words.length) return;
    try {
      fetch_('/api/quiz/progress', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          book: activeUnit.book, unit: activeUnit.unit,
          queue: quizQueue, quiz_pos: quizPos,
          word_index: wordIndexInUnit, stats: quizStats
        })
      }).catch(function(){});
    } catch(e) {}
  }
  function clearUnitProgress() {
    try {
      fetch_('/api/quiz/progress', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ clear: true })
      }).catch(function(){});
    } catch(e) {}
  }
  async function tryRestoreUnitProgress() {
    if (!unitMode) return false;
    try {
      var r = await fetch_('/api/quiz/progress', { headers: authHeaders() });
      var d = await r.json();
      if (d && d.has && d.progress && d.progress.book) {
        var p = d.progress;
        var b = booksData.find(function(x) { return String(x.book) === String(p.book); });
        if (b && b.units && b.units.some(function(u) { return u.name === p.unit; })) {
          await selectUnit(p.book, p.unit, p);
          return true;
        }
      }
    } catch(e) {}
    return false;
  }

  // 册名与主应用（server.py api_books）完全一致
  var BOOK_NAMES = {
    1: "必修第一册", 2: "必修第二册", 3: "必修第三册",
    4: "选择性必修第一册", 5: "选择性必修第二册",
    6: "选择性必修第三册", 7: "选择性必修第四册"
  };
  function formatUnitName(u) {
    var bookName = BOOK_NAMES[u.book];
    return (bookName ? bookName + ' · ' : '') + u.unit;
  }

  function setUnitMode(on) {
    unitMode = on;
    el.unitModeCheck.checked = on;
    el.unitSelectWrap.style.display = on ? 'block' : 'none';
    if (on) {
      // 打开时若尚未加载书籍数据则初始化
      if (!booksData.length) {
        initUnitPanel();
      } else if (!activeUnit) {
        // 已有数据但未选单元，重新触发默认选择
        var bookSel = el.bookSelect;
        if (bookSel && bookSel.value) fillUnitsForBook(bookSel.value);
      } else {
        startUnitRecite();
      }
    } else {
      if (!words.length) {
        el.quizArea.innerHTML =
          '<div class="empty-tip">未选择词库。请先在单词浏览页选择收藏夹，再进入测试。</div>';
        el.quizMeta.innerHTML = '尚未选择词库';
      } else {
        refreshQuiz();
      }
    }
  }

  // 开始/继续单元背诵（用当前 activeUnit）
  function startUnitRecite() {
    if (!activeUnit || !activeUnit.words.length) return;
    clearAutoJump();
    resetSessions();
    // 随机顺序出题
    quizQueue = unitQueueIds();
    quizPos = 0;
    wordIndexInUnit = 0;
    quizStats = { answered: 0, correct: 0 };
    quizAnswered = false;
    selectedChoice = null;
    clearChoiceCache();
    updateUnitProgress();
    renderQuiz();
    saveUnitProgress();
  }

  function updateUnitProgress() {
    var u = activeUnit;
    if (!u) return;
    var total = u.words.length;
    var done = wordIndexInUnit + (quizAnswered ? 1 : 0);
    el.unitProgress.style.display = 'block';
    var barW = Math.round(done / Math.max(1, total) * 100);
    el.unitProgress.innerHTML =
      '单元进度：' + formatUnitName(u) + '<br>' +
      '<span class="up-fill" style="width:' + barW + '%;"></span>' +
      '<div style="font-size:11px;margin-top:2px;">本单元：第 ' + (wordIndexInUnit + 1) +
      ' / ' + u.words.length + ' 词</div>';
  }

  // ---- 出题队列 ----
  // 随机模式：从收藏夹里抽 10 个未掌握的词
  function randomQueueIds() {
    var notMastered = words.filter(function(w) { return !(progress[w.id] && progress[w.id].master); });
    var pool = notMastered.length ? notMastered : words;
    return shuffle(pool).slice(0, 10).map(function(w) { return w.id; });
  }
  // 单元模式：整个单元洗一遍
  function unitQueueIds() {
    if (!activeUnit || !activeUnit.words.length) return [];
    return shuffle(activeUnit.words).map(function(w) { return w.id; });
  }

  // ---- 出题（随机模式）----
  function refreshQuiz() {
    if (!words.length) return;
    clearAutoJump();
    resetSessions();
    quizQueue = randomQueueIds();
    quizPos = 0;
    quizStats = { answered: 0, correct: 0 };
    quizAnswered = false;
    selectedChoice = null;
    clearChoiceCache();
    renderQuiz();
  }

  // 从当前词源查找单词：单元模式优先用 activeUnit.words（主应用完整单元），
  // 随机模式用收藏夹 words
  function curQuizWord() {
    var id = quizQueue[quizPos];
    if (id == null) return null;
    var pool = (unitMode && activeUnit && activeUnit.words.length) ? activeUnit.words : words;
    for (var i = 0; i < pool.length; i++) if (pool[i].id === id) return pool[i];
    return null;
  }

  // ---- 单词流（模糊效果）----
  // 仅在"单元背诵模式开启 + 已选中具体单元"时显示，常规随机测试不显示
  function renderWordStream(currentWord) {
    if (!unitMode || !activeUnit || !activeUnit.words || !activeUnit.words.length) {
      el.wordStreamWrap.style.display = 'none';
      return;
    }
    var list = activeUnit.words;
    var shown = list.slice(0, 40);
    if (currentWord && shown.indexOf(currentWord) < 0) {
      shown = [currentWord].concat(shown);
      if (shown.length > 40) shown = shown.slice(0, 40);
    }
    el.wordStreamWrap.style.display = 'block';
    el.wordStream.innerHTML = '';
    shown.forEach(function(w) {
      var chip = document.createElement('span');
      var cls = 'ws-chip';
      if (currentWord && w.id === currentWord.id) {
        cls += ' current';                       // 当前测试词：蓝色高亮优先
      } else if (progress[w.id] && progress[w.id].master) {
        cls += ' master';                        // 已掌握：标绿
      }
      chip.className = cls;
      chip.draggable = false;          // 反作弊：模糊的 chip 不许被拖进输入框
      chip.textContent = w.word;
      el.wordStream.appendChild(chip);
    });
  }

  // ---- 渲染 ----
  function renderQuiz() {
    var w = curQuizWord();
    if (!w) {
      if (unitMode) renderUnitComplete();
      else renderResult();
      return;
    }
    var answered = quizAnswered;
    // 单词流始终渲染：单元模式显示当前单元，随机模式显示全部单词
    if (unitMode) {
      updateUnitProgress();
    }
    renderWordStream(w);
    var html;
    if (unitMode && activeUnit) {
      html = '<div class="quiz-question">' + formatUnitName(activeUnit) +
             ' · 第 ' + (wordIndexInUnit + 1) + ' / ' + activeUnit.words.length +
             ' 词 · 答对 ' + quizStats.correct + ' 题</div>';
    } else {
      html = '<div class="quiz-question">第 ' + (quizPos + 1) + ' / ' + quizQueue.length +
             ' 题 · 答对 ' + quizStats.correct + ' 题</div>';
    }
    if (mode === 'spell') {
      html += renderSpell(w, answered);
    } else if (mode === 'choice') {
      html += renderChoice(w, answered);
    } else {
      html += renderDictate(w, answered);
    }
    el.quizArea.innerHTML = html;
    if (!answered) bindEvents(w);
    else {
      var nb = $('quizNextBtn');
      if (nb) nb.onclick = nextQuiz;
    }
  }

  function renderSpell(w, answered) {
    var html =
      '<div class="quiz-word' + (answered ? ' reveal' : '') + '" draggable="false">' + esc(w.word) + '</div>' +
      '<div class="quiz-mean">' + (w.meaning ? esc(w.meaning) : '') + '</div>';
    if (!answered) {
      html +=
        '<div class="quiz-input-row">' +
          '<input type="text" id="answerInput" placeholder="输入单词拼写" autocomplete="off" autocapitalize="off" spellcheck="false">' +
          '<button class="quiz-submit" id="submitBtn">确认</button>' +
        '</div>' +
        '<div class="quiz-feedback" id="feedback"></div>';
    } else {
      var p = progress[w.id] || {};
      var acc = p.seen ? Math.round(p.correct / p.seen * 100) + '%' : '--';
      html += '<div class="quiz-feedback ok">累计正确率 ' + acc + '</div>' +
        '<div class="quiz-next-row"><button id="quizNextBtn">下一题</button></div>';
    }
    return html;
  }

  // 生成某道题的 4 个释义选项（正确答案 + 3 个干扰项，顺序随机）
  function buildChoiceOptions(w) {
    var opts = [w.meaning];
    var distractorPool = (unitMode && activeUnit && activeUnit.words.length) ? activeUnit.words : words;
    var distractor = shuffle(distractorPool);
    var added = 0;
    for (var k = 0; k < distractor.length && added < 3; k++) {
      var d = distractor[k];
      if (d.id !== w.id && d.meaning && d.meaning !== w.meaning && opts.indexOf(d.meaning) < 0) {
        opts.push(d.meaning); added++;
      }
    }
    return shuffle(opts);
  }
  function clearChoiceCache() { choiceCache = { wid: null, opts: null }; }

  function renderChoice(w, answered) {
    // 同一道题的选项顺序固定：切模式、答完揭晓都复用首次生成的结果，
    // 否则每次重绘选项都会重新洗牌，用户刚要点的位置就变了。
    var opts;
    if (choiceCache.wid === w.id && choiceCache.opts && choiceCache.opts.length) {
      opts = choiceCache.opts;
    } else {
      opts = buildChoiceOptions(w);
      choiceCache = { wid: w.id, opts: opts };
    }
    var html =
      '<div class="quiz-word' + (answered ? ' reveal' : '') + '" draggable="false">' + esc(w.word) + '</div>' +
      '<div class="quiz-mean">' + (w.pron ? esc(w.pron) : '') + '</div>' +
      '<div class="quiz-options">';
    opts.forEach(function(o) {
      var cls = '';
      if (answered) {
        if (o === w.meaning) cls = 'correct';
        else if (o === selectedChoice) cls = 'wrong';
      }
      html += '<button class="quiz-option ' + cls + '" data-choice="' + esc(o) + '"' +
              (answered ? ' disabled' : '') + '>' + esc(o) + '</button>';
    });
    html += '</div>';
    if (answered) {
      html += '<div class="quiz-next-row"><button id="quizNextBtn">下一题</button></div>';
    }
    return html;
  }

  function renderDictate(w, answered) {
    // 听写：只展示读音按钮和释义，用户听写单词
    var html =
      '<div class="dictate-hint">点击播放读音，输入听到的单词</div>' +
      '<div class="quiz-sound-row">' +
        '<button class="quiz-sound" id="soundBtn">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>' +
          '播放读音</button>' +
      '</div>';
    if (answered) {
      html += '<div class="quiz-mean">' + esc(w.word) + ' · ' + esc(w.meaning) + '</div>' +
        '<div class="quiz-next-row"><button id="quizNextBtn">下一题</button></div>';
    } else {
      html +=
        '<div class="quiz-input-row">' +
          '<input type="text" id="answerInput" placeholder="输入听到的单词" autocomplete="off" autocapitalize="off" spellcheck="false">' +
          '<button class="quiz-submit" id="submitBtn">确认</button>' +
        '</div>' +
        '<div class="quiz-feedback" id="feedback"></div>';
    }
    return html;
  }

  // ---- 单元完成后的自动流转 ----
  // 旧实现停在完成页等用户点「进入下一单元」，一轮接一轮时每单元都要手动点一次。
  // 现在：倒计时自动进入下一单元；从「一起背」房间深链进来的，完成后回房间页
  // （房间约定的是同一份词表，继续跳下一单元会跑出房间范围）。
  var autoJumpTimer = null;
  var roomScoped = false;   // 是否从 /quiz?book=&unit=（房间）进入

  function clearAutoJump() {
    if (autoJumpTimer) { clearTimeout(autoJumpTimer); autoJumpTimer = null; }
  }

  // 下一站：同册下一单元 → 下一册第一单元 → 全部背完返回 null
  function nextUnitTarget(book, unit) {
    var idx = -1;
    for (var i = 0; i < booksData.length; i++) {
      if (String(booksData[i].book) === String(book)) { idx = i; break; }
    }
    if (idx < 0) return null;
    var units = booksData[idx].units || [];
    var ui = -1;
    for (var k = 0; k < units.length; k++) if (units[k].name === unit) { ui = k; break; }
    if (ui >= 0 && ui < units.length - 1) return { book: book, unit: units[ui + 1].name };
    for (var b = idx + 1; b < booksData.length; b++) {
      var nu = booksData[b].units || [];
      if (nu.length) return { book: booksData[b].book, unit: nu[0].name };
    }
    return null;
  }

  // 单元完成提示（4 秒后自动继续，可取消）
  function renderUnitComplete() {
    var u = activeUnit;
    if (!u) { renderResult(); return; }
    clearAutoJump();
    var pct = quizStats.answered ? Math.round(quizStats.correct / quizStats.answered * 100) : 0;
    var target = roomScoped ? null : nextUnitTarget(u.book, u.unit);
    var jumpLabel = roomScoped ? '返回房间' : (target ? '进入下一单元：' + target.unit : '');

    el.quizArea.innerHTML =
      '<div class="quiz-result">' +
        '<div class="score">' + quizStats.correct + ' / ' + quizStats.answered + '</div>' +
        '<div class="sub">正确率 ' + pct + '%</div>' +
        '<div class="sub" style="margin-top:8px;font-size:15px;font-weight:700;color:var(--primary);">' +
          '单元完成：' + formatUnitName(u) + ' ✔</div>' +
        (jumpLabel
          ? '<div class="sub" id="unitAutoHint" style="margin-top:10px">' + esc(jumpLabel) +
              ' · <b id="unitAutoSec">4</b> 秒后自动继续</div>' +
            '<div class="quiz-next-row">' +
              '<button id="quizNextNow">' + esc(jumpLabel) + '</button>' +
              '<button id="quizNextCancel" class="quiz-ghost">取消自动</button>' +
            '</div>'
          : '<div class="sub" style="margin-top:10px">全部单元已背完</div>' +
            '<div class="quiz-next-row">' +
              '<button id="unitAllDoneBtn">再来一轮</button>' +
              '<a class="quiz-ghost" href="/study" style="text-decoration:none;display:inline-block;line-height:1.6">查看学习统计</a>' +
            '</div>') +
      '</div>';

    function go() {
      clearAutoJump();
      if (roomScoped) { location.href = '/together'; return; }
      if (target) selectUnit(u.book, target.unit);
    }

    var now = $('quizNextNow');
    if (now) now.onclick = go;
    var cancel = $('quizNextCancel');
    if (cancel) cancel.onclick = function () {
      clearAutoJump();
      var h = $('unitAutoHint');
      if (h) h.textContent = '已取消自动跳转，点上方按钮继续';
      cancel.disabled = true;
    };
    var allDone = $('unitAllDoneBtn');
    if (allDone) allDone.onclick = function () { startUnitRecite(); };

    if (jumpLabel) {
      var left = 4;
      (function tick() {
        autoJumpTimer = setTimeout(function () {
          left--;
          var sec = $('unitAutoSec');
          if (!sec) { autoJumpTimer = null; return; }   // 已切走/已取消
          if (left <= 0) { go(); return; }
          sec.textContent = left;
          tick();
        }, 1000);
      })();
    }
  }

  // ---- 交互 ----
  // 清洗朗读文本：去动词变位括号、等号注释、斜杠等特殊字符
  function cleanForTTS(w) {
    if (!w) return '';
    var s = String(w)
      .replace(/\([^)]*\)/g, '')
      .replace(/［[^］]*］/g, '')
      .split('=')[0];
    if (s.indexOf('/') >= 0) s = s.split('/')[0];
    return s.trim().replace(/\s+/g, ' ').replace(/\*+/g, '').replace(/\s+\.$/, '');
  }
  // 使用有道 TTS 在线音频播放（与主站朗读一致，兼容性好）
  // 跟随首页的英/美音设置（localStorage.accent: gb|us）
  function accentType() { return localStorage.getItem('accent') === 'us' ? 1 : 0; }
  function youdaoURL(word) {
    return 'https://dict.youdao.com/dictvoice?type=' + accentType() + '&audio=' + encodeURIComponent(cleanForTTS(word));
  }
  function playAudioOnce(url) {
    return new Promise(function(resolve) {
      var done = false, timer = null, a = null;
      function fin(ok) {
        if (done) return;
        done = true;
        if (timer) clearTimeout(timer);
        if (a) { try { a.pause(); } catch (e) {} }
        resolve(ok);
      }
      try {
        a = new Audio(url);
        a.addEventListener('ended', function() { fin(true); });
        a.addEventListener('error', function() { fin(false); });
        // 网络卡住时 'ended'/'error' 都可能不触发，导致朗读链永久挂起
        timer = setTimeout(function() { fin(false); }, 12000);
        a.play().then(function() {}, function() { fin(false); });
      } catch(e) { fin(false); }
    });
  }
  // 逐词兜底：整句失败时按单词逐个朗读
  function speakFallback(phrase) {
    var words = cleanForTTS(phrase).split(' ').filter(function(x) { return x; });
    var i = 0;
    return new Promise(function(resolve) {
      function next() {
        if (i >= words.length) { resolve(true); return; }
        playAudioOnce('https://dict.youdao.com/dictvoice?type=' + accentType() + '&audio=' + encodeURIComponent(words[i]))
          .then(function() { i++; next(); });
      }
      next();
    });
  }
  function speak(text) {
    return playAudioOnce(youdaoURL(text)).then(function(ok) {
      if (!ok) return speakFallback(text);
      return true;
    });
  }

  // ---- 反作弊：输入框不接受任何"投喂" ----
  // 被模糊的单词只是视觉上糊掉，DOM 里仍是明文：把它拖进输入框或复制粘贴进去，
  // 点确认就等于直接给答案。这里从输入端把拖拽 / 粘贴 / 中键粘贴全部掐掉。
  function hintNoInject(msg) {
    var f = $('feedback');
    if (!f) return;
    f.className = 'quiz-feedback bad';
    f.textContent = msg;
  }
  function blockInject(input) {
    ['paste', 'drop'].forEach(function(t) {
      input.addEventListener(t, function(e) {
        e.preventDefault();
        e.stopPropagation();
        hintNoInject(t === 'paste' ? '不能粘贴答案，请手动输入' : '不能拖拽答案，请手动输入');
      });
    });
    // 不 preventDefault 的话光标不会变成"可放置"，但默认行为仍要单独拦
    ['dragover', 'dragenter'].forEach(function(t) {
      input.addEventListener(t, function(e) { e.preventDefault(); });
    });
    // 兜底：Linux 中键粘贴、拖放插入等不一定触发 paste/drop
    input.addEventListener('beforeinput', function(e) {
      var it = e.inputType || '';
      if (it.indexOf('Paste') >= 0 || it.indexOf('Drop') >= 0) {
        e.preventDefault();
        hintNoInject('不能粘贴或拖拽答案，请手动输入');
      }
    });
  }

  function bindEvents(w) {
    // 听写模式：绑定读音按钮
    var sb = $('soundBtn');
    var submit = $('submitBtn');
    var input = $('answerInput');

    if (sb) {
      sb.onclick = function() {
        speak(w.word);
      };
    }

    if (input) {
      // 单词保持模糊（CSS 默认），输入框仅管理焦点便于作答
      input.focus();
      blockInject(input);
      // 用户再次输入时清除提示（空提交提示 / 反作弊提示）
      input.addEventListener('input', function() {
        var f = $('feedback');
        if (f && (f.textContent === '请先输入答案再提交' || f.textContent.slice(0, 2) === '不能')) {
          f.className = 'quiz-feedback';
          f.textContent = '';
        }
      });
      input.onkeydown = function(e) {
        if (e.key === 'Enter') submit && submit.click();
      };
    }

    if (mode === 'choice') {
      var opts = document.querySelectorAll('.quiz-option:not([disabled])');
      opts.forEach(function(btn) {
        btn.onclick = function() {
          if (quizAnswered) return;
          selectedChoice = btn.getAttribute('data-choice');
          var correct = selectedChoice === w.meaning;
          quizAnswered = true;
          recordAnswer(w, correct);
          quizStats.answered++;
          if (correct) quizStats.correct++;
          renderQuiz();
        };
      });
      return;
    }

    if (submit) submit.onclick = function() {
      if (quizAnswered) return;
      var raw = (input.value || '').trim();
      var val = raw.toLowerCase();
      // 未填写答案就提交：仅提示，不判题、不禁用、不跳题、保持模糊状态
      if (!val) {
        var emptyFb = $('feedback');
        if (emptyFb) {
          emptyFb.className = 'quiz-feedback bad';
          emptyFb.textContent = '请先输入答案再提交';
        }
        input.focus();
        return;
      }
      var correct = val === w.word.toLowerCase();
      quizAnswered = true;
      recordAnswer(w, correct);
      quizStats.answered++;
      if (correct) quizStats.correct++;
      // 答题完成：给当前单词添加 reveal 类，取消模糊展示正确答案
      var qw = $('quizArea').querySelector('.quiz-word');
      if (qw) qw.classList.add('reveal');
      var fb = $('feedback');
      if (correct) {
        fb.className = 'quiz-feedback ok';
        fb.textContent = '回答正确！';
        input.classList.add('correct');
      } else {
        fb.className = 'quiz-feedback bad';
        fb.textContent = '错误，正确答案：' + w.word;
        input.classList.add('wrong');
      }
      input.disabled = true;
      submit.disabled = true;
      // 追加下一题按钮
      var next = document.createElement('div');
      next.className = 'quiz-next-row';
      next.innerHTML = '<button id="quizNextBtn">下一题</button>';
      submit.parentNode.parentNode.appendChild(next);
      var nb = $('quizNextBtn');
      if (nb) nb.onclick = nextQuiz;
    };
  }

  function nextQuiz() {
    clearAutoJump();
    quizPos++;
    quizAnswered = false;
    selectedChoice = null;
    clearChoiceCache();   // 换题 → 选项重新洗牌
    if (unitMode && activeUnit) {
      wordIndexInUnit++;
      if (wordIndexInUnit >= activeUnit.words.length) {
        // 本单元完成：清除断点进度（下次进入该单元重新从头背诵）
        clearUnitProgress();
        renderUnitComplete();
      } else {
        renderQuiz();
        saveUnitProgress();
      }
    } else if (quizPos >= quizQueue.length) {
      renderResult();
    } else {
      renderQuiz();
    }
  }

  // 同册内下一个单元的逻辑已合并进 nextUnitTarget（含跨册回退），此处不再保留第二份实现

  function renderResult() {
    var pct = quizStats.answered ? Math.round(quizStats.correct / quizStats.answered * 100) : 0;
    var grade = pct >= 90 ? '太棒了！' : (pct >= 70 ? '不错，继续加油！' : '再接再厉！');
    // 完成态给两条明确出路：继续练 / 回来源页。复习队列从学习页进来，就不该回收藏夹。
    var backHref = reviewMode ? '/study' : '/favorites';
    var backText = reviewMode ? '返回学习' : '返回收藏夹';
    el.quizArea.innerHTML =
      '<div class="quiz-result">' +
        '<div class="score">' + quizStats.correct + ' / ' + quizStats.answered + '</div>' +
        '<div class="sub">正确率 ' + pct + '% · ' + grade + '</div>' +
        '<div class="quiz-next-row">' +
          '<button id="quizRestartBtn">再来一轮</button>' +
          '<a class="quiz-ghost" href="' + backHref + '" style="text-decoration:none">' + backText + '</a>' +
        '</div>' +
      '</div>';
    var rb = $('quizRestartBtn');
    if (rb) rb.onclick = refreshQuiz;
  }

  // ---- 模式切换 ----
  // 三个模式各自独立一份练习（队列 / 位置 / 统计 / 选项顺序），切模式 = 切到那一份练习。
  // 好处：刚在拼写模式看过的词，切到选择释义不会再原样问一遍；切回来又能接着上次的位置。
  // 只有词池里只剩一个词时，三个模式才必然共用同一个词。
  var sessions = { spell: null, choice: null, dictate: null };

  function snapshotSession() {
    sessions[mode] = {
      queue: quizQueue, pos: quizPos, stats: quizStats,
      answered: quizAnswered, selected: selectedChoice,
      wordIdx: wordIndexInUnit, cache: choiceCache
    };
  }
  function restoreSession(m) {
    var s = sessions[m];
    if (!s || !Array.isArray(s.queue) || !s.queue.length) return false;
    quizQueue = s.queue;
    quizPos = s.pos;
    quizStats = s.stats;
    quizAnswered = s.answered;
    selectedChoice = s.selected;
    wordIndexInUnit = s.wordIdx;
    choiceCache = s.cache || { wid: null, opts: null };
    return true;
  }
  function resetSessions() { sessions = { spell: null, choice: null, dictate: null }; }

  // 为当前模式新开一份队列；avoid 里是其他模式正在显示的词，尽量避开，
  // 让三个模式不会同时问同一个词（词池只剩一个时避无可避，那就只能重复）
  function beginModeSession(avoid) {
    var q;
    if (unitMode && activeUnit && activeUnit.words.length) q = unitQueueIds();
    else if (words.length) q = randomQueueIds();
    else return false;
    if (!q.length) return false;
    if (q.length > 1) {
      for (var i = 0; i < q.length; i++) {
        if (!avoid[q[i]]) {
          if (i > 0) { var t = q[0]; q[0] = q[i]; q[i] = t; }
          break;
        }
      }
    }
    quizQueue = q;
    quizPos = 0;
    quizStats = { answered: 0, correct: 0 };
    quizAnswered = false;
    selectedChoice = null;
    wordIndexInUnit = 0;
    clearChoiceCache();
    return true;
  }

  function setMode(m) {
    if (m === mode) return;                  // 重复点当前标签：不动状态
    clearAutoJump();                         // 完成页的倒计时不跨模式残留
    snapshotSession();
    // 其他模式当前挂着的词，新开的这份练习要避开
    var avoid = {};
    Object.keys(sessions).forEach(function(k) {
      var s = sessions[k];
      if (!s || !s.queue) return;
      var id = s.queue[s.pos];
      if (id != null) avoid[id] = 1;
    });
    mode = m;
    try { localStorage.setItem(MODE_KEY, m); } catch (e) {}
    paintModeTabs();
    if (restoreSession(m)) { renderQuiz(); saveUnitProgress(); return; }
    if (!beginModeSession(avoid)) return;    // 无词源：保持原画面（提示文案别被覆盖）
    renderQuiz();
    saveUnitProgress();                      // 断点跟随当前模式，刷新后回到同一模式
  }

  // 同步标签高亮与卡片 data-mode（CSS 用它限定「拼写模式才模糊」）
  function paintModeTabs() {
    var tabs = { spell: $('modeSpell'), choice: $('modeChoice'), dictate: $('modeDictate') };
    Object.keys(tabs).forEach(function(k) {
      if (tabs[k]) tabs[k].classList.toggle('active', k === mode);
    });
    syncModeAttr();
  }

  // 把当前模式写到卡片上，供 CSS 限定「拼写模式才模糊」
  function syncModeAttr() {
    var card = $('quizCard');
    if (card) card.setAttribute('data-mode', mode);
  }

  // ---- 初始化 ----
  // ---- 复习模式：从服务端待复习队列取词（?review=1）----
  async function loadReviewQueue(limit) {
    el.quizMeta.innerHTML = '复习队列 · 加载中…';
    try {
      var res = await fetch_('/api/study/review/queue?limit=' + (limit || 20) + '&tz=' + tzOffset());
      var data = await res.json();
      if (data && data.guest) {
        el.quizMeta.innerHTML = '复习队列';
        el.quizArea.innerHTML =
          '<div class="empty-tip">游客模式不保存学习记录，无法生成复习队列。<br>' +
          '<a href="/register" style="display:inline-block;margin-top:10px;color:var(--primary);font-size:13px;">注册后开始积累</a></div>';
        return;
      }
      words = (data.words || []).map(function(w) {
        return { id: w.id, word: w.word, pron: w.pronunciation, meaning: w.meaning,
                 book: w.book, unit: w.unit };
      });
      if (!words.length) {
        el.quizMeta.innerHTML = '复习队列';
        el.quizArea.innerHTML =
          '<div class="empty-tip">当前没有到期需要复习的单词 🎉<br>' +
          '<a href="/study" style="display:inline-block;margin-top:10px;color:var(--primary);font-size:13px;">返回学习页</a></div>';
        return;
      }
      el.quizMeta.innerHTML = '复习队列：<strong>' + words.length + ' 词</strong> · 到期未掌握';
      quizQueue = shuffle(words);
      quizPos = 0;
      quizStats = { answered: 0, correct: 0 };
      quizAnswered = false;
      refreshQuiz();
    } catch (e) {
      el.quizMeta.innerHTML = '复习队列';
      el.quizArea.innerHTML = '<div class="empty-tip">加载失败，请稍后重试</div>';
    }
  }

  function init() {
    el.quizMeta = $('quizMeta');
    el.quizArea = $('quizArea');
    el.quizCard = $('quizCard');
    el.unitPanel = $('unitPanel');
    el.unitModeCheck = $('unitModeCheck');
    el.unitSelectWrap = $('unitSelectWrap');
    el.bookSelect = $('bookSelect');
    el.unitSelect = $('unitSelect');
    el.unitProgress = $('unitProgress');
    el.wordStreamWrap = $('wordStreamWrap');
    el.wordStream = $('wordStream');

    // 用户栏
    var ub = $('userBar');
    if (ub && window.__auth_user) {
      ub.style.display = 'inline-flex';
      ub.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>' +
        '<span>' + esc(window.__auth_user.display_name || window.__auth_user.username || '') + '</span>';
    }

    $('modeSpell').onclick = function() { setMode('spell'); };
    $('modeChoice').onclick = function() { setMode('choice'); };
    $('modeDictate').onclick = function() { setMode('dictate'); };
    // 沿用上次的模式：刷新后断点（服务端只存一份）才对得上当前模式
    try {
      var savedMode = localStorage.getItem(MODE_KEY);
      if (savedMode === 'choice' || savedMode === 'dictate') mode = savedMode;
    } catch (e) {}
    paintModeTabs();

    // 单元背诵模式开关
    el.unitModeCheck.addEventListener('change', function() {
      setUnitMode(el.unitModeCheck.checked);
    });

    // 反作弊（源端）：被模糊的单词 / 单词流 chip 不允许被拖走。
    // CSS 的 user-select:none 只挡选中，挡不住浏览器把元素当作可拖拽文本；
    // 一旦拖进输入框就是白送答案，所以在 dragstart 直接取消。
    document.addEventListener('dragstart', function(e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var w = t.closest('.quiz-word');
      // 已揭晓（答完）的单词允许复制，未揭晓的一律不许拖
      if (w && !w.classList.contains('reveal')) { e.preventDefault(); return; }
      if (t.closest('.word-stream')) e.preventDefault();   // 单词流始终模糊
    });
    // 反作弊（目标端兜底）：把任何东西拖到答题区 / 单词流上都不接受
    document.addEventListener('drop', function(e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t.closest('#quizArea, #wordStreamWrap')) {
        e.preventDefault();
        if (t.id === 'answerInput') hintNoInject('不能拖拽答案，请手动输入');
      }
    });

    // 先读本地进度，再异步从服务器拉取覆盖
    loadProgressLocal();
    loadProgressServer();

    // 从 URL 读取 cid，无则回退到浏览页保存的快照
    var cid = getQueryParam('cid');
    var reviewParam = getQueryParam('review');
    if (reviewParam === '1' || reviewParam === 'true') {
      // 复习模式：按服务端到期队列自检，事件 action 记为 review
      reviewMode = true;
      unitMode = false;
      if (el.unitModeCheck) el.unitModeCheck.checked = false;
      if (el.unitSelectWrap) el.unitSelectWrap.style.display = 'none';
      loadReviewQueue(parseInt(getQueryParam('limit') || '20', 10));
      return;
    }

    // 从「一起背」房间进入：按房间约定的册 / 单元直接开背同一份词表。
    // 先以 unitMode=false 初始化面板（只填下拉、不出题），再锁定房间的册/单元，
    // 避免 initUnitPanel 的默认选中与断点续背逻辑覆盖房间词表。
    var roomBook = getQueryParam('book');
    var roomUnit = getQueryParam('unit');
    if (roomBook && /^\d+$/.test(roomBook)) {
      roomScoped = true;   // 完成后回房间页，不自动跳到下一个单元
      unitMode = false;
      initUnitPanel().then(function () {
        if (!el.bookSelect || !el.bookSelect.options.length) return;
        el.bookSelect.value = roomBook;
        fillUnitsForBook(roomBook);
        unitMode = true;
        el.unitModeCheck.checked = true;
        el.unitSelectWrap.style.display = 'block';
        var targetUnit = '';
        if (roomUnit && el.unitSelect) {
          el.unitSelect.value = roomUnit;   // 单元不存在时回落为整册
          targetUnit = el.unitSelect.value || '';
        }
        selectUnit(roomBook, targetUnit);   // 空 unit → /api/words 不加单元过滤，取整册
      });
      return;
    }

    if (cid) {
      // 从收藏夹进入：默认关闭单元背诵，直接随机背诵收藏夹单词
      unitMode = false;
      el.unitModeCheck.checked = false;
      el.unitSelectWrap.style.display = 'none';
      loadCollection(cid);
    } else {
      // 尝试读取浏览页快照
      try {
        words = JSON.parse(localStorage.getItem(WORDS_KEY) || '[]');
      } catch(e) { words = []; }
      try {
        var meta = JSON.parse(localStorage.getItem(META_KEY) || 'null');
        if (meta) {
          el.quizMeta.innerHTML = '当前词库：<strong>' + esc(meta.name) + '</strong> · ' + words.length + ' 词（本地快照）';
        }
      } catch(e) {}
      if (words.length) {
        // 单元模式自己会出题：先 refreshQuiz 再被 selectUnit 覆盖，会闪一下随机题
        // 并把「当前词库」提示冲掉，所以只在随机模式下先出题。
        if (!unitMode) refreshQuiz();
        initUnitPanel().then(function () {
          // 词库没有分册信息时 initUnitPanel 会把 unitMode 关掉，这里兜底补上随机题
          if (!unitMode && !quizQueue.length) refreshQuiz();
        });
      } else {
        // 无 cid 且无本地快照：没有任何词源。单元背诵需要分册词库（book/unit），
        // 当前库若没有分册信息则只能走收藏夹路径，因此给出明确指引。
        el.quizMeta.innerHTML = '尚未选择词库';
        el.quizArea.innerHTML =
          '<div class="empty-tip">未选择单词来源。<br>' +
          '请先到「收藏夹」挑一个词表，再点「开始测试」。<br>' +
          '<a href="/favorites" style="display:inline-block;margin-top:10px;color:var(--primary);font-size:13px;">前往收藏夹</a></div>';
        initUnitPanel();
      }
    }
  }

  // 关闭/切页前尽量把未上报事件发出（失败也不阻塞卸载）
  window.addEventListener('pagehide', function() {
    clearAutoJump();   // 离开页面不要再触发自动跳转
    if (!_events.length) return;
    try {
      var batch = _events.slice(0, 200);
      _events = [];
      fetch('/api/study/events', {
        method: 'POST', keepalive: true,
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': getToken() || '' },
        body: JSON.stringify({ events: batch, tz: tzOffset() })
      }).catch(function() {});
    } catch (e) {}
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
