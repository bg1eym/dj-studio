// DJ Studio 引擎脚本：作为 Mixxx 控制器映射运行在 Mixxx 里（IAC 虚拟 MIDI 总线）。
//
// 职责：确定性地执行 Python 下发的「过渡程序」（以 A 曲网格的拍为时间线的自动化曲线 + 事件），
// 装载曲目（AutoDJ 只当装载器），并每 100ms 回传状态。不做任何决策——决策全在 Python。
//
// 通信：JSON 文本，按 ≤200 字节分片，装进 SysEx：F0 7D 44 <dir> <seq> <part> <nparts> <payload> F7，
// dir = 0x53 'S'（Python→引擎）/ 0x45 'E'（引擎→Python）。IAC 总线会把消息回送给所有监听者（包括发送者），
// 所以双方都只认对方方向的帧。
// 备用传输（SysEx 不通时）：同样的分片放进 CC 流（CC2 开始 / CC1 数据 / CC3 结束），
// Python→引擎走 ch16（0xBF），引擎→Python 走 ch15（0xBE）。
// 协议详见 docs/ENGINE.md；Python 侧见 djstudio/engine/protocol.py。改协议必须两边同步。
//
// 只用 ES5 语法，兼容各版本 Mixxx 的脚本引擎。
//
// 依据 Mixxx 2.5.6 源码核对过的行为（详见 docs/MIXXX_NOTES.md）：
// - playposition 每秒只更新 15 次（enginebuffer.cpp kPlaypositionUpdateRate），beat_distance 每个音频
//   buffer 都更新（EngineBuffer::postProcess）→ 拍位 = playposition 估出的整数拍 + beat_distance 小数部分。
// - quantize 开着时按 play，引擎会把这一 deck 的相位对齐到另一个正在播的 deck（requestSyncPhase →
//   getBeatMatchPosition，就近 ±0.5 拍）。所以 B 停在 b_in 的整拍上，到点只按 play，相位由 Mixxx 采样级对齐。
// - 暂停时 seek 不做相位调整（processSeek: !paused && SEEK_PHASE），cue 到的就是精确位置。
// - 空 deck 上 eject 会重新装回上一首被弹出的歌（basetrackplayer.cpp slotEjectTrack）→ 只对已装载的 deck eject。
// - keylock 开着时时间拉伸有未补偿的延迟（真引擎测试台实测，见 docs/MIXXX_NOTES.md）→ 默认关。

var DJStudio = {};

DJStudio.VERSION = "0.4.1";
DJStudio.TICK_MS = 20;
DJStudio.STATUS_EVERY = 5; // 5 × 20ms = 100ms
DJStudio.CHUNK = 200;
DJStudio.LOAD_TIMEOUT_MS = 15000;
DJStudio.PHASE_TOL = 0.03; // 拍；超过就用 beatsync_phase 校正
DJStudio.PHASE_CHECK_MS = 2000;
DJStudio.LATE_TOL = 1.0; // 拍；程序到达时时间线已过 0 点超过这么多 → 拒绝（late）
DJStudio.AUTODJ_ACCEPT_MS = 1500; // 开 AutoDJ 后这么久还没生效 = 被拒（队列空 / 两边都在播 / 朝向不对）

DJStudio.FX_UNIT = { echo: 1, reverb: 2 };

// ------------------------------------------------------------------ 状态

DJStudio.reset = function () {
    DJStudio.rx = {};
    DJStudio.txSeq = 0;
    DJStudio.mode = "sysex";
    DJStudio.ccBuf = null;
    DJStudio.tickCount = 0;
    DJStudio.t0 = Date.now();
    DJStudio.prog = null;
    DJStudio.loader = null;
    DJStudio.timer = 0;
    DJStudio.lastErr = "";
    DJStudio.xfMode = null;
    DJStudio.loadSeq = [0, 0, 0]; // 每个 deck 装上新歌的次数（下标 1、2）
};
DJStudio.reset();

DJStudio.ch = function (d) { return "[Channel" + d + "]"; };
DJStudio.eqGroup = function (d) { return "[EqualizerRack1_[Channel" + d + "]_Effect1]"; };
DJStudio.qfxGroup = function (d) { return "[QuickEffectRack1_[Channel" + d + "]]"; };
DJStudio.fxGroup = function (u) { return "[EffectRack1_EffectUnit" + u + "]"; };

