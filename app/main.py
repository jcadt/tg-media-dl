"""FastAPI application — multi-user TG Media Downloader with Authentik OIDC."""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import (
    exchange_code,
    generate_pkce,
    get_auth_url,
    get_userinfo,
    make_session_token,
    read_session_token,
)
from .config import settings
from .db import (
    create_job,
    create_request,
    get_job,
    get_job_downloads,
    get_request,
    get_setting,
    init_db,
    list_jobs,
    list_pending_requests,
    list_requests,
    load_session,
    save_session,
    set_setting,
    update_job,
    update_request,
    upsert_user,
    get_user,
    get_user_by_email,
)
from .downloader import engine
from .media_scanner import (
    SERIES_FILE_RE,
    find_movie_in_library,
    find_series_in_library,
    get_existing_episodes,
    get_series_dirs,
    scan_series_files,
    infer_series_name_and_year,
)
from .telegram_client import get_client, get_dialogs, search_media

app = FastAPI(title="TG Media Downloader")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ── Auth helpers ─────────────────────────────────────────────

def get_current_user(request: Request) -> dict | None:
    """Get the authenticated user from session cookie."""
    if not settings.AUTH_ENABLED:
        # Open access mode — return a default admin user
        return {"sub": "local", "email": "local", "name": "Admin", "role": "admin"}
    token = request.cookies.get("session")
    return read_session_token(token)


def require_auth(request: Request) -> dict:
    """Redirect to login if not authenticated."""
    user = get_current_user(request)
    if not user:
        raise RedirectResponse(url="/auth/login", status_code=303)
    return user


def require_admin(request: Request) -> dict:
    """Redirect if not admin."""
    user = require_auth(request)
    if user.get("role") != "admin":
        raise RedirectResponse(url="/", status_code=303)
    return user


def user_context(request: Request) -> dict:
    """Build common template context with user info."""
    user = get_current_user(request)
    if user and settings.AUTH_ENABLED:
        u = get_user(user.get("sub", ""))
        if u:
            user["role"] = u["role"]
    return {
        "request": request,
        "user": user,
        "auth_enabled": settings.AUTH_ENABLED,
    }


# ── Lifecycle ───────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    settings.auto_discover_media()
    # If auth is enabled, set admin role for configured admin emails
    if settings.AUTH_ENABLED:
        pass  # Roles set during login callback
    # Try to restore Telegram session
    stored = load_session("default")
    if stored:
        try:
            client = await get_client()
            engine.set_client(client)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════════════

@app.get("/auth/login")
async def auth_login(request: Request):
    """Redirect to Authentik for login."""
    if not settings.AUTH_ENABLED:
        return RedirectResponse(url="/")
    state, code_verifier = generate_pkce()[:2]
    auth_url = get_auth_url(state, code_verifier)
    # Store PKCE verifier in a cookie tied to state
    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(key="pkce", value=code_verifier, max_age=300, httponly=True)
    response.set_cookie(key="oauth_state", value=state, max_age=300, httponly=True)
    return response


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = Query(...),
                         state: str = Query(...)):
    """Authentik OIDC callback."""
    cookie_state = request.cookies.get("oauth_state")
    code_verifier = request.cookies.get("pkce")

    if not code_verifier or not cookie_state:
        return templates.TemplateResponse(request, "error.html", {
            "error": "Sesión expirada. Vuelve a iniciar sesión.",
        })

    try:
        tokens = await exchange_code(code, code_verifier)
        user_info = await get_userinfo(tokens["access_token"])
    except Exception as e:
        return templates.TemplateResponse(request, "error.html", {
            "error": f"Error de autenticación: {e}",
        })

    # Determine role based on configured admin emails
    email = user_info.get("email", "")
    role = "admin" if settings.is_user_admin(email) else "user"

    # Store in DB
    upsert_user(
        user_id=user_info["sub"],
        email=email,
        name=user_info.get("name", email.split("@")[0]),
        role=role,
    )

    # Create session token
    session_token = make_session_token({
        "sub": user_info["sub"],
        "email": email,
        "name": user_info.get("name", ""),
        "role": role,
    })

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(key="session", value=session_token, max_age=604800, httponly=True)
    response.delete_cookie("pkce")
    response.delete_cookie("oauth_state")
    return response


