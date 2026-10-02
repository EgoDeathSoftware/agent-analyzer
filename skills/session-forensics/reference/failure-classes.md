# Failure classes — post-mortem reference

One entry per class in `sessionkit/taxonomy.py`; `tests/test_parity.py` keeps the two in step.
Load only the entries for classes the report actually contains.

Each entry gives:
- the tier: **S** = sk's verdict, **S->J** = a candidate you confirm, **J** = your judgment;
- the family;
- the fix target: **model**, **harness** (instructions file, harness config, token limits),
  **serving** (vLLM parser, chat template, sampling), **env**, or **none**.

Where instructions go:
- **Claude Code:** `CLAUDE.md` for rules, `settings.json` hooks for enforcement.
- **pi:** `AGENTS.md` for rules (pi also reads `CLAUDE.md`), `.pi/SYSTEM.md` for the system
  prompt, and a `tool_call` extension for enforcement.
- **pi model limits:** set per model in `~/.pi/agent/models.json` (`maxTokens`,
  `contextWindow`, `compat`). The thinking level lives in `settings.json`.

## Tool protocol

### `args-dropped`
S · tool-protocol · fix: serving
- **What:** the harness received a tool call with empty arguments (`Received arguments: {}`).
  pi repairs unparseable tool-call JSON to `{}`, so the model's arguments were lost between the
  model and the harness.
- **Confirm:** the model's own text or reasoning shows it meant to pass arguments. Several
  such failures in one session point to a parser problem, not a model mistake.
- **Fix:** check the vLLM `--tool-call-parser` and chat template for this model family.
  Upgrade vLLM if a parser fix exists; for pi, confirm the provider `compat` settings in
  `models.json`.

### `invalid-args`
S · tool-protocol · fix: model
- **What:** the arguments failed the tool's schema (wrong type, missing required field).
- **Confirm:** read the validation message. One slip is noise; repeats on the same tool mean
  the model misunderstands its schema.
- **Fix:** add a one-line usage example for that tool to the instructions file. If it
  persists on one model, compare rates with `sk findings --group-by model --kind
  invalid-args`.

### `unknown-tool`
S · tool-protocol · fix: model
- **What:** the model called a tool that does not exist (`Tool X not found`,
  `Unknown skill:`).
- **Confirm:** compare the name with the real tool list. Near-misses such as `bash_exec` for
  `bash` are hallucinations; names from another harness point to training carry-over.
- **Fix:** name the available tools in the instructions file. Remove stale tool names from
  skills and prompts.

### `truncated-call`
S · tool-protocol · fix: harness
- **What:** a tool call was cut off by the output token limit and never ran.
- **Confirm:** the assistant message's stop reason is `length`, and the call's arguments are
  large (a whole file in a write).
- **Fix:** raise `maxTokens` for the model (pi `models.json`), or tell the agent to write
  large files in parts.

### `edit-mismatch`
S · tool-protocol · fix: model
- **What:** an edit's old text was not found, was not unique, changed nothing, or overlapped
  another edit.
- **Confirm:** read the preceding read of that file. A stale view (an edit in between, or a
  formatter hook) or whitespace drift is the usual cause.
- **Fix:** add the rule "re-read the exact region before editing" to the instructions file.
  If a formatter hook rewrites files, say so there.

### `tool-misuse`
S · tool-protocol · fix: model
- **What:** a tool was pointed at the wrong kind of target, such as reading a directory
  (`EISDIR`) or using an offset past the end of the file.
- **Confirm:** check the path in the call against what an earlier listing showed.
- **Fix:** tell the agent to list or check a path before reading it. Usually a one-off.

### `sibling-cascade`
S · tool-protocol · fix: none
- **What:** a parallel call failed only because a sibling in the same batch failed.
- **Confirm:** find the sibling's own error; that is the real finding.
- **Fix:** none on its own; fix the sibling's failure.

### `file-too-large`
S · tool-protocol · fix: model
- **What:** a full read exceeded the tool's token cap.
- **Confirm:** the result names the file size.
- **Fix:** use offset/limit reads or search first. State this in the instructions file for
  repos with large files.

