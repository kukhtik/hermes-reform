# toolset-narrow

Narrow the advertised tool schema per step so provider requests carry only the
tools a step can plausibly use.

**Measured on this host (2026-09-30):**

| | Value |
| --- | --- |
| Core tool modules (`discover_builtin_tools`) | 43 |
| `_HERMES_CORE_TOOLS` | 53 |
| Serialized schema at 53 tools | ~13 250 input tokens |
| Schema after narrowing to 2 tools | ~500 tokens |
| **Saving** | **~12 750 tokens/call (96%)** |
| **Middleware cost** | **+1.97 ms/call** (0.265 → 2.238 ms) |

## How it works

Registers `llm_request` middleware (`docs/middleware/README.md`). Before the
provider call, it inspects the most recent user message for an intent keyword
and replaces `request["tools"]` with a small keep-list for that intent.

The middleware is deliberately conservative:

- **Ambiguity passes through.** No keyword match → return `None` → the full
  tool set is advertised. A false narrow that hides a needed tool costs more
  than the tokens it saves.
- **Never empties the tool set.** A narrowing that would keep nothing (or keep
  everything) is discarded.
- **Fail-open.** Any internal error returns `None`; a filter bug can never
  break a turn.
- **Never forces `tool_choice`.** It shrinks the schema only — it does not
  fabricate or force a call.

## Configuration

Behavioural settings live in `config.yaml` (not `.env`):

```yaml
plugins:
  toolset_narrow:
    enabled: true
    always: [read_file, terminal]      # kept for every step
    groups:
      - name: files
        when: [file, path, directory, edit, patch, файл, директор]
        keep: [read_file, write_file, patch, search_files, terminal]
      - name: web
        when: [http, url, web, fetch, интернет]
        keep: [web_search, web_extract, browser_exec, terminal]
```

Defaults ship for `files`, `web`, `git`, `tasks` (English and Russian
triggers). Override `groups` to change them entirely.

## Enablement, app-wide (all profiles)

Two layers, configured differently:

1. **Plugin code — per profile.** Plugins are discovered from
   `get_hermes_home() / "plugins"`, so a plugin in the global
   `~/.hermes/plugins/` is visible **only to the `default` profile**. Put the
   directory in every profile, or symlink one shared copy into each
   (symlinks verified working on this host).
2. **Enablement — one file via managed scope.** `plugins.enabled` is read from
   the profile's own `config.yaml`. To turn it on everywhere at once, use
   managed scope (`$HERMES_MANAGED_DIR`, default `/etc/hermes`), which
   deep-merges over every profile.

   **Important:** `_deep_merge` replaces lists wholesale. The managed
   `plugins.enabled` must be the **complete** list, or profile-local plugins
   are silently disabled.

## Verification

```bash
# plugin is seen by a profile
hermes -p <profile> plugins list

# the request really is narrowed
HERMES_DUMP_REQUESTS=1 hermes chat --query 'git commit this change'
# -> sessions/request_dump_*.json shows a short "tools" array

# audit trail
# middleware_trace in the pre_api_request payload names toolset-narrow
```

## Why not a model

An earlier plan proposed inserting a small decision model (Laya/von, ~400M) to
make these choices. It was measured and rejected for this task — see
[`structural-classification-by-rule.md`](../../docs/middleware/structural-classification-by-rule.md).
A rule over `role` plus structural markers covers 100% of the stream in ~1 ms;
the model would cost ~1.3 s per call and ~1.9 GB RSS to answer a question a
one-line rule answers exactly.
