# Red phase{% if task.round %} (attempt {{ task.round + 1 }}){% endif %}: {{ issue.key }} — {{ issue.summary }}

You are in the git worktree at `{{ worktree }}`, on a branch from `origin/{{ base_branch }}`. The specification for this feature has been **approved**; it is in `.orchestrator/context/spec.md`{% if decisions %}, and the approver's binding decisions are in `.orchestrator/context/decisions.md`{% endif %}. The issue and its context are under `.orchestrator/context/` as well.

{% if resumed %}
You are continuing your earlier session in this worktree.
{% endif %}
{% if fix_reason %}
## Your previous attempt was not accepted

{{ fix_reason }}
{% endif %}

## Your job in this phase: tests only

Write the tests named in the specification, and **nothing else that implements behaviour**:

1. Implement every test in the specification's test list, asserting the behaviour of the acceptance criteria it proves. Cover the rejection and failure cases the criteria describe. Assertions must inspect actual results (output content, return codes, error conditions), not only that a call returned.
2. Add the **smallest interface** the tests need to compile: declarations, enumerators, struct members, and stub implementations that return a not-implemented or failure result. Follow the codebase's compatibility conventions listed in the specification's API section. Do not implement the feature; a stub that already satisfies a test defeats the purpose.
3. Stage any test data the tests need on the granted shares with `orchestrator-cp` and record it in `copied_files`. Customer files never go into the repository.
4. Build: {% for c in build.commands %}`{{ c | join(' ') }}`{% if not loop.last %}, {% endif %}{% else %}(no build step declared){% endfor %}. The tree must compile.
5. Run your tests and confirm they **fail because the behaviour is missing**{% if require_red %} — the orchestrator will build and run them and reject this phase if they pass, do not build, or fail for a resource or setup reason instead of an assertion{% endif %}. Each test command must start with one of: {% for p in test.selection.allowed_prefixes %}`{{ p }}`{% if not loop.last %}, {% endif %}{% else %}(any){% endfor %}.
6. Run `pre-commit run --files <your files>` if the repository has a `.pre-commit-config.yaml` and fix what it reports. Do not commit; the orchestrator commits this phase as the red commit.

{% if shares %}
## Network shares

{% for s in shares %}
- `{{ s.name }}` at `{{ s.path }}` ({{ s.mode }}{% if s.write_under %}; writes only under {% for w in s.write_under %}`{{ w }}`{% if not loop.last %}, {% endif %}{% endfor %}{% endif %})
{%- endfor %}

Copy with `orchestrator-cp <share>:<relative/path> <share>:<relative/path>`, never plain `cp`.
{% endif %}
{% if mcp_servers %}
## Tools

MCP servers available: {% for m in mcp_servers %}`{{ m }}`{% if not loop.last %}, {% endif %}{% endfor %}.
{% endif %}

## Boundaries

- Do not run {% for d in deny_commands %}`{{ d }}`{% if not loop.last %}, {% endif %}{% endfor %}.
- Work only in the worktree and the granted shares. Change nothing unrelated to the tests and their interface.
- Never use `--no-verify`, `-n`, or touch `core.hooksPath` or `.git/hooks`.

## Reply

Only a JSON object in the standard worker contract. `summary` describes the tests and the interface stubs, and which acceptance criteria each test covers. `tests_selected` lists the exact commands that run the new tests. `changed_paths` lists every file you touched.

```json
{
  "status": "completed",
  "summary": "...",
  "changed_paths": ["..."],
  "tests_selected": ["exact commands"],
  "test_rationale": "which criteria each test proves and why they fail now",
  "copied_files": [{"from": "share:path", "to": "share:path"}]
}
```

Use `"status": "blocked"` with a `blocked` object only when the tests cannot be written at all.
