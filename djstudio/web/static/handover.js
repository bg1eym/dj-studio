// 交接说明（打印 / 存 PDF）：/static/handover.html?set=<项目 id>
//
// 和演出页用同一份 chart（GET /api/sets/<pid>）、同一套画法（chart5.js），只是画成 SVG，打印出来是矢量的。
// 版式：封面（标题、版本、整场结构、素材 / 片段）→ 每个场景一节（策略、四 Deck 共轴图、动作、练习重点）。
// 一条共轴图最多 32 小节；更长的场景分成连续的几条，每条都重复标尺、Deck 的行头（颜色 / 编号 / 线型 / 角色）
// 和每个参数在这一条起点的值。

import {
  METRICS, PRINT_THEME, BEATS, SvgPainter, layout, packBands, placeLaneLabels, drawChart, drawGutter, rulerStep,
  posText, barOf, durText, tempoAt, fmtBpm, sceneRoles, sceneActions, practicePoints, deckStyle,
} from "./chart5.js";

const M = METRICS.print, T = PRINT_THEME;
const NS = "http://www.w3.org/2000/svg";
const PAGE_W = 1032; // A4 横版去掉左右各 12mm = 273mm ≈ 1032px：SVG 的 1 个单位印出来就是 1px（0.75pt），12px 的字 = 9pt
const GUTTER = 150;
const MAX_BARS = 32; // 一条最多画这么多小节，再多小节号和旋钮位就挤得看不清
const MAX_BAR_PX = 58; // 短的场景也不把一小节拉得太宽
const STRATEGY = ["作用", "保留", "前景", "LOW", "MID/HIGH"];
const KIND_ZH = { load: "装载", start: "起播", stop: "停", loop: "Loop", loop_out: "出 Loop", jump: "跳段", knob: "旋钮", fx: "效果", fx_off: "效果", tempo: "速度", mark: "备注" };

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}

const chip = (d) => h("span", { class: `chip d${d}` }, String(d));

function dash(d, w = 40) {
  const s = document.createElementNS(NS, "svg");
  for (const [k, v] of Object.entries({ viewBox: `0 0 ${w} 6`, width: w, height: 6, class: `dash d${d}` })) s.setAttribute(k, v);
  const l = document.createElementNS(NS, "line");
  for (const [k, v] of Object.entries({ x1: 0, y1: 3, x2: w, y2: 3, "stroke-width": 2.2, "stroke-dasharray": deckStyle(d).dash.join(" ") })) l.setAttribute(k, v);
  s.append(l);
  return s;
}

const deckTag = (d) => [chip(d), `D${d}`];
const barRange = (a, b) => `${posText(a)} – ${posText(b)}`;
const barsText = (beats) => durText(beats);

function tempoText(chart, a, b) {
  const t0 = tempoAt(chart, a), t1 = tempoAt(chart, b - 1e-6);
  let lo = Math.min(t0, t1), hi = Math.max(t0, t1);
  for (const t of chart.tempo || []) if (t.at > a && t.at < b) { lo = Math.min(lo, t.bpm); hi = Math.max(hi, t.bpm); }
  if (Math.abs(hi - lo) < 0.05) return fmtBpm(t0);
  return Math.abs(t0 - t1) < 0.05 ? `${fmtBpm(lo)} – ${fmtBpm(hi)}` : `${fmtBpm(t0)} → ${fmtBpm(t1)}`;
}

function lenText(sec) {
  const s = Math.round(sec || 0);
  return `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`;
}

// ------------------------------------------------------------------ 一条共轴图

// 场景分成几条：每条不超过 32 小节，尽量一样长，按 4 小节取整（乐句不被切在中间）
function stripsOf(scene) {
  const total = scene.end - scene.start;
  const bars = total / BEATS;
  const n = Math.max(1, Math.ceil(bars / MAX_BARS - 1e-9));
  const per = n === 1 ? total : Math.ceil(bars / n / 4) * 4 * BEATS;
  const out = [];
  for (let a = scene.start; a < scene.end - 1e-9; a += per) out.push([a, Math.min(scene.end, a + per)]);
  return { strips: out, per };
}