DJStudio.get = function (g, k) { return engine.getValue(g, k); };
DJStudio.set = function (g, k, v) { engine.setValue(g, k, v); };

DJStudio.now = function () { return Date.now() - DJStudio.t0; };

// ------------------------------------------------------------------ 生命周期

DJStudio.init = function (id, debugging) {
    DJStudio.reset();
    for (var d = 1; d <= 2; d++) {
        DJStudio.set(DJStudio.ch(d), "quantize", 1);
        // keylock 默认关：在真 Mixxx 2.5.6 上实测，keylock 开着且 deck 不在原速时，时间拉伸器（SoundTouch /
        // RubberBand 都一样）让声音比报告的位置晚 17–29ms，两个 kick 叠出明显的「双击」。set 可以显式打开。
        DJStudio.set(DJStudio.ch(d), "keylock", 0);
    }
    // 两 deck 分在推子两侧（AutoDJ 的前提），推子归中；混音只用通道音量。
    DJStudio.set("[Channel1]", "orientation", 0);
    DJStudio.set("[Channel2]", "orientation", 2);
    DJStudio.set("[Master]", "crossfader", 0);
    // 推子用「加性」模式：居中时两边增益都是 1，AutoDJ 装载时把推子甩到一边也不改变在播 deck 的音量
    // （enginexfader.cpp getXfadeGains；恒功率模式居中时两边都 < 1）。退出时恢复用户设置。
    DJStudio.xfMode = DJStudio.get("[Mixer Profile]", "xFaderMode");
    DJStudio.set("[Mixer Profile]", "xFaderMode", 0);
    for (var u = 1; u <= 2; u++) {
        DJStudio.set(DJStudio.fxGroup(u), "mix", 0);
        DJStudio.set(DJStudio.fxGroup(u), "group_[Channel1]_enable", 0);
        DJStudio.set(DJStudio.fxGroup(u), "group_[Channel2]_enable", 0);
    }
    // 装载计数：track_loaded 变成 1 就加一（用来区分「同一首」和「装了新的一首」）
    engine.makeConnection("[Channel1]", "track_loaded", function (v) { if (v > 0) { DJStudio.loadSeq[1]++; } });
    engine.makeConnection("[Channel2]", "track_loaded", function (v) { if (v > 0) { DJStudio.loadSeq[2]++; } });
    DJStudio.timer = engine.beginTimer(DJStudio.TICK_MS, function () { DJStudio.tick(); });
    DJStudio.send({ t: "hello", v: DJStudio.VERSION, boot: 1 });
};

DJStudio.shutdown = function () {
    if (DJStudio.timer) { engine.stopTimer(DJStudio.timer); DJStudio.timer = 0; }
    if (DJStudio.xfMode !== null) { DJStudio.set("[Mixer Profile]", "xFaderMode", DJStudio.xfMode); }
};

// ------------------------------------------------------------------ 传输层

DJStudio.incomingData = function (data, length) {
    var bytes = data;
    if (typeof ArrayBuffer !== "undefined" && data instanceof ArrayBuffer) { bytes = new Uint8Array(data); }
    var n = (length === undefined) ? bytes.length : length;
    if (n < 8) { return; }
    if ((bytes[0] & 0xFF) !== 0xF0 || bytes[1] !== 0x7D || bytes[2] !== 0x44 || bytes[3] !== 0x53) { return; }
    var p = [];
    for (var i = 4; i < n - 1; i++) { p.push(bytes[i] & 0x7F); }
    DJStudio.mode = "sysex";
    DJStudio.onPacket(p);
};

// 备用 CC 传输（ch16）：CC2 = 包开始，CC1 = 一个数据字节，CC3 = 包结束
DJStudio.ccStart = function (channel, control, value) { DJStudio.ccBuf = []; };
DJStudio.ccByte = function (channel, control, value) { if (DJStudio.ccBuf) { DJStudio.ccBuf.push(value & 0x7F); } };
DJStudio.ccEnd = function (channel, control, value) {
    var p = DJStudio.ccBuf;
    DJStudio.ccBuf = null;
    if (p && p.length >= 3) { DJStudio.mode = "cc"; DJStudio.onPacket(p); }
};

