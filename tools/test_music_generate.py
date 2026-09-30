"""Tests for music_generate.py (Lyria RealTime background-music generation)
and core.utils.get_music_api_key().

Live Lyria calls cost real API quota and take real time, so nothing here
calls the actual model. What CAN be verified offline — and is, because a live
probe against a real key on 2026-07-20 already confirmed the parts that
can't be — is covered:

  * key resolution precedence (core.utils.get_music_api_key)
  * mime_type parsing (the one thing the live probe discovered empirically:
    "audio/l16;rate=48000;channels=2", not the documented-sounding default)
  * WAV file writing from raw PCM
  * the ffmpeg transcode path (ffmpeg itself IS actually invoked here — it's
    free, local, and fast, unlike the Lyria call)
  * CLI flag -> LiveMusicGenerationConfig kwarg wiring
  * error surfacing (JSON vs stderr, non-zero exit) without a live session

The `_capture()` coroutine (the actual websocket call) is mocked everywhere
except test_live_smoke, which runs only when ERA_LIVE_TESTS=1 is set — never
in a routine suite run, only for a deliberate manual re-verification.
"""

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import music_generate as mg  # noqa: E402
from core import utils as core_utils  # noqa: E402

# ---------------------------------------------------------------------------
# core.utils.get_music_api_key — precedence chain
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("GEMINI_MUSIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(core_utils, "load_config", lambda: {})
    return monkeypatch


def test_music_key_prefers_dedicated_config_key(clean_env):
    clean_env.setattr(core_utils, "load_config", lambda: {"gemini_music_api_key": "dedicated"})
    assert core_utils.get_music_api_key() == "dedicated"


def test_music_key_falls_back_to_dedicated_env(clean_env):
    clean_env.setenv("GEMINI_MUSIC_API_KEY", "dedicated-env")
    assert core_utils.get_music_api_key() == "dedicated-env"


def test_music_key_falls_back_to_main_config_key(clean_env):
    clean_env.setattr(core_utils, "load_config", lambda: {"gemini_api_key": "shared"})
    assert core_utils.get_music_api_key() == "shared"


def test_music_key_falls_back_to_main_env(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "shared-env")
    assert core_utils.get_music_api_key() == "shared-env"


def test_music_key_dedicated_wins_over_shared(clean_env):
    """The whole point of a dedicated key (quota isolation from voice) fails
    silently if the shared key is ever preferred by accident."""
    clean_env.setattr(
        core_utils,
        "load_config",
        lambda: {"gemini_music_api_key": "dedicated", "gemini_api_key": "shared"},
    )
    assert core_utils.get_music_api_key() == "dedicated"


def test_music_key_raises_when_nothing_configured(clean_env):
    with pytest.raises(RuntimeError, match="No Gemini API key found for music"):
        core_utils.get_music_api_key()


# ---------------------------------------------------------------------------
# mime_type parsing — the one detail only a live call could reveal
# ---------------------------------------------------------------------------


def test_parses_confirmed_live_mime_type():
    """Exact string observed from a real Lyria RealTime response, 2026-07-20."""
    rate, channels = mg._parse_pcm_format("audio/l16;rate=48000;channels=2")
    assert (rate, channels) == (48000, 2)


def test_parses_different_rate_and_channels():
    rate, channels = mg._parse_pcm_format("audio/l16;rate=24000;channels=1")
    assert (rate, channels) == (24000, 1)


def test_missing_mime_type_degrades_to_fallback():
    assert mg._parse_pcm_format(None) == (mg._FALLBACK_SAMPLE_RATE, mg._FALLBACK_CHANNELS)


def test_malformed_mime_type_degrades_to_fallback():
    assert mg._parse_pcm_format("not a mime type at all") == (
        mg._FALLBACK_SAMPLE_RATE,
        mg._FALLBACK_CHANNELS,
    )


# ---------------------------------------------------------------------------
# WAV writing
# ---------------------------------------------------------------------------


def test_write_wav_produces_a_valid_readable_file(tmp_path):
    pcm = (b"\x00\x01" * 4) * 48000  # 1 second of fake stereo 16-bit silence-ish noise
    out = tmp_path / "track.wav"
    mg._write_wav(pcm, sample_rate=48000, channels=2, out_path=out)

    with wave.open(str(out), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getsampwidth() == 2
        assert w.getframerate() == 48000
        assert w.getnframes() == len(pcm) // (2 * 2)


def test_write_wav_creates_parent_directories(tmp_path):
    out = tmp_path / "nested" / "dir" / "track.wav"
    mg._write_wav(b"\x00\x00" * 100, 48000, 2, out)
    assert out.exists()


# ---------------------------------------------------------------------------
# ffmpeg transcode — real ffmpeg, no network
# ---------------------------------------------------------------------------


def test_wav_output_skips_transcode(tmp_path):
    wav = tmp_path / "track.wav"
    mg._write_wav(b"\x00\x00" * 48000, 48000, 2, wav)
    result = mg._transcode_if_needed(wav, tmp_path / "track.wav")
    assert result == wav
    assert wav.exists()


@pytest.mark.skipif(
    subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0,
    reason="ffmpeg not on PATH in this environment",
)
def test_mp3_output_actually_transcodes_and_removes_source_wav(tmp_path):
    wav = tmp_path / "track.wav"
    mg._write_wav(b"\x00\x01" * 4 * 48000, 48000, 2, wav)
    mp3 = tmp_path / "track.mp3"

    result = mg._transcode_if_needed(wav, mp3)

    assert result == mp3
    assert mp3.exists() and mp3.stat().st_size > 0
    assert not wav.exists(), "intermediate WAV must be cleaned up after transcode"


def test_transcode_failure_raises_music_generation_error(tmp_path, monkeypatch):
    wav = tmp_path / "track.wav"
    mg._write_wav(b"\x00\x00" * 48000, 48000, 2, wav)

    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(a, returncode=1, stdout="", stderr="ffmpeg exploded")

    monkeypatch.setattr(mg.subprocess, "run", fake_run)
    with pytest.raises(mg.MusicGenerationError, match="ffmpeg exploded"):
        mg._transcode_if_needed(wav, tmp_path / "track.mp3")


# ---------------------------------------------------------------------------
# generate_music — full flow with _capture mocked (no live call)
# ---------------------------------------------------------------------------


def test_generate_music_end_to_end_with_mocked_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(mg, "get_music_api_key", lambda: "fake-key")
    fake_pcm = (b"\x00\x01" * 4) * 48000  # 1s of fake audio at 48kHz/stereo/16-bit

    async def fake_capture(prompt, duration_seconds, api_key, model, gen_config):
        assert api_key == "fake-key"
        assert prompt == "test prompt"
        return fake_pcm, 48000, 2

    monkeypatch.setattr(mg, "_capture", fake_capture)
    out = tmp_path / "track.wav"

    result = mg.generate_music("test prompt", 1.0, out)

    assert result["ok"] is True
    assert result["sample_rate"] == 48000
    assert result["channels"] == 2
    assert Path(result["path"]).exists()


def test_generate_music_propagates_capture_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(mg, "get_music_api_key", lambda: "fake-key")

    async def failing_capture(*a, **kw):
        raise mg.MusicGenerationError("session closed unexpectedly")

    monkeypatch.setattr(mg, "_capture", failing_capture)

    with pytest.raises(mg.MusicGenerationError, match="session closed"):
        mg.generate_music("prompt", 5.0, tmp_path / "out.wav")


# ---------------------------------------------------------------------------
# CLI — flag wiring and error surfacing, generate_music mocked
# ---------------------------------------------------------------------------


def _run_cli(args, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["music_generate.py", *args])
    exit_code = mg.main()
    captured = capsys.readouterr()
    return exit_code, captured


def test_cli_wires_generation_config_flags(monkeypatch, capsys, tmp_path):
    captured_kwargs = {}

    def fake_generate_music(prompt, duration, out_path, model=mg.DEFAULT_MODEL, **gen_config):
        captured_kwargs.update(gen_config)
        return {
            "ok": True,
            "path": str(out_path),
            "duration_seconds": duration,
            "sample_rate": 48000,
            "channels": 2,
        }

    monkeypatch.setattr(mg, "generate_music", fake_generate_music)
    exit_code, out = _run_cli(
        [
            "--prompt",
            "test",
            "--out",
            str(tmp_path / "t.wav"),
            "--bpm",
            "120",
            "--scale",
            "C_MAJOR_A_MINOR",
            "--mute-drums",
            "--json",
        ],
        monkeypatch,
        capsys,
    )

    assert exit_code == 0
    assert captured_kwargs == {"bpm": 120, "scale": "C_MAJOR_A_MINOR", "mute_drums": True}
    payload = json.loads(out.out)
    assert payload["ok"] is True


def test_cli_reports_failure_as_json_with_nonzero_exit(monkeypatch, capsys, tmp_path):
    def failing_generate_music(*a, **kw):
        raise mg.MusicGenerationError("no audio received")

    monkeypatch.setattr(mg, "generate_music", failing_generate_music)
    exit_code, out = _run_cli(
        ["--prompt", "test", "--out", str(tmp_path / "t.wav"), "--json"],
        monkeypatch,
        capsys,
    )

    assert exit_code == 1
    payload = json.loads(out.out)
    assert payload == {"ok": False, "error": "no audio received"}


def test_cli_reports_failure_to_stderr_without_json_flag(monkeypatch, capsys, tmp_path):
    def failing_generate_music(*a, **kw):
        raise mg.MusicGenerationError("no audio received")

    monkeypatch.setattr(mg, "generate_music", failing_generate_music)
    exit_code, out = _run_cli(
        ["--prompt", "test", "--out", str(tmp_path / "t.wav")],
        monkeypatch,
        capsys,
    )

    assert exit_code == 1
    assert "no audio received" in out.err
    assert out.out == ""


# ---------------------------------------------------------------------------
# Live smoke test — opt-in only, real API call, real cost
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("ERA_LIVE_TESTS") != "1",
    reason="opt-in: makes a REAL, paid Lyria call — set ERA_LIVE_TESTS=1 to run",
)
@pytest.mark.slow
def test_live_smoke_generates_real_audio(tmp_path):
    """Deliberately opt-in: costs real quota. Run manually with ERA_LIVE_TESTS=1
    to re-verify after an SDK upgrade or if the endpoint starts 404ing again
    (v1alpha is what worked 2026-07-20; Google could move it). The key comes
    from wherever Era reads it (config first, then env) — the old gate checked
    only the environment, so it skipped even with a key configured."""
    from core.utils import get_music_api_key

    assert get_music_api_key(), "ERA_LIVE_TESTS=1 but no music/Gemini key is configured"
    result = mg.generate_music("short calm ambient pad, no vocals", 5.0, tmp_path / "smoke.wav")
    assert result["ok"] is True
    path = Path(result["path"])
    assert path.exists() and path.stat().st_size > 1000
