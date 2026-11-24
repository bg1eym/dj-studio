// 曲谱 v5 的演出界面：和两 Deck 的演出页（live.js）同一套东西，排成四台 Deck。
//
// 自上而下：全场位置 / 主速 / 当前场景 / 暂停、跳转 → 场景条 → 整首曲子（每台 Deck 一行：它现在放的、或者下一段要放的
// 那首原曲的整首波形，标出这套谱要放的那一段、hot cue、播放头；点哪从哪放）→ 每台 Deck 的滚动波形（播放头在正中）
// → 调音台（旋钮、推子、电平，拖动 = 手动接管）+ 现在的 EQ 操作 + 接下来的动作
// → 每台 Deck 的 EQ / Filter / 推子曲线（谱，带场景和全场小节的标尺、缩放）→ 当前场景的策略。
//
// - 波形是原曲的波形（蓝 = 低频、绿 = 中频、白 = 高频，wavedraw.js），片段放到哪、loop 转几圈，波形就照那样摆。
// - 旋钮、推子、电平、Deck 的状态、动作做完没有，全部来自引擎报的状态和回执；曲线是谱（编译出的 chart）。
//   游标在两次状态之间按速度外推（只有游标）。
// - 旋钮、推子、电平表就是 live.js 里那几个部件；这里只管四 Deck 的排法和全场小节轴。

import { drawTriBand, WAVE_COLORS } from "./wavedraw.js";
import { Knob, MiniKnob, Fader, Meter, meterScale, BAND_HEX, FX_HEX, drawSeekHover, drawHotCues, clockText } from "./live.js";
import {
  PARAM_ZH, BEATS, posText, posTextFine, barOf, durText, sceneAt, tempoAt, fmtBpm, clipAt, clipById, srcAt, laneAt, lanePoints,
  sceneRoles, EQ_CUT,
} from "./chart5.js";

const DECK_HEX = { 1: "#3d8bea", 2: "#dc7526", 3: "#27ae60", 4: "#9c7cec" };
const ZOOMS = [8, 16, 32, "scene"];
const ZOOM_ZH = { 8: "8 小节", 16: "16 小节", 32: "32 小节", scene: "整个场景" };
const MIN_BAR_PX = 18; // 再长的场景也不把一小节压到比这更窄：滚动，不缩
const PLAYHEAD_AT = 0.3; // 跟随时游标停在画面的 30% 处，右边 70% 是接下来的
const LEAD_IN = 8; // 开演前留两小节（装载、摆旋钮）
const SPAN_S = 7; // 每台 Deck 的滚动波形：播放头两侧各 7 秒（音乐里的时间），四台同一比例
const STRATEGY = ["作用", "保留", "前景", "LOW", "MID/HIGH"];
const TRACK_H = 62; // 整首曲子那一行的高（左边要写三行字：片段、第几小节 / 选用哪一段、要它的哪些元素）
const ARR = { left: 30, scene: 20, ho: 17, row: 18, gap: 2 }; // 整套 set 的安排：场景一行 + 主要的交接一行 + 每台 Deck 一行
const HO_HEX = "#f2b84b"; // 主要的交接（主线上一首接一首）
const LN = { scene: 20, ruler: 20, head: 22, row: 24, gap: 3, group: 8, band: 15 }; // 曲线每一行的高
const LANE_PARAMS = ["hi", "mid", "lo", "filter", "vol"]; // 和调音台上从上到下一样
const LANE_ZH = { hi: "HIGH", mid: "MID", lo: "LOW", filter: "FILTER", vol: "推子" };
const BG = "#0b0d11";
const STATE_ZH = {
  idle: "没在放", preparing: "准备中：装载、摆旋钮", prerun: "准备中：静音预运行", running: "", pausing: "暂停中…", paused: "已暂停",
  stopping: "停止中…", done: "整场放完了",
};

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  put(e, kids);
  return e;
}

// append 的空值安全版：数组摊平，null / false 跳过（原生 append 会把它们写成字）
function put(node, ...kids) {
  for (const c of kids.flat(Infinity)) if (c !== null && c !== undefined && c !== false) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
}

// 内容没变就不碰 DOM
function setText(node, text) {
  if (node._t !== text) { node._t = text; node.textContent = text; }
}
function setClass(node, cls) {
  if (node._c !== cls) { node._c = cls; node.className = cls; }
}

function chip(d) { return el("span", { class: `l5-chip d${d}` }, String(d)); }

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// ------------------------------------------------------------------ 单位（谱上的单位 ↔ 旋钮的钟点位 ↔ Mixxx 的原生值）

// EQ：谱上是 dB（-25 = 7 点，全切；0 = 12 点；+6 = 5 点），钟点位和 live.js 的 eqHours 是同一条曲线
export const eqHours = (db) => (db <= EQ_CUT ? -5 : db >= 0 ? Math.min(5, (db / 6) * 5) : -5 * Math.min(1, Math.pow(db / -25, 1 / 1.56)));
export const eqDb = (h) => (h <= -4.97 ? EQ_CUT : h >= 0 ? (Math.min(5, h) / 5) * 6 : -25 * Math.pow(-h / 5, 1.56));
// 曲线行里的纵坐标 0..1 = 旋钮转角（7 点在下、12 点在中间、5 点在上；Filter 中线 = 关；推子 = 推子位置）——和两 Deck 的时间线一样
export function laneFrac(param, v) {
  if (param === "vol") return clamp(v, 0, 1);
  if (param === "filter") return clamp((v + 1) / 2, 0, 1);
  return (eqHours(v) + 5) / 10;
}
// 谱上的单位 → live.js 的旋钮 / 推子吃的原生值（拖动时先照手上的值画，引擎报回来再换成实际的）
export function nativeOf(param, v) {
  if (param === "vol") return v <= 0 ? 0 : v >= 1 ? 1 : (Math.pow(10, v - 1) - 0.1) / 0.9;
  if (param === "filter") return 0.5 + clamp(v, -1, 1) / 2;
  return v <= EQ_CUT ? Math.pow(10, EQ_CUT / 20) : Math.pow(10, v / 20);
}
const laneColor = (param, d) => (param === "vol" ? DECK_HEX[d] : param === "filter" ? "#c9a6ff" : BAND_HEX[param]);

// 曲线上第 beat 拍的纵坐标（0..1）：两点之间在转角上走直线，和画出来的线是同一条
function laneFracAt(clip, param, beat) {
  const pts = (clip.lanes || {})[param] || [];
  if (!pts.length) return null;
  let i = 0;
  while (i + 1 < pts.length && pts[i + 1].at <= beat + 1e-9) i++;
  const a = pts[i], b = pts[i + 1];
  const fa = laneFrac(param, a.v);
  if (beat < a.at || !b || b.step || b.at <= a.at) return fa;
  return fa + (laneFrac(param, b.v) - fa) * ((beat - a.at) / (b.at - a.at));
}

// ------------------------------------------------------------------ 波形：全场第 g 拍这台 Deck 读到原曲哪里

// 原曲第 src 拍在文件的第几秒
const srcSec = (clip, src) => (clip.t0 || 0) + (src * 60) / clip.bpm;

// 全场 [g0, g1) 这一小段对应的波形（三段各取最大）；没有声音的地方（片段外、文件外）= null
export function waveCol(clip, wf, g0, g1) {
  if (g0 < clip.start - 1e-9 || g0 >= clip.stop) return null;
  const s = srcAt(clip, g0);
  if (s === null) return null;
  const ta = srcSec(clip, s), tb = ta + ((g1 - g0) * 60) / clip.bpm;
  let ia = Math.floor(ta * wf.rate), ib = Math.max(ia + 1, Math.floor(tb * wf.rate));
  ia = Math.max(0, ia); ib = Math.min(wf.n, ib);
  if (ia >= ib) return null;
  let lo = 0, mi = 0, hi = 0;
  for (let i = ia; i < ib; i++) {
    if (wf.low[i] > lo) lo = wf.low[i];
    if (wf.mid[i] > mi) mi = wf.mid[i];
    if (wf.high[i] > hi) hi = wf.high[i];
  }
  return [lo / 255, mi / 255, hi / 255];
}

// 一段里原曲的小节线落在全场哪些拍上：cb(全场拍, 原曲小节号, 是不是这一圈 / 这一段的头)
export function eachSrcBar(clip, gA, gB, cb, unit = BEATS) {
  for (const s of clip.segs || []) {
    if (!(s.g1 > s.g0) || s.g1 < gA || s.g0 > gB) continue;
    const laps = s.loop > 0 ? Math.ceil((s.g1 - s.g0) / s.loop - 1e-9) : 1;
    const j0 = s.loop > 0 ? Math.max(0, Math.floor((gA - s.g0) / s.loop)) : 0;
    for (let j = j0; j < laps; j++) {
      const lg0 = s.g0 + j * (s.loop || 0), lg1 = s.loop > 0 ? Math.min(s.g1, lg0 + s.loop) : s.g1;
      if (lg0 > gB) break;
      // 这一圈里原曲的第 k 个单位（小节 / 拍）：src = s.src + (g - lg0)
      const k0 = Math.ceil((s.src + Math.max(0, gA - lg0)) / unit - 1e-9);
      for (let k = k0; ; k++) {
        const g = lg0 + (k * unit - s.src);
        if (g > lg1 - 1e-9 || g > gB) break;
        if (g >= lg0 - 1e-9) cb(g, k, Math.abs(g - lg0) < 1e-9);
      }
    }
  }
}

// 全场第 beat 拍是开演后第几秒（音乐里的时间）。速度图：BPM 对全场拍的分段直线，同一拍两个点 = 一下换速；
// 匀速变速的一段按积分算（和编译器的 TempoMap.sec_at 是同一个公式）
export function secAt(tempo, beat) {
  const pts = tempo && tempo.length ? tempo : [{ at: 0, bpm: 120 }];
  const x = Math.max(0, beat);
  const seg = (v0, v1, L, u) => { // 这一段长 L 拍、速度从 v0 匀速变到 v1：走到第 u 拍用了几秒
    if (u <= 0) return 0;
    if (Math.abs(v1 - v0) < 1e-9 || L <= 0) return (u * 60) / v0;
    const k = (v1 - v0) / L;
    return (60 / k) * Math.log((v0 + k * u) / v0);
  };
  if (x <= pts[0].at) return (x * 60) / pts[0].bpm;
  let t = (pts[0].at * 60) / pts[0].bpm;
  for (let i = 0; i + 1 < pts.length; i++) {
    const a = pts[i], b = pts[i + 1], L = b.at - a.at;
    if (L <= 0) continue;
    if (x <= b.at) return t + seg(a.bpm, b.bpm, L, x - a.at);
    t += seg(a.bpm, b.bpm, L, L);
  }
  const last = pts[pts.length - 1];
  return t + ((x - last.at) * 60) / last.bpm;
}

// 秒 → 「3:12」（一小时以上「1:03:12」）
export function clock(sec) {
  const s = Math.max(0, Math.floor(sec + 1e-6)), m = Math.floor(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : `${m}:${String(s % 60).padStart(2, "0")}`;
}

// 原曲第 src 拍在这个片段里第一次放到是全场第几拍（这套谱不放这一段 = null）。loop：圈里的位置按第一圈算
export function globalOfSrc(clip, src) {
  for (const s of clip.segs || []) {
    if (!(s.g1 > s.g0)) continue;
    let g;
    if (s.loop > 0) {
      if (src < s.lsrc - 1e-9 || src >= s.lsrc + s.loop - 1e-9) continue;
      g = src >= s.src - 1e-9 ? s.g0 + (src - s.src) : s.g0 + (s.lsrc + s.loop - s.src) + (src - s.lsrc); // 从圈中间起播的：圈头要等转回来
    } else {
      if (src < s.src - 1e-9) continue;
      g = s.g0 + (src - s.src);
    }
    if (g < s.g1 - 1e-9) return Math.max(s.g0, g);
  }
  return null;
}

// 片段在原曲上用到的那几段（原曲的拍）：直放的一段、loop 的那一圈
export function clipSpans(clip) {
  const out = [];
  for (const s of clip.segs || []) {
    if (!(s.g1 > s.g0)) continue;
    out.push(s.loop > 0 ? { a: s.lsrc, b: s.lsrc + s.loop, loop: true, id: s.id, len: s.loop, g0: s.g0, laps: (s.g1 - s.g0) / s.loop }
      : { a: s.src, b: s.src + (s.g1 - s.g0), loop: false, id: s.id, kind: s.kind, g0: s.g0 });
  }
  return out;
}

// 这个片段要原曲的哪些元素（三个频段）：on = 一直在；off = 从头到尾切着（7 点）；in / out = 先切着后来放进来 / 先在后来切掉
export function clipBands(clip) {
  const out = {};
  for (const b of ["lo", "mid", "hi"]) {
    const pts = (clip.lanes || {})[b] || [];
    const cut = pts.map((p) => p.v <= EQ_CUT + 1e-6);
    out[b] = !pts.length || cut.every((c) => !c) ? "on" : cut.every((c) => c) ? "off" : cut[0] ? "in" : "out";
  }
  return out;
}

// 片段选用了原曲的哪一段（原曲的拍）：放到的最前面到最后面（直放的、loop 的那一圈都算）
export function clipPick(clip) {
  const sp = clipSpans(clip);
  if (!sp.length) return null;
  return { a: Math.min(...sp.map((x) => x.a)), b: Math.max(...sp.map((x) => x.b)) };
}

function fitText(g, text, maxW) {
  if (maxW <= 6) return "";
  if (g.measureText(text).width <= maxW) return text;
  let t = text;
  while (t.length > 1 && g.measureText(t + "…").width > maxW) t = t.slice(0, -1);
  return t.length > 1 ? t + "…" : "";
}

// 深色底上的小标签
function tag(g, text, x, y, color, right = false) {
  const tw = g.measureText(text).width;
  const lx = right ? x - tw - 6 : x + 4;
  g.fillStyle = "rgba(11,13,17,.82)";
  g.fillRect(lx - 3, y - 10, tw + 6, 13);
  g.fillStyle = color;
  g.fillText(text, lx, y);
}

function sizeCanvas(c, w, h) {
  const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); c.style.height = `${h}px`;
  }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return g;
}

export class Live5View {
  constructor(root, opts = {}) {
    this.root = root;
    root.classList.add("live5", "live");
    this.api = opts.api;
    this.onSeek = opts.onSeek || null;
    this.toast = opts.toast || (() => {});
    this.loadTrack = opts.loadTrack || null; // 曲库里的 id → Promise<波形>
    this.waves = new Map(); // 曲库 id → 波形（null = 还在下 / 没有）
    this.chart = null;
    this.engine = null;
    this.progress = null;
    this.zoom = 16;
    this.follow = true;
    try {
      const z = localStorage.getItem("djs.l5.zoom");
      if (z) this.zoom = z === "scene" ? z : ZOOMS.includes(+z) ? +z : 16;
    } catch { /* 无痕窗口 */ }
    this.b0 = -LEAD_IN;
    this.ppb = 20;
    this.W = 0; // 曲线那张图的宽：一屏几拍按它算
    this.restBeat = 0; // 没在放的时候游标停在哪
    this.shown = null;
    this.decks = [1, 2]; // 这份谱用到的 Deck
    this.pending = {}; // 接管：每个旋钮最后要发的值（节流）
    this.localManual = {}; // 刚拖过、引擎还没报回来的
    this.localValue = {}; // 刚拖到的值（引擎报回来之前旋钮先照它画）
    this.returning = {};
    this.showPast = false;
    this._frame = this._frame.bind(this);
    this._build();
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(this.lplot);
    this._onDocDown = (ev) => { if (this.bubble && !this.bubble.contains(ev.target) && !ev.target.closest(".l5-jump")) this._closeBubble(); };
    this._onKey = (ev) => { if (ev.key === "Escape") this._closeBubble(); };
    document.addEventListener("pointerdown", this._onDocDown);
    document.addEventListener("keydown", this._onKey);
  }

