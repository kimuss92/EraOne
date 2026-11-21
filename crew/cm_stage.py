"""The Crew's stage in the Community-Manager lane.

WHAT THIS IS
  Before a Meta Ads *build* run, the crew turns the order into a creative package:
  Tom (creative direction), then Maya (copy), then Nabar (independent review). The package goes into the ads brief as input; the run
  that holds the Meta connection still creates the campaign, PAUSED.

WHAT THIS IS NOT
  The crew has no Meta access and no publishing or spend capability (see
  config/crew/*.yaml). It produces text only. Nothing here approves, publishes or
  changes spend, and the two approval gates stay where they were.

DEGRADE, NEVER RAISE
  A crew that is down or stalls costs the package, never the job: the caller gets
  `ok=False` with a reason and the brief says the crew did not contribute, so the
  run writes its own copy and the owner can see that it did.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The limits the Meta skill states (.claude/skills/era-ads-manager/SKILL.md,
# "Field limits"). Kept as one constant so the prompt and the skill cannot drift
# apart silently: tools/test_cm_crew_stage.py reads both.
FIELD_LIMITS = {"primary_text": 125, "headline": 27, "description": 27}

_SKILL_PATH = ".claude/skills/era-ads-manager/SKILL.md"
_SPECS_MAX_AGE_DAYS = 30


def specs_age_days(today: Any = None, skill_text: str | None = None) -> int | None:
    """Days since the skill's `last_verified:` date, or None when it cannot be read."""
    import datetime as _dt
    from pathlib import Path

    try:
        if skill_text is None:
            skill_text = (Path(__file__).resolve().parent.parent / _SKILL_PATH).read_text(
                encoding="utf-8"
            )
        m = re.search(r"^last_verified:\s*(\d{4}-\d{2}-\d{2})", skill_text, re.M)
        if not m:
            return None
        day = _dt.date.fromisoformat(m.group(1))
        return ((today or _dt.date.today()) - day).days
    except Exception:  # noqa: BLE001 - a missing date never blocks a job
        return None


def specs_age_note(today: Any = None, skill_text: str | None = None) -> str:
    """A brief block telling the run to re-verify Meta's specs when the reference is stale."""
    age = specs_age_days(today, skill_text)
    if age is None:
        return (
            "=== META SPECS REFERENCE: NO VERIFICATION DATE ===\n"
            "Verify formats, limits and policy against Meta's Ads Guide before building.\n\n"
        )
    if age > _SPECS_MAX_AGE_DAYS:
        return (
            f"=== META SPECS REFERENCE IS {age} DAYS OLD ===\n"
            "Meta changes formats, limits and policy often. Check the Ads Guide "
            "(facebook.com/business/ads-guide/update) before building, and say in your "
            "summary that the reference needs a refresh.\n\n"
        )
    return ""


def facts_block(brand: dict | None) -> str:
    """What the crew is allowed to assume about the brand, straight from brands.yaml.
    Live 2026-11-19: with no facts Maya wrote cookware copy for "busy Paris parents"
    for a Tunisian kitchen and wardrobe brand."""
    brand = brand or {}
    ads = brand.get("ads") or {}
    langs = brand.get("content_languages") or {}
    lines = []
    sells = [k for k in ("kitchen", "wardrobe") if k in (brand.get("design_rules") or {})]
    if sells:
        lines.append("Sells: " + " and ".join(sells) + " (made to measure, showroom sales).")
    if brand.get("timezone"):
        lines.append(f"Market timezone: {brand['timezone']} (not Paris).")
    if langs:
        lines.append(
            f"Languages: {langs.get('primary', '')} (about {langs.get('primary_share', '?')}%) "
            f"and {langs.get('secondary', '')}."
        )
    if ads.get("default_objective"):
        lines.append(f"Default objective: {ads['default_objective']}.")
    for aud in (ads.get("audiences") or [])[:1]:
        if isinstance(aud, dict) and aud.get("insight"):
            lines.append(f"Audience insight: {str(aud['insight'])[:240]}")
    lines.append(
        "Do NOT invent products, prices, offers, places or what the reel shows. You cannot "
        "see the reel; the ad reuses the brand's latest published post."
    )
    return "BRAND FACTS (from brands.yaml):\n- " + "\n- ".join(lines) + "\n"


