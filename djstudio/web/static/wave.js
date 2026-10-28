// 三频段波形 + 小节网格 + 段落 + 标记。滚轮缩放，拖动平移，单击定位。

const SECTION_COLORS = {
  intro: "rgba(120,140,170,.10)", groove: "rgba(80,160,255,.07)", breakdown: "rgba(255,200,80,.13)",
  drop: "rgba(255,90,90,.12)", outro: "rgba(160,120,200,.12)",
};

export class Wave {
  constructor(canvas, opts = {}) {
    this.c = canvas;
    this.h = opts.height || 150;
    this.wf = null;
    this.grid = null;
    this.sections = [];
    this.markers = [];
    this.dur = 0;
    this.t0 = 0;
    this.t1 = 0;
    this.playhead = null;
    this.onSeek = null;
    this._drag = null;
    canvas.addEventListener("wheel", (e) => this._wheel(e), { passive: false });
    canvas.addEventListener("mousedown", (e) => { this._drag = { x: e.clientX, t0: this.t0, t1: this.t1, moved: false }; });
    window.addEventListener("mousemove", (e) => this._move(e));
    window.addEventListener("mouseup", (e) => this._up(e));
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  setData(wf, grid, sections = [], markers = []) {
    this.wf = wf;
    this.grid = grid;
    this.sections = sections || [];
    this.markers = markers || [];
    this.dur = wf ? wf.n / wf.rate : 0;
    this.t0 = 0;
    this.t1 = this.dur;
    this.draw();
  }

  setMarkers(markers) { this.markers = markers || []; this.draw(); }
  setPlayhead(t) { this.playhead = t; this.draw(); }

  barTime(bar) {
    const g = this.grid;
    return g.bar1 + (bar - 1) * 4 * 60 / g.bpm;
  }

  timeBar(t) {
    const g = this.grid;
    return (t - g.bar1) / (4 * 60 / g.bpm) + 1;
  }

  xOf(t, w) { return (t - this.t0) / (this.t1 - this.t0) * w; }
  tOf(x, w) { return this.t0 + x / w * (this.t1 - this.t0); }

  _wheel(e) {
    if (!this.wf) return;
    e.preventDefault();
    const r = this.c.getBoundingClientRect();
    const span = this.t1 - this.t0;
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      const d = (e.deltaX || e.deltaY) / r.width * span;
      this._setView(this.t0 + d, this.t1 + d);
      return;
    }
    const tm = this.tOf(e.clientX - r.left, r.width);
    const k = Math.exp(e.deltaY * 0.0015);
    const ns = Math.min(this.dur, Math.max(4, span * k));
    const f = (tm - this.t0) / span;
    this._setView(tm - f * ns, tm - f * ns + ns);
  }

  _setView(a, b) {
    const span = b - a;
    if (a < 0) { a = 0; b = span; }
    if (b > this.dur) { b = this.dur; a = Math.max(0, b - span); }
    this.t0 = a;
    this.t1 = b;
    this.draw();
  }

  _move(e) {
    if (!this._drag) return;
    const dx = e.clientX - this._drag.x;
    if (Math.abs(dx) > 3) this._drag.moved = true;
    if (!this._drag.moved) return;
    const r = this.c.getBoundingClientRect();
    const d = -dx / r.width * (this._drag.t1 - this._drag.t0);
    this._setView(this._drag.t0 + d, this._drag.t1 + d);
  }

