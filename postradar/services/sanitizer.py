"""Deterministic, conservative cleanup for source-specific Telegram clutter."""

import re
from collections.abc import Mapping, Sequence
from typing import Any

# Add source usernames here when a recurring footer is unique to that source.
SOURCE_SPECIFIC_FOOTER_PATTERNS: dict[str, tuple[str, ...]] = {}

_PROMOTIONAL_START = re.compile(
    r"^\s*(?:подписаться|подпишись|подписывайся)\b", re.IGNORECASE
)
_SOURCE_PROMO_START = re.compile(r"^\s*(?:наш\s+канал|мы\s+в\s+telegram)\b", re.IGNORECASE)
_SOURCE_LABEL = re.compile(r"^\s*источник\s*[:—-]", re.IGNORECASE)
_DANGLING_LABEL = re.compile(
    r"^\s*(?:read\s+more|подробнее|читать\s+далее|источник|"
    r"подписаться|подпишись|подписывайся)\s*[:—-]?\s*$",
    re.IGNORECASE,
)


def _username(source: Any) -> str | None:
    value = source if isinstance(source, str) else getattr(source, "username", None)
    if not value:
        return None
    return str(value).lstrip("@").strip() or None


def _source_reference_patterns(username: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    escaped = re.escape(username)
    mention = re.compile(rf"(?<![\w@])@{escaped}(?!\w)", re.IGNORECASE)
    link = re.compile(
        rf"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/{escaped}"
        rf"(?:/\d+)?(?:\?[\w=&%.-]*)?"
        rf"(?![\w])"
    )
    return mention, link


def _is_promotional_line(line: str, username: str | None) -> bool:
    if _PROMOTIONAL_START.match(line):
        return True
    if username is None:
        return False

    mention, link = _source_reference_patterns(username)
    contains_source_reference = bool(mention.search(line) or link.search(line))
    if _SOURCE_LABEL.match(line) and contains_source_reference:
        return True
    return bool(_SOURCE_PROMO_START.match(line) and contains_source_reference)


def sanitize_text(
    text: str | None,
    source: Any = None,
    *,
    source_footer_patterns: Mapping[str, Sequence[str]] | None = None,
) -> str | None:
    """Remove obvious source promotion while preserving body text and other links.

    ``source_footer_patterns`` maps source usernames to regular expressions that
    match complete footer lines. It can be supplied by tests or small local rules.
    """
    if text is None or text == "":
        return text

    username = _username(source)
    custom_patterns = source_footer_patterns or SOURCE_SPECIFIC_FOOTER_PATTERNS
    source_patterns = custom_patterns.get(username.lower(), ()) if username else ()
    compiled_custom = [re.compile(pattern, re.IGNORECASE) for pattern in source_patterns]

    output_lines: list[str] = []
    mention_pattern: re.Pattern[str] | None = None
    link_pattern: re.Pattern[str] | None = None
    if username:
        mention_pattern, link_pattern = _source_reference_patterns(username)

    for original_line in text.splitlines():
        if not original_line.strip():
            output_lines.append("")
            continue
        if _is_promotional_line(original_line, username) or any(
            pattern.fullmatch(original_line.strip()) for pattern in compiled_custom
        ):
            continue

        line = original_line
        if link_pattern:
            line = link_pattern.sub("", line)
        if mention_pattern:
            line = mention_pattern.sub("", line)

        line = re.sub(r"\(\s*\)|\[\s*\]", "", line)
        line = re.sub(r"[ \t]+([,;:])", r"\1", line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        line = re.sub(r"(?:\s*[,;:—-])+$", "", line).rstrip()
        if not line or _DANGLING_LABEL.fullmatch(line):
            continue
        output_lines.append(line)

    cleaned = "\n".join(output_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned
