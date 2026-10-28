"""演示模式：不需要 Mixxx。

合成几首 techno 片段 → 导入托管曲库 → 造一个假的 Mixxx 曲库 → 写一套示例 set，
引擎用 Node 模拟器（真正的 DJStudio.js 跑在假 Mixxx 里）。用来在没有 Mac / Mixxx 的地方试网页，
也是网页的端到端测试环境。声音是听不到的——模拟器只模拟控件和时间。
"""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .engine.link import SimLink
from .mixxx import fakedb, process
from .setfile import SetFile
from .studio import Studio
from .synth import techno, write_wav

LAYOUT = (("intro", 16), ("groove", 32), ("breakdown", 16), ("drop", 32), ("outro", 16))
TRACKS = [  # 文件名, bpm, first_beat, downbeat
    ("Demo One.wav", 126.0, 0.20, 0),
    ("Demo Two.wav", 128.0, 0.35, 2),
    ("Demo Three.wav", 130.0, 0.10, 1),
    ("Demo Four.wav", 132.0, 0.50, 3),
]


def setup_demo(root: Path, speed: float = 8.0) -> Studio:
    root = root.resolve()
    src = root / "source" / "Demo Pack"
    if not src.exists():
        src.mkdir(parents=True)
        for k, (name, bpm, fb, db) in enumerate(TRACKS):
            write_wav(src / name, techno(bpm, fb, db, LAYOUT, seed=k + 3))
    mixxx_dir = root / "mixxx"
    mixxx_dir.mkdir(exist_ok=True)
    lib = root / "Music" / "DJStudio"
    cfg = Config(home=root / "home", library=lib, mixxx_dir=mixxx_dir, browse_roots=[root, Path.home()])
    fresh_db = not cfg.mixxx_db.exists()
    if fresh_db:
        fakedb.create(cfg.mixxx_db, [lib])

    state = {"running": False}
    process.mixxx_running = lambda: state["running"]  # type: ignore[assignment]

    def launch() -> None:
        state["running"] = True

    def quit_(timeout: float = 30.0) -> bool:
        state["running"] = False
        return True

    process.launch_mixxx = launch  # type: ignore[assignment]
    process.quit_mixxx = quit_  # type: ignore[assignment]

    box: dict[str, Studio] = {}

    def link_factory() -> SimLink:
        st = box["s"]
        rows = []
        for t in st.store.all():
            m = st._mixxx_tracks([t]).get(t.id)
            if m and m.grid:
                rows.append({"id": str(m.id), "dur": m.duration, "bpm": m.grid.bpm, "first_beat": m.grid.first_beat})
        link = SimLink(rows, speed=speed)
        link.sim({"cmd": "queue", "ids": [str(i) for i in st.mixxx.autodj_queue()]})
        return link

    studio = Studio(cfg, link_factory=link_factory)
    box["s"] = studio
    res = studio.library.import_paths([str(src)])
    studio.library.wait_idle(120)
    if fresh_db:
        # 模拟「Mixxx 扫描 + 分析」：用已知的网格写进假曲库
        by_name = {Path(t.path).name: t for t in studio.store.all()}
        for name, bpm, fb, _ in TRACKS:
            t = by_name.get(name)
            if t:
                fakedb.add_track(cfg.mixxx_db, t.path, t.duration, bpm, fb, title=Path(name).stem)
        for t in studio.store.all():
            studio.library.enqueue(t.id)
        studio.library.wait_idle(120)
    if not studio.set_path("demo").exists():
        by_name = {Path(t.path).name: t for t in studio.store.all()}
        ids = [by_name[n].id for n, *_ in TRACKS]
        studio.save(SetFile.model_validate({
            "schema": "djset/2", "id": "demo", "name": "演示 set", "created": "2026-10-28",
            "source": "演示：合成曲目",
            "tracks": [{"id": i, "title": Path(n).stem} for i, (n, *_) in zip(ids, TRACKS)],
            "transitions": [
                {"technique": "longhand", "start": 61, "b_in": 1, "bars": 48, "note": "长交接 48 小节，B 半音量进"},
                {"technique": "instant", "start": 81, "b_in": 65, "note": "drop 对 drop，1 拍真空"},
                {"technique": "longhand", "start": 77, "b_in": 17, "bars": 32},
            ],
            "ending": {"mode": "fade", "bar": 97, "bars": 8},
        }))
    _ = res
    return studio
