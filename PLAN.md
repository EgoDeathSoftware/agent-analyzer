# sessionkit — what we're building

**Status:** Phase 0 (scaffolding), Phase 1 (`error-patterns`) and Phase 2 (`sk commands`, `sk
hooks`, `sk forensics`, `session-forensics`) complete and verified against the live corpus.
Phases 3–8 planned. Last updated 2026-08-27.

This document answers **what is being built and what's next**. Why it is built this way is
`SPEC.md`; what we learned along the way is `NOTES.md`. See `SPEC.md` §6 for the ownership rule —
a fact is updated in exactly one of the three.

---

## 1. What sessionkit is

The **command-line counterpart to the tracker UI**: same corpus, different ergonomics.

The tracker's web UI is the browsing surface for `~/.claude/projects/**` — three panels, a session
detail view, config management — and it is in daily use. It answers *"show me this session."*
sessionkit answers *"which sessions, and why."*

Two consumers, and both matter:

1. **A human at a terminal.** Ad-hoc questions across the whole corpus — what broke most this
   week, what did that agent actually run, which sessions never finished. Clicking through a
   session list does not answer these.
2. **A Claude session.** A session can analyse *every other* session on this machine without
   reading raw transcripts into context. The corpus is **42 MB / 87 JSONL files** today and grows
   for as long as retention allows (§3.3), so the budget-enforced output contract (§4) is not a
   convenience — it is what makes these skills possible at all.

Where the two surfaces overlap — cost, in particular — they must agree. See `SPEC.md` §4.1.

## 2. The rule that decides scope

Everything below follows from one dividing line. It is **not** "CLI or skills":

- **The CLI is the deterministic part.** Parse, pair `tool_use` to `tool_result`, classify,
  aggregate across the corpus, enforce the budget. Everything whose answer must be identical
  twice.
- **The skill is the judgment part.** `error-patterns`' actual value is three sentences — *rank by
  breadth not count*, *the fix is a CLAUDE.md line not a better hook*, *a large `other` bucket is
  itself a finding*. None of that is code.

### Why the CLI has to exist

Stated narrowly enough to be true, because the broad version ("skills can't read files") is false
and led to over-scoping:

- **Grep is the wrong shape for JSONL.** One JSON object per line, so a hit returns the whole
  line — measured **2.4 KB average, 51 KB maximum**. Matching the 269 known failures pulls roughly
  650 KB of raw lines into context before any analysis begins. `Read` is worse: the largest
  transcript is 3.7 MB.
- **The signal is cross-record.** `is_error` lives on the `tool_result`, not the `tool_use`.
  Knowing a call failed means pairing them by id. The taxonomy on top is order-dependent
  (§6.1) — 308 lines in `classify.py`. No search expresses it.
- **Improvising the glob undercounts silently.** Subagents sit one level deeper and carry the
  *parent's* `sessionId` (§3.1). Miss that and you drop 41% of files while reporting a confident
  number over 59% of the corpus. This already happened once — see the arithmetic correction in
  `NOTES.md` §3.
- **Determinism.** 96 tests pin the taxonomy. "73/269, 27.1%" is worthless if it moves run to run.

### The honest limit, which is what sets scope

A skill *can* run Python through Bash. A skill carrying the parser inline **is** the CLI, just
distributed worse — untested, duplicated across every skill, drifting independently. The CLI is
not a capability skills lack; it is the tested, shared form of code they would otherwise
improvise. Therefore:

> **A command earns its place by aggregating across sessions or applying the taxonomy.** One that
> does neither is a wrapper around "print the transcript."
>
> **A skill earns its place by adding judgment its command does not have.** One that does not is a
> routing liability against a directory that already holds 20 skills.

## 3. Constraints (verified, not assumed)

| Constraint | Evidence | Consequence |
|---|---|---|
| No `sqlite3` CLI, no `pnpm`, no `fd` binary | `command -v` all fail; `fd` is a zsh alias to `fdfind`, invisible to scripts | Python stdlib only; no shelling out to these |
| Python 3.12.3 on the host | verified in-process | stdlib is enough; `sqlite3` is no longer imported anywhere |
| `uv` present, `ruff`/`pytest` absent | `ruff not found` in prior transcripts | Runtime = stdlib only; tests are `unittest` |
| Corpus 42 MB, 87 files (51 top-level, 36 subagents) | `du -sh ~/.claude/projects` | A full re-parse is fast enough that no cache is warranted (`SPEC.md` §2) |
| Sources in `sources.json` are **container paths** | read config | Host-side resolution needed; never hardcode |
| **The tracker runs in a container; sessionkit runs on the host** | `docker-compose.yml` mounts | Skills must never require the server to be up |
| `history.jsonl`: 258 rows `{display, project, sessionId, timestamp}` | `wc -l` | Prompt→session mapping is free |

> **On unreachable sources.** In a sandbox or a different user account, some or all of the paths in
> `.env` will not resolve — as user `agent`, 1 of 5 sources is visible. That is an environment
> fact, **not** evidence about what the tracker does or how much it is used. `sk doctor` names
> every source it cannot see and states that totals cover only what it could read. Never infer
> feature usage from a source being unreachable; that mistake was made once already.

### 3.1 Transcript facts that cost a bug each

| Finding | Impact |
|---|---|
| **Two transcript layouts.** Subagents live at `projects/<project>/<parent-session>/subagents/agent-*.jsonl` | A top-level-only glob silently dropped **36 of 87 files (41%)** |
| **Subagent transcripts carry the *parent's* `sessionId`**, their own identity in `agentId` | Keying on `sessionId` collapsed 87 files into 51 rows. Subagents key on `agentId`, with `parent_sid` recorded |
| **`agentType` lives in a `.meta.json` sidecar** beside each subagent transcript | The only source of the subagent's type (`Explore`, `general-purpose`, …) |
| **A `progress` record type** exists in subagent transcripts, carrying hook events | Not in the top-level schema; would have been dropped |
| **Session ids are not unique per file** | Two files sharing an id made the old cache delete one and re-parse both forever. Disambiguated with a path-derived suffix |
| **`<synthetic>` appears as a model id** for locally-injected messages | Treated as non-billable, or every corpus looks misconfigured |
| **`type: file-history-snapshot` records are dropped.** 340 in the corpus, 207 populated | `parse.py` handles only `assistant`/`user`/`system`/`progress`. Blocks Phase 6 |
| **Large tool results are spilled to `<session>/tool-results/`**, and the transcript's two copies of the result disagree: `toolUseResult` keeps the full text, `message.content` keeps a ~2 KB preview plus a path | Measured **2,165 vs 98,075 bytes** on one call — 45×. Reading either alone gets Phase 4 and Phase 5 wrong (§3.2.1) |

