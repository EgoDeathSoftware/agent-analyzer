# Agent Post-Mortem Analysis — Design

Status: **written spec, awaiting review.** Sections 1–6 were approved in brainstorming on
2026-09-27.

## 1. Goal and scope

`sk` plus its skills diagnose troubled coding-agent sessions from any family `sk` can parse
(Claude Code and pi today). They report **what** went wrong (crash, stall or hang, loop,
drift, tool-calling failure), **why** it went wrong (the decisive error, which comes before
the point where the failure surfaced), and **what to change**, with each fix aimed at the right
target: model, harness or config, serving stack, or environment. The emphasis is on
open-model agents running on local vLLM.

**In scope**
- A failure taxonomy (§2).
- A restructured detection layer (§3).
- Additive data-model fields populated by both parsers (§4).
- `sk forensics` becomes the post-mortem report, a new `sk findings` fleet view, and small
  CLI additions (§5).
- A post-mortem procedure in `session-forensics` with a failure-class knowledge base, and
  fleet updates to `error-patterns` (§6).
- Tests, corpus acceptance and real-session validation (§7).

**Out of scope**
- **Live guard.** Running `sk` against an active session to nudge or halt it is deferred.
  Detectors are pure functions over a session, so they already work on a partial one; nothing
  else is built for live use.
- **codex and opencode parsers.** Each gets its own spec after this one. §A.3 records the
  codex format notes gathered here.
- **Any LLM call inside `sk`.** Judgment-tier work belongs to the skill.

**Constraints**
- System Python 3.11+, stdlib only.
- Model fields are additive, with defaults.
- Tests use inline fixture builders, never committed `.jsonl` files.
- The existing 297 tests must keep passing.

## 2. Failure taxonomy

**Tiers**
- **S** (structural): `sk` alone reaches the verdict.
- **S→J** (candidate): `sk` emits a line-cited candidate, and the skill confirms or dismisses it.
- **J** (judgment): only the skill can detect it.

**Fix targets**
- `model`: swap it, or change sampling.
- `harness`: instructions file, tool config, `max_tokens`.
- `serving`: vLLM tool-call parser, chat template, sampling defaults.
- `env`: environment.
- `—`: no fix target.

Class and kind names share one namespace and are unique. There are two kinds of taxonomy entry:
- **Error classes** are assigned to individual failed tool calls, or API errors, by text
  pattern.
- **Finding kinds** are emitted per session by detectors.

Every error class in the tool-protocol, generation and environment families also surfaces as a
finding whenever it occurs at least once. `exit-code`, `not-found` and the other existing
generic classes stay error-only, as today.

### 2.1 Error classes: new, or with widened patterns

| Class | Patterns (Claude Code / pi) | Family | Fix |
|---|---|---|---|
| `exit-code` | widen: pi puts `Command exited with code N` at the **end** of bash output; match it anywhere, and parse N for the existing 124/137/143 mapping | generic | — |
| `edit-mismatch` | CC `String to replace not found`, `Found N matches of the string`; pi `Could not find the exact text in`, `Could not find edits[`, `Found N occurrences of the text`, `No changes made to`, `edits[a] and edits[b] overlap` | tool protocol | model |
| `invalid-args` (widens `input-validation`) | `InputValidationError`, `parameter \`x\` type is expected`, pi `Validation failed for tool "`, MCP `Invalid arguments`, `is required when` | tool protocol | model |
| `args-dropped` | an `invalid-args` match whose body contains `Received arguments:\s*\{\}` (checked before `invalid-args`) | tool protocol | serving |
| `unknown-tool` | pi `Tool X not found`, CC `No such tool available`, `Unknown skill:` | tool protocol | model / harness |
| `truncated-call` | pi `was not executed: the response hit the output token limit` | tool protocol | harness |
| `tool-misuse` | `EISDIR`, `Offset N is beyond end of file` | tool protocol | model |
| `sibling-cascade` | `Sibling tool call errored` | tool protocol | — (not counted toward `error-cascade` runs) |
| `aborted` | `[Request interrupted by user`, pi `Operation aborted`, `Command aborted` | lifecycle | — |
| `tool-error-structured` | body starts with `{"ok": false` | environment | env |
| `policy-denied` | widen: `File is in a directory that is denied by your permission`, worktree-isolation blocks (`This session is isolated in the worktree`), pi `Tool execution was blocked` | environment | harness |
| `user-rejected` | widen: `The user doesn't want to take this action` | environment | — |
| `api-error` / `context-overflow` / `auth` / `rate-limit` / `network` | classified from `Message.error_text` (§4): overflow (`maximum context length`, `context length exceeded`, pi `Context overflow recovery failed`); auth (`invalid_grant`, `OAuth`, `401`, `403`); network (`ECONNREFUSED`, `fetch failed`, DNS); `Provider finish_reason:` becomes `api-error` | environment | env / harness |

### 2.2 Finding kinds

| Kind | Rule (initial thresholds; all live in `THRESHOLDS`) | Tier | Family | Fix |
|---|---|---|---|---|
| `tool-text-leak` | assistant message with `markup_leak != ""` and `tool_calls == 0` | S | tool protocol | serving / model |
| `empty-turn` | assistant message with `stop_reason == "end"`, `tool_calls == 0`, no visible text | S | generation | model / sampling |
| `degeneration` | assistant message with `repeat_ratio ≥ 0.5` (see §4) | S | generation | serving (sampling) |
| `output-truncated` | assistant message with `stop_reason == "length"` (in addition to the error class) | S | generation | harness |
| `repeat-tool` | identical `(tool, input)` ≥ 3; calls with role `poll` are exempt | S | loop | model |
| `edit-mismatch-spiral` | ≥ 3 `edit-mismatch` failures on the same path | S | loop | model |
| `read-only-spiral` | ≥ 8 consecutive `read`/`search` role calls with no `edit`/`write`/`exec` call and no assistant visible text ≥ 200 chars between them | S | loop | model |
| `file-thrash` | **retuned**: ≥ 8 edits to one path, or ≥ 5 edits of which ≥ 2 failed | S | loop | model |
| `read-loop` | **retuned**: ≥ 5 reads of one path, not counting reads that follow a compaction or an edit of that path | S | loop | model |
| `error-cascade`, `hook-pingpong`, `rejection-persist`, `agent-kill`, `orphan-subagent`, `compaction-churn` | unchanged, except `sibling-cascade` is excluded from runs, the compaction check uses the normalized `compaction` subtype, and the subagent check uses role `dispatch` | S | loop / lifecycle | as today |
| `stall` | **fixed**: roles `ask-user`, `dispatch`, `poll`, `edit`, `write` are excluded (their duration includes human approval or background work); `exec` fires at > 600 s; everything else at > 120 s | S | lifecycle | env |
| `hung` | not live, and either the end state is `interrupted-tool` on a call whose role is not `ask-user` or `dispatch`, or the last assistant message has `stop_reason == ""` (the stream never finished) | S | lifecycle | env / harness |
| `abrupt-truncation` | `last_line_malformed` and not live | S | lifecycle | — |
| `error-ignored` | a failed tool result, followed by an assistant message whose visible text matches the completion regex, with no retry of that tool before it | S→J | recovery | model |
| `give-up` | the final tool call failed and was not part of a cascade, and the session then ends with no further tool call, tail signal `apology` or `neutral` | S→J | recovery | model |
| `unverified-completion` | final visible text is a completion claim, the session made edits, and no `exec` call matching `VERIFY_COMMANDS` (pytest, unittest, `npm/pnpm test`, `cargo test`, `go test`, `make`, `tsc`, `ruff`, `ty`, `vitest`; the full list lives in `harness.py`) ran after the last edit | S→J | alignment | harness |
| `premature-stop` | end state `complete`, tail signal `next_step_stated`, no later user message | S→J | alignment | model |
| `scope-creep` | the governing ask matches review/explain verbs (`review`, `explain`, `analyze`, `don't change`, `read-only`; the full lists live in `alignment.py`) and no implement verbs (`fix`, `implement`, `add`, `change`, `refactor`, `update`, `write`), yet the turn has `edit`/`write` operations | S→J | alignment | harness |
| `drift-candidate` | focus profile (§3.4): an off-reference run holding ≥ 40% of the turn's calls and ≥ 8 calls | S→J | alignment | harness / model |
| `false-premise`, `spec-neglect`, `misread-output` | no structural signal; the skill's judgment checklist (§6) | J | alignment | model / harness |

