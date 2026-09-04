# Agent Orchestrator: Design Plan

Status: v3, 2026-09-03 (v2 added per-role agent runtimes; v3 added network-share copies
and pass-through of existing MCP servers). Implemented in this repository; see README.md. Produced from a design interview; decisions recorded in
section 2 are settled unless marked as open.

## 1. Purpose

A Python 3.13/3.14 command-line tool that takes Jira issues (or epics), hands each one to
a Claude Code worker agent running in an isolated git worktree, validates the result with
a local build and a targeted test subset, has a second agent review the change, feeds the
review back to the worker, and finally opens a GitHub pull request. When the work cannot
be completed the system produces a findings report instead of a PR and posts it to Jira
and Confluence.

The orchestrator is deliberately thin. Judgment lives in the agents; the orchestrator owns
sequencing, isolation, credentials, validation, and the paper trail.

## 2. Decisions from the interview

| Topic | Decision | Consequence for the design |
|---|---|---|
| Agent runtime | Per role, chosen in YAML. v1 ships Claude Code for the worker and Codex for the reviewer; Gemini CLI, OpenCode, and Hermes are further adapters behind the same interface | Every runtime is a subprocess adapter implementing `AgentRunner` and declaring its capabilities. The pipeline adapts to missing capabilities (section 4.4). A cross-vendor reviewer avoids the worker and reviewer sharing blind spots. |
| Target repos | One GitHub repo per YAML config, any language | Build and test commands must be declared in YAML; nothing language-specific is hard-coded. |
| Work intake | CLI: issue or epic keys passed explicitly | No daemon, webhook, or polling in v1. Intake is an interface so JQL polling can be added later. |
| Deployment | Developer workstation, foreground CLI | Single user. Credentials come from the developer's own environment. |
| Build/test knowledge | Declared in YAML per repo | Predictable and auditable; the worker never invents build commands. |
| Review loop | 2 review rounds by default, configurable | Bounded cost. Unresolved blocking findings after the cap produce a findings report, not a PR. |
| Jira write-back | Comment with status and PR link, transition status, attach findings when blocked | Needs a Jira token with write scope for the project. Transition names are configurable. |
| Human gates | None; the PR is the gate | Everything up to and including PR creation is autonomous. A `--pause-before` flag exists for debugging but is off by default. |
| Secrets | Environment variables and `.netrc`, never in YAML | Config names *where* a secret lives. Loader rejects literal secrets. |
| Worker sandbox | Claude Code with permissions bypassed, relying on worktree isolation | Compensating controls are mandatory: scrubbed environment, no tokens in the agent process, hooks that block `git push` and `gh`, hard turn and time limits. See section 7. |
| Concurrency | Parallel from day one, configurable N | asyncio scheduler, one worktree and one agent session per issue, a global build semaphore. |
| Confluence | Publish findings and run reports to a configured space | Confluence links listed in config are also fetched read-only as agent context (this was in the original brief and is kept). |
| Network shares | The worker may copy files from the support share to the raid share as part of the Jira work | Shares are declared in YAML with per-platform paths (`/Volumes/support` on macOS, `/support` on Linux). Source is read-only, destination is read-write under configured roots. Grants flow into each adapter's sandbox flags and a copy helper logs every transfer (section 4.5). |
| MCP servers | Agents get the MCP servers already configured for each CLI, filtered by an allowlist per role | The orchestrator extracts named server definitions from the existing CLI configs and writes a per-run MCP config, so isolation flags stay on while the agents keep Jenkins, RAGFlow, and similar tools (section 4.6). |

## 3. End-to-end flow

```
orchestrator run PROJ-123 PROJ-140 EPIC-7
        │
        ▼
 ┌─ Intake ─────────────────────────────────────────────────┐
 │ resolve keys → expand epics → build TaskSpec per issue   │
 └──────────────────────────────────────────────────────────┘
        │  (N in parallel, bounded by max_parallel)
        ▼
 ┌─ Per-issue pipeline ─────────────────────────────────────┐
 │ 1 CONTEXT   fetch issue, comments, attachments, links,   │
 │             configured Confluence pages                  │
 │ 2 WORKTREE  fetch base branch, create worktree + branch  │
 │ 3 WORK      worker agent implements or declares blocked  │
 │ 4 BUILD     run declared build (build semaphore)         │
 │ 5 TEST      run selected test subset                     │
 │ 6 COMMIT    orchestrator commits on the branch           │
 │ 7 REVIEW    reviewer agent produces structured verdict   │
 │ 8 FIX       worker addresses blocking findings (≤ rounds)│
 │      └─ back to 4 BUILD                                  │
 │ 9 PUSH      orchestrator pushes branch                   │
 │10 PR        orchestrator opens PR with generated body    │
 │11 REPORT    Jira comment + transition, Confluence page   │
 └──────────────────────────────────────────────────────────┘
        │
        ▼
   BLOCKED path (from 3, 4, 5, or 8): write findings.md,
   attach to Jira, transition to blocked status, publish to
   Confluence, keep the worktree for inspection.
```

Per-issue state machine:

```
QUEUED → CONTEXT → WORKTREE → WORKING → BUILDING → TESTING → COMMITTING
   → REVIEWING → (FIXING → BUILDING …) → PUSHING → OPENING_PR → REPORTING → DONE

Any state → BLOCKED   (agent declared it cannot proceed, or validation failed after retries)
Any state → FAILED    (infrastructure error: auth, network, git, tool crash)
```

`BLOCKED` is a legitimate outcome with a findings report. `FAILED` is an orchestrator
problem and is retried or surfaced as an error; it never produces a findings report that
blames the work item.

## 4. Architecture

### 4.1 Components

