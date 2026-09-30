# @HERMESHUMANBOT Multiplexer — Operation Runbook

> **Audience:** Operator (Max) + contributors touching the gateway on this machine
> **Scope:** the Windows install at `C:\Users\Nitro\AppData\Local\hermes` (called `$H` below)
> **Status:** live since 2026-09-30 — one bot, 13 profile topics, topic→profile routing
> **Related:** [Profile-Based Routing](profile-routing.md), [Session Lifecycle](session-lifecycle.md), `docs/kanban/multi-gateway.md`

## 1. What this is and why

Before: every profile that wanted Telegram needed its own bot token, and three profiles
(`default`, `research-parent`, `wnn`) shared one token by copy-paste. Two pollers on one
token is a hard Telegram error (`409 Conflict: terminated by other getUpdates request`),
so the gateway died in a retry loop — the bot was permanently down.

Now: **one bot (`@HERMESHUMANBOT`, id 8697088373) serves every profile** through the
default-profile multiplexer:

```
                 Telegram DM with @HERMESHUMANBOT (single token, single poller)
                 │
   ┌─────────────┼───────────────────────────────────────────────┐
   │  root DM    │  topic «research-parent»  topic «wnn»   …     │   ← DM topics, one per profile
   │  system     │      (thread 273613)      (273625)            │
   │  lobby      │                                               │
   └─────────────┼───────────────────────────────────────────────┘
                 ▼
        gateway.profile_routes  (thread_id → profile)
                 ▼
        default-profile multiplexer (single gateway process)
        ├── serves research-parent  → its config / memory / skills / credentials
        ├── serves wnn              → …
        └── serves … (13 profiles)
```

Each topic is a first-class Hermes session lane; `/sessions all` inside a topic lists that
profile's sessions **including Desktop ones**, and `/resume <n>` continues any of them.

## 2. Machine layout (what runs where)

| Component | Path | Role |
|---|---|---|
| Multiplexer gateway | `$H` (`HERMES_HOME=…\AppData\Local\hermes`) | The only Telegram poller. Serves all profiles |
| Bot token | `$H/.env` → `TELEGRAM_BOT_TOKEN` | **Single source.** Never copy it to profile `.env` files |
| Profile topics | `$H/config.yaml` → `platforms.telegram.extra.dm_topics` | Declared once; `thread_id`s are auto-persisted back by the adapter |
| Topic→profile map | `$H/config.yaml` → `gateway.profile_routes` | 13 routes, `chat_id=623315564`, one per topic |
| Admin gate | `$H/config.yaml` → `platforms.telegram.extra.allow_admin_from` | `623315564` — unlocks `/sessions all`, `/resume` |
| Auto-start (login) | `Startup\Hermes_Gateway_local.vbs` | Launches `$H\gateway-service\Hermes_Gateway.vbs` |
| Other install (untouched) | `C:\Users\Nitro\.hermes` | Bot `VETKA_COD_BOT`; owns `Startup\Hermes_Gateway.vbs` — do not overwrite |
| WSL install (untouched, except one) | `\\wsl$\Ubuntu\home\nitro\.hermes` | Bot `HERMES_MULTIBOT` (default gateway). `hermes-gateway-unified-meta.service` **stopped+disabled** (see §5) |

## 3. Configure from scratch (reproduce)

```bash
export HERMES_HOME="C:/Users/Nitro/AppData/Local/hermes"

# 1) Single token owner
#    $H/.env                       → TELEGRAM_BOT_TOKEN=…    (keep)
#    $H/profiles/*/.env            → comment the line out    (remove duplicates!)

# 2) Multiplex + admin
hermes config set gateway.multiplex_profiles true
hermes config set platforms.telegram.extra.allow_admin_from '["623315564"]'

# 3) Declare the topics (names only; thread_ids are filled in on first start)
hermes config set platforms.telegram.extra.dm_topics \
  '[{"chat_id": 623315564, "topics": [{"name":"research-parent"}, …]}]'

# 4) Start (VBS path = same as the login item; avoids the Windows Job Object kill)
wscript.exe "C:\Users\Nitro\AppData\Local\hermes\gateway-service\Hermes_Gateway.vbs"

# 5) After the first start, read the persisted thread_ids and write the routes
python - <<'PY'   # or any YAML reader
import yaml
d = yaml.safe_load(open(r'config.yaml', encoding='utf-8'))
for t in d['platforms']['telegram']['extra']['dm_topics'][0]['topics']:
    print(t['name'], t['thread_id'])
PY
# then:
hermes config set gateway.profile_routes '[{"name":"tg-research-parent","platform":"telegram",
  "chat_id":"623315564","thread_id":"<tid>","profile":"research-parent"}, …]'
hermes gateway restart
```

Notes:
- `multiplex_profiles: true` with **no** `multiplex_profile_allowlist` = serve every named
  profile, including ones created later (that is the intended behavior here).
- `hermes config set` prints a "not a recognized config key" warning for
  `gateway.multiplex_profiles` / `gateway.profile_routes`. It is cosmetic — the gateway's
  own raw-YAML loader reads both keys (`gateway/config_loader.py` top-level bridge table).
  Verify with `hermes config get <key>`, not by the warning.

## 4. Daily operation

```bash
export HERMES_HOME="C:/Users/Nitro/AppData/Local/hermes"
hermes gateway status                      # service + login item health
cat $H/gateway_state.json | python -m json.tool   # platform states (telegram should be "connected")
tail -f $H/logs/gateway.log                # live log ("polling confirmed healthy" = good)
hermes gateway restart                     # restart the multiplexer
hermes send --to telegram:623315564 "msg"  # one-off outbound (no agent turn)
```