  destroy() {
    cancelAnimationFrame(this._raf);
    this._raf = 0;
    this._ro.disconnect();
    document.removeEventListener("pointerdown", this._onDocDown);
    document.removeEventListener("keydown", this._onKey);
    for (const p of Object.values(this.pending)) clearTimeout(p.timer);
    this.root.innerHTML = "";
    this.root.classList.remove("live5", "live");
  }

  // ---------------------------------------------------------------- 搭 DOM

  _build() {
    const R = this.root;
    R.innerHTML = "";
    const stat = (label, cls) => {
      const v = el("b", { class: cls });
      return [el("span", { class: "l5-stat" }, el("span", { class: "l5-lab" }, label), v), v];
    };
    const [posBox, pos] = stat("全场位置", "l5-pos");
    const [timeBox, time] = stat("Set 时间", "l5-time");
    this.timeEl = time;
    this.leftEl = el("span", { class: "l5-speed dim" });
    put(timeBox, this.leftEl);
    timeBox.title = "这套 set 放到第几分几秒 / 一共多长（音乐里的时间：倍速时走得比墙上的钟快）";
    const [bpmBox, bpm] = stat("主速", "l5-bpm");
    const [sceneBox, scene] = stat("当前场景", "l5-scene-name");
    this.posEl = pos; this.bpmEl = bpm; this.sceneEl = scene;
    this.speedEl = el("span", { class: "l5-speed dim hidden" });
    put(bpmBox, this.speedEl);
    this.draftEl = el("span", { class: "badge warn hidden" }, "草稿");
    put(sceneBox, this.draftEl);
    sceneBox.classList.add("grow");
    this.stageEl = el("span", { class: "l5-stage" });
    this.pauseBtn = el("button", { class: "l5-pause", onclick: () => this._pauseResume() }, "⏸ 暂停");
    this.jumpBtn = el("button", { class: "l5-jump", onclick: () => this._askSeek(null, this.jumpBtn, true) }, "跳转…");
    this.lateEl = el("span", { class: "l5-late hidden" });
    this.nextEl = el("span", { class: "l5-nextline" });
    put(R, el("div", { class: "panel l5-head" },
      el("div", { class: "l5-row" }, posBox, timeBox, bpmBox, sceneBox, this.stageEl, this.lateEl, this.pauseBtn, this.jumpBtn),
      this.arrEl = el("div", { class: "l5-arr", title: "整套 set 的安排：最上面是场景，下面每台 Deck 一行，色块 = 片段。点场景 = 从那个场景的开头放；点色块 = 从那一小节放" },
        this.arr = el("canvas", { class: "l5-arrc", role: "img", "aria-label": "整套 set 的安排：场景和每台 Deck 的片段" })),
      el("div", { class: "l5-row l5-horow" }, el("span", { class: "l5-kind main" }, "主要的交接"), this.hoEl = el("span", { class: "l5-ho" }),
        el("span", { class: "grow" }),
        el("span", { class: "l5-arrleg dim" }, "安排图：粗的色块 = 主线（主曲）· 细的 = 工具（循环、人声…）· 橙色 = 交接 · 蓝线 = 换低频")),
      el("div", { class: "l5-row l5-rolerow" }, this.rolesEl = el("span", { class: "l5-row l5-roles" }),
        el("span", { class: "l5-nextrow" }, el("span", { class: "l5-lab" }, "下一步"), this.nextEl))));

    // 整首曲子：每台 Deck 一行——它现在放的（或者下一段要放的）那首原曲的整首波形
    this.ovTitle = el("b", {}, "整首曲子");
    this.ovWhere = el("span", { class: "ho-where mono" });
    const legend = el("span", { class: "ho-legend", title: "Denon（Engine DJ）三频段波形：三段各自的音量叠在一起" }, "波形：",
      [["lo", "低频 LOW"], ["mid", "中频 MID"], ["hi", "高频 HIGH"]].map(([b, name]) => el("span", {}, el("i", { style: `background:${WAVE_COLORS[b]}` }), name)));
    this.tracksEl = el("div", { class: "l5-tracks" });
    put(R, el("div", { class: "handoff l5-ov" },
      el("div", { class: "ho-head" }, this.ovTitle,
        el("span", { class: "dim l5-ovhint" }, "亮的一段 = 这套谱要放的；小旗 = hot cue（打击垫 1–8）；点波形 = 从那里放"),
        el("span", { class: "grow" }), legend, this.ovWhere),
      this.tracksEl));
    this.zoomBtns = {};
    const tools = el("span", { class: "l5-tools" }, el("span", { class: "l5-lab" }, "一屏看"),
      ZOOMS.map((z) => (this.zoomBtns[z] = el("button", { class: "small", onclick: () => this._setZoom(z) }, ZOOM_ZH[z]))),
      this.followBtn = el("button", { class: "small", title: "游标停在画面左边三成的位置，谱往左走。拖动 / 横向滚动会关掉跟随",
        onclick: () => { this.follow = !this.follow; this._syncTools(); this._kick(true); } }, "跟随播放"));

    // 每台 Deck 的滚动波形
    this.decksEl = el("div", { class: "live-decks" });
    put(R, this.decksEl);

    // 调音台 + 接下来 ｜ 曲线 + 策略
    this.mixerEl = el("div", { class: "mixer l5-mixer", title: "旋钮 / 推子 / 电平都是从引擎读回的实际值。拖旋钮 / 推子 = 手动接管（只有它不再跟谱）；双击 = 下一拍回谱" });
    this.actsEl = el("div", { class: "l5-acts" });
    this.nowEl = el("span", { class: "l5-nowpos" });
    this.pastBtn = el("button", { class: "small", onclick: () => { this.showPast = !this.showPast; this._actSig = ""; this._renderActions(); } }, "回看更早的");
    this.lnTitle = el("b", {}, "EQ / Filter / 推子");
    this.lnWhere = el("span", { class: "tl-where mono" });
    this.lgutter = el("div", { class: "l5-gutter l5-lgutter" });
    this.lcanvas = el("canvas", { class: "l5-canvas l5-lcanvas", role: "img", "aria-label": "每台 Deck 的 EQ、Filter、推子曲线（谱上写的）" });
    this.ltip = el("div", { class: "l5-tip hidden" });
    this.lplot = el("div", { class: "l5-plot" }, this.lcanvas, this.ltip);
    this.stratEl = el("div", { class: "panel l5-strategy" });
    put(R, el("div", { class: "live-bottom l5-bottom" },
      el("div", { class: "live-left" }, this.mixerEl, this.eqEl = el("div", { class: "tl-next l5-eqops" }),
        el("div", { class: "panel l5-next" }, el("div", { class: "l5-h" }, "接下来", el("span", { class: "dim" }, "未来 4 小节的动作；做完没有按引擎的回执"), el("span", { class: "grow" }), this.pastBtn), this.actsEl)),
      el("div", { class: "live-left" },
        el("div", { class: "timeline-live l5-lanes" },
          el("div", { class: "tl-head" }, this.lnTitle, tools, el("span", { class: "grow" }), this.lnWhere),
          el("div", { class: "dim l5-lnhint" }, "线 = 谱；小旋钮和白边的点 = 引擎读回的实际值。拖动 = 前后看，点一下 = 从那一小节放"),
          el("div", { class: "l5-grid l5-lgrid" }, this.lgutter, this.lplot)),
        this.stratEl)));

    this._bindPlot(this.lcanvas);
    this.arr.addEventListener("mousemove", (ev) => { const r = this.arr.getBoundingClientRect(); this.arrHover = { x: ev.clientX - r.left, y: ev.clientY - r.top }; this._kick(true); });
    this.arr.addEventListener("mouseleave", () => { this.arrHover = null; this._kick(true); });
    this.arr.addEventListener("click", (ev) => {
      if (!this.chart || !this.onSeek) return;
      const r = this.arr.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
      const beat = this._arrBeatAt(x);
      if (beat === null) return;
      const sc = y < ARR.scene ? sceneAt(this.chart, beat) : null; // 点场景那一行：从场景的开头放
      const ho = y >= ARR.scene && y < ARR.scene + ARR.ho ? this._handoverAt(beat) : null; // 点交接那一行：从交接开始的地方放
      if (sc) this._askSeek(sc.start, { el: this.arr, x, y }, false, `场景 ${sc.no}${sc.name ? "「" + sc.name + "」" : ""}`);
      else if (ho) this._askSeek(ho.start, { el: this.arr, x, y }, false, `交接 ${ho.n}（${ho.from} → ${ho.to}）`);
      else this._askSeek(beat, { el: this.arr, x, y });
    });
    this._syncTools();
  }

  // 用到哪几台 Deck，就摆哪几台：滚动波形、调音台的通道
  _buildDecks() {
    // 整首曲子：每台 Deck 一行（标签 + 整首波形），和两 Deck 的交接概览同一个样子
    this.tracksEl.innerHTML = "";
    this.ov = {};
    for (const d of this.decks) {
      const r = { d, hoverX: null, clip: null, kind: el("span", { class: "l5-kind" }), name: el("span", { class: "ho-name" }), role: el("span", { class: "l5-role" }),
        bar: el("span", {}), pick: el("span", { class: "l5-pick" }), need: el("div", { class: "l5-need" }), canvas: el("canvas", { class: "ho-wave l5-twave" }) };
      r.canvas.addEventListener("mousemove", (ev) => { r.hoverX = ev.clientX - r.canvas.getBoundingClientRect().left; this._kick(true); });
      r.canvas.addEventListener("mouseleave", () => { r.hoverX = null; this._kick(true); });
      r.canvas.addEventListener("click", (ev) => {
        const x = ev.clientX - r.canvas.getBoundingClientRect().left;
        const g = this._trackBeatAt(r, x);
        if (g !== null) this._askSeek(g, { el: r.canvas, x, y: TRACK_H }, false, r.clip ? `${r.clip.id} 原曲第 ${Math.floor(this._trackSrcAt(r, x) / BEATS) + 1} 小节` : "");
      });
      r.root = el("div", { class: "ho-row l5-track" },
        el("div", { class: "ho-label" }, el("div", { class: "ho-title" }, el("span", { class: "deck-no", style: `background:${DECK_HEX[d]}` }, String(d)), r.kind, r.name, r.role),
          el("div", { class: "ho-bar mono" }, r.bar, r.pick), r.need),
        r.canvas);
      this.ov[d] = r;
      put(this.tracksEl, r.root);
    }
    this.trackImg = new Map(); // 整首波形的底图（素材 + 宽度 → 离屏 canvas），不用每帧重画

    this.decksEl.innerHTML = "";
    this.dp = {};
    for (const d of this.decks) {
      const o = { d, hoverX: null,
        title: el("span", { class: "deck-title" }, "（空）"), role: el("span", { class: "deck-role" }), state: el("span", { class: "l5-dstate" }),
        quiet: el("span", { class: "l5-quiet hidden", title: "这台 Deck 在放，但现在没出声（推子在底 / 被切掉了）" }, "没出声"),
        barEl: el("span", { class: "deck-bar" }, "–"), dots: [0, 1, 2, 3].map(() => el("i")), phrase: el("span", { class: "deck-phrase" }),
        bpm: el("span", { class: "deck-bpm" }, "–"), pitch: el("span", { class: "deck-pitch" }), time: el("span", { class: "deck-time" }),
        canvas: el("canvas", { class: "live-wave l5-wave" }) };
      o.canvas.addEventListener("mousemove", (ev) => { o.hoverX = ev.clientX - o.canvas.getBoundingClientRect().left; this._kick(true); });
      o.canvas.addEventListener("mouseleave", () => { o.hoverX = null; this._kick(true); });
      o.canvas.addEventListener("click", (ev) => {
        const r = o.canvas.getBoundingClientRect();
        const g = this._zoomBeatAt(ev.clientX - r.left, r.width);
        if (g !== null && g >= 0 && g < (this.chart.length || 0)) this._askSeek(g, { el: o.canvas, x: ev.clientX - r.left, y: r.height });
      });
      o.root = el("div", { class: `deck-live l5-deck d${d}` },
        el("div", { class: "deck-head" },
          el("span", { class: "deck-no", style: `background:${DECK_HEX[d]}` }, String(d)), o.title, o.role, o.state, o.quiet, el("span", { class: "grow" }),
          el("span", { class: "deck-beat" }, o.barEl, el("span", { class: "dots" }, o.dots), o.phrase),
          el("span", { class: "deck-tempo" }, o.bpm, o.pitch), o.time),
        o.canvas);
      this.dp[d] = o;
      put(this.decksEl, o.root);
    }

    // 调音台：通道在两边、主输出在中间（两台时 CH MASTER CH；四台时 CH CH MASTER CH CH）
    const M = this.mixerEl;
    M.innerHTML = "";
    this.ch = {};
    const strips = [];
    for (const d of this.decks) strips.push(this._buildChannel(d));
    this.mvu = new Meter();
    this.overLamp = el("span", { class: "l5-over", title: "主输出过载（削波）" }, "过载");
    this.overCount = el("div", { class: "l5-overn hidden" });
    this.masterDb = el("div", { class: "kv" }, "–");
    const master = el("div", { class: "master" }, el("div", { class: "ch-name" }, "MASTER"), el("div", { class: "kl" }, "主输出 dBFS"), this.overLamp, this.overCount,
      el("div", { class: "vu-wrap" }, meterScale(), this.mvu.root), this.masterDb);
    strips.splice(Math.ceil(this.decks.length / 2), 0, master);
    put(M, el("div", { class: "mixer-chs l5-chs", style: `grid-template-columns:${strips.map((s) => (s === master ? "50px" : "1fr")).join(" ")}` }, strips));
  }

  _buildChannel(d) {
    const col = DECK_HEX[d];
    const c = { d,
      k: { hi: new Knob("HIGH", "hi", BAND_HEX.hi, false, BAND_HEX.hi), mid: new Knob("MID", "mid", BAND_HEX.mid, false, BAND_HEX.mid),
        lo: new Knob("LOW", "lo", BAND_HEX.lo, false, BAND_HEX.lo), filter: new Knob("FILTER", "filter", "#c9a6ff") },
      fxA: new Knob("ECHO", "echo", FX_HEX, true), fxB: new Knob("REVERB", "reverb", FX_HEX, true),
      vu: new Meter(), fader: new Fader(col),
      clip: el("div", { class: "ch-gain" }), kind: el("span", { class: "l5-kind hidden" }), live: el("div", { class: "l5-chfx" }),
      back: el("div", { class: "l5-back hidden" }) };
    c.fxA.set(0); c.fxB.set(0);
    c.manualEl = el("span", { class: "l5-backlab" });
    put(c.back, c.manualEl,
      c.beatBtn = el("button", { class: "small", title: "下一拍起，用一拍回到谱上的位置", onclick: () => this._giveBack(d, null, "beat") }, "下一拍回谱"),
      c.barBtn = el("button", { class: "small", title: "下一小节起，用一拍回到谱上的位置", onclick: () => this._giveBack(d, null, "bar") }, "下一小节回谱"));
    for (const p of ["hi", "mid", "lo", "filter"]) this._dragKnob(c.k[p], d, p);
    this._dragFader(c.fader, d);
    c.root = el("div", { class: `channel l5-ch ch${d}` },
      el("div", { class: "ch-name", style: `color:${col}` }, `CH ${d}`, c.clip, c.kind),
      el("div", { class: "ch-body" },
        el("div", { class: "ch-knobs" }, c.k.hi.root, c.k.mid.root, c.k.lo.root, c.k.filter.root),
        el("div", { class: "fader-row" }, c.vu.root, c.fader.root)),
      el("div", { class: "fx-row" }, c.fxA.root, c.fxB.root), // 发送的两个小旋钮放在通道底下（不撑宽旋钮那一列）
      c.live, c.back);
    this.ch[d] = c;
    return c.root;
  }

