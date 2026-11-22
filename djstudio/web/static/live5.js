// 曲谱 v5 的演出界面：四台 Deck 共用一条全场小节轴。
//
// 自上而下：全场位置 / 主速 / 当前场景 / 暂停、跳转 / 主输出 → 角色 → 场景 → 共轴图（标尺、D1–D4 播放带、
// LOW / MID / HIGH / Filter / 推子、效果 / Loop 带）→ 策略、接下来、手动接管。
//
// - 图是谱（编译出的 chart）；「现在」的旋钮位置、Deck 的状态、动作做完没有，全部来自引擎报的状态和回执，
//   界面不按自己的计时打勾。游标在两次状态之间按速度外推（只有游标）。
// - 画法在 chart5.js（交接说明用同一份）。整张谱先画进一张离屏的 canvas，每帧只贴图 + 画游标和「现在」的点；
//   行头、动作表、策略是 DOM，内容变了才改。

import {
  PARAMS, PARAM_ZH, METRICS, AXIS, BEATS, CanvasPainter, themeFromCss, layout, packBands, placeLaneLabels, drawChart, drawChip,
  rulerStep, posText, posTextFine, barOf, srcText, durText, sceneAt, tempoAt, fmtBpm, clipAt, clipById, segAt, srcAt, laneAt,
  sceneRoles, frac, deckStyle, withAlpha, EQ_CUT,
} from "./chart5.js";

const M = METRICS.live;
const ZOOMS = [8, 16, 32, "scene"];
const ZOOM_ZH = { 8: "8 小节", 16: "16 小节", 32: "32 小节", scene: "整个场景" };
const MIN_BAR_PX = 18; // 再长的场景也不把一小节压到比这更窄：滚动，不缩字
const PLAYHEAD_AT = 0.3; // 跟随时游标停在画面的 30% 处，右边 70% 是接下来的
const CACHE_K = 2; // 离屏的图比画面宽一倍，滚到头再重画
const LEAD_IN = 8; // 开演前留两小节（装载、摆旋钮）
const STRATEGY = ["作用", "保留", "前景", "LOW", "MID/HIGH"];
// 接管用的滑杆（谱上的单位）：EQ 7 点（-25 dB，全切）到 5 点（+6 dB）；Filter 左 -1 … 关 0 … 右 +1；推子 0..1
const SLIDER = {
  lo: [EQ_CUT, 6, 0.25], mid: [EQ_CUT, 6, 0.25], hi: [EQ_CUT, 6, 0.25], filter: [-1, 1, 0.02], vol: [0, 1, 0.01],
};
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

// 线型的小样：和图上那台 Deck 的线一样
function dashSample(d, w = 34) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", `0 0 ${w} 6`);
  s.setAttribute("width", w);
  s.setAttribute("height", 6);
  s.setAttribute("class", `l5-dash d${d}`);
  const l = document.createElementNS("http://www.w3.org/2000/svg", "line");
  for (const [k, v] of Object.entries({ x1: 0, y1: 3, x2: w, y2: 3, "stroke-width": 2.2, "stroke-dasharray": deckStyle(d).dash.join(" ") })) l.setAttribute(k, v);
  put(s, l);
  return s;
}

