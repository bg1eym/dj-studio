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


def stft_bands(x: np.ndarray, sr: int) -> Frames:
    win = np.hanning(N_FFT).astype(np.float32)
    n = 1 + max(0, (len(x) - N_FFT) // HOP)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    masks = {k: (freqs >= lo) & (freqs < hi) for k, (lo, hi) in BANDS.items()}
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
    return Frames(sr, sr / HOP, out["low"], out["mid"], out["high"], out["flux"], out["low_flux"])


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
    t_frames = frame_time(np.arange(len(fr.low)), fr.sr)
    t_pts = (np.arange(n) + 0.5) / PEAK_RATE
    for k in ("low", "mid", "high"):
        v = np.sqrt(np.interp(t_pts, t_frames, getattr(fr, k)))
        v = v / (np.percentile(v, 99.5) or 1.0)
        out[k] = np.round(np.clip(v, 0, 1) * 255).astype(int).tolist()
    return out


# ---------------------------------------------------------------- 自估网格

def _norm(v: np.ndarray) -> np.ndarray:
    v = v - np.median(v)
    v = np.maximum(v, 0)
    return v / (np.percentile(v, 99) or 1.0)


def estimate_grid(fr: Frames, bpm_range: tuple[float, float] = (100.0, 160.0)) -> tuple[float, float, float]:
    """返回 (bpm, first_beat 秒, 置信度)。techno 恒速假设。"""
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

    # 梳状搜索：在 ±1% BPM 内找 (bpm, 相位) 使拍点上的 kick 包络之和最大；
    # 再用逐拍局部峰做稳健线性拟合精修到亚帧
    kick = _norm(fr.low_flux) + 0.3 * _norm(fr.flux)
    frames = np.arange(n)
    best, best_ph, best_period = -1.0, 0.0, 60.0 / bpm * fps
    for cand in np.arange(bpm * 0.99, bpm * 1.01, 0.01):
        period = 60.0 / cand * fps
        k_idx = np.arange(int((n - 1) / period))
        phases = np.arange(0, period, 0.5)
        pos = phases[:, None] + k_idx[None, :] * period
        s = (np.interp(pos, frames, kick) * (pos < n - 1)).sum(axis=1)
        j = int(np.argmax(s))
        if s[j] > best:
            best, best_ph, best_period = float(s[j]), float(phases[j]), period
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
            bar1: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 (分析结果, 波形)。mixxx_grid = (bpm, first_beat)；有就以它为准切小节。
    bar1 = 用户确认过的第 1 小节起点（秒）；给了就不用自估的小节相位。"""
    fr = stft_bands(x, sr)
    wf = waveform(x, fr)
    own_bpm, own_first_raw, own_conf = estimate_grid(fr)
    # 精修：把自估网格线放到 kick 起音点上（与其他曲子同一标准）
    own_kick, own_kick_sharp = kick_phase(x, sr, own_bpm, own_first_raw)
    own_first = (own_first_raw + own_kick * 60.0 / own_bpm) % (60.0 / own_bpm)
    if mixxx_grid and mixxx_grid[0] > 0:
        bpm, first, src = mixxx_grid[0], mixxx_grid[1], "mixxx"
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
                     "kick_sharpness": round(own_kick_sharp, 2)},
        "bars": bars,
        "sections": secs,
        "cues": cues,
    }
    if src == "mixxx":
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