  // 总览 / 曲线左边的行头
  _buildGutters() {
    const L = this.lgutter;
    L.innerHTML = "";
    this.gLane = {};
    this.gHead = {};
    for (const r of this.lnRows) {
      const cell = el("div", { class: `l5-g l5-g-${r.kind}`, style: `top:${r.y}px;height:${r.h}px` });
      if (r.kind === "scene") put(cell, el("span", { class: "dim" }, "场景 / 速度"));
      else if (r.kind === "ruler") put(cell, el("span", { class: "dim" }, "全场小节"));
      else if (r.kind === "head") {
        const o = { who: el("span", { class: "l5-lwho" }) };
        put(cell, el("b", { style: r.deck ? `color:${DECK_HEX[r.deck]}` : "" }, r.deck ? `CH ${r.deck}` : "MASTER"), o.who);
        this.gHead[r.deck] = o;
      } else {
        const col = laneColor(r.param, r.deck);
        const o = { mini: new MiniKnob(r.param, col), val: el("span", { class: "tl-kv mono" }, "–"), man: el("span", { class: "l5-vm hidden", title: "手动接管中" }, "手"), cell };
        put(cell, o.mini.root, el("span", { class: "tl-pn", style: `color:${col}` }, LANE_ZH[r.param]), o.val, o.man);
        this.gLane[`${r.deck}.${r.param}`] = o;
      }
      put(L, cell);
    }
    L.style.height = `${this.lnH}px`;
  }

  // ---------------------------------------------------------------- 数据

  setChart(chart) {
    if (chart === this.chart) return;
    this.chart = chart && chart.clips ? chart : null;
    this.hover = null;
    this._actSig = "";
    this._sceneNo = undefined;
    this._tickBeat = undefined; // 换了谱：位置、场景那几个字下一帧重新写（同一个实例在首页和控制台之间搬）
    this._closeBubble();
    if (!this.chart) { this.tracksEl.innerHTML = ""; this.lgutter.innerHTML = ""; return; }
    this.restBeat = 0;
    this.shown = null;
    // 每台 Deck 的片段（按起播排）、落在它身上的效果
    const used = new Set();
    this.deckClips = {};
    for (const c of this.chart.clips) { used.add(c.deck); (this.deckClips[c.deck] = this.deckClips[c.deck] || []).push(c); }
    for (const list of Object.values(this.deckClips)) list.sort((a, b) => a.start - b.start);
    this.decks = [...used].sort((a, b) => a - b);
    if (this.decks.length < 2) this.decks = [...new Set([...this.decks, 1, 2])].sort((a, b) => a - b);
    for (const c of this.chart.clips) this._wave(c);
    this.assets = Object.fromEntries((this.chart.assets || []).map((a) => [a.id, a])); // 整首多长、打击垫 1–8 在哪
    this._buildDecks();
    this._relayout();
    this._renderState();
    this._kick(true);
  }

  // 这个片段的原曲波形（第一次要的时候去下，下好了重画）
  _wave(clip) {
    const id = clip.track_id;
    if (!id || !this.loadTrack) return null;
    if (!this.waves.has(id)) {
      this.waves.set(id, null);
      this.loadTrack(id).then((wf) => { if (wf && wf.n) { this.waves.set(id, wf); this._kick(true); } }).catch(() => {});
    }
    return this.waves.get(id);
  }

  _deckClipAt(d, g) {
    for (const c of this.deckClips[d] || []) if (g >= c.start && g < c.stop) return c;
    return null;
  }

  update(engine, progress) {
    const e = engine && engine.v5 && engine.status && (!this.chart || !engine.set || engine.set === this.chart.id) ? engine : null;
    this.progress = progress && progress.v5 ? progress : null;
    if (e) {
      const now = Date.now(), st = e.status, old = this.engine && this.engine.status;
      // 状态说的是哪一刻：引擎给的墙上时间；和这台机器的钟差太多（不是同一台）就按收到的时刻
      this.at = e.at && Math.abs(now - e.at) < 3000 ? e.at : now;
      if (!old || this.engine.from_beat !== e.from_beat || st.beat < old.beat - 0.01 || st.state !== old.state) this.shown = null; // 跳转 / 重新开始：游标直接过去
      // 停下以后引擎回到空闲、拍数归回起点：游标留在停下的地方（放完以后尾音还在走的那几拍不算，停在最后一小节的线上）
      if (st.state !== "idle") this.restBeat = this.chart && this.chart.length ? Math.min(st.beat, this.chart.length) : st.beat;
    }
    this.engine = e;
    this._renderState();
    this._kick(true);
  }

  // 没在放的时候游标该停在哪（「继续」会从这一拍接着放）：正在放时不管
  setRest(beat) {
    if (this._isLive() || typeof beat !== "number" || beat === this.restBeat) return;
    this.restBeat = beat;
    this._actSig = "";
    this._tickBeat = undefined;
    this._renderState();
    this._kick(true);
  }

  _status() { return this.engine ? this.engine.status : null; }
  // 引擎报的拍（不外推）：动作做到哪了按它算；没在放 = 上次停下的地方
  _engineBeat() { const st = this._status(); return st && st.state !== "idle" ? st.beat : this.restBeat; }
  _isLive() { const st = this._status(); return !!st && !["idle", "done"].includes(st.state); }
  _deckSt(d) { const st = this._status(); return (st && (st.decks || []).find((x) => x.deck_id === d)) || null; }

  // 游标现在在全场第几拍：引擎报的拍 + 从那一刻到现在走过的（只在 running 时走）
  _beatNow() {
    const st = this._status();
    if (!st || st.state === "idle") return this.restBeat;
    let b = st.beat;
    if (st.state === "running") {
      const dt = Math.max(0, Math.min(1500, Date.now() - this.at)); // 状态断了最多往前推 1.5 秒，不会自己跑下去
      b += (dt / 60000) * (st.bpm || 0) * (this.engine.speed || 1);
      if (this.shown !== null && b < this.shown) b = this.shown; // 新状态比外推的略早：停一下，不往回抖
      this.shown = b;
    }
    return Math.min(b, (this.chart.length || b) + 64);
  }

  // ---------------------------------------------------------------- 尺寸、缩放、滚动

  _resize() {
    const w = Math.floor(this.lplot.clientWidth);
    if (!w || w === this.W) { this._kick(true); return; }
    this.W = w;
    if (this.chart) { this._relayout(); this._kick(true); }
  }

  _span() { // 一屏几拍
    if (this.zoom === "scene") {
      const s = sceneAt(this.chart, this.follow || !this._viewScene ? this._beatNow() : this._viewScene) || (this.chart.scenes || [])[0];
      return s ? s.end - s.start : 64;
    }
    return this.zoom * BEATS;
  }

  _relayout() {
    if (!this.chart || !this.W) return;
    this.ppb = Math.max(MIN_BAR_PX / BEATS, this.W / this._span());
    // 曲线的行：每台 Deck 一组（组头那一行画落在这台 Deck 上的效果），最后是进主输出的（采样、总输出的效果）
    const fxOf = (d) => (this.chart.fx || []).filter((f) => (d ? (f.decks || []).includes(d) : !(f.decks || []).length));
    const pack = (list) => { // 重叠的效果错开几层
      const ends = [];
      const out = [];
      for (const f of [...list].sort((a, b) => a.start - b.start)) {
        const end = f.end + (f.tail || 0);
        let lane = ends.findIndex((e) => e <= f.start + 1e-6);
        if (lane < 0) lane = ends.length;
        ends[lane] = end;
        out.push({ f, lane });
      }
      return { items: out, n: Math.max(1, ends.length) };
    };
    let y = LN.scene + LN.ruler + LN.gap;
    this.lnRows = [{ kind: "scene", y: 0, h: LN.scene }, { kind: "ruler", y: LN.scene, h: LN.ruler }];
    for (const d of [...this.decks, 0]) {
      const fx = pack(fxOf(d));
      if (!d && !fx.items.length) continue;
      if (this.lnRows.length > 2) y += LN.group;
      const hh = Math.max(LN.head, 4 + fx.n * LN.band);
      this.lnRows.push({ kind: "head", deck: d, y, h: hh, fx });
      y += hh + LN.gap;
      if (!d) continue;
      for (const p of LANE_PARAMS) { this.lnRows.push({ kind: "param", deck: d, param: p, y, h: LN.row }); y += LN.row + LN.gap; }
    }
    this.lnH = y;
    this._buildGutters();
    this._renderState();
    this._clampView();
  }

  _viewRange() {
    const span = this.W / this.ppb;
    const lo = -LEAD_IN, hi = Math.max(lo, (this.chart.length || 0) + BEATS - span);
    return [lo, hi, span];
  }

  _clampView() {
    const [lo, hi] = this._viewRange();
    this.b0 = Math.max(lo, Math.min(hi, this.b0));
  }

  _pan(b0) {
    this.follow = false;
    this.b0 = b0;
    this._clampView();
    this._viewScene = this.b0 + (this.W / this.ppb) / 2;
    this._syncTools();
    this._kick(true);
  }

  _setZoom(z) {
    if (!this.chart) return;
    const beat = this._beatNow();
    const mid = this.b0 + (this.W / this.ppb) / 2;
    this.zoom = z;
    try { localStorage.setItem("djs.l5.zoom", String(z)); } catch { /* 无痕窗口 */ }
    this._viewScene = mid;
    this._relayout();
    if (!this.follow) { // 不跟随：以画面中间为准缩放
      if (z === "scene") { const s = sceneAt(this.chart, mid); this.b0 = s ? s.start : mid; } else this.b0 = mid - (this.W / this.ppb) / 2;
      this._clampView();
    } else this._followTo(beat);
    this._syncTools();
    this._kick(true);
  }

  _followTo(beat) {
    const [lo, hi, span] = this._viewRange();
    let b0;
    const s = this.zoom === "scene" ? sceneAt(this.chart, beat) : null;
    if (s && (s.end - s.start) * this.ppb <= this.W + 1) b0 = s.start; // 整个场景放得下：场景不动，游标走
    else b0 = beat - span * PLAYHEAD_AT;
    this.b0 = Math.max(lo, Math.min(hi, b0));
  }

  _syncTools() {
    for (const z of ZOOMS) this.zoomBtns[z].classList.toggle("on", z === this.zoom);
    this.followBtn.classList.toggle("on", this.follow);
    this.followBtn.textContent = this.follow ? "✓ 跟随播放" : "跟随播放";
  }

  // ---------------------------------------------------------------- 每帧

  _kick(dirty = false) {
    if (dirty) this.dirty = true;
    if (!this._raf && this.root.isConnected) this._raf = requestAnimationFrame(this._frame);
  }

  _frame() {
    this._raf = 0;
    if (!this.root.isConnected) return; // 页面换走了：停表，下次 update() 再开
    const st = this._status();
    const moving = !!st && st.state === "running";
    if (moving || this.dirty) this._raf = requestAnimationFrame(this._frame);
    if (!this.chart || !this.W || !this.lnRows) return;
    const beat = this._beatNow();
    if (this.follow && (moving || this.dirty)) {
      if (this.zoom === "scene") { // 换了场景：一屏的小节数跟着变
        const s = sceneAt(this.chart, beat);
        if ((s ? s.no : 0) !== this._zoomScene) { this._zoomScene = s ? s.no : 0; this._relayout(); }
      }
      this._followTo(beat);
    }
    if (!this.dirty && beat === this._drawnBeat && this.b0 === this._drawnB0) return;
    this.dirty = false;
    this._drawnBeat = beat;
    this._drawnB0 = this.b0;
    this._drawArr(beat);
    this._drawTracks(beat);
    this._drawLanes(beat);
    this._drawDecks(beat);
    const whole = Math.floor(beat);
    if (whole !== this._tickBeat) { this._tickBeat = whole; this._tick(beat); }
    const t = performance.now();
    if (!this._eqAt || t - this._eqAt > 250) { this._eqAt = t; this._renderEqOps(beat); this._renderTime(beat); this._renderHandover(beat); }
  }

  // Set 时间：放到第几分几秒 / 一共多长，还剩多久
  _renderTime(beat) {
    const total = this.chart.duration || secAt(this.chart.tempo, this.chart.length || 0);
    const now = Math.min(total, secAt(this.chart.tempo, Math.min(beat, this.chart.length || beat)));
    setText(this.timeEl, `${clock(now)} / ${clock(total)}`);
    setText(this.leftEl, `还剩 ${clock(Math.max(0, total - now))}`);
  }

  // 竖线：全场的小节线（够宽才画每一小节）
  _barLines(g, X, bA, bB, top, bottom) {
    const barPx = this._curPpb * BEATS;
    const step = barPx >= 14 ? 1 : barPx >= 7 ? 2 : barPx >= 3.5 ? 4 : 8;
    for (let bar = Math.max(0, Math.floor(bA / BEATS)); bar * BEATS <= bB; bar++) {
      if (bar % step) continue;
      const x = Math.round(X(bar * BEATS)) + 0.5;
      g.strokeStyle = bar % 8 === 0 ? "rgba(255,255,255,.13)" : "rgba(255,255,255,.05)";
      g.beginPath(); g.moveTo(x, top); g.lineTo(x, bottom); g.stroke();
    }
  }

  // ---------------------------------------------------------------- 整套 set 的安排（场景 + 每台 Deck 放什么）

  _arrBeatAt(x) {
    const w = this.arr.clientWidth - ARR.left, len = this.chart.length || 0;
    if (!w || !len || x < ARR.left) return null;
    const b = ((x - ARR.left) / w) * len;
    return b >= 0 && b < len ? b : null;
  }

  // 全场第 beat 拍落在哪一次交接里（没有 = null）；零长度的（直接接上）前后各让半小节好点中
  _handoverAt(beat) {
    return (this.chart.handovers || []).find((h) => beat >= h.start - (h.end > h.start ? 0 : 2) && beat < Math.max(h.end, h.start + 2)) || null;
  }

