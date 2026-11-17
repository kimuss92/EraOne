"""Real status for the HUD: health, providers, integrations, diagnostics.

Every value is read from the running engine, the OS, or a file the backend
owns. Where a value cannot be known it is reported as None / "unavailable" —
the HUD renders that as N/A. Nothing here returns a credential: integrations
report only whether they are configured.
"""

from __future__ import annotations

import functools
import os
import platform
import socket
import threading
import time
from pathlib import Path
from typing import Any

from app.hud import tool_adapter
from app.hud.events import hud

_BUILD = None


def build_id() -> str:
    """Backend build stamp: main.py's modification time (git is disabled here)."""
    global _BUILD
    if _BUILD is None:
        try:
            mtime = (Path(__file__).resolve().parents[2] / "main.py").stat().st_mtime
            _BUILD = time.strftime("%Y.%m.%d-%H%M", time.localtime(mtime))
        except OSError:
            _BUILD = "unknown"
    return _BUILD


@functools.lru_cache(maxsize=1)
def os_name() -> str:
    try:
        if platform.system() == "Windows":
            ver = platform.version()  # e.g. 10.0.26200
            build = int(ver.split(".")[-1]) if ver else 0
            return f"Windows {'11' if build >= 22000 else platform.release()} ({ver})"
        return f"{platform.system()} {platform.release()}"
    except Exception:
        return "unknown"


# ── internet reachability (cached, off the event loop) ────────────────────

_net = {"at": 0.0, "online": None, "busy": False}


def _probe() -> None:
    online = False
    for host in (("1.1.1.1", 443), ("8.8.8.8", 443)):
        try:
            with socket.create_connection(host, timeout=1.5):
                online = True
                break
        except OSError:
            continue
    _net.update(at=time.monotonic(), online=online, busy=False)


def internet_online() -> bool | None:
    """Last probe result; refreshed in the background at most every 15 s."""
    if not _net["busy"] and time.monotonic() - _net["at"] > 15:
        _net["busy"] = True
        threading.Thread(target=_probe, name="HudNetProbe", daemon=True).start()
    return _net["online"]


# ── providers & integrations ──────────────────────────────────────────────


def voice_providers(engine: Any) -> dict:
    mode = None
    try:
        from core.voice_mode import VOICE_CFG

        mode = VOICE_CFG.mode
    except Exception:
        pass
    tts = {
        "gemini_audio": "Gemini Live native audio",
        "fish_audio": "Fish Audio",
        "kokoro_audio": "Kokoro (local)",
    }.get(mode or "", mode or "unknown")
    local = bool(getattr(engine, "_local_mode_active", False))
    stt = "Local STT (sherpa-onnx)" if local else "Gemini Live input transcription"
    return {"voiceMode": mode, "tts": tts, "stt": stt, "localMode": local}


def audio_output() -> dict:
    try:
        from core import voice_output

        route = voice_output.active_route() or {}
        pin = voice_output.get_pin() or {}
        current = route.get("generation") == voice_output.generation()
        return {
            "device": (route.get("name") or None) if current else None,
            "requested_device": pin.get("name"),
            "routing_verified": voice_output.route_matches_pin(pin or None),
            "pinned": bool(pin),
            "fallback": bool(route.get("fallback")) if current else False,
        }
    except Exception:
        return {"device": None, "pinned": False, "fallback": False}


def integrations() -> list[dict]:
    rows: list[dict] = []

    def add(key: str, name: str, fn) -> None:
        try:
            ok = bool(fn())
            rows.append({"id": key, "name": name, "connected": ok})
        except Exception:
            rows.append({"id": key, "name": name, "connected": False})

    def _cfg(*keys: str) -> bool:
        from core.utils import load_config

        cfg = load_config() or {}
        return all(str(cfg.get(k) or os.environ.get(k) or "").strip() for k in keys)

    def _fish() -> bool:
        from core.tts_fish import is_configured

        return is_configured()

    def _gmail() -> bool:
        from core.gmail_auth import token_path

        return token_path().is_file()

    add("gemini", "Gemini", lambda: _cfg("gemini_api_key"))
    add("fish", "Fish Audio", _fish)
    add("spotify", "Spotify", lambda: _cfg("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET"))
    add("gmail", "Gmail", _gmail)
    add(
        "telegram",
        "Telegram",
        lambda: os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"),
    )
    # No calendar integration exists in Era. Reported, never faked.
    rows.append({"id": "calendar", "name": "Calendar", "connected": False, "available": False})
    return rows


