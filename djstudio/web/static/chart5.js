// 曲谱 v5 的四 Deck 共轴图：数据换算 + 画法。
//
// 演出页（live5.js，canvas）和交接说明（handover.js，SVG）画的是同一张图，所以画法只写一遍：
// drawChart() 只认一个「画笔」（CanvasPainter / SvgPainter），不知道自己画在哪。
// 数据是后端编译出来的 chart（GET /api/sets/<pid>）：全场拍从 0 起（小节 = 拍 / 4 + 1），旋钮的值是谱上的单位
// （EQ dB、Filter -1..1、推子 0..1），钟点位的说法（「11点」）用后端给的 text，这里不自己换算。

export const PARAMS = ["lo", "mid", "hi", "filter", "vol"];
export const PARAM_ZH = { lo: "LOW", mid: "MID", hi: "HIGH", filter: "Filter", vol: "推子" };

// Deck 的身份是固定的：颜色 + 编号 + 线型三样一起出现，角色（主曲 / 节奏层 …）是另外的文字，不改颜色
export const DECK_STYLE = {
  1: { zh: "蓝", line: "实线", dash: [] },
  2: { zh: "橙", line: "虚线", dash: [7, 4] },
  3: { zh: "绿", line: "点线", dash: [2, 3] },
  4: { zh: "紫", line: "点划线", dash: [9, 3, 2, 3] },
};
export const deckStyle = (d) => DECK_STYLE[d] || { zh: "", line: "实线", dash: [] };

// 深色界面 / 白纸上的四个 Deck 颜色（两套都过了色觉障碍和对比度的检查；橙和绿在红绿色弱下偏近，靠线型和编号分）
export const DECK_DARK = { 1: "#3d8bea", 2: "#dc7526", 3: "#27ae60", 4: "#9c7cec" };
export const DECK_PRINT = { 1: "#1668c7", 2: "#c85a00", 3: "#1c8a3c", 4: "#7a3fc0" };

const SANS = "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Helvetica Neue', 'Microsoft YaHei', sans-serif";
const NS = "http://www.w3.org/2000/svg";

// ------------------------------------------------------------------ 位置、时值

export const BEATS = 4; // 首期只有 4/4

// 全场拍 → 「小节.拍」（1.1 = 第 0 拍）；开演前的装载是负的
export function posText(beat) {
  if (beat === null || beat === undefined || isNaN(beat)) return "–";
  if (beat < 0) return "开演前";
  const b = Math.floor(beat + 1e-6);
  return `${Math.floor(b / BEATS) + 1}.${(b % BEATS) + 1}`;
}

// 带零头的位置（回执里「晚了」的实际位置）：33.2+0.31拍
export function posTextFine(beat) {
  if (beat === null || beat === undefined || isNaN(beat) || beat < 0) return posText(beat);
  const fr = beat - Math.floor(beat + 1e-6);
  return posText(beat) + (fr > 0.01 ? `+${fr.toFixed(2)}拍` : "");
}

export const barOf = (beat) => Math.floor((beat + 1e-6) / BEATS) + 1;

// 原曲的拍 → 原曲的小节号（正好在小节线上只写小节，不在就写「小节.拍」）
export function srcText(src) {
  const b = Math.floor(src + 1e-6);
  const bar = Math.floor(b / BEATS) + 1, beat = (b % BEATS) + 1;
  return beat === 1 ? String(bar) : `${bar}.${beat}`;
}

export function durText(beats) {
  if (Math.abs(beats / BEATS - Math.round(beats / BEATS)) < 1e-6 && beats >= BEATS) return `${Math.round(beats / BEATS)}小节`;
  if (Math.abs(beats - Math.round(beats)) < 1e-6) return `${Math.round(beats)}拍`;
  for (const d of [2, 3, 4, 8]) {
    const n = beats * d;
    if (Math.abs(n - Math.round(n)) < 1e-6) return `${Math.round(n)}/${d}拍`;
  }
  return `${beats.toFixed(2)}拍`;
}

// ------------------------------------------------------------------ 从 chart 里查东西

export function sceneAt(chart, beat) {
  const sc = chart.scenes || [];
  return sc.find((s) => beat >= s.start && beat < s.end) || (beat < 0 ? sc[0] : null) || null;
}

export function tempoAt(chart, beat) {
  const t = chart.tempo || [];
  if (!t.length) return null;
  if (beat <= t[0].at) return t[0].bpm;
  for (let i = 0; i + 1 < t.length; i++) {
    const a = t[i], b = t[i + 1];
    if (beat < b.at) return b.at > a.at ? a.bpm + ((b.bpm - a.bpm) * (beat - a.at)) / (b.at - a.at) : b.bpm;
  }
  return t[t.length - 1].bpm;
}

export const fmtBpm = (v) => (v === null || v === undefined ? "–" : Math.abs(v - Math.round(v)) < 0.05 ? String(Math.round(v)) : v.toFixed(1));

export function clipAt(chart, deck, beat) {
  return (chart.clips || []).find((c) => c.deck === deck && beat >= c.start && beat < c.stop) || null;
}

export const clipById = (chart, id) => (chart.clips || []).find((c) => c.id === id) || null;

// 片段在全场第 g 拍读到原曲哪一拍（每个片段各数各的，loop 里是圈内的位置）
export function segAt(clip, g) {
  const segs = clip.segs || [];
  let hit = null;
  for (const s of segs) if (s.g1 > s.g0 && g >= s.g0 - 1e-9) hit = s;
  return hit && g < hit.g1 + 1e-9 ? hit : hit || segs[0] || null;
}

export function srcAt(clip, g) {
  const s = segAt(clip, g);
  if (!s) return null;
  const d = Math.max(0, g - s.g0);
  return s.loop > 0 ? s.src + (d % s.loop) : s.src + d;
}

// 一条参数线画成折线要走的点（拍，值）：斜线 = 渐变，竖线 = 一下到位，横线 = 保持；片段起止之外不画
export function lanePoints(clip, param) {
  const pts = (clip.lanes || {})[param] || [];
  const out = [];
  let prev = null;
  for (const p of pts) {
    if (prev && p.step) out.push([p.at, prev.v]); // 横着走到这一拍，再竖着跳
    out.push([p.at, p.v]);
    prev = p;
  }
  if (!out.length) return out;
  if (out[0][0] > clip.start) out.unshift([clip.start, out[0][1]]);
  if (prev.at < clip.stop) out.push([clip.stop, prev.v]);
  return out;
}

