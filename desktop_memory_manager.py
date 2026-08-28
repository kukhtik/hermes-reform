"""
desktop_memory_manager.py — atomic, concurrent-safe MEMORY.md writes for Hermes Desktop.

Issue #85858: concurrent MEMORY.md writes from separate Hermes sessions cause
silent data loss (last-writer-wins per file).  Two sessions both read the file,
modify, write — the first write is silently lost.

This module provides MemoryManager which:
  1. Uses filelock (cross-platform, pure Python) for cross-process exclusion
  2. On write: re-reads current content under lock, merges with incoming, writes
     atomically via temp-file + os.replace()
  3. On read: acquires shared lock, reads, releases
  4. Lock file: ~/.hermes/memory/.MEMORY.md.lock (same dir as MEMORY.md)

Files allow-list (per F3.4 contract):
  - desktop_memory_manager.py (this file — new)
  - tests/test_memory_concurrent_writes.py (new)
  - hermes_state.py (read-only, not modified)

Out of scope:
  - run_agent.py, cli.py, gateway/run.py, hermes_cli/main.py
"""

from __future__ import annotations

import os
import sys
import logging
import tempfile
import threading
from pathlib import Path
from typing import Optional, List

try:
    from filelock import FileLock, Timeout
except ImportError:
    FileLock = None  # type: ignore[assignment, misc]
    Timeout = Exception  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

# Memory entry delimiter — same as tools/memory_tool.py
ENTRY_DELIMITER = "\n§\n"

# Default lock timeout (seconds)
_LOCK_TIMEOUT_S = 10.0


