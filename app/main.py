"""FastAPI application — Web UI for Telegram media downloader."""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .db import (create_job, get_job, get_job_downloads, init_db,
                 list_jobs, load_session, update_job)
from .downloader import engine
from .media_scanner import (EpisodeFile, SERIES_FILE_RE, find_movie_in_library,
                            find_series_in_library, get_existing_episodes,
                            get_series_dirs, scan_series_files,
                            infer_series_name_and_year)
from .telegram_client import get_client, get_dialogs, search_media

app = FastAPI(title="TG Media Downloader")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ── Lifecycle ───────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    settings.auto_discover_media()
    # Try to restore Telegram session
    stored = load_session("default")
    if stored:
        try:
            client = await get_client()
            engine.set_client(client)
        except Exception:
            pass  # Will prompt user to reconnect


# ── Routes ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    jobs = list_jobs(20)
    connected = engine._tg_client is not None and engine._tg_client.is_connected()
    return templates.TemplateResponse(request, "index.html", {
        "jobs": jobs,
        "connected": connected,
        "series_paths": [str(p) for p in settings.SERIES_PATHS],
        "movies_paths": [str(p) for p in settings.MOVIES_PATHS],
        "downloads_dir": str(settings.DOWNLOADS_DIR),
        "media_dir": str(settings.MEDIA_DIR),
    })


@app.post("/api/connect")
async def api_connect(phone: str = Form(...)):
    """Connect to Telegram with phone number."""
    try:
        client = await get_client(phone=phone)
        engine.set_client(client)
        return {"success": True, "message": "Código enviado. Revisa Telegram."}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/status")
async def api_status():
    """Check connection status + server info."""
    connected = False
    me = None
    if engine._tg_client and engine._tg_client.is_connected():
        connected = True
        try:
            me_obj = await engine._tg_client.get_me()
            me = {"first_name": me_obj.first_name, "username": me_obj.username}
        except Exception:
            pass
    return {
        "connected": connected,
        "user": me,
        "running": engine.is_running,
        "downloads_dir": str(settings.DOWNLOADS_DIR),
        "media_dir": str(settings.MEDIA_DIR),
        "series_paths": [str(p) for p in settings.SERIES_PATHS],
        "movies_paths": [str(p) for p in settings.MOVIES_PATHS],
    }


@app.get("/api/channels")
async def api_channels():
    """List available Telegram channels."""
    if not engine._tg_client or not engine._tg_client.is_connected():
        return {"error": "Not connected"}
    try:
        channels = await get_dialogs(engine._tg_client)
        return {"channels": channels}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/scan")
async def api_scan(channel_id: int = Form(...), query: str = Form(...),
                   kind: str = Form("series")):
    """Search a channel for media matching query."""
    if not engine._tg_client or not engine._tg_client.is_connected():
        return {"error": "Not connected"}

    # Find channel name
    channels = await get_dialogs(engine._tg_client)
    channel_name = next((c["name"] for c in channels if c["id"] == channel_id), str(channel_id))

    job_id = create_job(kind, query, channel_id, channel_name)

    if kind == "series":
        asyncio.ensure_future(engine.scan_series(job_id, channel_id, query))
    else:
        asyncio.ensure_future(engine.scan_movie(job_id, channel_id, query))

    return {"job_id": job_id, "status": "scanning"}


@app.get("/api/jobs")
async def api_jobs():
    """List all jobs."""
    return {"jobs": list_jobs(50)}


@app.get("/api/jobs/{job_id}")
async def api_job_detail(job_id: int):
    """Get job + downloads detail."""
    job = get_job(job_id)
    if not job:
        return {"error": "Not found"}
    # Parse progress JSON
    try:
        job["progress"] = json.loads(job["progress"]) if isinstance(job["progress"], str) else job["progress"]
    except (json.JSONDecodeError, TypeError):
        job["progress"] = {}
    downloads = get_job_downloads(job_id)
    return {"job": job, "downloads": downloads}


@app.post("/api/jobs/{job_id}/run")
async def api_run_job(job_id: int):
    """Start downloading queued files."""
    job = get_job(job_id)
    if not job:
        return {"error": "Not found"}

    if job["status"] not in ("pending", "scanning"):
        return {"error": f"Job is {job['status']}, not ready to run"}

    if job["kind"] == "series":
        asyncio.ensure_future(engine.run_series_job(job_id))
    else:
        asyncio.ensure_future(engine.run_movie_job(job_id))

    return {"status": "started"}


@app.get("/api/series")
async def api_series():
    """List all series found in media library."""
    dirs = get_series_dirs()
    result = []
    for s in dirs:
        eps = scan_series_files(s.series_dir)
        seasons = {}
        for ep in eps:
            seasons.setdefault(ep.season, 0)
            seasons[ep.season] += 1
        result.append({
            "name": s.series_name,
            "year": s.series_year,
            "dir": str(s.series_dir),
            "episodes": len(eps),
            "seasons": dict(sorted(seasons.items())),
        })
    return {"series": sorted(result, key=lambda x: x["name"].lower())}


@app.post("/api/check-series")
async def api_check_series(query: str = Form(...)):
    """Check if a series exists in library and what episodes are present."""
    info, eps = find_series_in_library(query)
    if not info:
        return {"exists": False, "series": None, "episodes": []}
    return {
        "exists": True,
        "series": {
            "name": info.series_name,
            "year": info.series_year,
            "dir": str(info.series_dir),
        },
        "episodes": [
            {"season": e.season, "episode": e.episode,
             "title": e.title, "path": str(e.file_path)}
            for e in eps
        ],
    }


@app.post("/api/check-movie")
async def api_check_movie(query: str = Form(...)):
    """Check if a movie exists in library."""
    movie = find_movie_in_library(query)
    if not movie:
        return {"exists": False}
    return {
        "exists": True,
        "title": movie.title,
        "year": movie.year,
        "path": str(movie.file_path),
    }
