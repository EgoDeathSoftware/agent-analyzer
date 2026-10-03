# Workflow Cost Attribution & Forensics — Design

Status: **written spec, awaiting review.** All six sections were approved in
brainstorming on 2026-10-03.

## 1. Goal and scope

Workflow-driven sessions (107 agents, 52M tokens) are invisible to `sk cost
--subagents` and `sk forensics`. The real spend shows `dispatches: []` because
sessionkit only follows `Agent`-tool children, not `Workflow`-internal agents.
The workflow subagent transcripts at `subagents/workflows/<runId>/agent-*.jsonl`
(143 files in the test corpus) are never discovered — `corpus.py:transcripts()`
globs `*/subagents/*.jsonl` and misses the deeper `workflows/<runId>/` path.
The `wf_*.json` file already has structured per-agent phase/model/token data that
sessionkit doesn't ingest. Two cost-efficiency patterns (single-model fan-out,
low-yield verification) are invisible to `sk forensics`.

**In scope**
- A new `sessionkit/workflow.py` module that parses `wf_*.json` (§2).
- Discovery of workflow subagent transcripts and linking them to their parent
  session (§3).
- Per-phase cost attribution and per-agent model in `sk cost --subagents` and
  `sk forensics` (§4).
- Two new tier-S finding kinds — `uniform-model-fanout` and
  `low-yield-verification` — in a new `cost` family (§5).
- Tests and real-session validation (§6).

**Out of scope**
- **Multi-level workflow nesting.** Workflows that dispatch sub-workflows are
  not walked recursively. One level of `Workflow`→children is enough for the
  corpus today.
- **Cost-tiering recommendations beyond the two findings.** No automated
  suggestion of which model to use for which phase — the finding flags the
  pattern; the remedy is the skill's job.
- **Live workflow monitoring.** Same constraint as the existing forensics
  system: everything is parsed on demand from completed transcripts.

**Constraints**
- System Python 3.11+, stdlib only.
- Additive model fields with defaults.
- Tests use inline fixture builders, never committed `.jsonl` files.
- The existing tests must keep passing.

## 2. Architecture: `sessionkit/workflow.py`

A new module owns workflow JSON parsing. It is a pure-data reader — no file
writes, no session parsing, no detector logic.

### Data model

```python
@dataclass
class WorkflowAgent:
    agent_id: str        # from workflowProgress[].agentId
    phase_index: int     # from workflowProgress[].phaseIndex
    phase_title: str     # from workflowProgress[].phaseTitle
    model: str           # from workflowProgress[].model
    tokens: int          # from workflowProgress[].tokens
    tool_calls: int      # from workflowProgress[].toolCalls
    label: str           # from workflowProgress[].label
    state: str           # from workflowProgress[].state

@dataclass
class WorkflowRun:
    run_id: str          # "wf_30c2aca0-aba"
    workflow_name: str   # "deep-research"
    default_model: str    # "glm-5.2-dbs[1m]"
    agent_count: int     # 107
    total_tokens: int     # 8884333
    total_tool_calls: int
    duration_ms: int
    phases: list[dict]   # [{title, detail}, ...] from wf_*.json phases[]
    agents: list[WorkflowAgent]
    stats: dict           # from result.stats: confirmed, killed, etc.
    status: str           # "completed"
    parent_sid: str       # resolved from the Workflow tool_result
```

### Functions

```python
def parse_workflow(path: Path) -> WorkflowRun:
    """Parse one wf_*.json into a WorkflowRun."""

def discover_workflows(source: Source) -> list[tuple[Path, str]]:
    """Every wf_*.json under a source's projects/, paired with project dir name.
    Globs: projects/<project>/workflows/wf_*.json"""

def workflow_phase_rows(run: WorkflowRun, children: list[Loaded]) -> list[Row]:
    """Per-phase breakdown: phase, agents, tokens, cost, model(s).
    Joins WorkflowAgent.agent_id to child session sids."""
```

The `Corpus` dataclass gains `workflows: dict[str, WorkflowRun]` (keyed by
parent sid). `corpus.load()` discovers and parses workflow files alongside
transcripts.

### Why a separate module

The workflow JSON is a different format from session transcripts (JSON object,
not JSONL), has different fields (phases, progress, stats), and serves a
different purpose (metadata about the parent's dispatch, not a session to
analyse). Keeping it in its own module avoids polluting `parse.py` with a
second format and lets the workflow logic be tested in isolation.

