// 假 Mixxx：在 Node 里加载真正的 DJStudio.js 映射，按 Mixxx 2.5.6 源码的行为模拟控件、deck、AutoDJ、MIDI。
// 用来在没有 Mac / Mixxx 的环境里端到端测试引擎脚本与 Python conductor。
//
// 用法：node sim/fake_mixxx.mjs --mapping path/DJStudio.js [--speed 20]
// stdin / stdout 每行一条：十六进制 = 原始 MIDI 消息；'#' 开头 = 模拟器控制命令 / 回复（JSON）。
// 虚拟时钟按 speed 倍速推进；脚本里的 Date.now() 与定时器都走虚拟时钟。
//
// 每条被模拟的行为都对应 Mixxx 2.5.6 的具体源码（行号见注释与 docs/MIXXX_NOTES.md）。
// 不模拟 keylock 时间拉伸的延迟（那是在真引擎测试台上实测出来的，见 tools/mixxx-harness）。
// 它仍然只是模型：真机验证见 `djstudio probe`。

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
const STEP_MS = 5; // 音频 buffer 长度（虚拟毫秒）
const LOAD_LATENCY_MS = 250;
const PLAYPOS_UPDATE_MS = 1000 / 15; // enginebuffer.cpp:53 kPlaypositionUpdateRate = 15
const UNREPLACE_MS = 500; // basetrackplayer.h:24 kUnreplaceDelay
const PAST_BEAT_THRESHOLD = 1 / 8; // bpmcontrol.cpp:41 kPastBeatMatchThreshold
const SR = 44100;

let vnow = 0;
const epoch = 1_700_000_000_000;
let frozen = false; // 故障注入：引擎卡死（不跑定时器、不收不发）
let dropOut = 0;
const history = []; // [t, group, key, value]（脚本写入的值）
const library = new Map(); // id -> {id, dur, bpm, first_beat}
const log = (...a) => process.stderr.write(a.join(" ") + "\n");

// ------------------------------------------------------------------ 控件与连接

const store = new Map(); // "group,key" -> value
const conns = new Map(); // "group,key" -> [cb]
const pendingCallbacks = []; // 连接回调排队到控制器线程（这里：下一个 step）
const defaults = {
  volume: 1, parameter1: 1, parameter2: 1, parameter3: 1, super1: 0.5, beatloop_size: 4,
};

function emit(group, key, value) {
  const cbs = conns.get(`${group},${key}`);
  if (cbs) for (const cb of cbs) pendingCallbacks.push(() => cb(value, group, key));
}

function storeSet(group, key, value) {
  const k = `${group},${key}`;
  store.set(k, value);
  emit(group, key, value);
}

function storeGet(group, key) {
  const k = `${group},${key}`;
  return store.has(k) ? store.get(k) : (defaults[key] ?? 0);
}

// ------------------------------------------------------------------ deck

function makeDeck(n) {
  return {
    n, track: null, playing: false, pos: 0, rate: 1, loop: null,
    pp: 0, ppAcc: 0, // 已发布的 playposition 与距上次发布的累计时间
    phaseSyncPending: false, ejectT: -1e9, loadingUntil: 0, pendingLoad: null,
  };
}
const decks = { 1: makeDeck(1), 2: makeDeck(2) };
storeSet("[Channel1]", "orientation", 1); // 默认居中（EngineChannel 构造时的默认值由皮肤 / 配置决定）
storeSet("[Channel2]", "orientation", 1);
storeSet("[Master]", "crossfader", 0);
storeSet("[Mixer Profile]", "xFaderMode", 1); // 假定用户原来是恒功率，验证脚本会改成加性并在退出时恢复

const beatLen = (t) => 60 / t.bpm;
const beatPos = (d) => (d.pos - d.track.first_beat) / beatLen(d.track);
const otherDeck = (d) => decks[d.n === 1 ? 2 : 1];
const deckOf = (group) => { const m = /^\[Channel(\d)\]$/.exec(group); return m ? decks[m[1]] : null; };

function publishPlaypos(d) {
  d.pp = d.track ? Math.min(1, d.pos / d.track.dur) : 0;
  d.ppAcc = 0;
  emit(`[Channel${d.n}]`, "playposition", d.pp);
  autodjPositionChanged(d);
}