### `read-truncated`
S · tool-protocol · fix: model
- **What:** the read returned only part of the file.
- **Confirm:** check whether the agent later acted on content past the cut.
- **Fix:** use narrower reads; watch for false-premise edits based on unseen content.

### `stale-read`
S · tool-protocol · fix: model
- **What:** the file changed after the agent read it (another writer, hook or subagent).
- **Confirm:** look for a concurrent writer: a formatter, a parallel agent, or the user.
- **Fix:** re-read before editing. If agents run in parallel on one tree, give each its own
  worktree.

### `unread-file`
S · tool-protocol · fix: model
- **What:** a write or edit was attempted on a file that was never read.
- **Confirm:** there was no earlier read of the path.
- **Fix:** add the rule "read before editing". It is harmless but costs a round trip.

### `tool-text-leak`
S · tool-protocol · fix: serving
- **What:** the assistant wrote a tool call as plain text (`<tool_call>`, `<function=`, a bare
  `{"name": …, "arguments": …}` or harmony markup). No call ran, and the turn ended as if it
  were complete.
- **Confirm:** the cited message's text holds call markup and the next thing is the user
  re-prompting or the session ending. It is common on Qwen and gpt-oss, and worse at long
  context.
- **Fix:** check the vLLM `--tool-call-parser` and chat template for this model, and use a
  version with the parser fix. Keep the context smaller. If one model leaks repeatedly,
  compare it on `sk findings --group-by model --kind tool-text-leak`.

## Generation

### `empty-turn`
S · generation · fix: serving
- **What:** a turn ended normally with no text and no tool call, usually reasoning-only
  output.
- **Confirm:** the Health section shows reasoning on that turn and no visible text.
- **Fix:** check the reasoning parser and chat template, or lower the thinking level (pi
  `settings.json`).

### `degeneration`
S · generation · fix: serving
- **What:** one turn repeats itself: the same plan restated many times, typically in the
  reasoning.
- **Confirm:** read the cited message. Look for the point where it starts repeating, and what
  it was stuck on (often an unresolved ambiguity or an unreachable resource).
- **Fix:** adjust sampling for this model. Raise the temperature to the vendor's recommended
  value and add a repetition or presence penalty. Lower the thinking level. Keep the context
  smaller before hard steps. A 4-bit quantization degenerates more than a higher-precision
  one.

### `output-truncated`
S · generation · fix: harness
- **What:** a turn hit the output token limit.
- **Confirm:** the cited turn's stop reason is `length`.
- **Fix:** raise `maxTokens`, and keep the context-window reservation in mind (see
  `context-overflow`). Ask for smaller steps.

### `generation-stall`
S · generation · fix: serving
- **What:** one model turn took far longer than the session's norm.
- **Confirm:** check the turn for repetition (see `degeneration`), and check whether the
  server was busy then (see `resource-interference`). Median latency in Health tells a slow
  model from a stalled turn.
- **Fix:** same as `degeneration` when repetition is present. Otherwise address the server's
  load, or set a request timeout in the harness.

## Loops

### `repeat-tool`
S · loop · fix: model
- **What:** the identical call (same tool, same input) ran three or more times. Polling tools
  are exempt.
- **Confirm:** check whether the results changed between calls. If nothing changed, the agent
  ignored the result.
- **Fix:** add the rule "when a command's result is unchanged, stop and diagnose" to the
  instructions file.

### `file-thrash`
S · loop · fix: model
- **What:** one file was edited many times, or several times with failures in between.
- **Confirm:** read the edit sequence. Planned incremental edits are fine; undo/redo cycles
  are not.
- **Fix:** plan the full change before editing. Use one multi-edit call.

### `read-loop`
S · loop · fix: model
- **What:** the same file was re-read five or more times. Reads after an edit or a compaction
  are not counted.
- **Confirm:** check whether the agent used what it read.
- **Fix:** tell the agent to keep notes of what it read. Frequent loops can mean the context
  is too small for the task.

