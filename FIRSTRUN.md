# FIRSTRUN — dogfooding notes from the first real incident

Not a spec change, not a commitment — a field report. `PLAN.md` owns scope, `SPEC.md` owns
decisions; this is what happened when the tools in this repo were pointed at a real problem for
the first time, in the order it happened.

**The task:** diagnose why Claude Code session `3befc22d` (agent-shell, OpenCode config) went
sideways, then — mid-investigation — chase a second, unrelated report ("a subagent that never
ended") to its actual session (`4454ae64`, a 15-task `subagent-driven-development` pipeline,
$93.35), find the specific killed child (`ad67a90d`, Task 15's implementer), and produce a
prompt to finish its work from the worktree state it left behind.

---

## 1. What worked

Worth saying plainly, per this repo's own rule (`PLAN.md` §"Acceptance": *"the report names what
went right as well"*):

- `sk forensics <sid>` on session 3befc22d was one command to a correct diagnosis: two
  `file-thrash` findings, health numbers, done. The misdiagnosis loop itself (wrong host theory →
  rejected → Docker theory → also wrong) came straight out of `sk show --mode messages --range`,
  no raw-JSONL reading needed at any point.
- `hook-block` / `hook-pingpong` findings were exactly right both times they fired, with the
  triggering rule quoted verbatim — zero interpretation required.
- The `agent-kill` finding on `4454ae64` was the correct, load-bearing signal in an otherwise
  noisy 255-tool-call session — it's genuinely what justified the follow-up ($93 in one session
  is a lot to spend on nothing).
- `--budget-kb` doing its job as an escape hatch (raising it un-blocked the truncated fleet-wide
  subagent scan) — the mechanism is right even where the row-vs-cell distinction (§3 below) still
  bites.
- Read-only discipline held throughout without friction: every claim in the final report traced
  to a `sk` command, and the one time I needed the Task 15 spec text I went to the actual plan
  doc on disk rather than trying to pull it back out of a transcript.

## 2. The reconstruction only worked because of a lucky path, not a designed one

Getting from "a subagent never ended" to a finished reconstruction prompt took ten-plus `sk`
calls, most of them guesses:

