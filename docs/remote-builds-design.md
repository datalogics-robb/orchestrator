# Remote Builds: Design

Status: proposal, 2026-09-27. Not implemented. This extends `design-plan.md`: when a bug needs
a particular platform, its builds and tests run on a dedicated build machine for that platform,
while the agents stay on the orchestrator host.

Settled on 2026-09-27:

- Remote builds run on **dedicated machines**, not the Jenkins CI nodes.
- Each machine is configured with a **root directory** to work in and, optionally, the path of
  an existing **checkout** of the repository there.
- **ssh is used only when necessary**: when the bug report states that the problem is
  platform-specific and names the platform, or when the bug can't be resolved on the
  orchestrator host. Every other task runs locally, exactly as today.

## 1. Goal

Some bug reports only reproduce on one platform: Win32, Linux on arm64, AIX. Today every build
and test runs in the local worktree on the orchestrator host, so the worker can't reproduce
those bugs, and the red check and test gates prove nothing about the platform that matters.

The goal is for such a task to move its platform-sensitive commands to a build machine for the
right platform. That covers the agent's own builds while it investigates, and the
orchestrator's build, red-check, and test gates. Most tasks never leave the orchestrator host.

Out of scope: checking a change on every platform. Once the PR is open, the existing Jenkins CI
does that. The orchestrator proves the fix on the one platform the bug needs and leaves the
rest to CI.

## 2. Decisions

| Topic | Decision | Why |
|---|---|---|
| When a task goes remote | Only when (a) the bug report states the problem is platform-specific and names the platform, (b) the worker can't reproduce or resolve the bug locally and the report names the platform it occurs on, or (c) the operator passes `--platform`. Otherwise it stays local. | Remote builds cost setup time on another machine and add a network dependency. Local stays the default. |
| Who decides (a) and (b) | The worker, which must quote the sentence in the issue that names the platform. The orchestrator checks the quote is really in the issue and the platform is configured. | "States that the problem is platform-specific" is a judgment about prose, not a field. The quote check stops a guessed platform from sending a task to a node. |
| Where agents run | Always on the orchestrator host. Only commands move. | The Claude subscription login is tied to the operator's login session. Claude Code doesn't run on AIX. Hooks, MCP, `orchestrator-cp`, and the audit log all assume the host. |
| Machines | Dedicated build machines per platform, each with a dedicated build account. | No competition with CI for CPU or disk, and an agent's test code can't damage CI workspaces. |
| Transport | ssh into the build account. | sshd already provides authentication, encryption, and running commands on every platform we build on. A node service would mean deploying, upgrading, and securing a daemon on AIX and Windows too. |
| What runs on the machine | A single standard-library Python script, `orchestrator-node`, as the account's forced ssh command. No daemon. | A machine that builds pdfl18 needs Python 3 for mkenv and invoke anyway. A forced command limits the ssh key to the script's operations, even if an agent reads the key. |
| Directories | `root` (required): where per-task directories go. `checkout` (optional): an existing clone that task directories are created from as git worktrees. | With a checkout, starting a task sends only the task's changes, not the whole repository. Without one, the first sync sends every tracked file. |
| Moving source | A tar stream built with Python's `tarfile` on both ends, sending only files that differ from what the machine already has. | Doesn't depend on rsync or GNU tar, which AIX lacks. |
| Build directories | One task directory on one machine, kept for the task's whole life. | Incremental builds: a pdfl18 build is allowed 180 minutes, so a full rebuild every round is not an option. |
| How agents reach a machine | An `orchestrator-build` helper, staged only for remote tasks, that asks the orchestrator process to run a command. `ssh`, `scp`, `rsync`, and `sftp` join the commands agents are denied. | Same model as `orchestrator-cp`: every remote command is checked against an allowlist, bounded, and audited, and the agent never needs the key. |
| Jenkins | Not used. | Automated CI already runs on Jenkins; the orchestrator doesn't duplicate it. |

## 3. When a task goes remote

```
CONTEXT ──▶ TRIAGING ──┬── report states platform X ──────────▶ WORKTREE (pinned to X) ──▶ WORKING (remote gates)
   │                   └── otherwise ─────────────────────────▶ WORKTREE (local) ──▶ WORKING (local)
   │                                                                                     │
   └─ --platform X: skip TRIAGING, pin to X                         worker: needs_platform X, with evidence
                                                                                         ▼
                                                                         ESCALATING: pin to X, remote setup
                                                                                         ▼
                                                                           WORKING again, same session, remote gates
```