### `error-cascade`
S · loop · fix: model
- **What:** three or more consecutive failing calls.
- **Confirm:** check whether the calls were variants of one approach. The first failure's
  cause is usually the finding.
- **Fix:** add the rule "after two failures, stop and diagnose the root cause" to the
  instructions file.

### `hook-pingpong`
S · loop · fix: harness
- **What:** the same tool was blocked by a hook repeatedly.
- **Confirm:** the model kept emitting the forbidden form.
- **Fix:** state the rule in the instructions file (`CLAUDE.md`, or `AGENTS.md` for pi) and
  keep the hook as enforcement.

### `rejection-persist`
S · loop · fix: model
- **What:** a call the user rejected was retried unprompted.
- **Confirm:** the retry has the same input as the rejected call.
- **Fix:** add the rule "a rejection means ask, don't retry" to the instructions file.

### `edit-mismatch-spiral`
S · loop · fix: model
- **What:** edits to one file failed three or more times.
- **Confirm:** check whether the agent re-read the file between attempts.
- **Fix:** add the rule "re-read the exact region before each edit". On weak models, prefer
  whole-file writes for small files.

### `read-only-spiral`
S · loop · fix: model
- **What:** a long run of reads and searches with no edit, command or answer.
- **Confirm:** check whether the reads converged on something. Aimless exploration and
  necessary discovery look alike, so read the run's paths.
- **Fix:** ask for a plan or a first change. Add the rule "state a hypothesis after N
  reads". For open models, a smaller first task.

## Lifecycle

### `aborted`
S · lifecycle · fix: none
- **What:** the user stopped a call or turn.
- **Confirm:** see `user-interrupt` for what came next.
- **Fix:** none on its own.

### `killed`
S · generic · fix: env
- **What:** a command was killed (SIGKILL, usually out-of-memory) or terminated.
- **Confirm:** check the command and the machine's memory at the time.
- **Fix:** reduce the command's footprint or raise the limit.

### `agent-kill`
S · lifecycle · fix: none
- **What:** a subagent was killed before returning.
- **Confirm:** check whether the user stopped it or a timeout did.
- **Fix:** if it was a timeout, raise it or narrow the delegated task.

### `orphan-subagent`
S · lifecycle · fix: harness
- **What:** a subagent was dispatched and never returned.
- **Confirm:** run `sk children <sid>`. An unresolved dispatch means no result ever came
  back.
- **Fix:** check the subagent's own session for a hang; make the parent wait or poll.

### `compaction-churn`
S · lifecycle · fix: harness
- **What:** the context was compacted more than once.
- **Confirm:** check whether behavior changed after a compaction, such as forgotten
  constraints.
- **Fix:** split the work, narrow the scope, or use a model with a larger window.

### `stall`
S · lifecycle · fix: env
- **What:** a tool call ran far too long. Commands with a declared timeout only count near
  that timeout. In Claude Code, local reads and searches, and commands that overran their
  declared timeout, are skipped because their duration includes waiting for approval.
- **Confirm:** check what the command was waiting on: network, a lock, or an interactive
  prompt.
- **Fix:** give the command a timeout. Avoid interactive commands.

### `hung`
S · lifecycle · fix: env
- **What:** the session ended waiting on a call, or on a model turn that never finished.
- **Confirm:** check what was pending and whether its server or process was alive.
- **Fix:** fix the pending tool or server; set timeouts.

### `abrupt-truncation`
S · lifecycle · fix: none
- **What:** the transcript ends in a partial write, meaning the agent process died.
- **Confirm:** check for an OOM, a crash log, or the terminal being closed.
- **Fix:** investigate the harness process (memory, crashes); there is nothing in the
  transcript to fix.

### `user-interrupt`
S · lifecycle · fix: none
- **What:** the user stopped the agent and redirected it.
- **Confirm:** the redirect message says what the user wanted instead. Compare it with what
  the agent was doing (the paired `drift-candidate`).
- **Fix:** comes from the confirmed cause: drift, over-engineering, or a wrong approach.

