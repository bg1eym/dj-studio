"""打碟谱（Plan）→ djset/2。全部是确定性规则，结果是普通的 set 文件，网页上照常能改。

小节对齐
  谱上每首的第 1 小节 = 「第 1 小节第 1 拍」的秒数（Engine DJ 清单）。把它吸附到这首歌的网格拍上：
  该拍的拍号 mod 4 就是小节相位（写进 set 的 ``downbeat`` 覆盖，不改曲库）；本应用的第 1 小节 = 曲中最早的
  小节线，所以「谱上第 b 小节 = 本应用第 b + k 小节」，k 写进 note。再用谱上的 breakdown / drop 小节
  与本应用切出的段落交叉核对（谱里自己说了：差 1 小节说明第 1 拍选错了一拍）。

旋钮（谱上是 Prime 4+ 的钟点位，见 cdw.py）
  EQ：12 点 = 0 dB，7 点 = 全切（谱上写 -25 dB），5 点 = +6 dB。12 点以左按 -25·(钟头/5)^1.56 dB：
  10 点 ≈ -6 dB，对上谱里「B LOW 给到一半」写 10 点。这条曲线是假设（docs/DEVICE_TEST.md），只在 eq_db 一处。
  Filter：左负右正，钟头/5 → -1..1（「左 9 点」= -0.6 =「切一半」）。推子：0–10 → 推子位置 0..1。
  两行之间直线过渡；相邻两行只差 1 小节而且变化大（≥ 2 个钟头 / 推子 ≥ 3 格）= 在后一行那一拍跳变
  （谱里这些都是「一拍回中」「同一拍拧到底」之类的动作）。

过渡
  一律 custom：时间线从表格第一行（B 还没进的那一行）开始，B 在「B 的第 1 小节落在 A 的第 X 小节」起播；
  A 自己播完（谱：出曲推子不拉，跑到自己的最后一小节）。主速：页眉的主速；「B 第 m–n 小节把主速 X → Y」
  按 B 的小节换算进时间线。特效「Main FX Reverb，第 a–b 小节，Wet/Dry 1/3」→ 该 deck 的 reverb 从 a 起 1/3。
"""
from __future__ import annotations

import difflib
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..grid import BEATS_PER_BAR, Grid
from ..setspec import SPEC_VERSION
from ..setfile import SetFile
from .cdw import Knobs, Plan, PlanRow, PlanTrack, PlanTransition

EQ_CUT_DB = -25.0  # 谱：7 点 = 全切（-25 dB）
EQ_MAX_DB = 6.0  # 谱：5 点 = 满（+6 dB）
EQ_TAPER = 1.56  # 10 点 = -6 dB（「一半」）
STEP_HOURS = 2.0  # 相邻一小节、变化 ≥ 这么多钟头 → 跳变
STEP_FADER = 3.0
OUTLIER_MS = 25.0
WRONG_BEAT_TOL = 0.2  # 谱的第 1 拍离最近网格拍超过这么多拍 → 可能对错了拍


def eq_db(hours: float) -> float:
    if hours >= 0:
        return round(EQ_MAX_DB * min(hours, 5.0) / 5.0, 2)
    return round(EQ_CUT_DB * (min(-hours, 5.0) / 5.0) ** EQ_TAPER, 2)


def filter_val(hours: float) -> float:
    return round(max(-5.0, min(5.0, hours)) / 5.0, 3)


def fader_pos(v: float) -> float:
    return round(max(0.0, min(10.0, v)) / 10.0, 3)


@dataclass
class LibTrack:
    """候选曲目：曲库里的一首，带网格（Mixxx 的，或分析的）。"""

    id: str
    path: str
    title: str
    artist: str
    duration: float
    grid: Grid | None  # bpm / first_beat / downbeat（曲库里确认过的小节相位）
    sections: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Note:
    level: str  # error / warn / info
    where: str
    msg: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}：{self.msg}"