### 3.1 Trigger (a): the report says so, found by triage

A new TRIAGING state runs between CONTEXT and WORKTREE, before any local setup, so a task headed
for a remote machine doesn't pay for a local bootstrap first. It runs only when at least one
remote platform is configured and `--platform` wasn't given.

It is one short, read-only call to the worker's runtime. The prompt contains the issue and its
comments (not the codebase), and the call is capped at a few turns. It returns a small contract:

```json
{"platform_specific": true,
 "platform": "windows-x64",
 "evidence": "This only happens on Windows; the same file renders correctly on macOS and Linux."}
```

The prompt defines the bar: the report must **state** that the problem is specific to a
platform and **name** it. A platform that only appears in the customer's environment line, such
as "(Schlafender, APDFL15, Win32)", doesn't meet it. That case is left to trigger (b).

The orchestrator accepts the answer only if the platform, or one of its `aliases`, is
configured, and `evidence` appears in the issue text (compared case-insensitively with
whitespace collapsed) and mentions that platform or an alias. Otherwise the task stays local,
and the audit log says why. Triage uses the worker's login and model, and its cost counts
toward the task.

### 3.2 Trigger (b): it can't be resolved locally, found by the worker

The worker's contract gains a third status beside `completed` and `blocked`:

```json
{"status": "needs_platform",
 "platform_request": {
   "platform": "windows-x64",
   "evidence": "Environment: Windows Server 2022, APDFL 18.0.5, 64-bit",
   "tried_markdown": "Built and ran SF41157 on macOS: the text is removed correctly. ..."}}
```

The worker, fixer, and red/green prompts say when to use it. The worker must have tried locally
and failed to reproduce, or found that the behaviour depends on platform code (for example a
Win32-only code path). The issue must name a platform the bug was seen on. `tried_markdown`
records what was tried, for the PR body or the findings.

The orchestrator applies the same checks as for triage (a configured platform, and evidence
found in the issue that names it), and escalates at most once per task:

1. It enters ESCALATING. It pins the task to a machine for the platform (section 4), prepares
   the task directory, syncs the worktree, and runs `build.setup` there.
2. It returns to the stage that asked (WORKING, FIXING, TEST_WRITING, or IMPLEMENTING). The
   worker's session is resumed, the prompt says the task now runs on platform X through
   `orchestrator-build`, and `tried_markdown` is carried forward.
3. Escalating doesn't use up a fix round.

If the checks fail, the task has already been escalated, or no machine for the platform is
reachable, the task is blocked. The findings include what the worker tried and ask the
reporter which platform the bug reproduces on (or say which platform has no machine).

A local build or test failure alone never escalates a task. That means the change is wrong, and
it goes through the normal fix rounds.

### 3.3 Trigger (c): the operator says so

`run --platform linux-armv8 KEY` skips triage and pins the task before WORKTREE.
`resume --platform` applies only to tasks that haven't been pinned yet. `--platform local`
disables both triggers for the run.

### 3.4 No ssh otherwise

A local task never opens an ssh connection. `doctor` doesn't contact the machines unless asked
(`doctor --nodes`, or `orchestrator nodes check`), and neither does the web daemon at startup.
A machine is first contacted when a task is pinned to it, and the pin checks it then (`info`,
section 6).

## 4. Machines and directories

```yaml
platforms:
  linux-armv8:
    aliases: [aarch64, arm64, "linux arm", "arm linux"]
    ssh:
      user: orchbuild
      identity_file: ~/.ssh/orchestrator_build
      known_hosts: ~/.ssh/orchestrator_known_hosts   # pinned; host keys are always checked
    root: /build/orchestrator          # default for this platform's machines
    checkout: /build/pdfl18_all        # optional default
    max_concurrent_builds: 2           # per machine
    shares: {support: /support, raid: /raid}
    share_sync_minutes: 10
    env: {CONAN_HOME: /build/conan}
    agent_commands: []                 # extra allowed prefixes for orchestrator-build
    nodes:
      - host: arm-build-01             # uses the platform's root and checkout
      - host: arm-build-02
        root: /data/orchestrator       # per-machine override
        checkout: /data/pdfl18_all
  windows-x64:
    aliases: [windows, win32, win64]
    nodes: [{host: win-build-01, root: 'D:\orchestrator'}]
    # ...
```

