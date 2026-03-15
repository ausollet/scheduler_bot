import os
import secrets
from typing import Optional, Tuple

from fastapi import HTTPException
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
import json

# Scopes required for calendar access (read/write events)
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]

# We'll keep OAuth flow objects in memory keyed by state.
# This is fine for a small prototype; in production you'd want a more robust session store.
_flows: dict[str, Flow] = {}


def get_client_secrets_path() -> str:
    # Allow override via env var, otherwise use bundled JSON.
    return os.getenv("GOOGLE_OAUTH_CLIENT_SECRETS")


def _make_flow(redirect_uri: str) -> Flow:
    client_secrets = get_client_secrets_path()
    if not client_secrets:
        raise FileNotFoundError(f"OAuth client secrets file not found: {client_secrets}")
    flow = Flow.from_client_config(
        client_secrets,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


def create_authorization_url(session_id: str, redirect_uri: str) -> str:
    """Create an OAuth2 authorization URL for a given session."""
    state = f"{session_id}:{secrets.token_urlsafe(16)}"
    flow = _make_flow(redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",
    )
    # Store flow so we can resume it on callback.
    _flows[state] = flow
    return auth_url


def exchange_code_for_credentials(state: str, code: str) -> Tuple[str, Credentials]:
    """Exchange the OAuth code for credentials, returning the session_id and creds."""
    flow = _flows.get(state)
    if not flow:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OAuth token exchange failed: {exc}")

    creds = flow.credentials
    session_id = state.split(":", 1)[0]

    # Clean up the flow to reduce memory usage
    _flows.pop(state, None)

    # Refresh if expired (shouldn't happen immediately but ensures you have a valid token)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return session_id, creds


def credentials_to_dict(creds: Credentials) -> dict:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }


def credentials_from_dict(data: dict) -> Credentials:
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
    )


def ensure_credentials_valid(creds: Credentials) -> Credentials:
    """Refresh credentials if needed."""
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds
