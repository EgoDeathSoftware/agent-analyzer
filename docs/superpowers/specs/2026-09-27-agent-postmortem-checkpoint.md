# Agent Post-Mortem Analysis — Brainstorming Checkpoint

Status: **design in progress** (brainstorming, architectural path). Not yet an approved spec.
This file captures decisions and research so far; it becomes the input to the written spec.

## Goal

`sk` + skills diagnose troubled coding-agent sessions across agent families (Claude Code and
pi now; codex and opencode next): identify *what* went wrong (crash, stall/hang, loop,
drift/hyperfocus, tool-calling failure), explain *why*, and suggest fixes aimed at the right
target (model, harness/config, environment). Particular emphasis on open-model agents running
on local vLLM (`prana`: qwen, gpt-oss, gemma, nemotron).

## Decisions made

| Decision | Detail |
|---|---|
| Scope | A (detectors) + B (post-mortem skill) together, as one spec |
| Live guard | **Deferred** to a future version (run `sk` against an active session, nudge/halt via Claude Code `PreToolUse` hook / pi `tool_call` extension). Detector design should stay incremental-friendly so it isn't rebuilt later, but nothing is built for live use now |
| Other families | codex (43 local sessions, rollout JSONL) and opencode (SQLite) parsers come **after** this work, each as its own spec |
| Detection split | `sk` detects and cites; the skill confirms, explains, prescribes. Three tiers: **S** (structural, `sk` alone), **S→J** (`sk` emits a line-cited candidate, skill confirms/dismisses), **J** (skill judgment only) |
| Drift | Hybrid: `sk` emits a per-user-turn *focus profile* (the ask, then effort runs grouped by file/dir/command family with their share of the turn); runs touching nothing the ask referenced are flagged `drift-candidate`; the skill confirms against the timeline. The task is the *latest* user message, not only the first prompt |
| Fix attribution | Every class carries a fix target: model (swap/sampling), harness (`AGENTS.md`/`CLAUDE.md`, tool config, serving parser, max_tokens), or env |

## Phase 0 — bugs found in the shipped pi support

Found by the harness-mechanics research (pi 0.84.1 source); Task 7 missed them because the
only real pi sessions had no failures.

1. `classify.py` `_EXIT_RE` / `exit-code` rule anchor on `^exit code`; pi appends
   `\n\nCommand exited with code N` at the **end** of bash output → every pi bash failure lands
   in `other`.
2. `parse_pi.py` emits sysevent subtype `compaction`; `classify.py` (end-state and
   `_compaction_churn`) looks for `compact_boundary` → compaction detectors never fire for pi.
   Compaction `usage` cost also isn't added to session totals.
3. `parse_pi._text_of` merges `thinking` blocks into assistant text → `text_len` and tail-signal
   regexes (apology/done/next-step) can fire on reasoning instead of the visible answer.
4. Only `stopReason: "error"` is recorded. `length` (output truncated) and `aborted` (Esc) are
   dropped; an aborted final turn reads as `complete`. `rawStopReason`, `diagnostics`,
   `usage.reasoning` are also ignored.
5. API errors live in the assistant `errorMessage` and are kept only as a 120-char `api_error`
   sysevent — never classified (overflow, `Provider finish_reason: …`, connection errors).

## Failure taxonomy (approved in principle)

| Family | Class | Evidence | Tier | Fix target |
|---|---|---|---|---|
| Tool protocol | `tool-text-leak` | assistant text contains `<tool_call>` / `{"name":…,"arguments"` / `<function=`, no toolCall block, run ends idle | S | harness (vLLM parser) / model |
| | `args-dropped` | validation error with `Received arguments: {}` (pi `parseStreamingJson` silently returns `{}` on unparseable args) | S | harness (parser) |
| | `invalid-args` | `Validation failed for tool "X"`, `InputValidationError`, MCP `Invalid arguments` | S | model |
| | `unknown-tool` | `Tool X not found`, `Unknown skill: …` | S | model / harness |
| | `output-truncated` | stopReason `length`; `Tool call "X" was not executed: the response hit the output token limit` | S | harness (max_tokens) |
| | `edit-mismatch` | pi: `Could not find the exact text in`, `Found N occurrences`, `No changes made to`, `edits[a] and edits[b] overlap`; CC: `String to replace not found`, `Found N matches` | S | model |
| | `tool-misuse` | `EISDIR`, `Offset N is beyond end of file` | S | model / harness |
| | `sibling-cascade` | `Sibling tool call errored` | S | — (collapse into root error) |
| Generation | `empty-turn` | assistant with no text and no tool call, stop=`stop` (reasoning-only) | S | model / sampling |
| | `degeneration` | high repeated n-gram ratio within one message | S | sampling |
| | `api-error` / `context-overflow` / `aborted` | classified from `errorMessage` / stopReason | S | env / harness |
| Loops | `repeat-tool` | identical calls ≥3; **polling tools exempt** (codex `wait_agent` spun 65×) | S | model |
| | `edit-mismatch-spiral` | repeated edit failures on one file | S | model |
| | `read-only-spiral` | N consecutive read/search calls with no write or answer | S | model |
| | existing: `file-thrash`, `read-loop`, `error-cascade`, `hook-pingpong`, `rejection-persist` | retuned (below) | S | varies |
| Lifecycle | `stall` | **fixed**: exclude approval-gated tools, AskUserQuestion, subagents; Bash ≥600s | S | env |
| | `hung` | ends on an unresolved call or an unfinished assistant turn — never raw wall-clock gaps | S | env / harness |
| | `abrupt-truncation` | partial last line / pending call with no live process | S | — |
| Recovery | `error-ignored` | error result then success claim, no retry | S→J | model |
| | `give-up` | one error then apology/end | S→J | model |
| Alignment | `drift-candidate` | focus profile | S→J | harness (prompt) / model |
| | `unverified-completion` | completion claim, no test/build after last edit | S→J | harness (`AGENTS.md`) |
| | `premature-stop` | ends on a stated next step | S→J | model |
| | `scope-creep` | ask was review/explain, session wrote files | S→J | harness |
| | `false-premise`, `spec-neglect`, `misread-output` | none structural | J | model / harness |
| Environment | `rate-limit`, `auth`, `network`, `sandbox-denied`, `mcp-error`, `tool-error-structured` (`{"ok": false, "errors": …}`) | error text | S | env |