## Recovery

### `error-ignored`
S->J · recovery · fix: model
- **What:** a call failed and the next message claims success.
- **Confirm:** read the failure and the claim. Dismiss the candidate if the failure was
  irrelevant to the claim, for example an optional check.
- **Fix:** add the rule "report failures honestly; re-run before claiming success" to the
  instructions file.

### `give-up`
S->J · recovery · fix: model
- **What:** the session stopped after a single failure.
- **Confirm:** judge whether the failure was recoverable (a typo, or a path that exists
  elsewhere). Dismiss the candidate if the environment genuinely blocked the task.
- **Fix:** add the rule "on failure, diagnose and try one alternative before stopping" to the
  instructions file.

## Alignment

### `unverified-completion`
S->J · alignment · fix: harness
- **What:** the agent claimed completion with no test or build after its last change.
- **Confirm:** check whether the change needed verification. Documentation changes may not.
- **Fix:** require a verification command before a completion claim in the instructions
  file.

### `premature-stop`
S->J · alignment · fix: model
- **What:** the session ended on a stated next step that never ran.
- **Confirm:** check whether the step was needed for the ask.
- **Fix:** add the rule "finish the stated plan or say why not" to the instructions file.
  With open models, check for an `empty-turn` or `tool-text-leak` at the end.

### `scope-creep`
S->J · alignment · fix: harness
- **What:** the ask was to review or explain, but files changed.
- **Confirm:** re-read the ask; implicit permission counts.
- **Fix:** state read-only intent explicitly. Use a plan/read-only mode where the harness has
  one.

### `drift-candidate`
S->J · alignment · fix: harness
- **What:** a large share of a turn went to things the ask never referenced, or the calls
  right before a user interrupt.
- **Confirm:** read the run's paths or commands against the ask. Necessary investigation (a
  dependency of a referenced file) is a dismissal. A rabbit hole, such as reading a
  package's source when the docs would do, is a confirmation.
- **Fix:** give the ask a scope: name the files or the expected approach. Add the rule "prefer
  docs over source; ask before deep dives" to the instructions file.

### `false-premise`
J · alignment · fix: model
- **What:** the agent acted on an assumption it never checked. This is the largest single
  cause of failure in the literature.
- **Confirm:** walk back from the first failure to the first unverified assumption.
- **Fix:** add the rule "verify assumptions (paths, versions, environment) before acting" to
  the instructions file, naming the specific assumption.

### `spec-neglect`
J · alignment · fix: harness
- **What:** a stated requirement of the ask was dropped.
- **Confirm:** list the ask's requirements and mark which ones were met.
- **Fix:** put requirements in a checklist the agent must tick. Tell it to restate the
  requirements before starting.

### `misread-output`
J · alignment · fix: model
- **What:** a tool result contradicted the agent's next action.
- **Confirm:** quote the output and the action that ignored it.
- **Fix:** model-side; compare models. For a harness fix, reduce output noise (quieter
  commands) so the signal is visible.

### `ignored-own-warning`
J · alignment · fix: model
- **What:** the agent stated a hazard or constraint, then acted against it.
- **Confirm:** quote both the warning and the violating action.
- **Fix:** state the constraint as a hard rule in the instructions file. Structural risks
  (for example, benchmarking the server the agent runs on) need an environment change, not a
  prompt.

### `role-drift`
J · alignment · fix: harness
- **What:** an orchestrator did its delegate's work in parallel with the delegate.
- **Confirm:** the parent's calls in the child's run window touch the child's task.
- **Fix:** the orchestration skill should say "while a delegate runs, only observe; steer
  through the dispatch tool".

## Environment

### `hook-block`
S · environment · fix: harness
- **What:** a hook rejected the call after the model emitted it.
- **Confirm:** the result names the hook's rule. Check whether that rule is already in the
  instructions file.
- **Fix:** Claude Code: state the rule in `CLAUDE.md` and keep the hook. pi: state the rule
  in `AGENTS.md`; enforcement is a `tool_call` extension.

