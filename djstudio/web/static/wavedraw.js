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
