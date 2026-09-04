"""Lossy Atlassian Document Format to Markdown conversion."""

from __future__ import annotations

from typing import Any


def adf_to_markdown(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_markdown(n) for n in node)
    t = node.get("type", "")
    content = node.get("content", [])
    attrs = node.get("attrs", {}) or {}

    def children(sep: str = "") -> str:
        return sep.join(adf_to_markdown(c) for c in content)

    if t == "doc":
        return children("\n\n").strip() + "\n"
    if t == "paragraph":
        return children()
    if t == "text":
        text = node.get("text", "")
        for mark in node.get("marks", []) or []:
            mt = mark.get("type")
            if mt == "strong":
                text = f"**{text}**"
            elif mt == "em":
                text = f"*{text}*"
            elif mt == "code":
                text = f"`{text}`"
            elif mt == "strike":
                text = f"~~{text}~~"
            elif mt == "link":
                text = f"[{text}]({mark.get('attrs', {}).get('href', '')})"
        return text
    if t == "heading":
        return "#" * int(attrs.get("level", 1)) + " " + children()
    if t == "bulletList":
        return "\n".join("- " + adf_to_markdown(li).replace("\n", "\n  ") for li in content)
    if t == "orderedList":
        return "\n".join(
            f"{i}. " + adf_to_markdown(li).replace("\n", "\n   ") for i, li in enumerate(content, 1)
        )
    if t == "listItem":
        return children("\n")
    if t == "codeBlock":
        lang = attrs.get("language", "") or ""
        return f"```{lang}\n{children()}\n```"
    if t == "blockquote":
        return "\n".join("> " + line for line in children("\n").splitlines())
    if t == "rule":
        return "---"
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return f"@{attrs.get('text', attrs.get('id', 'someone')).lstrip('@')}"
    if t == "emoji":
        return attrs.get("shortName", "")
    if t == "inlineCard":
        return attrs.get("url", "")
    if t == "mediaSingle" or t == "mediaGroup":
        return children("\n")
    if t == "media":
        return f"[attachment: {attrs.get('alt') or attrs.get('id', 'media')}]"
    if t == "table":
        rows = [adf_to_markdown(r) for r in content]
        if not rows:
            return ""
        header = rows[0]
        cols = header.count("|") - 1
        sep = "|" + "---|" * max(cols, 1)
        return "\n".join([header, sep] + rows[1:])
    if t == "tableRow":
        return "| " + " | ".join(adf_to_markdown(c).replace("\n", " ") for c in content) + " |"
    if t in ("tableCell", "tableHeader"):
        return children(" ")
    if t == "panel":
        return "\n".join("> " + line for line in children("\n").splitlines())
    if t == "expand" or t == "nestedExpand":
        return f"**{attrs.get('title', '')}**\n\n" + children("\n\n")
    if t == "status":
        return f"[{attrs.get('text', '')}]"
    if t == "date":
        return attrs.get("timestamp", "")
    return children("\n")
