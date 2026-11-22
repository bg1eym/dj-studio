"""时间线编译器：项目（djset/3）+ 每份素材的网格 → 确定的执行计划（djplan/1）。

相同的输入得到相同的计划。计划在准备阶段整份交给引擎里的原生调度器（tools/mixxx-engine/djs5），
演出时浏览器和 Python 都不参与逐拍调度。这里做的事：

- 全场速度图（TempoMap）：BPM 对全场拍分段线性，秒数按 BPM 曲线积分（和引擎里 core.hpp 的 TempoMap 同一套公式）；
- 片段的源位置分段：起播、loop（源位置回绕、全场照常往前）、出 loop（从实际读到的地方接着放）、跳段；
- Deck 占用：同一台 Deck 上片段的播放区间不重叠，准备（装载）区间也算在内；装不下就报错，让写谱的人换 Deck 或留出间隔；
- 参数曲线：人读单位（dB / 推子位置 / Filter）→ Mixxx 原生值，和 djset/2 的编译器同一个换算（compiler._lane_native）；
- 效果：检查参数、算尾音、给每个发送效果分一个实例（同一时间一个实例只给一个效果）；
- 能力检查：谱要的东西引擎没有 → 指出场景、目标和行号，不略过；
- 提示（不是错误）：多个低频来源、两条声线、中频可能拥挤——只按旋钮位估计，写成「可能」，可以在场景里标成有意安排。

错误和提示分开：错误 = 这份谱不能演出；提示 = 值得看一眼。两类都带谱里的行号。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable

from ..compiler import _lane_native, to_native
from ..grid import BAR1_TOL, Grid
from ..setspec import knob_text
from .caps import BUILTIN, Caps
from .model import FX_SEND, PARAMS, Clip, Fx, Jump, Loop, Project, Scene
from .pos import BEATS_PER_BAR, fmt_dur, fmt_pos

COMPILER_VERSION = "5.0.0"
PREP_SECONDS = 4.0  # 不写准备时间时：起播前这么久开始装
MIN_PREP_SECONDS = 1.0  # 再短就来不及装了（解码器起来、读盘线程把起播点读进缓存）
RELEASE_SECONDS = 0.15  # 同一台 Deck：上一个片段停下以后过这么久才能装下一个（门关完、deck 停稳）
MIN_LOOP_BEATS = Fraction(1, 8)  # loop 最短 1/8 拍（再短，出 loop 的那一下落不进最后一遍）
PARAM_ZH = {"vol": "推子", "lo": "LOW", "mid": "MID", "hi": "HIGH", "filter": "Filter"}

# 发送效果的参数：名字 → (默认, 最小, 最大)。Delay 和 Echo 是同一个算法，差在默认反馈（Delay 只回一次）
FX_PARAMS: dict[str, dict[str, tuple[float, float, float]]] = {
    "delay": {"beats": (0.5, 0.125, 2.0), "feedback": (0.0, 0.0, 0.95), "pingpong": (0.0, 0.0, 1.0),
              "send_gain": (1.0, 0.0, 1.0)},
    "echo": {"beats": (0.5, 0.125, 2.0), "feedback": (0.7079, 0.0, 0.95), "pingpong": (0.0, 0.0, 1.0),
             "send_gain": (0.7079, 0.0, 1.0)},
    "reverb": {"decay": (0.5, 0.0, 1.0), "bandwidth": (1.0, 0.0, 1.0), "damping": (0.0, 0.0, 1.0),
               "send_gain": (1.0, 0.0, 1.0)},
}
FX_ZH = {"delay": "Delay", "echo": "Echo", "reverb": "Reverb", "trans": "Trans", "roll": "Roll",
         "filter_echo": "Filter Echo", "spiral": "Spiral", "noise": "Noise", "flanger": "Flanger", "phaser": "Phaser",
         "filter": "Filter", "sample": "采样", "release": "Release"}


# ---------------------------------------------------------------- 速度图

class TempoMap:
    """全场速度图。points：[(拍, BPM)]，同一拍两个点 = 一下换速。和引擎里的 TempoMap（core.hpp）同一套公式。"""

    def __init__(self, points: list[tuple[float, float]]) -> None:
        self.b = [float(p[0]) for p in points]
        self.v = [float(p[1]) for p in points]
        self.t = [0.0]
        for i in range(1, len(self.b)):
            L = self.b[i] - self.b[i - 1]
            self.t.append(self.t[-1] + self._seg_seconds(self.v[i - 1], self.v[i], L, L))

    @staticmethod
    def _seg_seconds(v0: float, v1: float, L: float, u: float) -> float:
        if L <= 0 or u <= 0:
            return 0.0
        k = (v1 - v0) / L
        return 60.0 * u / v0 if abs(k) < 1e-12 else 60.0 / k * math.log1p(k * u / v0)

    @staticmethod
    def _seg_beats(v0: float, v1: float, L: float, sec: float) -> float:
        k = (v1 - v0) / L if L > 0 else 0.0
        return sec * v0 / 60.0 if abs(k) < 1e-12 else v0 * math.expm1(k * sec / 60.0) / k

    def _index(self, xs: list[float], x: float) -> int:
        i = 0
        for k in range(len(xs)):
            if xs[k] <= x:
                i = k
        return i

    def bpm_at(self, beat: float) -> float:
        if beat < self.b[0]:
            return self.v[0]
        i = self._index(self.b, beat)
        if i + 1 >= len(self.b):
            return self.v[i]
        L = self.b[i + 1] - self.b[i]
        return self.v[i] + (self.v[i + 1] - self.v[i]) * (beat - self.b[i]) / L if L > 0 else self.v[i + 1]

    def sec_at(self, beat: float) -> float:
        if beat < self.b[0]:
            return (beat - self.b[0]) * 60.0 / self.v[0]
        i = self._index(self.b, beat)
        if i + 1 >= len(self.b):
            return self.t[i] + (beat - self.b[i]) * 60.0 / self.v[i]
        return self.t[i] + self._seg_seconds(self.v[i], self.v[i + 1], self.b[i + 1] - self.b[i], beat - self.b[i])

    def beat_at(self, sec: float) -> float:
        if sec < 0:
            return self.b[0] + sec * self.v[0] / 60.0
        i = self._index(self.t, sec)
        if i + 1 >= len(self.b):
            return self.b[i] + (sec - self.t[i]) * self.v[i] / 60.0
        return self.b[i] + self._seg_beats(self.v[i], self.v[i + 1], self.b[i + 1] - self.b[i], sec - self.t[i])


# ---------------------------------------------------------------- 输入 / 输出

@dataclass
class AssetInfo:
    """编译需要的一份素材的事实：网格（含小节相位和时长）、文件、音量统一的增益。"""

    id: str
    grid: Grid
    path: str | None = None
    gain: float | None = None
    title: str = ""
    track_id: str = ""

    @property
    def bpm(self) -> float:
        return self.grid.bpm

    def beat_time(self, src_beats: float) -> float:
        """这首自己的第 src_beats 拍（从它的 1.1 数，0 起）离文件开头多少秒。"""
        return self.grid.beat_time(self.grid.bar1_beat + src_beats)

    @property
    def last_beat(self) -> float:
        """曲尾是这首自己的第几拍。"""
        return self.grid.time_beat(self.grid.duration) - self.grid.bar1_beat


@dataclass
class Issue5:
    level: str  # error | hint
    code: str
    where: str
    msg: str
    line: int = 0
    fix: str = ""

    def __str__(self) -> str:
        head = f"第 {self.line} 行，" if self.line else ""
        tail = f"（{self.fix}）" if self.fix else ""
        return f"[{'错误' if self.level == 'error' else '提示'}] {head}{self.where}：{self.msg}{tail}"

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "where": self.where, "msg": self.msg, "line": self.line,
                "fix": self.fix, "text": str(self)}


@dataclass
class SegC:
    g: Fraction  # 全场拍
    src: Fraction  # 这首自己的拍（从它的 1.1 数）
    loop: Fraction  # loop 长度（拍）；0 = 一直往下读
    lsrc: Fraction
    id: str
    kind: str  # start | loop | exit | jump
    line: int = 0


@dataclass
class ClipC:
    clip: Clip
    info: AssetInfo
    start: float
    stop: float
    load: float
    gen: int = 0
    segs: list[SegC] = field(default_factory=list)
    lanes: dict[str, list[list[Any]]] = field(default_factory=dict)  # 人读单位：[[拍, 值, "step"?]]（含起播那一拍的锚点）

    def seg_at(self, g: float) -> SegC:
        s = self.segs[0]
        for x in self.segs[1:]:
            if float(x.g) <= g + 1e-9:
                s = x
        return s

    def src_beat_at(self, g: float) -> float:
        """全场第 g 拍时读到这首自己的第几拍。"""
        s = self.seg_at(g)
        d = g - float(s.g)
        if s.loop and d >= 0:
            return float(s.lsrc) + math.fmod(d + float(s.src - s.lsrc), float(s.loop))
        return float(s.src) + d

    def value_at(self, param: str, g: float) -> float:
        """这个旋钮在全场第 g 拍的谱值（人读单位）。"""
        pts = self.lanes.get(param)
        if not pts:
            return float(getattr(self.clip.init, param))
        x = g
        if x < pts[0][0]:
            return float(pts[0][1])
        i = 0
        for k in range(len(pts)):
            if pts[k][0] <= x + 1e-9:
                i = k
        if i + 1 >= len(pts):
            return float(pts[i][1])
        a, b = pts[i], pts[i + 1]
        if len(b) == 3 or b[0] <= a[0]:
            return float(a[1])
        return float(a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]))


@dataclass
class Compiled5:
    project: Project
    caps: Caps
    issues: list[Issue5] = field(default_factory=list)
    tempo: TempoMap | None = None
    clips: list[ClipC] = field(default_factory=list)
    fx: list[dict[str, Any]] = field(default_factory=list)  # 计划里的效果（含 slot、tail、line）
    plan: dict[str, Any] | None = None
    length: float = 0.0  # 全场拍

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    @property
    def errors(self) -> list[Issue5]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def hints(self) -> list[Issue5]:
        return [i for i in self.issues if i.level == "hint"]

    @property
    def duration(self) -> float:
        """整场多少秒。"""
        return self.tempo.sec_at(self.length) if self.tempo else 0.0

    def clip(self, cid: str) -> ClipC | None:
        return next((c for c in self.clips if c.clip.id == cid), None)

    def library_rows(self) -> list[dict[str, Any]]:
        """交给引擎曲库的行（每份素材一行；id = 素材 ID）。"""
        rows, seen = [], set()
        for c in self.clips:
            a = c.info
            if a.id in seen:
                continue
            seen.add(a.id)
            row: dict[str, Any] = {"id": a.id, "dur": a.grid.duration, "bpm": a.grid.bpm, "first_beat": a.grid.first_beat,
                                   "path": a.path}
            if a.gain:
                row["gain"] = round(a.gain, 5)
            rows.append(row)
        return rows

    # ------------------------------------------------------------ 任意一拍的状态（练习起点、界面）

    def prerun_from(self, beat: float) -> float:
        """从全场第 beat 拍开始练习时，要从哪一拍起静音预运行，才能把还在响的效果尾音补出来。
        只看发送效果：送出在 beat 之前开过、尾音到 beat 还没完的；往回最多 caps.restore_max_beats 拍。"""
        start = beat
        for f in self.fx:
            if f["start"] < beat < f["end"] + f["tail"]:
                start = min(start, max(f["start"], beat - f["t80"], beat - self.caps.restore_max_beats))
        return start

    def state_at(self, beat: float) -> dict[str, Any]:
        """全场第 beat 拍时四台 Deck 应该在哪：片段、原曲位置（loop 里是第几遍）、每个旋钮的谱值、还在响的效果。"""
        decks = []
        for d in range(1, self.caps.max_decks + 1):
            cur = next((c for c in self.clips if c.clip.deck == d and c.start <= beat < c.stop), None)
            nxt = next((c for c in sorted(self.clips, key=lambda c: c.start)
                        if c.clip.deck == d and c.load <= beat < c.start), None)
            o: dict[str, Any] = {"deck": d, "clip": None, "state": "idle"}
            if cur:
                s = cur.seg_at(beat)
                sb = cur.src_beat_at(beat)
                o.update(clip=cur.clip.id, asset=cur.clip.asset, state="looping" if s.loop else "playing",
                         source_beat=round(sb, 6), source_pos=fmt_pos(Fraction(sb).limit_denominator(96)),
                         params={p: round(cur.value_at(p, beat), 4) for p in PARAMS})
                if s.loop:
                    o["loop"] = {"id": s.id, "length": fmt_dur(s.loop),
                                 "lap": int((beat - float(s.g) + float(s.src - s.lsrc)) // float(s.loop)) + 1}
            elif nxt:
                o.update(clip=nxt.clip.id, asset=nxt.clip.asset, state="ready")
            decks.append(o)
        fx = [{"id": f["id"], "kind": f["kind"], "clip": f["clip"], "tail": beat >= f["end"]}
              for f in self.fx if f["start"] <= beat < f["end"] + f["tail"]]
        return {"beat": beat, "pos": fmt_pos(Fraction(beat).limit_denominator(96)), "bpm": self.tempo.bpm_at(beat),
                "decks": decks, "fx": fx, "prerun_from": self.prerun_from(beat)}


# ---------------------------------------------------------------- 编译

def _zh_pos(b: Fraction | float) -> str:
    if b < 0:
        return "开演前"
    return fmt_pos(b if isinstance(b, Fraction) else Fraction(b).limit_denominator(96))


def scene_of(p: Project, at: Fraction | float) -> Scene | None:
    return next((s for s in p.scenes if s.start <= at < s.end), None)


def compile_project(p: Project, resolve: Callable[[Any], AssetInfo | None], caps: Caps = BUILTIN) -> Compiled5:
    c = Compiled5(project=p, caps=caps)

    def where(at: Fraction | float | None = None, what: str = "") -> str:
        sc = scene_of(p, at) if at is not None else None
        head = f"场景 {sc.no}" + (f"「{sc.name}」" if sc.name else "") if sc else "全场"
        return f"{head} {what}".strip()

    def err(code: str, w: str, msg: str, line: int = 0, fix: str = "") -> None:
        c.issues.append(Issue5("error", code, w, msg, line, fix))

    def hint(code: str, w: str, msg: str, line: int = 0, fix: str = "") -> None:
        c.issues.append(Issue5("hint", code, w, msg, line, fix))

    if "5" not in caps.score_versions and not p.legacy:
        err("caps_score", "全场", f"这个引擎（{caps.name}）只能放 v1–v4 的谱，放不了 v5 的四 Deck 谱",
            fix="用内置引擎（默认）")
        return c

    # ---- 速度图
    pts: list[tuple[float, float]] = []
    for i, t in enumerate(p.tempo):
        if i and not t.ramp and t.bpm != p.tempo[i - 1].bpm:
            pts.append((float(t.at), p.tempo[i - 1].bpm))
        elif i and t.ramp and t.at == p.tempo[i - 1].at:
            err("tempo", where(t.at, "速度"), f"{_zh_pos(t.at)} 写了渐变，但和上一个点在同一拍", t.line,
                "渐变要有长度：上一行写从哪开始变")
        pts.append((float(t.at), t.bpm))
    c.tempo = tempo = TempoMap(pts)

    # ---- 素材
    infos: dict[str, AssetInfo] = {}
    for a in p.assets:
        info = resolve(a)
        if info is None:
            err("asset_missing", f"素材 {a.id}", f"找不到「{a.file or a.title}」", a.line, "把这首放进谱所在的文件夹再导入")
            continue
        if info.grid.bpm <= 0 or info.grid.duration <= 0:
            err("asset_grid", f"素材 {a.id}", f"「{a.file or a.title}」还没分析（没有拍网格 / 时长）", a.line)
            continue
        infos[a.id] = info
    used = {k.asset for k in p.clips}
    for a in p.assets:
        if a.id not in used:
            hint("asset_unused", f"素材 {a.id}", "素材表里有，但没有片段用它", a.line)

    # ---- 片段：源位置分段、起止
    by_deck: dict[int, list[ClipC]] = {}
    for k in p.clips:
        w = where(k.start, f"片段 {k.id}（D{k.deck}）")
        if k.deck > caps.max_decks:
            err("caps_decks", w, f"用了 D{k.deck}，这个引擎只有 {caps.max_decks} 台 Deck", k.line)
            continue
        info = infos.get(k.asset)
        if info is None:
            continue
        loops = [x for x in p.loops if x.clip == k.id]
        jumps = [x for x in p.jumps if x.clip == k.id]
        segs = _segments(k, loops, jumps, lambda code, msg, line, fix="": err(code, w, msg, line, fix), caps)
        last = segs[-1]
        end_beat = info.last_beat
        if k.stop is None:
            if last.loop:
                err("loop_open", w, f"{last.id} 一直没出：片段不会自己播完", last.line, "给 loop 写出点，或者给片段写全场停止")
                continue
            stop = float(last.g) + (end_beat - float(last.src))
            if stop <= float(k.start):
                err("clip_range", w, f"原曲起点 {_zh_pos(k.src)} 已经在曲尾之后（这首到 {_zh_pos(end_beat)}）", k.line)
                continue
        else:
            stop = float(k.stop)
            if stop <= float(k.start):
                err("clip_range", w, f"全场停止 {_zh_pos(k.stop)} 不在起播 {_zh_pos(k.start)} 之后", k.line)
                continue
        cc = ClipC(clip=k, info=info, start=float(k.start), stop=stop, load=0.0, segs=segs)
        # 原曲范围：每一段直着读的部分不能读过曲尾；起点不能在文件开头之前
        if info.beat_time(float(k.src)) < -BAR1_TOL - 1e-6:
            err("clip_range", w, f"原曲起点 {_zh_pos(k.src)} 在文件开头之前", k.line)
        for i, s in enumerate(segs):
            g_end = float(segs[i + 1].g) if i + 1 < len(segs) else stop
            if g_end > stop + 1e-9:
                err("clip_range", w, f"{s.id} 在 {_zh_pos(s.g)}，片段 {_zh_pos(stop)} 就停了", s.line)
                continue
            far = float(s.lsrc + s.loop) if s.loop else float(s.src) + (g_end - float(s.g))
            if far > end_beat + 1.0:  # 容一拍（时长取整）
                err("clip_range", w, f"要读到原曲 {_zh_pos(far)}，这首只到 {_zh_pos(end_beat)}", s.line or k.line,
                    "把全场停止提前，或者换一段原曲")
        if k.keylock:
            hint("keylock", w, "开了 Key Lock：变速的 Deck 声音会比位置晚约 20–30 ms（没有补偿），两个 kick 叠着时听得出", k.line)
        c.clips.append(cc)
        by_deck.setdefault(k.deck, []).append(cc)

    # ---- Deck 占用和准备区间
    for d, lst in sorted(by_deck.items()):
        lst.sort(key=lambda x: x.start)
        prev: ClipC | None = None
        for cc in lst:
            k = cc.clip
            w = where(k.start, f"片段 {k.id}（D{d}）")
            t_start = tempo.sec_at(cc.start)
            want = tempo.beat_at(t_start - PREP_SECONDS) if k.prep is None else cc.start - float(k.prep)
            if k.prep is not None and t_start - tempo.sec_at(want) < MIN_PREP_SECONDS - 1e-6:
                err("prep_short", w, f"准备时间 {fmt_dur(k.prep)} 只有 {t_start - tempo.sec_at(want):.2f} 秒，至少要 "
                                     f"{MIN_PREP_SECONDS:g} 秒", k.line)
            if prev is not None:
                if cc.start < prev.stop - 1e-9:
                    err("deck_overlap", w, f"和 {prev.clip.id} 在 D{d} 上重叠（{prev.clip.id} 放到 {_zh_pos(prev.stop)}，"
                                           f"{k.id} {_zh_pos(cc.start)} 就起播）", k.line, "换一台 Deck，或者错开时间")
                    prev = cc if cc.stop > prev.stop else prev
                    cc.load = want
                    continue
                free = tempo.beat_at(tempo.sec_at(prev.stop) + RELEASE_SECONDS)
                gap = t_start - tempo.sec_at(free)
                if gap < MIN_PREP_SECONDS - 1e-6:
                    need = tempo.beat_at(tempo.sec_at(prev.stop) + RELEASE_SECONDS + MIN_PREP_SECONDS) - prev.stop
                    err("deck_prep", w, f"D{d} 上 {prev.clip.id} 到 {_zh_pos(prev.stop)} 才停，{k.id} {_zh_pos(cc.start)} 就要起播："
                                        f"中间只有 {cc.start - prev.stop:.3g} 拍，装下一首至少要 {math.ceil(need)} 拍",
                        k.line, "换一台空着的 Deck，或者把起播推后 / 把上一个片段的停止提前")
                elif want < free:
                    if k.prep is not None:
                        err("deck_prep", w, f"准备时间 {fmt_dur(k.prep)} 伸进了 {prev.clip.id} 还在放的部分（它 {_zh_pos(prev.stop)} 才停）",
                            k.line, "准备时间写短一点，或者换一台 Deck")
                    want = free
                    if gap < 2.0:
                        hint("prep_tight", w, f"D{d} 上从 {prev.clip.id} 停到 {k.id} 起播只有 {gap:.1f} 秒准备", k.line)
            cc.load = want
            prev = cc
    for n, cc in enumerate(sorted(c.clips, key=lambda x: (x.load, x.clip.deck)), 1):
        cc.gen = n

    # ---- 参数曲线
    for ln in p.lanes:
        cc = c.clip(ln.clip)
        if cc is None:
            continue
        k = cc.clip
        zh = PARAM_ZH[ln.param]
        pts_h: list[list[Any]] = []
        bad = False
        for pt in ln.points:
            w = where(pt.at, f"{k.id} 的 {zh}")
            if float(pt.at) < cc.start - 1e-9 or float(pt.at) > cc.stop + 1e-9:
                err("lane_range", w, f"{_zh_pos(pt.at)} 不在片段播放的时间里（{k.id}：{_zh_pos(cc.start)} – {_zh_pos(cc.stop)}）",
                    pt.line, "写在这个片段放着的时候；片段换了要写新片段的 ID")
                bad = True
                continue
            if pts_h and float(pt.at) == pts_h[-1][0] and not pt.step:
                err("lane_same", w, f"{_zh_pos(pt.at)} 这一拍写了两次（同一个旋钮在同一拍只能有一个去向）", pt.line,
                    "要在这一拍跳变，写「跳」")
                bad = True
                continue
            pts_h.append([float(pt.at), float(pt.value), "step"] if pt.step else [float(pt.at), float(pt.value)])
        if bad or not pts_h:
            continue
        init = float(getattr(k.init, ln.param))
        if pts_h[0][0] > cc.start + 1e-9 or len(pts_h[0]) == 3:
            pts_h.insert(0, [cc.start, init])
        cc.lanes[ln.param] = pts_h

    # ---- 效果
    _effects(c, p, err, hint, where)

    # ---- 场景
    prev_s: Scene | None = None
    for s in sorted(p.scenes, key=lambda s: s.start):
        w = f"场景 {s.no}" + (f"「{s.name}」" if s.name else "")
        if s.end <= s.start:
            err("scene_range", w, f"结束 {_zh_pos(s.end)} 不在开始 {_zh_pos(s.start)} 之后", s.line)
        if prev_s is not None and s.start < prev_s.end:
            err("scene_overlap", w, f"和场景 {prev_s.no} 重叠（它到 {_zh_pos(prev_s.end)}）", s.line)
        if not s.draft and not p.legacy and s.missing():
            err("scene_strategy", w, f"正式场景要写五项说明，还缺：{' / '.join(s.missing())}", s.line,
                "补上；或者先写「- 状态：草稿」")
        for dk in s.roles:
            if not (len(dk) == 2 and dk[0] == "D" and dk[1].isdigit() and 1 <= int(dk[1]) <= caps.max_decks):
                err("scene_role", w, f"角色里的「{dk}」不是 D1–D{caps.max_decks}", s.line)
        prev_s = s
    _crowd_hints(c, p, hint)

    c.length = max([cc.stop for cc in c.clips] + [f["end"] + f["tail"] for f in c.fx] + [0.0])
    if not c.clips and not c.errors:
        err("empty", "全场", "没有片段")
    if c.ok:
        c.plan = _plan(c)
    c.issues.sort(key=lambda i: (i.level != "error", i.line or 1 << 30))
    return c


def _segments(k: Clip, loops: list[Loop], jumps: list[Jump], err: Callable[..., None], caps: Caps) -> list[SegC]:
    """片段的源位置分段。loop：就地（或从 src）转到 until，出来从实际读到的地方接着放；跳段：那一拍改读原曲的 to。"""
    segs = [SegC(k.start, k.src, Fraction(0), k.src, f"{k.id}.start", "start", k.line)]

    def src_at(g: Fraction) -> Fraction:
        s = segs[0]
        for x in segs[1:]:
            if x.g <= g:
                s = x
        d = g - s.g
        return s.lsrc + (d + (s.src - s.lsrc)) % s.loop if s.loop else s.src + d

    def put(seg: SegC) -> None:
        if seg.g == segs[-1].g:
            # 起播那一拍就开 loop / 出 loop 的那一拍接着跳段或再开一个 loop：新的一段顶替掉那一拍原来的
            if segs[-1].kind in ("start", "exit"):
                segs[-1] = seg
                return
            err("same_beat", f"{seg.id} 和 {segs[-1].id} 在同一拍 {_zh_pos(seg.g)}：同一个片段同一拍只能有一个动作", seg.line)
            return
        segs.append(seg)

    events = sorted([(x.at, 0, x) for x in loops] + [(x.at, 1, x) for x in jumps], key=lambda e: (e[0], e[1]))
    open_until: Fraction | None = None
    last_loop: Fraction | None = None  # 上一个 loop 的开头（「同起点」= 缩短 loop：还从那里转，只是变短）
    n = 0
    for at, _, ev in events:
        n += 1
        eid = ev.id or f"{k.id}.{'L' if isinstance(ev, Loop) else 'J'}{n}"
        if at < k.start:
            err("seg_range", f"{eid} 在 {_zh_pos(at)}，片段 {_zh_pos(k.start)} 才起播", ev.line)
            continue
        if isinstance(ev, Loop):
            if ev.slip and not caps.slip_loop:
                err("caps_slip", f"{eid} 是 Slip Loop：这个引擎还不支持", ev.line, "去掉 slip，按普通 loop 写")
                continue
            if ev.length < MIN_LOOP_BEATS:
                err("loop_short", f"{eid} 只有 {fmt_dur(ev.length)}：loop 最短 {fmt_dur(MIN_LOOP_BEATS)}", ev.line)
                continue
            if ev.until <= at:
                err("loop_range", f"{eid} 的出点 {_zh_pos(ev.until)} 不在开 loop（{_zh_pos(at)}）之后", ev.line)
                continue
            if open_until is not None and at < open_until:
                err("loop_overlap", f"{eid} 在 {_zh_pos(at)} 开，但上一个 loop 到 {_zh_pos(open_until)} 才出", ev.line,
                    "先出上一个 loop（缩短 loop = 在上一个的出点接着开一个短的）")
                continue
            if ev.src is not None:
                lsrc = ev.src
            elif ev.same_start:
                if last_loop is None:
                    err("loop_anchor", f"{eid} 写了「同起点」，但这个片段前面没有 loop", ev.line)
                    continue
                lsrc = last_loop
            else:
                lsrc = src_at(at)
            last_loop = lsrc
            put(SegC(at, lsrc, ev.length, lsrc, eid, "loop", ev.line))
            ph = (ev.until - at) % ev.length
            out = lsrc + (ph if ph else ev.length)
            segs.append(SegC(ev.until, out, Fraction(0), out, eid + ".out", "exit", ev.line))
            open_until = ev.until
        else:
            if open_until is not None and at < open_until:
                err("jump_in_loop", f"{eid} 在 {_zh_pos(at)}，那时 loop 还开着（{_zh_pos(open_until)} 才出）", ev.line,
                    "跳段写在出 loop 的那一拍或之后")
                continue
            put(SegC(at, ev.to, Fraction(0), ev.to, eid, "jump", ev.line))
    return segs


def fx_tail_beats(kind: str, params: dict[str, float], bpm: float, floor_db: float = -60.0) -> float:
    """关掉送出以后尾音掉到 floor_db 要多少拍（上界）。Delay / Echo：每回一次乘一次反馈；Reverb：按衰减给的秒数。"""
    k = 10 ** (floor_db / 20.0)
    if kind in ("delay", "echo"):
        fb, beats = params["feedback"], params["beats"]
        n = 1 if fb <= 1e-3 else math.ceil(math.log(k) / math.log(fb))
        return float(min(128.0, max(1.0, math.ceil((n + 1) * beats))))
    seconds = (1.5 + 8.0 * params.get("decay", 0.5)) * (-floor_db / 60.0)
    return float(min(128.0, max(2.0, math.ceil(seconds * bpm / 60.0))))


def _effects(c: Compiled5, p: Project, err: Callable[..., None], hint: Callable[..., None],
             where: Callable[..., str]) -> None:
    busy: list[float] = []  # 每个实例占到哪一拍
    for f in sorted(p.fx, key=lambda f: (f.start, f.id)):
        zh = FX_ZH.get(f.kind, f.kind)
        w = where(f.start, f"效果 {f.id}（{zh}）")
        if f.kind not in c.caps.fx:
            err("caps_fx", w, f"这个引擎还不会「{zh}」", f.line,
                "这是现场效果（P1）里的；现在能用的：" + " / ".join(FX_ZH[k] for k in c.caps.fx))
            continue
        if f.version and f.version != c.caps.fx[f.kind]:
            hint("fx_version", w, f"谱是按 {zh} 第 {f.version} 版存的，引擎现在是第 {c.caps.fx[f.kind]} 版，声音可能不一样", f.line)
        if f.kind in FX_SEND and f.route != "send":
            err("fx_route", w, f"{zh} 走发送（路由写「发送」）：干声照常出，另送一份进效果", f.line)
            continue
        if f.route not in c.caps.routes:
            err("caps_route", w, f"这个引擎还没有「{f.route}」这种路由", f.line)
            continue
        if len(f.targets) != 1:
            err("fx_target", w, "发送效果一次接一个片段" + ("，这里没写目标" if not f.targets else f"，这里写了 {len(f.targets)} 个"),
                f.line, "几个片段都要，就各写一条")
            continue
        cc = c.clip(f.targets[0])
        if cc is None:
            continue
        if f.end <= f.start:
            err("fx_range", w, f"结束 {_zh_pos(f.end)} 不在开始 {_zh_pos(f.start)} 之后", f.line)
            continue
        if float(f.start) < cc.start - 1e-9 or float(f.start) >= cc.stop:
            err("fx_range", w, f"{_zh_pos(f.start)} 开，但 {cc.clip.id} 那时没在放（{_zh_pos(cc.start)} – {_zh_pos(cc.stop)}）", f.line)
            continue
        spec = FX_PARAMS[f.kind]
        params = {k: v[0] for k, v in spec.items()}
        bad = False
        for key, val in f.params.items():
            if key == "legacy":
                continue
            if key not in spec:
                err("fx_param", w, f"{zh} 没有「{key}」这个参数（有：{' / '.join(spec)}）", f.line)
                bad = True
                continue
            lo, hi = spec[key][1], spec[key][2]
            if not lo <= float(val) <= hi:
                err("fx_param", w, f"{key} = {val:g} 超出范围 {lo:g}–{hi:g}", f.line)
                bad = True
                continue
            params[key] = float(val)
        if bad:
            continue
        bpm = c.tempo.bpm_at(float(f.end))
        tail = float(f.tail) if f.tail is not None else fx_tail_beats(f.kind, params, bpm)
        t80 = fx_tail_beats(f.kind, params, bpm, -80.0)

        def curve(pts: list[Any], default: float, f: Fx = f) -> list[list[float]]:
            """送出 / 返回量：没写 = 一直是默认值；写了点 = 第一个点之前保持默认值，斜线 / 跳变和旋钮一样。"""
            out: list[list[float]] = []
            prev = default
            for pt in pts:
                x = min(max(float(pt.at), float(f.start)), float(f.end))
                if not out and x > float(f.start):
                    out.append([float(f.start), default])
                if pt.step:
                    out.append([x, prev])
                out.append([x, float(pt.value)])
                prev = float(pt.value)
            return out or [[float(f.start), default]]

        end_all = float(f.end) + tail
        slot = next((i for i, b in enumerate(busy) if b <= float(f.start)), None)
        if slot is None:
            slot = len(busy)
            busy.append(0.0)
        if slot >= c.caps.fx_slots:
            err("caps_slots", w, f"这时同时用着的效果实例（含还在响的尾音）超过了引擎的上限 {c.caps.fx_slots} 个", f.line,
                "把前面的效果早点关，或者把尾音写短")
            continue
        busy[slot] = end_all
        c.fx.append({"id": f.id, "kind": f.kind, "clip": cc.clip.id, "deck": cc.clip.deck, "slot": slot,
                     "start": float(f.start), "end": float(f.end), "tail": round(tail, 4), "t80": round(t80, 4),
                     "p": {k: round(v, 6) for k, v in params.items()},
                     "send": curve(f.send, 1.0), "wet": curve(f.wet, 1.0), "line": f.line, "route": f.route})


def _crowd_hints(c: Compiled5, p: Project, hint: Callable[..., None]) -> None:
    """只按旋钮位估计的提示：同时有几个低频来源、两条声线、中频主题可能拥挤。旋钮位判断不了实际的频谱，
    所以都写成「可能」；场景里写「- 有意：多低频」之类就不再提。"""
    if not c.clips:
        return
    vocal = ("声线", "人声", "vocal", "Vocal", "acapella")
    theme = ("主曲", "进曲", "出曲", "主题", "旋律")
    for s in p.scenes:
        low_bars = voc_bars = mid_bars = 0.0
        first: dict[str, float] = {}
        g = float(s.start)
        while g < float(s.end):
            on = [cc for cc in c.clips if cc.start <= g < cc.stop and cc.value_at("vol", g) >= 0.2]
            lows = [cc for cc in on if cc.value_at("lo", g) >= -6.0]
            vocs = [cc for cc in on if any(v in (s.roles.get(f"D{cc.clip.deck}") or cc.clip.role) for v in vocal)]
            mids = [cc for cc in on if any(v in (s.roles.get(f"D{cc.clip.deck}") or cc.clip.role) for v in theme)
                    and cc.value_at("mid", g) >= -3.0 and cc.value_at("vol", g) >= 0.7]
            for key, lst, acc in (("low", lows, "low_bars"), ("voc", vocs, "voc_bars"), ("mid", mids, "mid_bars")):
                if len(lst) >= 2:
                    first.setdefault(key, g)
            low_bars += len(lows) >= 2
            voc_bars += len(vocs) >= 2
            mid_bars += len(mids) >= 2
            g += BEATS_PER_BAR
        w = f"场景 {s.no}" + (f"「{s.name}」" if s.name else "")
        if low_bars >= 2 and "multi_low" not in s.intent:
            hint("multi_low", w, f"从 {_zh_pos(first['low'])} 起约 {low_bars:g} 小节同时有两个以上的低频来源（按 LOW 旋钮位估计）",
                 s.line, "是有意的就在场景里写「- 有意：多低频」")
        if voc_bars >= 1 and "multi_vocal" not in s.intent:
            hint("multi_vocal", w, f"从 {_zh_pos(first['voc'])} 起约 {voc_bars:g} 小节有两条声线同时在放", s.line,
                 "是有意的就写「- 有意：两条声线」")
        if mid_bars >= 16 and "multi_mid" not in s.intent:
            hint("multi_mid", w, f"从 {_zh_pos(first['mid'])} 起约 {mid_bars:g} 小节两条主题的 MID 都开着，中频可能拥挤", s.line,
                 "是有意的就写「- 有意：中频叠加」")


def _plan(c: Compiled5) -> dict[str, Any]:
    p = c.project
    clips = []
    for cc in sorted(c.clips, key=lambda x: x.gen):
        k, info = cc.clip, cc.info
        init = {}
        curves = {}
        for param in PARAMS:
            pts = cc.lanes.get(param)
            if pts:
                # 和 djset/2 同一个换算：按小节喂进去，出来是拍 + Mixxx 原生值（dB / 推子位置上的直线按拍重采样）
                bars = [[x / BEATS_PER_BAR, v, *rest] for x, v, *rest in pts]
                native = [[x, y] for x, y in _lane_native(f"A.{param}", bars, 0.0)]
                curves[param] = native
                init[param] = native[0][1]
            else:
                init[param] = to_native(param, float(getattr(k.init, param)))
        segs = []
        for s in cc.segs:
            o = {"g": round(float(s.g), 6), "src": round(info.beat_time(float(s.src)), 6), "loop": round(float(s.loop), 6),
                 "id": s.id}
            if s.loop:
                o["lsrc"] = round(info.beat_time(float(s.lsrc)), 6)
            segs.append(o)
        clips.append({"id": k.id, "deck": k.deck, "asset": k.asset, "gen": cc.gen, "load": round(cc.load, 6),
                      "start": round(cc.start, 6), "stop": round(cc.stop, 6), "bpm": info.grid.bpm, "keylock": k.keylock,
                      "segs": segs, "init": init, "curves": curves})
    fx = [{k: v for k, v in f.items() if k in ("id", "kind", "clip", "slot", "start", "end", "tail", "p", "send", "wet")}
          for f in c.fx]
    body = {"t": c.caps.plan, "tempo": [[b, v] for b, v in zip(c.tempo.b, c.tempo.v)], "clips": clips, "fx": fx,
            "len": round(c.length, 6)}
    # 执行编号：计划本身 + 编译器版本 + 能力签名（换了引擎 / 设备要重新编）
    h = hashlib.sha256(json.dumps([body, COMPILER_VERSION, c.caps.signature(), p.id], sort_keys=True).encode())
    return {**body, "exec": h.hexdigest()[:12], "compiler": COMPILER_VERSION, "caps": c.caps.signature()}


# ---------------------------------------------------------------- 图谱数据与动作提示

def actions(c: Compiled5) -> list[dict[str, Any]]:
    """整场的动作（演出页的「接下来」、图上的关键点、PDF 的标注都用这一份）：每条有全场位置、Deck、片段、文字、
    谱里的行号；rc = 引擎执行后回执的编号（没有的 = 连续变化的旋钮，完成与否看引擎报的状态）。"""
    p = c.project
    out: list[dict[str, Any]] = []

    def add(at: float, kind: str, text: str, line: int, clip: str = "", deck: int = 0, rc: str = "",
            until: float | None = None) -> None:
        out.append({"at": round(at, 4), "pos": _zh_pos(at), "kind": kind, "text": text, "line": line, "clip": clip,
                    "deck": deck, "rc": rc, "until": until})

    for cc in c.clips:
        k = cc.clip
        name = p.asset(k.asset).title or k.asset
        add(cc.load, "load", f"D{k.deck} 装载 {k.id}「{name}」，摆好旋钮", k.line, k.id, k.deck, f"{k.id}.load", cc.start)
        add(cc.start, "start", f"D{k.deck} 起播 {k.id}「{name}」，从原曲 {_zh_pos(k.src)}", k.line, k.id, k.deck,
            f"{k.id}.start")
        add(cc.stop, "stop", f"D{k.deck} 停 {k.id}", k.line, k.id, k.deck, f"{k.id}.stop")
        for s in cc.segs[1:] if cc.segs[0].kind == "start" else cc.segs:
            if s.kind == "loop":
                nxt = next((x for x in cc.segs if x.id == s.id + ".out"), None)
                add(float(s.g), "loop", f"{k.id} 打 {fmt_dur(s.loop)} loop（原曲 {_zh_pos(s.lsrc)} 起）"
                    + (f"，到 {_zh_pos(nxt.g)} 出" if nxt else ""), s.line, k.id, k.deck, s.id,
                    float(nxt.g) if nxt else None)
            elif s.kind == "exit":
                add(float(s.g), "loop_out", f"{k.id} 出 loop，从原曲 {_zh_pos(s.src)} 接着放", s.line, k.id, k.deck, s.id)
            elif s.kind == "jump":
                add(float(s.g), "jump", f"{k.id} 跳到原曲 {_zh_pos(s.src)}", s.line, k.id, k.deck, s.id)
        for param, pts in cc.lanes.items():
            zh = PARAM_ZH[param]
            lines = {float(pt.at): pt.line for ln in p.lanes if ln.clip == k.id and ln.param == param for pt in ln.points}
            for i in range(1, len(pts)):
                a, b = pts[i - 1], pts[i]
                if len(b) == 3:
                    if abs(b[1] - a[1]) > 1e-9:
                        add(b[0], "knob", f"{k.id} {zh} 一下到 {knob_text(param, b[1])}", lines.get(b[0], k.line), k.id, k.deck)
                elif abs(b[1] - a[1]) > 1e-9:
                    add(a[0], "knob", f"{k.id} {zh} {knob_text(param, a[1])} → {knob_text(param, b[1])}，"
                                      f"用 {fmt_dur(Fraction(b[0] - a[0]).limit_denominator(96))}",
                        lines.get(b[0], k.line), k.id, k.deck, until=b[0])
    for f in c.fx:
        zh = FX_ZH[f["kind"]]
        d = f"拍长 {fmt_dur(Fraction(f['p']['beats']).limit_denominator(96))}" if "beats" in f["p"] else f"衰减 {f['p']['decay']:g}"
        add(f["start"], "fx", f"{f['id']} {zh} 开（{f['clip']}，发送，{d}）", f["line"], f["clip"], f["deck"], until=f["end"])
        add(f["end"], "fx_off", f"{f['id']} {zh} 关送出，尾音留 {fmt_dur(Fraction(f['tail']).limit_denominator(4))}", f["line"],
            f["clip"], f["deck"], until=f["end"] + f["tail"])
    for i in range(1, len(p.tempo)):
        a, b = p.tempo[i - 1], p.tempo[i]
        if abs(a.bpm - b.bpm) > 1e-9:
            if b.ramp:
                add(float(a.at), "tempo", f"主速 {a.bpm:g} → {b.bpm:g}，到 {_zh_pos(b.at)}", b.line, until=float(b.at))
            else:
                add(float(b.at), "tempo", f"主速一下换到 {b.bpm:g}", b.line)
    for m in p.marks:
        cc = c.clip(m.clip) if m.clip else None
        add(float(m.at), "mark", m.text, m.line, m.clip, cc.clip.deck if cc else 0)
    order = {"load": 0, "stop": 1, "loop_out": 2, "jump": 3, "start": 4, "loop": 5, "knob": 6, "fx": 7, "fx_off": 8,
             "tempo": 9, "mark": 10}
    out.sort(key=lambda a: (a["at"], order.get(a["kind"], 99), a["deck"]))
    for n, a in enumerate(out, 1):
        a["n"] = n
    return out


def chart(c: Compiled5) -> dict[str, Any]:
    """整场的图谱数据：同一条全场小节轴上的四条播放带（每段读到原曲哪里）、每个旋钮的曲线、效果、场景和策略、动作。
    网页的共轴图和交接说明都从这一份画——和交给引擎的计划同一次编译。"""
    p = c.project
    clips = []
    for cc in c.clips:
        k = cc.clip
        a = p.asset(k.asset)
        segs = []
        for i, s in enumerate(cc.segs):
            g1 = float(cc.segs[i + 1].g) if i + 1 < len(cc.segs) else cc.stop
            segs.append({"g0": float(s.g), "g1": g1, "src": float(s.src), "src_pos": _zh_pos(s.src), "loop": float(s.loop),
                         "kind": s.kind, "id": s.id, "line": s.line})
        lanes = {}
        for param in PARAMS:
            pts = cc.lanes.get(param) or [[cc.start, float(getattr(k.init, param))]]
            lanes[param] = [{"at": x, "v": v, "step": bool(rest), "text": knob_text(param, v)} for x, v, *rest in pts]
        clips.append({"id": k.id, "deck": k.deck, "asset": k.asset, "title": a.title or a.file or a.id, "role": k.role,
                      "track_id": cc.info.track_id, "bpm": cc.info.grid.bpm, "start": cc.start, "stop": cc.stop,
                      "load": cc.load, "gen": cc.gen, "segs": segs, "lanes": lanes, "line": k.line, "note": k.note,
                      "keylock": k.keylock})
    scenes = [{"no": s.no, "name": s.name, "start": float(s.start), "end": float(s.end), "draft": s.draft,
               "strategy": {"作用": s.purpose, "保留": s.keep, "前景": s.lead, "LOW": s.low, "MID/HIGH": s.midhigh},
               "missing": s.missing(), "roles": s.roles, "intent": s.intent, "line": s.line} for s in p.scenes]
    return {"id": p.id, "name": p.name, "exec": (c.plan or {}).get("exec"), "length": c.length, "duration": c.duration,
            "bars": c.length / BEATS_PER_BAR, "tempo": [{"at": b, "bpm": v} for b, v in zip(c.tempo.b, c.tempo.v)],
            "decks": c.caps.max_decks, "clips": clips, "fx": [{k: v for k, v in f.items() if k != "t80"} for f in c.fx],
            "scenes": scenes, "actions": actions(c), "issues": [i.to_dict() for i in c.issues],
            "legacy": p.legacy, "caps": c.caps.to_dict()}