  _drawArr(beat) {
    const c = this.arr, w = c.clientWidth, ch = this.chart, len = ch.length || 1;
    if (!w) return;
    const top = ARR.scene + ARR.ho;
    const H = top + this.decks.length * (ARR.row + ARR.gap);
    const g = sizeCanvas(c, w, H);
    g.clearRect(0, 0, w, H);
    g.textBaseline = "alphabetic";
    const L = ARR.left, X = (b) => L + (b / len) * (w - L);
    const cur = sceneAt(ch, beat);
    // 场景
    g.font = "bold 11px sans-serif";
    for (const s of ch.scenes || []) {
      const x0 = X(s.start), x1 = X(s.end);
      g.fillStyle = s.no % 2 ? "#1b2740" : "#2a2440";
      g.fillRect(x0, 0, x1 - x0 - 1, ARR.scene - 2);
      if (cur && cur.no === s.no) { g.strokeStyle = "#e8ecf3"; g.lineWidth = 1.5; g.strokeRect(x0 + 0.75, 0.75, x1 - x0 - 2.5, ARR.scene - 3.5); g.lineWidth = 1; }
      g.fillStyle = "#e8ecf3";
      const bar = String(barOf(s.start));
      g.fillText(fitText(g, `${s.no} ${s.name || ""}${s.draft ? "（草稿）" : ""}`, x1 - x0 - 14 - g.measureText(bar).width), x0 + 5, 13);
      g.fillStyle = "#8a92a3"; g.font = "10px ui-monospace, Menlo, monospace";
      g.fillText(bar, x1 - g.measureText(bar).width - 5, 13);
      g.font = "bold 11px sans-serif";
    }
    g.fillStyle = "#8a92a3"; g.font = "10px sans-serif";
    g.fillText("场景", 2, 13);
    g.fillText("交接", 2, ARR.scene + 12);
    // 主要的交接：橙色的一段 = 下一首进来到上一首交完；蓝线 = 换低频的那一拍。往下淡淡地贯穿到 Deck 那几行
    g.fillStyle = "rgba(255,255,255,.03)";
    g.fillRect(L, ARR.scene, w - L, ARR.ho - 2);
    for (const h of ch.handovers || []) {
      const x0 = X(h.start), x1 = Math.max(X(h.end), x0 + 3), live = beat >= h.start && beat < h.end;
      g.fillStyle = "rgba(242,184,75,.07)";
      g.fillRect(x0, top, x1 - x0, H - top);
      g.fillStyle = live ? "rgba(242,184,75,.75)" : "rgba(242,184,75,.4)";
      g.fillRect(x0, ARR.scene, x1 - x0, ARR.ho - 2);
      g.fillStyle = live ? "#1a1300" : "#ffe2a6"; g.font = "bold 10px sans-serif";
      const t = fitText(g, `交接 ${h.n}  ${h.from} → ${h.to}`, Math.max(x1 - x0, 0) - 8);
      if (t) g.fillText(t, x0 + 4, ARR.scene + 11);
      else { g.fillStyle = "#ffe2a6"; g.fillText(fitText(g, `交接 ${h.n}`, 60), x1 + 3, ARR.scene + 11); }
      if (h.swap !== null && h.swap !== undefined) { g.fillStyle = "#5b8cff"; g.fillRect(Math.round(X(h.swap)) - 1, ARR.scene, 2, H - ARR.scene); }
    }
    // 每台 Deck：粗的色块 = 主线（这一首歌），细的 = 工具（循环、一句人声…）；浅一点的是 loop，底下黄色的小条是效果
    this.decks.forEach((d, i) => {
      const y = top + i * (ARR.row + ARR.gap), h = ARR.row;
      g.fillStyle = BG;
      g.fillRect(L, y, w - L, h);
      g.fillStyle = DECK_HEX[d];
      g.fillRect(2, y + 2, 20, h - 4);
      g.fillStyle = "#0b0d11"; g.font = "bold 11px sans-serif";
      g.fillText(`D${d}`, 4, y + h - 5);
      for (const k of this.deckClips[d] || []) {
        const x0 = X(k.start), x1 = X(k.stop), live = beat >= k.start && beat < k.stop;
        const by = k.main ? y : y + 5, bh = k.main ? h : h - 10;
        g.globalAlpha = live ? 0.95 : k.main ? 0.6 : 0.45;
        g.fillStyle = DECK_HEX[d];
        g.fillRect(x0, by, Math.max(2, x1 - x0 - 1), bh);
        g.globalAlpha = 1;
        for (const sg of k.segs || []) { // loop：一圈一圈的竖线
          if (!(sg.loop > 0) || !(sg.g1 > sg.g0)) continue;
          g.fillStyle = "rgba(255,255,255,.22)";
          g.fillRect(X(sg.g0), by, X(sg.g1) - X(sg.g0), bh);
          g.fillStyle = "rgba(11,13,17,.55)";
          for (let q = sg.g0; q < sg.g1 - 1e-6; q += sg.loop) g.fillRect(Math.round(X(q)), by, 1, bh);
        }
        if (k.main) {
          g.fillStyle = "#0b0d11"; g.font = "bold 11px sans-serif";
          g.fillText(fitText(g, `${k.id} ${k.title}${k.role ? " · " + k.role : ""}`, x1 - x0 - 8), x0 + 4, y + h - 5);
        } else { // 工具：字写在细条的上沿外面放不下，就压在条上用小字
          g.fillStyle = "#e8ecf3"; g.font = "10px sans-serif";
          const t = fitText(g, `${k.id} ${k.role || k.title}`, Math.max(x1 - x0, 70) - 6);
          if (t) { g.fillStyle = "rgba(11,13,17,.6)"; g.fillRect(x0 + 2, y + 3, g.measureText(t).width + 4, h - 6); g.fillStyle = "#e8ecf3"; g.fillText(t, x0 + 4, y + h - 6); }
        }
      }
      for (const f of ch.fx || []) {
        if (!(f.decks || []).includes(d)) continue;
        g.fillStyle = "#f0c050";
        g.fillRect(X(f.start), y + h - 3, Math.max(2, X(f.end) - X(f.start)), 3);
      }
    });
    // 放过的压暗、游标、悬停
    const xp = X(Math.min(beat, len));
    g.fillStyle = "rgba(0,0,0,.3)";
    g.fillRect(L, 0, Math.max(0, xp - L), H);
    g.fillStyle = "#fff";
    g.fillRect(Math.round(xp) - 1, 0, 2, H);
    const hv = this.arrHover;
    if (hv && this.onSeek) {
      const hb = this._arrBeatAt(hv.x);
      if (hb !== null) {
        const sc = hv.y < ARR.scene ? sceneAt(ch, hb) : null;
        const ho = !sc && hv.y < top ? this._handoverAt(hb) : null;
        const row = Math.floor((hv.y - top) / (ARR.row + ARR.gap));
        const k = !sc && !ho && row >= 0 && row < this.decks.length ? this._deckClipAt(this.decks[row], hb) : null;
        drawSeekHover(g, hv.x, H, sc ? `▶ 从场景 ${sc.no} 的开头放（全场第 ${barOf(sc.start)} 小节）`
          : ho ? `▶ 从交接 ${ho.n}（${ho.from} → ${ho.to}）开始的地方放（全场第 ${barOf(ho.start)} 小节${ho.swap !== null && ho.swap !== undefined ? `，第 ${barOf(ho.swap)} 小节换低频` : ""}）`
            : `▶ 从全场第 ${barOf(hb)} 小节放${k ? `（${k.main ? "主线" : "工具"} ${k.id} ${k.title}，原曲第 ${Math.floor(srcAt(k, hb) / BEATS) + 1} 小节）` : ""}`);
      }
    }
  }

  // 「主要的交接」那一行字：正在交接 = 谁接谁、低频哪一拍换、还有几小节交完；没在交接 = 下一次在哪
  _renderHandover(beat) {
    const hs = this.chart.handovers || [];
    const bars = (n) => Math.max(0, Math.ceil(n / BEATS - 1e-6));
    const name = (id) => { const k = clipById(this.chart, id); return k ? `${id} ${k.title}` : id; };
    const cur = hs.find((h) => beat >= h.start && beat < h.end), nx = hs.find((h) => h.start > beat);
    let text = hs.length ? "后面没有交接了" : "这套谱只有一条主线，没有交接", cls = "l5-ho dim";
    if (cur) {
      const sw = cur.swap === null || cur.swap === undefined ? "" : cur.swap > beat ? `全场 ${posText(cur.swap)} 换低频（还有 ${bars(cur.swap - beat)} 小节）` : "低频已经换过";
      text = `交接 ${cur.n} 进行中：${name(cur.from)}（D${cur.from_deck}）→ ${name(cur.to)}（D${cur.to_deck}）${sw ? " · " + sw : ""} · 还有 ${bars(cur.end - beat)} 小节交完`;
      cls = "l5-ho live";
    } else if (nx) {
      text = `下一次：交接 ${nx.n}，全场 ${posText(nx.start)} ${name(nx.from)}（D${nx.from_deck}）→ ${name(nx.to)}（D${nx.to_deck}），还有 ${bars(nx.start - beat)} 小节`;
      cls = "l5-ho";
    }
    setText(this.hoEl, text);
    setClass(this.hoEl, cls);
  }

  // ---------------------------------------------------------------- 整首曲子（每台 Deck 一行）

  // 这一行画哪个片段：这台 Deck 正在放的；没有就是下一段要放的
  _trackClip(d, beat) {
    const list = this.deckClips[d] || [];
    return list.find((c) => beat >= c.start && beat < c.stop) || list.find((c) => c.start >= beat) || null;
  }

  // 整首有多长（秒）：谱编译时量的；没有就按波形的长度
  _trackDur(clip) {
    const a = this.assets[clip.asset], wf = this.waves.get(clip.track_id);
    return (a && a.dur) || clip.dur || (wf ? wf.n / wf.rate : 0);
  }

  // 这一行上的 x → 原曲的拍 / 全场的拍（这套谱不放那一段 = null）
  _trackSrcAt(r, x) {
    const c = r.clip, w = r.canvas.clientWidth, dur = c ? this._trackDur(c) : 0;
    if (!c || !w || !dur) return null;
    return (((x / w) * dur - (c.t0 || 0)) * c.bpm) / 60;
  }

  _trackBeatAt(r, x) {
    const src = this._trackSrcAt(r, x);
    if (src === null || !this.onSeek) return null;
    const g = globalOfSrc(r.clip, src);
    return g !== null && g >= 0 && g < (this.chart.length || 0) ? g : null;
  }

  // 整首波形的底图：波形 + 乐句线（每 16 小节）+ 每 32 小节的小节号。和播放位置无关，画一次存着
  _trackImage(clip, w, dur) {
    const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
    const wf = this._wave(clip);
    const key = `${clip.track_id}:${clip.t0}:${clip.bpm}:${w}:${dpr}:${wf ? 1 : 0}`;
    if (this.trackImg.has(key)) return this.trackImg.get(key);
    const c = document.createElement("canvas"), H = TRACK_H;
    c.width = Math.round(w * dpr); c.height = Math.round(H * dpr);
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (wf) {
      drawTriBand(g, w, H / 2, H / 2 - 2, (x) => {
        let ia = Math.floor(((x / w) * dur) * wf.rate), ib = Math.max(ia + 1, Math.floor((((x + 1) / w) * dur) * wf.rate));
        ia = Math.max(0, ia); ib = Math.min(wf.n, ib);
        if (ia >= ib) return null;
        let lo = 0, mi = 0, hi = 0;
        for (let i = ia; i < ib; i++) {
          if (wf.low[i] > lo) lo = wf.low[i];
          if (wf.mid[i] > mi) mi = wf.mid[i];
          if (wf.high[i] > hi) hi = wf.high[i];
        }
        return [lo / 255, mi / 255, hi / 255];
      });
    }
    g.font = "9px ui-monospace, Menlo, monospace";
    const bars = Math.ceil(((dur - (clip.t0 || 0)) * clip.bpm) / 60 / BEATS);
    for (let b = 0; b <= bars; b += 16) {
      const x = (srcSec(clip, b * BEATS) / dur) * w;
      g.fillStyle = "rgba(255,255,255,.22)";
      g.fillRect(Math.round(x), 0, 1, H);
      if (b % 32 === 0) { g.fillStyle = "#8a92a3"; g.fillText(String(b + 1), x + 2, H / 2 - 2); }
    }
    if (this.trackImg.size > 16) this.trackImg.clear();
    this.trackImg.set(key, c);
    return c;
  }

  _drawTracks(beat) {
    let where = "";
    for (const d of this.decks) {
      const r = this.ov[d];
      if (!r) continue;
      const w = r.canvas.clientWidth, H = TRACK_H;
      if (!w) continue;
      const g = sizeCanvas(r.canvas, w, H);
      g.clearRect(0, 0, w, H);
      g.textBaseline = "alphabetic";
      const clip = (r.clip = this._trackClip(d, beat));
      const dur = clip ? this._trackDur(clip) : 0;
      if (!clip || !dur) {
        setText(r.name, clip ? `${clip.id} ${clip.title}` : "");
        setText(r.bar, clip ? "" : "后面没有片段了");
        setText(r.pick, "");
        setText(r.kind, ""); setClass(r.kind, "l5-kind hidden");
        r.root.classList.remove("main", "tool");
        if (r.need._sig !== "") { r.need._sig = ""; r.need.innerHTML = ""; }
        continue;
      }
      const asset = this.assets[clip.asset] || {};
      const live = beat >= clip.start && beat < clip.stop;
      const X = (src) => (srcSec(clip, src) / dur) * w; // 原曲的拍 → x
      g.drawImage(this._trackImage(clip, w, dur), 0, 0, w, H);
      // 这套谱要放的那几段亮着，别的压暗；loop 的那一圈垫一层蓝
      const spans = clip._spans || (clip._spans = clipSpans(clip));
      const used = spans.map((sp) => [Math.max(0, X(sp.a)), Math.min(w, X(sp.b))]).sort((a, b) => a[0] - b[0]);
      let x0 = 0;
      g.fillStyle = "rgba(8,9,12,.62)";
      for (const [a, b] of used) { if (a > x0) g.fillRect(x0, 0, a - x0, H); x0 = Math.max(x0, b); }
      if (x0 < w) g.fillRect(x0, 0, w - x0, H);
      for (const sp of spans) {
        if (!sp.loop) continue;
        g.fillStyle = "rgba(91,140,255,.22)";
        g.fillRect(X(sp.a), 0, Math.max(2, X(sp.b) - X(sp.a)), H);
      }
      // 选用的那一段：用这台 Deck 的颜色框出来，写上原曲第几到第几小节、几小节（整 16 小节的写几个乐句）
      const pick = clip._pick || (clip._pick = clipPick(clip));
      const col = DECK_HEX[d];
      const span = (a, b) => `${Math.floor(a / BEATS + 1e-6) + 1}–${Math.max(Math.floor(a / BEATS + 1e-6) + 1, Math.ceil(b / BEATS - 1e-6))}`;
      if (pick) {
        const xa = Math.max(1, X(pick.a)), xb = Math.min(w - 1, X(pick.b));
        g.strokeStyle = col; g.lineWidth = clip.main ? 2.5 : 1.5;
        if (!clip.main) g.setLineDash([5, 3]); // 工具：虚线框
        g.strokeRect(xa, 1, Math.max(3, xb - xa), H - 2);
        g.setLineDash([]);
        g.lineWidth = 1;
        // 框够宽就把「选用哪几小节」写在框中间（Deck 颜色的底、深色的字）；窄的只画框，字在左边的标签里
        g.font = "bold 11px sans-serif";
        const whole = pick.b - pick.a >= 64 && Math.abs((pick.b - pick.a) / 64 - Math.round((pick.b - pick.a) / 64)) < 1e-6;
        const t = `选用 第 ${span(pick.a, pick.b)} 小节` + (whole ? ` · ${Math.round((pick.b - pick.a) / 64)} 个乐句` : pick.a >= BEATS ? ` · ${durText(pick.b - pick.a)}` : "");
        const tw = g.measureText(t).width;
        if (xb - xa >= tw + 140) {
          const lx = xa + (xb - xa - tw) / 2;
          g.fillStyle = col;
          g.fillRect(lx - 5, 19, tw + 10, 16);
          g.fillStyle = "#0b0d11";
          g.fillText(t, lx, 31);
        }
      }
      setText(r.pick, pick ? `选用 ${span(pick.a, pick.b)}` : "");
      // 起播 / 停 / loop / 跳段（标签写原曲的小节号）
      g.font = "bold 11px sans-serif";
      const mark = (src, label, color, top, right) => {
        const x = X(src);
        g.fillStyle = color;
        g.fillRect(Math.round(x) - 1, 0, 2, H);
        const tw = g.measureText(label).width;
        const lx = right ? Math.max(2, x - tw - 4) : Math.min(w - tw - 2, x + 4), ly = top ? 2 : H - 27;
        g.fillStyle = "rgba(15,17,21,.8)";
        g.fillRect(lx - 2, ly, tw + 4, 14);
        g.fillStyle = color;
        g.fillText(label, lx, ly + 11);
      };
      const barNo = (src) => Math.floor(src / BEATS + 1e-6) + 1;
      const inSrc = srcAt(clip, clip.start);
      mark(inSrc, `起播 ${barNo(inSrc)}`, "#3ccf7a", true, false);
      for (const sp of spans) {
        if (sp.loop) mark(sp.a, `↻ ${durText(sp.len)} × ${Math.abs(sp.laps - Math.round(sp.laps)) < 1e-6 ? Math.round(sp.laps) : sp.laps.toFixed(1)} 圈`, "#9db8ff", false, false);
        else if (sp.kind === "jump" && sp.g0 > clip.start) mark(sp.a, `⤳ 跳到 ${barNo(sp.a)}`, "#c9a6ff", false, false);
      }
      const lastSp = spans[spans.length - 1];
      if (lastSp && !lastSp.loop) mark(lastSp.b, `停 ${barNo(lastSp.b)}`, "#f2b84b", true, true);
      // hot cue：细色线 + 底下带打击垫号的小旗
      drawHotCues(g, asset.hotcues || [], (b) => X((b - 1) * BEATS), 0, H, w, true);
      // 播放头（正在放这一段时）
      const total = Math.max(1, Math.floor(asset.bars || ((dur - (clip.t0 || 0)) * clip.bpm) / 60 / BEATS));
      if (live) {
        const src = srcAt(clip, beat), xp = X(src);
        g.fillStyle = "rgba(0,0,0,.3)";
        g.fillRect(0, 0, xp, H);
        g.fillStyle = "#fff";
        g.fillRect(Math.round(xp) - 1, 0, 2, H);
        setText(r.bar, `第 ${Math.max(1, barNo(src))} / ${total} 小节`);
        if (!where) where = `D${d} ${clip.id} 还剩 ${Math.max(0, Math.ceil((clip.stop - beat) / BEATS))} 小节`;
      } else setText(r.bar, `${Math.max(0, Math.ceil((clip.start - beat) / BEATS))} 小节后进 / 共 ${total}`);
      r.root.classList.toggle("next", !live);
      r.root.classList.toggle("main", !!clip.main);
      r.root.classList.toggle("tool", !clip.main);
      setText(r.kind, clip.main ? "主线" : "工具");
      setClass(r.kind, `l5-kind ${clip.main ? "main" : "tool"}`);
      if (r.need._sig !== clip.id) {
        r.need._sig = clip.id;
        const bands = clipBands(clip);
        const BZ = { lo: "LOW", mid: "MID", hi: "HIGH" };
        const say = { on: "要", off: "不要（从头到尾切着）", in: "先切着，后来放进来", out: "先在，后来切掉" };
        r.need.innerHTML = "";
        put(r.need, el("span", { class: "l5-lab" }, "要"),
          ["lo", "mid", "hi"].map((b) => el("span", { class: `l5-el ${b} ${bands[b]}`, title: `${BZ[b]}：${say[bands[b]]}` },
            BZ[b] + (bands[b] === "in" ? " 后进" : bands[b] === "out" ? " 后切" : ""))),
          clip.note ? el("span", { class: "l5-neednote", title: clip.note }, clip.note) : null);
      }
      setText(r.name, `${live ? "" : "下一段 "}${clip.id} ${clip.title}`);
      // 鼠标在波形上：从这里放（这套谱不放的那一段点不了）
      if (r.hoverX !== null) {
        const src = this._trackSrcAt(r, r.hoverX), gb = this._trackBeatAt(r, r.hoverX);
        if (src !== null && this.onSeek) {
          drawSeekHover(g, r.hoverX, H, gb !== null ? `▶ 从原曲第 ${Math.max(1, barNo(src))} 小节放（全场第 ${barOf(gb)} 小节）` : `这套谱不放这一段（原曲第 ${Math.max(1, barNo(src))} 小节）`);
          r.canvas.style.cursor = gb !== null ? "pointer" : "not-allowed";
        }
      }
    }
    const sc = sceneAt(this.chart, beat);
    setText(this.ovWhere, sc ? `场景 ${sc.no} 第 ${Math.floor((beat - sc.start) / BEATS) + 1} / ${Math.round((sc.end - sc.start) / BEATS)} 小节` : "");
  }

