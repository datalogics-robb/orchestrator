# Review: {{ issue.key }} — {{ issue.summary }}

Review the change on branch `{{ task.branch }}` against `origin/{{ base_branch }}`{% if round %} (fix round {{ round }}){% endif %}. You are in a read-only checkout at `{{ cwd }}`.

## The issue

Files under `.orchestrator/context/` hold the issue (`issue.md`), discussion (`comments.md`), and any reference pages under `confluence/`. Verify the change does what the issue asks and nothing more.

## What the author reports

> {{ worker.summary }}

Changed paths: {% for p in worker.changed_paths %}`{{ p }}`{% if not loop.last %}, {% endif %}{% else %}(none listed){% endfor %}

Tests the orchestrator ran, all passing:
{% for t in tests_run %}
- `{{ t }}`
{%- else %}
- none
{%- endfor %}

The full test suite was **not** run. If the change needs tests that did not run, or lacks tests it should have, say so as a `major` finding.
{% if worker.test_rationale %}
Author's test rationale: {{ worker.test_rationale }}
{% endif %}
{% if worker.copied_files %}
Files the author copied between network shares (confirm they landed where the issue asked; you have read access to the shares):
{% for c in worker.copied_files %}
- `{{ c['from'] }}` -> `{{ c['to'] }}`
{%- endfor %}
{% endif %}

## The diff

{% if diff_truncated %}
The diff is large; read it from `{{ diff_path }}` or run `git diff origin/{{ base_branch }}`.
{% else %}
```diff
{{ diff }}
```
{% endif %}

## What to check

- Correctness against the issue and acceptance criteria.
- Tests: adequate, meaningful, actually exercising the change.
- Scope: nothing unrelated, no new dependencies or network calls the issue did not ask for.
- Security: no credentials, no customer data committed, no unsafe handling of input.
- Consistency with the standards in the Confluence pages, when present.

## Reply

Only a JSON object:

```json
{
  "verdict": "approve | request_changes",
  "findings": [
    {"severity": "blocking | major | minor | nit", "path": "file or null", "line": 42,
     "title": "short title", "detail": "what is wrong and why", "suggested_fix": "how to fix it"}
  ],
  "summary_markdown": "two or three sentences for the pull request body"
}
```

Use `request_changes` only when there is at least one `blocking` or `major` finding.