DJStudio.onPacket = function (p) {
    var seq = p[0], part = p[1], n = p[2];
    var buf = DJStudio.rx[seq];
    if (!buf || buf.n !== n || part === 0) { buf = DJStudio.rx[seq] = { n: n, parts: [], got: 0 }; }
    if (buf.parts[part] === undefined) { buf.got++; }
    buf.parts[part] = p.slice(3);
    if (buf.got < n) { return; }
    delete DJStudio.rx[seq];
    var s = "";
    for (var i = 0; i < n; i++) {
        var c = buf.parts[i];
        for (var j = 0; j < c.length; j++) { s += String.fromCharCode(c[j]); }
    }
    var msg;
    try { msg = JSON.parse(s); } catch (e) { DJStudio.error("bad json: " + e); return; }
    try { DJStudio.handle(msg); } catch (e2) { DJStudio.error("handler " + msg.t + ": " + e2); }
};

DJStudio.ascii = function (s) {
    return s.replace(/[\u0080-￿]/g, function (c) {
        return "\\u" + ("0000" + c.charCodeAt(0).toString(16)).slice(-4);
    });
};

DJStudio.send = function (obj) {
    var s = DJStudio.ascii(JSON.stringify(obj));
    var n = Math.max(1, Math.ceil(s.length / DJStudio.CHUNK));
    var seq = DJStudio.txSeq;
    DJStudio.txSeq = (DJStudio.txSeq + 1) & 0x7F;
    for (var part = 0; part < n; part++) {
        var chunk = s.substr(part * DJStudio.CHUNK, DJStudio.CHUNK);
        var body = [seq, part, n];
        for (var i = 0; i < chunk.length; i++) { body.push(chunk.charCodeAt(i) & 0x7F); }
        if (DJStudio.mode === "cc") {
            midi.sendShortMsg(0xBE, 2, 0);
            for (var k = 0; k < body.length; k++) { midi.sendShortMsg(0xBE, 1, body[k]); }
            midi.sendShortMsg(0xBE, 3, 0);
        } else {
            var msg = [0xF0, 0x7D, 0x44, 0x45].concat(body);
            msg.push(0xF7);
            midi.sendSysexMsg(msg, msg.length);
        }
    }
};

DJStudio.error = function (m) {
    DJStudio.lastErr = m;
    DJStudio.send({ t: "err", m: m });
};

// ------------------------------------------------------------------ deck 读数

DJStudio.durMs = function (d) {
    var g = DJStudio.ch(d);
    var dur = DJStudio.get(g, "duration");
    if (!(dur > 0)) {
        var s = DJStudio.get(g, "track_samples"), r = DJStudio.get(g, "track_samplerate");
        dur = r > 0 ? s / 2 / r : 0;
    }
    return dur * 1000;
};

DJStudio.posMs = function (d) { return DJStudio.get(DJStudio.ch(d), "playposition") * DJStudio.durMs(d); };
DJStudio.playing = function (d) { return DJStudio.get(DJStudio.ch(d), "play") > 0; };
DJStudio.loaded = function (d) { return DJStudio.get(DJStudio.ch(d), "track_loaded") > 0; };

// grid = [t0 毫秒, bpm]
DJStudio.beatAt = function (grid, ms) { return (ms - grid[0]) * grid[1] / 60000; };
DJStudio.msAt = function (grid, beat) { return grid[0] + beat * 60000 / grid[1]; };

// deck 当前所在的网格拍号（小数）。playposition 最多滞后约 67ms，只用来定整数拍；
// 小数部分取 Mixxx 每个音频 buffer 都更新的 beat_distance（与我们解码的是同一份网格）。
DJStudio.beatNow = function (d, grid) {
    var est = DJStudio.beatAt(grid, DJStudio.posMs(d));
    if (!DJStudio.playing(d)) { return est; }
    var frac = DJStudio.get(DJStudio.ch(d), "beat_distance");
    if (!(frac >= 0 && frac < 1)) { return est; }
    var b = Math.round(est - frac) + frac;
    return Math.abs(b - est) <= 0.5 ? b : est;
};

