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

app = typer.Typer(
    name="orchestrator",
    help="Turns Jira issues into reviewed GitHub pull requests using configurable agent runtimes.",
    no_args_is_help=True,
    add_completion=False,
)
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
def init(path: Path = typer.Argument(DEFAULT_CONFIG, help="Where to write the example config.")) -> None:
    """Write a commented example configuration."""
    if path.exists():
        err.print(f"[red]{path} already exists[/red]")
        raise typer.Exit(1)
    path.write_text(EXAMPLE_CONFIG)
    console.print(f"Wrote {path}. Edit it, then run [bold]orchestrator doctor --config {path}[/bold].")


@app.command()
def doctor(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file."),
    offline: bool = typer.Option(
        False, "--offline", help="Skip Jira, GitHub, and Confluence connectivity checks."
    ),
) -> None:
    """Check the machine, credentials, adapters, shares, and MCP sources."""
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
        color = {"DONE": "green", "BLOCKED": "yellow", "FAILED": "red"}.get(stage, "cyan")
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
) -> int:
    from orchestrator.intake.base import ExplicitKeys
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
        dry_run = dry_run or row.dry_run
        rt.dry_run = dry_run
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
            results = await run_all(rt, specs, existing)
            live.update(_progress_table(states))
        rt.store.finish_run(rt.run_id)
        report = write_run_report(rt.run_dir, rt.run_id, results, dry_run)
        console.print(run_report_markdown(rt.run_id, results, dry_run), markup=False)
        console.print(f"report: {report}")
        return 0 if all(r.state == "DONE" for r in results) else 1
    finally:
        await rt.aclose()


@app.command()
def run(
    keys: list[str] = typer.Argument(None, help="Jira issue or epic keys."),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Do everything except push, open a PR, or write to Jira/Confluence."
    ),
    keep_worktrees: bool = typer.Option(False, "--keep-worktrees", help="Keep worktrees of completed tasks."),
    max_parallel: int | None = typer.Option(None, "--max-parallel", help="Override scheduler.max_parallel."),
    show_prompt: bool = typer.Option(
        False, "--show-prompt", help="Fetch context, print the worker prompt, and exit."
    ),
) -> None:
    """Process the given issues through to pull requests."""
    if not keys:
        err.print("[red]give at least one issue or epic key[/red]")
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
        )
    )
    raise typer.Exit(code)


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id from `orchestrator status`."),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    keep_worktrees: bool = typer.Option(False, "--keep-worktrees"),
) -> None:
    """Continue an interrupted run from each task's last checkpoint."""
    cfg = _load(config)
    code = asyncio.run(
        _run(
            cfg, config, [], dry_run=False, keep_worktrees=keep_worktrees, resume_id=run_id, show_prompt=False
        )
    )
    raise typer.Exit(code)


@app.command()
def status(
    run_id: str | None = typer.Argument(None, help="Show one run's tasks; omit to list recent runs."),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """List recent runs or the tasks of one run."""
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
    for col in ("issue", "state", "rounds", "cost", "pr / error"):
        table.add_column(col)
    for t in store.load_tasks(run_id).values():
        table.add_row(t.key, t.state, str(t.round), f"${t.cost_usd:.2f}", t.pr_url or t.error or "")
    console.print(table)


def _parse_age(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([dhm])", text)
    if not m:
        raise typer.BadParameter("use a number followed by d, h, or m, e.g. 7d")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"d": "days", "h": "hours", "m": "minutes"}[unit]: n})


@app.command()
def clean(
    older_than: str = typer.Option("7d", "--older-than", help="Age threshold, e.g. 7d, 12h."),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Remove run directories and worktrees older than the threshold."""
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
    config: Path = typer.Option(
        DEFAULT_CONFIG, "--config", "-c", help="Config whose matching role supplies model, auth, and options."
    ),
    role: str = typer.Option("worker", "--role", help="Which role's settings to use: worker or reviewer."),
    keep: bool = typer.Option(False, "--keep", help="Keep the temporary files for inspection."),
) -> None:
    """Drive a real runtime through a canned task to prove the adapter works. Spends tokens."""
    from orchestrator.agents.conformance.kit import run_conformance
    from orchestrator.agents.registry import get_runner
    from orchestrator.config.loader import resolve_secret

    cfg = _load(config)
    role_cfg = cfg.agents.role(role)  # type: ignore[arg-type]
    if role_cfg.runner != runner:
        role_cfg = role_cfg.model_copy(update={"runner": runner})
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


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        err.print("interrupted; use `orchestrator resume <run-id>` to continue")
        sys.exit(130)


if __name__ == "__main__":
    main()
