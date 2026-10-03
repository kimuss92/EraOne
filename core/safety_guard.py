# core/safety_guard.py
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from core.utils import is_windows_system_path

if TYPE_CHECKING:
    from core.vision_types import VisionResult

SAFE = "SAFE"
LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"
BLOCKED = "BLOCKED"

_DANGER_PATHS: list[Path] = []
if sys.platform == "win32":
    _DANGER_PATHS = [
        Path("C:\\Windows").resolve(),
        Path("C:\\Program Files").resolve(),
        Path("C:\\Program Files (x86)").resolve(),
    ]
    for env_var in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA"):
        p = os.environ.get(env_var)
        if p:
            try:
                _DANGER_PATHS.append(Path(p).resolve())
            except Exception:
                pass
else:
    _DANGER_PATHS = [
        Path("/bin"),
        Path("/sbin"),
        Path("/usr"),
        Path("/etc"),
        Path("/var"),
        Path("/System"),
        Path("/Library"),
    ]

_SENSITIVE_KEYWORDS = ("payment", "submit", "login", "password", "buy", "purchase")

# Powering the machine off/down. Gated tool-agnostically (see classify_action):
# whichever tool the model routes through, these always require confirmation —
# the machine going down is not something to get wrong on a mis-heard word.
_POWER_ACTIONS = frozenset(
    {
        "shutdown",
        "shut_down",
        "shutdown_computer",
        "power_off",
        "poweroff",
        "restart",
        "reboot",
        "restart_computer",
        "log_off",
        "logoff",
        "sign_out",
        "hibernate",
        "sleep",
        "suspend",
    }
)
# Tools that control a media player and nothing else. They cannot change the
# machine's power state, so a player-semantic "restart" (start the episode
# over) must not be read as a reboot. Keep this list to tools whose dispatcher
# is confined to its own action vocabulary.
_MEDIA_PLAYER_TOOLS = frozenset(
    {
        "netflix_control",
        "youtube_video",
        "spotify_control",
        "media_coordinator",
        "premiere_control",
    }
)

# Tools whose dispatcher can really power the machine off. A HIGH-risk category
# waiver (core/user_prefs/risk_waiver.py) applies to these only — the power GATE
# stays tool-agnostic, but gmail_control(action="sign_out") must never inherit a
# power waiver by name collision.
_POWER_WAIVER_TOOLS = frozenset({"computer_settings", "computer_control"})

# computer_settings resolves an EMPTY action from free-text `description` with an LLM
# AFTER this gate has run, so a description naming a power change is gated here.
_POWER_DESCRIPTION_RE = re.compile(
    r"\b(?:shut\s*down|power\s*off|turn\s+off\s+the\s+(?:pc|computer|laptop|machine)"
    r"|restart|reboot|log\s*off|sign\s*out|hibernate|[ée]tein\w*|red[ée]marr\w*)\b",
    re.IGNORECASE,
)

# Cancelling a pending shutdown is the safe direction — never gated.
_POWER_ABORT_ACTIONS = frozenset(
    {
        "cancel_shutdown",
        "abort_shutdown",
        "cancel_restart",
    }
)
_DANGEROUS_HOTKEYS = (
    "ctrl+w",
    "alt+f4",
    "ctrl+shift+esc",
    "ctrl+alt+del",
    "win+d",
    "win+r",
    "ctrl+alt+delete",
)


def _is_protected_path(target: str) -> bool:
    if not target:
        return False
    try:
        if is_windows_system_path(target, include_app_data=True):
            return True
        resolved = Path(target).resolve()
        for danger in _DANGER_PATHS:
            try:
                if resolved == danger or danger in resolved.parents:
                    return True
            except Exception:
                if str(resolved).lower().startswith(str(danger).lower()):
                    return True
    except Exception:
        pass
    return False


def _has_sensitive_keywords(params: dict) -> bool:
    for field in ("text", "description", "query", "url", "value"):
        val = params.get(field, "")
        if any(kw in val.lower() for kw in _SENSITIVE_KEYWORDS):
            return True
    return False


