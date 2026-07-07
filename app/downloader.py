"""Download engine: queue management, Telegram download, rename, copy to media."""

import asyncio
import re
import shutil
from pathlib import Path

from .config import settings
from .db import (create_job, create_download, get_job, get_job_downloads,
                 list_jobs, update_download, update_job)
from .media_scanner import (EpisodeFile, MovieInfo, SERIES_FILE_RE,
                            build_movie_filename, build_series_filename,
                            find_movie_in_library, find_series_in_library,
                            get_existing_episodes, infer_series_name_and_year,
                            scan_series_files)
from .telegram_client import download_media, search_media


class DownloadEngine:
    """Orchestrates series/movie discovery and download."""

    def __init__(self):
        self._tg_client = None
        self._running_jobs: set[int] = set()

    def set_client(self, client) -> None:
        self._tg_client = client

    @property
    def is_running(self) -> bool:
        return bool(self._running_jobs)

    # ── Series workflow ──────────────────────────────────────────

    async def scan_series(self, job_id: int, channel_id: int,
                          query: str, progress_cb=None) -> None:
        """Phase 1: scan channel for files, compare with library, create download queue."""
        update_job(job_id, status="scanning")

        if not self._tg_client:
            update_job(job_id, status="error", error_msg="No Telegram client connected")
            return

        # Search for files in channel
        files = await search_media(self._tg_client, channel_id, query)

        if not files:
            update_job(job_id, status="done", error_msg="No files found for this query")
            return

        # Infer series name from filenames
        series_name, _ = infer_series_name_and_year(files)

        # Find existing series in library
        existing_info, existing_eps = find_series_in_library(series_name)
        existing_map = get_existing_episodes(existing_info, existing_eps)

        if progress_cb:
            progress_cb(0, len(files), f"Serie: {series_name}")

        # Match Telegram files to episodes
        queued = 0
        skipped = 0
        for f in files:
            fname = f["file_name"]
            m = SERIES_FILE_RE.match(fname)
            if not m:
                # Try to extract episode number in other ways
                ep_num = self._extract_ep_num(fname)
                if ep_num is None:
                    # Flag as unknown
                    ep_info = {"season": None, "episode": None}
                else:
                    ep_info = {"season": 1, "episode": ep_num}
            else:
                ep_info = {"season": int(m.group("season")),
                           "episode": int(m.group("episode"))}

            # Check if already exists
            exists = False
            if ep_info["season"] is not None and ep_info["episode"] is not None:
                season_eps = existing_map.get(ep_info["season"], set())
                if ep_info["episode"] in season_eps:
                    exists = True

            dl_id = create_download(
                job_id, fname, f.get("file_size", 0),
                msg_id=f.get("id", 0),
            )

            if exists:
                update_download(dl_id, status="skipped",
                                downloaded_path="",
                                media_path=str(self._find_existing_path(
                                    existing_info, ep_info["season"], ep_info["episode"]) or ""))
                skipped += 1
            else:
                update_download(dl_id, status="pending")
                queued += 1

        update_job(job_id, status="pending",
                   total_files=len(files),
                   completed_files=skipped,
                   progress={"series_name": series_name,
                             "queued": queued, "skipped": skipped,
                             "total": len(files)})

    async def run_series_job(self, job_id: int, progress_cb=None) -> None:
        """Phase 2: download pending files, rename, copy to media."""
        self._running_jobs.add(job_id)
        try:
            update_job(job_id, status="downloading")
            job = get_job(job_id)
            if not job:
                return

            downloads = get_job_downloads(job_id)
            pending = [d for d in downloads if d["status"] == "pending"]

            if not pending:
                update_job(job_id, status="done",
                           completed_files=job.get("total_files", 0))
                return

            series_name = (job.get("progress") or {}).get("series_name", "desconocido")
            existing_info, _ = find_series_in_library(series_name)

            if not existing_info:
                # Create new series directory
                series_dir = settings.SERIES_PATHS[0] / f"{series_name}" if settings.SERIES_PATHS else \
                    settings.MEDIA_DIR / "Series" / series_name
            else:
                series_dir = existing_info.series_dir

            series_dir.mkdir(parents=True, exist_ok=True)

            tmp_dir = settings.DOWNLOADS_DIR / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)

            completed = job.get("completed_files", 0) or 0
            total = len(pending) + completed

            for i, dl in enumerate(pending):
                if progress_cb:
                    progress_cb(i + 1, len(pending), f"Descargando: {dl['file_name']}")

                update_download(dl["id"], status="downloading")

                try:
                    # Download
                    dest = await download_media(
                        self._tg_client,
                        job["channel_id"],
                        self._get_msg_id_from_name(dl["file_name"], job_id),
                        tmp_dir,
                    )

                    # Rename to library format
                    target_name = self._rename_for_library(dl["file_name"], series_name)
                    target_path = series_dir / target_name

                    # Move to media
                    if dest.exists():
                        shutil.move(str(dest), str(target_path))

                    update_download(dl["id"], status="done",
                                    downloaded_path=str(dest) if dest.exists() else "",
                                    media_path=str(target_path) if target_path.exists() else "")
                    completed += 1
                    update_job(job_id, completed_files=completed)

                except Exception as e:
                    update_download(dl["id"], status="error", error_msg=str(e))

            update_job(job_id, status="done", completed_files=completed)

        finally:
            self._running_jobs.discard(job_id)

    # ── Movie workflow ───────────────────────────────────────────

    async def scan_movie(self, job_id: int, channel_id: int,
                         query: str, progress_cb=None) -> None:
        """Scan channel for a specific movie and check if it exists."""
        update_job(job_id, status="scanning")

        if not self._tg_client:
            update_job(job_id, status="error", error_msg="No Telegram client connected")
            return

        files = await search_media(self._tg_client, channel_id, query)

        if not files:
            update_job(job_id, status="done", error_msg="No files found")
            return

        # Use the query as movie name
        movie_name = query.strip()
        existing = find_movie_in_library(movie_name)

        queued = 0
        skipped = 0
        for f in files:
            dl_id = create_download(job_id, f["file_name"], f.get("file_size", 0),
                                    msg_id=f.get("id", 0))
            if existing:
                update_download(dl_id, status="skipped", media_path=str(existing.file_path))
                skipped += 1
            else:
                update_download(dl_id, status="pending")
                queued += 1

        update_job(job_id, status="pending",
                   total_files=len(files),
                   completed_files=skipped,
                   progress={"movie_name": movie_name,
                             "queued": queued, "skipped": skipped,
                             "total": len(files)})

    async def run_movie_job(self, job_id: int, progress_cb=None) -> None:
        """Download pending movie files, rename, copy to media."""
        self._running_jobs.add(job_id)
        try:
            update_job(job_id, status="downloading")
            job = get_job(job_id)
            if not job:
                return

            downloads = get_job_downloads(job_id)
            pending = [d for d in downloads if d["status"] == "pending"]

            if not pending:
                update_job(job_id, status="done")
                return

            movie_name = (job.get("progress") or {}).get("movie_name", "desconocido")

            # Determine target dir
            movies_base = settings.MOVIES_PATHS[0] if settings.MOVIES_PATHS else \
                settings.MEDIA_DIR / "Peliculas"

            # Try to extract year from filename
            year = None
            year_m = re.search(r"\((\d{4})\)", pending[0]["file_name"])
            if year_m:
                year = year_m.group(1)

            if year:
                movie_dir = movies_base / f"{movie_name} ({year})"
            else:
                movie_dir = movies_base / movie_name

            movie_dir.mkdir(parents=True, exist_ok=True)

            tmp_dir = settings.DOWNLOADS_DIR / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)

            completed = job.get("completed_files", 0) or 0
            total = len(pending) + completed

            for i, dl in enumerate(pending):
                if progress_cb:
                    progress_cb(i + 1, len(pending), f"Descargando: {dl['file_name']}")

                update_download(dl["id"], status="downloading")

                try:
                    dest = await download_media(
                        self._tg_client,
                        job["channel_id"],
                        self._get_msg_id_from_name(dl["file_name"], job_id),
                        tmp_dir,
                    )

                    # Determine extension
                    ext = Path(dl["file_name"]).suffix or ".mp4"
                    target_name = build_movie_filename(movie_name, year, ext)
                    target_path = movie_dir / target_name

                    if dest.exists():
                        shutil.move(str(dest), str(target_path))

                    update_download(dl["id"], status="done",
                                    downloaded_path=str(target_path) if target_path.exists() else "",
                                    media_path=str(target_path) if target_path.exists() else "")
                    completed += 1
                    update_job(job_id, completed_files=completed)

                except Exception as e:
                    update_download(dl["id"], status="error", error_msg=str(e))

            update_job(job_id, status="done", completed_files=completed)

        finally:
            self._running_jobs.discard(job_id)

    # ── Helpers ──────────────────────────────────────────────────

    def _extract_ep_num(self, fname: str) -> int | None:
        patterns = [
            r'(?:Episodio|Episode|Cap[ií]tulo|E[p]?)\s*[.]?\s*(\d+)',
            r'[Ee](\d{2,})',
        ]
        for pat in patterns:
            m = re.search(pat, fname)
            if m:
                return int(m.group(1))
        return None

    def _find_existing_path(self, info, season, episode) -> Path | None:
        if not info:
            return None
        eps = scan_series_files(info.series_dir)
        for ep in eps:
            if ep.season == season and ep.episode == episode:
                return ep.file_path
        return None

    def _get_msg_id_from_name(self, fname: str, job_id: int) -> int:
        """Return the stored msg_id for a filename."""
        downloads = get_job_downloads(job_id)
        for dl in downloads:
            if dl["file_name"] == fname:
                return dl.get("msg_id", 0)
        return 0

    def _rename_for_library(self, original_name: str, series_name: str) -> str:
        """Convert a downloaded filename to library naming convention."""
        m = SERIES_FILE_RE.match(original_name)
        if m:
            return original_name  # Already correct format
        return original_name


engine = DownloadEngine()
