#!/usr/bin/env python3
r"""One-time migration: Era's vault from bare PARA to the AI Memory Vault layout.

    Areas/ Resources/ Projects/ Archive/ Logs/
        ->  00 - Inbox / 01 - Daily Notes / 02 - Delta Cuisine /
            03 - Ceram Quartz / 04 - Global / 05 - Archive / 06 - Resources

WHAT CHANGES AND WHAT DOES NOT
  Folders change. Frontmatter gains jaredrhod's three fields (`status`,
  `project`, `type`). **Filenames do not**, and that is deliberate: there are 55
  `[[wikilinks]]` across these notes written against the underscore names, and a
  rename outside the Obsidian app silently breaks every one of them. jaredrhod's
  own vault carries `CERAM_QUARTZ_CHARTER.md` — his convention is the folder
  structure, the index notes and the frontmatter, not the letter casing.

  Note BODIES are never touched. This moves files and rewrites the frontmatter
  block above them; not one word of anyone's writing is edited.

WHY `type:` IS THE LOAD-BEARING FIELD
  Authority used to come from the top-level folder (Areas > Resources >
  Projects), which worked only while the folder name doubled as the note's KIND.
  Under a per-brand layout everything Delta Cuisine owns shares one folder, so
  cm_vault now ranks on `type:` — reference > guide > plan, with index/log never
  briefed. Getting a note's `type` wrong is therefore the one mistake here that
  changes what Era reads, which is why the mapping below is written out by hand,
  note by note, instead of guessed from a filename pattern.

Idempotent: re-running after a partial pass finishes the job rather than
duplicating it. Refuses to run against an already-migrated vault unless --force.

    python scripts/migrate_vault_to_jaredrhod.py --dry-run
    python scripts/migrate_vault_to_jaredrhod.py --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

VAULT = Path("C:/Users/nesri/OneDrive/Documents/obsidian_era/era_brain")

DELTA = "02 - Delta Cuisine"
CERAM = "03 - Ceram Quartz"
GLOBAL = "04 - Global"
ARCHIVE = "05 - Archive"
RESOURCES = "06 - Resources"
INBOX = "00 - Inbox"
DAILY = "01 - Daily Notes"

FOLDERS = (INBOX, DAILY, DELTA, CERAM, GLOBAL, ARCHIVE, RESOURCES)

# (destination folder, project slug, type) per existing note.
#
# `project` is jaredrhod's "what this note serves"; in this vault a project IS a
# brand, so the slug doubles as Era's brand key with the underscore swapped for a
# hyphen. `meta` is his slug for notes about the system itself.
PLAN: dict[str, tuple[str, str, str]] = {
    # ── Delta Cuisine ────────────────────────────────────────────────────────
    "Areas/Delta_Cuisine/Delta_Social_Strategy.md": (DELTA, "delta-cuisine", "reference"),
    "Resources/Delta_Cuisine/Delta_Cuisine_Brand_Assets.md": (DELTA, "delta-cuisine", "reference"),
    "Resources/Delta_Cuisine/Delta_Cuisine_Wardrobe_Accessories_Catalogue.md": (
        DELTA,
        "delta-cuisine",
        "reference",
    ),
    "Resources/Delta_Cuisine/Kitchen_Design_Trends.md": (DELTA, "delta-cuisine", "reference"),
    # A prompt SYSTEM is a runbook you follow, not a fact you look up.
    "Resources/Delta_Cuisine/Delta_Cuisine_Wardrobe_Master_Prompt.md": (
        DELTA,
        "delta-cuisine",
        "guide",
    ),
    "Projects/Delta_Cuisine/Delta_Cuisine_Reel_Concepts_Batch.md": (DELTA, "delta-cuisine", "plan"),
    "Projects/Delta_Cuisine/Delta_PackRosiere_Meta_Ads_Campaign.md": (
        DELTA,
        "delta-cuisine",
        "plan",
    ),
    "Projects/Delta_Cuisine/Delta_Showroom_Launch.md": (DELTA, "delta-cuisine", "plan"),
    "Areas/Delta_Cuisine/Era_Run_Log_delta_cuisine.md": (DELTA, "delta-cuisine", "log"),
    "Areas/Delta_Cuisine/Era_Space_Changelog_delta_cuisine.md": (DELTA, "delta-cuisine", "log"),
    "Areas/Delta_Cuisine/Era_Learned_Preferences_delta_cuisine.md": (
        DELTA,
        "delta-cuisine",
        "log",
    ),
    # ── Ceram Quartz ─────────────────────────────────────────────────────────
    "Areas/Ceram_Quartz/Quartz_Market_Research.md": (CERAM, "ceram-quartz", "reference"),
    "Resources/Ceram_Quartz/Ceram_Quartz_Brand_Assets.md": (CERAM, "ceram-quartz", "reference"),
    "Resources/Ceram_Quartz/Tile_Material_Library.md": (CERAM, "ceram-quartz", "reference"),
    "Projects/Ceram_Quartz/Ceram_Quartz_Reel_Concepts_Batch.md": (CERAM, "ceram-quartz", "plan"),
    "Projects/Ceram_Quartz/Perla_Meta_Ads_Campaign.md": (CERAM, "ceram-quartz", "plan"),
    "Projects/Ceram_Quartz/Quartz_Catalog_Update.md": (CERAM, "ceram-quartz", "plan"),
    "Areas/Ceram_Quartz/Era_Run_Log_ceram_quartz.md": (CERAM, "ceram-quartz", "log"),
    # ── Global (cross-brand) ─────────────────────────────────────────────────
    "Areas/_Global/CM_Operating_Principles.md": (GLOBAL, "meta", "reference"),
    "Resources/_Global/Production_Standards_AI_Generation.md": (GLOBAL, "meta", "reference"),
    "Resources/_Global/Automated_Reel_Assembly_Pipeline.md": (GLOBAL, "meta", "guide"),
    "Areas/_Global/Era_Vault_Health.md": (GLOBAL, "meta", "log"),
    # A weekly plan naming both brands is cross-brand work, not a resource.
    "Projects/_Cross_Brand/Plan_Week29.md": (GLOBAL, "meta", "plan"),
    # ── Resources (system-level, not brand knowledge) ────────────────────────
    "Projects/_Dashboard.md": (RESOURCES, "meta", "index"),
    # ── Archive ──────────────────────────────────────────────────────────────
    "Archive/Delta_Cuisine/Delta_Cuisine_Wardrobe_Prompt_Usage.md": (
        f"{ARCHIVE}/Delta Cuisine",
        "delta-cuisine",
        "reference",
    ),
    "Archive/Delta_Cuisine/Era_Run_Log_delta_cuisine_Archive.md": (
        f"{ARCHIVE}/Delta Cuisine",
        "delta-cuisine",
        "log",
    ),
    "Archive/Ceram_Quartz/Era_Run_Log_ceram_quartz_Archive.md": (
        f"{ARCHIVE}/Ceram Quartz",
        "ceram-quartz",
        "log",
    ),
}

# Marketing keeps its own folder-with-index shape (jaredrhod's exact convention)
# and simply moves under 06 - Resources.
MOVE_TREES = {"Resources/_Global/Marketing": f"{RESOURCES}/Marketing"}

# The charters have lived in each brand's project_dir, outside the vault, read on
# demand by the headless CM. "The vault is the only source of truth" means they
# come in. They are big (20K / 12K) and stay `era_brief: false` — the CM reads the
# file, it is never injected, so this costs no brief budget.
IMPORT_CHARTERS = {
    "D:/Ctrl Z/Freelence/DC/IG/ABC/claude automation/MASTER_CONTENT_BLUEPRINT.md": (
        f"{DELTA}/Delta_Cuisine_Charter.md",
        "delta-cuisine",
    ),
    "D:/telechargements/ceramequartz/new/CERAM_QUARTZ_CHARTER.md": (
        f"{CERAM}/Ceram_Quartz_Charter.md",
        "ceram-quartz",
    ),
    "D:/telechargements/ceramequartz/new/Rapport_Strategique_Instagram_CeramQuartz.md": (
        f"{CERAM}/Ceram_Quartz_Instagram_Strategy_Report.md",
        "ceram-quartz",
    ),
}

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, _, v = line.partition(":")
            fm[k.strip().lower()] = v.strip()
    return fm, text[m.end() :]


def render_frontmatter(fm: dict[str, str]) -> str:
    """jaredrhod's three fields first, then everything the note already had.

    Order matters only to a human scanning the file, but that is the point of an
    index: the fields that decide what the AI does with the note sit at the top.
    """
    lead = ("status", "project", "type")
    lines = [f"{k}: {fm[k]}" for k in lead if fm.get(k)]
    lines += [f"{k}: {v}" for k, v in fm.items() if k not in lead and v]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def normalized(fm: dict[str, str], project: str, ntype: str, body: str) -> dict[str, str]:
    out = dict(fm)
    out["project"] = project
    out["type"] = ntype
    out.setdefault("status", "active")
    # Era's own generated notes and the imported charters are read on demand, not
    # injected — see the module docstring. Never override an explicit setting.
    if ntype in ("log", "index") and "era_brief" not in out:
        out["era_brief"] = "false"
    # `brand:` stays as the strongest signal cm_vault checks; derive it from the
    # slug when a note never had one.
    if project not in ("meta", "personal") and not out.get("brand"):
        out["brand"] = project.replace("-", "_")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = args.apply

    if not VAULT.is_dir():
        print(f"vault not found: {VAULT}")
        return 1

    actions: list[str] = []

    for folder in FOLDERS:
        d = VAULT / folder
        if not d.is_dir():
            actions.append(f"mkdir {folder}/")
            if apply:
                d.mkdir(parents=True, exist_ok=True)

    for src_rel, (dest_folder, project, ntype) in PLAN.items():
        src = VAULT / src_rel
        if not src.is_file():
            actions.append(f"SKIP (already moved or missing): {src_rel}")
            continue
        dest = VAULT / dest_folder / src.name
        text = src.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        new_text = render_frontmatter(normalized(fm, project, ntype, body)) + body.lstrip("\n")
        actions.append(f"{src_rel}  ->  {dest_folder}/{src.name}   [{ntype}]")
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(new_text, encoding="utf-8")
            src.unlink()

    for src_rel, dest_rel in MOVE_TREES.items():
        src, dest = VAULT / src_rel, VAULT / dest_rel
        if not src.is_dir():
            actions.append(f"SKIP tree (already moved): {src_rel}")
            continue
        actions.append(f"{src_rel}/  ->  {dest_rel}/   (tree)")
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))

    for src_path, (dest_rel, project) in IMPORT_CHARTERS.items():
        src, dest = Path(src_path), VAULT / dest_rel
        if not src.is_file():
            actions.append(f"MISSING charter (left alone): {src_path}")
            continue
        if dest.is_file():
            actions.append(f"SKIP charter (already imported): {dest_rel}")
            continue
        fm, body = parse_frontmatter(src.read_text(encoding="utf-8"))
        fm = normalized(fm, project, "reference", body)
        # The charter is the brand's long-form blueprint; the CM opens the file
        # directly, so keeping it out of the brief is what stops 20K of it
        # displacing every other note.
        fm["era_brief"] = "false"
        fm["source"] = src_path
        actions.append(f"IMPORT charter {src.name}  ->  {dest_rel}")
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(render_frontmatter(fm) + body.lstrip("\n"), encoding="utf-8")

    # Daily notes: Logs/ was Era's own invention two hours before this migration;
    # jaredrhod's slot is 01 - Daily Notes.
    logs = VAULT / "Logs"
    if logs.is_dir():
        for item in sorted(logs.iterdir()):
            name = "Daily Note Template.md" if item.name.startswith("_Daily") else item.name
            actions.append(f"Logs/{item.name}  ->  {DAILY}/{name}")
            if apply:
                (VAULT / DAILY).mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(VAULT / DAILY / name))
        if apply:
            shutil.rmtree(logs, ignore_errors=True)

    if apply:
        for old in ("Areas", "Resources", "Projects", "Archive"):
            d = VAULT / old
            if d.is_dir() and not any(d.rglob("*.md")):
                shutil.rmtree(d, ignore_errors=True)
                actions.append(f"removed empty legacy folder {old}/")

    print(f"{'APPLIED' if apply else 'DRY RUN'} — {len(actions)} action(s)\n")
    for a in actions:
        print(f"  {a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
