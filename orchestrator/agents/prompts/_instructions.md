{% if instructions %}
## The operator's instructions

This task was reported blocked{% if resumed %} earlier in this session{% endif %}, and the operator, the person running the orchestrator, read the report and replied. Their instructions take precedence over the earlier conclusion that the task cannot be done: act on them, within the boundaries below. If they still cannot be carried out, report `status: blocked` again and say exactly why. They are listed oldest first; the last one is new.
{% for i in instructions %}
---

{{ i }}
{% endfor %}
{% endif %}
