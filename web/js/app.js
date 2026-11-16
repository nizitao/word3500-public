// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

// word3500 前端逻辑（原生 JS，无依赖）+ SSO 鉴权集成

const PAGE_SIZE = 50;
const state = { page: 1, totalPages: 1, beginId: null };
const PAGE_RATE_LIMIT = 500; // 毫秒，每秒最多2次
var _lastPageLoad = 0;
var _pageInputTimer = null;
let reading = false;

// 鉴权拦截
if (typeof requireLogin === 'function') { requireLogin(); }
if (typeof onSessionExpired === 'function') {
  // 会话过期必须回登录页：原实现 quiet 登出后停在原页面，用户看到满屏 401 却不知道要重新登录
  onSessionExpired(function() { logout(); });
}

// 游客检测（统一走 auth.js 的 isGuestUser，兼容历史 guest 与新版 guest_xxx 临时账户）
var _isGuest = (typeof isGuestUser === 'function') ? isGuestUser() : (function() {
  var u = typeof getCurrentUser === 'function' ? getCurrentUser() : null;
  return u && u.username === 'guest';
})();

// 游客隐藏「标记」列（表头 + 单元格布局保持一致，否则列错位导致朗读读到错误的列）
if (_isGuest) {
  var _starHeader = document.getElementById('starHeader');
  if (_starHeader) _starHeader.style.display = 'none';
}

// 册/单元筛选状态
var _bookFilter = '';
var _unitFilter = '';
var _booksData = [];

// ---- 内联 SVG 图标 ----
const ICON_STAR   = '<svg class="icon star-ic" viewBox="0 0 24 24"><path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.8 6.1 20.5l1.2-6.5L2.5 9.4l6.6-.9z"/></svg>';
const ICON_ADD    = '<svg class="icon" viewBox="0 0 24 24"><path d="M12 5v14"/><path d="M5 12h14"/></svg>';
const ICON_CHECK  = '<svg class="icon" viewBox="0 0 24 24" style="width:16px;height:16px;stroke:#2f6fed;fill:none"><path d="M20 6 9 17l-5-5"/></svg>';
const ICON_VOLUME = '<svg class="icon" viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a9 9 0 0 1 0 14"/></svg>';
const ICON_PLAY   = '<svg class="icon" viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z"/></svg>';
const ICON_PAUSE  = '<svg class="icon" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>';

const getAccent  = () => localStorage.getItem('accent')  || 'gb';
const setAccent  = v  => localStorage.setItem('accent', v);
const getControl = () => localStorage.getItem('control') || 'once';
const setControl = v  => localStorage.setItem('control', v);
const getBegin   = () => parseInt(localStorage.getItem('beginId') || '0', 10) || null;
const setBegin   = id => localStorage.setItem('beginId', id);
const accentType = () => (getAccent() === 'gb' ? 0 : 1);

// ---------------------------------------------------------------------------
// 页面加载与渲染
// ---------------------------------------------------------------------------
async function loadPage(page) {
  state.page = page;
  var fetcher = typeof authFetch === 'function' ? authFetch : fetch;
  var url = '/api/words?page=' + page;
  if (_bookFilter) url += '&book=' + _bookFilter;
  if (_unitFilter) url += '&unit=' + encodeURIComponent(_unitFilter);
  try {
    var res = await fetcher(url);
    const data = await res.json();
    if (data && data.words) {
      state.totalPages = data.total_pages;
      renderTable(data);
      document.getElementById('pageInfo').textContent = '第 ' + data.page + ' / ' + data.total_pages + ' 页（共 ' + data.total + ' 词）';
      document.getElementById('prevBtn').disabled = data.page <= 1;
      document.getElementById('nextBtn').disabled = data.page >= data.total_pages;
    } else {
      renderTable(data);
      document.getElementById('pageInfo').textContent = '加载失败，请刷新重试';
    }
  } catch(_) {
    renderTable(null);
    document.getElementById('pageInfo').textContent = '连接失败，请检查网络';
  }
  window.scrollTo(0, 0);
}

