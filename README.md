# sessionkit

Fleet-wide analysis of coding-agent sessions: parses every reachable transcript on demand and
emits reports sized for an agent's context window.

It covers Claude Code and pi, including pi-subagents async runs, which are loaded and linked to
their parent session. It exists because the local corpus is over a thousand sessions and grows
monotonically — the skills that analyse it cannot read it directly, so this does the reading and
hands back kilobytes.

**Status:** the error, command, hook, tail, files, search and cost commands are built, and so is
the agent post-mortem (`sk forensics`, `sk findings`) with three skills on top. See `PLAN.md`
for the remaining phases.

## Sources

`sk doctor` lists every source it probed and whether it was reachable. Transcript roots come
from environment variables, or from the tracker's `.env` (`SESSIONKIT_ENV` names one
explicitly); real environment variables win over the file.

| Key | Default | Source |
|---|---|---|
| `CLAUDE_DIR` | `~/.claude` | Claude Code, this host |
| `PI_DIR` | `~/.pi/agent` | pi (`sessions/` under it) |
| `CLAUDE_DIR_WSL`, `CLAUDE_DIR_WINDOWS` | unset | extra Claude Code roots |
| `AGENT_CLAUDE_ROOT` | unset | directory of per-container Claude Code stores |
| `OPENCODE_DATA_DIR` | unset | opencode (`opencode.db`) |

## Quick start

```bash
./sk doctor                  # what's reachable, corpus totals, unpriced models
./sk index --since 7d        # one line per session
./sk errors                  # cluster every failure across the fleet
./sk show <sid> --mode errors
./sk forensics <sid>         # post-mortem of one session: findings, health, timeline
./sk findings --group-by kind  # detector findings across the fleet, as rates
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
| **2 — aggregate** | `sk errors`, `sk findings`, `sk doctor` | ~4 KB | clusters and counts, one exemplar each |
| **3 — excerpt** | `sk show <sid> --mode …`, `sk forensics <sid>` | ~8 KB | one session, surgically |

Every command takes `--budget-kb` and `--json`. **Truncation is never silent** — a capped table
ends with `… N more row(s) omitted`, because a partial report that reads as complete is worse
than no report.

## Post-mortem model

`sk forensics` and `sk findings` report failures as findings. Each finding has:

- a **tier**: `S` is sk's verdict; `S->J` is a candidate the agent confirms or rejects (shown as
  `candidate` in output); `J` is a judgment class the `session-forensics` skill applies itself;
- a **family**: tool-protocol, generation, loop, lifecycle, recovery, alignment or environment;
- a **fix target**: model, harness, serving, env or none.

All of these live in one table in `sessionkit/taxonomy.py`. Detector thresholds are in
`taxonomy.THRESHOLDS`, tool roles per harness in `sessionkit/harness.py`, and the detectors in
`sessionkit/detectors/`. The knowledge base
`skills/session-forensics/reference/failure-classes.md` and `tests/test_parity.py` keep the docs
and the code in step. Design detail: `docs/superpowers/specs/2026-09-27-agent-postmortem-design.md`.

## Commands

| Command | Purpose |
|---|---|
| `doctor` | Source reachability, corpus totals, unpriced models |
| `index` | Layer 1 session list; `--since --project --source --state --finding --subagents` |
| `show <sid>` | Layer 3 excerpt; `--mode summary\|timeline\|messages\|tools\|errors` |
| `errors` | Layer 2 failure clusters; `--group-by class\|tool\|signature\|session\|model\|harness` |
| `commands` | Every tool call fleet-wide; `--group-by command\|tool\|session\|agent --agent-type` |
| `hooks` | Hook block/deny failures joined against `settings.json` |
| `forensics <sid>` | Layer 3 post-mortem: tiered findings, focus profile, anchored timeline, health |
| `findings` | Layer 2 detector findings as rates; `--group-by kind\|model\|harness\|project\|session --tier --family --kind` |
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
  taxonomy.py         failure table (error classes and finding kinds) and THRESHOLDS
  harness.py          tool roles per harness (Claude Code, pi)
  detectors/          anomaly and post-mortem detectors, one module per family group
skills/               skill definitions, symlinked into ~/.claude/skills/
  session-forensics/reference/   failure-class knowledge base the skill reads
docs/superpowers/     design specs and implementation plans
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
for skill in error-patterns session-forensics unfinished-work; do
  ln -sfn /mnt/c/Users/david/Projects/CAT_AI/claude-session-analyzer/skills/$skill \
    ~/.claude/skills/$skill
done
```

## Tests

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -t .
```

503 tests, no dependencies. Fixtures are built inline so each test shows the exact transcript
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
`sessionId` — they are keyed by `agentId` instead. subagents are a large share
of the files, so missing either fact loses a large part of the data.

**Safety.** Strictly read-only: `sk` writes nothing but its own stdout. Every preview passes
through secret redaction as it is built, so a key in a transcript cannot reach a terminal or an
agent's context. No network calls.
