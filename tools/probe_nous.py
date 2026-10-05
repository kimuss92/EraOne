"""Live probe of Nous Portal for the crew router: what it really serves and allows.

    python tools/probe_nous.py            # model listing + one tiny completion
    python tools/probe_nous.py --no-chat  # model listing only (spends nothing)

Needs `nous_api_key` in config/api_keys.json (placed by the user; read here, in this process,
and sent ONLY as a Bearer header to an https *.nousresearch.com URL: it is never printed).
Reports: whether the configured model slug exists, its context window if Nous states one, the
latency and token usage of one real completion, and every rate-limit header Nous returns —
the observed limits that routing.budget.nous_portal should be set from.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm.router import routing_policy  # noqa: E402
from crew.providers import (  # noqa: E402
    NOUS_KEY_ENTRY,
    NousExecutor,
    RateLimited,
    _https_json,
    read_nous_key,
)

_CTX_FIELDS = ("context_length", "context_window", "max_model_len", "max_context_length")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-chat", action="store_true", help="skip the completion")
    args = ap.parse_args(argv)

    key = read_nous_key()
    if not key:
        print(f"NO KEY: put {NOUS_KEY_ENTRY} in config/api_keys.json, then run this again.")
        return 2
    ex = NousExecutor.from_config(routing_policy())
    model = ex.alias_for("standard")
    base = ex._base
    print(f"base : {base}\nmodel: {model}   (key present: yes, not shown)")

    # 1. the catalogue
    status, body, headers = _https_json(
        "GET", f"{base}/models", {"Authorization": f"Bearer {key}"}, None, 20.0
    )
    print(f"\nGET /models -> HTTP {status}")
    models = body.get("data") if isinstance(body, dict) else None
    if isinstance(models, list):
        ids = [m.get("id") for m in models if isinstance(m, dict)]
        print(f"  {len(ids)} models listed; configured slug listed: {model in ids}")
        free = [i for i in ids if i and i.endswith(":free")]
        print(f"  free-suffixed models: {len(free)} {free[:8]}")
        mine = next((m for m in models if isinstance(m, dict) and m.get("id") == model), None)
        if mine:
            ctx = {k: mine[k] for k in _CTX_FIELDS if k in mine}
            print(f"  stated limits for {model}: {ctx or 'none stated in the listing'}")
    shown = {k: v for k, v in headers.items() if k.startswith("x-ratelimit") or k == "retry-after"}
    print(f"  rate-limit headers: {shown or 'none returned'}")
    if args.no_chat:
        return 0 if status == 200 else 1

    # 2. one real, tiny completion
    t0 = time.monotonic()
    try:
        reply = ex.call(
            "Reply with exactly one JSON object.", '{"final": "ok"} - repeat it.', "standard", 60.0
        )
    except RateLimited as exc:
        print(f"\nPOST /chat/completions -> RATE LIMITED, retry after {exc.retry_after_s:.0f}s")
        print(f"  observed headers: {ex.last_rate_limit}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\nPOST /chat/completions FAILED: {type(exc).__name__}: {exc}")
        print(f"  observed headers: {ex.last_rate_limit}")
        return 1
    dt = time.monotonic() - t0
    print(f"\nPOST /chat/completions -> OK in {dt:.1f}s, model served: {reply.model}")
    print(f"  tokens in/out: {reply.tokens_in}/{reply.tokens_out}")
    print(f"  reply text: {reply.text[:200]!r}")
    print(f"  rate-limit headers: {ex.last_rate_limit or 'none returned'}")
    print("\nSet routing.budget.nous_portal from the headers above before trusting the cap.")
    print(json.dumps({"ok": True, "model": reply.model, "latency_s": round(dt, 2)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
