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
| 1 | [`2026-09-27-agent-postmortem-01-foundation.md`](2026-09-27-agent-postmortem-01-foundation.md) | 1, 2 | `taxonomy.py`, `harness.py` and the `detectors/` package replace the monolithic `classify.py`, with no behavior change. | done |
| 2 | [`2026-09-27-agent-postmortem-02-parsers.md`](2026-09-27-agent-postmortem-02-parsers.md) | 3, 4, 5, 6 | Parse-time features and new `Message`/`ParsedSession` fields in both parsers; the pi and Claude Code parser bugs are fixed; pi-subagents async runs are loaded and linked. | done |
| 3 | [`2026-09-27-agent-postmortem-03-detectors-core.md`](2026-09-27-agent-postmortem-03-detectors-core.md) | 7, 8, 9, 10 | The new and widened error classes; the tool-protocol, generation, loop and lifecycle detectors; the new end states; liveness passed into the detectors. | done |
| 4 | [`2026-09-27-agent-postmortem-04-detectors-judgment.md`](2026-09-27-agent-postmortem-04-detectors-judgment.md) | 11, 12, 13 | The recovery and alignment candidates, the focus profile, drift candidates, resource interference, and the registry completeness tests. | done |
| 5 | [`2026-09-27-agent-postmortem-05-cli.md`](2026-09-27-agent-postmortem-05-cli.md) | 14, 15 | `sk forensics` as the post-mortem report; `sk findings` with rates; `errors --group-by model\|harness`; `index --finding`. | done |
| 6 | [`2026-09-27-agent-postmortem-06-skills-validation.md`](2026-09-27-agent-postmortem-06-skills-validation.md) | 16, 17, 18 | The failure-class knowledge base, the post-mortem and fleet skills, the README, corpus acceptance, the dry-run regression and real pi sessions. | todo |

## Tasks

- [x] **T1** (CP1) `taxonomy.py`, the single failure table (refactor, no behavior change) — commit: `68ffd17`
- [x] **T2** (CP1) `harness.py` tool roles and the `detectors/` package (refactor, no behavior change) — commit: `ed94928`
- [x] **T3** (CP2) `features.py`, the parse-time text features — commit: `4e5208e`
- [x] **T4** (CP2) Data-model fields, populated by the Claude Code parser — commit: `9dc6531`
- [x] **T5** (CP2) The pi parser populates the same fields, and end state reads `stop_reason` — commit: `32b4354`
- [x] **T6** (CP2) Load pi-subagents async child runs and link them to their parent — commit: `ac5c5c5`
- [x] **T7** (CP3) New and widened error classes, and API-error classification — commit: `eb3a40f`
- [x] **T8** (CP3) Tool-protocol and generation detectors — commit: `0d71491`
- [x] **T9** (CP3) Loop detectors (new and retuned) — commit: `5435d42`
- [x] **T10** (CP3) Lifecycle detectors, end states, and liveness plumbing — commit: `68a85e5`
- [x] **T11** (CP4) Recovery and alignment candidates — commit: `2e0556e`
- [x] **T12** (CP4) Focus profile and drift candidates — commit: `d92f246`
- [x] **T13** (CP4) Cross-session resource interference, and registry completeness — commit: `0d7d5bb`
- [x] **T14** (CP5) `sk forensics` becomes the post-mortem report — commit: `fc21d6d`
- [x] **T15** (CP5) `sk findings` fleet view, `errors --group-by model|harness`, and `index --finding` — commit: `fe4ff75`
- [ ] **T16** (CP6) The failure-class knowledge base and parity tests — commit: `—`
- [ ] **T17** (CP6) The post-mortem skill, the fleet skill, and the README — commit: `—`
- [ ] **T18** (CP6) Validation — corpus acceptance, dry-run regression, real pi sessions — commit: `—`

## Log

- 2026-09-27 — Plan split into 6 checkpoints; no implementation started.
- 2026-09-27 — CP1 done (commits 68ffd17, ed94928, plus fix-wave commit 0d59330 for two
  Important parsing bugs a final review found in the plan's prescribed `harness.py`:
  `command_program` didn't split multi-line commands on newline, and `command_of`'s
  truncated-preview fallback returned JSON-escaped text unescaped). 323/323 tests passing.
