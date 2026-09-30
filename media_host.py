r"""
media_host.py — get a LOCAL media file to a public HTTPS URL that Instagram /
Facebook (through Composio) can actually fetch.

WHY THIS EXISTS
  IG/FB video publishing needs a *publicly reachable* video_url (Instagram's reel
  flow fetches the video from a URL; it cannot read a local path, and there is no
  generic "upload raw local bytes" tool in the headless Composio environment). A
  large approved reel (e.g. 66 MB) therefore can't be published directly. This
  script uploads the file to a media host and prints the resulting public URL, so
  the publish step can pass it straight to the post tool as `video_url`.

BACKEND — CLOUDINARY, EXCLUSIVELY
  Cloudinary is the ONLY backend (decided 2026-07-29 that it is the permanent
  default; the alternatives were removed 2026-07-30). Credentials come from
  env CLOUDINARY_URL, the discrete CLOUDINARY_* env vars, or
  config/media_host.json — in that order.

  There is deliberately no fallback chain. This module used to fall through to
  Bunny, then litterbox, then catbox when an upload failed, and that was the
  wrong shape for the job: silently swapping in a free anonymous host with a
  known 0-byte failure mode is exactly the instability Cloudinary was
  configured to eliminate. A Cloudinary failure — wrong or expired creds,
  quota, a real outage — must surface loudly so it gets fixed, not get masked
  by a quieter host that can itself fail later, or by litterbox's 72h expiry
  quietly deleting a published reel's source. Cloudinary is also the only one
  of them with a real authenticated delete API, which CLEANUP below depends on.

  Removing the chain changed no runtime behaviour: with Cloudinary configured,
  it was already the only backend attempted.

  Every successful upload is verified with a ranged GET before its URL is
  returned — the same kind of partial fetch IG/FB/TikTok perform — so an
  upload that was accepted but serves an empty/broken response is caught here
  rather than after a platform rejects (or silently mis-publishes) the asset.

CLEANUP
  Once a platform has confirmed it received the media, delete the staged
  copy so reels don't sit on a public URL indefinitely:
    python media_host.py --cleanup "<url returned above>" --json

USAGE
  python media_host.py "D:\path\Perla reel (approved).mp4"        # prints the URL
  python media_host.py "<path>" --json                            # {"ok":true,"url":...}
  python media_host.py --cleanup "<url>" --json

OPTIONAL CONFIG  config/media_host.json  (env vars take precedence)
  {
    "backend": "cloudinary",
    "cloudinary": {"cloud_name": "...", "api_key": "...", "api_secret": "..."}
  }

Exit code 0 on success (URL on stdout); non-zero on failure (reason on stderr).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # requests ships with the project; guard anyway
    requests = None

CONFIG_PATH = Path(__file__).with_name("config") / "media_host.json"

BACKEND = "cloudinary"  # the only one; kept named so results/errors stay labelled

UPLOAD_TIMEOUT = 600  # 10 min — big reels on a slow uplink
VERIFY_RANGE_BYTES = 4096  # small ranged read to confirm the host
# actually serves the file, not just a 200


class MediaHostError(RuntimeError):
    """Raised when a file cannot be hosted (with a human-readable reason)."""


# ── Config ────────────────────────────────────────────────────────────────────


def _config_path() -> Path:
    """Honours MEDIA_HOST_CONFIG_PATH, the same escape hatch core.utils's
    get_memory_dir gives ERA_MEMORY_DIR. Without it, any real
    config/media_host.json a user creates is read by BOTH production and the
    test suite (this module has no path-isolation of its own), so a genuine
    Cloudinary config would silently change what the tests observe.

    NOTE the config file is only half the isolation story: credentials also
    come from CLOUDINARY_URL and the discrete CLOUDINARY_* env vars, which no
    override here can shadow. A test that sets a fake cloud_name while the
    real CLOUDINARY_URL is exported in the shell reads the REAL cloud name,
    because _cloudinary_creds checks CLOUDINARY_URL first. That is what broke
    six tests on 2026-07-30 the day Cloudinary was configured for real. Tests
    must therefore clear the whole CLOUDINARY_* family, not just point this
    path elsewhere — tools/test_media_host.py does it in an autouse fixture."""
    override = os.environ.get("MEDIA_HOST_CONFIG_PATH", "").strip()
    return Path(override) if override else CONFIG_PATH


def _load_config() -> dict:
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _cloudinary_creds(cfg: dict) -> dict | None:
    """Cloudinary creds from CLOUDINARY_URL, discrete env vars, or the config
    file — in that order. Returns None when nothing is configured."""
    url = os.getenv("CLOUDINARY_URL", "").strip()
    if url.startswith("cloudinary://"):
        # cloudinary://<api_key>:<api_secret>@<cloud_name>
        rest = url[len("cloudinary://") :]
        try:
            creds, cloud = rest.split("@", 1)
            api_key, api_secret = creds.split(":", 1)
            return {"cloud_name": cloud, "api_key": api_key, "api_secret": api_secret}
        except ValueError:
            pass
    env = {
        "cloud_name": os.getenv("CLOUDINARY_CLOUD_NAME", "").strip(),
        "api_key": os.getenv("CLOUDINARY_API_KEY", "").strip(),
        "api_secret": os.getenv("CLOUDINARY_API_SECRET", "").strip(),
    }
    if all(env.values()):
        return env
    c = cfg.get("cloudinary") or {}
    if all(c.get(k) for k in ("cloud_name", "api_key", "api_secret")):
        return {k: c[k] for k in ("cloud_name", "api_key", "api_secret")}
    return None


def _require_cloudinary_creds(cfg: dict) -> dict:
    """Creds or a message that says exactly how to supply them. Every entry
    point needs them, and 'no credentials configured' is the single most
    likely first-run failure, so the remedy belongs in the error itself."""
    creds = _cloudinary_creds(cfg)
    if not creds:
        raise MediaHostError(
            "No Cloudinary credentials configured. Set CLOUDINARY_URL "
            "(cloudinary://<api_key>:<api_secret>@<cloud_name>), or the discrete "
            "CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET "
            "env vars, or a 'cloudinary' block in config/media_host.json."
        )
    return creds


# ── Upload ────────────────────────────────────────────────────────────────────


def _cloudinary_resource_type(mime: str) -> str:
    """Cloudinary requires the upload/destroy endpoint's resource_type to match
    the asset (image vs video vs raw) — posting a carousel image to the video
    endpoint is rejected or miscategorized. Every publish asset here is either
    a video/*  or an image/*; anything else (rare) goes through /raw/upload,
    Cloudinary's generic-file endpoint."""
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    return "raw"


def _upload_cloudinary(path: Path, creds: dict) -> str:
    ts = int(time.time())
    # Signed upload: sha1 of the alphabetically-sorted params to sign + api_secret.
    # resource_type is a URL path segment, not a signed param, so it's safe to
    # vary per file without touching the signature.
    to_sign = f"timestamp={ts}{creds['api_secret']}"
    signature = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    resource_type = _cloudinary_resource_type(mime)
    endpoint = f"https://api.cloudinary.com/v1_1/{creds['cloud_name']}/{resource_type}/upload"
    with path.open("rb") as fh:
        resp = requests.post(
            endpoint,
            data={"timestamp": str(ts), "api_key": creds["api_key"], "signature": signature},
            files={"file": (path.name, fh, mime)},
            timeout=UPLOAD_TIMEOUT,
        )
    if resp.status_code >= 400:
        raise MediaHostError(
            f"Cloudinary rejected the upload ({resp.status_code}): {resp.text[:300]}"
        )
    url = (resp.json() or {}).get("secure_url")
    if not url:
        raise MediaHostError(f"Cloudinary returned no secure_url: {resp.text[:300]}")
    return url


def _verify_public_url(url: str) -> None:
    """Confirm the host actually serves the file before handing its URL to a
    publish step. A ranged GET mirrors how IG/FB/TikTok fetch a video_url —
    this is what catches an upload that was accepted but serves an
    empty/broken response, instead of that surfacing only after a platform
    rejects (or silently mis-publishes) the asset."""
    try:
        resp = requests.get(
            url,
            headers={"Range": f"bytes=0-{VERIFY_RANGE_BYTES - 1}"},
            timeout=30,
            stream=True,
        )
    except requests.RequestException as e:
        raise MediaHostError(f"could not verify {url} is publicly reachable: {e}") from e

    try:
        if resp.status_code not in (200, 206):
            raise MediaHostError(f"{url} returned HTTP {resp.status_code} on verification")

        content_length = resp.headers.get("Content-Length")
        if content_length is not None and content_length.strip() == "0":
            raise MediaHostError(f"{url} reports Content-Length: 0 — host served an empty file")

        chunk = next(resp.iter_content(VERIFY_RANGE_BYTES), b"")
        if not chunk:
            raise MediaHostError(
                f"{url} served an empty body on verification — treating as a failed upload"
            )
    finally:
        resp.close()


# ── Public API ────────────────────────────────────────────────────────────────


def _check_backend(name: str | None) -> None:
    """Cloudinary is the only backend. An explicit request for anything else is
    an error rather than a silent substitution — a caller that asked for a
    specific host and quietly got a different one is the failure mode this
    module was rewritten to remove."""
    if name is not None and name != BACKEND:
        raise MediaHostError(
            f"Unknown backend {name!r}. Cloudinary is the only supported backend "
            "(bunny/litterbox/catbox were removed 2026-07-30)."
        )


def host_file(path: str | Path, backend: str | None = None) -> str:
    """Upload a local file to Cloudinary and return its public HTTPS URL,
    verified reachable. Raises MediaHostError with a human-readable reason.

    A verification failure is a hard error, not a signal to try somewhere else:
    there is nowhere else, and an unverifiable URL handed to a publish step is
    worse than a failed publish, because the post goes out broken."""
    if requests is None:
        raise MediaHostError("The 'requests' package is not installed (pip install requests).")
    _check_backend(backend)
    p = Path(os.path.expandvars(str(path))).expanduser()
    if not p.is_file():
        raise MediaHostError(f"File not found: {p}")

    creds = _require_cloudinary_creds(_load_config())
    url = _upload_cloudinary(p, creds)
    _verify_public_url(url)
    return url


def _cloudinary_public_id(url: str) -> str:
    path = urlparse(url).path
    m = re.search(r"/upload/(?:v\d+/)?(.+?)\.[A-Za-z0-9]+$", path)
    if not m:
        raise MediaHostError(f"Could not parse a Cloudinary public_id out of {url!r}")
    return m.group(1)


def _cloudinary_resource_type_from_url(url: str) -> str:
    """The secure_url Cloudinary returns encodes resource_type in the path
    (.../<cloud_name>/<resource_type>/upload/...), so destroy can read back
    exactly what upload used instead of assuming "video" for every asset."""
    path = urlparse(url).path
    m = re.search(r"/(image|video|raw)/upload/", path)
    return m.group(1) if m else "video"


def _delete_cloudinary(url: str, creds: dict) -> None:
    public_id = _cloudinary_public_id(url)
    resource_type = _cloudinary_resource_type_from_url(url)
    ts = int(time.time())
    to_sign = f"public_id={public_id}&timestamp={ts}{creds['api_secret']}"
    signature = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()
    endpoint = f"https://api.cloudinary.com/v1_1/{creds['cloud_name']}/{resource_type}/destroy"
    resp = requests.post(
        endpoint,
        data={
            "public_id": public_id,
            "timestamp": str(ts),
            "api_key": creds["api_key"],
            "signature": signature,
        },
        timeout=UPLOAD_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise MediaHostError(
            f"Cloudinary rejected the delete ({resp.status_code}): {resp.text[:300]}"
        )
    result = (resp.json() or {}).get("result")
    if result not in ("ok", "not found"):
        raise MediaHostError(f"Cloudinary delete returned an unexpected result: {resp.text[:300]}")


def cleanup_file(url: str, backend: str | None = None) -> dict:
    """Delete a previously-hosted file now that the destination platform has
    confirmed it received the media.

    A URL from somewhere other than Cloudinary is refused rather than
    best-effort deleted: reporting a successful cleanup for an asset still
    sitting on a public URL is the one outcome worse than failing."""
    if requests is None:
        raise MediaHostError("The 'requests' package is not installed (pip install requests).")
    _check_backend(backend)

    host = (urlparse(url).hostname or "").lower()
    if host != "res.cloudinary.com":
        raise MediaHostError(
            f"{url!r} is not a Cloudinary URL (host {host or '<none>'}), so this module "
            "cannot delete it. Cloudinary is the only supported backend."
        )

    creds = _require_cloudinary_creds(_load_config())
    _delete_cloudinary(url, creds)
    return {"ok": True, "deleted": True, "backend": BACKEND}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Host a local media file at a public HTTPS URL.")
    ap.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path to the local media file to host. Omit when using --cleanup.",
    )
    ap.add_argument(
        "--backend",
        choices=[BACKEND],
        default=None,
        help="Backend to use. Cloudinary is the only one; the flag is kept so "
        "existing callers passing --backend cloudinary keep working.",
    )
    ap.add_argument(
        "--cleanup",
        metavar="URL",
        default=None,
        help="Delete a previously-hosted file at URL instead of hosting a new "
        "one — call this after the destination platform(s) confirm they have "
        "the media.",
    )
    ap.add_argument("--json", action="store_true", help="Emit a JSON result object.")
    args = ap.parse_args(argv)

    if args.cleanup:
        try:
            result = cleanup_file(args.cleanup, backend=args.backend)
        except MediaHostError as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            else:
                print(f"ERROR: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result.get("note") or ("deleted" if result["deleted"] else "no action needed"))
        return 0

    if not args.path:
        ap.error("path is required unless --cleanup is given")

    try:
        url = host_file(args.path, backend=args.backend)
    except MediaHostError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True, "url": url}, ensure_ascii=False))
    else:
        print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