function drawStrip(chart, scene, b0, b1, ppb, roles) {
  const svg = document.createElementNS(NS, "svg");
  const p = new SvgPainter(svg);
  const range = [b0, b1];
  const bands = packBands(p, chart, ppb, M, range);
  const L = layout(chart, M, bands.nFx, bands.nLoop);
  const G = { x0: GUTTER, w: (b1 - b0) * ppb, b0, b1, ppb, rows: L.rows, height: L.height };
  const labels = {};
  for (const r of L.rows) if (r.kind === "param") labels[r.param] = placeLaneLabels(p, chart, r.param, ppb, M, r.h, range);
  drawChart(p, chart, G, T, M, { labels, bands, step: rulerStep(p, ppb, M), anchor: barOf(scene.start), sceneNames: false, range });
  drawGutter(p, chart, G, T, M, roles, GUTTER);
  p.rect(0.5, 0.5, G.x0 + G.w - 0.5, G.height, { stroke: T.gridStrong });
  svg.setAttribute("viewBox", `0 0 ${PAGE_W} ${Math.ceil(L.height) + 1}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `场景 ${scene.no} 第 ${barOf(b0)} 到 ${barOf(b1 - 0.01)} 小节的四 Deck 共轴图`);
  return svg;
}

// ------------------------------------------------------------------ 各节

function legend() {
  return h("div", { class: "legend block" },
    [1, 2, 3, 4].map((d) => h("span", {}, deckTag(d), deckStyle(d).zh, dash(d), deckStyle(d).line)),
    h("span", {}, "斜线 = 渐变，竖线 = 一下到位，横线 = 保持"),
    h("span", {}, "播放带里的小字 = 原曲的小节号（每个片段各数各的）；一格一格的 = loop"),
    h("span", {}, "效果带：深色 = 送出开着，淡色 = 关了以后的尾音"));
}

function cover(chart) {
  const scenes = chart.scenes || [], clips = chart.clips || [];
  const date = new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric" });
  const el = h("section", { class: "sheet" },
    h("h1", {}, `交接说明：${chart.name || chart.id}`),
    h("div", { class: "meta" },
      h("span", {}, "计划版本 ", h("b", { class: "mono" }, chart.exec || "–")),
      h("span", {}, "谱 ", h("b", {}, chart.name || chart.id)),
      h("span", {}, "日期 ", h("b", {}, date)),
      h("span", {}, "全长 ", h("b", {}, `${Math.round(chart.bars || 0)} 小节，约 ${lenText(chart.duration)}`)),
      h("span", {}, "主速 ", h("b", {}, tempoText(chart, 0, chart.length || 1))),
      h("span", {}, h("b", {}, `${scenes.length} 个场景 · ${clips.length} 个片段`))),
    h("div", { class: "dim" }, "这份说明和演出画面是同一次编译出来的：版本号一样，谱、图、引擎执行的计划就是同一份。"),
    legend());

  el.append(h("h3", {}, "整场结构"),
    h("table", {}, h("thead", {}, h("tr", {}, ["场景", "名字", "全场位置", "长度", "主速", "角色", "谱行"].map((t) => h("th", {}, t)))),
      h("tbody", {}, scenes.map((s) => {
        const roles = sceneRoles(chart, s);
        const miss = STRATEGY.filter((k) => !((s.strategy || {})[k] || "").trim() || (s.missing || []).includes(k));
        return h("tr", {},
          h("td", { class: "nw" }, h("b", {}, String(s.no)), s.draft ? h("span", { class: "tag" }, "草稿") : null),
          h("td", {}, s.name || "–", miss.length ? h("span", { class: "dim" }, `（策略待补充：${miss.join("、")}）`) : null),
          h("td", { class: "nw" }, barRange(s.start, s.end)),
          h("td", { class: "nw" }, barsText(s.end - s.start)),
          h("td", { class: "nw" }, tempoText(chart, s.start, s.end)),
          h("td", {}, Object.entries(roles).filter(([, r]) => r !== "待命").map(([d, r]) => h("span", { style: "white-space:nowrap;margin-right:10px" }, chip(d), `D${d} ${r}`))),
          h("td", { class: "num" }, s.line || ""));
      }))));

  // 素材：从片段里归出来（没有片段用的素材不在这份图里）
  const assets = new Map();
  for (const c of clips) {
    if (!assets.has(c.asset)) assets.set(c.asset, { id: c.asset, title: c.title, bpm: c.bpm, clips: [] });
    assets.get(c.asset).clips.push(c.id);
  }
  el.append(h("h3", {}, "素材"),
    h("table", {}, h("thead", {}, h("tr", {}, ["素材", "曲名", "原 BPM", "用在"].map((t) => h("th", {}, t)))),
      h("tbody", {}, [...assets.values()].map((a) => h("tr", {}, h("td", { class: "nw" }, h("b", {}, a.id)), h("td", {}, a.title || "–"),
        h("td", { class: "nw" }, fmtBpm(a.bpm)), h("td", {}, a.clips.join("、")))))));

  el.append(h("h3", {}, "片段"),
    h("table", {}, h("thead", {}, h("tr", {}, ["片段", "Deck", "素材", "角色", "全场起播 – 停止", "长度", "原曲起点", "Loop / 跳段", "备注", "谱行"].map((t) => h("th", {}, t)))),
      h("tbody", {}, clips.map((c) => {
        const segs = (c.segs || []).filter((s) => s.g1 > s.g0);
        const specials = segs.filter((s) => s.loop > 0 || s.kind === "jump").map((s) => s.loop > 0
          ? `${s.id} ${posText(s.g0)} 起 loop ${durText(s.loop)}（原曲 ${s.src_pos} 起）` : `${s.id} ${posText(s.g0)} 跳到原曲 ${s.src_pos}`);
        return h("tr", {},
          h("td", { class: "nw" }, h("b", {}, c.id)),
          h("td", { class: "nw" }, deckTag(c.deck)),
          h("td", {}, `${c.asset} ${c.title || ""}`),
          h("td", { class: "nw" }, c.role || "–"),
          h("td", { class: "nw" }, barRange(c.start, c.stop)),
          h("td", { class: "nw" }, barsText(c.stop - c.start)),
          h("td", { class: "nw" }, segs.length ? segs[0].src_pos : "–"),
          h("td", {}, specials.length ? specials.map((t) => h("div", { class: "nw" }, t)) : "–"),
          h("td", {}, c.note || ""),
          h("td", { class: "num" }, c.line || ""));
      }))));

  const hints = (chart.issues || []).filter((i) => i.level !== "error");
  if (hints.length) {
    el.append(h("h3", {}, "编译时的提示"), h("ul", { class: "practice" }, hints.map((i) => h("li", {},
      i.line ? `第 ${i.line} 行 ` : "", i.where ? `${i.where}：` : "", i.msg, i.fix ? `（${i.fix}）` : ""))));
  }
  return el;
}

function sceneSection(chart, s) {
  const roles = sceneRoles(chart, s);
  const el = h("section", { class: "scene" });
  el.append(h("h2", {}, `场景 ${s.no}${s.name ? "：" + s.name : ""}`, s.draft ? h("span", { class: "tag" }, "草稿") : null,
    h("span", { class: "range" }, `全场 ${barRange(s.start, s.end)} · ${barsText(s.end - s.start)} · 主速 ${tempoText(chart, s.start, s.end)} · 谱第 ${s.line} 行`)));
  const dl = h("dl", { class: "strategy" });
  for (const k of STRATEGY) {
    const v = ((s.strategy || {})[k] || "").trim();
    const missing = !v || (s.missing || []).includes(k);
    dl.append(h("dt", {}, k), h("dd", { class: missing ? "todo" : "" }, missing ? "待补充" : v));
  }
  el.append(dl, h("div", { class: "roles" }, h("span", { class: "dim" }, "角色"),
    Object.entries(roles).map(([d, r]) => h("span", {}, deckTag(d), dash(d, 26), r === "待命" ? h("span", { class: "dim" }, r) : h("b", {}, r)))));

  // 四 Deck 共轴图：一条最多 32 小节
  const { strips, per } = stripsOf(s);
  const ppb = Math.min(MAX_BAR_PX, (PAGE_W - GUTTER - 1) / (per / BEATS)) / BEATS;
  strips.forEach(([b0, b1], i) => {
    el.append(h("figure", { class: "strip" },
      h("figcaption", {}, `全场第 ${barOf(b0)} – ${barOf(b1 - 0.01)} 小节`,
        strips.length > 1 ? h("span", {}, `这个场景 ${barsText(s.end - s.start)}，分 ${strips.length} 条，第 ${i + 1} 条`) : null),
      drawStrip(chart, s, b0, b1, ppb, roles)));
  });

  // 动作：位置、Deck、做什么、谱里第几行
  const acts = sceneActions(chart, s);
  el.append(h("h3", {}, `动作（${acts.length}）`));
  if (acts.length) {
    el.append(h("table", { class: "acts" }, h("thead", {}, h("tr", {}, ["全场位置", "Deck", "类别", "动作", "谱行"].map((t) => h("th", {}, t)))),
      h("tbody", {}, acts.map((a) => h("tr", {},
        h("td", { class: "nw mono" }, a.pos),
        h("td", { class: "nw" }, a.deck ? deckTag(a.deck) : "全场"),
        h("td", { class: "nw" }, KIND_ZH[a.kind] || a.kind),
        h("td", {}, a.text),
        h("td", { class: "num" }, a.line || ""))))));
  } else el.append(h("div", { class: "dim" }, "这个场景里没有动作。"));

  // 练习重点：只列谱上有的
  const pr = practicePoints(chart, s);
  const items = [];
  for (const g of pr.same) items.push(h("li", {}, h("b", {}, `${g.pos} 同一拍：`), g.items.map((a, i) => [i ? "；" : "", a.text])));
  for (const a of pr.loops) items.push(h("li", {}, h("b", {}, `${a.pos} Loop：`), a.text));
  for (const a of pr.fx) items.push(h("li", {}, h("b", {}, `${a.pos} 效果：`), a.text));
  for (const a of pr.jumps) items.push(h("li", {}, h("b", {}, `${a.pos} 跳段：`), a.text));
  el.append(h("div", { class: "block" }, h("h3", {}, "练习重点"),
    items.length ? h("ul", { class: "practice" }, items) : h("div", { class: "dim" }, "这个场景没有同一拍的多 Deck 动作、loop、效果或跳段。")));
  return el;
}

// ------------------------------------------------------------------ 入口

async function main() {
  const doc = document.getElementById("doc");
  document.getElementById("print-btn").addEventListener("click", () => window.print());
  const pid = new URLSearchParams(location.search).get("set");
  const fail = (msg) => { doc.innerHTML = ""; doc.append(h("div", { class: "err" }, msg)); };
  if (!pid) return fail("地址里没有写是哪个项目（?set=项目 id）。从 DJ Studio 首页点「交接说明」进来。");
  let data;
  try {
    const r = await fetch(`/api/sets/${encodeURIComponent(pid)}`);
    data = await r.json();
    if (!r.ok) throw new Error((data && (data.detail || data.error)) || r.statusText);
  } catch (e) {
    return fail(`读不到项目「${pid}」：${e.message}`);
  }
  if (!data.v5 || !data.chart) {
    // 旧谱（v1–v4）的 set：按四 Deck 的编译器转一遍（只读，原来的 set 不动），A / B 两台交替落在 D1 / D2 上，画同样的图
    try {
      const r = await fetch(`/api/sets/${encodeURIComponent(pid)}/v5check`);
      const legacy = await r.json();
      if (!r.ok) throw new Error((legacy && (legacy.detail || legacy.error)) || r.statusText);
      if (!legacy.chart) throw new Error((legacy.diffs || []).slice(0, 3).join("；") || "转不过来");
      data = { v5: true, chart: legacy.chart, legacyDiffs: legacy.n_diffs || 0 };
    } catch (e) {
      return fail(`这个 set 转不成四 Deck 的图：${e.message}`);
    }
  }
  const chart = data.chart;
  if (!chart.clips || chart.ok === false) {
    const errs = (chart.issues || []).filter((i) => i.level === "error");
    return fail("曲谱没编过，先在首页改掉这些再出交接说明：\n" + errs.map((i) => `${i.line ? `第 ${i.line} 行 ` : ""}${i.where || ""}：${i.msg}${i.fix ? `（${i.fix}）` : ""}`).join("\n"));
  }
  document.title = `交接说明 · ${chart.name || chart.id}`;
  doc.innerHTML = "";
  doc.append(cover(chart));
  for (const s of chart.scenes || []) doc.append(sceneSection(chart, s));
  if (!(chart.scenes || []).length) doc.append(h("div", { class: "dim" }, "这份谱没有写场景。"));
  document.body.dataset.ready = "1"; // 截图 / 出 PDF 的脚本等这个
}

main();
