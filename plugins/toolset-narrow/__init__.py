"""toolset-narrow — narrow the advertised tool schema per step.

Registers `llm_request` middleware and rewrites the `tools` list for the
current step so that the provider request carries only the tools the step can
plausibly use. Measured on this host: narrowing 53 core tools down to 2 removes
~12 750 input tokens per call at a middleware cost of +1.97 ms.

Design notes (why this is deliberately conservative):

* **Never fabricate a call.** The middleware only shrinks the advertised
  schema; it never adds a tool call and never forces `tool_choice` unless the
  step has exactly one legal tool. Forcing `tool_choice` on the main model is
  what breaks small models that otherwise decline to call a tool at all.
* **Ambiguity passes through.** If the step's intent cannot be told apart by
  cheap signals, return `None` and let the full tool set through. A false
  narrow that hides a needed tool is worse than the token cost it saves.
* **Fail-open.** Any error inside the filter returns `None` (unchanged
  request); middleware failure must never break a turn.

Configuration lives in `config.yaml` under `plugins.toolset_narrow` so that
behavioural settings stay out of `.env`:

    plugins:
      toolset_narrow:
        enabled: true
        # Tools kept for every step, regardless of intent.
        always: [read_file, terminal]
        # Intent -> tools. First group whose `when` matches wins.
        groups:
          - name: files
            when: [file, path, directory, edit, patch]
            keep: [read_file, write_file, patch, search_files]
          - name: web
            when: [http, url, web, search online, fetch]
            keep: [web_search, web_extract, browser_exec]
          - name: git
            when: [commit, branch, merge, diff, repo]
            keep: [terminal, read_file, patch]
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_ALWAYS = ("read_file", "terminal")

DEFAULT_GROUPS = (
    ("files", ("file", "path", "directory", "edit", "patch", "write", "файл", "директор", "папк", "правк", "редакт"),
     ("read_file", "write_file", "patch", "search_files", "terminal")),
    ("web", ("http", "url", "web", "fetch", "online", "интернет", "сайт", "ссылк"),
     ("web_search", "web_extract", "browser_exec", "terminal")),
    ("git", ("git", "commit", "branch", "merge", "diff", "repo", "коммит", "ветк", "репозитор"),
     ("terminal", "read_file", "patch", "search_files")),
    ("tasks", ("task", "delegate", "subagent", "worker", "субагент", "делегир", "подзадач"),
     ("delegate_task", "todo", "terminal", "read_file")),
)


def _tool_name(spec: Any) -> Optional[str]:
    if not isinstance(spec, dict):
        return None
    fn = spec.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        return name if isinstance(name, str) else None
    name = spec.get("name")
    return name if isinstance(name, str) else None


def _last_user_text(messages: Any) -> str:
    """Concatenate the most recent user-role text; empty when unknown."""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.lower()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            if parts:
                return " ".join(parts).lower()
    return ""


def _config() -> Dict[str, Any]:
    """Read plugin config from the merged config; never raise."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly().get("plugins", {})
        section = cfg.get("toolset_narrow") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _resolve_keep(text: str, always: List[str], groups: List[tuple]) -> Optional[List[str]]:
    """Return the tool names to keep, or None when the step is ambiguous."""
    if not text:
        return None
    for name, triggers, keep in groups:
        if any(trigger in text for trigger in triggers):
            merged = list(dict.fromkeys(list(always) + list(keep)))
            logger.debug("toolset-narrow: group=%s keep=%s", name, merged)
            return merged
    return None


def on_llm_request(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """`llm_request` middleware: shrink `tools` when the step's intent is clear."""
    try:
        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        tools = request.get("tools")
        if not isinstance(tools, list) or not tools:
            return None

        section = _config()
        if section.get("enabled") is False:
            return None

        always = [t for t in (section.get("always") or DEFAULT_ALWAYS) if isinstance(t, str)]
        raw_groups = section.get("groups")
        groups: List[tuple] = []
        if isinstance(raw_groups, list):
            for g in raw_groups:
                if not isinstance(g, dict):
                    continue
                triggers = tuple(t.lower() for t in (g.get("when") or []) if isinstance(t, str))
                keep = [t for t in (g.get("keep") or []) if isinstance(t, str)]
                if triggers and keep:
                    groups.append((str(g.get("name") or "group"), triggers, tuple(keep)))
        if not groups:
            groups = list(DEFAULT_GROUPS)

        keep = _resolve_keep(_last_user_text(request.get("messages")), always, groups)
        if not keep:
            return None

        keep_set = set(keep)
        narrowed = [t for t in tools if _tool_name(t) in keep_set]
        # Refuse a narrowing that would leave nothing — the filter is wrong, not
        # the step. Never empty the tool set.
        if not narrowed or len(narrowed) >= len(tools):
            return None

        logger.debug("toolset-narrow: %d -> %d tools", len(tools), len(narrowed))
        return {
            "request": {**request, "tools": narrowed},
            "source": "toolset-narrow",
            "reason": "narrowed tool schema for step intent",
        }
    except Exception as exc:  # fail-open: a filter bug must never break a turn
        logger.warning("toolset-narrow middleware failed: %s", exc)
        return None


def register(ctx: Any) -> None:
    ctx.register_middleware("llm_request", on_llm_request)