@dataclass
class Alignment:
    downbeat: int  # 网格拍号 mod 4 = 小节第 1 拍
    k: int  # 谱上第 b 小节 = 本应用第 b + k 小节
    residual_ms: float  # 谱的第 1 拍 − 最近的网格拍
    bar1_s: float  # 按谱的小节相位，本应用第 1 小节的起点（秒）——相位和曲库不同时写回曲库
    rephase: bool  # 小节相位和曲库里的不同
    cue_dev: list[tuple[float, str, float | None]] = field(default_factory=list)  # (谱小节, 标签, 偏差小节)
    last_bar: float | None = None  # 曲子结束在谱上的第几小节（文件末尾的位置）


@dataclass
class Converted:
    set: SetFile | None
    matches: list[LibTrack | None]
    alignments: list[Alignment | None]
    notes: list[Note]

    @property
    def ok(self) -> bool:
        return self.set is not None and not any(n.level == "error" for n in self.notes)


# ---------------------------------------------------------------- 曲目匹配

def _norm(s: str) -> str:
    s = Path(s).stem if s.lower().endswith((".mp3", ".wav", ".flac", ".aiff", ".aif", ".m4a", ".ogg")) else s
    s = re.sub(r"^\s*\d{1,3}[\s_.\-]+", "", s)  # 文件名前面的编号
    return re.sub(r"[^0-9a-z]", "", s.lower().replace("&", ""))


def _lead_no(path: str) -> int | None:
    m = re.match(r"^\s*(\d{1,3})[\s_.\-]", Path(path).name)
    return int(m.group(1)) if m else None


def duration_ok(pt: PlanTrack, duration: float) -> bool:
    """时长核对：谱上的小节数 × 原 BPM + 第 1 拍的秒数 ≈ 文件时长（容差 12 小节）。谱上没写就不核对。"""
    if not (pt.bars and pt.bpm_orig):
        return True
    bar = BEATS_PER_BAR * 60.0 / pt.bpm_orig
    return abs((pt.bar1_s or 0.0) + pt.bars * bar - duration) <= 12 * bar


FILE_MATCH = 0.85  # 在电脑里按文件名找曲子：名字的相似度门槛


def _words(s: str) -> frozenset[str]:
    """文件名 / 曲名里的词（小写，去掉扩展名和前面的编号）。"""
    s = Path(s).stem if s.lower().endswith((".mp3", ".wav", ".flac", ".aiff", ".aif", ".m4a", ".ogg")) else s
    s = re.sub(r"^\s*\d{1,3}[\s_.\-]+", "", s.lower().replace("&", " "))
    return frozenset(re.findall(r"[^\W_]+", s))


def _name_sim(want: tuple[str, frozenset[str]], have: tuple[str, frozenset[str]]) -> float:
    """两个名字有多像：字符相似度和「词的集合」相似度取大的。
    词集合管得住艺人写法、顺序、重复不同的情况（「Push,Charlotte de Witte - … (Charlotte de Witte Rework)」
    对「14_Push_-_…_Charlotte_de_Witte_Rework」）；多一个词（Edit / Remix）就到不了门槛。"""
    words = len(want[1] & have[1]) / len(want[1] | have[1]) if want[1] and have[1] else 0.0
    if words >= FILE_MATCH:
        return words
    sm = difflib.SequenceMatcher(None, want[0], have[0])
    if sm.real_quick_ratio() < FILE_MATCH or sm.quick_ratio() < FILE_MATCH:
        return words
    return max(words, sm.ratio())


def file_matches(plan: Plan, paths: list[str], want: list[int]) -> dict[int, str]:
    """在一堆音频文件里按文件名找谱上的曲子（want = 要找的曲目下标）。只看名字；时长由调用方用 duration_ok 核对。
    每首取最像的文件，一个文件只给一首。"""
    names = [(p, (_norm(Path(p).name), _words(Path(p).name))) for p in paths]
    scored: list[tuple[float, int, int]] = []
    for i in want:
        pt = plan.tracks[i]
        targets = [(_norm(x), _words(x)) for x in (pt.file_name, pt.title.replace("—", " ")) if x]
        for j, (p, have) in enumerate(names):
            if not have[0]:
                continue
            best = max((_name_sim(w, have) for w in targets), default=0.0)
            if best >= FILE_MATCH:
                scored.append((best + (0.01 if _lead_no(p) == pt.no else 0.0), i, j))
    scored.sort(reverse=True)
    out: dict[int, str] = {}
    used: set[int] = set()
    for _, i, j in scored:
        if i in out or j in used:
            continue
        out[i] = names[j][0]
        used.add(j)
    return out