def _user_waived_close_confirmation(params: dict) -> bool:
    """True when the user granted a persistent 'close without confirmation'
    exception (core.user_prefs) for the app targeted by this close action.

    Fails closed: any error means no waiver, so the normal confirmation
    gate stays in force.
    """
    try:
        from core.user_prefs import is_close_confirmation_waived

        # Only identity-ish params — free-content fields like "value" are
        # excluded because their text can be influenced by page content.
        for field in ("name", "title", "app", "target"):
            target = params.get(field)
            if target and is_close_confirmation_waived(str(target)):
                return True
    except Exception:
        pass
    return False


def _user_waived_file_creation_confirmation() -> bool:
    """True when the user granted a persistent 'create files without
    confirmation' exception (core.user_prefs). Global, not per-path.

    Fails closed: any error means no waiver, so the normal confirmation
    gate stays in force.
    """
    try:
        from core.user_prefs import is_file_creation_confirmation_waived

        return is_file_creation_confirmation_waived()
    except Exception:
        return False


def _user_waived_risk(action: str) -> str:
    """The waived category covering `action` (core.user_prefs.risk_waiver), or ''.

    Per category only — there is no global HIGH-risk switch. Fails closed.
    """
    try:
        from core.user_prefs.risk_waiver import category_for_action, is_risk_confirmation_waived

        category = category_for_action(action)
        return category if category and is_risk_confirmation_waived(category) else ""
    except Exception:
        return ""


def _desktop_reaches_planner(action: str, params: dict) -> bool:
    """True when `desktop_control` would hand this call to its LLM planner.

    Mirrors the dispatch chain in `actions/desktop.py`: a directly-handled
    action runs a fixed function, and everything else that carries an action or
    a task becomes a generated plan. Fails CLOSED — if the action list cannot be
    imported, the call is treated as a planner call and confirmed.
    """
    try:
        from actions.desktop_ops import DIRECT_ACTIONS, WINDOW_ACTIONS
    except Exception:
        DIRECT_ACTIONS = WINDOW_ACTIONS = frozenset()
    if action in DIRECT_ACTIONS:
        return False
    task = str(params.get("task") or params.get("description") or "").strip()
    # A task string is planned WHATEVER the action is. This is the case that
    # bit: `{"action": "minimize", "task": "<attacker text>"}` matched the
    # window branch below, returned LOW, and then ran the plan anyway.
    if task:
        return True
    if action in WINDOW_ACTIONS:
        return False  # redirected by the dispatcher, never planned
    return bool(action)


