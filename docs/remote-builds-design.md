# Remote Builds: Design

Status: proposal, 2026-09-27; identity model revised 2026-10-08. The core of R1 is implemented,
2026-10-08, with the operator trigger only (section 13). This extends `design-plan.md`: when a
bug needs a particular platform, its builds and tests run on a build machine for that platform,
while the agents stay on the orchestrator host.

Settled on 2026-09-27, revised 2026-10-08:

- **The operator's own identity.** The orchestrator is run by a regular user, with that user's
  identity and permissions (`design-plan.md` section 2). It reaches a build machine with the
  login the operator already uses there, the same command they would type, for example:

  ```
  ssh -i ~/.ssh/engineering-dl.pem rocky@10.200.0.78
  ```

  That holds whether the account is personal or shared, and whatever it is allowed to do. The
  orchestrator creates no accounts, keys, or `authorized_keys` entries, and needs no
  administrator on the machine.
- **Commands move, agents never do.** A build machine runs build and test commands and the
  node script that starts them, nothing else. No agent CLI, agent login, MCP server, or
  orchestrator credential is ever placed on or run on a build machine.
- **Machines the operator can reach.** Any machine the operator can ssh to for a platform can
  be a build machine. Dedicated machines are preferred. Shared ones, including Jenkins CI
  agents, are allowed with a per-machine command limit (`max_concurrent_builds`).
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
| Where agents run | Always on the orchestrator host. Only build and test commands move. | The Claude subscription login is tied to the operator's login session. Claude Code doesn't run on AIX. Hooks, MCP, `orchestrator-cp`, and the audit log all assume the host. Keeping agents off the machines also keeps their logins and tokens off them. |
| Identity on the machine | The operator's own ssh login: their user, key, `known_hosts`, and ssh config, as in `ssh -i ~/.ssh/engineering-dl.pem rocky@10.200.0.78`. | The orchestrator acts as the operator everywhere (`design-plan.md` section 2). Asking for a dedicated account would mean an administrator, and a second identity, on every machine. |
| Machines | Any machine the operator can ssh to for the platform. Dedicated ones are preferred; shared ones, such as CI agents, get a low `max_concurrent_builds`. | A dedicated machine leaves CI's CPU and disk alone. When the only machines for a platform are CI agents, a command limit and a separate `root` let both share them. |
| Transport | ssh, with the options pinned by the orchestrator (batch mode, strict host-key checking). | sshd already provides authentication, encryption, and running commands on every platform we build on. A node service would mean deploying, upgrading, and securing a daemon on AIX and Windows too. |
| What runs on the machine | A single standard-library Python script, `orchestrator-node`, run as an ordinary remote command over that login: `ssh ... python3 <root>/node.py <operation> ...`. No daemon, nothing installed outside `root`. | A machine that builds pdfl18 needs Python 3 for mkenv and invoke anyway. One script gives every platform the same operations, process handling, and path checks. |
| Directories | `root` (required): where per-task directories go. `checkout` (optional): an existing clone that task directories are created from as git worktrees. | With a checkout, starting a task sends only the task's changes, not the whole repository. Without one, the first sync sends every tracked file. |
| Moving source | git on the host works out what changed since the last sync; a gzipped tar stream built with Python's `tarfile` carries it, and the node script unpacks it. | git already knows which files changed and which are ignored, so the machine keeps no manifest. The stream doesn't depend on rsync or GNU tar, which AIX lacks. |
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
    ssh:                               # the operator's own login, as they would type it
      user: rocky
      identity_file: ~/.ssh/engineering-dl.pem
      # known_hosts: omitted, so ~/.ssh/known_hosts; host keys are always checked
    root: /home/rocky/robb/orchestrator   # default for this platform's machines
    checkout: /home/rocky/robb/pdfl18_all # optional default
    max_concurrent_builds: 1           # per machine; these are also CI agents
    shares: {support: /support, raid: /raid}
    share_sync_minutes: 10
    env: {CONAN_HOME: /build/conan}
    agent_commands: []                 # extra allowed prefixes for orchestrator-build
    nodes:
      - host: 10.200.0.78              # uses the platform's root and checkout
      - host: 10.200.0.224
        root: /data/orchestrator       # a per-machine override, for illustration
        checkout: /data/pdfl18_all
  windows-x64:
    aliases: [windows, win32, win64]
    nodes: [{host: win-build-01, root: 'D:\orchestrator'}]
    # ...
