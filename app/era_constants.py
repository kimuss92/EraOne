"""Shared EraLive constants (extracted from main.py) importable by main and the
era_live mixins without circular imports."""

# The single source of truth for what Era says when she wakes. Both the spoken ack and
# the Telegram notification read this, so the chat can never again report a different
# resume message than the one she actually speaks.
#
# Standby-logic overhaul (2026-09-16): the resume phrase must be exactly one of the
# two sanctioned affirmations — "Era One online." or "System online." — and nothing
# else (the old "Back online, sir." is no longer permitted).
_RESUME_ACK = "Era One online."


# ── Pending brand-ask tool policy ──────────────────────────────
# While Era is waiting for the user to say which brand a CM request is for, Gemini
# must NOT run off and guess/act (it would call screen_process to read the brand
# off-screen, web_search it, etc.). We use an ALLOW-LIST, not a deny-list: during
# the ask EVERY tool is blocked except this minimal escape-hatch set, so a tool
# added in the future is protected by default rather than silently slipping
# through. Suppression is gated on _pending_cm_ask_active() so it lifts the instant
# the ask resolves or its 45s window expires (see that method).
_PENDING_ASK_ALLOWED_TOOLS = {"enter_standby"}

# ── Awaiting-reply window ──────────────────────────────────────
# How long after Era ends a turn with a QUESTION her user's next short message is
# read as the answer to it rather than as an unclear fragment. Only the typed path
# needs this: speech goes straight into the live session, which remembers what it
# just asked, while text is screened one message at a time by InputSafetyGate.
#
# Three minutes because the channel that needs it most is Telegram, where the user
# is away from the desk — they read the question on a phone, then type back. A
# window measured in seconds would expire exactly when they are slowest, which is
# the case this exists to serve. It only ever relaxes a "that was unclear" guard;
# no action runs without its own confirmation, so a generous window costs nothing.
_AWAITING_REPLY_WINDOW_S = 180

# ── CM hand-off suppression window ─────────────────────────────
# When the lane router claims an utterance for the community-management lane, the headless
# era_dispatch job OWNS it end-to-end (find approved assets, stage the reel to a
# temp public URL via Composio, post, mark .posted). But the same utterance also
# reaches the live Gemini voice model acoustically (open mic), and Gemini — which
# knows nothing about that pipeline — will independently flail on it: web_search
# nonsense, enter_standby, shutdown_era, or narrate a hallucinated blocker ("I need
# a public URL…"). The pending-brand-ask suppression only arms when the brand is
# ambiguous; when it resolves on the first try there was NO guard at all. So the
# instant a CM utterance is claimed we arm this short window, which (like the
# brand-ask) blocks Gemini's tool calls AND drops its spoken reply for the turn,
# leaving the CM lane the sole owner. It self-expires so it can never get stuck.
_CM_HANDOFF_WINDOW_S = 15
