"""Configuration loaded from environment variables."""

import os
from pathlib import Path


class Settings:
    # ── Paths (set via env or docker volumes) ──────────────────
    DOWNLOADS_DIR: Path = Path(os.environ.get("DOWNLOADS_DIR", "/downloads"))
    MEDIA_DIR: Path = Path(os.environ.get("MEDIA_DIR", "/media"))
    DATA_DIR: Path = Path(os.environ.get("DATA_DIR", "/data"))

    # ── Telegram API (set via env) ────────────────────────────
    TG_API_ID: int = int(os.environ.get("TG_API_ID", "0"))
    TG_API_HASH: str = os.environ.get("TG_API_HASH", "")

    # ── App ───────────────────────────────────────────────────
    HOST: str = os.environ.get("HOST", "0.0.0.0")
    PORT: int = int(os.environ.get("PORT", "8000"))

    # ── Media paths detected at runtime ───────────────────────
    SERIES_PATHS: list[Path] = []
    MOVIES_PATHS: list[Path] = []

    def auto_discover_media(self) -> None:
        """Scan MEDIA_DIR for known top-level categories."""
        if not self.MEDIA_DIR.exists():
            return
        for entry in sorted(self.MEDIA_DIR.iterdir()):
            name_lower = entry.name.lower()
            if "series" in name_lower and entry.is_dir():
                self.SERIES_PATHS.append(entry)
            elif "pelicula" in name_lower or entry.name == "Movies" or "movie" in name_lower:
                if entry.is_dir():
                    self.MOVIES_PATHS.append(entry)
        # Also check animacion subdirs
        for entry in sorted(self.MEDIA_DIR.iterdir()):
            if entry.is_dir() and "animacion" in name_lower or "anime" in name_lower:
                # check if it has Series-like or Movies-like subdirs
                for sub in entry.iterdir():
                    sname = sub.name.lower()
                    if "series" in sname:
                        self.SERIES_PATHS.append(sub)
                    elif "pelicula" in sname or "movie" in sname:
                        self.MOVIES_PATHS.append(sub)


settings = Settings()