- **`root`** (required, absolute): every task directory lives at
  `<root>/<run id>/<issue key>/`, holding `src/` (the source), `jobs/` (pid files), and the sync
  manifest. The node script refuses any path outside `root`, except the `checkout` operations
  below.
- **`checkout`** (optional, absolute): an existing clone of `repo.github`, maintained by
  whoever set up the machine. The orchestrator only ever runs `git fetch` in it and adds and
  removes worktrees from it. It never checks out a branch, pulls into its working tree, or
  writes files there, so a person can keep using it. When set:
  - `prepare` runs `git fetch <remote> <base branch>` in the checkout if the task's base commit
    is missing. This needs the build account to have read access to GitHub, such as a
    read-only deploy key. It then runs `git worktree add --detach <root>/<run>/<key>/src <base sha>`.
  - The first sync sends only what differs from the base commit: the committed, staged, and
    unstaged changes (`git diff --name-status <base sha>`) plus untracked files that aren't
    ignored.
  - `nodes check` verifies the checkout's remote URL matches `repo.github`.
- **Without `checkout`**, `prepare` creates an empty `src/`, and the first sync sends every file
  `git ls-files -co --exclude-standard` lists.
- **Afterwards**, each sync sends only files whose size or hash differs from the task's last
  manifest, plus deletions. Ignored files are never sent or deleted, so build outputs and the
  venv survive between rounds.

**Picking a machine.** When a task is pinned, the orchestrator picks the configured machine for
the platform that passes `info` and has the fewest running jobs. The task keeps that machine
until it ends. If the machine is unreachable on a later resume, the task fails as a transient
infrastructure error. Once retries run out, `resume --repin` moves it to another machine,
starting from a fresh directory.

## 5. What runs where once a task is remote

| Step | Where |
|---|---|
| Triage, worker, fixer, reviewer (the agents themselves) | orchestrator host |
| `build.local_setup` (the local venv pre-commit needs; for pdfl18, `python3 mkenv.py`) | orchestrator host |
| `build.setup`, `build.commands`, red check, selected tests | the task's machine |
| The agent's own builds and tests through `orchestrator-build` | the task's machine |
| Pre-commit, commit, review (the reviewer reads the diff), push, PR, Jira | orchestrator host |

A local task runs `build.local_setup` and then `build.setup` locally, as today. `build.setup`
currently does both jobs. The PR body gains a line such as "Verified on linux-armv8
(arm-build-02); triggered by the report: '...'".

A DONE task's machine directory is removed under the same rule as its local worktree: it's kept
with `--keep-worktrees` or in a dry run, and blocked or failed ones are always kept. Removing
it also runs `git worktree remove` in the checkout. `orchestrator clean` removes machine
directories older than its cutoff, but only for machines the store says it has used.

## 6. The node script: `orchestrator-node`

One file, standard library only, installed into the build account by
`orchestrator nodes install <host>`. ssh runs it as the forced command for the orchestrator's
key:

```
# ~orchbuild/.ssh/authorized_keys on the machine
restrict,command="python3 ~/.orchestrator-node/node.py" ssh-ed25519 AAAA... orchestrator@robbs-macbook-pro
```

It reads the requested operation from `SSH_ORIGINAL_COMMAND`, and the root and checkout from
its arguments, checked against a small config file written at install:

| Operation | Does |
|---|---|
| `info` | Prints JSON: script version, OS, architecture, Python version, free disk under the root, which share paths exist, and whether the checkout exists and which remote it has. |
| `prepare <run> <key> <base sha>` | Creates the task directory. With a checkout: fetches if needed and adds a detached worktree at the base commit. Without one: creates an empty `src/`. |
| `sync <run> <key>` | Reads a tar stream on stdin into `src/`, applies the deletion list, and writes the new manifest. Refuses any member path that is absolute, contains `..`, or is a symlink pointing outside `src/`. |
| `run <run> <key> <job> -- argv...` | Runs argv in `src/` in a new process group, with a scrubbed environment plus the configured variables, and streams output. Writes `jobs/<job>.pid`. If the ssh connection drops (stdin reaches EOF), it kills the group. |
| `cancel <run> <key> <job>` | Kills a job's process group, with SIGTERM and then SIGKILL. |
| `clean <run> [<key>]`, `clean --older-than 7d` | Removes task directories, and their worktree registrations in the checkout. |

