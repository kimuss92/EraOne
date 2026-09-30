# cleanup_delete.py — destructive delete with confirmation gate
from pathlib import Path

PLACEHOLDERS = {"no", "known", "nothing", "observation", "output"}
PROTECTED_NAMES = {"__init__.py", ".gitkeep"}  # never touched, whatever their content


def find_targets(root=Path(".")):
    targets = []
    for p in root.iterdir():
        if not p.is_file() or p.name in PROTECTED_NAMES or p.name.startswith((".", "_")):
            continue
        if p.stat().st_size == 0:
            continue  # empty placeholder files are always preserved
        try:
            text = p.read_text(encoding="utf-8", errors="ignore").strip().lower()
            if text in PLACEHOLDERS or (len(text) < 30 and text in PLACEHOLDERS):
                targets.append(p)
        except Exception:
            pass
    return targets


def delete_with_confirm(targets, auto_confirm=False):
    if not targets:
        return 0
    if not auto_confirm:
        print(f"Would delete {len(targets)}: {', '.join(str(t) for t in targets)}")
        response = input("Confirm delete? [y/N]: ").strip().lower()
        if response not in ("y", "yes"):
            print("Canceled (safety gate).")
            return 0
    deleted = 0
    for t in targets:
        if t.name in PROTECTED_NAMES:
            print(f"Protected, not touched: {t}")
            continue  # guard for callers passing their own target list
        t.unlink()
        deleted += 1
        print(f"Deleted: {t}")
    return deleted  # what was actually removed, not what was asked for
