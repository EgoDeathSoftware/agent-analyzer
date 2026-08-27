# Map of `~/.claude`

What every file and directory in this Claude Code config directory is, what it contains, and who
writes it. Generated 2026-08-27 against Claude Code **2.1.247** on this container
(`ghcr.io/kafkaesqu3/agent-shell`, workspace `/workspace`).

Two kinds of things live here:

- **Config you author** — settings, hooks, skills, statusline. Stable across runs.
- **Application data Claude Code writes** — transcripts, prompt history, file snapshots, locks,
  caches. Created and rotated on every run.

Everything here is **plaintext**. Anything a tool reads or prints lands in a transcript on disk.
OS file permissions are the only protection.

---

## Quick tree (what actually exists here)

```
~/.claude.json                    app state, OAuth account, per-project stats  (OUTSIDE this dir)
~/.claude/
├── README.md                     this file
├── settings.json                 ★ user settings: env, permissions, hooks, plugins, statusline
├── statusline.sh                 ★ custom two-line statusline script (invoked per render)
├── hooks/                        ★ hook scripts referenced from settings.json
│   ├── check-imports.sh          PreToolUse(Write|Edit): blocks relative imports
│   └── post-write-lint.sh        PostToolUse(Write|Edit): shellcheck/actionlint/jq lint
├── skills/                       ★ personal skills, available in every project (16 here)
│   └── <name>/SKILL.md
├── plugins/                      installed plugins + cloned marketplaces (18 MB)
│   ├── installed_plugins.json    what is installed, version, install path, git SHA
│   ├── known_marketplaces.json   marketplace name → source repo + clone location
│   ├── marketplaces/<mp>/        git clone of each marketplace
│   ├── cache/<mp>/<plugin>/<ver>/  the actual installed plugin files
│   └── data/<plugin>-<mp>/       per-plugin scratch state
├── projects/-workspace/          per-project transcripts + auto memory
│   ├── <session-id>.jsonl        full conversation transcript (one per session)
│   ├── <session-id>/
│   │   ├── custom-title.json     session title set via /rename
│   │   └── tool-results/         large tool outputs spilled to files
│   └── memory/                   auto memory: MEMORY.md index + topic files
├── sessions/<pid>.json           one file per RUNNING session (liveness/crash detection)
├── session-env/<session-id>/     per-session environment metadata
├── tasks/session-<short-id>/     per-session task lists from the task tools
├── teams/session-<short-id>/     agent-team roster (experimental agent teams)
├── file-history/<session-id>/    pre-edit file snapshots for checkpoint restore
├── plans/*.md                    plan-mode plan documents
├── backups/                      last 5 copies of ~/.claude.json
├── cache/changelog.md            cached Claude Code changelog (568 KB)
├── security/                     security-guidance plugin state + its Python venv (298 MB)
├── shell-snapshots/              shell aliases/functions captured at startup
├── autoharness/requests/         autoharness plugin queue (empty)
├── history.jsonl                 every prompt you've typed, for up-arrow recall
├── .credentials.json             ★ OAuth tokens — secrets
├── .gitignore                    keeps credentials + caches out of git
├── .last-cleanup                 timestamp of last retention sweep
├── .last-update-result.json      result of the last self-update
└── .tracker-origin.json          container/host provenance for this sandbox
```

★ = authored by you; everything else is written by Claude Code or a plugin.

---

## Config you author

### `settings.json`
User-scope settings, applied to every project. Lowest precedence of the writable layers
(managed → CLI `--settings` → project local → project shared → **user**). What's set here:

| Key | Value in this file |
| --- | --- |
| `cleanupPeriodDays` | `365` — transcripts/snapshots kept a year instead of the 30-day default |
| `env` | telemetry, error reporting, and feedback survey off; `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` |
| `attribution` | no Claude co-author trailer on commits or PRs |
| `permissions.deny` | 42 rules: `rm -rf`, `sudo`, `dd`, `mkfs`, curl/wget-pipe-to-shell, force push, `git reset --hard`, edits to shell rc files, and reads of `~/.ssh`, `~/.aws`, `~/.kube`, `~/.npmrc`, gh/docker/git credentials, keychains, and crypto-wallet data |
| `hooks` | 9 inline `PreToolUse:Bash` guards + the two scripts in `hooks/` (see below) |
| `statusLine` | runs `~/.claude/statusline.sh` every 1 s |
| `enabledPlugins` | 11 plugins across 3 marketplaces |
| `extraKnownMarketplaces` | `anthropics/claude-plugins-official`, `obra/superpowers-marketplace`, `tigerless-labs/autoharness` |
| `outputStyle` | `Concise` |
| `alwaysThinkingEnabled` | `true` |
| `skipDangerousModePermissionPrompt` | `true` |