The script never runs a shell. The orchestrator refuses a machine whose script is older than its
own and says to run `orchestrator nodes install`.

## 7. The agent's helper: `orchestrator-build`

Staged into the agent's `PATH` next to `orchestrator-cp`, but only once the task is pinned to a
machine:

```
orchestrator-build invoke -e build --config=Release
orchestrator-build invoke -e test --config=Release --tests=SF41157
```

- **Transport.** The shim connects to a unix socket in the task's run directory
  (`ORCHESTRATOR_BUILD_SOCKET`). The orchestrator listens on it while that task's agent runs.
  The orchestrator syncs the worktree, runs the command on the machine, and streams the output
  back. The shim prints it and exits with the remote exit code.
- **Allowlist.** The orchestrator accepts argv matching `build.setup`, `build.commands`,
  `test.selection.allowed_prefixes`, or the platform's `agent_commands` prefixes. Anything else
  is refused, with the list of what's allowed. The argv is passed without a shell.
- **Paths.** Before output reaches the agent, the machine's `src/` prefix is rewritten to the
  local worktree path, so compiler errors point at files the agent can open.
- **Limits.** Each call counts against the machine's build limit and the agent's timeout, and
  is audited as `remote_command` with machine, argv, exit code, and duration. A killed agent
  drops the socket connection, which cancels the remote job.
