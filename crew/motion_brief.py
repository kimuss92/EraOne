"""The Motion Design brief: the fixed-format note that carries a storyboard from the crew
(Tom) to a Claude Code session running the `motion-design` skill.

WHY A MODULE AND NOT THE MODEL'S OWN WORDS
  The brief must always carry the same things: the brand's logo files, the texture files, the
  vertical format, and the owner's text rule (2026-11-20: on-screen text is FRENCH or NONE, he
  adds it later, and the build must ASK him before it starts). A small model forgets those; this
  code never does. Tom supplies only what is creative (shots, ending, textures, reference notes).

WHAT IT DOES NOT DO
  It never runs the build, renders anything or says a video exists. A brief is a proposal that
  waits for the owner's approval of the storyboard.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_ARABIC = re.compile(r"[؀-ۿ]")
_TEXT_MODES = ("ask", "french", "none")

RULES = (
    "TEXT RULE (owner): on-screen text is FRENCH or NONE (Karim adds it later). Never Arabic, "
    "never English. Before ANY work the build asks Karim which one, and waits.",
    "FORMAT: vertical 1080x1920 (set W=1080, H=1920 in the page; the template default is landscape).",
    "LOGOS: use only the files listed under Logos, never redraw or crop one. Transparent version "
    "over footage or dark/colour backgrounds; with-background version on its own matching card. "
    "A huge logo (Ceram: 11811 px) is never loaded raw: make a copy of at most 2000 px in assets/ first.",
    "TEXTURES: use the files listed, honestly: a 410x819 px texture looks soft past about 820 px "
    "tall; keep it a hero panel or blur it by depth of field, never a macro close-up.",
    "REFERENCE: inspiration only. Reuse the structure and pacing, never another brand's logo, "
    "name, text or footage.",
    "HONESTY: never invent a price, offer, claim or slab name. Nothing is a video until it is rendered.",
)


def motion_config(brand_key: str) -> dict:
    """brands.yaml -> brands.<key>.motion (logos, textures_dir, text policy). {} if absent."""
    import yaml

    try:
        cfg = yaml.safe_load((ROOT / "brands.yaml").read_text(encoding="utf-8")) or {}
        return dict((cfg.get("brands") or {}).get(brand_key, {}).get("motion") or {})
    except Exception:  # noqa: BLE001 - a missing config means a brief with a warning, not a crash
        return {}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "reel"


def resolve_textures(names: list[str], textures_dir: str | None) -> list[dict]:
    """[{name, path, exists, size}] — size is read with PIL when available (a low-resolution
    texture is reported so the build can plan around it)."""
    out = []
    base = Path(textures_dir) if textures_dir else None
    files = {}
    if base and base.is_dir():
        for p in base.iterdir():
            files[re.sub(r"\.(jpe?g|png|webp)$", "", p.name, flags=re.I).lower().strip()] = p
    for raw in names or []:
        key = re.sub(r"\.(jpe?g|png|webp)$", "", str(raw), flags=re.I).lower().strip()
        p = files.get(key)
        entry = {"name": str(raw), "path": str(p) if p else "", "exists": bool(p), "size": ""}
        if p:
            try:
                from PIL import Image

                with Image.open(p) as im:
                    entry["size"] = f"{im.size[0]}x{im.size[1]}"
            except Exception:  # noqa: BLE001
                pass
        out.append(entry)
    return out


def render_brief(
    brand_key: str,
    title: str,
    shots: list[str],
    ending: str,
    *,
    length_s: Any = "",
    textures: list[str] | None = None,
    reference_url: str = "",
    reference_notes: str = "",
    notes: str = "",
    motion: dict | None = None,
) -> str:
    """The note body (the vault adds its own frontmatter). Pure: no writes, no network."""
    m = motion if motion is not None else motion_config(brand_key)
    logos = (m.get("logos") or {}) if m else {}
    tex = resolve_textures(textures or [], (m or {}).get("textures_dir"))
    lines = [
        "## Machine fields (the skill reads these)",
        "motion_brief: true",
        f"brand: {brand_key}",
        "text_mode: ask",
        "storyboard_approved: pending",
        f"format: {(m or {}).get('format', '1080x1920')}",
        f"length_s: {length_s or 'about 16'}",
        "",
        "## Start (what Karim says in Claude Code)",
        f'start motion design from this brief: "{title}" (vault Inbox)',
        "",
        "## Rules that always apply",
        *[f"- {r}" for r in RULES],
        "",
        "## Text decision (ASK FIRST)",
        "Before any work ask Karim ONE question: on-screen text in French, or no text at all "
        "(he adds it later)? Wait for the answer. If French, show the French lines for his "
        "approval before building. If none, build with no text at all (the logo image stays).",
        "",
        "## Logos",
    ]
    if logos:
        lines += [f"- {k}: {v}" for k, v in logos.items()]
    else:
        lines.append("- NOT CONFIGURED for this brand: ask Karim for the logo files before building.")
    if (m or {}).get("style"):
        lines += ["", "## Brand motion style (this overrides any older Ceram Quartz look)", f"- Look: {m['style']}"]
        if m.get("avoid"):
            lines.append(f"- AVOID: {m['avoid']}")
        if m.get("background_rule"):
            lines.append(f"- BACKGROUND: {m['background_rule']}")
        if m.get("animation_reference"):
            lines.append(f"- Animation reference (motion only, not its colours): {m['animation_reference']}")
        if m.get("template_dir"):
            lines.append(f"- START FROM the brand template folder: {m['template_dir']} (it holds the ANIMATION skeleton only: copy it, then re-theme its background and slab look to the rules above before anything else, and change the textures and the text)")
        if m.get("reference_url"):
            lines.append(f"- Style reference: {m['reference_url']}")
    lines += ["", "## Textures"]
    if tex:
        for t in tex:
            tag = t["path"] or "NOT FOUND in the textures folder"
            lines.append(f"- {t['name']}: {tag}" + (f" ({t['size']} px)" if t["size"] else ""))
    else:
        lines.append("- none chosen: ask Karim which textures to use.")
    lines += ["", "## Reference"]
    if reference_url or reference_notes:
        if reference_url:
            lines.append(f"- link: {reference_url}")
        if reference_notes:
            lines += ["- what to take from it (structure and pacing only):", f"  {reference_notes.strip()}"]
    else:
        lines.append(f"- none. If Karim sends a link, run `python {ROOT / 'scripts' / 'motion_reference.py'} <link>` and look at the sheet.")
    lines += ["", "## Storyboard (numbered shots; Karim approves before anything is built)"]
    lines += [f"{i}. {str(s).strip()}" for i, s in enumerate(shots or [], 1)] or ["(no shots given)"]
    lines += ["", "## Ending", str(ending or "(none given)").strip(), ""]
    if notes:
        lines += ["## Notes", str(notes).strip(), ""]
    return "\n".join(lines)


def validate_brief(text: str) -> list[str]:
    """Problems with a brief; [] means it is complete and follows the owner's rules."""
    problems = []
    t = str(text or "")
    for field in ("motion_brief: true", "text_mode: ask", "storyboard_approved:", "format: 1080x1920"):
        if field not in t:
            problems.append(f"missing field: {field}")
    for heading in ("## Rules that always apply", "## Text decision (ASK FIRST)", "## Logos",
                    "## Textures", "## Reference", "## Storyboard", "## Ending"):
        if heading not in t:
            problems.append(f"missing section: {heading}")
    if "ONE question" not in t or "French" not in t:
        problems.append("the ask-first French-or-no-text rule is missing")
    board = t.split("## Storyboard", 1)[-1].split("## Ending", 1)[0]
    if _ARABIC.search(board):
        problems.append("Arabic text in the storyboard: on-screen text is French or none")
    if motion_config_style_missing(t):
        problems.append("the brand motion style section is missing")
    if "NOT CONFIGURED" in t:
        problems.append("logos are not configured for this brand")
    if "NOT FOUND" in t:
        problems.append("a requested texture was not found")
    if not re.search(r"^1\. ", board, re.M):
        problems.append("the storyboard has no numbered shots")
    return problems


