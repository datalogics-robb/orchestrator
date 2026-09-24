# orchestrator

Turns Jira issues into reviewed GitHub PRs: a worker agent implements each issue in its own git
worktree, the orchestrator builds and tests it, a reviewer agent from another vendor reviews it,
and a draft PR is opened. `README.md` covers setup and use; `docs/design-plan.md` is the design
and rationale. Read it before changing security, isolation, or adapter behaviour.

## Commands

The venv is created by mkenv (`python mkenv.py`) as `python-env-<hostname>`; nothing needs
activating.

```bash
python-env-*/bin/pytest                          # ~10s, fake agents, no tokens spent
python-env-*/bin/ruff format . && python-env-*/bin/ruff check .
python-env-*/bin/mypy orchestrator
bin/orchestrator --help                          # runs the package from the venv
```

`mkenv.py` is a vendored copy of the `datalogics/mkenv` bootstrapper. Ruff reports a format
diff and two lint errors in it; leave them, and don't edit the file except to replace it with a
newer upstream copy.

Never run `bin/orchestrator run` without `--dry-run`, or `conformance`, or `doctor` without
`--offline`, unless asked: they push branches, open PRs, write to Jira/Confluence, or spend
tokens. `orchestrator.yaml` at the repo root is the user's local, gitignored config;
`configs/pdfl18_all.yaml` is the checked-in real configuration.

## Layout

- `cli.py` – typer app: `init`, `doctor`, `run`, `status`, `resume`, `clean`, `conformance`,
  `config-reference`. Also holds the long `--help` text and the commented example YAML `init`
  writes.
- `config/schema.py` – pydantic models for the YAML. `loader.py` loads it, rejects anything
  shaped like a secret, and resolves `auth:` blocks (env var, `~/.netrc`, or CLI login).
  `reference.py` renders `config-reference` from the field descriptions.
- `pipeline/` – the state machine.
  - `task.py`: `TaskState` (the checkpoint) and the `State` literal. Raise `Blocked` when the
    work item can't be done (produces a findings report), `Failed` for infrastructure problems
    (`transient=True` is retried).
  - `stages.py` (bugfix path) and `feature.py` (spec → approval → red → green): each stage is
    `async def stage_x(rt, spec, task) -> State` returning the next state, registered in the
    module's `STAGES` dict.
  - `scheduler.py`: drives tasks, saving state after every transition; handles parallelism,
    dependency order, and retries.
  - `runtime.py`: `Runtime`, built once per run: config, adapters, secrets, trackers, the store,
    the audit log, and the jinja environment for prompts.
- `agents/`
  - `base.py`: the runtime-independent `AgentRunner` protocol, `AgentRequest` and
    `AgentResult`, `Capabilities`, the optional `LiveCheck` protocol that `doctor` probes, and
    `run_process`.
  - `runners/`: one adapter per CLI (claude-code, codex, gemini-cli, opencode, hermes). New
    runners go in both `registry._BUILTIN` and the `orchestrator.runners` entry points in
    `pyproject.toml`.
  - `contracts.py`: the pydantic output contracts agents must return. Codex needs
    `strict_schema()` (OpenAI strict mode), which `tests/test_strict_schema.py` checks.
  - `prompts/*.md`: jinja templates rendered with `StrictUndefined`.
  - `hooks/claude_pretool.py`: runs inside Claude Code as a hook, so it must stay
    standard-library only.
- `scm/` (git, worktrees, GitHub via `gh`, pre-commit gate), `trackers/` (Jira, ADF
  conversion), `docs/confluence.py`, `mcp/passthrough.py` (reads each CLI's own MCP config and
  forwards only allowlisted servers), `shares/` (grants plus the `orchestrator-cp` entry point),
  `state/store.py` (SQLite checkpoints), `reporting/` (the `audit.jsonl` log with redaction, PR
  and findings Markdown), `build/` (build semaphore, test selection), `environment.py` (the
  scrubbed env for agent and build processes).

## Conventions

- **Config keys:** models are `StrictModel` (`extra="forbid"`, frozen), and every field needs
  a `description=`, since `config-reference` is generated from them. When you add or change a
  key, also update the example YAML in `cli.py` `init`, and the README or design plan if they
  describe it.
- **Dependencies:** add them to both `requirements.in` (what mkenv installs) and
  `pyproject.toml`.
- **Secrets:** never in YAML, logs, or prompts. Secrets go through `Redactor` and the audit
  log; agent processes get the env from `environment.build_env`, not `os.environ`.
- **Side effects:** every external side effect (Jira, GitHub, Confluence, share copy) is
  recorded with `rt.audit.record(...)`, and is skipped under `rt.dry_run`. A failed Jira or
  Confluence write-back must never fail the task: catch it and audit it.
- **Tests:** pipeline tests (`tests/test_pipeline.py`) build a temp git repo with a bare
  origin and drive the whole state machine using `tests/fakes.py`. Queue agent outputs with
  `fakes.SCRIPT[role]` and inspect `fakes.CALLS[role]`. Adapter tests stub the CLI binaries.
  New behaviour gets a test here rather than a live run.
- **Style:** Python 3.13, ruff at line length 110, and full type hints with mypy clean.
  Docstrings and comments state the rule the code enforces, not how it was discovered.
- **Commits:** an imperative, sentence-case subject with no type prefix (a component prefix
  like `Codex:` is fine). The body explains why, wrapped at about 80 columns.