// 速率限制：用户触发的翻页，每秒最多2次
function _throttledLoadPage(page) {
  var now = Date.now();
  if (now - _lastPageLoad < PAGE_RATE_LIMIT) return;
  _lastPageLoad = now;
  loadPage(page);
}

// 跳转到指定页码（带边框校验），手动输入不走限速
function _goToPage(inputEl) {
  if (!inputEl) inputEl = document.getElementById('pageInput');
  var val = inputEl.value.trim();
  inputEl.classList.remove('error');
  if (!val || !/^\d+$/.test(val)) {
    inputEl.classList.add('error');
    setTimeout(function() { inputEl.classList.remove('error'); }, 1500);
    return;
  }
  var p = parseInt(val, 10);
  if (p < 1 || p > state.totalPages) {
    inputEl.classList.add('error');
    inputEl.value = '';
    inputEl.placeholder = '1~' + state.totalPages;
    setTimeout(function() { inputEl.classList.remove('error'); inputEl.placeholder = '页码'; }, 1500);
    return;
  }
  inputEl.value = '';
  loadPage(p);
}

function renderTable(data) {
  const body = document.getElementById('wordBody');
  body.innerHTML = '';
  if (!data || !data.words) {
    body.innerHTML = '<tr><td colspan="' + (_isGuest ? 6 : 7) + '" style="text-align:center;color:#94a3b8;padding:20px">无法加载单词列表，请刷新重试</td></tr>';
    return;
  }
  data.words.forEach(function(w) {
    var tr = document.createElement('tr');
    tr.id = 'row-' + w.id;
    tr.dataset.id = w.id;
    var tdId = document.createElement('td'); tdId.className = 'c-id'; tdId.textContent = w.id;
    var tdStar;
    if (!_isGuest) {
      tdStar = document.createElement('td');
      tdStar.className = 'c-star';
      var starBtn = document.createElement('button');
      starBtn.className = 'starBtn' + (w.star ? ' on' : '');
      starBtn.innerHTML = ICON_STAR;
      starBtn.title = w.star ? '已收藏' : '收藏';
      starBtn.dataset.wid = w.id;
      starBtn.onclick = function() { openCollectionPicker(w.id, this, w.collection_ids || []); };
      tdStar.appendChild(starBtn);
    }
    var tdWord = document.createElement('td'); tdWord.className = 'c-word'; tdWord.textContent = w.word;
    var tdPron = document.createElement('td'); tdPron.className = 'c-pron'; tdPron.textContent = w.pronunciation || '';
    var tdBegin = document.createElement('td');
    var rb = document.createElement('input'); rb.type = 'radio'; rb.name = 'begin'; rb.value = w.id;
    if (getBegin() === w.id) rb.checked = true;
    rb.onchange = function() { setBegin(w.id); };
    tdBegin.appendChild(rb);
    var tdMean = document.createElement('td'); tdMean.className = 'c-mean'; tdMean.textContent = w.meaning || '';
    var tdRead = document.createElement('td');
    var readBtn = document.createElement('button'); readBtn.className = 'readBtn'; readBtn.innerHTML = ICON_VOLUME; readBtn.title = '朗读';
    readBtn.onclick = function() { readWord(w.word); };
    tdRead.appendChild(readBtn);
    var cells = [tdId];
    if (!_isGuest) cells.push(tdStar);
    cells.push(tdWord, tdPron, tdBegin, tdMean, tdRead);
    tr.append.apply(tr, cells);
    body.appendChild(tr);
  });
}

// ---- 收藏夹弹窗 ----
var _collectionsCache = [];
var _activePopup = null;

function closePopup() {
  if (_activePopup) { _activePopup.remove(); _activePopup = null; }
  window.removeEventListener('scroll', onPageScroll, true);
}

