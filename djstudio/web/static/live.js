// 演出页的实时混音台：两条滚动波形 + 两通道混音台 + 过渡时间线。
//
// 旋钮、推子、电平表显示的是引擎从 Mixxx（或内置引擎里的模拟 Mixxx）读回的控件值（状态帧 d / m / x / mv，每 100ms 一帧），
// 不是计划值——Mixxx 里旋钮在哪，这里就画在哪。计划值（编译出的自动化曲线）画在过渡时间线上，
// 游标走到哪里，旋钮应该就在曲线的那个位置。波形与游标在两帧之间按播放速度外推，每个画面帧重画。

import { drawTriBand, WAVE_COLORS } from "./wavedraw.js";

const NS = "http://www.w3.org/2000/svg";
const DECK_HEX = { 1: "#ff9a3c", 2: "#46c2ff" };
// EQ 三段的颜色（旋钮、时间线曲线、EQ 操作）和波形同一套：LOW = 蓝、MID = 绿、HIGH = 白（Denon 三频段波形的颜色，
// 看波形就知道哪个旋钮管哪一层）。蓝色在深色底上调亮一点，字看得清。
const BAND_HEX = { lo: "#5b8cff", mid: WAVE_COLORS.mid, hi: "#f2f4f8" };
const SPAN_S = 7; // 波形窗口：播放头两侧各 7 秒（真实时间，两边 deck 同一比例，拍对齐时小节线上下对齐）
const MIX = { hi: 0, mid: 1, lo: 2, filter: 3, echo: 4, reverb: 5, vu: 6 };
const PARAM_ORDER = ["vol", "hi", "mid", "lo", "filter", "echo", "reverb"];
const PARAM_LABEL = { vol: "推子", hi: "HIGH", mid: "MID", lo: "LOW", filter: "FILTER", echo: "ECHO", reverb: "REVERB" };
// 过渡时间线每组的行：和左边混音台旋钮同一顺序；没有曲线时的值（推子：A 满、B 由程序决定）
const LANE_PARAMS = ["hi", "mid", "lo", "filter", "echo", "reverb", "vol"];
const NEUTRAL_V = { hi: 1, mid: 1, lo: 1, filter: 0.5, echo: 0, reverb: 0, vol: 1 };
const TECH_LABEL = { longhand: "长交接", instant: "瞬时切换", custom: "自定义" };
const SECTION_TINT = {
  intro: "rgba(120,140,170,.10)", groove: "rgba(80,160,255,.06)", breakdown: "rgba(255,200,80,.12)",
  drop: "rgba(255,90,90,.11)", outro: "rgba(160,120,200,.12)",
};

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}

function svg(tag, attrs = {}, parent = null) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.append(e);
  return e;
}

// ------------------------------------------------------------------ 单位

export const dB = (g) => (g <= 0.0005 ? -Infinity : 20 * Math.log10(g));
const isEq = (p) => p === "hi" || p === "mid" || p === "lo";
const isFx = (p) => p === "echo" || p === "reverb";

// 钟点位（Prime 4+ 的读法，和打碟谱一样）：离 12 点的钟头数，左负右正，-5 = 7 点，+5 = 5 点。
// EQ：12 点 0 dB，7 点 -25 dB（谱：全切），5 点 +6 dB；左半边 dB = -25·(钟头/5)^1.56（10 点 ≈ -6 dB）。
// 与 djstudio/plan/convert.py 的 eq_db 是同一条曲线。
const EQ_CUT_DB = -25, EQ_MAX_DB = 6, EQ_TAPER = 1.56;
export function eqHours(gain) {
  if (gain <= 0.0005) return -5;
  const d = dB(gain);
  if (d >= 0) return Math.min(5, (d / EQ_MAX_DB) * 5);
  return -5 * Math.min(1, Math.pow(d / EQ_CUT_DB, 1 / EQ_TAPER));
}
const filterHours = (v) => Math.max(-5, Math.min(5, (v - 0.5) * 10));
// 推子：Mixxx 的 volume 值是增益，换回推子位置（ControlAudioTaperPot(-20, 0, 1) 的 valueToParameter）
export function faderPos(gain) {
  if (gain <= 0) return 0;
  if (gain >= 1) return 1;
  return Math.max(0, (20 * Math.log10(gain + 0.1 * (1 - gain)) + 20) / 20);
}
function hours(param, v) {
  if (isEq(param)) return eqHours(v);
  if (param === "filter") return filterHours(v);
  return null;
}
export function clockText(h, filt = false) {
  if (Math.abs(h) < 0.15) return filt ? "关" : "12点";
  const pos = h < 0 ? 12 + h : h; // 7..12 或 0..5
  const r = Math.round(pos * 2) / 2;
  const whole = Math.floor(r) === 0 ? 12 : Math.floor(r);
  let t = `${whole}点${r % 1 ? "半" : ""}`;
  if (filt) t = (h < 0 ? "左" : "右") + t;
  else if (h <= -4.9) t += "(切)";
  return t;
}

export function fmtParam(param, v) {
  if (isEq(param)) return clockText(eqHours(v));
  if (param === "filter") return clockText(filterHours(v), true);
  if (param === "vol") return `推子 ${(faderPos(v) * 10).toFixed(1).replace(/\.0$/, "")}`;
  return `${Math.round(v * 100)}%`;
}

function fmtSub(param, v) {
  if (isEq(param)) {
    const d = dB(v);
    return d === -Infinity || d < -40 ? "kill" : Math.abs(d) < 0.05 ? "0 dB" : `${d > 0 ? "+" : ""}${d.toFixed(1)} dB`;
  }
  if (param === "filter") {
    const k = (v - 0.5) * 2;
    return Math.abs(k) < 0.01 ? "" : `${k < 0 ? "LPF" : "HPF"} ${Math.round(Math.abs(k) * 100)}%`;
  }
  return "";
}

// 旋钮角度 = 钟点位：7 点 -150°，12 点 0°，5 点 +150°（Prime 4+ 的 300° 行程）
function knobAngle(param, v) {
  const h = hours(param, v);
  if (h !== null) return h * 30;
  return -150 + Math.max(0, Math.min(1, v)) * 300;
}

// 时间线行里的纵坐标 0..1 = 旋钮转角（7 点在下、12 点在中间、5 点在上；filter 中线 = 关，上 = HPF，下 = LPF；推子 = 推子位置）
function laneY(param, v) {
  if (param === "vol") return faderPos(v);
  return (knobAngle(param, v) + 150) / 300;
}

const FX_HEX = "#f0c050";
const paramColor = (param, d) => (isEq(param) ? BAND_HEX[param] : param === "filter" ? "#c9a6ff" : isFx(param) ? FX_HEX : DECK_HEX[d]);

// 时间线每行左边的小旋钮：和混音台上的旋钮同一画法（推子画成 7 点～5 点的转角）
class MiniKnob {
  constructor(param, color) {
    const S = 22, c = 11, r = 8;
    this.param = param; this.c = c; this.r = r;
    this.root = svg("svg", { viewBox: `0 0 ${S} ${S}`, width: S, height: S, class: "mini-knob" });
    svg("path", { d: arcPath(c, c, r, -150, 150), class: "knob-track" }, this.root);
    this.arc = svg("path", { d: "", class: "knob-arc", stroke: color }, this.root);
    this.ptr = svg("line", { x1: c, y1: c - r + 1, x2: c, y2: c, stroke: "#fff", "stroke-width": 1.6, "stroke-linecap": "round" }, this.root);
  }

