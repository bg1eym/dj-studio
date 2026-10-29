"""命令行入口：`djstudio serve`（默认）/ app / selftest / demo / doctor / validate / import / prepare / play / probe。"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path


def _serve(studio, host: str, port: int, open_browser: bool) -> None:  # type: ignore[no-untyped-def]
    import uvicorn

    from .selfupdate import free_port, version_text
    from .web.app import create_app

    if not free_port(host, port):
        raise SystemExit(1)
    app = create_app(studio)
    url = f"http://{host}:{port}/"
    print(f"DJ Studio {version_text()} → {url}")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=2)


def _running_version(host: str, port: int) -> str | None:
    """端口上已经开着的 DJ Studio 是哪个版本（没开 / 不是 DJ Studio → None）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=1.5) as r:
            return json.loads(r.read()).get("version")
    except Exception:  # noqa: BLE001
        return None


def _app() -> int:
    """DJ Studio.app（双击打开）：日志写到 ~/DJStudio/logs/app.log；同一版本已经开着就只打开网页。"""
    from .config import get_config
    from .selfupdate import version_text
    from .studio import Studio

    cfg = get_config()
    cfg.ensure_dirs()
    log = open(cfg.logs_dir / "app.log", "a", buffering=1, encoding="utf-8")  # noqa: SIM115
    sys.stdout = sys.stderr = log
    print(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} DJ Studio {version_text()} 启动")
    url = f"http://{cfg.host}:{cfg.port}/"
    if _running_version(cfg.host, cfg.port) == version_text():
        webbrowser.open(url)
        return 0
    _serve(Studio(cfg), cfg.host, cfg.port, True)
    return 0


def _selftest() -> int:
    """自检（打包好的应用在构建机上跑；用户也可以跑）：找得到声音引擎、能读文件、能解码、引擎能起来、声音输出情况。"""
    from .engine.bundled import engine_binary, system_mappings
    from .engine.client import EngineClient
    from .engine.link import EngineLink
    from .library.audio import decode_mono, probe
    from .selfupdate import version_text
    from .synth import techno, write_wav
    from .web.app import STATIC

    res: dict = {"version": version_text(), "frozen": bool(getattr(sys, "frozen", False)),
                 "web": (STATIC / "index.html").exists()}
    binary = engine_binary()
    res["engine"] = str(binary) if binary else None
    res["system_mappings"] = str(system_mappings(binary)) if binary else None
    ok = bool(binary and res["system_mappings"] and res["web"])
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "t.wav"
        write_wav(wav, techno(128.0, 0.1, 0, (("groove", 8),), seed=1))
        try:
            p = probe(wav)
            res["probe"] = {"duration": round(p.duration, 3), "samplerate": p.samplerate}
            res["decode_samples"] = int(len(decode_mono(wav)))
        except Exception as e:  # noqa: BLE001
            res["decode_error"] = str(e)
            ok = False
        if binary:
            link = EngineLink(binary, [{"id": "t", "path": str(wav), "bpm": 128.0, "first_beat": 0.1}], audio=True)
            try:
                client = EngineClient(link)
                res["engine_online"] = client.wait_until(client.online, 20)
                client.send({"t": "hello"})
                client.wait_until(lambda: client.version is not None, 5)
                res["engine_script"] = client.version
                res["audio"] = link.audio()
                ok = ok and res["engine_online"]
            finally:
                try:
                    link.sim({"cmd": "quit"})
                except Exception:  # noqa: BLE001
                    pass
                link.close()
    res["ok"] = ok
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if ok else 1


