"""网页后端（FastAPI，只绑 127.0.0.1）。所有业务都委托给 Studio。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..compiler import Compiled
from ..grid import Grid
from ..library.analysis import downbeat_from_bar1
from ..library.importer import browse
from ..library.store import Track
from ..mixxx.db import MixxxTrack
from ..setfile import SetFile
from ..studio import Studio, StudioError, mapping_version

STATIC = Path(__file__).parent / "static"


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
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        kind, payload = await asyncio.wait_for(q.get(), timeout=0.2)
                        yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        es = studio.engine_state()
                        cur = json.dumps(es, default=str)
                        if cur != last_status:
                            last_status = cur
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
    def api_set(sid: str) -> dict[str, Any]:
        s = studio.load(sid)
        return {"set": json.loads(s.to_json()), "compiled": compiled_json(studio.compile(s))}

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
    def api_play(sid: str) -> dict[str, Any]:
        return studio.play(sid).to_dict()

    @app.post("/api/stop")
    def api_stop() -> dict[str, Any]:
        studio.stop()
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

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