  set(v) {
    if (v === null || v === undefined || v === this.v) return;
    this.v = v;
    const a = laneY(this.param, v) * 300 - 150;
    const zero = isEq(this.param) || this.param === "filter" ? 0 : -150;
    this.arc.setAttribute("d", arcPath(this.c, this.c, this.r, zero, a));
    this.ptr.setAttribute("transform", `rotate(${a.toFixed(1)} ${this.c} ${this.c})`);
  }
}

function arcPath(cx, cy, r, a0, a1) {
  if (Math.abs(a1 - a0) < 0.5) return "";
  const p = (a) => [cx + r * Math.sin((a * Math.PI) / 180), cy - r * Math.cos((a * Math.PI) / 180)];
  const [x0, y0] = p(Math.min(a0, a1)), [x1, y1] = p(Math.max(a0, a1));
  const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
  return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`;
}

// ------------------------------------------------------------------ 混音台部件

class Knob {
  constructor(label, param, color, small = false, labelColor = null) {
    this.param = param;
    this.v = null;
    const S = small ? 32 : 42, c = S / 2, r = c - 4;
    this.svg = svg("svg", { viewBox: `0 0 ${S} ${S}`, width: S, height: S, class: "knob-svg" });
    svg("path", { d: arcPath(c, c, r, -150, 150), class: "knob-track" }, this.svg);
    if (param !== "echo" && param !== "reverb") svg("line", { x1: c, y1: 1, x2: c, y2: 4, class: "knob-tick" }, this.svg);
    this.arc = svg("path", { d: "", class: "knob-arc", stroke: color }, this.svg);
    svg("circle", { cx: c, cy: c, r: r - 5, class: "knob-cap" }, this.svg);
    this.ptr = svg("g", { class: "knob-ptr" }, this.svg);
    svg("line", { x1: c, y1: c - r + 6, x2: c, y2: c - 3, stroke: "#fff", "stroke-width": 2.2, "stroke-linecap": "round" }, this.ptr);
    this.c = c; this.r = r;
    this.val = el("div", { class: "kv" }, "–");
    this.sub = el("div", { class: "ks" });
    this.root = el("div", { class: "knob" + (small ? " small" : "") }, this.svg,
      el("div", { class: "kl" + (labelColor ? " band" : ""), style: labelColor ? `color:${labelColor}` : null }, label), this.val, this.sub);
  }

  set(v) {
    if (v === null || v === undefined || v === this.v) return;
    this.v = v;
    const a = knobAngle(this.param, v);
    const zero = isFx(this.param) ? -150 : 0;
    this.arc.setAttribute("d", arcPath(this.c, this.c, this.r, zero, a));
    if (this.a !== undefined && Math.abs(a - this.a) > 0.25) this._flash();
    this.a = a;
    this.ptr.setAttribute("transform", `rotate(${a.toFixed(1)} ${this.c} ${this.c})`);
    this.val.textContent = fmtParam(this.param, v);
    this.sub.textContent = fmtSub(this.param, v);
    const kill = isEq(this.param) && eqHours(v) <= -4.9;
    const off = isFx(this.param) ? v < 0.005 : this.param === "filter" ? Math.abs(v - 0.5) < 0.005 : Math.abs(dB(v)) < 0.05;
    this.root.classList.toggle("kill", kill);
    this.root.classList.toggle("moved", !off);
  }
}

// 正在动的旋钮 / 推子亮一下（像看 DJ 的手在哪）
function flash(target) {
  target.root.classList.add("turning");
  clearTimeout(target._ft);
  target._ft = setTimeout(() => target.root.classList.remove("turning"), 450);
}
Knob.prototype._flash = function () { flash(this); };

class Fader {
  constructor(color) {
    this.cap = el("div", { class: "fader-cap", style: `border-color:${color}` });
    this.fill = el("div", { class: "fader-fill", style: `background:${color}` });
    this.val = el("div", { class: "kv" }, "–");
    this.root = el("div", { class: "fader" }, el("div", { class: "fader-track" }, this.fill, this.cap), this.val);
  }

  set(v) {
    if (v === this.v) return;
    if (this.v !== undefined && Math.abs(v - this.v) > 0.002) flash(this);
    this.v = v;
    const pos = faderPos(v);
    this.cap.style.bottom = `calc(${pos * 100}% - 7px)`;
    this.fill.style.height = `${pos * 100}%`;
    this.val.textContent = (pos * 10).toFixed(1).replace(/\.0$/, "");
  }
}

// 电平表（dBFS）：条 = RMS（300 ms 积分），亮点 = 峰值（每秒落 20 dB），顶上的灯 = 峰值到 0 dBFS（会削波）。
// CH1 / MASTER / CH2 三条同一高度、同一刻度，能直接比：两边推子前的电平和主输出差多少。
const METER_LO = -36, METER_HI = 0, METER_N = 32; // 每格 1.125 dB
const meterIdx = (db) => Math.floor(((db - METER_LO) / (METER_HI - METER_LO)) * METER_N);
// 真 Mixxx（没有 dBFS 电平）：vu_meter = log10(32767·平均绝对值/1000 + 1)（enginevumeter.cpp:53-56）反推，约等于 RMS
const vuToDb = (v) => (v > 0 ? 20 * Math.log10(((10 ** v - 1) * 1000) / 32767) + 1 : -90);

class Meter {
  constructor() {
    this.segs = [];
    this.clip = el("span", { class: "clip", title: "峰值到 0 dBFS：主输出会削波失真" });
    this.root = el("div", { class: "vu" }, this.clip);
    for (let i = METER_N - 1; i >= 0; i--) {
      const top = METER_LO + ((i + 1) / METER_N) * (METER_HI - METER_LO);
      const sp = el("span", { class: top > -3 ? "r" : top > -9 ? "y" : "g" });
      this.segs[i] = sp;
      this.root.append(sp);
    }
  }

  // rms / peak：dBFS（peak 可以没有）
  set(rms, peak) {
    const lit = Math.max(0, Math.min(METER_N, meterIdx(rms ?? -90)));
    const pk = peak === null || peak === undefined ? -1 : Math.min(METER_N - 1, meterIdx(peak));
    const key = lit * 100 + pk;
    if (key === this.key) return;
    this.key = key;
    this.segs.forEach((sp, i) => { sp.classList.toggle("on", i < lit); sp.classList.toggle("pk", i === pk && i >= lit); });
    this.clip.classList.toggle("on", peak !== null && peak !== undefined && peak >= -0.1);
  }
}

// 刻度（和电平表同高，放在 MASTER 旁边）
function meterScale() {
  const root = el("div", { class: "vu-scale" });
  for (const db of [0, -3, -6, -9, -12, -18, -24, -30, -36]) {
    const y = ((METER_HI - db) / (METER_HI - METER_LO)) * 100;
    root.append(el("span", { style: `top:${y}%` }, String(db)));
  }
  return root;
}

class Channel {
  constructor(d) {
    const col = DECK_HEX[d];
    this.k = {
      hi: new Knob("HIGH", "hi", BAND_HEX.hi, false, BAND_HEX.hi), mid: new Knob("MID", "mid", BAND_HEX.mid, false, BAND_HEX.mid),
      lo: new Knob("LOW", "lo", BAND_HEX.lo, false, BAND_HEX.lo),
      filter: new Knob("FILTER", "filter", "#c9a6ff"),
      echo: new Knob("ECHO", "echo", FX_HEX, true), reverb: new Knob("REVERB", "reverb", FX_HEX, true),
    };
    this.vu = new Meter();
    this.fader = new Fader(col);
    this.gain = el("div", { class: "ch-gain", title: "按响度统一音量的增益（每首统一到 -14 LUFS，像 Mixxx 的 ReplayGain / Prime 的 Auto Gain）" });
    this.name = el("div", { class: "ch-name", style: `color:${col}` }, `CH ${d}`, this.gain);
    // 旋钮一列、电平 + 推子一列；CH2 镜像，两边推子都靠中间（像真混音台）
    this.root = el("div", { class: `channel ch${d}` },
      this.name,
      el("div", { class: "ch-body" + (d === 2 ? " mirror" : "") },
        el("div", { class: "ch-knobs" }, this.k.hi.root, this.k.mid.root, this.k.lo.root, this.k.filter.root,
          el("div", { class: "fx-row" }, this.k.echo.root, this.k.reverb.root)),
        el("div", { class: "fader-row" }, this.vu.root, this.fader.root)));
  }

  // mx：[hi, mid, lo, filter, echo, reverb, vu, rms dB, peak dB]（后两项只有声音引擎有）；gainDb：这首的统一音量增益
  set(mx, vol, playing, gainDb) {
    if (mx) for (const p of ["hi", "mid", "lo", "filter", "echo", "reverb"]) this.k[p].set(mx[MIX[p]]);
    if (!mx || !playing) this.vu.set(-90, null);
    else if (mx.length >= 9) this.vu.set(mx[7], mx[8]);
    else this.vu.set(vuToDb(mx[MIX.vu]), null);
    this.fader.set(vol ?? 0);
    const g = gainDb === null || gainDb === undefined ? "" : `增益 ${gainDb > 0 ? "+" : ""}${gainDb.toFixed(1)} dB`;
    if (this.gain.textContent !== g) this.gain.textContent = g;
  }
}

// ------------------------------------------------------------------ deck：信息条 + 滚动波形

class DeckView {
  constructor(d) {
    this.d = d;
    this.track = null; // {idx, id, title, bpm, bar1, wave, sections, markers, key}
    this.anchor = { pos: 0, t: 0, rate: 1, playing: false };
    this.st = null;
    this.mx = null;
    const col = DECK_HEX[d];
    this.badge = el("span", { class: "deck-no", style: `background:${col}` }, String(d));
    this.title = el("span", { class: "deck-title" }, "（空）");
    this.role = el("span", { class: "deck-role" });
    this.bpm = el("span", { class: "deck-bpm" }, "–");
    this.pitch = el("span", { class: "deck-pitch" });
    this.barEl = el("span", { class: "deck-bar" }, "–");
    this.dots = [0, 1, 2, 3].map(() => el("i"));
    this.phrase = el("span", { class: "deck-phrase" });
    this.time = el("span", { class: "deck-time" });
    this.canvas = el("canvas", { class: "live-wave" });
    this.root = el("div", { class: `deck-live d${d}` },
      el("div", { class: "deck-head" },
        this.badge, this.title, this.role, el("span", { class: "grow" }),
        el("span", { class: "deck-beat" }, this.barEl, el("span", { class: "dots" }, this.dots), this.phrase),
        el("span", { class: "deck-tempo" }, this.bpm, this.pitch), this.time),
      this.canvas);
  }

  setTrack(t) {
    this.track = t;
    this.title.textContent = t ? `#${t.no} ${t.title}` : "（空）";
  }

  setRole(text) { this.role.textContent = text || ""; this.role.style.display = text ? "" : "none"; }

  // tFrame = 这一帧里的位置从扬声器出来的时刻（页面时钟，ms）；speed = 预演倍速
  setStatus(ds, mx, tFrame, speed) {
    this.st = ds;
    this.mx = mx;
    if (!ds || !ds[0]) {
      this.anchor = { pos: 0, t: tFrame, rate: 1, playing: false };
      this.bpm.textContent = "–";
      this.pitch.textContent = "";
      this.time.textContent = "";
      this.root.classList.remove("playing");
      return;
    }
    const measured = ds[2] / 1000, playing = !!ds[1];
    const rate = ds[5] > 0 ? ds[4] / ds[5] : 1;
    const pred = this.posAt(tFrame);
    // 引擎的位置每秒更新十次：小误差平滑并入，大跳变（seek / 装新歌）直接跟上
    const pos = playing && this.anchor.playing && Math.abs(pred - measured) < 0.15 ? pred + (measured - pred) * 0.3 : measured;
    this.anchor = { pos, t: tFrame, rate, speed: speed || 1, playing };
    this.bpm.textContent = ds[4].toFixed(2);
    const pc = (rate - 1) * 100;
    this.pitch.textContent = Math.abs(pc) < 0.005 ? " 原速" : ` ${pc > 0 ? "+" : ""}${pc.toFixed(2)}%`;
    const left = Math.max(0, ds[3] / 1000 - measured) / (rate || 1);
    this.time.textContent = `-${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}`;
    this.root.classList.toggle("playing", playing);
  }

  posAt(now) {
    const a = this.anchor;
    return a.playing ? a.pos + ((now - a.t) / 1000) * a.rate * (a.speed || 1) : a.pos;
  }

  draw(now) {
    const c = this.canvas, w = c.clientWidth, H = 84;
    if (!w) return;
    const dpr = window.devicePixelRatio || 1;
    if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(H * dpr); c.style.height = H + "px"; }
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, H);
    const t = this.track, ds = this.st;
    const cx = Math.round(w / 2);
    if (!t || !ds || !ds[0]) {
      g.fillStyle = "#555"; g.font = "12px sans-serif"; g.fillText(ds && ds[0] ? "已装载" : "空", 10, 20);
      return;
    }
    const pos = this.posAt(now);
    const rate = this.anchor.rate || 1;
    const half = SPAN_S * rate;
    const X = (tt) => cx + ((tt - pos) / half) * (w / 2);
    const top = 15, mid = top + (H - top) / 2, amp = (H - top) / 2 - 1;
    const barDur = (4 * 60) / t.bpm, beatDur = 60 / t.bpm;
    const timeBar = (tt) => (tt - t.bar1) / barDur + 1;
    const barTime = (b) => t.bar1 + (b - 1) * barDur;

    // 段落底色
    for (const s of t.sections || []) {
      const x0 = X(barTime(s.start)), x1 = X(barTime(s.end));
      if (x1 < 0 || x0 > w) continue;
      g.fillStyle = SECTION_TINT[s.label] || "rgba(255,255,255,.04)";
      g.fillRect(x0, top, x1 - x0, H - top);
      if (x1 - x0 > 50) { g.fillStyle = "#7d8597"; g.font = "10px sans-serif"; g.fillText(s.label, Math.max(2, x0 + 3), H - 3); }
    }

    // 波形：Denon 三频段（wavedraw.js）；按当前 EQ / 滤波缩放各频段（看得出「低频切掉了」），整体亮度跟推子
    const wf = t.wave;
    if (wf) {
      const mx = this.mx;
      let gl = 1, gm = 1, gh = 1;
      if (mx) {
        gl = Math.min(1.6, mx[MIX.lo]); gm = Math.min(1.6, mx[MIX.mid]); gh = Math.min(1.6, mx[MIX.hi]);
        const k = Math.min(1, Math.abs(mx[MIX.filter] - 0.5) * 2);
        if (mx[MIX.filter] < 0.5) { gh *= 1 - k; gm *= 1 - 0.7 * k; } else { gl *= 1 - k; gm *= 1 - 0.7 * k; }
      }
      const vol = faderPos(ds[6]);
      g.globalAlpha = ds[1] ? 0.35 + 0.65 * vol : 0.3;
      const pps = wf.rate;
      drawTriBand(g, w, mid, amp, (x) => {
        const ta = pos + ((x - cx) / (w / 2)) * half, tb = pos + ((x + 1 - cx) / (w / 2)) * half;
        let ia = Math.floor(ta * pps), ib = Math.max(ia + 1, Math.floor(tb * pps));
        ia = Math.max(0, ia); ib = Math.min(wf.n, ib);
        if (ia >= ib) return null;
        let lo = 0, mi = 0, hi = 0;
        for (let i = ia; i < ib; i++) {
          if (wf.low[i] > lo) lo = wf.low[i];
          if (wf.mid[i] > mi) mi = wf.mid[i];
          if (wf.high[i] > hi) hi = wf.high[i];
        }
        return [(lo / 255) * gl, (mi / 255) * gm, (hi / 255) * gh];
      });
      g.globalAlpha = 1;
    }

    // 拍 / 小节 / 乐句线
    const b0 = Math.floor(timeBar(pos - half)) - 1, b1 = Math.ceil(timeBar(pos + half)) + 1;
    g.font = "10px ui-monospace, Menlo, monospace";
    for (let b = b0; b <= b1; b++) {
      const xb = X(barTime(b));
      for (let k = 1; k < 4; k++) {
        const xk = X(barTime(b) + k * beatDur);
        if (xk < 0 || xk > w) continue;
        g.strokeStyle = "rgba(255,255,255,.10)";
        g.beginPath(); g.moveTo(xk + 0.5, top); g.lineTo(xk + 0.5, top + 6); g.stroke();
      }
      if (xb < -30 || xb > w + 30 || b < 1) continue;
      const phrase = (b - 1) % 16 === 0;
      g.strokeStyle = phrase ? "rgba(255,255,255,.55)" : "rgba(255,255,255,.18)";
      g.lineWidth = phrase ? 1.5 : 1;
      g.beginPath(); g.moveTo(xb + 0.5, top); g.lineTo(xb + 0.5, H); g.stroke();
      g.lineWidth = 1;
      g.fillStyle = phrase ? "#e8ecf3" : "#8a92a3";
      g.fillText(String(b), xb + 3, 11);
    }

    // 过渡标记
    for (const m of t.markers || []) {
      const xm = X(m.t);
      if (m.t1 !== undefined) {
        const xe = X(m.t1);
        if (xe >= 0 && xm <= w) {
          g.fillStyle = m.fill || "rgba(242,184,75,.10)";
          g.fillRect(xm, top, xe - xm, H - top);
        }
      }
      if (xm < -2 || xm > w + 2) continue;
      g.strokeStyle = m.color; g.lineWidth = 2;
      g.beginPath(); g.moveTo(xm, top); g.lineTo(xm, H); g.stroke();
      g.lineWidth = 1;
      g.fillStyle = m.color; g.font = "bold 11px sans-serif";
      g.fillText(m.label, xm + 4, top + 13);
    }

    // 已播部分压暗；播放头
    g.fillStyle = "rgba(0,0,0,.28)";
    g.fillRect(0, top, cx, H - top);
    g.strokeStyle = "#fff"; g.lineWidth = 2;
    g.beginPath(); g.moveTo(cx, 0); g.lineTo(cx, H); g.stroke();
    g.lineWidth = 1;

    // 信息：小节 / 拍 / 乐句
    const bar = timeBar(pos) + 1e-3; // cue 在小节线上时别因为浮点误差显示成上一小节
    if (bar >= 1) {
      const bi = Math.floor(bar), beat = Math.floor((bar - bi) * 4);
      this.barEl.textContent = `第 ${bi} 小节`;
      this.dots.forEach((dEl, i) => dEl.classList.toggle("on", i === beat));
      this.phrase.textContent = `乐句 ${((bi - 1) % 16) + 1}/16`;
    } else {
      this.barEl.textContent = "第 1 小节前";
      this.dots.forEach((dEl) => dEl.classList.remove("on"));
      this.phrase.textContent = "";
    }
  }
}

// ------------------------------------------------------------------ 过渡时间线：每条曲线一行 + 游标 + 接下来的动作

function laneSegments(pts) {
  // 曲线 → 动作列表：阶跃（同一拍两点）或渐变（同方向的相邻段合并）
  const out = [];
  for (let i = 0; i + 1 < pts.length; i++) {
    const [a, va] = pts[i], [b, vb] = pts[i + 1];
    if (Math.abs(vb - va) < 1e-4) continue;
    const dir = Math.sign(vb - va);
    const last = out[out.length - 1];
    if (b === a) { out.push({ a, b, from: va, to: vb, step: true }); continue; }
    if (last && !last.step && last.dir === dir && Math.abs(last.b - a) < 1e-6) { last.b = b; last.to = vb; continue; }
    out.push({ a, b, from: va, to: vb, dir, step: false });
  }
  return out;
}

class Timeline {
  constructor() {
    this.prog = null;
    this.head = el("div", { class: "tl-head" });
    this.rows = el("div", { class: "tl-rows" });
    this.cursor = el("div", { class: "tl-cursor" });
    this.next = el("div", { class: "tl-next" });
    this.root = el("div", { class: "timeline-live" }, this.head, el("div", { class: "tl-wrap" }, this.rows, this.cursor), this.next);
    this.tl = { v: null, t: 0, bps: 2, running: false };
  }

