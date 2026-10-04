"""crew/hermes_transport.py — the crew's transport onto the Hermes Agent gateway (Mode A).

Hermes is the BRAIN and Era is the GATE. A crew turn is still run by Era's own turn loop
(crew/runtime.py): the model's reply names a capability, Era checks the bot was granted it,
Era runs the one implementation in crew/capabilities.py (vault bridge, Nabar, the staged
replace that only the user's own "yes" can apply). The only thing this transport changes is
WHERE the model call goes: text in, text out, to the real gateway

    http://127.0.0.1:8642/p/era_one      (profile `era_one`; never the default profile)

so Hermes never touches the vault, the filesystem or a shell, and cannot bypass a gate it
cannot reach. That is only true while the profile exposes NO tools, so:

  STARTUP / DRIFT CHECK  before the first turn, and again every RECHECK_S, `GET /v1/toolsets`
                         must answer, authenticated, and report NOTHING enabled. A tool
                         enabled, an unreadable answer, a 401 or an unreachable gateway all
                         REFUSE to run (fail closed): the config closes the hole today, this
                         closes drift (a Hermes update, a setting changed by hand).

  THE KEY                `hermes_era_one_api_key` in config/api_keys.json, read at CALL time
                         (never cached, so rotating it needs no restart). Missing, empty or
                         "placeholder" is a typed `MissingCredential` and nothing is sent. It
                         is sent ONLY as `Authorization: Bearer` and never logged or returned.
                         It is its own key: never the default profile's.

  LOOPBACK ONLY          a base URL that is not 127.0.0.1 / localhost / ::1 is refused at
                         construction, so a mis-edit can never ship the key anywhere else.

Implements crew/transport.py's existing interface and no new call shape. Not the default:
crew/runtime.get_transport selects it only when ERA_CREW_TRANSPORT=hermes_agent.

This module must not import lane_router / lane_dispatch (see crew/transport.py).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.logger import log_event
from core.tool_result import ToolResult
from core.utils import CONFIG_PATH
from crew import router as crew_router
from crew.nabar import parse_json_object
from crew.runtime import InProcessTransport, runtime_logger
from crew.transport import BotSpec, HealthStatus, Turn, fail

DEFAULT_BASE = "http://127.0.0.1:8642/p/era_one"
KEY_ENTRY = "hermes_era_one_api_key"
RECHECK_S = 300.0
CHAT_TIMEOUT_S = 90.0
PROBE_TIMEOUT_S = 5.0
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

# http(method, url, headers, body_or_None, timeout) -> (status, parsed_json_or_None)
Http = Callable[[str, str, dict, "bytes | None", float], "tuple[int, Any]"]


class HermesKeyMissing(Exception):
    """The era_one key is not in config/api_keys.json (the message never holds a key)."""


def read_key(config_path: Path | None = None) -> str:
    """The era_one key, read from disk NOW. Raises HermesKeyMissing without echoing anything."""
    try:
        data = json.loads(Path(config_path or CONFIG_PATH).read_text(encoding="utf-8-sig"))
        value = data.get(KEY_ENTRY) if isinstance(data, dict) else None
    except (OSError, ValueError):
        value = None
    key = value.strip() if isinstance(value, str) else ""
    if not key or key.lower() == "placeholder":
        raise HermesKeyMissing(f"{KEY_ENTRY} is not set in config/api_keys.json")
    return key


def _urllib_http(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return status, None


def _check_loopback(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in _LOOPBACK:
        raise ValueError(f"the Hermes gateway must be on loopback, got {base_url!r}")
    return base_url.rstrip("/")


class _Refused(Exception):
    """Carries (error_type, detail) out of a check."""

    def __init__(self, error_type: str, detail: str):
        super().__init__(detail)
        self.error_type, self.detail = error_type, detail


class HermesAgentTransport(InProcessTransport):
    name = "hermes_agent"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        *,
        config_path: Path | None = None,
        http: Http | None = None,
        clock: Callable[[], float] = time.monotonic,
        recheck_s: float = RECHECK_S,
    ) -> None:
        # An injected `llm` bypasses the router and the caches: Hermes IS the brain here.
        super().__init__(llm=self._never)
        self._base = _check_loopback(base_url)
        self._root = f"{urlsplit(self._base).scheme}://{urlsplit(self._base).netloc}"
        self._config_path = config_path
        self._http: Http = http or _urllib_http
        self._clock = clock
        self._recheck_s = float(recheck_s)
        self._toolsets_ok_until = 0.0

    @staticmethod
    def _never(system: str, user: str, tier: str) -> dict:  # pragma: no cover - replaced by _ask
        return {}

    # ── the gate ────────────────────────────────────────────────────────────
    def _headers(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _gateway_check(self) -> str:
        """Key present, gateway reachable, NOTHING enabled. Returns the key; raises _Refused."""
        try:
            key = read_key(self._config_path)
        except HermesKeyMissing as exc:
            raise _Refused("MissingCredential", str(exc)) from None
        if self._clock() < self._toolsets_ok_until:
            return key
        try:
            status, body = self._http(
                "GET", f"{self._base}/v1/toolsets", self._headers(key), None, PROBE_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001 - unreachable gateway, timeout, refused
            raise _Refused(
                "TransportDown", f"the Hermes gateway is unreachable ({type(exc).__name__})"
            ) from None
        if status == 401:
            raise _Refused("TransportDown", "Hermes rejected the era_one key (HTTP 401)")
        if status == 404:
            raise _Refused("TransportDown", "Hermes does not serve the era_one profile (HTTP 404)")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise _Refused("TransportDown", f"Hermes /v1/toolsets was unreadable (HTTP {status})")
        if not body["data"]:
            raise _Refused(
                "TransportDown", "Hermes reported no toolsets at all, so none can be verified off"
            )
        enabled = sorted(
            str(t.get("name")) for t in body["data"] if isinstance(t, dict) and t.get("enabled")
        )
        if enabled:
            raise _Refused(
                "TransportDown",
                "refusing to run: the era_one profile has tools enabled ("
                + ", ".join(enabled[:8])
                + "). With tools on, Hermes could reach the vault, files or a shell around "
                "Era's gates. Disable them in the profile's config.",
            )
        self._toolsets_ok_until = self._clock() + self._recheck_s
        return key

    # ── contract: dispatch / health (register + capabilities are inherited) ──
    def dispatch(self, bot_id: str, turn: Turn) -> ToolResult:
        try:
            self._gateway_check()
        except _Refused as refusal:
            log_event(
                runtime_logger,
                "crew.hermes.refused",
                "WARNING",
                error_type=refusal.error_type,
                detail=refusal.detail,
            )
            return fail(refusal.error_type, refusal.detail)
        return super().dispatch(bot_id, turn)

    def health(self) -> HealthStatus:
        n = len(self._bots)
        try:
            status, _ = self._http("GET", f"{self._root}/health", {}, None, PROBE_TIMEOUT_S)
            if status != 200:
                return HealthStatus(False, self.name, f"Hermes /health answered HTTP {status}", n)
            self._gateway_check()
        except _Refused as refusal:
            return HealthStatus(False, self.name, refusal.detail, n)
        except Exception as exc:  # noqa: BLE001 - health never raises
            return HealthStatus(False, self.name, f"Hermes unreachable ({type(exc).__name__})", n)
        if n == 0:
            return HealthStatus(False, self.name, "no bots registered", 0)
        return HealthStatus(True, self.name, f"{n} bots registered; era_one has no tools", n)

    # ── the model call: text in, text out ───────────────────────────────────
    def _ask(self, spec: BotSpec, turn: Turn, system: str, user: str) -> dict:
        tier = crew_router.tier_for(crew_router.classify(spec, turn, system, user))
        try:
            key = read_key(self._config_path)  # again, at call time: a turn has many calls
            status, body = self._http(
                "POST",
                f"{self._base}/v1/chat/completions",
                self._headers(key),
                json.dumps(
                    {
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "stream": False,
                    }
                ).encode("utf-8"),
                CHAT_TIMEOUT_S,
            )
        except HermesKeyMissing as exc:
            crew_router.log_turn(spec.id, tier, "hermes", 0, 0, False)
            return {"__unavailable__": str(exc)}
        except Exception as exc:  # noqa: BLE001
            self._toolsets_ok_until = 0.0  # re-verify before trusting it again
            crew_router.log_turn(spec.id, tier, "hermes", 0, 0, False)
            return {"__unavailable__": f"the Hermes gateway failed ({type(exc).__name__})"}
        if status != 200 or not isinstance(body, dict):
            self._toolsets_ok_until = 0.0
            crew_router.log_turn(spec.id, tier, "hermes", 0, 0, False)
            return {"__unavailable__": f"Hermes answered HTTP {status}"}
        try:
            text = str(body["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            text = ""
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        tin = int(usage.get("prompt_tokens") or crew_router.estimate_tokens(system + user))
        tout = int(usage.get("completion_tokens") or crew_router.estimate_tokens(text))
        crew_router.log_turn(spec.id, tier, "hermes", tin, tout, False)
        obj = parse_json_object(text)
        return obj if obj else {"__bad_output__": text[:200]}
