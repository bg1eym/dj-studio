// DJ Studio 前端（原生 JS，无构建步骤）
import { Wave } from "./wave.js";
import { LiveView } from "./live.js";

// ------------------------------------------------------------------ 工具

const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// append 的空值安全版（条件渲染时常有 null）
function put(el, ...kids) {
  el.append(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
}

async function api(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch { /* 空响应 */ }
  if (!r.ok) {
    let msg = data && (data.error || data.detail) || r.statusText;
    if (typeof msg === "object") msg = (msg.error || "") + (msg.details ? "\n" + msg.details.join("\n") : "");
    throw new Error(msg);
  }
  return data;
}

let toastTimer = 0;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), err ? 8000 : 3000);
}

async function guard(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

// 按钮在请求进行中变灰、显示 label（启动引擎要几秒：连点会再开一次）
async function busy(e, label, fn) {
  const b = e && e.currentTarget;
  if (b && b.disabled) return;
  const text = b ? b.textContent : "";
  if (b) { b.disabled = true; b.textContent = label; }
  try { return await guard(fn); } finally { if (b) { b.disabled = false; b.textContent = text; } }
}
window.busy = busy;

// 开始演出（内置引擎可以从第 k 段过渡、倍速）；记下参数，停下后「再开始」用同样的
async function startShow(setId, body) {
  await api("POST", `/api/sets/${setId}/play`, body);
  S.showSetId = setId; S.showCompiled = null; S.homeDone = null;
  await loadState();
  renderHome(true);
}

async function stopShow() {
  await api("POST", "/api/stop");
  toast("停止中…");
}

// 演出中点波形：从那里重新开始（交接完成了也能回卷）。track = 整套 set 里的第几首（0 起）
let seeking = false;
async function seekShow(track, bar) {
  if (seeking) return;
  seeking = true;
  toast(`从 #${track + 1} 第 ${Math.max(1, Math.floor(bar))} 小节重新开始…`);
  try {
    await api("POST", "/api/seek", { track, bar });
    S.homeDone = null;
    await loadState();
    renderHome(true);
  } catch (e) {
    toast(e.message);
  } finally {
    seeking = false;
  }
}

// 停下后「继续」：从停下的那首、那一拍接着放
async function resumeShow() {
  await api("POST", "/api/resume");
  S.homeDone = null;
  await loadState();
  renderHome(true);
}

const fmt = (t) => {
  if (t === null || t === undefined || isNaN(t)) return "–";
  const s = Math.max(0, t);
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
};
const fmtBar = (b) => (b === null || b === undefined || isNaN(b) ? "–" : b.toFixed(1));
const deckFor = (i) => (i % 2 === 0 ? 1 : 2);
// 内置引擎（默认）：DJStudio.js 在内置的 Mixxx 引擎里跑（原速时出声），不连本机的 Mixxx
const isBuiltin = () => !S.state || S.state.engine_mode !== "mixxx";
const hasSound = () => !!(S.state && S.state.sound_engine);
function soundText(e) {
  const who = e.builtin ? (e.link === "mixxx-engine" ? "内置 Mixxx 引擎" : "内置模拟器") : "预演 · 模拟器";
  const a = e.audio || {};
  const snd = a.on ? `声音：${a.device}` : e.link === "mixxx-engine" && e.speed === 1 && e.builtin ? `没有声音（${a.error || "打不开声音输出"}）` : "没有声音";
  return `${who} · ${e.speed}× · ${snd}`;
}

// ------------------------------------------------------------------ 全局状态

const S = {
  tab: "home",
  engine: { connected: false, online: false },
  state: null,
  tracks: [],
  trackById: {},
  selTrack: null,
  browse: null,
  browseSel: new Set(),
  showBrowser: false,
  sets: [],
  setId: null,
  setModel: null,
  compiled: null,
  dirty: false,
  progress: null,
  job: null,
  log: [],
  showSetId: null,
  showCompiled: null,
};

// ------------------------------------------------------------------ 路由

// 首页（#home）一页走完；高级（控制台 / 曲库 / Set / 体检）收在右上角
const TABS = ["home", "show", "library", "sets", "doctor"];
function route() {
  let tab = (location.hash.slice(1) || "home").split("/")[0];
  if (!TABS.includes(tab)) tab = "home"; // 旧链接 #start 等
  S.tab = tab;
  for (const a of document.querySelectorAll("#tabs a")) a.classList.toggle("active", a.dataset.tab === S.tab);
  $("#tabs").classList.toggle("hidden", S.tab === "home");
  $("#adv-link").classList.toggle("hidden", S.tab !== "home");
  for (const s of document.querySelectorAll(".tab")) {
    s.classList.toggle("active", s.id === "tab-" + S.tab);
    // 演出画面（liveRoot、#show-status）只放在当前这一页里，别的页清空，免得重复 id
    if (s.id !== "tab-" + S.tab && (s.id === "tab-home" || s.id === "tab-show")) s.innerHTML = "";
  }
  S.homeMode = null;
  if (S.tab === "home") { renderHome(true); schedulePlanReload(); }
  if (S.tab === "library") renderLibrary();
  if (S.tab === "sets") renderSets();
  if (S.tab === "show") renderShow();
  if (S.tab === "doctor") renderDoctor();
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ 实时事件

function connectEvents() {
  const es = new EventSource("/api/events");
  es.addEventListener("state", (e) => {
    const st = JSON.parse(e.data);
    S.state = st;
    S.engine = st.engine;
    S.progress = st.progress;
    S.job = st.job;
    if (st.progress) S.log = st.progress.log || [];
    updateEngineBadge();
    if (S.tab === "show") renderShow();
    if (S.tab === "home") renderHome();
  });
  es.addEventListener("engine_state", (e) => {
    S.engine = JSON.parse(e.data);
    updateEngineBadge();
    if (S.tab === "show" || S.tab === "home") updateDecks();
  });
  es.addEventListener("progress", (e) => {
    const was = S.progress && S.progress.finished_at;
    S.progress = JSON.parse(e.data);
    S.log = S.progress.log || [];
    if (S.tab === "show") renderShowStatus();
    if (S.tab === "home") renderHome();
    updateEngineBadge();
    if (S.progress.finished_at && !was) setTimeout(() => loadState().then(() => { if (S.tab === "show") renderShow(); if (S.tab === "home") renderHome(); }), 300);
  });
  es.addEventListener("job", (e) => {
    S.job = JSON.parse(e.data);
    renderJobPanels();
    if (S.tab === "home") loadState().then(() => renderHome());
    if (S.job.state !== "running") {
      loadTracks();
      loadState().then(() => { if (S.tab === "show") renderShow(); if (S.tab === "home") renderHome(); });
    }
  });
  es.addEventListener("track", () => { scheduleTracksReload(); if (S.tab === "home") schedulePlanReload(); });
  es.addEventListener("plan", () => { if (S.tab === "home") schedulePlanReload(); });
  es.onerror = () => { S.engine = { connected: false, online: false }; updateEngineBadge(); };
}

let tracksReloadTimer = 0;
function scheduleTracksReload() {
  clearTimeout(tracksReloadTimer);
  tracksReloadTimer = setTimeout(async () => {
    await loadTracks();
    if (S.selTrack) await openTrack(S.selTrack.id, true);
  }, 400);
}

async function loadState() {
  const st = await api("GET", "/api/state");
  S.state = st;
  S.engine = st.engine;
  S.progress = st.progress;
  S.job = st.job;
  updateEngineBadge();
}

// 任务日志在多个页面出现：每 1 秒刷新运行中的任务
setInterval(async () => {
  if (S.job && S.job.state === "running") {
    const st = await api("GET", "/api/state").catch(() => null);
    if (st) { S.job = st.job; S.state = st; renderJobPanels(); if (S.tab === "home") renderHome(); }
  }
}, 1000);

function updateEngineBadge() {
  const b = $("#engine-badge");
  const e = S.engine || {};
  const lost = S.progress && S.progress.stage !== "done" && S.progress.engine_ok === false;
  b.className = "engine" + (e.online ? " online" : (e.connected || lost ? " lost" : ""));
  b.title = e.builtin ? "内置引擎：DJStudio.js 在内置的模拟 Mixxx 里运行，不连本机的 Mixxx，没有声音" : "引擎状态";
  $(".txt", b).textContent = e.rehearsal ? (e.builtin ? `演出中（${e.speed}×${e.audio && e.audio.on ? " · 有声音" : ""}）` : `预演中（模拟器 ${e.speed}×）`)
    : e.builtin ? (hasSound() ? "内置 Mixxx 引擎" : "内置模拟器（没有声音）") : e.online ? "Mixxx 已连接" : (e.connected ? "引擎失联" : "Mixxx 未连接");
  if (!e.builtin && e.online) b.title = `引擎脚本 DJStudio.js ${e.version || ""}`;
  $("#ver").textContent = (S.state && S.state.version) || "";
  const al = $("#alarm");
  const playing = S.state && S.state.playing;
  if (lost || (playing && !e.online)) {
    al.textContent = "⚠ 引擎失联：Mixxx 没有回传状态。conductor 已停发命令，正在等待恢复。看一下 Mixxx 是否卡住 / 弹窗。";
    al.classList.remove("hidden");
  } else al.classList.add("hidden");
}

// ------------------------------------------------------------------ 首页：一页走完 放进打碟谱 → 分析 → 演出

const UP = { files: [], busy: false };
const PLAN_EXTS = ["pdf", "md", "markdown"]; // 打碟谱：PDF 模板，或按《打碟谱规范》写的 .md
const isPlanFile = (name) => PLAN_EXTS.includes(name.toLowerCase().split(".").pop());
const UPLOAD_EXTS = ["pdf", "md", "markdown", "mp3", "wav", "aif", "aiff", "flac", "m4a", "aac", "ogg", "opus"];

function putFile(file, onprog) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", "/api/upload?name=" + encodeURIComponent(file.name));
    x.upload.onprogress = (e) => { if (e.lengthComputable) onprog(e.loaded / e.total); };
    x.onload = () => {
      let body = {};
      try { body = JSON.parse(x.responseText || "{}"); } catch (_) { /* 非 JSON */ }
      if (x.status < 300) resolve(body);
      else reject(new Error(body.detail || body.error || x.statusText));
    };
    x.onerror = () => reject(new Error("上传失败（网页服务停了？）"));
    x.send(file);
  });
}