// 仅在页面主体滚动时关闭弹窗；弹窗内部列表（collPopupList）滚动不关闭
function onPageScroll(e) {
  if (_activePopup && _activePopup.contains(e.target)) return;
  closePopup();
}
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function ensureCollections() {
  var f = typeof authFetch === 'function' ? authFetch : fetch;
  try {
    var r = await f('/api/collections'), d = await r.json();
    _collectionsCache = d.collections || [];
    if (!_collectionsCache.length) {
      await f('/api/collections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'create',name:'默认收藏夹'})});
      var r2 = await f('/api/collections'), d2 = await r2.json();
      _collectionsCache = d2.collections || [];
    }
  } catch(_) { _collectionsCache = []; }
}

function openCollectionPicker(wid, btnEl, cids) {
  closePopup();
  var p = document.createElement('div'); p.className = 'coll-popup';
  p.innerHTML = '<div class="coll-popup-hd">收藏到</div><div class="coll-popup-list" id="collPopupList"></div><div class="coll-popup-foot"><input type="text" id="collNewName" placeholder="新建收藏夹..." maxlength="30"><button id="collNewBtn" title="创建">' + ICON_ADD + '</button></div>';
  _activePopup = p; var r = btnEl.getBoundingClientRect();
  // 弹窗定位：不超过视口底部（给列表滚动留出空间），手机上避免被挤出屏幕
  var topLimit = Math.max(8, window.innerHeight - 320);
  p.style.cssText = 'position:fixed;top:' + Math.min(r.bottom + 4, topLimit) + 'px;left:' + Math.max(4, Math.min(r.left, window.innerWidth - 240)) + 'px';
  document.body.appendChild(p);
  window.addEventListener('scroll', onPageScroll, true);
  // 阻止弹窗内部滚动事件传播到 window，避免在手机上滚动列表时误关弹窗
  p.addEventListener('scroll', function(e) { e.stopPropagation(); }, true);
  p.addEventListener('wheel', function(e) { e.stopPropagation(); }, { passive: true });
  p.addEventListener('touchmove', function(e) { e.stopPropagation(); }, { passive: true });
  var listEl = document.getElementById('collPopupList');
  listEl.innerHTML = '<div style="padding:12px;text-align:center;color:#94a3b8;font-size:13px">加载中...</div>';
  ensureCollections().then(function() {
    function rl(ids) {
      listEl.innerHTML = '';
      _collectionsCache.forEach(function(c) {
        var ch = ids.indexOf(c.id) !== -1, it = document.createElement('div');
        it.className = 'coll-popup-item' + (ch ? ' on' : '');
        it.innerHTML = '<span class="coll-popup-cb">' + (ch ? ICON_CHECK : '') + '</span><span class="coll-popup-name">' + esc(c.name) + '</span><span class="coll-popup-cnt">' + c.cnt + '</span>';
        it.onclick = function() { var n = ch ? ids.filter(function(x) { return x !== c.id; }) : ids.concat([c.id]); toggleColl(wid, n); rl(n); };
        listEl.appendChild(it);
      });
    }
    rl(cids || []);
    document.getElementById('collNewBtn').onclick = function() { var n = document.getElementById('collNewName').value.trim(); if (n) createColl(n, wid, function(i) { rl(i); }); };
    document.getElementById('collNewName').onkeydown = function(e) { if (e.key === 'Enter') document.getElementById('collNewBtn').click(); };
  });
}

async function toggleColl(wid, cids) {
  var f = typeof authFetch === 'function' ? authFetch : fetch;
  try {
    var r = await f('/api/collections/toggle', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ wid: wid, cids: cids }) });
    if (!r.ok) return;
    var b = document.querySelector('.starBtn[data-wid="' + wid + '"]');
    if (b) { b.classList.toggle('on', cids.length > 0); b.title = cids.length > 0 ? cids.length + ' 个收藏夹' : '收藏'; }
  } catch(_) {}
}