### `user-rejected`
S · environment · fix: none
- **What:** the user declined a proposed call.
- **Confirm:** read what was proposed.
- **Fix:** an approach problem; clarify in the ask what not to attempt.

### `policy-denied`
S · environment · fix: harness
- **What:** a permission policy, a worktree isolation rule or an extension blocked the call.
  No human saw it.
- **Confirm:** check whether the call was reasonable.
- **Fix:** allowlist it, or state the boundary in the instructions file.

### `tool-error-structured`
S · environment · fix: env
- **What:** a tool returned `{"ok": false, "errors": …}`.
- **Confirm:** read the errors list; the input usually broke the tool's own rules.
- **Fix:** fix the input pattern. If one tool dominates, its documentation is unclear.

### `missing-tool`
S · environment · fix: env
- **What:** a command was not found.
- **Confirm:** check whether it is installed on this machine.
- **Fix:** install it, or tell the agent which tool to use instead.

### `permission-denied`
S · environment · fix: env
- **What:** the write target was not writable.
- **Confirm:** check the path.
- **Fix:** name the writable locations in the instructions file.

### `rate-limit`
S · environment · fix: env
- **What:** the provider or a tool throttled the request.
- **Confirm:** check how often it happened in the session.
- **Fix:** back off, or route to another provider.

### `api-error`
S · environment · fix: env
- **What:** a model or tool endpoint returned a server error, or finished for an unexpected
  reason (`content_filter`).
- **Confirm:** check whether it was transient.
- **Fix:** retry policy, or a provider check.

### `mcp-error`
S · environment · fix: env
- **What:** an MCP server failed (errors, or no transport).
- **Confirm:** check the server, not the prompt.
- **Fix:** restart or fix the MCP server.

### `context-overflow`
S · environment · fix: harness
- **What:** the prompt plus the reserved output tokens exceeded the context window.
- **Confirm:** read the error. Prompt tokens plus `maxTokens` exceed the window; pi then
  compacts and retries.
- **Fix:** lower the model's `maxTokens` in pi's `models.json` (32K reserved on a 131K window
  leaves 98K), or compact earlier. A large reservation on a small window is the common cause.

### `auth`
S · environment · fix: env
- **What:** the provider sign-in is missing or expired.
- **Confirm:** the error names `401`, `invalid_grant`, a login or OAuth.
- **Fix:** re-authenticate (`pi auth`, `/login`). Not a model problem.

### `network`
S · environment · fix: env
- **What:** the model endpoint was unreachable or too slow (`Request timed out`, connection
  refused).
- **Confirm:** check the server's health at that time, and whether something else was loading
  it (see `resource-interference`).
- **Fix:** server capacity or restarts; keep benchmarks and other heavy jobs off the server
  the agent uses.

### `resource-interference`
S->J · environment · fix: harness
- **What:** a model call failed while another session in the same project ran a long
  command.
- **Confirm:** open the other session (`sk show <sid> --mode tools`) and check that the
  command used the shared resource (the model server, GPU, a lock). Dismiss the candidate if
  the timing is coincidental.
- **Fix:** do not run heavy jobs on the server the agent depends on; serialize them, or point
  the agent at a separate endpoint during the job.

## Generic

### `not-a-repo`
S · generic · fix: none
- **What:** a git command ran outside a repository.
- **Confirm:** check the working directory.
- **Fix:** state the repo layout in the instructions file if this recurs.

### `not-found`
S · generic · fix: none
- **What:** a path did not exist.
- **Confirm:** check whether it was a false premise (an assumed path).
- **Fix:** tell the agent to list before reading. A recurring path belongs in the
  instructions file.

### `timeout`
S · generic · fix: none
- **What:** a command hit its timeout.
- **Confirm:** check whether the timeout was realistic.
- **Fix:** raise the timeout, or split the work.

### `exit-code`
S · generic · fix: none
- **What:** a command failed with a non-zero exit code.
- **Confirm:** read the output. This is often a genuine failure that needs no systemic fix.
- **Fix:** usually none; say so rather than inventing one.