function getValue(group, key) {
  const d = deckOf(group);
  if (d) {
    const t = d.track;
    switch (key) {
      case "play": return d.playing ? 1 : 0;
      case "track_loaded": return t ? 1 : 0;
      case "duration": return t ? t.dur : 0; // basetrackplayer.cpp:601 元数据时长
      case "track_samples": return t ? Math.round(t.dur * SR * 2) : 0;
      case "track_samplerate": return t ? SR : 0;
      case "playposition": return d.pp; // 每秒只更新 15 次
      case "file_bpm": return t ? t.bpm : 0;
      case "bpm": return t ? t.bpm * d.rate : 0;
      case "beat_distance": { if (!t) return 0; const b = beatPos(d); return b - Math.floor(b); } // 每个 buffer 更新
      case "loop_enabled": return d.loop ? 1 : 0;
      case "vu_meter": return simLevel(d);
    }
  }
  if ((group === "[Master]" || group === "[Main]") && key === "vu_meter") {
    const e = [1, 2].reduce((a, n) => a + storeGet(`[Channel${n}]`, "volume") * simEnergy(decks[n]), 0);
    return Math.min(1, 0.85 * Math.sqrt(e));
  }
  return storeGet(group, key);
}

// 电平表：模拟器没有音频，按 EQ / 滤波 / 拍内位置（kick）合成一个看起来合理的值。
// 真 Mixxx 的 [ChannelN],vu_meter 在推子前、EQ 与滤波之后（enginedeck.cpp:69-81）；这里同样不乘推子。
function simEnergy(d) {
  if (!d.track || !d.playing) return 0;
  const eq = `[EqualizerRack1_[Channel${d.n}]_Effect1]`;
  let lo = 0.5 * Math.min(4, storeGet(eq, "parameter1"));
  let mid = 0.3 * Math.min(4, storeGet(eq, "parameter2"));
  let hi = 0.2 * Math.min(4, storeGet(eq, "parameter3"));
  const f = storeGet(`[QuickEffectRack1_[Channel${d.n}]]`, "super1");
  const k = Math.min(1, Math.abs(f - 0.5) * 2);
  if (f < 0.5) { hi *= 1 - k; mid *= 1 - 0.7 * k; } else { lo *= 1 - k; mid *= 1 - 0.7 * k; }
  const b = beatPos(d), frac = b - Math.floor(b);
  return (lo * (0.35 + 0.65 * Math.exp(-frac * 7)) + mid + hi);
}
function simLevel(d) { return Math.min(1, 0.85 * Math.sqrt(simEnergy(d))); }

// EngineBuffer::processSeek + BpmControl::getBeatMatchPosition：把 d 的相位对齐到另一个在播 deck，就近 ±0.5 拍
function applyPhaseSync(d) {
  d.phaseSyncPending = false;
  const o = otherDeck(d);
  if (!d.track || !o.playing || !o.track) return;
  let fo = beatPos(o) - Math.floor(beatPos(o));
  if (fo > 1 - PAST_BEAT_THRESHOLD) fo -= 1;
  const b = beatPos(d);
  let target = Math.floor(b) + fo;
  if (target - b > 0.5) target -= 1;
  if (b - target > 0.5) target += 1;
  d.pos = d.track.first_beat + target * beatLen(d.track);
}