export class Live5View {
  constructor(root, opts = {}) {
    this.root = root;
    root.classList.add("live5");
    this.api = opts.api;
    this.onSeek = opts.onSeek || null;
    this.toast = opts.toast || (() => {});
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
    this.W = 0;
    this.H = 0;
    this.restBeat = 0; // 没在放的时候游标停在哪
    this.shown = null;
    this.deck = 1; // 接管面板上选的 Deck
    this.pending = {}; // 接管：每个旋钮最后要发的值（节流）
    this.localManual = {}; // 刚拖过、引擎还没报回来的
    this.returning = {};
    this.showPast = false;
    this._frame = this._frame.bind(this);
    this._build();
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(this.plot);
    this._onDocDown = (ev) => { if (this.bubble && !this.bubble.contains(ev.target) && !ev.target.closest(".l5-jump, .l5-scene")) this._closeBubble(); };
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
    this.root.classList.remove("live5");
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
    this.meterFill = el("i", {});
    this.meterPeak = el("u", {});
    this.meterText = el("span", { class: "l5-db" }, "–");
    this.overLamp = el("span", { class: "l5-over", title: "主输出过载（削波）" }, "过载");
    this.overCount = el("span", { class: "l5-overn hidden" });
    this.lateEl = el("span", { class: "l5-late hidden" });
    this.nextEl = el("span", { class: "l5-nextline" });
    put(R, el("div", { class: "panel l5-head" },
      el("div", { class: "l5-row" }, posBox, bpmBox, sceneBox, this.stageEl, this.pauseBtn, this.jumpBtn,
        el("span", { class: "l5-master", title: "主输出电平（dBFS）" }, el("span", { class: "l5-lab" }, "主输出"),
          el("span", { class: "l5-meter" }, this.meterFill, this.meterPeak), this.meterText, this.overLamp, this.overCount, this.lateEl)),
      el("div", { class: "l5-row l5-rolerow" }, this.rolesEl = el("span", { class: "l5-row l5-roles" }),
        el("span", { class: "l5-nextrow" }, el("span", { class: "l5-lab" }, "下一步"), this.nextEl)),
      this.scenesEl = el("div", { class: "l5-row l5-scenes" })));

    // 图
    this.zoomBtns = {};
    const tools = el("div", { class: "l5-row l5-tools" }, el("span", { class: "l5-lab" }, "一屏看"),
      ZOOMS.map((z) => (this.zoomBtns[z] = el("button", { class: "small", onclick: () => this._setZoom(z) }, ZOOM_ZH[z]))),
      this.followBtn = el("button", { class: "small", title: "游标停在画面左边三成的位置，谱往左走。拖动 / 横向滚动图会关掉跟随",
        onclick: () => { this.follow = !this.follow; this._syncTools(); this._kick(true); } }, "跟随播放"),
      el("span", { class: "grow" }),
      el("span", { class: "l5-legend" }, [1, 2, 3, 4].map((d) => el("span", { class: "l5-leg" }, chip(d), `D${d} ${deckStyle(d).zh}`, dashSample(d, 30), deckStyle(d).line)),
        el("span", { class: "l5-leg dim" }, "斜线 = 渐变 · 竖线 = 一下到位 · 横线 = 保持"),
        el("span", { class: "l5-leg dim" }, el("span", { class: "l5-chip hollow" }), "谱上的值", el("span", { class: "l5-chip d1" }, "1"), "实际")));
    this.gutter = el("div", { class: "l5-gutter" });
    this.canvas = el("canvas", { class: "l5-canvas", role: "img", "aria-label": "四 Deck 共轴图：点小节标尺从那里重新开始" });
    this.tip = el("div", { class: "l5-tip hidden" });
    this.plot = el("div", { class: "l5-plot" }, this.canvas, this.tip);
    put(R, el("div", { class: "panel l5-chart" }, tools, el("div", { class: "l5-grid" }, this.gutter, this.plot),
      el("div", { class: "l5-hint dim" }, "点小节标尺：从那一小节重新开始。拖动 / 横向滚动：前后看。带子里的小字是原曲的小节号（每个片段各数各的）。")));

    // 策略 / 接下来 / 手动接管
    this.stratEl = el("div", { class: "panel l5-strategy" });
    this.actsEl = el("div", { class: "l5-acts" });
    this.nowEl = el("span", { class: "l5-nowpos" });
    this.pastBtn = el("button", { class: "small", onclick: () => { this.showPast = !this.showPast; this._actSig = ""; this._renderActions(); } }, "回看更早的");
    this.takeEl = el("div", { class: "panel l5-take" });
    put(R, el("div", { class: "l5-lower" }, this.stratEl,
      el("div", { class: "panel l5-next" }, el("div", { class: "l5-h" }, "接下来", el("span", { class: "dim" }, "未来 4 小节的动作；做完没有按引擎的回执"), el("span", { class: "grow" }), this.pastBtn), this.actsEl),
      this.takeEl));
    this._buildTake();

    const c = this.canvas;
    c.addEventListener("pointerdown", (ev) => this._down(ev));
    c.addEventListener("pointermove", (ev) => this._move(ev));
    c.addEventListener("pointerup", (ev) => this._up(ev));
    c.addEventListener("pointercancel", () => { this.drag = null; });
    c.addEventListener("pointerleave", () => { this.hover = null; this.tip.classList.add("hidden"); this._kick(true); });
    c.addEventListener("wheel", (ev) => {
      const dx = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.shiftKey ? ev.deltaY : 0;
      if (!dx || !this.chart) return; // 竖着滚 = 滚页面
      ev.preventDefault();
      this._pan(this.b0 + dx / this.ppb);
    }, { passive: false });
    this._syncTools();
  }

  _buildTake() {
    const T = this.takeEl;
    T.innerHTML = "";
    this.tabBtns = {};
    this.sl = {};
    this.takeNote = el("div", { class: "l5-take-note dim" });
    put(T, el("div", { class: "l5-h" }, "手动接管", el("span", { class: "dim" }, "拖哪个，哪个就不再跟谱，别的照常")),
      el("div", { class: "l5-tabs" }, [1, 2, 3, 4].map((d) => (this.tabBtns[d] = el("button", { class: `l5-tab d${d}`, onclick: () => { this.deck = d; this._renderTake(); } },
        el("span", { class: "l5-tab-1" }, chip(d), `D${d}`, dashSample(d, 20)), el("span", { class: "l5-tab-st" }))))),
      this.takeNote);
    for (const p of PARAMS) {
      const [min, max, step] = SLIDER[p];
      const input = el("input", { type: "range", min, max, step, "aria-label": PARAM_ZH[p] });
      const s = { input, val: el("b", { class: "l5-sl-v" }, "–"), plan: el("span", { class: "l5-sl-plan dim" }), man: el("span", { class: "badge warn hidden" }, "手动"),
        beat: el("button", { class: "small", onclick: () => this._giveBack(p, "beat") }, "下一拍回谱"),
        bar: el("button", { class: "small", onclick: () => this._giveBack(p, "bar") }, "下一小节回谱") };
      input.addEventListener("input", () => { s.shownV = null; this._takeover(p, +input.value); });
      input.addEventListener("pointerdown", () => { s.held = true; });
      const release = () => { s.held = false; s.touched = performance.now(); };
      input.addEventListener("pointerup", release);
      input.addEventListener("pointercancel", release);
      input.addEventListener("blur", release);
      this.sl[p] = s;
      put(T, el("div", { class: "l5-sl" }, el("b", { class: "l5-sl-n" }, PARAM_ZH[p]), input, s.val, s.man,
        el("span", { class: "l5-sl-2" }, s.plan, el("span", { class: "grow" }), s.beat, s.bar)));
    }
  }