  // 游标：放过去的一侧压暗，白线 + 上面的小三角；悬停的细线
  _cursor(g, xp, top, W, H, hover) {
    if (xp > 0) { g.fillStyle = "rgba(0,0,0,.34)"; g.fillRect(0, top, Math.min(W, xp), H - top); }
    if (hover) { g.fillStyle = "rgba(255,255,255,.35)"; g.fillRect(Math.round(hover.x), 0, 1, H); }
    if (xp < -30 || xp > W + 30) return;
    g.fillStyle = "rgba(255,255,255,.2)";
    g.fillRect(xp - 2, 0, 4, H);
    g.fillStyle = "#fff";
    g.fillRect(xp - 1, 0, 2, H);
    g.beginPath(); g.moveTo(xp - 6, 0); g.lineTo(xp + 6, 0); g.lineTo(xp, 8); g.closePath(); g.fill();
  }

  // 每台 Deck 的 EQ / Filter / 推子曲线：和总览同一段全场小节
  _drawLanes(beat) {
    const W = this.W, H = this.lnH, ch = this.chart;
    if (!W || !this.lnRows) return;
    const g = sizeCanvas(this.lcanvas, W, H);
    const ppb = (this._curPpb = this.ppb), b0 = this.b0, bB = b0 + W / ppb;
    const X = (b) => (b - b0) * ppb;
    const st = this._status();
    g.clearRect(0, 0, W, H);
    g.textBaseline = "alphabetic";
    for (const r of this.lnRows) {
      if (r.kind === "scene") {
        g.fillStyle = BG;
        g.fillRect(0, r.y, W, r.h);
        g.font = "bold 11px sans-serif";
        for (const sc of ch.scenes || []) {
          const x0 = X(sc.start), x1 = X(sc.end);
          if (x1 < 0 || x0 > W) continue;
          g.fillStyle = sc.no % 2 ? "rgba(255,255,255,.07)" : "rgba(255,255,255,.035)";
          g.fillRect(x0, r.y, x1 - x0 - 1, r.h);
          const tx = Math.max(x0, 0) + 5;
          g.fillStyle = "#c8cdd8";
          g.fillText(fitText(g, `${sc.no} ${sc.name || ""}${sc.draft ? "（草稿）" : ""}`, x1 - tx - 6), tx, r.y + 14);
        }
        // 速度：变速的那一段底下画一道，写上从多少到多少
        g.font = "10px ui-monospace, Menlo, monospace";
        const t = ch.tempo || [];
        for (let i = 0; i + 1 < t.length; i++) {
          if (t[i + 1].bpm === t[i].bpm) continue;
          const x0 = X(t[i].at), x1 = X(t[i + 1].at);
          if (x1 < 0 || x0 > W) continue;
          g.fillStyle = "rgba(242,184,75,.9)";
          g.fillRect(x0, r.y + r.h - 2, Math.max(2, x1 - x0), 2);
          const text = `${fmtBpm(t[i].bpm)}→${fmtBpm(t[i + 1].bpm)}`;
          g.fillText(text, Math.min(W - g.measureText(text).width - 3, Math.max(x0, 0) + 3 + (x0 < 0 ? 0 : 90)), r.y + r.h - 5);
        }
        continue;
      }
      if (r.kind === "ruler") {
        g.fillStyle = BG;
        g.fillRect(0, r.y, W, r.h);
        g.font = "10px ui-monospace, Menlo, monospace";
        const barPx = ppb * BEATS;
        const step = [1, 2, 4, 8, 16, 32].find((n) => n * barPx >= 34) || 64;
        for (let bar = Math.max(0, Math.floor(b0 / BEATS)); bar * BEATS <= bB; bar++) {
          const x = Math.round(X(bar * BEATS)) + 0.5;
          const major = bar % step === 0;
          if (!major && barPx < 6) continue;
          g.strokeStyle = major ? "rgba(255,255,255,.45)" : "rgba(255,255,255,.16)";
          g.beginPath(); g.moveTo(x, r.y + (major ? 0 : r.h - 5)); g.lineTo(x, r.y + r.h); g.stroke();
          if (major) { g.fillStyle = bar % (step * 4) === 0 ? "#e8ecf3" : "#8a92a3"; g.fillText(String(bar + 1), x + 3, r.y + 12); }
        }
        continue;
      }
      if (r.kind === "head") {
        // 落在这台 Deck 上的效果：一条带子，淡色的那一截是关掉以后的尾音
        g.font = "bold 10px sans-serif";
        for (const { f, lane } of r.fx.items) {
          const x0 = X(f.start), x1 = X(f.end), x2 = X(f.end + (f.tail || 0));
          if (x2 < 0 || x0 > W) continue;
          const y = r.y + 3 + lane * LN.band, hh = LN.band - 3;
          const on = st && (st.fx || []).some((x) => x.id === f.id && !x.tail && x.on !== false);
          g.fillStyle = "rgba(240,192,80,.14)";
          g.fillRect(x1, y, x2 - x1, hh);
          g.fillStyle = on ? "rgba(240,192,80,.55)" : "rgba(240,192,80,.3)";
          g.fillRect(x0, y, Math.max(2, x1 - x0), hh);
          g.fillStyle = "#f6dfa2";
          const tx = Math.max(x0, 0) + 4;
          g.fillText(fitText(g, `${f.id} ${f.name}`, Math.max(x2, x1) - tx - 4), tx, y + hh - 3);
        }
        continue;
      }
      const d = r.deck, param = r.param, col = laneColor(param, d);
      g.fillStyle = "#12151b";
      g.fillRect(0, r.y, W, r.h);
      const Y = (f) => r.y + 3 + (1 - f) * (r.h - 6);
      if (param !== "vol") { // 12 点 / 关 的位置
        g.strokeStyle = "rgba(255,255,255,.16)";
        g.setLineDash([2, 4]);
        g.beginPath(); g.moveTo(0, Y(0.5) + 0.5); g.lineTo(W, Y(0.5) + 0.5); g.stroke();
        g.setLineDash([]);
      }
      for (const c of this.deckClips[d] || []) {
        if (c.stop < b0 || c.start > bB) continue;
        const pts = lanePoints(c, param);
        if (!pts.length) continue;
        const moving = pts.some(([, v]) => Math.abs(v - pts[0][1]) > 1e-4);
        const path = new Path2D();
        pts.forEach(([b, v], i) => { const x = X(b), y = Y(laneFrac(param, v)); if (i) path.lineTo(x, y); else path.moveTo(x, y); });
        const fill = new Path2D(path);
        fill.lineTo(X(pts[pts.length - 1][0]), r.y + r.h); fill.lineTo(X(pts[0][0]), r.y + r.h); fill.closePath();
        g.fillStyle = col; g.globalAlpha = moving ? 0.16 : 0.07;
        g.fill(fill);
        g.globalAlpha = moving ? 1 : 0.5;
        g.strokeStyle = col; g.lineWidth = moving ? 1.8 : 1.2;
        g.stroke(path);
        g.globalAlpha = 1; g.lineWidth = 1;
      }
    }
    const top = LN.scene + LN.ruler;
    this._barLines(g, X, b0, bB, top, H);
    // 主要的交接：标尺底下一道橙色（从下一首进来到上一首交完），换低频的那一拍一条蓝线贯穿下来
    g.font = "bold 10px sans-serif";
    for (const h of ch.handovers || []) {
      const x0 = X(h.start), x1 = Math.max(X(h.end), x0 + 3);
      if (x1 < 0 || x0 > W) continue;
      g.fillStyle = "rgba(242,184,75,.85)";
      g.fillRect(x0, top - 3, x1 - x0, 3);
      g.fillStyle = "rgba(242,184,75,.05)";
      g.fillRect(x0, top, x1 - x0, H - top);
      const t = `交接 ${h.n} ${h.from} → ${h.to}`, tx = Math.max(x0, 0) + 4;
      if (x1 - tx > g.measureText(t).width + 8) { g.fillStyle = "rgba(11,13,17,.8)"; g.fillRect(tx - 2, top + 1, g.measureText(t).width + 4, 12); g.fillStyle = HO_HEX; g.fillText(t, tx, top + 11); }
      if (h.swap !== null && h.swap !== undefined) {
        const xs = Math.round(X(h.swap));
        if (xs >= 0 && xs <= W) {
          g.fillStyle = "#5b8cff";
          g.fillRect(xs - 1, top - 3, 2, H - top + 3);
          tag(g, "换低频", xs, top + 24, "#9db8ff", xs > W - 60);
        }
      }
    }
    for (const s of ch.scenes || []) {
      const x = Math.round(X(s.start)) + 0.5;
      if (x < 0 || x > W) continue;
      g.strokeStyle = "rgba(255,255,255,.3)";
      g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke();
    }
    const xp = X(beat);
    this._cursor(g, xp, top, W, H, this.hover);
    setText(this.lnWhere, `全场 ${posText(b0 < 0 ? 0 : b0)} – ${posText(Math.min(bB, ch.length || bB))}`);
    if (this.hover && this.onSeek) { // 点一下从这一小节放
      const hb = b0 + this.hover.x / ppb;
      if (hb >= 0 && hb < (ch.length || 0) && this.hover.y < top) drawSeekHover(g, this.hover.x, top, `▶ 从全场第 ${barOf(hb)} 小节放`);
    }
    if (xp < -10 || xp > W + 10) return;
    // 游标处的点：空心 = 谱上这一拍的值；实心白边 = 引擎报的实际位置（手动接管 / 没跟上时两个不在一处）
    for (const r of this.lnRows) {
      if (r.kind !== "param") continue;
      const c = this._deckClipAt(r.deck, beat);
      if (!c) continue;
      const Y = (f) => r.y + 3 + (1 - f) * (r.h - 6);
      const col = laneColor(r.param, r.deck);
      const fp = laneFracAt(c, r.param, beat);
      const ds = this._deckSt(r.deck), act = ds && ds.clip_id === c.id && ds.human ? ds.human[r.param] : null;
      const fa = act ? laneFrac(r.param, act.v) : null;
      if (fa !== null && fp !== null && Math.abs(Y(fa) - Y(fp)) > 2.5) {
        g.strokeStyle = col; g.lineWidth = 1.5;
        g.beginPath(); g.moveTo(xp, Y(fp)); g.lineTo(xp, Y(fa)); g.stroke();
        g.beginPath(); g.arc(xp, Y(fp), 3.5, 0, 2 * Math.PI); g.fillStyle = "#12151b"; g.fill(); g.stroke();
        g.lineWidth = 1;
      }
      const f = fa !== null ? fa : fp;
      if (f === null) continue;
      g.beginPath(); g.arc(xp, Y(f), 4.5, 0, 2 * Math.PI);
      g.fillStyle = col; g.fill();
      g.strokeStyle = "#fff"; g.lineWidth = 2; g.stroke(); g.lineWidth = 1;
    }
  }

  // 滚动波形的横轴：画面上第 x 个像素是全场第几拍（播放头在正中，两侧各 SPAN_S 秒，变速的地方照速度图走）
  _zoomBeats(beat, w) {
    const n = w + 2, cx = Math.round(w / 2), dt = SPAN_S / (w / 2);
    const arr = new Float64Array(n);
    arr[cx] = beat;
    for (let x = cx + 1; x < n; x++) arr[x] = arr[x - 1] + (dt * (tempoAt(this.chart, arr[x - 1]) || 120)) / 60;
    for (let x = cx - 1; x >= 0; x--) arr[x] = arr[x + 1] - (dt * (tempoAt(this.chart, arr[x + 1]) || 120)) / 60;
    return arr;
  }

  _zoomBeatAt(x, w) {
    if (!this.chart || !w) return null;
    const arr = this._zb && this._zb.length === Math.round(w) + 2 ? this._zb : this._zoomBeats(this._beatNow(), Math.round(w));
    return arr[clamp(Math.round(x), 0, arr.length - 1)];
  }

  _drawDecks(beat) {
    const first = this.dp && this.dp[this.decks[0]];
    if (!first) return;
    const w = first.canvas.clientWidth;
    if (!w) return;
    const arr = (this._zb = this._zoomBeats(beat, w));
    const cx = Math.round(w / 2);
    // 全场拍 → 画面上的 x（arr 是单调的：二分）
    const XB = (gb) => {
      if (gb <= arr[0]) return -1e4;
      if (gb >= arr[arr.length - 1]) return 1e4;
      let lo = 0, hi = arr.length - 1;
      while (hi - lo > 1) { const m = (lo + hi) >> 1; if (arr[m] <= gb) lo = m; else hi = m; }
      return lo + (gb - arr[lo]) / (arr[hi] - arr[lo]);
    };
    const bpmNow = tempoAt(this.chart, beat) || 120;
    for (const d of this.decks) this._drawDeck(this.dp[d], beat, arr, cx, w, XB, bpmNow);
  }

