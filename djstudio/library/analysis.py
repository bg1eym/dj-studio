"""确定性的音频结构分析（只用 numpy）。

产出：
- 三频段波形（给网页画图）：每秒 50 点，low/mid/high/peak 各 0..255
- 自估网格（Mixxx 还没分析时用，也用来交叉核对 Mixxx 的网格）
- 小节相位 downbeat（0..3）+ 置信度
- 逐小节特征：kick 强度、能量
- 段落：intro / breakdown / drop / outro 等，及建议的 cue 小节

注意：这里只做「辅助」——编排方案里的 cue 由人给出；分析结果用来在波形上标小节号、
帮人核对，以及在没有方案时给出建议。研究结论：不要用 kick 相似度自动决定接法。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..grid import BEATS_PER_BAR, Grid

ANALYSIS_VERSION = 2
N_FFT = 1024
HOP = 256
PEAK_RATE = 50  # 波形每秒点数
BANDS = {"low": (30.0, 150.0), "mid": (150.0, 2500.0), "high": (2500.0, 11025.0)}
# 波形显示用的三段：Denon（Engine DJ）三频段波形的分界——蓝 = 低频 ≈20–500 Hz、绿 = 中频 ≈500–2000 Hz、
# 白 = 高频 ≈2 kHz 以上（Engine DJ 用户手册的配色说明；分界是 Engine 社区里的说法，两边会混一点）
WAVE_BANDS = {"low": (20.0, 500.0), "mid": (500.0, 2000.0), "high": (2000.0, 11025.0)}
# 每段画满时的有效值：-6 dBFS（一首响的母带的 kick），中高频按 0.48 份（参照 CDJ-3000 三频段显示的仿制实现，
# cmicali/vibe-libs 的 WaveformLevelMath.h）；分析信号是 0.707·(L+R) 下混，两声道相同时比单声道高 3 dB
WAVE_FULL_RMS = 0.5 * 2 ** 0.5
WAVE_SHARES = {"low": 1.0, "mid": 0.48, "high": 0.48}
# 网格估计的版本：2 = 节拍搜索不再被第一步（自相关）的估计困住。1 只在第一步估计的 ±1% 里找，第一步偏了 1% 以上
# 就找不回来，结果停在搜索范围的边上（How You Move 138 BPM 被估成 137.00）。旧版本的分析会重做
GRID_VERSION = 2
TEMPO_SPAN = 0.04  # 第一步估计上下各 4% 都搜
WAVEFORM_VERSION = 2  # 2 = 三频段（Denon 分界、按满幅标定）；1 = 旧的三段各自拉满


def frame_time(i: np.ndarray | float, sr: int) -> np.ndarray | float:
    """第 i 帧的窗中心时间（秒）。"""
    return (np.asarray(i) * HOP + N_FFT / 2) / sr


def time_frame(t: float, sr: int) -> int:
    return int(round((t * sr - N_FFT / 2) / HOP))


@dataclass
class Frames:
    sr: int
    fps: float
    low: np.ndarray  # 各频段能量（线性功率和）
    mid: np.ndarray
    high: np.ndarray
    flux: np.ndarray  # 全频谱通量（onset 包络）
    low_flux: np.ndarray  # 低频通量（kick）
    wave: dict[str, np.ndarray] | None = None  # 波形显示用的三段（WAVE_BANDS）每帧有效值


def stft_bands(x: np.ndarray, sr: int) -> Frames:
    win = np.hanning(N_FFT).astype(np.float32)
    n = 1 + max(0, (len(x) - N_FFT) // HOP)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    masks = {k: (freqs >= lo) & (freqs < hi) for k, (lo, hi) in BANDS.items()}
    wmasks = {k: (freqs >= lo) & (freqs < hi) for k, (lo, hi) in WAVE_BANDS.items()}
    wave = {k: np.zeros(n, np.float32) for k in WAVE_BANDS}
    lowmask = (freqs >= 30) & (freqs < 130)
    out = {k: np.zeros(n, np.float32) for k in ("low", "mid", "high", "flux", "low_flux")}
    prev = None
    prev_mag = None
    chunk = 2048
    for s in range(0, n, chunk):
        e = min(n, s + chunk)
        idx = (np.arange(s, e) * HOP)[:, None] + np.arange(N_FFT)[None, :]
        idx = np.minimum(idx, len(x) - 1)
        mag = np.abs(np.fft.rfft(x[idx] * win, axis=1)).astype(np.float32)
        p = mag ** 2
        for k, m in masks.items():
            out[k][s:e] = p[:, m].sum(axis=1)
        for k, m in wmasks.items():
            wave[k][s:e] = p[:, m].sum(axis=1)
        lg = np.log1p(100.0 * mag)
        if prev is None:
            prev = lg[:1]
        d = np.diff(np.vstack([prev, lg]), axis=0)
        d = np.maximum(d, 0)
        out["flux"][s:e] = d.sum(axis=1)
        if prev_mag is None:
            prev_mag = mag[:1]
        dl = np.maximum(np.diff(np.vstack([prev_mag, mag]), axis=0)[:, lowmask], 0)
        out["low_flux"][s:e] = dl.sum(axis=1)
        prev = lg[-1:]
        prev_mag = mag[-1:]
    # 功率和 → 这一帧该频段的有效值（Parseval：单边谱 ×2，除以 N 和窗的能量）
    norm = 2.0 / (N_FFT * float(np.sum(win.astype(np.float64) ** 2)))
    wave = {k: np.sqrt(v * norm) for k, v in wave.items()}
    return Frames(sr, sr / HOP, out["low"], out["mid"], out["high"], out["flux"], out["low_flux"], wave)


# ---------------------------------------------------------------- 波形

def waveform(x: np.ndarray, fr: Frames) -> dict[str, Any]:
    dur = len(x) / fr.sr
    n = max(1, int(dur * PEAK_RATE))
    spp = len(x) / n
    edges = (np.arange(n + 1) * spp).astype(int)
    absx = np.abs(x)
    peak = np.maximum.reduceat(absx, edges[:-1]) if len(x) else np.zeros(n)
    out: dict[str, Any] = {"rate": PEAK_RATE, "n": n}
    pk = peak / (peak.max() or 1.0)
    out["peak"] = np.round(pk * 255).astype(int).tolist()
    # 三频段：每个点 = 这 20 ms 里该频段最响的那一帧的有效值（不取平均：kick 会被摊平），按满幅标定
    # （WAVE_FULL_RMS × 份额 = 画满），三段之间的比例是真实的。整首统一一个系数，把最响的 kick 放到满高
    # （响的母带不至于全部顶满），这个系数三段相同，不改变三段的比例。
    out["v"] = WAVEFORM_VERSION
    t_frames = frame_time(np.arange(len(fr.low)), fr.sr)
    idx = np.clip(np.searchsorted(t_frames, np.arange(n) / PEAK_RATE), 0, max(0, len(t_frames) - 1))
    levels = {}
    for k in WAVE_BANDS:
        rms = fr.wave[k] if fr.wave is not None else np.sqrt(getattr(fr, k))
        if len(rms) == 0:
            levels[k] = np.zeros(n)
            continue
        loud = np.maximum.reduceat(rms, idx) if len(idx) else np.zeros(n)
        levels[k] = loud / (WAVE_FULL_RMS * WAVE_SHARES[k])
    top = float(np.percentile(levels["low"], 99.5)) if n else 0.0
    k_track = 1.0 / top if top > 1.0 else 1.0
    for k in WAVE_BANDS:
        out[k] = np.round(np.clip(levels[k] * k_track, 0, 1) * 255).astype(int).tolist()
    return out


# ---------------------------------------------------------------- 自估网格

def _norm(v: np.ndarray) -> np.ndarray:
    v = v - np.median(v)
    v = np.maximum(v, 0)
    return v / (np.percentile(v, 99) or 1.0)


def _comb_best(kick: np.ndarray, period: float, a: int, b: int, ph_step: float) -> tuple[float, float]:
    """kick 包络在 [a, b) 帧里按 period 帧一拍取样：所有相位里拍点上的和最大的那个 → (和, 相位)。"""
    frames_n = len(kick)
    m = np.arange(int(max(0, b - a - 1) / period) + 1)
    phases = np.arange(0, period, ph_step)
    pos = a + phases[:, None] + m[None, :] * period
    inside = pos < min(b, frames_n - 1)
    s = (np.interp(pos, np.arange(frames_n), kick) * inside).sum(axis=1)
    j = int(np.argmax(s))
    return float(s[j]), float(phases[j])


def _windows(kick: np.ndarray, fps: float, seconds: float = 30.0, count: int = 8) -> list[tuple[int, int]]:
    """kick 最多的几段（每段 seconds 秒，不重叠）：粗搜节拍只看这些段，跳过 breakdown。"""
    w = int(seconds * fps)
    n = len(kick)
    if n <= w:
        return [(0, n)]
    starts = list(range(0, n - w + 1, w))
    energy = [float(kick[a:a + w].sum()) for a in starts]
    best = sorted(range(len(starts)), key=lambda i: -energy[i])[:count]
    return [(starts[i], starts[i] + w) for i in sorted(best)]


def estimate_grid(fr: Frames, bpm_range: tuple[float, float] = (100.0, 160.0),
                  diag: dict[str, Any] | None = None) -> tuple[float, float, float]:
    """返回 (bpm, first_beat 秒, 置信度)。techno 恒速假设。diag 给了就填诊断信息（第一步的估计、粗搜结果）。"""
    env = _norm(fr.flux) + 1.5 * _norm(fr.low_flux)
    env = env - env.mean()
    fps = fr.fps
    n = len(env)
    lags = np.arange(int(fps * 60 / bpm_range[1]), int(fps * 60 / bpm_range[0]) + 1)
    f = np.fft.rfft(env, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[: lags[-1] + 2]
    ac /= ac[0] or 1.0
    # 同时看 2 倍、4 倍延迟，抑制半拍 / 双倍速误判
    score = np.array([ac[L] + 0.5 * ac[min(len(ac) - 1, 2 * L)] if 2 * L < len(ac) else ac[L] for L in lags])
    i = int(np.argmax(score))
    L = float(lags[i])
    if 0 < i < len(score) - 1:
        a, b, c = score[i - 1], score[i], score[i + 1]
        den = a - 2 * b + c
        if den:
            L += 0.5 * (a - c) / den
    bpm = 60.0 * fps / L
    conf = float(score[i])

    guess = bpm
    kick = _norm(fr.low_flux) + 0.3 * _norm(fr.flux)
    # 1) 粗搜：第一步估计 ±4%，每 0.05 BPM 一个候选。分段打分（kick 最多的几段 30 秒，每段自己找相位）：
    #    整首一起打分的峰只有 ±0.01 BPM 宽，粗步长会跨过去；分段的峰约 ±0.3 BPM 宽。搜到边上就往外再搜一段
    wins = _windows(kick, fps)

    def coarse(lo: float, hi: float) -> tuple[float, float]:
        best_c, best_s = lo, -1.0
        for cand in np.arange(lo, hi + 1e-9, 0.05):
            period = 60.0 / cand * fps
            sc = sum(_comb_best(kick, period, a, b, 1.0)[0] for a, b in wins)
            if sc > best_s:
                best_c, best_s = float(cand), sc
        return best_c, best_s

    lo, hi = max(bpm_range[0], guess * (1 - TEMPO_SPAN)), min(bpm_range[1], guess * (1 + TEMPO_SPAN))
    c0, _ = coarse(lo, hi)
    if (c0 <= lo + 0.05 and lo > bpm_range[0]) or (c0 >= hi - 0.05 and hi < bpm_range[1]):
        lo2, hi2 = (max(bpm_range[0], lo - guess * TEMPO_SPAN), lo) if c0 <= lo + 0.05 else (hi, min(bpm_range[1], hi + guess * TEMPO_SPAN))
        c1, _ = coarse(lo2, hi2)
        c0 = max((c0, c1), key=lambda c: sum(_comb_best(kick, 60.0 / c * fps, a, b, 1.0)[0] for a, b in wins))
    # 2) 细搜：整首一起，粗搜结果 ±0.15 BPM，每 0.005 一个候选
    best, best_ph, best_period = -1.0, 0.0, 60.0 / c0 * fps
    for cand in np.arange(c0 - 0.15, c0 + 0.15 + 1e-9, 0.005):
        period = 60.0 / cand * fps
        sc, ph = _comb_best(kick, period, 0, n, 0.5)
        if sc > best:
            best, best_ph, best_period = sc, ph, period
    if diag is not None:
        diag.update(tempo_guess=round(float(guess), 3), coarse=round(float(c0), 3))
    # 3) 逐拍局部峰做稳健线性拟合，精修到亚帧
    period = best_period
    bpm = 60.0 * fps / period
    k_idx = np.arange(int(n / period))
    pos = best_ph + k_idx * period
    pos = pos[pos < n - 2]
    w = max(1, int(0.06 * fps))
    xs, ys = [], []
    for k, p in enumerate(pos):
        lo, hi = int(p) - w, int(p) + w + 1
        if lo < 1 or hi >= n:
            continue
        seg = kick[lo:hi]
        j = int(np.argmax(seg))
        if seg[j] < 0.3:
            continue
        jj = lo + j
        # 抛物线插值到亚帧
        a, b, c = kick[jj - 1], kick[jj], kick[jj + 1]
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den else 0.0
        xs.append(k)
        ys.append(jj + off)
    if len(xs) >= 16:
        xs_a, ys_a = np.array(xs, float), np.array(ys, float)
        slope, icpt = np.polyfit(xs_a, ys_a, 1)
        resid = ys_a - (slope * xs_a + icpt)
        keep = np.abs(resid) < 2.5 * (np.median(np.abs(resid)) + 1e-9)
        slope, icpt = np.polyfit(xs_a[keep], ys_a[keep], 1)
        bpm = 60.0 * fps / slope
        period = slope
        first = icpt
    else:
        first = best_ph
    t0 = float(frame_time(first, fr.sr))
    beat = 60.0 / bpm
    t0 = t0 % beat
    return float(bpm), float(t0), conf


# ---------------------------------------------------------------- kick 相位精修

KICK_BAND = (30.0, 150.0)   # kick 的音高从一两百 Hz 扫到 50Hz 左右；上限太低会漏掉起音（偏晚 15–40ms）
KICK_ONSET_FRAC = 0.2       # 起音点 = 包络从谷底升到峰值 20% 处（与 Mixxx 自己放对的网格相差 ≈6ms）


def kick_phase(x: np.ndarray, sr: int, bpm: float, first_beat: float) -> tuple[float, float]:
    """kick 起音相对网格拍点的位置（拍）与显著度（峰 / 谷）。

    STFT 帧（11.6ms）对拍网格来说太粗：不同曲子的网格会因此差出十几毫秒，叠两个 kick 时听得出。
    做法：低频带（软边带通）的解析信号包络（Hilbert，没有 2f 纹波），按网格把每拍前后 1/4 拍切出来，
    取有 kick 的拍求平均；在网格线 -50..+60ms 内找 kick 峰，往回找包络升到 20% 的位置 = 起音点。
    Mixxx 自己放对的网格也在起音点上（RW2023 #10 / #14 实测差 +6.5 / -6.1ms），所以校正后的网格与没校正的一致。
    找不到明显的 kick（显著度 < 1.5）就返回 0，不动网格。
    """
    n = len(x)
    nfft = 1 << (n - 1).bit_length()  # 2 的幂，长度是大质数时 FFT 会慢几十倍
    X = np.fft.rfft(x, nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    edge = 10.0  # 带通两侧 20Hz 的余弦过渡，减轻砖墙滤波的前振铃
    w = (np.sin(0.5 * np.pi * np.clip((f - KICK_BAND[0] + edge) / (2 * edge), 0, 1)) ** 2
         * np.sin(0.5 * np.pi * np.clip((KICK_BAND[1] + edge - f) / (2 * edge), 0, 1)) ** 2)
    Z = np.zeros(nfft, dtype=complex)
    Z[: len(X)] = X * w
    Z[1: nfft // 2] *= 2
    lo = np.abs(np.fft.ifft(Z)[:n])
    hop = max(1, int(round(sr * 0.0005)))
    env = lo[: len(lo) // hop * hop].reshape(-1, hop).mean(axis=1)
    dt = hop / sr
    beat = 60.0 / bpm
    half = int(0.25 * beat / dt)
    segs = []
    for k in range(int(np.ceil(-first_beat / beat)) + 1, int((n / sr - first_beat) / beat)):
        c = int(round((first_beat + k * beat) / dt))
        if c - half >= 0 and c + half < len(env):
            segs.append(env[c - half:c + half])
    if len(segs) < 16:
        return 0.0, 0.0
    S = np.array(segs)
    pk = S.max(axis=1)
    m = S[pk > np.percentile(pk, 40)].mean(axis=0)  # 只取有 kick 的拍（breakdown 不算）
    t = (np.arange(-half, half) + 0.5) * dt
    sel = np.nonzero((t > -0.05) & (t < 0.06))[0]
    i = int(sel[np.argmax(m[sel])])
    j0 = max(1, i - int(0.08 / dt))
    base = float(m[j0:i + 1].min())
    sharp = float(m[i] / base) if base > 0 else 0.0
    if sharp < 1.5:
        return 0.0, sharp
    thr = base + KICK_ONSET_FRAC * (m[i] - base)
    j = i
    while j > j0 and m[j - 1] > thr:
        j -= 1
    a, b = m[j - 1], m[j]
    tj = t[j] - dt * ((b - thr) / (b - a) if b != a else 0.0)
    return float(tj / beat), sharp


# ---------------------------------------------------------------- 小节特征与段落

def beat_features(fr: Frames, grid: Grid) -> np.ndarray:
    """每拍一行：[low, mid, high, kick]（kick = 拍点附近低频通量峰值）。"""
    n = len(fr.low)
    dur = float(frame_time(n, fr.sr))
    k0 = int(np.ceil(grid.time_beat(0)))
    k1 = int(np.floor(grid.time_beat(dur))) - 1
    lowf = _norm(fr.low_flux)
    rows = []
    for k in range(k0, k1 + 1):
        a, b = grid.beat_time(k), grid.beat_time(k + 1)
        ia = max(0, time_frame(a, fr.sr))
        ib = max(ia + 1, time_frame(b, fr.sr))
        w = max(1, int(0.05 * fr.fps))
        kk = lowf[max(0, ia - w): ia + w + 1]
        rows.append([fr.low[ia:ib].mean(), fr.mid[ia:ib].mean(), fr.high[ia:ib].mean(),
                     float(kk.max()) if len(kk) else 0.0, k])
    return np.array(rows, float) if rows else np.zeros((0, 5))


def estimate_downbeat(feat: np.ndarray) -> tuple[int, float]:
    """小节相位：段落变化总落在小节线上（尤其 8/16 小节乐句线）。返回 (downbeat, 置信度 0..1)。"""
    if len(feat) < 32:
        return 0, 0.0
    f = np.log1p(feat[:, :4] / (np.median(feat[:, :4], axis=0) + 1e-9))
    beats = feat[:, 4].astype(int)
    scores = np.zeros(4)
    win = 8
    nov = np.zeros(len(f))
    for i in range(win, len(f) - win):
        nov[i] = float(np.linalg.norm(f[i:i + win].mean(axis=0) - f[i - win:i].mean(axis=0)))
    # 只让明显的段落变化（新颖度局部峰）投票，平稳段里的噪声不参与
    thr = np.median(nov) + 2 * nov.std()
    for i in range(1, len(nov) - 1):
        if nov[i] > thr and nov[i] >= nov[i - 1] and nov[i] >= nov[i + 1]:
            scores[beats[i] % 4] += nov[i] - thr
    if scores.max() <= 0:
        return 0, 0.0
    order = np.argsort(scores)[::-1]
    best, second = scores[order[0]], scores[order[1]]
    conf = float((best - second) / (best + 1e-9))
    return int(order[0]), conf


def bar_features(feat: np.ndarray, grid: Grid) -> list[dict[str, float]]:
    """逐小节 kick 强度与能量（0..1），下标 0 = 第 1 小节。"""
    if len(feat) == 0:
        return []
    beats = feat[:, 4].astype(int)
    energy = feat[:, 0] + feat[:, 1] + feat[:, 2]
    e_ref = np.percentile(energy, 95) or 1.0
    k_ref = np.percentile(feat[:, 3], 90) or 1.0
    b1 = grid.bar1_beat
    nbars = int((beats.max() - b1) // BEATS_PER_BAR) + 1
    out = []
    for bar in range(max(0, nbars)):
        m = (beats >= b1 + bar * 4) & (beats < b1 + bar * 4 + 4)
        if not m.any():
            out.append({"k": 0.0, "e": 0.0})
            continue
        out.append({"k": round(float(min(1.0, np.median(feat[m, 3]) / k_ref)), 3),
                    "e": round(float(min(1.0, energy[m].mean() / e_ref)), 3)})
    return out


def sections(bars: list[dict[str, float]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按 kick 有无切段并打标签；返回 (段落列表, 建议 cue)。小节号 1 起。"""
    if not bars:
        return [], {}
    kick = np.array([b["k"] for b in bars])
    energy = np.array([b["e"] for b in bars])
    has = kick > 0.45
    # 平滑：孤立的 1 小节翻转视为噪声
    for i in range(1, len(has) - 1):
        if has[i - 1] == has[i + 1] != has[i]:
            has[i] = has[i - 1]
    runs: list[tuple[int, int, bool]] = []
    s = 0
    for i in range(1, len(has) + 1):
        if i == len(has) or has[i] != has[s]:
            runs.append((s, i, bool(has[s])))
            s = i
    secs: list[dict[str, Any]] = []
    for j, (a, b, k) in enumerate(runs):
        if k:
            # breakdown 之后 kick 回来 = drop；intro 之后第一次进 kick 只是 groove
            label = "drop" if j >= 2 and not runs[j - 1][2] else "groove"
        else:
            label = "intro" if j == 0 else ("outro" if j == len(runs) - 1 else "breakdown")
        secs.append({"start": a + 1, "end": b + 1, "label": label, "energy": round(float(energy[a:b].mean()), 3)})
    kick_bars = np.nonzero(has)[0]
    cues: dict[str, Any] = {}
    if len(kick_bars):
        cues["first_kick"] = int(kick_bars[0]) + 1
        cues["last_kick"] = int(kick_bars[-1]) + 1
    cues["breakdowns"] = [[s_["start"], s_["end"]] for s_ in secs if s_["label"] == "breakdown"]
    cues["drops"] = [s_["start"] for s_ in secs if s_["label"] == "drop"]
    return secs, cues