```
orchestrator/
  cli.py               Typer app: init, doctor, run, status, resume, clean
  config/
    schema.py          Pydantic models for the YAML; secret-reference types
    loader.py          YAML → validated Config; env/netrc resolution
  intake/
    base.py            IntakeSource protocol (yields TaskSpec)
    cli_keys.py        Explicit keys, epic expansion
  trackers/
    base.py            Tracker protocol: get_issue, comment, transition, attach
    jira.py            Jira Cloud REST v3 via httpx
  docs/
    confluence.py      fetch pages as Markdown; publish pages
  scm/
    worktree.py        git worktree lifecycle, branch naming, commit, push
    github.py          PR creation via `gh` CLI (PyGithub optional later)
  build/
    runner.py          run declared commands with timeout, log capture, env scrub
    selection.py       TestSelector strategies
  agents/
    base.py            AgentRunner protocol, Capabilities, AgentResult
    registry.py        name → adapter; discovers third-party adapters via entry points
    runners/
      claude_code.py   `claude -p`           (v1, default worker)
      codex.py         `codex exec`          (v1, default reviewer)
      gemini_cli.py    `gemini -p`           (adapter, M6)
      opencode.py      `opencode run`        (adapter, M6)
      hermes.py        `hermes -z`           (adapter, M6)
    conformance/       shared test kit every adapter must pass
    prompts/           Jinja2 templates: worker.md, reviewer.md, fixer.md
    contracts.py       Pydantic models for worker/reviewer structured output
  pipeline/
    task.py            TaskSpec, TaskState, transitions
    stages.py          one function per stage
    scheduler.py       asyncio scheduler, semaphores, retries
  reporting/
    findings.py        findings.md generation
    run_report.py      per-run summary (Markdown + JSON)
    audit.py           append-only JSONL audit log of every external action
  state/
    store.py           SQLite store for resumability
```

### 4.2 Key interfaces

```python
class AgentRunner(Protocol):
    name: str                       # "claude-code", "codex", ...
    capabilities: Capabilities      # what the pipeline can rely on
    def preflight(self) -> list[Problem]        # binary, version, auth; used by `doctor`
    async def run(self, *, cwd: Path, prompt: str, role: Role,
                  schema: dict, session: str | None,
                  limits: Limits, access: Access) -> AgentResult: ...

@dataclass(frozen=True)
class Capabilities:
    structured_output: bool     # native JSON-schema enforcement
    session_resume: bool        # can continue a prior session
    turn_cap: bool              # honors a max-turns limit natively
    budget_cap: bool            # honors a max-spend limit natively
    usage_report: bool          # reports cost/tokens after the run
    read_only_mode: bool        # can be forced to not write files
    command_deny_hooks: bool    # can block specific shell commands
    config_dir_isolation: bool  # per-run home/config dir via env var

@dataclass(frozen=True)
class PathGrant:
    name: str                   # "support", "raid"
    path: Path                  # resolved for the current platform
    mode: Literal["read", "read-write"]

@dataclass(frozen=True)
class Access:
    worktree: Literal["read-only", "workspace-write"]
    grants: tuple[PathGrant, ...]   # paths outside the worktree the role may touch

class Tracker(Protocol):
    async def get_issue(self, key: str) -> Issue
    async def children(self, epic_key: str) -> list[Issue]
    async def comment(self, key: str, body: str) -> None
    async def transition(self, key: str, to_status: str) -> None
    async def attach(self, key: str, path: Path) -> None

class ScmHost(Protocol):
    async def open_pr(self, repo: str, head: str, base: str,
                      title: str, body: str, draft: bool) -> PullRequest

class TestSelector(Protocol):
    def select(self, spec: TaskSpec, changed_paths: list[Path]) -> list[str]
```

`Tracker` and `ScmHost` have one implementation each in v1 (Jira, GitHub). `AgentRunner`
has two in v1 (Claude Code, Codex) and three more planned. The protocols exist so that
another implementation is a new module, not a refactor.

### 4.3 Agent output contracts

Agents return JSON that the orchestrator validates with Pydantic. The schema is handed to
the adapter; adapters with native structured output (Claude Code `--json-schema`, Codex
`--output-schema`) let the CLI enforce it. Adapters without it receive the schema in the
prompt, and the orchestrator extracts the last fenced JSON block and validates it,
re-prompting once on failure. A result that still fails validation marks the task
`FAILED`. The contracts below are runtime-independent.

Worker result:

```json
{
  "status": "completed | blocked",
  "summary": "one paragraph for the PR body",
  "changed_paths": ["src/foo.py", "tests/test_foo.py"],
  "tests_selected": ["tests/test_foo.py::test_bar"],
  "test_rationale": "why this subset covers the change",
  "copied_files": [{"from": "support:cases/SF12345/input.pdf",
                    "to": "raid:DevTests/assets/SF12345/input.pdf"}],
  "blocked": {
    "reason": "category: ambiguous-requirements | missing-access | out-of-scope | technical",
    "details_markdown": "full explanation, becomes findings.md",
    "questions_for_reporter": ["..."]
  }
}
```

Reviewer result:

```json
{
  "verdict": "approve | request_changes",
  "findings": [
    {"severity": "blocking | major | minor | nit",
     "path": "src/foo.py", "line": 42,
     "title": "...", "detail": "...", "suggested_fix": "..."}
  ],
  "summary_markdown": "posted into the PR body"
}
```

Only `blocking` and `major` findings trigger a fix round. `minor` and `nit` findings are
carried into the PR body for the human reviewer.

### 4.4 Agent runtime adapters

Each supported CLI is wrapped by an adapter that turns the orchestrator's request (prompt,
schema, limits, access level, cwd) into a command line and turns the CLI's output back
into an `AgentResult` (structured output, session id, cost, turns, termination reason).
Adapters are selected per role in YAML, so the worker and reviewer can be different
vendors. Third-party adapters register through the `orchestrator.runners` entry-point
group, which is how "some other" runtime gets added without touching this codebase.

The pipeline reads each adapter's `Capabilities` and fills gaps itself:

| Missing capability | Pipeline behavior |
|---|---|
| `structured_output` | Schema embedded in the prompt; orchestrator parses the last fenced JSON block, validates, re-prompts once. |
| `session_resume` | Fix round starts a fresh session whose prompt includes the worker's previous summary, the current diff, and the reviewer's findings. |
| `turn_cap` / `budget_cap` | Orchestrator relies on its wall-clock timeout and, where `usage_report` exists, marks `FAILED` when reported spend exceeds the configured budget after the run. `doctor` warns that the cap is soft. |
| `read_only_mode` | Reviewer runs in a throwaway `git worktree` copy so any writes are discarded. |
| `command_deny_hooks` | Rely on the adapter's sandbox mode plus the fetch-only remote and scrubbed environment (section 7.2). |
| `config_dir_isolation` | Adapter runs with `HOME` pointed at the run directory; `doctor` warns that the developer's global config for that CLI is not loaded. |

Role requirements differ. The worker needs `workspace-write` access and the ability to
run the declared build and test commands. The reviewer needs only `read-only` access to
the worktree and diff, which is why a stricter sandbox is used for it regardless of
adapter.

Capability matrix for the installed versions on 2026-09-03 (Claude Code 2.1.259, Codex
0.153.0, Gemini CLI 0.46.0, OpenCode 1.18.20, Hermes 0.21.0). See Appendix B for the
flags behind each cell.