# The skill's own direction rules (references/style-rules.md), checked mechanically so a weak
# storyboard is flagged before Karim sees it. Warnings, not blocks: he decides.
_REVERSALS = (
    (r"zoom(?:s|ing)?\s+in", r"zoom(?:s|ing)?\s+out", "zooms in AND out (direction must never reverse)"),
    (r"scale(?:s|d)?\s+up", r"scale(?:s|d)?\s+(?:down|out)|shrink", "scales up AND down (no reversal)"),
    (r"push(?:es|ing)?\s+in", r"pull(?:s|ing)?\s+(?:back|out)|zoom(?:s|ing)?\s+out", "pushes in AND pulls back"),
)
_BANNED = (
    (r"cross[- ]?dissolve|crossfade|dissolve", "cross dissolve between shots (shots must transform into each other)"),
    (r"bounce|bouncy|pop(?:s|ping)?\s+in|spring", "bounce or pop (text must fade up from blur, no bounces)"),
    (r"flash|strobe", "flash or strobe"),
    (r"kaleidoscope", "kaleidoscope effect"),
)


def style_warnings(shots: list[str]) -> list[str]:
    """Direction-rule problems in the shots' text. [] means none found."""
    board = " ".join(str(s) for s in shots or []).lower()
    out = []
    for first, second, why in _REVERSALS:
        if re.search(first, board) and re.search(second, board):
            out.append(why)
    for pattern, why in _BANNED:
        if re.search(pattern, board):
            out.append(why)
    if shots and not re.search(r"transform|becomes?|match[- ]?cut|morph|turns? into|continues?", board):
        out.append("no shot says how it becomes the next (every shot must transform into the next)")
    return out