# ── capabilities (the real tool registry) ─────────────────────────────────


def capabilities() -> dict:
    from app.hud.views import snapshot as view_registry

    try:
        from app.tool_declarations import TOOL_DECLARATIONS
    except Exception:
        TOOL_DECLARATIONS = []
    tools = []
    for decl in TOOL_DECLARATIONS:
        name = str(decl.get("name", "")) if isinstance(decl, dict) else ""
        if not name:
            continue
        desc = " ".join(str(decl.get("description", "")).split())
        first = desc.split(". ")[0][:160]
        category, label, _ = tool_adapter.meta(name)
        tools.append({"name": name, "label": label, "category": category, "description": first})
    groups: dict[str, list[str]] = {}
    for t in tools:
        groups.setdefault(t["category"], []).append(t["name"])
    return {
        "count": len(tools),
        "tools": tools,
        "skills": [{"id": k, "tools": v, "count": len(v)} for k, v in sorted(groups.items())],
        "integrations": integrations(),
        "hud_views": view_registry(),
    }


# ── status / health / diagnostics ─────────────────────────────────────────


def _mic_state(engine: Any) -> str:
    if engine is None:
        return "unavailable"
    try:
        if getattr(engine, "_standby_active", False):
            return "standby"
        if getattr(engine, "_mic_muted", False):
            return "muted"
        fn = getattr(engine, "mic_effective_active", None)
        if callable(fn) and not fn():
            return "off"
        return "active"
    except Exception:
        return "unavailable"


_last_mode: dict = {"mode": None, "kind": None}


def _reason_kind(why: str) -> str:
    """The reason without its ticking numbers ("window open, 23s left" ->
    "window open"), so a countdown never floods the log."""
    return why.split(",")[0].strip()


def listening_mode(engine: Any) -> str:
    """Read the engine's real window; never start or re-anchor one for the HUD."""
    mode, why = _listening_mode_and_reason(engine)
    kind = _reason_kind(why)
    if mode != _last_mode["mode"] or kind != _last_mode.get("kind"):
        # PF-040: every change of mode OR of reason is logged, so "inactivity
        # never came" is traced even while the mode stays "active" (window
        # open -> busy: processing; dev-review).
        _last_mode["mode"] = mode
        _last_mode["kind"] = kind
        try:
            from core.logger import log_event, main_logger

            log_event(main_logger, "listening.mode", "INFO", mode=mode, reason=why)
        except Exception:
            pass
    return mode


def _listening_mode_and_reason(engine: Any) -> tuple[str, str]:
    if engine is None:
        return "unavailable", "no engine"
    try:
        mic = _mic_state(engine)
        if mic != "active":
            return mic, "mic"
        state = getattr(engine, "_listening", None)
        if state is None:
            return "unavailable", "no listening state yet"
        if state.window_open():
            return "active", f"window open, {state.session_remaining_s():.0f}s left"
        from app.era_live.audio import AudioIOMixin

        reason = (
            AudioIOMixin._busy_reason(engine)
            if isinstance(engine, AudioIOMixin)
            else ("busy" if engine._attention_keep_open() else None)
        )
        if reason:
            return "active", f"busy: {reason}"
        return "inactive", "window expired"
    except Exception as e:
        return "unavailable", f"{type(e).__name__}"