def match_tracks(plan: Plan, lib: list[LibTrack]) -> tuple[list[LibTrack | None], list[Note]]:
    notes: list[Note] = []
    scored: list[tuple[float, int, int]] = []
    for i, pt in enumerate(plan.tracks):
        want = [_norm(pt.file_name), _norm(pt.title.replace("—", " "))]
        for j, lt in enumerate(lib):
            have = [_norm(lt.path), _norm(f"{lt.artist} {lt.title}"), _norm(lt.title)]
            sim = max(difflib.SequenceMatcher(None, w, h).ratio() for w in want if w for h in have if h)
            score = sim + (0.35 if _lead_no(lt.path) == pt.no else 0.0)
            if lt.duration and not duration_ok(pt, lt.duration):
                score -= 0.4
            scored.append((score, i, j))
    scored.sort(reverse=True)
    out: list[LibTrack | None] = [None] * len(plan.tracks)
    used: set[int] = set()
    for score, i, j in scored:
        if out[i] is not None or j in used or score < 0.6:
            continue
        out[i] = lib[j]
        used.add(j)
    for i, pt in enumerate(plan.tracks):
        if out[i] is None:
            notes.append(Note("error", f"#{pt.no:02d} {pt.title}", f"曲库里找不到这首（谱上的文件名：{pt.file_name or '—'}）"))
    return out, notes


# ---------------------------------------------------------------- 小节对齐

def align(pt: PlanTrack, lt: LibTrack) -> tuple[Alignment | None, list[Note]]:
    where = f"#{pt.no:02d} {pt.title}"
    g = lt.grid
    if g is None or g.bpm <= 0:
        return None, [Note("error", where, "还没有网格（分析 / Mixxx 分析没完成）")]
    notes: list[Note] = []
    if pt.bpm_orig and abs(g.bpm - pt.bpm_orig) > 0.6:
        notes.append(Note("error", where, f"BPM 对不上：谱上原 BPM {pt.bpm_orig:g}，网格 {g.bpm:.2f}"))
    anchor = pt.bar1_s if pt.bar1_s is not None else g.bar_time(1)
    jf = g.time_beat(anchor)
    j = round(jf)
    res = (jf - j) * g.beat_len * 1000
    if abs(jf - j) > WRONG_BEAT_TOL:
        notes.append(Note("warn", where, f"谱的第 1 小节（{anchor:.3f}s）离最近的网格拍 {res:+.0f}ms，"
                                         "可能对到了别的拍：在曲库页看一下这首的小节线"))
    db = j % BEATS_PER_BAR
    g2 = Grid(g.bpm, g.first_beat, db, g.duration)
    k = round(g2.beat_bar(j)) - 1
    al = Alignment(db, k, round(res, 1), round(g2.bar_time(1), 5), db != g.downbeat,
                   last_bar=round(g2.last_bar - k, 2) if g.duration else None)
    if al.rephase:
        notes.append(Note("info", where, f"小节线按谱定（和原来自动估的差 {(db - g.downbeat) % 4} 拍）"))
    # 段落核对：谱上的 breakdown / drop 小节 vs 本应用切出的段落起点
    starts = {"break": [s["start"] for s in lt.sections if s["label"] == "breakdown"],
              "drop": [s["start"] for s in lt.sections if s["label"] in ("drop", "groove")]}
    for bar, label in pt.cues:
        kind = "drop" if "drop" in label else "break" if "break" in label else None
        if not kind or not starts[kind]:
            al.cue_dev.append((bar, label, None))
            continue
        # 段落起点是按曲库里的小节相位（g）数的，换成谱的相位（g2）的小节号再比
        ours = [g2.beat_bar(g.bar_beat(s_)) for s_ in starts[kind]]
        dev = min((o - (bar + k) for o in ours), key=abs)
        al.cue_dev.append((bar, label, round(dev, 2)))
    devs = [d for _, _, d in al.cue_dev if d is not None]
    near = [d for d in devs if abs(d) <= 4]
    if near and not al.rephase:
        same = len(near) >= 3 and len(set(near)) == 1 and near[0] != 0
        txt = "、".join(f"第 {b:g} 小节 {lab.split()[0]} {d:+g}" for b, lab, d in al.cue_dev if d is not None and abs(d) <= 4)
        notes.append(Note("warn" if same else "info", where,
                          f"谱上的段落和本应用切出的段落：{txt}" + ("——整体差一个常数，小节号可能错位" if same else "")))
    if pt.bars and g.duration:
        ours_total = g2.last_bar - k
        if abs(ours_total - pt.bars) > 2:
            notes.append(Note("warn" if abs(ours_total - pt.bars) > 8 else "info", where,
                              f"谱上 {pt.bars} 小节，本应用对齐后约 {ours_total:.1f} 小节"))
    return al, notes