1. `sk index --state interrupted-tool` (wrong session — that's this live session)
2. `sk index --subagents only --project agent-shell` (wrong project — Docker builds, unrelated)
3. `sk index --project ccswitch` / `--project ccswitch-remote-endpoints` (both empty — no
   top-level sessions there)
4. `sk index 2>&1 | rg -i ccswitch` (fleet grep, since I had no other way to find the parent) →
   finally landed on `4454ae64` by its **label text**, not by any structured link
5. `sk forensics 4454ae64` → `agent-kill` at line 2785, no child sid in the finding
6. `sk show 4454ae64 --mode tools` grepped for `Agent` calls to get the dispatch description
   ("Implement Task 15...")
7. `sk index --subagents only --project ccswitch-remote-endpoints` again, this time scanning 49
   rows by **label text match** against "Task 15" to find `ad67a90d`

Steps 2–4 and 7 exist only because there is no direct parent→child lookup. And per `PLAN.md`
§3.1/§6.2, the data is already there — subagent transcripts are described as carrying `parent_sid`
internally, and `agentType` comes from a `.meta.json` sidecar. None of that is surfaced by `sk
index` or `sk forensics`; I reconstructed the link by matching human-readable labels against
timestamps, which is exactly the kind of thing this tool exists to make unnecessary.

**Suggestion:** two small additions would have collapsed steps 2–7 into one call:
- A `parent_sid` (and `agent_type`) column on `sk index` rows when `--subagents include|only` is
  passed, so a fleet grep isn't needed to find a session's children.
- `sk forensics <sid> --children` (or a standalone `sk children <sid>`) that lists every `Agent`
  dispatch from that session alongside its resolved child sid, state, cost, and duration in one
  table — description, subagent_type, sid, state, cost, wall time. For `4454ae64` this is a
  15-row table (one per task, several with 3 review sub-dispatches) that I built by hand across
  five separate commands; the raw material (the `Agent` tool calls plus each child's own parsed
  session) is exactly what layer-3 `sk show <sid> --mode tools` and the subagent index already
  have.

This isn't a new idea competing with the roadmap — `PLAN.md` §5.1 already merges `subagents` into
`cost --subagents` for the cost-comparison angle. The gap is specifically the **navigational**
one: cost aggregation answers "was delegation worth it", this answers "which of these 15 children
is the one I need to look at", and I needed the second question three separate times before
finding the right session.

## 3. A precise bug, not just friction: `file-thrash`/`read-loop` findings have no line numbers

`sk forensics 3befc22d` reported two `file-thrash` findings with `count=5` each but an **empty**
`lines` column, and the "Timeline (finding-anchored)" section rendered `(none)` even though real
findings existed. I initially treated this as "nothing to show," then hit the same thing again on
`4454ae64` and `ad67a90d` (three more `file-thrash`/`read-loop` findings, same blank `lines`) and
went looking.

Root cause, traced to source:

- `sessionkit/classify.py:236` — `_path_churn()` (backing both `file-thrash` and `read-loop`)
  builds `Anomaly(kind, path, n, [])` — **the line list is hardcoded empty**. It only ever counts
  occurrences (`counts[f.path] += 1`), never records where they happened.
- One level deeper, `sessionkit/parse.py:88-94` — `FileOp` has no `line` field at all (`path, op,
  ts, tool_use_id` only), even though the `ToolCall` it's derived from does carry `.line`
  (`parse.py:293`). `_file_op()` at `parse.py:311` constructs `FileOp(value, op, ts,
  call.tool_use_id)` and simply drops `call.line` on the floor.
- Contrast with `_error_cascade` (`classify.py:239-252`), which does exactly the right thing —
  `lines.append(call.line)` — because `ToolCall` carries the field it needs and `FileOp` doesn't.

**Fix is small and localized:** add `line: int` to `FileOp`, pass `call.line` through at
`parse.py:311`, then have `_path_churn` accumulate `dict[str, list[int]]` instead of
`dict[str, int]` and pass the real list into `Anomaly(...)`. This directly fixes the "Timeline
(none) despite real findings" symptom, and it's the second-most-common anomaly kind by volume in
this session (`repeat-tool` and `file-thrash` were both firing) — worth more than the two lines it
costs to fix.

## 4. `--json` truncation breaking JSON validity — confirms an already-known gap

Hit exactly what `PLAN.md` §4 already documents ("the rule currently covers rows and not cells,
and that is a gap, not a scope boundary"): pulling the Task 15 dispatch prompt via `sk show
--mode tools --json` at `--budget-kb 500` returned a string cut mid-escape-sequence —
`json.loads()` threw `Unterminated string starting at: line 1 column 113`. `PLAN.md` already
prescribes the fix (truncate at the renderer with a marker, never truncate a cell under `--json`,
offer `--full` for one row) — this run is a real repro to attach to that item, not a new finding.
Concretely: the field I needed was 19,771 characters (`ad67a90d`'s opening user message, the full
Task 15 dispatch prompt); I ended up going around the tool entirely and reading the plan doc
directly off disk to reconstruct the spec, which happened to work here only because the doc still
existed and was current. A `--full` flag on `sk show --mode messages` would have made that
detour unnecessary and would work even when there's no separate source-of-truth doc to fall back
to.

## 5. Also hit, smaller

- `sk index --since 2026-08-17` rejected with `unrecognised --since value... expected e.g. 7d,
  12h, 2w`. Confirms the planned natural-language/absolute-date `--since` (`PLAN.md` §4, §5.1) —
  wanting "since this specific date" rather than a relative window is the normal way to scope a
  known-date incident.
- `sk index --subagents only` with no project filter returned 387 rows fleet-wide before I
  narrowed it — expected given the tool's own guidance to narrow first, but a free-text
  `--label-contains`/`--grep` filter on `index` would have let me jump straight to "Task 15" or
  "code-reviewer" matches instead of scanning by project/time window blind (this is what step 4
  in §2 above stood in for, badly).

## 6. `session-forensics` skill — content suggestions

- Add a short subsection for the orchestrator/subagent case: when a finding names `agent-kill` or
  `orphan-subagent`, the child transcript lives under `--subagents only`, filtered by **its own**
  project (which is the worktree/cwd it ran in, not the parent's project name) — this tripped me
  twice before I found it. Once `sk children`/`parent_sid` (§2) exists, replace this note with the
  one-command version.
- The skill currently glosses `agent-kill` and `orphan-subagent` together as "killed/orphaned
  subagents" — they're distinct taxonomy entries (`PLAN.md` §6.2) with different implications (a
  user-initiated kill vs. one that silently never returned). Worth naming both kinds explicitly so
  the report distinguishes "you pulled the plug" from "it vanished."
- Add the caveat from §3 above until it's fixed: `file-thrash`/`read-loop` findings can report a
  real `count` with an empty `lines`/Timeline — don't read that as "nothing to show."

## 7. Other skills worth running on this same incident

Only `error-patterns` and `session-forensics` are built (`.claude/skills/`); the rest below are
`PLAN.md`-roadmap items, not available today — noted as prioritization input, not a "go run this."

- **`error-patterns`**, for real, on this corpus: is the sleep-then-poll pattern seen in `4454ae64`
  (32 `sleep N; echo noop` Bash calls, escalating 2s → 10s) and the back-to-back-to-triple full
  `bats tests/bats/` reruns (~90s, ~90s, then a third one killed 13s in) specific to this one
  pipeline run, or does it recur across other `subagent-driven-development` sessions? If it's
  systemic, the fix belongs upstream in the `superpowers:subagent-driven-development` skill itself
  (wait on the completion notification instead of polling; don't re-run a suite that already
  passed) rather than in anything sessionkit reports after the fact — but sessionkit is exactly
  the tool that can tell "systemic" from "one bad run," which is the whole differentiator argued
  in `NOTES.md` §1.
- **`cost-forensics`** doesn't exist yet, but this session is a strong argument for building it
  soon: $93.35 in one session with a visible chunk of that spent on redundant polling and triple
  test-suite runs is exactly the "bloat attribution" gap `NOTES.md` §1 says `ccusage` doesn't
  cover and sessionkit should.
- Once it exists, a **`plan-diff`**-style check (roadmapped, `PLAN.md` §5.1) would have directly
  answered "is the worktree actually caught up with the plan" during the reconstruction step,
  instead of me manually diffing `git status`/`git diff --stat` against the Task 15 spec by eye.

## 8. Cost analysis follow-up: the exact join `sk cost` needs, with proof it matters

Asked to cost-break `4454ae64` by spawned agent. No `sk cost` exists yet, so I hand-built it from
three `sk` JSON calls stitched in Python — worth recording exactly what that script needed, because
one step in it exposed a correctness trap, not just a missing convenience.

**First attempt was wrong, silently.** With no parent→child link exposed (§2), I paired the 49
`Agent` dispatches to the 49 `ccswitch-remote-endpoints` subagent-index rows positionally, by
sorting children on `ended` ascending and zipping against dispatch order. It ran clean, produced a
complete table, and **the pairing was wrong** — e.g. "Implement Task 1" landed on `af92fb1e`
($1.33), a session whose own label is a spec-compliance *review*, while the true implementer,
`af091f9c` ($7.25), landed elsewhere in the table. Sorting by finish time silently assumes
completions arrive in dispatch order; they don't once any retries or non-adjacent progress
notifications happen (confirmed: task-id `a3c7bebc` notifies twice, 3 other dispatches apart). A
cost report is exactly the kind of output where a wrong-but-plausible number is worse than a
missing one — this one would have told you the wrong task cost $14.86.

**The fix exists in the transcript today**, and it's why I could tell the two runs apart at all:
every background-agent dispatch resolves through a `<task-notification>` block that carries both
`<task-id>` (= the child's own sid) and `<tool-use-id>` (= the *specific* `Agent` tool_use that
dispatched it) in the same record, e.g. `<task-id>af091f9c464dd0561</task-id>
<tool-use-id>toolu_019EF9JDv3yWjPg8WBjmK75h</tool-use-id>`. That `tool_use_id` is the same one
already on the originating `ToolCall` (`parse.py:293`, same field `_error_cascade` already reads).
**This is a straightforward, exact join** — no ordering assumption required — and it's what let me
rebuild a verified table (developer dispatches $86.61 across 15, reviewer dispatches $69.08 across
34; Task 10 alone $22.58, the single costliest task, driven by a $14.86 settings-writer
implementation run).

**What `sk cost --subagents` (or the resolver under it) should do:** parse `<task-notification>`
blocks in the parent transcript, extract `(tool_use_id → task_id)`, and join that against the
`tool_use_id` already recorded on the parent's own `Agent` `ToolCall` rows. That gives an exact
dispatch→child edge for free — no sidecar file, no timestamp heuristic — and turns this from a
15-minute hand-rolled script into what should be a single `sk` call. Two smaller asks fell out of
building this by hand:

- **Sunk-cost framing for non-`complete` children.** `ad67a90d` (Task 15, killed, $8.86) and
  `ac055f04` (Task 11's first quality-review pass, `interrupted-user`, $2.05, superseded by a
  second $1.86 pass) are real spend with no surviving output. A cost report should flag
  non-`complete` children as sunk rather than folding them into the total undifferentiated.
- **Fleet-wide `--budget-kb` sizing for this kind of join.** Getting clean `description` /
  `subagent_type` text out of `sk show --mode tools --json` for all 49 dispatches needed
  `--budget-kb 1500`; the default truncated mid-string on several rows (§4). Once `sk cost` does
  this natively it only needs the fields it joins on, not full JSON dumps, so this mostly
  disappears — noted in case an interim script hits the same wall.

**One more real discovery, orthogonal to tooling:** while pulling the ccswitch subagent index I
found a 50th, newer row — `atask15-…`, `ended 2026-08-27T23:26`, `$1.27`, `interrupted-tool`,
label opening `<teammate-message teammate_id="team-lead" summary="Finish Task 15 verification and
commit">` — a near-paraphrase of the reconstruction prompt handed over earlier this session. It
also didn't finish. Worth the user's attention directly, separate from this cost report.
