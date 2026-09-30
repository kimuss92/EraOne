"""
hermes_dispatch.py — put CM dispatch BEHIND the Hermes router.

This is the seam between Era's live flow and the heavy community-management lane.
It is deliberately tiny and side-effect-light so main.py can adopt it from ONE
place without the 144KB voice loop having to know how routing or dispatch work.

FLOW
      text ─► Hermes.route ─┬─ category "assistant" ─► return {handled: False}   (Era keeps going)
                           │
                           └─ category "cm" ────────► era_dispatch.run_task(...)
                                                       ├─ resolved brand    ─► run it
                                                       └─ no/ambiguous brand ─► {need_brand: True}
                                                                                 (Era asks "which brand?")

CONTRACT (what callers get back — always a dict, never raises)
  handled      : bool   — True if Hermes claimed this for the CM lane. If False,
                          the caller should process `text` through Era's normal flow.
  category     : str    — "cm" | "assistant"
  decision     : dict   — the full Hermes RouteDecision (category/brand/confidence/…)
  When handled is True, one of:
    need_brand : True    — CM work but the brand was ambiguous/missing; ask the user.
    ok         : bool     — dispatch ran; result/error carry the outcome.
    result / error / brand — as returned by era_dispatch.run_task.

USAGE FROM ERA (main.py), single call site:
    from hermes_dispatch import handle
    out = handle(user_text, task_type="weekly")
    if not out["handled"]:
        ...            # normal Era flow (device control, chat, media, memory)
    elif out.get("need_brand"):
        speak("Which brand is this for?")
    elif out["ok"]:
        speak(out["result"])
    else:
        speak("That community-management task failed: " + out.get("error", ""))

Nothing here blocks the event loop except the era_dispatch call itself, which is
already serialized by its own global + cross-process lock. In async Era, run
`handle` in a thread (e.g. asyncio.to_thread) so the voice loop stays responsive.
"""

from __future__ import annotations

from typing import Any

import era_dispatch as ed
from hermes_router import RouteDecision, route


def handle(
    text: str,
    *,
    task_type: str = "weekly",
    allowed_tools: str | None = None,
    dispatch: bool = True,
    timeout: int = 1800,
) -> dict[str, Any]:
    """Route `text`; if it's CM work, dispatch it. Returns the contract dict above.

    Args:
      task_type    : passed through to era_dispatch (weekly/campaign/rush/publish).
      allowed_tools: optional tool allowlist for the Claude Code run.
      dispatch     : if False, classify + resolve brand but DON'T run era_dispatch
                     (dry run — useful for confirmation prompts and tests).
      timeout      : dispatch timeout in seconds.
    """
    decision: RouteDecision = route(text)
    base = {
        "handled": decision.is_cm,
        "category": decision.category,
        "decision": decision.as_dict(),
    }

    if not decision.is_cm:
        # Not our lane — hand straight back to Era's normal flow.
        return base

    if not dispatch:
        # Classified as CM but caller only wants the decision (e.g. to confirm first).
        return {**base, "ok": True, "dispatched": False, "brand": decision.brand}

    # Prefer the brand Hermes already resolved; era_dispatch will re-resolve from
    # the text if we pass None, and will return need_brand if it stays ambiguous.
    result = ed.run_task(
        decision.text,
        task_type=task_type,
        brand_key=decision.brand,
        allowed_tools=allowed_tools,
        timeout=timeout,
    )
    return {**base, "dispatched": True, **result}


if __name__ == "__main__":
    import json
    import sys

    q = " ".join(sys.argv[1:]) or "prepare this week's content for Delta Cuisine"
    # Dry run by default from the CLI so a manual poke never fires a real Claude job.
    out = handle(q, dispatch=False)
    print(json.dumps(out, ensure_ascii=False, indent=2))
