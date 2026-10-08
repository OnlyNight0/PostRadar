"""Regression tests for real Telegram links, formatting and safe chunking."""

from types import SimpleNamespace

import pytest
from telethon import types

from postradar.services.telegram_markup import (
    MarkupError, canonicalize, extract_urls, normalize_message, plain_text,
    split_html, validate_edit, visible_length,
)


def test_hidden_ticket_url_after_astral_emoji_is_preserved() -> None:
    text = "😀 Узнать про билеты здесь"
    url = "https://tickets.example/event?date=1&ref=2"
    offset = len(text[:text.index("здесь")].encode("utf-16-le")) // 2
    markup = normalize_message(SimpleNamespace(message=text, entities=[types.MessageEntityTextUrl(offset, 5, url)]))
    assert '<a href="https://tickets.example/event?date=1&amp;ref=2">здесь</a>' in markup
    assert plain_text(markup) == text
    assert extract_urls(markup) == {url}


def test_all_supported_formatting_and_pre_whitespace_survive() -> None:
    entities = [
        types.MessageEntityBold(0, 4), types.MessageEntityItalic(5, 4),
        types.MessageEntityUnderline(10, 4), types.MessageEntityStrike(15, 4),
        types.MessageEntitySpoiler(20, 4), types.MessageEntityCode(25, 4),
        types.MessageEntityBlockquote(30, 4), types.MessageEntityPre(35, 4, "python"),
    ]
    text = "bold ital unde stri spoi code quot pre!\n"
    markup = normalize_message(SimpleNamespace(message=text, entities=entities))
    assert plain_text(markup) == text
    for tag in ("b", "i", "u", "s", "tg-spoiler", "code", "blockquote", "pre"):
        assert f"<{tag}" in markup
    assert '<pre><code class="language-python">pre!</code></pre>' in markup


def test_plain_and_hidden_urls_coexist() -> None:
    text = "https://github.com/example/tool здесь"
    markup = normalize_message(SimpleNamespace(message=text, entities=[types.MessageEntityTextUrl(len(text)-5, 5, "https://tickets.example/event")]))
    assert extract_urls(markup) == {"https://github.com/example/tool", "https://tickets.example/event"}
    assert plain_text(markup) == text


@pytest.mark.parametrize("markup", [
    "<b>unfinished", "<b><i>wrong</b></i>", "<script>bad</script>",
    '<a href="javascript:alert(1)">bad</a>', '<a href="data:text/plain,x">bad</a>',
    '<a href="https://example.com" onclick="bad">bad</a>', "<b", "<!--hidden-->",
    "<pre><b>bad</b></pre>", "<code><i>bad</i></code>",
])
def test_invalid_or_unsafe_markup_is_rejected(markup: str) -> None:
    with pytest.raises(MarkupError):
        canonicalize(markup)


def test_existing_url_accepted_exactly_and_new_url_rejected() -> None:
    source = '<a href="https://tickets.example/?a=1&amp;b=2">здесь</a>'
    assert validate_edit(source, source) == source
    with pytest.raises(MarkupError):
        validate_edit(source, '<a href="https://tickets.example/?a=1&amp;b=3">здесь</a>')
    assert validate_edit(source, "Useful content without a promotional URL")


def test_long_nested_formatting_and_link_split_preserves_visible_text() -> None:
    content = "😀 & < > текст\n" * 800
    from html import escape
    markup = '<b><i><a href="https://tickets.example/event">' + escape(content) + "</a></i></b>"
    chunks = split_html(markup)
    assert len(chunks) > 1
    assert "".join(plain_text(chunk) for chunk in chunks) == content
    for chunk in chunks:
        assert canonicalize(chunk) == chunk
        assert visible_length(chunk) <= 4096
        assert extract_urls(chunk) == {"https://tickets.example/event"}


def test_split_moves_ordinary_visible_url_to_next_chunk_when_it_fits() -> None:
    url = "https://tickets.example/event?id=42"
    prefix = "Lead: "
    limit = len(url.encode("utf-16-le")) // 2 + 1
    chunks = split_html(prefix + url + " more text", limit=limit)
    assert chunks[0] == prefix
    assert chunks[1].startswith(url)
    assert "".join(plain_text(chunk) for chunk in chunks) == prefix + url + " more text"
    assert all(visible_length(chunk) <= limit for chunk in chunks)
    assert all(canonicalize(chunk) == chunk for chunk in chunks)


def test_caption_length_uses_visible_utf16_text() -> None:
    from postradar.bot.review import fits_caption
    assert fits_caption('<b>' + '&amp;' * 1024 + '</b>')
    assert not fits_caption('<b>' + '😀' * 513 + '</b>')


def test_unsafe_source_href_preserves_visible_text() -> None:
    markup = normalize_message(SimpleNamespace(message="здесь", entities=[types.MessageEntityTextUrl(0, 5, "javascript:alert(1)")]))
    assert markup == "здесь"


def test_plain_url_query_punctuation_must_remain_exact() -> None:
    source = "Tickets https://tickets.example/?token=abc!"
    assert validate_edit(source, source) == source
    with pytest.raises(MarkupError):
        validate_edit(source, "Tickets https://tickets.example/?token=abc")


def test_new_schemeless_url_is_rejected() -> None:
    with pytest.raises(MarkupError):
        validate_edit("Useful text", "Useful text www.invented.example/buy")


def test_expandable_blockquote_and_nested_formatting_normalize() -> None:
    markup = normalize_message(SimpleNamespace(message="quote", entities=[
        types.MessageEntityBlockquote(0, 5, collapsed=True), types.MessageEntityBold(0, 5),
    ]))
    assert markup == "<blockquote expandable><b>quote</b></blockquote>"


def test_pre_language_without_safe_identifier_preserves_code_text() -> None:
    markup = normalize_message(SimpleNamespace(message="code", entities=[types.MessageEntityPre(0, 4, 'weird " language')]))
    assert markup == "<pre>code</pre>"


def test_invented_plain_url_with_other_scheme_is_rejected() -> None:
    with pytest.raises(MarkupError):
        validate_edit("Useful text", "Download ftp://files.example/archive")


@pytest.mark.parametrize("opening,closing", [
    ("<b>", "</b>"),
    ('<a href="https://example.test/a">', "</a>"),
    ('<b><i><a href="https://example.test/a">', "</a></i></b>"),
])
@pytest.mark.parametrize("prefix,suffix", [("1234", "56"), ("123", "😀")])
def test_split_boundary_does_not_introduce_empty_wrappers(
    opening: str, closing: str, prefix: str, suffix: str,
) -> None:
    chunks = split_html(prefix + opening + suffix + closing, limit=4)
    assert chunks == [prefix, opening + suffix + closing]
    assert "".join(plain_text(chunk) for chunk in chunks) == prefix + suffix
    for chunk in chunks:
        assert canonicalize(chunk) == chunk
        assert visible_length(chunk) <= 4
        assert "<b></b>" not in chunk
        assert "<i></i>" not in chunk
        assert '<a href="https://example.test/a"></a>' not in chunk