// 谱上这一拍的值：保持 → 那个点的说法；斜线中途 → 「12点 → 9点」（数值按谱上的单位线性算，用来定纵坐标）
export function laneAt(clip, param, beat) {
  const pts = (clip.lanes || {})[param] || [];
  if (!pts.length) return null;
  let i = 0;
  while (i + 1 < pts.length && pts[i + 1].at <= beat + 1e-9) i++;
  const a = pts[i], b = pts[i + 1];
  if (beat < a.at || !b || b.step || b.v === a.v || b.at <= a.at) return { v: a.v, text: a.text, ramp: false };
  const k = (beat - a.at) / (b.at - a.at);
  return { v: a.v + (b.v - a.v) * k, text: `${a.text} → ${b.text}`, ramp: true, from: a, to: b };
}

// 这个场景里每台 Deck 的角色：谱上写了的按谱；没写但那里有片段的按片段的角色；都没有 = 待命
export function sceneRoles(chart, scene, beat = null) {
  const out = {};
  const n = chart.decks || 4;
  for (let d = 1; d <= n; d++) {
    let role = scene && scene.roles ? scene.roles[`D${d}`] : "";
    if (!role) {
      const c = (chart.clips || []).find((k) => k.deck === d && (beat !== null ? beat >= k.start && beat < k.stop
        : scene && k.start < scene.end && k.stop > scene.start));
      role = (c && c.role) || "";
    }
    out[d] = role || "待命";
  }
  return out;
}

// ------------------------------------------------------------------ 纵坐标

// EQ：+6 dB（5 点）在顶，0 dB（12 点）是参考线，切到底（7 点 = -25 dB，以及更低的）在底
export const EQ_TOP = 6, EQ_FLOOR = -26, EQ_CUT = -25;
const clamp01 = (x) => Math.max(0, Math.min(1, x));

// 值 → 行里的高度（0 = 行底，1 = 行顶）。Filter 中线 = 关，上 = 右（高通），下 = 左（低通）；推子 0..10
export function frac(param, v) {
  if (param === "vol") return clamp01(v);
  if (param === "filter") return clamp01((v + 1) / 2);
  const x = v <= EQ_CUT ? EQ_FLOOR : Math.min(EQ_TOP, v);
  return (x - EQ_FLOOR) / (EQ_TOP - EQ_FLOOR);
}

// 每行的参考线和两头的刻度字
export const AXIS = {
  lo: { ref: 0, refText: "12点", top: "", bottom: "切" },
  mid: { ref: 0, refText: "12点", top: "", bottom: "切" },
  hi: { ref: 0, refText: "12点", top: "", bottom: "切" },
  filter: { ref: 0, refText: "关", top: "右", bottom: "左" },
  vol: { ref: null, refText: "", top: "10", bottom: "0" },
};

// ------------------------------------------------------------------ 画笔

function rgba(color, a) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((color || "").trim());
  if (!m) return color;
  let h = m[1];
  if (h.length === 3) h = h.replace(/./g, (c) => c + c);
  const n = parseInt(h, 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}
export const withAlpha = rgba;

let measureCtx = null;
class Painter {
  font(size, bold) { return `${bold ? "600 " : ""}${size}px ${SANS}`; }
  textWidth(str, size, bold = false) {
    if (!measureCtx) measureCtx = document.createElement("canvas").getContext("2d");
    measureCtx.font = this.font(size, bold);
    return measureCtx.measureText(str).width;
  }
  // 放不下就截短，末尾加省略号；一个字都放不下 = 空
  fit(str, maxW, size, bold = false) {
    if (maxW <= 0) return "";
    if (this.textWidth(str, size, bold) <= maxW) return str;
    let lo = 0, hi = str.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.textWidth(str.slice(0, mid) + "…", size, bold) <= maxW) lo = mid; else hi = mid - 1;
    }
    return lo > 0 ? str.slice(0, lo) + "…" : "";
  }
}

// o：{ fill, fa（填充透明度）, stroke, sa, lw, dash, r }；文字：{ fill, size, bold, align, base, alpha }
export class CanvasPainter extends Painter {
  constructor(ctx) { super(); this.g = ctx; }
  _stroke(o) {
    const g = this.g;
    g.globalAlpha = o.sa === undefined ? 1 : o.sa;
    g.strokeStyle = o.stroke;
    g.lineWidth = o.lw || 1;
    g.setLineDash(o.dash || []);
    g.stroke();
    g.setLineDash([]);
    g.globalAlpha = 1;
  }
  rect(x, y, w, h, o = {}) {
    const g = this.g;
    if (w <= 0 || h <= 0) return;
    g.beginPath();
    if (o.r && g.roundRect) g.roundRect(x, y, w, h, Math.min(o.r, w / 2, h / 2)); else g.rect(x, y, w, h);
    if (o.fill) { g.globalAlpha = o.fa === undefined ? 1 : o.fa; g.fillStyle = o.fill; g.fill(); g.globalAlpha = 1; }
    if (o.stroke) this._stroke(o);
  }
  line(x1, y1, x2, y2, o = {}) {
    const g = this.g, lw = o.lw || 1;
    if (lw <= 1 && !(o.dash && o.dash.length) && (x1 === x2 || y1 === y2)) { // 细的横竖线贴到像素上，不发虚
      g.globalAlpha = o.sa === undefined ? 1 : o.sa;
      g.fillStyle = o.stroke;
      if (x1 === x2) g.fillRect(Math.round(x1), Math.min(y1, y2), 1, Math.abs(y2 - y1));
      else g.fillRect(Math.min(x1, x2), Math.round(y1), Math.abs(x2 - x1), 1);
      g.globalAlpha = 1;
      return;
    }
    g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2);
    this._stroke(o);
  }
  poly(pts, o = {}) {
    if (pts.length < 2) return;
    const g = this.g;
    g.beginPath();
    pts.forEach((p, i) => (i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])));
    g.lineJoin = "round";
    this._stroke(o);
  }
  circle(cx, cy, r, o = {}) {
    const g = this.g;
    g.beginPath(); g.arc(cx, cy, r, 0, Math.PI * 2);
    if (o.fill) { g.globalAlpha = o.fa === undefined ? 1 : o.fa; g.fillStyle = o.fill; g.fill(); g.globalAlpha = 1; }
    if (o.stroke) this._stroke(o);
  }
  text(str, x, y, o = {}) {
    const g = this.g;
    g.font = this.font(o.size || 11, o.bold);
    g.textAlign = o.align || "left";
    g.textBaseline = o.base || "middle";
    g.globalAlpha = o.alpha === undefined ? 1 : o.alpha;
    g.fillStyle = o.fill || "#fff";
    g.fillText(str, x, y);
    g.globalAlpha = 1;
  }
  clip(x, y, w, h) { const g = this.g; g.save(); g.beginPath(); g.rect(x, y, w, h); g.clip(); }
  unclip() { this.g.restore(); }
}