DJStudio.seekMs = function (d, ms) {
    var dur = DJStudio.durMs(d);
    if (dur > 0) { DJStudio.set(DJStudio.ch(d), "playposition", ms / dur); }
};

DJStudio.deckStatus = function (d) {
    var g = DJStudio.ch(d);
    return [
        DJStudio.loaded(d) ? 1 : 0,
        DJStudio.playing(d) ? 1 : 0,
        Math.round(DJStudio.posMs(d)),
        Math.round(DJStudio.durMs(d)),
        Math.round(DJStudio.get(g, "bpm") * 100) / 100,
        Math.round(DJStudio.get(g, "file_bpm") * 100) / 100,
        Math.round(DJStudio.get(g, "volume") * 1000) / 1000,
        DJStudio.loadSeq[d]
    ];
};

// 混音台读数（网页上画旋钮用）：[hi, mid, lo, filter, echo, reverb, vu]，都是 Mixxx 控件的原生值。
// EQ 0..4（1 = 0dB），filter 0..1（0.5 = 关），echo / reverb = 效果单元对这个通道开着时的 mix，否则 0；
// vu = [ChannelN],vu_meter（推子前、EQ / 滤波之后，enginedeck.cpp:69-81，0..1）
DJStudio.r3 = function (v) { return Math.round(v * 1000) / 1000; };
DJStudio.fxSend = function (d, name) {
    var g = DJStudio.fxGroup(DJStudio.FX_UNIT[name]);
    return DJStudio.get(g, "enabled") > 0 && DJStudio.get(g, "group_" + DJStudio.ch(d) + "_enable") > 0
        ? DJStudio.r3(DJStudio.get(g, "mix")) : 0;
};
DJStudio.mixerStatus = function (d) {
    var eq = DJStudio.eqGroup(d);
    return [
        DJStudio.r3(DJStudio.get(eq, "parameter3")),
        DJStudio.r3(DJStudio.get(eq, "parameter2")),
        DJStudio.r3(DJStudio.get(eq, "parameter1")),
        DJStudio.r3(DJStudio.get(DJStudio.qfxGroup(d), "super1")),
        DJStudio.fxSend(d, "echo"),
        DJStudio.fxSend(d, "reverb"),
        DJStudio.r3(DJStudio.get(DJStudio.ch(d), "vu_meter"))
    ];
};

// ------------------------------------------------------------------ 控制映射

// 通道名 "A.vol" → 对某 deck 设值（v 已是 Mixxx 原生值）
DJStudio.applyParam = function (d, param, v) {
    var g;
    if (param === "vol") { DJStudio.setIf(DJStudio.ch(d), "volume", v); }
    else if (param === "lo") { DJStudio.setIf(DJStudio.eqGroup(d), "parameter1", v); }
    else if (param === "mid") { DJStudio.setIf(DJStudio.eqGroup(d), "parameter2", v); }
    else if (param === "hi") { DJStudio.setIf(DJStudio.eqGroup(d), "parameter3", v); }
    else if (param === "filter") { DJStudio.setIf(DJStudio.qfxGroup(d), "super1", v); }
    else if (param === "echo" || param === "reverb") {
        g = DJStudio.fxGroup(DJStudio.FX_UNIT[param]);
        DJStudio.setIf(g, "group_" + DJStudio.ch(d) + "_enable", v > 0.001 ? 1 : 0);
        DJStudio.setIf(g, "enabled", 1);
        DJStudio.setIf(g, "mix", v);
    }
};

DJStudio.setIf = function (g, k, v) {
    if (Math.abs(DJStudio.get(g, k) - v) > 0.0005) { DJStudio.set(g, k, v); }
};

DJStudio.neutral = function (d) {
    DJStudio.set(DJStudio.eqGroup(d), "parameter1", 1);
    DJStudio.set(DJStudio.eqGroup(d), "parameter2", 1);
    DJStudio.set(DJStudio.eqGroup(d), "parameter3", 1);
    DJStudio.set(DJStudio.qfxGroup(d), "super1", 0.5);
    for (var u = 1; u <= 2; u++) { DJStudio.set(DJStudio.fxGroup(u), "group_" + DJStudio.ch(d) + "_enable", 0); }
    if (DJStudio.get(DJStudio.ch(d), "loop_enabled") > 0) { DJStudio.set(DJStudio.ch(d), "reloop_toggle", 1); }
};

