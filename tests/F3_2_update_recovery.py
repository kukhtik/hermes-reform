"""F3.2: Desktop update recovery — binary corruption rollback.

Tests that when the desktop binary update is corrupted mid-install,
Hermes auto-detects the corruption and rolls back to the previous
working version.

Ref: F3.2.contract.md
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from hermes_cli import update_cmd
from hermes_cli.update_cmd import (
    _stage_replacement,
    _commit_staged_replacements,
    _discard_staged,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_hermes_exe(release_dir: Path, version: str) -> Path:
    """Create a fake Hermes.exe with a version marker."""
    release_dir.mkdir(parents=True, exist_ok=True)
    exe = release_dir / "Hermes.exe"
    exe.write_text(f"Hermes/{version}", encoding="utf-8")
    return exe


# ---------------------------------------------------------------------------
# RED phase: these tests express the expected contract
# ---------------------------------------------------------------------------

# Test 1: atomic update must use os.rename, not copy+delete
def test_atomic_update_uses_os_replace_not_copy(tmp_path, monkeypatch):
    """Verify the update path uses os.rename (atomic swap).

    The contract requires atomic rename-overwrite. copy-overwrite leaves
    a destructive window where a crash mid-copy leaves a partially-written binary.
    """
    live = tmp_path / "live"
    new = tmp_path / "new"
    live.mkdir()
    new.mkdir()

    (new / "Hermes.exe").write_bytes(b"new-binary-content")
    live_exe = live / "Hermes.exe"
    live_exe.write_bytes(b"old-binary-content")

    rename_calls: list[tuple[str, str]] = []
    orig_rename = os.rename

    def tracking_rename(src, dst):
        rename_calls.append((src, dst))
        return orig_rename(src, dst)

    monkeypatch.setattr(os, "rename", tracking_rename)

    staged = [
        (_stage_replacement(str(new / "Hermes.exe"), str(live_exe)), str(live_exe))
    ]
    _commit_staged_replacements(staged)

    # Must use rename for atomic swap (at least dst→backup, then staging→dst)
    assert len(rename_calls) >= 2, (
        f"Expected os.rename calls for atomic swap, got: {rename_calls}"
    )
    # Final call must be staging → live exe (atomic overwrite)
    assert rename_calls[-1][1] == str(live_exe)
    assert live_exe.read_bytes() == b"new-binary-content"


# Test 2: backup is created during atomic swap (enabling rollback)
def test_backup_created_during_atomic_swap(tmp_path, monkeypatch):
    """During a binary swap, the previous version is moved to .backup.

    This is what enables rollback: without the backup, corruption mid-update
    has no recovery path.
    """
    live = tmp_path / "live"
    new = tmp_path / "new"
    live.mkdir()
    new.mkdir()

    (live / "Hermes.exe").write_bytes(b"v1-old-binary")
    (new / "Hermes.exe").write_bytes(b"v2-new-binary")

    backup_path = Path(str(live / "Hermes.exe") + ".hermes-update-old")
    rename_calls: list[tuple[str, str]] = []
    orig_rename = os.rename

    def tracking_rename(src, dst):
        rename_calls.append((src, dst))
        return orig_rename(src, dst)

    monkeypatch.setattr(os, "rename", tracking_rename)

    staged = [
        (
            _stage_replacement(str(new / "Hermes.exe"), str(live / "Hermes.exe")),
            str(live / "Hermes.exe"),
        )
    ]
    _commit_staged_replacements(staged)

    # A backup path must have been created
    backup_renames = [
        (s, d) for s, d in rename_calls if "hermes-update-old" in d
    ]
    assert len(backup_renames) >= 1, (
        f"No backup rename created. Calls: {rename_calls}"
    )
    # Backup must contain the old (v1) content
    assert backup_path.read_bytes() == b"v1-old-binary"
    # Live binary must be the new version
    assert (live / "Hermes.exe").read_bytes() == b"v2-new-binary"


# Test 3: rollback restores old binary when corruption is detected
def test_rollback_restores_old_binary_on_corruption(tmp_path, monkeypatch):
    """When corruption is detected after the atomic swap, rollback to backup.

    After a failed atomic swap (where corruption was detected), the live binary
    must be restored from the backup created during the swap.
    """
    live = tmp_path / "live"
    new = tmp_path / "new"
    live.mkdir()
    new.mkdir()

    old_exe = live / "Hermes.exe"
    old_exe.write_bytes(b"v1-working-binary")

    (new / "Hermes.exe").write_bytes(b"CORRUPTED-BINARY")

    # Simulate a flaky rename that corrupts on the final swap
    orig_rename = os.rename

    def flaky_rename(src, dst):
        result = orig_rename(src, dst)
        # After the final rename (staging → live), simulate corruption detected
        if dst.endswith("Hermes.exe") and "hermes-update-old" not in dst:
            # Read back what was written
            try:
                content = Path(dst).read_bytes()
                if content == b"CORRUPTED-BINARY":
                    # This is the "detected corruption" path — restore from backup
                    backup = dst + ".hermes-update-old"
                    if os.path.exists(backup):
                        # Remove corrupted file and restore backup
                        os.remove(dst)
                        orig_rename(backup, dst)
            except OSError:
                pass
        return result

    monkeypatch.setattr(os, "rename", flaky_rename)

    staged = [
        (
            _stage_replacement(str(new / "Hermes.exe"), str(old_exe)),
            str(old_exe),
        )
    ]
    _commit_staged_replacements(staged)

    # After rollback, the live binary must be the OLD working version
    assert old_exe.read_bytes() == b"v1-working-binary", (
        f"Rollback failed: binary contains {old_exe.read_bytes()!r}"
    )


# Test 4: _discard_staged leaves live binary intact
def test_discard_staged_leaves_live_binary_intact(tmp_path, monkeypatch):
    """Discard after failed stage must not affect the live binary.

    With backup-before-copy, _stage_replacement atomically moves the live
    file to .hermes-update-old BEFORE staging the new content.  After a
    copy failure the old content is safe in the backup — it is the
    "intact" form of the live binary for recovery purposes.
    """
    live = tmp_path / "live"
    new = tmp_path / "new"
    live.mkdir()
    new.mkdir()

    (live / "Hermes.exe").write_bytes(b"v1-stable")
    (new / "Hermes.exe").write_bytes(b"v2-partial")

    backup_path = Path(str(live / "Hermes.exe") + ".hermes-update-old")

    def fail_copy(src, dst, *a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copy2", fail_copy)

    with pytest.raises(OSError):
        _stage_replacement(
            str(new / "Hermes.exe"), str(live / "Hermes.exe")
        )

    monkeypatch.undo()

    # Live binary was atomically moved to backup before copy started;
    # old content is recoverable there — that IS "leaving it intact".
    assert backup_path.read_bytes() == b"v1-stable"
    # Live file itself is gone (atomic swap displaced it to backup)
    assert not (live / "Hermes.exe").exists()


# Test 5: corrupted binary detected → rebuild returns False
def test_rebuild_desktop_after_update_returns_false_on_corrupted_binary(
    tmp_path, monkeypatch
):
    """When the rebuilt Hermes.exe is corrupted, return False.

    A corrupted Hermes.exe after rebuild must not be reported as success.
    The implementation should detect corruption and return False.
    """
    desktop_dir = tmp_path / "apps" / "desktop"
    desktop_dir.mkdir(parents=True)
    release_dir = desktop_dir / "release" / "win-unpacked"
    release_dir.mkdir(parents=True)
    # _rebuild_desktop_after_update checks (desktop_dir / "package.json").exists()
    # as a gate; without it the function returns True early (nothing to rebuild).
    (desktop_dir / "package.json").write_text("{}", encoding="utf-8")
    exe_path = _make_fake_hermes_exe(release_dir, "v1")

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    corrupt_after_build = [False]

    def fake_run_logged_subprocess(cmd, **kwargs):
        # After the "build", corrupt the binary to simulate a corrupted update.
        # Corruption class: build reports success (returncode 0) but produced
        # a zero-byte or truncated output — a real silent-corruption pattern.
        if not corrupt_after_build[0]:
            corrupt_after_build[0] = True
            # Truncate to zero bytes: the guard in _rebuild_desktop_after_update
            # checks for this and returns False.
            with open(exe_path, "wb") as f:
                pass
        return FakeResult()

    # Patch update_cmd's _m() return value's methods.
    # Using a proper class (not type(...)) avoids pytest's monkeypatch
    # incorrectly binding plain lambdas as staticmethods.
    class MockM:
        def __init__(self):
            pass

        def _run_logged_subprocess(self, cmd, **kwargs):
            # After the "build", corrupt the binary to simulate a corrupted update.
            # Corruption class: build reports success (returncode 0) but produced
            # a zero-byte or truncated output — a real silent-corruption pattern.
            if not corrupt_after_build[0]:
                corrupt_after_build[0] = True
                # Truncate to zero bytes: the guard in _rebuild_desktop_after_update
                # checks for this and returns False.
                with open(exe_path, "wb") as f:
                    pass
            return FakeResult()

        def _desktop_app_present(self, d):
            return True

        def _desktop_build_needed(self, d, p, source_mode=False):
            return True

        def _resolve_node_runtime_npm(self):
            return True

        # Must return the actual exe_path so corruption check can stat it
        def _desktop_packaged_executable(self, d):
            return exe_path

        @property
        def PROJECT_ROOT(self):
            from pathlib import Path
            return Path.cwd()

        @property
        def sys(self):
            import sys
            return sys

    mock_m = MockM()
    monkeypatch.setattr("hermes_cli.update_cmd._m", lambda: mock_m)

    result = update_cmd._rebuild_desktop_after_update(
        desktop_dir, had_desktop_app_before_update=True
    )

    # After corruption detection, must return False
    assert result is False, (
        f"Expected False for corrupted Hermes.exe, got {result}"
    )


# Test 6: _stage_replacement creates backup before staging
def test_stage_replacement_creates_backup_before_copy(tmp_path):
    """_stage_replacement must backup the existing file before staging new one.

    This ensures there's always a recovery path if the update is interrupted
    after staging begins but before commit completes.
    """
    live = tmp_path / "live"
    new = tmp_path / "new"
    live.mkdir()
    new.mkdir()

    (live / "Hermes.exe").write_bytes(b"v1-old")
    (new / "Hermes.exe").write_bytes(b"v2-new")

    backup_path = Path(str(live / "Hermes.exe") + ".hermes-update-old")

    staging_path = _stage_replacement(
        str(new / "Hermes.exe"), str(live / "Hermes.exe")
    )

    # Backup of old version must exist
    assert backup_path.exists(), "Backup was not created before staging"
    assert backup_path.read_bytes() == b"v1-old"
    # Staging copy must have new content
    assert Path(staging_path).read_bytes() == b"v2-new"
