#!/usr/bin/env python
"""One-shot memory cleanup — run by Karim, not by Claude (guarded files).

Applies the plan in docs/memory_feedback_cleanup.md:

  1. long_term.json
       - remove operational_instruction.avoid_son_topic  (unblocks the son's song)
       - set   preferences.favorite_song = "Berin - Reborn - Radio Edit"
       - add   preferences.son_favorite_song = "I Am on Vacation"
       - add   operational_instruction.spotify_verify_then_minimize  (single rule)
       - add   operational_instruction.close_apps_policy             (resolves conflict)
       - promote deduped durable lessons into user_feedback so Era applies them

  2. feedback_memory.jsonl
       - regroup by canonical category, sort by time, drop noise,
         collapse exact-duplicate user_text (kept in the .bak)

Both files are backed up before any write.

Usage:
    python scripts/cleanup_memory.py            # dry run (default) — shows changes
    python scripts/cleanup_memory.py --apply    # write, after backing up
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LONG_TERM = ROOT / "memory" / "long_term.json"
FEEDBACK = ROOT / "memory" / "feedback_memory.jsonl"

TODAY = datetime.now().strftime("%Y-%m-%d")
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# 1. long_term.json
# ---------------------------------------------------------------------------


def _entry(value: str) -> dict:
    return {"value": value, "updated": TODAY}


# Deduped, consolidated durable lessons promoted into user_feedback. Each is the
# single canonical statement of a rule the raw journal repeated many times.
PROMOTED_USER_FEEDBACK = {
    "spotify_visibility": _entry(
        "After any Spotify task, verify playback actually started and stabilized, "
        "THEN minimize/hide Spotify. Never minimize before verifying. 'hide' = minimize."
    ),
    "verify_task_done": _entry(
        "Always verify a task actually happened (via vision/state), not just that the "
        "tool call returned success. Never ask the user to 'try again' — ensure it is done."
    ),
    "standby_phrasing": _entry(
        "Only enter standby on an explicit command (offline/standby/take a break/ciao). "
        "When entering standby say 'offline'/'on standby'; when resuming say 'online'. "
        "Never enter/exit standby from noise or inactivity misfires."
    ),
    "language_policy": _entry(
        "Speak English permanently; only switch to Tunisian Darija + French when explicitly "
        "asked. No accent. Ignore speech in languages other than French/English/Arabic."
    ),
    "response_style": _entry(
        "Vary confirmation phrasing; keep final confirmations to a concise 2-3 words "
        "(e.g. 'Done', 'Track skipped'). Do not re-list the actions performed."
    ),
    "reuse_open_tab": _entry(
        "For Facebook/WhatsApp/Instagram, if a matching browser tab is already open, focus "
        "it instead of opening a duplicate tab. Use Brave for Facebook."
    ),
    "file_save_location": _entry(
        "Never save new files in the project/source directories. Save to the user's "
        "Documents or Downloads. Append to issue reports; never overwrite them."
    ),
    "clarification_on_unclear": _entry(
        "If speech is unclear or multiple voices are present, do not act — ask "
        "'Sorry, I did not catch that. Can you repeat?' instead of guessing."
    ),
    "app_launch_prefs": _entry(
        "Open Claude via the Windows search bar (type 'Claude'), never in a terminal/VS Code. "
        "Use Photoshop 2026 and Premiere 2026 (not beta). 'download manager' = Neat Download Manager."
    ),
}


def clean_long_term(dry: bool) -> None:
    data = json.loads(LONG_TERM.read_text(encoding="utf-8"))
    changes: list[str] = []

    ops = data.setdefault("operational_instruction", {})
    if "avoid_son_topic" in ops:
        ops.pop("avoid_son_topic")
        changes.append("removed operational_instruction.avoid_son_topic")

    prefs = data.setdefault("preferences", {})
    if prefs.get("favorite_song", {}).get("value") != "Berin - Reborn - Radio Edit":
        prefs["favorite_song"] = _entry("Berin - Reborn - Radio Edit")
        changes.append("set preferences.favorite_song = 'Berin - Reborn - Radio Edit'")
    if "son_favorite_song" not in prefs:
        prefs["son_favorite_song"] = _entry("I Am on Vacation")
        changes.append("added preferences.son_favorite_song = 'I Am on Vacation'")

    if "spotify_verify_then_minimize" not in ops:
        ops["spotify_verify_then_minimize"] = _entry(
            "After any Spotify task, first verify playback truly started and stabilized, "
            "THEN minimize/hide Spotify. Do not minimize before verifying; do not ask for "
            "confirmation while music is already playing. 'hide' means minimize."
        )
        changes.append("added operational_instruction.spotify_verify_then_minimize")
    if "close_apps_policy" not in ops:
        ops["close_apps_policy"] = _entry(
            "Close Spotify and other no-risk apps without asking for confirmation. "
            "All other applications still require explicit authorization before closing."
        )
        changes.append("added operational_instruction.close_apps_policy")

    uf = data.setdefault("user_feedback", {})
    for key, entry in PROMOTED_USER_FEEDBACK.items():
        if key not in uf:
            uf[key] = entry
            changes.append(f"promoted user_feedback.{key}")

    print("\n[long_term.json] planned changes:")
    for c in changes:
        print(f"  - {c}")
    if not changes:
        print("  (nothing to change)")

    if not dry and changes:
        _backup(LONG_TERM)
        LONG_TERM.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print("  -> written.")


# ---------------------------------------------------------------------------
# 2. feedback_memory.jsonl — regroup / dedupe / drop noise
# ---------------------------------------------------------------------------

# Canonical categories in logical render order.
CATEGORY_ORDER = [
    "identity_naming",
    "media_spotify",
    "media_routing",
    "app_launching",
    "file_handling",
    "standby_wake",
    "startup_greeting",
    "language_speech",
    "clarification_safety",
    "verification",
    "response_style",
    "brand_cm",
    "songs",
    "feature_request",
    "system_issue",
    "praise",
    "misc",
]

_CLASSIFIERS = [
    ("media_spotify", r"\bspotify\b|minimize.*music|hide.*(spotify|music)"),
    ("songs", r"\bson'?s\b.*song|favorite song|perfect song|reborn|on vacation"),
    ("media_routing", r"\bnetflix\b|\byoutube\b|wild robot|route it|cross.?rout"),
    (
        "app_launching",
        r"\bphotoshop\b|\bpremiere\b|\bclaude\b|download manager|open (the )?app|brave|new tab|reuse|existing tab",
    ),
    (
        "file_handling",
        r"\bfile\b|\bfolder\b|download folder|overwrite|append|report.*(file|issue)|save.*(path|folder)",
    ),
    ("standby_wake", r"standby|offline|wake|back online|era online|ciao|going offline"),
    ("startup_greeting", r"startup|new session|greet|era ready|operational|launch.*routine"),
    (
        "language_speech",
        r"\benglish\b|\bfrench\b|\barabic\b|darija|tunisian|accent|language|spanish|korean|japanese",
    ),
    (
        "clarification_safety",
        r"multiple voices|clarification|did not catch|didn'?t get it|unclear|don'?t understand",
    ),
    ("verification", r"verify|make sure.*done|task is (clearly )?done|try again|check.*done"),
    ("response_style", r"brief|concise|phrasing|2-3 words|vary|short response"),
    ("brand_cm", r"delta cuisine|ceram|quartz|brand lock|community.?manag|carousel|@delta"),
    ("identity_naming", r"\bkarim\b|bachboucha|bashboucha|spell|my name"),
    ("feature_request", r"feature request|msi center|pc boost|self.?diagnos|ability to"),
    (
        "system_issue",
        r"\bissue\b|\bproblem\b|\bcrash\b|\bbug\b|mic (shows|status)|flicker|discrepancy|not accurate|unexpectedly",
    ),
    ("praise", r"i like (you|your voice|this)|super good|thank you|loyalty|i love"),
]

# Pure acknowledgement / ASR fragments with no actionable content -> dropped
# (still preserved in the .bak backup).
_NOISE = re.compile(
    r"^(ok(ay)?[.,! ]*)?(perfect|nice|thank you|thanks|yeah|yes|cool|great|"
    r"got it|sure)[.,!]*$",
    re.IGNORECASE,
)


def _classify(text: str) -> str:
    low = (text or "").lower()
    for cat, pat in _CLASSIFIERS:
        if re.search(pat, low):
            return cat
    return "misc"


def _is_noise(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 12 and not re.search(r"[a-z]{4,}\s+[a-z]{4,}", t.lower()):
        return True
    return bool(_NOISE.match(t))


def clean_feedback(dry: bool) -> None:
    raw = [json.loads(ln) for ln in FEEDBACK.read_text(encoding="utf-8").splitlines() if ln.strip()]
    seen: set[str] = set()
    kept: list[dict] = []
    dropped_noise = 0
    dropped_dup = 0
    for rec in raw:
        text = (rec.get("user_text") or "").strip()
        if _is_noise(text):
            dropped_noise += 1
            continue
        norm = re.sub(r"\s+", " ", text.lower())
        if norm in seen:
            dropped_dup += 1
            continue
        seen.add(norm)
        rec["canonical_category"] = _classify(text)
        kept.append(rec)

    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    kept.sort(key=lambda r: (order.get(r["canonical_category"], 999), r.get("timestamp") or ""))

    counts: dict[str, int] = {}
    for r in kept:
        counts[r["canonical_category"]] = counts.get(r["canonical_category"], 0) + 1

    print("\n[feedback_memory.jsonl] regroup summary:")
    print(
        f"  raw={len(raw)}  kept={len(kept)}  dropped_noise={dropped_noise}  dropped_dup={dropped_dup}"
    )
    for cat in CATEGORY_ORDER:
        if counts.get(cat):
            print(f"    {cat:22} {counts[cat]}")

    if not dry:
        _backup(FEEDBACK)
        with FEEDBACK.open("w", encoding="utf-8") as fh:
            for r in kept:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("  -> written (regrouped).")


# ---------------------------------------------------------------------------


def _backup(path: Path) -> None:
    dest = path.with_suffix(path.suffix + f".bak-{STAMP}")
    shutil.copy2(path, dest)
    print(f"  backup: {dest.name}")


def main() -> int:
    apply = "--apply" in sys.argv
    mode = "APPLY (writing)" if apply else "DRY RUN (no writes) — pass --apply to write"
    print(f"cleanup_memory · {mode}")
    for p in (LONG_TERM, FEEDBACK):
        if not p.exists():
            print(f"ERROR: missing {p}")
            return 1
    clean_long_term(dry=not apply)
    clean_feedback(dry=not apply)
    if not apply:
        print("\nDry run complete. Re-run with --apply to write (both files are backed up first).")
    else:
        print("\nDone. Restart Era to load the cleaned memory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