let clipSeq = 0;
export class SvgPainter extends Painter {
  constructor(svg) { super(); this.root = svg; this.cur = svg; this.stack = []; }
  _el(tag, attrs) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null && v !== "") e.setAttribute(k, v);
    this.cur.append(e);
    return e;
  }
  _paint(o) {
    return {
      fill: o.fill || "none", "fill-opacity": o.fill && o.fa !== undefined ? o.fa : undefined,
      stroke: o.stroke, "stroke-opacity": o.stroke && o.sa !== undefined ? o.sa : undefined,
      "stroke-width": o.stroke ? o.lw || 1 : undefined,
      "stroke-dasharray": o.stroke && o.dash && o.dash.length ? o.dash.join(" ") : undefined,
    };
  }
  rect(x, y, w, h, o = {}) {
    if (w <= 0 || h <= 0) return;
    this._el("rect", { x: r2(x), y: r2(y), width: r2(w), height: r2(h), rx: o.r ? Math.min(o.r, w / 2, h / 2) : undefined, ...this._paint(o) });
  }
  line(x1, y1, x2, y2, o = {}) {
    this._el("line", { x1: r2(x1), y1: r2(y1), x2: r2(x2), y2: r2(y2), ...this._paint({ ...o, fill: null }),
      "shape-rendering": (o.lw || 1) <= 1 && (x1 === x2 || y1 === y2) ? "crispEdges" : undefined });
  }
  poly(pts, o = {}) {
    if (pts.length < 2) return;
    this._el("polyline", { points: pts.map((p) => `${r2(p[0])},${r2(p[1])}`).join(" "), "stroke-linejoin": "round", ...this._paint({ ...o, fill: null }) });
  }
  circle(cx, cy, r, o = {}) { this._el("circle", { cx: r2(cx), cy: r2(cy), r, ...this._paint(o) }); }
  text(str, x, y, o = {}) {
    const e = this._el("text", { x: r2(x), y: r2(y), "font-size": o.size || 12, "font-weight": o.bold ? 600 : undefined,
      "font-family": SANS, fill: o.fill || "#000", "fill-opacity": o.alpha,
      "text-anchor": { center: "middle", right: "end" }[o.align] || undefined,
      "dominant-baseline": { middle: "central", top: "hanging", bottom: "text-after-edge" }[o.base || "middle"] });
    e.textContent = str;
  }
  clip(x, y, w, h) {
    const id = `c5clip${++clipSeq}`;
    const cp = this._el("clipPath", { id });
    const r = document.createElementNS(NS, "rect");
    for (const [k, v] of Object.entries({ x: r2(x), y: r2(y), width: r2(w), height: r2(h) })) r.setAttribute(k, v);
    cp.append(r);
    const g = this._el("g", { "clip-path": `url(#${id})` });
    this.stack.push(this.cur);
    this.cur = g;
  }
  unclip() { this.cur = this.stack.pop() || this.root; }
}
const r2 = (v) => Math.round(v * 100) / 100;

// ------------------------------------------------------------------ 尺寸、配色

// 演出页最小字 11px；交接说明是按 A4 横版 1 单位 = 1px = 0.75pt 画的，图里的字 ≥ 12px（9pt）
export const METRICS = {
  live: { fs: 11, fsB: 12, scene: 22, ruler: 24, deck: 46, param: 48, band: 22, gap: 6, pad: 8, lw: 2, chip: 14 },
  print: { fs: 12, fsB: 12, scene: 18, ruler: 22, deck: 42, param: 46, band: 21, gap: 4, pad: 7, lw: 1.8, chip: 15 },
};

export const PRINT_THEME = {
  plot: "#ffffff", text: "#111111", dim: "#555555", grid: "rgba(0,0,0,.10)", gridStrong: "rgba(0,0,0,.32)",
  sceneLine: "rgba(0,0,0,.85)", rowAlt: "rgba(0,0,0,.035)", deck: DECK_PRINT, chipInk: "#ffffff", neutral: "#555555",
  bandFill: 0.14, bandFillAlt: 0.3,
};

// 演出页的颜色跟着 style.css 的变量走（换主题不用改这里）
export function themeFromCss(el) {
  const cs = getComputedStyle(el);
  const v = (name, fb) => (cs.getPropertyValue(name) || "").trim() || fb;
  const text = v("--text", "#e6e8ee");
  return {
    plot: v("--l5-plot", "#0b0d11"), text, dim: v("--dim", "#9aa1b1"),
    grid: rgba(text, 0.07), gridStrong: rgba(text, 0.2), sceneLine: rgba(text, 0.65), rowAlt: rgba(text, 0.035),
    deck: { 1: v("--d1", DECK_DARK[1]), 2: v("--d2", DECK_DARK[2]), 3: v("--d3", DECK_DARK[3]), 4: v("--d4", DECK_DARK[4]) },
    chipInk: v("--l5-chip-ink", "#0b0d11"), neutral: v("--dim", "#9aa1b1"), accent: v("--accent", "#4fa3ff"),
    ok: v("--ok", "#3ccf7a"), warn: v("--warn", "#f2b84b"), err: v("--err", "#ff5d5d"),
    bandFill: 0.2, bandFillAlt: 0.38,
  };
}

