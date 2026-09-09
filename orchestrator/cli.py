"""Command line: init, doctor, run, status, resume, clean, show-prompt."""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from orchestrator import __version__
from orchestrator.config.loader import ConfigError, load_config
from orchestrator.config.schema import Config

HELP = """\
Turns Jira issues into reviewed GitHub pull requests using configurable agent runtimes.

**How a run works.** For each issue key you pass, the orchestrator fetches the issue and its
Confluence context, creates a git worktree on a new branch, hands the work to the configured
worker agent, builds the project, runs a selected subset of tests, commits, has the configured
reviewer agent review the change, feeds blocking findings back to the worker for a bounded
number of fix rounds, pushes, opens a draft pull request, and reports back to Jira and
Confluence. Work that cannot be completed produces a findings report instead of a PR.

**Inputs.**

- *Issue keys* as positional arguments to `run`: `PROJ-123`. An epic key expands to its
  children, ordered by their "is blocked by" links.
- *A YAML config file*, `orchestrator.yaml` in the current directory unless `--config` says
  otherwise. `init` writes a commented example; `config-reference` lists every accepted key.
- *Secrets* are never in the file. Each `auth:` block names an environment variable or a
  `~/.netrc` machine, resolved at startup.

**Outputs.** Each run writes `<state_dir>/runs/<run-id>/` with prompts, agent transcripts,
build and test logs, the reviewed diff, the PR body, `findings.md` when blocked, and
`audit.jsonl` recording every external side effect. `status` and `resume` read the SQLite
checkpoints in `<state_dir>/state.db`.

**Typical session.** `init` → edit the YAML → `doctor` → `run PROJ-123 --dry-run` →
`run PROJ-123`.
"""

EPILOG = """\
Exit codes: 0 success; 1 at least one task blocked or failed, or doctor found problems;
2 bad arguments or invalid configuration; 130 interrupted (use `resume`).

Every command accepts `--help`. Documentation: README.md and docs/design-plan.md in the repository.
"""

app = typer.Typer(
    name="orchestrator",
    help=HELP,
    epilog=EPILOG,
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="markdown",
)

CONFIG_OPTION = typer.Option(
    Path("orchestrator.yaml"),
    "--config",
    "-c",
    help="YAML configuration file. See `orchestrator config-reference` for every key.",
    show_default=True,
)

KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
console = Console()
err = Console(stderr=True)

DEFAULT_CONFIG = Path("orchestrator.yaml")
REPO_ROOT = Path(__file__).resolve().parent.parent

