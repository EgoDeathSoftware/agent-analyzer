# pi source support: a third session kind

**Date:** 2026-09-25 · **Status:** Designed, not built.

`sessionkit` currently discovers two kinds of session store (`claude-code`, `opencode` — though
only `claude-code` has a working parser; `opencode`'s is aspirational, per `sources.py`'s
docstring and the absence of any `opencode`-specific code in `parse.py`/`corpus.py`). This spec
adds a third, real one: **pi** (`@earendil-works/pi-coding-agent`, installed at
`~/.pi/agent/` on this machine, npm package `@earendil-works/pi-coding-agent@0.84.1`).

**Scope:** discovery of pi's session store, a parser from pi's JSONL into the existing
`ParsedSession` model, wiring into `corpus.py`, one widened anomaly check, and rewording the
three existing skills to stop saying "Claude Code" when they mean "every reachable session."
No new CLI subcommands — every existing command already works fleet-wide across sources.

## 1. What was verified before designing

- **pi's own docs are authoritative and complete.**
  `node_modules/@earendil-works/pi-coding-agent/docs/session-format.md` (installed on this
  machine, not fetched from the network) documents the JSONL schema, entry types, and the full
  `AgentMessage` union. Verified against the package's shipped `.d.ts` files.
- **This machine has no real pi session data yet.** `~/.pi/agent/sessions/--mnt-c-Users-david-Projects-CAT_AI-pi--/`
  exists but is empty. The parser is designed from the documented schema, not from a live
  corpus — unlike `sessionkit`'s Claude Code parser, whose module docstring says record shapes
  were "verified against the live corpus." This is a real gap: acceptance for this work must
  include feeding the parser a session produced by an actual `pi` run, not just hand-built
  fixtures, before calling it done.
- **pi's session files are a tree, not a line-sequential log.** Every entry has `id`/`parentId`;
  `/tree` branching appends new children rather than rewriting history. Claude Code's parser
  (`parse.py`) reads records in pure file order and has no notion of branches. This spec keeps
  that same simplification for pi (§3.4) rather than implementing leaf-path resolution.
- **The subagent extension is symlinked but not enabled.** `~/.pi/agent/extensions/subagent`
  points at the package's example extension, but `~/.pi/agent/settings.json` does not list it
  under an `extensions` key, so it is inactive on this machine today. It is still specced here
  (§4) because you asked for it now, and because its shape (nested nested `results[].messages[]`
  inside a `toolResult.details`) is fixed by the extension's source, not by live data.
- **pi already computes cost per message.** `usage.cost.total` on every `assistant` message and
  on `subagent` results. `sessionkit/pricing.py` is Claude-Code-only and is not extended for pi.
- **`classify.py` has no Claude-Code-specific field access.** Every function in it reads only
  `ParsedSession`/`ToolCall`/`SysEvent` — the generic model — with one exception: `_agent_trouble`
  matches the literal tool name `"Agent"` for dispatch-orphan detection (§5).

## 2. Source discovery — `sources.py`

Add `kind="pi"`:

- New `ENV_KEYS` entry: `"PI_DIR": ("pi", "pi", "single")`, default root `~/.pi/agent` (mirrors
  the existing `CLAUDE_DIR` default-to-home-dir pattern).
- `Source.sessions_root` property (new, alongside the existing `projects_dir`): for `kind=="pi"`,
  `root / "sessions"`; for the other kinds, callers keep using `projects_dir`. Kept as a property
  rather than branching in every caller.
- `_probe()`: for `kind=="pi"`, reachable if `root/sessions` is a directory. No `opencode.db` or
  `projects/` requirement — pi's layout has no such marker file to check.

## 3. Parser — new module `sessionkit/parse_pi.py`

Produces the *existing* `ParsedSession`/`Message`/`ToolCall`/`FileOp`/`SysEvent` dataclasses,
unchanged. `corpus.transcripts()` globs `sessions/*/*.jsonl` under a pi source (no `projects/`
indirection, no separate subagent-file glob) and `corpus.load_one()` dispatches to
`parse_pi.parse_file()` when `source.kind == "pi"`.

