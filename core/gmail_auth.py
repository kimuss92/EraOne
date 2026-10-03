# core/gmail_auth.py
"""OAuth 2.0 credential handling for Era's Gmail lane.

Split out of gmail_service.py so the credential surface — where the token
lives, how it is hardened, which scopes were granted, how it is refreshed and
revoked — is one readable file rather than a section of a large one.

Design constraints this module exists to satisfy:

* **Never break the voice loop.** Every Google import is lazy and every public
  function returns a value instead of raising, so a missing dependency, an
  expired token or a dead network degrades Gmail only — the rest of Era keeps
  running. Nothing here is imported at Era startup.
* **Least privilege.** Two scopes only: read, and send. No `gmail.modify`, so
  a stolen token cannot delete, label or trash the user's mail.
* **Credentials never live in the project folder.** The refresh token is
  written under the user profile (`~/.eraone/gmail/`) with inheritance
  stripped, not into the repo, which OneDrive syncs off the machine.
* **Nothing here returns token material.** auth_status() and the error
  formatter in gmail_service both answer in prose, never with a secret.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from core.utils import get_config_dir, log

# Read + send. Deliberately NOT gmail.modify: Era never needs to mutate the
# mailbox, and a token that cannot delete mail is worth far less to an attacker.
# Changing this list invalidates the stored token by design — the user is
# re-prompted to consent to the new scope.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)

# Where the OAuth *client* (app identity, downloaded from Google Cloud Console)
# is expected.
CLIENT_SECRET_FILENAMES = ("gmail_client_secret.json", "gmail_credentials.json")

_service_lock = threading.Lock()
# One API client PER THREAD. The client rides a single httplib2 connection, which
# is not thread-safe: a scheduled task reading mail while a live turn does the same
# interleaved two requests on one TLS stream and failed with SSLError (2026-09-18).
# A generation counter lets _invalidate_service() retire every thread's client.
_service_tls = threading.local()
_service_generation = 0
_user_email_cache: str | None = None

_EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_DISPLAY_ADDR_RE = re.compile(r"^(?P<name>[^<>]{0,120})<(?P<addr>[^<>]+)>$")


class GmailNotConnected(RuntimeError):
    """Raised internally; every public entry point converts it to a message."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def gmail_state_dir() -> Path:
    """Directory holding the OAuth token and watcher state.

    Outside the project tree by default. `ERA_GMAIL_DIR` overrides it, which is
    what the tests use so a test run can never touch the real mailbox token.
    """
    override = os.environ.get("ERA_GMAIL_DIR")
    base = Path(override) if override else Path.home() / ".eraone" / "gmail"
    base.mkdir(parents=True, exist_ok=True)
    return base


def token_path() -> Path:
    return gmail_state_dir() / "token.json"


def client_secret_path() -> Path | None:
    """First existing OAuth client file, or None if the user hasn't added one."""
    cfg = get_config_dir()
    for name in CLIENT_SECRET_FILENAMES:
        candidate = cfg / name
        if candidate.is_file():
            return candidate
    return None


def _harden_permissions(path: Path) -> None:
    """Best-effort: make the token readable only by this user account.

    POSIX chmod is close to meaningless on NTFS, so on Windows we also strip
    inheritance and grant only the current user. Never fatal — a token with
    loose ACLs still beats a broken mail integration.
    """
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dependency probing
# ---------------------------------------------------------------------------


def dependencies_ready() -> tuple[bool, str]:
    """(ok, human explanation). Cheap enough to call on every entry point."""
    missing = []
    try:
        import googleapiclient.discovery  # noqa: F401
    except Exception:
        missing.append("google-api-python-client")
    try:
        import google.oauth2.credentials  # noqa: F401
    except Exception:
        missing.append("google-auth")
    try:
        import google_auth_oauthlib.flow  # noqa: F401
    except Exception:
        missing.append("google-auth-oauthlib")
    if missing:
        return False, (
            "Gmail support needs these Python packages: "
            + ", ".join(missing)
            + ". Install with: pip install "
            + " ".join(missing)
        )
    return True, "ok"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _load_credentials():
    """Return live Credentials, refreshing if needed, or None.

    Returns None for every failure mode (no token, corrupt token, revoked
    refresh token) — callers turn that into "not connected", never a crash.
    """
    ok, _ = dependencies_ready()
    if not ok:
        return None
    path = token_path()
    if not path.is_file():
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(path), list(SCOPES))
    except Exception as e:
        log("GMAIL", f"stored token unreadable: {type(e).__name__}")
        return None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _write_token(creds)
            return creds
        except Exception as e:
            # Refresh token revoked from the Google account, or offline.
            log("GMAIL", f"token refresh failed: {type(e).__name__}")
            return None
    return None


def _write_token(creds) -> None:
    path = token_path()
    try:
        path.write_text(creds.to_json(), encoding="utf-8")
        _harden_permissions(path)
    except Exception as e:
        log("GMAIL", f"could not persist token: {type(e).__name__}")


def is_connected() -> bool:
    """Cheap, local-only connection check — no network round-trip.

    `auth_status()` also fetches the profile to report the address, which costs
    an API call. Callers that only need a yes/no gate (every read action does)
    use this instead, so a mail command pays for one request, not two.
    """
    try:
        return _load_credentials() is not None
    except Exception:
        return False