  setProgram(prog, info) {
    if (this.prog === prog && this.info === info?.key) return;
    this.prog = prog;
    this.info = info?.key;
    this.rows.innerHTML = "";
    this.next.innerHTML = "";
    if (!prog) { this.head.textContent = info?.text || "没有进行中的过渡"; this.root.classList.add("idle"); return; }
    this.root.classList.remove("idle");
    this.head.innerHTML = "";
    this.head.append(el("b", {}, info.title), el("span", { class: "dim" }, info.sub || ""), el("span", { class: "grow" }), this.where = el("span", { class: "tl-where mono" }));
    const len = prog.len || 1;
    const deckOf = (side) => (side === "A" ? prog.a.deck : prog.b ? prog.b.deck : 0);
    const names = Object.keys(prog.lanes).sort((x, y) => {
      const [sx, px] = x.split("."), [sy, py] = y.split(".");
      return sx === sy ? PARAM_ORDER.indexOf(px) - PARAM_ORDER.indexOf(py) : sx < sy ? -1 : 1;
    });
    // 两组（CH 1 在上、CH 2 在下，和混音台一样），每组 7 行，顺序和左边旋钮一样。没有动作的行也画：整段过渡里它停在哪。
    // 纵坐标 = 旋钮转角（7 点在下、12 点在中间、5 点在上；推子 = 推子位置），每行左边的小旋钮是引擎读回的实际值，
    // 游标处的点是计划值——两者应该重合。
    const W = 1000, RH = 30;
    this.dots = [];
    this.minis = [];
    const evs = [...(prog.events || [])];
    if (prog.play !== null && prog.play !== undefined && prog.b) evs.unshift([prog.play, "B 起播"]);
    const addRow = (label, draw, dot) => {
      const s = svg("svg", { viewBox: `0 0 ${W} ${RH}`, preserveAspectRatio: "none", class: "tl-svg" });
      for (let b = 0; b <= len; b += 4) {
        svg("line", { x1: (b / len) * W, x2: (b / len) * W, y1: 0, y2: RH, stroke: b % 32 === 0 ? "rgba(255,255,255,.14)" : "rgba(255,255,255,.05)", "vector-effect": "non-scaling-stroke" }, s);
      }
      for (const [b, ev] of evs) { // 事件竖线贯穿每一行：B 起播（绿）、A 停 / loop（红）
        const x = (b / len) * W;
        svg("line", { x1: x, x2: x, y1: 0, y2: RH, stroke: ev === "B 起播" ? "#3ccf7a" : "#ff5d5d", "stroke-dasharray": "3 3", "vector-effect": "non-scaling-stroke" }, s);
      }
      draw(s, W, RH);
      const cell = el("div", { class: "tl-cell" }, s);
      dot.dot = el("span", { class: "tl-dot", style: `background:${dot.color}` });
      dot.RH = RH;
      cell.append(dot.dot);
      this.dots.push(dot);
      this.rows.append(label, cell);
    };
    const roleOf = (d) => (prog.a.deck === d ? "A" : prog.b && prog.b.deck === d ? "B" : null);
    for (const d of [1, 2]) {
      const side = roleOf(d);
      if (!side) continue;
      const who = (info.names || {})[side] || "";
      this.rows.append(el("div", { class: "tl-group", style: `color:${DECK_HEX[d]}` },
        `CH ${d} · ${side === "A" ? (prog.id === "end" ? "终曲" : "A 出") : "B 进"}${who ? " · " + who : ""}`));
      for (const param of LANE_PARAMS) {
        const lane = prog.lanes[`${side}.${param}`];
        const base = side === "B" && param === "vol" ? 0 : NEUTRAL_V[param];
        const pts = lane || [[0, base]];
        const moving = !!lane && pts.some(([, v]) => Math.abs(v - pts[0][1]) > 1e-4);
        const col = paramColor(param, d);
        const mini = new MiniKnob(param, col);
        const val = el("span", { class: "tl-kv mono" });
        this.minis.push({ d, param, mini, val });
        const label = el("div", { class: "tl-label" + (moving ? "" : " still") }, mini.root,
          el("span", { class: "tl-pn", style: `color:${col}` }, PARAM_LABEL[param]), val);
        const scale = (v) => laneY(param, v);
        addRow(label, (sv) => {
          const Y = (v) => 3 + (1 - scale(v)) * (RH - 6);
          if (param !== "vol" && !isFx(param)) { // 12 点 / 关 的位置
            const y = Y(NEUTRAL_V[param]);
            svg("line", { x1: 0, x2: W, y1: y, y2: y, stroke: "rgba(255,255,255,.16)", "stroke-dasharray": "2 4", "vector-effect": "non-scaling-stroke" }, sv);
          }
          const all = [[0, pts[0][1]], ...pts, [len, pts[pts.length - 1][1]]];
          const dd = all.map((q, k) => `${k ? "L" : "M"}${((q[0] / len) * W).toFixed(1)},${Y(q[1]).toFixed(1)}`).join(" ");
          svg("path", { d: dd + ` L${W},${RH} L0,${RH} Z`, fill: col, "fill-opacity": moving ? 0.14 : 0.05, stroke: "none" }, sv);
          svg("path", { d: dd, fill: "none", stroke: col, "stroke-opacity": moving ? 1 : 0.45, "stroke-width": moving ? 1.8 : 1.2, "vector-effect": "non-scaling-stroke" }, sv);
          if (side === "B" && prog.play > 0) { // B 还没起播的部分压暗
            svg("rect", { x: 0, y: 0, width: (prog.play / len) * W, height: RH, fill: "rgba(0,0,0,.45)" }, sv);
          }
        }, { side, param, pts, scale, color: col });
      }
    }
    if (evs.length) {
      const evText = el("div", { class: "tl-events dim" });
      evText.textContent = "事件（虚线）：" + evs.map(([b, ev]) => `${ev.replace("A.stop", "A 停").replace("B.stop", "B 停")} @ 第 ${(b / 4 + 1).toFixed(b % 4 ? 2 : 0)} 小节`).join("，");
      this.rows.append(el("div"), evText);
    }
    // 动作表
    this.moves = [];
    for (const name of names) {
      const [side, param] = name.split(".");
      for (const sgm of laneSegments(prog.lanes[name])) this.moves.push({ side, param, deck: deckOf(side), ...sgm });
    }
    for (const [b, ev] of prog.events || []) this.moves.push({ side: ev[0], event: ev, deck: deckOf(ev[0]), a: b, b, step: true });
    this.moves.sort((x, y) => x.a - y.a);
  }

