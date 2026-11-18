# A question from the operator: {{ key }} — {{ summary }}

{% if resumed -%}
This continues your session on this issue.
{%- else -%}
An agent session worked on this issue in this worktree; you are answering for it. Its context files are under `.orchestrator/context/`, and `git diff` shows what it changed.
{%- endif %} The task is now {{ task.state }}{% if task.stopped_at %}, after it stopped in {{ task.stopped_at }}{% endif %}{% if task.error %} (`{{ task.error.splitlines()[0] }}`){% endif %}.
{% if findings %}
The orchestrator's report on why it stopped:

{{ findings }}
{% endif %}
{% if earlier %}
## Earlier messages about this task

{% for m in earlier -%}
**{{ m.kind }}** ({{ m.by }}): {{ m.text }}

{% endfor %}
{% endif %}
## The question

The operator, the person running the orchestrator, asks:

{{ question }}

Answer it from the work and what you found. This is a conversation, not another attempt at the task: your access is read-only, so inspect the worktree, the context files, and the shares as you need, but change nothing. If the operator wants you to do something, say what you would do; they can send it back to you as an instruction.

Reply with only a JSON object:

```json
{"answer": "your answer, in Markdown"}
```