EXAMPLE_CONFIG = """\
# Orchestrator configuration. One file per target repository.
# Secrets are never written here: name an environment variable (token_env) or a
# ~/.netrc machine (netrc_machine) and the orchestrator resolves it at startup.
version: 1

tracker:
  kind: jira
  base_url: https://example.atlassian.net
  project: PROJ
  auth:
    netrc_machine: example.atlassian.net      # login = account email, password = API token
  statuses:
    in_progress: "In Progress"
    in_review: "In Review"
    blocked: "Blocked"
  comment_on: [started, pr_opened, blocked, failed]

confluence:
  base_url: https://example.atlassian.net/wiki
  auth:
    netrc_machine: example.atlassian.net
  context_pages:                               # read-only reference for both agents
    - https://example.atlassian.net/wiki/spaces/ENG/pages/123456/Coding+Standards
  publish:
    space: ENG
    parent_page_id: "789012"
    when: [blocked, completed]

repo:
  github: your-org/your-repo
  base_branch: develop
  clone_path: ~/development/your-repo          # existing clone; worktrees are created elsewhere
  worktree_root: ~/development/.orchestrator/worktrees
  branch_template: "agent/{key}-{slug}"
  pr:
    draft: true
    labels: [agent-generated]
    reviewers: []
    title_template: "{key}: {summary}"
  auth:
    token_env: GITHUB_TOKEN                    # used only by the orchestrator, never by agents

shares:
  support:
    paths: {darwin: /Volumes/support, linux: /support}
    expect_read_only_mount: true
  raid:
    paths: {darwin: /Volumes/raid, linux: /raid}
    write_under: [agent-drops]

mcp:
  sources:                                     # where each CLI already keeps MCP definitions
    claude-code: [~/.claude.json, .mcp.json]
    codex: [~/.codex/config.toml]
  deny_tools:
    mcp-jenkins: [triggerBuild, rebuildBuild, replayBuild, updateBuild]

build:
  setup: []                                    # e.g. ["python mkenv.py"] for a Python target
  commands: []                                 # e.g. ["make -j8 debug"]
  timeout_minutes: 40
  env: {}
  max_concurrent_builds: 1

test:
  timeout_minutes: 20
  selection:
    strategy: changed-paths                    # changed-paths | named-suite | agent-chosen
    map:
      "src/": ["pytest tests -q"]
    fallback: ["pytest tests/smoke -q"]
    max_commands: 3
  full_suite: ["pytest -q"]                    # never run by the pipeline; noted in the PR

agents:
  review_rounds: 2
  prompt_overrides: {}
  worker:
    runner: claude-code
    model: claude-fable-5-1
    access: workspace-write
    timeout_minutes: 90
    max_turns: 200
    max_budget_usd: 15
    auth:
      token_env: ANTHROPIC_API_KEY
    shares:
      support: read
      raid: read-write
    mcp_servers: [mcp-jenkins, ragflow]
    options:
      effort: high
  reviewer:
    runner: codex
    model: gpt-5.4
    access: read-only
    timeout_minutes: 30
    max_budget_usd: 5
    auth:
      token_env: OPENAI_API_KEY
    shares:
      support: read
      raid: read
    mcp_servers: [ragflow]
    options:
      reasoning_effort: high

scheduler:
  max_parallel: 3
  retry_infra_failures: 2

hooks:
  after_worktree: []
  before_pr: []
"""


def _load(config: Path) -> Config:
    try:
        return load_config(config)
    except ConfigError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None