The J-tier kinds exist in the taxonomy, so they carry hints and reference entries, but no
detector ever emits them.

## 3. Architecture

`classify.py` (413 lines) currently holds the regexes, hints, end-state and tail-signal logic,
and all detectors. It splits into four units.

### 3.1 `sessionkit/taxonomy.py`

This is the single source of truth. Each entry is a `FailureClass(name, family, tier,
fix_target, hint, patterns)`, where `patterns` holds harness variants side by side. It is
ordered, because patterns overlap.

It exposes:
- `classify_error(text) -> (class, hint)`: same signature as today; the exit-code
  pre-check is kept.
- `classify_api_error(text) -> class`
- `signature()`
- `lookup(kind) -> FailureClass`

It replaces `_RULES`, `_EXIT_CODES` and `ANOMALY_HINTS`. `THRESHOLDS` moves here too.

### 3.2 `sessionkit/harness.py`

Maps each harness's tool names to roles: `read`, `search`, `edit`, `write`, `exec`,
`dispatch`, `poll`, `ask-user`, `other`. It also records each harness's argument keys for
extracting targets (path, pattern, command).

It exposes:
- `role(harness, tool_name) -> str`
- `target(harness, call) -> str`: derived from `input_preview`, the 2,000-char JSON; for
  `exec`, the command's first word plus any path arguments.
- `VERIFY_COMMANDS`

The hardcoded `("Agent", "subagent")` check is removed in favour of role `dispatch`.

### 3.3 `sessionkit/detectors/`

The detectors are pure functions `(ParsedSession) -> list[Anomaly]`, one module per family:
`tool_protocol.py`, `generation.py`, `loops.py`, `lifecycle.py`, `recovery.py`,
`alignment.py`.

- `detectors/__init__.py` holds the registry and `detect(session)`, whose signature and
  sort order are unchanged.
- `Anomaly` keeps its shape, `kind, detail, count, lines`. Tier, family, fix target and hint
  are looked up through `taxonomy.lookup(kind)` when the report is built.
- Liveness (for `hung` and `abrupt-truncation`) is passed in by the caller from
  `live.live_sids`, so detectors do no I/O.

`classify.py` keeps `annotate_errors`, `derive_end_state` and `derive_tail_signal`.
`derive_end_state` gains `aborted` (final assistant `stop_reason == "aborted"`) and
`output-truncated` (final assistant `stop_reason == "length"`), and uses
`stop_reason == "error"` instead of the `api_error` sysevent.

### 3.4 Focus profile (`detectors/alignment.py`)

1. Split the session into **user turns**. The governing ask is the latest user message with
   text.
2. Within each turn, map every tool call to `harness.target()` and group the calls into
   **runs** of consecutive calls sharing a target key (a directory for file roles, a command
   family for `exec`).
3. A run is **referenced** if any target shares a path component or basename with the ask's
   `refs`, or sits in a directory the turn has already touched through a referenced target.
4. **Asks with no refs produce no drift candidates.** Without references `sk` cannot judge
   relevance, so the skill checks those turns itself.

`query.focus_profile(entry)` exposes the per-turn structure (ask preview, refs, runs with
their share and `referenced` flag) for `sk forensics`.

## 4. Data model (all fields additive, with defaults)

Shared helper `sessionkit/features.py` is used by both parsers, so the two harnesses
extract the same features:

- `split_content(blocks, tool_block_type) -> (visible, reasoning, tool_calls)`
- `markup_leak(text) -> str`: `tool_call-xml` for `<tool_call>`; `function-tag` for
  `<function=`; `json-call` for a bare `{"name": …, "arguments": …}` object on its own, not a
  JSON example inside prose or a code fence; `harmony` for `<|channel|>…to=functions.`.
- `repeat_ratio(text) -> float`: `1 − distinct/total` over word 8-grams; returns 0 when
  there are fewer than 50 grams.
- `extract_refs(text) -> list[str]`: paths, backticked identifiers and filenames with
  extensions, deduplicated, capped at 30.

**`Message`**

| Field | Meaning |
|---|---|
| `stop_reason` | normalized to one of `end`, `tool_use`, `length`, `error`, `aborted`, `""`. CC `end_turn`/`stop_sequence` → `end`, `max_tokens` → `length`, `isApiErrorMessage` → `error`. pi `stop` → `end`, `toolUse` → `tool_use`, plus `length`, `error`, `aborted` |
| `tool_calls` | count of structured tool calls in the message |
| `reasoning_len` | length of the reasoning text. `text_len` and `preview` become **visible text only** (this fixes reasoning being merged into the visible text in both parsers) |
| `error_text` | preview of pi's `errorMessage` or CC's API-error text; replaces the `api_error` sysevent |
| `markup_leak` | scanned over the full visible and reasoning text |
| `repeat_ratio` | computed over the full visible and reasoning text |
| `refs` | user messages only |

**`ParsedSession`**

| Field | Meaning |
|---|---|
| `harness` | `claude-code` or `pi` |
| `last_line_malformed` | the final non-empty line failed to decode |

**Normalized sysevent subtypes.** Both CC `compact_boundary` and pi `compaction` become
`compaction`. pi's compaction `usage` cost is added to the session totals.

## 5. CLI

**`sk forensics <sid>`** is the post-mortem report. Its sections:

- **Header:** harness, model, end state and reason.
- **Findings:** `tier, family, kind, count, lines, fix_target, hint`. S→J rows are
  labeled `candidate`. Rows are sorted S first, then candidates, and by count within each
  tier.
- **Focus profile:** turns containing a drift candidate are expanded (ask, refs, runs);
  other turns get one line each.
- **Timeline:** unchanged; only the lines findings cite.
- **Health:** the existing tool-call metrics, plus stop-reason counts, empty turns,
  reasoning-to-visible ratio and compactions.

**`sk findings`** (new) is the fleet aggregation of detector findings.
- `--group-by kind|model|harness|project|session`.
- Filters: `--tier`, `--family`, `--kind`, plus the shared `--since/--project/--source/--subagents`.
- Every row carries `sessions_affected`, `sessions_in_group` and a rate. When grouping by
  model, `session.model` (the last model used) is the key, and the footer notes this
  limitation.

**Additions to existing commands**
- `sk errors --group-by model|harness`, with the same rate columns.
- `sk index --finding <kind>`.

**Unchanged:** Markdown/`--json` output and `--budget-kb`. There is no `sk postmortem`
command, because the narrative is the skill's job.

## 6. Skills

**`session-forensics`** becomes the post-mortem skill. It keeps its name and adds the
triggers "post-mortem", "what went wrong with this agent" and "why did it drift". Its
procedure:

1. **Select the session:** by id, or with `sk index --finding` / `--state`.
2. **Run `sk forensics <sid> --json`.**
3. **Triage by tier:**
   - S findings are accepted, but their cited lines are still read to explain why.
   - Each S→J candidate is confirmed or dismissed with one line of evidence, from
     `sk show --mode messages --range`. Dismissals are reported.
4. **Judgment checklist** (always run, and bounded):
   - **False premise:** walk back from the first failure to the first unverified
     assumption.
   - **Spec neglect:** the ask's requirements, each marked done or not done.
   - **Misread output:** a tool result that contradicts the next action.
   - For asks without refs, a drift check by reading the turn.