def motion_config_style_missing(text: str) -> bool:
    """True when the brand has a configured motion style but this brief does not carry it."""
    m = re.search(r"^brand:\s*(\S+)", str(text), re.M)
    cfg = motion_config(m.group(1)) if m else {}
    return bool(cfg.get("style")) and "## Brand motion style" not in str(text)


def sentence_for(title: str) -> str:
    return f'start motion design from this brief: "{title}" (vault Inbox)'


# ── launcher support (scripts/start_motion_design.ps1) ───────────────────────


def _brand_cfg(brand_key: str) -> dict:
    import yaml

    cfg = yaml.safe_load((ROOT / "brands.yaml").read_text(encoding="utf-8")) or {}
    return (cfg.get("brands") or {}).get(brand_key) or {}


def launch_info(brand_key: str = "", brief_title: str = "") -> dict:
    """What the one-click launcher needs: where to start Claude Code, which folders to allow, and
    the exact first message for the newest (or the named) motion brief. Never raises: a problem is
    returned as {"ok": False, "error": ...} so the launcher can say it in plain words."""
    try:
        from core import cm_vault

        inbox = Path(cm_vault.VAULT_DIR) / "00 - Inbox"
        briefs = sorted(inbox.glob("Motion_brief_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        want = brief_title.strip().lower().replace("_", " ")
        chosen, chosen_brand = None, ""
        for p in briefs:
            head = p.read_text(encoding="utf-8", errors="replace")[:4000]
            m = re.search(r"^brand:\s*(\S+)", head, re.M)
            b = m.group(1) if m else ""
            title = p.stem.replace("_", " ")
            if want and want not in title.lower():
                continue
            if brand_key and b != brand_key:
                continue
            chosen, chosen_brand = p, b
            break
        if not chosen:
            return {"ok": False, "error": "No motion brief found" + (f" for {brand_key}" if brand_key else "")
                    + (f" matching '{brief_title}'" if brief_title else "")
                    + f" in {inbox}. Ask Era: 'ask Tom to propose a motion design', then 'save it'."}
        cfg = _brand_cfg(chosen_brand)
        motion = cfg.get("motion") or {}
        project_dir = str(cfg.get("project_dir") or "")
        add_dirs = [str(Path(cm_vault.VAULT_DIR))]
        for key in ("textures_dir", "references_dir"):
            if motion.get(key):
                add_dirs.append(str(motion[key]))
        for p in (motion.get("logos") or {}).values():
            parent = str(Path(p).parent)
            if parent not in add_dirs:
                add_dirs.append(parent)
        add_dirs = [d for i, d in enumerate(add_dirs) if d and d not in add_dirs[:i]]
        title = chosen.stem.replace("_", " ")
        return {
            "ok": True,
            "brand": chosen_brand,
            "brief_path": str(chosen),
            "title": title,
            "sentence": sentence_for(title),
            "project_dir": project_dir,
            "project_dir_exists": bool(project_dir) and Path(project_dir).is_dir(),
            "add_dirs": add_dirs,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


if __name__ == "__main__":  # python -m crew.motion_brief launch-info [brand] [title]
    import json
    import sys

    args = sys.argv[1:]
    if args and args[0] == "launch-info":
        print(json.dumps(launch_info(*(args[1:3] + [""] * 2)[:2]), ensure_ascii=False))
    else:
        print("usage: python -m crew.motion_brief launch-info [brand_key] [title]")