| Capability | Claude Code | Codex | Gemini CLI | OpenCode | Hermes |
|---|---|---|---|---|---|
| Headless prompt, stdin | yes | yes | yes | yes (argv) | yes (argv) |
| Structured output (native schema) | yes | yes | no (JSON envelope only) | no (JSON events only) | no (text only) |
| Session resume | yes | yes | yes | yes | yes |
| Turn cap | yes (`--max-turns`) | no | via `model.maxSessionTurns` setting | no | via `agent.max_turns` / `run_budget_seconds` config (docs) |
| Budget cap | yes | no | no | no | no |
| Usage report | yes | via JSONL events | in JSON envelope | via events / `stats` | yes (`--usage-file`) |
| Read-only mode | via allow/deny tools | yes (`--sandbox read-only`) | yes (`--approval-mode plan`) | via `permission.edit` deny | no (docker terminal backend is its isolation) |
| Command deny hooks | yes (`PreToolUse`) | `PreToolUse` in `hooks.json`, execpolicy `.rules` | `BeforeTool` hook, policy engine | `permission.bash` deny patterns | `pre_tool_call` hook with `fail_closed` |
| Config dir isolation | `CLAUDE_CONFIG_DIR` | `CODEX_HOME` | `GEMINI_CLI_HOME` | `OPENCODE_CONFIG_DIR` | `HERMES_HOME` |
| Ignore user config | `--bare` | `--ignore-user-config` | `--policy`, `-e` | `--pure` | `--ignore-user-config` / `--safe-mode` |

Environment variable names were confirmed against the installed binaries. Remaining
unconfirmed details are listed in Appendix B and verified when each adapter is built (M6).

### 4.5 Network shares and the copy helper

Some Jira work needs files moved from the support share to the raid share, for example
customer assets that must reach a test-data location without entering the repository.
The orchestrator models this as *path grants* rather than as a special stage, so the
worker can do the copy at the point in its work where it makes sense and the rest of the
pipeline is unchanged.

- **Declaration.** `shares:` in YAML names each share and gives its mount path per
  platform. The orchestrator resolves the path for the machine it is running on (`darwin`
  or `linux`) and exposes both the name and the resolved path to prompts, so the same
  config file works on a Mac workstation and a Linux box.
- **Grants per role.** Each role lists the shares it may use and in which mode. The
  default gives the worker `support: read` and `raid: read-write`; the reviewer gets both
  as `read` so it can verify that a copy happened without being able to alter anything.
- **Write roots.** A read-write grant may be narrowed to listed subdirectories
  (`write_under`). Writes elsewhere on the share are denied by the adapter's sandbox or
  hooks, and by the helper.
- **Copy helper.** The orchestrator places a small `orchestrator-cp` command on the agent's
  `PATH`. It accepts `<share>:<relative path>` arguments, resolves them against the grants,
  refuses symlink escapes and paths outside a write root, preserves file metadata, and
  appends source, destination, size, and hash of every file to the audit log. The worker
  prompt says to use it for share-to-share copies. Raw `cp` and `rsync` are not blocked
  where the adapter cannot distinguish them, but the helper is what the audit trail is
  built on.
- **Mapping to adapters.** Grants become adapter flags: Claude Code `--add-dir <path>` plus
  hook rules that deny Edit/Write/Bash writes to read-only grants; Codex `--add-dir <path>`
  for read-write grants (its `workspace-write` sandbox already permits reading the whole
  disk); Gemini CLI `--include-directories`; OpenCode `permission.external_directory`;
  Hermes has no path sandbox, so only the helper and hooks apply.
- **The mount is the real boundary.** Hooks and sandboxes are best effort under bypassed
  permissions. `doctor` checks that both shares are mounted, that the source is readable
  and the destination write roots are writable, and warns when the source mount is not
  read-only. Mounting the support share read-only for the orchestrator's account is the
  recommended setup.

### 4.6 MCP servers

The CLIs on this machine already have MCP servers configured (Jenkins, RAGFlow, Kapa
docs, and the Atlassian connector in Claude). The isolation flags in section 7.2
(`--bare`, `--strict-mcp-config`, a per-run `CODEX_HOME`) would drop them, so the
orchestrator carries them across deliberately:

- **Sources.** `mcp.sources` lists where each CLI keeps its MCP definitions (defaults:
  `~/.claude.json` and the repo's `.mcp.json` for Claude Code, `$CODEX_HOME/config.toml`
  for Codex, `settings.json` for Gemini CLI, `opencode.json` for OpenCode, `config.yaml`
  for Hermes). Definitions are read, not edited.
- **Allowlist per role.** `agents.<role>.mcp_servers` names the servers that role may use.
  The worker typically gets Jenkins and RAGFlow; the reviewer gets RAGFlow only. A name
  that is not defined in the source for that role's runner is a `doctor` error.
- **Per-run config.** For each run the orchestrator writes a config containing only the
  allowed servers in the adapter's native format and passes it explicitly: Claude Code
  `--mcp-config <file> --strict-mcp-config`; Codex a generated `config.toml` inside the
  per-run `CODEX_HOME` (so `--ignore-user-config` is no longer passed for Codex);
  Gemini CLI `--allowed-mcp-server-names`; OpenCode and Hermes a generated config in the
  per-run config directory.
- **Tool-level denial.** `mcp.deny_tools` lists tools that stay off even when their server
  is allowed, for example `mcp-jenkins` build triggering. Claude Code takes these as
  `--disallowedTools mcp__<server>__<tool>`; Codex takes per-tool `[mcp_servers.<name>.tools.<tool>]`
  entries; other adapters fall back to prompt instructions and `doctor` says so.
- **Secrets.** MCP definitions often carry tokens in headers or env. Those reach the agent
  process for the servers it is allowed, which is the same exposure the developer already
  accepts interactively. The allowlist per role limits it; secrets are redacted from the
  saved per-run config copies in the run directory.

## 5. Configuration

One YAML file per target repository. Secrets are referenced, never stored.