  _drawDeck(o, beat, arr, cx, w, XB, bpmNow) {
    const d = o.d, H = 68, top = 15;
    const g = sizeCanvas(o.canvas, w, H);
    g.clearRect(0, 0, w, H);
    const gA = arr[0], gB = arr[w];
    const clips = (this.deckClips[d] || []).filter((c) => c.stop > gA && c.start < gB);
    const cur = this._deckClipAt(d, beat);
    const mid = top + (H - top) / 2, amp = (H - top) / 2 - 1;
    for (const c of clips) {
      for (const s of c.segs || []) { // loop 的那一段垫底色
        if (!(s.loop > 0) || s.g1 < gA || s.g0 > gB) continue;
        const x0 = Math.max(0, XB(s.g0)), x1 = Math.min(w, XB(s.g1));
        g.fillStyle = "rgba(91,140,255,.10)";
        g.fillRect(x0, top, x1 - x0, H - top);
      }
    }
    // 波形：这台 Deck 在这几秒里读到的原曲（loop 就是同一截一圈一圈地来）
    drawTriBand(g, w, mid, amp, (x) => {
      const ga = arr[x], c = this._deckClipAt(d, ga);
      const wf = c && this._wave(c);
      return wf ? waveCol(c, wf, ga, arr[x + 1]) : null;
    });
    // 原曲的拍 / 小节 / 乐句线和小节号
    g.font = "10px ui-monospace, Menlo, monospace";
    for (const c of clips) {
      eachSrcBar(c, gA, gB, (gb, k) => {
        if (k % 4 === 0) return;
        const x = XB(gb);
        g.strokeStyle = "rgba(255,255,255,.10)";
        g.beginPath(); g.moveTo(x + 0.5, top); g.lineTo(x + 0.5, top + 6); g.stroke();
      }, 1);
      eachSrcBar(c, gA, gB, (gb, k, head) => {
        const x = XB(gb), phrase = k % 16 === 0;
        g.strokeStyle = head && gb > c.start + 1e-6 ? "#9db8ff" : phrase ? "rgba(255,255,255,.55)" : "rgba(255,255,255,.18)";
        g.lineWidth = phrase || head ? 1.5 : 1;
        g.beginPath(); g.moveTo(x + 0.5, top); g.lineTo(x + 0.5, H); g.stroke();
        g.lineWidth = 1;
        g.fillStyle = phrase ? "#e8ecf3" : "#8a92a3";
        g.fillText(String(k + 1), x + 3, 11);
      });
      // 标记：起播 / 停 / loop / 跳段
      g.font = "bold 11px sans-serif";
      const mark = (gb, label, color) => {
        const x = XB(gb);
        if (x < -2 || x > w + 2) return;
        g.strokeStyle = color; g.lineWidth = 2;
        g.beginPath(); g.moveTo(x, top); g.lineTo(x, H); g.stroke();
        g.lineWidth = 1;
        g.fillStyle = color;
        g.fillText(label, x + 4, top + 13);
      };
      mark(c.start, `${c.id} 起播`, "#3ccf7a");
      mark(c.stop, `${c.id} 停`, "#f2b84b");
      for (const s of c.segs || []) {
        if (s.loop > 0 && s.g0 > c.start) mark(s.g0, `↻ ${s.id} ${durText(s.loop)}`, "#9db8ff");
        else if (s.loop > 0) mark(s.g0 + 1e-6, "", "#9db8ff");
        if (s.kind === "jump" && s.g0 > c.start) mark(s.g0, `⤳ 原曲 ${s.src_pos}`, "#c9a6ff");
        if (s.kind === "exit" && s.g0 < c.stop) mark(s.g0, "出圈", "#9db8ff");
      }
      // hot cue：这份素材的打击垫 1–8 落在这几秒里的哪里
      g.font = "bold 10px ui-monospace, Menlo, monospace";
      const seen = new Set();
      for (const hc of (this.assets[c.asset] || {}).hotcues || []) {
        const hs = (hc.bar - 1) * BEATS;
        for (const sg of c.segs || []) {
          if (!(sg.g1 > sg.g0) || sg.g1 < gA || sg.g0 > gB) continue;
          const at = [];
          if (sg.loop > 0) {
            if (hs < sg.lsrc - 1e-6 || hs >= sg.lsrc + sg.loop - 1e-6) continue;
            for (let q = hs >= sg.src - 1e-6 ? sg.g0 + (hs - sg.src) : sg.g0 + (sg.lsrc + sg.loop - sg.src) + (hs - sg.lsrc); q < sg.g1 - 1e-6; q += sg.loop) at.push(q);
          } else if (hs >= sg.src - 1e-6 && sg.g0 + (hs - sg.src) <= sg.g1 + 1e-6) at.push(sg.g0 + (hs - sg.src));
          for (const q of at) {
            const x = Math.round(XB(q));
            if (x < -60 || x > w + 2 || seen.has(`${hc.pad}:${x}`)) continue;
            seen.add(`${hc.pad}:${x}`);
            g.fillStyle = hc.color;
            g.globalAlpha = 0.75;
            g.fillRect(x, top, 1, H - top);
            g.globalAlpha = 1;
            const label = `${hc.pad} ${hc.name}`;
            g.fillRect(x, H - 13, g.measureText(label).width + 6, 12);
            g.fillStyle = "#0b0d11";
            g.fillText(label, x + 3, H - 3);
          }
        }
      }
      g.font = "10px ui-monospace, Menlo, monospace";
    }
    // 已播部分压暗；播放头
    g.fillStyle = "rgba(0,0,0,.28)";
    g.fillRect(0, top, cx, H - top);
    g.strokeStyle = "#fff"; g.lineWidth = 2;
    g.beginPath(); g.moveTo(cx, 0); g.lineTo(cx, H); g.stroke();
    g.lineWidth = 1;
    if (!clips.length) {
      const nx = (this.deckClips[d] || []).find((c) => c.start >= beat);
      g.fillStyle = "#666e7d"; g.font = "12px sans-serif";
      g.fillText(nx ? `待命 · 下一段 ${nx.id}「${nx.title}」在全场 ${posText(nx.start)}` : "这台 Deck 后面没有片段了", 10, mid + 4);
    }
    if (o.hoverX !== null && this.onSeek) {
      const hb = arr[clamp(Math.round(o.hoverX), 0, w)];
      if (hb >= 0 && hb < (this.chart.length || 0)) drawSeekHover(g, o.hoverX, H, `▶ 从全场第 ${barOf(hb)} 小节放`);
    }
    // 信息条：片段、原曲第几小节、拍、乐句、速度、还剩多久
    const nxt = cur || (this.deckClips[d] || []).find((c) => c.start >= beat) || null;
    setText(o.title, cur ? `${cur.id} ${cur.title}` : nxt ? `下一段 ${nxt.id} ${nxt.title}` : "（空）");
    if (cur) {
      const src = srcAt(cur, beat) + 1e-3;
      const bi = Math.floor(src / BEATS), bt = Math.floor(src - bi * BEATS);
      setText(o.barEl, `原曲 第 ${bi + 1} 小节`);
      o.dots.forEach((dEl, i) => dEl.classList.toggle("on", i === bt));
      setText(o.phrase, `乐句 ${(bi % 16) + 1}/16`);
      setText(o.bpm, fmtBpm(bpmNow));
      const pc = (bpmNow / cur.bpm - 1) * 100;
      setText(o.pitch, Math.abs(pc) < 0.005 ? " 原速" : ` ${pc > 0 ? "+" : ""}${pc.toFixed(2)}%`);
      const left = Math.max(0, ((cur.stop - beat) * 60) / bpmNow);
      setText(o.time, `-${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}`);
    } else {
      setText(o.barEl, nxt ? `${Math.max(0, Math.ceil((nxt.start - beat) / BEATS))} 小节后进` : "–");
      o.dots.forEach((dEl) => dEl.classList.remove("on"));
      setText(o.phrase, "");
      setText(o.bpm, nxt ? fmtBpm(nxt.bpm) : "–");
      setText(o.pitch, nxt ? " 原曲的 BPM" : "");
      setText(o.time, "");
    }
  }

  // 拍数变了才做的事：位置、场景、角色
  _tick(beat) {
    setText(this.posEl, posText(beat));
    const sc = sceneAt(this.chart, beat);
    const no = sc ? sc.no : 0;
    if (no !== this._sceneNo) { this._sceneNo = no; this._renderScene(sc, beat); }
    this._renderDeckInfo(beat);
    setText(this.nowEl, posText(beat));
    if (!this._status()) setText(this.bpmEl, fmtBpm(tempoAt(this.chart, beat)));
    this._renderTime(beat);
  }

  // ---------------------------------------------------------------- 跟引擎状态走的 DOM

  _renderState() {
    if (!this.chart || !this.lnRows) return;
    const st = this._status(), e = this.engine;
    const beat = this._engineBeat();
    const stage = st ? STATE_ZH[st.state] ?? st.state
      : (this.progress && { starting: "连接引擎…", preparing: "准备中…", running: "准备中…", stopping: "停止中…", stopped: "已停止", done: "整场放完了", error: "出错停止" }[this.progress.stage]) || "没在放";
    setText(this.stageEl, stage);
    setClass(this.stageEl, "l5-stage" + (stage ? "" : " hidden") + (st && st.state === "paused" ? " warn" : ""));
    if (st) setText(this.bpmEl, fmtBpm(st.bpm));
    const fast = st && e.speed && e.speed !== 1;
    setText(this.speedEl, fast ? `${e.speed}× 速放` : "");
    setClass(this.speedEl, "l5-speed dim" + (fast ? "" : " hidden"));
    const paused = st && (st.state === "paused" || st.state === "pausing");
    setText(this.pauseBtn, paused ? "▶ 继续" : "⏸ 暂停");
    this.pauseBtn.disabled = !st || !["running", "paused"].includes(st.state);
    this.jumpBtn.disabled = !this.onSeek;
    this.root.classList.toggle("seekable", !!this.onSeek);
    const warn = [];
    if (st && st.late > 0) warn.push(`引擎晚了 ${st.late} 次`);
    if (st && st.misses > 0) warn.push(`读盘没跟上 ${st.misses} 次`);
    setText(this.lateEl, warn.join("，"));
    setClass(this.lateEl, "l5-late" + (warn.length ? "" : " hidden"));
    this._renderMixer(st);
    this._renderDecks(st);
    this._renderDeckInfo(this._beatNow());
    this._renderActions();
    if (this._sceneNo === undefined) this._tick(beat);
  }

  // 一台 Deck 现在的状态（只看引擎报的）
  _deckState(d, st) {
    const rc = (this.engine && this.engine.receipts) || {};
    const tails = (st.fx || []).filter((f) => f.tail && (clipById(this.chart, f.clip) || {}).deck === d.deck_id);
    const failed = [d.clip_id, d.next_clip].filter(Boolean).map((id) => rc[`${id}.load`]).find((r) => r && r.st === "failed");
    if (d.state === "error" || failed) return { k: "error", text: "✕ 出错：没装上" + (failed && failed.msg ? `（${failed.msg}）` : "") };
    if (d.state === "looping") return { k: "loop", text: `↻ 循环 第 ${d.loop_lap || 1} 圈`, quiet: d.audible === false };
    if (d.state === "playing") return { k: "play", text: "▶ 播放", quiet: d.audible === false };
    if (d.state === "loading" || d.state === "ready") return { k: "ready", text: `准备下一段 ${d.clip_id || d.next_clip || ""}，还没出声` };
    if (tails.length) return { k: "tail", text: `≈ 只剩尾音（${tails.map((f) => f.id).join("、")}）` };
    if (d.state === "stopped") return { k: "stop", text: "■ 已停" };
    const nx = d.next_clip ? clipById(this.chart, d.next_clip) : null;
    return { k: "idle", text: nx ? `○ 待命，下一段 ${nx.id}（${posText(nx.start)}）` : "○ 待命" };
  }

  // 谱上第 beat 拍这台 Deck 的旋钮在哪（没有片段 = null）
  _planAt(d, beat) {
    const c = this._deckClipAt(d, beat);
    if (!c) return null;
    const out = {};
    for (const p of LANE_PARAMS) { const v = laneAt(c, p, beat); if (!v) return null; out[p] = v; }
    return out;
  }

  // 调音台：旋钮、推子、电平 = 引擎读回的原生值（和两 Deck 的调音台一样）；刚拖过的先照手上的值画。
  // 没在放的时候（引擎没有状态）：照谱上游标那一拍的值摆，表示「从这里开始会是这样」
  _renderMixer(st) {
    if (!this.ch) return;
    const live = !!st && ["running", "paused", "pausing"].includes(st.state);
    const rest = st ? null : this._engineBeat();
    for (const d of this.decks) {
      const c = this.ch[d], ds = st && (st.decks || []).find((x) => x.deck_id === d);
      const plan = st ? null : this._planAt(d, rest);
      const ctl = ds ? ds.controls : plan ? Object.fromEntries(LANE_PARAMS.map((p) => [p, nativeOf(p, plan[p].v)]))
        : st ? null : { hi: 1, mid: 1, lo: 1, filter: 0.5, vol: 0 };
      const usable = !!(live && ds && ds.clip_id);
      c.usable = usable;
      c.ds = ds;
      const manual = [];
      for (const p of ["hi", "mid", "lo", "filter", "vol"]) {
        const key = `${d}.${p}`;
        if (ds && (ds.manual || []).includes(p)) delete this.localManual[key]; // 引擎已经认了：以后只看引擎的
        else if (!this._isManual(d, p, null)) { delete this.returning[key]; delete this.localValue[key]; }
        const man = usable && this._isManual(d, p, ds);
        if (man) manual.push(p);
        const held = this.drag && this.drag.key === key;
        const local = this.localValue[key];
        const v = held || (local && performance.now() - local.t < 600) ? nativeOf(p, (held ? this.drag : local).v) : ctl ? ctl[p] : null;
        const w = p === "vol" ? c.fader : c.k[p];
        if (v !== null && v !== undefined) w.set(v);
        w.root.classList.toggle("manual", man);
        w.root.classList.toggle("usable", usable);
      }
      // 发送效果：这台 Deck 现在的片段送出去多少（Delay / Echo 一个旋钮、Reverb 一个）
      const sends = st ? (st.fx || []).filter((f) => f.clip && (clipById(this.chart, f.clip) || {}).deck === d && !f.bus) : [];
      const a = sends.find((f) => f.kind === "echo" || f.kind === "delay"), b = sends.find((f) => f.kind === "reverb");
      const la = a && a.kind === "delay" ? "DELAY" : "ECHO";
      const kl = c.fxA.root.querySelector(".kl");
      if (kl.textContent !== la) kl.textContent = la;
      c.fxA.set(a ? clamp(a.send * (a.wet ?? 1), 0, 1) : 0);
      c.fxB.set(b ? clamp(b.send * (b.wet ?? 1), 0, 1) : 0);
      // 现场效果（插入 / 组 / 总输出里落在这台 Deck 上的）：开着的亮
      const units = (this.chart.fx || []).filter((f) => f.route !== "send" && (f.decks || []).includes(d));
      const on = st ? units.filter((f) => (st.fx || []).some((x) => x.id === f.id && x.on !== false && !x.tail)) : [];
      const sig = on.map((f) => f.id).join(",");
      if (c.live._sig !== sig) { c.live._sig = sig; c.live.innerHTML = ""; put(c.live, on.slice(0, 3).map((f) => el("span", { class: "l5-fxon", title: `${f.id} ${f.name}：${f.text || ""}` }, f.name))); }
      if (!ds || !ds.clip_id || !live) c.vu.set(-90, null);
      else c.vu.set(ds.level, null);
      const pc = st ? null : this._deckClipAt(d, rest);
      setText(c.clip, ds && ds.clip_id ? ds.clip_id : st ? "空" : pc ? pc.id : "空");
      c.root.classList.toggle("off", !st);
      setClass(c.back, "l5-back" + (manual.length ? "" : " hidden"));
      setText(c.manualEl, manual.length ? `手动：${manual.map((p) => LANE_ZH[p]).join("、")}` : "");
      const back = manual.map((p) => this.returning[`${d}.${p}`]).find(Boolean);
      c.beatBtn.disabled = c.barBtn.disabled = !!back;
      setText(c.beatBtn, back === "beat" ? "回谱中…" : "下一拍回谱");
      setText(c.barBtn, back === "bar" ? "回谱中…" : "下一小节回谱");
      c.manual = manual;
    }
    const m = st && st.master;
    this.mvu.set(m ? m.level : -90, m ? m.peak : null);
    setText(this.masterDb, m ? (m.level <= -90 ? "–∞" : m.level.toFixed(0)) : "–");
    setClass(this.overLamp, "l5-over" + (m && m.over_now ? " on" : m && m.over > 0 ? " was" : ""));
    setText(this.overCount, m && m.over > 0 ? `过载 ${m.over} 次` : "");
    setClass(this.overCount, "l5-overn" + (m && m.over > 0 ? "" : " hidden"));
    // 曲线左边的小旋钮和读数：同一个数
    for (const d of this.decks) {
      const ds = st && (st.decks || []).find((x) => x.deck_id === d);
      const plan = st ? null : this._planAt(d, rest);
      for (const p of LANE_PARAMS) {
        const o = this.gLane && this.gLane[`${d}.${p}`];
        if (!o) continue;
        const on = !!(ds && ds.clip_id && ds.human && ds.human[p] && ds.controls);
        if (on) o.mini.set(ds.controls[p]);
        else if (plan) o.mini.set(nativeOf(p, plan[p].v));
        setText(o.val, on ? ds.human[p].text : plan ? plan[p].text.split(" → ")[0] : "–");
        const man = on && this._isManual(d, p, ds);
        setClass(o.cell, `l5-g l5-g-param${on ? "" : " off"}${man ? " manual" : ""}`);
        setClass(o.man, "l5-vm" + (man ? "" : " hidden"));
      }
    }
  }