async function createColl(name, wid, cb) {
  var f = typeof authFetch === 'function' ? authFetch : fetch;
  try {
    await f('/api/collections', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'create', name: name }) });
    document.getElementById('collNewName').value = '';
    var r = await f('/api/collections'), d = await r.json();
    _collectionsCache = d.collections || [];
    var c = _collectionsCache[_collectionsCache.length - 1];
    if (c && wid) {
      var ci = _activePopup ? _activePopup.querySelectorAll('.coll-popup-item.on') : [], oi = [];
      for (var x = 0; x < ci.length; x++) { var idx = Array.from(ci[x].parentNode.children).indexOf(ci[x]); if (_collectionsCache[idx]) oi.push(_collectionsCache[idx].id); }
      oi.push(c.id); await toggleColl(wid, oi); if (cb) cb(oi);
    } else { if (cb) cb([]); }
  } catch(_) {}
}

// ---------------------------------------------------------------------------
// 朗读
// ---------------------------------------------------------------------------
// 播放单个音频，返回 Promise：成功 true / 失败 false。
// 带超时兜底：网络卡住时原实现永不 resolve，导致自动朗读整条链静默停死。
var AUDIO_TIMEOUT_MS = 12000;
function playAudioOnce(url, timeoutMs) {
  return new Promise(function(resolve) {
    var done = false, timer = null, a = null;
    function finish(ok) {
      if (done) return;
      done = true;
      if (timer) clearTimeout(timer);
      resolve(ok);
    }
    try {
      a = new Audio(url);
      a.addEventListener('ended', function() { finish(true); });
      a.addEventListener('error', function() { finish(false); });
      timer = setTimeout(function() {
        try { a.pause(); } catch (e) {}
        finish(false);
      }, timeoutMs || AUDIO_TIMEOUT_MS);
      a.play().then(function() {}, function() { finish(false); });
    } catch (e) {
      finish(false);
    }
  });
}

function audioPlay(url) { return playAudioOnce(url); }
function youdaoURL(word) { return 'https://dict.youdao.com/dictvoice?type=' + accentType() + '&audio=' + encodeURIComponent(cleanForTTS(word)); }
function baiduURL(text) { var part = text.slice(text.lastIndexOf('.') + 1); return 'https://tts.baidu.com/text2audio?cuid=baiduid&lan=zh&ctp=1&pdt=311&tex=' + encodeURIComponent(part); }

// 逐词兜底朗读：短语整体失败时按单词逐个播放
function playWordFallback(phrase) {
  var words = cleanForTTS(phrase).split(' ').filter(function(x) { return x; });
  var i = 0;
  function next() {
    if (i >= words.length) return;
    playAudioOnce('https://dict.youdao.com/dictvoice?type=' + accentType() + '&audio=' + encodeURIComponent(words[i]))
      .then(function() { i++; next(); });
  }
  next();
}

function readWord(word) {
  // 优先整句有道朗读；失败（HTTP 404/500，如不认识的短语）时逐词兜底
  playAudioOnce(youdaoURL(word)).then(function(ok) {
    if (!ok) playWordFallback(word);
  });
}

// 清洗朗读文本：去掉动词变位括号、等号注释、斜杠等特殊字符，提取核心朗读内容
function cleanForTTS(word) {
  if (!word) return '';
  var w = String(word);
  // 去掉括号内容（动词变位等），如 "arise (arose, arisen)" -> "arise"
  w = w.replace(/\([^)]*\)/g, '').replace(/［[^］]*］/g, '');
  // 去掉 "= 注释" 部分，如 "bike = bicycle" -> "bike"
  w = w.split('=')[0];
  // 斜杠分隔时取第一部分，如 "absorbed in sth./sb." -> "absorbed in sth"
  if (w.indexOf('/') >= 0) w = w.split('/')[0];
  // 去掉首尾空格与多余符号
  w = w.trim().replace(/\s+/g, ' ');
  // 去掉残余的星号（生词标记）、句点等
  w = w.replace(/[*]/g, '').replace(/\s+\.$/, '');
  return w;
}

var getRows = function() { return Array.from(document.querySelectorAll('#wordBody tr')); };
var rowById = function(id) { return document.getElementById('row-' + id); };

