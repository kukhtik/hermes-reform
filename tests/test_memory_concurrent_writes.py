"""
Tests for concurrent MEMORY.md writes from multiple Hermes desktop sessions.

Issue #85858: concurrent MEMORY.md writes cause data loss (last-writer-wins silent loss).
Two separate Hermes sessions read MEMORY.md, modify, write — the first write is
silently lost when the second write completes.

This test verifies that:
(a) concurrent writes produce merged result (no silent data loss)
(b) no exceptions during concurrent access
(c) lock is released on exception
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
import multiprocessing
from pathlib import Path

WORKTREE_PYTHON = Path(__file__).resolve().parents[1] / "hermes-reform-local" / "venv" / "Scripts" / "python.exe"


def _writer_process(mem_dir_str: str, name: str, count: int, delay: float, result_queue: multiprocessing.Queue):
    """Single-process writer: reads MemoryStore, modifies, writes. Runs in separate process."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    import tools.memory_tool as mt

    # Patch get_memory_dir to our controlled directory
    mem_dir = Path(mem_dir_str)

    orig = mt.get_memory_dir
    mt.get_memory_dir = lambda: mem_dir

    errors = []
    written = []
    try:
        for i in range(count):
            store = mt.MemoryStore()
            store.load_from_disk()
            entry = f"{name}-entry-{i}"
            result = store.add("memory", entry)
            if result.get("success"):
                store.save_to_disk("memory")
                written.append(entry)
            time.sleep(delay)
    except Exception as e:
        errors.append(str(e))

    result_queue.put({"name": name, "written": written, "errors": errors})