### 3.1 Header and per-entry mapping

| pi entry / field | `ParsedSession` field |
|---|---|
| `session.id`, `session.timestamp`, `session.cwd` | `sid`, `started_at`, `cwd` |
| `message` entry, `message.role == "user"` | `Message(role="user", ...)` |
| `message` entry, `message.role == "assistant"` | `Message(role="assistant", model=..., tok_*=usage.*, ...)`; each `content[]` block with `type=="toolCall"` opens a pending `ToolCall` |
| `message` entry, `message.role == "toolResult"` | closes the pending `ToolCall` matched by `toolCallId`: `is_error`, `output_preview`, `dur_ms` |
| `message` entry, `message.role == "bashExecution"` | a self-contained `ToolCall(name="bash", is_error=exitCode != 0 or cancelled)` — no pairing needed, the entry carries both call and result |
| `compaction`, `branch_summary` | `SysEvent(subtype="compaction"/"branch_summary", ...)` |
| `model_change`, `thinking_level_change`, `label`, `session_info` | `SysEvent`, subtype = entry `type` |
| `custom`, `custom_message` | `SysEvent(subtype="custom:<customType>")` |

Unmapped/unrecognized entry `type` values increment `malformed_lines`, same as Claude Code's
parser does for undecodable JSON — a schema `pi` adds later should not silently vanish.

### 3.2 Tool-call pairing

Same `pending: dict[str, ToolCall]` approach as `parse.py`, keyed by `toolCall.id` /
`toolResult.toolCallId` instead of `tool_use`/`tool_use_id`. A `toolCall` left pending when the
file ends gets `err_class="no-result"`, same as today.

### 3.3 File ops

Reuse the `FileOp` dataclass. New mapping table (pi's builtin tool names are lowercase and there
is no `MultiEdit`/`NotebookEdit`):

```python
FILE_TOOLS_PI = {"read": "read", "write": "write", "edit": "edit"}
```

Path key is `arguments["path"]` (pi's tools use `path`, not `file_path` — verified against
`dist/core/tools/*.d.ts`).

### 3.4 Tree handling (the documented tradeoff)

Entries are consumed in file order, exactly like `parse.py` does for Claude Code. Totals,
`messages`, and `tools` cover **every entry ever written**, including abandoned `/tree` branches
— not just the entries on the active leaf's path to root. This is a deliberate simplification,
not an oversight: computing the active path requires walking `id`/`parentId` backward from the
leaf, which needs a second pass and a definition of "the leaf" (pi's own `getLeafId()` is
runtime state, not a file fact). Flagged here so a future skill relying on "what actually
happened" rather than "everything ever attempted" knows to build leaf-path resolution rather
than assume today's parser already does it.

### 3.5 Cost and end state

`cost_usd`, `tok_in`, `tok_out`, `tok_cache_read`, `tok_cache_create` are summed directly from
each assistant message's `usage` (`usage.input`, `usage.output`, `usage.cacheRead`,
`usage.cacheWrite`, `usage.cost.total`) — no `pricing.py` call for pi sessions.

`classify.derive_end_state` is called unchanged from `corpus.load_one`, same as today. It already
only reads `err_class`, `sysev` subtypes, and the last message's role, all of which the pi parser
populates. No pi-specific end-state logic is added.

## 4. Subagent nesting

