"""四 Deck 调度器（v5）在真正的声音引擎里的冒烟测试 + 基准。构建机（Mac）和开发机（Linux）都跑这一份。

用法：python packaging/macos/smoke_v5.py <djstudio-engine 路径> [--long 秒数]

做三件事，打印出来的数字就是 docs/V5_G0.md 里那张表的来源：
1. 对齐：四台 Deck（四个不同速度、不同相位的脉冲素材）+ loop + 变速 + 发送效果 + 分频的 Trans + Roll + 采样，
   倍速录下主输出，量每个计划重合的脉冲落在第几帧——偏差要在 5 ms 以内（验收 A05 / A06）。
2. 负载：四路压缩音频（有 ffmpeg / afconvert 就用 MP3 / AAC）+ 四个 Echo + 四个 Reverb + 每台一个带分频的处理组 + 总输出处理组，
   量每个音频块的处理时间（平均 / 最大）对预算（512 帧 = 11.6 ms）。
3. 预跑：从中间开始时静音预跑的速度（比实时快多少倍）。
--long：负载那一段按原速放这么多秒（默认 20 秒；长时间稳定性用 3600）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN = Path(sys.argv[1]).resolve()
LONG = float(sys.argv[sys.argv.index("--long") + 1]) if "--long" in sys.argv else 20.0
os.environ["DJSTUDIO_ENGINE_BIN"] = str(BIN)
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from djstudio.compiler import fader_gain  # noqa: E402
from djstudio.v5.caps import BUILTIN, Caps  # noqa: E402
from tests.v5util import SPECS, SR, Run, click_wav, compiled, onsets, project  # noqa: E402

FAILED = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg, flush=True)
    if not cond:
        FAILED.append(msg)


def compress(wav: Path) -> Path:
    """有编码器就把素材压成 MP3 / AAC（真实的 set 是压缩音频）；没有就用 WAV。"""
    if shutil.which("ffmpeg"):
        out = wav.with_suffix(".mp3")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-codec:a", "libmp3lame", "-b:a", "320k", str(out)])
        return out if r.returncode == 0 and out.exists() else wav
    if shutil.which("afconvert"):
        out = wav.with_suffix(".m4a")
        r = subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "256000", str(wav), str(out)])
        return out if r.returncode == 0 and out.exists() else wav
    return wav


tmp = Path(tempfile.mkdtemp(prefix="djstudio-v5-smoke-"))
wavs = {}
for aid, (bpm, first) in SPECS.items():
    wavs[aid] = tmp / f"{aid}.wav"
    click_wav(wavs[aid], bpm, first, 200.0)
full = {"vol": 0.5}  # 推子 5：四个脉冲叠起来不超过满幅（录音是 16 位的，超了会被削平）

# ---------------------------------------------------------------- 1 对齐
p = project(
    [{"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": "17.1", "init": full},
     {"id": "C2", "asset": "A2", "deck": 2, "src": "5.1", "start": "3.1", "stop": "17.1", "init": full},
     {"id": "C3", "asset": "A3", "deck": 3, "src": "2.1", "start": "3.1", "stop": "17.1", "init": full},
     {"id": "C4", "asset": "A4", "deck": 4, "src": "9.1", "start": "3.1", "stop": "17.1", "init": full}],
    tempo=[{"at": "1.1", "bpm": 132}, {"at": "9.1", "bpm": 132}, {"at": "13.1", "bpm": 134, "ramp": True}],
    loops=[{"id": "L1", "clip": "C3", "at": "5.1", "length": "1小节", "until": "9.1"}],
    fx=[{"id": "F1", "kind": "delay", "route": "send", "targets": ["C4"], "start": "7.1", "end": "9.1",
         "params": {"beats": 0.5, "feedback": 0.0}, "send": [{"at": "7.1", "value": 0.5, "step": True}]},
        {"id": "F2", "kind": "trans", "route": "insert", "targets": ["C2"], "start": "9.1", "end": "13.1",
         "params": {"频段": "HIGH", "周期": "1/2拍"}},
        {"id": "F3", "kind": "roll", "route": "insert", "targets": ["C1"], "start": "15.1", "end": "16.1",
         "params": {"拍长": "1拍 → 1/2拍"}},
        {"id": "F4", "kind": "sample", "route": "master-insert", "targets": ["rim"], "start": "13.1", "end": "15.1",
         "params": {"重复": "1拍", "音量": "0.5"}}])
c = compiled(p, 200.0, wavs)
check(c.ok, "示例计划编得过" + ("" if c.ok else "：" + "；".join(str(i) for i in c.errors[:3])))
with Run(c, speed=20, sim=False, record=tmp / "align.wav") as r:
    caps = Caps.from_engine(r.e.call({"cmd": "v5.caps"}))
    check(caps.signature() == BUILTIN.signature(), f"引擎报的能力和 Python 里的对照表一致（{caps.max_decks} 台 Deck，分频延迟 {caps.split_latency} 帧）")
    check(r.latency == BUILTIN.split_latency, f"用到分频的计划：输出晚 {r.latency} 帧 = {r.latency / SR * 1000:.2f} ms")
    r.start()
    r.wait_state("done", 240)
    x = r.audio()
    rc = r.rc()
    bad = [(m["id"], m["st"]) for m in rc if m["st"] != "ok"]
    check(not bad, f"{len(rc)} 条回执全部 ok" + (f"，除了 {bad}" if bad else ""))
    # 四台都在放、没有效果插进来的那一段（第 3–4 小节）：每一拍四个脉冲应该落在同一帧
    on = onsets(x, thr=0.2, gap=int(0.2 * SR * 60 / 134))
    errs = []
    for beat in range(8, 16):
        want = r.frame_of(float(beat))
        near = on[np.abs(on - want) < 0.05 * SR]
        if len(near):
            errs.append(float(near[0] - want))
        seg = x[int(want) - 8:int(want) + int(0.004 * SR)]
        errs.append(float(np.argmax(np.abs(seg) > 0.2) - 8 + int(want) - want) if np.any(np.abs(seg) > 0.2) else 1e9)
    worst = max(abs(e) for e in errs) if errs else 1e9
    check(worst <= 0.005 * SR, f"四路计划重合的脉冲：最大偏差 {worst:.1f} 帧 = {worst / SR * 1000:.3f} ms（要求 ≤ 5 ms）")
    peak = [float(np.abs(x[int(r.frame_of(float(b))):][:200]).max()) for b in range(8, 16)]
    one = 0.4 * fader_gain(0.5)  # 一个脉冲（非小节第 1 拍）过了推子有多大
    check(min(peak) > 3.2 * one, f"四个脉冲叠在一起的峰值 {min(peak):.2f}–{max(peak):.2f}（单个 {one:.3f}：错开了就叠不到四倍）")
    ph = [abs(d["phase_ms"]) for d in r.e.status["decks"]]
    perf = r.e.call({"cmd": "v5.perf"})
    print(f"  每块 {perf['avg_us']:.0f} µs 平均 / {perf['max_us']:.0f} µs 最大（预算 {perf['budget_us']:.0f} µs，{perf['buffers']} 块）", flush=True)

# ---------------------------------------------------------------- 1b Key Lock 的处理延迟（只量、只记，不判过不过）
for keylock in (False, True):
    ck = compiled(project([{"id": "C1", "asset": "A1", "deck": 1, "start": "1.1", "stop": "9.1", "init": full, "keylock": keylock}]),
                  200.0, wavs)
    with Run(ck, speed=20, sim=False, record=tmp / "kl.wav") as r:
        r.start()
        r.wait_state("done", 120)
        x = r.audio()
        on = onsets(x, thr=0.05, gap=int(0.2 * SR * 60 / 132))
        d = [float(on[np.argmin(np.abs(on - r.frame_of(float(b))))] - r.frame_of(float(b))) for b in range(4, 28)]
        w = int(0.05 * SR)  # 能量的重心（变速器会把脉冲抹开，起点早、能量晚）
        cen = []
        for b in range(4, 28):
            i = int(round(r.frame_of(float(b))))
            e = x[i - w:i + w].astype(np.float64) ** 2
            cen.append(float((np.arange(-w, w) * e).sum() / max(e.sum(), 1e-12)))
        ref_cen = cen if not keylock else ref_cen  # noqa: F821  关的时候记下来当基准
        print(f"  Key Lock {'开' if keylock else '关'}（素材 128 BPM 放在 132）：脉冲的起点比计划晚 {np.median(d) / SR * 1000:+.2f} ms"
              f"（{min(d) / SR * 1000:+.2f} … {max(d) / SR * 1000:+.2f}）；能量重心比不开时晚 "
              f"{(np.median(cen) - np.median(ref_cen)) / SR * 1000:+.2f} ms", flush=True)

# ---------------------------------------------------------------- 2 负载
srcs = {aid: compress(w) for aid, w in wavs.items()}
kind = next(iter(srcs.values())).suffix
bars = max(40, int(LONG * 132 / 60 / 4) + 3)
stop = f"{bars}.1"
clips = [{"id": f"C{d}", "asset": f"A{d}", "deck": d, "start": "1.1", "stop": stop, "init": {"vol": 0.6}} for d in range(1, 5)]
effects = []
for d in range(1, 5):
    effects += [
        {"id": f"E{d}", "kind": "echo", "route": "send", "targets": [f"C{d}"], "start": "1.1", "end": stop, "params": {"beats": 0.5, "feedback": 0.6},
         "send": [{"at": "1.1", "value": 0.3, "step": True}], "tail": "1拍"},
        {"id": f"R{d}", "kind": "reverb", "route": "send", "targets": [f"C{d}"], "start": "1.1", "end": stop, "params": {"decay": 0.6},
         "send": [{"at": "1.1", "value": 0.3, "step": True}], "tail": "1拍"},
        {"id": f"T{d}", "kind": "trans", "route": "insert", "targets": [f"C{d}"], "start": "2.1", "end": stop,
         "params": {"频段": "HIGH", "周期": "1/2拍"}},
        {"id": f"H{d}", "kind": "phaser", "route": "insert", "targets": [f"C{d}"], "start": "2.1", "end": stop, "params": {}},
    ]
# 素材只有 200 秒：放得比它长时，每台在 64 小节（两分钟）里循环——圈比读盘的缓存长得多，解码一直在干活
loops = [{"id": f"L{d}", "clip": f"C{d}", "at": "3.1", "length": "64小节", "until": stop} for d in range(1, 5)] if bars > 80 else []
c2 = compiled(project(clips, fx=effects, loops=loops), 200.0, srcs)
check(c2.ok, "负载计划编得过（四路 + 四个 Echo + 四个 Reverb + 四个带分频的处理组）" + ("" if c2.ok else "：" + "；".join(str(i) for i in c2.errors[:3])))
with Run(c2, speed=1, sim=False) as r:
    r.start()
    r.wait_beat(8.0, 60)
    r.e.call({"cmd": "v5.perf"})  # 清掉装载那一段
    t0 = time.time()
    worst = 0.0
    while time.time() - t0 < LONG and r.state() == "running":
        time.sleep(0.5)
        worst = max(worst, max(abs(d["phase_ms"]) for d in r.e.status["decks"]))
    perf = r.e.call({"cmd": "v5.perf"})
    st = r.e.status
    budget = perf["budget_us"]
    n = max(1, perf["buffers"])
    # 最大值会被同一台机器上别的进程打断（构建机不是专用的）：看平均和「超了预算的块数」
    check(perf["buffers"] > 0 and perf["avg_us"] < 0.6 * budget and perf["over"] <= max(2, n // 100),
          f"负载（四路 {kind} 原速 {time.time() - t0:.0f} 秒，{n} 块）：每块平均 {perf['avg_us']:.0f} µs = 预算 {budget:.0f} µs 的 "
          f"{perf['avg_us'] / budget * 100:.1f}%；最大 {perf['max_us']:.0f} µs；超过一半预算 {perf['over_half']} 块，超过预算 {perf['over']} 块")
    check(perf["late"] == 0 and perf.get("underruns", 0) == 0, f"没有晚到的控制步、没有欠载（late {perf['late']}，underruns {perf.get('underruns', 0)}）")
    check(st["master"]["over"] == 0, f"主输出没有过载（峰值 {st['master']['peak']:.1f} dBFS）")
    check(worst < 1.0, f"这一段里四台 Deck 的位置和全场拍的差最大 {worst:.3f} ms（不漂）")
    dup = [m["id"] for m in r.rc()]
    check(len(dup) == len(set(dup)), "没有重复执行的动作（回执编号不重复）")

# ---------------------------------------------------------------- 3 预跑
with Run(c2, speed=1, sim=False) as r:
    t0 = time.time()
    r.start(8.0 * 4 + 64.0, 8.0 * 4)  # 往回预跑 64 拍
    r.wait_state("running", 120)
    dt = time.time() - t0
    real = 64 * 60 / 132
    print(f"  静音预跑 64 拍（{real:.1f} 秒的音乐）用了 {dt:.2f} 秒 = {real / dt:.0f} 倍速（含装载四首）", flush=True)
    check(dt < real, "预跑比实时快")

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("✓ v5 冒烟测试通过" if not FAILED else f"✗ {len(FAILED)} 项没过：" + "；".join(FAILED)), flush=True)
sys.exit(1 if FAILED else 0)
