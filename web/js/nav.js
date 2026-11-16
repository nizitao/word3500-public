// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

/* 全站底部导航（设计文档 §6，v3 收藏夹升为一级入口）
   - 五个主入口：单词 / 收藏 / 一起背（凸起）/ 学习 / 我
   - 收藏夹原先只能从「首页工具栏」或「我 → 我的收藏夹」绕进去，现在是一级入口
   - 「一起背」为凸起主按钮，点击上滑弹出三个高频快捷入口
   - 五槽布局：左右各两项、每项 flex 1，凸起按钮天然居中（旧版靠「我」占两份宽度凑中线）
   - 自注入样式，页面只需一行 <script src="/js/nav.js">，避免逐页维护 CSS 引用 */
(function () {
  if (window.__w35NavReady) return;
  window.__w35NavReady = true;

  var path = location.pathname.replace(/\/+$/, '') || '/';
  // 登录/注册页不显示导航（避免未登录时误点）
  if (path === '/login' || path === '/register' || path === '/login.html' || path === '/register.html') return;

  var CSS =
    '.w35-nav{position:fixed;left:0;right:0;bottom:0;z-index:9990;display:flex;align-items:flex-end;' +
    'justify-content:space-around;height:62px;padding:0 6px calc(env(safe-area-inset-bottom,0px));' +
    'background:var(--bg-card,#fff);' +
    'background:color-mix(in srgb,var(--bg-card,#fff) 88%,transparent);' +
    '-webkit-backdrop-filter:blur(18px) saturate(1.4);backdrop-filter:blur(18px) saturate(1.4);' +
    'border-top:1px solid var(--border-light,#e3e8ef);box-shadow:0 -2px 18px rgba(15,23,42,.06)}' +
    '.w35-nav-item{flex:1 1 0;display:flex;flex-direction:column;align-items:center;justify-content:center;' +
    'gap:3px;height:100%;text-decoration:none;color:var(--text-muted,#94a3b8);font-size:11px;' +
    'border:none;background:transparent;font-family:inherit;cursor:pointer;padding:0;min-width:0}' +
    '.w35-nav-item svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.8;' +
    'stroke-linecap:round;stroke-linejoin:round}' +
    '.w35-nav-item.active{color:var(--primary,#2f6fed)}' +
    '.w35-nav-item.active svg{stroke-width:2.1}' +
    '.w35-nav-item span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}' +
    /* 五槽：左右各两项等宽，凸起按钮落在 50% 中线上 */
    /* 中央凸起按钮 */
    '.w35-nav-center{flex:1 1 0;position:relative;height:100%;border:none;background:transparent;' +
    'cursor:pointer;font-family:inherit;display:flex;align-items:flex-end;justify-content:center;padding:0}' +
    '.w35-nav-center .w35-fab{position:absolute;bottom:calc(100% - 26px);width:54px;height:54px;' +
    'border-radius:50%;background:linear-gradient(135deg,#2f6fed,#3b82f6);color:#fff;display:flex;' +
    'align-items:center;justify-content:center;box-shadow:0 6px 18px rgba(47,111,237,.42);' +
    'border:3px solid var(--bg-card,#fff);transition:transform .22s ease}' +
    '.w35-nav-center .w35-fab svg{width:26px;height:26px;fill:none;stroke:currentColor;stroke-width:1.9;' +
    'stroke-linecap:round;stroke-linejoin:round}' +
    '.w35-nav-center.open .w35-fab{transform:rotate(45deg) scale(.96)}' +
    '.w35-nav-center .w35-fab-label{position:absolute;bottom:8px;font-size:11px;color:var(--text-muted,#94a3b8)}' +
    '.w35-nav-center.active .w35-fab-label{color:var(--primary,#2f6fed)}' +
    /* 快捷面板 */
    '.w35-quick{position:fixed;left:50%;bottom:calc(74px + env(safe-area-inset-bottom,0px));' +
    'transform:translate(-50%,14px);z-index:9989;width:min(300px,86vw);padding:12px;border-radius:16px;' +
    'background:var(--bg-card,#fff);border:1px solid var(--border-light,#e3e8ef);' +
    'box-shadow:0 12px 40px rgba(15,23,42,.2);opacity:0;pointer-events:none;transition:opacity .2s ease,transform .2s ease}' +
    '.w35-quick.show{opacity:1;pointer-events:auto;transform:translate(-50%,0)}' +
    '.w35-quick a{display:flex;align-items:center;gap:10px;padding:11px 12px;border-radius:10px;' +
    'text-decoration:none;color:var(--text-primary,#222);font-size:14px}' +
    '.w35-quick a:hover{background:var(--bg-hover,#eef2f7)}' +
    '.w35-quick a svg{width:20px;height:20px;fill:none;stroke:var(--primary,#2f6fed);stroke-width:1.9;' +
    'stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}' +
    '.w35-quick a em{font-style:normal;font-size:11px;color:var(--text-muted,#94a3b8);margin-left:auto}' +
    '.w35-quick .w35-quick-foot{margin-top:8px;padding:9px 12px 3px;border-top:1px solid var(--border-lighter,#eef1f5);' +
    'font-size:12px;color:var(--text-tertiary,#64748b);display:flex;justify-content:space-between;align-items:center}' +
    '.w35-quick .w35-quick-foot a{padding:0;font-size:12px;color:var(--primary,#2f6fed);text-decoration:none}' +
    /* 内容不被导航遮挡 */
    'body{padding-bottom:calc(74px + env(safe-area-inset-bottom,0px)) !important}' +
    /* 在线人数徽标上移，避免被导航压住 */
    '.w35-online-badge{bottom:calc(76px + env(safe-area-inset-bottom,0px)) !important}' +
    '@media(max-width:640px){.w35-online-badge{right:10px !important}}';

  var ICONS = {
    words: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    fav: '<path d="M2 6a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z"/>',
    study: '<path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 4-6"/>',
    together: '<circle cx="9" cy="8" r="3.2"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16.5 6.2a3 3 0 0 1 0 5.6"/><path d="M18 20a6 6 0 0 0-2.2-4.6"/>',
    me: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    quiz: '<circle cx="10" cy="7" r="4"/><path d="M10.8 11 18 21l2-1.5-7.2-9.7"/><path d="m6.5 15 2.7 2.5L12 15"/>',
    book: '<path d="M2 6a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z"/>',
    check: '<path d="M20 6 9 17l-5-5"/>'
  };

  function svg(name) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true">' + (ICONS[name] || '') + '</svg>';
  }

  function currentKey() {
    var p = path;
    if (p === '/' || p === '/index.html') return 'words';
    if (p === '/favorites' || p === '/favorites.html') return 'fav';
    if (p === '/study' || p === '/study.html') return 'study';
    if (p === '/together') return 'together';
    if (p === '/learn' || p === '/search' || p === '/quiz') return 'words';
    return 'me';
  }

  function build() {
    var style = document.createElement('style');
    style.id = 'w35-nav-style';
    style.textContent = CSS;
    document.head.appendChild(style);

    var key = currentKey();
    var nav = document.createElement('nav');
    nav.className = 'w35-nav';
    nav.setAttribute('role', 'navigation');
    nav.setAttribute('aria-label', '主导航');
    nav.innerHTML =
      '<a class="w35-nav-item' + (key === 'words' ? ' active' : '') + '" href="/">' + svg('words') + '<span>单词</span></a>' +
      '<a class="w35-nav-item' + (key === 'fav' ? ' active' : '') + '" href="/favorites">' + svg('fav') + '<span>收藏</span></a>' +
      '<button class="w35-nav-center' + (key === 'together' ? ' active' : '') + '" id="w35NavCenter" aria-expanded="false" aria-label="一起背与快捷入口">' +
        '<span class="w35-fab">' + svg('together') + '</span>' +
        '<span class="w35-fab-label">一起背</span>' +
      '</button>' +
      '<a class="w35-nav-item' + (key === 'study' ? ' active' : '') + '" href="/study">' + svg('study') + '<span>学习</span></a>' +
      '<a class="w35-nav-item' + (key === 'me' ? ' active' : '') + '" href="/me">' + svg('me') + '<span>我</span></a>';
    document.body.appendChild(nav);

    var quick = document.createElement('div');
    quick.className = 'w35-quick';
    quick.id = 'w35Quick';
    quick.innerHTML =
      '<a href="/quiz">' + svg('quiz') + '单词训练<em>测词</em></a>' +
      '<a href="/quiz?review=1">' + svg('check') + '单词自检<em>复习</em></a>' +
      '<div class="w35-quick-foot"><span>组队学习</span><a href="/together">一起背 ›</a></div>';
    document.body.appendChild(quick);

    var btn = nav.querySelector('#w35NavCenter');
    function toggle(open) {
      var show = open == null ? !quick.classList.contains('show') : open;
      quick.classList.toggle('show', show);
      btn.classList.toggle('open', show);
      btn.setAttribute('aria-expanded', show ? 'true' : 'false');
    }
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      toggle();
    });
    document.addEventListener('click', function (e) {
      if (!quick.contains(e.target) && e.target !== btn) toggle(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') toggle(false);
    });
    // 下滑收起
    var startY = 0;
    quick.addEventListener('touchstart', function (e) {
      startY = e.touches[0].clientY;
    }, { passive: true });
    quick.addEventListener('touchmove', function (e) {
      if (e.touches[0].clientY - startY > 36) toggle(false);
    }, { passive: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();