// 拖进来的可以是文件，也可以是整个文件夹（里面的音频和 PDF 都收，别的跳过；文件夹里的 .md 多半是说明文字，不当打碟谱）
function filesFromDrop(dt) {
  const entries = [...(dt.items || [])].map((it) => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return Promise.resolve([...dt.files]);
  const out = [];
  const walk = async (entry, inDir) => {
    if (entry.isFile) {
      const f = await new Promise((res, rej) => entry.file(res, rej));
      if (inDir) f.djsInDir = true;
      out.push(f);
      return;
    }
    if (!entry.isDirectory) return;
    const reader = entry.createReader();
    for (;;) {
      const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
      if (!batch.length) break;
      for (const e of batch) await walk(e, true).catch(() => {});
    }
  };
  return Promise.all(entries.map((e) => walk(e, false).catch(() => {}))).then(() => out);
}

async function addFiles(list) {
  for (const f of list) {
    const ext = f.name.toLowerCase().split(".").pop();
    if (f.name.startsWith(".") || !UPLOAD_EXTS.includes(ext)) continue; // 文件夹里的封面、文本之类直接跳过
    if (f.djsInDir && ext !== "pdf" && isPlanFile(f.name)) continue;
    UP.files.push({ name: f.name, file: f, state: "queued", frac: 0 });
  }
  renderHomeUploads();
  if (UP.busy) return;
  UP.busy = true;
  // 打碟谱先传：一读到谱，就能开始在电脑里找曲子
  const order = () => UP.files.filter((u) => u.state === "queued").sort((a, b) => (isPlanFile(b.name) ? 1 : 0) - (isPlanFile(a.name) ? 1 : 0));
  for (let q = order(); q.length; q = order()) {
    const u = q[0];
    u.state = "sending";
    renderHomeUploads();
    try {
      const r = await putFile(u.file, (fr) => { u.frac = fr; renderHomeUploads(); });
      u.state = "ok";
      if (r.kind === "plan") { S.plan = r.plan; S.homeNew = false; S.homeSkipJob = S.job ? S.job.started : null; }
    } catch (e) {
      u.state = "err";
      u.msg = e.message;
    }
    u.file = null;
    renderHomeUploads();
    schedulePlanReload();
  }
  UP.busy = false;
}

let planReloadTimer = 0;
function schedulePlanReload() {
  clearTimeout(planReloadTimer);
  planReloadTimer = setTimeout(async () => {
    S.plan = await api("GET", "/api/plan").catch(() => S.plan);
    renderHome();
  }, 400);
}

// 首页最上面一行：现在是哪一套 set（哪一份打碟谱），换一套 / 新 set
function setBar() {
  const bar = h("div", { class: "set-bar" });
  const fill = (plans) => {
    bar.innerHTML = "";
    bar.append(h("span", { class: "dim" }, "Set"));
    if (plans.length) {
      const sel = h("select", { class: "set-pick", title: "放进来过的打碟谱，一份 = 一套 set",
        onchange: (e) => guard(async () => {
          const st = await api("POST", "/api/plans/select", { file: e.target.value });
          S.plan = st; S.homeNew = false; S.homeSkipJob = S.job ? S.job.started : null;
          renderHome(true);
        }) },
        plans.map((pl) => h("option", { value: pl.file, selected: pl.current && !S.homeNew },
          `${pl.title}${pl.n_tracks ? `（${pl.n_tracks} 首）` : ""}${pl.error ? "（读不了）" : ""}`)),
        S.homeNew ? h("option", { value: "", selected: true, disabled: true }, "新 set（拖打碟谱进来）") : null);
      bar.append(sel);
    } else {
      bar.append(h("span", {}, "还没有"));
    }
    bar.append(h("span", { class: "grow" }), S.homeNew
      ? h("button", { onclick: () => { S.homeNew = false; renderHome(true); } }, "取消")
      : h("button", { onclick: () => { S.homeNew = true; renderHome(true); }, title: "换一套歌：放新的打碟谱进来，以前的 set 都留着" }, "＋ 新 set"));
  };
  fill(S.plans || []);
  api("GET", "/api/plans").then((plans) => { S.plans = plans; fill(plans); }).catch(() => {});
  return bar;
}

// 首页现在该显示哪一屏
function homeMode() {
  const p = S.plan;
  const aj = S.job && S.job.name === "分析" && S.job.started !== S.homeSkipJob ? S.job : null;
  // 只认当前这份谱的分析：换了一套 set（新放进来的谱）以后，上一套的「可以演出了」不算
  const job = aj && (!aj.plan || (p && !p.error && aj.plan === p.file)) ? aj : null;
  const prog = S.progress;
  if (S.state && S.state.playing) return "live";
  if (S.homeNew) return "drop";
  if (job && job.state === "running") return "job";
  if (prog && prog.finished_at && S.homeDone !== prog.started_at) return "live"; // 播完 / 停下：先停在演出画面，点「回到开始」再回来
  if (job && job.state === "error") return "error";
  if (job && job.state === "ok") return "ready";
  return p && !p.error ? "plan" : "drop";
}

async function renderHome(force = false) {
  const root = $("#tab-home");
  if (!root || S.tab !== "home") return;
  const mode = homeMode();
  const playing = !!(S.state && S.state.playing);
  if (mode === "live" && playing !== S.homeLivePlaying) force = true; // 停下了：「停止」换成「再开始」
  if (mode === S.homeMode && !force && root.childElementCount) {
    if (mode === "drop" || mode === "plan") renderHomePlan();
    else if (mode === "live") renderShowStatus();
    else renderHomeJob();
    return;
  }
  S.homeMode = mode;
  const seq = (S.homeSeq = (S.homeSeq || 0) + 1);
  if (mode === "live") {
    S.homeLivePlaying = playing;
    const last = S.state && S.state.last_play;
    S.showSetId = ((S.engine || {}).rehearsal && S.engine.set) || (last && last.set) || (S.progress && S.progress.set_id) || S.showSetId;
    await loadShowCompiled();
    if (S.homeMode !== "live" || seq !== S.homeSeq) return; // 等编译结果时状态变了 / 又开始了一次渲染
    root.innerHTML = "";
    const prog = S.progress || {};
    const resume = S.state && S.state.resume;
    root.append(
      h("div", { class: "panel row home-live-bar" },
        h("b", {}, (S.showCompiled && S.showCompiled.set && S.showCompiled.set.name) || S.showSetId || ""),
        h("span", { class: "grow" }),
        h("a", { href: `/api/sets/${S.showSetId}/cuesheet.md`, target: "_blank", class: "dim", style: "margin-right:12px" }, "Hot Cue 表"),
        playing ? h("button", { class: "danger", onclick: (e) => busy(e, "停止中…", stopShow) }, "■ 停止")
          : [resume && resume.set === S.showSetId
              ? h("button", { class: "primary", title: "从停下的位置接着放", onclick: (e) => busy(e, "启动中…", resumeShow) }, `▶ 继续（${resume.label}）`)
              : null,
            h("button", { onclick: () => { S.homeDone = prog.started_at; renderHome(true); } }, "回到开始")]),
      h("div", { class: "panel", id: "show-status" }),
      liveRoot,
      h("details", { class: "panel job-log" }, h("summary", {}, "运行日志"), h("div", { class: "log", id: "show-log" })),
    );
    live.setSet(S.showCompiled);
    live.onSeek = isBuiltin() ? seekShow : null; // 内置引擎：点波形从那里放
    renderShowStatus();
    return;
  }
  root.innerHTML = "";
  const card = h("div", { class: "home" });
  root.append(card);
  card.append(setBar());
  if (mode === "drop" || mode === "plan") {
    const input = h("input", { type: "file", multiple: true, accept: "audio/*,.pdf,application/pdf,.md,.markdown,text/markdown", class: "hidden",
      onchange: (e) => { addFiles([...e.target.files]); e.target.value = ""; } });
    const drop = h("div", { class: "drop" + (mode === "plan" ? " small" : ""), onclick: () => input.click() },
      h("div", { class: "drop-big" }, mode === "drop" ? (S.homeNew ? "新 set：把这一套的打碟谱拖到这里（.md 或 PDF）" : "把打碟谱拖到这里（.md 或 PDF）") : "缺的曲子拖到这里"),
      h("div", { class: "dim" }, mode === "drop"
        ? "这一套的曲子可以一起拖进来（文件或整个文件夹）。没拖进来的，会按谱上的文件名去「下载 / 桌面 / 文稿 / 音乐」里找。以前的 set 都还在，上面可以换回去。"
        : "文件或整个文件夹都行；也可以点这里选文件。"));
    // 写谱用的：规范 + 模板（复制模板改成自己的 set）
    const docs = h("div", { class: "dim score-docs" }, "自己写谱：",
      h("a", { href: "/api/score/spec.md", target: "_blank" }, "打碟谱规范"), " · ",
      h("a", { href: "/api/score/template.md?download=1" }, "下载模板"),
      "（Denon Prime 4+ 的旋钮位、推子、效果器、Hot Cue 怎么写）");
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); filesFromDrop(e.dataTransfer).then(addFiles); });
    card.append(h("div", { id: "home-plan" }), drop, input, docs, h("div", { id: "home-uploads" }));
    renderHomePlan();
    renderHomeUploads();
  } else {
    card.append(h("div", { id: "home-job" }));
    renderHomeJob();
  }
}