  setTl(tl, bpm, running, now) {
    if (tl === null) { this.tl = { v: null, t: now, bps: 2, running: false }; return; }
    const pred = this.tlAt(now);
    const v = this.tl.running && running && pred !== null && Math.abs(pred - tl) < 0.5 ? pred + (tl - pred) * 0.3 : tl;
    this.tl = { v, t: now, bps: bpm / 60, running };
  }

  tlAt(now) {
    const s = this.tl;
    if (s.v === null) return null;
    return s.running ? s.v + ((now - s.t) / 1000) * s.bps : s.v;
  }

  draw(now) {
    const p = this.prog;
    if (!p) return;
    const tl = this.tlAt(now);
    if (tl === null) { this.cursor.style.display = "none"; return; }
    const f = Math.max(0, Math.min(1, tl / p.len));
    this.cursor.style.display = tl < 0 ? "none" : "";
    this.cursor.style.left = `calc(var(--tl-label) + (100% - var(--tl-label)) * ${f})`;
    if (this.where) {
      this.where.textContent = tl < 0 ? `还有 ${(-tl / 4).toFixed(1)} 小节开始` : `第 ${(tl / 4 + 1).toFixed(1)} / ${p.len / 4} 小节`;
    }
    for (const d of this.dots || []) {
      const hide = tl < 0 || (d.side === "B" && p.play !== null && p.play !== undefined && tl < p.play);
      d.dot.style.display = hide ? "none" : "";
      if (hide) continue;
      const v = laneAt(d.pts, Math.min(tl, p.len));
      d.dot.style.left = `${f * 100}%`;
      d.dot.style.top = `${((3 + (1 - d.scale(v)) * (d.RH - 6)) / d.RH) * 100}%`;
    }
  }

  // 每行左边的小旋钮 = 引擎读回的实际值（和混音台上的旋钮是同一个数）
  setMixer(m, ds) {
    for (const k of this.minis || []) {
      const mx = m ? m[k.d - 1] : null, dsd = ds ? ds[k.d - 1] : null;
      const v = k.param === "vol" ? (dsd ? dsd[6] : null) : mx ? mx[MIX[k.param]] : null;
      if (v === null || v === undefined) continue;
      k.mini.set(v);
      const t = k.param === "vol" ? fmtParam("vol", v).replace("推子 ", "") : fmtParam(k.param, v) || "关";
      if (k.val.textContent !== t) k.val.textContent = t;
    }
  }

