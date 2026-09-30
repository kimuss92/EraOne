"""core.root_file_guard: intercepts the stray writes that drop 0-byte junk
files into the project root.

Every extensionless file that has ever appeared directly in the root came from a
value being mistaken for a path (``dict[str``, ``bool``, ``{'``, ``200``). The
guard redirects those writes to a quarantine directory and logs a stack trace so
the real call site can be found - and, critically, it must never break a
legitimate file operation to do it.

These tests install the real guard (it monkey-patches ``builtins.open``,
``io.open`` and three ``Path`` methods process-wide) against a temporary root,
then restore every patched name. The restoration is the important part: leaking
an installed guard would silently wrap file I/O for the rest of the session.
"""

import builtins
import io
import os
from pathlib import Path

import pytest

import core.root_file_guard as rfg


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """Point the guard's root/log/quarantine at a throwaway tree."""
    root = (tmp_path / "EraOne").resolve()
    root.mkdir()
    monkeypatch.setattr(rfg, "_ROOT", root)
    monkeypatch.setattr(rfg, "_LOG", root / "runtime" / "diagnosis" / "root_file_guard.log")
    monkeypatch.setattr(rfg, "_QUARANTINE", root / "runtime" / "diagnosis" / "quarantine")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def installed(fake_root):
    """Install the guard for real, then put the process back as it was.

    This has to survive the guard ALREADY being installed - another test module
    may import something whose entry point calls install(). An earlier version of
    this fixture flipped ``_installed = False`` and called install() again, which
    rebound ``_orig_path_open`` to the guard's own wrapper; every later Path.open
    then recursed until RecursionError. (Not hypothetical - it took down a
    full-suite run at pytest_sessionfinish.) install() is hardened against that
    now, but the fixture still goes through uninstall()/install() rather than
    poking module state by hand.
    """
    was_installed = rfg._installed
    rfg.uninstall()
    assert rfg.install() is True
    # If this ever trips, the guard is wrapping itself.
    assert rfg._orig_path_open is not rfg._guarded_path_open
    try:
        yield fake_root
    finally:
        rfg.uninstall()
        if was_installed:
            rfg.install()


def _quarantined(root, name):
    return root / "runtime" / "diagnosis" / "quarantine" / name


# ── write-mode detection ─────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["w", "wb", "a", "ab", "x", "r+", "rb+", "w+b"])
def test_write_modes_are_detected(mode):
    assert rfg._is_write_mode(mode) is True


@pytest.mark.parametrize("mode", ["r", "rb", "rt", ""])
def test_read_modes_are_not_write_modes(mode):
    assert rfg._is_write_mode(mode) is False


# ── junk-target detection ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name",
    [
        "dict[str",
        "tuple[bytes",
        "bool",
        "str",
        "None",
        "Path",
        "{",
        "{'",
        "200",
        "33",
        "run",
        "Claude",
    ],
)
def test_real_junk_names_are_recognised(fake_root, name):
    """The exact shapes that have actually landed in the root."""
    assert rfg._is_junk_target(fake_root / name) is True


def test_relative_path_resolves_against_cwd(fake_root):
    """The stray writes are usually relative - `open("bool", "w")` from the root."""
    assert rfg._is_junk_target("bool") is True


@pytest.mark.parametrize("name", ["notes.md", "main.py", "brands.yaml", "api_keys.json"])
def test_legitimate_root_files_have_extensions_and_pass(fake_root, name):
    assert rfg._is_junk_target(fake_root / name) is False


def test_dotfiles_are_left_alone(fake_root):
    """.env / .gitignore are extensionless by basename but entirely legitimate."""
    for name in (".env", ".gitignore", ".python-version"):
        assert rfg._is_junk_target(fake_root / name) is False


def test_extensionless_files_outside_the_root_are_left_alone(fake_root):
    """Only the ROOT holds no legitimate extensionless file; subdirectories and
    other trees are full of them (LICENSE, Makefile, git internals)."""
    (fake_root / "sub").mkdir()
    assert rfg._is_junk_target(fake_root / "sub" / "LICENSE") is False
    assert rfg._is_junk_target(fake_root.parent / "Makefile") is False