- **Share consistency.** A machine may see `/raid` through a mirror that lags behind. Before
  running tests, the orchestrator checks that every file this task copied with
  `orchestrator-cp` (the audit log has each one's SHA-256) exists on the machine with the same
  hash. It waits up to `share_sync_minutes` for the mirror to catch up and says so in the
  output, rather than letting a test fail on a file that hasn't arrived yet.

## 8. Code changes

- **`agents/contracts.py`.** `WorkerResult.status` gains `needs_platform`, and a
  `PlatformRequest` model (`platform`, `evidence`, `tried_markdown`). There's also a
  `TriageResult` contract. Both schemas pass `strict_schema()` for Codex.
- **`agents/prompts/`.** A new `triage.md`. The worker, fixer, red, and green prompts gain the
  rules for `needs_platform`, and a section that renders only when the task is remote (the
  platform, the machine, `orchestrator-build`).
- **`pipeline/task.py`.** New states TRIAGING and ESCALATING. New fields `platform`, `node`,
  `node_manifest`, `platform_trigger` (`report`, `unresolved`, or `operator`),
  `platform_evidence`, and `escalated_from` (the stage to return to).
- **`pipeline/triage.py` (new).** `stage_triage`, evidence checking, and alias matching.
- **`pipeline/stages.py` and `feature.py`.** The `needs_platform` handling, shared by every
  stage that parses a `WorkerResult`, and `stage_escalate`. WORKTREE runs `build.local_setup`
  locally, and `build.setup` locally or on the machine. BUILDING, RED_CHECK, and TESTING use
  `rt.executor_for(task)`.
- **`build/executor.py` (new).** An `Executor` protocol with `prepare`, `sync`, `run`,
  `cancel`, and `clean`. `LocalExecutor` wraps today's `run_process` unchanged. `SshExecutor`
  runs `ssh` with fixed options (`BatchMode=yes`, `StrictHostKeyChecking=yes`, the pinned
  `UserKnownHostsFile`, `ServerAliveInterval`), and keeps the manifest and path mapping.
- **`build/runner.py`.** `run_step` takes an executor. `BuildSemaphore` becomes one semaphore
  per target: the local host, or each machine.
- **`shares/`.** Staging the `orchestrator-build` shim, and the per-task socket server.
- **`agents/runners/common.py`.** `ssh `, `scp `, `rsync `, and `sftp ` join
  `DEFAULT_DENY_COMMANDS`.
- **`remote/node.py` (new, standard library only).** The node script.
- **`config/schema.py`.** `platforms`, `build.local_setup`, and per-node `root` and `checkout`,
  each with a description, plus the `init` example.
- **`cli.py`.** `run/resume --platform`, `resume --repin`, `doctor --nodes`, and
  `orchestrator nodes install|check|list|clean`.
- **Web monitor.** The Agents tab gains a Platform / machine column, and shows TRIAGING and
  ESCALATING. A Nodes tab fits the tab plan after the web monitor's W2.

## 9. Security

- **A dedicated account on each dedicated machine**, with no sudo. An agent's tests run code it
  wrote; the account limits what a mistake can reach.
- **The key can only run the node script.** The agent runs as the operator's user and can read
  `~/.ssh/orchestrator_build`, so hiding the key isn't the control. The forced command is: with
  `restrict,command=...`, the key can do nothing but the operations in section 6, confined to
  the root and, for fetch and worktree operations, the checkout. The deny list, which stops
  `ssh`, `scp`, `rsync`, and `sftp`, is a second, weaker layer: like every hook rule, it
  matches command patterns and is not a sandbox (a command wrapped in `bash -c "..."` gets past
  it).
- **Host keys are pinned** in a known-hosts file of their own, and a changed key fails the
  machine's check rather than being accepted.
- **No orchestrator secrets reach a machine.** Jira, GitHub-write, and agent credentials stay
  on the host. What a build needs on the machine (a Conan remote login, or the checkout's
  read-only deploy key) is configured once in the build account by the operator, and
  `nodes check` looks for it.
- **The evidence check** (section 3) means a worker can't send a task to a machine on a guess.
  It has to point at a sentence in the issue.
- **Tar extraction is strict** (section 6), because the stream contains files the agent chose.
- **Audit.** Triage results, escalations, pins, syncs (file count, bytes), remote commands,
  cancels, and cleans are recorded in the run's `audit.jsonl` with the machine's name.

## 10. Tests

- **Triage and escalation.** A fake worker returns `needs_platform` and fake triage results.
  The tests cover: accepted when the evidence is in the issue and names a configured platform;
  refused when the quote is invented, the platform unknown, or the platform only appears in an
  environment line during triage; refused when the task was already escalated. They check that
  escalating doesn't use a fix round, and that `--platform` skips triage. A local task never
  constructs an `SshExecutor`.
- **The node script without ssh.** An executor test transport runs `node.py` directly as a
  subprocess with `SSH_ORIGINAL_COMMAND` set, against a temporary root and a temporary checkout
  (a clone of the test repository). That covers prepare with and without a checkout, a first
  sync that sends only the delta from the base commit, incremental syncs, deletions, ignored
  files kept, the path-escape refusals, run with streaming and exit codes, cancel, and EOF
  killing a job.
- **Pipeline.** The fake-agent pipeline tests run with a "remote" platform whose executor is
  that local transport. They check which steps ran in the machine directory and which locally,
  and that a DONE task's directory and checkout worktree are removed, except in a dry run.
- **The helper.** Allowlist acceptance and refusal, path rewriting, and cancelling when the
  shim's connection drops.
- **Real machines.** `orchestrator nodes check` and an opt-in `orchestrator nodes smoke <host>`,
  which prepares, syncs, and runs `python3 -c ...` in a scratch task directory.

## 11. Milestones

| # | Scope | Exit criterion |
|---|---|---|
| R0 | Contracts (`needs_platform`, triage), TRIAGING and ESCALATING states, the evidence check, `--platform`, `platforms` config, `build.local_setup`, the `Executor` abstraction with `LocalExecutor` only | Behaviour is unchanged for tasks that stay local. With a fake remote platform, triage and escalation pin tasks correctly, and invented evidence is refused |
| R1 | `node.py`, `SshExecutor`, root and checkout handling, `nodes install/check/clean`, remote setup, build, red check, and test gates on Linux x86_64 and armv8 | A real Linux-only bug whose report says so goes through a dry run with every gate on the machine. Cancelling mid-build leaves no process there |
| R2 | The `orchestrator-build` helper and socket server, remote-task prompts, the deny list, path rewriting, the share consistency wait | A worker that can't reproduce a bug locally escalates, then reproduces and fixes it through the helper |
| R3 | AIX | An AIX bug goes through a dry run end to end |
| R4 | Windows: OpenSSH Server, the node script's process handling through job objects, PowerShell or cmd build commands | A Win32 bug goes through a dry run end to end |

## 12. Open questions

- **Which platform first after Linux.** AIX (R3) comes before Windows (R4) because it needs no
  new process handling. If Win32 bugs are more common, swap them.
- **Extra platforms for an agent.** Whether a remote task may also build on a second platform
  (for example "confirm this doesn't break Linux"), or whether that is always left to CI. The
  plan currently leaves it to CI.
- **Checkout upkeep.** Whether the orchestrator should ever advance a checkout (for example
  `git fetch` on a schedule) or leave that entirely to whoever maintains the machine. The plan
  fetches only when a task's base commit is missing.