# ---------------------------------------------------------------- 过渡

PARAMS = ("lo", "mid", "hi", "filter", "vol")


def _value(param: str, k: Knobs) -> float:
    if param in ("lo", "mid", "hi"):
        return eq_db(getattr(k, param))
    if param == "filter":
        return filter_val(k.filter)
    return fader_pos(k.vol)


def _raw(param: str, k: Knobs) -> float:
    return getattr(k, param)


def _lane(rows: list[tuple[float, Knobs]], param: str, first_step: bool = False) -> list[list[Any]]:
    pts: list[list[Any]] = []
    prev: tuple[float, Knobs] | None = None
    for t, kn in rows:
        v = _value(param, kn)
        step = False
        if prev is None:
            step = first_step
        elif t - prev[0] <= 1.0 + 1e-9:
            d = abs(_raw(param, kn) - _raw(param, prev[1]))
            step = d >= (STEP_FADER if param == "vol" else STEP_HOURS)
        pts.append([t, v, "step"] if step else [t, v])
        prev = (t, kn)
    # 去掉中间重复：连续三个点值相同（且中间那个不是跳变）就删中间的
    out: list[list[Any]] = []
    for i, p in enumerate(pts):
        if 0 < i < len(pts) - 1 and len(p) == 2 and p[1] == pts[i - 1][1] == pts[i + 1][1]:
            continue
        out.append(p)
    return out


def _neutral(lane: list[list[Any]], param: str) -> bool:
    neutral = {"lo": 0.0, "mid": 0.0, "hi": 0.0, "filter": 0.0, "vol": 1.0}[param]
    return all(abs(float(p[1]) - neutral) < 1e-9 for p in lane)