def test_malformed_targets_are_not_junk(fake_root):
    """The detector must never raise - it runs inside every open() call."""
    for bad in ("", None, 12345, object(), b"\xff\xfe"):
        assert rfg._is_junk_target(bad) is False


# ── the guard in action ──────────────────────────────────────────────────────
def test_builtin_open_write_is_redirected_to_quarantine(installed):
    with open(installed / "dict[str", "w", encoding="utf-8") as fh:
        fh.write("stray")
    assert not (installed / "dict[str").exists(), "the root stays clean"
    assert _quarantined(installed, "dict[str").read_text(encoding="utf-8") == "stray"


def test_relative_open_from_the_root_is_redirected(installed):
    with open("bool", "w", encoding="utf-8") as fh:
        fh.write("x")
    assert not (installed / "bool").exists()
    assert _quarantined(installed, "bool").exists()


def test_path_write_text_is_redirected(installed):
    (installed / "None").write_text("stray", encoding="utf-8")
    assert not (installed / "None").exists()
    assert _quarantined(installed, "None").read_text(encoding="utf-8") == "stray"


def test_path_write_bytes_is_redirected(installed):
    (installed / "200").write_bytes(b"\x00\x01")
    assert not (installed / "200").exists()
    assert _quarantined(installed, "200").read_bytes() == b"\x00\x01"


def test_path_open_write_is_redirected(installed):
    """pathlib does not route through io.open on 3.14, which is why Path.open is
    patched separately."""
    with (installed / "Claude").open("w", encoding="utf-8") as fh:
        fh.write("stray")
    assert not (installed / "Claude").exists()
    assert _quarantined(installed, "Claude").exists()


def test_the_caller_still_succeeds(installed):
    """A redirect must never surface as an error to a live session."""
    handle = open(installed / "run", "w", encoding="utf-8")
    handle.write("still works")
    handle.close()
    assert handle.closed


# ── legitimate I/O must be untouched ─────────────────────────────────────────
def test_files_with_extensions_write_to_the_root_normally(installed):
    (installed / "notes.md").write_text("real content", encoding="utf-8")
    assert (installed / "notes.md").read_text(encoding="utf-8") == "real content"
    assert not _quarantined(installed, "notes.md").exists()


def test_extensionless_files_in_subdirectories_are_untouched(installed):
    sub = installed / "docs"
    sub.mkdir()
    (sub / "LICENSE").write_text("MIT", encoding="utf-8")
    assert (sub / "LICENSE").read_text(encoding="utf-8") == "MIT"


def test_reading_an_existing_root_file_is_not_redirected(installed):
    """Only create-writes are intercepted; a read of the same name passes through.

    The file is seeded with raw os.open/os.write - the guard patches
    builtins.open, io.open and three Path methods, but never the os module, so
    this puts a real extensionless file in the root without being redirected.
    """
    fd = os.open(str(installed / "legacy"), os.O_CREAT | os.O_WRONLY)
    try:
        os.write(fd, b"kept")
    finally:
        os.close(fd)
    assert (installed / "legacy").exists(), "seeded directly in the root"
    with open(installed / "legacy", encoding="utf-8") as fh:
        assert fh.read() == "kept"


def test_dotfiles_write_to_the_root_normally(installed):
    (installed / ".env").write_text("KEY=1", encoding="utf-8")
    assert (installed / ".env").read_text(encoding="utf-8") == "KEY=1"


# ── the diagnostic trail ─────────────────────────────────────────────────────
def test_a_blocked_write_records_a_stack_trace(installed):
    (installed / "tuple[bytes").write_text("x", encoding="utf-8")
    log = (installed / "runtime" / "diagnosis" / "root_file_guard.log").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "blocked root junk file" in log
    assert "tuple[bytes" in log
    assert "test_root_file_guard.py" in log, "the trace must name the real call site"