// 行的排法（自上而下，全部共用一条全场小节轴）：场景 / 速度 → 小节标尺 → D1–D4 → LOW / MID / HIGH / Filter / 推子 → 效果 → Loop
export function layout(chart, M, nFx, nLoop) {
  const rows = [];
  let y = 0;
  const add = (kind, h, extra = {}) => { rows.push({ kind, y, h, ...extra }); y += h; };
  add("scene", M.scene);
  add("ruler", M.ruler);
  y += M.gap;
  const top = y;
  const n = Math.min(4, chart.decks || 4);
  for (let d = 1; d <= n; d++) add("deck", M.deck, { deck: d });
  y += M.gap;
  for (const p of PARAMS) add("param", M.param, { param: p });
  if (nFx || nLoop) y += M.gap;
  if (nFx) add("fx", nFx * M.band + 4, { lanes: nFx });
  if (nLoop) add("loop", nLoop * M.band + 4, { lanes: nLoop });
  return { rows, height: y, top };
}

// 小节号隔几个小节标一个（标得下就每小节都标）
export function rulerStep(p, ppb, M) {
  const need = p.textWidth("888", M.fs) + 9;
  for (const s of [1, 2, 4, 8, 16, 32, 64]) if (s * BEATS * ppb >= need) return s;
  return 128;
}

// ------------------------------------------------------------------ 效果 / Loop 带：排到不打架的行里

const MAX_BAND_LANES = { fx: 3, loop: 2 };

// 返回 { fx: [...], loop: [...], nFx, nLoop }；每项 { a, b, tail, decks, label, sub, lane, showLabel, labelW, ref }。
// range = [b0, b1]：只排这一段里的（交接说明的一条）；不给 = 整场（演出页，滚动时行不会变）。
// 带子本身不重叠就放同一行；字写不下、又会压到后面带子的不写（悬停 / 动作表里有全部说明）。
export function packBands(p, chart, ppb, M, range = null) {
  const inRange = (a, b) => !range || (a < range[1] && b > range[0]);
  const fx = [], loop = [];
  for (const f of chart.fx || []) {
    const tail = f.tail || 0;
    if (!inRange(f.start, f.end + tail)) continue;
    fx.push({ kind: "fx", a: f.start, b: f.end, tail, decks: f.decks || [], label: `${f.id} ${f.name || f.kind || ""}`.trim(),
      sub: [f.route_zh, f.scope].filter(Boolean).join(" · "), ref: f });
  }
  for (const c of chart.clips || []) {
    for (const s of c.segs || []) {
      if (!(s.loop > 0) || s.g1 <= s.g0 || !inRange(s.g0, s.g1)) continue;
      loop.push({ kind: "loop", a: s.g0, b: s.g1, tail: 0, decks: [c.deck], label: `${s.id} loop ${durText(s.loop)}`,
        sub: `${c.id}${c.role ? " " + c.role : ""}`, ref: s, clip: c });
    }
  }
  const pack = (items, cap) => {
    items.sort((x, y) => x.a - y.a || x.b - y.b);
    const rectEnd = [], labelEnd = [], last = [];
    const xe = range ? range[1] * ppb : Infinity;
    for (const it of items) {
      const a = range ? Math.max(it.a, range[0]) : it.a;
      const x0 = a * ppb, xr = Math.min(xe, (it.b + it.tail) * ppb);
      const lw = (M.chip + 1) * it.decks.length + 7 + p.textWidth(it.label, M.fs, true) + (it.sub ? p.textWidth(" " + it.sub, M.fs) + 4 : 0) + 6;
      const lx0 = Math.min(x0, xe - lw); // 一条的末尾放不下：字往左挪，贴着右边写完
      it.labelW = lw;
      it.showLabel = true;
      const rectFree = (i) => rectEnd[i] <= x0 + 0.5;
      let lane = rectEnd.findIndex((e, i) => rectFree(i) && labelEnd[i] + 6 <= lx0);
      if (lane < 0 && rectEnd.length < cap) { lane = rectEnd.length; rectEnd.push(0); labelEnd.push(0); last.push(null); }
      if (lane < 0) {
        lane = rectEnd.findIndex((e, i) => rectFree(i));
        if (lane < 0) { lane = rectEnd.indexOf(Math.min(...rectEnd)); it.showLabel = false; } // 同时在响的比行数还多：挤着画，不写字
        else if (lx0 < x0) it.showLabel = false; // 字只能压到前一条带子上：不写（动作表里有）
        else if (last[lane] && last[lane].labelX1 + 6 > x0) last[lane].showLabel = false; // 前一条的字伸到这条带子上了：前一条不写字
      }
      it.lane = lane;
      it.labelX0 = lx0;
      it.labelX1 = lx0 + lw;
      rectEnd[lane] = Math.max(rectEnd[lane], xr);
      labelEnd[lane] = it.showLabel ? Math.max(xr, lx0 + lw) : rectEnd[lane];
      if (it.showLabel) last[lane] = it;
    }
    return rectEnd.length;
  };
  const nFx = pack(fx, MAX_BAND_LANES.fx), nLoop = pack(loop, MAX_BAND_LANES.loop);
  return { fx, loop, nFx, nLoop };
}

// ------------------------------------------------------------------ 参数行上的值标签：先放要紧的，会撞的不放