def _version(value: bool) -> None:
    if value:
        console.print(f"orchestrator {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    pass


@app.command()
def init(
    path: Path = typer.Argument(
        DEFAULT_CONFIG, help="Where to write the example config. Refuses to overwrite an existing file."
    ),
) -> None:
    """Write a commented example configuration to start from.

    The example covers every section: tracker, confluence, repo, shares, mcp, build, test,
    agents, scheduler, and hooks. Edit it, then run `doctor`.
    """
    if path.exists():
        err.print(f"[red]{path} already exists[/red]")
        raise typer.Exit(1)
    path.write_text(EXAMPLE_CONFIG)
    console.print(f"Wrote {path}. Edit it, then run [bold]orchestrator doctor --config {path}[/bold].")


@app.command()
def doctor(
    config: Path = CONFIG_OPTION,
    offline: bool = typer.Option(
        False, "--offline", help="Skip Jira, GitHub, and Confluence connectivity checks."
    ),
) -> None:
    """Check that everything a run needs is in place.

    Verifies the interpreter and venv, git and gh, that every secret reference resolves,
    each role's agent CLI (binary, minimum version, which limits it enforces natively),
    repository paths, build commands, share mounts and write roots, MCP sources, and,
    unless `--offline`, connectivity to Jira, GitHub, and Confluence. Exit 1 when anything fails.
    """
    from orchestrator.doctor import doctor_sync

    cfg = _load(config)
    checks = doctor_sync(cfg, REPO_ROOT, online=not offline)
    table = Table(title=f"doctor: {config}")
    table.add_column("area")
    table.add_column("")
    table.add_column("detail")
    colors = {"ok": "green", "warn": "yellow", "fail": "red"}
    for c in checks:
        table.add_row(c.area, f"[{colors[c.status]}]{c.status}[/{colors[c.status]}]", c.detail)
    console.print(table)
    fails = sum(1 for c in checks if c.status == "fail")
    warns = sum(1 for c in checks if c.status == "warn")
    console.print(f"{fails} problem(s), {warns} warning(s)")
    raise typer.Exit(1 if fails else 0)


def _progress_table(states: dict[str, tuple[str, str, float]]) -> Table:
    table = Table(title="orchestrator run")
    table.add_column("issue")
    table.add_column("stage")
    table.add_column("elapsed", justify="right")
    table.add_column("note")
    for key, (stage, note, started) in states.items():
        color = {"DONE": "green", "BLOCKED": "yellow", "FAILED": "red", "AWAITING_APPROVAL": "magenta"}.get(
            stage, "cyan"
        )
        table.add_row(key, f"[{color}]{stage}[/{color}]", f"{int(time.monotonic() - started)}s", note[:80])
    return table


async def _run(
    cfg: Config,
    config: Path,
    keys: list[str],
    *,
    dry_run: bool,
    keep_worktrees: bool,
    resume_id: str | None,
    show_prompt: bool,
    retry_failed: bool = False,
    only: list[str] | None = None,
    workflow: str | None = None,
    approve: str | None = None,
    revise: str | None = None,
    decisions: str = "",
) -> int:
    from orchestrator.intake.base import ExplicitKeys
    from orchestrator.pipeline import feature
    from orchestrator.pipeline.runtime import build_runtime
    from orchestrator.pipeline.scheduler import run_all
    from orchestrator.reporting.run_report import run_report_markdown, write_run_report

    rt = build_runtime(cfg, config, run_id=resume_id, dry_run=dry_run, keep_worktrees=keep_worktrees)
    existing = {}
    if resume_id:
        row = rt.store.get_run(resume_id)
        if row is None:
            err.print(f"[red]no run {resume_id}[/red]")
            return 2
        keys = keys or row.keys
        existing = rt.store.load_tasks(resume_id)
        for key, action in ((approve, "approve"), (revise, "revise")):
            if not key:
                continue
            task = existing.get(key)
            if task is None:
                err.print(f"[red]{key} is not part of run {resume_id}[/red]")
                return 2
            try:
                if action == "approve":
                    feature.approve(task, decisions)
                else:
                    feature.revise(task, decisions, cfg.workflows.feature.spec_revisions)
            except ValueError as e:
                err.print(f"[red]{e}[/red]")
                return 2
            rt.store.save_task(resume_id, task)
            rt.audit.record(action, key, decisions=decisions, state=task.state)
        if only:
            keys = [k for k in keys if k in only]
            existing = {k: t for k, t in existing.items() if k in only}
        if retry_failed:
            from orchestrator.pipeline.task import TERMINAL

            for t in existing.values():
                if t.state == "FAILED":
                    previous = [s for _, s in t.history if s not in TERMINAL]
                    t.outcome, t.error = "", None
                    t.transition(previous[-1] if previous else "CONTEXT")  # type: ignore[arg-type]
                    rt.store.save_task(resume_id, t)
        dry_run = dry_run or row.dry_run
        rt.dry_run = dry_run
    if not resume_id:
        rt.store.create_run(rt.run_id, config, keys, dry_run)
    console.print(f"run [bold]{rt.run_id}[/bold] -> {rt.run_dir}" + (" (dry run)" if dry_run else ""))
    try:
        specs = await ExplicitKeys(rt.tracker, keys).tasks()
        if show_prompt:
            from orchestrator.pipeline import stages
            from orchestrator.pipeline.task import TaskState
            from orchestrator.scm.worktree import Worktree

            for spec in specs:
                task = TaskState(key=spec.key, summary=spec.issue.summary)
                await stages.stage_context(rt, spec, task)
                fake = Worktree(rt.task_dir(spec.key), "preview", cfg.repo.base_branch)
                ctx_src = rt.task_dir(spec.key) / "context"
                dst = fake.path / ".orchestrator" / "context"
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copytree(ctx_src, dst, dirs_exist_ok=True)
                console.rule(f"worker prompt for {spec.key}")
                console.print(
                    rt.render("worker.md", **stages._prompt_common(rt, spec, task, fake, "worker")),
                    markup=False,
                )
            return 0
        states: dict[str, tuple[str, str, float]] = {s.key: ("QUEUED", "", time.monotonic()) for s in specs}

        with Live(_progress_table(states), console=console, refresh_per_second=2) as live:

            def on_event(key: str, state: str, note: str) -> None:
                started = states.get(key, ("", "", time.monotonic()))[2]
                states[key] = (state, note, started)
                live.update(_progress_table(states))

            rt.on_event = on_event
            results = await run_all(rt, specs, existing, workflow=workflow)  # type: ignore[arg-type]
            live.update(_progress_table(states))
        if not any(r.paused for r in results):
            rt.store.finish_run(rt.run_id)
        report = write_run_report(rt.run_dir, rt.run_id, results, dry_run)
        console.print(run_report_markdown(rt.run_id, results, dry_run), markup=False)
        console.print(f"report: {report}")
        for r in results:
            if r.paused:
                console.print(
                    f"[magenta]{r.key}[/magenta] is waiting for approval of its specification: "
                    f"{rt.task_dir(r.key) / 'spec.md'}\n  approve: orchestrator resume {rt.run_id} --approve {r.key} "
                    f"[--decisions FILE]\n  send back: orchestrator resume {rt.run_id} --revise {r.key} --decisions FILE"
                )
        if all(r.state == "DONE" for r in results):
            return 0
        if all(r.state == "DONE" or r.paused for r in results):
            return 3
        return 1
    finally:
        await rt.aclose()


@app.command()
def run(
    keys: list[str] = typer.Argument(
        None,
        metavar="KEY...",
        help="One or more Jira issue or epic keys, e.g. PROJ-123 EPIC-7. Case-insensitive. "
        "An epic expands to its child issues.",
    ),
    config: Path = CONFIG_OPTION,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run the agents, build, test, commit, and review locally, but do not push, open a PR, "
        "or write to Jira or Confluence. Worktrees are kept.",
    ),
    keep_worktrees: bool = typer.Option(
        False,
        "--keep-worktrees",
        help="Keep worktrees of completed tasks (blocked and failed ones are always kept).",
    ),
    max_parallel: int | None = typer.Option(
        None, "--max-parallel", min=1, help="Issues processed at once; overrides scheduler.max_parallel."
    ),
    show_prompt: bool = typer.Option(
        False,
        "--show-prompt",
        help="Fetch the issue and Confluence context, print the rendered worker prompt, and exit without "
        "creating a worktree or running an agent.",
    ),
    workflow: str = typer.Option(
        "auto",
        "--workflow",
        help="auto: route by Jira issue type (workflows.feature_issue_types). bugfix: implement, build, test, "
        "review, PR. feature: specify, pause for approval, write failing tests (red commit), implement (green "
        "commit), review against the specification, PR.",
        show_default=True,
    ),
) -> None:
    """Process the given issues through to pull requests.

    Progress is shown live, one row per issue. When all tasks finish, a run report is printed
    and saved under the run directory. Exit 0 only when every task reached DONE; a blocked
    task leaves a findings.md and exits 1; exit 3 means every remaining task is a feature
    waiting for its specification to be approved with `resume --approve`.
    """
    if workflow not in ("auto", "bugfix", "feature"):
        err.print("[red]--workflow must be auto, bugfix, or feature[/red]")
        raise typer.Exit(2)
    if not keys:
        err.print("[red]give at least one issue or epic key, e.g. PROJ-123[/red]")
        raise typer.Exit(2)
    bad = [k for k in keys if not KEY_PATTERN.match(k.upper())]
    if bad:
        err.print(f"[red]not Jira keys: {', '.join(bad)}; expected the form PROJ-123[/red]")
        raise typer.Exit(2)
    cfg = _load(config)
    if max_parallel:
        cfg = cfg.model_copy(
            update={"scheduler": cfg.scheduler.model_copy(update={"max_parallel": max_parallel})}
        )
    code = asyncio.run(
        _run(
            cfg,
            config,
            [k.upper() for k in keys],
            dry_run=dry_run,
            keep_worktrees=keep_worktrees,
            resume_id=None,
            show_prompt=show_prompt,
            workflow=None if workflow == "auto" else workflow,
        )
    )
    raise typer.Exit(code)