```yaml
version: 1

tracker:
  kind: jira
  base_url: https://datalogics.atlassian.net
  project: PDFL
  auth:
    netrc_machine: datalogics.atlassian.net   # or: token_env: JIRA_API_TOKEN
  statuses:
    in_progress: "In Progress"
    in_review: "In Review"
    blocked: "Blocked"
  comment_on: [started, pr_opened, blocked, failed]

confluence:
  base_url: https://datalogics.atlassian.net/wiki
  auth:
    netrc_machine: datalogics.atlassian.net
  context_pages:                      # read-only, given to worker and reviewer
    - https://datalogics.atlassian.net/wiki/spaces/ENG/pages/123/Coding+Standards
    - https://datalogics.atlassian.net/wiki/spaces/PDFL/pages/456/Architecture
  publish:
    space: ENG
    parent_page_id: 789
    when: [blocked, completed]        # which outcomes get a page (`on` is a YAML boolean)

repo:
  github: datalogics/apdfl
  base_branch: develop
  clone_path: ~/development/apdfl     # existing clone; worktrees are created beside it
  worktree_root: ~/development/.orchestrator/worktrees
  branch_template: "agent/{key}-{slug}"
  pr:
    draft: true
    labels: [agent-generated]
    reviewers: []
    title_template: "{key}: {summary}"
  auth:
    token_env: GITHUB_TOKEN           # used only by the orchestrator, never by agents

shares:
  support:
    paths: {darwin: /Volumes/support, linux: /support}
    expect_read_only_mount: true      # doctor warns if the mount is writable
  raid:
    paths: {darwin: /Volumes/raid, linux: /raid}
    write_under:                      # optional; omit to allow the whole share
      - DevTests/assets
      - agent-drops

mcp:
  sources:                            # where each CLI already keeps MCP definitions
    claude-code: [~/.claude.json, .mcp.json]
    codex: [~/.codex/config.toml]
  deny_tools:
    - mcp-jenkins: [triggerBuild, rebuildBuild, replayBuild, updateBuild]

build:
  setup: ["python mkenv.py"]          # runs once per worktree
  commands: ["make -j8 debug"]
  timeout_minutes: 40
  env:
    CC: clang
  max_concurrent_builds: 1            # global semaphore across all tasks

test:
  timeout_minutes: 20
  selection:
    strategy: changed-paths           # changed-paths | named-suite | agent-chosen
    map:                              # path prefix → test command(s)
      "src/pdf/":   ["pytest tests/pdf -q"]
      "src/core/":  ["pytest tests/core -q"]
    fallback: ["pytest tests/smoke -q"]
    max_commands: 3
  full_suite: ["pytest -q"]           # never run by the pipeline; documented in the PR

agents:
  review_rounds: 2
  prompt_overrides:                   # optional, per repo
    worker: .orchestrator/prompts/worker.md

  worker:
    runner: claude-code               # any name in the adapter registry
    model: claude-fable-5-1
    access: workspace-write
    timeout_minutes: 90
    max_turns: 200                    # ignored with a doctor warning if unsupported
    max_budget_usd: 15
    auth:
      token_env: ANTHROPIC_API_KEY    # the only secret passed into the agent process
    shares:
      support: read
      raid: read-write
    mcp_servers: [mcp-jenkins, ragflow]
    options:                          # adapter-specific, validated by the adapter
      effort: high

  reviewer:
    runner: codex
    model: gpt-5.4
    access: read-only
    timeout_minutes: 30
    max_budget_usd: 5                 # soft: enforced from the usage report after the run
    auth:
      token_env: OPENAI_API_KEY
    shares:
      support: read
      raid: read
    mcp_servers: [ragflow]
    options:
      reasoning_effort: high

  # Alternative reviewer blocks, swap in by editing `reviewer:`
  # reviewer: {runner: gemini-cli, model: gemini-3-pro, access: read-only, auth: {token_env: GEMINI_API_KEY}}
  # reviewer: {runner: opencode,   model: anthropic/claude-opus-5, access: read-only, auth: {token_env: ANTHROPIC_API_KEY}}
  # reviewer: {runner: hermes,     model: anthropic/claude-opus-5, access: read-only, auth: {token_env: ANTHROPIC_API_KEY}}
  # reviewer: {runner: claude-code, model: claude-opus-5, access: read-only, auth: {token_env: ANTHROPIC_API_KEY}}

scheduler:
  max_parallel: 3
  retry_infra_failures: 2

hooks:                                # optional shell hooks, run by the orchestrator
  after_worktree: [".orchestrator/hooks/install-deps.sh"]
  before_pr: []
```

Validation rules enforced by the loader:

- Any string field under an `auth:` block other than `netrc_machine` or `*_env` is an error.
- Any value anywhere that matches common token shapes (`ghp_`, `ATATT`, `sk-ant-`) is an error.
- Paths are expanded and checked at `doctor` time; commands are stored as lists, never shell strings.
- Unknown keys are errors (Pydantic `extra="forbid"`) so typos surface immediately.
- `agents.<role>.runner` must name a registered adapter, and `options:` is validated by
  that adapter's own Pydantic model, so a Codex option under a Claude Code runner is an
  error rather than silently ignored.
- Limits the chosen adapter cannot enforce natively are accepted but reported by `doctor`
  as soft.
- Every share referenced under `agents.<role>.shares` must be declared under `shares:`,
  must have a path for the current platform, and a `read-write` grant is only valid where
  the role's worktree access is also `workspace-write`.
- Every name under `agents.<role>.mcp_servers` must exist in one of that runner's
  `mcp.sources`.

## 6. Stage details

### 6.1 Context

- Fetch the issue: summary, description (rendered to Markdown), acceptance criteria custom
  field if configured, comments, linked issues, attachments under a size cap.
- For epics: enumerate children with the configured child JQL; each child becomes its own
  task. Children with an `is blocked by` link to another child are scheduled after it.
- Fetch configured Confluence pages and convert storage format to Markdown. Cache by page
  version in `.orchestrator/cache/`.
- Everything fetched is written into the worktree at `.orchestrator/context/` so the agent
  reads files rather than receiving a giant prompt. That directory is gitignored via
  `.git/info/exclude` in the worktree.

### 6.2 Worktree

- `git fetch origin <base>` under a per-repo lock, then
  `git worktree add <root>/<key> -b <branch> origin/<base>`.
- Run `build.setup` and `hooks.after_worktree`.
- Worktrees are kept after `BLOCKED` or `FAILED` for inspection and removed after `DONE`
  unless `--keep-worktrees` is passed. `orchestrator clean` prunes by age.

### 6.3 Work

- Render `worker.md` with: task metadata, paths to context files, the declared build and
  test commands (so the agent can run them itself while iterating), the output contract,
  the share grants with their resolved paths and the `orchestrator-cp` usage, and explicit
  rules (section 7.3).
- Share copies happen here, driven by the issue content (for example a support case
  folder named in the Jira description). The worker records what it copied in its result
  (`copied_files`), and the audit log has the helper's per-file entries.
- Spawn the configured worker adapter with the worktree as cwd. Capture the session id so
  review feedback can resume the same session in the fix stage (or, for adapters without
  resume, so the fix prompt can reference the earlier run).
