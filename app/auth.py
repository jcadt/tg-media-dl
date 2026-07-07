"""Authentik OIDC authentication."""

import json
from base64 import urlsafe_b64encode
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any

import httpx
from itsdangerous import URLSafeTimedSerializer
from starlette.responses import RedirectResponse

from .config import settings

# ── Session serializer for user cookies ──────────────────────
serializer = URLSafeTimedSerializer(
    secret_key=settings.SECRET_KEY,
    salt="tg-media-dl-auth",
)


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = token_urlsafe(64)
    digest = sha256(verifier.encode()).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def get_auth_url(state: str, code_challenge: str) -> str:
    """Build the Authorize URL for Authentik."""
    params = (
        f"response_type=code"
        f"&client_id={settings.AUTHENTIK_CLIENT_ID}"
        f"&redirect_uri={settings.APP_URL}/auth/callback"
        f"&scope=openid+email+profile"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )
    return f"{settings.oidc_authorize}?{params}"


async def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    """Exchange authorization code for tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            settings.oidc_token,
            data={
                "grant_type": "authorization_code",
                "client_id": settings.AUTHENTIK_CLIENT_ID,
                "client_secret": settings.AUTHENTIK_CLIENT_SECRET,
                "redirect_uri": f"{settings.APP_URL}/auth/callback",
                "code": code,
                "code_verifier": code_verifier,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def get_userinfo(access_token: str) -> dict[str, Any]:
    """Get user info from Authentik with access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            settings.oidc_userinfo,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "sub": data.get("sub"),
            "email": data.get("email", ""),
            "name": data.get("name", data.get("preferred_username", "")),
            "nickname": data.get("nickname", data.get("preferred_username", "")),
        }


def make_session_token(user: dict[str, Any]) -> str:
    """Create a signed session cookie for the user."""
    return serializer.dumps(user)


def read_session_token(token: str | None) -> dict[str, Any] | None:
    """Read and verify a session token. Returns user dict or None."""
    if not token:
        return None
    try:
        return serializer.loads(token, max_age=86400 * 7)  # 7 days
    except Exception:
        return None
