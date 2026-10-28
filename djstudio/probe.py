"""真机探测：逐条验证引擎依赖的 Mixxx 行为（这些在云端 / 模拟器里都无法验证）。

前提：Mixxx 在运行、IAC 总线启用了「DJ Studio」映射、deck1 里放了一首已分析的歌（停着，不用播放）。
输出每一项 ok / warn / error，并给出结论（例如网格位置单位应取哪个值）。
"""
from __future__ import annotations

import time
from contextlib import closing
from typing import Any

from .engine.client import EngineError
from .mixxx.beats import decode

CH1 = "[Channel1]"


def run_probe(studio: Any, job: Any) -> dict[str, Any]:
    results: list[dict[str, str]] = []

    def add(name: str, level: str, detail: str) -> None:
        results.append({"name": name, "level": level, "detail": detail})
        job.say(f"[{level}] {name}：{detail}")

    c = studio.client()
    if not c.wait_until(c.online, 5):
        add("SysEx 引擎→Python", "error", "5 秒内没收到状态帧（映射没启用？SysEx 被丢弃？）")
        return {"results": results}
    add("SysEx 引擎→Python", "ok", f"状态帧正常（{c.link.name}）")

    try:
        ack = c.send({"t": "hello"})
        add("SysEx Python→引擎", "ok", f"命令往返正常，引擎版本 {ack.get('v')}")
    except EngineError as e:
        add("SysEx Python→引擎", "error", f"{e}——试试备用 CC 传输")
    old = c.transport
    try:
        c.transport = "cc"
        c.send({"t": "hello"})
        add("CC 备用传输", "ok", "可用")
    except EngineError as e:
        add("CC 备用传输", "warn", str(e))
    finally:
        c.transport = old

    def get(*keys: tuple[str, str]) -> list[float]:
        return c.send({"t": "get", "keys": [list(k) for k in keys]})["vals"]

    # 控件是否存在：写一个值再读回来（Mixxx 对不存在的控件读出 0）
    for g, k, test in (("[EffectRack1_EffectUnit1]", "mix", 0.37), ("[EqualizerRack1_[Channel2]_Effect1]", "parameter1", 0.61),
                       ("[QuickEffectRack1_[Channel2]]", "super1", 0.42)):
        (orig,) = get((g, k))
        c.send({"t": "set", "g": g, "k": k, "v": test})
        time.sleep(0.1)
        (back,) = get((g, k))
        c.send({"t": "set", "g": g, "k": k, "v": orig})
        ok = abs(back - test) < 1e-3
        add(f"控件 {g},{k}", "ok" if ok else "error", f"写 {test} 读回 {back:g}" + ("" if ok else "（控件不存在或名字不对）"))

    xf, o1, o2 = get(("[Master]", "crossfader"), ("[Channel1]", "orientation"), ("[Channel2]", "orientation"))
    add("推子 / deck 朝向", "ok" if (abs(xf) < 0.01 and o1 == 0 and o2 == 2) else "warn",
        f"crossfader={xf:g}，deck1 朝向={o1:g}，deck2 朝向={o2:g}（期望 0 / 0 / 2）")

    loaded, dur, ts, sr, fbpm, pos, bd = get((CH1, "track_loaded"), (CH1, "duration"), (CH1, "track_samples"),
                                             (CH1, "track_samplerate"), (CH1, "file_bpm"), (CH1, "playposition"),
                                             (CH1, "beat_distance"))
    if not loaded:
        add("deck1 曲目", "warn", "deck1 没有歌：往 deck1 拖一首已分析的歌（停着）再探测，才能验证下面几项")
        return {"results": results}
    dur_calc = ts / 2 / sr if sr else 0
    add("时长控件", "ok" if dur > 0 else "warn",
        f"duration={dur:.3f}s，track_samples/2/samplerate={dur_calc:.3f}s" + ("" if dur > 0 else "（duration 控件不可用，引擎会用后者）"))

    # bpm 控件可写（引擎用它变速）
    (bpm0,) = get((CH1, "bpm"))
    target = round(fbpm * 1.02, 3)
    c.send({"t": "bpm", "deck": 1, "v": target})
    time.sleep(0.2)
    (bpm1,) = get((CH1, "bpm"))
    c.send({"t": "bpm", "deck": 1, "v": bpm0})
    add("bpm 控件可写", "ok" if abs(bpm1 - target) < 0.05 else "error", f"写 {target} 读回 {bpm1:g}")

    # 网格位置单位：用 Mixxx 自己算的 beat_distance 对照我们解码的网格
    d = dur if dur > 0 else dur_calc
    t_now = pos * d
    cands = []
    with closing(studio.mixxx._ro()) as conn:
        rows = conn.execute("SELECT id, title, duration, samplerate, beats, beats_version FROM library "
                            "WHERE abs(duration - ?) < 1.0 AND beats IS NOT NULL", (d,)).fetchall()
    import djstudio.mixxx.beats as B

    verdicts = []
    for r in rows:
        for spp in (2, 1):
            saved = B.SAMPLES_PER_POS
            B.SAMPLES_PER_POS = spp
            try:
                g = decode(r["beats"], r["beats_version"], int(r["samplerate"] or 0))
            finally:
                B.SAMPLES_PER_POS = saved
            if not g:
                continue
            ph = ((t_now - g.first_beat) * g.bpm / 60.0) % 1.0
            err = min(abs(ph - bd), 1 - abs(ph - bd))
            cands.append((err, spp, r["title"], g.bpm))
    if not cands:
        add("网格位置单位", "warn", f"库里找不到时长≈{d:.1f}s 且有网格的歌，无法核对")
    else:
        cands.sort()
        err, spp, title, gb = cands[0]
        verdicts.append(spp)
        lvl = "ok" if err < 0.02 and spp == B.SAMPLES_PER_POS else ("error" if err < 0.02 else "warn")
        add("网格位置单位", lvl,
            f"《{title}》用 SAMPLES_PER_POS={spp} 时 beat_distance 误差 {err:.3f}"
            + ("" if spp == B.SAMPLES_PER_POS else f"——需要把 djstudio/mixxx/beats.py 的 SAMPLES_PER_POS 改成 {spp}")
            + ("" if err < 0.02 else "（误差偏大，结论不可靠：换一首、把播放位置拖到曲中再试）"))
    return {"results": results}
