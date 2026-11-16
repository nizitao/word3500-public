// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

// word3500 SSO 客户端认证模块（原生 JS，无依赖）
// 职责：Token 管理 · 自动刷新 · 会话超时 · 401 拦截 · fetch 包装

const AUTH_STORAGE_KEY = 'word3500_auth';
const REFRESH_LOCK_KEY = 'word3500_refresh_lock';
const SESSION_TIMEOUT = 30 * 60 * 1000;   // 30 分钟空闲超时
const REFRESH_BUFFER = 5 * 60 * 1000;      // 提前 5 分钟刷新

let _authState = null;         // 内存缓存
let _idleTimer = null;         // 空闲超时定时器
let _refreshTimer = null;      // 自动刷新定时器
let _onSessionExpired = null;  // 会话过期回调

// ---------------------------------------------------------------------------
// 状态管理
// ---------------------------------------------------------------------------
function getAuthState() {
  if (_authState) return _authState;
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY);
    _authState = raw ? JSON.parse(raw) : null;
  } catch {
    _authState = null;
  }
  return _authState;
}

function setAuthState(state) {
  _authState = state;
  if (state) {
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(state));
  } else {
    localStorage.removeItem(AUTH_STORAGE_KEY);
  }
}

function isAuthenticated() {
  const st = getAuthState();
  if (!st || !st.access_token) return false;
  // 检查过期
  if (st.expires_at && Date.now() > st.expires_at) return false;
  return true;
}

function getAccessToken() {
  const st = getAuthState();
  return st ? st.access_token : null;
}

function getCurrentUser() {
  const st = getAuthState();
  return st ? st.user : null;
}

// 统一游客判定：历史共享账户 guest 与新版临时账户 guest_xxx 都算游客
// 与服务端 auth.is_guest_username 保持同一口径
function isGuestName(username) {
  const name = String(username || '').trim().toLowerCase();
  return name === 'guest' || name.indexOf('guest_') === 0;
}

function isGuestUser() {
  const u = getCurrentUser();
  return !!u && isGuestName(u.username);
}

// 只允许站内相对路径，避免 ?redirect=javascript:... 或 //evil.com 造成的开放重定向/XSS
function safeRedirectTarget(raw) {
  if (!raw) return '/';
  if (raw.charAt(0) !== '/' || raw.charAt(1) === '/' || raw.charAt(1) === '\\') return '/';
  if (/[\u0000-\u001f]/.test(raw)) return '/';
  return raw;
}

// ---------------------------------------------------------------------------
// 登录
// ---------------------------------------------------------------------------
async function login(username, password, extra) {
  const body = { username, password };
  if (extra) Object.assign(body, extra);
  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.message || data.error || '登录失败');
  }
  // 存储状态（含过期时间戳）
  const state = {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    user: data.user,
    expires_at: Date.now() + (data.expires_in || 1800) * 1000,
    must_change_password: !!data.must_change_password,
  };
  setAuthState(state);
  _scheduleRefresh(data.expires_in || 1800);
  _resetIdleTimer();
  return data;
}

// ---------------------------------------------------------------------------
// 登出
// ---------------------------------------------------------------------------
async function logout(quiet) {
  const token = getAccessToken();
  if (token) {
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Auth-Token': token,
        },
      });
    } catch (_) { /* 网络错误静默 */ }
  }
  setAuthState(null);
  _clearTimers();
  if (!quiet) {
    const redirect = encodeURIComponent(location.pathname + location.search);
    location.href = '/login?redirect=' + redirect;
  }
}

// ---------------------------------------------------------------------------
// 单点注销（撤销所有会话）
// ---------------------------------------------------------------------------
async function sloLogout() {
  const token = getAccessToken();
  if (!token) return;
  try {
    await fetch('/api/auth/slo', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + token,
      },
    });
  } catch (_) {}
  logout(true);
}