- 2026-09-27 — For CP3: `classify_error`'s `_EXIT_CODES` inline hints for `killed`/`timeout`
  differ in text from `taxonomy.lookup("killed"/"timeout").hint` — same failure class, two
  hint strings depending on lookup path. Left as-is in CP1 since CP3 rewrites error classes;
  worth unifying there.
- 2026-09-27 — Known, deliberately unfixed gap in `harness.command_program`: a command
  starting with `set -e` (or another shell builtin not in the `cd`-style skip-list) followed
  by the real command on the next line still returns `"set"` as the program, not the real
  one. Revisit if CP4's focus-profile grouping shows this matters on real corpus data.
- 2026-09-28 — CP2 done (commits 4e5208e, 9dc6531, 32b4354, ac5c5c5, plus fix-wave commit
  e7ce888). Fix wave addressed one Important regression a final review found: normalizing
  Claude Code's `compact_boundary` and pi's native `compaction` sysevent subtype into one
  name (needed so `derive_end_state`'s existing `compacted-idle` check kept working for
  Claude Code) incidentally made that check reachable for pi too — and pi's sparse sysevent
  stream meant one early compaction in an 80-turn healthy session still misclassified as
  `compacted-idle`. Fixed by keying the check on message position (compaction after the last
  user message) instead of "last 5 sysevents"; the identical heuristic was also corrected in
  the not-yet-implemented CP3 plan doc so CP3 doesn't reintroduce it. 365/365 tests passing.
