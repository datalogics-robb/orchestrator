# Fix round {{ task.round }} of {{ review_rounds }}: {{ issue.key }} — {{ issue.summary }}

{% if resumed %}
You are continuing your earlier work on this issue in the same worktree at `{{ worktree }}`.
{% else %}
An earlier agent session implemented this issue in the worktree at `{{ worktree }}`; you are picking the work up. Its summary of what it did:

> {{ previous_summary }}

Inspect `git diff origin/{{ base_branch }}` to see the current change. Context files are under `.orchestrator/context/`.
{% endif %}

The change did not pass validation. Address everything below, then rebuild and rerun the relevant tests before finishing.

{{ fix_reason }}
{% if spec_md %}
## The approved specification still governs

The specification is in `.orchestrator/context/spec.md`{% if decisions %} and the approver's decisions in `.orchestrator/context/decisions.md`{% endif %}. Fix the findings within it. Do not weaken the red tests to get green; if a test itself is wrong, fix it and say why in your summary.
{% endif %}

{% if interrupted %}
## Your previous session was interrupted

The runtime running you failed part-way through (`{{ interrupted }}`); this is the same session, resumed. Everything you wrote is still in the worktree. Check where you left off with `git status` and your own notes, finish the remaining work, and reply with the JSON object. Do not start over.
{% endif %}
## Boundaries

Same as before: work only in the worktree and granted shares, do not run {% for d in deny_commands %}`{{ d }}`{% if not loop.last %}, {% endif %}{% endfor %}, keep the change focused on the issue, and use `orchestrator-cp` for any share copies.

## When you are done

Reply with only the JSON object in the same format as before (`status` completed or blocked, `summary`, `changed_paths`, `tests_selected`, `test_rationale`, `copied_files`, and `blocked` when applicable). The summary should describe the whole change, not only this round.
