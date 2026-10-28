// 假 Mixxx：在 Node 里加载真正的 DJStudio.js 映射脚本，模拟 Mixxx 的 deck / AutoDJ / 定时器 / MIDI，
// 用于在没有 Mac 和 Mixxx 的环境里端到端测试引擎脚本与 Python conductor。
//
// 用法：node sim/fake_mixxx.mjs --mapping path/DJStudio.js [--speed 20]
// stdin / stdout 每行一条：十六进制 = 原始 MIDI 消息；'#' 开头 = 模拟器控制命令 / 回复（JSON）。
// 虚拟时钟按 speed 倍速推进；引擎脚本里的 Date.now() 与定时器都走虚拟时钟。
//
// 这里只模拟引擎脚本用到的那部分 Mixxx 行为，且按我们对 Mixxx 的理解写——它不能替代真机验证
// （真机验证见 `djstudio probe`）。

import fs from "node:fs";
import vm from "node:vm";
import readline from "node:readline";
import path from "node:path";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, arr) => (a.startsWith("--") ? [...acc, [a.slice(2), arr[i + 1]]] : acc), [])
);
const SPEED = parseFloat(args.speed || "1");
const MAPPING = args.mapping;
const XML = MAPPING.replace(/\.js$/, ".midi.xml");
const STEP_MS = 5; // 虚拟时间步长
const LOAD_LATENCY_MS = 250;
const SR = 44100;

let vnow = 0; // 虚拟毫秒
const epoch = 1_700_000_000_000;
let frozen = false; // 故障注入：引擎卡死（不跑定时器、不收不发）
let dropOut = 0; // 故障注入：丢弃接下来 N 条外发消息
const history = []; // [t, group, key, value]
const library = new Map(); // id -> {id, dur, bpm, first_beat}
let autodjQueue = [];
let loadWrongNext = false;
const log = (...a) => process.stderr.write(a.join(" ") + "\n");

// ------------------------------------------------------------------ deck 模型

function makeDeck(n) {
  return { n, track: null, playing: false, pos: 0, rate: 1, loop: null, ctl: {} };
}
const decks = { 1: makeDeck(1), 2: makeDeck(2) };
const controls = new Map(); // 非 deck 控件："group,key" -> value
const defaults = {
  volume: 1, "parameter1": 1, "parameter2": 1, "parameter3": 1, super1: 0.5, orientation: 1,
  beatloop_size: 4, quantize: 0, keylock: 0,
};

function deckOf(group) {
  let m = /^\[Channel(\d)\]$/.exec(group);
  if (m) return { deck: decks[m[1]], kind: "ch" };
  m = /^\[EqualizerRack1_\[Channel(\d)\]_Effect1\]$/.exec(group);
  if (m) return { deck: decks[m[1]], kind: "eq" };
  m = /^\[QuickEffectRack1_\[Channel(\d)\]\]$/.exec(group);
  if (m) return { deck: decks[m[1]], kind: "qfx" };
  return null;
}

const beatLen = (t) => 60 / t.bpm;
const beatPos = (d) => (d.pos - d.track.first_beat) / beatLen(d.track);
const otherDeck = (d) => decks[d.n === 1 ? 2 : 1];

function getValue(group, key) {
  const dd = deckOf(group);
  if (dd && dd.kind === "ch") {
    const d = dd.deck, t = d.track;
    switch (key) {
      case "play": return d.playing ? 1 : 0;
      case "track_loaded": return t ? 1 : 0;
      case "duration": return t ? t.dur : 0;
      case "track_samples": return t ? Math.round(t.dur * SR * 2) : 0;
      case "track_samplerate": return t ? SR : 0;
      case "playposition": return t ? d.pos / t.dur : 0;
      case "file_bpm": return t ? t.bpm : 0;
      case "bpm": return t ? t.bpm * d.rate : 0;
      case "beat_distance": { if (!t) return 0; const b = beatPos(d); return b - Math.floor(b); }
      case "loop_enabled": return d.loop ? 1 : 0;
    }
  }
  const k = dd ? `${group},${key}` : `${group},${key}`;
  if (controls.has(k)) return controls.get(k);
  return defaults[key] ?? 0;
}

