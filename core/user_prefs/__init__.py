"""
Persistent user-granted preference exceptions and behavior rules.

This is the layer that makes explicit user instructions survive a relaunch:

- Confirmation waivers: "don't ask my confirmation when I close Spotify"
  -> safety_guard consults `is_close_confirmation_waived("spotify")` and
     downgrades the close action to LOW for that app only.
  Reversible: "from now on Spotify needs my confirmation before closing"
  -> `set_close_confirmation_waiver("spotify", False)`.

- Behavior rules: free-text standing instructions like
  "minimize Spotify when you're done with it". These are injected into the
  system prompt at every session build so the model applies them after any
  relaunch.

Storage: memory/user_prefs.json (created lazily, atomic writes via safe_store).

This package is split by feature area (close waiver, file-creation waiver,
post-action minimize, auto-memory, behavior rules, feedback auto-promotion,
prompt formatting), but the storage core (PREFS_PATH, the lock, load/save)
stays here in __init__.py — every submodule reads and writes through it, and
tests monkeypatch `core.user_prefs.PREFS_PATH` directly to isolate storage,
which only works if this module owns that global. Every public name from the
submodules is re-exported here so `from core.user_prefs import X` keeps
working exactly as it did when this was a single file.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from core.utils import get_memory_dir
from memory.safe_store import safe_write_json

PREFS_PATH = get_memory_dir() / "user_prefs.json"
_LOCK = threading.RLock()

_DEFAULT: dict[str, Any] = {
    "version": 1,
    "close_confirmation_waived": {},  # app_name -> {"granted_at": iso, "active": bool}
    "behavior_rules": [],  # [{"rule": str, "added_at": iso}]
    # app_name -> {"granted_at": iso, "active": bool}. "minimize Spotify after
    # use" — enforced by the ACTION layer, not by the prompt (see
    # set_post_action_minimize for why a free-text rule was not enough).
    "post_action_minimize": {},
    # When True (default), Era may save a memory on her own initiative — no
    # "remember this" keyword, no spoken confirmation — as long as the value
    # is grounded in what the user actually said (see main._autonomous_memory_save_allowed).
    "auto_memory": True,
    # When True, file_controller write/create_file/create_folder actions skip
    # confirmation (safety_guard.classify_action). Default False: file creation
    # keeps its MEDIUM confirmation gate until the user explicitly waives it.
    "file_creation_confirmation_waived": False,
    # category -> {"active": bool, "changed_at": iso}. Per-category HIGH-risk
    # confirmation waivers (risk_waiver.WAIVABLE) — never one global switch.
    "risk_confirmation_waived": {},
    # Persisted voice-lock preference so it survives restarts (see voice_lock tool).
    "voice_lock_enabled": True,
    # Voice mic-mute ("stop listening") — persisted so a crash-relaunch cannot
    # silently reopen a mic the user closed. See core/mic_mute.py.
    "mic_muted": False,
    # The output device the user pinned Era's SPOKEN VOICE to, or None for the
    # automatic route. {"id", "name", "set_at", "voice_lock"}. Deliberately NOT
    # the Windows default device: media playback is a separate setting and must
    # not move when her voice does. See core/voice_output.py.
    "voice_output_device": None,
}


def _normalize_app(app: str) -> str:
    """Canonical app key: lowercase, no .exe suffix, trimmed."""
    import re

    a = (app or "").strip().lower()
    a = re.sub(r"\.exe$", "", a)
    return a.strip()


def _load_unlocked() -> dict[str, Any]:
    if not PREFS_PATH.exists():
        return json.loads(json.dumps(_DEFAULT))
    try:
        data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            merged = json.loads(json.dumps(_DEFAULT))
            merged.update(data)
            if not isinstance(merged.get("close_confirmation_waived"), dict):
                merged["close_confirmation_waived"] = {}
            if not isinstance(merged.get("behavior_rules"), list):
                merged["behavior_rules"] = []
            if not isinstance(merged.get("post_action_minimize"), dict):
                merged["post_action_minimize"] = {}
            if not isinstance(merged.get("auto_memory"), bool):
                merged["auto_memory"] = True
            if not isinstance(merged.get("file_creation_confirmation_waived"), bool):
                merged["file_creation_confirmation_waived"] = False
            if not isinstance(merged.get("risk_confirmation_waived"), dict):
                merged["risk_confirmation_waived"] = {}
            if not isinstance(merged.get("voice_lock_enabled"), bool):
                merged["voice_lock_enabled"] = True
            if not isinstance(merged.get("mic_muted"), bool):
                merged["mic_muted"] = False
            if not isinstance(merged.get("voice_output_device"), dict):
                merged["voice_output_device"] = None
            return merged
    except Exception:
        pass
    return json.loads(json.dumps(_DEFAULT))


def load_prefs() -> dict[str, Any]:
    with _LOCK:
        return _load_unlocked()


def _save(data: dict[str, Any]) -> None:
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    safe_write_json(PREFS_PATH, data)


# ── Re-exports: public API unchanged for every external call site ────────
# Import order matters: feedback_promotion and prompt_format call setters/
# getters from the other submodules, so those must be imported first.

from core.user_prefs.auto_memory import (  # noqa: E402
    is_auto_memory_enabled,
    set_auto_memory,
)
from core.user_prefs.behavior_rules import (  # noqa: E402
    add_behavior_rule,
    get_behavior_rules,
    remove_behavior_rule,
)
from core.user_prefs.close_waiver import (  # noqa: E402
    is_close_confirmation_waived,
    list_close_waivers,
    set_close_confirmation_waiver,
)
from core.user_prefs.feedback_promotion import (  # noqa: E402
    is_standing_instruction,
    promote_feedback_to_prefs,
)
from core.user_prefs.file_creation_waiver import (  # noqa: E402
    is_file_creation_confirmation_waived,
    set_file_creation_confirmation_waiver,
)
from core.user_prefs.minimize_rule import (  # noqa: E402
    is_post_action_minimize,
    list_post_action_minimize,
    set_post_action_minimize,
)
from core.user_prefs.prompt_format import format_prefs_for_prompt  # noqa: E402
from core.user_prefs.risk_waiver import (  # noqa: E402
    is_risk_confirmation_waived,
    list_risk_waivers,
    set_risk_confirmation_waiver,
)


def get_voice_lock_enabled() -> bool:
    return load_prefs().get("voice_lock_enabled", True)


def set_voice_lock_enabled(value: bool) -> None:
    data = load_prefs()
    data["voice_lock_enabled"] = bool(value)
    _save(data)


__all__ = [
    "PREFS_PATH",
    "load_prefs",
    "set_close_confirmation_waiver",
    "is_close_confirmation_waived",
    "list_close_waivers",
    "set_file_creation_confirmation_waiver",
    "is_file_creation_confirmation_waived",
    "set_post_action_minimize",
    "is_post_action_minimize",
    "list_post_action_minimize",
    "set_auto_memory",
    "is_auto_memory_enabled",
    "add_behavior_rule",
    "remove_behavior_rule",
    "get_behavior_rules",
    "is_standing_instruction",
    "promote_feedback_to_prefs",
    "format_prefs_for_prompt",
    "set_risk_confirmation_waiver",
    "is_risk_confirmation_waived",
    "list_risk_waivers",
    "get_voice_lock_enabled",
    "set_voice_lock_enabled",
]


def get_mic_muted() -> bool:
    return bool(load_prefs().get("mic_muted", False))


def set_mic_muted(value: bool) -> None:
    with _LOCK:
        data = _load_unlocked()
        data["mic_muted"] = bool(value)
        _save(data)
