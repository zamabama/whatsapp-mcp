"""Regression test: messages must order chronologically even when the DB stores
timestamps in two different string formats.

The Go bridge writes incoming messages as space-separated (`2026-05-21 18:19:50+08:00`),
while outbound messages logged by the Python side use ISO 'T' format
(`2026-05-21T18:01:44.411492+08:00`). SQLite compares TEXT lexicographically, and a
space (0x20) sorts before 'T' (0x54) — so a later incoming message sorts as if it were
older than an earlier outbound one, and `list_messages` silently drops it.
"""
import os
import sqlite3
import tempfile
import unittest

import whatsapp


class MessageOrderingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "messages.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
            CREATE TABLE messages (
                id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
                is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT, media_key BLOB,
                file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
                quoted_message_id TEXT, quoted_text TEXT, quoted_sender TEXT,
                PRIMARY KEY (id, chat_jid)
            );
            INSERT INTO chats VALUES ('c@s.whatsapp.net', 'Supplier', '2026-05-21 18:19:50+08:00');
            -- EARLIER, outbound, 'T'+microseconds (Python-logged format)
            INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type)
              VALUES ('OUT', 'c@s.whatsapp.net', 'me', 'earlier outbound', '2026-05-21T18:01:44.411492+08:00', 1, '');
            -- LATER, incoming, space format (Go bridge format)
            INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type)
              VALUES ('IN', 'c@s.whatsapp.net', '123', 'seperti ini yaa pak', '2026-05-21 18:19:50+08:00', 0, '');
            """
        )
        conn.commit()
        conn.close()
        self._orig = whatsapp.MESSAGES_DB_PATH
        whatsapp.MESSAGES_DB_PATH = self.db

    def tearDown(self):
        whatsapp.MESSAGES_DB_PATH = self._orig

    def test_latest_message_surfaces_even_when_space_formatted(self):
        # Asking for the single most recent message must return the 18:19 incoming
        # message, not the 18:01 outbound one that merely sorts higher as text.
        out = whatsapp.list_messages(chat_jid="c@s.whatsapp.net", limit=1, include_context=False)
        self.assertIn("seperti ini yaa pak", out)
        self.assertNotIn("earlier outbound", out)

    def test_after_filter_includes_later_space_formatted_message(self):
        # A time window starting at 18:10 must include the 18:19 incoming message.
        out = whatsapp.list_messages(
            chat_jid="c@s.whatsapp.net", after="2026-05-21T18:10:00+08:00",
            include_context=False, limit=20,
        )
        self.assertIn("seperti ini yaa pak", out)
        # ...and must EXCLUDE the 18:01 outbound (it's before the window).
        self.assertNotIn("earlier outbound", out)


if __name__ == "__main__":
    unittest.main()