// ---------------------------------------------------------------------------
// Token 刷新
// ---------------------------------------------------------------------------
async function refreshAccessToken() {
  // 防并发刷新：锁带时间戳，避免某个标签页崩溃后锁永远不释放
  // （原实现只写 '1'，一旦异常退出，之后所有刷新都直接返回旧 token → 必然 401 → 被登出）
  const LOCK_TTL = 15000;
  const raw = localStorage.getItem(REFRESH_LOCK_KEY);
  const lockAt = raw ? parseInt(raw, 10) : 0;
  if (lockAt && Date.now() - lockAt < LOCK_TTL) {
    // 其他标签页/后台刷新正在续期：等它把新 token 写回来再返回，
    // 只等一次固定时长可能拿到旧 token，导致 401 重试后直接登出。
    const before = getAccessToken();
    for (let i = 0; i < 12; i++) {
      await new Promise(r => setTimeout(r, 300));
      const now = getAccessToken();
      if (now && now !== before) return now;
      if (!localStorage.getItem(REFRESH_LOCK_KEY)) break;
    }
    return getAccessToken();
  }
  localStorage.setItem(REFRESH_LOCK_KEY, String(Date.now()));
  try {
    const st = getAuthState();
    if (!st || !st.refresh_token) return null;
    const res = await fetch('/api/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: st.refresh_token }),
    });
    if (!res.ok) {
      // 刷新失败 → 登出
      setAuthState(null);
      _clearTimers();
      return null;
    }
    const data = await res.json();
    st.access_token = data.access_token;
    st.expires_at = Date.now() + (data.expires_in || 1800) * 1000;
    setAuthState(st);
    _scheduleRefresh(data.expires_in || 1800);
    _resetIdleTimer();
    return data.access_token;
  } finally {
    localStorage.removeItem(REFRESH_LOCK_KEY);
  }
}

// ---------------------------------------------------------------------------
// 带认证的 fetch 包装
// ---------------------------------------------------------------------------
async function authFetch(url, options) {
  if (!options) options = {};
  if (!options.headers) options.headers = {};
  const token = getAccessToken();
  if (token) {
    // 用 X-Auth-Token 替代 Authorization，避免 Cloudflare 代理剥除 Bearer 头
    options.headers['X-Auth-Token'] = token;
  }
  let res = await fetch(url, options);
  // 401 → 尝试刷新 Token 后重试一次
  if (res.status === 401) {
    const newToken = await refreshAccessToken();
    if (newToken) {
      // 服务端只用 X-Auth-Token 校验（规避 Cloudflare 剥除 Authorization），
      // 重试时必须同步更新 X-Auth-Token，否则仍 401 并被登出。
      options.headers['X-Auth-Token'] = newToken;
      options.headers['Authorization'] = 'Bearer ' + newToken;
      res = await fetch(url, options);
    }
  }
  // 刷新后仍 401 → 触发登出
  if (res.status === 401) {
    if (_onSessionExpired) _onSessionExpired();
    else logout(true);
  }
  return res;
}

// ---------------------------------------------------------------------------
// 空闲超时
// ---------------------------------------------------------------------------
let _idleBound = false;
let _lastActivity = 0;

function _onUserActivity() {
  const now = Date.now();
  // 节流：mousemove/scroll 触发极其频繁，1 秒内只处理一次
  if (now - _lastActivity < 1000) return;
  _lastActivity = now;
  _restartIdleTimer();
}

function _restartIdleTimer() {
  if (_idleTimer) clearTimeout(_idleTimer);
  _idleTimer = setTimeout(() => {
    if (_onSessionExpired) _onSessionExpired();
    else {
      alert('会话已超时，请重新登录');
      logout();
    }
  }, SESSION_TIMEOUT);
}

function _resetIdleTimer() {
  _restartIdleTimer();
  if (_idleBound) return;   // 监听器只绑定一次，避免每次活动都新增 5 个监听器
  _idleBound = true;
  ['click', 'keydown', 'scroll', 'mousemove', 'touchstart'].forEach(evt => {
    document.addEventListener(evt, _onUserActivity, { passive: true });
  });
}

function _scheduleRefresh(expiresInSeconds) {
  if (_refreshTimer) clearTimeout(_refreshTimer);
  const delay = Math.max(0, (expiresInSeconds * 1000) - REFRESH_BUFFER);
  _refreshTimer = setTimeout(() => {
    refreshAccessToken();
  }, delay);
}

function _clearTimers() {
  if (_idleTimer) { clearTimeout(_idleTimer); _idleTimer = null; }
  if (_refreshTimer) { clearTimeout(_refreshTimer); _refreshTimer = null; }
}

// ---------------------------------------------------------------------------
// 会话过期回调注册（由业务页面设置）
// ---------------------------------------------------------------------------
function onSessionExpired(callback) {
  _onSessionExpired = callback;
}

