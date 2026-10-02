"""Unit tests for src.channels.conversation_policy."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from src.channels.conversation_policy import (
    ConversationPolicy,
    policy_for,
    quote_reply,
)


class PolicyForTests(unittest.TestCase):
    def test_telegram(self):
        policy = policy_for("telegram")
        self.assertEqual(policy.reply_style, "native_reply")
        self.assertTrue(policy.edit_text)
        self.assertTrue(policy.edit_caption)
        self.assertTrue(policy.delete_sent)
        self.assertFalse(policy.customer_typing)
        self.assertTrue(policy.outbound_typing)

    def test_slack(self):
        policy = policy_for("slack")
        self.assertEqual(policy.reply_style, "thread")
        self.assertTrue(policy.edit_text)
        self.assertFalse(policy.edit_caption)
        self.assertTrue(policy.delete_sent)
        self.assertFalse(policy.customer_typing)
        self.assertFalse(policy.outbound_typing)

    def test_email(self):
        policy = policy_for("email")
        self.assertEqual(policy.reply_style, "email_thread")
        self.assertFalse(policy.edit_text)
        self.assertFalse(policy.edit_caption)
        self.assertFalse(policy.delete_sent)
        self.assertFalse(policy.customer_typing)
        self.assertFalse(policy.outbound_typing)

    def test_widget(self):
        policy = policy_for("widget")
        self.assertEqual(policy.reply_style, "local_reply")
        self.assertTrue(policy.edit_text)
        self.assertFalse(policy.edit_caption)
        self.assertTrue(policy.delete_sent)
        self.assertTrue(policy.customer_typing)
        self.assertTrue(policy.outbound_typing)

    def test_unknown_channel_is_conservative(self):
        policy = policy_for("carrier_pigeon")
        self.assertEqual(policy.reply_style, "quote")
        self.assertFalse(policy.edit_text)
        self.assertFalse(policy.edit_caption)
        self.assertFalse(policy.delete_sent)
        self.assertFalse(policy.customer_typing)
        self.assertFalse(policy.outbound_typing)

    def test_unknown_channel_empty_string(self):
        policy = policy_for("")
        self.assertEqual(policy.reply_style, "quote")

    def test_policy_is_frozen(self):
        policy = policy_for("telegram")
        with self.assertRaises(FrozenInstanceError):
            policy.edit_text = False  # type: ignore[misc]

    def test_channel_lookups_return_distinct_reply_styles(self):
        styles = {
            policy_for(name).reply_style
            for name in ("telegram", "slack", "email", "widget")
        }
        self.assertEqual(
            styles, {"native_reply", "thread", "email_thread", "local_reply"}
        )


class QuoteReplyTests(unittest.TestCase):
    def test_basic_quote(self):
        result = quote_reply("Alice", "Hello there")
        self.assertEqual(result, "> Alice\n> Hello there\n\n")

    def test_normalizes_internal_whitespace(self):
        result = quote_reply("Bob", "  Hello,\n\tworld!   again  ")
        self.assertEqual(result, "> Bob\n> Hello, world! again\n\n")

    def test_normalizes_author_whitespace(self):
        result = quote_reply("  Carol   Jones  ", "hi")
        self.assertEqual(result, "> Carol Jones\n> hi\n\n")

    def test_author_bounded_to_sixty_chars(self):
        long_author = "A" * 100
        result = quote_reply(long_author, "hi")
        author_line = result.splitlines()[0]
        self.assertEqual(author_line, "> " + "A" * 60)

    def test_snippet_bounded_to_limit(self):
        long_text = "x" * 500
        result = quote_reply("Dana", long_text, limit=10)
        snippet_line = result.splitlines()[1]
        self.assertEqual(snippet_line, "> " + "x" * 10)

    def test_snippet_exactly_at_limit_not_truncated_further(self):
        text = "y" * 240
        result = quote_reply("Eve", text)
        snippet_line = result.splitlines()[1]
        self.assertEqual(snippet_line, "> " + "y" * 240)

    def test_blank_text_returns_empty_string(self):
        self.assertEqual(quote_reply("Frank", ""), "")
        self.assertEqual(quote_reply("Frank", "   \n\t  "), "")

    def test_blank_author_omits_author_line(self):
        result = quote_reply("   ", "just the text")
        self.assertEqual(result, "> just the text\n\n")

    def test_empty_author_omits_author_line(self):
        result = quote_reply("", "just the text")
        self.assertEqual(result, "> just the text\n\n")

    def test_blank_line_follows_quote(self):
        result = quote_reply("Gina", "note")
        self.assertTrue(result.endswith("\n\n"))
        self.assertEqual(result.count("\n\n"), 1)

    def test_zero_limit_raises(self):
        with self.assertRaises(ValueError):
            quote_reply("Hank", "text", limit=0)

    def test_negative_limit_raises(self):
        with self.assertRaises(ValueError):
            quote_reply("Hank", "text", limit=-5)

    def test_does_not_interpret_or_escape_markup(self):
        text = "<script>alert(1)</script> & <b>bold</b>"
        result = quote_reply("Ivy", text)
        self.assertIn("<script>alert(1)</script> & <b>bold</b>", result)

    def test_every_present_line_is_prefixed(self):
        result = quote_reply("Jack", "hello world")
        content_lines = [line for line in result.splitlines() if line]
        for line in content_lines:
            self.assertTrue(line.startswith("> "))

    def test_quote_dataclass_is_reusable_value(self):
        self.assertIsInstance(policy_for("telegram"), ConversationPolicy)


if __name__ == "__main__":
    unittest.main()