Inside Telegram:
- **Root DM** = system lobby (commands only). Normal chats live in topics.
- `/topic help`, `/sessions all`, `/resume <n>` — per-topic lane, scoped to that topic's profile.
- `/whoami` — confirm admin tier (needed for cross-session listing).

## 5. Known hazards (read before touching anything)

1. **Token duplicates are fatal, everywhere on the machine.** The same token must not be
   live in any second install. Check before believing "the bot is broken":
   ```bash
   grep -rl "^TELEGRAM_BOT_TOKEN=" ~/.hermes* $H $H/profiles/* 2>/dev/null
   ```
   On 2026-09-30 the WSL install's `unified-meta` service held the same token → 409 loop.
   It is stopped+disabled now; re-enable only if this machine's multiplexer is off:
   `wsl -d Ubuntu -- systemctl --user enable --now hermes-gateway-unified-meta.service`.
2. **After any restart expect up to ~1 min of 409-retries.** Telegram keeps the previous
   long-poll session open server-side. The adapter retries 5× over 200 s and recovers
   (`polling confirmed healthy`) — do not restart again during that window; it makes it worse.
3. **`hermes gateway install` would overwrite `Startup\Hermes_Gateway.vbs`**, which belongs
   to the native `C:\Users\Nitro\.hermes` install. This setup intentionally avoids it — the
   login item here is the hand-made `Hermes_Gateway_local.vbs` (§2). Keep it that way.
4. **Desktop cron scheduler vs multiplexer**: the desktop `serve` backend ticks profiles
   whose own gateway is not running; the multiplexer ticks all served profiles. Both take
   the per-store `cron/.tick.lock`, so runs cannot double-fire, but expect both to log ticks.
5. **A topic's profile is resolved per inbound message** from `profile_routes`. A topic with
   no route falls back to the **default** profile (root DM is default by design).

## 6. Rollback

Backups from the cutover: `$H/backups/humanbot-mux-20260930/` (config, three `.env`s,
Startup folder snapshot). Full revert =

```bash
hermes config set gateway.multiplex_profiles false
hermes config set gateway.profile_routes '[]'
# restore config.yaml + the three .env files from $H/backups/humanbot-mux-20260930/,
# restore the two Startup .vbs files, restart the gateway
```

WSL side: `systemctl --user enable --now hermes-gateway-unified-meta.service` (restores the
old poller — only after the Windows multiplexer is stopped, or the 409 war returns).

## 7. Verified on 2026-09-30 (cutover receipts)

- `getMe`: `has_topics_enabled=true`, `allows_users_to_create_topics=true`
- Topics: 13 created, thread_ids 273613–273637 persisted to `config.yaml`
- Routes: 13 loaded by `load_gateway_config()`; `match_profile_route()` maps
  `273613→research-parent`, `273637→pena-bot`, root DM → default
- Gateway: `telegram=connected`, "polling confirmed healthy", cron ticking 14 profiles
- `hermes send` delivered to the DM (operator confirmed the message arrived)

## 8. Post-cutover fixes (2026-09-30, evening)

Two bugs surfaced the first time a routed topic turn actually ran, both invisible
from the Desktop app (the desktop backend reads its own process env; routed
gateway turns read the *profile's* `.env`):

### 8.1 Provider auth failed in every routed turn

Symptom (gateway log, `profiles/research-parent/logs/gateway.log`):

```
WARNING gateway.run: Primary provider auth failed: No usable credentials found
for provider 'ollama-cloud'. Set OLLAMA_API_KEY. — trying fallback
```

Cause: under `multiplex_profiles` the per-turn secret scope is built **only** from
`<profile>/.env` (`agent/secret_scope.py::build_profile_secret_scope`, fail-closed
so profiles cannot leak into each other). Eight profiles — `default`, `family-tree`,
`geo-converter`, `mpt`, `pena-bot`, `research-parent`, `trading-bot`, `wnn` — kept
`OLLAMA_API_KEY` only in the *machine* user env (set for the desktop app), never in
their own `.env`, so every routed turn raised `AuthError(missing_api_key)`.

Fix: append `OLLAMA_API_KEY=…` to each affected `<profile>/.env` (same value as the
Windows user environment variable `OLLAMA_API_KEY`), then `hermes gateway restart`.

Rule of thumb for this machine: **any credential a profile's configured model needs
must exist in that profile's own `.env`** — the user-level env vars only serve the
desktop backend, not multiplexed gateway turns.

### 8.2 Retired model `deepseek-v4-flash:0731`

The second failure after fixing auth was HTTP 410 from ollama-cloud:

```
{"error":{"message":"deepseek-v4-flash:0731 was retired at 2026-09-25 00:00:00 -0700 PDT ..."}}
```

`deepseek-v4-flash:0731` was in `model.default` for five profiles (`default`,
`orchestrator`, `research-parent`, `wnn`, `worker-analyst`). Replacement —
`deepseek-v4.1-flash` (verified live, HTTP 200; it is also the model the working
desktop sessions already ran). `hermes migrate` only covers xAI retirements, so the
swap is a manual `config.yaml` edit on each profile.

Verification pass after both fixes (resolve in multiplex scope + live completion,
`OLLAMA_API_KEY` absent from `os.environ`): all 13 multiplexed profiles → HTTP 200.

### 8.3 Telegram receive-path hiccups

Around 17:40 and 18:06–18:09 the log showed `Sticky Telegram path … failed` /
`Dual-stack api.telegram.org path failed` and one `telegram_network_error` rebuild.
These were transient network flaps, not config; the adapter reconnected on its own
(`polling confirmed healthy`), no action needed. A 409 window after each restart is
normal (§5.2).
