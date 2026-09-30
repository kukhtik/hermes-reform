# ADR: Structural classification by rule, not by a decision model

Status: Implemented

Date: 2026-09-30

Implementation: `agent/structural_classify.py` with contract tests in
`tests/agent/test_structural_classify.py` (17/17 passing). Verified on a live
session DB: **100.00% coverage, 0 unknown, 1.0 ms** for a 2000-message sample.
The decision-model path is deliberately not shipped; its measured evidence and
the conditions under which it *would* be justified are recorded below and in
`plugins/toolset-narrow/README.md`.

## Context

A plan was proposed to reduce per-step LLM cost by inserting a small
"decision model" (Jev-style: Laya 421M / von 395M, non-autoregressive, typed
`choice`/`score`/`noul` questions answered in one forward pass) in front of the
agent, so that routine micro-decisions would be answered locally instead of by
the main model. The concrete first target was **classifying incoming messages
in an agent's own session stream** — a 5-class `choice` over real messages.

Before building that layer, both halves were measured on this host.

### The decision-model half: tested, then rejected for this use

Four checkpoints were run on the same 200 real messages drawn from a live
profile's session DB, with labels assigned deterministically (not by hand):

| Run | Model | Task | Accuracy | Baseline | Verdict |
| --- | --- | --- | --- | --- | --- |
| 1 | laya 421M | 5-class, terse criteria | 3.5% | 20% | below chance |
| 2 | laya-multilingual 322M (ctx 8192) | 5-class | 3.5% | 20% | context was not the limit |
| 3 | laya-typed-decisions 421M | 5-class | 3.5% | 20% | slowest (p95 6818 ms) |
| 4 | von 1.3 395M | 5-class | 3.5% | 20% | only correct-calibrated one |
| 5 | von 1.3 | binary, descriptive criteria | 67.8% | 51.4% | +16.3 pp |
| 6 | laya 421M | 4 classes, balanced sample | **45.0%** | **26.5%** | **+18.5 pp** |

Three hypotheses were eliminated by direct test rather than argument:

- **Context length was not the cause.** The multilingual checkpoint has 8192
  tokens of context versus 512 on the English one; accuracy was unchanged.
- **Language was not the cause.** Same checkpoint, split by script: Cyrillic
  3.1% vs Latin 4.0% — a 0.9 pp difference is noise.
- **Batching was not a latency fix.** `predict_batch` measured **1498 ms/state**
  versus **1147 ms** for single calls on CPU: the batch path is slower here.

What *did* change the result, twice, in two different ways:

