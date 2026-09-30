# ADR: Toolset narrowing via `llm_request` middleware, app-wide (all profiles)

Status: Implemented

Date: 2026-09-30

Implementation: `plugins/toolset-narrow/` (`__init__.py`, `plugin.yaml`,
`README.md`). Verified in a live Hermes process against an isolated
`HERMES_HOME`: the plugin loads, registers `llm_request`, and narrows by intent
— `files` → 5 tools, `git` → 4, `web` → 5, `tasks` → 4, ambiguous → the full
set (fail-open). Existing middleware tests still pass (`tests/hermes_cli/test_plugins.py`,
65/65).

## Context

Every model tool we ship is serialized into **every** provider request. On a
full Hermes install the tool schema is large: `discover_builtin_tools()`
returns **43 tool modules**, `_HERMES_CORE_TOOLS` lists **53**, and
`toolsets.py` defines **59 toolsets** (measured 2026-09-30 on the pinned
checkout). Serialized as OpenAI tool definitions that is roughly **13 000
input tokens per call** — paid on every request of every turn, whether or not
the model needs those tools.

Two facts make this worth acting on:

1. **Most tool choices are determined by the task, not by the model.** On
   12 369 real tool calls from a working profile: `terminal` 55.3%,
   `read_file` 11.9%, `write_file` 6.8%, `patch` 5.9% — 74% is accounted for by
   three file/shell tools. 77% of assistant turns carry exactly one tool call.
   The model is not choosing among 53 tools; it is choosing among the two or
   three that fit the step.
2. **Middleware can already rewrite the request.** `hermes_cli/middleware.py`
   exposes `llm_request`, which replaces the effective provider kwargs —
   including `tools` and `tool_choice` — before execution. This is not a hook
   (observation only) and requires no core change.

The mechanism was verified on the live runtime, not inferred from docs:

- A probe plugin registering `llm_request` received the payload with
  `tools`, `tool_choice`, and tool names present (`mw_evidence.jsonl`).
- Returning `{"request": {...}}` with a filtered `tools` list changed the
  effective request: **4 tools → 2**, `tool_choice` `auto` → `required`,
  middleware trace recorded (`probe_rewrite.py`).
- In a real `run_conversation`, the request dump written to
  `sessions/request_dump_*.json` contained `"tools": [...]` with **`n_tools: 2`**
  (only `read_file`, `terminal`) — the narrowing reaches the provider payload,
  not just an internal structure.

## Decision

Ship a **toolset-narrowing plugin** that registers `llm_request` middleware and
rewrites the `tools` list for the current step, and enable it **app-wide**
(every profile, every agent, subagents included).

Scope of the rewrite is deliberately conservative: the plugin narrows the
advertised tool schema for a step; it never fabricates a tool call and never
forces `tool_choice` unless a step has exactly one legal tool.

## Consequences

**Measured cost (T4):** middleware overhead is **+1.97 ms per call** (0.265 ms
baseline → 2.238 ms with middleware, 200 iterations, 60-tool payload).

**Measured saving (T4b/T8):** narrowing 60 → 2 tools removes **59 144 chars /
~14 786 tokens** (96.7% of the tool schema); at the measured core size of 53
tools the saving is **~12 750 tokens per call**. The overhead is four orders of
magnitude cheaper than the saving, so the trade is not close.

**Positive:**
- Provider requests shrink by roughly 96% of their tool-schema weight.
- A model that sees fewer, more relevant tools has fewer ways to pick the
  wrong one — the failure mode the plan originally proposed a decision model
  for (see the rejected ADR on Laya/von judges).

**Negative / risks:**
- **Over-narrowing.** If the filter drops a tool the step needs, the agent
  cannot call it. The filter must be derived from the step's actual intent,
  with a documented safe default of "narrow only when the intent is
  unambiguous; otherwise pass the full set through".