### Calibration changes (from corpus data, 1,176 sessions)

| Detector | Now | Proposal | Evidence |
|---|---|---|---|
| `stall` | 120s any tool | exclusions above | flags 33.2% of sessions; `dur_ms` includes human approval and background work |
| `file-thrash` | 5 | 8, or require intervening errors/re-reads | only 14/145 hits have an edit error on a cited line |
| `read-loop` | 4 | 5–6, discount reads after compaction/edit | 4.8% flagged at 4, 1.6% at 6 |
| `repeat-tool` | 3 | keep | 4.2% flagged |
| `error-cascade` | 3 | keep (maybe 4 for the anomaly, 3 for end_state) | 8.5% flagged |
| session gap | — | don't use raw gaps | max gap ≥30 min in 15.2% of sessions — mostly the human away |

## Research summary

**Literature** (sources to cite in the spec):
- Terminal-bench trajectory study, arXiv 2607.09510 — 1,184 failed runs, 7 models incl.
  Qwen3-Coder/DeepSeek/Devstral. 57.9% epistemic / 32.8% competence / 9.4% environment;
  false premise largest decisive cause (30.7%); median decisive error at step 7, surfacing ~10
  steps later; a monitor given the task spec reaches 28.8% recall → drift checks need the ask.
- Interaction-centric taxonomy, arXiv 2607.28802 — 41 modes attributed model- vs harness-side
  (basis for the fix-target column); GLM-5.1 130-write loop; false success after errors.
- MAST, arXiv 2503.13657 — multi-agent failure taxonomy (verification, inter-agent misalignment).
- Open-model tool-call issues: QwenLM/qwen-code#8003 (XML leak at long context),
  vllm-project/vllm#33965 (parser-corrupted args), openai/gpt-oss-20b discussion 80,
  ollama/ollama#17353, anomalyco/opencode#16488 (truncation → 400), smallcode CHANGELOG
  (read-only spiral thresholds 5 nudge / 8 stop; "poisoned history" spiral).

**Corpus** (Claude Code 1,172 + pi 4): 26,004 tool calls, 1,984 failed (7.6%). `other` = 459
(23%), of which ~240 are structured `{"ok": false}` MCP errors (one tool), 36 EISDIR, 31
worktree-isolation blocks, 11 sibling-errored, 10 misclassified permission denials. Tool-calling
problems = 8.5% of failures. `tail_signal` `neutral` (491) is the largest bucket and not
diagnostic alone. pi: no failure evidence beyond OAuth errors.

**Codex** (43 sessions, all OpenAI models): rollout JSONL (`session_meta`, `turn_context`,
`response_item`, `event_msg`); cleanest error signal is `event_msg.item_completed` with
`item.status`; 17 `turn_aborted`; `wait_agent` polling loops dominate repeats. Input for the
later codex parser spec.

**Known gap:** open-model classes (`tool-text-leak`, `args-dropped`, `empty-turn`,
`degeneration`) are grounded in literature and pi source, not in local data. Build from pi's
exact strings; validate against real failures as they accumulate.

## Still to do

Brainstorming (remaining design sections, each needs approval):
1. **Architecture** — where detectors live (`classify.py` is 414 lines; likely split into a
   taxonomy module + a detectors package), keeping the detector interface incremental-friendly.
2. **Data model** — additive fields on `Message` (stop reason, has-tool-call, reasoning length
   separate from text) and how parsers populate them for Claude Code and pi.
3. **CLI output** — how `sk forensics` presents tiers, S→J candidates, fix targets, and the
   focus profile; fleet aggregation by model/harness (e.g. `sk errors --group-by model`).
4. **Skill** — `session-forensics` post-mortem procedure: confirm candidates, J-tier checks
   (false premise, spec neglect, misread output), fix-target-aware prescriptions (`AGENTS.md`
   vs `CLAUDE.md` vs model/sampling vs vLLM parser).
5. **Testing & validation** — fixtures per class from exact harness strings; calibration check
   against the corpus; how new real pi failures get folded in.

Then: write the spec, self-review, user review, `writing-plans`, implement (phase 0 first).

Data to gather (helps implementation, not blocking design): organic pi sessions that went
badly; targeted provocations — long session hitting compaction, an Esc abort, a low
`maxTokens` model entry to force `length`, qwen at long context to try to provoke
tool-call leakage.