_ITEM = re.compile(
    r"^[\s>*#\-\d.)]*\**(headline|primary|description)\**\s*\d*\s*[:\-]\**\s*(.+?)\**\s*$", re.I
)


def check_limits(copy: str) -> tuple[list[str], list[str]]:
    """Measure Maya's HEADLINE:/PRIMARY:/DESCRIPTION: lines in CODE (a model cannot count
    characters: it reported 14 for a 17-character headline). Returns (report, failures)."""
    limit = {"headline": FIELD_LIMITS["headline"], "primary": FIELD_LIMITS["primary_text"]}
    limit["description"] = FIELD_LIMITS["description"]
    report, failures = [], []
    # a small model sometimes writes the two characters backslash-n instead of a newline
    for line in copy.replace("\\n", "\n").splitlines():
        m = _ITEM.match(line)
        if not m:
            continue
        kind, text = m.group(1).lower(), m.group(2).strip().strip('"*')
        n, cap = len(text), limit[kind]
        ok = n <= cap
        report.append(f"{'OK ' if ok else 'OVER'} {kind} {n}/{cap}: {text}")
        if not ok:
            failures.append(text)
    return report, failures


def _tidy_review(text: str) -> str:
    """Nabar answers in a JSON report; show it as a sentence a person can read."""
    start = text.find("{")
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:]) if start >= 0 else (None, 0)
    except ValueError:
        return text
    if not isinstance(data, dict):
        return text
    found = [
        (f"{f.get('severity', '')}: " if f.get("severity") else "") + str(f.get("issue") or f)
        if isinstance(f, dict)
        else str(f)
        for f in data.get("findings") or []
    ]
    note = str(data.get("no_testable_surface") or "")
    out = ("Findings: " + "; ".join(found)) if found else "No findings raised."
    return out + (f" Could not verify: {note}" if note else "")


def _ask(runner, bot: str, task: str, user_text: str) -> tuple[str, str]:
    """One crew turn. Returns (text, reason); text is "" when the bot gave nothing."""
    try:
        result = runner(
            {"action": "ask", "bot": bot, "task": task}, user_text=user_text, origin="cm"
        )
        text = str(getattr(result, "message", "") or "").strip()
        if getattr(result, "success", False) and text:
            return text, ""
        return "", str(getattr(result, "error", "") or text or "no usable reply")[:160]
    except Exception as exc:  # noqa: BLE001 - one bot failing never stops the others
        return "", f"{type(exc).__name__}: {exc}"[:160]


def _direction_task(brand_key: str, name: str, instruction: str, facts: str = "") -> str:
    return (
        f"Creative direction for a Meta Ads build for {name} (brand key {brand_key}).\n"
        f"ORDER FROM THE OWNER: {instruction}\n{facts}"
        "Pick ONE angle, the hook and the narrative, and name who the ad is for. Read the "
        "vault notes for this brand first. Answer in under 120 words. The ad will reuse an "
        "already-published post. You cannot publish or change anything."
    )


def _copy_task(name: str, instruction: str, direction: str, facts: str = "") -> str:
    lim = FIELD_LIMITS
    return (
        f"Write ad copy for {name} from this creative direction.\n"
        f"ORDER FROM THE OWNER: {instruction}\n{facts}DIRECTION:\n{direction}\n\n"
        f"Give 3 headlines (max {lim['headline']} characters each), 3 primary texts (max "
        f"{lim['primary_text']} characters each), 1 description (max {lim['description']}) "
        "and 1 call to action from Meta's list, in the brand's voice and languages.\n"
        "FORMAT, exactly, one item per line and nothing else on the line:\n"
        "HEADLINE: <text>\nPRIMARY: <text>\nDESCRIPTION: <text>\nCTA: <Meta CTA name>\n"
        "Do not write character counts; they are measured separately. Drafts only."
    )