- **Prompt cache.** Middleware rewrites the request payload, not the system
  prompt, so per-conversation prefix caching is not invalidated by this
  change. The tool list is part of the request body, not the cached system
  prompt; confirm this stays true if the filter is later made per-turn-aware.
- **Debugging.** A narrowed request looks different from the configured
  toolset; `middleware_trace` in the `pre_api_request` payload names the
  plugin and reason, which is the intended audit trail.

## Enablement (app-wide)

Two independent layers must line up; they are configured differently and this
is the part that is easy to get wrong.

### 1. The plugin code — per profile

Plugins are discovered from `get_hermes_home() / "plugins"`
(`hermes_cli/plugins_discovery.py`, `user_dir = get_hermes_home() / "plugins"`),
so a plugin placed only in the global `~/.hermes/plugins/` is visible **only to
the `default` profile**. Measured: a plugin present in the global root loaded
in the `default` home (58 plugins) but **not** in a second home (57 plugins).

For every profile, either copy the plugin into `<profile>/plugins/<id>/` or
**symlink** one shared directory into each profile. Symlinks were verified to
work on this host (Developer Mode enabled) — three test profiles all resolved
the plugin through a symlink, so 12 copies are not required.

### 2. Enablement — app-wide via managed scope

`plugins.enabled` is read from the profile's own `config.yaml`
(`get_config_path()` → `get_hermes_home() / "config.yaml"`). Editing 12 configs
is brittle. The global layer is **managed scope** (`hermes_cli/managed_scope.py`),
which deep-merges a directory (`$HERMES_MANAGED_DIR`, default `/etc/hermes` on
POSIX) over **every** profile's config.

Verified behaviour:

- A managed `config.yaml` applied to an isolated profile: managed keys took
  effect, unrelated profile settings were preserved.
- **Lists are replaced, not merged.** `_deep_merge` assigns non-dict values
  wholesale, so `plugins.enabled` from the managed layer **overwrote** the
  profile's list. The managed value must therefore be the **complete** list of
  plugins that should be enabled, or profile-local plugins will be silently
  disabled.
- **Dicts merge recursively.** A managed `mcp_servers.judge` did not clobber a
  profile's existing `mcp_servers.open-design` — both survived. This matters
  for the MCP judge ADR, not this one.

At the time of writing every one of the 12 live profiles had an **empty**
`plugins.enabled` (`[]` or unset), so a managed list containing only the new
plugin loses nothing.

Caveat: managed scope is documented as an IT-pushed, user-immutable layer.
Using it for a personal preference is mechanically sound but semantically an
admin override; if that is unwanted, fall back to editing each profile's
`config.yaml`.

## Verification checklist

1. `hermes -p <profile> plugins list` shows the plugin for **every** profile.
2. A request dump (`HERMES_DUMP_REQUESTS=1`) shows a narrowed `tools` array.
3. `middleware_trace` in `pre_api_request` names the plugin and reason.
4. Token accounting: measure prompt tokens with and without the plugin on the
   same step.
5. Regression: a task needing a narrowed-away tool must still work (i.e. the
   filter must pass the full set when the step is ambiguous).

## Evidence

- `hermes_cli/middleware.py` — `VALID_MIDDLEWARE`, `apply_llm_request_middleware`
- `agent/turn_api_request.py:149` — the single `llm_request` call site
- `tools/delegate_tool_child_run.py:411,659` — subagents re-enter
  `run_conversation`, so middleware covers them too
- `hermes_cli/plugins_discovery.py:151` — `user_dir = get_hermes_home() / "plugins"`
- `hermes_cli/config.py:465` — `get_config_path()`; `:2133` — `_merge_managed_overlay`
- `hermes_cli/managed_scope.py` — managed-scope resolution and merge
- Probe artifacts: `mw_evidence.jsonl`, `probe_rewrite.py`,
  `t3_live_request_dump.json`, `t4_overhead.py`, `t4b_tokens.py`, `t8_toolcount.py`
