"""现场效果的编译：谱上的一行效果（Trans / Roll / Filter / Filter Echo / Spiral / Noise / Flanger / Phaser / 采样 / Release）
→ 引擎里的「处理组」（bus.hpp）和采样触发。

- 路由：插入（一个片段）/ 组插入（几个片段一起）/ 总输出。同一条路由 + 同一组目标 = 同一个处理组；
  Release 写同样的路由和目标，收的就是那一组。
- 同一时间一个片段最多在一个处理组里；总输出在处理的时候，别的处理组不能同时开着（P1 不做任意串联）。
- 宏（Filter Echo）在这里展开成组里的两件（Filter + Echo），展开后的参数留在编译结果里，图上、提示里看得到。
- Release 开始的那一拍，同组里还开着的效果到此为止（不会在 Release 之后被旧的安排重新打开）；
  「退场」之后这一组一直静音，到这一组下一次显式启用（下一条效果的开始）为止。
- 采样：内置的几种鼓，或者谱旁边的短音频文件；到「结束」那一拍还在响的全部收掉。两组采样不能重叠（收的时候分不开）。

参数在谱里是中文名 + 值（「频段 HIGH；周期 1/4拍；深度 1」），这里按每种效果的表检查、换算；看不懂的指出行号。
"""
from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Callable

from .pos import PosError, fmt_dur, parse_dur

if TYPE_CHECKING:
    from .compile import Compiled5
    from .model import Fx, Project

LIVE_KINDS = ("trans", "roll", "filter", "filter_echo", "spiral", "noise", "flanger", "phaser", "sample", "release")
ROUTE_ENGINE = {"insert": "insert", "group-insert": "group", "master-insert": "master"}
BANDS = ("LOW", "MID", "HIGH")
MODES = {"高通": 1, "hpf": 1, "hp": 1, "低通": 0, "lpf": 0, "lp": 0, "带通": 2, "bpf": 2, "bp": 2}
MODE_ZH = {0: "低通", 1: "高通", 2: "带通"}
RELEASE_KINDS = {"echo": "rel_echo", "回声": "rel_echo", "刹车": "brake", "brake": "brake", "回转": "spin",
                 "backspin": "spin", "spin": "spin"}
RELEASE_ZH = {"rel_echo": "Echo", "brake": "刹车", "spin": "回转"}
BUS_MARGIN = 1.0  # 拍：两个处理组用同一个片段时，中间至少隔这么久
ROLL_MAX_SECONDS = 4.0  # dsp.hpp Roll::kMaxSeconds
ECHO_MAX_SECONDS = 3.0  # fx.hpp DelayLine::kMaxSeconds
BRAKE_MAX_SECONDS = 8.0  # dsp.hpp BrakeSpin：刹车最长 2 × 4 秒
SPIN_MAX_SECONDS = 2.0  # 回转最长 2 秒
SAMPLE_MAX_SECONDS = 8.0
# 每种效果认哪些参数（谱上的名字）
PARAMS: dict[str, tuple[str, ...]] = {
    "trans": ("频段", "周期", "占空", "深度"),
    "roll": ("频段", "拍长", "混合"),
    "filter": ("模式", "截止", "共振"),
    "filter_echo": ("模式", "截止", "共振", "拍长", "反馈", "送出", "返回"),
    "spiral": ("拍长", "音高", "反馈", "混合"),
    "noise": ("音量", "频率", "共振"),
    "flanger": ("周期", "深度", "反馈", "混合"),
    "phaser": ("周期", "深度", "反馈", "混合"),
    "sample": ("重复", "音量"),
    "release": ("方式", "收尾", "拍长", "反馈"),
}
# 一种效果用到引擎里的哪几件（能力表里的名字）
ENGINE_KINDS: dict[str, tuple[str, ...]] = {
    "trans": ("trans",), "roll": ("roll",), "filter": ("filter",), "filter_echo": ("filter", "insert_echo"),
    "spiral": ("spiral",), "noise": ("noise",), "flanger": ("flanger",), "phaser": ("phaser",), "sample": ("sample",),
    "release": ("rel_echo", "brake", "spin"),
}


