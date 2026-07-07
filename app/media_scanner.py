"""Scan the existing media library to detect naming patterns and find existing files."""

import re
from pathlib import Path
from typing import NamedTuple

from .config import settings


class SeriesInfo(NamedTuple):
    series_name: str           # e.g. "After Life"
    series_year: str | None    # e.g. "2019"
    series_dir: Path           # e.g. /media/Series/After Life (2019)/


class EpisodeFile(NamedTuple):
    series_name: str
    season: int
    episode: int
    title: str
    file_path: Path


class MovieInfo(NamedTuple):
    title: str
    year: str | None
    file_path: Path


SERIES_FILE_RE = re.compile(
    r"^(?P<series>.+?)\s*-\s*[Ss](?P<season>\d+)[Ee](?P<episode>\d+)\s*-\s*(?P<title>.+)\.\w+$"
)
SERIES_DIR_RE = re.compile(r"^(.+?)\s*\((\d{4})\)$")
MOVIE_DIR_RE = re.compile(r"^(.+?)\s*\((\d{4})\)$")
MOVIE_FILE_RE = re.compile(r"^(.+?)\s*\((\d{4})\)\.\w+$")


def get_series_dirs() -> list[SeriesInfo]:
    """Return all series directories found under SERIES_PATHS."""
    results: list[SeriesInfo] = []
    seen: set[str] = set()
    for base in settings.SERIES_PATHS:
        if not base.exists():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            m = SERIES_DIR_RE.match(entry.name)
            if m:
                key = (m.group(1).strip(), m.group(2))
            else:
                key = (entry.name, None)
            if key not in seen:
                seen.add(key)
                results.append(SeriesInfo(
                    series_name=m.group(1).strip() if m else entry.name,
                    series_year=m.group(2) if m else None,
                    series_dir=entry,
                ))
    return results


def scan_series_files(series_dir: Path) -> list[EpisodeFile]:
    """Scan a series directory and return all episode files found."""
    episodes: list[EpisodeFile] = []
    if not series_dir.exists():
        return episodes
    for entry in sorted(series_dir.iterdir()):
        if not entry.is_file():
            continue
        m = SERIES_FILE_RE.match(entry.name)
        if m:
            episodes.append(EpisodeFile(
                series_name=m.group("series").strip(),
                season=int(m.group("season")),
                episode=int(m.group("episode")),
                title=m.group("title").strip(),
                file_path=entry,
            ))
    return episodes


def series_name_variants(name: str) -> list[str]:
    """Generate common variants of a series name for matching."""
    names = [name]
    # Lowercase
    names.append(name.lower())
    # Without special chars
    cleaned = re.sub(r"[^\w\s]", "", name)
    if cleaned != name:
        names.append(cleaned)
        names.append(cleaned.lower())
    # Without year suffix
    name_no_year = re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip()
    if name_no_year != name:
        names.append(name_no_year)
        names.append(name_no_year.lower())
    return list(set(names))


def find_series_in_library(query: str) -> tuple[SeriesInfo | None, list[EpisodeFile]]:
    """Find a series in the library by name and return its info + existing episodes."""
    series_list = get_series_dirs()
    query_lower = query.lower().strip()

    # Direct name match
    for s in series_list:
        if s.series_name.lower() == query_lower:
            eps = scan_series_files(s.series_dir)
            return s, eps

    # Partial match
    for s in series_list:
        if query_lower in s.series_name.lower() or s.series_name.lower() in query_lower:
            eps = scan_series_files(s.series_dir)
            return s, eps

    return None, []


def get_existing_episodes(series_info: SeriesInfo | None,
                          eps: list[EpisodeFile]) -> dict[int, set[int]]:
    """Return {season: {episode_numbers}} for existing episodes."""
    existing: dict[int, set[int]] = {}
    for ep in eps:
        existing.setdefault(ep.season, set()).add(ep.episode)
    return existing


def find_movies_in_library() -> list[MovieInfo]:
    """Return all movies found under MOVIES_PATHS."""
    results: list[MovieInfo] = []
    for base in settings.MOVIES_PATHS:
        if not base.exists():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            m = MOVIE_DIR_RE.match(entry.name)
            if not m:
                continue
            title = m.group(1).strip()
            year = m.group(2)
            # Find the actual movie file inside
            movie_file = None
            for f in entry.iterdir():
                if f.is_file() and MOVIE_FILE_RE.match(f.name):
                    movie_file = f
                    break
            results.append(MovieInfo(title=title, year=year, file_path=movie_file or entry))
    return results


def find_movie_in_library(query: str) -> MovieInfo | None:
    """Find a movie by name."""
    movies = find_movies_in_library()
    ql = query.lower().strip()
    for m in movies:
        if m.title.lower() == ql or ql in m.title.lower():
            return m
    return None


def infer_series_name_and_year(files: list[dict]) -> tuple[str, str | None]:
    """Given a list of files from Telegram, try to infer the series name and year."""
    if not files:
        return "desconocido", None

    # Use the pattern from filenames: "NAME - S01E01 - ..."
    for f in files:
        fname = f.get("file_name", "")
        m = SERIES_FILE_RE.match(fname)
        if m:
            return m.group("series").strip(), None

    # If none match the SxxExx pattern, use the first filename's base
    fname = files[0].get("file_name", "")
    # Try to strip season/episode markers
    cleaned = re.sub(r"\s*[Ss]\d+[Ee]\d+.*", "", fname).strip()
    if cleaned:
        return cleaned, None
    return "desconocido", None


def build_series_filename(series_name: str, season: int, episode: int,
                          title: str | None, ext: str = ".mp4") -> str:
    """Build a filename matching the existing library convention."""
    ep_title = title or f"Episodio {episode}"
    return f"{series_name} - S{season:02d}E{episode:02d} - {ep_title}{ext}"


def build_movie_filename(title: str, year: str | None, ext: str = ".mp4") -> str:
    """Build a movie filename matching library convention."""
    if year:
        return f"{title} ({year}){ext}"
    return f"{title}{ext}"