// ---------------------------------------------------------------------------
// 初始化：检查登录状态，未登录则跳转
// ---------------------------------------------------------------------------
function requireLogin(redirectUrl) {
  const st = getAuthState();
  const hasRefresh = !!(st && st.refresh_token);
  const target = redirectUrl || location.pathname + location.search;
  // 完全没有任何凭据才跳登录页
  if (!st || (!st.access_token && !hasRefresh)) {
    location.href = '/login?redirect=' + encodeURIComponent(target);
    return false;
  }
  // access_token 已过期但 refresh_token 还在 → 后台静默续期，
  // 不要直接踢回登录页（否则 7 天有效期的 refresh_token 永远用不上）
  if (st.access_token && st.expires_at && Date.now() > st.expires_at && hasRefresh) {
    refreshAccessToken().catch(() => {});
  }
  _resetIdleTimer();
  if (st.expires_at && st.expires_at > Date.now()) {
    const remaining = Math.max(0, (st.expires_at - Date.now()) / 1000);
    _scheduleRefresh(remaining);
  }
  return true;
}

// ---------------------------------------------------------------------------
// 登录页专用逻辑
// ---------------------------------------------------------------------------
(function initLoginPage() {
  const form = document.getElementById('loginForm');
  if (!form) return;  // 不在登录页

  // 首启随机口令：已登录但未改密时留在本页改密，别把用户丢回会被 403 的主页
  if (isAuthenticated()) {
    if (getAuthState().must_change_password) {
      _showChangePanel(_p);
      return;
    }
    const params = new URLSearchParams(location.search);
    const redirect = safeRedirectTarget(params.get('redirect'));
    location.href = redirect;
    return;
  }

  const btn = document.getElementById('loginBtn');
  const errEl = document.getElementById('errorMsg');
  const cpForm = document.getElementById('changePwForm');
  const cpBtn = document.getElementById('changePwBtn');
  const cpErr = document.getElementById('changePwErr');
  var _u = '', _p = '';

  // 追踪用户输入，解决 autofill/浏览器延迟读取问题
  document.getElementById('username').addEventListener('input', function() { _u = this.value; });
  document.getElementById('password').addEventListener('input', function() { _p = this.value; });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    var username = _u.trim();
    var password = _p;
    // 兜底：如果 input 事件未捕获到（如 autofill 后直接提交），直接从 DOM 读
    if (!username && !password) {
      try { username = document.getElementById('username').value.trim(); } catch(_) {}
      try { password = document.getElementById('password').value; } catch(_) {}
    }
    if (!username || !password) {
      _showError('请输入用户名和密码');
      return;
    }
    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner"></span>登录中…';
    errEl.className = 'error-msg';
    errEl.textContent = '';

    try {
      const data = await login(username, password);
      if (data && data.must_change_password) {
        _showChangePanel(password);
        return;
      }
      var params = new URLSearchParams(location.search);
      var redirect = safeRedirectTarget(params.get('redirect'));
      location.href = redirect;
    } catch (err) {
      _showError(err.message || '登录失败，请检查用户名和密码');
    } finally {
      btn.disabled = false;
      btn.innerHTML = '登 录';
    }
  });

  // 改密面板：初始口令只在内存里传递，不写 localStorage / sessionStorage
  function _showChangePanel(oldPw) {
    form.hidden = true;
    var panel = document.getElementById('changePwForm');
    if (panel) {
      panel.hidden = false;
      if (oldPw) document.getElementById('oldPassword').value = oldPw;
      document.getElementById('newPassword').focus();
    }
  }
  function _showChangeError(msg) {
    cpErr.textContent = msg;
    cpErr.className = 'error-msg visible';
  }
  if (cpForm) cpForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const oldPw = document.getElementById('oldPassword').value;
    const newPw = document.getElementById('newPassword').value;
    const again = document.getElementById('confirmPassword').value;
    if (!oldPw || !newPw || newPw !== again || newPw.length < 6) {
      _showChangeError('请填写当前口令，并输入两次一致的新密码（至少 6 位）');
      return;
    }
    cpBtn.disabled = true;
    cpErr.className = 'error-msg';
    cpErr.textContent = '';
    try {
      const res = await authFetch('/api/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.message || '修改失败');
      const st = getAuthState();
      st.must_change_password = false;
      setAuthState(st);
      location.href = safeRedirectTarget(new URLSearchParams(location.search).get('redirect'));
    } catch (err) {
      _showChangeError(err.message || '修改失败，请重试');
    } finally {
      cpBtn.disabled = false;
    }
  });

  function _showError(msg) {
    errEl.textContent = msg;
    errEl.className = 'error-msg visible';
  }
})();

