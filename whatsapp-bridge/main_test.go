package main

import (
	"path/filepath"
	"testing"
	"time"

	waProto "go.mau.fi/whatsmeow/binary/proto"
	"google.golang.org/protobuf/proto"
)

// A quote-reply sent as extended text should yield the quoted message's stanza ID,
// the participant who sent the quoted message, and a snippet of the quoted text.
func TestExtractQuotedContext_ExtendedTextReply(t *testing.T) {
	msg := &waProto.Message{
		ExtendedTextMessage: &waProto.ExtendedTextMessage{
			Text: proto.String("yes, this one is correct"),
			ContextInfo: &waProto.ContextInfo{
				StanzaID:    proto.String("ABCD1234"),
				Participant: proto.String("27820001111@s.whatsapp.net"),
				QuotedMessage: &waProto.Message{
					Conversation: proto.String("Is the 25mm gland the right one?"),
				},
			},
		},
	}

	id, text, sender := extractQuotedContext(msg)

	if id != "ABCD1234" {
		t.Errorf("quotedID = %q, want %q", id, "ABCD1234")
	}
	if sender != "27820001111@s.whatsapp.net" {
		t.Errorf("quotedSender = %q, want %q", sender, "27820001111@s.whatsapp.net")
	}
	if text != "Is the 25mm gland the right one?" {
		t.Errorf("quotedText = %q, want the quoted conversation text", text)
	}
}

// A plain message (not a reply) must produce no quoted fields, so non-reply
// messages don't get cluttered with empty/noisy context.
func TestExtractQuotedContext_NotAReply(t *testing.T) {
	msg := &waProto.Message{Conversation: proto.String("just a normal message")}

	id, text, sender := extractQuotedContext(msg)

	if id != "" || text != "" || sender != "" {
		t.Errorf("non-reply should yield empty quoted fields, got id=%q text=%q sender=%q", id, text, sender)
	}
}

// A reply attached to an image (caption) carries contextInfo on the image message.
func TestExtractQuotedContext_ImageCaptionReply(t *testing.T) {
	msg := &waProto.Message{
		ImageMessage: &waProto.ImageMessage{
			Caption: proto.String("here's the photo you asked for"),
			ContextInfo: &waProto.ContextInfo{
				StanzaID:    proto.String("IMG999"),
				Participant: proto.String("27820002222@s.whatsapp.net"),
				QuotedMessage: &waProto.Message{
					Conversation: proto.String("can you send a picture?"),
				},
			},
		},
	}

	id, text, sender := extractQuotedContext(msg)

	if id != "IMG999" || sender != "27820002222@s.whatsapp.net" || text != "can you send a picture?" {
		t.Errorf("image-caption reply: got id=%q sender=%q text=%q", id, sender, text)
	}
}

// When replying TO an image that had a caption, the snippet should announce it's an
// image AND include the caption — so a reply to one of several product photos is
// unambiguous rather than reading like a plain text message.
func TestExtractQuotedContext_QuotedImageKeepsCaptionWithMarker(t *testing.T) {
	msg := &waProto.Message{
		ExtendedTextMessage: &waProto.ExtendedTextMessage{
			Text: proto.String("yes this one"),
			ContextInfo: &waProto.ContextInfo{
				StanzaID:    proto.String("X2"),
				Participant: proto.String("27820004444@s.whatsapp.net"),
				QuotedMessage: &waProto.Message{
					ImageMessage: &waProto.ImageMessage{Caption: proto.String("Option A - 25mm gland")},
				},
			},
		},
	}

	_, text, _ := extractQuotedContext(msg)

	if text != "[image] Option A - 25mm gland" {
		t.Errorf("quoted captioned image: text = %q, want %q", text, "[image] Option A - 25mm gland")
	}
}

// A reply to a media message with no caption should fall back to a type marker
// rather than an empty snippet.
func TestExtractQuotedContext_QuotedMediaFallsBackToMarker(t *testing.T) {
	msg := &waProto.Message{
		ExtendedTextMessage: &waProto.ExtendedTextMessage{
			Text: proto.String("got it"),
			ContextInfo: &waProto.ContextInfo{
				StanzaID:      proto.String("X1"),
				Participant:   proto.String("27820003333@s.whatsapp.net"),
				QuotedMessage: &waProto.Message{ImageMessage: &waProto.ImageMessage{}},
			},
		},
	}

	_, text, _ := extractQuotedContext(msg)

	if text != "[image]" {
		t.Errorf("quoted captionless image: text = %q, want %q", text, "[image]")
	}
}