- Parse and validate the result. `blocked` short-circuits to the BLOCKED path.

### 6.4 Build and test

- Build commands run under the global build semaphore with the configured timeout, in a
  scrubbed environment (section 7.2) plus the declared `build.env`.
- Test selection per strategy:
  - `changed-paths`: match `git diff --name-only origin/<base>` against the map; dedupe;
    cap at `max_commands`; use `fallback` if nothing matches.
  - `named-suite`: always run the listed commands.
  - `agent-chosen`: the worker's `tests_selected` is validated against an allowlist of
    permitted command prefixes, then run.
- A build or test failure is fed back to the worker as a fix round (counts against
  `review_rounds`). Repeated failure produces BLOCKED with logs attached.

### 6.5 Commit

- The orchestrator, not the agent, runs `git add -A` (respecting `.gitignore` and an
  orchestrator denylist for things like `.env`, `*.pem`, files over a size cap) and commits
  with a conventional message: `PROJ-123: <summary>` plus a trailer identifying the run id
  and the agent model.
- Agent-created commits, if any, are squashed into this one so history is uniform.

### 6.6 Review

- A fresh session on the configured reviewer adapter, with no shared context with the
  worker, receives the diff against base, the issue context, the Confluence standards
  pages, and the test log. The reviewer runs with `read-only` access; with Codex that is
  `--sandbox read-only`, so it can inspect the tree and run read-only commands but cannot
  modify it.
- Using a different vendor for review is the default configuration precisely so the two
  agents do not share training-correlated blind spots. Same-vendor review remains a valid
  configuration.
- The reviewer is told what tests ran and that the full suite did not; it may flag
  additional tests as a `major` finding. It also receives the `copied_files` list and has
  read access to both shares, so it can confirm the copies landed where the issue asked.
- Verdict handling: `approve` → PUSH. `request_changes` with blocking or major findings →
  FIX if rounds remain, else BLOCKED with the findings embedded in the report.

### 6.7 Fix

- Resume the worker session with the reviewer's findings. Loop back to BUILD.

### 6.8 Push and PR

- Push with the orchestrator's token; the agent never has one.
- PR body template: issue link, worker summary, tests run and rationale, reviewer summary
  with minor/nit findings, run id, and a checklist item reminding the human that the full
  suite has not been run.
- Draft by default (configurable).

### 6.9 Report

- Jira: comment with outcome and PR link; transition to `in_review` or `blocked`; attach
  `findings.md` and the test log when blocked.
- Confluence: publish a page under the configured parent titled `PROJ-123 agent run
  <date>` with the findings or the run summary.
- Local: `.orchestrator/runs/<run-id>/<key>/` holds prompts, raw agent output, logs, diff,
  findings, and the audit JSONL.

## 7. Security

Permissions are bypassed inside the agent by decision, so isolation must come from the
process boundary rather than from Claude Code's permission prompts.

### 7.1 Credential handling

- Secrets are resolved at startup from env vars or `.netrc` into memory and are never
  written to disk, logs, or prompts. Log formatter redacts any resolved secret value.
- Only the orchestrator process talks to Jira, Confluence, and GitHub APIs. Agents get
  files, not tokens.
- The GitHub token needs `repo` scope only; recommend a fine-grained token limited to
  the target repository. The Jira token should belong to a dedicated automation account
  so agent comments are visibly attributed.

### 7.2 Agent process environment

- Agents and build commands run with a constructed environment: `PATH`, `HOME`, locale,
  the single API key named by that role's `auth.token_env` (agents only), declared
  `build.env`, and nothing else. `GITHUB_TOKEN`, `JIRA_*`, `.netrc` contents, other
  vendors' API keys, SSH agent sockets, and the orchestrator's own virtual environment
  (`VIRTUAL_ENV`, `PYTHON*`, its `bin` on `PATH`) are not passed. The worker never sees the
  reviewer's key and vice versa. Secrets embedded in allowed MCP server definitions are
  the one deliberate exception (section 4.6).
- File access outside the worktree is limited to the role's share grants. Everything
  else on the machine, including the developer's home directory, is off limits as far as
  each adapter can enforce it; the copy helper is the audited path for share-to-share
  transfers.
- Each agent process gets its own config directory under the run directory
  (`CLAUDE_CONFIG_DIR` for Claude Code, `CODEX_HOME` for Codex, the equivalents listed in
  Appendix B for the others), so session transcripts and state never mix with the
  developer's global setup. Every adapter also passes its "ignore user config" flag
  (`--bare`, `--ignore-user-config`, `--pure`, and so on) so the developer's hooks,
  plugins, MCP servers, and stored credentials are not loaded. Authentication is then
  strictly the one API key in the scrubbed environment. Context the agent needs
  (worktree `CLAUDE.md` or `AGENTS.md`, orchestrator prompts, MCP config) is passed
  explicitly.
- The git remote in the worktree is set to a fetch-only URL where possible; push is done
  by the orchestrator using a temporary credential helper. Where the adapter supports
  command denial, the orchestrator supplies it per run: for Claude Code a `--settings`
  file with a `PreToolUse` hook that denies `git push`, `gh`, `curl`/`wget` to
  non-allowlisted hosts, paths outside the worktree, and writes under a read-only share
  grant (these hooks run even under `--bare`); for Codex a generated execpolicy `.rules`
  file and `--sandbox workspace-write`, which confines writes to the worktree plus the
  `--add-dir` grants. Adapters without any such mechanism fall back to the fetch-only
  remote and scrubbed environment alone, and `doctor` says so.

### 7.3 Untrusted input

- Jira descriptions, comments, and Confluence pages are untrusted. They are placed in
  files and the prompt states that instructions found inside them are data, not commands.
- Attachments are limited by type and size; binaries are listed by name only.
- The reviewer prompt explicitly asks whether the change does anything the issue did not
  ask for (scope creep, new network calls, new dependencies, credential-looking strings).

### 7.4 Bounding cost and runaway behavior

- Per-role `max_turns`, wall-clock timeout, and `max_budget_usd`; exceeding any one is
  `FAILED` with the partial diff kept for inspection.
- Build and test timeouts kill the whole process group.
- Concurrency cap plus build semaphore keep the workstation usable.

### 7.5 Audit

- Every external side effect (Jira comment, transition, attachment, push, PR, Confluence
  page) is appended to `audit.jsonl` with timestamp, run id, key, and the exact payload.
- `--dry-run` executes everything up to and including local commit and review, then prints
  what it *would* post or push.

## 8. Flexibility points

