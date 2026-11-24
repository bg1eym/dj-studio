// 波形的画法：Denon Prime 4（Engine DJ）的三频段波形（Tri-Band），不按 RGB 混色。
// 三个频段各是一条独立的波形，高度 = 这一段自己的音量（分析时按 Denon 的分界 ≈500 Hz / 2 kHz 算、按满幅标定），
// 按 蓝（低）→ 绿（中）→ 白（高）的顺序直接叠上去，后画的盖住先画的。低频最高到满高、中频 3/4、高频一半，
// 所以三段套在一起：低频在最外面（kick 是一大块蓝），中频在里面，高频（hi-hat、镲片）是白色的尖。
// 颜色是 Engine DJ 的蓝、绿、白（Engine DJ 用户手册：蓝 = 低频、绿 = 中频、白 = 高频）。

export const WAVE_COLORS = { lo: "#2f69e0", mid: "#4cdf80", hi: "#ffffff" };
const CEILING = [1.0, 0.75, 0.5];

// column(x) → null 或 [lo, mid, high]：三段的音量，0..1（已经乘上 EQ 等增益，超过 1 按 1）；amp = 满高的半高（像素）
export function drawTriBand(g, w, mid, amp, column) {
  const paths = [new Path2D(), new Path2D(), new Path2D()];
  for (let x = 0; x < w; x++) {
    const c = column(x);
    if (!c) continue;
    for (let b = 0; b < 3; b++) {
      const h = Math.min(1, Math.max(0, c[b])) * CEILING[b] * amp;
      if (h >= 0.35) paths[b].rect(x, mid - h, 1, 2 * h);
    }
  }
  const colors = [WAVE_COLORS.lo, WAVE_COLORS.mid, WAVE_COLORS.hi];
  for (let b = 0; b < 3; b++) {
    g.fillStyle = colors[b];
    g.fill(paths[b]);
  }
}

// ------------------------------------------------------------------ 整首波形（Track Overview）
// Prime 4 / Engine DJ 的整首波形和滚动波形不是一个画法，也不是滚动波形切一半。
// 依据：Prime 4 用户手册 5.3.0 第 33 页里嵌的 1280×800 原始屏幕截图（Engine DJ 用户手册 5.3.0 第 39 页的图同），把概览那一条放大了逐列量：
// - 每一列从底边往上都是蓝、绿、白三层摞着，三层在每一列里都有；白的最薄（中位 4–5 像素），蓝 9–10、绿 6–10 像素。
//   三层都在、白的又一直很薄 → 是三个频段的音量加在一起（摞起来），比例是真实的（哪一段响哪一层就厚），不是三段各画各的、也不是各自拉满；
// - 上沿比较平滑：一个点管一小段时间（Engine 的数据整首只有 1024 个点，每个点三个频段各一个值——libdjinterop 的
//   overview_waveform_data_blob），不是取尖峰；
// - 放过的部分暗一档（蓝 #1066EF → #043263），没放的是亮的。
// 所以这里：每一列取这一小段时间（不短于 smooth 秒）里三段各自的平均值，三段直接相加；整首统一一个比例——最高的那一列画满。
// 每个频段的音量 Engine 怎么算没有资料（这里用本应用分析出来的三段），记在 docs/DEVICE_TEST.md 的假设表里。

export const OVERVIEW_DIM = { lo: "#0b3068", mid: "#1c6a42", hi: "#7b828d" }; // 放过的 / 不放的：暗一档

// 每一列的三段（已经除过整首最高那一列的总和）：cols[x] = [lo, mid, high]，三个加起来 ≤ 1；曲子以外 = null
export function overviewColumns(wf, w, dur, smooth = 0.4) {
  if (!wf._ps) { // 前缀和：取一段的平均不用每次重加
    const ps = [new Float64Array(wf.n + 1), new Float64Array(wf.n + 1), new Float64Array(wf.n + 1)];
    for (let i = 0; i < wf.n; i++) { ps[0][i + 1] = ps[0][i] + wf.low[i]; ps[1][i + 1] = ps[1][i] + wf.mid[i]; ps[2][i + 1] = ps[2][i] + wf.high[i]; }
    wf._ps = ps;
  }
  const ps = wf._ps, half = (smooth * wf.rate) / 2, cols = new Array(w).fill(null);
  let top = 0;
  for (let x = 0; x < w; x++) {
    const fa = ((x / w) * dur) * wf.rate, fb = (((x + 1) / w) * dur) * wf.rate, mid = (fa + fb) / 2;
    if (fa >= wf.n) break;
    const ia = Math.max(0, Math.floor(Math.min(fa, mid - half))), ib = Math.min(wf.n, Math.max(ia + 1, Math.ceil(Math.max(fb, mid + half))));
    const c = [0, 1, 2].map((b) => (ps[b][ib] - ps[b][ia]) / (ib - ia) / 255);
    cols[x] = c;
    top = Math.max(top, c[0] + c[1] + c[2]);
  }
  if (top > 0) for (const c of cols) if (c) { c[0] /= top; c[1] /= top; c[2] /= top; }
  return cols;
}

// 一列摞起来以后每段的上沿和高：[[y, h] × 3]，从底边 base 往上——低频贴着底边，中频接在它上面，高频在最上面
export function stackColumn(c, base, height) {
  const out = [];
  let y = base;
  for (let b = 0; b < 3; b++) { const h = Math.max(0, c[b]) * height; y -= h; out.push([y, h]); }
  return out;
}

// cols = overviewColumns 的结果；base = 底边的 y，height = 满高；colors = WAVE_COLORS（亮）或 OVERVIEW_DIM（暗）
export function drawStack(g, cols, base, height, colors = WAVE_COLORS) {
  const paths = [new Path2D(), new Path2D(), new Path2D()];
  cols.forEach((c, x) => { if (c) stackColumn(c, base, height).forEach(([y, h], b) => { if (h > 0.05) paths[b].rect(x, y, 1, h); }); });
  [colors.lo, colors.mid, colors.hi].forEach((col, b) => { g.fillStyle = col; g.fill(paths[b]); });
}