// 返回 [{ x, y, w, h, text, deck, ax, ay }]，x 是「全场拍 × ppb」的坐标（画的时候再平移），y 是相对行顶的。
// 要紧的次序：到位的点（斜线的终点、跳变的目标）> 片段进来时的初值 > 斜线的起点。只是「保持」的中间点不标。
export function placeLaneLabels(p, chart, param, ppb, M, rowH, range = null) {
  const cands = [];
  const yv = (v) => M.pad + (1 - frac(param, v)) * (rowH - 2 * M.pad);
  for (const c of chart.clips || []) {
    if (range && (c.stop <= range[0] || c.start >= range[1])) continue;
    const pts = (c.lanes || {})[param] || [];
    pts.forEach((pt, i) => {
      if (range && (pt.at < range[0] || pt.at > range[1])) return;
      const prev = pts[i - 1], next = pts[i + 1];
      let prio;
      if (i === 0) prio = 1;
      else if (prev.v !== pt.v) prio = 0;
      else if (next && next.v !== pt.v && !next.step) prio = 2;
      else return;
      cands.push({ at: pt.at, v: pt.v, text: pt.text, deck: c.deck, prio, left: prio === 2 || (range && pt.at >= range[1] - 1e-6) });
    });
  }
  cands.sort((a, b) => a.prio - b.prio || a.at - b.at || a.deck - b.deck);
  const placed = [];
  const h = M.fs + 3;
  const hits = (b) => placed.some((q) => b.x < q.x + q.w + 4 && q.x < b.x + b.w + 4 && b.y < q.y + q.h + 1 && q.y < b.y + b.h + 1);
  const lo = range ? range[0] * ppb : -Infinity, hi = range ? range[1] * ppb : Infinity;
  for (const c of cands) {
    const w = M.chip + 3 + p.textWidth(c.text, M.fs);
    const ax = c.at * ppb, ay = yv(c.v) + deckOffset(c.deck);
    const xr = ax + 5, xl = ax - 5 - w;
    const ya = ay - 5 - h, yb = ay + 5;
    const order = c.left ? [[xl, ya], [xl, yb], [xr, ya], [xr, yb]] : [[xr, ya], [xr, yb], [xl, ya], [xl, yb]];
    for (const [x, y] of order) {
      const b = { x, y, w, h };
      if (y < 1 || y + h > rowH - 1 || x < lo + 1 || x + w > hi - 1 || hits(b)) continue;
      placed.push({ ...b, text: c.text, deck: c.deck, ax, ay });
      break;
    }
  }
  return placed;
}

// 几台 Deck 的线叠在同一行、值又一样时会完全盖住：每台错开一点点（1 个多像素），看得出是几条
export const deckOffset = (d) => (d - 2.5) * 1.4;

// ------------------------------------------------------------------ 画图

export function drawChip(p, x, cy, d, T, M) {
  const s = M.chip;
  p.rect(x, cy - s / 2, s, s, { fill: T.deck[d] || T.neutral, r: 3 });
  p.text(String(d), x + s / 2, cy + 0.5, { fill: T.chipInk, size: s - 3, bold: true, align: "center" });
}

// G：{ x0, w, b0, b1, ppb, rows, height }；opt：{ labels: {param: [...]}, bands, step（小节号间隔）, anchor（从哪个小节起数间隔）,
//      sceneNames（场景行写不写场景名）, range（只画这一段：交接说明的一条）,
//      repeat（像素：长带子的名字每隔这么远再写一遍，演出页滚动时总有一个在画面里） }
export function drawChart(p, chart, G, T, M, opt = {}) {
  const X = (beat) => G.x0 + (beat - G.b0) * G.ppb;
  const step = opt.step || rulerStep(p, G.ppb, M);
  const anchor = opt.anchor || 1;
  const rows = G.rows;
  const ruler = rows.find((r) => r.kind === "ruler");
  const bodyTop = ruler.y + ruler.h;
  const barPx = G.ppb * BEATS;
  const bar0 = Math.max(1, Math.floor(G.b0 / BEATS) + 1), bar1 = Math.ceil(G.b1 / BEATS) + 1;
  const lastBar = Math.round((chart.length || 0) / BEATS) + 1;
  const isLabelled = (bar) => ((bar - anchor) % step + step) % step === 0;

  p.rect(G.x0, 0, G.w, G.height, { fill: T.plot });
  p.clip(G.x0, 0, G.w, G.height);

  // 行底：隔一行深一点，行与行分得开
  for (const r of rows) {
    if ((r.kind === "deck" && r.deck % 2 === 0) || (r.kind === "param" && PARAMS.indexOf(r.param) % 2 === 1)) {
      p.rect(G.x0, r.y, G.w, r.h, { fill: T.rowAlt });
    }
  }
  // 整场之外（开演前、结束后）盖一层，知道那里没有谱
  const xEnd = X(chart.length || 0), xStart = X(0);
  if (xStart > G.x0) p.rect(G.x0, bodyTop, xStart - G.x0, G.height - bodyTop, { fill: T.rowAlt });
  if (xEnd < G.x0 + G.w) p.rect(xEnd, bodyTop, G.x0 + G.w - xEnd, G.height - bodyTop, { fill: T.rowAlt });

  // 小节线：贯穿所有行
  for (let bar = bar0; bar <= Math.min(bar1, lastBar); bar++) {
    const x = X((bar - 1) * BEATS);
    if (x < G.x0 - 1 || x > G.x0 + G.w + 1) continue;
    const strong = isLabelled(bar);
    if (!strong && barPx < 7) continue;
    p.line(x, strong ? ruler.y + ruler.h - 7 : bodyTop, x, G.height, { stroke: strong ? T.gridStrong : T.grid });
    if (strong) p.text(String(bar), x + 4, ruler.y + ruler.h / 2 - 1, { fill: T.text, size: M.fs, bold: true });
  }
  p.line(G.x0, bodyTop, G.x0 + G.w, bodyTop, { stroke: T.gridStrong });

  drawSceneRow(p, chart, G, T, M, rows.find((r) => r.kind === "scene"), X, opt);
  // 场景的分界：一条重线从标尺贯到底
  for (const s of chart.scenes || []) {
    for (const b of [s.start, s.end]) {
      const x = X(b);
      if (x >= G.x0 - 1 && x <= G.x0 + G.w + 1) p.line(x, 0, x, G.height, { stroke: T.sceneLine, lw: 1.5 });
    }
  }

  for (const r of rows) {
    if (r.kind === "deck") drawDeckRow(p, chart, G, T, M, r, X, step, anchor, opt);
    else if (r.kind === "param") drawParamRow(p, chart, G, T, M, r, X, (opt.labels || {})[r.param] || []);
    else if (r.kind === "fx" || r.kind === "loop") drawBandRow(p, G, T, M, r, X, ((opt.bands || {})[r.kind]) || [], opt);
  }
  p.unclip();
}

