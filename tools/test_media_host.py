"""Tests for media_host.py — Cloudinary upload, verification, and cleanup.

No real network calls: every requests.post/get is monkeypatched.

ENVIRONMENT ISOLATION IS THE POINT OF THE AUTOUSE FIXTURE BELOW.
  media_host resolves credentials from CLOUDINARY_URL first, then the discrete
  CLOUDINARY_* env vars, then config/media_host.json. Only the config file has
  an override hook (MEDIA_HOST_CONFIG_PATH) — nothing shadows the env vars.

  So on 2026-07-30, the day a real CLOUDINARY_URL was exported for production,
  six tests in this file started failing at once. Not one of them had a logic
  bug: tests asserting "no credentials configured" saw real ones, and tests
  setting a fake cloud_name of "mycloud" read the real cloud name instead,
  because CLOUDINARY_URL is checked ahead of the discrete vars they set. The
  suite had been silently depending on the developer's shell being empty.

  `hermetic_env` closes that hole for every test in the file at once, rather
  than leaving each test to remember. This is the same lesson the confirmation
  tests learned about memory/user_prefs.json: a test that reads real user
  configuration is not testing the code, it is testing the machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import media_host  # noqa: E402

# Every env var that can inject credentials into media_host. Kept as one list so
# adding a credential source to the module and forgetting it here shows up as a
# test that mysteriously reads production config.
_CREDENTIAL_ENV_VARS = (
    "CLOUDINARY_URL",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
)


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch, tmp_path):
    """Cut every path by which real credentials could reach the module.

    Autouse because the failure mode is silent: a leaked credential does not
    error, it just makes assertions describe the developer's machine instead of
    the code. A test that opts in by hand is a test someone forgets to.
    """
    for var in _CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Point the config file at a path that does not exist, so _load_config()
    # returns {} rather than reading a real config/media_host.json.
    monkeypatch.setenv("MEDIA_HOST_CONFIG_PATH", str(tmp_path / "absent_media_host.json"))


@pytest.fixture
def creds(monkeypatch):
    """A complete set of fake Cloudinary credentials, via the discrete env vars."""
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "mycloud")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "key123")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "secret456")
    return {"cloud_name": "mycloud", "api_key": "key123", "api_secret": "secret456"}


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, headers=None, content=b""):
        self.status_code = status_code
        self.text = text
        self._json = json_data or {}
        self.headers = headers or {}
        self._content = content

    def json(self):
        return self._json

    def iter_content(self, chunk_size):
        if self._content:
            yield self._content

    def close(self):
        pass


@pytest.fixture
def media_file(tmp_path):
    p = tmp_path / "reel.mp4"
    p.write_bytes(b"fake video bytes")
    return p


# ---------------------------------------------------------------------------
# The isolation itself. These are the regression tests for the 2026-07-30
# breakage — if they fail, every other test in this file is untrustworthy.
# ---------------------------------------------------------------------------


def test_hermetic_env_hides_a_real_cloudinary_url():
    """The exact leak: a developer with CLOUDINARY_URL exported must still see
    an unconfigured module here."""
    assert media_host._cloudinary_creds(media_host._load_config()) is None


def test_hermetic_env_hides_a_real_config_file():
    assert media_host._load_config() == {}


def test_cloudinary_url_outranks_discrete_vars(monkeypatch):
    """Why the leak beat tests that set CLOUDINARY_CLOUD_NAME: the URL wins.
    Pinning the precedence keeps the fixture's delenv list honest."""
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://urlkey:urlsecret@urlcloud")
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "discretecloud")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "discretekey")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "discretesecret")
    assert media_host._cloudinary_creds({}) == {
        "cloud_name": "urlcloud",
        "api_key": "urlkey",
        "api_secret": "urlsecret",
    }


def test_malformed_cloudinary_url_falls_through_to_discrete_vars(monkeypatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://nonsense-without-an-at-sign")
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "discretecloud")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "discretekey")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "discretesecret")
    assert media_host._cloudinary_creds({})["cloud_name"] == "discretecloud"


def test_partial_discrete_vars_are_not_credentials(monkeypatch):
    """Two of three is not a usable credential set — better to report nothing
    configured than to send a signed request with an empty secret."""
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "mycloud")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "key123")
    assert media_host._cloudinary_creds({}) is None


