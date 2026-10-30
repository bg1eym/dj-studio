"""构建机上的声音引擎冒烟测试（只用标准库）：读文件信息、解码、起引擎跑 DJStudio.js、收到状态帧、查声音输出。
用法：python3 smoke_engine.py <djstudio-engine 路径>"""
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN = Path(sys.argv[1]).resolve()
ENV = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "DJSTUDIO_SYSTEM_MAPPINGS": str(BIN.parent / "res" / "controllers"),
       "DJSTUDIO_MAPPING": str(ROOT / "djstudio" / "engine" / "mapping" / "DJStudio.midi.xml")}


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg, flush=True)
    if not cond:
        sys.exit(1)


tmp = Path(tempfile.mkdtemp())
wav = tmp / "kick.wav"
with wave.open(str(wav), "wb") as w:  # 10 秒 128 BPM 的 kick
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(44100)
    frames = bytearray()
    for i in range(44100 * 10):
        t = (i % int(44100 * 60 / 128)) / 44100
        v = math.sin(2 * math.pi * 55 * t) * math.exp(-t * 18) * 0.8
        frames += struct.pack("<hh", int(v * 32767), int(v * 32767))
    w.writeframes(bytes(frames))
m4a = tmp / "kick.m4a"
subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(m4a)], check=True)

for f in (wav, m4a):
    r = subprocess.run([str(BIN), "--probe", str(f)], capture_output=True, env=ENV, timeout=60)
    line = next((ln for ln in r.stdout.decode().splitlines() if ln.startswith("{")), "{}")
    info = json.loads(line)
    check(abs(info.get("duration", 0) - 10) < 0.1, f"--probe {f.suffix}: {info}")
    out = tmp / "mono.f32"
    subprocess.run([str(BIN), "--decode", str(f), str(out), "22050"], check=True, env=ENV, timeout=60)
    n = out.stat().st_size // 4
    check(abs(n - 220500) < 3000, f"--decode {f.suffix} → {n} 个采样（22050 Hz）")

p = subprocess.Popen([str(BIN)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV, text=True)
replies, hex_lines = {}, []
err_lines = []


def read():
    for line in p.stdout:
        line = line.strip()
        if line.startswith("#"):
            d = json.loads(line[1:])
            replies[d.get("_id")] = d
        elif line:
            hex_lines.append(line)


threading.Thread(target=read, daemon=True).start()
threading.Thread(target=lambda: err_lines.extend(p.stderr), daemon=True).start()


def cmd(i, c, timeout=30):
    p.stdin.write("#" + json.dumps({**c, "_id": i}) + "\n")
    p.stdin.flush()
    t0 = time.time()
    while i not in replies and time.time() - t0 < timeout:
        time.sleep(0.05)
    return replies.get(i)


lib = cmd(1, {"cmd": "library", "tracks": [{"id": "k", "path": str(m4a), "bpm": 128.0, "first_beat": 0.0}]}, 60)
check(lib and lib["tracks"][0]["duration"] > 9, f"引擎装进曲目：{lib}")
time.sleep(2)
check(any(h.lower().startswith("f0") for h in hex_lines), f"DJStudio.js 在 Mixxx 的脚本引擎里跑起来了（收到 {len(hex_lines)} 条 SysEx）")
a = cmd(2, {"cmd": "audio"})
print("  声音输出：", a)
if a and a.get("on"):
    # 声卡按自己的时钟取数据：3 秒里取走的应该正好 3 秒（快了 = 采样率不对，声音会变调、和画面错开）
    t0, p0 = time.time(), a["played_ms"]
    time.sleep(3)
    b = cmd(4, {"cmd": "audio"})
    ratio = (b["played_ms"] - p0) / ((time.time() - t0) * 1000)
    check(abs(ratio - 1) < 0.03, f"声卡取数据的速度 = 墙钟的 {ratio:.3f} 倍（设备 {b.get('device')}，"
          f"设备采样率 {b.get('device_rate')}，引擎 {b.get('rate')}，延迟 {b.get('latency_ms', 0):.1f} ms，断音 {b.get('underruns')} 次）")
cmd(3, {"cmd": "quit"})
p.wait(10)
if p.returncode != 0:
    print("".join(err_lines[-30:]))
check(p.returncode == 0, "引擎正常退出")