```

- **`root`** (required, absolute): every task directory lives at
  `<root>/<run id>/<issue key>/`, holding `src/` (the source) and `jobs/` (pid files), and the
  node script lives at `<root>/node.py`. On a shared login, a root inside the operator's own
  folder (`/home/rocky/robb/orchestrator`) keeps the orchestrator's files apart from everyone
  else's. The node script refuses any path outside `root`, except the `checkout` operations
  below.
- **`checkout`** (optional, absolute): an existing clone of `repo.github`, maintained by
  whoever set up the machine. The orchestrator only ever runs `git fetch` in it and adds and
  removes worktrees from it. It never checks out a branch, pulls into its working tree, or
  writes files there, so a person can keep using it. It may be the operator's own clone, even of
  a fork. When set:
  - `prepare` runs `git fetch <fetch_url> <base branch>` in the checkout if the task's base
    commit is missing. Fetching by URL adds no remote. It uses whatever GitHub access the login
    already has. It then runs `git worktree add --detach <root>/<run>/<key>/src <base sha>`.
  - The first sync sends only what differs from the base commit: committed, staged, and unstaged
    changes, and untracked files that aren't ignored.
  - `nodes check` warns when the checkout's remote doesn't look like the repository.
- **Without `checkout`**, `prepare` creates an empty `src/`, and the first sync sends every file
  that isn't ignored.

**How a sync knows what to send.** Each task keeps a private git index file on the host, in its
run directory. Before each gate, `git add -A` into that index (with `GIT_INDEX_FILE`) snapshots
the worktree as a tree: ignored files are left out, and unchanged files are skipped by their stat
data, so a later snapshot costs little. `git diff-tree` between that tree and the tree the
machine last received lists exactly the files to send and the paths to delete:

- **The first comparison** is against the base commit's tree when the machine starts from a
  checkout (the index starts with `git read-tree <base sha>`), or the empty tree when it starts
  empty.
- **After a successful sync**, the new tree id is stored on the task as `node_tree`, and the next
  sync diffs against it.
- **What's carried:** regular files with their executable bit, and symlinks. Gitlinks are
  skipped.
- **On the machine:** the sync applies the deletions and unpacks the tar into `src/`. It keeps no
  manifest, and files it was never sent, such as build output and the venv, survive between
  rounds.

The snapshot writes blobs for untracked files into the host repository's object store, where
`git gc` prunes them later. `prepare` resets the task's index and `node_tree`, so a re-prepared
directory starts from the base again.

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

One file, standard library only, copied to `<root>/node.py` by `orchestrator nodes install`,
or automatically when a task is pinned to a machine whose copy is missing or older. It runs as
an ordinary command over the operator's login, every word shell-quoted:

```
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -i ~/.ssh/engineering-dl.pem rocky@10.200.0.78 \
    -- python3 /home/rocky/robb/orchestrator/node.py info --root /home/rocky/robb/orchestrator ...