function renderHomeUploads() {
  const el = $("#home-uploads");
  if (!el) return;
  el.innerHTML = "";
  const n = UP.files.length;
  if (!n) return;
  const done = UP.files.filter((u) => u.state === "ok" || u.state === "err").length;
  const cur = UP.files.find((u) => u.state === "sending");
  const frac = (done + (cur ? cur.frac : 0)) / n;
  if (done < n) {
    el.append(h("div", { class: "up-line" }, h("span", {}, `正在放进来 ${done} / ${n}`),
      h("span", { class: "up-bar grow" }, h("span", { style: `width:${Math.round(frac * 100)}%` })),
      h("span", { class: "dim up-name" }, cur ? cur.name : "")));
  }
  for (const u of UP.files.filter((x) => x.state === "err")) el.append(h("div", { class: "up-err" }, `${u.name}：${u.msg}`));
}

function planHeader(p) {
  const nl = p.techniques.filter((t) => t === "longhand").length;
  return h("div", { class: "home-title" },
    h("h1", {}, p.title),
    h("div", { class: "dim" }, `${p.n_tracks} 首 · ${p.n_transitions} 段过渡（${nl} 长交接 · ${p.n_transitions - nl} 瞬时切换）`));
}

function renderHomePlan() {
  const el = $("#home-plan");
  if (!el) return;
  const p = S.plan;
  el.innerHTML = "";
  if (!p) return;
  if (p.error) { el.append(h("div", { class: "err-box" }, `「${p.file}」读不了：${p.error}`)); return; }
  const have = p.tracks.filter((t) => t.track).length;
  const missing = p.tracks.filter((t) => !t.track);
  const f = p.finder || {};
  const job = S.job && S.job.state === "running" ? S.job : null;
  const findMsg = f.state === "running" ? h("div", { class: "dim" }, "正在电脑里按谱上的文件名找曲子…")
    : f.state === "done" && f.found && f.found.length ? h("div", { class: "dim" }, `在电脑里找到 ${f.found.length} 首，已放进曲库`)
      : f.state === "error" ? h("div", { class: "warn-box" }, `在电脑里找曲子时出错：${f.error}`) : null;
  // 原生 append 会把 null 当成文字「null」：只放有的
  el.append(planHeader(p),
    h("div", { class: "home-count" }, h("span", { class: "big-pct" }, `${have} / ${p.n_tracks}`), h("span", {}, " 首曲子已就位")),
    ...(findMsg ? [findMsg] : []));
  if (p.warnings && p.warnings.length) el.append(h("div", { class: "warn-box" }, p.warnings.map((w) => h("div", {}, w))));
  if (missing.length && f.state !== "running") {
    el.append(h("div", { class: "missing" }, h("div", {}, "还缺这几首，把文件拖到下面："),
      missing.map((t) => h("div", { class: "mono" }, `#${String(t.no).padStart(2, "0")} ${t.title}`, h("span", { class: "dim" }, t.file ? `  （${t.file}）` : "")))));
  }
  if (!missing.length && f.state !== "running") {
    el.append(h("div", { class: "row", style: "margin-top:10px" },
      h("button", { class: "primary", disabled: !!job, onclick: () => guard(async () => { S.job = await api("POST", "/api/auto"); S.homeSkipJob = null; renderHome(); }) },
        job ? `「${job.name}」进行中…` : "开始分析")));
  }
}

function renderHomeJob() {
  const el = $("#home-job");
  if (!el) return;
  const job = S.job;
  const p = S.plan;
  el.innerHTML = "";
  if (!job) return;
  if (p && !p.error) el.append(planHeader(p));
  const pct = Math.round((job.progress || 0) * 100);
  const nStages = (job.stages || []).length;
  if (job.state !== "ok") {
    el.append(
      h("div", { class: "big-bar" + (job.state === "error" ? " err" : "") }, h("div", { style: `width:${job.state === "error" ? 100 : pct}%` })),
      h("div", { class: "row", style: "margin-top:6px" },
        h("span", { class: "big-pct" }, job.state === "error" ? "没完成" : `${pct}%`),
        h("span", { class: "grow" }, job.state === "error" ? ""
          : nStages ? `第 ${Math.min(job.stage + 1, nStages)} / ${nStages} 步：${job.stages[job.stage] || ""}` : job.name),
        h("span", { class: "dim" }, job.state === "error" ? "" : job.detail || "")));
    if (job.state === "running") el.append(h("div", { class: "dim", style: "margin-top:8px" },
      isBuiltin() ? "不用管，大概几分钟。不会动你的 Mixxx。" : "分析中会重启 Mixxx。不用管，大概几分钟。"));
  }
  if (job.state === "error") {
    const errs = job.log.filter((l) => l.includes("!!")).slice(-3).map((l) => l.replace(/^\S+ !! /, ""));
    el.append(h("div", { class: "err-box" }, errs.join("\n") || "出错了，看下面的日志"),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: () => guard(async () => { S.job = await api("POST", "/api/auto"); S.homeSkipJob = null; renderHome(); }) }, "重新分析"),
        h("button", { onclick: () => { S.homeSkipJob = job.started; renderHome(true); } }, "换一份打碟谱 / 补曲子")));
  }
  const r = job.state === "ok" ? job.result : null;
  if (r) {
    const warns = (r.notes || []).filter((n) => n.level === "warn");
    const builtin = isBuiltin();
    const nTr = (p && !p.error && p.n_transitions) || 0;
    // 内置引擎：可以从第 k 段过渡前开始、倍速看
    const opts = builtin ? h("div", { class: "row play-opts" },
      h("label", {}, "从 ", h("select", { id: "play-from" }, h("option", { value: 0 }, "头"),
        Array.from({ length: nTr }, (_, i) => h("option", { value: i + 1 }, `过渡 ${i + 1} 前 8 小节（#${i + 1} → #${i + 2}）`))), " 开始"),
      h("label", {}, "速度 ", h("select", { id: "play-speed" }, [1, 2, 4, 8].map((v) => h("option", { value: v }, v === 1 ? "1×（有声音）" : `${v}×（没有声音）`))))) : null;
    el.append(h("div", { class: "ready" },
      h("div", { class: "grow" }, h("div", { class: "ready-big" }, "可以演出了"),
        h("div", { class: "dim" }, builtin ? `约 ${Math.round(r.duration / 60)} 分钟。用内置的 Mixxx 引擎，不连你装的 Mixxx。` +
          (hasSound() ? "原速时从电脑的声音输出放出来；倍速时没有声音。" : "没有找到声音引擎：看得到旋钮，没有声音。")
          : `约 ${Math.round(r.duration / 60)} 分钟。Mixxx 已经准备好。`), opts),
      h("button", { class: "primary huge", onclick: (e) => busy(e, "启动中…", () =>
        startShow(r.set, builtin ? { from: +$("#play-from").value, speed: +$("#play-speed").value } : {})) }, "▶ 开始演出")));
    if (warns.length) el.append(h("div", { class: "warn-box" }, h("b", {}, "需要注意"), warns.map((n) => h("div", {}, `${n.where}：${n.msg}`))));
    el.append(h("div", { class: "row dim", style: "margin-top:10px" }, builtin ? null :
      h("a", { href: "#home", onclick: (e) => { e.preventDefault(); guard(async () => {
        await api("POST", `/api/sets/${r.set}/rehearse`, { speed: 1, from: 0 });
        S.showSetId = r.set; S.showCompiled = null; S.homeDone = null;
        await loadState(); renderHome(true);
      }); } }, "先在模拟器里预演（不碰 Mixxx，没有声音）"),
      h("a", { href: `/api/sets/${r.set}/cuesheet.md`, target: "_blank", title: "按播放规范（docs/SET_SPEC.md）生成的 hot cue：打击垫 1–8、名字、颜色、位置" }, "Hot Cue 表（Prime 4+）"),
      h("a", { href: `/api/sets/${r.set}/cuesheet.md?download=1` }, "下载 .md"),
      h("span", { class: "grow" }),
      h("a", { href: "#home", onclick: (e) => { e.preventDefault(); S.homeNew = true; renderHome(true); } }, "＋ 新 set")));
  }
  el.append(h("details", { class: "job-log" }, h("summary", {}, "详细日志"), h("div", { class: "log" }, job.log.join("\n"))));
}