### 3.2 Data sources beyond the transcripts

`~/.claude` holds eleven more machine-readable sources. Most support work the transcripts cannot;
three correct work already planned. A full map of the directory is `CLAUDE_FOLDER.md`.

| Source | Shape | Volume today | Supports |
|---|---|---|---|
| **`file-history/`** | `<session-id>/<sha256(abs_path)[:16]>@v<N>` — full file content per version | 310 snapshots, 27 sessions; 55 files ≥v3, 20 ≥v4, 7 ≥v5 | Phase 6 |
| **`plans/`** | `<slug>.md`, plus `<slug>-agent-<id>.md` for subagent plans | 8 | Phase 7 |
| **`history.jsonl`** | `{display, project, sessionId, timestamp}` per user prompt | 258 rows | Phase 8 |
| **`settings.json`** | hook definitions **and** `permissions.deny`, joinable to `hook-block` / `user-rejected` / `permission-denied` failures | 9 inline Bash guards, 2 hook scripts, 42 deny rules | Phase 2 |
| **`<session>/tool-results/`** | `toolu_<id>.txt` — one file per spilled tool result | 3 in this container | Phases 4, 5 |
| **`sessions/<pid>.json`** | live-session registry: `sessionId`, `status`, `name`, `formerNames`, `agent`, `entrypoint` | 1 per *running* session | Phase 3 |
| **`~/.claude.json`** | per-project `last*` counters and `lastModelUsage` — Claude Code's own per-model token and dollar figures | 1 file, last session per project | Phases 5, 8 |
| **`projects/<project>/memory/`** | auto memory: `MEMORY.md` index + one typed topic file per fact (`user`, `feedback`, `project`, `reference`) | empty here; present on any machine with auto memory on | Phase 8, §10.3 |
| **skill and plugin inventory** | `plugins/installed_plugins.json`, `known_marketplaces.json`, `skills/<name>/SKILL.md` frontmatter, `settings.json:enabledPlugins`, `.claude.json:skillUsage`/`pluginUsage` | 11 plugins, ~20 skills, usage counts per skill | Phase 8, §10.1 |
| **session titles** | `<session>/custom-title.json`, plus `ai-title` / `agent-name` transcript records and `sessions/*.json:name` | one per named session | §4 layer 1 |
| **`teams/session-<id>/config.json`** | agent-team roster: `leadSessionId`, `members[]` with `agentType`, `cwd`, `backendType` | 1 | Phases 2, 5 |

**Use the transcript's own index, not the on-disk hash.** The 340 `file-history-snapshot` records
carry path → version → `backupTime`, anchored to a `messageId`, so a rewrite ties to the exact
turn that caused it:

```json
{"type":"file-history-snapshot","messageId":"…","isSnapshotUpdate":true,
 "snapshot":{"messageId":"…","timestamp":"…",
   "trackedFileBackups":{"Dockerfile":{"backupFileName":null,"version":1,
                                        "backupTime":"2026-03-14T02:31:14.826Z"}}}}
```

The naming scheme is decoded and verified — `sha256(abs_path)[:16]` reproduced **39 of 39** hashes
for one session's 43 edited paths — but it is a fallback, not the primary index. 21 of 27 history
directories have a matching transcript.

`file-history/` is the only source carrying **file content** rather than the record that an edit
happened, which makes real before/after diffs possible without git and without the working tree
still being in that state.

#### 3.2.1 Spilled tool results, and the two sizes that are not the same number

When a tool result is too large for context, Claude Code writes it to
`projects/<project>/<session>/tool-results/toolu_<id>.txt` and puts a `<persisted-output>` stub in
its place. The transcript then holds the result **twice, at different sizes**:

| Copy | Contains | Measured |
|---|---:|---|
| `message.content[].tool_result` | the stub: `Output too large (93.7KB)`, the spill path, and a 2 KB preview | **2,165 B** |
| `toolUseResult` | the full result text | **98,075 B** |
| `tool-results/toolu_<id>.txt` | the same full text, on disk | 93.7 KB |

Both numbers are true and they answer different questions. **`toolUseResult` is what the tool
produced; `message.content` is what the model paid for.** Phase 5's `--bloat` attributes dollars
to context, so it must measure the `message.content` side and treat spilling as the *mitigation*
it is — a spilled 94 KB result is the cheap case, not the expensive one. Measuring `toolUseResult`
would rank every spilled call as the worst offender in the report, exactly inverting the finding.

The same split cuts the other way for Phase 4: `sk search` scans JSONL, and for a spilled result
the JSONL holds only the first 2 KB in the stub — but `toolUseResult` retains the full text on the
same line, so a whole-line scan still sees it. The `tool-results/` files are a third copy needed
only when a transcript has aged out from under them, or for `WebFetch`, whose `toolUseResult`
carries a `bytes` field giving the true fetched size directly.

#### 3.2.2 Three cross-checks the transcripts cannot provide

- **`sessions/<pid>.json` says which sessions are alive.** A running session has an unmatched
  `tool_use` at the tail of its transcript at all times, which `end_state` classifies as
  `interrupted-tool`. Verified: while this document was being written, the live session was
  `{"status":"busy"}` in `sessions/234.json` and looked interrupted in its own JSONL. Phase 3 must
  read this registry and exclude live sessions, or `sk unfinished` reports every concurrently
  running session as abandoned work. Files here are removed on exit, so absence means "not
  running", and crash leftovers are cleared at next launch.