def test_repeated_blocks_append_rather_than_overwrite(installed):
    (installed / "bool").write_text("a", encoding="utf-8")
    (installed / "str").write_text("b", encoding="utf-8")
    log = (installed / "runtime" / "diagnosis" / "root_file_guard.log").read_text(
        encoding="utf-8", errors="replace"
    )
    assert log.count("blocked root junk file") == 2


def test_record_returns_a_quarantine_path_and_creates_the_directory(installed):
    dest = rfg._record(str(installed / "weird"))
    assert dest.parent == installed / "runtime" / "diagnosis" / "quarantine"
    assert dest.name == "weird"
    assert dest.parent.is_dir()


# ── install() contract ───────────────────────────────────────────────────────
def test_install_is_idempotent(installed):
    """Every entry point calls it; a second call must not re-wrap the wrappers."""
    wrapped = builtins.open
    assert rfg.install() is True
    assert builtins.open is wrapped


def test_install_over_its_own_patches_does_not_recurse(installed):
    """Forcing `_installed = False` between installs is exactly what the fixture
    used to do. install() must still capture the REAL originals, not its own
    wrappers - otherwise every write below recurses to RecursionError."""
    rfg._installed = False
    assert rfg.install() is True

    assert rfg._orig_builtins_open is not rfg._guarded_open
    assert rfg._orig_io_open is not rfg._guarded_open
    assert rfg._orig_path_open is not rfg._guarded_path_open
    assert rfg._orig_path_write_text is not rfg._guarded_write_text
    assert rfg._orig_path_write_bytes is not rfg._guarded_write_bytes

    with open(installed / "dict[str", "w", encoding="utf-8") as fh:
        fh.write("stray")
    assert _quarantined(installed, "dict[str").read_text(encoding="utf-8") == "stray"

    with (installed / "Claude").open("w", encoding="utf-8") as fh:
        fh.write("stray")
    assert _quarantined(installed, "Claude").read_text(encoding="utf-8") == "stray"

    (installed / "None").write_text("stray", encoding="utf-8")
    assert _quarantined(installed, "None").read_text(encoding="utf-8") == "stray"

    (installed / "200").write_bytes(b"\x00")
    assert _quarantined(installed, "200").read_bytes() == b"\x00"

    # Legitimate I/O still passes straight through.
    (installed / "notes.md").write_text("real", encoding="utf-8")
    assert (installed / "notes.md").read_text(encoding="utf-8") == "real"


def test_uninstall_restores_the_true_originals(installed):
    """The inverse of install(), so a guard aimed at a temporary root never
    leaks into the rest of the session."""
    assert rfg.uninstall() is True
    assert builtins.open is rfg._orig_builtins_open
    assert io.open is rfg._orig_io_open
    assert Path.open is rfg._orig_path_open
    assert Path.write_text is rfg._orig_path_write_text
    assert Path.write_bytes is rfg._orig_path_write_bytes
    assert rfg.uninstall() is False, "a second uninstall is a no-op"

    # With the guard gone the junk write lands in the root untouched.
    (installed / "bool").write_text("x", encoding="utf-8")
    assert (installed / "bool").read_text(encoding="utf-8") == "x"
    assert not _quarantined(installed, "bool").exists()


def test_guard_survives_a_broken_quarantine_directory(installed, monkeypatch):
    """If the guard's own bookkeeping fails it must fall through to the original
    call, never crash the caller."""
    # A FILE where the quarantine DIRECTORY should be: mkdir can never succeed.
    (installed / "notes.md").write_text("blocker", encoding="utf-8")
    monkeypatch.setattr(rfg, "_QUARANTINE", installed / "notes.md" / "impossible")
    monkeypatch.setattr(rfg, "_LOG", installed / "notes.md" / "impossible.log")
    try:
        (installed / "bool").write_text("x", encoding="utf-8")
    except OSError:
        pytest.fail("the guard must not turn a stray write into a crash")
    # Fell through to the original target rather than a quarantine path.
    assert (installed / "bool").read_text(encoding="utf-8") == "x"