  _renderDecks(st) {
    for (const d of this.decks) {
      const ds = st && (st.decks || []).find((x) => x.deck_id === d);
      const s = ds ? this._deckState(ds, st) : { k: "idle", text: st ? "○ 待命" : "" };
      const o = this.dp && this.dp[d];
      if (o) {
        setText(o.state, s.text);
        setClass(o.state, `l5-dstate st-${s.k}`);
        setClass(o.quiet, "l5-quiet" + (s.quiet ? "" : " hidden"));
        o.root.classList.toggle("playing", s.k === "play" || s.k === "loop");
      }
    }
  }

  // 行头上的角色、片段名（文字跟着场景和片段走；Deck 的颜色 / 编号不变）
  _renderDeckInfo(beat) {
    const roles = sceneRoles(this.chart, sceneAt(this.chart, beat), beat);
    for (const d of this.decks) {
      const c = this._deckClipAt(d, beat);
      const role = roles[d] || "待命";
      const o = this.dp && this.dp[d];
      const kindZh = c ? (c.main ? "主线" : "工具") : "";
      if (o) {
        setText(o.role, c ? `${kindZh} · ${role}` : role);
        setClass(o.role, "deck-role" + (c ? (c.main ? " main" : " tool") : ""));
        o.role.style.display = role ? "" : "none";
        o.root.classList.toggle("tool", !!c && !c.main);
      }
      const tr = this.ov && this.ov[d];
      if (tr) { // 整首曲子那一行：片段名和小节数在 _drawTracks 里写，这里只管角色
        setText(tr.role, role);
        setClass(tr.role, "l5-role" + (role === "待命" ? " dim" : ""));
      }
      const gh = this.gHead && this.gHead[d];
      if (gh) setText(gh.who, `${kindZh ? kindZh + " · " : ""}${role}${c ? " · " + c.id + " " + c.title : ""}`);
      const mc = this.ch && this.ch[d];
      if (mc) { setText(mc.kind, kindZh); setClass(mc.kind, `l5-kind ${c ? (c.main ? "main" : "tool") : "hidden"}`); }
    }
    // 头上的摘要：主线是谁、工具有哪些、谁在待命
    const groups = { main: [], tool: [], idle: [] };
    for (const d of this.decks) {
      const c = this._deckClipAt(d, beat);
      groups[c ? (c.main ? "main" : "tool") : "idle"].push([d, c ? roles[d] || "" : "待命", c]);
    }
    const sig = JSON.stringify([groups.main, groups.tool, groups.idle].map((gp) => gp.map(([d, r, c]) => [d, r, c && c.id])));
    if (sig !== this._rolesSig) {
      this._rolesSig = sig;
      this.rolesEl.innerHTML = "";
      const part = (label, cls, list) => (list.length ? [el("span", { class: `l5-kind ${cls}` }, label),
        list.map(([d, r, c]) => el("span", { class: "l5-rolechip" + (cls === "idle" ? " dim" : "") }, chip(d), el("b", {}, r), c ? el("span", { class: "dim" }, `${c.id} ${c.title}`) : null))] : []);
      put(this.rolesEl, part("主线", "main", groups.main), part("工具", "tool", groups.tool), part("待命", "idle", groups.idle));
    }
    const gm = this.gHead && this.gHead[0];
    if (gm) setText(gm.who, "直接进主输出的（采样、总输出上的效果）");
  }

  _renderScene(sc, beat) {
    setText(this.sceneEl, sc ? `${sc.no}${sc.name ? " " + sc.name : ""}` : "–");
    this.draftEl.classList.toggle("hidden", !(sc && sc.draft));
    this._rolesSig = ""; // 角色摘要在 _renderDeckInfo 里写（它跟着片段走）
    // 策略
    const S = this.stratEl;
    S.innerHTML = "";
    if (!sc) { put(S, el("div", { class: "l5-h" }, "策略"), el("div", { class: "dim" }, "这一段不在任何场景里。")); return; }
    put(S, el("div", { class: "l5-h" }, "策略", el("span", {}, `场景 ${sc.no}${sc.name ? " " + sc.name : ""}`), sc.draft ? el("span", { class: "badge warn" }, "草稿") : null,
      el("span", { class: "dim" }, `${posText(sc.start)} – ${posText(sc.end)} · 谱第 ${sc.line} 行`)));
    const dl = el("dl", {});
    for (const k of STRATEGY) {
      const v = ((sc.strategy || {})[k] || "").trim();
      const missing = !v || (sc.missing || []).includes(k);
      put(dl, el("dt", {}, k), el("dd", { class: missing ? "l5-todo" : "" }, missing ? "待补充" : v));
    }
    put(S, dl);
  }

  // ---------------------------------------------------------------- 现在的 EQ 操作（大字：哪台 Deck 的哪个旋钮正在拧、拧到哪、还剩几小节）

  // 谱上的值写成人话：EQ = 钟点位，Filter = 左 / 右几点，推子 = 0–10
  _say(param, v) {
    if (param === "vol") return (clamp(v, 0, 1) * 10).toFixed(1).replace(/\.0$/, "");
    if (param === "filter") return clockText(clamp(v, -1, 1) * 5, true);
    return clockText(eqHours(v));
  }

  _renderEqOps(beat) {
    if (!this.chart || !this.eqEl) return;
    const isEq = (p) => p === "hi" || p === "mid" || p === "lo";
    const now = [], other = [], held = [];
    let next = null;
    for (const d of this.decks) {
      const cur = this._deckClipAt(d, beat);
      for (const k of this.deckClips[d] || []) {
        if (k.stop <= beat) continue;
        for (const p of LANE_PARAMS) {
          const pts = (k.lanes || {})[p] || [];
          for (let i = 0; i + 1 < pts.length; i++) {
            const a = pts[i], b = pts[i + 1];
            if (Math.abs(b.v - a.v) < 1e-6) continue;
            const at = b.step ? b.at : a.at; // 一下到位的在那一拍；渐变的从上一个点开始拧
            if (k === cur && !b.step && a.at <= beat && beat < b.at) (isEq(p) ? now : other).push({ d, p, a, b });
            else if (at > beat && isEq(p) && (!next || at < next.at)) next = { d, p, a, b, at, step: !!b.step };
          }
        }
      }
      if (cur) for (const p of ["hi", "mid", "lo"]) {
        const v = laneAt(cur, p, beat);
        if (v && !v.ramp && Math.abs(v.v) > 0.5) held.push({ d, p, v: v.v });
      }
    }
    const roles = sceneRoles(this.chart, sceneAt(this.chart, beat), beat);
    const who = (d) => el("b", { style: `color:${DECK_HEX[d]}` }, `D${d}${roles[d] && roles[d] !== "待命" ? " " + roles[d] : ""}`);
    const what = (p) => el("b", { style: isEq(p) ? `color:${BAND_HEX[p]}` : "" }, LANE_ZH[p]);
    const bars = (n) => (n / BEATS).toFixed(1);
    const out = [el("div", { class: "tl-sec big" }, "现在的 EQ 操作")];
    for (const m of now) {
      const f = (beat - m.a.at) / (m.b.at - m.a.at), v = m.a.v + (m.b.v - m.a.v) * f;
      const ds = this._deckSt(m.d), man = ds && (ds.manual || []).includes(m.p);
      out.push(el("div", { class: "eq-op", style: `border-color:${BAND_HEX[m.p]}` },
        who(m.d), " ", what(m.p), ` ${this._say(m.p, m.a.v)} → ${this._say(m.p, m.b.v)}`,
        el("span", { class: "eq-op-now" }, man ? `手动接管中：${ds.human[m.p].text}` : `现在 ${this._say(m.p, v)}（${v <= EQ_CUT ? "切" : (v > 0 ? "+" : "") + v.toFixed(1) + " dB"}）`),
        el("span", { class: "eq-op-left" }, `还剩 ${bars(m.b.at - beat)} 小节`)));
    }
    const still = held.filter((h) => !now.some((m) => m.d === h.d && m.p === h.p));
    if (still.length) out.push(el("div", { class: "eq-held" }, "保持：", still.map((h, i) => [i ? "、" : "", who(h.d), " ", what(h.p), ` ${this._say(h.p, h.v)}`])));
    if (!now.length) out.push(el("div", { class: "eq-op idle" }, "EQ 现在没有在动"));
    if (next) {
      out.push(el("div", { class: "eq-next" }, `下一个 EQ 动作：全场 ${posText(next.at)} `, who(next.d), " ", what(next.p),
        ` ${this._say(next.p, next.a.v)} → ${this._say(next.p, next.b.v)}`, next.step ? "（一下到位）" : `，用 ${durText(next.b.at - next.a.at)}`,
        `（还有 ${bars(next.at - beat)} 小节）`));
    } else out.push(el("div", { class: "eq-next" }, "后面没有 EQ 动作了"));
    if (other.length) {
      out.push(el("div", { class: "eq-other" }, "同时：", other.map((m, i) => [i ? "；" : "", who(m.d), " ", what(m.p),
        ` ${this._say(m.p, m.a.v)} → ${this._say(m.p, m.b.v)}，还剩 ${bars(m.b.at - beat)} 小节`])));
    }
    // 一秒四次：字没变就不动 DOM
    const sig = out.map((n) => n.textContent).join("|");
    if (sig === this._eqSig) return;
    this._eqSig = sig;
    this.eqEl.replaceChildren(...out.flat(Infinity));
  }

  // ---------------------------------------------------------------- 接下来（状态只看回执）

  _actionStatus(a, beat) {
    const rc = (this.engine && this.engine.receipts) || {};
    const live = this._isLive();
    if (a.rc) {
      const r = rc[a.rc];
      if (r) {
        if (r.st === "ok") return { k: "ok", text: "✓" };
        // 正好落在起点这一拍上的：引擎是摆成那样再开始的（不是又做了一遍）
        if (r.st === "restored") return { k: "ok", text: "✓ 起点", sub: "从这一拍开始，已经按谱摆好" };
        if (r.st === "late") return { k: "late", text: "晚了", sub: `计划 ${posTextFine(r.beat)}，实际 ${posTextFine(r.at)}${r.msg ? "，" + r.msg : ""}` };
        if (r.st === "skipped") return { k: "skipped", text: "跳过", sub: r.msg || "" };
        return { k: "failed", text: "失效", sub: r.msg || "" };
      }
      // 到点了还没有回执：先等两拍；再没有就照实写「没有回执」
      if (live && a.at <= beat) return beat < a.at + 2 ? { k: "wait", text: "等回执" } : { k: "none", text: "没有回执" };
      return { k: "", text: "" };
    }
    // 没有回执的（连续拧的旋钮、效果、速度）：引擎的拍在它的起止之间 = 进行中；别的时候不打勾
    if (live && a.until !== null && a.until !== undefined && a.at <= beat && beat < a.until) return { k: "going", text: "进行中" };
    return { k: "", text: "" };
  }

  _renderActions() {
    if (!this.chart) return;
    const st = this._status(), rc = (this.engine && this.engine.receipts) || {};
    const beat = this._engineBeat();
    const from = this.engine ? this.engine.from_beat || 0 : 0;
    const all = this.chart.actions || [];
    const ongoing = (a) => !a.rc && a.until !== null && a.until !== undefined && a.at <= beat && beat < a.until;
    // 这一场没经过的（从后面的场景开始时，前面的动作）不列；有回执的（恢复现场时装上、起播的）照列
    const past = all.filter((a) => a.at <= beat && !ongoing(a) && (a.at >= from || (a.rc && rc[a.rc])));
    const now = all.filter(ongoing);
    const next = all.filter((a) => a.at > beat && a.at <= beat + 4 * BEATS);
    const later = all.filter((a) => a.at > beat + 4 * BEATS).slice(0, Math.max(0, 3 - next.length));
    const pastShown = this.showPast ? past : past.slice(-4);
    const rowsOf = (list, kind) => list.map((a) => {
      const s = this._actionStatus(a, beat);
      let when = "";
      if (kind === "next" || kind === "later") { const n = a.at - Math.floor(beat); when = n <= 2 * BEATS ? `还有 ${Math.max(1, Math.ceil(a.at - beat))} 拍` : `${Math.round(n / BEATS)} 小节后`; }
      return { a, kind, s, when };
    });
    const rows = [...rowsOf(pastShown, "past"), ...rowsOf(now, "now"), { divider: true }, ...rowsOf(next, "next"), ...rowsOf(later, "later")];
    const sig = rows.map((r) => (r.divider ? "|" : `${r.a.n}${r.kind}${r.s.k}${r.s.sub || ""}${r.when}`)).join(",") + (this.showPast ? "P" : "") + past.length;
    // 头上那一行「下一步」
    const nx = next[0] || later[0];
    setText(this.nextEl, nx ? `${nx.pos}  ${nx.text}（${rowsOf([nx], "next")[0].when}）` : st ? "后面没有动作了" : "–");
    if (sig === this._actSig) return;
    this._actSig = sig;
    setText(this.pastBtn, this.showPast ? "只看最近的" : `回看更早的（${past.length}）`);
    this.pastBtn.classList.toggle("hidden", past.length <= 4);
    const keep = this.actsEl.scrollTop, atEnd = this.actsEl.scrollTop + this.actsEl.clientHeight >= this.actsEl.scrollHeight - 4;
    this.actsEl.innerHTML = "";
    for (const r of rows) {
      if (r.divider) { put(this.actsEl, el("div", { class: "l5-nowline" }, "现在 ", this.nowEl)); continue; }
      const { a, s } = r;
      put(this.actsEl, el("div", { class: `l5-act ${r.kind} k-${a.kind}` + (s.k ? ` s-${s.k}` : "") },
        el("span", { class: "l5-apos" }, a.pos),
        a.deck ? chip(a.deck) : el("span", { class: "l5-chip none" }),
        el("span", { class: "l5-atxt" }, a.kind === "mark" ? el("span", { class: "l5-note" }, "备注 ") : null, a.text,
          s.sub ? el("span", { class: "l5-asub" }, s.sub) : null),
        el("span", { class: "l5-ast" }, s.text || r.when)));
    }
    if (!next.length && !later.length && !now.length) put(this.actsEl, el("div", { class: "dim l5-act" }, "后面没有动作了。"));
    this.actsEl.scrollTop = this.showPast && !atEnd ? keep : this.actsEl.scrollHeight;
  }

  // ---------------------------------------------------------------- 暂停 / 跳转

