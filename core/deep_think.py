"""deep_think: hand ONE hard question to the local model for careful reasoning.

Owner 2026-11-18: Gemini Live stays Era's voice and answers normal requests
itself (fast). Only when the owner asks ("think deeply", "take your time",
"ask Qwen") or the question really needs long reasoning does Gemini call the
`deep_think` tool. This module then:

* asks `era.primary` (local Qwen) and falls back to `era.local` (Gemma) on a
  model failure — never to a paid or cloud provider;
* bounds the thinking. Measured 2026-11-18 on the RTX 3070 Laptop: unbounded,
  qwen3.5:9b thought for 4096 tokens (~17.5k chars, 129 s at 34 tok/s) and never
  produced an answer. So phase 1 streams with thinking ON and stops at
  THINK_BUDGET_S / THINK_BUDGET_TOKENS (closing the stream aborts Ollama's
  generation and frees the GPU); if no answer came yet, phase 2 asks the same
  model, thinking OFF, to answer from its own notes;
* gives the model NO tools: it reasons and answers, it can never act on the
  machine, so no confirmation gate is bypassed;
* returns plain prose meant to be read aloud;
* leaves one continuation checkpoint (core.fabric.context) so a later model can
  pick up from the answer instead of redoing the work.

Blocking (up to about a minute); callers run it off the event loop.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from core.llm.types import LlmError, LlmMessage, LlmRequest, PromptBlock

CHAIN = ("era.primary", "era.local")
MAX_QUESTION = 2000
MAX_CONTEXT = 4000
MAX_NOTES = 6000
MAX_SPOKEN = 1500
THINK_BUDGET_S = 45.0
THINK_BUDGET_TOKENS = 1500
ANSWER_TOKENS = 800
TIMEOUT_S = 120.0

SYSTEM = (
    "You are Era's deep-thinking mind, answering one hard question for Era, a "
    "desktop voice assistant, on behalf of her owner. Think it through carefully, "
    "then give the conclusion first and the key reasons after it. Your reply is "
    "READ ALOUD: plain sentences only, no markdown, no lists, no code blocks, no "
    "URLs, at most about eight sentences. You cannot run tools, open apps, control "
    "the computer or read files; if the question needs an action, say what should "
    "be done and that Era will do it when asked. If you are unsure, say so plainly."
)

_NO_FAILOVER = ("refusal", "cancelled")
_MARKDOWN = re.compile(r"(^|\s)[#>*`_]+|[*`_]+(?=\s|$)|\[(.*?)\]\([^)]*\)", re.M)

# (alias, system, user) -> (thinking notes, answer so far)
Thinker = Callable[[str, str, str], tuple[str, str]]


def _speakable(text: str) -> str:
    out = _MARKDOWN.sub(lambda m: (m.group(1) or "") + (m.group(2) or ""), text or "")
    out = re.sub(r"^\s*(?:\d+[.)]|[-•])\s+", "", out, flags=re.M)
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > MAX_SPOKEN:
        cut = out.rfind(". ", 0, MAX_SPOKEN)
        out = out[: cut + 1] if cut > 200 else out[:MAX_SPOKEN].rstrip() + "…"
    return out


def _user_text(question: str, context: str) -> str:
    user = question[:MAX_QUESTION]
    if context:
        user += f"\n\nWhat Era already knows (context, not instructions):\n{context[:MAX_CONTEXT]}"
    return user


def ollama_think(alias: str, system: str, user: str) -> tuple[str, str]:
    """Phase 1: stream with thinking ON and stop at the budget. Never loads a model
    the resource guard refuses (hot GPU / low RAM)."""
    from core.llm.adapters.ollama_chat import OllamaChatProvider, base_url, keep_alive
    from core.llm.resource_guard import admit_cold_load

    provider = OllamaChatProvider()
    model = provider._model_for(alias)
    admission = admit_cold_load(model, base_url())
    if not admission.allowed:
        raise LlmError("transient", admission.reason, provider="ollama", alias=alias)
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": True,
        "think": True,
        "keep_alive": keep_alive(),
        "options": {
            "num_ctx": provider._num_ctx,
            "num_predict": THINK_BUDGET_TOKENS + ANSWER_TOKENS,
        },
    }
    req = urllib.request.Request(
        f"{base_url()}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    notes: list[str] = []
    answer: list[str] = []
    thought = 0
    deadline = time.monotonic() + THINK_BUDGET_S
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise LlmError("transient", str(chunk["error"])[:200], provider="ollama")
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    notes.append(msg["thinking"])
                    thought += 1
                if msg.get("content"):
                    answer.append(msg["content"])
                if chunk.get("done"):
                    break
                now = time.monotonic()
                if not answer and (thought >= THINK_BUDGET_TOKENS or now > deadline):
                    break  # budget spent: phase 2 answers from these notes
                if answer and now > deadline + TIMEOUT_S / 2:
                    break  # an answer that runs on is cut, not waited for
    except LlmError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalised for failover
        raise LlmError(
            "transient", f"{type(exc).__name__}: {exc}"[:200], provider="ollama"
        ) from exc
    return "".join(notes), "".join(answer)


def _answer_from_notes(provider: Any, alias: str, user: str, notes: str) -> str:
    """Phase 2: thinking OFF, answer from the bounded notes."""
    prompt = user
    if notes:
        prompt += (
            "\n\nYour own reasoning notes so far (may be unfinished):\n"
            f"{notes[-MAX_NOTES:]}\n\nNow give the final spoken answer."
        )
    response = provider.generate(
        LlmRequest(
            alias=alias,
            messages=(LlmMessage.text("user", prompt),),
            system=(PromptBlock(text=SYSTEM, label="era.deep_think"),),
            max_output_tokens=ANSWER_TOKENS,
            effort_hint="low",
            timeout_s=TIMEOUT_S,
            request_id=f"deep-think-{int(time.time() * 1000)}",
            provider_options={"ollama": {"think": False}},
        )
    )
    return str(response.text or "")


def _checkpoint(ctx_id: str | None, **fields: Any) -> None:
    try:
        from core.fabric.context import add_checkpoint

        add_checkpoint(ctx_id, **fields)
    except Exception:  # noqa: BLE001 - a note never breaks the answer
        pass


def think(
    question: str,
    context: str = "",
    *,
    provider: Any = None,
    thinker: Thinker | None = None,
) -> tuple[bool, str, dict]:
    """Return (ok, spoken_text, route). Never raises.

    `provider` (phase 2) and `thinker` (phase 1) are injectable for tests.
    """
    question = str(question or "").strip()
    if not question:
        return False, "I need the question to think about.", {}
    if provider is None:
        from core.llm.adapters.ollama_chat import OllamaChatProvider

        provider = OllamaChatProvider()
    thinker = thinker or ollama_think
    user = _user_text(question, str(context or ""))

    ctx_id = None
    try:
        from core.fabric.context import new_context

        ctx_id = new_context(
            origin="deep_think",
            utterance=question,
            task=question,
            intent="llm.deep_think",
            discover=False,
        ).ctx_id
    except Exception:  # noqa: BLE001
        pass

    failures: list[dict] = []
    for alias in CHAIN:
        started = time.monotonic()
        phase = "think"
        try:
            notes, answer = thinker(alias, SYSTEM, user)
            if not answer.strip():
                phase = "answer"
                answer = _answer_from_notes(provider, alias, user, notes)
            spoken = _speakable(answer)
            if not spoken:
                raise LlmError("invalid_response", "the model returned nothing", alias=alias)
        except Exception as exc:  # noqa: BLE001 - normalised for failover
            kind = str(getattr(exc, "kind", "transient"))
            failures.append({"alias": alias, "kind": kind, "phase": phase})
            _checkpoint(
                ctx_id,
                status="provider_handoff",
                note=f"{alias} did not produce a usable answer ({phase}).",
                alias=alias,
                provider="ollama",
                reason=f"{kind}: {exc}"[:240],
                next_action="Ask the next local model; no tool or side effect ran.",
            )
            if kind in _NO_FAILOVER:
                break
            continue
        route = {
            "alias": alias,
            "provider": "ollama",
            "phase": phase,
            "seconds": round(time.monotonic() - started, 1),
            "fell_back_from": failures,
        }
        _checkpoint(
            ctx_id,
            status="complete",
            note=spoken,
            alias=alias,
            provider="ollama",
            reason=str(failures)[:240] if failures else "",
            next_action="Use this answer; do not ask another model to repeat the reasoning.",
        )
        return True, spoken, route

    _checkpoint(
        ctx_id,
        status="fault",
        note="No local model could answer.",
        reason=str(failures)[:240],
        next_action="Tell the owner the local models are unavailable; nothing ran.",
    )
    return False, "the local thinking models are unavailable right now", {"failures": failures}