def _review_task(copy: str) -> str:
    lim = FIELD_LIMITS
    return (
        "Review this ad copy. Check every variant against the limits (headline "
        f"{lim['headline']}, primary text {lim['primary_text']}, description "
        f"{lim['description']} characters) and Meta ad policy: no personal-attribute "
        "claims, no before/after promises, no unsupported guarantees, no misleading "
        "urgency. List which variants fail and why, then give a one-line verdict.\n\n"
        f"COPY:\n{copy}"
    )


def build_task(brand_key: str, display_name: str, instruction: str) -> str:
    """The first step's task (kept for callers and tests that want the prompt)."""
    return _direction_task(brand_key, display_name, instruction)


def creative_package(
    brand_key: str,
    display_name: str,
    instruction: str,
    *,
    runner: Any = None,
    brand: dict | None = None,
) -> dict:
    """Tom -> Maya -> Nabar, in that order. Returns {"ok", "text", "reason"}; never raises.

    Era's code coordinates, not Laura: asked to run all three herself, Laura spent her
    whole step budget before reaching Maya or Nabar (live, 2026-11-19), so each bot gets
    its own turn and its own budget, and a step that fails is reported, not fatal."""
    try:
        if runner is None:
            from crew import runtime

            runner = runtime.run_tool
        facts = facts_block(brand) if brand is not None else ""
        direction, r1 = _ask(
            runner, "tom", _direction_task(brand_key, display_name, instruction, facts), instruction
        )
        copy, r2 = "", ""
        if direction:
            copy, r2 = _ask(
                runner,
                "maya",
                _copy_task(display_name, instruction, direction, facts),
                instruction,
            )
        review, r3 = "", ""
        if copy:
            review, r3 = _ask(runner, "nabar", _review_task(copy), instruction)
            review = _tidy_review(review) if review else review
        parts, missing = [], []
        for label, text, why in (
            ("ANGLE AND DIRECTION (Tom)", direction, r1),
            ("COPY (Maya)", copy, r2 or ("needs Tom's direction" if not direction else "")),
            ("REVIEW (Nabar)", review, r3 or ("needs Maya's copy" if not copy else "")),
        ):
            if text:
                parts.append(f"{label}:\n{text}")
            else:
                missing.append(f"{label.split(' (')[0]} - {why or 'no reply'}")
        if not parts:
            return {"ok": False, "text": "", "reason": "; ".join(missing)[:200]}
        if copy:
            report, failures = check_limits(copy)
            if report:
                parts.append("LIMIT CHECK (measured by code, not by the model):\n" + "\n".join(report))
            if failures:
                parts.append("DO NOT USE (over the limit): " + " | ".join(failures))
        if missing:
            parts.append("NOT PRODUCED: " + "; ".join(missing))
        return {"ok": True, "text": "\n\n".join(parts), "reason": ""}
    except Exception as exc:  # noqa: BLE001 - the crew never blocks a CM job
        return {"ok": False, "text": "", "reason": f"{type(exc).__name__}: {exc}"[:200]}


def render_note(package: dict) -> str:
    """The block the ads brief carries (empty crew result -> an explicit notice)."""
    if package.get("ok") and package.get("text"):
        return (
            "=== CREW CREATIVE PACKAGE (Laura, with Tom, Maya and Nabar) ===\n"
            "Use this as your starting point for angle, copy and the creative brief. Where "
            "Nabar failed a variant, do not use it. Never exceed the field limits in the "
            "skill. It is a suggestion: the ad still reuses an already-published post, and "
            "the campaign is still created PAUSED.\n\n" + package["text"].strip() + "\n\n"
        )
    return (
        "=== CREW DID NOT CONTRIBUTE ===\n"
        f"The crew could not produce a package ({package.get('reason') or 'unavailable'}). "
        "Write the angle and copy yourself, and say in your summary that the crew did not "
        "contribute.\n\n"
    )
