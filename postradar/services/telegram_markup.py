"""Technical Telegram HTML normalization, validation and balanced splitting."""

import re
from html import escape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from telethon import types
from aiogram.types import MessageEntity
from aiogram.utils.text_decorations import HtmlDecoration


class MarkupError(ValueError):
    """Markup cannot safely be sent with Telegram HTML parse mode."""


_ALIASES = {"strong": "b", "em": "i", "del": "s", "strike": "s"}
_TAGS = {"b", "i", "u", "s", "tg-spoiler", "code", "pre", "blockquote", "a"}
_URL = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|mailto:|www\.)[^\s<>\"']+"
    r"|(?<![\w@/])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?::[0-9]+)?(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)


def safe_url(value: str) -> bool:
    """Allow explicit Telegram/web/mail links without controls or credentials."""
    if not value or any(ord(char) < 33 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme.lower() in {"http", "https", "tg", "mailto"}
            and (bool(parsed.netloc) if parsed.scheme.lower() != "mailto" else bool(parsed.path))
            and parsed.username is None and parsed.password is None
        )
    except ValueError:
        return False


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []
        self.tokens: list[tuple[str, str]] = []
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = _ALIASES.get(tag, tag)
        if tag == "span" and attrs == [("class", "tg-spoiler")]:
            tag, attrs = "tg-spoiler", []
        if tag not in _TAGS or len(dict(attrs)) != len(attrs):
            raise MarkupError("Unsupported tag or duplicate attributes")
        allowed = {"a": {"href"}, "code": {"class"}, "blockquote": {"expandable"}}.get(tag, set())
        if set(dict(attrs)) - allowed:
            raise MarkupError("Unsupported attributes")
        active = [item[0] for item in self.stack]
        if tag in active:
            raise MarkupError("Nested identical entities")
        if tag in {"a", "blockquote"} and any(item in active for item in ("a", "blockquote")):
            raise MarkupError("Links and blockquotes cannot contain each other")
        if any(item in active for item in ("code", "pre")) and not (tag == "code" and active[-1:] == ["pre"]):
            raise MarkupError("Invalid code nesting")
        if tag in {"code", "pre"} and active and not (tag == "code" and active == ["pre"]):
            raise MarkupError("Code cannot contain other entities")
        attributes = dict(attrs)
        opening = f"<{tag}>"
        if tag == "a":
            href = attributes.get("href")
            if not isinstance(href, str) or not safe_url(href):
                raise MarkupError("Unsafe link")
            self.links.add(href)
            opening = f'<a href="{escape(href, quote=True)}">'
        elif tag == "code" and attributes:
            language = attributes.get("class") or ""
            if active[-1:] != ["pre"] or not re.fullmatch(r"language-[A-Za-z0-9_+.-]+", language):
                raise MarkupError("Invalid code language")
            opening = f'<code class="{language}">'
        elif tag == "blockquote" and attributes:
            if attributes["expandable"] not in {None, ""}:
                raise MarkupError("Invalid blockquote attribute")
            opening = "<blockquote expandable>"
        self.stack.append((tag, opening))
        self.tokens.append(("open", opening))

    def handle_endtag(self, tag: str) -> None:
        tag = _ALIASES.get(tag, tag)
        if tag == "span":
            tag = "tg-spoiler"
        if not self.stack or self.stack[-1][0] != tag:
            raise MarkupError("Unbalanced tags")
        self.stack.pop()
        self.tokens.append(("close", f"</{tag}>"))

    def handle_data(self, data: str) -> None:
        self.tokens.append(("text", data))

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        raise MarkupError("Self-closing tags are unsupported")

    def handle_comment(self, data: str) -> None:
        raise MarkupError("Comments are unsupported")

    def handle_decl(self, decl: str) -> None:
        raise MarkupError("Declarations are unsupported")

    def handle_pi(self, data: str) -> None:
        raise MarkupError("Processing instructions are unsupported")

    def unknown_decl(self, data: str) -> None:
        raise MarkupError("Declarations are unsupported")


def _parse(value: str) -> _Parser:
    outside_tags = re.sub(r"</?[A-Za-z][^<>]*>", "", value)
    if "<" in outside_tags or ">" in outside_tags:
        raise MarkupError("Unescaped angle bracket or incomplete tag")
    parser = _Parser()
    parser.feed(value)
    parser.close()
    if parser.stack:
        raise MarkupError("Unclosed tags")
    return parser


def canonicalize(value: str) -> str:
    parser = _parse(value)
    return "".join(escape(value, quote=False) if kind == "text" else value for kind, value in parser.tokens)


def plain_text(value: str) -> str:
    return "".join(value for kind, value in _parse(value).tokens if kind == "text")


def visible_length(value: str) -> int:
    return len(plain_text(value).encode("utf-16-le")) // 2


def extract_urls(value: str) -> set[str]:
    parser = _parse(value)
    text = "".join(value for kind, value in parser.tokens if kind == "text")
    return parser.links | {match.group() for match in _URL.finditer(text)}


def validate_edit(source_html: str, edited_html: str) -> str:
    result = canonicalize(edited_html)
    if not plain_text(result).strip():
        raise MarkupError("Empty candidate")
    source_urls, result_urls = extract_urls(source_html), extract_urls(result)
    if result_urls - source_urls:
        raise MarkupError("Introduced URL")
    return result


class _SafeDecoration(HtmlDecoration):
    def link(self, value: str, link: str) -> str:
        return f'<a href="{escape(link, quote=True)}">{value}</a>'

    def pre_language(self, value: str, language: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_+.-]+", language):
            return self.pre(value)
        return f'<pre><code class="language-{language}">{value}</code></pre>'


def normalize_message(message: Any) -> str | None:
    """Convert Telethon entities using aiogram's supported UTF-16 unparser.

    Telethon 1.45 HTML unparse omits spoilers and adds whitespace to pre blocks.
    aiogram covers both; the small decoration override escapes link attributes
    and emits Telegram's documented code-language attribute.
    """
    text = getattr(message, "message", None)
    if text is None:
        return None
    entity_types = {
        types.MessageEntityBold: "bold", types.MessageEntityItalic: "italic",
        types.MessageEntityUnderline: "underline", types.MessageEntityStrike: "strikethrough",
        types.MessageEntitySpoiler: "spoiler", types.MessageEntityCode: "code",
        types.MessageEntityPre: "pre", types.MessageEntityBlockquote: "blockquote",
    }
    entities: list[MessageEntity] = []
    for entity in getattr(message, "entities", None) or []:
        kind = entity_types.get(type(entity))
        extra: dict[str, Any] = {}
        if isinstance(entity, types.MessageEntityTextUrl):
            if not safe_url(entity.url):
                continue
            kind, extra = "text_link", {"url": entity.url}
        elif isinstance(entity, types.MessageEntityMentionName):
            kind, extra = "text_link", {"url": f"tg://user?id={entity.user_id}"}
        elif isinstance(entity, types.MessageEntityPre):
            extra["language"] = entity.language
        elif isinstance(entity, types.MessageEntityBlockquote) and entity.collapsed:
            kind = "expandable_blockquote"
        if kind:
            entities.append(MessageEntity(type=kind, offset=entity.offset, length=entity.length, **extra))
    return canonicalize(_SafeDecoration().unparse(text, entities))


def split_html(value: str, limit: int = 4096) -> list[str]:
    """Split HTML safely, preferring visible URL boundaries when they fit."""
    if limit < 2:
        raise ValueError("Chunk limit must accommodate a UTF-16 surrogate pair")
    tokens = _parse(value).tokens
    active: list[str] = []
    output: list[str] = []
    current: list[str] = []
    units = 0

    def closures(openings: list[str]) -> list[str]:
        return [f'</{opening[1:].split(">", 1)[0].split(" ", 1)[0]}>' for opening in reversed(openings)]

    def split_chunk() -> None:
        nonlocal current, units
        # Move newly opened wrappers to the next chunk rather than emitting
        # empty entities before the first enclosed character.
        pending_openings = 0
        while current and current[-1].startswith("<") and not current[-1].startswith("</"):
            current.pop()
            pending_openings += 1
        previous_active = active[:-pending_openings] if pending_openings else active
        output.append("".join(current + closures(previous_active)))
        current = list(active)
        units = 0

    for kind, token in tokens:
        if kind == "open":
            active.append(token)
            current.append(token)
        elif kind == "close":
            active.pop()
            current.append(token)
        else:
            url_ranges = [
                (match.start(), match.end(), len(match.group().encode("utf-16-le")) // 2)
                for match in _URL.finditer(token)
            ]
            url_starts = {start: (end, length) for start, end, length in url_ranges}
            for index, character in enumerate(token):
                size = len(character.encode("utf-16-le")) // 2
                url = url_starts.get(index)
                if (
                    url is not None
                    and units
                    and url[1] <= limit
                    and units + url[1] > limit
                ):
                    split_chunk()
                if units + size > limit:
                    split_chunk()
                current.append(escape(character, quote=False))
                units += size
    if units:
        output.append("".join(current))
    return output