def transition(tr: PlanTransition, a: Alignment, b: Alignment) -> tuple[dict[str, Any], list[Note]]:
    where = f"过渡 {tr.no}（{tr.a_name} → {tr.b_name}）"
    notes: list[Note] = []
    a0 = tr.rows[0].a_bar
    play_at = tr.b1_at_a - a0
    if play_at < 0:
        raise ValueError(f"{where}：表格第一行在 B 起播之后")
    last = tr.rows[-1].a_bar - a0

    stop = tr.rows[-1].stop  # 「停」：A 在最后一行那一小节的第 1 拍停；否则 A 自己播完
    a_rows = [(r.a_bar - a0, r.a) for r in tr.rows]
    b_rows = [(r.a_bar - a0, r.b) for r in tr.rows if r.b is not None]
    lanes: dict[str, list[list[Any]]] = {}
    for p in PARAMS:
        la = _lane(a_rows, p)
        if not _neutral(la, p):
            lanes[f"A.{p}"] = la
        lb = _lane(b_rows, p, first_step=(p == "vol"))  # type: ignore[arg-type]
        if p == "vol" or not _neutral(lb, p):
            lanes[f"B.{p}"] = lb

    bars = float(last)
    # 效果「第 a–b 小节」：a 的第 1 拍打开，b 结束时关。A 的效果开到 A 结束 / 停的那一小节：不关，让尾巴自己响完
    # （过渡结束 3 秒后出场 deck 归位，见 DJStudio.js）
    for fx in tr.effects:
        if fx.deck == "A":
            t0, t1 = fx.bar0 - a0, fx.bar1 + 1 - a0
            keep = fx.bar1 + 1 >= tr.rows[-1].a_bar
        else:
            t0, t1 = play_at + fx.bar0 - tr.b_in, play_at + fx.bar1 + 1 - tr.b_in
            keep = False
        name = f"{fx.deck}.{fx.kind}"
        lane = lanes.setdefault(name, [[max(0.0, t0), 0.0]])
        lane.append([max(0.0, t0), round(fx.wet, 3), "step"])
        if not keep:
            lane += [[t1, round(fx.wet, 3)], [t1, 0.0, "step"]]
            bars = max(bars, float(t1))

    tempo: list[list[Any]] = [[0, tr.tempo_from]]
    if tr.ramp_b:
        m, n = tr.ramp_b
        t0, t1 = play_at + m - tr.b_in, play_at + n + 1 - tr.b_in
        tempo = [[0, tr.tempo_from], [t0, tr.tempo_from], [t1, tr.tempo_to]]
        bars = max(bars, float(t1))
    elif tr.tempo_to != tr.tempo_from:
        notes.append(Note("warn", where, f"主速 {tr.tempo_from:g} → {tr.tempo_to:g} 没写在哪几小节换，按 B 起播时跳变处理"))
        tempo = [[0, tr.tempo_from], [play_at, tr.tempo_to, "step"]]

    if not tr.rows[-1].end:
        notes.append(Note("warn", where, "最后一行没标「结束」，A 仍按自己播完处理"))
    elif not stop and a.last_bar is not None and abs(a.last_bar - (tr.rows[-1].a_bar + 1)) > 2:
        notes.append(Note("warn", where, f"谱上 A 在第 {tr.rows[-1].a_bar} 小节结束，A 实际播到第 {a.last_bar - 1:.1f} 小节才完"
                                         f"（「结束」= A 自己播完，时间线等它停）。要在这里停 A，写「{tr.rows[-1].a_bar} 停」"))
    note = f"打碟谱过渡 {tr.no}：{'长交接' if tr.technique == 'longhand' else '瞬时切换'}，B 第 {tr.b_in} 小节 = A 第 {tr.b1_at_a} 小节"
    if a.k or b.k:
        note += f"（谱上的小节号换成本应用：A {a.k:+d}，B {b.k:+d}）"
    out = {
        "technique": "custom",
        "start": float(a0 + a.k),
        "b_in": float(tr.b_in + b.k),
        "play_at": float(play_at),
        "bars": bars,
        "params": {"a_end": "stop" if stop else "playout"},
        "lanes": lanes,
        "events": [[float(last), "A.stop"]] if stop else [],
        "tempo": tempo,
        "label": "长交接" if tr.technique == "longhand" else "瞬时切换",
        "note": note,
        "marks": [[float(r.a_bar - a0), r.marks] for r in tr.rows if r.marks.strip()],
    }
    return out, notes


# ---------------------------------------------------------------- 总装

def _slug(s: str) -> str:
    """谱文件名 → set id。全是中文的文件名：「plan-」加一小段哈希（不同的谱不会撞成同一个 set）。"""
    out = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return out or "plan-" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]