  updateNext(now) {
    const p = this.prog;
    if (!p || !this.moves) return;
    const tl = this.tlAt(now) ?? -1e9;
    const cur = [], up = [];
    for (const m of this.moves) {
      if (m.b < tl - 0.01) continue;
      if (m.a <= tl && !m.step) cur.push(m);
      else if (m.a > tl) up.push(m);
    }
    const who = (m) => el("b", { style: `color:${DECK_HEX[m.deck]}` }, `${m.side}（Deck ${m.deck}）`);
    const what = (m) => el("b", { style: isEq(m.param) ? `color:${BAND_HEX[m.param]}` : "" }, PARAM_LABEL[m.param] || m.param);
    const barNo = (b) => (b / 4 + 1).toFixed(b % 4 ? 2 : 0);
    const out = [el("div", { class: "tl-sec big" }, "现在的 EQ 操作")];
    if (tl < 0) {
      out.push(el("div", { class: "eq-op idle" }, `过渡还有 ${(-tl / 4).toFixed(1)} 小节开始`));
    }
    const eqNow = cur.filter((m) => isEq(m.param));
    for (const m of eqNow) {
      const v = laneAt(p.lanes[`${m.side}.${m.param}`], tl);
      out.push(el("div", { class: "eq-op", style: `border-color:${BAND_HEX[m.param]}` },
        who(m), " ", what(m), ` ${fmtParam(m.param, m.from)} → ${fmtParam(m.param, m.to)}`,
        el("span", { class: "eq-op-now" }, `现在 ${fmtParam(m.param, v)}（${fmtSub(m.param, v)}）`),
        el("span", { class: "eq-op-left" }, `还剩 ${((m.b - tl) / 4).toFixed(1)} 小节`)));
    }
    // 停在非 12 点的 EQ（比如 B 的 LOW 先切着）
    if (tl >= 0) {
      const held = [];
      for (const [name, pts] of Object.entries(p.lanes)) {
        const [side, param] = name.split(".");
        if (!isEq(param) || eqNow.some((m) => m.side === side && m.param === param)) continue;
        if (side === "B" && p.play !== null && p.play !== undefined && tl < p.play) continue; // B 还没起播
        const v = laneAt(pts, tl);
        if (v !== null && Math.abs(dB(v)) > 0.5) held.push([side, param, v]);
      }
      if (held.length) {
        out.push(el("div", { class: "eq-held" }, "保持：", ...held.map(([side, param, v], i) => [
          i ? "、" : "", el("b", { style: `color:${DECK_HEX[side === "A" ? p.a.deck : p.b.deck]}` }, side), " ",
          el("b", { style: `color:${BAND_HEX[param]}` }, PARAM_LABEL[param]), ` ${fmtParam(param, v)}`])));
      }
    }
    if (!eqNow.length && tl >= 0) out.push(el("div", { class: "eq-op idle" }, "EQ 现在没有在动"));
    const nx = up.find((m) => isEq(m.param));
    if (nx) {
      out.push(el("div", { class: "eq-next" }, `下一个 EQ 动作：第 ${barNo(nx.a)} 小节 `, who(nx), " ", what(nx),
        ` ${fmtParam(nx.param, nx.from)} → ${fmtParam(nx.param, nx.to)}`, nx.step ? "（瞬间）" : `，用 ${((nx.b - nx.a) / 4).toFixed(nx.b % 4 ? 1 : 0)} 小节`,
        `（还有 ${((nx.a - tl) / 4).toFixed(1)} 小节）`));
    } else if (!eqNow.length && tl >= 0) {
      out.push(el("div", { class: "eq-next" }, "这段过渡后面没有 EQ 动作了"));
    }
    const other = cur.filter((m) => !isEq(m.param));
    if (other.length) {
      out.push(el("div", { class: "eq-other" }, "同时：", ...other.map((m, i) => [i ? "；" : "", el("b", { style: `color:${DECK_HEX[m.deck]}` }, m.side), " ",
        what(m), ` ${fmtParam(m.param, m.from)} → ${fmtParam(m.param, m.to)}，还剩 ${((m.b - tl) / 4).toFixed(1)} 小节`])));
    }
    this.next.replaceChildren(...out);
  }
}

// 曲线在 x 拍处的值（分段线性；同一拍两点 = 阶跃，取后一个）
function laneAt(pts, x) {
  if (!pts || !pts.length) return null;
  if (x <= pts[0][0]) return pts[0][1];
  for (let i = pts.length - 1; i >= 0; i--) {
    if (pts[i][0] <= x) {
      const nx = pts[i + 1];
      if (!nx || nx[0] === pts[i][0]) return pts[i][1];
      return pts[i][1] + ((nx[1] - pts[i][1]) * (x - pts[i][0])) / (nx[0] - pts[i][0]);
    }
  }
  return pts[0][1];
}

// ------------------------------------------------------------------ 交接概览：两首歌的整首波形 + 进出点 + 小节计数

class Handoff {
  constructor() {
    this.title = el("b");
    this.sub = el("span", { class: "dim" });
    this.where = el("span", { class: "ho-where mono" });
    const legend = el("span", { class: "ho-legend", title: "Denon（Engine DJ）三频段波形：三段各自的音量叠在一起" }, "波形：",
      [["lo", "低频 LOW"], ["mid", "中频 MID"], ["hi", "高频 HIGH"]].map(([b, name]) => el("span", {}, el("i", { style: `background:${WAVE_COLORS[b]}` }), name)));
    this.rows = { A: this._row(), B: this._row() };
    this.root = el("div", { class: "handoff hidden" },
      el("div", { class: "ho-head" }, this.title, this.sub, el("span", { class: "grow" }), legend, this.where),
      this.rows.A.root, this.rows.B.root);
    this.cache = new Map();
    this.key = null;
  }

  _row() {
    const r = { badge: el("span", { class: "deck-no" }), name: el("span", { class: "ho-name" }), bar: el("div", { class: "ho-bar mono" }),
      canvas: el("canvas", { class: "ho-wave" }), t: null };
    r.root = el("div", { class: "ho-row" }, el("div", { class: "ho-label" }, el("div", { class: "ho-title" }, r.badge, r.name), r.bar), r.canvas);
    return r;
  }

  // info：{ key, title, sub, len, A: track, B: track | null }；track = { id, no, title, deck, bpm, bar1, dur, bars, wave, sections, marks }
  set(info) {
    this.root.classList.toggle("hidden", !info);
    if (!info || info.key === this.key) { if (info) this.info = info; return; }
    this.key = info.key;
    this.info = info;
    this.title.textContent = info.title;
    this.sub.textContent = info.sub;
    for (const side of ["A", "B"]) {
      const r = this.rows[side], t = info[side];
      r.t = t;
      r.root.classList.toggle("hidden", !t);
      if (!t) continue;
      r.badge.textContent = String(t.deck);
      r.badge.style.background = DECK_HEX[t.deck];
      r.name.textContent = `${side} · #${t.no} ${t.title}`;
    }
  }