class ParamError(ValueError):
    pass


def amount(text: str) -> float:
    """量：1/2、0.5、50%。"""
    t = text.strip().replace("％", "%")
    try:
        if t.endswith("%"):
            return float(t[:-1]) / 100.0
        if "/" in t:
            a, b = t.split("/", 1)
            return float(Fraction(int(a.strip()), int(b.strip())))
        return float(t)
    except (ValueError, ZeroDivisionError) as e:
        raise ParamError(f"「{text}」不是一个量（写 1/2、0.5 或 50%）") from e


def stages(text: str) -> list[str]:
    """「a → b → c」拆成几段。"""
    return [x.strip() for x in re.split(r"\s*(?:→|->|—>)\s*", text.strip()) if x.strip()]


def hz(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(k|K)?\s*(?:hz|Hz|HZ)?", text.strip())
    if not m:
        raise ParamError(f"「{text}」不是频率（写 200Hz、2.5kHz）")
    v = float(m.group(1)) * (1000.0 if m.group(2) else 1.0)
    if not 20.0 <= v <= 20000.0:
        raise ParamError(f"频率 {text} 超出 20Hz–20kHz")
    return v


def bands(text: str) -> list[int]:
    t = text.strip().upper()
    if t in ("全部", "全频", "ALL", ""):
        return [1, 1, 1]
    out = [0, 0, 0]
    for part in re.split(r"[+＋,，、/\s]+", t):
        if not part:
            continue
        if part not in BANDS:
            raise ParamError(f"频段「{part}」：写 LOW / MID / HIGH，几段用 + 连起来")
        out[BANDS.index(part)] = 1
    return out


def bands_text(b: list[int]) -> str:
    return "全部" if all(b) else "+".join(n for n, on in zip(BANDS, b) if on)


def within(name: str, v: float, lo: float, hi: float) -> float:
    if not lo <= v <= hi:
        raise ParamError(f"{name} {v:g} 超出范围 {lo:g}–{hi:g}")
    return v


def dur(name: str, text: str, lo: float, hi: float) -> float:
    try:
        v = float(parse_dur(text))
    except PosError as e:
        raise ParamError(f"{name}：{e}") from e
    if not lo <= v <= hi:
        raise ParamError(f"{name} {text} 超出范围 {fmt_dur(Fraction(lo).limit_denominator(96))}–"
                         f"{fmt_dur(Fraction(hi).limit_denominator(96))}")
    return v


def ramp(vals: list[float], a: float, b: float) -> list[list[float]]:
    """几段的值在 [a, b] 里均匀铺开（一个值 = 一直是它）。"""
    if len(vals) == 1:
        return [[a, vals[0]]]
    return [[round(a + (b - a) * i / (len(vals) - 1), 6), v] for i, v in enumerate(vals)]


def dtext(beats: float) -> str:
    return fmt_dur(Fraction(beats).limit_denominator(96))


def compile_live(c: "Compiled5", p: "Project", err: Callable[..., None], hint: Callable[..., None],
                 where: Callable[..., str], samples: Callable[[str], Any] | None) -> None:
    from .compile import FX_ZH, _zh_pos, fx_tail_beats

    buses: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    fills: list[dict[str, Any]] = []
    sample_ids: dict[str, int] = {}

    for f in sorted((f for f in p.fx if f.kind in LIVE_KINDS), key=lambda f: (f.start, f.id)):
        zh = FX_ZH.get(f.kind, f.kind)
        w = where(f.start, f"效果 {f.id}（{zh}）")
        s0, s1 = float(f.start), float(f.end)
        # ---- 能力
        need = ENGINE_KINDS[f.kind]
        if f.kind == "release":
            how = RELEASE_KINDS.get(str(f.params.get("方式", "")).strip().lower())
            need = (how,) if how else need
        missing = [k for k in need if k not in c.caps.fx]
        if missing:
            have = [FX_ZH[k] for k in ("delay", "echo", "reverb") if k in c.caps.fx]
            err("caps_fx", w, f"这个引擎还不会「{zh}」", f.line,
                "这是现场效果里的；这个引擎能用的：" + (" / ".join(have) or "没有效果"))
            continue
        for k in need:
            if f.version and f.version != c.caps.fx[k]:
                hint("fx_version", w, f"谱是按 {zh} 第 {f.version} 版存的，引擎现在是第 {c.caps.fx[k]} 版，声音可能不一样", f.line)
                break
        bpm = c.tempo.bpm_at(s0)
        spb = 60.0 / bpm  # 一拍多少秒（按开始那一拍的速度）
        unknown = [k for k in f.params if k not in PARAMS[f.kind] and k != "legacy"]
        if unknown:
            err("fx_param", w, f"{zh} 没有「{unknown[0]}」这个参数（有：{' / '.join(PARAMS[f.kind])}）", f.line)
            continue

        def get(name: str, default: str | None = None, f: "Fx" = f) -> str | None:
            v = f.params.get(name)
            return default if v is None or str(v).strip() == "" else str(v).strip()

        # ---- 采样：不走处理组
        if f.kind == "sample":
            if s1 < s0:
                err("fx_range", w, f"结束 {_zh_pos(f.end)} 在开始 {_zh_pos(f.start)} 之前", f.line)
                continue
            if len(f.targets) != 1:
                err("fx_target", w, "采样的「目标」写音源：内置的名字（kick / snare / clap / hat / openhat / rim / tom / crash），"
                                    "或者谱旁边的音频文件名", f.line)
                continue
            src = f.targets[0]
            info = samples(src) if samples else None
            if info is None:
                err("sample_missing", w, f"找不到采样「{src}」", f.line,
                    "内置的有 kick / snare / clap / hat / openhat / rim / tom / crash；自己的采样把文件放在谱旁边，这里写文件名")
                continue
            if info.frames > SAMPLE_MAX_SECONDS * c.caps.sample_rate:
                err("sample_long", w, f"采样「{src}」有 {info.frames / c.caps.sample_rate:.1f} 秒，最长 {SAMPLE_MAX_SECONDS:g} 秒",
                    f.line, "长的声音做成素材，放在 Deck 上")
                continue
            try:
                rep = get("重复")
                every = dur("重复", rep, 1 / 16, 64.0) if rep else 0.0
                gain = within("音量", amount(get("音量", "0.8") or "0.8"), 0.0, 1.5)
            except ParamError as e:
                err("fx_param", w, str(e), f.line)
                continue
            if src not in sample_ids:
                if len(sample_ids) >= c.caps.sampler:
                    err("caps_sampler", w, f"一份谱最多用 {c.caps.sampler} 种采样", f.line)
                    continue
                sample_ids[src] = len(sample_ids)
                c.samples.append({"id": src, "path": info.path, "frames": info.frames})
            beats_at = [s0]
            while every > 0 and beats_at[-1] + every < s1 - 1e-9:
                beats_at.append(round(beats_at[-1] + every, 6))
            end_at = s1 if s1 > s0 else s0 + info.frames / c.caps.sample_rate / spb
            fills.append({"id": f.id, "start": s0, "end": end_at, "line": f.line})
            for i, b in enumerate(beats_at):
                c.hits.append({"id": f.id if i == 0 else "", "slot": sample_ids[src], "beat": b, "gain": round(gain, 4)})
            if s1 > s0:
                c.hits.append({"id": "", "slot": 0, "beat": s1, "stop": True})
            text = f"{src}" + (f" · 每 {dtext(every)} 一下，共 {len(beats_at)} 下" if every else " · 打一下") + f" · 音量 {gain:g}"
            c.live.append({"id": f.id, "kind": "sample", "name": "采样", "route": "master", "clips": [], "bus": "",
                           "start": s0, "end": end_at, "tail": 0.0, "text": text, "line": f.line, "rc": f.id,
                           "note": f.note, "scope": "直接进主输出（不是原曲里的声音）", "restore": "sample",
                           "last_ring": info.frames / c.caps.sample_rate})
            continue

        # ---- 路由和目标
        if f.route == "send":
            err("fx_route", w, f"{zh} 是插入处理：路由写「插入」（一个片段）、「组插入」（几个片段一起）或「总输出」", f.line,
                "发送只给 Delay / Echo / Reverb 用")
            continue
        route = ROUTE_ENGINE[f.route]
        if route not in c.caps.routes:
            err("caps_route", w, f"这个引擎还没有「{f.route}」这种路由", f.line)
            continue
        n_t = len(f.targets)
        if (route == "insert" and n_t != 1) or (route == "group" and n_t < 2) or (route == "master" and n_t):
            err("fx_target", w, {"insert": f"插入一次处理一个片段，这里写了 {n_t} 个", "group": "组插入要两个以上的片段（C1+C3）",
                                 "master": "总输出不写片段（目标写「总输出」）"}[route], f.line,
                "几个片段一起处理用「组插入」；整个主混音用「总输出」")
            continue
        members = [c.clip(t) for t in f.targets]
        if any(m is None for m in members):
            continue
        if len({m.clip.deck for m in members}) != len(members):
            err("fx_target", w, "组里的片段要在不同的 Deck 上", f.line)
            continue
        if s1 <= s0:
            err("fx_range", w, f"结束 {_zh_pos(f.end)} 不在开始 {_zh_pos(f.start)} 之后", f.line)
            continue
        off = [m for m in members if s0 >= m.stop or s1 <= m.start]
        if off or (route == "insert" and (s0 < members[0].start - 1e-9 or s1 > members[0].stop + 1e-9)):
            m = (off or members)[0]
            err("fx_range", w, f"{_zh_pos(f.start)} – {_zh_pos(f.end)} 这一段，{m.clip.id} 没有一直在放"
                               f"（它是 {_zh_pos(m.start)} – {_zh_pos(m.stop)}）", f.line)
            continue

        # ---- 参数 → 引擎里的件
        units: list[dict[str, Any]] = []
        tail = float(f.tail) if f.tail is not None else 0.0
        back: bool | None = None
        try:
            if f.kind == "trans":
                bd = bands(get("频段", "全部") or "")
                per = dur("周期", get("周期", "1/2拍") or "", 1 / 16, 16.0)
                duty = within("占空", amount(get("占空", "1/2") or ""), 0.05, 0.95)
                depth = within("深度", amount(get("深度", "1") or ""), 0.0, 1.0)
                units.append({"kind": "trans", "bands": bd, "p": {"period": per, "duty": duty, "depth": depth}})
                text = f"频段 {bands_text(bd)} · 周期 {dtext(per)} · 开 {duty:g} · 深度 {depth:g}"
            elif f.kind == "roll":
                bd = bands(get("频段", "全部") or "")
                lens = [dur("拍长", x, 1 / 32, 4.0) for x in stages(get("拍长", "1拍") or "")]
                mix = within("混合", amount(get("混合", "1") or ""), 0.0, 1.0)
                if lens[0] * spb > ROLL_MAX_SECONDS:
                    raise ParamError(f"拍长 {dtext(lens[0])} 在这个速度下超过 {ROLL_MAX_SECONDS:g} 秒")
                if len(lens) > 16:
                    raise ParamError("拍长最多写 16 段")
                if any(b > a for a, b in zip(lens, lens[1:])):
                    raise ParamError("拍长只能越写越短（1拍 → 1/2拍 → 1/4拍）：后面的每一遍放的是抓到的那一段的开头")
                step = (s1 - s0) / len(lens)
                ev = [[round(s0 + i * step, 6), lens[i]] for i in range(1, len(lens))]
                units.append({"kind": "roll", "bands": bd, "p": {"len": lens[0], "mix": mix}, "ev": ev})
                text = f"频段 {bands_text(bd)} · 在 {_zh_pos(f.start)} 抓 {' → '.join(dtext(x) for x in lens)}" + \
                       (f" · 混合 {mix:g}" if mix < 1 else "")
            elif f.kind in ("filter", "filter_echo"):
                mode_t = (get("模式", "高通") or "").lower()
                if mode_t not in MODES:
                    raise ParamError(f"模式「{get('模式')}」：写 高通 / 低通 / 带通")
                cut_t = get("截止")
                if not cut_t:
                    raise ParamError("要写截止频率（「截止 200Hz → 4kHz」，一个值 = 一直在那）")
                cuts = [hz(x) for x in stages(cut_t)]
                q = within("共振", float(amount(get("共振", "0.7") or "")), 0.3, 20.0)
                units.append({"kind": "filter", "p": {"mode": MODES[mode_t], "q": q},
                              "c": {"cut": [[b, round(math.log2(v), 6)] for b, v in ramp(cuts, s0, s1)]}})
                text = f"{MODE_ZH[MODES[mode_t]]} {' → '.join(f'{v:g}Hz' for v in cuts)}"
                if f.kind == "filter_echo":
                    beats = dur("拍长", get("拍长", "1/2拍") or "", 1 / 16, 4.0)
                    if beats * spb > ECHO_MAX_SECONDS:
                        raise ParamError(f"拍长 {dtext(beats)} 在这个速度下超过 {ECHO_MAX_SECONDS:g} 秒")
                    fb = within("反馈", amount(get("反馈", "0.6") or ""), 0.0, 0.95)
                    sends = [within("送出", amount(x), 0.0, 1.0) for x in stages(get("送出", "1") or "")]
                    wet = within("返回", amount(get("返回", "1") or ""), 0.0, 1.0)
                    if f.tail is None:
                        tail = min(16.0, fx_tail_beats("echo", {"feedback": fb, "beats": beats}, bpm))
                    units.append({"kind": "echo", "sub": "echo", "tail": tail, "p": {"beats": beats, "feedback": fb, "wet": wet},
                                  "c": {"send": ramp(sends, s0, s1)}})
                    text += f" · Echo {dtext(beats)}，反馈 {fb:g}" + \
                            (f"，送出 {' → '.join(f'{v:g}' for v in sends)}" if sends != [1.0] else "")
            elif f.kind == "spiral":
                beats = dur("拍长", get("拍长", "1/2拍") or "", 1 / 16, 2.0)
                semis_t = (get("音高", "+12") or "").replace("半音", "").replace("＋", "+").replace("−", "-").strip()
                try:
                    semis = within("音高", float(semis_t), -24.0, 24.0)
                except ValueError as e:
                    raise ParamError(f"音高「{get('音高')}」：写 +12 / -12（半音）") from e
                fb = within("反馈", amount(get("反馈", "0.7") or ""), 0.0, 0.98)
                mix = within("混合", amount(get("混合", "1/2") or ""), 0.0, 1.0)
                if f.tail is None:
                    tail = 4.0
                units.append({"kind": "spiral", "tail": tail,
                              "p": {"beats": beats, "feedback": fb, "mix": mix, "semis": semis, "glide": s1 - s0}})
                text = f"拍长 {dtext(beats)} · 音高 {semis:+g} 半音 · 反馈 {fb:g} · 混合 {mix:g}"
            elif f.kind == "noise":
                levels = [within("音量", amount(x), 0.0, 1.0) for x in stages(get("音量", "0 → 1/4") or "")]
                freqs = [hz(x) for x in stages(get("频率", "400Hz → 8kHz") or "")]
                if len(freqs) > 2:
                    raise ParamError("频率写一个值，或者「从 → 到」")
                q = within("共振", float(amount(get("共振", "1.5") or "")), 0.3, 20.0)
                units.append({"kind": "noise", "p": {"q": q, "from": freqs[0], "to": freqs[-1]},
                              "c": {"level": ramp(levels, s0, s1)}})
                text = f"音量 {' → '.join(f'{v:g}' for v in levels)} · {' → '.join(f'{v:g}Hz' for v in freqs)}（不是原曲里的声音）"
            elif f.kind in ("flanger", "phaser"):
                per = dur("周期", get("周期", "1小节") or "", 1 / 4, 64.0)
                depth = within("深度", amount(get("深度", "0.7") or ""), 0.0, 1.0)
                fb = within("反馈", amount(get("反馈", "1/2") or ""), 0.0, 0.95 if f.kind == "flanger" else 0.9)
                mix = within("混合", amount(get("混合", "1/2") or ""), 0.0, 1.0)
                units.append({"kind": f.kind, "p": {"period": per, "depth": depth, "feedback": fb, "mix": mix}})
                text = f"周期 {dtext(per)} · 深度 {depth:g} · 反馈 {fb:g} · 混合 {mix:g}"
            else:  # release
                how_t = get("方式")
                how = RELEASE_KINDS.get((how_t or "").lower())
                if not how:
                    raise ParamError("要写方式：「方式 Echo」「方式 刹车」或「方式 回转」")
                end_t = get("收尾")
                if end_t not in ("回原声", "退场"):
                    raise ParamError("要写收尾：「回原声」（放完回到这一组现在的干声）或「退场」（放完这一组保持静音）")
                back = end_t == "回原声"
                span = c.tempo.sec_at(s1) - c.tempo.sec_at(s0)
                pr: dict[str, float] = {}
                if how == "rel_echo":
                    beats = dur("拍长", get("拍长", "1/2拍") or "", 1 / 16, 4.0)
                    if beats * spb > ECHO_MAX_SECONDS:
                        raise ParamError(f"拍长 {dtext(beats)} 在这个速度下超过 {ECHO_MAX_SECONDS:g} 秒")
                    if get("反馈"):
                        fb = within("反馈", amount(get("反馈") or ""), 0.0, 0.95)
                    else:
                        fb = release_feedback(s1 - s0, beats)  # 不写：按时长算，回声正好响过声明的这一段
                    pr = {"beats": beats, "feedback": fb}
                    text = f"Echo {dtext(beats)}，反馈 {fb:g}，最多响 {dtext(s1 - s0)}"
                else:
                    for k in ("拍长", "反馈"):
                        if get(k):
                            raise ParamError(f"{RELEASE_ZH[how]}没有「{k}」这个参数")
                    cap = BRAKE_MAX_SECONDS if how == "brake" else SPIN_MAX_SECONDS
                    if span > cap + 1e-6:
                        raise ParamError(f"{RELEASE_ZH[how]}最长 {cap:g} 秒，这里写了 {span:.1f} 秒（{dtext(s1 - s0)}）")
                    pr = {"frames": round(span * c.caps.sample_rate)}
                    text = f"{RELEASE_ZH[how]} {dtext(s1 - s0)}"
                text += " · " + ("放完回原声" if back else "放完退场（这一组保持静音）")
                units.append({"kind": how, "p": pr, "back": back})
        except ParamError as e:
            err("fx_param", w, str(e), f.line)
            continue

        key = (route, tuple(sorted(f.targets)))
        bus = buses.setdefault(key, {"route": route, "clips": [m.clip.id for m in members], "units": [], "line": f.line})
        for i, u in enumerate(units):
            u.update(id=f.id if i == 0 else f"{f.id}.{u.pop('sub', i)}", start=s0, end=s1, src=f.id, line=f.line,
                     score_kind=f.kind)
            u.setdefault("tail", 0.0)
            u.pop("sub", None)
            bus["units"].append(u)
        c.live.append({"id": f.id, "kind": f.kind, "name": zh if f.kind != "release" else f"Release {RELEASE_ZH[units[0]['kind']]}",
                       "route": route, "clips": [m.clip.id for m in members], "bus": key, "start": s0, "end": s1,
                       "tail": max(u["tail"] for u in units), "text": text, "line": f.line, "rc": f.id, "note": f.note,
                       "back": back, "restore": {"roll": "capture", "release": "release", "noise": "start",
                                                 "spiral": "tail", "filter_echo": "tail"}.get(f.kind, "none"),
                       "pre": release_lead(units[0], s0, s1) if f.kind == "release" else 0.0})

    # ---- 组里：同一种效果一次开一个；Release 开始时收掉还开着的
    order = sorted(buses.items(), key=lambda kv: min(u["start"] for u in kv[1]["units"]))
    for n, (key, bus) in enumerate(order, 1):
        bus["id"] = f"B{n}"
        us = sorted(bus["units"], key=lambda u: (u["start"], u["id"]))
        rels = [u for u in us if u["kind"] in RELEASE_ZH]
        for a, b in zip(us, us[1:]):
            same = a["kind"] == b["kind"] or (a["kind"] in RELEASE_ZH and b["kind"] in RELEASE_ZH)
            if same and b["start"] < a["end"] + a["tail"] - 1e-9 and a["src"] != b["src"]:
                err("fx_overlap", where(b["start"], f"效果 {b['src']}"),
                    f"和 {a['src']} 是同一组里的同一种效果，{a['src']} 到 {_zh_pos(a['end'] + a['tail'])} 才收完", b["line"],
                    "一组里同一种效果一次开一个：把前一个早点关，或者把尾音写短")
        for r in rels:
            for u in us:
                if u is r or u["kind"] in RELEASE_ZH:
                    continue
                if u["start"] < r["start"] < u["end"] + u["tail"] - 1e-9:
                    # Release 接手：这一件到此为止，尾音也不留（残余在 Release 开始时清掉）
                    u["end"], u["tail"], u["cut_by"] = min(u["end"], r["start"]), 0.0, r["src"]
            if not r["back"]:
                nxt = min((u["start"] for u in us if u["start"] >= r["end"] - 1e-9 and u is not r), default=None)
                if nxt is not None:
                    r["until"] = nxt
        for lv in c.live:
            if lv.get("bus") == key:
                lv["bus"] = bus["id"]
                mine = [u for u in us if u["src"] == lv["id"]]
                lv["end"] = max(u["end"] for u in mine)
                lv["tail"] = max(u["end"] + u["tail"] for u in mine) - lv["end"]
                cut = next((u["cut_by"] for u in mine if u.get("cut_by")), None)
                if cut:
                    lv["text"] += f" · 到 {_zh_pos(lv['end'])} 被 {cut} 收掉"
                    lv["cut_by"] = cut
                rel = next((u for u in mine if u["kind"] in RELEASE_ZH), None)
                if rel is not None and not rel["back"]:
                    lv["muted_until"] = rel.get("until")
        bus["units"] = [u for u in us if u["end"] > u["start"] + 1e-9]
        bus["from"] = min(u["start"] for u in us)
        bus["to"] = max(max(u["end"] + u["tail"], u.get("until", 0.0) if u["kind"] in RELEASE_ZH and not u["back"] else 0.0)
                        for u in us)
        if len(bus["units"]) > 32:
            err("caps_units", where(bus["from"], f"处理组 {bus['id']}"), "一个处理组里最多 32 件效果", bus["line"])

    # ---- 组和组之间：一个片段同一时间只在一个组里；总输出处理时别的组不能开着
    def windows(bus: dict[str, Any]) -> list[tuple[float, float, dict[str, Any]]]:
        return [(u["start"], u["end"] + u["tail"], u) for u in bus["units"]]

    blist = [b for _, b in order]
    for i, a in enumerate(blist):
        for b in blist[i + 1:]:
            shared = set(a["clips"]) & set(b["clips"])
            if not shared and "master" not in (a["route"], b["route"]):
                continue
            hit = next(((ua, ub) for a0, a1, ua in windows(a) for b0, b1, ub in windows(b)
                        if a0 < b1 + BUS_MARGIN and b0 < a1 + BUS_MARGIN), None)
            if hit:
                ua, ub = hit
                first, second = (ua, ub) if (ua["start"], ua["id"]) <= (ub["start"], ub["id"]) else (ub, ua)
                what = "总输出在处理的时候，别的处理组不能同时开着" if "master" in (a["route"], b["route"]) else \
                    f"{' / '.join(sorted(shared))} 同一时间只能在一个处理组里"
                err("bus_overlap", where(second["start"], f"效果 {second['src']}"),
                    f"{what}：{first['src']} 到 {_zh_pos(first['end'] + first['tail'])} 才收完", second["line"],
                    f"两个处理组之间至少隔 {BUS_MARGIN:g} 拍；要一起处理就写成同一条路由、同样的目标")
    if len(blist) > c.caps.buses:
        err("caps_buses", "全场", f"处理组（路由 + 目标的组合）有 {len(blist)} 个，引擎最多 {c.caps.buses} 个", blist[c.caps.buses]["line"])
    for a, b in zip(sorted(fills, key=lambda x: x["start"]), sorted(fills, key=lambda x: x["start"])[1:]):
        if b["start"] < a["end"] - 1e-9:
            err("sample_overlap", where(b["start"], f"效果 {b['id']}"),
                f"和 {a['id']} 重叠（它到 {_zh_pos(a['end'])}）：一组采样到点会把还在响的都收掉，两组不能叠着", b["line"])
    c.hits.sort(key=lambda h: (h["beat"], 0 if h.get("stop") else 1))
    c.buses = [{"id": b["id"], "route": b["route"], "clips": b["clips"],
                "units": [{k: v for k, v in u.items() if k in ("id", "kind", "start", "end", "tail", "bands", "p", "c", "ev",
                                                                 "back", "until")} for u in b["units"]]} for b in blist]
    c.live.sort(key=lambda lv: (lv["start"], lv["id"]))


REL_ECHO_PRE_PERIODS = 4.0  # bus.hpp kRelEchoPrePeriods
REL_ECHO_PRE_MAX_BEATS = 8.0  # bus.hpp kRelEchoPreMaxBeats
SPIN_MAX_SPEED = 3.0  # dsp.hpp kSpinMaxSpeed


def release_feedback(span: float, beats: float) -> float:
    """Release Echo 没写反馈时用多少：让回声在声明的时长里衰减约 20 dB——时长里转 n 遍，每遍减 20 / n dB。
    太小（两三遍就没了）、太大（收不住）都不好听，夹在 0.4–0.85 之间。"""
    n = max(1.0, span / max(beats, 1e-6))
    return round(min(0.85, max(0.4, 10 ** (-20.0 / (20.0 * n)))), 2)


def release_lead(unit: dict[str, Any], s0: float, s1: float) -> float:
    """Release 开始之前引擎要先喂多久（拍），和 bus.hpp 的 unitLead 一样：Echo 带着反馈预卷的那几个周期；
    回转要倒着读回去的那一段；刹车不用。从中间开始时静音预跑要从这之前起（restore_need）。"""
    if unit["kind"] == "rel_echo":
        return min(REL_ECHO_PRE_MAX_BEATS, max(1.0, REL_ECHO_PRE_PERIODS * unit["p"].get("beats", 0.5)))
    if unit["kind"] == "spin":
        return 0.5 * (SPIN_MAX_SPEED - 1.0) * (s1 - s0) + 0.25
    return 0.0


def restore_need(lv: dict[str, Any], beat: float) -> tuple[float, bool] | None:
    """从全场第 beat 拍开始时，这条现场效果要从哪一拍起静音预跑才能和连续放过来一样。
    返回 (起点, 硬不硬)：硬 = 起点之后才开始就是错的（抓取型：Roll 没抓到、Release 没有可放的），软 = 尽量。None = 不用。"""
    s0, s1, tail = lv["start"], lv["end"], lv["tail"]
    how = lv["restore"]
    if how == "capture":
        return (s0 - 0.25, True) if s0 - 0.25 < beat < s1 + 0.25 else None
    if how == "release":
        pre = lv.get("pre", 0.0) + 1.0
        return (s0 - pre, True) if s0 - pre < beat < s1 else None
    if how == "start":
        return (s0, False) if s0 < beat < s1 else None
    if how == "tail":
        return (s0, False) if s0 < beat < s1 + tail else None
    if how == "sample":
        ring = lv.get("last_ring", 0.0)
        return (s0, False) if s0 < beat < s1 + ring else None
    return (max(s0, beat - 1.0), False) if s0 < beat < s1 else None