The inline Bash guards block, with exit code 2 and a message on stderr: recursive/forced `rm` (and
`find -delete` as a bypass), SQL `DROP`/`TRUNCATE`/`FLUSHALL` and Mongo `.drop()`, destructive
`docker volume rm`/`system prune`/`compose down -v`, recursive S3/GCS/Azure deletions, `pip` and
`poetry` (use `uv`), `black`/`pylint`/`flake8` (use `ruff`), `mypy`/`pyright` (use `ty`),
`eslint`/`prettier` (use `oxlint`/`oxfmt`), `git add` of `.env`/`.pem`/`.key`/credentials files, and
any commit carrying an Anthropic `Co-Authored-By` trailer.

### `hooks/`
Shell scripts invoked by the `hooks` block. Each receives the hook payload as JSON on stdin
(`.tool_name`, `.tool_input.*`) and signals a block with exit code 2.

- **`check-imports.sh`** — PreToolUse on Write/Edit. Rejects `from ..` in `.py` and `from '../'` in
  `.ts/.js/.tsx/.jsx`. Absolute imports only.
- **`post-write-lint.sh`** — PostToolUse on Write/Edit. Warns if a `.sh` file lacks
  `set -euo pipefail`, then runs `shellcheck`; runs `actionlint` on GitHub workflow YAML; runs
  `jq empty` on `.json`. Advisory only — writes to stderr, always exits 0.

### `statusline.sh`
Reads the statusline JSON payload on stdin and prints two lines: model / folder / branch, then a
context-usage progress bar, cost, and duration. Prefers Claude Code's precomputed
`context_window.remaining_percentage` (which accounts for the compaction reserve) and falls back to
summing `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` against
`context_window_size`. Also reports cache hit rate and lines added/removed.

### `skills/`
Personal skills, one directory per skill, each with a `SKILL.md` whose YAML frontmatter carries
`name`, `description` (the trigger text Claude matches against), and optionally `argument-hint`.
Available in every project and invocable as `/<name>`. Present here: `research-search`,
`search-vault`, `search-arxiv`, `search-github`, `search-web-exa`, `research-workup`,
`capture-source`, `vault-query`, `tidy-vault`, `weekly-review`, `monthly-review`,
`auditing-claude-projects`, `build-agent`, `opencode-delegate`, `firecrawl`,
`marker-document-extraction`.

### `.credentials.json` — secrets
OAuth material, not for reading or copying:

- `claudeAiOauth` — access token, refresh token, both expiries, granted scopes, subscription type,
  rate-limit tier.
- `mcpOAuth.<server-key>` — per-MCP-server tokens (here: the `exa` plugin server) with server name,
  URL, access token, expiry, discovery state, and client ID.

### `.gitignore`
Ignores `.credentials.json`, `claude.json`, `plugins/`, and `statsig/` — so this directory can be
version-controlled without leaking tokens or 18 MB of plugin clones.

---

## Application data Claude Code writes

### `projects/<project>/` — transcripts and memory
`<project>` is derived from the git repo root, or the working directory outside a repo. Here it's
`-workspace` (from `/workspace`, slashes → dashes).

**`<session-id>.jsonl`** is the full transcript: one JSON object per line, append-only. This is the
richest artifact in the whole directory. Record types observed:

| `type` | Contents |
| --- | --- |
| `user` | Your message, plus `cwd`, `gitBranch`, `permissionMode`, `promptSource`, `version`, `uuid`/`parentUuid` threading, `toolUseResult` for tool returns, `toolDenialKind` when you deny a call, `userFeedback`, `isMeta`, `isSidechain` (subagent turns) |
| `assistant` | The model's message, plus `requestId`, `effort` (reasoning effort), `attributionSkill` / `attributionPlugin` (which skill or plugin produced the turn) |
| `attachment` | Injected context: hook output (`hook_success` with the hook's stdout), file reads, system reminders |
| `system` | Hook lifecycle records — `subtype: stop_hook_summary`, `hookCount`, `hookInfos[]` with each command and its `durationMs`, `hookErrors`, `stopReason`, `preventedContinuation` |
| `file-history-snapshot` | Checkpoint marker: `messageId` + `trackedFileBackups` map |
| `file-history-delta` | One file backed up: `trackingPath`, `backupFileName`, `version`, `backupTime` |
| `mode` / `permission-mode` | Mode at that point (`normal`, `plan`, …) |
| `ai-title` | Model-generated session title |
| `agent-name` | Derived session/agent name |
| `last-prompt` / `leafUuid` | Pointers to the current conversation leaf, for resume |
| `atis-latch` | Internal latch state |

Sibling directories per session: **`<session-id>/custom-title.json`** (title from `/rename`),
**`<session-id>/tool-results/`** (tool outputs too large for context, spilled to
`toolu_*.txt` — WebFetch results and long command output land here), and
**`<session-id>/subagents/`** for subagent transcripts.

**`memory/`** is auto memory — Claude's own notes, one fact per markdown file with
`name`/`description`/`metadata.type` frontmatter (`user`, `feedback`, `project`, `reference`), plus
a `MEMORY.md` index. Only the first 200 lines / 25 KB of `MEMORY.md` load at session start; topic
files are read on demand. This directory is **excluded** from the retention sweep. Empty here.

### `sessions/<pid>.json`
One file per *live* session, keyed by PID. Contains `sessionId`, `cwd`, `startedAt`, `procStart`,
`version`, `kind` (`interactive`), `entrypoint` (`cli` or `sdk-cli`), `status`, `name` and
`nameSource`, `formerNames[]`, and `agent` when the session is a subagent (e.g.
`autoharness:reflector`). Used to detect concurrent sessions and crash leftovers; removed on exit,
not aged out by the sweep.

### `history.jsonl`
Every prompt you've typed, one per line: `display` (the raw text, slash commands included),
`pastedContents`, `timestamp`, `project`, `sessionId`. Drives up-arrow recall. Never swept — it
grows until you delete it or run `claude project purge`.

### `file-history/<session-id>/`
Pre-edit copies of files Claude modified, named `<hash>@v<n>`, the payload being the file's prior
contents verbatim. Backs `/rewind` checkpoint restore. Retains snapshots for the 100 most recent
checkpoints.

### `plans/*.md`
Plan documents written in plan mode, filename derived from the prompt
(`we-need-to-fix-mighty-noodle.md` here). Swept by age.

### `session-env/<session-id>/`, `tasks/session-<short>/`
Per-session environment metadata and task-tool task lists. Both directories exist per session and
are often empty.

### `teams/session-<short>/config.json`
Agent-team roster (from `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`): team `name`, `createdAt`,
`leadAgentId`, `leadSessionId`, and `members[]` with `agentId`, `agentType`, `cwd`, `tmuxPaneId`,
`subscriptions`, `backendType`.

### `backups/`
Rotating copies of `~/.claude.json`, named `.claude.json.backup.<epoch-ms>`, written whenever
Claude Code rewrites that file. Five newest kept, plus any version that failed to parse
(`.claude.json.corrupted.*`).

### `shell-snapshots/snapshot-zsh-<ts>-<rand>.sh`
Your shell's aliases, functions, and options captured at startup and re-sourced by the Bash tool
before each command, so `Bash` behaves like your interactive shell. Deleted on clean exit; the
sweep clears crash leftovers.

### `cache/changelog.md`
Cached Claude Code changelog (568 KB, current through 2.1.247), used to render release notes after
an update. Refreshed in the background.

### `security/`
Written by the **security-guidance** plugin, not by Claude Code itself. Contains
`security_warnings_state_<session-id>.json` (`shown_warnings`, `untracked_at_baseline`,
`touched_paths` — so a warning isn't repeated), a `.lock` file per session, `log.txt` (hook
invocation log), `.sdk_bootstrap_spawned`, and `agent-sdk-venv/` — a 298 MB Python virtualenv that
is the single largest thing in this directory.

### `autoharness/requests/`
Queue directory for the autoharness plugin's skill-staging requests. Empty.

### Small state files

| File | Contents |
| --- | --- |
| `.last-cleanup` | ISO timestamp of the last retention sweep |
| `.last-update-result.json` | `timestamp`, `path` (`native`), `outcome`, `version_from` → `version_to`, `error_code` — here 2.1.241 → 2.1.247 |
| `.tracker-origin.json` | Sandbox provenance: `container`, `image`, `hostWorkspace` (the real path on the host), `workspaceMount`, `host`, `updatedAt` — specific to this container setup, not a Claude Code file |

---

## `~/.claude.json` (one level up, not inside this directory)

Machine-scoped app state and UI preferences — **not** settings. Never hand-edit while a session is
running; it's rewritten frequently, with copies landing in `backups/`. Keys present:

- **Identity/auth**: `oauthAccount`, `userID`, `machineID`, `installMethod`, `firstStartTime`,
  `firstStartVersion`, `numStartups`
- **Per-project state** under `projects["/workspace"]`: `allowedTools`, `mcpServers`,
  `enabledMcpjsonServers` / `disabledMcpjsonServers`, `hasTrustDialogAccepted`,
  `hasClaudeMdExternalIncludesApproved`, `lastSessionId`, `lastCost`, `lastDuration`,
  `lastAPIDuration`, `lastLinesAdded` / `lastLinesRemoved`, `lastTotalInputTokens` /
  `lastTotalOutputTokens` / `lastTotalCacheReadInputTokens` / `lastTotalCacheCreationInputTokens`,
  `lastModelUsage`, `lastGracefulShutdown`
- **Caches**: `modelAccessCache`, `additionalModelCostsCache`, `orgModelDefaultCache`,
  `autoCompactWindowsCache`, `passesEligibilityCache`, `groveConfigCache`
- **UI/onboarding**: `hasCompletedOnboarding`, `tipsHistory`, `tipLifetimeShownCounts`,
  `seenNotifications`, `lastReleaseNotesSeen`, `lastPlanModeUse`, `skillUsage`, `pluginUsage`,
  `migrationVersion`

---

## Documented but absent here

These are the standard `~/.claude` entries this install simply doesn't have yet:

| Path | Purpose |
| --- | --- |
| `CLAUDE.md` | Personal instructions loaded into every session, every project |
| `rules/*.md` | User-level topic rules, optionally gated by `paths:` frontmatter; loaded before project rules |
| `agents/*.md` | Personal subagent definitions (own prompt, tools, model) |
| `commands/*.md` | Personal single-file `/name` prompts — the legacy form of skills, still supported |
| `workflows/*.js` | Saved dynamic workflow scripts; each becomes a `/<name>` command |
| `agent-memory/<name>/` | Persistent memory for subagents declaring `memory: user` |
| `output-styles/*.md` | Custom system-prompt sections (`Concise` here is built in) |
| `keybindings.json` | Custom keyboard shortcuts |
| `themes/*.json` | Custom color themes |
| `stats-cache.json` | Aggregated token/cost counts behind `/usage` |
| `remote-settings.json`, `policy-limits.json` | Cached org-managed settings and feature policy; deleted on logout |
| `debug/`, `paste-cache/`, `image-cache/`, `uploads/`, `usage-data/`, `feedback/drafts/`, `feedback-bundles/` | Per-feature runtime data, all age-swept |
| `todos/`, `statsig/`, `logs/` | Legacy directories; no longer written, removed by the sweep |

Managed settings (`/etc/claude-code/managed-settings.json` on Linux) and a managed
`CLAUDE.md` (`/etc/claude-code/CLAUDE.md`) live outside the home directory and outrank everything
here. Note that this install's `permissions.deny` blocks reads of `/opt/claude-config/**` and
`/opt/claude-seed/**`.

---

## Retention

`cleanupPeriodDays` is **365** here (default 30). Files older than that under `projects/`,
`file-history/`, `plans/`, `session-env/`, `tasks/`, `shell-snapshots/`, `backups/`, `debug/`,
`paste-cache/`, `image-cache/`, and `uploads/` are deleted on the sweep, whose last run is stamped
in `.last-cleanup`. Exempt: `sessions/` (cleared on session exit instead),
`projects/<project>/memory/` (auto memory), and `history.jsonl` (never swept).

`claude project purge <path>` clears one project's transcripts, auto memory, per-session
`tasks/`/`debug/`/`file-history/`, its matching `history.jsonl` lines, and its `~/.claude.json`
entry. It leaves `shell-snapshots/` and `backups/` alone, since neither is project-scoped.

Do not delete `~/.claude.json`, `settings.json`, or `plugins/` — auth, preferences, and installed
plugins live there.

---

## Sources

Verified by direct inspection of this directory, plus the Claude Code docs:
[.claude directory](https://code.claude.com/docs/en/claude-directory),
[settings](https://code.claude.com/docs/en/settings),
[memory](https://code.claude.com/docs/en/memory),
[skills](https://code.claude.com/docs/en/skills).
