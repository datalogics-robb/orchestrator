"""Confluence Cloud: fetch pages as Markdown context and publish run pages."""

from __future__ import annotations

import base64
import html
import json
import re
from pathlib import Path
from typing import Any

import httpx

from orchestrator.config.schema import ConfluenceConfig

_PAGE_ID = re.compile(r"/pages/(\d+)")


class ConfluenceError(Exception):
    pass


def storage_to_markdown(storage: str) -> str:
    """Rough conversion of Confluence storage XHTML to Markdown."""
    text = storage
    text = re.sub(
        r"<ac:structured-macro[^>]*ac:name=\"code\"[^>]*>.*?<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>.*?</ac:structured-macro>",
        r"\n```\n\1\n```\n",
        text,
        flags=re.DOTALL,
    )
    for level in range(6, 0, -1):

        def heading(m: re.Match[str], level: int = level) -> str:
            return "\n" + "#" * level + " " + m.group(1) + "\n"

        text = re.sub(rf"<h{level}[^>]*>(.*?)</h{level}>", heading, text, flags=re.DOTALL)
    text = re.sub(r"<(strong|b)>(.*?)</\1>", r"**\2**", text, flags=re.DOTALL)
    text = re.sub(r"<(em|i)>(.*?)</\1>", r"*\2*", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<a[^>]*href=\"([^\"]*)\"[^>]*>(.*?)</a>", r"[\2](\1)", text, flags=re.DOTALL)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</?(p|div|ul|ol|table|tbody|thead)[^>]*>", "\n", text)
    text = re.sub(r"<tr[^>]*>", "\n| ", text)
    text = re.sub(r"</t[dh]>", " | ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def markdown_to_storage(md: str) -> str:
    """Minimal Markdown to storage format: headings, paragraphs, code fences, lists."""
    out: list[str] = []
    in_code = False
    code: list[str] = []
    in_list = False
    for line in md.splitlines():
        if line.startswith("```"):
            if in_code:
                body = html.escape("\n".join(code))
                out.append(
                    f'<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[{"\n".join(code)}]]></ac:plain-text-body></ac:structured-macro>'
                )
                _ = body
                code, in_code = [], False
            else:
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        if line.startswith("- ") or line.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            out.append(f"<h{len(m.group(1))}>{_inline(m.group(2))}</h{len(m.group(1))}>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)
    return text


class ConfluenceClient:
    def __init__(self, cfg: ConfluenceConfig, *, login: str | None, token: str) -> None:
        self.cfg = cfg
        if login:
            cred = base64.b64encode(f"{login}:{token}".encode()).decode()
            headers = {"Authorization": f"Basic {cred}"}
        else:
            headers = {"Authorization": f"Bearer {token}"}
        headers["Accept"] = "application/json"
        self._client = httpx.AsyncClient(base_url=cfg.base_url, headers=headers, timeout=60)

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def page_id_from_url(url: str) -> str | None:
        m = _PAGE_ID.search(url)
        return m.group(1) if m else None

    async def fetch_page_markdown(self, url_or_id: str, cache_dir: Path | None = None) -> tuple[str, str]:
        """Return (title, markdown), cached by page version."""
        page_id = self.page_id_from_url(url_or_id) or url_or_id
        r = await self._client.get(f"/api/v2/pages/{page_id}", params={"body-format": "storage"})
        if r.status_code >= 400:
            raise ConfluenceError(f"page {page_id}: {r.status_code} {r.text[:200]}")
        data = r.json()
        version = (data.get("version") or {}).get("number", 0)
        title = data.get("title", page_id)
        if cache_dir:
            cached = cache_dir / f"confluence-{page_id}-v{version}.md"
            if cached.exists():
                return title, cached.read_text()
        md = storage_to_markdown(((data.get("body") or {}).get("storage") or {}).get("value", ""))
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / f"confluence-{page_id}-v{version}.md").write_text(md)
        return title, md

    async def publish(self, title: str, markdown: str) -> str:
        """Create a page under the configured parent; returns its URL."""
        pub = self.cfg.publish
        if not pub:
            raise ConfluenceError("confluence.publish is not configured")
        spaces = await self._client.get("/api/v2/spaces", params={"keys": pub.space})
        if spaces.status_code >= 400 or not spaces.json().get("results"):
            raise ConfluenceError(f"space {pub.space} not found")
        space_id = spaces.json()["results"][0]["id"]
        payload: dict[str, Any] = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "parentId": pub.parent_page_id,
            "body": {"representation": "storage", "value": markdown_to_storage(markdown)},
        }
        r = await self._client.post(
            "/api/v2/pages", content=json.dumps(payload), headers={"Content-Type": "application/json"}
        )
        if r.status_code >= 400:
            raise ConfluenceError(f"publish '{title}': {r.status_code} {r.text[:300]}")
        data = r.json()
        links = data.get("_links", {})
        return (links.get("base", self.cfg.base_url) + links.get("webui", "")) if links else self.cfg.base_url

    async def check(self) -> list[str]:
        problems = []
        for url in self.cfg.context_pages:
            pid = self.page_id_from_url(url)
            if not pid:
                problems.append(f"confluence: cannot find a page id in {url}")
                continue
            r = await self._client.get(f"/api/v2/pages/{pid}")
            if r.status_code >= 400:
                problems.append(f"confluence: page {pid}: {r.status_code}")
        if self.cfg.publish:
            r = await self._client.get(f"/api/v2/pages/{self.cfg.publish.parent_page_id}")
            if r.status_code >= 400:
                problems.append(f"confluence: parent page {self.cfg.publish.parent_page_id}: {r.status_code}")
        return problems
