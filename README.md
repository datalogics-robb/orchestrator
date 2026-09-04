# orchestrator

Turns Jira issues into reviewed GitHub pull requests. Each issue is handed to a worker
agent in its own git worktree, the result is built and tested locally, a second agent from
a different vendor reviews it, review findings go back to the worker, and a draft pull
request is opened. Work that cannot be completed produces a findings report that is
attached to the Jira issue and published to Confluence.

The design is in [`docs/design-plan.md`](docs/design-plan.md). This README covers setup
and daily use.

## Requirements

- Python 3.13 or newer, `git`, and the `gh` CLI (authenticated or with a token).
- The agent CLIs you configure: `claude` for Claude Code, `codex` for Codex, and
  optionally `gemini`, `opencode`, `hermes`.
- A `~/.netrc` entry for `datalogics.jfrog.io` so mkenv can install packages.
- Credentials as environment variables or `~/.netrc` entries: a Jira API token, a GitHub
  token with `repo` scope, and an API key for each agent runtime.

## Setup

```bash
python mkenv.py                      # creates python-env-<hostname> with all dependencies
bin/orchestrator init                # writes a commented orchestrator.yaml
$EDITOR orchestrator.yaml
bin/orchestrator doctor              # checks credentials, adapters, shares, MCP sources
```

`bin/orchestrator` runs the package from the mkenv virtual environment without activating
it. Activate the environment instead if you prefer:

```bash
. ./python-env-$(hostname -s | tr '[:upper:]' '[:lower:]')/bin/activate
python -m orchestrator --help
```

`doctor` exits non-zero while anything is wrong. It also lists, per role, which limits the
chosen runtime enforces natively and which the orchestrator enforces softly.

## Running

```bash
bin/orchestrator run PROJ-123 PROJ-140 --dry-run    # everything except push, PR, Jira, Confluence
bin/orchestrator run PROJ-123 PROJ-140              # the real thing
bin/orchestrator run EPIC-7                         # expands to the epic's children, dependency-ordered
bin/orchestrator run PROJ-123 --show-prompt         # fetch context and print the worker prompt only
bin/orchestrator status                             # recent runs
bin/orchestrator status 20260903-141500-a1b2c3      # one run's tasks
bin/orchestrator resume 20260903-141500-a1b2c3      # continue an interrupted run
bin/orchestrator clean --older-than 7d              # remove old run directories and worktrees
bin/orchestrator conformance codex --role reviewer  # prove an adapter works (spends tokens)
bin/orchestrator config-reference                   # every accepted YAML key, from the schema
bin/orchestrator --help                             # the workflow, inputs, outputs, exit codes
```

Every command accepts `--help`. `orchestrator --help` explains the workflow and what the tool
reads and writes; `orchestrator config-reference` documents each configuration key with its
type, default, and meaning (`--format markdown` for a document, `--section agents` to narrow).

Each run writes to `<state_dir>/runs/<run-id>/`: per-issue prompts, agent transcripts,
build and test logs, the diff sent for review, the PR body, `findings.md` when blocked,
and `audit.jsonl` recording every external side effect and every share copy.

## Configuration

One YAML file per target repository; `orchestrator init` writes a commented example.
Points worth knowing:

- **Secrets never live in the file.** `auth:` blocks name an environment variable
  (`token_env`) or a `~/.netrc` machine (`netrc_machine`). The loader refuses files
  containing anything shaped like a token.
- **Agents are chosen per role.** `agents.worker` and `agents.reviewer` each pick a
  `runner` (`claude-code`, `codex`, `gemini-cli`, `opencode`, `hermes`, or a third-party
  adapter registered under the `orchestrator.runners` entry-point group), a model, limits,
  share grants, MCP servers, and adapter-specific `options`.
- **Network shares.** `shares:` declares mounts with per-platform paths. Roles get `read`
  or `read-write` grants; `write_under` narrows where writes may land. Agents copy between
  shares with `orchestrator-cp <share>:<path> <share>:<path>`, which enforces the grants
  and logs every file.
- **MCP servers.** The orchestrator reads the servers already configured for each CLI
  (`~/.claude.json`, `~/.codex/config.toml`, and so on) and passes only the allowlisted
  names to each role. `mcp.deny_tools` keeps specific tools off even when their server is
  allowed.
- **Build and tests.** `build.commands` run under a global semaphore; `test.selection`
  picks a subset by `changed-paths`, `named-suite`, or `agent-chosen`. The full suite is
  never run; the PR body says so.

## Development

```bash
python-env-*/bin/pytest                 # unit tests plus dry-run pipeline tests with fake agents
python-env-*/bin/ruff format . && python-env-*/bin/ruff check .
python-env-*/bin/mypy orchestrator
```

The pipeline tests build a temporary git repository and drive the whole state machine with
fake worker and reviewer runners, so no tokens are spent. Real adapters are exercised with
`orchestrator conformance <runner>`.