function drawSceneRow(p, chart, G, T, M, row, X, opt) {
  if (!row || !row.h) return;
  const cy = row.y + row.h / 2;
  const boxes = [];
  const free = (x, w) => !boxes.some((b) => x < b[1] + 8 && b[0] < x + w + 8);
  if (opt.sceneNames !== false) {
    (chart.scenes || []).forEach((s, i) => {
      if (s.end <= G.b0 || s.start >= G.b1) return;
      const x1 = X(s.start), x2 = X(s.end);
      if (i % 2 === 1) p.rect(x1, row.y, x2 - x1, row.h, { fill: T.rowAlt });
      const name = `场景 ${s.no}${s.name ? " " + s.name : ""}${s.draft ? "（草稿）" : ""}`;
      const t = p.fit(name, x2 - x1 - 12, M.fsB, true);
      if (t) { p.text(t, x1 + 7, cy, { fill: T.text, size: M.fsB, bold: true }); boxes.push([x1 + 7, x1 + 7 + p.textWidth(t, M.fsB, true)]); }
    });
  } else {
    const tp0 = chart.tempo || [];
    const k = tp0.findIndex((b, i) => i > 0 && tp0[i - 1].at <= G.b0 && b.at > G.b0 && Math.abs(b.bpm - tp0[i - 1].bpm) > 1e-9);
    const t = `主速 ${fmtBpm(tempoAt(chart, G.b0))}` + (k > 0 ? ` → ${fmtBpm(tp0[k].bpm)}（渐变，到 ${posText(tp0[k].at)}）` : "");
    p.text(t, G.x0 + 5, cy - 1, { fill: T.text, size: M.fs });
    boxes.push([G.x0 + 5, G.x0 + 5 + p.textWidth(t, M.fs)]);
  }
  // 主速变化：行底一条细线（两头有小竖线）标出从哪到哪在变，旁边写「主速 132 → 134」
  const tp = chart.tempo || [];
  for (let i = 1; i < tp.length; i++) {
    const a = tp[i - 1], b = tp[i];
    if (Math.abs(a.bpm - b.bpm) < 1e-9 || b.at <= G.b0 || a.at >= G.b1) continue;
    const x1 = X(a.at), x2 = X(b.at), yl = row.y + row.h - 1.5;
    p.line(Math.max(x1, G.x0), yl, Math.min(x2, G.x0 + G.w), yl, { stroke: T.dim, lw: 1.5 });
    for (const xe of [x1, x2]) if (xe >= G.x0 && xe <= G.x0 + G.w) p.line(xe, yl - 4, xe, yl + 1, { stroke: T.dim, lw: 1.5 });
    const t = b.at > a.at ? `主速 ${fmtBpm(a.bpm)} → ${fmtBpm(b.bpm)}` : `主速换到 ${fmtBpm(b.bpm)}`;
    const w = p.textWidth(t, M.fs);
    const x = Math.max(x1, G.x0) + 6;
    if (free(x, w) && x + w < G.x0 + G.w) { p.text(t, x, cy - 1, { fill: T.dim, size: M.fs }); boxes.push([x, x + w]); }
  }
}

