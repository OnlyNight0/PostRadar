"""Focused tests for deterministic source text cleanup."""

import unittest
from types import SimpleNamespace

from postradar.services.sanitizer import sanitize_text


class SanitizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = SimpleNamespace(username="example_channel")

    def test_removes_source_username_mention(self) -> None:
        self.assertEqual(
            sanitize_text("Подпишись на @example_channel", self.source),
            "",
        )

    def test_removes_source_telegram_links(self) -> None:
        cases = (
            "https://t.me/example_channel",
            "http://t.me/example_channel/123",
            "https://telegram.me/example_channel",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(sanitize_text(text, self.source), "")

    def test_preserves_other_channel_references(self) -> None:
        text = "Follow @another_channel and https://t.me/another_channel"
        self.assertEqual(sanitize_text(text, self.source), text)

    def test_preserves_external_web_url(self) -> None:
        text = "Read the announcement at https://openai.com/blog/example"
        self.assertEqual(sanitize_text(text, self.source), text)

    def test_removes_promotional_footer_lines(self) -> None:
        text = "Main update\n\nПодписывайся: @example_channel\nНаш канал — https://t.me/example_channel"
        self.assertEqual(sanitize_text(text, self.source), "Main update")

    def test_removes_source_attribution_line(self) -> None:
        self.assertEqual(
            sanitize_text("News\nИсточник: @example_channel", self.source),
            "News",
        )

    def test_collapses_repeated_blank_lines(self) -> None:
        self.assertEqual(sanitize_text("First\n\n\n\nSecond", self.source), "First\n\nSecond")

    def test_ordinary_text_only_gets_whitespace_normalized(self) -> None:
        text = "  Ordinary prose, with a channel mention in context.  \n\n\nNext line.  "
        self.assertEqual(
            sanitize_text(text, self.source),
            "Ordinary prose, with a channel mention in context.\n\nNext line.",
        )

    def test_none_and_empty_text_are_safe(self) -> None:
        self.assertIsNone(sanitize_text(None, self.source))
        self.assertEqual(sanitize_text("", self.source), "")

    def test_custom_source_footer_pattern(self) -> None:
        self.assertEqual(
            sanitize_text(
                "Useful update\n⚡ TechMedia — subscribe",
                SimpleNamespace(username="techmedia"),
                source_footer_patterns={"techmedia": (r"⚡ TechMedia — subscribe",)},
            ),
            "Useful update",
        )


if __name__ == "__main__":
    unittest.main()