// 分段线性插值；同一拍上的重复点表示阶跃（取后一个）
DJStudio.interp = function (pts, x) {
    var n = pts.length;
    if (n === 0) { return null; }
    if (x < pts[0][0]) { return pts[0][1]; }
    var i = n - 1;
    while (i > 0 && pts[i][0] > x) { i--; }
    if (i === n - 1) { return pts[i][1]; }
    var a = pts[i], b = pts[i + 1];
    return a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]);
};

// ------------------------------------------------------------------ 命令

DJStudio.handle = function (m) {
    var ok = true, err = "", extra = null;
    var t = m.t;
    if (t === "hello" || t === "ping") {
        extra = { v: DJStudio.VERSION, mode: DJStudio.mode };
    } else if (t === "program") {
        err = DJStudio.arm(m);
    } else if (t === "abort") {
        if (DJStudio.prog && (DJStudio.prog.st === "armed" || DJStudio.prog.st === "running")) {
            DJStudio.prog.st = "aborted";
        }
    } else if (t === "load") {
        err = DJStudio.startLoad(m.deck);
    } else if (t === "cue") {
        DJStudio.seekMs(m.deck, m.ms);
    } else if (t === "play") {
        DJStudio.set(DJStudio.ch(m.deck), "play", m.on ? 1 : 0);
    } else if (t === "bpm") {
        DJStudio.set(DJStudio.ch(m.deck), "bpm", m.v);
    } else if (t === "vol") {
        DJStudio.set(DJStudio.ch(m.deck), "volume", m.v);
    } else if (t === "neutral") {
        DJStudio.neutral(m.deck);
    } else if (t === "param") {
        // 按通道名设一个旋钮 / 推子（和自动化曲线同一套映射）：停下后「继续」时先把 A 摆到时间线当前位置
        DJStudio.applyParam(m.deck, m.p, m.v);
    } else if (t === "eject") {
        // 空 deck 上 eject 会把上一首弹出的歌装回来，所以只弹已装载的
        if (DJStudio.playing(m.deck)) { err = "deck playing"; }
        else if (DJStudio.loaded(m.deck)) { DJStudio.set(DJStudio.ch(m.deck), "eject", 1); }
    } else if (t === "set") {
        DJStudio.set(m.g, m.k, m.v);
    } else if (t === "get") {
        extra = { vals: [] };
        for (var i = 0; i < m.keys.length; i++) {
            extra.vals.push(DJStudio.get(m.keys[i][0], m.keys[i][1]));
        }
    } else {
        err = "unknown command " + t;
    }
    if (err) { ok = false; }
    if (m.c !== undefined) {
        var r = { t: "ack", c: m.c, ok: ok };
        if (err) { r.err = err; }
        if (extra) { for (var key in extra) { r[key] = extra[key]; } }
        DJStudio.send(r);
    }
};

// ------------------------------------------------------------------ 过渡程序

DJStudio.arm = function (p) {
    var cur = DJStudio.prog;
    if (cur && cur.st === "running") { return "busy: " + cur.p.id + " running"; }
    var a = p.a.deck;
    if (!DJStudio.loaded(a)) { return "deck A not loaded"; }
    if (p.b && !DJStudio.loaded(p.b.deck)) { return "deck B not loaded"; }
    var tl = DJStudio.beatNow(a, p.a.grid) - p.a.start;
    // resume = 停下后「继续」：A 从过渡中途起播是有意的，不拒绝；B 在 startB 里按已经过去的拍数对位起播
    if (tl > DJStudio.LATE_TOL && !p.resume) { return "late: tl=" + tl.toFixed(2); }
    DJStudio.prog = {
        p: p, st: "armed", tl: tl, fired: {}, bStarted: false, aStopped: false,
        lastPhase: 0, doneAt: 0
    };
    if (p.b) {
        var b = p.b.deck;
        // 先停再 cue：暂停时 seek 不做相位调整，B 停在 b_in 的精确整拍上
        DJStudio.set(DJStudio.ch(b), "play", 0);
        DJStudio.set(DJStudio.ch(b), "quantize", 1);
        DJStudio.seekMs(b, DJStudio.msAt(p.b.grid, p.b["in"]));
        var vb = p.lanes["B.vol"] ? DJStudio.interp(p.lanes["B.vol"], -1) : 0;
        DJStudio.set(DJStudio.ch(b), "volume", vb);
        var bpm0 = p.bpm && p.bpm.length ? DJStudio.interp(p.bpm, p.play) : DJStudio.get(DJStudio.ch(a), "bpm");
        if (bpm0 > 0) { DJStudio.set(DJStudio.ch(b), "bpm", bpm0); }
    }
    return "";
};

