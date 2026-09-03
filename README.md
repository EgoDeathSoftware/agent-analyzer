# sessionkit

Fleet-wide analysis of Claude Code sessions: parses every reachable transcript on demand and
emits reports sized for an agent's context window.

It exists because the corpus is ~42 MB across 87 files and growing monotonically — the skills
that analyse it cannot read it directly, so this does the reading and hands back kilobytes.

**Status:** Phase 0 (scaffolding) and Phase 1 (`error-patterns`) complete. See `PLAN.md` for the
remaining phases and the eight-skill scope.

## Quick start

```bash
./sk doctor                  # what's reachable, corpus totals, unpriced models
./sk index --since 7d        # one line per session
./sk errors                  # cluster every failure across the fleet
./sk show <sid> --mode errors
```

There is no setup step and nothing to keep current: every command parses what it needs and
exits. A whole-corpus command costs ~1.1 s; `sk show` finds its session by filename and costs
~0.25 s.

## Requirements

System Python 3.11+ and nothing else. No pip install, no virtualenv, no network. This is
deliberate: the skills must work from any session, including ones where the dev container is
down and `pnpm`/`ruff`/`pytest` are unavailable.

## The three-layer output contract

Every consumer descends these layers and never reads raw JSONL. This is what makes the corpus
tractable.

| Layer | Command | Budget | Content |
|---|---|---|---|
| **1 — index** | `sk index` | ~12 KB | one line per session |
| **2 — aggregate** | `sk errors`, `sk doctor` | ~4 KB | clusters and counts, one exemplar each |
| **3 — excerpt** | `sk show <sid> --mode …` | ~8 KB | one session, surgically |

Every command takes `--budget-kb` and `--json`. **Truncation is never silent** — a capped table
ends with `… N more row(s) omitted`, because a partial report that reads as complete is worse
than no report.

## Commands

| Command | Purpose |
|---|---|
| `doctor` | Source reachability, corpus totals, unpriced models |
| `index` | Layer 1 session list; `--since --project --source --state --subagents` |
| `show <sid>` | Layer 3 excerpt; `--mode summary\|timeline\|messages\|tools\|errors` |
| `errors` | Layer 2 failure clusters; `--group-by class\|tool\|signature\|session` |
| `commands` | Every tool call fleet-wide; `--group-by command\|tool\|session\|agent --agent-type` |
| `hooks` | Hook block/deny failures joined against `settings.json` |
| `forensics <sid>` | Layer 3: why one session went wrong |
| `children <sid>` | Agent dispatches from one session, resolved to child sid |
| `cost [sid]` | Layer 2/3 token/dollar totals; `--bloat --subagents` |
| `tail <sid>\|--all` | Last N turns of one session, with a tail signal |
| `files [sid]` | Files a session (or scope) touched; `--uncommitted` |
| `search <query>` | Layer 2 full-text search across every transcript; `--regex --context --per-session` |

Session ids accept a unique prefix: `sk show 95f3a6a6`.

## Layout

```
sk                    launcher (absolute path; skills invoke this)
sessionkit/           the package — zero third-party imports
skills/               skill definitions, symlinked into ~/.claude/skills/
tests/                stdlib unittest, no install step
PLAN.md               what we're building — scope, phases, acceptance
SPEC.md               why it's built this way — decisions and measurements
NOTES.md              what we learned — prior art, borrowed ideas, changelog
```

Nothing is written anywhere. There is no cache, no index and no database — the JSONL corpus is
the store of record and everything derived from it lives only for the length of one command.

## Installing a skill

Skills are versioned here and symlinked into the discovery path:

```bash
ln -sfn /mnt/c/Users/david/Projects/CAT_AI/claude-session-analyzer/skills/error-patterns ~/.claude/skills/error-patterns
```

## Tests

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -t .
```

96 tests, no dependencies. Fixtures are built inline so each test shows the exact transcript
shape it asserts against. Every anomaly detector has a positive *and* a negative case — a
detector that can never return false is not tested.

## Design notes

**Why not reuse the tracker's parser?** `claude-project-tracker` (a sibling repo)'s
`server/src/parser.ts` would couple every skill to its dev container being up (it was down when
this was written) or to its HTTP API, which serves UI-shaped `Session` objects rather than error
clusters. The duplication is narrower than it looks: sessionkit needs a normalised event stream,
not the tracker's rich session model.

**Pricing is duplicated with the tracker, and the two agree.** `claude-project-tracker`'s
`server/src/pricing.ts` was corrected on 2026-08-26 and ported from `sessionkit/pricing.py`, so
the UI and `sk` report the same figure for the same session. (Before that it billed Opus at
Opus-4.5 rates and had no Claude 5 entries.) They are two tables over one set of facts — change
one, change the other. `sk doctor` reports unpriced models instead of silently defaulting.

**Two transcript layouts.** Top-level sessions live at `projects/<project>/<session>.jsonl`;
subagent transcripts live one level deeper at
`projects/<project>/<parent-session>/subagents/agent-*.jsonl` and carry the **parent's**
`sessionId` — they are keyed by `agentId` instead. 36 of this corpus's 87 files are subagents,
so missing either fact loses 41% of the data.

**Safety.** Strictly read-only: `sk` writes nothing but its own stdout. Every preview passes
through secret redaction as it is built, so a key in a transcript cannot reach a terminal or an
agent's context. No network calls.
