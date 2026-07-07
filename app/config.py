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
    APP_URL: str = os.environ.get("APP_URL", "http://localhost:8000")
    HOST: str = os.environ.get("HOST", "0.0.0.0")
    PORT: int = int(os.environ.get("PORT", "8000"))
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "change-me-in-production")

    # ── Authentik / OIDC (enables multi-user) ─────────────────
    AUTH_ENABLED: bool = os.environ.get("AUTH_ENABLED", "0") == "1"
    AUTHENTIK_DOMAIN: str = os.environ.get("AUTHENTIK_DOMAIN", "")
    AUTHENTIK_CLIENT_ID: str = os.environ.get("AUTHENTIK_CLIENT_ID", "")
    AUTHENTIK_CLIENT_SECRET: str = os.environ.get("AUTHENTIK_CLIENT_SECRET", "")
    ADMIN_EMAILS: list[str] = [
        e.strip() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()
    ]

    @property
    def oidc_issuer(self) -> str:
        return f"{self.AUTHENTIK_DOMAIN}/application/o/tg-media-dl"

    @property
    def oidc_authorize(self) -> str:
        return f"{self.oidc_issuer}/authorize/"

    @property
    def oidc_token(self) -> str:
        return f"{self.oidc_issuer}/token/"

    @property
    def oidc_userinfo(self) -> str:
        return f"{self.oidc_issuer}/userinfo/"

    @property
    def oidc_jwks(self) -> str:
        return f"{self.oidc_issuer}/jwks/"

    @property
    def oidc_logout(self) -> str:
        return f"{self.AUTHENTIK_DOMAIN}/if/session-end/tg-media-dl/"

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
        # Also check animacion/anime subdirs
        for entry in sorted(self.MEDIA_DIR.iterdir()):
            name_lower = entry.name.lower()
            if "animacion" in name_lower or "anime" in name_lower:
                if not entry.is_dir():
                    continue
                for sub in entry.iterdir():
                    sname = sub.name.lower()
                    if "series" in sname:
                        self.SERIES_PATHS.append(sub)
                    elif "pelicula" in sname or "movie" in sname:
                        self.MOVIES_PATHS.append(sub)

    @property
    def is_admin(self) -> list[str]:
        return self.ADMIN_EMAILS

    def is_user_admin(self, email: str) -> bool:
        return email.lower() in [e.lower() for e in self.ADMIN_EMAILS]


settings = Settings()