// ---------------------------------------------------------------------------
// 深色模式管理（全局，所有页面共享）
// ---------------------------------------------------------------------------
(function(){
  var THEME_KEY = 'word3500_theme';
  var html = document.documentElement;

  // 读取已保存主题，若无则按系统偏好
  function getPreferredTheme() {
    var saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function applyTheme(theme) {
    html.setAttribute('data-theme', theme);
    localStorage.setItem(THEME_KEY, theme);
  }

  function toggleTheme() {
    var current = html.getAttribute('data-theme') || 'light';
    var next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    updateToggleIcon(next);
  }

  function updateToggleIcon(theme) {
    var btn = document.getElementById('_themeToggle');
    if (!btn) return;
    var svg = btn.querySelector('svg');
    if (!svg) return;
    // 清空 SVG 重新构建
    svg.innerHTML = '';
    var ns = 'http://www.w3.org/2000/svg';
    var path = document.createElementNS(ns, 'path');
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('stroke-linecap', 'round');
    if (theme === 'dark') {
      path.setAttribute('d', 'M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z');
      svg.appendChild(path);
    } else {
      path.setAttribute('d', 'M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41');
      svg.appendChild(path);
      var circle = document.createElementNS(ns, 'circle');
      circle.setAttribute('cx', '12');
      circle.setAttribute('cy', '12');
      circle.setAttribute('r', '4');
      circle.setAttribute('fill', 'none');
      circle.setAttribute('stroke', 'currentColor');
      circle.setAttribute('stroke-width', '2');
      svg.appendChild(circle);
    }
  }

  // 初始化主题
  applyTheme(getPreferredTheme());

  // 监听系统主题变化（用户未手动覆盖时跟随）
  var mq = window.matchMedia('(prefers-color-scheme: dark)');
  mq.addEventListener('change', function(e) {
    var saved = localStorage.getItem(THEME_KEY);
    if (!saved) {
      applyTheme(e.matches ? 'dark' : 'light');
    }
  });

  // 搜索用户栏并注入主题切换按钮
  function makeToggleBtn(isDark) {
    var btn = document.createElement('button');
    btn.id = '_themeToggle';
    btn.className = 'theme-toggle';
    btn.title = '切换主题';
    btn.onclick = toggleTheme;
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    var path = document.createElementNS(ns, 'path');
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('stroke-linecap', 'round');
    if (isDark) {
      path.setAttribute('d', 'M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z');
      svg.appendChild(path);
    } else {
      path.setAttribute('d', 'M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41');
      svg.appendChild(path);
      var circle = document.createElementNS(ns, 'circle');
      circle.setAttribute('cx', '12'); circle.setAttribute('cy', '12');
      circle.setAttribute('r', '4'); circle.setAttribute('fill', 'none');
      circle.setAttribute('stroke', 'currentColor'); circle.setAttribute('stroke-width', '2');
      svg.appendChild(circle);
    }
    btn.appendChild(svg);
    return btn;
  }

  function injectToggle() {
    if (document.getElementById('_themeToggle')) return;
    var bar = document.getElementById('userBar');
    var isDark = (html.getAttribute('data-theme') === 'dark');
    if (bar) {
      bar.appendChild(makeToggleBtn(isDark));
      return;
    }
    // 无用户栏的页面（登录/注册），在标题右侧放一个
    var card = document.querySelector('.login-card, .register-card');
    if (card) {
      var h2 = card.querySelector('h2');
      if (h2) {
        h2.style.display = 'flex';
        h2.style.alignItems = 'center';
        h2.style.justifyContent = 'center';
        h2.style.gap = '10px';
        var btn = makeToggleBtn(isDark);
        btn.style.flexShrink = '0';
        h2.appendChild(btn);
      }
    }
  }
  // DOM 就绪后注入
  injectToggle();
  if (!document.getElementById('_themeToggle')) {
    setTimeout(injectToggle, 500);
  }
})();