function drawDeckRow(p, chart, G, T, M, row, X, step, anchor, opt) {
  const range = opt.range || null, repeat = opt.repeat || 0;
  const d = row.deck, col = T.deck[d] || T.neutral, ds = deckStyle(d);
  const y = row.y + 3, h = row.h - 6;
  const yTop = y + h * 0.3, yBot = y + h * 0.74;
  for (const c of chart.clips || []) {
    if (c.deck !== d) continue;
    const load = c.load === undefined || c.load === null ? c.start : Math.min(c.load, c.start);
    if (c.stop <= G.b0 || load >= G.b1) continue;
    const x1 = X(c.start), x2 = X(c.stop);
    // 起播前的准备（装载、摆旋钮）：还没出声，只画一个空框
    if (load < c.start) {
      const xl = X(load);
      p.rect(xl, y + h * 0.25, x1 - xl, h * 0.5, { stroke: col, sa: 0.7, lw: 1, dash: [2, 2], r: 3 });
      const t = p.fit("准备", x1 - xl - 6, M.fs);
      if (t) p.text(t, xl + 4, y + h / 2, { fill: T.dim, size: M.fs });
    }
    // 播放带：线型跟 Deck 走
    p.rect(x1, y, x2 - x1, h, { fill: col, fa: T.bandFill, stroke: col, lw: 1.5, dash: ds.dash, r: 4 });
    p.rect(x1, y, 3, h, { fill: col });
    // loop 段：一圈一个小格
    for (const s of c.segs || []) {
      if (!(s.loop > 0) || s.g1 <= s.g0 || s.g1 <= G.b0 || s.g0 >= G.b1) continue;
      const cell = s.loop * G.ppb;
      const n = Math.ceil((s.g1 - s.g0) / s.loop - 1e-9);
      if (cell >= 5) {
        for (let k = 0; k < n; k++) {
          const a = s.g0 + k * s.loop, b = Math.min(s.g1, a + s.loop);
          if (b <= G.b0 || a >= G.b1) continue;
          if (k % 2 === 0) p.rect(X(a) + 1, y + 2, X(b) - X(a) - 1, h - 4, { fill: col, fa: T.bandFillAlt - T.bandFill, r: 2 });
          if (k > 0) p.line(X(a), y + 1, X(a), y + h - 1, { stroke: col, sa: 0.9 });
        }
      } else {
        p.rect(X(s.g0), y + 2, X(s.g1) - X(s.g0), h - 4, { fill: col, fa: T.bandFillAlt - T.bandFill });
      }
    }
    const cx1 = Math.max(x1, G.x0), cx2 = Math.min(x2, G.x0 + G.w);
    if (cx2 - cx1 < 4) continue; // 只有起播前的准备那一段在画面里
    p.clip(cx1, y, cx2 - cx1, h);
    // 上面一行：片段 ID + 曲名（+ 角色），后面是 loop 的圈长
    const top = [];
    const freeTop = (x, w) => !top.some((b) => x < b[1] + 8 && b[0] < x + w + 8);
    const idW = p.textWidth(c.id + " ", M.fsB, true);
    const role = c.role ? ` · ${c.role}` : "";
    const name = c.title || c.asset || "";
    const fullW = idW + p.textWidth(name + role, M.fs);
    const at = [x1];
    if (repeat > 0) for (let x = x1 + repeat; x + fullW + 24 < x2; x += repeat) at.push(x);
    if (range && x1 < X(range[0]) - 1) at.push(X(range[0]));
    for (const xa of at) {
      if (xa + 8 > cx2 || xa + 8 + fullW < cx1) continue;
      const room = x2 - xa - 14 - idW;
      let title = p.fit(name + role, room, M.fs);
      if (role && title.endsWith("…")) title = p.fit(name, room, M.fs);
      p.text(c.id, xa + 8, yTop, { fill: T.text, size: M.fsB, bold: true });
      if (title) p.text(title, xa + 8 + idW, yTop, { fill: T.text, size: M.fs });
      top.push([xa + 8, xa + 8 + idW + (title ? p.textWidth(title, M.fs) : 0)]);
    }
    for (const s of c.segs || []) {
      if (!(s.loop > 0) || s.g1 <= s.g0) continue;
      const laps = (s.g1 - s.g0) / s.loop;
      const full = `loop ${durText(s.loop)} ×${Math.abs(laps - Math.round(laps)) < 1e-6 ? Math.round(laps) : laps.toFixed(1)}`;
      for (const t of [full, `loop ${durText(s.loop)}`]) {
        const w = p.textWidth(t, M.fs);
        let x = Math.max(X(s.g0), range ? X(range[0]) : -Infinity) + 6;
        if (!freeTop(x, w)) { const hit = top.find((b) => x < b[1] + 8 && b[0] < x + w + 8); x = hit[1] + 10; } // 和名字撞了：接在名字后面
        if (x + w <= X(s.g1) - 3 && freeTop(x, w)) { p.text(t, x, yTop, { fill: T.text, size: M.fs }); top.push([x, x + w]); break; }
      }
    }
    // 下面一行：原曲的小节号（每个片段各数各的）。先标起点和每次接到别处的地方，再在标了号的全场小节线上标
    const bot = [];
    const put = (x, t, bold) => {
      const w = p.textWidth(t, M.fs, bold);
      if (x + w > x2 - 3 || bot.some((b) => x < b[1] + 7 && b[0] < x + w + 7)) return;
      p.text(t, x, yBot, { fill: bold ? T.text : T.dim, size: M.fs, bold });
      bot.push([x, x + w]);
    };
    const segs = (c.segs || []).filter((s) => s.g1 > s.g0);
    if (range && c.start < range[0]) { // 这一条从片段中间开始：开头先写这里读到原曲哪一小节
      const src = srcAt(c, range[0]);
      if (src !== null) put(X(range[0]) + 8, "原曲 " + srcText(src), true);
    }
    segs.forEach((s, i) => {
      const lead = i === 0 ? "原曲 " : s.kind === "loop" ? "loop " : s.kind === "exit" ? "出 loop " : s.kind === "jump" ? "跳到 " : "";
      if (i > 0 && s.kind !== "loop") p.line(X(s.g0), y + 1, X(s.g0), y + h - 1, { stroke: col, lw: 1.5 });
      put(X(s.g0) + (i === 0 ? 8 : 5), lead + srcText(s.src), true);
    });
    const from = Math.max(c.start, G.b0), to = Math.min(c.stop, G.b1);
    for (let bar = Math.floor(from / BEATS) + 1; (bar - 1) * BEATS < to; bar++) {
      const g = (bar - 1) * BEATS;
      if (g <= c.start || g >= c.stop || ((bar - anchor) % step + step) % step !== 0) continue;
      const src = srcAt(c, g);
      if (src !== null) put(X(g) + 4, srcText(src), false);
    }
    p.unclip();
  }
}

function drawParamRow(p, chart, G, T, M, row, X, labels) {
  const param = row.param, ax = AXIS[param];
  const yv = (v) => row.y + M.pad + (1 - frac(param, v)) * (row.h - 2 * M.pad);
  p.line(G.x0, row.y, G.x0 + G.w, row.y, { stroke: T.grid });
  if (ax.ref !== null) p.line(G.x0, yv(ax.ref), G.x0 + G.w, yv(ax.ref), { stroke: T.dim, sa: 0.55, dash: [1, 4] });
  for (const c of chart.clips || []) {
    if (c.stop <= G.b0 || c.start >= G.b1) continue;
    const col = T.deck[c.deck] || T.neutral, off = deckOffset(c.deck);
    const pts = lanePoints(c, param);
    // 只留画面附近的点（两头各多留一个，线才接得上）
    let i0 = 0, i1 = pts.length - 1;
    while (i0 + 1 < pts.length && pts[i0 + 1][0] < G.b0) i0++;
    while (i1 > 0 && pts[i1 - 1][0] > G.b1) i1--;
    const seg = pts.slice(i0, i1 + 1).map(([b, v]) => [X(b), yv(v) + off]);
    p.poly(seg, { stroke: col, lw: M.lw, dash: deckStyle(c.deck).dash });
    // 关键点：值变了的地方点一个小圆点
    const raw = (c.lanes || {})[param] || [];
    raw.forEach((pt, i) => {
      if (pt.at < G.b0 || pt.at > G.b1) return;
      const prev = raw[i - 1], next = raw[i + 1];
      if (i === 0 || prev.v !== pt.v || (next && next.v !== pt.v && !next.step)) p.circle(X(pt.at), yv(pt.v) + off, 2.6, { fill: col });
    });
  }
  const dx = G.x0 - G.b0 * G.ppb;
  for (const l of labels) {
    const x = l.x + dx;
    if (x + l.w < G.x0 || x > G.x0 + G.w) continue;
    const cy = row.y + l.y + l.h / 2;
    p.rect(x - 2, row.y + l.y - 1, l.w + 4, l.h + 2, { fill: T.plot, fa: 0.82, r: 3 });
    drawChip(p, x, cy, l.deck, T, M);
    p.text(l.text, x + M.chip + 3, cy + 0.5, { fill: T.text, size: M.fs });
  }
}