// ------------------------------------------------------------------ 曲库

async function loadTracks() {
  S.tracks = await api("GET", "/api/tracks");
  S.trackById = Object.fromEntries(S.tracks.map((t) => [t.id, t]));
  if (S.tab === "library") renderTrackTable();
}

function trackBadges(t) {
  const b = [];
  if (t.analysis_error) b.push(h("span", { class: "badge err", title: t.analysis_error }, "分析失败"));
  else if (!t.analyzed) b.push(h("span", { class: "badge" }, "分析中"));
  if (!t.in_mixxx) b.push(h("span", { class: "badge warn" }, "不在 Mixxx"));
  else if (!t.mixxx_analyzed) b.push(h("span", { class: "badge warn" }, "Mixxx 未分析"));
  else b.push(h("span", { class: "badge ok" }, "Mixxx ✓"));
  if (t.grid_check && !t.grid_check.ok) b.push(h("span", { class: "badge err", title: t.grid_check.note }, "网格存疑"));
  if (t.analyzed && !t.bar1_confirmed && (t.downbeat_conf ?? 1) < 0.3) b.push(h("span", { class: "badge warn" }, "小节线待确认"));
  return b;
}

function renderLibrary() {
  const root = $("#tab-library");
  root.innerHTML = "";
  const aq = S.state && S.state.analysis;
  root.append(
    h("div", { class: "panel row" },
      h("button", { class: "primary", onclick: () => { S.showBrowser = !S.showBrowser; renderLibrary(); if (S.showBrowser && !S.browse) browseTo(null); } },
        S.showBrowser ? "收起导入" : "导入音频…"),
      h("button", { onclick: () => guard(async () => { await api("POST", "/api/sync", {}); toast("已开始同步到 Mixxx（见下方任务日志）"); }) },
        "同步到 Mixxx"),
      h("span", { class: "dim" }, "同步 = 让 Mixxx 扫描托管曲库并分析新歌（会重启 Mixxx）"),
      S.tracks.some((t) => t.grid_check && !t.grid_check.ok)
        ? h("button", { class: "danger", onclick: () => fixGrids(null) },
          `校正 ${S.tracks.filter((t) => t.grid_check && !t.grid_check.ok).length} 首存疑网格`) : null,
      h("span", { class: "grow" }),
      aq && (aq.busy || aq.queued.length) ? h("span", { class: "dim" }, `分析中… 还有 ${aq.queued.length + (aq.busy ? 1 : 0)} 首`) : null,
      h("input", { placeholder: "搜索", id: "track-q", oninput: renderTrackTable }),
    ),
    h("div", { class: "panel" + (S.showBrowser ? "" : " hidden"), id: "browser" }),
    h("div", { id: "job-library" }),
    h("div", { class: "panel" }, h("table", { id: "track-table" })),
    h("div", { class: "panel hidden", id: "track-detail" }),
  );
  if (S.showBrowser) renderBrowser();
  renderTrackTable();
  renderJobPanels();
  if (S.selTrack) renderTrackDetail();
}

async function browseTo(path) {
  await guard(async () => {
    S.browse = await api("GET", "/api/browse" + (path ? "?path=" + encodeURIComponent(path) : ""));
    S.browseSel = new Set();
    renderBrowser();
  });
}

function renderBrowser() {
  const el = $("#browser");
  if (!el || !S.browse) return;
  const b = S.browse;
  el.innerHTML = "";
  el.append(
    h("div", { class: "row" },
      b.parent !== null ? h("button", { class: "small", onclick: () => browseTo(b.parent || null) }, "↑ 上一级") : null,
      h("span", { class: "crumbs grow" }, b.path || "选择位置"),
      b.path ? h("button", { class: "small", onclick: () => doImport([b.path]) }, `导入整个文件夹（含子文件夹）`) : null,
      h("button", { class: "small primary", disabled: !S.browseSel.size, onclick: () => doImport([...S.browseSel]) },
        `导入所选 ${S.browseSel.size || ""}`),
    ),
    h("div", { class: "browser" },
      b.dirs.map((d) => h("div", { class: "item dir", onclick: () => browseTo(d.path) }, "📁 ", d.name)),
      b.files.map((f) => h("label", { class: "item" },
        h("input", { type: "checkbox", checked: S.browseSel.has(f.path),
          onchange: (e) => { e.target.checked ? S.browseSel.add(f.path) : S.browseSel.delete(f.path); renderBrowser(); } }),
        f.name, h("span", { class: "dim" }, (f.size / 1048576).toFixed(1) + " MB"),
        f.cloud ? h("span", { class: "badge warn", title: "iCloud 仅云端：导入时会先下载" }, "云端") : null)),
      !b.dirs.length && !b.files.length ? h("div", { class: "dim" }, "这里没有文件夹或音频文件") : null,
    ),
  );
}

async function doImport(paths) {
  await guard(async () => {
    toast("导入中…（复制到托管曲库）");
    const r = await api("POST", "/api/import", { paths });
    toast(`导入 ${r.added.length} 首；已存在 ${r.existing.length} 首` + (r.failed.length ? `；失败 ${r.failed.length}：\n` + r.failed.map((f) => f.join("：")).join("\n") : ""), r.failed.length > 0);
    S.browseSel = new Set();
    await loadTracks();
    await loadState();
    renderLibrary();
  });
}

function renderTrackTable() {
  const tb = $("#track-table");
  if (!tb) return;
  const q = ($("#track-q") || {}).value || "";
  const rows = S.tracks.filter((t) => !q || (t.display + t.path).toLowerCase().includes(q.toLowerCase()));
  tb.innerHTML = "";
  tb.append(h("thead", {}, h("tr", {}, ["曲目", "时长", "BPM", "调", "小节", "状态"].map((x) => h("th", {}, x)))));
  tb.append(h("tbody", {}, rows.map((t) => h("tr", { class: S.selTrack && S.selTrack.id === t.id ? "sel" : "", onclick: () => openTrack(t.id) },
    h("td", {}, h("div", {}, t.title || "?"), h("div", { class: "dim" }, t.artist)),
    h("td", { class: "mono" }, fmt(t.duration)),
    h("td", { class: "mono" }, t.grid ? t.grid.bpm.toFixed(2) : "–", t.grid ? h("div", { class: "dim" }, t.grid.source === "mixxx" ? "Mixxx" : "自估") : null),
    h("td", {}, t.key || "–"),
    h("td", { class: "mono" }, t.grid ? t.grid.bars : "–"),
    h("td", {}, trackBadges(t)),
  ))));
  if (!rows.length) tb.append(h("tr", {}, h("td", { colspan: 6, class: "dim" }, "曲库是空的：点「导入音频…」从本地文件夹导入")));
}

let wave = null;
let audio = null;
let detailToken = 0;

async function openTrack(id, keepView = false) {
  const t = await api("GET", "/api/tracks/" + id);
  const changed = !S.selTrack || S.selTrack.id !== id;
  S.selTrack = t;
  renderTrackTable();
  renderTrackDetail(keepView && !changed);
}

