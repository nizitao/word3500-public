// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

/*!
 * W35Charts —— 学习统计页纯 SVG 图表（零第三方依赖 / ES5 / 无构建步骤）
 *
 * 用法：
 *   W35Charts.line(hostEl, {
 *     labels: ['11-01', '11-02', '11-03'],
 *     series: [{ key: 'mastery', label: '掌握率', color: '#2f6fed', values: [42, 55, 68] }],
 *     height: 220, yMax: 100,
 *     yFormat: function (v) { return v + '%'; }
 *   });
 *
 *   W35Charts.hbar(hostEl, {
 *     items: [{ label: '名词', value: 72, total: 100 }],
 *     max: 100,
 *     onClick: function (item, index) { ... }   // 点击整行下钻
 *   });
 *
 * 约定：轴文字与数值统一用 currentColor，明暗由父元素 color 控制；
 *       折线/柱条颜色由调用方传入，未传时使用内置调色板。
 * 同一 host 重复渲染会先执行 host.__w35chartCleanup 清理上一次的 resize 监听。
 */
(function (global) {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';
  var PALETTE = ['#2f6fed', '#16a34a', '#f59e0b', '#dc2626'];
  var seq = 0;

  function isArr(v) { return Object.prototype.toString.call(v) === '[object Array]'; }
  function num(v) { v = Number(v); return isFinite(v) ? v : 0; }
  function round1(v) { return Math.round(num(v) * 10) / 10; }

  function el(tag, attrs, text) {
    var node = document.createElementNS(NS, tag);
    if (attrs) {
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k) && attrs[k] != null) {
          node.setAttribute(k, String(attrs[k]));
        }
      }
    }
    if (text != null) node.textContent = String(text);
    return node;
  }

  function setContent(host, node) {
    while (host.firstChild) host.removeChild(host.firstChild);
    if (node) host.appendChild(node);
  }

  function emptyBox(text) {
    var p = document.createElement('div');
    p.textContent = text;
    p.setAttribute('style', 'padding:28px 0;text-align:center;font-size:13px;opacity:.55');
    return p;
  }

  function clearChart(host) {
    if (host && typeof host.__w35chartCleanup === 'function') {
      try { host.__w35chartCleanup(); } catch (e) { /* 忽略 */ }
    }
    if (host) host.__w35chartCleanup = null;
  }

  function bindResize(host, redraw) {
    var timer = null;
    function onResize() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () { timer = null; redraw(); }, 160);
    }
    window.addEventListener('resize', onResize);
    host.__w35chartCleanup = function () {
      window.removeEventListener('resize', onResize);
      if (timer) clearTimeout(timer);
      timer = null;
    };
  }

  function niceMax(v) {
    v = num(v);
    if (v <= 5) return 5;
    var exp = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
    var f = v / exp;
    var nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return nf * exp;
  }

  function clipText(s, px) {
    s = String(s == null ? '' : s);
    var max = Math.max(2, Math.floor(num(px) / 12));
    if (s.length <= max) return s;
    return s.slice(0, max - 1) + '…';
  }

  // -------------------------------------------------------------------------
  // 折线图
  // -------------------------------------------------------------------------
  function line(host, opts) {
    if (!host || !host.appendChild || !opts) return;
    clearChart(host);

    try {
      var labels = isArr(opts.labels) ? opts.labels : [];
      var src = isArr(opts.series) ? opts.series : [];
      var list = [];
      var i;
      for (i = 0; i < src.length; i++) {
        if (src[i] && isArr(src[i].values) && src[i].values.length) list.push(src[i]);
      }
      var H = num(opts.height) > 0 ? num(opts.height) : 220;
      var emptyText = opts.emptyText || '暂无数据';

      function colorOf(item, idx) {
        if (item && item.color) return String(item.color);
        return PALETTE[idx % PALETTE.length];
      }

      function tickText(v) {
        if (typeof opts.yFormat === 'function') {
          try { return String(opts.yFormat(v)); } catch (e) { /* 忽略 */ }
        }
        return String(Math.round(num(v)));
      }

      function pointTip(item, idx) {
        if (typeof opts.tooltip === 'function') {
          try { return String(opts.tooltip(idx)); } catch (e) { /* 忽略 */ }
        }
        var head = String(labels[idx] == null ? '' : labels[idx]);
        var value = String(num(item.values[idx]));
        if (list.length > 1) head += ' · ' + String(item.label || '');
        return head + ': ' + value;
      }

      function draw() {
        var cnt = labels.length;
        if (!cnt || !list.length) {
          setContent(host, emptyBox(emptyText));
          return;
        }

        var W = Math.max(260, Math.round(host.clientWidth || 640));
        var P = { l: 44, r: 14, t: 14, b: 26 };
        var iw = W - P.l - P.r;
        var ih = H - P.t - P.b;
        if (iw < 20 || ih < 20) return;

        var maxV = 1;
        var a, b, s;
        for (a = 0; a < list.length; a++) {
          for (b = 0; b < cnt; b++) maxV = Math.max(maxV, num(list[a].values[b]));
        }
        var top = num(opts.yMax) > 0 ? num(opts.yMax) : niceMax(maxV);
        var step = top / 4;

        function xf(idx) { return cnt <= 1 ? P.l + iw / 2 : P.l + idx * iw / (cnt - 1); }
        function yf(v) { return P.t + ih - Math.min(num(v), top) / top * ih; }

        var root = el('svg', {
          viewBox: '0 0 ' + W + ' ' + H,
          width: '100%',
          height: H,
          preserveAspectRatio: 'xMidYMid meet',
          role: 'img',
          'aria-label': '折线图',
          style: 'display:block'
        });

        // 水平网格 + Y 轴刻度
        var g;
        for (g = 0; g <= 4; g++) {
          var val = step * g;
          var gy = yf(val);
          root.appendChild(el('line', {
            x1: P.l, y1: round1(gy), x2: W - P.r, y2: round1(gy),
            stroke: 'currentColor', 'stroke-opacity': 0.18,
            'stroke-width': 1, 'stroke-dasharray': '4 4'
          }));
          root.appendChild(el('text', {
            x: P.l - 8, y: round1(gy + 4), 'text-anchor': 'end',
            'font-size': 11, fill: 'currentColor', 'fill-opacity': 0.62
          }, tickText(val)));
        }

        // X 轴标签（约 6 个等距，首尾必显示）
        if (cnt === 1) {
          root.appendChild(el('text', {
            x: round1(xf(0)), y: H - 8, 'text-anchor': 'middle',
            'font-size': 11, fill: 'currentColor', 'fill-opacity': 0.62
          }, String(labels[0])));
        } else {
          var count = Math.min(cnt, 6);
          var seen = {};
          var k;
          for (k = 0; k < count; k++) {
            var idx = Math.round(k * (cnt - 1) / (count - 1));
            if (seen[idx]) continue;
            seen[idx] = 1;
            var anchor = idx === 0 ? 'start' : (idx === cnt - 1 ? 'end' : 'middle');
            root.appendChild(el('text', {
              x: round1(xf(idx)), y: H - 8, 'text-anchor': anchor,
              'font-size': 11, fill: 'currentColor', 'fill-opacity': 0.62
            }, String(labels[idx])));
          }
        }

        // 第一条曲线的渐变面积
        if (cnt > 1) {
          var gid = 'w35area' + (++seq);
          var defs = el('defs');
          var grad = el('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
          grad.appendChild(el('stop', {
            offset: '0%', 'stop-color': colorOf(list[0], 0), 'stop-opacity': 0.26
          }));
          grad.appendChild(el('stop', {
            offset: '100%', 'stop-color': colorOf(list[0], 0), 'stop-opacity': 0.02
          }));
          defs.appendChild(grad);
          root.appendChild(defs);

          var base = P.t + ih;
          var area = 'M' + round1(xf(0)) + ' ' + round1(base);
          for (a = 0; a < cnt; a++) area += ' L' + round1(xf(a)) + ' ' + round1(yf(list[0].values[a]));
          area += ' L' + round1(xf(cnt - 1)) + ' ' + round1(base) + ' Z';
          root.appendChild(el('path', { d: area, fill: 'url(#' + gid + ')' }));
        }

        // 折线
        for (s = 0; s < list.length; s++) {
          var item = list[s];
          var d = '';
          for (a = 0; a < cnt; a++) {
            d += (a ? ' L' : 'M') + round1(xf(a)) + ' ' + round1(yf(item.values[a]));
          }
          var lineAttrs = {
            d: d, fill: 'none', stroke: colorOf(item, s), 'stroke-width': 2,
            'stroke-linejoin': 'round', 'stroke-linecap': 'round'
          };
          if (item.dashed) lineAttrs['stroke-dasharray'] = '5 4';
          root.appendChild(el('path', lineAttrs));
        }

        // 数据点（含原生 <title> 悬浮提示）
        for (s = 0; s < list.length; s++) {
          var dotColor = colorOf(list[s], s);
          for (a = 0; a < cnt; a++) {
            var dot = el('circle', {
              cx: round1(xf(a)), cy: round1(yf(list[s].values[a])), r: 3,
              fill: dotColor, stroke: '#ffffff', 'stroke-opacity': 0.5, 'stroke-width': 1
            });
            dot.appendChild(el('title', null, pointTip(list[s], a)));
            root.appendChild(dot);
          }
        }

        setContent(host, root);
      }

      draw();
      if (labels.length && list.length) bindResize(host, draw);
    } catch (e) {
      // 渲染失败静默处理，不打断页面
    }
  }

  // -------------------------------------------------------------------------
  // 横向柱状图
  // -------------------------------------------------------------------------
  function hbar(host, opts) {
    if (!host || !host.appendChild || !opts) return;
    clearChart(host);

    try {
      var items = isArr(opts.items) ? opts.items : [];
      var max = num(opts.max) > 0 ? num(opts.max) : 100;
      var emptyText = opts.emptyText || '暂无数据';

      function colorOf(item, idx) {
        if (item && item.color) return String(item.color);
        return PALETTE[idx % PALETTE.length];
      }

      function valueText(v) {
        if (typeof opts.valueFormat === 'function') {
          try { return String(opts.valueFormat(v)); } catch (e) { /* 忽略 */ }
        }
        return String(v) + '%';
      }

      function draw() {
        if (!items.length) {
          setContent(host, emptyBox(emptyText));
          return;
        }

        var W = Math.max(260, Math.round(host.clientWidth || 640));
        var H = num(opts.height) > 0 ? num(opts.height) : items.length * 34 + 20;
        var padT = 10;
        var padB = 10;
        var rowH = (H - padT - padB) / items.length;
        if (rowH <= 0) return;

        var labelW = 92;
        var valueW = 52;
        var barX = labelW;
        var barW = Math.max(20, W - labelW - valueW - 10);
        var barH = Math.min(14, Math.max(6, rowH - 16));

        var root = el('svg', {
          viewBox: '0 0 ' + W + ' ' + H,
          width: '100%',
          height: H,
          preserveAspectRatio: 'xMidYMid meet',
          role: 'img',
          'aria-label': '横向柱状图',
          style: 'display:block'
        });

        for (var i = 0; i < items.length; i++) {
          var item = items[i] || {};
          var value = num(item.value);
          var ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
          var y0 = padT + i * rowH;
          var cy = y0 + rowH / 2;
          var by = cy - barH / 2;
          var full = String(item.label == null ? '' : item.label);
          var shown = valueText(value);

          var row = el('g', { 'class': 'w35-hbar-row' });
          if (typeof opts.onClick === 'function') row.setAttribute('style', 'cursor:pointer');

          // 整行命中区（透明）
          row.appendChild(el('rect', {
            x: 0, y: round1(y0), width: W, height: round1(rowH),
            fill: '#000000', 'fill-opacity': 0
          }));

          row.appendChild(el('text', {
            x: labelW - 10, y: round1(cy + 4), 'text-anchor': 'end',
            'font-size': 12, fill: 'currentColor', 'fill-opacity': 0.8
          }, clipText(full, labelW - 14)));

          // 轨道（value 为 0 时也保留，便于看出量程）
          row.appendChild(el('rect', {
            x: barX, y: round1(by), width: round1(barW), height: barH,
            rx: 3, ry: 3, fill: 'currentColor', 'fill-opacity': 0.12
          }));

          if (ratio > 0) {
            row.appendChild(el('rect', {
              x: barX, y: round1(by), width: round1(Math.max(2, barW * ratio)),
              height: barH, rx: 3, ry: 3, fill: colorOf(item, i)
            }));
          }

          row.appendChild(el('text', {
            x: W - 6, y: round1(cy + 4), 'text-anchor': 'end',
            'font-size': 12, 'font-weight': 600, fill: 'currentColor'
          }, shown));

          var tip = full;
          if (item.extra != null && item.extra !== '') tip += ' · ' + item.extra;
          row.appendChild(el('title', null, tip + ' · ' + shown));

          row.addEventListener('mouseenter', function () { this.setAttribute('opacity', '0.72'); });
          row.addEventListener('mouseleave', function () { this.removeAttribute('opacity'); });

          if (typeof opts.onClick === 'function') {
            (function (it, index) {
              row.addEventListener('click', function () {
                try { opts.onClick(it, index); } catch (e) { /* 忽略 */ }
              });
            })(item, i);
          }

          root.appendChild(row);
        }

        setContent(host, root);
      }

      draw();
      if (items.length) bindResize(host, draw);
    } catch (e) {
      // 渲染失败静默处理，不打断页面
    }
  }

  global.W35Charts = { line: line, hbar: hbar };
})(window);