5. **Decisive error:** the earliest step after which failure was inevitable. Cite it
   separately from the line where the failure surfaced.
6. **Prescribe fixes by target**, per harness:

   | Target | Claude Code | pi |
   |---|---|---|
   | Instructions | `CLAUDE.md`, hooks | `AGENTS.md` (pi also reads `CLAUDE.md`), `.pi/SYSTEM.md`, a `tool_call` extension |
   | Harness | `settings.json` | `models.json` (`maxTokens`, `compat`), `settings.json` (thinking level) |
   | Serving | — | the vLLM tool-call parser flag, chat template, sampling |
   | Model | — | — |
   | Env | auth, network, rate limits | same |

   A model swap may be recommended for either harness, but only with the rate from
   `sk findings --group-by model`.
7. **Report:** a one-line verdict; the decisive error; confirmed and dismissed findings;
   checklist results; fixes ranked by expected impact; health.

**Knowledge base: `skills/session-forensics/reference/failure-classes.md`.** It has one entry
per taxonomy class, covering what the class is, how to confirm it, its common causes and the
fixes per harness. It is loaded only for the classes a report actually contains.

**`error-patterns`**
- Add a `sk findings` pass.
- Add a model/harness comparison that uses rates only.
- Replace the hardcoded `SK=` path with the shared resolution block.

**Rules**
- Read-only: propose fixes, never apply them.
- A clean session is a valid result.
- Never claim coverage you don't have.
- State small samples explicitly, e.g. "pi: 4 sessions, rates not meaningful".

## 7. Testing and validation

1. **Refactor first, with no behavior change.** The §3 split lands with all 297 existing
   tests passing unmodified.
2. **Each class gets a positive and a near-miss negative test**, built from inline fixtures
   (`tests/fixtures.py`, `tests/fixtures_pi.py`). pi cases use the exact strings from pi's
   source (§A.2); CC cases use signatures from the corpus. The near-miss cases guard the noisy
   detectors: iterative edits that succeed are not `file-thrash`, and an approval wait is not
   a `stall`.
3. **`features.py` gets table-driven tests.** They cover each leak format, a JSON example
   inside prose (not a leak), degenerate versus normal text, and refs extraction.
4. **Parity tests.** Every taxonomy class has an entry in `reference/failure-classes.md`, and
   every role in `harness.py` is used by at least one detector.
5. **Corpus acceptance.** Before and after numbers go in the PR:

   | Metric | Before | Target |
   |---|---|---|
   | `stall` share of sessions flagged | 33.2% | < 10% |
   | `other` error bucket | 459 | at most 230 |
   | `file-thrash` hits with a real edit failure | 14/145 | majority |
   | pi bash failures classified | 0% | 100% |
   | `read-only-spiral` share of sessions flagged | — | ≤ 5% (tune the threshold if not) |

6. **Real-session validation.** Targeted pi runs on local models: a session that hits
   compaction, an Esc abort (run by the user, since it is interactive), forced `length`
   truncation, and a qwen long-context leak attempt. The truncation run needs a low
   `maxTokens` model entry in `~/.pi/agent/models.json`, added only with the user's approval.
   Any surprises are folded back into the fixtures.

## 8. Implementation phases

1. **Refactor:** taxonomy, harness and detectors modules, with no behavior change (§7.1).
2. **Data model and parser fixes:** the `features.py` fields in §4, the normalized sysevents
   and the `stop_reason` mapping. This fixes the known pi and CC bugs listed below.
3. **Taxonomy extension:** the §2.1 error classes.
4. **New and retuned detectors** (§2.2), including the focus profile.
5. **CLI:** `sk forensics` sections, `sk findings`, and the `errors`/`index` additions.
6. **Skills:** the `session-forensics` procedure, `reference/failure-classes.md` and the
   `error-patterns` updates.