async function renderTrackDetail(keepView = false) {
  const el = $("#track-detail");
  const t = S.selTrack;
  if (!el || !t) return;
  el.classList.remove("hidden");
  const view = keepView && wave ? [wave.t0, wave.t1] : null;
  const token = ++detailToken;
  el.innerHTML = "";
  if (audio) { audio.pause(); }
  audio = new Audio("/api/tracks/" + t.id + "/audio");
  const posEl = h("span", { class: "mono" }, "0:00.0");
  const canvas = h("canvas", { class: "wave" });
  const g = t.grid;
  const beat = g ? 60 / g.bpm : 0;
  const shiftBar1 = (beats) => guard(async () => {
    await api("PATCH", "/api/tracks/" + t.id, { bar1: g.bar1 + beats * beat, bar1_confirmed: true });
    toast("已移动小节线，重新切段落中…");
    await openTrack(t.id, true);
  });
  put(el,
    h("div", { class: "row" },
      h("h2", { class: "grow" }, t.display),
      trackBadges(t),
    ),
    h("div", { class: "wave-wrap" }, canvas),
    h("div", { class: "row", style: "margin-top:8px" },
      h("button", { onclick: () => (audio.paused ? audio.play() : audio.pause()) }, "▶ / ❚❚"),
      posEl,
      h("span", { class: "grow" }),
      g ? h("span", { class: "dim" }, `第 1 小节线 ${fmt(g.bar1)}`) : null,
      g ? h("button", { class: "small", onclick: () => shiftBar1(-1) }, "◀ 1 拍") : null,
      g ? h("button", { class: "small", onclick: () => shiftBar1(1) }, "1 拍 ▶") : null,
      g ? h("label", { class: "dim" }, h("input", { type: "checkbox", checked: t.bar1_confirmed,
        onchange: (e) => guard(() => api("PATCH", "/api/tracks/" + t.id, { bar1_confirmed: e.target.checked })) }), " 小节线已确认") : null,
      h("button", { class: "small", onclick: () => guard(async () => { await api("POST", `/api/tracks/${t.id}/reanalyze`); toast("已排队重新分析"); }) }, "重新分析"),
      t.grid_check && !t.grid_check.ok ? h("button", { class: "small danger", onclick: () => fixGrids([t.id]) }, "校正网格") : null,
    ),
    h("div", { class: "dim", style: "margin-top:6px" },
      g ? `网格：${g.bpm.toFixed(3)} BPM（${g.source === "mixxx" ? "Mixxx" : "自估，Mixxx 分析后以 Mixxx 为准"}），共 ${g.bars} 小节。` : "还没有网格。",
      t.grid_check && !t.grid_check.ok ? ` ⚠ 与自估网格不一致：${t.grid_check.note}` : "",
      " 滚轮缩放，拖动平移，单击定位。"),
    t.sections && t.sections.length ? h("div", { style: "margin-top:8px" },
      h("h3", {}, "段落（自动分析，仅供参考）"),
      h("div", { class: "row" }, t.sections.map((s) => h("span", { class: "badge" }, `${s.label} ${s.start}–${s.end - 1}`)))) : null,
    h("div", { class: "dim mono", style: "margin-top:6px;word-break:break-all" }, t.path),
  );
  const wv = wave = new Wave(canvas, { height: 170 });
  const au = audio;
  wv.onSeek = (time) => { au.currentTime = time; wv.setPlayhead(time); };
  const wf = t.analyzed ? await api("GET", `/api/tracks/${t.id}/waveform`).catch(() => null) : null;
  if (token !== detailToken) return;
  wv.setData(wf, g, t.sections);
  if (view) wv._setView(view[0], view[1]);
  const tick = () => {
    if (token !== detailToken) { au.pause(); return; }
    const ct = au.currentTime;
    posEl.textContent = fmt(ct) + (g ? `  ·  第 ${fmtBar(wv.timeBar(ct))} 小节` : "");
    if (!au.paused) wv.setPlayhead(ct);
    requestAnimationFrame(tick);
  };
  tick();
}

async function fixGrids(ids) {
  await guard(async () => {
    const plan = (await api("GET", "/api/grids/plan")).filter((p) => !ids || ids.includes(p.id));
    if (!plan.length) { toast("没有需要校正的网格"); return; }
    const msg = plan.map((p) => `· ${p.title}：${p.note}`).join("\n");
    if (!confirm(`把这些歌的 Mixxx 网格换成自估网格（网格线对准 kick，并锁定 BPM）？会退出 Mixxx 并先备份曲库。\n\n${msg}`)) return;
    await api("POST", "/api/grids/fix", { track_ids: ids });
    toast("校正中…（见任务日志）");
  });
}

// ------------------------------------------------------------------ 任务面板

function jobPanel(compact = false) {
  const j = S.job;
  if (!j) return null;
  const color = j.state === "error" ? "var(--err)" : j.state === "ok" ? "var(--ok)" : "var(--accent)";
  const head = h("div", { class: "row" }, h("b", {}, j.name), h("span", { style: `color:${color}` }, { running: "进行中…", ok: "完成", error: "失败" }[j.state]));
  const log = h("div", { class: "log", style: "margin-top:6px;max-height:220px" }, j.log.join("\n"));
  if (compact && j.state === "ok") {
    // 演出页：完成的任务只留一行，点开看日志（别把混音台挤到屏幕外）
    const d = h("details", { class: "panel job-mini" }, h("summary", {}, head), log);
    return d;
  }
  return h("div", { class: "panel" }, head, log);
}

function renderJobPanels() {
  for (const id of ["job-library", "job-sets", "job-show", "job-doctor"]) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.innerHTML = "";
    const p = jobPanel(id === "job-show");
    if (p) el.append(p);
    const log = el.querySelector(".log");
    if (log) log.scrollTop = log.scrollHeight;
  }
}

// ------------------------------------------------------------------ Set

async function renderSets() {
  const root = $("#tab-sets");
  root.innerHTML = "";
  S.sets = await api("GET", "/api/sets").catch(() => []);
  if (!S.setId && S.sets.length) S.setId = S.sets[0].id;
  const list = h("div", { class: "panel setlist" },
    h("div", { class: "row" }, h("h2", { class: "grow" }, "Set"), h("button", { class: "small", onclick: newSet }, "新建")),
    S.sets.map((s) => h("div", { class: "item" + (s.id === S.setId ? " active" : ""), onclick: () => { if (confirmLeave()) { S.setId = s.id; S.setModel = null; renderSets(); } } },
      h("div", {}, s.name || s.id), h("div", { class: "dim" }, s.error ? "⚠ 文件有误" : `${s.n} 首 · ${s.id}`))),
    !S.sets.length ? h("div", { class: "dim" }, "还没有 set。点「新建」，或让 Claude 按编排方案生成 set 文件放进 ~/DJStudio/sets/。") : null,
  );
  const main = h("div", { id: "set-main" });
  root.append(h("div", { class: "split" }, list, main));
  if (S.setId) await loadSet(S.setId);
}

function confirmLeave() {
  return !S.dirty || confirm("当前 set 有未保存的修改，放弃吗？");
}

async function newSet() {
  const id = prompt("新 set 的 id（字母数字、- 或 _）：", "set" + new Date().toISOString().slice(0, 10).replace(/-/g, ""));
  if (!id) return;
  const name = prompt("名称：", id) || id;
  await guard(async () => {
    await api("PUT", "/api/sets/" + id, {
      schema: "djset/2", id, name, created: new Date().toISOString().slice(0, 10), tracks: [], transitions: [],
    });
    S.setId = id;
    S.setModel = null;
    S.dirty = false;
    renderSets();
  });
}

async function loadSet(id) {
  await guard(async () => {
    if (!S.tracks.length) await loadTracks();
    const r = await api("GET", "/api/sets/" + id);
    S.setModel = r.set;
    S.compiled = r.compiled;
    S.dirty = false;
    renderSetMain();
  });
}

let validateTimer = 0;
function changed() {
  S.dirty = true;
  const sb = $("#save-btn");
  if (sb) sb.disabled = false;
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    try {
      S.compiled = await api("POST", "/api/sets/validate", S.setModel);
      renderCompile();
    } catch (e) {
      const el = $("#compile");
      if (el) { el.innerHTML = ""; el.append(h("div", { class: "issues" }, h("div", { class: "error" }, e.message))); }
    }
  }, 400);
}

function syncTransitions() {
  const m = S.setModel;
  while (m.transitions.length < Math.max(0, m.tracks.length - 1)) {
    m.transitions.push({ technique: "longhand", start: 97, b_in: 1, params: {}, lanes: {}, events: [], tempo: null, note: "" });
  }
  m.transitions.length = Math.max(0, m.tracks.length - 1);
}