function setValue(group, key, value) {
  if (frozen) return;
  history.push([Math.round(vnow), group, key, value]);
  const d = deckOf(group);
  if (d) {
    const t = d.track;
    switch (key) {
      case "play":
        // enginebuffer.cpp:776 slotControlPlayRequest：quantize 开着时起播请求相位同步
        if (value && t && !d.playing && d.pos < t.dur) {
          d.playing = true;
          if (getValue(group, "quantize")) d.phaseSyncPending = true;
        } else if (!value) {
          d.playing = false;
        }
        emit(group, "play", d.playing ? 1 : 0);
        return;
      case "playposition": // slotControlSeek → doSeekFractional；暂停时不做相位调整
        if (t) {
          d.pos = Math.max(0, Math.min(t.dur, value * t.dur));
          if (d.playing && getValue(group, "quantize")) d.phaseSyncPending = true;
          publishPlaypos(d);
        }
        return;
      case "bpm": // bpmcontrol.cpp:1105 rate_ratio = bpm / local_bpm，不受 rateRange 限制
        if (t && value > 0) d.rate = value / t.bpm;
        return;
      case "eject": // basetrackplayer.cpp:373 slotEjectTrack
        if (!value || d.playing) return;
        {
          const dbl = vnow - d.ejectT < UNREPLACE_MS;
          d.ejectT = vnow;
          if (dbl) { if (secondLastEjected) scheduleLoad(d, secondLastEjected, false); return; }
          if (!t) { if (lastEjected) scheduleLoad(d, lastEjected, false); return; }
          noteEjected(t.id);
          unloadTrack(d);
        }
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
        if (value && d.playing) d.phaseSyncPending = true;
        return;
    }
  }
  if (group === "[AutoDJ]" && key === "enabled") {
    // 需要确认的控件：值只在 toggleAutoDJ 成功时才变（主线程，异步）
    autodjRequests.push(() => toggleAutoDJ(!!value));
    return;
  }
  if (group === "[AutoDJ]" && key === "skip_next") {
    if (value) autodjRequests.push(() => skipNext());
    return;
  }
  storeSet(group, key, value);
}

// ------------------------------------------------------------------ 装载 / 弹出

let lastEjected = null;
let secondLastEjected = null;
function noteEjected(id) {
  if (id === lastEjected) return;
  secondLastEjected = lastEjected;
  lastEjected = id;
}

function unloadTrack(d) {
  d.track = null; d.pos = 0; d.loop = null; d.playing = false; d.rate = 1;
  emit(`[Channel${d.n}]`, "track_loaded", 0);
  publishPlaypos(d);
}

function scheduleLoad(d, id, play) {
  // 装新歌先卸旧歌（playerLoadingTrack → 引擎 ejectTrack）
  if (d.track) { noteEjected(d.track.id); unloadTrack(d); }
  d.pendingLoad = { id, play, at: vnow + LOAD_LATENCY_MS };
}

function runLoads() {
  for (const d of [decks[1], decks[2]]) {
    const L = d.pendingLoad;
    if (!L || L.at > vnow) continue;
    d.pendingLoad = null;
    const t = library.get(L.id);
    if (!t) continue;
    d.track = { ...t }; d.pos = 0; d.rate = 1; d.loop = null; d.playing = false;
    loadLog.push([Math.round(vnow), d.n, t.id]);
    emit(`[Channel${d.n}]`, "track_loaded", 1);
    publishPlaypos(d);
    autodjTrackLoaded(d);
    if (L.play) { d.playing = true; emit(`[Channel${d.n}]`, "play", 1); }
  }
}

// ------------------------------------------------------------------ AutoDJ（autodjprocessor.cpp 移植）

let autodjQueue = []; // track id 列表（PlaylistTracks 按 position）
const loadLog = []; // [t, deck, id]：每次真正装上一首（测试用）
let adjState = "DISABLED"; // DISABLED | P1LOADED | IDLE
const autodjRequests = [];

const leftDeck = () => [decks[1], decks[2]].find((d) => getValue(`[Channel${d.n}]`, "orientation") === 0) || null;
const rightDeck = () => [decks[1], decks[2]].find((d) => getValue(`[Channel${d.n}]`, "orientation") === 2) || null;

function removeLoadedTrackFromTopOfQueue(d) { // :933
  if (!d.track || !autodjQueue.length || autodjQueue[0] !== d.track.id) return false;
  autodjQueue.shift();
  return true;
}

function loadNextTrackFromQueue(d, play) { // :916
  const next = autodjQueue[0];
  if (next === undefined) { toggleAutoDJ(false); if (!d.playing && d.track) { noteEjected(d.track.id); unloadTrack(d); } return false; }
  scheduleLoad(d, next, play);
  return true;
}