- **`~/.claude.json:lastModelUsage` is a third cost source, computed by Claude Code itself.** Per
  model, it carries `inputTokens`, `outputTokens`, `cacheReadInputTokens`,
  `cacheCreationInputTokens`, `webSearchRequests` and `costUSD`, alongside `lastCost`,
  `lastDuration`, `lastLinesAdded`/`lastLinesRemoved` and `lastSessionId`. Observed here:
  `lastCost 3.6640615` split across `claude-opus-5[1m]` and `claude-haiku-4-5`. Phase 5 currently
  plans parity against the tracker UI alone — but both hand-maintained rate tables were wrong at
  the same time on 2026-08-26, so agreeing with each other proves nothing. This is an independent
  third opinion, and it settles §10.4's rate question empirically. Two limits: it is overwritten
  per project, so only the **last** session is covered (`backups/` holds the five previous
  rewrites), and it is keyed by project, not by session id beyond `lastSessionId`.
  `lastLinesAdded`/`lastLinesRemoved` also give `NOTES.md` §2.8 its missing lines-changed
  denominator a second source to validate the `file-history/` derivation against.

- **`projects/<project>/memory/` is what Claude already learned.** Auto memory holds one fact per
  file with a `type` of `user`, `feedback`, `project` or `reference` — `feedback` being, precisely,
  corrections the user gave. Phase 8's rule is *said twice → propose a CLAUDE.md rule*; a proposal
  that duplicates a memory already on disk is noise, and repeated every run. Read the memory
  directory as a suppression list before proposing, and treat a `feedback` memory that keeps
  getting re-triggered as a stronger signal than a fresh correction, not a weaker one.

#### 3.2.3 The inventory needed before proposing anything

`suggest-skills` (Phase 8) proposes new skills, and §10.1 gates the whole plan on skill routing
staying reliable. Both need the same thing: **what is already installed, and what its routing text
says.** `plugins/installed_plugins.json` gives name, version, install path and git SHA per plugin;
each `skills/<name>/SKILL.md` frontmatter carries the `description` that Claude actually routes on;
`settings.json:enabledPlugins` says which are live. `.claude.json:skillUsage` and `pluginUsage`
then add counts and `lastUsedAt` — observed here, `hookify` at 126 invocations against several
plugins at 0. A skill that has never fired is either mis-described or unnecessary, and that is a
finding `suggest-skills` should make before it proposes a twenty-first skill.

Excluded, having been checked rather than assumed: `tasks/` (26 dirs, no files), `agent-memory/`,
`todos/`, `session-env/` and `shell-snapshots/` (environment captures with no analysis value yet),
`security/` (a plugin's own state, plus a 298 MB venv), `cache/changelog.md`, and `autoharness/`.
Re-survey before assuming any stay empty.

### 3.3 Retention bounds the corpus, and it is a setting

`cleanupPeriodDays` decides how long any of this survives. **The default is 30 days**; this machine
sets **365**, which is the only reason the corpus reads as append-only history. The sweep deletes
aged files under `projects/`, `file-history/`, `plans/`, `tasks/`, `session-env/` and
`shell-snapshots/`, and it is what `.last-cleanup` timestamps.

Three consequences:

- **`sk doctor` must report the effective retention per source**, next to reachability. "No errors
  before March" and "transcripts before March were deleted" are the same output today.
- **Phase 6 is bounded by it.** `file-history/` ages out on the same clock, so churn analysis has a
  shorter horizon than the transcripts it is anchored to.
- Two paths are exempt and can be relied on: `projects/<project>/memory/` is excluded from the
  sweep entirely, and `history.jsonl` is never swept — which makes the 258 prompt rows the longest
  continuous record on the machine, outliving the transcripts they point at. Expect
  `history.jsonl` rows whose `sessionId` no longer resolves, and report them as such rather than
  dropping them.

---

## 4. The three-layer output contract

Every skill descends these layers and **never** reads raw JSONL.

| Layer | Command shape | Budget | Content |
|---|---|---|---|
| **1 index** | `sk index` | ~12 KB (all 87 sessions ≈ 10 KB) | one line per session, carrying its **title** where one exists (§3.2) — the cheapest signal per byte in the whole contract, and the difference between a list of UUIDs and a list a human can scan |
| **2 aggregate** | `sk errors`, `sk cost`, `sk commands`, `sk digest` … | ≤4 KB | clusters, counts, **one exemplar each** |
| **3 excerpt** | `sk show <sid> --mode …` | ≤8 KB | one session, surgically |

Implemented and tested:

- Every subcommand accepts `--json` and `--budget-kb`.
- Global flags parse **before or after** the subcommand — skills compose invocations as strings,
  and a flag that only works in one position is a trap.
- **No silent truncation.** A capped table ends with
  `… N more row(s) omitted (raise --budget-kb or narrow --since)`.

**The rule currently covers rows and not cells, and that is a gap, not a scope boundary.** Tool
inputs are cut to ~70 characters when the row is *built*, so no flag recovers them — `--json`
emits the same truncated string, mid-token and mid-escape:
`"command": "ls -la /workspace && echo \\\"---\\\" && ls -R /workspace 2>/`. For `sk show --mode
tools` and `sk commands`, whose entire purpose is showing what an agent ran, a silently halved
command is worse than an omitted row: an omitted row announces itself. Cells obey the same
contract — truncate at the renderer against the remaining budget, mark it (`…`), and offer
`--full` for the one row being investigated. Under `--json`, never truncate a cell: JSON output is
consumed by a program, and the budget exists to protect a context window that JSON is not being
read into.

**What "never reads raw JSONL" covers, now that §3.2 lists eleven sources.** The rule was written
when the only source was the transcripts, and read literally as "never read `~/.claude`" it would
force a command for every one of them — including ones where a command aggregates nothing and
applies no taxonomy, which §2 says does not earn its place. The dividing line is §2's, not a new
one:

| | Sources | Why |
|---|---|---|
| **Always through `sk`** | transcripts, `file-history/` content, `tool-results/` spill, and every **join** — `sk hooks` against `permissions.deny`, `sk cost` against `lastModelUsage` | Megabytes, cross-record, or taxonomy-dependent. This is the whole reason the CLI exists (§2) |
| **A skill may read directly** | `memory/*.md`, `SKILL.md` frontmatter, `custom-title.json`, a single `settings.json` key | Small, single-file, human-readable. A command that cats one of these adds a routing surface and aggregates nothing |

The test for anything added later: **does answering it require more than one session, or the
taxonomy?** If not, the skill opens the file. The rationale for the original rule is the context
budget — a 40-line memory file is not a budget problem, and pretending it is would grow the CLI
along exactly the axis §2 warns about.

Planned refinements (rationale and attribution in `NOTES.md` §2.4). The budget is currently
enforced by cutting rows; these fit more signal into the same bytes instead: a `--detail` ladder
and compact mode, `conversation_arc` as the default `sk show` shape, `--metadata-only`, and
natural-language `--since`.

## 5. Surface

### 5.1 Commands

**Shipped:** `doctor`, `index`, `show`, `errors`, `commands`, `hooks`, `forensics`, `children`.

**Planned (10):** `unfinished`, `search`, `cost`, `files`, `churn`, `stores`, `digest`,
`plan-diff`, `corrections`, `procedures`.

Changes from the earlier draft, by the rule in §2:

| Change | Command | Why |
|---|---|---|
| Cut | `handoff` | A handoff brief is judgment, not computation. Built by `unfinished-work` from `sk unfinished` + `sk show`. |
| Merge | `subagents` → `cost --subagents` | Comparing child cost to parent cost is a section of a cost report, not a separate question. |
| Add | `hooks` | The computational half of the old `hook-tuning` skill: join `hook-block` failures to `settings.json` definitions. |

> The earlier draft's command list named 12 but two phases required two more it omitted (`churn`,
> `plan-diff`), so the real starting figure was 14. After the cut and the merge: 13.