function numInput(obj, key, opts = {}) {
  return h("input", { type: "number", step: opts.step || "any", value: obj[key] ?? "", placeholder: opts.placeholder || "",
    style: opts.width ? `width:${opts.width}px` : null,
    onchange: (e) => { const v = e.target.value; obj[key] = v === "" ? null : parseFloat(v); changed(); } });
}

function renderSetMain() {
  const el = $("#set-main");
  const m = S.setModel;
  if (!el || !m) return;
  el.innerHTML = "";
  el.append(
    h("div", { class: "panel" },
      h("div", { class: "row" },
        h("input", { value: m.name, style: "font-size:16px;font-weight:600;width:320px", onchange: (e) => { m.name = e.target.value; changed(); } }),
        h("span", { class: "dim mono" }, m.id),
        h("span", { class: "grow" }),
        h("button", { id: "save-btn", class: "primary", disabled: !S.dirty, onclick: saveSet }, "保存"),
        h("button", { onclick: () => { const j = $("#json-panel"); j.classList.toggle("hidden"); $("textarea", j).value = JSON.stringify(m, null, 2); } }, "JSON"),
        h("button", { onclick: () => { S.showSetId = m.id; location.hash = "#show"; } }, "去演出 →"),
      ),
      h("div", { class: "row", style: "margin-top:6px" },
        h("label", { class: "dim" }, "来源 ", h("input", { value: m.source, style: "width:360px", placeholder: "编排方案出处", onchange: (e) => { m.source = e.target.value; changed(); } })),
        h("label", { class: "dim" }, "终曲 ",
          h("select", { onchange: (e) => { m.ending.mode = e.target.value; changed(); } },
            h("option", { value: "fade", selected: m.ending.mode === "fade" }, "淡出"),
            h("option", { value: "playout", selected: m.ending.mode === "playout" }, "播完")),
          " 从第", numInput(m.ending, "bar", { placeholder: "自动", width: 64 }), "小节，", numInput(m.ending, "bars", { width: 50 }), "小节"),
      ),
      h("div", { id: "json-panel", class: "hidden", style: "margin-top:8px" },
        h("textarea", { rows: 18 }),
        h("div", { class: "row", style: "margin-top:4px" },
          h("button", { onclick: () => guard(() => {
            const v = JSON.parse($("#json-panel textarea").value);
            if (v.id !== m.id) throw new Error("JSON 里的 id 必须是 " + m.id);
            S.setModel = v; changed(); renderSetMain();
          }) }, "应用 JSON"),
          h("span", { class: "dim" }, "把 Claude 按编排方案生成的 set JSON 粘贴进来，应用后再保存。"))),
    ),
    h("div", { id: "job-sets" }),
    h("div", { class: "panel", id: "compile" }),
    h("div", { class: "cards", id: "cards" }),
    h("div", { class: "panel", style: "margin-top:10px" },
      h("div", { class: "row" }, h("b", {}, "添加曲目"), h("input", { id: "add-q", placeholder: "在曲库里搜索…", oninput: renderAddResults, style: "width:300px" })),
      h("div", { id: "add-results" })),
  );
  renderCards();
  renderCompile();
  renderJobPanels();
}

function renderAddResults() {
  const el = $("#add-results");
  const q = $("#add-q").value.toLowerCase();
  el.innerHTML = "";
  if (!q) return;
  const hits = S.tracks.filter((t) => (t.display + " " + t.path).toLowerCase().includes(q)).slice(0, 12);
  el.append(h("table", {}, h("tbody", {}, hits.map((t) => h("tr", { onclick: () => {
    S.setModel.tracks.push({ id: t.id, title: t.display, artist: "", file: null, start: 1, downbeat: null, play_bpm: null, cues: {}, note: "" });
    syncTransitions(); changed(); renderCards(); $("#add-q").value = ""; el.innerHTML = "";
  } }, h("td", {}, t.display), h("td", { class: "mono dim" }, t.grid ? t.grid.bpm.toFixed(2) : "–"), h("td", {}, t.key || ""))))));
}

async function saveSet() {
  await guard(async () => {
    const r = await api("PUT", "/api/sets/" + S.setModel.id, S.setModel);
    S.setModel = r.set;
    S.compiled = r.compiled;
    S.dirty = false;
    toast("已保存");
    renderSetMain();
  });
}

function renderCompile() {
  const el = $("#compile");
  const c = S.compiled;
  if (!el || !c) return;
  el.innerHTML = "";
  const errs = c.issues.filter((i) => i.level === "error").length;
  el.append(
    h("div", { class: "row" },
      h("b", { style: `color:${c.ok ? "var(--ok)" : "var(--err)"}` }, c.ok ? "✓ 校验通过" : `✗ ${errs} 个错误`),
      h("span", { class: "dim" }, `总时长约 ${fmt(c.duration)}`)),
  );
  if (c.issues.length) {
    el.append(h("div", { class: "issues", style: "margin-top:6px" }, c.issues.map((i) => h("div", { class: i.level }, `${i.where}：${i.msg}`))));
  }
  if (c.schedule.length && c.duration > 0) {
    el.append(h("div", { class: "timeline", style: "margin-top:8px" }, c.schedule.map((s) => h("div", {
      class: s.kind, style: `flex:${Math.max(0.001, s.t1 - s.t0)}`,
      title: `${s.kind} ${s.label} ${fmt(s.t0)}–${fmt(s.t1)}`,
    }, s.kind === "transition" ? s.label : s.label))));
  }
  // 同步刷新每段的自动化预览
  c.programs.forEach((p, i) => {
    const holder = document.getElementById("lanes-" + i);
    if (holder) { holder.innerHTML = ""; holder.append(lanesSvg(p)); }
  });
}

function renderCards() {
  const el = $("#cards");
  if (!el) return;
  const m = S.setModel;
  el.innerHTML = "";
  m.tracks.forEach((st, i) => {
    el.append(trackCard(st, i));
    if (i < m.transitions.length) el.append(transCard(m.transitions[i], i));
  });
  if (!m.tracks.length) el.append(h("div", { class: "dim panel" }, "还没有曲目：在下面搜索添加，或用「JSON」粘贴整套。"));
}

function trackCard(st, i) {
  const m = S.setModel;
  const t = S.trackById[st.id];
  const wfHolder = h("div", { class: "hidden", style: "margin-top:8px" });
  const move = (d) => {
    const j = i + d;
    if (j < 0 || j >= m.tracks.length) return;
    [m.tracks[i], m.tracks[j]] = [m.tracks[j], m.tracks[i]];
    changed(); renderCards();
  };
  return h("div", { class: "card track" + (i % 2 ? " even" : "") },
    h("div", { class: "head" },
      h("b", {}, `#${i + 1}`),
      h("span", { class: "grow" }, st.title || (t && t.display) || st.id, !t ? h("span", { class: "badge err" }, "曲库里没有") : trackBadges(t)),
      h("span", { class: "dim mono" }, t && t.grid ? `${t.grid.bpm.toFixed(2)} BPM · ${t.key || "?"} · ${t.grid.bars} 小节` : ""),
      i === 0 ? h("label", {}, "从第", numInput(st, "start", { width: 56 }), "小节开场") : null,
      h("label", {}, "主控 BPM", numInput(st, "play_bpm", { placeholder: "原生", width: 72 })),
      h("button", { class: "small", onclick: () => toggleTrackWave(st, i, wfHolder) }, "波形"),
      h("button", { class: "small", onclick: () => move(-1) }, "↑"),
      h("button", { class: "small", onclick: () => move(1) }, "↓"),
      h("button", { class: "small", onclick: () => { if (confirm("移除这首？")) { m.tracks.splice(i, 1); m.transitions.splice(Math.max(0, i - 1), 1); syncTransitions(); changed(); renderCards(); } } }, "✕"),
    ),
    st.note ? h("div", { class: "dim" }, st.note) : null,
    wfHolder,
  );
}

async function toggleTrackWave(st, i, holder) {
  if (!holder.classList.contains("hidden")) { holder.classList.add("hidden"); holder.innerHTML = ""; return; }
  const t = S.trackById[st.id];
  if (!t) return;
  holder.classList.remove("hidden");
  const detail = await api("GET", "/api/tracks/" + t.id);
  const wf = await api("GET", `/api/tracks/${t.id}/waveform`).catch(() => null);
  const m = S.setModel;
  const marks = [];
  if (i === 0) marks.push({ bar: st.start || 1, label: "开场", color: "#3ccf7a" });
  if (i > 0) {
    const tin = m.transitions[i - 1];
    marks.push({ bar: tin.b_in, label: `进 ${tin.b_in}`, color: "#46c2ff" });
  }
  if (i < m.transitions.length) {
    const tr = m.transitions[i];
    const prog = S.compiled && S.compiled.programs[i];
    marks.push({ bar: tr.start, label: `出 ${tr.start}`, color: "#ff9a3c" });
    if (prog) marks.push({ bar: tr.start + prog.len / 4, label: "A 止", color: "#ff5d5d" });
  } else if (m.ending.mode === "fade" && m.ending.bar) {
    marks.push({ bar: m.ending.bar, label: "淡出", color: "#ff5d5d" });
  }
  const c = h("canvas", { class: "wave" });
  holder.innerHTML = "";
  holder.append(c);
  const w = new Wave(c, { height: 120 });
  w.setData(wf, detail.grid, detail.sections, marks);
}

