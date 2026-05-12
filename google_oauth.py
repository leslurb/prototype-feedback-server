"""Google OAuth installed-app flow + token cache.

Locates client_secret.json at one of:
    1. $GOOGLE_CLIENT_SECRET_PATH
    2. <repo>/data/google_client_secret.json
    3. ~/.config/gws/client_secret.json
    4. %APPDATA%/gws/client_secret.json   (Windows)

Caches the user token at <repo>/data/google_token.json.

Scopes: gmail.compose (covers send + draft), directory.readonly.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).parent
DATA = ROOT / "data"
TOKEN_PATH = DATA / "google_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/directory.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


class OAuthNotConfigured(Exception):
    """Raised when client_secret.json cannot be located."""


def find_client_secret() -> Path:
    candidates = []
    env = os.environ.get("GOOGLE_CLIENT_SECRET_PATH")
    if env:
        candidates.append(Path(env))
    candidates.append(DATA / "google_client_secret.json")
    candidates.append(Path.home() / ".config" / "gws" / "client_secret.json")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "gws" / "client_secret.json")

    for p in candidates:
        if p and p.exists():
            return p
    raise OAuthNotConfigured(
        "client_secret.json not found. Set GOOGLE_CLIENT_SECRET_PATH or place the file at one of: "
        + ", ".join(str(p) for p in candidates)
    )


def is_authorized() -> bool:
    if not TOKEN_PATH.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except Exception:
        return False
    if creds and creds.valid:
        return True
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_creds(creds)
            return True
        except Exception:
            return False
    return False


def get_credentials(allow_browser: bool = True) -> Credentials:
    creds: Optional[Credentials] = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception:
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_creds(creds)
        return creds

    if not allow_browser:
        raise OAuthNotConfigured(
            "Not authorized and browser flow disabled. Hit /auth/google to start."
        )

    secret = find_client_secret()
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    # run_local_server picks a free port and opens the user's default browser.
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        open_browser=True,
        authorization_prompt_message="Sign in to Google to enable Gmail + People for Prototype Feedback...",
        success_message="Authentication complete. You can close this window.",
    )
    _save_creds(creds)
    return creds


def revoke() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def whoami() -> dict:
    """Return basic profile info from the cached token."""
    if not TOKEN_PATH.exists():
        return {}
    try:
        raw = json.loads(TOKEN_PATH.read_text("utf-8"))
        # 'account' isn't always populated; many tokens carry id_token though.
        return {"scopes": raw.get("scopes", []), "client_id": raw.get("client_id")}
    except Exception:
        return {}


def _save_creds(creds: Credentials) -> None:
    DATA.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