@app.command()
def resume(
    run_id: str = typer.Argument(
        ..., help="Run id as shown by `orchestrator status`, e.g. 20260903-141500-a1b2c3."
    ),
    config: Path = CONFIG_OPTION,
    keep_worktrees: bool = typer.Option(False, "--keep-worktrees", help="Keep worktrees of completed tasks."),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Also re-run FAILED tasks, from the stage they failed in (an orchestrator fix has been applied).",
    ),
    only: list[str] = typer.Option(
        None, "--only", help="Restrict to these issue keys; repeatable. Others in the run are untouched."
    ),
    approve: str | None = typer.Option(
        None,
        "--approve",
        metavar="KEY",
        help="Approve the specification of a feature task that is AWAITING_APPROVAL; it goes on to write "
        "its failing tests. Combine with --decisions to attach answers and instructions the worker must follow.",
    ),
    revise: str | None = typer.Option(
        None,
        "--revise",
        metavar="KEY",
        help="Send a feature task's specification back to be rewritten. Requires --decisions saying what to change.",
    ),
    decisions: Path | None = typer.Option(
        None,
        "--decisions",
        help="Markdown file with the approver's answers to the specification's questions and any binding "
        "instructions. Recorded on the task, shown to the worker and the reviewer, and quoted in the PR.",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
) -> None:
    """Continue an interrupted run from each task's last checkpoint.

    Tasks already DONE or BLOCKED are left alone; the others pick up at the stage they were
    in. FAILED tasks are also left alone unless --retry-failed is given. A feature task waiting
    at AWAITING_APPROVAL stays there unless --approve or --revise names it. A run started with
    --dry-run stays a dry run.
    """
    cfg = _load(config)
    if revise and decisions is None:
        err.print("[red]--revise needs --decisions FILE with what to change[/red]")
        raise typer.Exit(2)
    if approve and revise:
        err.print("[red]give --approve or --revise, not both[/red]")
        raise typer.Exit(2)
    code = asyncio.run(
        _run(
            cfg,
            config,
            [],
            dry_run=False,
            keep_worktrees=keep_worktrees,
            resume_id=run_id,
            show_prompt=False,
            retry_failed=retry_failed,
            only=[k.upper() for k in only] if only else None,
            approve=approve.upper() if approve else None,
            revise=revise.upper() if revise else None,
            decisions=decisions.read_text() if decisions else "",
        )
    )
    raise typer.Exit(code)


