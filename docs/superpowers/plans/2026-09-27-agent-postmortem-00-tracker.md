# Agent Post-Mortem — Implementation Tracker

The fast view of implementation progress. Each checkpoint file holds its full tasks: exact code,
tests and commands.

**Spec:** `docs/superpowers/specs/2026-09-27-agent-postmortem-design.md`

**How to use:**
- Work one checkpoint at a time, in order.
- When a task's commit lands, tick its line below and fill in the commit's short SHA.
- When a checkpoint's last task is ticked and the full suite passes, set the checkpoint's
  status to `done`.
- Add a Log line for anything a later checkpoint needs to know, such as a tuned threshold, a
  deviation, or a surprise.

**Status values:** `todo` · `in progress` · `done` · `blocked (reason)`

## Checkpoints

| CP | File | Tasks | Delivers | Status |
|---|---|---|---|---|
| 1 | [`2026-09-27-agent-postmortem-01-foundation.md`](2026-09-27-agent-postmortem-01-foundation.md) | 1, 2 | `taxonomy.py`, `harness.py` and the `detectors/` package replace the monolithic `classify.py`, with no behavior change. | todo |
| 2 | [`2026-09-27-agent-postmortem-02-parsers.md`](2026-09-27-agent-postmortem-02-parsers.md) | 3, 4, 5, 6 | Parse-time features and new `Message`/`ParsedSession` fields in both parsers; the pi and Claude Code parser bugs are fixed; pi-subagents async runs are loaded and linked. | todo |
| 3 | [`2026-09-27-agent-postmortem-03-detectors-core.md`](2026-09-27-agent-postmortem-03-detectors-core.md) | 7, 8, 9, 10 | The new and widened error classes; the tool-protocol, generation, loop and lifecycle detectors; the new end states; liveness passed into the detectors. | todo |
| 4 | [`2026-09-27-agent-postmortem-04-detectors-judgment.md`](2026-09-27-agent-postmortem-04-detectors-judgment.md) | 11, 12, 13 | The recovery and alignment candidates, the focus profile, drift candidates, resource interference, and the registry completeness tests. | todo |
| 5 | [`2026-09-27-agent-postmortem-05-cli.md`](2026-09-27-agent-postmortem-05-cli.md) | 14, 15 | `sk forensics` as the post-mortem report; `sk findings` with rates; `errors --group-by model\|harness`; `index --finding`. | todo |
| 6 | [`2026-09-27-agent-postmortem-06-skills-validation.md`](2026-09-27-agent-postmortem-06-skills-validation.md) | 16, 17, 18 | The failure-class knowledge base, the post-mortem and fleet skills, the README, corpus acceptance, the dry-run regression and real pi sessions. | todo |

## Tasks

- [ ] **T1** (CP1) `taxonomy.py`, the single failure table (refactor, no behavior change) — commit: `—`
- [ ] **T2** (CP1) `harness.py` tool roles and the `detectors/` package (refactor, no behavior change) — commit: `—`
- [ ] **T3** (CP2) `features.py`, the parse-time text features — commit: `—`
- [ ] **T4** (CP2) Data-model fields, populated by the Claude Code parser — commit: `—`
- [ ] **T5** (CP2) The pi parser populates the same fields, and end state reads `stop_reason` — commit: `—`
- [ ] **T6** (CP2) Load pi-subagents async child runs and link them to their parent — commit: `—`
- [ ] **T7** (CP3) New and widened error classes, and API-error classification — commit: `—`
- [ ] **T8** (CP3) Tool-protocol and generation detectors — commit: `—`
- [ ] **T9** (CP3) Loop detectors (new and retuned) — commit: `—`
- [ ] **T10** (CP3) Lifecycle detectors, end states, and liveness plumbing — commit: `—`
- [ ] **T11** (CP4) Recovery and alignment candidates — commit: `—`
- [ ] **T12** (CP4) Focus profile and drift candidates — commit: `—`
- [ ] **T13** (CP4) Cross-session resource interference, and registry completeness — commit: `—`
- [ ] **T14** (CP5) `sk forensics` becomes the post-mortem report — commit: `—`
- [ ] **T15** (CP5) `sk findings` fleet view, `errors --group-by model|harness`, and `index --finding` — commit: `—`
- [ ] **T16** (CP6) The failure-class knowledge base and parity tests — commit: `—`
- [ ] **T17** (CP6) The post-mortem skill, the fleet skill, and the README — commit: `—`
- [ ] **T18** (CP6) Validation — corpus acceptance, dry-run regression, real pi sessions — commit: `—`