def analyze(x: np.ndarray, sr: int, mixxx_grid: tuple[float, float] | None = None,
            bar1: float | None = None, grid_source: str = "mixxx") -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 (分析结果, 波形)。mixxx_grid = (bpm, first_beat)；有就以它为准切小节。
    bar1 = 用户确认过的第 1 小节起点（秒）；给了就不用自估的小节相位。"""
    fr = stft_bands(x, sr)
    wf = waveform(x, fr)
    diag: dict[str, Any] = {}
    own_bpm, own_first_raw, own_conf = estimate_grid(fr, diag=diag)
    # 精修：把自估网格线放到 kick 起音点上（与其他曲子同一标准）
    own_kick, own_kick_sharp = kick_phase(x, sr, own_bpm, own_first_raw)
    own_first = (own_first_raw + own_kick * 60.0 / own_bpm) % (60.0 / own_bpm)
    if mixxx_grid and mixxx_grid[0] > 0:  # 给定的网格：Mixxx 的，或者手动定的（grid_source = "manual"）
        bpm, first, src = mixxx_grid[0], mixxx_grid[1], grid_source
    else:
        bpm, first, src = own_bpm, own_first, "own"
    dur = len(x) / sr
    g = Grid(bpm, first, 0, dur)
    feat = beat_features(fr, g)
    db_est, db_conf = estimate_downbeat(feat)
    db = db_est if bar1 is None else downbeat_from_bar1(g, bar1)
    g = Grid(bpm, first, db, dur)
    bars = bar_features(feat, g)
    secs, cues = sections(bars)
    result = {
        "version": ANALYSIS_VERSION,
        "grid_source": src,
        "bpm": round(bpm, 4),
        "first_beat": round(first, 5),
        "downbeat_est": db_est,
        "downbeat_conf": round(db_conf, 3),
        "bar1": round(g.bar_time(1), 5),
        "own_grid": {"bpm": round(own_bpm, 3), "first_beat": round(own_first, 5), "conf": round(own_conf, 3),
                     "kick_sharpness": round(own_kick_sharp, 2), **diag},
        "grid_version": GRID_VERSION,
        "bars": bars,
        "sections": secs,
        "cues": cues,
    }
    if src == "mixxx":  # 手动定的网格是用户有意改的，不拿自估的去「存疑」它
        result["grid_check"] = grid_agreement(mixxx_grid, (own_bpm, own_first))  # type: ignore[arg-type]
    return result, wf


def downbeat_from_bar1(g: Grid, bar1: float) -> int:
    """第 1 小节起点（秒）→ 该网格下的小节相位 0..3。"""
    return int(round(g.time_beat(bar1))) % BEATS_PER_BAR


def grid_agreement(mixxx: tuple[float, float], own: tuple[float, float]) -> dict[str, Any]:
    """比较 Mixxx 网格和自估网格：BPM 比值（抓 2/3、3/2、2 倍误判）与相位差。"""
    mb, mf = mixxx
    ob, of = own
    ratio = mb / ob if ob else 0
    beat = 60.0 / mb
    ph = ((of - mf) / beat) % 1.0
    ph = ph - 1 if ph > 0.5 else ph
    # 两个 kick 相差超过约 0.04 拍（135 BPM 时 ≈18ms）叠在一起就听得出来
    ok = abs(ratio - 1) < 0.01 and abs(ph) < 0.04
    note = ""
    for r, name in ((2 / 3, "Mixxx 判成了 2/3"), (1.5, "Mixxx 判成了 1.5 倍"), (0.5, "Mixxx 判成了一半"),
                    (2.0, "Mixxx 判成了两倍")):
        if abs(ratio - r) < 0.02:
            note = name
    if not ok and not note:
        if abs(ratio - 1) >= 0.01:
            note = "BPM 与自估不一致"
        elif abs(ph) > 0.35:
            note = f"Mixxx 网格在反拍上（差 {ph:+.2f} 拍）：同步时两个 kick 会错开半拍"
        else:
            note = f"Mixxx 网格与 kick 差 {ph:+.2f} 拍"
    return {"ok": ok, "ratio": round(ratio, 4), "phase": round(ph, 3), "note": note}
