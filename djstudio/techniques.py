"""过渡手法模板：把一段 Transition 展开成完整的自动化（仍是小节 + 人读单位）。

两类手法（来自《职业 DJ 打碟技法研究》对 25 次职业过渡的实测）：

longhand 长交接
  - 时长 ≥ 24 小节（实测 25–57，中位 39；默认 48 = 3 个 16 小节乐句）
  - 不"杀"低频：两边低频在交接窗口里渐变对换，总低频凹陷小
  - 推子与 EQ 解耦：B 先半音量进，交接完成后再推满
  - A 的 HIGH 从 B 进场起每小节缓降 ≈ 0.19 dB
  - A 不拉推子：默认最后几小节淡出 + reverb 尾巴（也可 playout 自然播完）

instant 瞬时切换
  - drop 对 drop：A 在前置 N 小节里做 HPF 扫频 + echo，
    切点前留一个"真空"（默认 1 拍静音），B 从自己的 drop 满音量起播，A 停

custom 自定义
  - 编排方案里逐小节写明的旋钮位，直接用 lanes / events，模板只补最少的默认值

用户在 Transition.lanes 里写的通道整条覆盖模板里的同名通道；events 追加。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .setfile import Point, Transition


class TechniqueError(ValueError):
    pass


@dataclass
class Expanded:
    """展开后的过渡（小节、人读单位）。lanes 的点：[bar, value] 或 [bar, value, "step"]。"""

    bars: float
    play_at: float
    lanes: dict[str, list[Point]] = field(default_factory=dict)
    events: list[Point] = field(default_factory=list)
    tempo: list[Point] = field(default_factory=list)
    a_playout: bool = False  # True：A 自然播完，时间线等 A 停下才结束


DEFAULTS: dict[str, dict[str, Any]] = {
    "longhand": {
        "bars": 48,
        "b_vol_half": 0.7,  # B 进场的半音量
        "b_full_after": None,  # B 进场后多少小节推满；None = min(32, 交接段 2/3)
        "lo_floor_db": -15.0,  # 低频对换的底
        "lo_from": 0.25,  # 低频对换窗口（交接段比例）
        "lo_to": 0.75,
        "a_hi_slope_db": -0.19,  # A HIGH 每小节变化
        "a_mid_end_db": -6.0,
        "a_end": "fade",  # fade | stop | playout
        "a_fade_bars": 4,
        "a_reverb_tail": 0.4,
    },
    "instant": {
        "pre_bars": 4,  # 切点前的扫频小节数
        "post_bars": 1,
        "vacuum_beats": 1,  # 真空长度（拍）；0 = 硬切
        "sweep": "hpf",  # hpf | lpf | none
        "sweep_amount": 0.6,
        "echo_out": 0.5,  # 切点前最后一小节 A 的 echo 湿度；0 = 不加
    },
    "custom": {},
}


def params_for(tr: Transition) -> dict[str, Any]:
    unknown = set(tr.params) - set(DEFAULTS[tr.technique])
    if unknown:
        raise TechniqueError(f"{tr.technique} 不认识的参数：{', '.join(sorted(unknown))}")
    return {**DEFAULTS[tr.technique], **tr.params}


def expand(tr: Transition, a_bpm: float, b_bpm: float, a_bars_left: float | None = None) -> Expanded:
    """a_bpm / b_bpm：A、B 的主控 BPM（play_bpm 或原生）。a_bars_left：A 从 start 到曲末的小节数（playout 用）。"""
    p = params_for(tr)
    if tr.technique == "longhand":
        ex = _longhand(tr, p, a_bars_left)
    elif tr.technique == "instant":
        ex = _instant(tr, p)
    else:
        ex = _custom(tr)

    for name, pts in tr.lanes.items():
        ex.lanes[name] = [list(pt) for pt in pts]
    ex.events.extend([list(e) for e in tr.events])
    ex.lanes.setdefault("B.vol", [[ex.play_at, 1.0, "step"]])

    if tr.tempo is not None:
        ex.tempo = [list(pt) for pt in tr.tempo]
    elif abs(a_bpm - b_bpm) < 0.05:
        ex.tempo = [[0, a_bpm]]
    elif tr.technique == "instant":
        ex.tempo = [[0, a_bpm], [ex.play_at, b_bpm, "step"]]
    else:
        # 在交接段前半程把主控 BPM 从 A 平滑过渡到 B
        mid = ex.play_at + (ex.bars - ex.play_at) / 2
        ex.tempo = [[ex.play_at, a_bpm], [mid, b_bpm]]

    _check(ex)
    return ex


def _longhand(tr: Transition, p: dict[str, Any], a_bars_left: float | None) -> Expanded:
    P = tr.play_at if tr.play_at is not None else 0.0
    a_end = p["a_end"]
    if a_end not in ("fade", "stop", "playout"):
        raise TechniqueError(f"a_end 只能是 fade / stop / playout：{a_end!r}")
    if tr.bars is not None:
        L = tr.bars
    elif a_end == "playout" and a_bars_left:
        L = a_bars_left
    else:
        L = float(p["bars"])
    ov = L - P
    if ov < 8:
        raise TechniqueError(f"长交接的交接段只有 {ov:g} 小节，太短（建议 ≥ 24）")

    half = float(p["b_vol_half"])
    full_after = p["b_full_after"] if p["b_full_after"] is not None else min(32.0, round(ov * 2 / 3))
    full_at = min(P + full_after, L - 4)
    lo0 = P + ov * float(p["lo_from"])
    lo1 = P + ov * float(p["lo_to"])
    floor = float(p["lo_floor_db"])

    lanes: dict[str, list[Point]] = {
        "B.vol": [[P, half, "step"], [full_at, half], [full_at + 4, 1.0]],
        "B.lo": [[P, floor], [lo0, floor], [lo1, 0.0]],
        "A.lo": [[lo0, 0.0], [lo1, floor]],
        "A.hi": [[P, 0.0], [L, max(-24.0, float(p["a_hi_slope_db"]) * ov)]],
        "A.mid": [[lo1, 0.0], [L, float(p["a_mid_end_db"])]],
    }
    events: list[Point] = []
    if a_end == "fade":
        fb = float(p["a_fade_bars"])
        lanes["A.vol"] = [[L - fb, 1.0], [L, 0.0]]
        if p["a_reverb_tail"]:
            lanes["A.reverb"] = [[L - fb, 0.0], [L, float(p["a_reverb_tail"])]]
        events.append([L, "A.stop"])
    elif a_end == "stop":
        events.append([L, "A.stop"])
    return Expanded(bars=L, play_at=P, lanes=lanes, events=events, a_playout=(a_end == "playout"))


def _instant(tr: Transition, p: dict[str, Any]) -> Expanded:
    pre = float(p["pre_bars"])
    P = tr.play_at if tr.play_at is not None else pre
    L = tr.bars if tr.bars is not None else P + float(p["post_bars"])
    vac = float(p["vacuum_beats"]) / 4.0
    cut = P - vac  # A 静音的时刻（小节）
    lanes: dict[str, list[Point]] = {
        "A.vol": [[cut, 1.0], [cut, 0.0, "step"]],
        "B.vol": [[P, 1.0, "step"]],
    }
    sweep = p["sweep"]
    if sweep in ("hpf", "lpf") and P > 0:
        amt = float(p["sweep_amount"]) * (1 if sweep == "hpf" else -1)
        lanes["A.filter"] = [[max(0.0, P - pre), 0.0], [cut, amt]]
    elif sweep != "none":
        raise TechniqueError(f"sweep 只能是 hpf / lpf / none：{sweep!r}")
    if p["echo_out"] and P >= 1:
        lanes["A.echo"] = [[P - 1, 0.0], [cut, float(p["echo_out"])]]
    return Expanded(bars=L, play_at=P, lanes=lanes, events=[[P, "A.stop"]])


def _custom(tr: Transition) -> Expanded:
    if tr.bars is None or tr.play_at is None:
        raise TechniqueError("custom 手法必须写明 bars 和 play_at")
    events: list[Point] = []
    if not any(str(e[1]) == "A.stop" for e in tr.events):
        events.append([tr.bars, "A.stop"])
    return Expanded(bars=tr.bars, play_at=tr.play_at, events=events)


def _check(ex: Expanded) -> None:
    if not (0 <= ex.play_at <= ex.bars):
        raise TechniqueError(f"play_at={ex.play_at:g} 不在时间线 [0, {ex.bars:g}] 内")
    for name, pts in ex.lanes.items():
        xs = [float(pt[0]) for pt in pts]
        if xs != sorted(xs):
            raise TechniqueError(f"{name} 的点没有按小节递增排序")
        if xs and (xs[0] < 0 or xs[-1] > ex.bars + 1e-9):
            raise TechniqueError(f"{name} 有点落在时间线 [0, {ex.bars:g}] 之外")
    for e in ex.events:
        if not (0 <= float(e[0]) <= ex.bars + 1e-9):
            raise TechniqueError(f"事件 {e} 落在时间线之外")
        if str(e[1]).startswith("B.") and float(e[0]) < ex.play_at:
            raise TechniqueError(f"事件 {e} 早于 B 起播（play_at={ex.play_at:g}）")
