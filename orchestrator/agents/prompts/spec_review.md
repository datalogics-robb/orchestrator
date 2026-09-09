# Review a specification: {{ issue.key }} — {{ issue.summary }}

A worker agent has written the specification below for this feature; a person will approve it before any code is written. Your critique is attached to what they read. You are in a read-only checkout at `{{ cwd }}`; the issue and its context are under `.orchestrator/context/`.

## The specification

{{ spec_md }}

## What to check

- **Completeness**: does it cover everything the issue asks, including error and rejection cases? Is anything in the issue silently dropped?
- **Testability**: can every acceptance criterion be shown by the listed tests, and can each test genuinely fail before the implementation? Name criteria without a test.
- **API design**: are the additions consistent with how this codebase already exposes similar things (naming, versioning, compatibility rules you can see in the headers)? Would an existing caller break?
- **Assumptions**: which assumptions should really be questions for the approver, because a different answer changes the design?
- **Scope**: anything beyond the issue.

Do not review code; there is none yet. Do not restate the specification.

## Reply

Only a JSON object. `spec_gap` is always `false` here.

```json
{
  "verdict": "approve | request_changes",
  "findings": [
    {"severity": "blocking | major | minor | nit", "path": null, "line": null,
     "title": "short title", "detail": "what is missing or wrong and why it matters",
     "suggested_fix": "what the specification should say instead", "spec_gap": false}
  ],
  "summary_markdown": "two or three sentences for the approver"
}
```