```

The operation and its arguments, including the root and checkout, come from its command line:

| Operation | Does |
|---|---|
| `info` | Prints JSON: script version, OS, architecture, Python version, free disk under the root, which share paths exist, and whether the checkout exists and which remote it has. |
| `prepare <run> <key> <base sha>` | Creates the task directory. With a checkout: fetches if needed and adds a detached worktree at the base commit. Without one: creates an empty `src/`. |
| `sync <run> <key>` | Reads a JSON header line (the deletion list) and then a tar stream on stdin into `src/`. Refuses any member path that is absolute, contains `..`, enters `.git`, or is a symlink pointing outside `src/`. |
| `run <run> <key> <job> -- argv...` | Runs argv in `src/` in a new process group, with a scrubbed environment plus the configured variables, and streams output. Writes `jobs/<job>.pid`. If the ssh connection drops (stdin reaches EOF, or SIGHUP or SIGTERM arrives), it kills the group. |
| `cancel <run> <key> <job>` | Kills a job's process group, with SIGTERM and then SIGKILL. |
| `clean <run> [<key>]`, `clean --older-than 7d` | Removes task directories, and their worktree registrations in the checkout. |

The script never runs a job through a shell: argv is executed directly. (The login shell does
parse the quoted command line that starts the script.)

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
  `node_tree` (the tree the machine last received), `platform_trigger` (`report`, `unresolved`,
  or `operator`), `platform_evidence`, and `escalated_from` (the stage to return to).
- **`pipeline/triage.py` (new).** `stage_triage`, evidence checking, and alias matching.
- **`pipeline/stages.py` and `feature.py`.** The `needs_platform` handling, shared by every
  stage that parses a `WorkerResult`, and `stage_escalate`. WORKTREE pins the task and, for a
  pinned task, runs `build.local_setup` locally and `build.setup` on the machine. SETUP,
  BUILDING, RED_CHECK, and TESTING go through `stages.run_gate`, which runs a step on the
  task's machine when it is pinned and through `build.runner.run_step` otherwise. Local steps
  are unchanged.
- **`remote/client.py` (new).** `NodeClient`, one method per node-script operation, over a
  `Transport`:
  - `SshTransport` runs `ssh` with the operator's login and fixed options (`-T`,
    `BatchMode=yes`, `StrictHostKeyChecking=yes`, `ServerAliveInterval`), shell-quoting the
    remote command.
  - `DirectTransport` runs the script as a local subprocess, for tests.
  - A `run` holds stdin open for the job's life, and on a timeout or cancellation stops ssh and
    then sends `cancel`.
- **`remote/delta.py` (new).** The sync computation above: snapshot, diff, and the payload (a
  JSON header line with the deletions, then the tar stream).
- **`remote/builds.py` (new).** `RemoteBuilds`, one per runtime:
  - picks a machine (fewest running jobs);
  - prepares, syncs, runs a step, and cleans;
  - holds each machine's `max_concurrent_builds` semaphore, shared across runs in a process;
  - rewrites the machine's `src/` path to the local worktree in logs.

  `build/runner.py` and the local `BuildSemaphore` are untouched.
- **`shares/`.** Staging the `orchestrator-build` shim, and the per-task socket server.
- **`agents/runners/common.py`.** `ssh `, `scp `, `rsync `, and `sftp ` join
  `DEFAULT_DENY_COMMANDS`.
- **`remote/node.py` (new, standard library only).** The node script.
- **`config/schema.py`.** `platforms`, `build.local_setup`, and per-node `root` and `checkout`,
  each with a description, plus the `init` example.
- **`cli.py`.** `run/resume --platform`, `resume --repin`, `doctor --nodes`, and
  `orchestrator nodes install|check|clean`.
- **Web monitor.** The Agents tab gains a Platform / machine column, and shows TRIAGING and
  ESCALATING. A Nodes tab fits the tab plan after the web monitor's W2.

## 9. Security

- **The operator's identity is the boundary.** The orchestrator, its agents, and its build
  commands all run as the operator. On a build machine, the commands run as the login the
  operator uses there, with that login's permissions, as if the operator ran them by hand. If
  that login can use sudo or write to CI workspaces, so can a build or test command, and a
  test the agent wrote is code the operator chose to run. The design doesn't pretend otherwise,
  and it doesn't require a weaker login to work. An operator who wants a narrower one
  can configure it (a less privileged account, or a key limited with `command=` in
  `authorized_keys`). Supporting such a key means reading the operation from
  `SSH_ORIGINAL_COMMAND`, which is not built yet.
- **Agents never hold the ssh access.** They run on the host as the operator, so they could
  read the key file, as they could read anything else in the operator's home. The controls
  are these:
  - Agents get no `SSH_AUTH_SOCK`.
  - `ssh`, `scp`, `rsync`, and `sftp` are denied to them, like `git push` and `gh`.
  - The only path to a machine is `orchestrator-build`, whose commands are allowlisted, bounded,
    and audited.

  The deny list matches command patterns and is not a sandbox (a command wrapped in
  `bash -c "..."` gets past it). These controls bound what an agent does with the operator's
  access; they are not a second identity.
- **Host keys are checked** against the operator's `known_hosts` (or a file named in
  `ssh.known_hosts`). An unknown or changed key fails the connection rather than being
  accepted.
- **No orchestrator secrets reach a machine.** Jira, GitHub-write, and agent credentials stay
  on the host. What a build needs there (a Conan remote login, GitHub read access for the
  checkout) is whatever the operator's login already has, and `nodes check` reports what it
  can see.
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
  contacts a machine.
- **The node script without ssh.** `DirectTransport` runs `node.py` directly as a subprocess,
  with the same command line ssh would carry, against a temporary root and a temporary checkout
  (a clone of the test repository). That covers prepare with and without a checkout, a first
  sync that sends only the delta from the base commit, incremental syncs, deletions, ignored
  files kept, the path-escape refusals, run with streaming and exit codes, cancel, and EOF
  killing a job.
- **Pipeline.** The fake-agent pipeline tests run with a "remote" platform reached through
  that transport. They check which steps ran in the machine directory and which locally,
  and that a DONE task's directory and checkout worktree are removed, except in a dry run.
- **The helper.** Allowlist acceptance and refusal, path rewriting, and cancelling when the
  shim's connection drops.
- **Real machines.** `orchestrator nodes check` and an opt-in `orchestrator nodes smoke <host>`,
  which prepares, syncs, and runs `python3 -c ...` in a scratch task directory.

## 11. Milestones

| # | Scope | Exit criterion |
|---|---|---|
| R0 | Contracts (`needs_platform`, triage), TRIAGING and ESCALATING states, the evidence check, `--platform`, `platforms` config, `build.local_setup`, `stages.run_gate` with local steps only | Behaviour is unchanged for tasks that stay local. With a fake remote platform, triage and escalation pin tasks correctly, and invented evidence is refused |
| R1 | `node.py`, `NodeClient` over ssh, the git-based sync, root and checkout handling, `nodes install/check/clean`, remote setup, build, red check, and test gates on Linux x86_64 and armv8 | A real Linux-only bug whose report says so goes through a dry run with every gate on the machine. Cancelling mid-build leaves no process there |
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

## 13. Implementation status

Implemented on 2026-10-08: the core of R1, driven by trigger (c) only. `run --platform NAME` (and
`resume --platform` for tasks not yet pinned) pins every new task of the run to a machine of that
platform, and its setup, build, red check, and test gates run there.

- `remote/node.py`: the node script, with `info`, `prepare`, `sync`, `run`, `cancel`, and `clean`.
- `remote/client.py`: `NodeClient` over `SshTransport`, plus a `DirectTransport` that runs the
  script as a local subprocess for the tests.
- `remote/delta.py`: what a sync sends. `remote/builds.py`: pinning, prepare, sync, remote steps,
  per-machine limits, cleanup.
- `stages.run_gate` sends a pinned task's gates to its machine. `build.local_setup` runs on the
  host for pinned tasks only.
- `orchestrator nodes check | install | clean`, and the `platforms` config.

It was verified against the two `linux-armv8` machines (10.200.0.78 and 10.200.0.224): `nodes
check` and `nodes install`, plus a smoke test of prepare (including a fetch by URL), sync of a
two-file delta, run, a timeout that killed the remote job, and clean. A full pdfl18 build there
through the pipeline has not been run yet.

Decisions that differ from the sections above:

The `linux-armv8` machines are the `apdfl-armv8-rocky9` Jenkins agents, reached as the
operator reaches them, `rocky@` with `~/.ssh/engineering-dl.pem`. That login is shared and has
passwordless sudo; section 9 covers what that means. `max_concurrent_builds: 1` leaves CI room,
and the root and checkout are in the operator's folder (`/home/rocky/robb`). The checkout is the
operator's clone of their fork; base commits come from `fetch_url` (default
`git@github.com:<repo.github>.git`).

Where the implementation goes beyond the sections above:

- **The node script is installed or upgraded automatically** when a task is pinned and the
  machine's copy is missing or older. It is a file in `root`, written with the operator's login,
  and `pinned` records the script version in the audit log.
- **`build.local_setup` runs only for pinned tasks.** A local task runs `build.setup` alone, which
  should cover it, so pdfl18 doesn't run `mkenv` twice locally.
- **The node script kills its job on SIGTERM, SIGHUP, and SIGINT**, as well as on stdin EOF. A
  dropped connection can arrive as either, and the orchestrator's own timeout stops the local end
  with SIGTERM.

Not done yet:

- Triggers (a) and (b): TRIAGING, `needs_platform`, and ESCALATING.
- R2: the agent's `orchestrator-build` helper, the deny list, and the share consistency wait. The
  worker can't build on the machine while it investigates; only the orchestrator's gates run there.
- `resume --repin`, a platform choice on the web page's Runs form, a Platform column on the
  Agents tab, and `orchestrator clean` reaching the machines.
- Reading the operation from `SSH_ORIGINAL_COMMAND`, for an operator who restricts their key
  with `command=` (section 9).
