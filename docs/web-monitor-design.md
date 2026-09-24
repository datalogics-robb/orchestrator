# Web Monitor and Control: Design

Status: proposal, 2026-09-24. Not implemented. This extends `design-plan.md` and changes two of
the decisions in its section 2: intake is no longer CLI-only, because the daemon accepts runs
over HTTP, and deployment is no longer a foreground CLI only. It keeps the design plan's
single-user model.

Settled on 2026-09-24:

- There is one daemon per config, meaning one per target repository.
- One user, the *operator*, controls the daemon. The daemon runs under that user's OS account.
- Every process the daemon starts must be able to use the operator's Claude subscription
  login (section 8).

## 1. Goal

Run the orchestrator as a long-lived background process that serves HTTP on a configurable
port. A browser shows a main page with tabs. The first tab, **Agents**, is a table with the
current status of every agent. It refreshes every 5 seconds. Later tabs add control: starting
runs, approving specifications, cancelling, and retrying.

The CLI keeps working as it does now. A run started with `orchestrator run` in a terminal also
appears on the Agents tab.

## 2. Decisions

| Topic | Decision | Why |
|---|---|---|
| Process | New `orchestrator serve` command. It runs in the foreground by default; `--detach` starts it in the background. | A foreground process is what launchd and systemd expect, and is easy to debug. `--detach` covers the quick "leave it running" case. |
| Where runs execute | Inside the daemon's asyncio loop, one `Runtime` per run, driven by the existing `run_all`. | The pipeline is already async and checkpointed. Running in-process makes cancel and approve direct calls, not signals to another process. |
| Where status comes from | SQLite (`state.db`), with a new `agents` table written around every agent invocation. | The daemon, CLI runs, and a restarted daemon all see the same data. The page never depends on in-memory state that a crash would lose. |
| HTTP stack | FastAPI on uvicorn, started on the same event loop as the runs. | Typed JSON responses from pydantic models, which are already used throughout. FastAPI's test client uses `httpx`, which is already a dependency. |
| Front end | One static HTML page with plain JavaScript and CSS, shipped as package data. No build step and no CDN. | Tabs and a polled table don't need a framework, and the page must load on machines without internet access. |
| Live updates | The browser polls `GET /api/agents` every 5 s and pauses while its tab is hidden. | This matches the requirement exactly and has no server-side connection state. Server-sent events can replace polling later without changing the API's data. |
| Exposure | Binds to `127.0.0.1:8765` by default. Binding any other address requires the operator's token. | "Open port" has to be something you opt into. The control endpoints can push code and spend money. |
| Scope | One daemon per config. The pidfile and database live in that config's `state_dir`. | Matches "one YAML per repo". Two repositories means two daemons on two ports. |
| Control | One operator, holding one token. Other people can be given read-only access. | There's a single user, so there's no user model, no roles, and no login page. |
| Identity | The daemon runs as the operator's own OS user, in their login session. Never as root, a service account, or a system-wide daemon. | The Claude subscription login belongs to that user's account and login session (section 8). |

## 3. Process model

```
orchestrator serve --config orchestrator.yaml [--host H] [--port P] [--detach]
        │
        ▼
 one asyncio loop
 ├── uvicorn server ─────── /  (static page)   /api/*  (JSON)
 ├── RunManager ─────────── asyncio.Task per run → run_all(rt, specs, ...)
 │     └── global limits: max_concurrent_runs, one BuildSemaphore for the process
 └── heartbeat ──────────── every 15 s: UPDATE runs SET heartbeat=now WHERE owner = this daemon
```

- **Single instance.** On start, `serve` takes an exclusive `flock` on `<state_dir>/serve.pid`
  and writes its pid, host, and port there. A second `serve` on the same state directory
  exits with an error naming the running one.
- **Detach.** `--detach` re-executes itself as
  `sys.executable -m orchestrator serve ...` with `start_new_session=True` and output going to
  `<state_dir>/serve.log`. It waits until `GET /api/health` answers, prints the URL, and
  exits. This avoids `fork()` after imports, which is unreliable on macOS.
  `orchestrator serve --stop` sends SIGTERM to the pid in the pidfile.
  `orchestrator serve --status` prints the pid and URL.