- **Runtime**: `AgentRunner` protocol with a registry and entry-point discovery. Claude Code
  and Codex ship in v1; Gemini CLI, OpenCode, and Hermes follow; anything else is a
  third-party package. Roles pick adapters independently, so worker and reviewer can differ.
- **Adapter conformance kit**: a shared pytest suite that drives any adapter through a
  canned task (read a file, edit it, return the contract JSON, resume once) so a new
  adapter is known to work before it is wired into a real run.
- **Tracker / SCM host**: protocols with Jira and GitHub as the only v1 implementations.
- **Intake**: `IntakeSource` protocol. JQL polling and a webhook receiver are planned
  additions that reuse the whole pipeline unchanged.
- **Prompts**: Jinja2 templates with per-repo overrides and a `--show-prompt` flag to render
  without running.
- **Test selection**: three strategies behind one protocol; new ones register by name.
- **Hooks**: shell hooks at defined lifecycle points for repo-specific setup.
- **Multiple repos**: `orchestrator run --config a.yaml --config b.yaml` accepts several
  configs; routing an issue to a config is by Jira component or label in a future version.

## 9. Ease of use

- `orchestrator init` writes a commented YAML from prompts and detects an existing clone.
- `orchestrator doctor` checks Python version, `git`, `gh`, the binary, minimum version,
  and auth of every configured adapter, tracker and Confluence auth, GitHub token scope,
  base branch reachability, worktree root writability, and that build commands exist on
  `PATH`. It confirms it is running from the orchestrator's own mkenv venv on Python 3.13
  or later (section 10.1). It also prints each role's runner with the limits it enforces
  natively versus the ones the orchestrator enforces softly. For shares it checks that each declared
  mount exists on this platform, reads a probe from the source, writes and removes a probe
  under each destination write root, and warns if a read-only share is mounted writable.
  For MCP it parses every source file and confirms each allowlisted server is defined.
- `orchestrator run KEY...` with a live Rich status table: one row per issue, current
  stage, elapsed time, cost so far.
- `orchestrator status` and `orchestrator resume <run-id>` read the SQLite store;
  interrupted runs continue from the last completed stage using the stored session ids.
- `orchestrator clean --older-than 7d` removes worktrees and run directories.
- Every run ends with a short Markdown run report printed to the terminal and saved.

## 10. Implementation plan

Packaging: `pyproject.toml` with `requires-python = ">=3.13"` and a console script
`orchestrator`. Dependencies are listed in `requirements.in` and compiled by mkenv, so the
repo follows the same convention as the rest of the development directory.

### 10.1 Environments

The orchestrator has its own Python virtual environment, created by mkenv. It is separate
from any environment belonging to a target project, and the two never mix.

- **Orchestrator venv.** `python mkenv.py` in the repo root clones the mkenv implementation
  into `.mkenv/`, creates `python-env-<hostname>/`, installs pip-tools from Artifactory,
  compiles `requirements.in` to a lock file, and syncs it. `requirements.in` lists the
  runtime and development dependencies directly (mkenv compiles with build isolation off
  and a fresh 3.13 venv has no setuptools, so an editable `-e .` line cannot resolve there);
  `pyproject.toml` carries the same list for anyone installing the package with pip. Both
  `/.mkenv` and `/python-env-*` are gitignored. `bin/orchestrator` runs
  `python -m orchestrator` with the venv's interpreter and the repo on `PYTHONPATH`, so
  nothing needs activating; activating the venv and running `python -m orchestrator` works
  too.
- **Target project venv, per worktree.** When the target is a Python project, `build.setup`
  runs its own `python mkenv.py` inside the worktree, producing a venv inside that
  worktree. With N parallel worktrees this means N environments and N syncs, so the
  orchestrator sets `PIP_CACHE_DIR` to a shared location under `.orchestrator/cache/`
  and runs `build.setup` under the build semaphore. For non-Python targets this step is
  simply absent.
- **Agents never see the orchestrator's venv.** The scrubbed environment (section 7.2)
  builds `PATH` from the worktree's own venv `bin` if one exists, then the system paths.
  The orchestrator's `python-env-*` directory, its `VIRTUAL_ENV`, and any `PYTHON*`
  variables are removed, so the worker's `python` and `pytest` resolve to the target
  project's interpreter, never the orchestrator's.
- **Interpreter requirement.** mkenv uses whichever `python` runs it, so `doctor` checks
  that the orchestrator is running on 3.13 or later and that `sys.prefix` is inside the
  repo's `python-env-*` directory, and warns otherwise.

