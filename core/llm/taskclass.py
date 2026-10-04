"""Deterministic task classification — which `routing.by_task` class is this?

WHY NOT A MODEL
---------------
This runs in front of every utterance, including "volume up". Calling a model to
decide whether to call a model costs more than the decision saves, and Era
already pays for one classifier pass (lane_router, cm-vs-assistant). A regex
pass is microseconds and, more importantly, it is TESTABLE: every routing
decision it produces can be pinned, which is what makes shadow mode meaningful.

This is also the instrument for the open question recorded in config/llm.yaml —
whether deterministic commands should bypass the LLM after reliable intent
detection. "Reliable" is the load-bearing word, and shadow mode is how it gets
measured instead of assumed.

WHY IT IS ALLOWED TO SAY "I DON'T KNOW"
---------------------------------------
`classify()` returns None whenever nothing matches confidently. That is the most
important behaviour here. A classifier that always has an opinion will route
things wrongly with total confidence, and the expensive direction of that
mistake — sending a two-word command to a 2.9s, $0.06 model — is exactly what
the routing policy exists to prevent.

Precedence is deliberate: cheap/local classes are matched BEFORE expensive ones,
so an utterance that looks like both ("turn up the volume and explain why it was
quiet") lands on the voice model rather than on Claude.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Class names mirror routing.by_task in config/llm.yaml. A name that drifts out
# of step there silently routes to the default instead — tools/test_taskclass.py
# checks these against the shipped registry.
MEDIA_PLAYBACK = "media_playback"
DEVICE_ADJUSTMENT = "device_adjustment"
NAVIGATION_STATUS = "navigation_status"
SHORT_COMMAND = "short_command"
COMPLEX_PLANNING = "complex_planning"
RESEARCH_SYNTHESIS = "research_synthesis"
LONG_CONTEXT = "long_context"

# An utterance longer than this is long-context work regardless of wording:
# pasted logs, a spec, an error dump. Chosen well above conversational length so
# an ordinary spoken sentence can never trip it.
LONG_CONTEXT_CHARS = 900

# Above this many words an utterance is not a "short command", whatever verbs it
# contains. Keeps "open the file and tell me why the third stanza scans badly"
# out of the device lane.
SHORT_COMMAND_MAX_WORDS = 7


@dataclass(frozen=True)
class TaskGuess:
    """A classification, with the evidence that produced it."""

    task: str
    matched: str  # the pattern name that fired — shown in the shadow report
    confident: bool = True


_MEDIA = re.compile(
    r"\b(play|pause|resume|unpause|stop|skip|next\s+(track|episode|song)|previous\s+(track|song)|"
    r"put\s+on|shuffle|netflix|spotify|youtube|watch|episode|movie|film)\b",
    re.IGNORECASE,
)

_DEVICE = re.compile(
    r"\b(volume|louder|quieter|mute|unmute|brightness|brighter|dimmer|"
    r"turn\s+(it\s+)?(up|down)|wifi|wi-?fi|bluetooth|airplane\s+mode)\b",
    re.IGNORECASE,
)

_STATUS = re.compile(
    r"\b(what\s+time|what'?s\s+the\s+time|battery|cpu|ram|memory\s+usage|disk\s+space|"
    r"temperature|weather|forecast|uptime|are\s+you\s+(there|online)|status)\b",
    re.IGNORECASE,
)

_SHORT_VERB = re.compile(
    r"^\W*(?:era\s*[,;:]?\s*)?(?:please\s+)?"
    r"(open|close|launch|start|quit|exit|switch\s+to|focus|minimi[sz]e|maximi[sz]e|"
    r"screenshot|lock|show|hide)\b",
    re.IGNORECASE,
)

# Planning/reasoning. Every alternative needs a phrase, not a single common word:
# a bare "how" or "why" appears in ordinary chat constantly, and routing all of
# it to a $0.06 model is the failure this file is shaped to avoid.
_PLANNING = re.compile(
    r"\b(trade[\s-]?offs?|pros\s+and\s+cons|architect(?:ure)?|refactor|migrat(?:e|ion)|"
    r"design\s+(?:a|an|the)\s+\w+|come\s+up\s+with\s+a\s+plan|draft\s+a\s+plan|"
    r"how\s+should\s+(?:i|we|you)|what'?s\s+the\s+best\s+way\s+to|"
    r"walk\s+me\s+through|step\s+by\s+step|strategy\s+for|roadmap)\b",
    re.IGNORECASE,
)

_RESEARCH = re.compile(
    r"\b(summari[sz]e|summary\s+of|compare\s+.{2,40}\s+(?:and|with|to)\s+|"
    r"research|look\s+into|dig\s+into|analy[sz]e|what\s+are\s+the\s+(?:main|key)\s+)\b",
    re.IGNORECASE,
)

# Ordered: cheap/local classes first, so a mixed utterance stays on the voice
# model. Each entry is (name, pattern, task).
_CHEAP_RULES = (
    ("device", _DEVICE, DEVICE_ADJUSTMENT),
    ("media", _MEDIA, MEDIA_PLAYBACK),
    ("status", _STATUS, NAVIGATION_STATUS),
)

_EXPENSIVE_RULES = (
    ("planning", _PLANNING, COMPLEX_PLANNING),
    ("research", _RESEARCH, RESEARCH_SYNTHESIS),
)


def classify(text: str) -> TaskGuess | None:
    """Best-effort task class, or None when nothing matches confidently.

    None is a real answer, not a failure: it means "no opinion, let the live
    session handle this as it always has".
    """
    body = (text or "").strip()
    if not body:
        return None

    # Length wins over wording. A pasted log is long-context work even if it
    # happens to contain the word "play".
    if len(body) >= LONG_CONTEXT_CHARS:
        return TaskGuess(LONG_CONTEXT, "length")

    for name, pattern, task in _CHEAP_RULES:
        if pattern.search(body):
            return TaskGuess(task, name)

    words = body.split()
    if len(words) <= SHORT_COMMAND_MAX_WORDS and _SHORT_VERB.match(body):
        return TaskGuess(SHORT_COMMAND, "short_verb")

    for name, pattern, task in _EXPENSIVE_RULES:
        if pattern.search(body):
            # A long-ish planning question is the strong case. A very short one
            # ("refactor this") is real but thin, so it is marked unconfident
            # and autopilot can be told to act only on confident guesses.
            return TaskGuess(task, name, confident=len(words) >= 5)

    return None


def describe(guess: TaskGuess | None) -> str:
    if guess is None:
        return "no opinion"
    suffix = "" if guess.confident else " (low confidence)"
    return f"{guess.task} via {guess.matched}{suffix}"