### 5.2 Skills

Eight, down from fifteen. The merges are not cosmetic — the earlier draft called skill routing
*"the largest risk in this plan"* and recommended fewer skills over a wider CLI, then kept fifteen
anyway. §2 is what resolves it.

| # | Skill | Phase | Status |
|---|---|---|---|
| 1 | `error-patterns` | 1 | **done** — gains a hooks section in Phase 2 |
| 2 | `session-forensics` | 2 | **done** — stays narrow (`NOTES.md` §1) |
| 3 | `unfinished-work` | 3 | planned |
| 4 | `cost-forensics` | 5 | planned |
| 5 | `edit-churn` | 6 | planned |
| 6 | `session-digest` | 7 | planned — the `<SUMMARY_DIR>` writer only |
| 7 | `plan-vs-outcome` | 7 | planned |
| 8 | `suggest-skills` | 8 | planned |

| Merged away | Into | Why |
|---|---|---|
| `hook-tuning` | `error-patterns` | Its entire judgment is already the skill's "hook-block trap" section. The `settings.json` join is a command. |
| `delegation-roi` | `cost-forensics` | A section of a cost report — the same merge already made for context-burn and model-routing. |
| `uncommitted-work` | `unfinished-work` | Both answer "what is at risk of being lost." |
| `corrections-to-rules`, `prompt-autopsy` | `suggest-skills` | *Said twice is a missing rule; said across five sessions is a missing skill.* One skill, two thresholds — not three skills over one signal. |

| Cut | Why |
|---|---|
| `prior-art` | Judgment is thin; `sk search` is the whole thing. |
| `store-reaper` | Reports orphans and emits a command. `sk stores` is enough. |

Still explicitly **out of scope** (deferred, not cut): `fleet-status`, `collision-check`.

**Every skill resolves `sk` the same way, and none of them hardcodes a path.** `error-patterns`
today opens with `SK=/mnt/c/Users/david/Projects/CAT_AI/claude-session-analyzer/sk` — the *host*
path, which does not resolve in the container this repo is also worked on from, where the binary
is `/workspace/sk`. The skill's own guard then fires and it stops, correctly but uselessly. That is
one hardcoded absolute path per skill across the eight in §5.2, drifting independently — the same
"distributed worse" failure §2 rejects for the parser, in a different costume.

Resolve in order, and stop with a named error if all four miss: `$SK` if set; `sk` on `PATH`; a
path recorded once in the repo and shared by every skill; the directory the skill itself lives in,
walked up to the repo root. The fallback that must never be added is the one the current skill
already forbids in prose — grepping `~/.claude` directly.

### 5.3 The handle: a session is addressed the way you resume it

**Whatever identifies a session to `claude` identifies it to `sk`.** A user moving between the two
should never have to translate. `claude --resume` accepts two things — a session ID, or a **search
term** matched against session names in the picker — so `sk` accepts both, everywhere a session is
named.

| Handle | Example | `claude` | `sk` today |
|---|---|---|---|
| *omitted* | — | `--continue` | ❌ required argument |
| `@last` | — | `--continue` | ❌ |
| Session UUID, or a unique prefix | `07a2c27f` | `--resume <id>` | ✅ `sk show <sid>`, prefix-matched (`corpus.py:123`) |
| Session name / title | `claude-folder-docs` | `--resume` picker search term | ❌ not resolvable |
| Subagent id | `agent-…` filename stem | — | ✅ resolved by the same function |
| Project | `-workspace` | cwd | ✅ `--project` on `index`, `errors` |

Only the second row is missing, and it is the one a human actually remembers. Names come from four
places, all cheap to read: `<session>/custom-title.json` (set by `/rename`), the `ai-title` and
`agent-name` transcript records, and `sessions/*.json:name` with its `formerNames` history (§3.2).
Resolve in that order, match case-insensitively on substring, and **on ambiguity list the
candidates rather than picking the newest** — silently showing the wrong session is the failure
mode `load_session` already guards against for ids.

This is also the fix for `sk index`'s `label` column, which today falls back to the first prompt
and renders `<command-name>/clear</command-name> <command-message>clear</` for a session whose
`custom-title.json` reads `claude-folder-docs`. Same lookup, both directions: names in, names out
(§4 layer 1).

Two constraints on this. Names are **not unique and not stable** — `formerNames` exists precisely
because they get reused and rewritten — so the UUID stays the canonical handle, a name is a lookup
convenience, and any report that cites a session cites the id alongside the name. And
`sessions/*.json` is deleted when a session exits (§3.3), so live names vanish; `custom-title.json`
and the transcript records are the durable sources.

**Omitting the handle means "the session I am in."** Claude Code exports `CLAUDE_CODE_SESSION_ID`
into the environment of every process a session spawns — verified: `07a2c27f-…` here, matching
`sessions/234.json` — so the common case costs nothing to support. Falling back, in order: the env
var, then the newest session in the cwd's project, then an error. `@last` names the second
explicitly. **Print the resolved target on the first line of every report**; a default that is not
shown is a default that gets misread.

**The default differs by scope, and conflating them would gut the tool.** `sk errors` means the
whole corpus today, and §1 is explicit that corpus-wide is the product. So:

- **Session-scoped** — `show`, `forensics`, `commands`, `plan-diff`: omitted → the current session.
- **Corpus-scoped** — `index`, `errors`, `cost`, `digest`, `search`: omitted → the whole corpus; a
  handle *narrows* to one session.

Same syntax either way, and `sk errors` versus `sk errors 07a2c27f` reads correctly in both.

**Ambiguity is data, not prose.** Skills compose `sk` invocations as strings and cannot read an
apology. Exit **3** for no match, exit **4** for ambiguous — printing the candidate table (id,
name, ended, project) and selecting nothing. This is §5.3's "list the candidates rather than
picking the newest", made mechanical enough for a caller to branch on.

Two consequences that would otherwise arrive as bugs:

- **The default target is usually a live session** (§3.2.2), still being written and always
  carrying an unmatched `tool_use` at its tail. It is not an edge case once omitting the handle is
  the normal invocation. Mark it — `target: 07a2c27f (live)` — and exclude the tail turn from
  `end_state`, or `sk forensics` with no arguments reports `interrupted-tool` every single time.
- **Skills must pass the handle explicitly and never inherit it.** `CLAUDE_CODE_CHILD_SESSION=1` is
  set alongside it, so the variable is per-process: a skill running inside a subagent reads the
  *subagent's* id, not the parent it was asked to analyse. The env default is a convenience for a
  human at a terminal. A skill that relies on it produces a correct-looking report about the wrong
  session, which is the worst available failure. Verify the resolved id has a transcript before
  trusting it, too — a `-p` or `--bare` session may not have persisted one.

---

## 6. Model and classifiers

`parse.py` produces, per session: `sessions`, `messages`, `tools`, `files`, `sysev`, `attach`,
`anomalies`, `prompts` — in memory, discarded when the process exits.

`tools.input_digest` is a sha1 of the canonicalised tool input. Repeated identical digests within
a session **are** the loop signal — this field is why forensics is cheap.

`sessions.end_state` ∈ `complete | interrupted-tool | interrupted-user | killed-agents |
compacted-idle | error-cascade | unknown`.

### 6.1 Error taxonomy (implemented)

`hook-block`, `user-rejected`, `file-too-large`, `read-truncated`, `stale-read`, `missing-tool`,
`permission-denied`, `not-a-repo`, `not-found`, `rate-limit`, `api-error`, `mcp-error`, `timeout`,
`killed`, `input-validation`, `policy-denied`, `exit-code`, `no-result`, `other`.

**Order matters.** Exit codes 124/137/143 are read *before* the body-text sweep, so a timeout or
an OOM kill is not flattened into a generic `exit-code`. `permission-denied` precedes `exit-code`
because a denied `mkdir` arrives wrapped as `Exit code 1 … Permission denied`.

Unmatched text lands in `other` **with its normalised signature retained**, so a large `other`
bucket is itself a reportable finding. That is how `permission-denied`, `stale-read`, `timeout`,
`killed` and `policy-denied` were added.

### 6.2 Anomaly detectors (implemented)

`repeat-tool`, `file-thrash`, `read-loop`, `error-cascade`, `hook-pingpong`, `agent-kill`,
`orphan-subagent`, `compaction-churn`, `stall`, `rejection-persist`. Thresholds live in one
`THRESHOLDS` dict.

### 6.3 Layout

```
claude-session-analyzer/              # own repo, sibling of the tracker
  README.md  PLAN.md  SPEC.md  NOTES.md
  sk                                  # launcher; skills invoke this absolute path
  sessionkit/
    __init__.py  __main__.py  cli.py
    sources.py                        # discovery + reachability probing
    parse.py                          # record → normalised events
    corpus.py                         # on-demand parse + post-parse pipeline
    query.py                          # aggregations
    classify.py                       # error taxonomy, end-state, anomaly detectors
    pricing.py                        # model rates (mirrors the tracker's server/src/pricing.ts)
    redact.py                         # secret scrubbing before anything is rendered
    render.py                         # budget-enforced emitters
  skills/<name>/SKILL.md              # symlinked into ~/.claude/skills/
  tests/                              # stdlib unittest
```

Nothing outside the repo is written — nothing is written at all. Redaction is purely a
**read-time** concern: previews pass through `redact.py` on the way to the terminal, not on the
way into a store, because there is no store.

---

## 7. Build phases

### Phase 0 — core scaffolding ✅

`sources.py`, `parse.py`, `pricing.py`, `redact.py`, `render.py`, `cli.py`, and
`doctor`/`index`/`show`. Acceptance met: `sk doctor` names every unreachable source and says
totals cover only what it saw; `sk index` fits all sessions in ≤12 KB; `sk show --timeline` fits
the 227-line session in 8 KB.

### Phase 1 — `error-patterns` ✅

The taxonomy, `sk errors` (`--group-by class|tool|signature|session`), and the skill.

Acceptance met: against the live corpus, `hook-block` ranks **#1 at 73/269 (27.1%) across 43 of
86 sessions**, and the report's headline recommends a CLAUDE.md rule rather than a hook change.
Output ≤4 KB.

A **single signature** — the `PreToolUse:Bash` hook rejecting `grep`/`find` — is 68 of those
failures across 41 sessions, 3.5× the next-largest and entirely self-inflicted. This is the
archetype for the whole toolkit: the UI can show you one of those 73 failures; it cannot tell you
there are 73, that they share one cause, or that the cause is a rule you wrote.

### Phase 2 — `sk forensics`, `sk commands`, `sk hooks`, skill `session-forensics` ✅

Two halves of one investigation — *what went wrong*, and *what was actually run*. The detectors
already exist and are unit-tested; this phase is the report shape.

`sk commands` answers "review agent commands": every Bash invocation and tool call in a session or
across a scope, normalised and deduplicated, with counts, exit status and duration; grouped by
`--group-by command|tool|session|agent`, and filterable with `--agent-type` (`Explore`,
`general-purpose`, …). **No UI equivalent exists at all** — the Tools tab shows calls one at a
time and cannot mark which failed (`SPEC.md` §4).