def health(server: Any, engine: Any) -> dict:
    ready = engine is not None
    return {
        "app": "eraone",
        "status": "ready" if ready else "starting",
        "version": "hud-2",
        "build": build_id(),
        "pid": os.getpid(),
        "port": getattr(server, "bound_port", None),
        "agent": "ready" if ready else "initializing",
        "voice": _mic_state(engine) if ready else "initializing",
        "model": hud.connection_info().get("kind"),
        "hud_clients": server.client_count() if hasattr(server, "client_count") else 0,
        "uptime_s": round(time.time() - getattr(server, "started_at", time.time()), 1),
    }


def status(server: Any, engine: Any) -> dict:
    conn = hud.connection_info()
    tools = capabilities()
    model = hud.model()
    return {
        "health": health(server, engine),
        "agent": hud.state(),
        "connection": conn,
        "model": {
            "voiceSession": getattr(server, "meta", {}).get("live_model"),
            "routed": model,
        },
        "voice": voice_providers(engine),
        "mic": _mic_state(engine),
        "listeningMode": listening_mode(engine),
        "voiceLock": _voice_lock(),
        "audioOutput": audio_output(),
        "tools": {"count": tools["count"], "active": hud.active_tools()},
        "memory": memory_overview(),
        "internet": internet_online(),
        "localServer": {"online": True, "port": getattr(server, "bound_port", None)},
        "integrations": tools["integrations"],
    }


def _voice_lock() -> bool | None:
    try:
        from core.speaker_recognition import get_verifier

        return bool(get_verifier().enabled)
    except Exception:
        return None


def _fabric_health() -> list[dict]:
    """Era Fabric F1 breakers (core/fabric/health.py); [] when the fabric is absent."""
    try:
        from core.fabric import health

        return health.snapshot()
    except Exception:
        return []


def diagnostics(server: Any, engine: Any) -> dict:
    from core.tool_health import snapshot as tool_health
    from crew.reliability import snapshot as crew_health

    return {
        "status": status(server, engine),
        "latency": hud.latency(),
        "failures": hud.failures(),
        "toolHealth": tool_health(),
        "crewHealth": crew_health(),
        "fabricHealth": _fabric_health(),
        "recentEvents": hud.recent_events(60),
        "websocket": {"clients": server.client_count() if hasattr(server, "client_count") else 0},
        "process": {"pid": os.getpid(), "python": platform.python_version(), "os": os_name()},
    }


# ── memory ────────────────────────────────────────────────────────────────


def memory_overview() -> dict:
    out: dict[str, Any] = {"session": dict(hud.memory_stats())}
    try:
        from memory.memory_manager import MEMORY_PATH, load_memory

        mem = load_memory() or {}
        cats = {k: len(v) for k, v in mem.items() if isinstance(v, dict) and not k.startswith("_")}
        out.update(
            categories=cats,
            entries=sum(cats.values()),
            lastUpdate=MEMORY_PATH.stat().st_mtime if MEMORY_PATH.exists() else None,
        )
    except Exception:
        out.update(categories={}, entries=None, lastUpdate=None)
    return out


def memory_search(query: str, limit: int = 8) -> list[dict]:
    """Read-only ranked lookup (no access-count reinforcement)."""
    query = (query or "").strip()[:200]
    if not query:
        return []
    from memory.memory_manager import retrieve_ranked_memory

    rows = retrieve_ranked_memory(query, top_k=max(1, min(limit, 20))) or []
    return [
        {
            "category": str(r.get("category", "")),
            "key": str(r.get("key", "")),
            "value": str(r.get("value", ""))[:240],
        }
        for r in rows
    ]


# ── weather (Era's own Open-Meteo source), cached ─────────────────────────

_weather_cache: dict[str, tuple[float, dict]] = {}


def weather(city: str) -> dict:
    key = (city or "").strip().lower()
    hit = _weather_cache.get(key)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    from actions.weather_report import fetch_current

    try:
        data = {"ok": True, **fetch_current(city)}
    except Exception as e:
        data = {"ok": False, "city": city, "error": type(e).__name__}
    # 5 min for a real reading (Open-Meteo's nowcast moves every 15 min, so this
    # never lags more than one step); a failure is retried after 1 min.
    _weather_cache[key] = (time.monotonic() + (300 if data["ok"] else 60), data)
    return data