@app.command()
def status(
    run_id: str | None = typer.Argument(
        None, help="Show one run's tasks; omit to list the 20 most recent runs."
    ),
    config: Path = CONFIG_OPTION,
) -> None:
    """List recent runs, or the state, rounds, cost, and result of each task in one run."""
    from orchestrator.state.store import Store

    cfg = _load(config)
    store = Store(cfg.db_path)
    if run_id is None:
        table = Table(title="runs")
        for col in ("run id", "started", "finished", "keys", "dry run"):
            table.add_column(col)
        for r in store.list_runs():
            table.add_row(r.run_id, r.started, r.finished or "", " ".join(r.keys), "yes" if r.dry_run else "")
        console.print(table)
        return
    table = Table(title=f"run {run_id}")
    for col in ("issue", "workflow", "state", "rounds", "cost", "pr / error"):
        table.add_column(col)
    for t in store.load_tasks(run_id).values():
        note = t.pr_url or t.error or ""
        if t.paused:
            note = f"spec awaiting approval: resume {run_id} --approve {t.key}"
        table.add_row(t.key, t.workflow, t.state, str(t.round), f"${t.cost_usd:.2f}", note)
    console.print(table)


def _parse_age(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([dhm])", text)
    if not m:
        raise typer.BadParameter("use a number followed by d, h, or m, e.g. 7d")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"d": "days", "h": "hours", "m": "minutes"}[unit]: n})