function highlight(row) { getRows().forEach(function(r) { r.classList.remove('active'); }); if (row) row.classList.add('active'); }
function stopReading() { reading = false; document.getElementById('playBtn').innerHTML = ICON_PLAY + ' 朗读'; getRows().forEach(function(r) { r.classList.remove('active'); }); }

async function nextPageAndContinue() {
  if (state.page < state.totalPages) { await loadPage(state.page + 1); var rows = getRows(); if (rows.length) playRow(rows[0]); } else { stopReading(); }
}

async function playRow(rowEl) {
  if (!rowEl || !reading) { stopReading(); return; }
  // 用 class 取列：游客模式下「标记」列被移除，按 children[2]/[5] 取会整体错位
  var wordCell = rowEl.querySelector('.c-word');
  var meanCell = rowEl.querySelector('.c-mean');
  var word = wordCell ? wordCell.textContent : '';
  var meaning = meanCell ? meanCell.textContent : '';
  if (!word) { stopReading(); return; }
  highlight(rowEl);
  await audioPlay(youdaoURL(word));
  if (!reading) return;
  if (meaning) await audioPlay(baiduURL(meaning));
  if (!reading) return;
  var mode = getControl();
  var rows = getRows();
  var idx = rows.indexOf(rowEl);
  if (idx === rows.length - 1) {
    if (mode === 'loop') playRow(rows[0]);
    else if (mode === 'seq') nextPageAndContinue();
    else stopReading();
  } else {
    playRow(rows[idx + 1]);
  }
}

function listen() {
  var btn = document.getElementById('playBtn');
  if (reading) { stopReading(); return; }
  reading = true;
  btn.innerHTML = ICON_PAUSE + ' 停止';
  var startRow = getBegin() ? rowById(getBegin()) : null;
  if (!startRow) startRow = getRows()[0];
  if (startRow) playRow(startRow); else stopReading();
}

// ---------------------------------------------------------------------------
// 控件绑定
// ---------------------------------------------------------------------------
function bindControls() {
  document.getElementById('homeBtn').onclick = function() { _throttledLoadPage(1); };
  document.getElementById('prevBtn').onclick = function() { _throttledLoadPage(Math.max(1, state.page - 1)); };
  document.getElementById('nextBtn').onclick = function() { _throttledLoadPage(state.page + 1); };
  document.getElementById('topBtn').onclick = function() { window.scrollTo(0, 0); };
  document.querySelectorAll('input[name=accent]').forEach(function(r) { r.checked = r.value === getAccent(); r.onchange = function() { setAccent(r.value); }; });
  document.querySelectorAll('input[name=control]').forEach(function(r) { r.checked = r.value === getControl(); r.onchange = function() { setControl(r.value); }; });
  document.getElementById('playBtn').onclick = listen;
  document.getElementById('searchPageBtn').onclick = function() { location.href = 'search.html'; };
  // 页码跳转：仅在「回车」与「跳转按钮点击」时触发，避免输入框失焦(change)与按钮点击重复触发
  // （旧逻辑中 onchange + onclick 双重调用会在第二次用空值误报错误，且旧缓存版本无法正确跳转）
  var pageInput = document.getElementById('pageInput');
  var goPageBtn = document.getElementById('goPageBtn');
  if (pageInput) {
    pageInput.onkeypress = function(e) { if (e.key === 'Enter') _goToPage(pageInput); };
    pageInput.oninput = function() { this.classList.remove('error'); };
  }
  if (goPageBtn) goPageBtn.onclick = function() { _goToPage(pageInput); };
  // 册/单元筛选
  var bookSel = document.getElementById('bookSelect');
  var unitSel = document.getElementById('unitSelect');
  if (bookSel) {
    bookSel.onchange = function() {
      _bookFilter = this.value;
      _unitFilter = '';
      unitSel.innerHTML = '<option value="">全部</option>';
      unitSel.disabled = !_bookFilter;
      if (_bookFilter) {
        var found = _booksData.find(function(b) { return String(b.book) === _bookFilter; });
        if (found && found.units && found.units.length) {
          found.units.forEach(function(u) {
            var opt = document.createElement('option');
            opt.value = u.name; opt.textContent = u.name + ' (' + u.count + ')';
            unitSel.appendChild(opt);
          });
          unitSel.disabled = false;
          // 选择册后自动选中第一个单元，立即显示该单元具体内容而非"全部"
          unitSel.value = found.units[0].name;
          _unitFilter = found.units[0].name;
        }
      }
      loadPage(1);
    };
  }
  if (unitSel) {
    unitSel.onchange = function() {
      _unitFilter = this.value;
      loadPage(1);
    };
  }
}