- 2026-09-28 — For CP3: pi's `aborted`/`length` stop reasons still fall through to `complete`
  in `derive_end_state` — CP3 is expected to add dedicated end states for these (per the
  spec's stop-reason vocabulary already carrying `aborted`).

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
- 2026-09-28 — CP3 done (commits eb3a40f, 0d71491, 5435d42, 68a85e5, plus fix-wave commits
  d7a4148..fd5a7f3). 423/423 tests passing. A final review ran the detectors on the real local
  corpus (1,229 Claude Code sessions) and found the plan's detectors misread Claude Code's
  per-block records: Claude Code writes the final `stop_reason` on *every* block record of one
  API message (a thinking-only record carries `end_turn` too), so `empty-turn` was ~98% false.
  Fixed by two additive fields: `Message.msg_id` (records of one message are grouped by it in
  `empty_turn`, `tool_text_leak`, `output_truncated` via `detectors.base.assistant_turns`) and
  `Message.is_prompt` (only typed user prompts count as `user-interrupt` redirects). Also:
  `<synthetic>` records are skipped and equal timestamps tie-break by line in
  `assistant_latencies`; the plan's `This session is isolated in the worktree` policy-denied
  pattern was restored; pi's trailing `Command exited/timed out/aborted` line is kept in long
  tool-result previews; the `aborted` pattern is anchored to the end of the body.
- 2026-09-28 — For CP4/CP5: group Claude Code records by `msg_id` for any per-turn measure
  (CP5's median turn latency especially — per-block gaps drag the median down). `is_prompt` is
  not yet used for `turns`/`first_prompt`/`refs`; harness-injected records still count there.
- 2026-09-28 — For CP5: pi has no live-session registry, so `hung`/`abrupt-truncation` can fire
  on a pi session that is still running; consider a recent-mtime heuristic. `<synthetic>`
  Claude Code API-error records get no latency (they still surface as error-class findings).
- 2026-09-28 — For CP6 (T16–T18): 110 of 350 Claude Code exec stalls ran past their own declared
  timeout, i.e. time spent waiting on permission approval, not the command; `read-only-spiral`
  fires in 128/1,229 Claude Code sessions, 94 of them subagents — retune (exempt runs ending in a
  substantial answer, or read-only subagent types); the 4 `tool-text-leak` hits are prose that
  discusses `<tool_call>` markup — tighten `features.markup_leak`; dry-run `01a0db1d` also yields
  a `generation-stall` at line 54 (11 min vs a 100 s median) that the spec's expected list lacks;
  `skills/unfinished-work/SKILL.md` doesn't know the `aborted`, `output-truncated` and
  `agent-unresponsive` end states; unify the `_EXIT_CODES` killed/timeout hint text with the
  taxonomy entries (deferred from CP3).
- 2026-09-28 — CP4 done (commits 2e0556e, 6f7afa8, d92f246, 0d7d5bb, plus final-review fix wave
  7521d22, 11e3212). Suite: 470/470. Rulings that amend the plan:
  `alignment.user_turns` and interrupt drift select asks by `is_prompt` (not `text_len > 0`);
  `error_ignored` groups Claude Code records by `msg_id` and cites the claiming record;
  `classify._NEXT_STEP_RE` no longer treats "let me know" as a next step — this also changes
  `derive_tail_signal`, which `skills/unfinished-work` reads; `alignment._IMPLEMENT` matches
  inflected verbs; Claude Code teammate relays (`<teammate-message`, "Another Claude session sent
  a message:") are no longer prompts, so they no longer split turns or count as
  `user-interrupt` redirects, and relay-only teammate sessions have no user turns; `give_up`
  requires end state `complete` (it blamed the model for API-error endings);
  `unverified_completion` ignores doc-only changes (`.md`/`.rst`/`.txt`); drift findings cite
  distinct lines (pi puts parallel calls on one line). Real-corpus rates after tuning:
  give-up 11, unverified-completion 22, premature-stop 7, scope-creep 8, drift-candidate ~112 (106 are the user-interrupt form),
  resource-interference 1, of ~1,245 sessions.
- 2026-09-28 — For CP5: `corpus.load_session()` (the `sk show <sid>` path) never runs the
  cross-session pass, so `resource-interference` appears in `sk forensics` but not `sk show`.
  `Run.referenced` defaults to `True` when the turn's ask has no refs; `focus_profile_rows`
  should print `-` for `referenced` when `turn.refs` is empty, not `yes`.
- 2026-09-28 — For CP6 tuning (T18): `scope_creep` reads only the 200-char ask preview;
  `_IMPLEMENT` misses verbs such as sort/organize; `premature_stop` flags "let me X" that the
  same message fulfils; refs are turn-scoped (a later turn loses earlier turns' refs);
  `_program_named` misses wrapper-heavy exec runs (ssh); `_names`'s bare-token arm is a raw
  substring match; run fragmentation (a detour split into many 1–3-call runs never reaches
  `drift_min_calls`); `give_up`'s `trailing_errors == 1` means 2 trailing errors is neither a
  give-up nor a cascade; the registry test checks for an `Anomaly("kind"` literal, not that the
  detector is in `DETECTORS`; 42 Claude Code sessions end on injected user records (`/exit`
  stdout) and read as `interrupted-user` because `_user_end` keys on role, not `is_prompt`,
  hiding them from the tail detectors.
- 2026-09-28 — CP5 done (commits fc21d6d, fe4ff75, 72c80f2, plus final-review fix wave 6ec6b4f,
  7ee583a). Suite: 494/494. Rulings that amend the plan: `forensics_health` counts stop reasons
  and empty turns per whole model message (`assistant_turns`), and `median_latency_s` uses only
  the first record of each `msg_id`; `reasoning_ratio` is `None` when the session recorded no
  reasoning text (current Claude Code redacts thinking, so 0 was misleading on ~88% of
  sessions); the report renders Health right after Findings and the focus profile drops 0-call
  turns (Health was cut by the 8 KB budget on 82/699 real sessions); `referenced` prints `-` when
  the ask named no refs or the run touched no paths; `index --finding` ignores index's default
  3-day window; `errors --group-by model|harness` shows `reach` and `sessions_in_group` instead
  of `fix`/`exemplar`; the small-sample note is skipped for `findings --group-by session`;
  unknown finding kinds count as tier `S` in every view.
- 2026-09-28 — For CP6: `skills/session-forensics/SKILL.md` still says the report reads
  Findings, Timeline, Health; it is now Findings, Health, Focus profile, Timeline (T17). 3/700
  real sessions still lose Health in `--json` because their Findings table alone exceeds 8 KB;
  smallest fix is an unbudgeted fixed-size table in `Report` for Health. The spec's focus
  profile shows the ask's refs on expanded turns, and says `findings --group-by model` makes
  latency comparable across models; neither is built. `render.OMIT_HINT` suggests `--since` on
  single-session output. `_GROUP_KEY` and `_FINDING_GROUP` duplicate the model/harness keys, and
  `cmd_findings` applies the scope twice. Deferred from CP3 still: pi has no liveness registry
  (recent-mtime heuristic), and `sk show` skips the cross-session pass.
