from __future__ import annotations

import json
from typing import Any

from orchestrator.agents.contracts import REVIEWER_SCHEMA, WORKER_SCHEMA, strict_schema


def _objects(node: Any):
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            yield node
        for v in node.values():
            yield from _objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _objects(v)


def test_strict_schema_meets_openai_rules() -> None:
    for schema in (WORKER_SCHEMA, REVIEWER_SCHEMA):
        strict = strict_schema(schema)
        objects = list(_objects(strict))
        assert objects, "no object schemas found"
        for obj in objects:
            assert obj["additionalProperties"] is False
            assert sorted(obj["required"]) == sorted(obj["properties"])
        text = json.dumps(strict)
        assert '"default"' not in text and '"const"' not in text and '"title"' not in text
    # the original is untouched
    assert "required" in WORKER_SCHEMA and set(WORKER_SCHEMA["required"]) == {"status"}