DJStudio.progTl = function (pr) {
    var p = pr.p;
    var a = p.a.deck;
    if (!pr.aStopped && DJStudio.playing(a)) {
        return DJStudio.beatNow(a, p.a.grid) - p.a.start;
    }
    if (p.b && pr.bStarted) {
        return DJStudio.beatNow(p.b.deck, p.b.grid) - p.b["in"] + p.play;
    }
    return pr.tl; // 两边都没在走：时间线冻结
};

DJStudio.fire = function (pr, ev) {
    var p = pr.p;
    var parts = ev.split(".");
    var d = parts[0] === "A" ? p.a.deck : (p.b ? p.b.deck : 0);
    if (!d) { return; }
    var act = parts[1];
    var g = DJStudio.ch(d);
    if (act === "stop") {
        DJStudio.set(g, "play", 0);
        if (parts[0] === "A") { pr.aStopped = true; }
    } else if (act.indexOf("loop:") === 0) {
        DJStudio.set(g, "beatloop_size", parseFloat(act.substr(5)));
        DJStudio.set(g, "beatloop_activate", 1);
    } else if (act === "loop_off") {
        if (DJStudio.get(g, "loop_enabled") > 0) { DJStudio.set(g, "reloop_toggle", 1); }
    }
    DJStudio.send({ t: "ev", e: "fired", id: p.id, ev: ev, tl: Math.round(pr.tl * 1000) / 1000 });
};

DJStudio.startB = function (pr) {
    var p = pr.p;
    var b = p.b.deck;
    var over = pr.tl - p.play; // 已经超过起播点多少拍（tick 间隔，通常 < 0.1 拍）
    var bpmA = DJStudio.get(DJStudio.ch(p.a.deck), "bpm");
    var bpm = p.bpm && p.bpm.length ? DJStudio.interp(p.bpm, pr.tl) : bpmA;
    if (bpm > 0) { DJStudio.set(DJStudio.ch(b), "bpm", bpm); }
    // Mixxx 起播时就近对齐相位（±0.5 拍）。晚得多的话先把整数拍补上，否则会对到前一拍。
    if (over >= 0.4) { DJStudio.seekMs(b, DJStudio.msAt(p.b.grid, p.b["in"] + Math.round(over))); }
    DJStudio.set(DJStudio.ch(b), "play", 1);
    pr.bStarted = true;
    pr.lastPhase = DJStudio.now();
    DJStudio.send({ t: "ev", e: "b_play", id: p.id, over: Math.round(over * 1000) / 1000 });
};

DJStudio.phaseCheck = function (pr) {
    var p = pr.p;
    if (!pr.bStarted || pr.aStopped || !DJStudio.playing(p.a.deck) || !DJStudio.playing(p.b.deck)) { return; }
    var now = DJStudio.now();
    if (now - pr.lastPhase < DJStudio.PHASE_CHECK_MS) { return; }
    pr.lastPhase = now;
    var ta = DJStudio.beatNow(p.a.deck, p.a.grid) - p.a.start;
    var tb = DJStudio.beatNow(p.b.deck, p.b.grid) - p.b["in"] + p.play;
    var diff = tb - ta;
    var frac = diff - Math.round(diff);
    if (Math.abs(Math.round(diff)) >= 1) {
        DJStudio.send({ t: "ev", e: "bar_slip", id: p.id, diff: Math.round(diff * 1000) / 1000 });
    }
    if (Math.abs(frac) > DJStudio.PHASE_TOL) {
        DJStudio.set(DJStudio.ch(p.b.deck), "beatsync_phase", 1);
        DJStudio.send({ t: "ev", e: "phase_fix", id: p.id, diff: Math.round(frac * 1000) / 1000 });
    }
};