7. **Validation:** corpus acceptance (§7.5) and the real pi sessions (§7.6).

**Known bugs fixed in phase 2 or 3** (phase 3 fixes the exit-code pattern):
- **pi bash failures are unclassified.** The exit status sits at the end of the output.
- **Compaction detectors never fire for pi.** The sysevent subtype is `compaction`, not
  `compact_boundary`.
- **Reasoning is merged into visible text** in both parsers.
- **pi's `length` and `aborted` stop reasons are dropped,** so an aborted final turn reads as
  `complete`.
- **API errors are never classified.** CC API errors are not recorded at all.
- **pi compaction cost is missing** from the session totals.

## Appendix A: evidence

### A.1 Literature
- **arXiv 2607.09510**, Terminal-bench trajectories: 1,184 failed runs across 7 models,
  including Qwen3-Coder, DeepSeek and Devstral.
  - Decisive errors were 57.9% epistemic, 32.8% competence and 9.4% environment.
  - False premise was the largest cause, at 30.7%.
  - The median decisive error came at step 7 and surfaced about 10 steps later.
  - A monitor given the task spec reached 28.8% recall.
- **arXiv 2607.28802:** 41 failure modes, each attributed to the model side or the harness
  side. This is the basis for the fix-target column in §2.
- **arXiv 2503.13657 (MAST):** failures in multi-agent systems.
- **Open-model tool-call issues:** QwenLM/qwen-code#8003, vllm-project/vllm#33965, the
  openai/gpt-oss-20b HF discussion 80, ollama/ollama#17353, anomalyco/opencode#16488, and the
  smallcode CHANGELOG (read-only spiral: nudge at 5, stop at 8).

### A.2 pi 0.84.1 mechanics (source-verified)
- **stopReason values:** `pending|stop|length|toolUse|error|aborted|deferred`.
- **vLLM `finish_reason` mapping:** `length` → `length`; `content_filter` or an unknown value
  → `error` with the message `Provider finish_reason: <x>`.
- **Malformed tool-call args are silently repaired to `{}`,** which then fails validation
  with `Validation failed for tool "X": … Received arguments: {}`.
- **Unknown tool:** `Tool X not found`.
- **`length` stop:** every tool call in the message fails with `Tool call "X" was not
  executed: the response hit the output token limit…`.
- **Tool call leaked as text:** no recovery. The text block contains the markup, stopReason
  is `stop`, there is no toolCall, and the run ends idle.
- **Bash:** `…\n\nCommand exited with code N`, `Command timed out after N seconds`,
  `Command aborted`.
- **Esc abort:** `Operation aborted`.
- **Overflow:** an error message in the assistant `errorMessage`, then one compaction and a
  retry. The errored message stays in the file.
- **Retries are not written to the JSONL.**
- **Reasoning:** `thinking` blocks, with `usage.reasoning`.
- **No built-in per-tool permission prompt.** Extension blocks produce `Tool execution was
  blocked`.

### A.3 Local corpus (2026-09-27)
- **Claude Code and pi:** 1,176 sessions (1,172 CC, of which 560 were subagents; 4 pi).
  - 26,004 tool calls, of which 1,984 failed (7.6%).
  - The `other` bucket held 459 failures: about 240 were structured `{"ok": false}` errors
    from MCP tools, 36 were EISDIR, 31 were worktree-isolation blocks, 11 were
    sibling-errored, and 10 were misclassified permission denials.
  - Tool-calling problems made up 8.5% of failures.
  - The largest tail-signal bucket was `neutral`, with 491 sessions.
- **pi:** the sessions show no failures beyond OAuth errors.
- **codex**, for its future spec: 43 sessions, all on OpenAI models.
  - The format is rollout JSONL (`session_meta`, `turn_context`, `response_item`,
    `event_msg`).
  - The error signal is `event_msg.item_completed.item.status`.
  - There were 17 `turn_aborted` events.
  - `wait_agent` polling loops (one session repeated one call 65 times) call for a `poll`
    role.