**`agent` is a required grouping, not a nice-to-have.** Subagents are 36 of 87 transcripts and
carry the *parent's* `sessionId` (§3.1), so `--group-by session` cannot answer "what did that
agent actually run" — the question the command exists for. Group on `agentId`, and take
`agentType` from the `.meta.json` sidecar, which is its only source. `sk show <agentId> --mode
tools` already resolves a subagent by id (`corpus.py:123`), so the per-agent view exists at layer
3 today; this is the layer-2 rollup over it.

`sk hooks` joins `hook-block` failures against `settings.json` definitions and emits, per hook,
how often it fired and what it cost. It ships here rather than later because the finding is
already in hand and stale: every session run between now and the fix pays the same 68-failure tax.
The proposal it feeds — relax the rule, or move it to CLAUDE.md where it teaches at generation
time instead of blocking at execution time — belongs to `error-patterns`, which gains a section
for it rather than a new skill being created.

Calibrate detectors against the borrowed thresholds in `NOTES.md` §2.1, then **re-validate each
against this corpus and record where ours differs.** Add the git join: a session that claimed
success, produced no commit, and left tests failing is a finding no transcript-only tool can make.

**Acceptance:**
- On a session containing `agents_killed`, `sk forensics` names it, gives a line-anchored
  timeline, and ends with a **prevention diff**, not a narrative.
- Every finding carries a `file:line` citation, and the report names what went **right** as well —
  a forensics tool read as pure negativity stops being read.
- `sk commands --group-by command` surfaces the repeated-`grep` signature as a top row *without*
  being told to look for errors — the same finding reached from the commands side.
- `sk commands --group-by agent` attributes calls to the subagent that made them, not to the
  parent session, and `--agent-type Explore` scopes to one kind. On a corpus with no subagents both
  degrade to an empty result that **says so**, rather than silently reporting the parent's calls.
- Every command is readable in full: no cell is cut without a marker, `--full` returns the
  complete input for one row, and `--json` never truncates (§4).
- `sk hooks` attributes the 68 failures to the specific `PreToolUse:Bash` rule, and
  `error-patterns` proposes the relocation with that count as justification. **Emits a diff for
  approval; never edits `settings.json`.**
- The same join covers the other half of the taxonomy: `user-rejected` and `permission-denied`
  failures resolve against `settings.json:permissions.deny` (42 rules here) and
  `.claude.json:allowedTools`, so a denial is attributed to the rule that caused it rather than
  reported as an anonymous refusal.
- `sk doctor` reports the **effective `cleanupPeriodDays` per source** alongside reachability
  (§3.3). Until it does, a swept corpus and a quiet one are the same output.

**Acceptance met**, with two scope decisions made explicitly rather than left implicit:

- `sk forensics` and `sk commands` take a required `<sid>`/session argument, matching `show`
  today. §5.3's full handle-resolution system (omitted → `$CLAUDE_CODE_SESSION_ID` → `@last` →
  name search → ambiguity exit codes) is real, cross-cutting work that also retrofits `show`; it
  is deferred to its own pass rather than built piecemeal here.
- `sk hooks` attributes failures by extracting each hook script's own `echo 'BLOCKED: …'` text
  and substring-matching it against the failure body — this corpus's hooks carry no failures to
  attribute yet, so the 68-failure/`PreToolUse:Bash` figure is validated against synthetic
  fixtures (`tests/test_corpus.py:HooksTest`), not this machine's live corpus. The join itself,
  and the `user-rejected`/`permission-denied`/`policy-denied` → `permissions.deny` +
  `.claude.json:allowedTools` half, are both implemented and tested.
- The cell-truncation gap in §4 is fixed as part of this phase, not deferred: `INPUT_PREVIEW`/
  `OUTPUT_PREVIEW` raised to 2000 chars, per-cell truncation centralised in `render.py` (marked
  with `…`, never applied to JSON), and `--full` added to `sk commands`.

### Phase 3 — `sk unfinished`, `sk files`, `gitlink.py`, skill `unfinished-work`

`end_state` already lands correctly — `interrupted-tool` is populated in the live corpus today.
`sk files --uncommitted` moves here from the old Phase 6, because the merged skill needs the git
join to answer "what is at risk."

**Acceptance:** ranks by recoverability, not recency; each row carries a paste-ready
`claude --resume <sid>` **and** a cold-start handoff brief; cross-references `files` against
`git log` in the session window, respecting `.gitignore`.

**Excludes live sessions**, read from `sessions/<pid>.json` (§3.2.2). A running session always has
an unmatched `tool_use` at the tail of its transcript and classifies as `interrupted-tool`; without
this check `sk unfinished` reports the session you are sitting in as abandoned work. A negative
fixture is required — a transcript with a dangling `tool_use` **and** a live registry entry must
not appear in the report.

### Phase 4 — `sk search`

No skill. `sk search` scans the corpus directly — 7 ms for a full-corpus pass — joined to
on-demand parse for context.

**Acceptance:** a known past error returns the session that hit it **and the resolution excerpt**
in ≤4 KB. No degradation path is needed; there is no index to be absent.

Scan whole JSONL lines, not the rendered message content: a spilled tool result keeps only a 2 KB
preview in `message.content` while `toolUseResult` retains the full text on the same line (§3.2.1).
Fall back to `tool-results/toolu_<id>.txt` only where the transcript has aged out from under the
spill files, and say so in the output when it happens.

### Phase 5 — `sk cost`, skill `cost-forensics`

Implement `tool_result_sizes` — per-tool total/avg/max result bytes — as the backbone of
`--bloat`. Starting thresholds from `NOTES.md` §2.1: >10 KB average for an unused result, >$1 for
a discarded agent dispatch.

**Measure the `message.content` side, not `toolUseResult` (§3.2.1).** `--bloat` attributes dollars
to context, and those two differ by 45× on a spilled result. Spilling is the mitigation, so a
spilled call is the *cheap* case; ranking on `toolUseResult` would put every spilled call at the
top of the report and invert the finding. Report both columns where they diverge — "produced 94 KB,
paid for 2 KB" is itself the signal that spilling worked.

**Acceptance:**
- `--bloat` surfaces repeat-reads, truncation notices, unbounded Bash output, oversized tool
  results, and the cache-read/create ratio, attributing dollars to each.
- `--subagents` compares subagent cost against parent cost, **states the sample size** so a thin
  result isn't read as a conclusion, and applies the wasted-dispatch test: was the subagent's
  returned summary actually used by the parent, or discarded?
