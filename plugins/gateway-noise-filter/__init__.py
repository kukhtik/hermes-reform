"""gateway-noise-filter — classify inbound gateway messages before dispatch.

Registers `pre_gateway_dispatch` and classifies each incoming message with the
deterministic rule in `agent/structural_classify.py`. Machine-originated traffic
(automated process notices, echoed tool output) is identified **before** the
agent wakes up, so a turn is never spent on it.

Default posture is **observe only**: the plugin records what it *would* skip and
leaves dispatch untouched. Set `plugins.gateway_noise_filter.drop: true` in
`config.yaml` to actually drop machine-only messages.

Why this is a filter and not a model: see
`docs/middleware/structural-classification-by-rule.md`. The rule covers 100% of
a real stream in ~1 ms; a ~400M decision model would cost ~1.3 s and ~1.9 GB.

Configuration (`config.yaml`, not `.env`):

    plugins:
      gateway_noise_filter:
        drop: false              # true -> actually skip machine notices
        drop_kinds: [system_notification]
        log: true                # write a line per classified message
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DROP_KINDS = ("system_notification",)

_stats: Dict[str, int] = {}


def _config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        plugins = load_config_readonly().get("plugins", {})
        section = plugins.get("gateway_noise_filter") if isinstance(plugins, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _classify(text: str) -> str:
    from agent.structural_classify import classify_message

    # Gateway events have no session role: markers are the only signal.
    return classify_message(text=text)


def on_pre_gateway_dispatch(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Classify an inbound message; optionally skip machine-only traffic."""
    try:
        event = kwargs.get("event")
        text = getattr(event, "text", None) or ""
        if not text.strip():
            return None

        kind = _classify(text)
        _stats[kind] = _stats.get(kind, 0) + 1

        cfg = _config()
        if cfg.get("log", True):
            logger.info(
                "gateway-noise-filter: kind=%s len=%d user=%s",
                kind, len(text), getattr(event, "user_id", None),
            )

        if not cfg.get("drop", False):
            return None

        drop_kinds = cfg.get("drop_kinds")
        if not isinstance(drop_kinds, list):
            drop_kinds = list(DEFAULT_DROP_KINDS)
        if kind in drop_kinds:
            logger.info("gateway-noise-filter: skipping %s message", kind)
            return {"action": "skip", "reason": "machine-originated (%s)" % kind}
        return None
    except Exception as exc:  # fail-open: never block dispatch on a filter bug
        logger.warning("gateway-noise-filter failed: %s", exc)
        return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