@app.get("/auth/logout")
async def auth_logout():
    """Logout: clear session."""
    response = RedirectResponse(url="/auth/login" if settings.AUTH_ENABLED else "/")
    response.delete_cookie("session")
    return response


@app.get("/auth/me")
async def auth_me(request: Request):
    """Return current user as JSON."""
    user = get_current_user(request)
    return {"user": user}


# ══════════════════════════════════════════════════════════════
#  MAIN PAGES
# ══════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = get_current_user(request)
    if not user and settings.AUTH_ENABLED:
        return RedirectResponse(url="/auth/login")

    is_admin = user and user.get("role") == "admin"

    if is_admin:
        return await admin_index(request)
    else:
        return await user_index(request)


async def admin_index(request: Request):
    """Full admin dashboard."""
    user = get_current_user(request)
    if user:
        upsert_user(
            user_id=user["sub"],
            email=user.get("email", "unknown"),
            name=user.get("name", ""),
            role=user.get("role", "user"),
        )
    jobs = list_jobs(20)
    pending_requests = list_pending_requests(10)
    connected = engine._tg_client is not None and engine._tg_client.is_connected()
    ctx = user_context(request)
    ctx.update({
        "jobs": jobs,
        "pending_requests": pending_requests,
        "connected": connected,
        "channel_series": get_setting("channel_series", ""),
        "channel_movie": get_setting("channel_movie", ""),
        "series_paths": [str(p) for p in settings.SERIES_PATHS],
        "movies_paths": [str(p) for p in settings.MOVIES_PATHS],
        "downloads_dir": str(settings.DOWNLOADS_DIR),
        "media_dir": str(settings.MEDIA_DIR),
        "is_admin": True,
    })
    return templates.TemplateResponse(request, "admin.html", ctx)


async def user_index(request: Request):
    """Simplified user view — search and request only."""
    ctx = user_context(request)
    ctx.update({
        "series_paths": [str(p) for p in settings.SERIES_PATHS],
        "movies_paths": [str(p) for p in settings.MOVIES_PATHS],
        "is_admin": False,
    })
    return templates.TemplateResponse(request, "user.html", ctx)


# ══════════════════════════════════════════════════════════════
#  ADMIN API — Telegram connection & management
# ══════════════════════════════════════════════════════════════

@app.post("/api/connect")
async def api_connect(request: Request, phone: str = Form(...)):
    require_admin(request)
    try:
        client = await get_client(phone=phone)
        engine.set_client(client)
        return {"success": True, "message": "Código enviado. Revisa Telegram."}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/status")
async def api_status(request: Request):
    user = get_current_user(request)
    is_admin = user and user.get("role") == "admin"

    # Only admins see Telegram connection info
    connected = False
    me = None
    running = False
    if is_admin:
        connected = engine._tg_client is not None and engine._tg_client.is_connected()
        running = engine.is_running
        if connected:
            try:
                me_obj = await engine._tg_client.get_me()
                me = {"first_name": me_obj.first_name, "username": me_obj.username}
            except Exception:
                pass

    return {
        "connected": connected,
        "user": me,
        "running": running,
        "auth_enabled": settings.AUTH_ENABLED,
        "current_user": user,
        "downloads_dir": str(settings.DOWNLOADS_DIR),
        "media_dir": str(settings.MEDIA_DIR),
        "series_paths": [str(p) for p in settings.SERIES_PATHS],
        "movies_paths": [str(p) for p in settings.MOVIES_PATHS],
    }


@app.get("/api/channels")
async def api_channels(request: Request):
    require_admin(request)
    if not engine._tg_client or not engine._tg_client.is_connected():
        return {"error": "Not connected"}
    try:
        channels = await get_dialogs(engine._tg_client)
        return {"channels": channels}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/scan")