function toggleAutoDJ(enable) { // :358
  if (!enable) { adjState = "DISABLED"; storeSet("[AutoDJ]", "enabled", 0); return; }
  const L = leftDeck(), R = rightDeck();
  if (!L || !R) { log("[autodj] ADJ_NOT_TWO_DECKS"); return; }
  let lp = L.playing, rp = R.playing;
  if (lp && rp) { log("[autodj] ADJ_BOTH_DECKS_PLAYING"); return; }
  if (lp) removeLoadedTrackFromTopOfQueue(L);
  else if (rp) removeLoadedTrackFromTopOfQueue(R);
  else if (L.pp < 0.66 && removeLoadedTrackFromTopOfQueue(L)) { L.playing = true; lp = true; }
  else if (R.pp < 0.66 && removeLoadedTrackFromTopOfQueue(R)) { R.playing = true; rp = true; }
  const next = autodjQueue[0];
  if (next === undefined) { log("[autodj] ADJ_QUEUE_EMPTY"); storeSet("[AutoDJ]", "enabled", 0); return; }
  storeSet("[AutoDJ]", "enabled", 1);
  if (!lp && !rp) {
    adjState = "P1LOADED";
    storeSet("[Master]", "crossfader", -1);
    scheduleLoad(L, next, true);
  } else {
    adjState = "IDLE";
    if (lp) { scheduleLoad(R, next, false); storeSet("[Master]", "crossfader", -1); }
    else { scheduleLoad(L, next, false); storeSet("[Master]", "crossfader", 1); }
  }
}

function autodjPositionChanged(d) { // :661，只模拟 P1LOADED → IDLE（我们从不让它走到自动淡化）
  if (adjState !== "P1LOADED") return;
  const L = leftDeck(), R = rightDeck();
  if (!L || !R) return;
  if (L.playing || R.playing) {
    adjState = "IDLE";
    if (!R.playing) { removeLoadedTrackFromTopOfQueue(L); loadNextTrackFromQueue(R, false); }
  }
}

function autodjTrackLoaded(d) { /* :1558 IDLE 时计算过渡并可能 seek；不影响我们的流程 */ }

function skipNext() { // :321
  if (adjState === "DISABLED") return;
  const L = leftDeck(), R = rightDeck();
  if (!L || !R) { toggleAutoDJ(false); return; }
  if (!L.playing) { removeLoadedTrackFromTopOfQueue(L); loadNextTrackFromQueue(L, false); }
  else if (!R.playing) { removeLoadedTrackFromTopOfQueue(R); loadNextTrackFromQueue(R, false); }
}

// ------------------------------------------------------------------ 音频推进

function advanceAudio(dtMs) {
  for (const d of [decks[1], decks[2]]) {
    if (d.phaseSyncPending && d.playing) applyPhaseSync(d);
    if (!d.playing || !d.track) continue;
    d.pos += (dtMs / 1000) * d.rate;
    if (d.loop && d.pos >= d.loop.start + d.loop.len) d.pos -= d.loop.len;
    if (d.pos >= d.track.dur) { d.pos = d.track.dur; d.playing = false; emit(`[Channel${d.n}]`, "play", 0); publishPlaypos(d); }
    d.ppAcc += dtMs;
    if (d.ppAcc >= PLAYPOS_UPDATE_MS) publishPlaypos(d);
  }
}

// ------------------------------------------------------------------ 定时器

let timerSeq = 0;
const timers = new Map();
function beginTimer(ms, fn, once) {
  if (typeof fn !== "function") throw new Error("beginTimer needs a function");
  const iv = Math.max(20, ms); // controllerscriptinterfacelegacy.cpp:507 最短 20ms
  const id = ++timerSeq;
  timers.set(id, { next: vnow + iv, ms: iv, fn, once: !!once });
  return id;
}
function stopTimer(id) { timers.delete(id); }
function runTimers() {
  if (frozen) return;
  for (const [id, t] of [...timers]) {
    if (t.next <= vnow) {
      if (t.once) timers.delete(id); else t.next += t.ms;
      try { t.fn(); } catch (e) { log("timer error:", e.stack || e); }
    }
  }
}

// ------------------------------------------------------------------ MIDI（含 IAC 回环）