def classify_action(
    tool: str, parameters: dict | None = None, *, honor_risk_waivers: bool = False
) -> dict:
    """`honor_risk_waivers`: apply the user's per-category HIGH-risk waivers. ONLY the
    live dispatcher sets it, and only for a call the router did not rewrite; the
    scheduler and the autonomous agent never do — a waiver is the user's standing
    "don't ask ME", not permission for an unattended run to power the machine off."""
    params = parameters or {}
    # str() before .lower(): a non-string `action` (a model can emit {"action": 5})
    # otherwise raised AttributeError out of the gate itself. That failed closed —
    # the tool call aborted — but as a stack trace rather than a refusal.
    action = str(params.get("action") or "").lower().strip()
    # computer_settings maps spaces/hyphens to "_" before dispatch; the power gate
    # must see the same name, or "shut down" / "Shut-Down" fell to default LOW.
    power_action = action.replace(" ", "_").replace("-", "_")
    path = params.get("path", "") or ""
    target = params.get("name", "") or path

    # --- Machine power state: confirm, whatever tool asked for it ---
    # Checked FIRST and independently of `tool`, so a shutdown routed through
    # computer_control (or any future tool) can never fall through to the
    # default-LOW branch and take the machine down unconfirmed. Era shutting
    # HERSELF down (shutdown_era) is a different, cheap, reversible thing and
    # is handled separately below.
    # Media players are exempt: "restart" means restart the EPISODE, "sleep"
    # could mean a sleep timer, and none of these tools can touch the machine's
    # power state — their dispatchers only handle their own action list and
    # reject anything else. Without this, netflix_control(action="restart")
    # was classified "Machine power state change: restart" and Era asked the
    # user to confirm rebooting the computer when they wanted the episode
    # started over. The guard itself stays tool-agnostic for every tool that
    # CAN power the machine down, which is the direction it exists to catch.
    if tool not in {"shutdown_era", "close_era", "exit_era"} and tool not in _MEDIA_PLAYER_TOOLS:
        if power_action in _POWER_ABORT_ACTIONS:
            return {
                "risk_level": LOW,
                "requires_confirmation": False,
                "reason": f"Cancelling a pending power action: {action}",
            }
        if power_action in _POWER_ACTIONS:
            if (
                honor_risk_waivers
                and tool in _POWER_WAIVER_TOOLS
                and _user_waived_risk(power_action)
            ):
                # Still HIGH; only the prompt is waived. The 20 s shutdown countdown
                # and cancel_shutdown remain.
                return {
                    "risk_level": HIGH,
                    "requires_confirmation": False,
                    "reason": f"Machine power state change: {action} "
                    "(user waived confirmation for this category)",
                }
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": f"Machine power state change: {action}",
            }
        if (
            tool == "computer_settings"
            and not action
            and _POWER_DESCRIPTION_RE.search(str(params.get("description") or ""))
        ):
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": "Possible machine power change described in free text.",
            }

    # --- LOW risk: confirmation management tools ---
    if tool in {"confirm_pending_confirmation", "cancel_pending_confirmation"}:
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": "Confirmation management action.",
        }

    if tool == "manage_preference":
        # GRANTING a waiver weakens a confirmation gate persistently, so the
        # grant itself must be confirmed once (prompt-injected content cannot
        # self-confirm — see executor confirmation_id flow). Revoking a waiver
        # or managing behavior rules is the safe direction and stays LOW.
        if action == "waive_close_confirmation":
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": True,
                "reason": "Granting a standing close-without-confirmation exception.",
            }
        if action == "waive_file_creation_confirmation":
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": True,
                "reason": "Granting a standing file-creation-without-confirmation exception.",
            }
        if action == "waive_high_risk_confirmation":
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": True,
                "reason": "Granting a standing high-risk-without-confirmation exception.",
            }
        if action == "enable_auto_memory":
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": True,
                "reason": "Re-enabling self-initiated memory saving.",
            }
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": "User-granted standing preference management — reversible.",
        }

    if tool == "record_feedback":
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": "Append-only feedback recording — non-destructive.",
        }

    if tool == "retrieve_memory_context":
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": "Read-only memory retrieval.",
        }

    if tool == "list_memory_keys":
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": "Read-only memory key listing.",
        }

    if tool == "forget_memory":
        return {
            "risk_level": HIGH,
            "requires_confirmation": True,
            "reason": "Deleting memory requires confirmation.",
        }

    # --- BLOCKED: protected system folders ---
    # For `open` action (read-only), allow AppData, LocalAppData, ProgramData
    if tool == "file_controller":
        dest = params.get("destination", "") or ""
        if action != "open":
            if any(_is_protected_path(p) for p in (path, dest, target) if p):
                return {
                    "risk_level": BLOCKED,
                    "requires_confirmation": False,
                    "reason": "Target path is a protected system location.",
                }
        else:
            # For `open`, only block Windows and Program Files (allow AppData etc.)
            _hard_blocked = [
                Path("C:\\Windows").resolve(),
                Path("C:\\Program Files").resolve(),
                Path("C:\\Program Files (x86)").resolve(),
            ]
            for p_str in (path, dest, target):
                if not p_str:
                    continue
                try:
                    resolved = Path(p_str).resolve()
                    for danger in _hard_blocked:
                        try:
                            if resolved == danger or danger in resolved.parents:
                                return {
                                    "risk_level": BLOCKED,
                                    "requires_confirmation": False,
                                    "reason": "Target path is a protected system location.",
                                }
                        except Exception:
                            if str(resolved).lower().startswith(str(danger).lower()):
                                return {
                                    "risk_level": BLOCKED,
                                    "requires_confirmation": False,
                                    "reason": "Target path is a protected system location.",
                                }
                except Exception:
                    pass

    # --- HIGH risk + requires confirmation ---
    if tool == "file_controller" and action in ("delete", "remove", "overwrite", "trash"):
        return {
            "risk_level": HIGH,
            "requires_confirmation": True,
            "reason": f"Destructive file action: {action}",
        }

    if tool == "file_controller" and action in ("move", "rename"):
        if _is_protected_path(path) or _is_protected_path(params.get("destination", "")):
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": "Moving/renaming inside protected path.",
            }
        return {
            "risk_level": MEDIUM,
            "requires_confirmation": True,
            "reason": f"File move/rename: {action}",
        }

    if tool == "send_message":
        return {
            "risk_level": HIGH,
            "requires_confirmation": True,
            "reason": "Sending a message requires confirmation.",
        }

    # --- Gmail: risk is per-action, not per-tool ---
    # Reading mail is the common case and gating it would make the feature
    # useless by friction. Anything that leaves the machine (send, reply) or
    # changes who Era can act as (authorize, revoke) is confirmed. Classified
    # BEFORE the generic communication-tool branch below so the read actions
    # are not swept up as HIGH.
    if tool == "gmail_control":
        if action in ("send", "compose", "send_email", "reply", "forward"):
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": f"Sending email leaves this machine: {action}",
            }
        if action in ("authorize", "connect", "auth", "login"):
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": True,
                "reason": "Granting Era access to the Gmail account.",
            }
        if action in ("revoke", "disconnect", "logout", "sign_out"):
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": True,
                "reason": "Disconnecting the Gmail account.",
            }
        # Read, search, criteria and watcher control: reversible and local.
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Read-only or local Gmail action: {action or 'status'}",
        }

    # --- Claude Code bridge ---
    # Claude Code has tools of its own and may run in an auto-approve mode, so a
    # message sent there is an instruction to an agent that can act on this
    # machine OUTSIDE Era's gates. A prompt-injected "tell Claude to delete X"
    # must reach the user first. Reading the reply is harmless.
    if tool == "claude_bridge":
        if action in ("ask", "reply"):
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": True,
                "reason": "Sending instructions to Claude Code, which can act on this machine.",
            }
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Reading the Claude Code conversation: {action or 'status'}",
        }

    if tool in {"shutdown_era", "close_era", "exit_era"}:
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": "Era One app shutdown requested.",
        }

    if tool in ("email_controller", "whatsapp_controller", "submit", "post"):
        return {
            "risk_level": HIGH,
            "requires_confirmation": True,
            "reason": f"Communication tool: {tool}",
        }

    # shutdown/restart/sleep are already handled by the tool-agnostic power
    # check at the top of this function; what remains here is lock/settings.
    if tool == "computer_settings" and action in ("lock", "change_setting"):
        return {
            "risk_level": HIGH,
            "requires_confirmation": True,
            "reason": f"System setting change: {action}",
        }

    if tool == "computer_control":
        if action in ("kill", "kill_process", "close_app"):
            # Waivers only ever cover graceful close — force-kill always
            # keeps its confirmation gate (kill can't be waived by feedback).
            if action == "close_app" and _user_waived_close_confirmation(params):
                return {
                    "risk_level": LOW,
                    "requires_confirmation": False,
                    "reason": "User granted a standing exception: close this app without confirmation.",
                }
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": f"Process termination: {action}",
            }
        if action == "hotkey":
            keys = (params.get("keys") or "").lower()
            if any(dk in keys for dk in _DANGEROUS_HOTKEYS):
                return {
                    "risk_level": HIGH,
                    "requires_confirmation": True,
                    "reason": f"Dangerous hotkey combination: {keys}",
                }

    if tool == "browser_control" and action == "click_ref":
        # Params carry only a number; the page's own label (recorded by
        # page_snapshot) is what says whether this click pays, sends or deletes.
        from actions.page_refs import is_sensitive_label, label_for_ref

        label = label_for_ref(params.get("ref", params.get("index")))
        if label is None:
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": True,
                "reason": "Click on an element not in the last page snapshot.",
            }
        if is_sensitive_label(label):
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": f"Browser click on {label}.",
            }
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Browser click on {label}.",
        }

    if tool == "browser_control" and action == "smart_click":
        # Was unclassified (LOW) before 2026-09-26, so "Buy now" clicked unasked.
        # The element's real label is re-checked at run time (page_refs.hold_if_sensitive).
        from actions.page_refs import is_sensitive_label

        wanted = str(params.get("description") or params.get("text") or "")
        if is_sensitive_label(wanted):
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": f"Browser click on '{wanted[:80]}'.",
            }
        # Otherwise fall through: the vision escalation below still applies.

    if tool == "browser_control" and action in ("fill_form", "type", "click"):
        if _has_sensitive_keywords(params):
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": "Browser action on sensitive form field.",
            }

    # --- MEDIUM risk ---
    if tool == "file_controller" and action in ("write", "create_file", "create_folder"):
        if _user_waived_file_creation_confirmation():
            return {
                "risk_level": LOW,
                "requires_confirmation": False,
                "reason": "User granted a standing exception: create files without confirmation.",
            }
        return {
            "risk_level": MEDIUM,
            "requires_confirmation": True,
            "reason": f"File modification: {action}",
        }

    if tool == "file_controller" and action == "copy":
        return {
            "risk_level": MEDIUM,
            "requires_confirmation": True,
            "reason": f"File modification: {action}",
        }

    if tool == "browser_control" and action in ("type", "fill_form", "click"):
        return {
            "risk_level": MEDIUM,
            "requires_confirmation": True,
            "reason": f"Browser interaction: {action}",
        }

    # `desktop_control` hands anything outside its directly-handled actions to an
    # LLM planner, which then runs real file operations (move/copy/make folder)
    # in the user's media folders. This is checked BEFORE the window and close
    # branches below: both match on `action` alone and return early, so
    # `{"action": "minimize", "task": "<anything>"}` was classified LOW and then
    # planned anyway. A close waiver must not authorise a file plan either.
    if tool == "desktop_control" and _desktop_reaches_planner(action, params):
        return {
            "risk_level": HIGH,
            "requires_confirmation": True,
            "reason": "Autonomous desktop task runs a generated plan — confirm first.",
        }

    # Routine, non-destructive window control (focus/switch/min/max/etc.) must
    # never prompt — it is the most common voice action and gating it is pure
    # friction. Only `close` can lose unsaved work, so that stays gated below.
    if tool in ("desktop_control", "window_control") and action in (
        "focus",
        "activate",
        "switch",
        "switch_to",
        "select",
        "minimize",
        "maximize",
        "restore",
        "show",
        "raise",
        "bring_to_front",
        "move_to_monitor",
        "monitors",
    ):
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Routine window focus/management: {action}",
        }

    if tool in ("desktop_control", "window_control") and action == "close":
        if _user_waived_close_confirmation(params):
            return {
                "risk_level": LOW,
                "requires_confirmation": False,
                "reason": "User granted a standing exception: close this app without confirmation.",
            }
        # Blanket per-category close waiver (core/user_prefs/risk_waiver.py "close").
        # Honoured ONLY for the live user's own un-rewritten call, like the power waiver —
        # never for a router-rewrite, a scheduled task, or the autonomous agent. Still
        # MEDIUM: this only drops the prompt, and a deliberate downgrade the owner asked
        # for — close can lose unsaved work, so the residual risk stays on the record.
        if honor_risk_waivers and _user_waived_risk(action):
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": False,
                "reason": f"Window/desktop close: {action} "
                "(user waived confirmation for this category)",
            }
        return {
            "risk_level": MEDIUM,
            "requires_confirmation": True,
            "reason": f"Window/desktop close: {action}",
        }

    if tool in {"visual_engine", "autonomous_development"}:
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": "Declarative HUD update or staged development; activation needs final HUD approval.",
        }
    if tool == "run_feature":
        return {
            "risk_level": HIGH,
            "requires_confirmation": True,
            "reason": "Execute user-approved custom feature code.",
        }

    # --- Protocol One (actions/protocol_one.py) ---
    # status/briefing only read runtime/hive. launch edits Era's code, but only
    # through self_improve's backup/verify/rollback pipeline, never the safety
    # layer, and the dispatcher refuses it unless the USER said "Launch Protocol
    # One" — the spoken command is the confirmation.
    if tool == "protocol_one":
        # launch (run a cycle) and confirm (apply the fixes it staged) both edit
        # Era's code, only through self_improve's backup/verify/rollback pipeline
        # and never the safety layer. Both are gated in the dispatcher on the
        # user's own words — "Launch Protocol One" / a spoken "yes" — so the
        # spoken command is the confirmation; a second prompt here is redundant.
        if str(params.get("action") or "status").lower() in ("launch", "confirm"):
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": False,
                "reason": "Protocol One maintenance cycle (explicit user command).",
            }
        return {"risk_level": LOW, "requires_confirmation": False, "reason": "Hive status read."}

    # --- Protocol Zero (actions/protocol_zero.py) ---
    # execute rewrites ONLY the marked auto-synced block of core/prompt.txt, with
    # a backup, from code-authored tool declarations (no model text). Gated on the
    # user saying "Launch Protocol Zero" in the dispatcher, like Protocol One.
    if tool == "protocol_zero":
        if str(params.get("action") or "status").lower() == "execute":
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": False,
                "reason": "Protocol Zero prompt sync (explicit user command).",
            }
        return {"risk_level": LOW, "requires_confirmation": False, "reason": "Hive status read."}

    # --- Scheduled tasks (actions/scheduled_task.py, core/task_scheduler.py) ---
    # Managing a schedule touches nothing on the machine: the task's OWN action is
    # re-classified by this function when it is due, and parks for the user's
    # approval if it needs confirmation. Approving that parked run and waiving a
    # task's confirmation are gated in the dispatcher on the user's own words
    # (like protocol_one launch), so the spoken instruction IS the confirmation —
    # a second prompt here would contradict the user's explicit "don't ask".
    if tool == "scheduled_task":
        if action in ("waive_confirmation", "waive"):
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": False,
                "reason": "Per-task confirmation waiver (explicit user command).",
            }
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": "Schedule management; the task's action is gated when it runs.",
        }

    # --- Autonomous self-healing (core/self_heal.py) ---
    # status/scan are read-only. heal/rollback reuse self_improve's pipeline and
    # self_heal refuses SAFETY_CRITICAL targets outright, so MEDIUM unconfirmed
    # like self_improve apply. Switching the unattended loop ON (auto/propose)
    # widens what Era does with nobody watching — the user confirms that.
    # Switching it OFF only ever narrows it.
    if tool == "self_heal":
        if action in ("status", "scan", ""):
            return {
                "risk_level": LOW,
                "requires_confirmation": False,
                "reason": "Read-only self-heal report.",
            }
        if action == "mode" and str(params.get("mode") or "").lower().strip() != "off":
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": "Enabling unattended self-modification — confirm first.",
            }
        return {
            "risk_level": MEDIUM,
            "requires_confirmation": False,
            "reason": "Scoped self-heal with backup, verification, probation and rollback.",
        }

    # --- Self-modification (actions/self_improve.py) ---
    # propose/list_backups are read-only. apply has its own backup + verify +
    # auto-rollback pipeline, so ordinary files run unconfirmed (that autonomy
    # is the point of the tool) — but the safety layer, the entry point, and
    # the supervisor are exactly what the pipeline cannot protect Era from
    # breaking, so touching those must be confirmed by the user out loud.
    if tool == "self_improve":
        if action in ("propose", "list_backups", "find", ""):
            return {
                "risk_level": LOW,
                "requires_confirmation": False,
                "reason": "Read-only self-improvement proposal.",
            }
        try:
            from actions.self_improve import SAFETY_CRITICAL, _rel, resolve_request

            target, status, _msg = resolve_request(
                params.get("file_path", "") or "", params.get("description", "") or ""
            )
            if status != "ok":
                # resolve_request guarantees the action writes NOTHING unless
                # status is ok — it just answers with the refusal/candidate
                # list, so gating it behind a confirmation would be noise.
                return {
                    "risk_level": LOW,
                    "requires_confirmation": False,
                    "reason": f"Self-improve target {status} — action will only report, not write.",
                }
            critical = _rel(target) in SAFETY_CRITICAL
        except Exception:
            critical = True  # fail closed: unknown target counts as critical
        if action == "rollback":
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": False,
                "reason": "Restoring own code from a self-improve backup.",
            }
        if critical:
            return {
                "risk_level": HIGH,
                "requires_confirmation": True,
                "reason": "Self-modification of a safety-critical file — confirm first.",
            }
        return {
            "risk_level": MEDIUM,
            "requires_confirmation": False,
            "reason": "Self-modification with backup, verification, and auto-rollback.",
        }

    if tool == "generated_code":
        return {
            "risk_level": MEDIUM,
            "requires_confirmation": True,
            "reason": "Generated code execution.",
        }

    # --- Vision-augmented risk escalation ---
    # For mutation actions that passed static rules, consult the live vision
    # context to detect screen-level danger (payment forms, destructive UIs,
    # etc.).  Only escalates — never downgrades a BLOCKED or HIGH already set
    # by static rules above.
    _VISION_MUTATION_TOOLS: set[str] = {"computer_control", "browser_control"}
    _VISION_MUTATION_ACTIONS: set[str] = {
        "click",
        "type",
        "press",
        "fill_form",
        "smart_click",
        "hotkey",
    }
    if tool in _VISION_MUTATION_TOOLS and action in _VISION_MUTATION_ACTIONS:
        try:
            from core.vision_context import get_latest_vision_result

            vision_ctx = get_latest_vision_result(max_age_seconds=7.0)
            if vision_ctx is not None:
                _request_id, vision_result = vision_ctx
                vision_safety = classify_vision_risk(
                    vision_result,
                    {"tool": tool, "action": action},
                )
                if not vision_safety.get("allow_action", True):
                    return {
                        "risk_level": HIGH,
                        "requires_confirmation": True,
                        "reason": vision_safety.get(
                            "reason",
                            "Vision analysis blocked this mutation action.",
                        ),
                    }
        except Exception:
            # Vision context unavailable or classification failed —
            # fall through to standard static rules below.
            pass

    # --- LOW risk ---
    LOW_TOOLS = {
        "open_app",
        "web_search",
        "weather_report",
        "youtube_video",
        "flight_finder",
        "game_updater",
        "screen_process",
        "reminder",
        "code_helper",
        "dev_agent",
        "file_processor",
        "save_memory",
        # Writes a markdown note into the user's own vault. Never deletes,
        # never overwrites a whole note, no external effect.
        "vault_note",
        "agent_task",
        "enter_standby",
        # Mutes/unmutes Era's own mic input. Reversible, no machine effect.
        "set_microphone",
        "premiere_control",
        # Generates new media files under runtime/designs only; no machine control.
        "design_studio",
        # Read-only self-inspection; writes only a report under runtime/diagnosis.
        "self_diagnosis",
        # Reversible performance-profile switch / memory cleanup via vendor apps.
        "system_performance",
    }
    if tool in LOW_TOOLS:
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Standard tool: {tool}",
        }

    LOW_BROWSER_ACTIONS = {
        "search",
        "go_to",
        "get_text",
        "get_url",
        "list_tabs",
        "active_tab",
        "switch_tab",
        "focus_tab",
        "next_tab",
        "prev_tab",
        "new_tab",
        "close_tab",
        "reload",
        "back",
        "forward",
        "screenshot",
        "page_snapshot",
        "cast_screen",
        "scroll",
        "press",
    }
    if tool == "browser_control" and action in LOW_BROWSER_ACTIONS:
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Browser navigation: {action}",
        }

    LOW_COMPUTER_ACTIONS = {
        "volume_up",
        "volume_down",
        "mute",
        "toggle_mute",
        "show_desktop",
        "full_screen",
        "screenshot",
        "dark_mode",
        "refresh_page",
        "spotify_play",
        "spotify_pause",
        "spotify_next",
        "spotify_previous",
        "spotify_like",
        "spotify_dislike",
        "spotify_search",
        "spotify_liked_songs",
        "spotify_new_releases",
        "spotify_made_for_you",
        "spotify_volume_up",
        "spotify_volume_down",
        # Radio + paired-device control: instantly reversible by saying the
        # opposite, and gating them would put a prompt in front of the most
        # routine "connect my speaker". Listed explicitly rather than left to
        # the default-LOW fallthrough, so they stay ungated on purpose.
        "toggle_bluetooth",
        "bluetooth_on",
        "bluetooth_off",
        "bluetooth_status",
        "bluetooth_settings",
        "connect_device",
        "disconnect_device",
    }
    if tool == "computer_settings" and action in LOW_COMPUTER_ACTIONS:
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Low-risk system action: {action}",
        }

    LOW_COMPUTER_CONTROL = {
        "running_apps",
        "system_snapshot",
        "active_window",
        "volume_up",
        "volume_down",
        "mute",
        "unmute",
    }
    if tool == "computer_control" and action in LOW_COMPUTER_CONTROL:
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Informational action: {action}",
        }

    if tool == "audio_control":
        # Clearing the Windows per-app device preference DELETES a user setting:
        # it is undone only by re-picking the device in the Volume mixer by hand,
        # and on this machine that preference is what keeps Era's voice on the
        # glasses. It does not belong under the blanket rule below.
        # Normalised through the tool's OWN alias table, so a spelling added
        # there later is gated automatically. The import can fail (this repo
        # lives on a synced path), so the raw-text test below must stand on its
        # own and FAIL CLOSED: anything that asks to undo an app preference is
        # gated whether or not the alias table could be read.
        raw = str(action or "").lower().strip().replace("-", "_").replace(" ", "_")
        canonical = raw
        try:
            from actions.audio_control import _norm_action

            canonical = _norm_action(action)
        except Exception:
            pass
        _UNDO = ("clear", "reset", "drop", "remove", "delete", "forget", "unpin")
        if canonical == "clear_app_preference" or ("app" in raw and any(w in raw for w in _UNDO)):
            return {
                "risk_level": MEDIUM,
                "requires_confirmation": True,
                "reason": "Deleting the Windows per-app audio device preference.",
            }
        # Volume, mute and default-device changes are instantly reversible and touch
        # no data. Listed explicitly (rather than left to the LOW default) so a future
        # default change cannot quietly start gating them.
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Reversible audio setting: {action}",
        }

    if tool in ("media_coordinator", "netflix_control", "spotify_control"):
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Media control: {tool}",
        }

    # `clean` and `organize` relocate EVERY loose file on the Desktop. They were
    # grouped with wallpaper/list/stats as "personalization" and ran unconfirmed,
    # which made them the cheap way around the planner's confirmation: injected
    # text wanting bulk file movement never needed action=task at all. Reversible
    # (files are moved, not deleted), hence MEDIUM rather than HIGH.
    if tool == "desktop_control" and action in ("clean", "organize"):
        return {
            "risk_level": MEDIUM,
            "requires_confirmation": True,
            "reason": f"Moves every loose file on the Desktop: {action}",
        }

    if tool == "desktop_control" and action in (
        "wallpaper",
        "wallpaper_url",
        "list",
        "stats",
    ):
        return {
            "risk_level": LOW,
            "requires_confirmation": False,
            "reason": f"Desktop personalization: {action}",
        }

    # --- default: LOW ---
    return {
        "risk_level": LOW,
        "requires_confirmation": False,
        "reason": "Unclassified action — defaulting to LOW risk.",
    }