  _overview(t, w, h, dpr) {
    const key = `${t.id}:${w}:${h}:${dpr}:${t.wave ? 1 : 0}:${(t.sections || []).length}`;
    if (this.cache.has(key)) return this.cache.get(key);
    const c = document.createElement("canvas");
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const X = (tt) => (tt / t.dur) * w;
    const barDur = 240 / t.bpm;
    for (const s of t.sections || []) {
      const x0 = X(t.bar1 + (s.start - 1) * barDur), x1 = X(t.bar1 + (s.end - 1) * barDur);
      g.fillStyle = SECTION_TINT[s.label] || "rgba(255,255,255,.04)";
      g.fillRect(x0, 0, x1 - x0, h);
    }
    const wf = t.wave;
    if (wf) {
      const mid = h / 2, amp = h / 2 - 2, pps = wf.rate;
      drawTriBand(g, w, mid, amp, (x) => {
        let ia = Math.floor(((x / w) * t.dur) * pps), ib = Math.max(ia + 1, Math.floor((((x + 1) / w) * t.dur) * pps));
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
    // 乐句线（每 16 小节），每 32 小节标小节号
    g.font = "9px ui-monospace, Menlo, monospace";
    for (let b = 1; b <= t.bars + 1; b += 16) {
      const x = X(t.bar1 + (b - 1) * barDur);
      g.fillStyle = "rgba(255,255,255,.22)";
      g.fillRect(Math.round(x), 0, 1, h);
      if ((b - 1) % 32 === 0) { g.fillStyle = "#8a92a3"; g.fillText(String(b), x + 2, h / 2 - 2); }
    }
    if (this.cache.size > 12) this.cache.clear();
    this.cache.set(key, c);
    return c;
  }

  draw(now, posOf, tl) {
    const info = this.info;
    if (!info || this.root.classList.contains("hidden")) return;
    const len = info.len;
    this.where.textContent = tl === null || tl === undefined ? "" : tl < 0 ? `还有 ${(-tl / 4).toFixed(1)} 小节开始交接`
      : tl <= len ? `交接 第 ${(tl / 4 + 1).toFixed(1)} / ${len / 4} 小节` : "交接完成";
    for (const side of ["A", "B"]) {
      const r = this.rows[side], t = r.t;
      if (!t) continue;
      const c = r.canvas, w = c.clientWidth, H = 54;
      if (!w) continue;
      const dpr = window.devicePixelRatio || 1;
      if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(H * dpr); c.style.height = H + "px"; }
      const g = c.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, H);
      g.drawImage(this._overview(t, w, H, dpr), 0, 0, w, H);
      const X = (tt) => (tt / t.dur) * w;
      const barOf = (tt) => (tt - t.bar1) / (240 / t.bpm) + 1;
      // 进出区段和标记
      for (const m of t.marks) {
        const x = X(m.t);
        if (m.t1 !== undefined) {
          const x1 = X(m.t1);
          g.fillStyle = m.fill;
          g.fillRect(x, 0, x1 - x, H);
        }
        g.fillStyle = m.color;
        g.fillRect(Math.round(x) - 1, 0, 2, H);
        g.font = "bold 11px sans-serif";
        const label = `${m.label} ${Math.floor(barOf(m.t) + 1e-3)}`;
        const tw = g.measureText(label).width;
        const lx = m.right ? Math.max(2, x - tw - 4) : Math.min(w - tw - 2, x + 4);
        const ly = m.top ? 2 : H - 16; // 起点类标签在上、终点类在下，挨得近也不重叠
        g.fillStyle = "rgba(15,17,21,.8)";
        g.fillRect(lx - 2, ly, tw + 4, 14);
        g.fillStyle = m.color;
        g.fillText(label, lx, ly + 11);
      }
      const pos = posOf(t);
      if (pos !== null) {
        const xp = X(pos);
        g.fillStyle = "rgba(0,0,0,.35)";
        g.fillRect(0, 0, xp, H);
        g.fillStyle = "#fff";
        g.fillRect(Math.round(xp) - 1, 0, 2, H);
        r.bar.textContent = `第 ${Math.max(1, Math.floor(barOf(pos) + 1e-3))} / ${Math.floor(t.bars)} 小节`;
      } else {
        r.bar.textContent = `共 ${Math.floor(t.bars)} 小节`;
      }
    }
  }
}

// ------------------------------------------------------------------ 总装

export class LiveView {
  constructor(root, loadTrack) {
    this.loadTrack = loadTrack; // id → Promise<{wave, sections}>
    this.cache = new Map();
    this.loaded = new Map(); // id → {wave, sections}（已经下载好的）
    this.comp = null;
    this.progress = null;
    this.decks = { 1: new DeckView(1), 2: new DeckView(2) };
    this.ch = { 1: new Channel(1), 2: new Channel(2) };
    this.mvu = new Meter();
    this.xcap = el("div", { class: "xf-cap" });
    this.timeline = new Timeline();
    this.handoff = new Handoff();
    this.banner = el("div", { class: "live-banner hidden" });
    this.mixer = el("div", { class: "mixer" },
      el("div", { class: "mixer-chs" },
        this.ch[1].root,
        el("div", { class: "master" }, el("div", { class: "ch-name" }, "MASTER"), el("div", { class: "kl" }, "主输出 dBFS"),
          el("div", { class: "vu-wrap" }, meterScale(), this.mvu.root)),
        this.ch[2].root),
      el("div", { class: "xfader" }, el("span", { class: "kl" }, "1"), el("div", { class: "xf-track" }, this.xcap), el("span", { class: "kl" }, "2")));
    this.mixer.title = "旋钮 / 推子 / 电平都是从引擎读回的实际值";
    root.append(this.banner, this.handoff.root,
      el("div", { class: "live-decks" }, this.decks[1].root, this.decks[2].root),
      el("div", { class: "live-bottom" }, this.mixer, this.timeline.root));
    this._raf = 0;
    this._lastNext = 0;
    const loop = (now) => {
      if (root.isConnected && root.offsetParent !== null) { // 不在演出页时不画
        this.decks[1].draw(now); this.decks[2].draw(now);
        this.timeline.draw(now);
        this.handoff.draw(now, (t) => this._posOf(t, now), this.timeline.tlAt(now));
        if (now - this._lastNext > 250) { this._lastNext = now; this.timeline.updateNext(now); }
      }
      this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  setSet(comp) {
    if (this.comp === comp) return;
    this.comp = comp;
    this._assigned = null;
  }

  setBanner(text) {
    this.banner.textContent = text || "";
    this.banner.classList.toggle("hidden", !text);
  }

  async _trackData(id) {
    if (!this.cache.has(id)) {
      this.cache.set(id, this.loadTrack(id).catch(() => ({})).then((x) => { this.loaded.set(id, x); return x; }));
    }
    return this.cache.get(id);
  }

  // 交接概览：当前（或下一段）过渡的 A、B 两首，整首波形上标出过渡开始 / B 起播 / 交出 / B 进点 / 接满
  _handoffInfo(prog, comp) {
    const p = this.progress;
    if (!prog || !comp || !p || p.stage === "idle" || p.stage === "done") return null;
    const off = comp._offset || 0;
    const end = prog.id === "end";
    const ai = prog.idx, bi = end ? null : prog.idx + 1;
    const beatT = (grid, beat) => grid[0] / 1000 + (beat * 60) / grid[1];
    const track = (k, deck) => {
      const ct = comp.tracks[k], st = comp.set.tracks[k];
      this._trackData(ct.id);
      const data = this.loaded.get(ct.id) || {};
      return { idx: k, id: ct.id, no: k + 1 + off, title: st.title || ct.title || ct.id, deck, bpm: ct.bpm, bar1: ct.bar1_time,
        dur: ct.duration, bars: ct.bars, wave: data.wave || null, sections: data.sections || [], marks: [] };
    };
    const A = track(ai, prog.a.deck);
    const B = bi === null ? null : track(bi, prog.b.deck);
    const t0 = beatT(prog.a.grid, prog.a.start);
    const stop = (prog.events || []).find(([, ev]) => ev === "A.stop");
    const aOut = stop ? beatT(prog.a.grid, prog.a.start + stop[0]) : prog.playout ? A.dur : beatT(prog.a.grid, prog.a.start + prog.len);
    A.marks.push({ t: t0, t1: aOut, label: end ? "淡出开始" : "过渡开始", color: "#f2b84b", fill: "rgba(242,184,75,.16)", top: true });
    if (B && prog.play !== null && prog.play !== undefined) {
      A.marks.push({ t: beatT(prog.a.grid, prog.a.start + prog.play), label: "B 起播", color: "#3ccf7a" });
    }
    A.marks.push({ t: Math.min(aOut, A.dur), label: end ? "停" : stop ? "交出" : prog.playout ? "放到曲尾" : "交出", color: "#f2b84b", right: true });
    if (B) {
      const tin = beatT(prog.b.grid, prog.b.in), tFull = beatT(prog.b.grid, prog.b.in + prog.len - (prog.play || 0));
      B.marks.push({ t: tin, t1: tFull, label: "进点", color: "#3ccf7a", fill: "rgba(60,207,122,.14)", top: true });
      B.marks.push({ t: tFull, label: "接满", color: "#3ccf7a", right: true });
    }
    const tr = end ? null : comp.set.transitions[prog.idx];
    return {
      key: `${comp._key || comp._id}:${prog.id}:${A.wave ? 1 : 0}${A.sections.length}:${B && B.wave ? 1 : 0}${B ? B.sections.length : 0}`,
      title: end ? "终曲" : `交接 ${prog.idx + 1 + off} / ${comp.programs.length + off} · ${tr.label || TECH_LABEL[tr.technique] || tr.technique}`,
      sub: end ? ` #${A.no} ${A.title} 淡出` : ` #${A.no} ${A.title} → #${B.no} ${B.title}`,
      len: prog.len, A, B,
    };
  }

  _posOf(t, now) {
    const dv = this.decks[t.deck];
    if (!dv.track || dv.track.idx !== t.idx || !dv.st || !dv.st[0]) return null;
    return dv.posAt(now);
  }

  // 哪首歌在哪个 deck、各自在当前 / 下一段程序里的角色
  _assign() {
    const comp = this.comp, p = this.progress;
    if (!comp || !p || p.stage === "idle") return { 1: null, 2: null };
    const i = p.index;
    const deckFor = (k) => (k % 2 === 0 ? 1 : 2);
    const out = { 1: null, 2: null };
    out[deckFor(i)] = i;
    if (i + 1 < comp.tracks.length && p.stage !== "done") out[deckFor(i + 1)] = i + 1;
    return out;
  }

  _program() {
    const comp = this.comp, p = this.progress;
    if (!comp || !p) return null;
    if (p.index < comp.programs.length) return comp.programs[p.index];
    return comp.ending || null;
  }

  _markers(k, prog) {
    const comp = this.comp;
    const out = [];
    if (!prog) return out;
    const beatT = (grid, beat) => grid[0] / 1000 + (beat * 60) / grid[1];
    const isEnd = prog.id === "end";
    if (prog.a && k === prog.idx) {
      const t0 = beatT(prog.a.grid, prog.a.start), t1 = beatT(prog.a.grid, prog.a.start + prog.len);
      out.push({ t: t0, t1, label: isEnd ? "淡出" : "过渡开始", color: "#f2b84b", fill: "rgba(242,184,75,.09)" });
      out.push({ t: t1, label: isEnd ? "停" : "交出", color: "#f2b84b" });
    }
    if (prog.b && k === prog.idx + 1) {
      const tin = beatT(prog.b.grid, prog.b.in);
      out.push({ t: tin, t1: beatT(prog.b.grid, prog.b.in + prog.len - (prog.play || 0)), label: "B 进", color: "#3ccf7a", fill: "rgba(60,207,122,.08)" });
    }
    return out;
  }

  update(engine, progress) {
    const now = performance.now();
    // 状态帧里的位置是什么时候「听到」的：服务器给了（收到时刻 + 声卡延迟，Unix 毫秒）就换算到页面时钟，
    // 否则只能当作现在（会比声音晚一个轮询间隔）
    let tFrame = engine && engine.at ? engine.at - performance.timeOrigin : now;
    if (Math.abs(tFrame - now) > 2000) tFrame = now; // 两边时钟对不上（不该发生）：退回按收到的时刻
    const speed = (engine && engine.speed) || 1;
    this.progress = progress;
    const st = engine && engine.status;
    const asg = this._assign();
    const prog = this._program();
    const comp = this.comp;
    for (const d of [1, 2]) {
      const k = asg[d];
      const dv = this.decks[d];
      if (k === null || !comp) { if (dv.track) dv.setTrack(null); }
      else if (!dv.track || dv.track.idx !== k || dv.track.prog !== prog) {
        const ct = comp.tracks[k], stt = comp.set.tracks[k];
        const t = { idx: k, no: k + 1 + (comp._offset || 0), id: ct.id, title: stt.title || ct.id, bpm: ct.bpm, bar1: ct.bar1_time, prog, markers: this._markers(k, prog), wave: null, sections: [] };
        dv.setTrack(t);
        this._trackData(ct.id).then((x) => { if (dv.track === t) { t.wave = x.wave || null; t.sections = x.sections || []; } });
      }
      const ds = st ? st.d[d - 1] : null;
      const mx = st && st.m ? st.m[d - 1] : null;
      dv.setStatus(ds, mx, tFrame, speed);
      const gains = (engine && engine.gain_db) || [];
      this.ch[d].set(mx, ds ? ds[6] : 0, !!(ds && ds[1]), k === null ? null : gains[k]);
      const main = progress && comp && k === progress.index;
      const inTr = progress && progress.stage === "transition" && prog && prog.id !== "end";
      dv.setRole(k === null ? "" : inTr ? (k === prog.idx ? "A · 出" : "B · 进") : main ? "主奏" : "下一首");
    }
    this.handoff.set(this._handoffInfo(prog, comp));
    if (!st) this.mvu.set(-90, null);
    else if (st.ml) this.mvu.set(st.ml[0], st.ml[1]);
    else this.mvu.set(vuToDb(st.mv), null);
    if (st && st.x !== undefined) this.xcap.style.left = `${((st.x + 1) / 2) * 100}%`;

    // 时间线
    const pg = st && st.pg;
    if (prog && comp && progress && !["done", "stopped", "error", "idle"].includes(progress.stage)) {
      const tr = prog.id === "end" ? null : comp.set.transitions[prog.idx];
      const ta = comp.set.tracks[prog.idx], tb = comp.set.tracks[prog.idx + 1];
      const off = comp._offset || 0; // 从第 k 段过渡开始：编号照原来的
      const title = prog.id === "end" ? "终曲淡出" : `过渡 ${prog.idx + 1 + off} / ${comp.programs.length + off} · ${tr.label || TECH_LABEL[tr.technique] || tr.technique}`;
      const sub = prog.id === "end" ? ` #${prog.idx + 1 + off} ${ta.title || ta.id}`
        : ` A = Deck ${prog.a.deck} #${prog.idx + 1 + off} ${ta.title || ta.id}（第 ${tr.start} 小节起）→ B = Deck ${prog.b.deck} #${prog.idx + 2 + off} ${tb.title || tb.id}（从第 ${tr.b_in} 小节进）`;
      const names = { A: `#${prog.idx + 1 + off} ${ta.title || ta.id}`, B: tb ? `#${prog.idx + 2 + off} ${tb.title || tb.id}` : "" };
      this.timeline.setProgram(prog, { key: prog.id + ":" + (comp._id || ""), title, sub, names });
      this.timeline.setMixer(st ? st.m : null, st ? st.d : null);
      if (pg && pg.id.replace("!", "") === prog.id) {
        const bpm = st.d[prog.a.deck - 1][1] ? st.d[prog.a.deck - 1][4] : prog.b ? st.d[prog.b.deck - 1][4] : 120;
        this.timeline.setTl(pg.tl, (bpm || 120) * speed, pg.st === "running" || pg.st === "armed", tFrame);
      } else {
        this.timeline.setTl(null, 120, false, now);
      }
    } else {
      this.timeline.setProgram(null, { key: "none", text: progress && progress.stage === "done" ? "整套播完" : "还没有开始播放" });
    }
  }
}
