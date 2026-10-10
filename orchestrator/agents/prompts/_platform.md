{% if platform %}
## Target platform: {{ platform }}

This issue is being worked for `{{ platform }}`, which is not the host you are running on. The orchestrator runs every build and test gate on a `{{ platform }}` build machine: after you finish, it copies the worktree there and runs the setup, build, and test commands, and any failure comes back to you with its log, its paths rewritten to this worktree. You cannot run commands on that machine yourself, and must not try to reach it.

- Building here is still worthwhile for catching compile errors, but a passing local test does not show that the fix works on `{{ platform }}`, and a local failure to reproduce is expected.
- Not being able to reproduce or verify the problem here is not a reason to block. Make the fix, write the test that should fail without it on `{{ platform }}`, finish with `"status": "completed"`, and say in `test_rationale` which results you could only reason about rather than run.
- Refer to test inputs the way the existing tests do (relative to the test data root they already use), never by a path that exists only on this host, because the gates run the tests on the build machine.
{% endif %}