async def api_scan(request: Request, channel_id: int = Form(...),
                   query: str = Form(...), kind: str = Form("series")):
    require_admin(request)
    if not engine._tg_client or not engine._tg_client.is_connected():
        return {"error": "Not connected"}
    channels = await get_dialogs(engine._tg_client)
    channel_name = next((c["name"] for c in channels if c["id"] == channel_id), str(channel_id))
    job_id = create_job(kind, query, channel_id, channel_name)
    if kind == "series":
        asyncio.ensure_future(engine.scan_series(job_id, channel_id, query))
    else:
        asyncio.ensure_future(engine.scan_movie(job_id, channel_id, query))
    return {"job_id": job_id, "status": "scanning"}


@app.get("/api/jobs")
async def api_jobs(request: Request):
    require_admin(request)
    return {"jobs": list_jobs(50)}


@app.get("/api/jobs/{job_id}")
async def api_job_detail(request: Request, job_id: int):
    require_admin(request)
    job = get_job(job_id)
    if not job:
        return {"error": "Not found"}
    try:
        job["progress"] = json.loads(job["progress"]) if isinstance(job["progress"], str) else job["progress"]
    except (json.JSONDecodeError, TypeError):
        job["progress"] = {}
    downloads = get_job_downloads(job_id)
    return {"job": job, "downloads": downloads}


@app.post("/api/jobs/{job_id}/run")
async def api_run_job(request: Request, job_id: int):
    require_admin(request)
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


@app.post("/api/downloads/{dl_id}/toggle")
async def api_toggle_download(request: Request, dl_id: int):
    """Toggle a download between pending and skipped."""
    require_admin(request)
    from .db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT id, status FROM downloads WHERE id=?", (dl_id,)).fetchone()
        if not row:
            return {"error": "Not found"}
        new_status = "skipped" if row["status"] == "pending" else "pending"
        conn.execute("UPDATE downloads SET status=? WHERE id=?", (new_status, dl_id))
    return {"dl_id": dl_id, "status": new_status}


@app.post("/api/downloads/{dl_id}/select")
async def api_select_download(request: Request, dl_id: int,
                                selected: str = Form("1")):
    """Mark a download as selected (pending) or unselected (skipped)."""
    require_admin(request)
    from .db import get_conn
    status = "pending" if selected == "1" else "skipped"
    with get_conn() as conn:
        conn.execute("UPDATE downloads SET status=? WHERE id=?", (status, dl_id))
    return {"dl_id": dl_id, "status": status}


@app.get("/api/series")
async def api_series(request: Request):
    user = get_current_user(request)
    if not user:
        return {"error": "Unauthorized"}
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


# ══════════════════════════════════════════════════════════════
#  PUBLIC CHECK API — any authenticated user can check library
# ══════════════════════════════════════════════════════════════