const loopback = []; // IAC 会把 Mixxx 自己发出的消息再送回 Mixxx 的输入
function out(bytes) {
  if (frozen) return;
  if (dropOut > 0) { dropOut--; return; }
  process.stdout.write(Buffer.from(bytes).toString("hex") + "\n");
  loopback.push(bytes);
}

const midi = {
  sendShortMsg: (s, a, b) => out([s, a, b]),
  // midicontroller.h:69 sendSysexMsg(data, length) — length 被忽略
  sendSysexMsg: (data) => out(Array.from(data)),
};

// XML 输入映射（legacymidicontrollermappingfilehandler.cpp）
const bindings = [];
if (fs.existsSync(XML)) {
  const xml = fs.readFileSync(XML, "utf8").replace(/<!--[\s\S]*?-->/g, "");
  for (const m of xml.matchAll(/<control>([\s\S]*?)<\/control>/g)) {
    const body = m[1];
    const get = (tag) => (new RegExp(`<${tag}>([^<]*)</${tag}>`).exec(body) || [])[1];
    if (!/<script-binding\s*\/>/i.test(body)) continue;
    const status = parseInt(get("status")) || 0;
    let midino = parseInt(get("midino"));
    if (isNaN(midino)) midino = 0;
    // midimessage.cpp:17 MidiKey：非两字节消息（含 0xF0）的 control 一律记成 0xFF
    const opcode = status & 0xf0;
    const twoByte = opcode >= 0x80 && opcode <= 0xe0;
    bindings.push({ status, midino: twoByte ? midino : 0xff, key: get("key"), group: get("group") });
  }
}

const ctx = vm.createContext({
  engine: {
    getValue, setValue,
    getParameter: getValue, setParameter: setValue,
    beginTimer, stopTimer,
    makeConnection: (group, key, cb) => {
      const k = `${group},${key}`;
      if (!conns.has(k)) conns.set(k, []);
      conns.get(k).push(cb);
      return { disconnect() { const a = conns.get(k); a.splice(a.indexOf(cb), 1); }, trigger() { cb(getValue(group, key), group, key); } };
    },
  },
  midi,
  print: (s) => log("[script]", s),
  console: { log: (...a) => log("[script]", ...a) },
  Date: { now: () => epoch + vnow },
  Math, JSON, String, Array, Object, parseFloat, parseInt, Uint8Array, ArrayBuffer,
});
vm.runInContext(fs.readFileSync(MAPPING, "utf8") + "\n;globalThis.__DJ = DJStudio;", ctx, { filename: path.basename(MAPPING) });
const DJ = ctx.__DJ;
const resolveFn = (key) => key.split(".").reduce((o, k) => (o ? o[k] : undefined), { DJStudio: DJ });

function incoming(bytes) {
  if (frozen) return;
  if (bytes[0] === 0xf0) {
    // midicontroller.cpp:577 receive(QByteArray)：只有存在 status=0xF0 的 script-binding 时才交给 incomingData
    if (bindings.some((b) => b.status === 0xf0)) DJ.incomingData(Uint8Array.from(bytes), bytes.length);
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
    case "load":
      scheduleLoad(decks[c.deck], c.id, false);
      return reply({ ok: true });
    case "freeze": frozen = true; return reply({ ok: true });
    case "unfreeze": frozen = false; return reply({ ok: true });
    case "drop_out": dropOut = c.n; return reply({ ok: true });
    case "reboot":
      timers.clear();
      conns.clear();
      DJ.init("sim", false);
      return reply({ ok: true });
    case "state":
      return reply({
        t: vnow, autodj: adjState, queue: autodjQueue, loads: loadLog, crossfader: storeGet("[Master]", "crossfader"),
        xFaderMode: storeGet("[Mixer Profile]", "xFaderMode"),
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
  const bytes = [...Buffer.from(line, "hex")];
  process.stdout.write(line + "\n"); // IAC 回环：发送者自己也会收到
  incoming(bytes);
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
    while (autodjRequests.length) autodjRequests.shift()();
    advanceAudio(dt);
    runLoads();
    while (pendingCallbacks.length && !frozen) {
      try { pendingCallbacks.shift()(); } catch (e) { log("callback error:", e.stack || e); }
    }
    while (loopback.length) incoming(loopback.shift());
    runTimers();
  }
}, 5);