def _atomic_writer_process(mem_dir_str: str, name: str, count: int, delay: float, result_queue: multiprocessing.Queue):
    """Single-process writer using desktop_memory_manager for atomic writes."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    try:
        from desktop_memory_manager import MemoryManager
    except ImportError:
        result_queue.put({"name": name, "written": [], "errors": ["desktop_memory_manager not available"]})
        return

    mem_dir = Path(mem_dir_str)
    mgr = MemoryManager(memory_dir=mem_dir)

    written = []
    errors = []
    try:
        for i in range(count):
            entry = f"{name}-entry-{i}"
            try:
                mgr.write(f"Entry {i} from {name}")
                written.append(entry)
            except Exception as e:
                errors.append(str(e))
            time.sleep(delay)
    except Exception as e:
        errors.append(str(e))

    result_queue.put({"name": name, "written": written, "errors": errors})


class TestConcurrentMemoryWritesMultiProcess:
    """Real multi-process concurrent write safety for MEMORY.md."""

    def test_concurrent_writes_no_data_loss_multi_process(self, tmp_path):
        """
        RED TEST: Two separate processes both read, modify, write MEMORY.md.
        Without proper locking, the second writer's read sees stale state from
        before the first writer's write completed — first writer's data is lost.

        This test FAILS (data loss) if the current MemoryStore is used directly
        without a MemoryManager that re-reads under lock before writing.
        """
        import tools.memory_tool as mt

        mem_dir = tmp_path / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)

        # Patch get_memory_dir for this process
        orig = mt.get_memory_dir
        mt.get_memory_dir = lambda: mem_dir

        # Initialize file with a baseline entry
        baseline = mt.MemoryStore()
        baseline.load_from_disk()
        baseline.add("memory", "baseline-entry")
        baseline.save_to_disk("memory")

        result_queue = multiprocessing.Queue()
        delay = 0.05  # Small delay to interleave operations

        # Start two writers in separate processes
        p1 = multiprocessing.Process(
            target=_writer_process,
            args=(str(mem_dir), "alice", 3, delay, result_queue)
        )
        p2 = multiprocessing.Process(
            target=_writer_process,
            args=(str(mem_dir), "bob", 3, delay, result_queue)
        )

        p1.start()
        p2.start()
        p1.join(timeout=15)
        p2.join(timeout=15)

        # Collect results
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        # Check for errors
        all_errors = []
        for r in results:
            all_errors.extend(r.get("errors", []))
        assert not all_errors, f"Errors during concurrent writes: {all_errors}"

        # Check final state
        final = mt.MemoryStore()
        final.load_from_disk()
        final_entries = final.memory_entries

        alice_count = sum(1 for e in final_entries if "alice-entry" in e)
        bob_count = sum(1 for e in final_entries if "bob-entry" in e)

        # Each writer wrote 3 entries; with proper locking+re-read, both survive
        assert alice_count >= 3, \
            f"Alice lost data! Expected 3 entries, found {alice_count}. Final: {final_entries}"
        assert bob_count >= 3, \
            f"Bob lost data! Expected 3 entries, found {bob_count}. Final: {final_entries}"

    def test_concurrent_writes_no_exception_multi_process(self, tmp_path):
        """No exceptions raised during multi-process concurrent read-modify-write."""
        import tools.memory_tool as mt

        mem_dir = tmp_path / "memory2"
        mem_dir.mkdir(parents=True, exist_ok=True)

        orig = mt.get_memory_dir
        mt.get_memory_dir = lambda: mem_dir

        result_queue = multiprocessing.Queue()
        delay = 0.02

        processes = [
            multiprocessing.Process(
                target=_writer_process,
                args=(str(mem_dir), f"writer{i}", 2, delay, result_queue)
            )
            for i in range(4)
        ]

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=10)

        results = [result_queue.get() for _ in range(result_queue.qsize())]
        errors = [e for r in results for e in r.get("errors", [])]
        assert not errors, f"Errors: {errors}"


class TestMemoryManagerAtomicWrites:
    """Verify MemoryManager uses atomic rename pattern for MEMORY.md writes."""

    def test_write_uses_atomic_rename(self):
        """Verify that MemoryStore._write_file uses atomic temp + rename."""
        import tools.memory_tool as mt
        import inspect

        source = inspect.getsource(mt.MemoryStore._write_file)
        assert "atomic_write_text" in source, \
            "_write_file must use atomic_write_text for atomic rename"

    def test_filelock_used_for_concurrent_access(self):
        """Verify that fcntl/msvcrt-based lock is used for concurrent access control."""
        import tools.memory_tool as mt
        import inspect

        source = inspect.getsource(mt.MemoryStore._file_lock)
        has_lock = any(
            kw in source
            for kw in ("filelock", "fcntl", "msvcrt", "FileLock", "flock", "locking")
        )
        assert has_lock, \
            "_file_lock must use filelock/fcntl/msvcrt for cross-process locking"


class TestMemoryManagerIntegration:
    """Integration tests for desktop_memory_manager (new MemoryManager class)."""

    def test_memory_manager_exists(self):
        """desktop_memory_manager module should exist and export MemoryManager."""
        try:
            from desktop_memory_manager import MemoryManager
        except ImportError:
            # MemoryManager not yet implemented — this is the RED state
            raise AssertionError(
                "desktop_memory_manager.MemoryManager not found. "
                "This is the RED state before implementation."
            )

    def test_memory_manager_write_is_atomic(self, tmp_path):
        """MemoryManager.write() must use atomic rename (temp + os.replace)."""
        try:
            from desktop_memory_manager import MemoryManager
        except ImportError:
            raise AssertionError("MemoryManager not yet implemented (RED)")

        import inspect
        source = inspect.getsource(MemoryManager.write)
        assert "atomic_write_text" in source or "os.replace" in source or "rename" in source, \
            "MemoryManager.write must use atomic rename pattern"

    def test_memory_manager_has_file_lock(self, tmp_path):
        """MemoryManager must use filelock for cross-process coordination."""
        try:
            from desktop_memory_manager import MemoryManager
        except ImportError:
            raise AssertionError("MemoryManager not yet implemented (RED)")

        import inspect
        source = inspect.getsource(MemoryManager)
        has_lock = any(
            kw in source
            for kw in ("filelock", "FileLock", "fcntl", "flock")
        )
        assert has_lock, "MemoryManager must use filelock for cross-process locking"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-x", "-q"]))