  _buildGutter() {
    const G = this.gutter;
    G.innerHTML = "";
    this.gDeck = {};
    this.gParam = {};
    for (const r of this.L.rows) {
      const cell = el("div", { class: `l5-g l5-g-${r.kind}`, style: `top:${r.y}px;height:${r.h}px` });
      if (r.kind === "scene") put(cell, el("span", { class: "dim" }, "场景 / 速度"));
      else if (r.kind === "ruler") put(cell, el("span", { class: "dim" }, "全场小节"));
      else if (r.kind === "deck") {
        const d = r.deck;
        const o = { role: el("span", { class: "l5-role" }), mark: el("span", { class: "l5-mark" }), st: el("span", { class: "l5-st" }),
          quiet: el("span", { class: "l5-quiet hidden", title: "这台 Deck 在放，但现在没出声（推子在底 / 被切掉了）" }, "没出声"),
          lv: el("i", {}), cell };
        put(cell, el("div", { class: "l5-g1" }, chip(d), el("b", {}, `D${d}`), dashSample(d), o.role),
          el("div", { class: "l5-g2" }, o.mark, o.st, o.quiet),
          el("div", { class: "l5-lv", title: "这台 Deck 出去的电平（推子之后）" }, o.lv));
        this.gDeck[d] = o;
      } else if (r.kind === "param") {
        const ax = AXIS[r.param];
        const yv = (v) => M.pad + (1 - frac(r.param, v)) * (r.h - 2 * M.pad);
        const vals = {};
        const grid = el("div", { class: "l5-vals" });
        for (let d = 1; d <= 4; d++) {
          const t = el("span", { class: "l5-vt" }, "–"), m = el("span", { class: "l5-vm hidden", title: "手动接管中" }, "手");
          const box = el("span", { class: "l5-val off" }, chip(d), t, m);
          put(grid, box);
          vals[d] = { box, t, m };
        }
        put(cell, el("b", { class: "l5-pn" }, PARAM_ZH[r.param]), grid,
          ax.ref !== null ? el("span", { class: "l5-ax", style: `top:${yv(ax.ref) - 7}px` }, ax.refText) : null,
          ax.top ? el("span", { class: "l5-ax", style: `top:${M.pad - 7}px` }, ax.top) : null,
          ax.bottom ? el("span", { class: "l5-ax", style: `top:${r.h - M.pad - 7}px` }, ax.bottom) : null);
        this.gParam[r.param] = vals;
      } else if (r.kind === "fx") put(cell, el("b", {}, "效果"), el("span", { class: "dim" }, "淡色 = 尾音"));
      else if (r.kind === "loop") put(cell, el("b", {}, "Loop"));
      put(G, cell);
    }
    G.style.height = `${this.L.height}px`;
  }

  // ---------------------------------------------------------------- 数据

  setChart(chart) {
    if (chart === this.chart) return;
    this.chart = chart && chart.clips ? chart : null;
    this.hover = null;
    this._actSig = "";
    this._sceneNo = undefined;
    this._closeBubble();
    if (!this.chart) { this.gutter.innerHTML = ""; return; }
    this.restBeat = 0;
    this.shown = null;
    this.scenesEl.innerHTML = "";
    this.scenesEl.classList.toggle("hidden", !(this.chart.scenes || []).length);
    put(this.scenesEl, el("span", { class: "l5-lab" }, "场景"));
    this.sceneBtns = {};
    for (const s of this.chart.scenes || []) {
      const b = el("button", { class: "l5-scene small", title: `从场景 ${s.no} 的开头（${posText(s.start)}）重新开始`,
        onclick: () => this._askSeek(s.start, b, false, `场景 ${s.no}${s.name ? "「" + s.name + "」" : ""}`) },
        el("b", {}, String(s.no)), s.name ? " " + s.name : "", s.draft ? el("span", { class: "badge warn" }, "草稿") : null,
        el("span", { class: "dim" }, ` ${barOf(s.start)}`));
      this.sceneBtns[s.no] = b;
      put(this.scenesEl, b);
    }
    this._relayout();
    this._renderState();
    this._kick(true);
  }