// StoreMessage must persist the three quoted columns so the Python reader can surface them.
func TestStoreMessage_PersistsQuotedColumns(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "messages.db")
	store, err := newMessageStoreAt(dbPath)
	if err != nil {
		t.Fatalf("newMessageStoreAt: %v", err)
	}
	defer store.Close()

	// The messages table has a foreign key to chats, so the chat must exist first
	// (the live handler calls StoreChat before StoreMessage for the same reason).
	if err := store.StoreChat("chat@s.whatsapp.net", "Test Chat", time.Now()); err != nil {
		t.Fatalf("StoreChat: %v", err)
	}

	err = store.StoreMessage(
		"MSG1", "chat@s.whatsapp.net", "27820001111", "yes, this one is correct",
		time.Now(), false, "", "", "", nil, nil, nil, 0,
		"QUOTED1", "the original question", "27820009999@s.whatsapp.net",
	)
	if err != nil {
		t.Fatalf("StoreMessage: %v", err)
	}

	var qid, qtext, qsender string
	row := store.db.QueryRow("SELECT quoted_message_id, quoted_text, quoted_sender FROM messages WHERE id = ?", "MSG1")
	if err := row.Scan(&qid, &qtext, &qsender); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if qid != "QUOTED1" || qtext != "the original question" || qsender != "27820009999@s.whatsapp.net" {
		t.Errorf("stored quoted cols = (%q, %q, %q)", qid, qtext, qsender)
	}
}

// A live inbound message must persist the message body and the chat's
// last_message_time together (single transaction), so an external reader can
// never observe a chat whose last_message_time is newer than its newest stored
// message. This is the core fix for the message-storage divergence bug.
func TestStoreChatAndMessage_NoDivergence(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "messages.db")
	store, err := newMessageStoreAt(dbPath)
	if err != nil {
		t.Fatalf("newMessageStoreAt: %v", err)
	}
	defer store.Close()

	ts := time.Now().Truncate(time.Second)
	if err := store.StoreChatAndMessage(
		"chat@s.whatsapp.net", "Sender Name",
		"MSG1", "27820001111", "hello there", ts, false,
		"", "", "", nil, nil, nil, 0,
		"", "", "",
	); err != nil {
		t.Fatalf("StoreChatAndMessage: %v", err)
	}

	// The chat row must exist with the message's timestamp...
	var chatLMT time.Time
	if err := store.db.QueryRow("SELECT last_message_time FROM chats WHERE jid=?", "chat@s.whatsapp.net").Scan(&chatLMT); err != nil {
		t.Fatalf("chat row missing after atomic store: %v", err)
	}
	// ...and the message row must exist with that same timestamp. Read the typed
	// column directly (not MAX(), which returns an untyped string the driver can't
	// scan into time.Time).
	var msgTS time.Time
	if err := store.db.QueryRow("SELECT timestamp FROM messages WHERE chat_jid=? ORDER BY timestamp DESC LIMIT 1", "chat@s.whatsapp.net").Scan(&msgTS); err != nil {
		t.Fatalf("message row missing after atomic store: %v", err)
	}
	if chatLMT.After(msgTS) {
		t.Errorf("divergence: chat last_message_time %v is newer than newest stored message %v", chatLMT, msgTS)
	}
}

// A contentless message (no text, no media — e.g. a reaction or receipt) must
// NOT create a chat row or advance last_message_time, otherwise the chat would
// claim a message it never stored.
func TestStoreChatAndMessage_SkipsContentless(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "messages.db")
	store, err := newMessageStoreAt(dbPath)
	if err != nil {
		t.Fatalf("newMessageStoreAt: %v", err)
	}
	defer store.Close()

	if err := store.StoreChatAndMessage(
		"chat@s.whatsapp.net", "Sender Name",
		"MSG1", "27820001111", "", time.Now(), false,
		"", "", "", nil, nil, nil, 0,
		"", "", "",
	); err != nil {
		t.Fatalf("StoreChatAndMessage: %v", err)
	}

	var n int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM chats WHERE jid=?", "chat@s.whatsapp.net").Scan(&n); err != nil {
		t.Fatalf("count chats: %v", err)
	}
	if n != 0 {
		t.Errorf("contentless message created a chat row (n=%d); expected none", n)
	}
}