## 3. Discovery and linking (items 1–2)

### Item 1: make `sk cost --subagents` see workflow agents

**Discovery fix — `corpus.py:transcripts()`:**

Add a third glob alongside the existing two:

```python
files = sorted(project_dir.glob("*.jsonl"))
files.extend(sorted(project_dir.glob("*/subagents/*.jsonl")))
files.extend(sorted(project_dir.glob("*/subagents/workflows/*/agent-*.jsonl")))
```

This discovers the 143 workflow subagent transcripts currently invisible. They
parse normally — they have `isSidechain`/`agentId` like any subagent, and the
existing `_Parser._meta` flags them `is_subagent=True` with `agentId` as their
sid.

**Role mapping — `harness.py`:**

Add `"Workflow": "dispatch"` to `_TOOL_ROLES["claude-code"]`. This makes
`children_rows()` recognize `Workflow` tool calls as dispatches.

**Linking — `corpus.py:_link_workflow_children(sessions)`:**

New function, called from `load()` after `_link_async_runs()`:

1. For each parent session, find `Workflow` tool calls (role == "dispatch").
2. Extract the run id from the `Workflow` tool_result text:
   `r"Run ID:\s*(wf_[a-z0-9-]+)"`.
3. Find child sessions by matching their path prefix:
   `subagents/workflows/<runId>/agent-*.jsonl`. Their `sid` (which is their
   `agentId`) becomes the child sid.
4. Populate `dispatch_edges` with `(tool_use_id, child_agentId)` pairs.

This makes `children_rows()` resolve `Workflow` dispatches the same way it
resolves `Agent` dispatches — no special-casing in the query layer. The
existing `subagent_dispatch_rows()` and `subagent_cost_summary()` functions
work unchanged.

### Item 2: parse `wf_*.json` as first-class cost source

`corpus.load()` discovers `wf_*.json` files via `discover_workflows()` (glob
`projects/<project>/workflows/wf_*.json`). Each is parsed by
`parse_workflow()` and stored in `corpus.workflows[parent_sid]`.

The parent sid is resolved by matching the workflow's transcript directory
(`subagents/workflows/<runId>/`) back to the parent session that owns that
directory — same path-based join as item 1.

`sk cost --subagents` (session-scoped) and `sk forensics` both render a
workflow summary section from this parsed data: `agentCount`, `totalTokens`,
`durationMs`, `status`, `defaultModel`.

## 4. Per-phase cost attribution and per-agent model (items 3–4)

### Item 3: per-phase table

New `query.py:workflow_phase_rows(entry, corpus)` produces per-phase rows:

| phase | agents | tokens | cost | model(s) |
|---|---|---|---|---|

- **agents**: count of `WorkflowAgent` entries in that phase (from
  `workflowProgress[]`).
- **tokens**: sum of `WorkflowAgent.tokens` across the phase. This is the
  workflow's own token count — a cross-check against the per-child `cost_usd`
  derived from transcripts.
- **cost**: sum of child sessions' `cost_usd` in that phase (from the
  transcripts, the detailed usage breakdown).
- **model(s)**: from `WorkflowAgent.model`.

The join: workflow child sessions are in `corpus.sessions` (after item 1's
discovery fix). Each has `is_subagent=True` and its `sid` equals the `agentId`
from `workflowProgress[]`. The phase comes from `WorkflowAgent` metadata, not
from the child transcript — no first-user-message heuristic needed.

`cli.py:_cmd_cost_session()` renders this table after the existing per-child
dispatch table when `--subagents` is passed. `cmd_forensics` renders the same
table in a "Workflow phases" section.

### Item 4: per-agent model in the breakdown

The `model(s)` column handles this directly:
- If all agents in a phase share one model, show it once: `glm-5.2-dbs[1m]`.
- If mixed, show a compact breakdown: `glm-5.2-dbs[1m]: 20, claude-haiku-4-5: 5`.

Data comes from `WorkflowAgent.model` — no transcript grep needed. The
workflow summary section (item 2) shows `defaultModel` alongside, making
"did it use one model for everything" a one-command answer.

### Rendering order for session-scoped `sk cost --subagents`

1. Totals (existing)
2. Cost by tool (existing)
3. Subagents — per-child dispatch table (existing, now populated for workflows)
4. **Workflow phases** (new) — per-phase breakdown
5. **Workflow summary** (new) — agentCount, totalTokens, durationMs, status,
   defaultModel