  async _call(method, url, body) {
    try { return await this.api(method, url, body); } catch (e) { this.toast(e.message, true); return null; }
  }

  async _pauseResume() {
    const st = this._status();
    if (!st) return;
    this.pauseBtn.disabled = true;
    await this._call("POST", st.state === "paused" ? "/api/v5/resume" : "/api/v5/pause");
  }

  _closeBubble() {
    if (this.bubble) { this.bubble.remove(); this.bubble = null; }
  }

  // 跳转前先问一句：anchor = 气泡贴着哪（元素，或某张图上的 {el, x, y}）；ask = 让用户填小节号
  _askSeek(beat, anchor, ask = false, what = "") {
    this._closeBubble();
    if (!this.onSeek || !this.chart) return;
    const live = this._isLive();
    const maxBar = Math.max(1, Math.round((this.chart.length || 0) / BEATS));
    let bar = beat === null ? barOf(this._beatNow()) : Math.max(1, Math.min(maxBar, barOf(beat)));
    const msg = el("div", { class: "l5-bub-msg" });
    const go = el("button", { class: "primary" }, live ? "重新开始" : "开始");
    const input = ask ? el("input", { type: "number", min: 1, max: maxBar, step: 1, value: bar, "aria-label": "小节" }) : null;
    const say = () => {
      msg.textContent = `从 ${what ? what + "（" : ""}全场 ${bar}.1${what ? "）" : ""} ${live ? "重新开始（会结束手动接管，旋钮回到谱上的位置）" : "开始放"}`;
    };
    say();
    const run = async () => {
      go.disabled = true;
      go.textContent = "跳转中…";
      this.follow = true;
      this._syncTools();
      try { await this.onSeek((bar - 1) * BEATS); } finally { this._closeBubble(); }
    };
    go.addEventListener("click", run);
    if (input) {
      input.addEventListener("input", () => { bar = Math.max(1, Math.min(maxBar, Math.round(+input.value || 1))); say(); });
      input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") run(); });
    }
    const b = (this.bubble = el("div", { class: "l5-bubble", role: "dialog" },
      input ? el("label", { class: "l5-bub-in" }, "跳到第 ", input, ` 小节（共 ${maxBar} 小节）`) : null, msg,
      el("div", { class: "l5-row" }, go, el("button", { onclick: () => this._closeBubble() }, "取消"))));
    put(this.root, b);
    const R = this.root.getBoundingClientRect();
    let x, y;
    if (anchor instanceof Element) { const a = anchor.getBoundingClientRect(); x = a.left - R.left; y = a.bottom - R.top + 6; } else {
      const a = anchor.el.getBoundingClientRect(); x = a.left - R.left + anchor.x - 20; y = a.top - R.top + anchor.y + 10;
    }
    b.style.left = `${Math.max(4, Math.min(R.width - b.offsetWidth - 4, x))}px`;
    b.style.top = `${y}px`;
    (input || go).focus();
    if (input) input.select();
  }

  // ---------------------------------------------------------------- 总览 / 曲线上的鼠标：拖 = 前后看，点 = 从那里放，悬停 = 说明

  _bindPlot(c) {
    const xy = (ev) => { const r = c.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; };
    c.addEventListener("pointerdown", (ev) => {
      if (!this.chart || ev.button) return;
      const { x, y } = xy(ev);
      this.pan = { x, y, b0: this.b0, moved: false };
      c.setPointerCapture(ev.pointerId);
    });
    c.addEventListener("pointermove", (ev) => {
      if (!this.chart || !this.lnRows) return;
      const { x, y } = xy(ev);
      const d = this.pan;
      if (d) {
        if (!d.moved && Math.abs(x - d.x) < 4) return;
        d.moved = true;
        this.ltip.classList.add("hidden");
        this.hover = null;
        this._pan(d.b0 - (x - d.x) / this.ppb);
        return;
      }
      this.hover = { x, y };
      const cur = this.onSeek ? "pointer" : "grab";
      if (c.style.cursor !== cur) c.style.cursor = cur;
      this._showTip(x, y);
      this._kick(true);
    });
    c.addEventListener("pointerup", () => {
      const d = this.pan;
      this.pan = null;
      if (!d || d.moved || !this.onSeek) return;
      const beat = this.b0 + d.x / this.ppb;
      if (beat >= 0 && beat < (this.chart.length || 0)) this._askSeek(beat, { el: c, x: d.x, y: d.y });
    });
    c.addEventListener("pointercancel", () => { this.pan = null; });
    c.addEventListener("pointerleave", () => { this.hover = null; this.ltip.classList.add("hidden"); this._kick(true); });
    c.addEventListener("wheel", (ev) => {
      const dx = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.shiftKey ? ev.deltaY : 0;
      if (!dx || !this.chart) return; // 竖着滚 = 滚页面
      ev.preventDefault();
      this._pan(this.b0 + dx / this.ppb);
    }, { passive: false });
  }

  // 悬停：这一拍上这一行的说明（图上放不下的字都在这里）
  _showTip(x, y) {
    const T = this.ltip, W = this.W, H = this.lnH;
    const row = this.lnRows.find((r) => y >= r.y && y < r.y + r.h) || null;
    const beat = this.b0 + x / this.ppb;
    if (!row || beat < -LEAD_IN || beat > (this.chart.length || 0) + BEATS) { T.classList.add("hidden"); return; }
    const key = `${row.kind}${row.deck || ""}${row.param || ""}:${Math.floor(beat)}:${row.kind === "head" ? Math.floor((y - row.y - 3) / LN.band) : ""}`;
    if (key !== this._tipKey) {
      this._tipKey = key;
      const lines = this._tipLines(row, beat, y);
      T.innerHTML = "";
      for (const l of lines) put(T, l instanceof Node ? l : el("div", {}, l));
      this._tipEmpty = !lines.length;
    }
    T.classList.toggle("hidden", !!this._tipEmpty);
    if (this._tipEmpty) return;
    const w = T.offsetWidth, h = T.offsetHeight;
    T.style.left = `${Math.max(2, Math.min(W - w - 2, x + 14))}px`;
    T.style.top = `${y + 18 + h > H ? Math.max(2, y - h - 10) : y + 18}px`;
  }

  _tipLines(row, beat, y) {
    const ch = this.chart, pos = posText(beat);
    const head = (t) => el("div", { class: "l5-tip-h" }, t);
    const click = this.onSeek && beat >= 0 ? el("div", { class: "dim" }, `点一下：从全场 ${barOf(beat)}.1 重新开始`) : null;
    if (row.kind === "ruler" || row.kind === "scene") {
      const sc = sceneAt(ch, beat);
      return [head(`全场 ${pos}`), sc ? `场景 ${sc.no}${sc.name ? " " + sc.name : ""}${sc.draft ? "（草稿）" : ""}` : "不在场景里",
        `主速 ${fmtBpm(tempoAt(ch, beat))}`, click].filter(Boolean);
    }
    if (row.kind === "param") {
      const c = clipAt(ch, row.deck, beat), v = c && laneAt(c, row.param, beat);
      if (!v) return [];
      return [head(`D${row.deck} ${PARAM_ZH[row.param]} · 全场 ${pos}`), el("div", { class: "l5-tip-v" }, "谱上：", el("b", {}, v.text),
        v.ramp ? el("span", { class: "dim" }, `渐变中，到 ${posText(v.to.at)}`) : null, el("span", { class: "dim" }, c.id))];
    }
    // 组头：这一行画的是效果
    const lane = Math.floor((y - row.y - 3) / LN.band);
    const it = row.fx.items.find((i) => i.lane === lane && beat >= i.f.start && beat < i.f.end + (i.f.tail || 0));
    if (!it) return [];
    const f = it.f;
    return [head(`${f.id} ${f.name}${f.route_zh ? "（" + f.route_zh + "）" : ""}`), `作用范围：${f.scope || (f.clips || []).join("、")}`,
      `${posText(f.start)} – ${posText(f.end)}` + (f.tail > 0 ? `，关了以后尾音留 ${durText(f.tail)}` : ""), f.text || null,
      el("div", { class: "dim" }, `谱第 ${f.line} 行`)].filter(Boolean);
  }

  // ---------------------------------------------------------------- 手动接管：拖调音台上的旋钮 / 推子

  _isManual(deck, param, d) {
    if (d && (d.manual || []).includes(param)) return true;
    const t = this.localManual[`${deck}.${param}`];
    return !!t && performance.now() - t < 2000; // 刚拖过：引擎报回来之前先标上；2 秒还没报回来（没发成）就撤掉
  }

  // 这台 Deck 这个旋钮现在的值（谱上的单位）：引擎报的
  _human(deck, param) {
    const ds = this._deckSt(deck);
    return ds && ds.human && ds.human[param] ? ds.human[param].v : null;
  }

  // 旋钮：按住上下拖（往上 = 顺时针）；双击 = 下一拍回谱
  _dragKnob(knob, deck, param) {
    const s = knob.svg;
    s.classList.add("l5-drag");
    s.addEventListener("pointerdown", (ev) => {
      const c = this.ch[deck];
      if (ev.button || !c || !c.usable) return;
      const v0 = this._human(deck, param);
      if (v0 === null) return;
      ev.preventDefault();
      s.setPointerCapture(ev.pointerId);
      this.drag = { key: `${deck}.${param}`, y: ev.clientY, v0, v: v0, moved: false };
    });
    s.addEventListener("pointermove", (ev) => {
      const d = this.drag;
      if (!d || d.key !== `${deck}.${param}`) return;
      const dy = ev.clientY - d.y;
      if (!d.moved && Math.abs(dy) < 2) return;
      d.moved = true;
      let v;
      if (param === "filter") { v = clamp(d.v0 - dy / 80, -1, 1); if (Math.abs(v) < 0.04) v = 0; v = Math.round(v * 100) / 100; } else {
        let h = clamp(eqHours(d.v0) - dy / 16, -5, 5); // 160 像素 = 从 7 点到 5 点
        if (Math.abs(h) < 0.2) h = 0;
        v = Math.round(eqDb(h) * 100) / 100;
      }
      if (v === d.v) return;
      d.v = v;
      this._takeover(deck, param, v);
    });
    const end = () => {
      const d = this.drag;
      if (!d || d.key !== `${deck}.${param}`) return;
      this.drag = null;
      if (d.moved) this.localValue[d.key] = { v: d.v, t: performance.now() };
      this._renderMixer(this._status());
    };
    s.addEventListener("pointerup", end);
    s.addEventListener("pointercancel", end);
    s.addEventListener("dblclick", () => { if (this._isManual(deck, param, this._deckSt(deck))) this._giveBack(deck, param, "beat"); });
    // 键盘：↑ ↓ 拧一小格（EQ 一刻钟、Filter 5%），Enter = 下一拍回谱
    s.setAttribute("tabindex", "0");
    s.setAttribute("role", "slider");
    s.setAttribute("aria-label", `D${deck} ${LANE_ZH[param]}`);
    s.addEventListener("keydown", (ev) => {
      const c = this.ch[deck];
      if (!c || !c.usable) return;
      if (ev.key === "Enter") { if (this._isManual(deck, param, this._deckSt(deck))) this._giveBack(deck, param, "beat"); return; }
      const dir = ev.key === "ArrowUp" || ev.key === "ArrowRight" ? 1 : ev.key === "ArrowDown" || ev.key === "ArrowLeft" ? -1 : 0;
      const local = this.localValue[`${deck}.${param}`];
      const cur = local && performance.now() - local.t < 600 ? local.v : this._human(deck, param);
      if (!dir || cur === null) return;
      ev.preventDefault();
      const v = param === "filter" ? Math.round(clamp(cur + dir * 0.05, -1, 1) * 100) / 100
        : Math.round(eqDb(clamp(Math.round(eqHours(cur) * 4 + dir) / 4, -5, 5)) * 100) / 100;
      this.localValue[`${deck}.${param}`] = { v, t: performance.now() };
      this._takeover(deck, param, v);
    });
  }

  // 推子：在槽里按住拖到哪就是哪；双击 = 下一拍回谱
  _dragFader(fader, deck) {
    const track = fader.root.querySelector(".fader-track");
    track.classList.add("l5-drag");
    const at = (ev) => { const r = track.getBoundingClientRect(); return Math.round(clamp(1 - (ev.clientY - r.top) / r.height, 0, 1) * 100) / 100; };
    track.addEventListener("pointerdown", (ev) => {
      const c = this.ch[deck];
      if (ev.button || !c || !c.usable) return;
      ev.preventDefault();
      track.setPointerCapture(ev.pointerId);
      this.drag = { key: `${deck}.vol`, v: at(ev), moved: true };
      this._takeover(deck, "vol", this.drag.v);
    });
    track.addEventListener("pointermove", (ev) => {
      const d = this.drag;
      if (!d || d.key !== `${deck}.vol`) return;
      const v = at(ev);
      if (v === d.v) return;
      d.v = v;
      this._takeover(deck, "vol", v);
    });
    const end = () => {
      const d = this.drag;
      if (!d || d.key !== `${deck}.vol`) return;
      this.drag = null;
      this.localValue[d.key] = { v: d.v, t: performance.now() };
      this._renderMixer(this._status());
    };
    track.addEventListener("pointerup", end);
    track.addEventListener("pointercancel", end);
    track.addEventListener("dblclick", () => { if (this._isManual(deck, "vol", this._deckSt(deck))) this._giveBack(deck, "vol", "beat"); });
    track.setAttribute("tabindex", "0");
    track.setAttribute("role", "slider");
    track.setAttribute("aria-label", `D${deck} 推子`);
    track.addEventListener("keydown", (ev) => {
      const c = this.ch[deck];
      if (!c || !c.usable) return;
      if (ev.key === "Enter") { if (this._isManual(deck, "vol", this._deckSt(deck))) this._giveBack(deck, "vol", "beat"); return; }
      const dir = ev.key === "ArrowUp" || ev.key === "ArrowRight" ? 1 : ev.key === "ArrowDown" || ev.key === "ArrowLeft" ? -1 : 0;
      const local = this.localValue[`${deck}.vol`];
      const cur = local && performance.now() - local.t < 600 ? local.v : this._human(deck, "vol");
      if (!dir || cur === null) return;
      ev.preventDefault();
      const v = Math.round(clamp(cur + dir * 0.05, 0, 1) * 100) / 100; // 半格
      this.localValue[`${deck}.vol`] = { v, t: performance.now() };
      this._takeover(deck, "vol", v);
    });
  }

  // 每个旋钮最多每秒发 20 次，上一条没回来就只留最新的值
  _takeover(deck, param, value) {
    const key = `${deck}.${param}`;
    this.localManual[key] = performance.now();
    delete this.returning[key];
    const q = this.pending[key] || (this.pending[key] = { last: 0, busy: false, timer: 0 });
    q.value = value;
    q.dirty = true;
    const pump = async () => {
      q.timer = 0;
      if (q.busy || !q.dirty) return;
      const wait = 50 - (performance.now() - q.last);
      if (wait > 0) { q.timer = setTimeout(pump, wait); return; }
      q.busy = true;
      q.dirty = false;
      q.last = performance.now();
      this.localManual[key] = q.last;
      await this._call("POST", "/api/v5/takeover", { deck, param, value: q.value });
      q.busy = false;
      if (q.dirty) pump();
    };
    if (!q.timer) pump();
    this._renderMixer(this._status());
    this._kick(true);
  }

  // 回谱：param = null → 这台 Deck 上所有手动的旋钮
  async _giveBack(deck, param, when) {
    const c = this.ch[deck];
    const params = param ? [param] : [...((c && c.manual) || [])];
    if (!params.length) return;
    for (const p of params) this.returning[`${deck}.${p}`] = when;
    this._renderMixer(this._status());
    for (const p of params) {
      const key = `${deck}.${p}`;
      const r = await this._call("POST", "/api/v5/return", { deck, param: p, when, ramp: 1 });
      if (!r) delete this.returning[key];
      delete this.localManual[key];
      delete this.localValue[key];
    }
    this._renderMixer(this._status());
  }
}