## Log

- 2026-09-27 — Plan split into 6 checkpoints; no implementation started.

---

## Global Constraints

- **Language and dependencies:** system Python 3.11+ with zero third-party imports.
- **Imports:** absolute only (`from sessionkit.x import y`).
- **Size limits:** lines ≤ 100 characters, functions ≤ 100 lines.
- **Data model:** changes are **additive fields with defaults**. The one deliberate meaning
  change is that `Message.text_len` and `Message.preview` become *visible text only*, with
  reasoning moving to `Message.reasoning_len` (spec §4).
- **Tests:** `unittest`, built from inline fixture builders (`tests/fixtures.py`,
  `tests/fixtures_pi.py`, `tests/detector_fixtures.py`). Never add committed `.jsonl` files.
- **Test command:** run from the worktree root:
  `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
- **Existing tests:** when an existing test fails, it must be one this task names as an
  intended change. Any other failure is a regression to fix in the code, not in the test.
- **Thresholds:** every detector threshold lives in `taxonomy.THRESHOLDS`.
- **Out of scope:** no LLM calls inside `sk`, no live guard, no codex or opencode parser.
- **`test/` holds real session transcripts** and is gitignored because they contain work
  endpoints. Never commit it, and never copy its text into fixtures; rebuild the *shapes*
  instead.

## Spec deviations decided while planning

1. **`harness.target()` becomes two functions:** `run_key(harness, call)` (what groups calls
   into a run) and `call_paths(harness, call)` (the paths a call touches). The focus profile
   needs both separately.
2. **Two extra additive fields, `Message.claims_done` and `ParsedSession.final_text`,** so the
   recovery and alignment detectors see completion claims past the 200-char preview without
   file I/O.
3. **Tier strings are `S`, `S->J` and `J`, and "no fix target" is written `none`.** This keeps
   CLI flags ASCII.
4. **pi's `Request timed out.` API error classifies as `network`.**
5. **The drift candidate that `user-interrupt` produces cites up to 8 calls immediately before
   the interrupt,** within the interrupted turn.
6. **Error classes with fix target `none` are not surfaced as findings:** `aborted`,
   `sibling-cascade`, `user-rejected`, and the generic classes. `user-interrupt` covers aborts.
7. **The `hung` rule "last assistant message has an empty stop reason" applies to pi only.**
   Claude Code writes `stop_reason: null` on every intermediate per-block record, so on
   Claude Code sessions the rule would fire constantly.

## Review Focus

1. **Claude Code multi-record assistant turns.** Claude Code writes one record per content
   block, and most of those records have `stop_reason: null`. They must not fire `hung`,
   `empty-turn` or `generation-stall` (tests in Tasks 8 and 10).
2. **Empty or mixed-format timestamps.** A message or tool result with an empty timestamp,
   or with `+00:00` instead of `Z`, must be skipped by the latency and interference checks,
   never cause an exception (tests in Tasks 3, 8 and 13).
3. **A tool input cut off by the 2,000-char `input_preview` cap** is invalid JSON. Path and
   command extraction must fall back to a regex, not raise (test in Task 2).
4. **A header-only session with no messages or tools** must give `detect()` → `[]` and
   `derive_end_state()` → `unknown` (test in Task 13).
5. **A pi-subagents run folder with a missing or malformed `meta.json`, or no
   `session_info` name,** must still load as a subagent linked to its parent (test in
   Task 6).