- **Shutdown.** SIGTERM or SIGINT stops accepting HTTP requests, cancels the run tasks
  (`run_process` already kills each agent's process group on `CancelledError`), marks those
  runs `interrupted`, and exits. Every task is already checkpointed, so the runs can be
  resumed.
- **Restart.** On start, runs whose owner was this daemon but whose `finished` is NULL are
  listed as *interrupted* on the page. With `web.resume_on_start: true` they are resumed
  automatically. The default is false, so a restart never spends money unasked.
- **Service files.** `docs/` gets two per-user examples: a launchd **LaunchAgent**
  (`~/Library/LaunchAgents`, loaded into `gui/<uid>`) and a **systemd user unit**
  (`systemctl --user`, with `loginctl enable-linger` so it survives logout). System-level
  forms (a LaunchDaemon, or a system unit with `User=`) are not offered, because they break
  the subscription login (section 8). Each example runs `bin/orchestrator serve` in the
  foreground. Each one also sets `PATH`, so `claude`, `codex`, and `gh` resolve (launchd and
  systemd start processes with a minimal `PATH`), and sets `CLAUDE_CONFIG_DIR` if the operator
  uses one.
- **Startup checks.** The daemon runs `doctor`, including the live login probes, and refuses
  to start if anything fails (section 8).
- **No terminal UI.** The `rich.Live` table is CLI-only. In the daemon, `Runtime.on_event`
  writes structured log lines to `serve.log`.

## 4. Changes to existing code

These are preconditions, needed whether or not the page exists, because a daemon runs more
than one run in one process.

1. **`BuildSemaphore.configure` resets the semaphore** (`build/runner.py`). `build_runtime`
   calls it for every run, so starting a second run while the first holds permits creates a
   fresh semaphore and doubles build concurrency. Configure it once per process: in `serve`
   at startup, and in `_run` for the CLI. Leave it out of `build_runtime`.
2. **Concurrency limits.** `scheduler.max_parallel` stays per run. A new
   `web.max_concurrent_runs` (default 1) queues extra submissions, so a second run cannot
   quietly multiply the load.
3. **Run ownership.** Add three columns to `runs`: `owner` (`<host>:<pid>`), `heartbeat`,
   and `status` (`running`, `paused`, `interrupted`, `finished`). The CLI's `_run` sets them
   too. `resume` refuses a run whose owner's heartbeat is less than 60 s old. Today nothing
   stops two processes from resuming the same run at once.
4. **SQLite across processes.** `Store.__init__` enables `PRAGMA journal_mode=WAL` and
   `busy_timeout=5000`. The CLI and the daemon can then write the same database at the same
   time.
5. **Agent activity table.** `stages._run_agent` records every invocation:

   ```sql
   CREATE TABLE IF NOT EXISTS agents (
       id INTEGER PRIMARY KEY,
       run_id TEXT NOT NULL, key TEXT NOT NULL, role TEXT NOT NULL,
       label TEXT NOT NULL,              -- work, fix-2, review-1, spec, red, green ...
       runner TEXT NOT NULL, model TEXT,
       started TEXT NOT NULL, ended TEXT,
       ok INTEGER, termination TEXT,
       cost_usd REAL, turns INTEGER, session TEXT, error TEXT
   );
   CREATE INDEX IF NOT EXISTS agents_run ON agents (run_id, key, role);
   ```

   It inserts a row before `runner.run(request)` and updates it afterwards, including when
   the call raises: use `try/finally`, and record `termination='killed'` on cancellation.
   The rows sit beside the existing `agent_start` and `agent_end` audit records; the audit log
   remains the permanent record.
6. **Redaction on the way out.** `save_task` stores `TaskState` without redaction, and its
   `error` and `fix_reason` fields can contain tool output. The daemon builds a `Redactor`
   from every secret the config resolves, the same way `build_runtime` does. Every API
   response passes through `redact_obj`.

## 5. The Agents tab

### 5.1 What a row is

One row per **(run, issue, role)** for runs that are running, paused, or interrupted, plus
runs that finished within `web.recent_minutes` (default 60). A role with no invocation yet
still gets a row, so the table shows every task and not only the busy ones.

Each row's status comes from the latest `agents` row for that role and the task's current
state:

| Status | Rule |
|---|---|
| `running` | The latest invocation has `ended IS NULL` and the run's heartbeat is fresh. |
| `orphaned` | The latest invocation has `ended IS NULL` and the heartbeat is stale; the owning process died. |
| `waiting` | Not running, the task is not terminal, and this role acts next (for example the reviewer while the task is BUILDING). |
| `idle` | Not running, and the task is not terminal. |
| `needs approval` | The task is in `AWAITING_APPROVAL` or `RED_REVIEW` (worker row). |
| `done` / `blocked` / `failed` | The task is terminal. |

### 5.2 Layout

```
┌ orchestrator · pdfl18_all · daemon 41213 on robbs-macbook-pro:8765 · updated 10:42:05 ────┐
│ [ Agents ]  Runs   Approvals   Audit   Config                                             │
├───────────────────────────────────────────────────────────────────────────────────────────┤
│ Run          Issue      Role      Runtime / model        Activity   Status     Task stage  Elapsed  Turns  Cost  │
│ …141500-a1b2 APDFL-4411 worker    claude-code / default fix-2      ● running  FIXING      6m12s    41   $3.10 │
│ …141500-a1b2 APDFL-4411 reviewer  codex / default         review-1   ◌ waiting  FIXING      —        —    $0.42 │
│ …141500-a1b2 APDFL-4420 worker    claude-code / default spec       ◆ approve  AWAITING_…  —        18   $1.05 │
│ …093012-ff01 APDFL-4388 worker    claude-code / default work       ✓ done     DONE        —        77   $6.80 │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- Rows are sorted running, then orphaned, needs approval, waiting, idle, and finished; then by
  run and issue.
- Elapsed is `now − started` for a running agent, computed on the server, so the refresh
  interval doesn't matter. Cost is the task's `cost_usd`, the total for all its agents.
- Clicking the issue opens the Jira issue. A PR link appears when the task has one.
- A header line shows the daemon's pid, host, and port, the time of the last successful
  refresh, and a red "stale" marker if two refreshes in a row fail.

### 5.3 API

`GET /api/agents` returns this. The page makes no other call for this tab.

```json
{
  "generated_at": "2026-09-24T15:42:05Z",
  "daemon": {"pid": 41213, "host": "robbs-macbook-pro", "port": 8765, "version": "0.1.0"},
  "agents": [
    {
      "run_id": "20260924-141500-a1b2c3", "run_status": "running", "dry_run": false,
      "key": "APDFL-4411", "summary": "…", "role": "worker",
      "runner": "claude-code", "model": null,
      "label": "fix-2", "status": "running", "task_state": "FIXING", "round": 2,
      "started": "2026-09-24T15:35:53Z", "elapsed_seconds": 372,
      "turns": 41, "task_cost_usd": 3.10,
      "issue_url": "https://…/browse/APDFL-4411", "pr_url": null, "error": null
    }
  ]
}
```

The response model is a pydantic model in `web/api.py`. Its fields are the contract the page
relies on, and a unit test covers the status rules in 5.1.

### 5.4 Polling

```js
const REFRESH_MS = 5000;
async function refresh() {
  if (document.hidden) return;
  const r = await fetch("/api/agents", {cache: "no-store"});
  if (r.ok) renderAgents(await r.json()); else markStale();
}
setInterval(refresh, REFRESH_MS);
document.addEventListener("visibilitychange", refresh);
refresh();
```

`renderAgents` rebuilds `<tbody>` from the JSON. Each row is keyed by `run_id/key/role` so a
row that is still present keeps its place and scroll position isn't lost. Text goes in
through `textContent`, never `innerHTML`, because summaries and errors come from Jira and
agents.

## 6. Tabs after the first

These are listed so the first cut leaves room for them; none are part of it.

| Tab | Shows | Controls (`POST`, token required) |
|---|---|---|
| Runs | `runs` rows with status, owner, and task counts | `/api/runs` start (keys, workflow, dry-run); `/api/runs/{id}/cancel`; `/api/runs/{id}/resume` with `retry_failed` / `retry_blocked` |
| Approvals | Tasks in `AWAITING_APPROVAL` or `RED_REVIEW`, with `spec.md` or `red.md` rendered | `/api/runs/{id}/tasks/{key}/approve` and `/revise`, with a decisions text box (calls the existing `feature.approve` and `feature.revise`) |
| Audit | Recent `audit.jsonl` entries, filterable by run and issue | none |
| Config | The loaded config (secrets are never in it) and the last `doctor` result | `/api/doctor` re-run |

Control is only offered for runs the daemon owns. A run started from the CLI is read-only on
the page, because cancelling it would mean signalling another process.

## 7. Security

- **Bind address.** The default is `127.0.0.1`. When `web.host` is anything else, `web.auth`
  is required and `serve` refuses to start without it.
- **Auth.** There is exactly one credential, the operator's token. `web.auth` is an `AuthRef`
  (`token_env` or `netrc_machine`; `use_cli_login` is rejected), so the no-secrets-in-YAML rule
  holds. Requests use HTTP Basic with user `operator` and the token as password. The browser
  prompts once and `fetch` reuses the credentials, so no login page is needed. Tokens are
  compared with `hmac.compare_digest`.
- **Viewers.** `web.public_read: true` leaves the `GET` endpoints open, so colleagues on a
  trusted network can watch without the token. They can see issue keys, summaries, and
  redacted errors. `POST` endpoints always require the operator's token; no second token or
  role exists.
- **TLS.** Basic auth over plain HTTP exposes the token to anyone on the network path.
  `web.tls` accepts `certfile` and `keyfile` paths, which are passed to uvicorn. Without TLS
  on a non-loopback bind, `doctor` warns and the page shows a banner.
- **Browser attacks.** On a loopback bind, `Host` must be `localhost` or `127.0.0.1`, which
  blocks DNS rebinding. Control is `POST` with an `Authorization` header and no cookies, so
  there is nothing for cross-site request forgery to use. No CORS headers are sent.
- **Files.** The API never serves arbitrary paths. `spec.md`, `red.md`, and logs are served
  by (run, key, name) against a fixed allowlist and resolved under `runs_dir`.
- **Audit.** Every control action is recorded in the run's `audit.jsonl` with the client
  address. Only the operator can control the daemon, so every control action is theirs.

## 8. Claude subscription login

**Requirement.** Every process the daemon starts must be able to use the operator's Claude
subscription login. That includes every worker in a parallel run, whether the daemon was
started from a terminal, by `--detach`, or by the per-user service.

**How it works today.** When `use_cli_login` is set, `ClaudeCodeRunner` leaves
`CLAUDE_CONFIG_DIR` unset, or passes the operator's own value through. Every `claude` process
therefore reads the same stored login from the default config directory. The README explains
why an isolated config directory can't carry the login. `environment.build_env` keeps `HOME`
and `USER`, so a child process looks up the login as the operator. The daemon doesn't change
any of this; it only has to run somewhere the stored login can be reached.

**Where the login can be reached.** Verify this in W0.

| Platform | Stored in | Reachable from | Not reachable from |
|---|---|---|---|
| macOS | the operator's login keychain | processes in the operator's GUI login session: Terminal, `serve --detach` run from Terminal, a LaunchAgent in `gui/<uid>` | a LaunchDaemon, root, another user, and usually an SSH session (the keychain is locked or belongs to another security session) |
| Linux | `~/.claude/.credentials.json` | any process running as the operator with their `HOME` | other users and system units without the operator's `HOME` |

This is why section 2 says the daemon runs as the operator, in their session, and why the
service files are per-user only. On macOS, the machine must stay logged in to the operator's
account; a locked screen is fine, a logout is not. Codex's stored login (`~/.codex/auth.json`,
copied into each run) and `gh auth token` follow the same rule and work anywhere the Claude
login does.

**Proving it: a live check for Claude Code.** Today only the Codex adapter implements
`LiveCheck`. Add `ClaudeCodeRunner.check_live`. It sends one throwaway prompt through the same
argv, environment, and login path a run uses, with `--max-turns 1`, and reports the CLI's own
error message. The daemon then uses it in three places:

- **At startup.** A failed probe stops `serve` with the CLI's message, before the port opens.
- **Before each run.** RunManager probes again if the last success is more than 30 minutes
  old. On failure, the run stays queued rather than starting. Otherwise every task would fail
  in its first worker call after the context fetch and worktree setup had already been paid
  for.
- **On the page.** `GET /api/health` reports `claude_login: ok | failed (<message>) | unchecked`
  with the time of the last probe. The header shows a red banner when it fails.

**Recovering an expired login.** The operator logs in again in a terminal (`claude`, then
`/login`). No restart is needed: every agent process reads the stored login when it starts,
and the next probe clears the banner. The Runs tab (W2) gets a "re-check login" button that
calls `POST /api/health/probe`.

**Parallel workers.** With `max_parallel` of 3 or more, several `claude` processes share one
login and may refresh its token at the same moment. W0 includes a run with
`max_parallel: 3` against the real subscription to confirm that concurrent refreshes don't
invalidate each other. If they do, the fix belongs in the runner: serialize the start of
each Claude process behind a short lock, which leaves the running sessions unaffected.

## 9. Configuration

A new top-level `web:` section. Every field gets a `description=`, and the commented example
written by `init` gains the section.

```yaml
web:
  host: 127.0.0.1          # anything else requires auth
  port: 8765
  auth:                    # required unless host is loopback
    token_env: ORCHESTRATOR_WEB_TOKEN
  public_read: false
  tls: {certfile: ~/certs/orch.pem, keyfile: ~/certs/orch.key}   # optional
  max_concurrent_runs: 1
  recent_minutes: 60
  resume_on_start: false
```

`serve --host/--port` override the file.

## 10. Package layout and dependencies

```
orchestrator/web/
  __init__.py
  daemon.py      # serve entry: pidfile lock, detach, signals, heartbeat, uvicorn on the loop
  runs.py        # RunManager: submit/cancel/resume, queue under max_concurrent_runs
  api.py         # FastAPI app factory, auth dependency, response models, /api/*
  status.py      # agent-row derivation (section 5.1); pure functions over Store rows
  static/index.html, static/app.js, static/app.css
```

- Add `fastapi` and `uvicorn` to both `requirements.in` and `pyproject.toml`. Add
  `web/static/*` to `package-data`.
- `cli.py` gains a `serve` command, with `--detach`, `--stop`, and `--status`.
- `Store` gains the `agents` table and the ownership columns. It creates them with
  `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ... ADD COLUMN` guarded by a
  `PRAGMA table_info` check, so an existing `state.db` upgrades in place.

## 11. Tests

- `status.py`: table-driven tests of the row rules in 5.1, including orphaned (stale
  heartbeat) and needs approval.
- `api.py`: `TestClient` checks for the `/api/agents` shape, the auth-required-off-loopback
  rule, the `Host` check, and redaction of a planted secret in `task.error`.
- Pipeline: the existing fake-runner pipeline test also asserts that `agents` rows open and
  close, including the cancellation path.
- Daemon: one test starts `serve` on an ephemeral port against a temp state directory.
  It submits a dry run with the fake runners, polls `/api/agents` until the run is `done`,
  and then sends SIGTERM.
- Login: `check_live` for Claude Code, tested against a stub `claude` binary for the ok,
  not-logged-in, and error cases. A RunManager test checks that a failed probe leaves a run
  queued and sets `claude_login: failed` in `/api/health`.

## 12. Milestones

| # | Scope | Exit criterion |
|---|---|---|
| W0 | Preconditions from section 4: semaphore fix, WAL, ownership and heartbeat, `agents` table, resume refuses a live run. `ClaudeCodeRunner.check_live`, and verification of the login table and parallel token refresh in section 8 | The CLI behaves as before; a concurrent `resume` of a running run is refused; `doctor` probes the Claude login; a `max_parallel: 3` run on the subscription completes |
| W1 | `serve` (foreground and `--detach`), static page with tab bar, Agents tab, auth, `Host` check | A CLI dry run in another terminal shows up live on the Agents tab |
| W2 | RunManager and the Runs tab with start, cancel, and resume | A run started from the browser completes and can be cancelled mid-agent without leftover processes |
| W3 | Approvals tab | A feature task is approved from the browser with decisions and proceeds to red |
| W4 | Audit and Config tabs; LaunchAgent and systemd user-unit examples | After a reboot and the operator's login, the LaunchAgent starts the daemon, the Claude probe passes, and the interrupted runs are listed |

## 13. Open questions

None. The three questions from the first draft were settled on 2026-09-24:

- **Several configs:** one daemon per config.
- **Identity:** one operator; other people get read-only access at most.
- **The Claude login under a service:** it must work from every process. Section 8 says how,
  and W0 and W4 verify it.