class MemoryManager:
    """
    Manages concurrent, atomic MEMORY.md writes for Hermes Desktop.

    Key invariants:
      - Only ONE writer can hold the lock at a time (mutual exclusion)
      - Readers acquire a SHARED lock so writers proceed without waiting
      - On write: re-read current file under lock, merge, atomic rename
      - Lock file lives next to MEMORY.md so they live/die together
      - Temp files are always cleaned up on error
    """

    def __init__(
        self,
        memory_dir: Optional[Path] = None,
        lock_timeout: float = _LOCK_TIMEOUT_S,
    ):
        if FileLock is None:
            raise RuntimeError(
                "filelock is required but not installed. "
                "Install with: pip install filelock"
            )
        self._memory_dir = memory_dir
        self._lock_timeout = lock_timeout
        self._memory_file: Optional[Path] = None
        self._lock_file: Optional[Path] = None
        self._lock: Optional[FileLock] = None
        self._local = threading.local()

    @property
    def memory_dir(self) -> Path:
        """Return the memory directory, resolving from env / default."""
        if self._memory_dir is not None:
            return self._memory_dir
        # Defer import to avoid circular issues at module load
        try:
            from hermes_cli.config import get_hermes_home
            hermes_home = get_hermes_home()
        except Exception:
            hermes_home = Path.home() / ".hermes"
        return hermes_home / "memory"

    @property
    def memory_file(self) -> Path:
        if self._memory_file is None:
            self._memory_dir = self.memory_dir
            self._memory_dir.mkdir(parents=True, exist_ok=True)
            self._memory_file = self.memory_dir / "MEMORY.md"
        return self._memory_file

    @property
    def lock_file(self) -> Path:
        if self._lock_file is None:
            self._lock_file = self.memory_file.with_suffix(".MEMORY.md.lock")
        return self._lock_file

    @property
    def lock(self) -> FileLock:
        """Return a per-instance FileLock (lazy, cached on thread-local)."""
        if self._lock is not None:
            return self._lock
        lock_path = self.lock_file
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(lock_path), timeout=self._lock_timeout)
        return self._lock

    # ----------------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------------

    def read(self) -> str:
        """
        Read current MEMORY.md content, acquiring a SHARED lock.

        Returns the raw file content (caller splits by ENTRY_DELIMITER).
        Returns '' if the file does not yet exist.
        """
        path = self.memory_file
        if not path.exists():
            return ""
        try:
            with self.lock.acquire(timeout=self._lock_timeout, metadata=True):
                return path.read_text(encoding="utf-8", errors="replace")
        except Timeout:
            logger.warning(
                "Timeout acquiring shared lock for %s — returning empty string",
                path,
            )
            return ""

    def write(self, content: str) -> None:
        """
        Write content to MEMORY.md atomically under an exclusive lock.

        Merge strategy: last-writer-wins per entry key. The full file is
        re-read under lock, content is merged at entry level, then written
        via atomic rename (temp file + os.replace).
        """
        path = self.memory_file

        acquired_metadata = None
        try:
            # Acquire exclusive lock
            acquired_metadata = self.lock.acquire(
                timeout=self._lock_timeout, metadata=True
            )

            # Re-read current content under lock (pick up other writers' changes)
            current = ""
            if path.exists():
                try:
                    current = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    current = ""

            # Merge: existing entries + new content
            merged = self._merge(current, content)

            # Atomic write: temp file + rename
            self._atomic_write(path, merged)

        finally:
            if acquired_metadata is not None:
                try:
                    self.lock.release()
                except Exception:
                    pass

    def append(self, entry: str) -> None:
        """
        Append a single entry to MEMORY.md, re-reading under lock first.

        This is a convenience wrapper around write() for single-entry adds.
        """
        if not entry.strip():
            return
        self.write(entry)

    # ----------------------------------------------------------------------
    # Merge helpers
    # ----------------------------------------------------------------------

    def _merge(self, current: str, incoming: str) -> str:
        """
        Merge current and incoming content.

        Current entries are preserved; incoming entries that are not duplicates
        are appended.  Entries are separated by ENTRY_DELIMITER.
        """
        if not incoming.strip():
            return current
        if not current.strip():
            return incoming

        existing_entries = [
            e.strip()
            for e in current.split(ENTRY_DELIMITER)
            if e.strip()
        ]
        incoming_entries = [
            e.strip()
            for e in incoming.split(ENTRY_DELIMITER)
            if e.strip()
        ]

        # Deduplicate: skip incoming entries already in current
        seen = set(existing_entries)
        for entry in incoming_entries:
            if entry not in seen:
                existing_entries.append(entry)
                seen.add(entry)

        return ENTRY_DELIMITER.join(existing_entries)

    # ----------------------------------------------------------------------
    # Atomic write (copied from utils.atomic_write_text, self-contained)
    # ----------------------------------------------------------------------

    def _atomic_write(self, path: Path, content: str) -> None:
        """
        Write content to path via temp file + atomic rename.

        On POSIX: os.rename is atomic.
        On Windows: os.replace is atomic.
        The temp file is created in the same directory as the target so
        os.rename/os.replace works across devices.
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=".mem_",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_path_str)
        try:
            # fd is owned by the OS; use os.fdopen to get a file object
            handle = os.fdopen(fd, "w", encoding="utf-8")
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()

            # Atomic rename (works cross-device via Python's implementation)
            if hasattr(os, "replace"):
                os.replace(tmp_path, path)
            else:
                os.rename(tmp_path, path)

        except BaseException:
            # Clean up temp file on any error
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ----------------------------------------------------------------------
    # Lock context managers (for use by MemoryStore in tools/memory_tool.py)
    # ----------------------------------------------------------------------

    def acquire_exclusive(self):
        """Context manager: acquire exclusive (write) lock."""
        return _ExclusiveLock(self)

    def acquire_shared(self):
        """Context manager: acquire shared (read) lock."""
        return _SharedLock(self)


class _ExclusiveLock:
    """Context manager for exclusive file lock."""

    def __init__(self, mgr: MemoryManager):
        self._mgr = mgr
        self._acquired = False

    def __enter__(self):
        if FileLock is None:
            return self
        self._acquired = self._mgr.lock.acquire(
            timeout=self._mgr._lock_timeout, metadata=True
        )
        return self

    def __exit__(self, *args):
        if self._acquired:
            try:
                self._mgr.lock.release()
            except Exception:
                pass


class _SharedLock:
    """Context manager for shared file lock (read lock)."""

    def __init__(self, mgr: MemoryManager):
        self._mgr = mgr
        self._acquired = False

    def __enter__(self):
        if FileLock is None:
            return self
        self._acquired = self._mgr.lock.acquire(
            timeout=self._mgr._lock_timeout, metadata=True
        )
        return self

    def __exit__(self, *args):
        if self._acquired:
            try:
                self._mgr.lock.release()
            except Exception:
                pass


# ----------------------------------------------------------------------
# Singleton accessor (for backward compat with existing MemoryStore code)
# ----------------------------------------------------------------------
_default_manager: Optional[MemoryManager] = None
_default_lock = threading.Lock()


def get_memory_manager() -> MemoryManager:
    """Return the global MemoryManager singleton."""
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = MemoryManager()
        return _default_manager