function setValue(group, key, value) {
  if (frozen) return;
  history.push([Math.round(vnow), group, key, value]);
  const dd = deckOf(group);
  if (dd && dd.kind === "ch") {
    const d = dd.deck, t = d.track;
    switch (key) {
      case "play":
        d.playing = !!value && !!t && d.pos < t.dur;
        return;
      case "playposition":
        if (t) d.pos = Math.max(0, Math.min(t.dur, value * t.dur));
        return;
      case "bpm":
        if (t && value > 0) d.rate = value / t.bpm;
        return;
      case "eject":
        if (value && !d.playing) { d.track = null; d.pos = 0; d.loop = null; }
        return;
      case "beatloop_activate":
        if (value && t) {
          const size = getValue(group, "beatloop_size");
          const start = Math.floor(beatPos(d));
          d.loop = { start: t.first_beat + start * beatLen(t), len: size * beatLen(t) };
        }
        return;
      case "reloop_toggle":
        if (value) d.loop = null;
        return;
      case "beatsync_phase":
        if (value && t) {
          const o = otherDeck(d);
          if (o.playing && o.track) {
            const fo = beatPos(o) - Math.floor(beatPos(o));
            const b = beatPos(d);
            let target = Math.floor(b) + fo;
            if (target - b > 0.5) target -= 1;
            if (b - target > 0.5) target += 1;
            d.pos = t.first_beat + target * beatLen(t);
          }
        }
        return;
    }
  }
  if (group === "[AutoDJ]" && key === "enabled") {
    setAutoDJ(!!value);
  }
  controls.set(`${group},${key}`, value);
}

function loadTrack(d, id) {
  const t = library.get(id);
  d.track = t ? { ...t } : null;
  d.pos = 0;
  d.rate = 1;
  d.loop = null;
  d.playing = false;
}

// ------------------------------------------------------------------ AutoDJ（按我们对 Mixxx 的理解）
// 启用时：两 deck 都在播 → 拒绝；都没播 → 队首装 deck1 并起播，随后预载下一首到 deck2；
// 一边在播 → 队首装进另一边。装载有延迟，装载后从队列移除。

let autodj = false;
const pendingLoads = []; // {at, deck, then}
function setAutoDJ(on) {
  if (on && decks[1].playing && decks[2].playing) { controls.set("[AutoDJ],enabled", 0); return; }
  autodj = on;
  if (!on) { pendingLoads.length = 0; return; }
  const p1 = decks[1].playing, p2 = decks[2].playing;
  if (!p1 && !p2) {
    pendingLoads.push({ at: vnow + LOAD_LATENCY_MS, deck: 1, play: true, preloadNext: true });
  } else {
    pendingLoads.push({ at: vnow + LOAD_LATENCY_MS, deck: p1 ? 2 : 1, play: false });
  }
}

function runAutoDJ() {
  while (pendingLoads.length && pendingLoads[0].at <= vnow) {
    const L = pendingLoads.shift();
    if (!autodj) continue;
    const d = decks[L.deck];
    if (d.playing) continue;
    if (loadWrongNext && autodjQueue.length > 1) { autodjQueue.shift(); loadWrongNext = false; }
    const id = autodjQueue.shift();
    if (id === undefined) continue;
    loadTrack(d, id);
    if (L.play) d.playing = true;
    if (L.preloadNext) pendingLoads.push({ at: vnow + LOAD_LATENCY_MS * 2, deck: 2, play: false });
  }
}

// ------------------------------------------------------------------ 音频推进

function advanceAudio(dtMs) {
  for (const d of [decks[1], decks[2]]) {
    if (!d.playing || !d.track) continue;
    d.pos += (dtMs / 1000) * d.rate;
    if (d.loop && d.pos >= d.loop.start + d.loop.len) d.pos -= d.loop.len;
    if (d.pos >= d.track.dur) { d.pos = d.track.dur; d.playing = false; }
  }
}

// ------------------------------------------------------------------ 定时器

let timerSeq = 0;
const timers = new Map(); // id -> {next, ms, fn, once}
function beginTimer(ms, fn, once) {
  const id = ++timerSeq;
  timers.set(id, { next: vnow + Math.max(20, ms), ms: Math.max(20, ms), fn, once: !!once });
  return id;
}
function stopTimer(id) { timers.delete(id); }
function runTimers() {
  if (frozen) return;
  for (const [id, t] of [...timers]) {
    if (t.next <= vnow) {
      if (t.once) timers.delete(id); else t.next += t.ms;
      try { (typeof t.fn === "function" ? t.fn : ctx[t.fn])(); } catch (e) { log("timer error:", e.stack || e); }
    }
  }
}

// ------------------------------------------------------------------ MIDI

function out(bytes) {
  if (frozen) return;
  if (dropOut > 0) { dropOut--; return; }
  process.stdout.write(Buffer.from(bytes).toString("hex") + "\n");
}

const midi = {
  sendShortMsg: (s, a, b) => out([s, a, b]),
  sendSysexMsg: (data, len) => out(Array.from(data).slice(0, len ?? data.length)),
};