@app.command()
def clean(
    older_than: str = typer.Option(
        "7d", "--older-than", help="Age threshold: a number followed by d (days), h (hours), or m (minutes)."
    ),
    config: Path = CONFIG_OPTION,
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Remove run directories and worktrees older than the threshold.

    Lists what will be removed and asks first unless --yes is given. The SQLite run history
    is kept.
    """
    from orchestrator.scm.worktree import WorktreeManager

    cfg = _load(config)
    cutoff = datetime.now(UTC) - _parse_age(older_than)
    victims: list[Path] = []
    for base in (cfg.runs_dir, cfg.repo.worktree_root):
        if not base.exists():
            continue
        for p in base.iterdir():
            if datetime.fromtimestamp(p.stat().st_mtime, UTC) < cutoff:
                victims.append(p)
    if not victims:
        console.print("nothing to clean")
        return
    for v in victims:
        console.print(f"  {v}")
    if not yes and not typer.confirm(f"Remove {len(victims)} item(s)?"):
        raise typer.Exit(1)
    manager = WorktreeManager(cfg.repo)

    async def go() -> None:
        for v in victims:
            if v.parent == cfg.repo.worktree_root:
                await manager.remove(v, force=True)
            else:
                shutil.rmtree(v, ignore_errors=True)

    asyncio.run(go())
    console.print(f"removed {len(victims)} item(s)")


@app.command()
def conformance(
    runner: str = typer.Argument(
        ..., help="Runner name, e.g. claude-code, codex, gemini-cli, opencode, hermes."
    ),
    config: Path = CONFIG_OPTION,
    role: str = typer.Option(
        "worker", "--role", help="Which role's model, auth, limits, and options to use: worker or reviewer."
    ),
    keep: bool = typer.Option(False, "--keep", help="Keep the temporary files for inspection."),
) -> None:
    """Drive a real agent runtime through a canned task to prove its adapter works. Spends tokens.

    The task reads a file, edits it, returns JSON matching a small schema, and resumes once
    when the adapter claims session resume. Each step is reported as pass or fail.
    """
    from orchestrator.agents.conformance.kit import run_conformance
    from orchestrator.agents.registry import get_runner
    from orchestrator.config.loader import resolve_secret

    cfg = _load(config)
    role_cfg = cfg.agents.role(role)  # type: ignore[arg-type]
    if role_cfg.runner != runner:
        role_cfg = role_cfg.model_copy(update={"runner": runner})
    api_key = ""
    if not role_cfg.auth.use_cli_login:
        try:
            api_key = resolve_secret(role_cfg.auth)
        except Exception as e:  # noqa: BLE001
            err.print(f"[red]{e}[/red]")
            raise typer.Exit(2) from None
    report = asyncio.run(run_conformance(get_runner(runner), role_cfg, api_key, keep=keep))
    table = Table(title=f"conformance: {runner}")
    table.add_column("step")
    table.add_column("")
    table.add_column("detail")
    for name, passed, detail in report.steps:
        table.add_row(name, "[green]pass[/green]" if passed else "[red]fail[/red]", detail)
    console.print(table)
    raise typer.Exit(0 if report.ok() else 1)


@app.command("config-reference")
def config_reference(
    fmt: str = typer.Option(
        "table", "--format", help="table for the terminal, markdown for a document.", show_default=True
    ),
    section: str | None = typer.Option(
        None, "--section", help="Only keys under this top-level section, e.g. agents or shares."
    ),
) -> None:
    """Document every key the YAML configuration accepts, with type, default, and meaning.

    Generated from the schema, so it is always in step with what the loader validates.
    """
    from orchestrator.config.reference import as_markdown, as_text

    if fmt == "markdown":
        text = as_markdown()
        if section:
            text = "\n".join(
                line
                for line in text.splitlines()
                if not line.startswith("| `") or line.startswith(f"| `{section}")
            )
        console.print(text, markup=False, highlight=False)
        return
    if fmt != "table":
        raise typer.BadParameter("--format must be table or markdown")
    table = Table(title="configuration keys", show_lines=False)
    table.add_column("key", no_wrap=True)
    table.add_column("type")
    table.add_column("default")
    table.add_column("description")
    for e in as_text():
        if section and not e.path.split(".")[0] == section:
            continue
        key = ("  " * e.depth) + e.path.rsplit(".", 1)[-1]
        if e.required:
            key = f"[bold]{key}[/bold]"
        table.add_row(key, e.type, e.default, e.description)
    console.print(table)
    console.print("Bold keys are required. Indentation shows nesting; `<name>` marks a user-chosen key.")


def main() -> None:
    try:
        app(prog_name="orchestrator")
    except KeyboardInterrupt:
        err.print("interrupted; use `orchestrator resume <run-id>` to continue")
        sys.exit(130)


if __name__ == "__main__":
    main()