Dependencies (kept small): `typer`, `pydantic>=2`, `pyyaml`, `httpx`, `jinja2`, `rich`,
`structlog`. GitHub via the `gh` CLI. Jira and Confluence via direct REST with `httpx`
rather than a thick client library. Dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy`.

Milestones:

| # | Milestone | Scope | Exit criterion |
|---|---|---|---|
| M0 | Skeleton | `mkenv.py`, `requirements.in`, `pyproject.toml`, package layout, config schema and loader, secret validation, adapter registry and `Capabilities`, `init`, `doctor`, audit log, SQLite store | `doctor` passes against a real Jira/GitHub setup and reports both configured adapters |
| M1 | Single issue to branch | Context fetch, worktree, worker agent, share grants and `orchestrator-cp`, MCP pass-through for Claude Code, build, test selection, local commit, findings.md on blocked | One real issue produces a validated local branch or a findings file, `--dry-run` only, including a support-to-raid copy recorded in the audit log |
| M2 | Review loop | Codex adapter with `read-only` sandbox, `--output-schema`, generated `CODEX_HOME` with allowlisted MCP servers, reviewer prompt, structured verdicts, fix rounds with session resume, PR body generation, push and PR | PR opened end to end with Claude Code as worker and Codex as reviewer, at least one fix round exercised |
| M3 | Write-back | Jira comments, transitions, attachments; Confluence context fetch and publish | Blocked and completed paths both visible in Jira and Confluence |
| M4 | Parallelism and resilience | asyncio scheduler, semaphores, retries, `resume`, `status`, `clean`, live status table | Three issues run concurrently on one machine without build contention |
| M5 | Epics and beyond | Epic expansion with dependency ordering, multi-config routing, JQL polling intake (optional) | An epic with dependent children completes in order |
| M6 | More runtimes | Gemini CLI, OpenCode, and Hermes adapters, entry-point discovery, conformance kit published, capability gap handling exercised | Each adapter passes the conformance kit; one real issue completes with a non-default reviewer |

Testing strategy for the orchestrator itself: unit tests for config validation, test
selection, and contract parsing; a fake `AgentRunner` that replays canned JSON so the
pipeline is tested without spending tokens, plus a second fake with `Capabilities` all
false to exercise every gap-filling path; a fake tracker and SCM host; one opt-in
integration test against a sandbox Jira project and a scratch GitHub repo.

## 11. Open questions and risks

1. **Bypassed permissions.** This was chosen for speed. If the compensating controls in
   section 7 prove leaky in M1 (for example the agent can reach the developer's global
   credential caches), the fallback is a generated allowlist settings file per run, which
   the design already supports.
2. **Claude Code CLI surface.** Verified against Claude Code 2.1.259 and current docs on
   2026-09-03 (Appendix A). `doctor` should pin a minimum version and fail on older CLIs.
   One gap remains: there is no per-session wall-clock flag, so the orchestrator enforces
   its own timeout by terminating the process group.
3. **Test subset adequacy.** `changed-paths` mapping needs maintenance. The PR template
   makes the gap explicit; CI remains the authority.
4. **Concurrent Claude Code sessions.** Resolved by giving each run its own
   `CLAUDE_CONFIG_DIR`. Worktrees are created by the orchestrator rather than with the CLI's
   own `--worktree` flag so that branch naming, base ref, and cleanup stay under one
   owner; revisit if the CLI's worktree isolation proves worth adopting.
5. **Jira rich text.** Atlassian Document Format to Markdown conversion is lossy; keep the
   raw ADF alongside the rendered text in the context directory.
6. **Confluence context scope.** The interview selected publishing only; reading the
   configured pages as context is retained from the original brief. Confirm or drop.
7. **Reviewer without native structured output.** Gemini CLI, OpenCode, and Hermes return
   free text (or an event stream) rather than schema-enforced JSON. Prompt-and-parse is
   workable but less reliable; if it proves flaky, the fallback is a tiny "formatter" pass
   on the worker's runtime that converts the review text into the contract.
8. **Codex `exec review`.** Codex has a built-in review subcommand (`--base <branch>`)
   producing a free-form review. It could seed the reviewer prompt, but v1 uses plain
   `codex exec` with the schema so every reviewer adapter follows the same contract.
9. **Codex limits.** Codex has no turn or budget flag; the orchestrator's timeout and the
   post-run usage check are the only bounds. Acceptable for a read-only reviewer, and a
   reason not to make Codex the default worker until that changes.
10. **Who decides what to copy.** This plan lets the worker infer the copy from the issue
    and record it. If the copies are predictable (say, every issue names a case folder and
    the destination follows a fixed pattern), a declared `copy:` rule in YAML executed by
    the orchestrator before the worker starts would be more deterministic and would remove
    the write grant from the agent entirely. Decide after seeing a few real issues.
11. **SMB semantics.** Copies onto an SMB mount can lose ownership, permissions, or
    extended attributes and can be slow for large trees. The helper preserves what the
    mount allows and reports what it could not; confirm that is acceptable for the assets
    involved.
12. **Codex auth in a per-run home.** With `CODEX_HOME` pointed at a fresh directory,
    Codex needs credentials there. The adapter runs `codex login --with-api-key` from the
    scrubbed key into that directory at run start; confirm the installed version honors
    `OPENAI_API_KEY` directly, in which case the login step is unnecessary.

## Appendix A. Runtime invocations

### A.1 Claude Code (default worker)

Verified against Claude Code 2.1.259 (`claude --help`) and the published CLI reference on
2026-09-03. Print mode returns exit code 0 on success and non-zero otherwise.

Worker invocation as built by `ClaudeCodeRunner`:

```bash
CLAUDE_CONFIG_DIR=<run>/<key>/claude-config \
ANTHROPIC_API_KEY=... \
claude -p \
  --bare \
  --output-format json \
  --json-schema "$(cat <run>/<key>/worker.schema.json)" \
  --model claude-fable-5-1 \
  --permission-mode bypassPermissions \
  --max-turns 200 \
  --max-budget-usd 15 \
  --settings <run>/<key>/settings.json \
  --append-system-prompt-file orchestrator/agents/prompts/worker.system.md \
  --add-dir <worktree> --add-dir /Volumes/support --add-dir /Volumes/raid \
  --mcp-config <run>/<key>/mcp.json --strict-mcp-config \
  --disallowedTools "mcp__mcp-jenkins__triggerBuild" \
  < <run>/<key>/worker.prompt.md
```

The process runs with `cwd` set to the worktree. The orchestrator wraps it in its own
wall-clock timeout and kills the process group on expiry, because the CLI has no
per-session timeout flag.

| Flag | Why the orchestrator uses it |
|---|---|
| `-p` (stdin prompt) | Non-interactive; the rendered prompt is piped in, avoiding argv length limits. Stdin is capped at 10 MB, which is why context goes into files. |
| `--bare` | Skips the developer's hooks, plugins, MCP servers, keychain, auto-memory, and CLAUDE.md auto-discovery. Auth becomes strictly `ANTHROPIC_API_KEY`. |
| `--output-format json` | One JSON object with `session_id`, `result`, `structured_output`, `num_turns`, `total_cost_usd`, `usage`, `is_error`, `subtype`, `terminal_reason`, `permission_denials`, `errors`. |
| `--json-schema` | CLI enforces the worker or reviewer contract and retries formatting itself; `subtype` becomes `error_max_structured_output_retries` if it cannot. |
| `--permission-mode bypassPermissions` | The chosen sandbox posture. Equivalent to `--dangerously-skip-permissions`. |
| `--max-turns`, `--max-budget-usd` | Hard caps from `agents.<role>` config. Exceeding either yields `subtype` `error_max_turns` or `error_max_budget_usd` and the task is marked `FAILED`. |
| `--settings <file>` | Per-run settings with the `PreToolUse` deny hook. Applied even under `--bare`. |
| `--append-system-prompt-file` | Role rules (section 7.3) that must not be overridden by repo content. |
| `--add-dir <worktree>` and share paths | Under `--bare`, also loads the worktree's `CLAUDE.md` explicitly. Share grants are added as further `--add-dir` entries; read-only grants are enforced by the hook since `--add-dir` itself grants read and write. |
| `--mcp-config` + `--strict-mcp-config` | The per-run file holding only this role's allowlisted servers, extracted from `~/.claude.json` and `.mcp.json`. Nothing else is inherited. |
| `--disallowedTools` | MCP tools from `mcp.deny_tools`, named `mcp__<server>__<tool>`. Works under bypassed permissions because it removes the tool rather than prompting. |
| `--resume <session_id>` | Fix round: resume the worker's session with the reviewer's findings as the new prompt, same flags otherwise. |
| `--no-session-persistence` | Used for the reviewer, which is single-shot and never resumed. |

Environment variables: `CLAUDE_CONFIG_DIR` isolates session storage per run;
`CLAUDE_CODE_PROJECT_DIR_NAME` can name the project directory when needed. Background
Bash tasks the agent starts are killed about five seconds after the final result, so the
worker prompt tells the agent to run builds in the foreground.

Result handling in the runner:

| `subtype` | Orchestrator action |
|---|---|
| `success` | Validate `structured_output`; continue the pipeline. |
| `error_max_turns`, `error_max_budget_usd` | `FAILED` with limits noted; partial diff kept. |
| `error_max_structured_output_retries` | `FAILED`; raw `result` saved for inspection. |
| `error_during_execution` | Retry once if `api_error_status` is 5xx or 429, else `FAILED`. |
| process killed by orchestrator timeout | `FAILED`, reason `timeout`. |

### A.2 Codex (default reviewer)

Verified against Codex CLI 0.153.0 (`codex exec --help`) on 2026-09-03.

Reviewer invocation as built by `CodexRunner`:

```bash
CODEX_HOME=<run>/<key>/codex-home \
OPENAI_API_KEY=... \
codex exec \
  --cd <worktree> \
  --sandbox read-only \
  --ephemeral \
  --model gpt-5.4 \
  -c model_reasoning_effort="high" \
  --output-schema <run>/<key>/reviewer.schema.json \
  --output-last-message <run>/<key>/reviewer.result.json \
  --json \
  - < <run>/<key>/reviewer.prompt.md \
  > <run>/<key>/reviewer.events.jsonl