# ── Vision risk classification ──────────────────────────────────────────


def classify_vision_risk(
    result: VisionResult | None,
    intended_action: dict | None = None,
) -> dict:
    """Classify risk from a VisionResult by delegating to VisionPolicy.

    Guarantees:
    - Never raises (always returns a dict)
    - Fails CLOSED for mutation actions (block + require confirmation)
    - Fails SAFE for read-only actions (allow description only)
    - Handles result=None gracefully
    - ImportError and all VisionPolicy exceptions caught separately for logging
    """
    try:
        from core.vision_policy import _is_mutation_action

        is_mutation = _is_mutation_action(intended_action)
    except ImportError:
        return _fail_safe(True, "VisionPolicy module deployment unavailable")

    try:
        from core.vision_policy import evaluate_vision_recommendation

        return evaluate_vision_recommendation(result, intended_action)
    except Exception as exc:
        return _fail_safe(is_mutation, f"VisionPolicy raised {type(exc).__name__}: {exc}")


def _fail_safe(is_mutation: bool, reason_prefix: str) -> dict:
    """Return a fail-safe classification dict.

    For mutation actions, blocks the action and requires user confirmation.
    For read-only actions, allows safe description only.

    Args:
        is_mutation: Whether the intended action modifies system state.
        reason_prefix: Explanation of why the fail-safe was triggered.

    Returns:
        Classification dict with risk_level, requires_confirmation, allow_action,
        recommended_path, and reason.
    """
    if is_mutation:
        return {
            "risk_level": "medium",
            "requires_confirmation": True,
            "allow_action": False,
            "recommended_path": "ask_user",
            "reason": f"{reason_prefix}; blocking mutation action as a precaution.",
        }
    return {
        "risk_level": "low",
        "requires_confirmation": False,
        "allow_action": True,
        "recommended_path": "safe_to_describe",
        "reason": f"{reason_prefix}; allowing description only.",
    }
