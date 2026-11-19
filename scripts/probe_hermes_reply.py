"""Read-only probe: one Laura model call through Hermes, raw reply printed.

Sends Laura's real crew prompt to the Hermes gateway ONCE and prints what comes
back, so a "gave no usable reply" can be diagnosed from the actual text. No
capability is executed and the Hermes key is never printed.

Usage (Windows Python, from the repo root):
    python scripts/probe_hermes_reply.py "task text"
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crew import hermes_transport as ht  # noqa: E402
from crew import runtime as rt  # noqa: E402
from crew.transport import Turn  # noqa: E402


def main() -> int:
    task = sys.argv[1] if len(sys.argv) > 1 else "Say hello to confirm you are connected."
    bot = sys.argv[2] if len(sys.argv) > 2 else "laura"
    t = ht.HermesAgentTransport()
    for bot_id, spec in rt.load_specs().specs.items():
        t.register(bot_id, spec)
    spec = t._bots[bot]
    t._gateway_check()  # the same pre-turn check a real dispatch runs
    raw: dict = {}
    real_post = t._post_chat

    extra = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}

    def spy(spec_, turn_, key, payload):
        merge = extra.pop("_merge_system", False) if isinstance(extra, dict) else False
        payload = {**payload, **extra}  # experiment: extra request fields (argv[3], JSON)
        if merge:  # experiment: carry the crew instructions inside the user message
            sys_msg, user_msg = payload["messages"][0]["content"], payload["messages"][1]["content"]
            payload["messages"] = [
                {"role": "user", "content": f"{sys_msg}\n\n---\n\n{user_msg}"},
            ]
        status, body = real_post(spec_, turn_, key, payload)
        raw.update(status=status, body=body, model=payload.get("model"))
        return status, body

    t._post_chat = spy
    turn = Turn(task=task, deadline=time.monotonic() + 120)
    system = rt._system(spec, t._bots)
    user = rt._user(turn, [], rt.step_budget.note(1, rt.MAX_STEPS), spec.capabilities)
    started = time.monotonic()
    reply = t._ask(spec, turn, system, user)
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    try:
        text = body["choices"][0]["message"]["content"]
        finish = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        text, finish = None, None
    print(
        f"seconds={time.monotonic() - started:.1f} status={raw.get('status')} "
        f"model_hint={raw.get('model')} served_model={body.get('model')} finish={finish}"
    )
    print(f"usage={json.dumps(body.get('usage'))}")
    print("RAW TEXT >>>")
    print(text)
    print("<<< PARSED:", json.dumps(reply)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