// 加载册/单元列表
async function loadBooks() {
  var bookSel = document.getElementById('bookSelect');
  var unitSel = document.getElementById('unitSelect');
  if (!bookSel) return;
  var fetcher = typeof authFetch === 'function' ? authFetch : fetch;
  try {
    var res = await fetcher('/api/books');
    var data = await res.json();
    _booksData = data.books || [];
    _booksData.forEach(function(b) {
      var opt = document.createElement('option');
      opt.value = b.book; opt.textContent = b.name + ' (' + b.units.length + ' 单元)';
      bookSel.appendChild(opt);
    });
  } catch(_) {}
}

function start() {
  var m = location.hash.match(/^#(\d+)$/);
  if (m) {
    var id = parseInt(m[1], 10);
    var targetPage = Math.floor((id - 1) / PAGE_SIZE) + 1;
    loadPage(targetPage).then(function() { var row = rowById(id); if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' }); });
  } else {
    loadPage(1);
  }
  loadBooks();  // 异步加载册/单元列表
}

bindControls();
start();

// ---------------------------------------------------------------------------
// 控制栏水波纹涟漪反馈（现代触感）
// ---------------------------------------------------------------------------
(function() {
  var controlsBar = document.getElementById('controlsBar');
  if (!controlsBar) return;
  function spawnRipple(e) {
    var target = e.currentTarget;
    if (!target || target.disabled) return;
    var rect = target.getBoundingClientRect();
    var size = Math.max(rect.width, rect.height) * 1.2;
    var ink = document.createElement('span');
    ink.className = 'ripple-ink';
    ink.style.width = ink.style.height = size + 'px';
    ink.style.left = (e.clientX - rect.left - size / 2) + 'px';
    ink.style.top = (e.clientY - rect.top - size / 2) + 'px';
    target.appendChild(ink);
    setTimeout(function() { ink.remove(); }, 600);
  }
  controlsBar.querySelectorAll('button, .filter').forEach(function(btn) {
    btn.addEventListener('pointerdown', spawnRipple);
  });
})();

// 播放按钮状态动画：切换时图标脉冲
(function() {
  var btn = document.getElementById('playBtn');
  if (!btn) return;
  var observer = new MutationObserver(function() {
    btn.classList.remove('icon-pulse');
    void btn.offsetWidth;   // 重启动画
    btn.classList.add('icon-pulse');
  });
  observer.observe(btn, { childList: true });
})();

// 用户状态显示与登出
(function() {
  var userBar = document.getElementById('userBar');
  var userName = document.getElementById('userName');
  var logoutBtn = document.getElementById('logoutBtn');
  var adminLink = document.getElementById('adminLink');
  var reportLink = document.getElementById('reportLink');
  if (!userBar || !userName || !logoutBtn) return;
  var user = typeof getCurrentUser === 'function' ? getCurrentUser() : null;
  if (user) {
    userName.textContent = user.display_name || user.username || '用户';
    var isAdmin = user.role === 'admin';
    if (adminLink) adminLink.style.display = isAdmin ? 'inline' : 'none';
    if (reportLink) reportLink.style.display = isAdmin ? 'inline' : 'none';
  }
  if (!logoutBtn.onclick) {
    logoutBtn.onclick = function() { if (typeof logout === 'function' && confirm('确定要退出登录吗？')) { logout(); } };
  }
})();