def _wait_job(job) -> int:  # type: ignore[no-untyped-def]
    seen = 0
    while True:
        for line in job.log[seen:]:
            print(line)
        seen = len(job.log)
        if job.state != "running":
            return 0 if job.state == "ok" else 1
        time.sleep(0.3)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="djstudio", description="DJ Studio：Mixxx 驱动的自动打碟 Web App")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="启动网页（默认）")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--no-browser", action="store_true")
    d = sub.add_parser("demo", help="演示模式：合成曲目 + 模拟 Mixxx，不需要真 Mixxx")
    d.add_argument("--dir", default=None, help="演示数据目录（默认临时目录）")
    d.add_argument("--speed", type=float, default=8.0, help="模拟器倍速")
    d.add_argument("--port", type=int, default=8431)
    d.add_argument("--no-browser", action="store_true")
    sub.add_parser("app", help="DJ Studio.app 用：启动网页并打开浏览器（日志写文件，已经开着就只打开网页）")
    sub.add_parser("selftest", help="自检：声音引擎、解码、声音输出")
    sub.add_parser("doctor", help="环境体检")
    v = sub.add_parser("validate", help="校验 set（id 或 json 文件路径）")
    v.add_argument("set")
    i = sub.add_parser("import", help="导入音频文件 / 文件夹到托管曲库")
    i.add_argument("paths", nargs="+")
    p = sub.add_parser("prepare", help="准备 set（重启 Mixxx、部署引擎、写 AutoDJ 队列）")
    p.add_argument("set")
    pl = sub.add_parser("play", help="在终端里播放已准备好的 set（Ctrl-C = 淡出停止）")
    pl.add_argument("set")
    sub.add_parser("probe", help="真机探测（需要 Mixxx 运行、deck1 有一首已分析的歌）")
    args = ap.parse_args(argv)
    cmd = args.cmd or "serve"

    if cmd == "demo":
        from .demo import setup_demo

        root = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="djstudio-demo-"))
        print(f"准备演示数据：{root}（合成曲目 + 分析，约半分钟）")
        studio = setup_demo(root, args.speed)
        _serve(studio, "127.0.0.1", args.port, not args.no_browser)
        return 0

    if cmd == "app":
        return _app()
    if cmd == "selftest":
        return _selftest()
    if cmd == "serve":
        from .selfupdate import self_update

        self_update(sys.argv[1:] if argv is None else argv)  # 拉到新代码会重启自己

    from .studio import Studio

    studio = Studio()
    if cmd == "serve":
        # 不带子命令（直接 `djstudio`）时 serve 的参数不会被解析，取默认值
        port = getattr(args, "port", None) or studio.cfg.port
        _serve(studio, studio.cfg.host, port, not getattr(args, "no_browser", False))
        return 0
    if cmd == "doctor":
        rc = 0
        for r in studio.doctor():
            mark = {"ok": "✓", "warn": "!", "error": "✗"}[r["level"]]
            print(f"{mark} {r['name']}：{r['detail']}")
            rc |= r["level"] == "error"
        return rc
    if cmd == "validate":
        from .setfile import load_set

        path = Path(args.set)
        st = load_set(path) if path.suffix == ".json" else studio.load(args.set)
        c = studio.compile(st)
        for iss in c.issues:
            print(iss)
        print("✓ 校验通过" if c.ok else "✗ 校验没通过", f"（约 {c.duration / 60:.1f} 分钟）")
        return 0 if c.ok else 1
    if cmd == "import":
        res = studio.library.import_paths(args.paths)
        print(f"导入 {len(res.added)} 首，已存在 {len(res.existing)} 首，失败 {len(res.failed)} 首")
        for f, e in res.failed:
            print(f"  ✗ {f}：{e}")
        print("分析中…")
        studio.library.wait_idle(3600)
        return 0
    if cmd == "prepare":
        return _wait_job(studio.prepare(args.set))
    if cmd == "probe":
        rc = _wait_job(studio.probe())
        if studio.job and studio.job.result:
            print(json.dumps(studio.job.result, ensure_ascii=False, indent=2))
        return rc
    if cmd == "play":
        studio.play(args.set)
        cond = studio.conductor
        assert cond
        seen = 0
        try:
            while cond.running:
                for line in cond.progress.log[seen:]:
                    print(line)
                seen = len(cond.progress.log)
                time.sleep(0.3)
        except KeyboardInterrupt:
            print("淡出停止…")
            cond.request_stop()
            cond.join(30)
        for line in cond.progress.log[seen:]:
            print(line)
        return 0 if cond.progress.stage == "done" else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