- **The parent↔child edge is `parse.py`'s `dispatch_edges`** (built for `sk children`, Phases
  0–2 addendum — see `docs/superpowers/plans/2026-08-28-firstrun-fixes.md` Task 3), joining each
  `Agent` `ToolCall.tool_use_id` to its child's sid via the `<task-notification>` record. Do not
  re-derive this by sorting children on completion time and zipping against dispatch order — that
  approach silently mispairs retries and non-adjacent completions (`FIRSTRUN.md` §8, confirmed:
  a wrong pairing put one task's cost on another task's row). Flag a non-`complete` child
  (`killed`, `interrupted-user`, …) as **sunk cost** rather than folding it into the parent's
  total undifferentiated — real spend with no surviving output is a distinct line, not noise.
- `sk cost` for a given session matches the tracker UI's Costs tab **to the cent**. Both tables
  were corrected on 2026-08-26; this phase is where that agreement gets a regression test.
- **And matches `~/.claude.json:lastModelUsage` for that session** (§3.2.2). The two hand-maintained
  rate tables were wrong simultaneously, so their agreeing with each other proves only that they
  were copied from each other. `lastModelUsage` is computed by Claude Code and carries per-model
  tokens plus `costUSD`, which makes it the only independent arbiter available. It covers the last
  session per project only, so this is a spot check on one session, not a corpus-wide assertion —
  state that scope in the test name so a green run is not read as more than it is.

### Phase 6 — `sk churn`, `filehistory.py`, `sk stores`, skill `edit-churn`

Ranks files by rewrite depth within a session, diffing `v1` against the final version to separate
convergent iteration from thrash. Every extra pass is tokens, so this reports in dollars as well
as counts.

**Prerequisite:** teach `parse.py` the `file-history-snapshot` record type (§3.2). Index off those
records — path, version, `backupTime`, anchored to a `messageId` — and read `file-history/` only
for content, falling back to the hash scheme when a snapshot record is missing.

`sk stores` reports orphaned stores and emits a copy-pasteable removal command. **It never
deletes.**

**Acceptance:** on the 7 files at `@v5` or deeper, distinguishes files whose late versions kept
changing substantively from files that oscillated between two states, attributes a token cost to
each rewrite pass, and names the turn that caused each rewrite via `messageId`. States coverage
honestly — only 21 of 27 history directories have a matching transcript, so 6 sessions can be
measured for churn but not attributed to a conversation, and `file-history/` ages out on the
retention clock (§3.3), giving churn a shorter horizon than the transcripts it anchors to.

### Phase 7 — `sk digest`, `sk plan-diff`, skills `session-digest` + `plan-vs-outcome`

`sk digest` covers in text the ground a dashboard would (`NOTES.md` §2.8). `sk plan-diff` compares
`plans/` artifacts against the transcript to answer the question neither source answers alone:
**which plan steps were silently dropped.** A session that abandons step 4 of 6 and reports
success looks identical, in the transcript, to one that finished. Subagent plans are named
`<slug>-agent-<id>.md`, so a delegated plan can be checked against the subagent transcript that
was supposed to execute it.

**Acceptance:**
- `digest --since 1d` covers all reachable sources in one ≤4 KB rollup.
- `session-digest` writes `<SUMMARY_DIR>/<sid>.md` for the tracker UI and nothing else.
- For a plan with N steps, `plan-vs-outcome` classifies each as done / partial / not attempted
  with a line-anchored citation, and reports **"no evidence either way" as its own category**
  rather than defaulting a step to done.

### Phase 8 — `sk procedures`, `sk corrections`, skill `suggest-skills`

`sk procedures` mines repeated tool n-grams across sessions. `sk corrections` classifies user
messages with the friction lexicon in `NOTES.md` §2.2 — corrections, redirects, repetitions,
stops, frustration — rather than timing alone, since timing says two prompts were close together
while the lexicon says which one was a correction. `history.jsonl` supplies 258 prompts with
`sessionId` and `timestamp`, so cadence needs no new parsing.

Findings emit through the fixed causal chain (*correction → what went wrong → why → wrong
assumption / missing context / bad skill guidance / wrong tool*), so each lands on a cause rather
than a complaint.

**Acceptance:**
- Identifies repeated multi-step sequences across ≥3 sessions and hands off to the existing
  `build-agent` skill rather than authoring skills itself.
- Ranks sessions by correction density and, for the top few, shows the prompt pair that signalled
  it.
- **Distinguishes a genuine re-prompt from a fast follow-up on successful work.** A negative
  fixture for this is required — without it every productive rapid-fire session reads as a
  failure.
- Applies both thresholds: said twice → propose a CLAUDE.md rule; said across five sessions →
  propose a skill.
- **Reads `projects/<project>/memory/` as a suppression list first** (§3.2.2). Auto memory already
  holds the user's corrections as typed `feedback` facts; re-proposing one as a novel CLAUDE.md
  rule is the exact failure mode §10.3 is about, and here it is avoidable for free. A `feedback`
  memory whose correction *keeps recurring* is a stronger finding than a fresh one, not a
  suppressed one — the rule exists and is not working.
- **Checks the installed inventory before proposing a skill** (§3.2.3): `installed_plugins.json`,
  the `description` frontmatter of every `SKILL.md`, and `.claude.json:skillUsage`. A proposal that
  collides with an existing skill's routing text is reported as a collision, not filed as new work.
  The same pass names skills at **zero invocations** — a directory of ~20 skills where several have
  never fired is a finding in its own right, and a cheaper one to act on than a new skill.

---

## 8. Testing

- **Runtime stdlib-only; tests stdlib-only.**
  `PYTHONPATH=. python3 -m unittest discover -s tests -t .`
- 96 tests across parsing, taxonomy, detectors, pricing, budget enforcement, the query layer, and
  end-to-end load-and-report.
- Fixtures are built inline, so each test shows the transcript shape it asserts against.
- Every detector has a positive **and** a negative case.

## 9. Guardrails

- **The `sk` CLI is read-only, with no exceptions** — it emits to stdout and writes nothing else.
  It has no writable state at all. This is the invariant that lets it run against a live corpus
  with zero risk to it. (`SPEC.md` §3.1 states the same boundary from the other side.)
- **Skills may write; `sk` may not.** `session-digest` writes `<SUMMARY_DIR>/<sid>.md` for the
  tracker UI. That is the skill writing, not the CLI.
