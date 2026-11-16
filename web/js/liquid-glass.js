// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

// liquid-glass.js —— 液态玻璃真实折射（Vanilla JS，无依赖）
// 基于 Shu Ding 的 SDF + feDisplacementMap 算法：把圆角矩形的位移场画进 canvas，
// 作为 feImage 喂给 feDisplacementMap，让 backdrop-filter 真正产生折射。
// 注意：本文件曾被 _minify.py 压成单行，行尾 `//` 注释吞掉了后续全部代码，
// 导致整个文件语法无效（Node 报 Unexpected end of input）、效果从未生效。
// 如需压缩请用 terser/esbuild，不要用正则剥注释。
(function (global) {
  'use strict';

  function smoothStep(a, b, t) {
    t = Math.max(0, Math.min(1, (t - a) / (b - a)));
    return t * t * (3 - 2 * t);
  }

  function length(x, y) {
    return Math.sqrt(x * x + y * y);
  }

  function roundedRectSDF(x, y, w, h, r) {
    const qx = Math.abs(x) - w + r;
    const qy = Math.abs(y) - h + r;
    return Math.min(Math.max(qx, qy), 0) + length(Math.max(qx, 0), Math.max(qy, 0)) - r;
  }

  function applyLiquidGlass(el, opts) {
    if (!el || el.nodeType !== 1) return null;
    opts = opts || {};
    if (global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      return null;
    }

    const supportsBackdrop =
      (typeof CSS !== 'undefined' && CSS.supports && CSS.supports('backdrop-filter', 'blur(1px)')) ||
      (typeof CSS !== 'undefined' && CSS.supports && CSS.supports('-webkit-backdrop-filter', 'blur(1px)'));
    // 不支持 backdrop-filter 时退回纯 CSS 的模糊/半透明样式
    if (!supportsBackdrop) return null;

    const supportsUrl = typeof CSS !== 'undefined' && CSS.supports && CSS.supports('backdrop-filter', 'url(#x) blur(1px)');
    const DPI = Math.min(global.devicePixelRatio || 1, 2);
    const id = 'lg-' + Math.random().toString(36).slice(2, 9);
    const SVGNS = 'http://www.w3.org/2000/svg';

    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('width', '0');
    svg.setAttribute('height', '0');
    svg.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;pointer-events:none;z-index:-1;';

    const defs = document.createElementNS(SVGNS, 'defs');
    const filter = document.createElementNS(SVGNS, 'filter');
    filter.setAttribute('id', id);
    filter.setAttribute('x', '-20%');
    filter.setAttribute('y', '-20%');
    filter.setAttribute('width', '140%');
    filter.setAttribute('height', '140%');
    filter.setAttribute('color-interpolation-filters', 'sRGB');

    const feImage = document.createElementNS(SVGNS, 'feImage');
    feImage.setAttribute('id', id + '-map');
    const feDisp = document.createElementNS(SVGNS, 'feDisplacementMap');
    feDisp.setAttribute('in', 'SourceGraphic');
    feDisp.setAttribute('in2', id + '-map');
    feDisp.setAttribute('xChannelSelector', 'R');
    feDisp.setAttribute('yChannelSelector', 'G');
    feDisp.setAttribute('scale', '0');

    filter.appendChild(feImage);
    filter.appendChild(feDisp);
    defs.appendChild(filter);
    svg.appendChild(defs);
    document.body.appendChild(svg);

    const canvas = document.createElement('canvas');
    canvas.style.display = 'none';
    document.body.appendChild(canvas);
    const ctx = canvas.getContext('2d');
    el.classList.add('lg-active');

    let lastW = 0;
    let lastH = 0;
    let rafId = 0;

    function render() {
      rafId = 0;
      const rect = el.getBoundingClientRect();
      const w = Math.max(1, Math.round(rect.width));
      const h = Math.max(1, Math.round(rect.height));
      if (w === lastW && h === lastH && canvas.width) return;
      lastW = w;
      lastH = h;

      const cw = Math.max(1, Math.round(w * DPI));
      const ch = Math.max(1, Math.round(h * DPI));
      canvas.width = cw;
      canvas.height = ch;

      const cs = global.getComputedStyle(el);
      let radius = parseFloat(cs.borderTopLeftRadius) || opts.radius || 20;
      radius = Math.min(radius, Math.min(w, h) / 2);
      const edge = Math.max(2, Math.min(opts.edge != null ? opts.edge : 22, h * 0.6, w * 0.25));

      const data = ctx.createImageData(cw, ch);
      const buf = data.data;
      const raw = [];
      let maxScale = 0;

      for (let i = 0; i < buf.length; i += 4) {
        const px = (i / 4) % cw;
        const py = Math.floor((i / 4) / cw);
        const uvx = px / cw;
        const uvy = py / ch;
        // 以元素中心为原点的 SDF 坐标
        const cx = (uvx - 0.5) * w;
        const cy = (uvy - 0.5) * h;
        const d = roundedRectSDF(cx, cy, w / 2, h / 2, radius);
        const displacement = smoothStep(edge, 0, d);
        const scaled = smoothStep(0, 1, displacement);
        // 越靠边折射越强：把采样点朝中心收缩
        const ix = uvx - 0.5;
        const iy = uvy - 0.5;
        const tx = ix * scaled + 0.5;
        const ty = iy * scaled + 0.5;
        const dx = tx * cw - px;
        const dy = ty * ch - py;
        if (Math.abs(dx) > maxScale) maxScale = Math.abs(dx);
        if (Math.abs(dy) > maxScale) maxScale = Math.abs(dy);
        raw.push(dx, dy);
      }

      maxScale = Math.max(maxScale * 0.5, 1);
      let k = 0;
      for (let i = 0; i < buf.length; i += 4) {
        const r = raw[k++] / maxScale + 0.5;
        const g = raw[k++] / maxScale + 0.5;
        buf[i] = Math.max(0, Math.min(255, r * 255));
        buf[i + 1] = Math.max(0, Math.min(255, g * 255));
        buf[i + 2] = 0;
        buf[i + 3] = 255;
      }
      ctx.putImageData(data, 0, 0);

      const url = canvas.toDataURL();
      feImage.setAttributeNS('http://www.w3.org/1999/xlink', 'href', url);
      feImage.setAttribute('href', url);
      feImage.setAttribute('width', w);
      feImage.setAttribute('height', h);
      feDisp.setAttribute('scale', (maxScale / DPI).toFixed(2));
    }

    function scheduleRender() {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(render);
    }

    scheduleRender();

    let ro = null;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(scheduleRender);
      ro.observe(el);
    }
    global.addEventListener('resize', scheduleRender);

    const blur = opts.blur != null ? opts.blur : 0.4;
    const saturate = opts.saturate != null ? opts.saturate : 1.15;
    const brightness = opts.brightness != null ? opts.brightness : 1.05;
    const contrast = opts.contrast != null ? opts.contrast : 1.06;

    if (supportsUrl) {
      const bf = 'url(#' + id + ') blur(' + blur + 'px) saturate(' + saturate +
                 ') brightness(' + brightness + ') contrast(' + contrast + ')';
      el.style.webkitBackdropFilter = bf;
      el.style.backdropFilter = bf;
    }

    return {
      el: el,
      update: scheduleRender,
      destroy: function () {
        if (ro) ro.disconnect();
        global.removeEventListener('resize', scheduleRender);
        if (rafId) cancelAnimationFrame(rafId);
        svg.remove();
        canvas.remove();
        el.classList.remove('lg-active');
        el.style.backdropFilter = '';
        el.style.webkitBackdropFilter = '';
      }
    };
  }

  global.applyLiquidGlass = applyLiquidGlass;
})(window);