  update(engine, progress) {
    const e = engine && engine.v5 && engine.status && (!this.chart || !engine.set || engine.set === this.chart.id) ? engine : null;
    this.progress = progress && progress.v5 ? progress : null;
    if (e) {
      const now = Date.now(), st = e.status, old = this.engine && this.engine.status;
      // 状态说的是哪一刻：引擎给的墙上时间；和这台机器的钟差太多（不是同一台）就按收到的时刻
      this.at = e.at && Math.abs(now - e.at) < 3000 ? e.at : now;
      if (!old || this.engine.from_beat !== e.from_beat || st.beat < old.beat - 0.01 || st.state !== old.state) this.shown = null; // 跳转 / 重新开始：游标直接过去
      if (st.state !== "idle") this.restBeat = st.beat; // 停下以后引擎回到空闲、拍数归回起点：游标留在停下的地方
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
    this._renderState();
    this._kick(true);
  }

  _status() { return this.engine ? this.engine.status : null; }
  // 引擎报的拍（不外推）：动作做到哪了按它算；没在放 = 上次停下的地方
  _engineBeat() { const st = this._status(); return st && st.state !== "idle" ? st.beat : this.restBeat; }
  _isLive() { const st = this._status(); return !!st && !["idle", "done"].includes(st.state); }

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
    const w = Math.floor(this.plot.clientWidth);
    if (!w || w === this.W) return;
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
    const dpr = (this.dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1)));
    this.T = themeFromCss(this.root);
    this.T.past = withAlpha(this.T.plot, 0.42);
    const ppb = Math.max(MIN_BAR_PX / BEATS, this.W / this._span());
    this.ppb = Math.round(ppb * dpr * 1000) / (dpr * 1000);
    const p = new CanvasPainter(document.createElement("canvas").getContext("2d"));
    this.bands = packBands(p, this.chart, this.ppb, M);
    this.L = layout(this.chart, M, this.bands.nFx, this.bands.nLoop);
    this.H = this.L.height;
    this.labels = {};
    for (const r of this.L.rows) if (r.kind === "param") this.labels[r.param] = placeLaneLabels(p, this.chart, r.param, this.ppb, M, r.h);
    this.step = rulerStep(p, this.ppb, M);
    this.canvas.width = Math.round(this.W * dpr);
    this.canvas.height = Math.round(this.H * dpr);
    this.canvas.style.height = `${this.H}px`;
    this.ctx = this.canvas.getContext("2d");
    this.cache = this.cache || document.createElement("canvas");
    this.cache.width = Math.min(16000, this.canvas.width * CACHE_K);
    this.cache.height = this.canvas.height;
    this.cacheOk = false;
    this._buildGutter();
    this._gutSig = "";
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
    if (!this.chart || !this.W || !this.L) return;
    if (Math.max(1, Math.min(3, window.devicePixelRatio || 1)) !== this.dpr) this._relayout();
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
    this._draw(beat);
    const whole = Math.floor(beat);
    if (whole !== this._tickBeat) { this._tickBeat = whole; this._tick(beat); }
  }

  _ensureCache() {
    const span = this.W / this.ppb, cspan = this.cache.width / (this.ppb * this.dpr);
    if (this.cacheOk && this.b0 >= this.cb0 - 1e-6 && this.b0 + span <= this.cb0 + cspan + 1e-6) return;
    const unit = this.ppb * this.dpr; // 离屏图的起点贴在设备像素上，贴图时不用插值
    this.cb0 = Math.floor((this.b0 - Math.min(span * 0.2, cspan - span)) * unit) / unit;
    const g = this.cache.getContext("2d");
    g.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    drawChart(new CanvasPainter(g), this.chart, { x0: 0, w: this.cache.width / this.dpr, b0: this.cb0, b1: this.cb0 + cspan, ppb: this.ppb, rows: this.L.rows, height: this.H },
      this.T, M, { labels: this.labels, bands: this.bands, step: this.step, repeat: Math.max(360, this.W * 0.6) });
    this.cacheOk = true;
  }

  _rowAt(y) { return this.L.rows.find((r) => y >= r.y && y < r.y + r.h) || null; }

  _draw(beat) {
    const g = this.ctx, dpr = this.dpr, W = this.W, H = this.H, T = this.T;
    this._ensureCache();
    g.setTransform(1, 0, 0, 1, 0, 0);
    const sx = Math.round((this.b0 - this.cb0) * this.ppb * dpr);
    g.clearRect(0, 0, this.canvas.width, this.canvas.height);
    g.drawImage(this.cache, sx, 0, this.canvas.width, this.canvas.height, 0, 0, this.canvas.width, this.canvas.height);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const p = this.painter || (this.painter = new CanvasPainter(g));
    p.g = g;
    const xp = (beat - this.b0) * this.ppb;
    const top = this.L.top - M.gap;
    // 放过去的一侧压暗一点
    if (xp > 0) { g.fillStyle = T.past; g.fillRect(0, top, Math.min(W, xp), H - top); }
    // 悬停的十字线
    if (this.hover) { g.fillStyle = T.dim; g.globalAlpha = 0.5; g.fillRect(Math.round(this.hover.x), 0, 1, H); g.globalAlpha = 1; }
    if (xp < -30 || xp > W + 30) return;
    // 游标
    g.fillStyle = withAlpha(T.text, 0.22);
    g.fillRect(xp - 2, 0, 4, H);
    g.fillStyle = T.text;
    g.fillRect(xp - 0.75, 0, 1.5, H);
    g.beginPath(); g.moveTo(xp - 6, 0); g.lineTo(xp + 6, 0); g.lineTo(xp, 8); g.closePath(); g.fill();
    const st = this._status();
    if (!st || !st.decks) return;
    const active = st.decks.map((d) => ({ d, clip: d.clip_id ? clipById(this.chart, d.clip_id) : null })).filter((a) => a.clip && a.d.human);
    const s = M.chip;
    // 游标和每条播放带的交点：这台 Deck 现在读到原曲哪里（每个片段各数各的）
    for (const { d, clip } of active) {
      if (d.state !== "playing" && d.state !== "looping") continue;
      const r = this.L.rows.find((x) => x.kind === "deck" && x.deck === d.deck_id);
      const src = srcAt(clip, Math.max(clip.start, Math.min(beat, clip.stop - 1e-6)));
      if (!r || src === null) continue;
      const t = `原曲 ${posText(src)}`, w = p.textWidth(t, M.fs, true) + 10;
      const x = xp + 5 + w > W ? xp - 5 - w : xp + 5, cy = r.y + r.h / 2;
      p.rect(x, cy - 9, w, 18, { fill: T.plot, fa: 0.9, stroke: T.deck[d.deck_id] || T.neutral, lw: 1, r: 4 });
      p.text(t, x + 5, cy + 0.5, { fill: T.text, size: M.fs, bold: true });
    }
    // 「现在」的旋钮：空心 = 谱上这一拍的值，实心 = 引擎报的实际位置；两个不在一处（手动接管 / 没跟上）就都画出来
    for (const r of this.L.rows) {
      if (r.kind !== "param") continue;
      const yv = (v) => r.y + M.pad + (1 - frac(r.param, v)) * (r.h - 2 * M.pad);
      active.forEach(({ d, clip }, i) => {
        const plan = laneAt(clip, r.param, Math.max(beat, clip.start)), act = d.human[r.param];
        if (!plan || !act) return;
        const x = xp + (i - (active.length - 1) / 2) * (s + 2);
        const yS = yv(plan.v), yA = yv(act.v), col = T.deck[d.deck_id] || T.neutral;
        if ((d.manual || []).includes(r.param) || Math.abs(yS - yA) > 3) {
          p.line(x, yS, x, yA, { stroke: col, lw: 1.5 });
          p.rect(x - s / 2 + 1, yS - s / 2 + 1, s - 2, s - 2, { fill: T.plot, stroke: col, lw: 1.5, r: 3 });
        }
        p.rect(x - s / 2 - 1.5, yA - s / 2 - 1.5, s + 3, s + 3, { fill: T.plot, r: 4 });
        drawChip(p, x - s / 2, yA, d.deck_id, T, M);
      });
    }
  }

  // 拍数变了才做的事：位置、场景、原曲位置
  _tick(beat) {
    setText(this.posEl, posText(beat));
    const sc = sceneAt(this.chart, beat);
    const no = sc ? sc.no : 0;
    if (no !== this._sceneNo) { this._sceneNo = no; this._renderScene(sc, beat); }
    this._renderDeckInfo(beat);
    setText(this.nowEl, posText(beat));
    if (!this._status()) setText(this.bpmEl, fmtBpm(tempoAt(this.chart, beat)));
  }

  // ---------------------------------------------------------------- 跟引擎状态走的 DOM

  _renderState() {
    if (!this.chart || !this.L) return;
    const st = this._status(), e = this.engine;
    const beat = this._engineBeat();
    // 头
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
    // 主输出
    const m = st && st.master;
    const pct = (db) => `${Math.max(0, Math.min(100, ((db + 48) / 48) * 100)).toFixed(1)}%`;
    this.meterFill.style.width = m ? pct(m.level) : "0%";
    this.meterPeak.style.left = m ? pct(m.peak) : "0%";
    setText(this.meterText, m ? (m.level <= -90 ? "–∞" : m.level.toFixed(0)) + " dB" : "–");
    setClass(this.overLamp, "l5-over" + (m && m.over_now ? " on" : m && m.over > 0 ? " was" : ""));
    setText(this.overCount, m && m.over > 0 ? `过载过 ${m.over} 次` : "");
    setClass(this.overCount, "l5-overn" + (m && m.over > 0 ? "" : " hidden"));
    setText(this.lateEl, st && st.late > 0 ? `引擎晚了 ${st.late} 次` : "");
    setClass(this.lateEl, "l5-late" + (st && st.late > 0 ? "" : " hidden"));
    // 行头
    this._renderDecks(st);
    this._renderDeckInfo(this._beatNow());
    this._renderTake();
    this._renderActions();
    if (this._sceneNo === undefined) this._tick(beat);
  }

  // 一台 Deck 现在的状态（只看引擎报的）
  _deckState(d, st) {
    const rc = (this.engine && this.engine.receipts) || {};
    const tails = (st.fx || []).filter((f) => f.tail && (clipById(this.chart, f.clip) || {}).deck === d.deck_id);
    const failed = [d.clip_id, d.next_clip].filter(Boolean).map((id) => rc[`${id}.load`]).find((r) => r && r.st === "failed");
    if (d.state === "error" || failed) return { k: "error", mark: "✕", text: "出错：没装上" + (failed && failed.msg ? `（${failed.msg}）` : "") };
    if (d.state === "looping") return { k: "loop", mark: "↻", text: `循环 第 ${d.loop_lap || 1} 圈 · ${d.clip_id || ""}`, quiet: d.audible === false };
    if (d.state === "playing") return { k: "play", mark: "▶", text: `播放 · ${d.clip_id || ""}`, quiet: d.audible === false };
    if (d.state === "loading" || d.state === "ready") return { k: "ready", mark: "", text: `准备下一段 ${d.clip_id || d.next_clip || ""}，还没出声` }; // 记号是 CSS 画的虚线圈
    if (tails.length) return { k: "tail", mark: "≈", text: `只剩尾音（${tails.map((f) => f.id).join("、")}）` };
    if (d.state === "stopped") return { k: "stop", mark: "■", text: "已停" };
    const nx = d.next_clip ? clipById(this.chart, d.next_clip) : null;
    return { k: "idle", mark: "○", text: nx ? `待命，下一段 ${nx.id}（${posText(nx.start)}）` : "待命" };
  }

  _renderDecks(st) {
    for (let n = 1; n <= 4; n++) {
      const o = this.gDeck && this.gDeck[n];
      if (!o) continue;
      const d = st && (st.decks || []).find((x) => x.deck_id === n);
      const s = d ? this._deckState(d, st) : { k: "idle", mark: "○", text: st ? "待命" : "" };
      setClass(o.cell, `l5-g l5-g-deck st-${s.k}`);
      setText(o.mark, s.mark);
      setText(o.st, s.text);
      if (o.st.title !== s.text) o.st.title = s.text;
      setClass(o.quiet, "l5-quiet" + (s.quiet ? "" : " hidden"));
      const out = d ? Math.max(0, Math.min(100, ((d.out + 60) / 60) * 100)) : 0;
      o.lv.style.width = `${out.toFixed(0)}%`;
      o.state = s;
      o.d = d;
      for (const p of PARAMS) {
        const v = this.gParam && this.gParam[p] && this.gParam[p][n];
        if (!v) continue;
        const on = !!(d && d.clip_id && d.human && d.human[p]);
        setClass(v.box, "l5-val" + (on ? "" : " off") + (on && this._isManual(n, p, d) ? " manual" : ""));
        setText(v.t, on ? d.human[p].text : "–");
        setClass(v.m, "l5-vm" + (on && this._isManual(n, p, d) ? "" : " hidden"));
      }
    }
  }

  // 行头上的角色（文字，跟着场景和片段走；Deck 的颜色 / 编号 / 线型不变）
  _renderDeckInfo(beat) {
    const roles = sceneRoles(this.chart, sceneAt(this.chart, beat), beat);
    for (let n = 1; n <= 4; n++) {
      const o = this.gDeck && this.gDeck[n];
      if (!o) continue;
      setText(o.role, roles[n]);
      setClass(o.role, "l5-role" + (roles[n] === "待命" ? " dim" : ""));
    }
  }

  _renderScene(sc, beat) {
    setText(this.sceneEl, sc ? `${sc.no}${sc.name ? " " + sc.name : ""}` : "–");
    this.draftEl.classList.toggle("hidden", !(sc && sc.draft));
    for (const [no, b] of Object.entries(this.sceneBtns || {})) b.classList.toggle("on", !!sc && +no === sc.no);
    // 角色摘要：角色是文字，Deck 的颜色 / 编号 / 线型不变
    const roles = sceneRoles(this.chart, sc, sc ? null : beat);
    this.rolesEl.innerHTML = "";
    put(this.rolesEl, el("span", { class: "l5-lab" }, "角色"),
      Object.entries(roles).sort((a, b) => (a[1] === "待命") - (b[1] === "待命") || a[0] - b[0])
        .map(([d, role]) => el("span", { class: "l5-rolechip" + (role === "待命" ? " dim" : "") }, el("b", {}, role), chip(d), `D${d}`, dashSample(d, 22))));
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
      // 到点了还没有回执：先等两拍；再没有就照实写「没有回执」（比如正好从这一拍开始放，落在起点上的收尾动作引擎不会执行）
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
    let next = all.filter((a) => a.at > beat && a.at <= beat + 4 * BEATS);
    const later = all.filter((a) => a.at > beat + 4 * BEATS).slice(0, Math.max(0, 3 - next.length));
    const pastShown = this.showPast ? past : past.slice(-5);
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
    this.pastBtn.classList.toggle("hidden", past.length <= 5);
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
        el("span", { class: "l5-aln dim" }, a.line ? `第 ${a.line} 行` : ""),
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

  // 跳转前先问一句：anchor = 气泡贴着哪（元素，或图上的 {x, y}）；ask = 让用户填小节号
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
      msg.textContent = `从 ${what ? what + "（" : ""}${bar}.1${what ? "）" : ""} ${live ? "重新开始（会结束手动接管，旋钮回到谱上的位置）" : "开始放"}`;
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
      const a = this.canvas.getBoundingClientRect(); x = a.left - R.left + anchor.x - 20; y = a.top - R.top + anchor.y + 10;
    }
    b.style.left = `${Math.max(4, Math.min(R.width - b.offsetWidth - 4, x))}px`;
    b.style.top = `${y}px`;
    (input || go).focus();
    if (input) input.select();
  }

  // ---------------------------------------------------------------- 图上的鼠标

  _xy(ev) { const r = this.canvas.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; }

  _down(ev) {
    if (!this.chart || ev.button) return;
    const { x, y } = this._xy(ev);
    this.drag = { x, y, b0: this.b0, moved: false, row: this._rowAt(y) };
    this.canvas.setPointerCapture(ev.pointerId);
  }

  _move(ev) {
    if (!this.chart || !this.L) return;
    const { x, y } = this._xy(ev);
    const d = this.drag;
    if (d) {
      if (!d.moved && Math.abs(x - d.x) < 4) return;
      d.moved = true;
      this.tip.classList.add("hidden");
      this.hover = null;
      this._pan(d.b0 - (x - d.x) / this.ppb);
      return;
    }
    this.hover = { x, y };
    const row = this._rowAt(y);
    const cur = row && (row.kind === "ruler" || row.kind === "scene") && this.onSeek ? "pointer" : "grab";
    if (this.canvas.style.cursor !== cur) this.canvas.style.cursor = cur;
    this._showTip(x, y);
    this._kick(true);
  }

  _up(ev) {
    const d = this.drag;
    this.drag = null;
    if (!d || d.moved || !d.row) return;
    if ((d.row.kind === "ruler" || d.row.kind === "scene") && this.onSeek) {
      const beat = this.b0 + d.x / this.ppb;
      if (beat >= 0 && beat < (this.chart.length || 0)) this._askSeek(beat, { x: d.x, y: d.row.y + d.row.h });
    }
  }

  // 悬停：这一拍上每台 Deck 的谱值 / 这个带子的全部说明（图上放不下的字都在这里）
  _showTip(x, y) {
    const row = this._rowAt(y);
    const beat = this.b0 + x / this.ppb;
    const T = this.tip;
    if (!row || beat < -LEAD_IN || beat > (this.chart.length || 0) + BEATS) { T.classList.add("hidden"); return; }
    const key = `${row.kind}${row.deck || row.param || ""}:${Math.floor(beat)}:${row.kind === "fx" || row.kind === "loop" ? Math.floor((y - row.y - 3) / M.band) : ""}`;
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
    T.style.left = `${Math.max(2, Math.min(this.W - w - 2, x + 14))}px`;
    T.style.top = `${y + 18 + h > this.H ? Math.max(2, y - h - 10) : y + 18}px`;
  }

  _tipLines(row, beat, y) {
    const ch = this.chart, pos = posText(beat);
    const head = (t) => el("div", { class: "l5-tip-h" }, t);
    if (row.kind === "ruler" || row.kind === "scene") {
      const sc = sceneAt(ch, beat);
      return [head(`全场 ${pos}`), sc ? `场景 ${sc.no}${sc.name ? " " + sc.name : ""}${sc.draft ? "（草稿）" : ""}` : "不在场景里",
        `主速 ${fmtBpm(tempoAt(ch, beat))}`, this.onSeek && beat >= 0 ? el("div", { class: "dim" }, `点一下：从 ${barOf(beat)}.1 重新开始`) : null].filter(Boolean);
    }
    if (row.kind === "deck") {
      const c = clipAt(ch, row.deck, beat);
      if (!c) {
        const pre = (ch.clips || []).find((k) => k.deck === row.deck && k.load !== undefined && beat >= k.load && beat < k.start);
        return pre ? [head(`D${row.deck} · ${pos}`), `准备 ${pre.id}「${pre.title}」，${posText(pre.start)} 起播`, el("div", { class: "dim" }, "准备下一段，还没出声")] : [];
      }
      const s = segAt(c, beat), src = srcAt(c, beat);
      const out = [head(`D${row.deck} · 全场 ${pos}`), `${c.id}「${c.title}」${c.role ? " · " + c.role : ""}`, `原曲 ${posText(src)}（原 BPM ${fmtBpm(c.bpm)}）`];
      if (s && s.loop > 0) out.push(`${s.id} loop ${durText(s.loop)}，第 ${Math.floor((beat - s.g0) / s.loop) + 1} 圈（原曲 ${s.src_pos} 起）`);
      out.push(el("div", { class: "dim" }, `全场 ${posText(c.start)} – ${posText(c.stop)} · 谱第 ${c.line} 行${c.note ? " · " + c.note : ""}`));
      return out;
    }
    if (row.kind === "param") {
      const out = [head(`${PARAM_ZH[row.param]} · 全场 ${pos}（谱上的值）`)];
      for (let d = 1; d <= 4; d++) {
        const c = clipAt(ch, d, beat), v = c && laneAt(c, row.param, beat);
        if (v) out.push(el("div", { class: "l5-tip-v" }, chip(d), el("b", {}, v.text), v.ramp ? el("span", { class: "dim" }, `渐变中，到 ${posText(v.to.at)}`) : null, el("span", { class: "dim" }, c.id)));
      }
      return out.length > 1 ? out : [];
    }
    const lane = Math.floor((y - row.y - 3) / M.band);
    const it = (this.bands[row.kind] || []).find((i) => i.lane === lane && beat >= i.a && beat < i.b + i.tail);
    if (!it) return [];
    if (it.kind === "fx") {
      const f = it.ref;
      return [head(`${it.label}${f.route_zh ? "（" + f.route_zh + "）" : ""}`), `作用范围：${f.scope || (f.clips || []).join("、")}`,
        `${posText(f.start)} – ${posText(f.end)}` + (f.tail > 0 ? `，关了以后尾音留 ${durText(f.tail)}` : ""), f.text || null,
        el("div", { class: "dim" }, `谱第 ${f.line} 行`)].filter(Boolean);
    }
    const s = it.ref, c = it.clip, laps = (s.g1 - s.g0) / s.loop;
    return [head(it.label), `目标：${c.id}「${c.title}」（D${c.deck}${c.role ? " " + c.role : ""}）`,
      `${posText(s.g0)} – ${posText(s.g1)}，原曲 ${s.src_pos} 起，转 ${Math.abs(laps - Math.round(laps)) < 1e-6 ? Math.round(laps) : laps.toFixed(1)} 圈`,
      el("div", { class: "dim" }, `谱第 ${s.line} 行`)];
  }

  // ---------------------------------------------------------------- 手动接管

  _isManual(deck, param, d) {
    if (d && (d.manual || []).includes(param)) return true;
    const t = this.localManual[`${deck}.${param}`];
    return !!t && performance.now() - t < 2000; // 刚拖过：引擎报回来之前先标上；2 秒还没报回来（没发成）就撤掉
  }

  _renderTake() {
    const st = this._status();
    for (let n = 1; n <= 4; n++) {
      const d = st && (st.decks || []).find((x) => x.deck_id === n);
      const b = this.tabBtns[n];
      b.classList.toggle("on", n === this.deck);
      const man = d && (d.manual || []).length;
      setText(b.querySelector(".l5-tab-st"), man ? `手动 ${man} 个` : d && d.clip_id ? d.clip_id : "空");
    }
    const d = st && (st.decks || []).find((x) => x.deck_id === this.deck);
    const usable = !!(d && d.clip_id && ["running", "paused", "pausing"].includes(st.state));
    if (this.takeEl.dataset.deck !== String(this.deck)) this.takeEl.dataset.deck = String(this.deck);
    setText(this.takeNote, !st ? "开始演出以后才能接管。" : !usable ? `D${this.deck} 现在没有片段。` : "");
    const clip = d && d.clip_id ? clipById(this.chart, d.clip_id) : null;
    for (const p of PARAMS) {
      const s = this.sl[p], key = `${this.deck}.${p}`;
      const act = usable && d.human ? d.human[p] : null;
      const manual = usable && this._isManual(this.deck, p, d);
      if (d && (d.manual || []).includes(p)) delete this.localManual[key]; // 引擎已经认了：以后只看引擎的
      else if (!this._isManual(this.deck, p, null)) delete this.returning[key];
      s.input.disabled = !usable;
      // 滑杆跟着实际值走；正拖着 / 刚松手的那一下不抢
      if (act && !s.held && !(s.touched && performance.now() - s.touched < 600)) {
        const [min, max] = SLIDER[p];
        const v = Math.max(min, Math.min(max, act.v));
        if (s.shownV !== v || s.shownDeck !== this.deck) { s.shownV = v; s.shownDeck = this.deck; s.input.value = String(v); }
      }
      setText(s.val, act ? act.text : "–");
      const plan = clip ? laneAt(clip, p, Math.max(clip.start, st.beat)) : null;
      setText(s.plan, plan ? `谱：${plan.text}` : "");
      setClass(s.man, "badge warn" + (manual ? "" : " hidden"));
      const back = this.returning[key];
      s.beat.disabled = s.bar.disabled = !manual || !!back;
      setText(s.beat, back === "beat" ? "回谱中…" : "下一拍回谱");
      setText(s.bar, back === "bar" ? "回谱中…" : "下一小节回谱");
    }
  }

  // 拖滑杆：每个旋钮最多每秒发 20 次，上一条没回来就只留最新的值
  _takeover(param, value) {
    const deck = this.deck, key = `${deck}.${param}`;
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
    this._renderTake();
  }

  async _giveBack(param, when) {
    const deck = this.deck, key = `${deck}.${param}`;
    this.returning[key] = when;
    this._renderTake();
    const r = await this._call("POST", "/api/v5/return", { deck, param, when, ramp: 1 });
    if (!r) delete this.returning[key];
    delete this.localManual[key];
    this._renderTake();
  }
}
