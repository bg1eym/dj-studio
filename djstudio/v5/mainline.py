"""主线和工具：四台 Deck 同时放的时候，哪些片段是「这一首歌」（主线），哪些是加在上面的工具（打击乐循环、一句人声…），
主线上一首接一首的地方就是**主要的交接**。

- 主线 = 角色是 主曲 / 进曲 / 出曲 的片段（片段表里写的，或者场景里给这台 Deck 写的）。写了别的角色（节奏层、声线…）= 工具。
  哪里都没写角色的：有 16 小节以上直着放的一段、而且 LOW 不是从头切到尾 → 主线；只转 loop 的、很短的、LOW 一直切着的 → 工具。
- 交接 = 主线按起播的先后一首接一首：从下一首起播（进来）到上一首停（交出去）。换低频的那一拍 = 下一首的 LOW 从切着放进来的那一拍
  （没有就看上一首的 LOW 切掉的那一拍）。两首没有叠着放的 = 直接接上（起点 = 终点）。

只是给界面、hot cue 表、交接说明分类用的，不影响声音：引擎照谱执行，不看这个。
"""

from __future__ import annotations

from typing import Any

from .pos import BEATS_PER_BAR

MAIN_ROLES = ("主曲", "进曲", "出曲")
IDLE_ROLES = ("待命", "")
CUT_DB = -25.0  # 谱上的 7 点：全切
LONG_BEATS = 16 * BEATS_PER_BAR  # 没写角色时：直着放 16 小节以上才算一首歌


def roles_of(c: Any, cc: Any) -> list[str]:
    """这个片段有过的角色：片段表里写的 + 它在放的那些场景里给这台 Deck 写的（「待命」不算）。"""
    k = cc.clip
    out = [k.role] if k.role else []
    for sc in c.project.scenes:
        if float(sc.start) < cc.stop and float(sc.end) > cc.start:
            r = sc.roles.get(f"D{k.deck}", "")
            if r:
                out.append(r)
    return [r for r in dict.fromkeys(out) if r not in IDLE_ROLES]


def is_main(c: Any, cc: Any) -> bool:
    roles = roles_of(c, cc)
    if roles:
        return any(r in MAIN_ROLES for r in roles)
    straight = 0.0
    for i, s in enumerate(cc.segs):
        g1 = float(cc.segs[i + 1].g) if i + 1 < len(cc.segs) else cc.stop
        if not s.loop:
            straight = max(straight, g1 - float(s.g))
    lo = [float(p[1]) for p in cc.lanes.get("lo", [])] or [float(cc.clip.init.lo)]
    return straight >= LONG_BEATS and not all(v <= CUT_DB + 1e-6 for v in lo)


def _low_in(cc: Any) -> float | None:
    """LOW 从切着到放进来的那一拍（一下到位 = 那一拍；慢慢拧 = 开始拧的那一拍）。"""
    pts = cc.lanes.get("lo", [])
    for a, b in zip(pts, pts[1:]):
        if float(a[1]) <= CUT_DB + 1e-6 < float(b[1]):
            return float(b[0]) if len(b) == 3 else float(a[0])
    return None


def _low_out(cc: Any) -> float | None:
    pts = cc.lanes.get("lo", [])
    for a, b in zip(pts, pts[1:]):
        if float(b[1]) <= CUT_DB + 1e-6 < float(a[1]):
            return float(b[0]) if len(b) == 3 else float(a[0])
    return None


def handovers(c: Any) -> list[dict[str, Any]]:
    """主要的交接，按先后：{n, from, to, from_deck, to_deck, start, end, swap}（拍；swap = 换低频的那一拍，没有 = None）。"""
    mains = sorted((cc for cc in c.clips if is_main(c, cc)), key=lambda x: (x.start, x.clip.deck))
    out = []
    for a, b in zip(mains, mains[1:]):
        start = b.start
        end = max(start, min(a.stop, b.stop))
        swap = next((x for x in (_low_in(b), _low_out(a)) if x is not None and start - 1e-6 <= x <= end + 1e-6), None)
        out.append({"n": len(out) + 1, "from": a.clip.id, "to": b.clip.id, "from_deck": a.clip.deck, "to_deck": b.clip.deck,
                    "start": start, "end": end, "swap": swap})
    return out