- **Criteria wording** (runs 1 → 5/6): descriptive criteria that give an
  example and an explicit negative ("machine output: starts with `{` or `[`,
  JSON objects, shell results … NOT written by a person") moved accuracy by
  **+16 pp**. Terse one-word criteria were the single worst choice.
- **Sample balance** (run 6): a 25/25/2/2/3 sample inflates the majority-class
  baseline to 43.9%, so a 40.4% result *looks* like a failure. On a balanced
  sample of the same task the model scores 45.0% against a 26.5% baseline.
  **Accuracy must be compared against a baseline computed on a balanced
  sample, or the metric hides the signal.**

Two further structural findings:

- **`tool_json` is not a real class in a message stream.** Across 12 000
  messages, textual JSON outside `role=tool` occurred **twice**. JSON arrives
  either as a tool result (`role=tool`) or as a tool call (`tool_calls`). A
  5-class task that includes it mixes a structural class into a semantic
  question and makes the task ill-posed.
- **Label naming is part of the prompt.** Identical criteria with natural
  label names scored 45.0%; with opaque codes (`A`/`B`/`C`/`D`) the same task
  scored 35.8% — **−9.3 pp** purely from the label text.

### The rule half: tested, and sufficient

The same 12 000 messages were classified with a **deterministic rule** using
`role` plus structural markers, no model involved:

```
tool_result             4969   62.4%
tool_call               2558   30.2%   (waiting: see note)
assistant_output         209    1.7%
system_notification      113    0.9%
user_request             108    0.9%
user_steering             43    0.4%
UNKNOWN                    0    0.0%   → 100.00% coverage of 2000-message sample
```

On a 2000-message sample the rule achieved **100.00% coverage with 0 UNKNOWN**
in **1.0 ms total** (0.0005 ms/message). The model-based path, for the subset it
answered correctly, cost roughly **1000 ms per message** on the same CPU.

## Decision

**Classify session-stream messages by rule, not by a decision model.** Use
`role` from the session DB (already known to the runtime, so this is not an
inference problem at all) plus the structural markers:

| Marker | Class |
| --- | --- |
| `role == "tool"` | tool result |
| `tool_calls` non-empty | tool call |
| text starts with `[OUT-OF-BAND` | user steering (mid-task correction) |
| text contains `Background process proc_` | system notification |
| otherwise `role == "assistant"` | assistant output |
| otherwise `role == "user"` | user request |

**Do not build a decision-model judge for this.** The model is not needed for
the classes that occur (98%+ of the stream is tool traffic plus two roles), and
where it *is* applicable (semantic sub-classes) it must be measured against a
balanced baseline before being trusted.

## Consequences

**Positive:**
- Zero tokens, ~0 ms, 100% coverage on the structural majority.
- No new dependency, no model weights, no RAM (see below), nothing to keep
  calibrated.
- The rule is auditable and testable as a pure function.

**Negative / limits:**
- The rule only covers classes that have a structural or role marker. It says
  nothing about *semantic* sub-classification (e.g. "is this a bug report or a
  feature request"), which is where a model would actually add value.
- `role` availability is a pre-condition. This ADR applies to consumers that
  read the session DB or an equivalent structured source. A consumer that sees
  only flattened text must fall back to the structural markers alone, which
  covers less.

**Cost of the rejected alternative, for the record:** a 400M decision model
holds ~**1.9 GB RSS** resident (`laya` 421M measured: 23.2 MB before load →
1942.6 MB after, +15 MB working delta, no growth over 15 calls) and answers in
**~1.3 s per call on CPU**. That is a large fixed cost to pay for a task a
one-line rule answers exactly and instantly.

## Where a decision model *is* justified

Kept for a future, narrower ADR. The evidence supports it only under all of:

- the question is **binary or a small balanced choice** — a 4-class task scored
  +18.5 pp but a 5-class task mixing structural and semantic classes did not;
- criteria are **descriptive** (example + explicit negative), never one word;
- **label names carry meaning** (natural names, not `A`/`B`/`C`);
- the evaluation **compares against a baseline on a balanced sample**;
- the model is run **locally** where the call volume amortises the ~1.9 GB and
  ~1.3 s per call.

Infrastructure for that already exists and was verified: `laya-mcp-server`
exposes 8 MCP tools (`laya_predict` for `choice`/`score`/`noul`,
`laya_predict_batch`, `laya_route`, `laya_shortlist`, `laya_preset`, …) over
stdio using `mcp.server.MCPServer` — the same class the in-tree
`mcp_serve.py` uses. The Jev-compatible HTTP surface `POST /v1/systemone`
responds with `{answers: {name: {choice|score|noul, probabilities, confidence}},
usage}` and was verified end-to-end. **No new server needs to be written**;
enabling it is a `mcp_servers` entry, which is configured per profile (dicts
merge across the managed layer, unlike `plugins.enabled` lists).

## Evidence

- Rule: `jev-test/t5d_final.py`, `t5b_role.py`, `t5c_unknown.py`
- Model runs: `t6_phrasing_full.py`, `t10_5class.py`, `t11_balanced.py`,
  `t11_make_balanced.py`
- Environment/interfaces: `t7_mcp_client.py`, `t8_ram.py`, `t9_http.py`,
  `mcp_judge_server.py`
- Raw per-message results: `_results*.jsonl`, `_dataset.json`,
  `_dataset_balanced.json`