// The atomic store must also persist quoted-reply columns, so a quoted reply
// arriving on the live path is fully recorded in one transaction.
func TestStoreChatAndMessage_PersistsQuotedColumns(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "messages.db")
	store, err := newMessageStoreAt(dbPath)
	if err != nil {
		t.Fatalf("newMessageStoreAt: %v", err)
	}
	defer store.Close()

	if err := store.StoreChatAndMessage(
		"chat@s.whatsapp.net", "Sender Name",
		"MSG1", "27820001111", "yes, this one", time.Now(), false,
		"", "", "", nil, nil, nil, 0,
		"QUOTED1", "the original question", "27820009999@s.whatsapp.net",
	); err != nil {
		t.Fatalf("StoreChatAndMessage: %v", err)
	}

	var qid, qtext, qsender string
	row := store.db.QueryRow("SELECT quoted_message_id, quoted_text, quoted_sender FROM messages WHERE id=?", "MSG1")
	if err := row.Scan(&qid, &qtext, &qsender); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if qid != "QUOTED1" || qtext != "the original question" || qsender != "27820009999@s.whatsapp.net" {
		t.Errorf("stored quoted cols = (%q, %q, %q)", qid, qtext, qsender)
	}
}

// A new message must advance a chat regardless of the text format its previous
// last_message_time was stored in. Older rows in the live DB use RFC3339 "T" format
// while new writes use SQLite's space-separated format; the upsert sets the new value
// unconditionally, so the chat always reflects its latest message (the MCP reader
// normalizes the mixed formats for ordering via datetime()). This guards against
// reintroducing a lexical comparison that would wrongly rank ' ' below 'T'.
func TestStoreChatAndMessage_AdvancesPastLegacyTFormatTimestamp(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "messages.db")
	store, err := newMessageStoreAt(dbPath)
	if err != nil {
		t.Fatalf("newMessageStoreAt: %v", err)
	}
	defer store.Close()

	// Seed a chat with a legacy "T"-format last_message_time (as older rows in the
	// live DB actually have).
	legacy := "2026-05-25T10:00:00.123456+08:00"
	if _, err := store.db.Exec(
		"INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		"chat@s.whatsapp.net", "Vendor", legacy,
	); err != nil {
		t.Fatalf("seed legacy chat: %v", err)
	}

	newer := time.Date(2026, 5, 25, 13, 0, 0, 0, time.FixedZone("WITA", 8*3600))
	if err := store.StoreChatAndMessage(
		"chat@s.whatsapp.net", "Vendor",
		"MSG1", "27820001111", "newer reply", newer, false,
		"", "", "", nil, nil, nil, 0, "", "", "",
	); err != nil {
		t.Fatalf("StoreChatAndMessage: %v", err)
	}

	var lmt time.Time
	if err := store.db.QueryRow("SELECT last_message_time FROM chats WHERE jid=?", "chat@s.whatsapp.net").Scan(&lmt); err != nil {
		t.Fatalf("read lmt: %v", err)
	}
	if !lmt.Equal(newer) {
		t.Errorf("last_message_time = %v, want %v (must advance past a legacy T-format timestamp)", lmt, newer)
	}
}

// Re-opening an existing DB must not fail: the additive migration tolerates
// columns that already exist (no wipe, no error).
func TestNewMessageStoreAt_MigrationIdempotent(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "messages.db")

	s1, err := newMessageStoreAt(dbPath)
	if err != nil {
		t.Fatalf("first open: %v", err)
	}
	s1.Close()

	s2, err := newMessageStoreAt(dbPath)
	if err != nil {
		t.Fatalf("second open (migration not idempotent): %v", err)
	}
	s2.Close()
}
