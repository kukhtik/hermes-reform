"""
F3.1: Desktop OOM crash-loop — content-visibility:auto layout-thrash

This test verifies that CSS content-visibility:auto is not present in desktop
renderer files, as it causes unbounded layout recalculation on visibility
changes leading to OOM in long sessions.
"""

import os
import re
import subprocess
import pytest


DESKTOP_RENDERER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "apps", "desktop", "src")

# Files that are allowed to use content-visibility:auto (explicitly audited)
ALLOWED_FILES = {
    # Add files that have been individually verified not to cause layout thrash
}

# Pattern to find content-visibility:auto (in CSS strings, not comments)
CONTENT_VISIBILITY_AUTO_RE = re.compile(
    r'content-visibility\s*:\s*auto\b',
    re.IGNORECASE
)


def find_content_visibility_auto(root_dir):
    """Find all files using content-visibility:auto."""
    violations = {}
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            if not (filename.endswith(('.css', '.scss', '.html', '.tsx', '.jsx'))):
                continue
            filepath = os.path.join(dirpath, filename)
            relpath = os.path.relpath(filepath, root_dir)
            with open(filepath, encoding='utf-8', errors='ignore') as f:
                for lineno, line in enumerate(f, 1):
                    # Skip comment-only lines
                    stripped = line.strip()
                    if stripped.startswith('//') or stripped.startswith('/*') or stripped.startswith('*'):
                        continue
                    if CONTENT_VISIBILITY_AUTO_RE.search(line):
                        violations.setdefault(relpath, []).append((lineno, line.rstrip()))
    return violations


def test_no_content_visibility_auto_in_renderer():
    """
    RED TEST: Verify content-visibility:auto is NOT present in desktop renderer files.
    This catches the OOM-causing CSS pattern from F3.1.
    """
    violations = find_content_visibility_auto(DESKTOP_RENDERER_DIR)
    
    # Filter out allowed files
    active_violations = {
        f: lines for f, lines in violations.items()
        if f not in ALLOWED_FILES
    }
    
    if active_violations:
        error_lines = []
        for f, lines in active_violations.items():
            for lineno, line in lines:
                error_lines.append(f"  {f}:{lineno}: {line}")
        pytest.fail(
            f"content-visibility:auto found in {len(active_violations)} file(s) — causes OOM layout thrash:\n"
            + "\n".join(error_lines)
        )


def test_rss_memory_growth_bounded():
    """
    Verify RSS growth is bounded. This is a structural test: if content-visibility:auto
    is present, the underlying layout thrash mechanism exists. This test documents
    the RSS requirement (≤512MB over baseline for 4h synthetic load).
    The actual RSS measurement requires a running desktop app and is done separately.
    """
    violations = find_content_visibility_auto(DESKTOP_RENDERER_DIR)
    active_violations = {
        f: lines for f, lines in violations.items()
        if f not in ALLOWED_FILES
    }
    # If content-visibility:auto is gone, the layout thrash root cause is fixed
    assert len(active_violations) == 0, (
        "RSS growth is unbounded because content-visibility:auto is still present"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
