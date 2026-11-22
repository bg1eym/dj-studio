"""v5 的演示：不用自己的曲子，就能把四 Deck 的整条路走一遍。

按示例谱（docs/score_v5_example.md）的素材表合成五个音频（BPM、第 1 小节、小节数都照表），和谱放进同一个文件夹，
再像用户那样「导入文件夹」：找曲子 → 分析 → 编译。引擎是内置的（有声音引擎就用它，没有就用模拟器）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..config import Config
from ..mixxx import fakedb
from ..studio import Studio
from ..synth import techno, write_wav
from . import score


def example_path() -> Path:
    """示例谱：源码里在仓库的 docs/，打包的 App 里在 djstudio/docs/。"""
    from ..plan.mdplan import score_doc

    f = score_doc("score_v5_example.md")
    if f is None:
        raise RuntimeError("找不到示例谱 score_v5_example.md")
    return f


def layout(bars: int) -> tuple[tuple[str, int], ...]:
    """把小节数分成 intro / groove / breakdown / drop / outro（合成器认这五种段落）。短素材整段都是 groove。"""
    if bars <= 32:
        return (("groove", bars),)
    unit = bars // 8
    return (("intro", unit), ("groove", 2 * unit), ("breakdown", unit), ("drop", 3 * unit), ("outro", bars - 7 * unit))


def make_folder(folder: Path, example: Path | None = None) -> Path:
    """演示用的文件夹：示例谱 + 按它的素材表合成的原曲。已经有的文件不重做。"""
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / "四 Deck 场景示例.md"
    if not md.exists():
        shutil.copyfile(example or example_path(), md)
    have_mp3 = bool(shutil.which("ffmpeg"))
    if not have_mp3 and ".mp3" in md.read_text(encoding="utf-8"):
        # 没有 ffmpeg 就做不出 MP3：演示改用 WAV（谱里的文件名跟着改）
        md.write_text(md.read_text(encoding="utf-8").replace(".mp3", ".wav"), encoding="utf-8")
    res = score.parse(md)
    if res.project is None:
        raise RuntimeError("示例谱读不了：" + "；".join(str(i) for i in res.issues[:3]))
    for k, a in enumerate(res.project.assets):
        out = folder / a.file
        if out.exists():
            continue
        wav = out.with_suffix(".wav")
        write_wav(wav, techno(a.bpm, max(0.0, a.bar1_s or 0.0), 0, layout(a.bars or 16), seed=k + 21))
        if out.suffix.lower() != ".wav":
            # 谱上写的是 .mp3：演示也走「谱 + 原曲 MP3 同一个文件夹」这条路
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-codec:a", "libmp3lame", "-b:a", "192k", str(out)],
                           check=True)
            wav.unlink()
    return md


def setup_demo5(root: Path) -> Studio:
    root = root.resolve()
    (root / "mixxx").mkdir(parents=True, exist_ok=True)
    cfg = Config(home=root / "home", library=root / "Music" / "DJStudio", mixxx_dir=root / "mixxx",
                 browse_roots=[root, Path.home()], engine="builtin")
    if not cfg.mixxx_db.exists():
        fakedb.create(cfg.mixxx_db)
    studio = Studio(cfg)
    folder = root / "四 Deck 场景示例"
    make_folder(folder)
    studio.v5.import_folder(folder)
    return studio