## 5. Two new forensics findings (items 5–6)

Both are tier `S` (structural — deterministic from the workflow JSON), family
`cost`, and attach to the **parent session**, not to individual child agents.

### Item 5: `uniform-model-fanout`

**Threshold** (in `taxonomy.THRESHOLDS`):
- `uniform_model_fanout_min_agents`: 10

**Detection** (in `corpus.py:_add_workflow_findings()`):
- Workflow run has `agent_count >= threshold`.
- Every agent's `model == run.default_model`.
- Emit `Anomaly("uniform-model-fanout", "{N} agents all on {default_model},
  zero model overrides", N, [])`.

**Fix target**: `harness` — tier cheaper models for verify/fetch phases in the
workflow script or instructions.

### Item 6: `low-yield-verification`

**Thresholds**:
- `low_yield_kill_rate_max`: 0.15 (kill rate below 15%)
- `low_yield_min_votes`: 30 (at least 30 verify-phase agents)

**Detection**:
- Find the phase with "verify" in `phaseTitle` (case-insensitive) in
  `workflowProgress[]`.
- Compute kill rate from `run.stats["killed"] / (run.stats["confirmed"] +
  run.stats["killed"])` when `result.stats` is available.
- If kill rate < threshold AND verify-phase agent count >=
  `low_yield_min_votes`, emit `Anomaly("low-yield-verification", "Verify: {N}
  votes, {K}% kill rate — over-provisioned", N, [])`.

**Fix target**: `harness` — reduce votes per claim or raise the verification
bar.

### Where detection runs

Both are corpus-level findings (they need the parsed `WorkflowRun`, not just a
`ParsedSession`), detected in a new `_add_workflow_findings(sessions,
workflows)` called from `corpus.load()` after `_add_cross_session_findings()`.
They attach to the parent session's `anomalies` list, so `sk forensics
<parent-sid>` and `sk findings --kind uniform-model-fanout` work with no
additional wiring.

### Taxonomy update checklist

1. Add both `_kind()` entries to `FINDING_KINDS` in `taxonomy.py`.
2. Add thresholds to `THRESHOLDS`.
3. Add `"cost"` to `VALID_FAMILIES` in `tests/test_taxonomy.py`.
4. Add `"cost"` to `--family` choices in `cli.py` (~line 898).
5. Add doc entries in `skills/session-forensics/reference/failure-classes.md`
   (parity test `tests/test_parity.py` enforces this).
6. The detector functions live in `corpus.py` (like `resource_interference`),
   not in `sessionkit/detectors/` — they need the `WorkflowRun`, which is
   corpus-level. The parity test (`tests/test_detectors_registry.py`) greps
   `detectors/` source for `Anomaly("kind")` literals; it must be updated to
   also scan `corpus.py` so the two new kinds are found. `resource_interference`
   is already an exception (it emits `Anomaly` from `corpus.py` via
   `interference.py`); this extends the same pattern rather than introducing a
   new one.

## 6. Data flow

```
corpus.load()
  ├─ transcripts()                          # now discovers workflows/<runId>/agent-*.jsonl
  ├─ load_one() per transcript              # existing — workflow subagents parse normally
  ├─ discover_workflows() + parse_workflow() # NEW: load wf_*.json into corpus.workflows
  ├─ _disambiguate()                        # existing
  ├─ _link_async_runs()                     # existing (pi)
  ├─ _link_workflow_children()             # NEW: Workflow tool_use → child agentIds
  ├─ _add_cross_session_findings()          # existing
  └─ _add_workflow_findings()              # NEW: uniform-model-fanout, low-yield-verification
