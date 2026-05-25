"""Tests for quoted/reply-context formatting in the WhatsApp MCP reader."""
import unittest
from datetime import datetime

from whatsapp import (
    format_quoted_prefix,
    format_message,
    Message,
    MCP_SERVER_VERSION,
    _build_version_info,
)


class FormatQuotedPrefixTest(unittest.TestCase):
    def test_short_text_reads_naturally(self):
        result = format_quoted_prefix("Mongkok Supplier", "yes, this one is correct")
        self.assertEqual(result, '↳ replying to Mongkok Supplier: "yes, this one is correct"')

    def test_long_text_is_truncated_with_ellipsis(self):
        long_text = "x" * 200
        result = format_quoted_prefix("Supplier", long_text, max_len=80)
        # The quoted snippet (between the quotes) must be bounded.
        snippet = result.split('"')[1]
        self.assertTrue(snippet.endswith("…"))
        self.assertLessEqual(len(snippet), 81)  # 80 chars + ellipsis

    def test_newlines_are_collapsed_to_single_line(self):
        result = format_quoted_prefix("Supplier", "first line\nsecond line")
        self.assertEqual(result, '↳ replying to Supplier: "first line second line"')

    def test_missing_sender_falls_back_to_placeholder(self):
        result = format_quoted_prefix("", "hello")
        self.assertEqual(result, '↳ replying to ?: "hello"')


class FormatMessageQuotedTest(unittest.TestCase):
    def _msg(self, **overrides):
        base = dict(
            timestamp=datetime(2026, 5, 21, 10, 0, 0),
            sender="me",
            content="yes this one",
            is_from_me=True,  # avoids a DB lookup for the sender name
            chat_jid="c@s.whatsapp.net",
            id="1",
        )
        base.update(overrides)
        return Message(**base)

    def test_prepends_quoted_line_when_replying(self):
        out = format_message(
            self._msg(quoted_text="Option A - 25mm gland", quoted_sender=None),
            show_chat_info=False,
        )
        self.assertIn('↳ replying to ?: "Option A - 25mm gland"', out)
        self.assertIn("From: Me: yes this one", out)

    def test_no_quoted_line_for_a_normal_message(self):
        out = format_message(self._msg(content="hello"), show_chat_info=False)
        self.assertNotIn("↳ replying to", out)


class VersionInfoTest(unittest.TestCase):
    def test_reports_the_mcp_server_version(self):
        info = _build_version_info("1.1.0-quoted-reply")
        self.assertEqual(info["mcp_server_version"], MCP_SERVER_VERSION)

    def test_passes_through_bridge_version_when_known(self):
        info = _build_version_info("1.1.0-quoted-reply")
        self.assertEqual(info["bridge_version"], "1.1.0-quoted-reply")

    def test_marks_bridge_version_unknown_when_missing(self):
        info = _build_version_info(None)
        self.assertTrue(info["bridge_version"].startswith("unknown"))

    def test_advertises_quoted_reply_feature(self):
        info = _build_version_info(None)
        self.assertIn("quoted-reply-context", info["features"])


if __name__ == "__main__":
    unittest.main()