When a `toolCall` named `"subagent"` closes with a `toolResult` whose `details.results` is a
non-empty array (the example extension's `SubagentDetails.results: SingleResult[]`, per
`examples/extensions/subagent/index.ts`), each `results[i]` becomes a **synthetic child
`ParsedSession`**, built by the same §3.1–3.3 mapping applied to `results[i].messages[]` as a
flat list (no tree bookkeeping — the nested messages have no `id`/`parentId`, they're a plain
`Message[]` from the child `pi` process's `--mode json` stream):

- `sid = f"{parent_sid}:{tool_call_id}:{i}"`
- `is_subagent = True`, `parent_sid = <parent sid>`, `agent_type = results[i].agent`
- `cost_usd`/`tok_*` taken from `results[i].usage` directly (already aggregated by the
  extension), not re-summed from the nested messages
- `end_state`/`end_reason`: `("complete", "")` if `results[i].exitCode == 0` and no error
  `stopReason`, else `("error-cascade", results[i].errorMessage or results[i].stderr)` —
  reusing `derive_end_state`'s vocabulary rather than inventing a new one

`corpus.load_one` appends these synthetic children to `Corpus.sessions` alongside the parent and
records `parent.dispatch_edges.append((tool_call_id, child_sid))` — the same
`(tool_use_id, task_id)` shape Claude Code's `Agent` dispatch already produces, so `sk children
<sid>` needs no changes to work for pi.

**Not handled:** `parallel` and `chain` modes produce multiple `results[]` per single tool
call — each still becomes its own child session by the same rule (`i` disambiguates), so this
falls out of the design above without special-casing the mode.

## 5. `classify.py` change

`_agent_trouble` currently matches only `c.name == "Agent"`. Widen the check to
`c.name in ("Agent", "subagent")` so an orphaned pi subagent dispatch (a `subagent` tool call
with `err_class == "no-result"`) is detected the same way an orphaned Claude Code `Agent` dispatch
is. No other function in `classify.py` changes — verified in §1 that none of them touch
provider-specific fields.

## 6. CLI

No new subcommands. `--source pi` becomes a valid value everywhere `--source` is already
accepted (it is validated against `discover()`'s result, not a hardcoded enum — confirmed in
`cli.py`). `sk doctor` reports the new source's reachability with no changes to `cmd_doctor`.

## 7. Skills

Reword `skills/error-patterns/SKILL.md`, `skills/session-forensics/SKILL.md`,
`skills/unfinished-work/SKILL.md`: replace "Claude Code session(s)" with "coding-agent
session(s)" / "every reachable session" throughout, including frontmatter `description` fields.
No behavioral change — these skills already only ever call `sk`, never read transcripts
directly, so once `sk` covers pi, the skills do too. No fourth skill is added.

## 8. Tests

`tests/test_parse_pi.py`, mirroring the existing Claude Code parser test file's structure:
inline JSONL fixtures for — a plain user/assistant exchange; a `toolCall`/`toolResult` pair
(success and error); a `bashExecution` entry; a `compaction` entry; an unresolved trailing
`toolCall` (asserting `err_class == "no-result"`); and a `subagent` `toolResult` with a
`details.results` array of 2, asserting two synthetic children plus one `dispatch_edges` entry
on the parent.

`tests/test_sources.py` / `tests/test_corpus.py`: a pi source round-trips through `discover()`
→ `scannable()` → `load()`, using a temp directory shaped like `sessions/--tmp-project--/*.jsonl`
rather than mocking the filesystem.

**Acceptance gap called out in §1**: before this is considered done, run a real `pi -p "..."`
locally (session-saving is the default; only `--no-session` disables it), confirm the resulting
file actually parses, and fold any real-schema surprises back into this spec and the fixtures
above. Fixture-only tests against
a schema nobody has run against real output is exactly the failure mode `SPEC.md` already warns
about for the Claude Code parser.

## 9. Explicitly out of scope

- Leaf-path-only parsing (§3.4) — full-tree parsing ships first; revisit if a skill needs it.
- A pi equivalent of hook-block detection — pi's extension model has no direct analog to Claude
  Code's `PreToolUse`/`PostToolUse` hooks exposed in the session file; `_hook_pingpong` simply
  never fires for pi sessions, which is correct, not a gap.
- `opencode` support — untouched by this work; still aspirational as found in §1.