```

## 7. Files to modify

| File | Changes |
|---|---|
| `sessionkit/workflow.py` | **NEW**: `WorkflowRun`, `WorkflowAgent` dataclasses, `parse_workflow()`, `discover_workflows()`, `workflow_phase_rows()`. |
| `sessionkit/corpus.py` | `transcripts()`: add workflow subagent glob. `Corpus`: add `workflows` field. `load()`: discover+parse `wf_*.json`, call `_link_workflow_children()` and `_add_workflow_findings()`. Add both functions. |
| `sessionkit/harness.py` | Add `"Workflow": "dispatch"` to `_TOOL_ROLES["claude-code"]`. |
| `sessionkit/taxonomy.py` | Add `uniform-model-fanout` and `low-yield-verification` to `FINDING_KINDS`. Add thresholds. |
| `sessionkit/query.py` | Add `workflow_phase_rows()`, `workflow_summary()`. |
| `sessionkit/cli.py` | `_cmd_cost_session()`: render per-phase table + workflow summary when `--subagents`. `cmd_forensics()`: render workflow sections. Add `"cost"` to `--family` choices. |
| `sessionkit/detectors/__init__.py` | No change — corpus-level detectors are not registered here. |
| `skills/session-forensics/reference/failure-classes.md` | Add entries for both new kinds. |
| `tests/test_workflow.py` | **NEW**: workflow parsing, discovery, phase attribution, findings. |
| `tests/test_taxonomy.py` | Add `"cost"` to `VALID_FAMILIES`. |
| `tests/test_detectors_registry.py` | Extend source scan to include `corpus.py` for corpus-level detectors. |
| `tests/test_parity.py` | Add the two new kinds to the doc-sync check. |

## 8. Tests

All tests use inline fixture builders — no committed `.jsonl` or `.json` files.

### New `tests/test_workflow.py`

- `parse_workflow()` — build a minimal `wf_*.json` dict, assert `WorkflowRun`
  fields parsed correctly.
- `discover_workflows()` — temp dir with `projects/<proj>/workflows/wf_xxx.json`,
  assert found.
- `workflow_phase_rows()` — workflow with 3 phases, assert per-phase
  agent/token/model aggregation.
- `_link_workflow_children` — parent session with a `Workflow` tool_result
  containing `Run ID: wf_xxx`, child sessions with matching agentIds, assert
  `dispatch_edges` populated.
- `uniform_model_fanout` — workflow with 15 agents all on `defaultModel` →
  finding emitted; one agent on a different model → no finding.
- `low_yield_verification` — workflow with `stats.confirmed=22, killed=3`, 75
  verify agents → finding emitted; `killed=10` → no finding; fewer than 30
  verify agents → no finding.
- `low_yield_verification` — no `result.stats` (field absent) → no finding
  (graceful degradation).

### Existing test updates

- `tests/test_taxonomy.py` — add `"cost"` to `VALID_FAMILIES`.
- `tests/test_detectors_registry.py` — extend the source scan to include
  `corpus.py` so the two new corpus-level kinds are found by the parity test.
  `resource_interference` already emits from `corpus.py` via `interference.py`;
  this extends the same pattern.
- `tests/test_parity.py` — add the two new kinds to the doc-sync check.

## 9. Verification

```bash
# 143 newly-discovered workflow subagents now visible
./sk doctor

# The 107-agent deep-research workflow session
./sk cost 81ce5cb3 --subagents
# → Subagents table shows 107 workflow children
# → Workflow phases: Scope=1, Search=5, Fetch=25, Verify=75, Synthesize=1
# → Verify = ~6.3M tokens (81% of total)

./sk forensics 81ce5cb3
# → Findings table includes uniform-model-fanout, low-yield-verification

# The 20-agent chrome-target-review workflow (different default model)
./sk cost 4298d835 --subagents
# → 20 agents, model claude-sonnet-4-6[1m]

# Fleet-wide findings
./sk findings --group-by kind
# → uniform-model-fanout and low-yield-verification appear as rows

./sk findings --family cost
# → filters to the two new cost findings

# All existing tests pass
pytest
```

## A. Real data reference

The design was validated against two real workflow runs in the corpus:

| Property | deep-research | chrome-target-review |
|---|---|---|
| run_id | `wf_30c2aca0-aba` | `wf_662f4b29-b02` |
| agent_count | 107 | 20 |
| total_tokens | 8,884,333 | 976,250 |
| default_model | `glm-5.2-dbs[1m]` | `claude-sonnet-4-6[1m]` |
| phases | Scope, Search, Fetch, Verify, Synthesize | 2 phases |
| Verify agents | 75 (70%) | 0 |
| Verify tokens | 6,260,220 (81%) | 0 |
| stats.confirmed | 22 | — |
| stats.killed | 3 | — |
| kill rate | 12% | — |
| all-same-model | yes | yes |
| workflow subagent files | 107 | 20 |

Both runs show `dispatches: []` today. Both have `defaultModel` matching every
agent's model. The deep-research run triggers both new findings;
chrome-target-review triggers `uniform-model-fanout` only (20 agents ≥ 10
threshold, no verify phase).
