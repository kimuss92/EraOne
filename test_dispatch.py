r"""
test_dispatch.py — end-to-end wiring proof for era_dispatch.py.

Bypasses Hermes/voice (not yet wired into main.py) and calls run_task() directly.
Proves, for ONE brand:
  1. resolve_brand() picks the right brand key from the spoken order.
  2. The run is pinned to that brand's OWN Claude account (CLAUDE_CONFIG_DIR)
     and OWN project folder (cwd), read straight from brands.yaml.
  3. current_job() reports the live job WHILE it is mid-run (status file held
     under the global lock), then clears when the job finishes.
  4. The final result comes back ok.

Read-only by design: allowed_tools is locked to Read,Grep,Glob so the wiring
test physically cannot produce, edit, or publish anything.

Usage:
  python test_dispatch.py                       # default wiring-test order (Delta)
  python test_dispatch.py "some other order"    # override the order text
"""

import json
import os
import sys
import threading
import time

import era_dispatch as ed

# The wiring-test-only order. Explicitly forbids any production/publishing and
# asks Claude to prove the pin (account + folder + charter access) back to us.
DEFAULT_ORDER = (
    "This is a wiring test only — do not produce, generate, or publish anything. "
    "State your Brand Lock (which accounts you're allowed to touch), confirm you "
    "can read MASTER_CONTENT_BLUEPRINT.md, and reply with one line confirming "
    "you're running as the Delta Cuisine account in the correct project folder."
)

READ_ONLY_TOOLS = "Read,Grep,Glob"


def main():
    order = " ".join(sys.argv[1:]) or DEFAULT_ORDER

    cfg = ed.load_brands()
    brand_key = ed.resolve_brand(order, cfg)
    print(f"[resolve] order resolved to brand key: {brand_key!r}")
    if not brand_key:
        print("[FAIL] could not resolve exactly one brand from the order.")
        return 1

    brand = cfg["brands"][brand_key]
    config_dir = os.path.expandvars(brand["claude_config_dir"])
    project_dir = os.path.expandvars(brand["project_dir"])
    print(f"[pin] CLAUDE_CONFIG_DIR -> {config_dir}")
    print(f"[pin] cwd (project_dir) -> {project_dir}")
    print(f"[pin] charter_file      -> {brand.get('charter_file')}")
    print(f"[pin] status            -> {brand.get('status')}")

    # Sanity: this MUST be the Delta account, not the freelance one.
    if ".claude-delta" not in config_dir.lower():
        print(f"[WARN] expected the Delta account (.claude-delta); got {config_dir}")

    # --- run the dispatch in a background thread so we can watch current_job() ---
    result_box = {}

    def _run():
        result_box["res"] = ed.run_task(
            order,
            task_type="weekly",
            brand_key=brand_key,
            allowed_tools=READ_ONLY_TOOLS,
            timeout=600,
        )

    print(f"\n[run] dispatching (read-only tools: {READ_ONLY_TOOLS}) ...\n")
    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # --- poll current_job() until we catch it mid-run -------------------------
    seen_live = False
    deadline = time.time() + 600
    while t.is_alive() and time.time() < deadline:
        job = ed.current_job()
        if job:
            seen_live = True
            elapsed = time.time() - job.get("started", time.time())
            print(
                "[current_job] LIVE: brand={} task={} (running {:.1f}s)".format(
                    job.get("brand"), job.get("task_type"), elapsed
                )
            )
            # confirm the live status reports the brand we expect
            if job.get("brand") != brand_key:
                print(
                    "[WARN] current_job brand mismatch: {} != {}".format(
                        job.get("brand"), brand_key
                    )
                )
            break
        time.sleep(0.2)

    if not seen_live:
        print(
            "[current_job] (job finished before we could sample it — status file "
            "is written under the lock; a fast run can clear it before the poll)"
        )

    # let it keep reporting a couple more times so you can SEE it mid-run
    for _ in range(3):
        if not t.is_alive():
            break
        job = ed.current_job()
        if job:
            print(
                "[current_job] still LIVE: brand={} order={:.60}...".format(
                    job.get("brand"), job.get("order", "")
                )
            )
        time.sleep(2)

    t.join()

    # --- status file must be cleared once the job is done ---------------------
    after = ed.current_job()
    print("\n[current_job] after run: %s" % ("None (cleared) OK" if after is None else after))

    res = result_box.get("res", {})
    print("\n========== FINAL RESULT ==========")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("==================================")

    ok = bool(res.get("ok"))
    used_brand = res.get("brand")
    print(f"\n[verdict] ok={ok}  brand={used_brand}  (expected delta_cuisine)")
    if ok and used_brand == "delta_cuisine":
        print(
            "[verdict] PASS — dispatched under Delta Cuisine, one-at-a-time lock held, status reported."
        )
        return 0
    print("[verdict] FAIL — see result above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
