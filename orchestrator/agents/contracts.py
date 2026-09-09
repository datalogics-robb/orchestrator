"""Runtime-independent structured output contracts for the worker and reviewer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Severity = Literal["blocking", "major", "minor", "nit"]
BlockReason = Literal["ambiguous-requirements", "missing-access", "out-of-scope", "technical"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BlockedInfo(Contract):
    reason: BlockReason
    details_markdown: str
    questions_for_reporter: list[str] = []


class CopiedFile(Contract):
    from_: str = Field(alias="from")
    to: str


class WorkerResult(Contract):
    status: Literal["completed", "blocked"]
    summary: str = ""
    changed_paths: list[str] = []
    tests_selected: list[str] = []
    test_rationale: str = ""
    copied_files: list[CopiedFile] = []
    blocked: BlockedInfo | None = None


class PlannedTest(Contract):
    name: str
    proves: str = ""
    """Which acceptance criteria the test demonstrates, by number or wording."""


class SpecResult(Contract):
    """The feature workflow's specification: what will be built and how it will be proved."""

    status: Literal["completed", "blocked"]
    summary: str = ""
    acceptance_criteria: list[str] = []
    api_surface: list[str] = []
    tests: list[PlannedTest] = []
    assumptions: list[str] = []
    risks: list[str] = []
    questions_for_reporter: list[str] = []
    blocked: BlockedInfo | None = None


class Finding(Contract):
    severity: Severity
    path: str | None = None
    line: int | None = None
    title: str = ""
    detail: str = ""
    suggested_fix: str = ""
    spec_gap: bool = False
    """The finding asks for behaviour the approved specification does not cover. It is a question
    for the approver, not a defect the worker must fix."""

    @model_validator(mode="after")
    def _title_from_detail(self) -> Finding:
        if not self.title:
            text = self.detail.strip() or self.suggested_fix.strip() or "finding"
            object.__setattr__(self, "title", text.split(". ")[0][:120])
        return self


class ReviewerResult(Contract):
    verdict: Literal["approve", "request_changes"]
    findings: list[Finding] = []
    summary_markdown: str = ""

    @property
    def actionable(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in ("blocking", "major") and not f.spec_gap]

    @property
    def carried(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in ("minor", "nit") and not f.spec_gap]

    @property
    def spec_gaps(self) -> list[Finding]:
        return [f for f in self.findings if f.spec_gap]


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True)
    # Inline $defs so every CLI's schema validator sees a self-contained object.
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                return resolve(defs[name])
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The subset of JSON Schema OpenAI's structured outputs accept.

    Every object gets additionalProperties: false and lists all properties as required;
    default, title, and format annotations are dropped; const becomes a one-value enum.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in ("default", "title", "format"):
                continue
            if k == "const":
                out["enum"] = [v]
                continue
            out[k] = walk(v)
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return walk(schema)


WORKER_SCHEMA: dict[str, Any] = _schema(WorkerResult)
REVIEWER_SCHEMA: dict[str, Any] = _schema(ReviewerResult)
SPEC_SCHEMA: dict[str, Any] = _schema(SpecResult)


class ContractError(Exception):
    pass


def parse_worker(data: dict[str, Any]) -> WorkerResult:
    try:
        return WorkerResult.model_validate(data)
    except ValidationError as e:
        raise ContractError(f"worker result does not match contract: {e}") from e


def parse_reviewer(data: dict[str, Any]) -> ReviewerResult:
    try:
        return ReviewerResult.model_validate(data)
    except ValidationError as e:
        raise ContractError(f"reviewer result does not match contract: {e}") from e


def parse_spec(data: dict[str, Any]) -> SpecResult:
    try:
        return SpecResult.model_validate(data)
    except ValidationError as e:
        raise ContractError(f"specification does not match contract: {e}") from e
