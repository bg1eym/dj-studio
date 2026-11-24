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
// Engine DJ / Prime 4 的整首波形和滚动波形不是一个画法（Engine DJ 用户手册 5.3.0 第 39 页、Prime 4 用户手册 5.3.0 第 33 页的截图，
// 设置里叫 Track Overview Style，Half / Full 两种，截图上都是 Half）：
// - 半边：从底边往上画，不上下对称；
// - 三个频段摞起来：蓝（低频）在最下面，绿（中频）摞在它上面，白（高频）在最上面；
// - 放过的部分暗一档，没放的是亮的。
// 每段的高怎么定没有公开的说法。Engine 的数据是每个点三个频段各一个值、最后再存每段的最大值
// （libdjinterop：overview_waveform_data_blob 的 waveform_points + maximum_point），所以这里按「这一段 ÷ 它在整首里的最大值」定高，
// 三段各占三分之一的高——三段都到自己的最大值时正好到顶。这一条是推的，记在 docs/DEVICE_TEST.md 的假设表里。

export const OVERVIEW_DIM = { lo: "#1b3a7a", mid: "#2b7044", hi: "#878d99" }; // 放过的 / 不放的：暗一档

// 一份波形三个频段各自在整首里的最大值（0..255）。很弱的那一段不拉满：不低于最响那一段的 15%（不把底噪放大成一大块）
export function overviewNorm(wf) {
  if (wf._norm) return wf._norm;
  const m = [0, 0, 0];
  for (let i = 0; i < wf.n; i++) {
    if (wf.low[i] > m[0]) m[0] = wf.low[i];
    if (wf.mid[i] > m[1]) m[1] = wf.mid[i];
    if (wf.high[i] > m[2]) m[2] = wf.high[i];
  }
  const floor = Math.max(1, 0.15 * Math.max(m[0], m[1], m[2]));
  return (wf._norm = m.map((v) => Math.max(v, floor)));
}

// 第 ia–ib 帧（不含 ib）这一列的三段：各取最响的那一帧，除以整首的最大值 → [lo, mid, high]，各自 0..1；没有帧 = null
export function overviewColumn(wf, ia, ib) {
  ia = Math.max(0, ia); ib = Math.min(wf.n, ib);
  if (ia >= ib) return null;
  const n = overviewNorm(wf);
  let lo = 0, mi = 0, hi = 0;
  for (let i = ia; i < ib; i++) {
    if (wf.low[i] > lo) lo = wf.low[i];
    if (wf.mid[i] > mi) mi = wf.mid[i];
    if (wf.high[i] > hi) hi = wf.high[i];
  }
  return [Math.min(1, lo / n[0]), Math.min(1, mi / n[1]), Math.min(1, hi / n[2])];
}

// 摞起来以后每段从哪到哪：[[y0, h], …]（y0 = 这一段的上沿，从底边 base 往上摞；height = 满高）
export function overviewStack(c, base, height) {
  const out = [];
  let y = base;
  for (let b = 0; b < 3; b++) {
    const h = (Math.min(1, Math.max(0, c[b])) * height) / 3;
    y -= h;
    out.push([y, h]);
  }
  return out;
}

// column(x) → null 或 [lo, mid, high]（overviewColumn 的结果）；base = 底边的 y，height = 满高；colors = WAVE_COLORS（亮）或 OVERVIEW_DIM（暗）
export function drawOverview(g, w, base, height, column, colors = WAVE_COLORS) {
  const paths = [new Path2D(), new Path2D(), new Path2D()];
  for (let x = 0; x < w; x++) {
    const c = column(x);
    if (!c) continue;
    overviewStack(c, base, height).forEach(([y, h], b) => { if (h >= 0.3) paths[b].rect(x, y, 1, h); });
  }
  [colors.lo, colors.mid, colors.hi].forEach((col, b) => { g.fillStyle = col; g.fill(paths[b]); });
}
