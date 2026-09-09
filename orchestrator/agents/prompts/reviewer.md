# Review: {{ issue.key }} — {{ issue.summary }}

Review the change on branch `{{ task.branch }}` against `origin/{{ base_branch }}`{% if round %} (fix round {{ round }}){% endif %}. You are in a read-only checkout at `{{ cwd }}`.

## The issue

Files under `.orchestrator/context/` hold the issue (`issue.md`), discussion (`comments.md`), and any reference pages under `confluence/`. Verify the change does what the issue asks and nothing more.
{% if spec_md %}
## The approved specification

This is feature work. A person approved the specification below{% if decisions %} together with the decisions that follow it{% endif %}, and the tests were committed and shown to fail before the implementation (the red commit). **Judge the change against this specification**, criterion by criterion.

{{ spec_md }}
{% if decisions %}
### Approver's decisions

{{ decisions }}
{% endif %}
{% if red_evidence %}
Failing test output before the implementation:

```
{{ red_evidence }}
```
{% endif %}

If you believe the specification itself is incomplete or wrong, say so as a finding with `"spec_gap": true`. Spec gaps go to the approver as open questions; they are not sent back to the worker and do not decide the verdict. Findings where the change fails an approved criterion, weakens a test, or has a defect are ordinary findings with `"spec_gap": false`.
{% endif %}

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
     "title": "short title", "detail": "what is wrong and why", "suggested_fix": "how to fix it",
     "spec_gap": false}
  ],
  "summary_markdown": "two or three sentences for the pull request body"
}
```

Use `request_changes` only when there is at least one `blocking` or `major` finding that is not a spec gap.