function drawBandRow(p, G, T, M, row, X, items, opt) {
  const range = opt.range || null, repeat = opt.repeat || 0;
  p.line(G.x0, row.y, G.x0 + G.w, row.y, { stroke: T.grid });
  for (const it of items) {
    if (it.b + it.tail <= G.b0 || it.a >= G.b1) continue;
    const y = row.y + 3 + it.lane * M.band, h = M.band - 4;
    const one = it.decks.length === 1 ? it.decks[0] : 0;
    const col = one ? T.deck[one] || T.neutral : T.neutral;
    const x1 = X(it.a), x2 = X(it.b);
    if (it.kind === "fx") {
      p.rect(x1, y, Math.max(2, x2 - x1), h, { fill: col, fa: 0.5, r: 3 });
      if (it.tail > 0) { // 尾音：送出关了以后还在响的那一段，淡色延长
        const x3 = X(it.b + it.tail);
        p.rect(x2, y, x3 - x2, h, { fill: col, fa: 0.16 });
        p.line(x3, y, x3, y + h, { stroke: col, sa: 0.7, dash: [2, 2] });
      }
    } else {
      p.rect(x1, y, x2 - x1, h, { fill: col, fa: 0.14, stroke: col, lw: 1.5, dash: deckStyle(one).dash, r: 3 });
    }
    if (!it.showLabel) continue;
    const cy = y + h / 2;
    const at = [it.labelX0 + G.x0 - G.b0 * G.ppb];
    if (repeat > 0) for (let x = x1 + repeat; x + it.labelW + 24 < x2; x += repeat) at.push(x);
    for (const xa of at) {
      if (xa > G.x0 + G.w || xa + it.labelW < G.x0) continue;
      let x = xa + 4;
      for (const d of it.decks) { drawChip(p, x, cy, d, T, M); x += M.chip + 1; }
      x += 3;
      p.text(it.label, x, cy + 0.5, { fill: T.text, size: M.fs, bold: true });
      if (it.sub) p.text(it.sub, x + p.textWidth(it.label + " ", M.fs, true) + 2, cy + 0.5, { fill: T.text, size: M.fs, alpha: 0.85 });
    }
  }
}

// ------------------------------------------------------------------ 交接说明用：左边的行头（演出页的行头是 DOM，显示实际值）

// 每条都重复：Deck 的颜色 / 编号 / 线型 / 角色，和每个参数在这一条起点的值
export function drawGutter(p, chart, G, T, M, roles, gw) {
  const b0 = G.b0;
  p.rect(0, 0, gw, G.height, { fill: T.plot });
  for (const r of G.rows) {
    const cy = r.y + r.h / 2;
    if (r.kind === "scene") p.text("速度", 4, cy, { fill: T.dim, size: M.fs });
    else if (r.kind === "ruler") p.text("全场小节", 4, cy - 1, { fill: T.dim, size: M.fs });
    else if (r.kind === "deck") {
      const d = r.deck, col = T.deck[d] || T.neutral;
      drawChip(p, 4, r.y + r.h * 0.32, d, T, M);
      p.text(`D${d}`, 4 + M.chip + 4, r.y + r.h * 0.32 + 0.5, { fill: T.text, size: M.fsB, bold: true });
      p.line(48, r.y + r.h * 0.32, gw - 8, r.y + r.h * 0.32, { stroke: col, lw: 2.2, dash: deckStyle(d).dash });
      p.text(p.fit(roles[d] || "待命", gw - 12, M.fs), 4, r.y + r.h * 0.72, { fill: roles[d] && roles[d] !== "待命" ? T.text : T.dim, size: M.fs });
    } else if (r.kind === "param") {
      p.text(PARAM_ZH[r.param], 4, r.y + 10, { fill: T.text, size: M.fsB, bold: true });
      // 这一条起点时每台 Deck 的值（斜线中途写「从 → 到」）
      let x = 4, y = r.y + 10 + M.fs + 4, line = 0;
      for (let d = 1; d <= (chart.decks || 4); d++) {
        const c = clipAt(chart, d, b0);
        const v = c && laneAt(c, r.param, b0);
        if (!v) continue;
        const w = M.chip + 3 + p.textWidth(v.text, M.fs) + 7;
        if (x > 4 && x + w > gw - 2) { x = 4; y += M.fs + 3; line++; }
        if (line > 1) break;
        drawChip(p, x, y, d, T, M);
        p.text(v.text, x + M.chip + 3, y + 0.5, { fill: T.text, size: M.fs });
        x += w;
      }
      const ax = AXIS[r.param];
      const yv = (v) => r.y + M.pad + (1 - frac(r.param, v)) * (r.h - 2 * M.pad);
      if (ax.ref !== null) p.line(gw - 5, yv(ax.ref), gw, yv(ax.ref), { stroke: T.dim });
    } else if (r.kind === "fx") p.text("效果", 4, r.y + 3 + (M.band - 4) / 2, { fill: T.text, size: M.fsB, bold: true });
    else if (r.kind === "loop") p.text("Loop", 4, r.y + 3 + (M.band - 4) / 2, { fill: T.text, size: M.fsB, bold: true });
  }
  p.line(gw, 0, gw, G.height, { stroke: T.gridStrong });
}

// ------------------------------------------------------------------ 动作

// 这个场景里的动作（落在场景起点到终点之前的；正好在终点上的算下一个场景的）
export function sceneActions(chart, scene) {
  return (chart.actions || []).filter((a) => a.at >= scene.start && a.at < scene.end);
}

// 练习重点：只把谱上有的东西按类列出来——同一拍两台以上 Deck 一起动的、loop、效果、跳段；不编建议
export function practicePoints(chart, scene) {
  const acts = sceneActions(chart, scene);
  const same = [];
  const byAt = new Map();
  for (const a of acts) {
    if (a.kind === "mark" || a.kind === "load" || !a.deck) continue;
    if (!byAt.has(a.at)) byAt.set(a.at, []);
    byAt.get(a.at).push(a);
  }
  for (const [at, list] of byAt) {
    if (new Set(list.map((a) => a.deck)).size >= 2) same.push({ at, pos: list[0].pos, items: list });
  }
  return {
    same,
    loops: acts.filter((a) => a.kind === "loop"),
    fx: acts.filter((a) => a.kind === "fx"),
    jumps: acts.filter((a) => a.kind === "jump"),
  };
}
