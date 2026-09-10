# Green phase: {{ issue.key }} — {{ issue.summary }}

{% if resumed %}
You are continuing your session in the worktree at `{{ worktree }}`. Your red-phase summary:
{% else %}
An earlier session wrote the tests for this feature in the worktree at `{{ worktree }}`; you are picking the work up. Its summary:
{% endif %}

> {{ previous_summary }}

The approved specification is in `.orchestrator/context/spec.md`{% if decisions %} and the approver's binding decisions in `.orchestrator/context/decisions.md`{% endif %}. The tests are committed as the red commit on this branch; the orchestrator built them and confirmed they fail:

{% for t in red_tests %}
- `{{ t }}`
{%- endfor %}

```
{{ red_evidence }}
```

## Your job in this phase: make the tests pass

1. Implement the behaviour the specification promises so every red test passes. Replace the stubs; keep the interface the tests already use.
2. **Do not weaken the tests.** You may change a test only to fix a mistake in the test itself, and you must say so in your summary with the reason. Removing an assertion, loosening a threshold, or skipping a case to get green is not acceptable.
3. Update public documentation the codebase keeps next to the interface (header comments, reference docs) for everything in the API section of the specification.
4. Build: {% for c in build.commands %}`{{ c | join(' ') }}`{% if not loop.last %}, {% endif %}{% else %}(no build step declared){% endfor %}. Run the red tests and any group of existing tests that exercises the code you changed; each command must start with one of: {% for p in test.selection.allowed_prefixes %}`{{ p }}`{% if not loop.last %}, {% endif %}{% else %}(any){% endfor %}. Report all of them in `tests_selected`.
5. Run `pre-commit run --files <your files>` if the repository has a `.pre-commit-config.yaml` and fix what it reports. Do not commit; the orchestrator commits this phase as the green commit.

{% if shares %}
## Network shares

{% for s in shares %}
- `{{ s.name }}` at `{{ s.path }}` ({{ s.mode }}{% if s.write_under %}; writes only under {% for w in s.write_under %}`{{ w }}`{% if not loop.last %}, {% endif %}{% endfor %}{% endif %})
{%- endfor %}

Copy with `orchestrator-cp <share>:<relative/path> <share>:<relative/path>`, never plain `cp`, and record copies in `copied_files`.
{% endif %}
{% if mcp_servers %}
## Tools

MCP servers available: {% for m in mcp_servers %}`{{ m }}`{% if not loop.last %}, {% endif %}{% endfor %}.
{% endif %}

{% if interrupted %}
## Your previous session was interrupted

The runtime running you failed part-way through (`{{ interrupted }}`); this is the same session, resumed. Everything you wrote is still in the worktree. Check where you left off with `git status` and your own notes, finish the remaining work, and reply with the JSON object. Do not start over.
{% endif %}
## Boundaries

- Do not run {% for d in deny_commands %}`{{ d }}`{% if not loop.last %}, {% endif %}{% endfor %}.
- Implement the specification as approved. If you believe it is wrong, implement it anyway and say why in the summary; the reviewer and the approver decide. Do not add behaviour it does not ask for.
- Never use `--no-verify`, `-n`, or touch `core.hooksPath` or `.git/hooks`.

## Reply

Only a JSON object in the standard worker contract. `summary` becomes the pull request description: what was built, how each acceptance criterion is met, any test you changed and why, and documentation updated.

```json
{
  "status": "completed",
  "summary": "...",
  "changed_paths": ["..."],
  "tests_selected": ["exact commands, red tests first"],
  "test_rationale": "...",
  "copied_files": [{"from": "share:path", "to": "share:path"}]
}
```

Use `"status": "blocked"` with a `blocked` object only when the specification cannot be implemented.
