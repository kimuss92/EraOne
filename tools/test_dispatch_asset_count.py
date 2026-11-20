"""_count_recent_files must see new renders even when the folder holds a huge
node_modules tree (a Remotion project lives inside each brand's master folder)."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import era_dispatch as ed  # noqa: E402


def _touch_many(folder: Path, count: int) -> None:
    folder.mkdir(parents=True)
    for i in range(count):
        (folder / f"f{i}.js").write_text("x")


def test_node_modules_does_not_hide_new_renders(tmp_path):
    # 'a_remotion' sorts before 'renders', so an unpruned walk burns the cap on it.
    _touch_many(tmp_path / "a_remotion" / "node_modules" / "pkg", 60)
    since = time.time()
    time.sleep(0.05)
    (tmp_path / "renders").mkdir()
    (tmp_path / "renders" / "reel.mp4").write_bytes(b"video")
    assert ed._count_recent_files(str(tmp_path), since, cap=50) == 1


def test_counts_recent_files_and_ignores_old_ones(tmp_path):
    old = tmp_path / "old.png"
    old.write_bytes(b"x")
    time.sleep(0.05)
    since = time.time()
    time.sleep(0.05)
    (tmp_path / "new.png").write_bytes(b"x")
    assert ed._count_recent_files(str(tmp_path), since) == 1


def test_missing_root_returns_zero(tmp_path):
    assert ed._count_recent_files(str(tmp_path / "nope"), 0) == 0