- **Proposing skills never apply.** `error-patterns` and `suggest-skills` emit a diff or a command
  and stop. The hooks proposal in particular targets `settings.json`, which governs every future
  session on the machine.
- **`file-history/` is read-only too.** It is Claude Code's own undo store; writing to it or
  pruning it would corrupt a facility the user depends on. `edit-churn` reads and reports.
- **`sk stores` reports; it never deletes.**
- **Never depend on the tracker being up.** The server runs in a container and sessionkit runs on
  the host; an analysis that dies with `docker compose` is useless exactly when it is needed.
- Every rendered preview passes through `redact.py` (API keys, tokens, JWTs, connection strings)
  and is length-capped.
- **`~/.claude/.credentials.json` is never read, and neither is any path under `security/`.** The
  first holds live OAuth access and refresh tokens for the account and for every connected MCP
  server; §3.2 turns sessionkit into a walker over `~/.claude`, and a walker that globs the
  directory will reach it. Deny it by path in `sources.py`, not by convention — this machine's own
  `permissions.deny` already blocks Claude from reading it, and sessionkit should not be the tool
  that puts it on stdout.
- **No network calls anywhere in sessionkit.** This constrains §10.4 — any pricing sync must be a
  committed artifact refreshed by an explicit command, never a runtime fetch.

## 10. Open decisions

1. **Validate skill routing after Phase 2 — a hard gate.** The merge in §5.2 takes 15 new skills
   down to 8, but three still start from failed tool calls (`error-patterns`,
   `session-forensics`, and the hooks proposal inside the first). `error-patterns` ships with an
   explicit boundary table against `session-forensics`, `cost-forensics`,
   `auditing-claude-projects` and `fewer-permission-prompts`; every subsequent skill must do the
   same. **If routing is unreliable at three overlapping skills, merge further before Phases 5–8
   add the rest** — merging is much cheaper before the skills exist than after.

   The validation is now measurable rather than impressionistic: `.claude.json:skillUsage` records
   an invocation count and `lastUsedAt` per skill (§3.2.3). Run the boundary table's scenarios,
   then read which skill actually fired. Observed here, counts range from 126 down to 0, so the
   signal is real and already being recorded — no instrumentation is needed to collect it.

2. **Parity test vs `parser.ts`.** Deferred to Phase 5, where cost figures start being compared
   against the UI directly and disagreement becomes user-visible. Copy the round-trip validator
   pattern from the Rust `claude-code-transcripts` crate: flag unrecognised record types and
   unknown fields rather than silently ignoring them. The divergence is already real in both
   directions (`SPEC.md` §4) and **neither gap announced itself**. The next schema change won't
   either.

3. **A findings feedback loop — and it conflicts with §9.** Every proposing skill shares a failure
   mode without one: it re-surfaces a rejected suggestion every run until the user stops reading
   the output. Two designs exist (`NOTES.md` §2.3); prefer agent-retro's status table for the
   mechanism and claude-improve's promotion rule for the lifecycle. **This would be sessionkit's
   only persistent writable state**, so adopting it requires either writing a named exception into
   §9 or keeping the file strictly skill-written to a user-visible path the user can read and
   delete by hand. *Recommend: the latter, decided after Phase 2 once the hooks proposal has
   produced real accept/reject decisions to learn from.*

   **Claude Code already shipped this pattern**, which settles the shape if not the decision: auto
   memory (§3.2.2) is skill-written, one plain-markdown fact per file, in a user-visible directory,
   readable and deletable by hand, and deliberately exempted from the retention sweep. That is the
   recommended design, already validated in the product this project analyses. The remaining
   question is only whether to write into that directory or beside it — prefer beside, since a
   proposing skill's accept/reject ledger is sessionkit's state, not Claude's memory.

4. **Pricing source: hand-maintained or generated?** Rates live in two hand-edited tables that were
   wrong for an unknown period. A generated, committed table refreshed by an explicit command
   removes the whole bug class without breaking §9's no-network rule — the artifact is committed,
   not fetched at runtime. *Recommend: decide in Phase 5, alongside the cost parity test.* Fold in
   **5-hour billing windows** there too: per-session sums do not match what Anthropic actually
   charges, in the UI or the CLI.

   `~/.claude.json:lastModelUsage` (§3.2.2) narrows this. It carries `costUSD` per model computed
   by Claude Code, so the hand-maintained tables can be checked against it without a network call
   and without a generator — for the last session per project, which is enough to detect a wrong
   rate but not enough to be the source of rates. If the tables agree with it across a few models,
   the generated-table work is not urgent; if they disagree, that is the bug class arriving again
   and the decision makes itself.

5. **Does OpenTelemetry replace part of this?** Claude Code ships built-in OTel support emitting
   spans around each model request and tool execution, plus accept/reject rates, latency, retries
   and quota. It does not obsolete sessionkit — OTel is prospective (42 MB of existing history
   stays transcript-only), needs a collector and a backend, and is aggregate-shaped rather than
   excerpt-shaped. But for *forward-looking* metrics it is plainly the better instrument.
   *Recommend: evaluate after Phase 2 — specifically whether `stall`, `turn_duration` and tool
   accept/reject are better sourced from OTel than re-derived. Do not start a metrics backend
   before then.*

6. **Is anything here better live than retrospective?** A warning raised while a session is still
   cheap to correct beats the same finding in a report written afterwards (`NOTES.md` §2.7). The
   tracker already owns the live half (SSE-backed session status), so this may belong in the UI.
   *Recommend: decide alongside §10.5, since OTel is the natural feed.*

7. **Does compaction destroy evidence we need?** `compaction-churn` reads what survived. If
   pre-compaction content is unrecoverable from the transcript, an archive hook is a
   **prerequisite for that detector being honest**, not an optional extra. *Recommend: verify
   against a compacted session before Phase 2 — a ten-minute check that decides whether a detector
   we already ship is telling the truth.*

8. **Cron.** `error-patterns` and `session-digest` are natural recurring jobs. *Recommend: manual
   until Phase 7, then revisit once signal-to-noise is known.*

9. **Whether any `sk` output should surface in the UI.** Today the two surfaces are independent and
   only summaries cross over. Error clusters and command rollups are plausible UI panels later;
   not in scope until a phase ships something worth embedding.
