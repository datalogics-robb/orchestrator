"""Jira Cloud REST v3 client over httpx."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from orchestrator.config.schema import TrackerConfig
from orchestrator.trackers.adf import adf_to_markdown
from orchestrator.trackers.base import Attachment, Issue, TrackerError

ISSUE_FIELDS = "summary,description,issuetype,status,comment,issuelinks,attachment,parent"


class JiraTracker:
    def __init__(self, cfg: TrackerConfig, *, login: str | None, token: str) -> None:
        self.cfg = cfg
        self.base = cfg.base_url
        if login:
            # Jira Cloud API tokens use basic auth with the account email.
            cred = base64.b64encode(f"{login}:{token}".encode()).decode()
            headers = {"Authorization": f"Basic {cred}"}
        else:
            headers = {"Authorization": f"Bearer {token}"}
        headers["Accept"] = "application/json"
        self._client = httpx.AsyncClient(base_url=self.base, headers=headers, timeout=60)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise TrackerError(f"GET {path}: {e.__class__.__name__}: {e}") from e
        if r.status_code >= 400:
            raise TrackerError(f"GET {path}: {r.status_code} {r.text[:300]}")
        return r.json()

    async def _post(self, path: str, json: Any, ok: tuple[int, ...] = (200, 201, 204)) -> Any:
        try:
            r = await self._client.post(path, json=json)
        except httpx.HTTPError as e:
            raise TrackerError(f"POST {path}: {e.__class__.__name__}: {e}") from e
        if r.status_code not in ok:
            raise TrackerError(f"POST {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def _issue_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    def _to_issue(self, data: dict[str, Any]) -> Issue:
        f = data.get("fields", {})
        comments = []
        for c in (f.get("comment") or {}).get("comments", []):
            author = (c.get("author") or {}).get("displayName", "unknown")
            comments.append((author, c.get("created", ""), adf_to_markdown(c.get("body"))))
        links = []
        blocked_by = []
        for link in f.get("issuelinks", []) or []:
            lt = link.get("type", {})
            if "inwardIssue" in link:
                other = link["inwardIssue"]
                rel = lt.get("inward", "relates to")
                links.append((rel, other["key"], other.get("fields", {}).get("summary", "")))
                if "blocked" in rel.lower():
                    blocked_by.append(other["key"])
            if "outwardIssue" in link:
                other = link["outwardIssue"]
                links.append(
                    (
                        lt.get("outward", "relates to"),
                        other["key"],
                        other.get("fields", {}).get("summary", ""),
                    )
                )
        attachments = [
            Attachment(
                a.get("filename", ""), int(a.get("size", 0)), a.get("mimeType", ""), a.get("content", "")
            )
            for a in f.get("attachment", []) or []
        ]
        acceptance = ""
        if self.cfg.acceptance_field and f.get(self.cfg.acceptance_field):
            val = f[self.cfg.acceptance_field]
            acceptance = adf_to_markdown(val) if isinstance(val, dict) else str(val)
        return Issue(
            key=data["key"],
            summary=f.get("summary", ""),
            description_markdown=adf_to_markdown(f.get("description")),
            issue_type=(f.get("issuetype") or {}).get("name", ""),
            status=(f.get("status") or {}).get("name", ""),
            url=self._issue_url(data["key"]),
            acceptance_criteria=acceptance,
            comments=comments,
            links=links,
            blocked_by=blocked_by,
            attachments=attachments,
            raw=data,
        )

    async def get_issue(self, key: str) -> Issue:
        fields = ISSUE_FIELDS + (f",{self.cfg.acceptance_field}" if self.cfg.acceptance_field else "")
        data = await self._get(f"/rest/api/3/issue/{key}", fields=fields)
        return self._to_issue(data)

    async def children(self, epic_key: str) -> list[Issue]:
        jql = self.cfg.epic_children_jql.format(key=epic_key)
        issues: list[Issue] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"jql": jql, "fields": ISSUE_FIELDS, "maxResults": 50}
            if token:
                params["nextPageToken"] = token
            data = await self._get("/rest/api/3/search/jql", **params)
            issues.extend(self._to_issue(i) for i in data.get("issues", []))
            token = data.get("nextPageToken")
            if not token or data.get("isLast", True):
                break
        return issues

    async def comment(self, key: str, body: str) -> None:
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": line}]}
                for line in body.split("\n")
                if line.strip()
            ]
            or [{"type": "paragraph", "content": [{"type": "text", "text": body}]}],
        }
        await self._post(f"/rest/api/3/issue/{key}/comment", {"body": adf})

    async def transition(self, key: str, to_status: str) -> None:
        data = await self._get(f"/rest/api/3/issue/{key}/transitions")
        for t in data.get("transitions", []):
            target = (t.get("to") or {}).get("name", "")
            if target.lower() == to_status.lower() or t.get("name", "").lower() == to_status.lower():
                await self._post(f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": t["id"]}})
                return
        names = ", ".join(t.get("name", "") for t in data.get("transitions", []))
        raise TrackerError(f"{key}: no transition to '{to_status}'; available: {names}")

    async def attach(self, key: str, path: Path) -> None:
        try:
            with path.open("rb") as f:
                r = await self._client.post(
                    f"/rest/api/3/issue/{key}/attachments",
                    files={"file": (path.name, f)},
                    headers={"X-Atlassian-Token": "no-check"},
                )
        except httpx.HTTPError as e:
            raise TrackerError(f"attach {path.name} to {key}: {e.__class__.__name__}: {e}") from e
        if r.status_code >= 400:
            raise TrackerError(f"attach {path.name} to {key}: {r.status_code} {r.text[:300]}")

    async def download_attachment(self, attachment: Attachment, dest: Path) -> None:
        try:
            async with self._client.stream("GET", attachment.url, follow_redirects=True) as r:
                if r.status_code >= 400:
                    raise TrackerError(f"download {attachment.filename}: {r.status_code}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
        except httpx.HTTPError as e:
            raise TrackerError(f"download {attachment.filename}: {e.__class__.__name__}: {e}") from e

    async def check(self) -> list[str]:
        problems = []
        try:
            me = await self._get("/rest/api/3/myself")
            proj = await self._get(f"/rest/api/3/project/{self.cfg.project}")
            _ = (me.get("displayName"), proj.get("key"))
        except (TrackerError, httpx.HTTPError) as e:
            problems.append(f"jira: {e}")
        return problems