DJStudio.runProg = function () {
    var pr = DJStudio.prog;
    if (!pr || (pr.st !== "armed" && pr.st !== "running")) { return; }
    var p = pr.p;
    pr.tl = DJStudio.progTl(pr);
    if (pr.st === "armed") {
        if (pr.tl < 0) { return; }
        pr.st = "running";
        DJStudio.send({ t: "ev", e: "started", id: p.id, tl: Math.round(pr.tl * 1000) / 1000 });
    }
    var i, name;
    // 事件（按拍序）；B 起播先于同拍的其他事件
    if (p.b && !pr.bStarted && pr.tl >= p.play) { DJStudio.startB(pr); }
    for (i = 0; i < p.events.length; i++) {
        if (!pr.fired[i] && pr.tl >= p.events[i][0]) {
            pr.fired[i] = true;
            DJStudio.fire(pr, p.events[i][1]);
        }
    }
    // 自动化曲线
    for (name in p.lanes) {
        if (!p.lanes.hasOwnProperty(name)) { continue; }
        var parts = name.split(".");
        var d = parts[0] === "A" ? p.a.deck : (p.b ? p.b.deck : 0);
        if (!d) { continue; }
        if (parts[0] === "A" && pr.aStopped && parts[1] === "vol") { continue; }
        DJStudio.applyParam(d, parts[1], DJStudio.interp(p.lanes[name], pr.tl));
    }
    // 主控 BPM：两边一起走，保持同速
    if (p.bpm && p.bpm.length) {
        var v = DJStudio.interp(p.bpm, pr.tl);
        if (!pr.aStopped) { DJStudio.setIf(DJStudio.ch(p.a.deck), "bpm", v); }
        if (p.b && pr.bStarted) { DJStudio.setIf(DJStudio.ch(p.b.deck), "bpm", v); }
    }
    DJStudio.phaseCheck(pr);
    // 结束
    var aPlaying = DJStudio.playing(p.a.deck) && !pr.aStopped;
    if (pr.tl >= p.len && (!p.playout || !aPlaying)) {
        if (aPlaying) { DJStudio.set(DJStudio.ch(p.a.deck), "play", 0); }
        pr.aStopped = true;
        pr.st = "done";
        pr.doneAt = DJStudio.now();
        DJStudio.send({ t: "ev", e: "done", id: p.id, tl: Math.round(pr.tl * 1000) / 1000 });
        var aDeck = p.a.deck;
        // 留 3 秒给 reverb/echo 尾巴，再把出场 deck 归位
        engine.beginTimer(3000, function () { DJStudio.neutral(aDeck); }, true);
    }
};

// ------------------------------------------------------------------ 装载（AutoDJ 只当装载器）

DJStudio.startLoad = function (d) {
    if (DJStudio.loader) { return "loader busy"; }
    if (DJStudio.playing(d)) { return "deck playing"; }
    var other = d === 1 ? 2 : 1;
    // 两边都没在播时 AutoDJ 总是装进左 deck（朝向 0 = deck1）并起播（autodjprocessor.cpp toggleAutoDJ）
    if (!DJStudio.playing(other) && d !== 1) { return "fresh load must target deck1"; }
    if (DJStudio.playing(other) && DJStudio.prog && DJStudio.prog.st === "running" && DJStudio.prog.p.b &&
        DJStudio.prog.p.b.deck === d) { return "deck in use by running program"; }
    DJStudio.set(DJStudio.ch(d), "volume", 0);
    var otherVol = DJStudio.get(DJStudio.ch(other), "volume");
    // 两边都没在播时，AutoDJ 可能把歌装进另一边并起播：另一边也先静音
    if (!DJStudio.playing(other)) { DJStudio.set(DJStudio.ch(other), "volume", 0); }
    DJStudio.loader = { deck: d, st: "eject", t: DJStudio.now(), otherVol: otherVol, otherWasPlaying: DJStudio.playing(other) };
    if (DJStudio.loaded(d)) { DJStudio.set(DJStudio.ch(d), "eject", 1); }
    return "";
};