def convert(plan: Plan, lib: list[LibTrack], source: str = "", set_id: str | None = None,
            keylock_note: bool = True) -> Converted:
    """keylock_note：谱上要求 Key Lock 时提醒为什么先关（只和真 Mixxx 有关；内置引擎没有声音，不提醒）。"""
    notes: list[Note] = [Note("warn", "打碟谱", w) for w in plan.warnings]
    matches, mnotes = match_tracks(plan, lib)
    notes += mnotes
    aligns: list[Alignment | None] = []
    for pt, lt in zip(plan.tracks, matches):
        if lt is None:
            aligns.append(None)
            continue
        al, an = align(pt, lt)
        aligns.append(al)
        notes += an
    if any(a is None for a in aligns):
        return Converted(None, matches, aligns, notes)
    res = sorted(a.residual_ms for a in aligns if a)
    med = res[len(res) // 2]
    for pt, a in zip(plan.tracks, aligns):
        if a and abs(a.residual_ms - med) > OUTLIER_MS:
            notes.append(Note("info", f"#{pt.no:02d} {pt.title}",
                              f"谱的第 1 拍比网格 {a.residual_ms:+.0f}ms（其它曲子中位 {med:+.0f}ms）。"
                              "谱的作者测起音的位置和这里不完全一样；同步按网格走，两个 kick 照样对齐"))

    keylocked = [pt for pt in plan.tracks if pt.keylock]
    if keylocked and keylock_note:
        notes.append(Note("warn", ", ".join(f"#{pt.no:02d}" for pt in keylocked),
                          "谱上要求 Key Lock 开。Mixxx 的时间拉伸会让这首比引擎位置晚 17–29ms，叠放时两个 kick 会出双击，"
                          "所以先关 Key Lock（音高随速度变化）。要开的话把 set 的 keylock 改成 true"))

    tracks = []
    for i, (pt, lt, al) in enumerate(zip(plan.tracks, matches, aligns)):
        assert lt and al
        t: dict[str, Any] = {"id": lt.id, "title": pt.title.split(" — ")[0], "play_bpm": pt.bpm_play or None,
                             "downbeat": al.downbeat,
                             "note": f"谱 #{pt.no:02d}；谱上第 1 小节 = {pt.bar1_s if pt.bar1_s is not None else '—'}s"
                                     + (f"，本应用第 {1 + al.k} 小节" if al.k else "")}
        if i == 0:
            t["start"] = float(plan.start_bar + al.k)
        # 谱上的段落标记（breakdown / drop …）换成本应用的小节号；hot cue 按 docs/SET_SPEC.md 由它们定
        cues: dict[str, float] = {}
        for bar, label in pt.cues:
            name, n = label, 2
            while name in cues:  # 同名的第 2、3 个：「drop #2」「drop #3」
                name, n = f"{label} #{n}", n + 1
            cues[name] = float(bar + al.k)
        t["cues"] = cues
        t["plan_bar_offset"] = al.k
        # 谱上写明的 hot cue（Markdown 谱）：照写的设；空着的垫子编译后按 set 放进、出和中间要处理的点
        # （setspec.fill_hot_cues 不动写了的）
        hot = []
        g2 = Grid(lt.grid.bpm, lt.grid.first_beat, al.downbeat, lt.grid.duration) if lt.grid else None
        for c in pt.hot_cues:
            if c.bar is not None:
                bar = c.bar + al.k
            elif g2 is not None and c.time is not None:
                bar = g2.beat_bar(round(g2.time_beat(c.time)))  # cue 落在拍上
            else:
                continue
            if bar < 1:
                notes.append(Note("error", f"#{pt.no:02d} {pt.title}", f"打击垫 {c.pad}「{c.name}」在曲子的第 1 小节之前"))
                continue
            hot.append({"pad": c.pad, "name": c.name, "bar": round(bar, 4), "color": c.color, "note": c.note or "谱上写的"})
        if hot:
            t["hot_cues"] = hot
        tracks.append(t)
    trans = []
    for tr in plan.transitions:
        a, b = aligns[tr.no - 1], aligns[tr.no]
        assert a and b
        d, tn = transition(tr, a, b)
        trans.append(d)
        notes += tn
    ending: dict[str, Any] = {"mode": "playout"}
    if plan.ending:
        at, n = plan.ending
        last_al = aligns[-1]
        assert last_al
        ending = {"mode": "fade", "bars": n, "bar": None if at is None else float(at + last_al.k)}
    if any(n.level == "error" for n in notes):
        return Converted(None, matches, aligns, notes)
    sid = set_id or _slug(Path(source).stem if source else plan.title)
    s = SetFile.model_validate({
        "schema": "djset/2", "spec": SPEC_VERSION, "id": sid, "name": plan.title, "created": time.strftime("%Y-%m-%d"),
        "source": source, "note": "由打碟谱自动生成（djstudio.plan），按播放规范 docs/SET_SPEC.md：旋钮钟点位 → dB、"
                                  "推子、效果器、hot cue（Prime 4+ 的 8 个打击垫）都按那里的表。",
        "keylock": False, "tracks": tracks, "transitions": trans,
        "ending": ending,
    })
    return Converted(s, matches, aligns, notes)