@app.post("/api/check-series")
async def api_check_series(request: Request, query: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return {"error": "Unauthorized"}
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
async def api_check_movie(request: Request, query: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return {"error": "Unauthorized"}
    movie = find_movie_in_library(query)
    if not movie:
        return {"exists": False}
    return {
        "exists": True,
        "title": movie.title,
        "year": movie.year,
        "path": str(movie.file_path),
    }


# ══════════════════════════════════════════════════════════════
#  CHANNEL CONFIG — admin channel preferences
# ══════════════════════════════════════════════════════════════

@app.get("/api/channel-config")
async def api_channel_config(request: Request):
    """Get saved channel preferences for series and movies (admin only)."""
    require_admin(request)
    return {
        "series": get_setting("channel_series", ""),
        "movie": get_setting("channel_movie", ""),
    }


@app.post("/api/channel-config")
async def api_save_channel_config(request: Request,
                                    series: str = Form(""),
                                    movie: str = Form("")):
    """Save which channels to use for series and movies."""
    require_admin(request)
    set_setting("channel_series", series)
    set_setting("channel_movie", movie)
    return {"status": "saved", "series": series, "movie": movie}


# ══════════════════════════════════════════════════════════════
#  REQUEST API — users submit, admin approves
# ══════════════════════════════════════════════════════════════

@app.post("/api/request")
async def api_create_request(request: Request, kind: str = Form(...),
                               query: str = Form(...)):
    """Submit a download request (any authenticated user)."""
    user = get_current_user(request)
    if not user:
        return {"error": "Unauthorized"}

    # Ensure user exists in DB
    upsert_user(
        user_id=user["sub"],
        email=user.get("email", "unknown"),
        name=user.get("name", ""),
        role=user.get("role", "user"),
    )

    # Check if already in library
    if kind == "series":
        info, eps = find_series_in_library(query)
        if info:
            episodes = [
                {"season": e.season, "episode": e.episode, "title": e.title}
                for e in eps
            ]
            return {
                "already_exists": True,
                "message": f"'{query}' ya está en la biblioteca",
                "series": {"name": info.series_name, "year": info.series_year},
                "episodes": episodes,
            }
    else:
        movie = find_movie_in_library(query)
        if movie:
            return {
                "already_exists": True,
                "message": f"'{query}' ya está en la biblioteca",
            }

    # Check if already requested
    existing_reqs = list_requests(limit=10)
    for r in existing_reqs:
        if r["kind"] == kind and r["query"].lower() == query.lower() and r["status"] == "pending":
            return {
                "already_requested": True,
                "message": f"Ya hay una solicitud pendiente para '{query}'",
                "request_id": r["id"],
            }

    # Admins skip approval — create job directly
    req_id = create_request(
        user_id=user["sub"],
        user_email=user.get("email", ""),
        user_name=user.get("name", ""),
        kind=kind,
        query=query,
    )

    if user.get("role") == "admin":
        return {
            "success": True,
            "message": "Solicitud creada. Como eres admin, puedes iniciar la descarga desde el panel.",
            "request_id": req_id,
            "needs_approval": False,
        }

    return {
        "success": True,
        "message": "Solicitud enviada. Un administrador la revisará.",
        "request_id": req_id,
        "needs_approval": True,
    }


@app.get("/api/requests")
async def api_list_requests(request: Request):
    """List all requests (admin sees all, user sees own)."""
    user = require_auth(request)
    is_admin = user.get("role") == "admin"
    all_reqs = list_requests(50)
    if not is_admin:
        all_reqs = [r for r in all_reqs if r["user_id"] == user["sub"]]
    return {"requests": all_reqs}


@app.get("/api/requests/pending")
async def api_pending_requests(request: Request):
    """Get pending requests (admin only)."""
    require_admin(request)
    return {"requests": list_pending_requests(50)}


@app.post("/api/requests/{req_id}/approve")
async def api_approve_request(request: Request, req_id: int,
                                channel_id: int = Form(None)):
    """Admin approves a request — creates a job for it."""
    require_admin(request)
    req = get_request(req_id)
    if not req:
        return {"error": "Request not found"}
    if req["status"] != "pending":
        return {"error": f"Request is already {req['status']}"}

    update_request(req_id, status="approved", message="Aprobado por administrador")

    # Create a job and start scan
    job_id = create_job(req["kind"], req["query"], channel_id, None, request_id=req_id)
    update_request(req_id, job_id=job_id)

    if not engine._tg_client or not engine._tg_client.is_connected():
        return {
            "job_id": job_id,
            "warning": "Telegram no conectado. Conéctate primero y luego inicia el job.",
        }

    if req["kind"] == "series":
        asyncio.ensure_future(engine.scan_series(job_id, channel_id, req["query"]))
    else:
        asyncio.ensure_future(engine.scan_movie(job_id, channel_id, req["query"]))

    return {"job_id": job_id, "status": "scanning"}


@app.post("/api/requests/{req_id}/reject")
async def api_reject_request(request: Request, req_id: int,
                               message: str = Form("")):
    """Admin rejects a request."""
    require_admin(request)
    req = get_request(req_id)
    if not req:
        return {"error": "Not found"}
    update_request(req_id, status="rejected", message=message)
    return {"status": "rejected"}