const TECH_LABEL = { longhand: "长交接", instant: "瞬时切换", custom: "自定义" };

function transCard(tr, i) {
  const paramsInput = h("input", { value: JSON.stringify(tr.params || {}), style: "width:100%;font-family:var(--mono);font-size:12px",
    onchange: (e) => guard(() => { tr.params = JSON.parse(e.target.value || "{}"); changed(); }) });
  return h("div", { class: "card trans" },
    h("div", { class: "head" },
      h("b", {}, `过渡 ${i + 1}`),
      h("select", { onchange: (e) => { tr.technique = e.target.value; changed(); } },
        Object.entries(TECH_LABEL).map(([k, v]) => h("option", { value: k, selected: tr.technique === k }, v))),
      h("label", {}, "A 第", numInput(tr, "start", { width: 64 }), "小节开始"),
      h("label", {}, "B 从第", numInput(tr, "b_in", { width: 64 }), "小节进"),
      h("label", {}, "长", numInput(tr, "bars", { placeholder: "默认", width: 60 }), "小节"),
      h("label", {}, "B 在第", numInput(tr, "play_at", { placeholder: "默认", width: 56 }), "小节起播"),
    ),
    h("div", { class: "row", style: "margin-top:6px" }, h("span", { class: "dim" }, "参数"), h("div", { class: "grow" }, paramsInput)),
    Object.keys(tr.lanes || {}).length || (tr.events || []).length
      ? h("div", { class: "dim mono", style: "font-size:12px;margin-top:4px" },
        `自定义曲线：${Object.keys(tr.lanes || {}).join(", ") || "无"}；事件：${(tr.events || []).map((e) => e.join("@")).join(", ") || "无"}（在 JSON 里改）`) : null,
    tr.note ? h("div", { class: "dim", style: "margin-top:4px" }, tr.note) : null,
    h("div", { id: "lanes-" + i, style: "margin-top:6px" }),
  );
}

// 自动化曲线预览：音量实线、低频虚线、高频点线；A 橙 B 蓝
function lanesSvg(p) {
  const NS = "http://www.w3.org/2000/svg";
  const W = 1000, H = 120, pad = 14;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "lanes");
  const len = p.len || 1;
  const X = (b) => (b / len) * W;
  const Y = (v) => pad + (1 - Math.max(0, Math.min(1, v))) * (H - 2 * pad);
  const add = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); svg.append(e); return e; };
  for (let b = 0; b <= len; b += 16) add("line", { x1: X(b), x2: X(b), y1: 0, y2: H, stroke: "rgba(255,255,255,.12)" });
  const styles = {
    vol: "", lo: "6 4", hi: "2 3", mid: "10 3 2 3", filter: "1 6", echo: "1 2", reverb: "1 2",
  };
  for (const [name, pts] of Object.entries(p.lanes)) {
    const [deck, param] = name.split(".");
    let norm = (v) => v;
    if (["lo", "mid", "hi"].includes(param)) norm = (v) => Math.min(1, v);
    if (param === "filter") norm = (v) => 1 - Math.abs(v - 0.5) * 2;
    const all = [[0, pts[0][1]], ...pts, [len, pts[pts.length - 1][1]]];
    const d = all.map((pt, k) => `${k ? "L" : "M"}${X(pt[0]).toFixed(1)},${Y(norm(pt[1])).toFixed(1)}`).join(" ");
    const e = add("path", { d, fill: "none", stroke: deck === "A" ? "#ff9a3c" : "#46c2ff", "stroke-width": param === "vol" ? 2.2 : 1.4,
      "stroke-dasharray": styles[param] || "", "vector-effect": "non-scaling-stroke" });
    const tt = document.createElementNS(NS, "title");
    tt.textContent = name;
    e.append(tt);
  }
  if (p.play !== null && p.play !== undefined) add("line", { x1: X(p.play), x2: X(p.play), y1: 0, y2: H, stroke: "#46c2ff", "stroke-width": 2, "vector-effect": "non-scaling-stroke" });
  for (const [b, ev] of p.events) {
    add("line", { x1: X(b), x2: X(b), y1: 0, y2: H, stroke: "#ff5d5d", "stroke-dasharray": "3 3", "vector-effect": "non-scaling-stroke" });
    const t = add("text", { x: Math.min(W - 60, X(b) + 4), y: 11, fill: "#ff8d8d", "font-size": 11 });
    t.textContent = ev;
  }
  const wrap = h("div", {});
  wrap.append(svg, h("div", { class: "dim", style: "font-size:11px" },
    `橙 = A（出），蓝 = B（进）；实线音量、虚线低频、点线高频；竖线 = B 起播 / 事件。共 ${len / 4} 小节，BPM ${p.bpm.map((x) => x[1]).filter((v, k, a) => a.indexOf(v) === k).join("→")}`));
  return wrap;
}

// ------------------------------------------------------------------ 演出

async function renderShow() {
  const root = $("#tab-show");
  if (!root) return;
  if (!S.sets.length) S.sets = await api("GET", "/api/sets").catch(() => []);
  const prepared = S.state && S.state.prepared;
  if (!S.showSetId) S.showSetId = (prepared && prepared.set) || S.setId || (S.sets[0] && S.sets[0].id);
  await loadShowCompiled();
  const playing = S.state && S.state.playing;
  const busy = playing || (S.job && S.job.state === "running");
  const isPrepared = prepared && prepared.set === S.showSetId;
  const full = S.showCompiled && S.showCompiled.set;
  root.innerHTML = "";
  root.append(
    h("div", { class: "panel row" },
      h("select", { disabled: playing, onchange: (e) => { S.showSetId = e.target.value; S.showCompiled = null; renderShow(); } },
        S.sets.map((s) => h("option", { value: s.id, selected: s.id === S.showSetId }, s.name || s.id))),
      h("span", { class: isPrepared ? "badge ok" : "badge warn" }, isPrepared ? `已准备 ${new Date(prepared.at * 1000).toLocaleTimeString()}` : "未准备"),
      h("span", { class: "grow" }),
      h("span", { class: "dim", title: "在模拟器里跑这套 set：真正的引擎脚本，假 Mixxx，没有声音。不需要准备，也不碰 Mixxx。" }, "预演"),
      h("select", { id: "rh-from", disabled: busy }, h("option", { value: 0 }, "从头"),
        (full && !(S.engine || {}).rehearsal ? full.transitions : []).map((tr, i) => h("option", { value: i + 1, selected: S.rhFrom === i + 1 }, `过渡 ${i + 1} 前 8 小节`))),
      h("select", { id: "rh-speed", disabled: busy }, [1, 2, 4, 8].map((v) => h("option", { value: v, selected: v === (S.rhSpeed || 1) }, `${v}×`))),
      h("button", { disabled: busy || !S.showSetId, onclick: (e) => window.busy(e, "启动中…", async () => {
        S.rhFrom = +$("#rh-from").value; S.rhSpeed = +$("#rh-speed").value;
        await api("POST", `/api/sets/${S.showSetId}/rehearse`, { speed: S.rhSpeed, from: S.rhFrom });
        await loadState(); renderShow();
      }) }, "▶ 预演"),
      h("span", { class: "sep" }),
      h("button", { disabled: playing || (S.job && S.job.state === "running"), onclick: () => guard(async () => {
        await api("POST", `/api/sets/${S.showSetId}/prepare`, {}); toast("准备中：会重启 Mixxx，约 1 分钟");
      }) }, "准备"),
      h("button", { class: "primary", disabled: playing || !isPrepared, onclick: (e) => window.busy(e, "启动中…", async () => {
        await api("POST", `/api/sets/${S.showSetId}/play`); await loadState(); renderShow();
      }) }, "▶ 开始"),
      h("button", { class: "danger", disabled: !playing, onclick: (e) => window.busy(e, "停止中…", stopShow) }, "■ 停止"),
    ),
    h("div", { id: "job-show" }),
    h("div", { class: "panel", id: "show-status" }),
    liveRoot,
    h("div", { class: "panel", style: "margin-top:14px" }, h("h3", {}, "运行日志"), h("div", { class: "log", id: "show-log" })),
  );
  live.setSet(S.showCompiled);
  live.onSeek = isBuiltin() ? seekShow : null;
  renderJobPanels();
  renderShowStatus();
  updateDecks();
}

