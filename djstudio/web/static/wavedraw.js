// 波形的画法：和 Denon Prime 4（Engine OS）一样的蓝白分层，不按 RGB 混色。
// 每一竖条三层叠在一起：外轮廓深蓝（整体音量，主要是低频 / kick），中间浅蓝（中频 + 高频占的比例），
// 白色的芯（高频占的比例：hi-hat、镲片）。kick 是一团深蓝，hat 是白尖，人声 / 合成器是浅蓝。

export const WAVE_COLORS = { lo: "#1f5eff", mid: "#7cbcff", hi: "#ffffff" };

// column(x) → null 或 [半高 a（像素）, lo, mid, high（三段能量，任意同一单位）]
export function drawBlueWave(g, w, mid, column) {
  const pLo = new Path2D(), pMid = new Path2D(), pHi = new Path2D();
  for (let x = 0; x < w; x++) {
    const c = column(x);
    if (!c) continue;
    const [a, lo, mi, hi] = c;
    if (a < 0.4) continue;
    const sum = lo + mi + hi;
    // 以蓝为主：三段一样响时浅蓝约占 6 成、白芯约 1/4；kick（低频占大头）几乎全是深蓝；hat 才冒出大块白
    const am = sum > 0 ? a * Math.min(1, ((mi + hi) / sum) * 0.95) : 0;
    const ah = sum > 0 ? a * Math.max(0, Math.min(1, (hi / sum - 0.15) * 1.4)) : 0;
    pLo.rect(x, mid - a, 1, 2 * a);
    if (am >= 0.4) pMid.rect(x, mid - am, 1, 2 * am);
    if (ah >= 0.4) pHi.rect(x, mid - ah, 1, 2 * ah);
  }
  g.fillStyle = WAVE_COLORS.lo;
  g.fill(pLo);
  g.fillStyle = WAVE_COLORS.mid;
  g.fill(pMid);
  g.fillStyle = WAVE_COLORS.hi;
  g.fill(pHi);
}