def test_config_file_is_the_last_resort(monkeypatch, tmp_path):
    cfg_file = tmp_path / "media_host.json"
    cfg_file.write_text(
        '{"cloudinary": {"cloud_name": "filecloud", "api_key": "fk", "api_secret": "fs"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("MEDIA_HOST_CONFIG_PATH", str(cfg_file))
    assert media_host._cloudinary_creds(media_host._load_config())["cloud_name"] == "filecloud"


# ---------------------------------------------------------------------------
# Backend surface — Cloudinary is the only one (bunny/litterbox/catbox removed
# 2026-07-30). Asking for another must fail loudly, never substitute silently.
# ---------------------------------------------------------------------------


def test_only_cloudinary_is_a_valid_backend():
    media_host._check_backend(None)  # auto
    media_host._check_backend("cloudinary")  # explicit
    for removed in ("catbox", "litterbox", "bunny"):
        with pytest.raises(media_host.MediaHostError, match="Unknown backend"):
            media_host._check_backend(removed)


def test_host_file_rejects_a_removed_backend(media_file):
    with pytest.raises(media_host.MediaHostError, match="Unknown backend"):
        media_host.host_file(media_file, backend="catbox")


def test_removed_backend_helpers_are_gone():
    """The chain is deleted, not merely unreferenced — a leftover
    _upload_catbox would be dead code that still looks callable."""
    for gone in (
        "_upload_catbox",
        "_upload_litterbox",
        "_upload_bunny",
        "_delete_bunny",
        "_bunny_creds",
        "_ordered_backends",
        "_upload_via",
    ):
        assert not hasattr(media_host, gone), f"{gone} should have been removed"


def test_missing_credentials_error_says_how_to_fix_it(media_file):
    """First-run failure mode: the remedy belongs in the message."""
    with pytest.raises(media_host.MediaHostError) as exc:
        media_host.host_file(media_file)
    msg = str(exc.value)
    assert "CLOUDINARY_URL" in msg
    assert "CLOUDINARY_API_SECRET" in msg
    assert "config/media_host.json" in msg


# ---------------------------------------------------------------------------
# Upload verification — a ranged GET must prove the asset is really served
# before its URL reaches a publish step.
# ---------------------------------------------------------------------------


def test_verify_public_url_rejects_zero_content_length(monkeypatch):
    def fake_get(url, headers=None, timeout=None, stream=None):
        return FakeResponse(status_code=200, headers={"Content-Length": "0"})

    monkeypatch.setattr(media_host.requests, "get", fake_get)
    with pytest.raises(media_host.MediaHostError, match="Content-Length: 0"):
        media_host._verify_public_url("https://res.cloudinary.com/c/video/upload/v1/x.mp4")


def test_verify_public_url_rejects_empty_body(monkeypatch):
    def fake_get(url, headers=None, timeout=None, stream=None):
        return FakeResponse(status_code=200, content=b"")

    monkeypatch.setattr(media_host.requests, "get", fake_get)
    with pytest.raises(media_host.MediaHostError, match="empty body"):
        media_host._verify_public_url("https://res.cloudinary.com/c/video/upload/v1/x.mp4")


def test_verify_public_url_rejects_error_status(monkeypatch):
    def fake_get(url, headers=None, timeout=None, stream=None):
        return FakeResponse(status_code=404)

    monkeypatch.setattr(media_host.requests, "get", fake_get)
    with pytest.raises(media_host.MediaHostError, match="HTTP 404"):
        media_host._verify_public_url("https://res.cloudinary.com/c/video/upload/v1/x.mp4")


def test_verify_public_url_accepts_partial_content(monkeypatch):
    def fake_get(url, headers=None, timeout=None, stream=None):
        return FakeResponse(status_code=206, content=b"some bytes")

    monkeypatch.setattr(media_host.requests, "get", fake_get)
    media_host._verify_public_url("https://res.cloudinary.com/c/video/upload/v1/x.mp4")  # no raise


# ---------------------------------------------------------------------------
# host_file end to end
# ---------------------------------------------------------------------------


def test_host_file_returns_the_verified_url(monkeypatch, media_file, creds):
    def fake_post(url, data=None, files=None, timeout=None):
        return FakeResponse(
            status_code=200,
            json_data={"secure_url": "https://res.cloudinary.com/mycloud/video/upload/v1/r.mp4"},
        )

    def fake_get(url, headers=None, timeout=None, stream=None):
        return FakeResponse(status_code=206, content=b"real bytes")

    monkeypatch.setattr(media_host.requests, "post", fake_post)
    monkeypatch.setattr(media_host.requests, "get", fake_get)

    assert (
        media_host.host_file(media_file)
        == "https://res.cloudinary.com/mycloud/video/upload/v1/r.mp4"
    )


def test_host_file_verification_failure_is_fatal(monkeypatch, media_file, creds):
    """With the fallback chain gone, an unverifiable upload has nowhere to fall
    through to — and must not be returned. Handing a publish step a URL that
    serves nothing means the post goes out broken, which is worse than a
    failed publish."""

    def fake_post(url, data=None, files=None, timeout=None):
        return FakeResponse(
            status_code=200,
            json_data={"secure_url": "https://res.cloudinary.com/mycloud/video/upload/v1/b.mp4"},
        )

    def fake_get(url, headers=None, timeout=None, stream=None):
        return FakeResponse(status_code=200, headers={"Content-Length": "0"})

    monkeypatch.setattr(media_host.requests, "post", fake_post)
    monkeypatch.setattr(media_host.requests, "get", fake_get)

    with pytest.raises(media_host.MediaHostError, match="Content-Length: 0"):
        media_host.host_file(media_file)


def test_host_file_reports_the_upload_error_verbatim(monkeypatch, media_file, creds):
    """No more "All media hosts failed" wrapper — one backend, one reason."""

    def fake_post(url, data=None, files=None, timeout=None):
        return FakeResponse(status_code=500, text="server error")

    monkeypatch.setattr(media_host.requests, "post", fake_post)

    with pytest.raises(media_host.MediaHostError) as exc:
        media_host.host_file(media_file)
    msg = str(exc.value)
    assert "Cloudinary rejected the upload (500)" in msg
    assert "server error" in msg
    assert "All media hosts failed" not in msg


def test_host_file_missing_file(creds):
    with pytest.raises(media_host.MediaHostError, match="File not found"):
        media_host.host_file("definitely_not_a_real_file.mp4")


# ---------------------------------------------------------------------------
# Cloudinary resource_type — a carousel IMAGE posted to the video endpoint is
# rejected or miscategorized. Upload must pick the endpoint from the file's
# mime type; destroy must read back whatever upload actually used, from the
# URL, rather than assuming every asset is a video.
# ---------------------------------------------------------------------------


def test_cloudinary_resource_type_from_mime():
    assert media_host._cloudinary_resource_type("video/mp4") == "video"
    assert media_host._cloudinary_resource_type("image/png") == "image"
    assert media_host._cloudinary_resource_type("image/jpeg") == "image"
    assert media_host._cloudinary_resource_type("application/octet-stream") == "raw"


@pytest.mark.parametrize(
    ("filename", "expected_segment"),
    [("reel.mp4", "video"), ("cover.png", "image"), ("notes.bin", "raw")],
)
def test_upload_cloudinary_picks_the_endpoint_from_mime(
    monkeypatch, tmp_path, creds, filename, expected_segment
):
    """The bug this covers: before it, every upload hit /video/upload
    regardless of file type, which Cloudinary rejects or miscategorizes for a
    carousel image — a real, previously-untested failure mode."""
    calls = {}

    def fake_post(url, data=None, files=None, timeout=None):
        calls["url"] = url
        return FakeResponse(
            status_code=200, json_data={"secure_url": "https://res.cloudinary.com/x"}
        )

    monkeypatch.setattr(media_host.requests, "post", fake_post)
    p = tmp_path / filename
    p.write_bytes(b"fake bytes")

    media_host._upload_cloudinary(p, creds)
    assert calls["url"] == f"https://api.cloudinary.com/v1_1/mycloud/{expected_segment}/upload"


def test_upload_cloudinary_signs_the_timestamp(monkeypatch, tmp_path, creds):
    """A wrong signature is a 401 that reads like bad credentials, so pin the
    scheme: sha1 of the sorted params to sign plus the api_secret."""
    import hashlib

    calls = {}

    def fake_post(url, data=None, files=None, timeout=None):
        calls["data"] = data
        return FakeResponse(
            status_code=200, json_data={"secure_url": "https://res.cloudinary.com/x"}
        )

    monkeypatch.setattr(media_host.requests, "post", fake_post)
    p = tmp_path / "reel.mp4"
    p.write_bytes(b"fake bytes")

    media_host._upload_cloudinary(p, creds)
    ts = calls["data"]["timestamp"]
    expected = hashlib.sha1(f"timestamp={ts}{creds['api_secret']}".encode()).hexdigest()
    assert calls["data"]["signature"] == expected
    assert calls["data"]["api_key"] == "key123"


def test_upload_cloudinary_rejects_a_response_without_secure_url(monkeypatch, tmp_path, creds):
    def fake_post(url, data=None, files=None, timeout=None):
        return FakeResponse(status_code=200, json_data={"error": "nope"}, text='{"error":"nope"}')

    monkeypatch.setattr(media_host.requests, "post", fake_post)
    p = tmp_path / "reel.mp4"
    p.write_bytes(b"fake bytes")

    with pytest.raises(media_host.MediaHostError, match="no secure_url"):
        media_host._upload_cloudinary(p, creds)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_cleanup_cloudinary_issues_destroy(monkeypatch, creds):
    calls = {}

    def fake_post(url, data=None, timeout=None, files=None):
        calls["url"] = url
        calls["data"] = data
        return FakeResponse(status_code=200, json_data={"result": "ok"})

    monkeypatch.setattr(media_host.requests, "post", fake_post)

    url = "https://res.cloudinary.com/mycloud/video/upload/v1234567890/reels/ktc_approved.mp4"
    assert media_host.cleanup_file(url) == {
        "ok": True,
        "deleted": True,
        "backend": "cloudinary",
    }
    assert calls["url"] == "https://api.cloudinary.com/v1_1/mycloud/video/destroy"
    assert calls["data"]["public_id"] == "reels/ktc_approved"


def test_cleanup_cloudinary_destroys_image_via_image_endpoint(monkeypatch, creds):
    """destroy must match whatever resource_type upload actually used —
    parsed from the URL Cloudinary returned, not hardcoded to 'video'."""
    calls = {}

    def fake_post(url, data=None, timeout=None, files=None):
        calls["url"] = url
        return FakeResponse(status_code=200, json_data={"result": "ok"})

    monkeypatch.setattr(media_host.requests, "post", fake_post)

    url = "https://res.cloudinary.com/mycloud/image/upload/v1234567890/carousel/cover.png"
    assert media_host.cleanup_file(url)["deleted"] is True
    assert calls["url"] == "https://api.cloudinary.com/v1_1/mycloud/image/destroy"


def test_cleanup_treats_not_found_as_success(monkeypatch, creds):
    """Cleanup runs after a platform confirms receipt, so an asset already gone
    is the desired end state, not an error to retry."""

    def fake_post(url, data=None, timeout=None, files=None):
        return FakeResponse(status_code=200, json_data={"result": "not found"})

    monkeypatch.setattr(media_host.requests, "post", fake_post)
    url = "https://res.cloudinary.com/mycloud/video/upload/v1/gone.mp4"
    assert media_host.cleanup_file(url)["deleted"] is True


def test_cleanup_refuses_a_non_cloudinary_url(creds):
    """Reporting a successful cleanup for an asset still sitting on a public
    URL is the one outcome worse than failing."""
    for foreign in (
        "https://files.catbox.moe/x.mp4",
        "https://litter.catbox.moe/x.mp4",
        "https://cdn.example.com/reel.mp4",
    ):
        with pytest.raises(media_host.MediaHostError, match="not a Cloudinary URL"):
            media_host.cleanup_file(foreign)


def test_cleanup_without_credentials_fails_loudly():
    url = "https://res.cloudinary.com/mycloud/video/upload/v1/x.mp4"
    with pytest.raises(media_host.MediaHostError, match="No Cloudinary credentials"):
        media_host.cleanup_file(url)


def test_cloudinary_public_id_parsing():
    cases = {
        "https://res.cloudinary.com/c/video/upload/v1234/reels/a_b.mp4": "reels/a_b",
        "https://res.cloudinary.com/c/video/upload/reels/a_b.mp4": "reels/a_b",
        "https://res.cloudinary.com/c/image/upload/v1/cover.png": "cover",
    }
    for url, expected in cases.items():
        assert media_host._cloudinary_public_id(url) == expected


def test_cloudinary_resource_type_from_url():
    assert (
        media_host._cloudinary_resource_type_from_url(
            "https://res.cloudinary.com/c/image/upload/v1/x.png"
        )
        == "image"
    )
    assert (
        media_host._cloudinary_resource_type_from_url(
            "https://res.cloudinary.com/c/raw/upload/v1/x.bin"
        )
        == "raw"
    )
    # Unparseable falls back to video — the overwhelmingly common asset here.
    assert media_host._cloudinary_resource_type_from_url("https://res.cloudinary.com/x") == "video"


# ---------------------------------------------------------------------------
# MEDIA_HOST_CONFIG_PATH — the module has no other path-isolation, so this
# override exists specifically so a real config/media_host.json a user
# creates can never bleed into what the test suite reads.
# ---------------------------------------------------------------------------


def test_config_path_honours_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom_media_host.json"
    custom.write_text('{"note": "custom"}', encoding="utf-8")
    monkeypatch.setenv("MEDIA_HOST_CONFIG_PATH", str(custom))
    assert media_host._load_config() == {"note": "custom"}


def test_config_path_defaults_without_override(monkeypatch):
    monkeypatch.delenv("MEDIA_HOST_CONFIG_PATH", raising=False)
    assert media_host._config_path() == media_host.CONFIG_PATH


def test_load_config_survives_invalid_json(monkeypatch, tmp_path):
    """Degrade, never raise: a broken config must cost configuration, not the
    publish — the credentials may still be in the environment."""
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("MEDIA_HOST_CONFIG_PATH", str(broken))
    assert media_host._load_config() == {}