async function loadShowCompiled() {
  const want = showKey();
  if (S.showSetId && (!S.showCompiled || S.showCompiled._key !== want.key)) {
    const r = await api("GET", want.url).catch(() => null);
    // _offset：从第 k 段过渡开始时，截短的 set 里第 1 首其实是原来的第 k 首（编号照原来的显示）
    S.showCompiled = r ? { ...r.compiled, _id: S.showSetId, _key: want.key, _offset: want.offset || 0, set: r.set } : null;
  }
}

// 演出页显示哪份编译结果：预演从第 k 段开始时是截短的那份（progress.index 按它算）
function showKey() {
  const e = S.engine || {};
  // 这一场从第几首第几小节开始（从第 k 段过渡开始、停下后继续）：正在放按引擎报的，停下以后按上一次开始时的
  const last = (S.state && S.state.last_play) || {};
  const r = e.rehearsal ? { set: e.set, track: e.track, bar: e.bar } : last;
  if (r.set === S.showSetId && r.track !== undefined && r.track !== null && r.bar !== undefined && r.bar !== null) {
    return { key: `${r.set}~${r.track}~${r.bar}`, url: `/api/sets/${r.set}?track=${r.track}&bar=${r.bar}`, offset: r.track };
  }
  return { key: S.showSetId, url: "/api/sets/" + S.showSetId };
}

// 实时混音台（live.js）：页面切换时保留同一个实例，波形缓存不用重新下载
const liveRoot = h("div", { class: "live", id: "live" });
const live = new LiveView(liveRoot, async (id) => {
  const [t, wave] = await Promise.all([api("GET", "/api/tracks/" + id), api("GET", `/api/tracks/${id}/waveform`).catch(() => null)]);
  return { wave, sections: t.sections || [] };
});

// 整套时间表：独奏 / 过渡 / 终曲按时长排开，当前所在段高亮
function setStrip(p) {
  const comp = S.showCompiled;
  if (!comp || !comp.schedule || !comp.schedule.length) return null;
  const total = comp.duration || comp.schedule[comp.schedule.length - 1].t1 || 1;
  const curKind = p.stage === "transition" ? "transition" : p.stage === "ending" ? "ending" : "solo";
  const curIdx = p.stage === "transition" ? (p.program === "end" ? -1 : p.index) : p.index;
  return h("div", { class: "timeline", style: "margin-top:6px" }, comp.schedule.map((sg) => {
    const on = !["done", "stopped", "error", "idle"].includes(p.stage) && sg.kind === curKind && sg.index === curIdx;
    const label = sg.kind === "solo" ? `#${sg.index + 1 + (comp._offset || 0)}` : sg.kind === "ending" ? "终曲" : `↗ ${TECH_LABEL[sg.label] || sg.label}`;
    return h("div", { class: sg.kind + (on ? " now" : ""), style: `width:${((sg.t1 - sg.t0) / total) * 100}%`,
      title: `${label} ${fmt(sg.t0)}–${fmt(sg.t1)}` }, label);
  }));
}

const STAGE_LABEL = {
  idle: "空闲", starting: "连接引擎", loading: "装载下一首", armed: "已就位，等待过渡", solo: "主奏中",
  transition: "过渡中", ending: "终曲淡出", done: "整套播完", stopping: "淡出停止中", stopped: "已停止", error: "出错停止",
};

function renderShowStatus() {
  const el = $("#show-status");
  if (!el) return;
  const p = S.progress;
  el.innerHTML = "";
  if (!p) { el.append(h("div", { class: "dim" }, "还没有开始播放。真机：先「准备」（重启 Mixxx、部署引擎、写 AutoDJ 队列），再「开始」。不开 Mixxx 也可以「预演」：在模拟器里把这套 set 跑一遍看旋钮怎么动。")); }
  else {
    const set = S.showCompiled && S.showCompiled.set;
    const cur = set && set.tracks[p.index];
    const off = (S.showCompiled && S.showCompiled._offset) || 0;
    const e = S.engine || {};
    put(el,
      h("div", { class: "row status-line" },
        h("span", { class: "stage", style: p.stage === "error" ? "color:var(--err)" : "" }, STAGE_LABEL[p.stage] || p.stage),
        e.rehearsal ? h("span", { class: "badge ok", title: e.link === "mixxx-engine"
          ? "内置的 Mixxx 引擎（Mixxx 2.5.6 的引擎核心）在跑 DJStudio.js，旋钮 / 推子是 Mixxx 控件读回的值"
          : "模拟器里跑的是真正的引擎脚本 DJStudio.js，旋钮 / 推子是模拟器里 Mixxx 控件的值" }, soundText(e)) : null,
        h("span", { class: "dim" }, p.message || ""),
        h("span", { class: "grow" }),
        h("span", {}, `第 ${p.index + 1 + off} / ${p.n_tracks + off} 首`, cur ? ` · ${cur.title || cur.id}` : "")),
      p.error ? h("div", { style: "color:var(--err);margin-top:6px" }, p.error) : null,
      setStrip(p),
    );
  }
  const log = $("#show-log");
  if (log) {
    log.innerHTML = "";
    for (const line of S.log.slice(-300)) log.append(h("div", { class: line.includes("!!") ? "e" : "" }, line));
    log.scrollTop = log.scrollHeight;
  }
  updateDecks();
}

let showReloading = false;
function updateDecks() {
  if (!liveRoot.isConnected) return;
  const e = S.engine || {};
  if (S.showCompiled && S.showCompiled._key !== showKey().key && !showReloading) {
    showReloading = true;
    (S.tab === "home" ? renderHome(true) : renderShow()).finally(() => { showReloading = false; });
    return;
  }
  live.setBanner(!e.rehearsal && e.connected && !e.online && S.state && S.state.playing ? "引擎失联：画面停在最后一帧" : "");
  live.update(e, S.progress);
}

// ------------------------------------------------------------------ 体检

async function renderDoctor() {
  const root = $("#tab-doctor");
  root.innerHTML = "";
  const table = h("table", {});
  root.append(
    h("div", { class: "panel" },
      h("div", { class: "row" }, h("h2", { class: "grow" }, "体检"),
        h("button", { onclick: renderDoctor }, "重新体检"),
        h("button", { onclick: () => guard(async () => { await api("POST", "/api/engine/reconnect"); toast("已重连 IAC"); renderDoctor(); }) }, "重连引擎"),
        h("button", { class: "primary", onclick: () => guard(async () => { await api("POST", "/api/probe"); toast("真机探测中…"); }) }, "真机探测")),
      table),
    h("div", { id: "job-doctor" }),
    h("div", { class: "panel" }, h("h3", {}, "一次性设置（Mixxx）"), h("ol", {},
      h("li", {}, "「音频 MIDI 设置」→ 显示 MIDI 工作室 → IAC 驱动程序 → 勾选「设备在线」。"),
      h("li", {}, "Mixxx → 偏好设置 → 曲库 → 添加托管曲库文件夹（默认 ~/Music/DJStudio）。"),
      h("li", {}, "先点一次「准备」或「同步到 Mixxx」把引擎脚本部署进 Mixxx，然后 Mixxx → 偏好设置 → 控制器 → IAC 总线 → 映射选「DJ Studio」→ 启用 → 确定。"),
      h("li", {}, "Mixxx → 偏好设置 → 效果：效果单元 1 第一个槽放 Echo，效果单元 2 第一个槽放 Reverb（过渡里的 echo / reverb 用它们）。"),
      h("li", {}, "往 deck1 拖一首已分析的歌（不用播放），点「真机探测」，把结果发给 Claude。"),
    )),
  );
  renderJobPanels();
  const rows = await api("GET", "/api/doctor").catch((e) => [{ name: "体检", level: "error", detail: e.message }]);
  const cls = { ok: "ok", warn: "warn", error: "err" };
  table.append(h("tbody", {}, rows.map((r) => h("tr", {},
    h("td", {}, h("span", { class: "badge " + cls[r.level] }, r.level)), h("td", {}, r.name), h("td", { class: "dim" }, r.detail)))));
}

// ------------------------------------------------------------------ 启动

// 退出 DJ Studio：应用没有窗口，从网页退出
$("#quit-link").addEventListener("click", (e) => {
  e.preventDefault();
  if (!confirm("退出 DJ Studio？正在演出的会停下。")) return;
  fetch("/api/quit", { method: "POST" }).catch(() => {}).finally(() => {
    document.body.innerHTML = '<div class="home" style="margin-top:80px"><h1>DJ Studio 已退出</h1><div class="dim">可以关掉这个网页了。再打开 DJ Studio 就会回来。</div></div>';
  });
});

(async function main() {
  await guard(loadState);
  await guard(loadTracks);
  connectEvents();
  route();
})();
