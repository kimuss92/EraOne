"""Step-budget messages for a crew turn (used by crew/runtime.py).

October 2026 logs: 11 of 35 crew asks ended "used all 6 steps without finishing",
each discarding everything the bot had already gathered. The model was never
told the budget existed, and running out threw the work away. So the turn loop
now warns on the last two steps and, once the budget is spent, makes ONE
capability-free "wrap up" call whose answer is built only from what was gathered.
"""

from __future__ import annotations

import json

WRAP_UP_NOTE = (
    "STEP BUDGET EXHAUSTED: you may not call any capability. Reply now with "
    '{"final": "<answer>"} built only from the observations above. Say plainly what you '
    "found, what is still missing, and what the user should ask next."
)


def note(step: int, max_steps: int) -> str:
    """The budget line appended to the bot's prompt on this step ("" when not yet close)."""
    left = max_steps - step + 1
    if left == 1:
        return 'STEP BUDGET: this is your LAST step. Reply now with {"final": ...} using what you have.'
    if left == 2:
        return (
            "STEP BUDGET: 2 steps left including this one. Finish on the next step at the latest."
        )
    return ""


def final_text(reply: object) -> str | None:
    """The answer in a wrap-up reply, or None when it is not a `final`.

    A reply that is still a capability call (a bot that will not stop) yields
    None: nothing from the wrap-up call is ever executed.
    """
    if not isinstance(reply, dict) or "final" not in reply:
        return None
    final = reply["final"]
    text = json.dumps(final, ensure_ascii=False) if isinstance(final, dict) else str(final)
    return text.strip() or None