DJStudio.runLoader = function () {
    var L = DJStudio.loader;
    if (!L) { return; }
    var d = L.deck, g = DJStudio.ch(d);
    var other = d === 1 ? 2 : 1;
    if (DJStudio.now() - L.t > DJStudio.LOAD_TIMEOUT_MS) {
        DJStudio.set("[AutoDJ]", "enabled", 0);
        DJStudio.loader = null;
        DJStudio.send({ t: "ev", e: "load_failed", deck: d, st: L.st });
        return;
    }
    if (L.st === "eject") {
        if (DJStudio.loaded(d)) { return; }
        L.st = "wait";
        L.enT = DJStudio.now();
        DJStudio.set("[AutoDJ]", "enabled", 1);
        return;
    }
    if (L.st === "wait" && !DJStudio.loaded(d) && DJStudio.get("[AutoDJ]", "enabled") <= 0 &&
        DJStudio.now() - L.enT > DJStudio.AUTODJ_ACCEPT_MS) {
        // 开启请求是异步的；被拒时 enabled 一直是 0（队列空 / 两边都在播 / 没有左右朝向的 deck）
        DJStudio.loader = null;
        DJStudio.set(DJStudio.ch(other), "volume", L.otherVol);
        DJStudio.send({ t: "ev", e: "load_failed", deck: d, st: "autodj_refused" });
        return;
    }
    if (L.st === "wait") {
        // AutoDJ 在没有 deck 在播时会把队首装进 deck1 并直接播放：音量已是 0，装上后立即停
        if (!DJStudio.loaded(d) || !(DJStudio.durMs(d) > 0)) { return; }
        DJStudio.set("[AutoDJ]", "enabled", 0);
        DJStudio.set(g, "play", 0);
        DJStudio.set(g, "volume", 0);
        DJStudio.set("[Master]", "crossfader", 0); // AutoDJ 会甩推子
        if (!L.otherWasPlaying && DJStudio.playing(other)) { DJStudio.set(DJStudio.ch(other), "play", 0); }
        DJStudio.set(DJStudio.ch(other), "volume", L.otherVol);
        DJStudio.neutral(d);
        DJStudio.loader = null;
        DJStudio.send({
            t: "ev", e: "loaded", deck: d, dur: Math.round(DJStudio.durMs(d)),
            fbpm: Math.round(DJStudio.get(g, "file_bpm") * 100) / 100,
            other: DJStudio.loaded(other) ? Math.round(DJStudio.durMs(other)) : 0
        });
    }
};

// ------------------------------------------------------------------ 主循环

DJStudio.tick = function () {
    DJStudio.tickCount++;
    try {
        DJStudio.runLoader();
        DJStudio.runProg();
    } catch (e) {
        DJStudio.error("tick: " + e);
    }
    if (DJStudio.tickCount % DJStudio.STATUS_EVERY === 0) {
        var pr = DJStudio.prog;
        DJStudio.send({
            t: "st",
            n: DJStudio.tickCount,
            up: DJStudio.now(),
            d: [DJStudio.deckStatus(1), DJStudio.deckStatus(2)],
            m: [DJStudio.mixerStatus(1), DJStudio.mixerStatus(2)],
            x: DJStudio.r3(DJStudio.get("[Master]", "crossfader")),
            mv: DJStudio.r3(DJStudio.get("[Main]", "vu_meter")), // 主输出电平在 [Main]（[Master] 只有旧名 VuMeter 的别名，enginevumeter.cpp:20-29）
            pg: pr ? { id: pr.p.id, st: pr.st, tl: Math.round(pr.tl * 1000) / 1000, b: pr.bStarted ? 1 : 0 } : null,
            ld: DJStudio.loader ? DJStudio.loader.deck : 0,
            ad: DJStudio.get("[AutoDJ]", "enabled") > 0 ? 1 : 0
        });
    }
};
