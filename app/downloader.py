"""Download engine: multi-channel scan, merge, download with retry & disk check."""

import asyncio
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .db import (create_download, get_conn, get_job, get_job_downloads,
                 update_download, update_job)
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

    # ── Disk space helper ─────────────────────────────────────

    @staticmethod
    def get_disk_info(path: Path | str) -> dict:
        """Return free/total disk space for a path."""
        try:
            usage = shutil.disk_usage(path)
            return {
                "free": usage.free,
                "total": usage.total,
                "free_gb": round(usage.free / (1024**3), 1),
                "total_gb": round(usage.total / (1024**3), 1),
            }
        except Exception:
            return {"free": 0, "total": 0, "free_gb": 0, "total_gb": 0}

    # ── Retry helper ──────────────────────────────────────────

    async def _download_with_retry(self, client, channel_id: int,
                                    msg_id: int, dest: Path,
                                    max_retries: int = 3) -> Path:
        """Download with exponential backoff on failure."""
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                return await download_media(client, channel_id, msg_id, dest)
            except Exception as e:
                last_exc = e
                err_str = str(e).lower()
                # Only retry on rate-limit / timeout / network errors
                if any(x in err_str for x in (
                    "flood", "rate", "timeout", "connection",
                    "reset", "too many", "retry", "429", "503"
                )):
                    wait = min(2 ** attempt * 5, 120)  # 10s, 20s, 40s capped at 120s
                    if attempt < max_retries:
                        await asyncio.sleep(wait)
                        continue
                raise  # Non-retryable or exhausted
        raise last_exc  # type: ignore

    # ── Series workflow ──────────────────────────────────────────

    async def scan_series(self, job_id: int, channel_ids: int | list[int],
                          query: str, channels_map: dict | None = None,
                          progress_cb=None) -> None:
        """
        Phase 1: scan one or more channels, merge results grouped by episode.

        channels_map: {channel_id: channel_name} — used when merging across channels.
        """
        update_job(job_id, status="scanning")

        if not self._tg_client:
            update_job(job_id, status="error", error_msg="No Telegram client connected")
            return

        if isinstance(channel_ids, int):
            channel_ids = [channel_ids]

        all_files: list[dict] = []
        channel_names: dict[int, str] = channels_map or {}

        for ch_id in channel_ids:
            files = await search_media(self._tg_client, ch_id, query)
            ch_name = channel_names.get(ch_id, str(ch_id))
            if progress_cb:
                progress_cb(len(all_files), 0, f"Canal {ch_name}: {len(files)} archivos")
            for f in files:
                f["channel_id"] = ch_id
                f["channel_name"] = ch_name
            all_files.extend(files)

        if not all_files:
            update_job(job_id, status="done", error_msg="No files found in any channel")
            return

        # Infer series name from filenames
        series_name, _ = infer_series_name_and_year(all_files)

        # Find existing series in library
        existing_info, existing_eps = find_series_in_library(series_name)
        existing_map = get_existing_episodes(existing_info, existing_eps)

        if progress_cb:
            progress_cb(0, len(all_files), f"Serie: {series_name}")

        # Process files: group by episode key
        queued = 0
        skipped = 0
        episodes: dict[str, list[dict]] = {}

        for f in all_files:
            fname = f["file_name"]
            m = SERIES_FILE_RE.match(fname)
            if not m:
                ep_num = self._extract_ep_num(fname)
                if ep_num is None:
                    ep_key = "__unknown__"
                    season_num, ep_num_val = None, None
                else:
                    ep_key = f"S01E{ep_num:02d}"
                    season_num, ep_num_val = 1, ep_num
            else:
                season_num = int(m.group("season"))
                ep_num_val = int(m.group("episode"))
                ep_key = f"S{season_num:02d}E{ep_num_val:02d}"

            exists = False
            if season_num is not None and ep_num_val is not None:
                season_eps = existing_map.get(season_num, set())
                if ep_num_val in season_eps:
                    exists = True

            dl_id = create_download(
                job_id, fname, f.get("file_size", 0),
                msg_id=f.get("id", 0),
            )

            resolution = f.get("resolution", "?")
            ch_id = f.get("channel_id", 0)
            update_download(dl_id, resolution=resolution, channel_id=ch_id)

            if exists:
                update_download(dl_id, status="skipped",
                                downloaded_path="",
                                media_path=str(self._find_existing_path(
                                    existing_info, season_num, ep_num_val) or ""))
                skipped += 1
            else:
                update_download(dl_id, status="pending")
                queued += 1

            episodes.setdefault(ep_key, []).append({
                "dl_id": dl_id,
                "file_name": fname,
                "resolution": resolution,
                "file_size": f.get("file_size", 0),
                "exists": exists,
                "msg_id": f.get("id", 0),
                "channel_id": ch_id,
                "channel_name": f.get("channel_name", ""),
            })

        update_job(job_id, status="pending",
                   total_files=len(all_files),
                   completed_files=skipped,
                   progress={
                       "series_name": series_name,
                       "queued": queued, "skipped": skipped,
                       "total": len(all_files),
                       "episodes": episodes,
                       "channels_scanned": len(channel_ids),
                   })

    async def run_series_job(self, job_id: int, progress_cb=None) -> None:
        """Phase 2: download pending files sequentially with retry & disk check."""
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

            # Check disk space
            total_bytes = sum(d.get("file_size", 0) for d in pending)
            disk = self.get_disk_info(settings.MEDIA_DIR)
            if disk["free"] < total_bytes:
                msg = (f"Espacio insuficiente: {disk['free_gb']}GB libres, "
                       f"necesarios ≈{round(total_bytes/(1024**3),1)}GB")
                update_job(job_id, status="error", error_msg=msg)
                return

            series_name = (job.get("progress") or {}).get("series_name", "desconocido")
            existing_info, _ = find_series_in_library(series_name)

            if not existing_info:
                series_dir = settings.SERIES_PATHS[0] / f"{series_name}" if settings.SERIES_PATHS else \
                    settings.MEDIA_DIR / "Series" / series_name
            else:
                series_dir = existing_info.series_dir

            series_dir.mkdir(parents=True, exist_ok=True)
            tmp_dir = settings.DOWNLOADS_DIR / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)

            completed = job.get("completed_files", 0) or 0

            for i, dl in enumerate(pending):
                if progress_cb:
                    progress_cb(i + 1, len(pending), f"Descargando: {dl['file_name']}")

                update_download(dl["id"], status="downloading")

                try:
                    ch_id = dl.get("channel_id") or job.get("channel_id") or 0
                    dest = await self._download_with_retry(
                        self._tg_client,
                        ch_id,
                        self._get_msg_id_from_name(dl["file_name"], job_id),
                        tmp_dir,
                    )

                    target_name = self._rename_for_library(dl["file_name"], series_name)
                    target_path = series_dir / target_name

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

    async def scan_movie(self, job_id: int, channel_ids: int | list[int],
                          query: str, channels_map: dict | None = None,
                          progress_cb=None) -> None:
        """Scan one or more channels for a specific movie."""
        update_job(job_id, status="scanning")

        if not self._tg_client:
            update_job(job_id, status="error", error_msg="No Telegram client connected")
            return

        if isinstance(channel_ids, int):
            channel_ids = [channel_ids]

        all_files: list[dict] = []
        channel_names: dict[int, str] = channels_map or {}

        for ch_id in channel_ids:
            files = await search_media(self._tg_client, ch_id, query)
            ch_name = channel_names.get(ch_id, str(ch_id))
            for f in files:
                f["channel_id"] = ch_id
                f["channel_name"] = ch_name
            all_files.extend(files)

        if not all_files:
            update_job(job_id, status="done", error_msg="No files found in any channel")
            return

        movie_name = query.strip()
        existing = find_movie_in_library(movie_name)

        queued = 0
        skipped = 0
        for f in all_files:
            dl_id = create_download(job_id, f["file_name"], f.get("file_size", 0),
                                    msg_id=f.get("id", 0))
            resolution = f.get("resolution", "?")
            ch_id = f.get("channel_id", 0)
            update_download(dl_id, resolution=resolution, channel_id=ch_id)
            if existing:
                update_download(dl_id, status="skipped", media_path=str(existing.file_path))
                skipped += 1
            else:
                update_download(dl_id, status="pending")
                queued += 1

        update_job(job_id, status="pending",
                   total_files=len(all_files),
                   completed_files=skipped,
                   progress={
                       "movie_name": movie_name,
                       "queued": queued, "skipped": skipped,
                       "total": len(all_files),
                       "channels_scanned": len(channel_ids),
                   })

    async def run_movie_job(self, job_id: int, progress_cb=None) -> None:
        """Download pending movie files sequentially with retry & disk check."""
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

            # Check disk space
            total_bytes = sum(d.get("file_size", 0) for d in pending)
            disk = self.get_disk_info(settings.MEDIA_DIR)
            if disk["free"] < total_bytes:
                msg = (f"Espacio insuficiente: {disk['free_gb']}GB libres, "
                       f"necesarios ≈{round(total_bytes/(1024**3),1)}GB")
                update_job(job_id, status="error", error_msg=msg)
                return

            movie_name = (job.get("progress") or {}).get("movie_name", "desconocido")
            movies_base = settings.MOVIES_PATHS[0] if settings.MOVIES_PATHS else \
                settings.MEDIA_DIR / "Peliculas"

            year = None
            year_m = re.search(r"\((\d{4})\)", pending[0]["file_name"])
            if year_m:
                year = year_m.group(1)

            movie_dir = movies_base / f"{movie_name} ({year})" if year else movies_base / movie_name
            movie_dir.mkdir(parents=True, exist_ok=True)

            tmp_dir = settings.DOWNLOADS_DIR / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)

            completed = job.get("completed_files", 0) or 0

            for i, dl in enumerate(pending):
                if progress_cb:
                    progress_cb(i + 1, len(pending), f"Descargando: {dl['file_name']}")

                update_download(dl["id"], status="downloading")

                try:
                    ch_id = dl.get("channel_id") or job.get("channel_id") or 0
                    dest = await self._download_with_retry(
                        self._tg_client,
                        ch_id,
                        self._get_msg_id_from_name(dl["file_name"], job_id),
                        tmp_dir,
                    )

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
        downloads = get_job_downloads(job_id)
        for dl in downloads:
            if dl["file_name"] == fname:
                return dl.get("msg_id", 0)
        return 0

    def _rename_for_library(self, original_name: str, series_name: str) -> str:
        m = SERIES_FILE_RE.match(original_name)
        if m:
            return original_name
        return original_name


engine = DownloadEngine()
