"""Markdown-lite rendering for internal documents.

Supports paragraphs, bold, italic, inline code, links, ordered and unordered
lists, headings and blockquotes. Raw HTML is escaped first, so a document can
never inject markup. Links are restricted to http, https and mailto schemes to
block ``javascript:`` payloads.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

_SAFE_SCHEMES = {"http", "https", "mailto"}

_LINK_RE = re.compile(r"\[([^\]]{1,200})\]\(([^)\s]{1,500})\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]{1,400})\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]{1,400})\*(?!\*)")
_CODE_RE = re.compile(r"`([^`\n]{1,200})`")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*(\d{1,3})\.\s+(.*)$")
_UNORDERED_RE = re.compile(r"^\s*[-*+]\s+(.*)$")


def _safe_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _SAFE_SCHEMES:
        return None
    if parsed.scheme.lower() in {"http", "https"} and not parsed.netloc:
        return None
    return html.escape(url, quote=True)


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)

    def link_replacer(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        safe = _safe_url(url)
        if safe is None:
            return html.escape(match.group(0), quote=False)
        return f'<a href="{safe}" rel="noopener noreferrer nofollow">{label}</a>'

    escaped = _LINK_RE.sub(link_replacer, escaped)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC_RE.sub(r"<em>\1</em>", escaped)
    return escaped


def render(text: str | None) -> str:
    """Render markdown-lite to safe HTML."""
    if not text:
        return ""

    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{'<br>'.join(paragraph)}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            flush_paragraph()
            close_list()
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1)) + 2  # keep h3-h6 so page h1 stays unique
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue

        if line.strip().startswith(">"):
            flush_paragraph()
            close_list()
            out.append(f"<blockquote>{_inline(line.strip()[1:].strip())}</blockquote>")
            continue

        ordered = _ORDERED_RE.match(line)
        unordered = _UNORDERED_RE.match(line)
        if ordered or unordered:
            flush_paragraph()
            kind = "ol" if ordered else "ul"
            if list_kind != kind:
                close_list()
                out.append(f"<{kind}>")
                list_kind = kind
            content = ordered.group(2) if ordered else unordered.group(1)
            out.append(f"<li>{_inline(content.strip())}</li>")
            continue

        if list_kind:
            close_list()
        paragraph.append(_inline(line.strip()))

    flush_paragraph()
    close_list()
    return "\n".join(out)


def excerpt(text: str | None, length: int = 200) -> str:
    """Plain-text excerpt with markup removed, for list views."""
    if not text:
        return ""
    plain = re.sub(r"[*`#>\[\]()]", " ", text)
    plain = " ".join(plain.split())
    if len(plain) <= length:
        return plain
    return plain[: length - 1].rstrip() + "…"