def auth_status() -> dict:
    """Connection state. Never raises, never returns token material."""
    deps_ok, deps_msg = dependencies_ready()
    secret = client_secret_path()
    status: dict[str, Any] = {
        "dependencies_ok": deps_ok,
        "dependencies_message": deps_msg,
        "client_secret_present": secret is not None,
        "client_secret_path": str(secret) if secret else "",
        "token_present": token_path().is_file(),
        "connected": False,
        "email": "",
        "scopes": list(SCOPES),
    }
    if not deps_ok:
        return status
    creds = _load_credentials()
    if creds is None:
        return status
    status["connected"] = True
    try:
        status["email"] = _profile_uncached(creds).get("emailAddress", "")
    except Exception:
        pass
    return status


def authorize(timeout_seconds: int = 180) -> dict:
    """Run the OAuth 2.0 installed-app flow.

    Opens Google's own consent page in the user's browser and receives the code
    on a loopback listener. Era never sees or handles the account password —
    that stays between the user and Google, which is the point of using OAuth
    here rather than scripted sign-in.
    """
    ok, msg = dependencies_ready()
    if not ok:
        return {"ok": False, "message": msg}

    secret = client_secret_path()
    if secret is None:
        cfg = get_config_dir()
        return {
            "ok": False,
            "message": (
                "No Gmail OAuth client file found. Create an OAuth client of type "
                "'Desktop app' in the Google Cloud console, enable the Gmail API, "
                f"download the JSON, and save it as {cfg / 'gmail_client_secret.json'}."
            ),
        }

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(secret), list(SCOPES))
        # port=0 → the OS picks a free loopback port. Loopback redirect is the
        # flow Google recommends for installed apps.
        creds = flow.run_local_server(
            port=0,
            authorization_prompt_message="",
            success_message="Era One is now connected to Gmail. You can close this tab.",
            open_browser=True,
            timeout_seconds=timeout_seconds,
        )
    except Exception as e:
        return {"ok": False, "message": f"Authorization did not complete: {e}"}

    if not creds:
        return {"ok": False, "message": "Authorization did not complete."}

    _write_token(creds)
    _invalidate_service()
    email = ""
    try:
        email = _profile_uncached(creds).get("emailAddress", "")
    except Exception:
        pass
    return {
        "ok": True,
        "email": email,
        "message": f"Gmail connected{f' as {email}' if email else ''}.",
    }


def revoke() -> dict:
    """Revoke the token with Google and delete the local copy."""
    creds = _load_credentials()
    revoked_remotely = False
    if creds is not None:
        try:
            import requests

            resp = requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": creds.refresh_token or creds.token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            revoked_remotely = resp.status_code == 200
        except Exception:
            pass
    try:
        p = token_path()
        if not p.is_file():
            _invalidate_service()
            return {"ok": True, "message": "There was no stored Gmail token to remove."}
        p.unlink()
    except Exception as e:
        return {"ok": False, "message": f"Could not delete the stored token: {e}"}
    _invalidate_service()
    suffix = (
        ""
        if revoked_remotely
        else " Local token removed; revoke the app in your Google account if you also want it invalidated server-side."
    )
    return {"ok": True, "message": f"Gmail access disconnected.{suffix}"}


# ---------------------------------------------------------------------------
# Service handle
# ---------------------------------------------------------------------------


def _invalidate_service() -> None:
    """Retire every thread's client (re-auth, revoke, lost credentials)."""
    global _service_generation, _user_email_cache
    with _service_lock:
        _service_generation += 1
        _user_email_cache = None


def drop_thread_service() -> None:
    """Retire only the calling thread's client — its connection may be broken."""
    _service_tls.entry = None


def get_service():
    """This thread's cached Gmail API client, or None when not connected."""
    creds = _load_credentials()
    if creds is None:
        _invalidate_service()
        return None
    key = getattr(creds, "token", None) or ""
    with _service_lock:
        generation = _service_generation
    entry = getattr(_service_tls, "entry", None)
    if entry is not None and entry[0] == generation and entry[1] == key:
        return entry[2]
    try:
        from googleapiclient.discovery import build

        # cache_discovery=False: the file cache warns loudly on modern
        # installs and buys nothing for a single API.
        svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:
        log("GMAIL", f"could not build Gmail client: {type(e).__name__}: {e}")
        _service_tls.entry = None
        return None
    _service_tls.entry = (generation, key, svc)
    return svc


def _profile_uncached(creds) -> dict:
    from googleapiclient.discovery import build

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return svc.users().getProfile(userId="me").execute()


def user_email() -> str:
    """The connected account's address, cached for the process.

    Scoring asks for this on every read to work out whether a message was
    addressed to the user directly. Going through auth_status() each time built
    a fresh API client and made a network round-trip per read — latency the
    voice loop pays for a value that cannot change without re-authorizing.
    Cleared by _invalidate_service(), so a re-auth or revoke picks up the new
    account.
    """
    global _user_email_cache
    if _user_email_cache is not None:
        return _user_email_cache
    creds = _load_credentials()
    if creds is None:
        return ""
    try:
        email = _profile_uncached(creds).get("emailAddress", "") or ""
    except Exception:
        return ""
    _user_email_cache = email
    return email


def _require_service():
    svc = get_service()
    if svc is None:
        raise GmailNotConnected("Gmail is not connected yet. Run the Gmail authorization first.")
    return svc


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