  _up(e) {
    const d = this._drag;
    this._drag = null;
    if (!d || d.moved || !this.wf) return;
    const r = this.c.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right) return;
    const t = this.tOf(e.clientX - r.left, r.width);
    if (this.onSeek) this.onSeek(t);
  }

  draw() {
    const c = this.c;
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth;
    const h = this.h;
    if (!w) return;
    c.style.height = h + "px";
    c.width = Math.round(w * dpr);
    c.height = Math.round(h * dpr);
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    if (!this.wf) {
      g.fillStyle = "#666";
      g.fillText("还没有波形（分析中或分析失败）", 10, 20);
      return;
    }
    const top = 16;
    const mid = top + (h - top) / 2;
    const amp = (h - top) / 2 - 2;

    // 段落底色
    if (this.grid) {
      for (const s of this.sections) {
        const x0 = this.xOf(this.barTime(s.start), w), x1 = this.xOf(this.barTime(s.end), w);
        if (x1 < 0 || x0 > w) continue;
        g.fillStyle = SECTION_COLORS[s.label] || "rgba(255,255,255,.05)";
        g.fillRect(x0, top, x1 - x0, h - top);
        g.fillStyle = "#8890a0";
        g.font = "10px sans-serif";
        if (x1 - x0 > 40) g.fillText(s.label, Math.max(x0, 0) + 3, h - 4);
      }
    }

    // 波形：颜色 = (low, mid, high) → (r, g, b)
    const wf = this.wf;
    const pps = wf.rate;
    for (let x = 0; x < w; x++) {
      const ta = this.tOf(x, w), tb = this.tOf(x + 1, w);
      let ia = Math.floor(ta * pps), ib = Math.max(ia + 1, Math.floor(tb * pps));
      ia = Math.max(0, ia); ib = Math.min(wf.n, ib);
      if (ia >= ib) continue;
      let pk = 0, lo = 0, mi = 0, hi = 0;
      for (let i = ia; i < ib; i++) {
        if (wf.peak[i] > pk) pk = wf.peak[i];
        lo = Math.max(lo, wf.low[i]); mi = Math.max(mi, wf.mid[i]); hi = Math.max(hi, wf.high[i]);
      }
      const s = Math.max(lo, mi, hi, 1);
      g.strokeStyle = `rgb(${Math.round(255 * lo / s)},${Math.round(200 * mi / s + 40)},${Math.round(255 * hi / s)})`;
      const a = pk / 255 * amp;
      g.beginPath();
      g.moveTo(x + 0.5, mid - a);
      g.lineTo(x + 0.5, mid + a);
      g.stroke();
    }

    // 小节网格
    if (this.grid && this.grid.bpm > 0) {
      const barDur = 4 * 60 / this.grid.bpm;
      const pxPerBar = barDur / (this.t1 - this.t0) * w;
      const labelEvery = pxPerBar > 40 ? 1 : pxPerBar > 18 ? 4 : pxPerBar > 8 ? 8 : pxPerBar > 3 ? 16 : 32;
      const b0 = Math.max(1, Math.floor(this.timeBar(this.t0)));
      const b1 = Math.ceil(this.timeBar(this.t1));
      g.font = "10px " + getComputedStyle(document.body).getPropertyValue("--mono");
      for (let b = b0; b <= b1; b++) {
        const x = this.xOf(this.barTime(b), w);
        const phrase = (b - 1) % 16 === 0;
        if (pxPerBar > 4 || phrase) {
          g.strokeStyle = phrase ? "rgba(255,255,255,.45)" : "rgba(255,255,255,.12)";
          g.beginPath(); g.moveTo(x + 0.5, top); g.lineTo(x + 0.5, h); g.stroke();
        }
        if ((b - 1) % labelEvery === 0) {
          g.fillStyle = phrase ? "#cfd5e0" : "#7c8494";
          g.fillText(String(b), x + 2, 11);
        }
      }
    }

    // 标记
    for (const m of this.markers) {
      const t = m.t !== undefined ? m.t : this.barTime(m.bar);
      const x = this.xOf(t, w);
      if (x < -2 || x > w + 2) continue;
      g.strokeStyle = m.color || "#ffd34d";
      g.lineWidth = 2;
      g.beginPath(); g.moveTo(x, top); g.lineTo(x, h); g.stroke();
      g.lineWidth = 1;
      g.fillStyle = m.color || "#ffd34d";
      g.font = "bold 11px sans-serif";
      g.fillText(m.label || "", x + 3, top + 12);
    }

    if (this.playhead !== null) {
      const x = this.xOf(this.playhead, w);
      g.strokeStyle = "#fff";
      g.beginPath(); g.moveTo(x + 0.5, 0); g.lineTo(x + 0.5, h); g.stroke();
    }
  }
}
