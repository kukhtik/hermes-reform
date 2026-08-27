"""agent.termination_markers — emit structured TERMINATED markers before os._exit."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

def _emit_terminated(
    reason: str,
    exit_code: int,
    context: dict[str, Any] | None = None,
) -> None:
    """Log a structured TERMINATED marker and write it to the errors session log.

    Must be called immediately before any ``os._exit()`` call so that the
    marker appears in logs before the process is forcibly killed.
    """
    ctx = context or {}
    marker = f"TERMINATED: reason={reason} exit_code={exit_code} context={json.dumps(ctx)}"
    # Always write to stderr first — logging may be shut down by the caller.
    try:
        print(marker, file=sys.stderr)
    except Exception:
        pass
    # Best-effort structured logging (may be no-op after logging.shutdown()).
    try:
        logging.error("TERMINATED: reason=%s exit_code=%d context=%s", reason, exit_code, json.dumps(ctx))
    except Exception:
        pass

    # Write to ~/.hermes/errors/<date>/<session_id>.jsonl when path is available.
    try:
        hermes_home = os.environ.get("HERMES_HOME")
        if hermes_home:
            session_id = os.environ.get("HERMES_SESSION_ID", "unknown")
            today = date.today().isoformat()
            err_dir = Path(hermes_home) / "errors" / today
            err_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"session_id": session_id, "reason": reason, "exit_code": exit_code, "context": ctx})
            with (err_dir / f"{session_id}.jsonl").open("a") as f:
                f.write(line + "\n")
                f.flush()
    except Exception:
        # Best-effort — any failure must not block the os._exit path.
        pass