```

| Flag | Why the orchestrator uses it |
|---|---|
| `codex exec -` | Non-interactive; `-` reads the prompt from stdin. |
| `--cd <worktree>` | Working root for the agent. |
| `--sandbox read-only` | The reviewer cannot modify the tree; it can still read files and run read-only commands. The worker role would use `workspace-write`. |
| generated `$CODEX_HOME/config.toml` | The per-run home holds a minimal `config.toml` with only this role's allowlisted `[mcp_servers.<name>]` blocks (copied from `~/.codex/config.toml`) and per-tool denials. The developer's own config is never loaded because `CODEX_HOME` points elsewhere, so `--ignore-user-config` is not needed. |
| `--add-dir <share>` (worker role only) | Grants write access to read-write shares alongside the worktree under `workspace-write`. The reviewer's `read-only` sandbox can already read both shares. |
| `--ephemeral` | Reviewer runs are single-shot; no session files written. Omitted for a Codex worker so `codex exec resume <id>` can serve the fix round. |
| `--model`, `-c model_reasoning_effort=` | From `agents.reviewer.model` and `options`. |
| `--output-schema <file>` | Native enforcement of the reviewer contract. |
| `--output-last-message <file>` | The final message (the contract JSON) lands in a file the orchestrator reads, independent of stdout noise. |
| `--json` | JSONL events on stdout, saved for the audit trail and parsed for token usage. |
| `--add-dir` | Would grant extra writable dirs; not used for the reviewer. |

Codex has no turn or budget flag. The orchestrator's wall-clock timeout bounds the run,
and the token usage parsed from the JSONL events is compared with `max_budget_usd`
afterwards. A generated execpolicy `.rules` file is added when the worker role uses
Codex; `--ignore-rules` is never passed.

### A.3 Other adapters (M6)

Flag sketches from installed help on 2026-09-03; each is confirmed when its adapter is
built.

- **Gemini CLI 0.46.0**: `gemini -p - --output-format json --approval-mode plan|yolo
  --model <m> --include-directories <worktree> --resume <id>`; `--policy` files for the
  policy engine. Structured output by prompt-and-parse from the JSON envelope's response
  field.
- **OpenCode 1.18.20**: `opencode run --dir <worktree> --format json --model
  <provider/model> --agent <name> --pure --session <id>`; `--auto` only for the worker
  role; permissions live in the OpenCode agent config. Structured output by
  prompt-and-parse from the final text event.
- **Hermes 0.21.0**: `hermes -z - --in <worktree> --model <m> --provider <p> --reasoning
  <level> --usage-file <path> --ignore-user-config --resume <id>`; `--yolo` only for the
  worker role; `--ignore-rules` to skip the developer's AGENTS.md and memory injection.
  Prints only the final response text, so structured output is prompt-and-parse.

## Appendix B. Adapter capability details

Confirmed items come from `--help` of the installed binaries and the vendors' published
docs on 2026-09-03. Items marked unconfirmed are checked when the adapter is built. Two
Codex details matter for a Codex *worker*: `workspace-write` keeps `.git` read-only, which
is fine because the orchestrator commits, and whether `hooks.json` fires under `codex exec`
is not documented, so the execpolicy `.rules` file is the primary deny mechanism.

| Adapter | Headless | Structured output | Resume | Sandbox / read-only | Usage | Isolation |
|---|---|---|---|---|---|---|
| Claude Code 2.1.259 | `-p`, stdin | `--json-schema` | `--resume <id>` | `--allowedTools`/`--disallowedTools`, `--permission-mode` | JSON result fields | `CLAUDE_CONFIG_DIR`, `--bare` |
| Codex 0.153.0 | `codex exec -` | `--output-schema` | `codex exec resume <id>` | `--sandbox read-only\|workspace-write` | JSONL events | `CODEX_HOME`, `--ignore-user-config` |
| Gemini CLI 0.46.0 | `-p`, stdin appended | none; `-o json` envelope with `response`, `stats`, `error` | `--resume <id>` (with `-p` unconfirmed) | `--approval-mode plan`, `--sandbox`, `tools.sandboxAllowedPaths` | `stats` in the envelope | `GEMINI_CLI_HOME`; `BeforeTool` hooks and `--policy` files |
| OpenCode 1.18.20 | `opencode run` (argv only; stdin declined upstream) | none; `--format json` events | `--session <id>` | `permission` config: `bash` deny patterns such as `git push *`, `edit` path globs | events / `opencode stats` | `OPENCODE_CONFIG_DIR`, `OPENCODE_CONFIG`, `OPENCODE_DISABLE_PROJECT_CONFIG`, `--pure` |
| Hermes 0.21.0 | `-z PROMPT` | none; final text only | `--resume <id>`, `--in DIR` | none; `-t` toolsets may omit write tools (unconfirmed); `terminal.backend: docker` for isolation | `--usage-file` | `HERMES_HOME`, `--ignore-user-config`, `--safe-mode`; `pre_tool_call` hooks |
