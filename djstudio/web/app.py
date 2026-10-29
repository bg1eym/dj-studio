"""网页后端（FastAPI，只绑 127.0.0.1）。所有业务都委托给 Studio。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..compiler import Compiled
from ..grid import Grid
from ..library.analysis import downbeat_from_bar1
from ..library.audio import AUDIO_EXTS
from ..library.importer import browse
from ..library.store import Track
from ..mixxx.db import MixxxTrack
from ..selfupdate import version_text
from ..setfile import SetFile
from ..studio import Studio, StudioError, mapping_version

ENGINE_PUSH_S = 0.1

STATIC = Path(__file__).parent / "static"


# 更新代码后浏览器一刷新就是新界面：网页文件每次都向服务确认是否有新版本（不用旧缓存）
NO_CACHE = {"Cache-Control": "no-cache"}


class _NoCacheStatic(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        resp = super().file_response(*args, **kwargs)
        resp.headers.update(NO_CACHE)
        return resp


def create_app(studio: Studio) -> FastAPI:
    app = FastAPI(title="DJ Studio", docs_url="/api/docs")
    app.state.studio = studio

    @app.exception_handler(StudioError)
    async def _studio_err(_: Request, e: StudioError) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def _perm_err(_: Request, e: PermissionError) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=403)

    # ------------------------------------------------------------ 状态 / 事件

    def state() -> dict[str, Any]:
        cond = studio.conductor
        return {
            "engine": studio.engine_state(),
            "progress": cond.progress.to_dict() if cond else None,
            "playing": bool(cond and cond.running),
            "job": studio.job.to_dict() if studio.job else None,
            "prepared": studio.prepared(),
            "analysis": studio.library.queue_state(),
            "mapping_version": mapping_version(),
            "version": version_text(),
            "engine_mode": "builtin" if studio.builtin else "mixxx",
        }

    @app.get("/api/state")
    def get_state() -> dict[str, Any]:
        return state()

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1000)

        def listener(kind: str, payload: Any) -> None:
            def put() -> None:
                if not q.full():
                    q.put_nowait((kind, payload))
            loop.call_soon_threadsafe(put)

        studio.listeners.append(listener)

        async def gen():  # type: ignore[no-untyped-def]
            try:
                yield f"event: state\ndata: {json.dumps(state(), ensure_ascii=False, default=str)}\n\n"
                last_status = None
                last_push = 0.0
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        kind, payload = await asyncio.wait_for(q.get(), timeout=0.05)
                        yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        pass
                    # 引擎状态（含旋钮读数）每 100ms 一帧，和引擎上报同频：网页混音台靠它动
                    now = time.monotonic()
                    if now - last_push >= ENGINE_PUSH_S:
                        cur = json.dumps(studio.engine_state(), default=str)
                        if cur != last_status:
                            last_status, last_push = cur, now
                            yield f"event: engine_state\ndata: {cur}\n\n"
            finally:
                if listener in studio.listeners:
                    studio.listeners.remove(listener)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ 曲库

    def track_json(t: Track, m: MixxxTrack | None, full: bool = False) -> dict[str, Any]:
        a = t.analysis or {}
        grid = None
        if m and m.grid and m.analyzed:
            g = Grid(m.grid.bpm, m.grid.first_beat, 0, m.duration or t.duration)
            src = "mixxx"
        elif a:
            g = Grid(a["own_grid"]["bpm"], a["own_grid"]["first_beat"], 0, t.duration)
            src = "own"
        else:
            g = None
            src = None
        if g:
            db = downbeat_from_bar1(g, t.bar1) if t.bar1 is not None else 0
            g = Grid(g.bpm, g.first_beat, db, g.duration)
            grid = {"bpm": g.bpm, "first_beat": g.first_beat, "downbeat": db, "bar1": g.bar_time(1),
                    "bars": int(g.last_bar), "source": src}
        out: dict[str, Any] = {
            "id": t.id, "title": t.title, "artist": t.artist, "album": t.album, "display": t.display,
            "path": t.path, "duration": t.duration, "codec": t.codec, "added_at": t.added_at,
            "bar1": t.bar1, "bar1_confirmed": t.bar1_confirmed, "note": t.note,
            "analyzed": bool(a), "analysis_error": t.analysis_error,
            "own_bpm": a.get("own_grid", {}).get("bpm"),
            "in_mixxx": m is not None,
            "mixxx_analyzed": bool(m and m.analyzed),
            "mixxx_tag_grid": bool(m and m.metadata_grid),
            "mixxx_bpm": m.bpm if m else None,
            "key": m.key if m else None,
            "grid": grid,
            "grid_check": a.get("grid_check"),
            "downbeat_conf": a.get("downbeat_conf"),
            "cues": a.get("cues"),
        }
        if full:
            out["sections"] = a.get("sections", [])
            out["bars_feat"] = a.get("bars", [])
        return out

    @app.get("/api/browse")
    def api_browse(path: str | None = None) -> dict[str, Any]:
        try:
            return browse(path, studio.cfg.browse_roots)
        except (FileNotFoundError, NotADirectoryError) as e:
            raise HTTPException(404, str(e)) from e

    @app.put("/api/upload")
    async def api_upload(request: Request, name: str) -> dict[str, Any]:
        """网页拖进来的文件（请求体就是文件内容）。音频进托管曲库并开始分析；PDF 当作打碟谱。"""
        fname = Path(name).name.strip()
        ext = Path(fname).suffix.lower()
        if not fname or fname.startswith(".") or (ext != ".pdf" and ext not in AUDIO_EXTS):
            raise HTTPException(400, f"不认识的文件：{name}（要音频文件或打碟谱 PDF）")
        dest_dir = studio.plans_dir if ext == ".pdf" else studio.inbox_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest_dir / f".{fname}.part"
        size = 0
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                size += len(chunk)
        if not size:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, f"{fname} 是空文件")
        dest = dest_dir / fname
        tmp.replace(dest)
        if ext == ".pdf":
            dest.touch()  # 最近放进来的打碟谱 = 当前打碟谱
            st = studio.plan_status()
            if st and st.get("error"):
                dest.rename(dest.with_suffix(".pdf.bad"))
                raise HTTPException(422, f"读不了这份打碟谱：{st['error']}")
            studio.find_plan_tracks()  # 谱上还缺的歌去电脑里找；齐了自动开始分析
            return {"kind": "plan", "plan": st}
        res = studio.library.import_paths([str(dest)], trusted=True)
        dest.unlink(missing_ok=True)  # 已复制进托管曲库
        if res.failed:
            raise HTTPException(422, f"{fname}：{res.failed[0][1]}")
        tid = (res.added or res.existing or [None])[0]
        studio.maybe_auto()
        return {"kind": "audio", "track": tid, "existing": bool(res.existing)}

    @app.get("/api/plan")
    def api_plan() -> dict[str, Any] | None:
        st = studio.plan_status()
        return {**st, "finder": studio.finder} if st else None

    @app.post("/api/auto")
    def api_auto() -> dict[str, Any]:
        return studio.auto_prepare().to_dict()

    @app.post("/api/import")
    def api_import(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        res = studio.library.import_paths(list(body.get("paths", [])))
        return {"added": res.added, "existing": res.existing, "failed": res.failed}

    @app.get("/api/tracks")
    def api_tracks(q: str | None = None) -> list[dict[str, Any]]:
        tracks = studio.store.search(q, 200) if q else studio.store.all()
        mx = studio._mixxx_tracks(tracks)
        return [track_json(t, mx.get(t.id)) for t in tracks]

    def _track(tid: str) -> Track:
        t = studio.store.get(tid)
        if not t:
            raise HTTPException(404, "没有这首歌")
        return t

    @app.get("/api/tracks/{tid}")
    def api_track(tid: str) -> dict[str, Any]:
        t = _track(tid)
        return track_json(t, studio._mixxx_tracks([t]).get(t.id), full=True)

    @app.patch("/api/tracks/{tid}")
    def api_track_patch(tid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        t = _track(tid)
        allowed = {"title", "artist", "note", "bar1", "bar1_confirmed"}
        fields = {k: v for k, v in body.items() if k in allowed}
        studio.store.update(tid, **fields)
        if "bar1" in fields:
            studio.library.enqueue(tid)  # 按新的小节线重新切段落
        return api_track(t.id)

    @app.post("/api/tracks/{tid}/reanalyze")
    def api_reanalyze(tid: str) -> dict[str, Any]:
        _track(tid)
        studio.library.enqueue(tid)
        return {"ok": True}

    @app.get("/api/tracks/{tid}/waveform")
    def api_waveform(tid: str) -> FileResponse:
        p = studio.store.waveform_path(_track(tid).id)
        if not p.exists():
            raise HTTPException(404, "还没分析")
        return FileResponse(p, media_type="application/json")

    @app.get("/api/tracks/{tid}/audio")
    def api_audio(tid: str) -> FileResponse:
        # 只按曲库里登记的路径提供文件（不接受任何外部路径）
        p = Path(_track(tid).path)
        if not p.exists():
            raise HTTPException(404, "文件不存在")
        return FileResponse(p)

    @app.get("/api/grids/plan")
    def api_grid_plan() -> list[dict[str, Any]]:
        return studio.grid_fix_plan()

    @app.post("/api/grids/fix")
    def api_grid_fix(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return studio.fix_grids(body.get("track_ids")).to_dict()

    @app.post("/api/sync")
    def api_sync(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return studio.sync_mixxx(body.get("track_ids")).to_dict()

    # ------------------------------------------------------------ set

    def compiled_json(c: Compiled) -> dict[str, Any]:
        tracks = []
        for st, info in zip(c.set.tracks, c.tracks):
            tracks.append({
                "id": st.id, "title": st.title,
                "bpm": info.grid.bpm if info else None,
                "bars": round(info.grid.last_bar, 2) if info and info.grid.duration else None,
                "duration": info.grid.duration if info else None,
                "bar1_time": info.grid.bar_time(1) if info and info.grid.bpm > 0 else None,
            })
        return {
            "ok": c.ok,
            "issues": [{"level": i.level, "where": i.where, "msg": i.msg} for i in c.issues],
            "schedule": [vars(s) for s in c.schedule],
            "duration": c.duration,
            "programs": c.programs,
            "ending": c.ending,
            "tracks": tracks,
        }

    @app.get("/api/sets")
    def api_sets() -> list[dict[str, Any]]:
        return studio.list_sets()

    @app.get("/api/sets/{sid}")
    def api_set(sid: str, rehearse_from: int = 0) -> dict[str, Any]:
        """rehearse_from = k：预演从第 k 段过渡开始时实际跑的那份（截掉前面的曲目）。"""
        s = studio.rehearsal_set(studio.load(sid), rehearse_from)
        return {"set": json.loads(s.to_json()), "compiled": compiled_json(studio.compile(s))}

    @app.post("/api/sets/{sid}/rehearse")
    def api_rehearse(sid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        speed = float(body.get("speed") or 1.0)
        if not 0.25 <= speed <= 16:
            raise HTTPException(400, "预演倍速应在 0.25–16 之间")
        return studio.rehearse(sid, speed, int(body.get("from") or 0)).to_dict()

    def parse_set(body: dict[str, Any]) -> SetFile:
        try:
            return SetFile.model_validate(body)
        except ValidationError as e:
            msgs = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
            raise HTTPException(422, {"error": "set 格式错误", "details": msgs}) from e

    @app.post("/api/sets/validate")
    def api_validate(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return compiled_json(studio.compile(parse_set(body)))

    @app.put("/api/sets/{sid}")
    def api_set_put(sid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        s = parse_set(body)
        if s.id != sid:
            raise HTTPException(400, "set id 与地址不一致")
        studio.save(s)
        return {"set": json.loads(s.to_json()), "compiled": compiled_json(studio.compile(s))}

    @app.delete("/api/sets/{sid}")
    def api_set_delete(sid: str) -> dict[str, Any]:
        p = studio.set_path(sid)
        if p.exists():
            p.rename(p.with_suffix(".json.deleted"))
        return {"ok": True}

    @app.post("/api/sets/{sid}/prepare")
    def api_prepare(sid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return studio.prepare(sid, body.get("settle")).to_dict()

    @app.post("/api/sets/{sid}/play")
    def api_play(sid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        # 内置引擎可以倍速、从第 k 段过渡开始；连 Mixxx 时只能从头按原速
        speed = float(body.get("speed") or 1.0)
        if not 0.25 <= speed <= 16:
            raise HTTPException(400, "倍速应在 0.25–16 之间")
        return studio.play(sid, speed, int(body.get("from") or 0)).to_dict()

    @app.post("/api/stop")
    def api_stop() -> dict[str, Any]:
        studio.stop()
        return {"ok": True}

    @app.post("/api/quit")
    def api_quit() -> dict[str, Any]:
        """退出 DJ Studio（应用没有窗口，从网页退出）：停演出、关引擎，然后结束进程。"""
        import os
        import threading

        studio.shutdown()
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return {"ok": True}

    # ------------------------------------------------------------ 体检 / 探测

    @app.get("/api/doctor")
    def api_doctor() -> list[dict[str, str]]:
        return studio.doctor()

    @app.post("/api/probe")
    def api_probe() -> dict[str, Any]:
        return studio.probe().to_dict()

    @app.post("/api/engine/reconnect")
    def api_reconnect() -> dict[str, Any]:
        studio.disconnect()
        try:
            studio.client()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e
        return studio.engine_state()

    # ------------------------------------------------------------ 前端

    app.mount("/static", _NoCacheStatic(directory=STATIC), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers=NO_CACHE)

    return app