// 解析 XML 映射里的 CC → 脚本函数
const bindings = [];
if (fs.existsSync(XML)) {
  const xml = fs.readFileSync(XML, "utf8");
  for (const m of xml.matchAll(/<control>([\s\S]*?)<\/control>/g)) {
    const body = m[1];
    const get = (tag) => (new RegExp(`<${tag}>([^<]*)</${tag}>`).exec(body) || [])[1];
    if (!/script-binding/.test(body)) continue;
    bindings.push({ status: parseInt(get("status")), midino: parseInt(get("midino")), key: get("key"), group: get("group") });
  }
}

const ctx = vm.createContext({
  engine: {
    getValue, setValue,
    getParameter: getValue, setParameter: setValue,
    beginTimer, stopTimer,
    makeConnection: () => ({ disconnect() {}, trigger() {} }),
  },
  midi,
  print: (s) => log("[script]", s),
  console: { log: (...a) => log("[script]", ...a) },
  Date: { now: () => epoch + vnow },
  Math, JSON, String, Array, Object, parseFloat, parseInt, Uint8Array, ArrayBuffer,
});
vm.runInContext(fs.readFileSync(MAPPING, "utf8") + "\n;globalThis.__DJ = DJStudio;", ctx, { filename: path.basename(MAPPING) });
const DJ = ctx.__DJ;

function resolveFn(key) {
  return key.split(".").reduce((o, k) => (o ? o[k] : undefined), { DJStudio: DJ });
}

function incoming(bytes) {
  if (frozen) return;
  if (bytes[0] === 0xf0) {
    DJ.incomingData(Uint8Array.from(bytes), bytes.length);
    return;
  }
  for (const b of bindings) {
    if (b.status === bytes[0] && b.midino === bytes[1]) {
      const fn = resolveFn(b.key);
      if (fn) fn(bytes[0] & 0x0f, bytes[1], bytes[2], bytes[0], b.group);
    }
  }
}

// ------------------------------------------------------------------ 模拟器控制命令

function simCommand(c) {
  const reply = (o) => process.stdout.write("#" + JSON.stringify({ _id: c._id, ...o }) + "\n");
  switch (c.cmd) {
    case "library":
      for (const t of c.tracks) library.set(t.id, t);
      return reply({ ok: true, n: library.size });
    case "queue":
      autodjQueue = [...c.ids];
      return reply({ ok: true });
    case "load": // 直接装载（测试用）
      loadTrack(decks[c.deck], c.id);
      return reply({ ok: true });
    case "freeze": frozen = true; return reply({ ok: true });
    case "unfreeze": frozen = false; return reply({ ok: true });
    case "drop_out": dropOut = c.n; return reply({ ok: true });
    case "load_wrong_next": loadWrongNext = true; return reply({ ok: true });
    case "reboot": // 模拟 Mixxx 重载映射
      timers.clear();
      DJ.init("sim", false);
      return reply({ ok: true });
    case "state":
      return reply({
        t: vnow, autodj, queue: autodjQueue,
        decks: [1, 2].map((n) => {
          const d = decks[n];
          return { n, id: d.track?.id ?? null, playing: d.playing, pos: d.pos, rate: d.rate,
                   bpm: d.track ? d.track.bpm * d.rate : 0, beat: d.track ? beatPos(d) : null,
                   volume: getValue(`[Channel${n}]`, "volume") };
        }),
      });
    case "history": {
      const keys = new Set(c.keys || []);
      return reply({ h: history.filter((h) => !keys.size || keys.has(h[2]) || keys.has(`${h[1]},${h[2]}`)) });
    }
    case "get":
      return reply({ v: getValue(c.group, c.key) });
    case "set":
      setValue(c.group, c.key, c.value);
      return reply({ ok: true });
    default:
      return reply({ ok: false, err: "unknown sim command " + c.cmd });
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  line = line.trim();
  if (!line) return;
  if (line.startsWith("#")) { simCommand(JSON.parse(line.slice(1))); return; }
  incoming([...Buffer.from(line, "hex")]);
});
rl.on("close", () => process.exit(0));

// ------------------------------------------------------------------ 启动

DJ.init("sim", false);
let lastWall = Date.now();
setInterval(() => {
  const wall = Date.now();
  let budget = (wall - lastWall) * SPEED;
  lastWall = wall;
  while (budget > 0) {
    const dt = Math.min(STEP_MS, budget);
    budget -= dt;
    vnow += dt;
    advanceAudio(dt);
    runAutoDJ();
    runTimers();
  }
}, 5);
