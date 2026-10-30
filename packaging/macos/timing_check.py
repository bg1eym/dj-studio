"""对时检查：分析看到的声音 = 引擎实际放出来的声音，拍线落在 kick 起音上。

用一段合成的 128 BPM kick（第一个 kick 在 0.300 秒），分别存成 WAV / MP3（tests/fixtures 里现成的）/ AAC（Mac 上
用 afconvert，Linux 上用 ffmpeg），每种格式：
  1. djstudio-engine --decode（分析用的解码）和 --render（deck 1 原速播放，真正的播放路径）逐点比，错位 ≤ 2 帧；
  2. 解码器相对原始信号的延迟（编码器延迟有没有去掉，只记录）；
  3. 原速单放时引擎输出和原曲一样（差 < -40 dB）——kick 的声音不该被引擎改动；
  4. 按 DJ Studio 的分析算出拍线，必须落在播放时 kick 的起音上（±5 ms）。
用法：python timing_check.py <djstudio-engine 路径>（需要 numpy 和 djstudio 包）
"""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BIN = Path(sys.argv[1]).resolve()
os.environ["DJSTUDIO_ENGINE_BIN"] = str(BIN)
ENV = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_LOGGING_RULES": "*.debug=false"}
SR, BPM, T0, DUR = 44100, 128.0, 0.300, 12.0
BEAT = 60.0 / BPM
FIXTURE_MP3 = ROOT / "tests" / "fixtures" / "kicks128.mp3"
failed = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg, flush=True)
    if not cond:
        failed.append(msg)


def synth() -> np.ndarray:
    """单声道合成信号：每拍一个 kick（150→48 Hz 下扫 + 指数衰减），反拍一个 hi-hat（固定种子的噪声）。"""
    n = int(DUR * SR)
    y = np.zeros(n)
    rng = np.random.default_rng(7)
    hat = rng.standard_normal(int(0.03 * SR)) * np.exp(-np.arange(int(0.03 * SR)) / (0.006 * SR))
    k_len = int(0.25 * SR)
    t = np.arange(k_len) / SR
    f = 48 + 102 * np.exp(-t / 0.03)
    kick = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.09)
    b = 0
    while T0 + b * BEAT < DUR - 0.3:
        s = int(round((T0 + b * BEAT) * SR))
        y[s:s + k_len] += 0.8 * kick
        h = int(round((T0 + (b + 0.5) * BEAT) * SR))
        y[h:h + len(hat)] += 0.15 * hat
        b += 1
    return y


def write_wav(path: Path, y: np.ndarray) -> None:
    pcm = (np.clip(y, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.repeat(pcm, 2).tobytes())


def lag_of(a: np.ndarray, b: np.ndarray, max_lag: int = 4096) -> int:
    """b 相对 a 晚几个采样（b[n + lag] ≈ a[n]）。"""
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    m = 1 << (2 * n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(b, m) * np.conj(np.fft.rfft(a, m)), m)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    vals = np.concatenate([c[: max_lag + 1], c[-max_lag:]])
    return int(lags[int(np.argmax(vals))])


def run(args, timeout=120) -> str:
    r = subprocess.run([str(BIN), *args], capture_output=True, env=ENV, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} 失败：{r.stderr.decode(errors='replace')[-800:]}")
    return r.stdout.decode()


def main() -> int:
    from djstudio.library.analysis import analyze
    from djstudio.library.audio import ANALYSIS_SR, decode_mono, decoder_name

    tmp = Path(tempfile.mkdtemp())
    src = synth()
    wav = tmp / "kicks.wav"
    write_wav(wav, src)
    files = [wav]
    if FIXTURE_MP3.exists():
        files.append(FIXTURE_MP3)
    m4a = tmp / "kicks.m4a"
    if shutil.which("afconvert"):
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(m4a)], check=True)
    elif shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "192k", str(m4a)], check=True)
    if m4a.exists():
        files.append(m4a)
    print(f"解码器：{decoder_name()}；引擎：{BIN}")
    ref = src * math.sqrt(2) * 0.70710678  # 引擎下混：0.7071 × (L + R)，两声道相同
    for f in files:
        name = f.suffix[1:]
        dec_path, ren_path = tmp / f"{name}.dec.f32", tmp / f"{name}.ren.f32"
        run(["--decode", str(f), str(dec_path), str(SR)])
        dec = np.fromfile(dec_path, dtype="<f4").astype(np.float64)
        out = run(["--render", str(f), str(ren_path), "8"], timeout=180)
        info = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
        ren = np.fromfile(ren_path, dtype="<f4").astype(np.float64)
        start = int(round(info["start"]))
        # 1. 播放出来的第 i 点 = 曲目第 start + i 帧；解码结果里同一帧的位置应该一模一样
        skip = SR // 2  # 跳过起播那一下的音量渐入
        seg = ren[skip:skip + 5 * SR]
        lag = lag_of(seg, dec[start + skip:start + skip + 5 * SR])
        # 线性变速器（EngineBufferScaleLinear）插值会晚 1 帧，0.02 ms，听不出来
        check(abs(lag) <= 2, f"{name}：分析用的解码 vs 实际播放 错位 {lag} 帧（{lag / SR * 1000:+.2f} ms），播放起点 {start}")
        # 1b. 原速单放时引擎不该改动声音（EQ 居中、没有效果、keylock 关）：对齐后和解码结果的差 < -40 dB
        a = ren[skip:skip + 5 * SR]
        b = dec[start + skip + lag:start + skip + lag + 5 * SR]
        resid = 10 * np.log10(np.sum((a - b) ** 2) / max(np.sum(b ** 2), 1e-12) + 1e-12)
        check(resid < -40, f"{name}：单放时引擎输出和原曲的差 {resid:.1f} dB（越低越好；kick 不应被改动）")
        # 2. 解码器延迟（相对原始信号）
        d = lag_of(ref[skip:skip + 5 * SR], dec[skip:skip + 5 * SR])
        print(f"  {name}：解码器延迟 {d} 帧（{d / SR * 1000:+.2f} ms）——拍线要跟着它走")
        if name == "wav":
            check(d == 0, "wav 没有延迟")
        # 4. 分析出的拍线 vs 播放时 kick 的起音（T0 + 解码器延迟）
        x = decode_mono(f, ANALYSIS_SR)
        res, _ = analyze(x, ANALYSIS_SR)
        g = res["own_grid"]
        onset = T0 + d / SR
        err = ((g["first_beat"] - onset + BEAT / 2) % BEAT) - BEAT / 2
        check(abs(g["bpm"] - BPM) < 0.05 and abs(err) < 0.005,
              f"{name}：拍线 BPM {g['bpm']}，第一拍 {g['first_beat'] * 1000:.1f} ms，kick 起音 {onset * 1000:.1f} ms，差 {err * 1000:+.1f} ms")
    if failed:
        print(f"\n{len(failed)} 项不对：", *failed, sep="\n  ")
        return 1
    print("\n对时检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
