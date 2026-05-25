import os
import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio

_STORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store')
MESSAGES_DB_PATH = os.path.join(_STORE_DIR, 'messages.db')
WHATSAPP_DB_PATH = os.path.join(_STORE_DIR, 'whatsapp.db')
LOCAL_CONTACTS_PATH = os.path.join(_STORE_DIR, 'local_contacts.json')
NEEDS_AUTH_FLAG_PATH = os.path.join(_STORE_DIR, 'NEEDS_AUTH')


def _resolve_bridge_port() -> str:
    """Find the port the co-located bridge listens on, so one codebase serves multiple
    accounts. Order: WHATSAPP_BRIDGE_PORT env -> store/bridge_port.txt written by the
    bridge -> 8080 default. This keeps the per-account port out of the source and out of
    every project's .mcp.json."""
    env_port = os.environ.get("WHATSAPP_BRIDGE_PORT")
    if env_port and env_port.strip().isdigit():
        return env_port.strip()
    try:
        with open(os.path.join(_STORE_DIR, 'bridge_port.txt')) as f:
            file_port = f.read().strip()
            if file_port.isdigit():
                return file_port
    except OSError:
        pass
    return "8080"


WHATSAPP_BRIDGE_PORT = _resolve_bridge_port()
WHATSAPP_API_BASE_URL = f"http://localhost:{WHATSAPP_BRIDGE_PORT}/api"

# Bump this whenever the MCP reader's behaviour changes, so an agent can self-report
# whether it is running the updated code. An agent that returns this string from the
# `whatsapp_version` tool is on the new build; an agent whose tool list lacks that tool
# is on an old build and needs its session/MCP server reloaded.
MCP_SERVER_VERSION = "1.2.0-message-ordering"


def _build_version_info(bridge_version: Optional[str]) -> dict:
    """Assemble the version payload. Pure (no I/O) so it can be unit-tested."""
    return {
        "mcp_server_version": MCP_SERVER_VERSION,
        "bridge_version": bridge_version or "unknown (bridge did not report a version)",
        "features": ["quoted-reply-context"],
    }


def get_version() -> dict:
    """Report the MCP reader version and, best-effort, the live bridge binary version.

    The MCP server version is what determines whether THIS agent renders quoted-reply
    context (it runs per-session). The bridge version reflects the shared background
    binary that captures the data. Both should read "1.1.0-quoted-reply" when updated.
    """
    bridge_version = None
    try:
        resp = requests.get(f"{WHATSAPP_API_BASE_URL}/health", timeout=3)
        if resp.ok:
            bridge_version = resp.json().get("version")
    except requests.RequestException:
        bridge_version = None
    return _build_version_info(bridge_version)


def check_bridge_health() -> str:
    """Check if the WhatsApp bridge is running and authenticated.

    Returns:
        'ok' if healthy, or an error message string if not.
    """
    # Check for NEEDS_AUTH flag file first (survives bridge restarts)
    if os.path.isfile(NEEDS_AUTH_FLAG_PATH):
        return ("BRIDGE AUTH EXPIRED: The WhatsApp bridge needs re-authentication. "
                "To fix: 1) Run 'launchctl unload ~/Library/LaunchAgents/com.garuda.whatsapp-bridge.plist' "
                "2) Run the bridge manually: ~/.claude/mcp-servers/whatsapp-mcp/whatsapp-bridge/whatsapp-bridge "
                "3) Scan the QR code with WhatsApp Business app "
                "4) After success, reload: 'launchctl load ~/Library/LaunchAgents/com.garuda.whatsapp-bridge.plist'")
    try:
        resp = requests.get(f"{WHATSAPP_API_BASE_URL}/health", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "unknown")
            if status == "ok":
                return "ok"
            elif status == "needs_reauth":
                return ("BRIDGE AUTH EXPIRED: The WhatsApp bridge is running but not authenticated. "
                        "Stop the LaunchAgent, run the bridge manually, scan QR code, then restart the agent.")
            elif status == "disconnected":
                return ("BRIDGE DISCONNECTED: The WhatsApp bridge is running but not connected to WhatsApp servers. "
                        "It may be reconnecting automatically. Try again in a minute.")
            else:
                return f"BRIDGE UNHEALTHY: status={status}"
        else:
            return f"BRIDGE ERROR: health endpoint returned HTTP {resp.status_code}"
    except requests.ConnectionError:
        return (f"BRIDGE NOT RUNNING: Cannot connect to the WhatsApp bridge on localhost:{WHATSAPP_BRIDGE_PORT}. "
                "Check if the LaunchAgent is loaded: 'launchctl list | grep whatsapp'")
    except requests.Timeout:
        return "BRIDGE TIMEOUT: Health check timed out after 3 seconds."
    except Exception as e:
        return f"BRIDGE ERROR: {str(e)}"


def _connect_db(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode so we can read the Go bridge's writes."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_quoted_columns(conn: sqlite3.Connection) -> None:
    """Make sure the quoted/reply-context columns exist before we read them.

    The Go bridge owns the schema and adds these on startup, but if the MCP server
    is (re)loaded before the bridge has restarted, the columns may not exist yet.
    This additive migration is idempotent — it tolerates columns that already exist.
    """
    for column in ("quoted_message_id", "quoted_text", "quoted_sender"):
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def _load_local_contacts() -> dict:
    """Load the local contacts mapping file (phone_number -> name)."""
    if os.path.isfile(LOCAL_CONTACTS_PATH):
        try:
            with open(LOCAL_CONTACTS_PATH, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_local_contacts(contacts: dict) -> None:
    """Save the local contacts mapping file."""
    with open(LOCAL_CONTACTS_PATH, 'w') as f:
        json.dump(contacts, f, indent=2)


def resolve_lid_to_phone(lid_jid: str) -> Optional[str]:
    """Resolve a LID JID (e.g. '44543760154755@lid') to a phone number.

    Returns the phone number string (e.g. '447928545139') or None if not found.
    """
    try:
        if '@' in lid_jid:
            lid_num = lid_jid.split('@')[0]
            # Strip any :N suffix (e.g. 44543760154755:2)
            lid_num = lid_num.split(':')[0]
        else:
            lid_num = lid_jid.split(':')[0]

        conn = _connect_db(WHATSAPP_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (lid_num,))
        result = cursor.fetchone()
        conn.close()

        if result:
            return str(result[0])
        return None
    except (sqlite3.Error, ValueError):
        return None


def resolve_phone_to_lid(phone: str) -> Optional[str]:
    """Resolve a phone number to a LID number.

    Returns the LID string (e.g. '44543760154755') or None if not found.
    """
    try:
        # Strip any non-digit characters and leading +
        phone_clean = phone.lstrip('+').replace(' ', '').replace('-', '')

        conn = _connect_db(WHATSAPP_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ?", (phone_clean,))
        result = cursor.fetchone()
        conn.close()

        if result:
            return str(result[0])
        return None
    except (sqlite3.Error, ValueError):
        return None


def get_whatsmeow_contact_name(jid: str) -> Optional[str]:
    """Get push_name or business_name from whatsmeow_contacts table."""
    try:
        conn = _connect_db(WHATSAPP_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT push_name, business_name, full_name, first_name FROM whatsmeow_contacts WHERE their_jid = ?",
            (jid,)
        )
        result = cursor.fetchone()
        conn.close()

        if result:
            # Return first non-empty name found
            for name in result:
                if name:
                    return name
        return None
    except sqlite3.Error:
        return None

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None
    # Quoted/reply context: set when this message is a reply to an earlier one.
    quoted_id: Optional[str] = None
    quoted_text: Optional[str] = None
    quoted_sender: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

def get_sender_name(sender_jid: str) -> str:
    try:
        # Check local contacts first (our custom name mappings)
        local_contacts = _load_local_contacts()

        # If it's a LID, resolve to phone number for local contact lookup
        phone_for_lookup = None
        if '@lid' in sender_jid or (sender_jid.isdigit() and len(sender_jid) > 10):
            phone_for_lookup = resolve_lid_to_phone(sender_jid)
        elif '@s.whatsapp.net' in sender_jid:
            phone_for_lookup = sender_jid.split('@')[0]

        if phone_for_lookup and phone_for_lookup in local_contacts:
            return local_contacts[phone_for_lookup]

        # Check whatsmeow_contacts for push_name/business_name
        wm_name = get_whatsmeow_contact_name(sender_jid)
        if wm_name:
            # Enrich with phone number if LID
            if phone_for_lookup and '@lid' in sender_jid:
                return f"{wm_name} (+{phone_for_lookup})"
            return wm_name

        # If it's a LID, also check whatsmeow_contacts by the phone JID
        if phone_for_lookup and '@lid' in sender_jid:
            phone_jid = f"{phone_for_lookup}@s.whatsapp.net"
            wm_name = get_whatsmeow_contact_name(phone_jid)
            if wm_name:
                return f"{wm_name} (+{phone_for_lookup})"

        conn = _connect_db(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        # First try matching by exact JID
        cursor.execute("""
            SELECT name
            FROM chats
            WHERE jid = ?
            LIMIT 1
        """, (sender_jid,))

        result = cursor.fetchone()

        # If no result, try looking for the number within JIDs
        if not result:
            # Extract the phone number part if it's a JID
            if '@' in sender_jid:
                phone_part = sender_jid.split('@')[0]
            else:
                phone_part = sender_jid

            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid LIKE ?
                LIMIT 1
            """, (f"%{phone_part}%",))

            result = cursor.fetchone()

        # If still no result and it's a LID, try finding by resolved phone number
        if not result and phone_for_lookup:
            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid LIKE ?
                LIMIT 1
            """, (f"%{phone_for_lookup}%",))
            result = cursor.fetchone()

        if result and result[0]:
            name = result[0]
            # Append phone number for LID-based chats for clarity
            if phone_for_lookup and '@lid' in sender_jid:
                return f"{name} (+{phone_for_lookup})"
            return name

        # Last resort: if LID, return with phone number
        if phone_for_lookup:
            return f"+{phone_for_lookup}"

        return sender_jid

    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

def format_quoted_prefix(sender_name: str, quoted_text: str, max_len: int = 80) -> str:
    """Render the '↳ replying to ...' line shown above a reply message.

    Pure string formatting (no DB) so it can be unit-tested directly. The snippet is
    collapsed to a single line and bounded so long quoted messages stay readable.
    """
    snippet = (quoted_text or "").replace("\n", " ").strip()
    if len(snippet) > max_len:
        snippet = snippet[:max_len].rstrip() + "…"
    who = sender_name or "?"
    return f'↳ replying to {who}: "{snippet}"'


def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    
    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        # If this message is a reply, show what it's replying to on its own line above.
        quoted_text = getattr(message, "quoted_text", None)
        if quoted_text:
            quoted_sender_jid = getattr(message, "quoted_sender", None)
            quoted_name = get_sender_name(quoted_sender_jid) if quoted_sender_jid else "?"
            output += "  " + format_quoted_prefix(quoted_name, quoted_text) + "\n"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        # Check bridge health before saying "no messages"
        health = check_bridge_health()
        if health != "ok":
            output += f"⚠️ {health}\n\n"
            output += "Messages may exist but cannot be retrieved due to the bridge issue above."
        else:
            output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = _connect_db(MESSAGES_DB_PATH)
        _ensure_quoted_columns(conn)
        cursor = conn.cursor()

        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, messages.quoted_message_id, messages.quoted_text, messages.quoted_sender FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            # datetime() normalizes the mixed stored formats (space- vs 'T'-separated,
            # with timezone offset) so the comparison is chronological, not lexical.
            where_clauses.append("datetime(messages.timestamp) > datetime(?)")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("datetime(messages.timestamp) < datetime(?)")
            params.append(before)

        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY datetime(messages.timestamp) DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7],
                quoted_id=msg[8],
                quoted_text=msg[9],
                quoted_sender=msg[10]
            )
            result.append(message)
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = _connect_db(MESSAGES_DB_PATH)
        _ensure_quoted_columns(conn)
        cursor = conn.cursor()

        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type, messages.quoted_message_id, messages.quoted_text, messages.quoted_sender
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()

        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")

        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8],
            quoted_id=msg_data[9],
            quoted_text=msg_data[10],
            quoted_sender=msg_data[11]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, messages.quoted_message_id, messages.quoted_text, messages.quoted_sender
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND datetime(messages.timestamp) < datetime(?)
            ORDER BY datetime(messages.timestamp) DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))

        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7],
                quoted_id=msg[8],
                quoted_text=msg[9],
                quoted_sender=msg[10]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, messages.quoted_message_id, messages.quoted_text, messages.quoted_sender
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND datetime(messages.timestamp) > datetime(?)
            ORDER BY datetime(messages.timestamp) ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))

        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7],
                quoted_id=msg[8],
                quoted_text=msg[9],
                quoted_sender=msg[10]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = _connect_db(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["""
            SELECT 
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                AND chats.last_message_time = messages.timestamp
            """)
            
        where_clauses = []
        params = []
        
        if query:
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "datetime(chats.last_message_time) DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        local_contacts = _load_local_contacts()
        result = []
        for chat_data in chats:
            jid = chat_data[0]
            name = chat_data[1]

            # Enrich LID-based chats with phone number and contact name
            if '@lid' in jid:
                phone = resolve_lid_to_phone(jid)
                if phone:
                    # Check local contacts first
                    if phone in local_contacts:
                        name = local_contacts[phone]
                    elif not name:
                        wm_name = get_whatsmeow_contact_name(jid)
                        if not wm_name:
                            wm_name = get_whatsmeow_contact_name(f"{phone}@s.whatsapp.net")
                        name = wm_name if wm_name else f"+{phone}"
                    if name and f"+{phone}" not in name:
                        name = f"{name} (+{phone})"

            chat = Chat(
                jid=jid,
                name=name,
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    try:
        search_pattern = '%' + query + '%'
        seen_jids = set()
        result = []

        # Load local contacts for name enrichment
        local_contacts = _load_local_contacts()

        # 1. Search messages.db chats table
        conn = _connect_db(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT
                jid,
                name
            FROM chats
            WHERE
                (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                AND jid NOT LIKE '%@g.us'
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern))

        for contact_data in cursor.fetchall():
            jid = contact_data[0]
            name = contact_data[1]
            phone = jid.split('@')[0]

            # If LID, resolve to phone number
            if '@lid' in jid:
                resolved = resolve_lid_to_phone(jid)
                if resolved:
                    phone = resolved

            # Check local contacts for a custom name
            if phone in local_contacts:
                name = local_contacts[phone]
            elif not name:
                # Try whatsmeow for push_name
                wm_name = get_whatsmeow_contact_name(jid)
                if wm_name:
                    name = wm_name

            seen_jids.add(jid)
            result.append(Contact(phone_number=phone, name=name, jid=jid))

        conn.close()

        # 2. Also search whatsmeow_contacts table for push_name/business_name matches
        try:
            conn2 = _connect_db(WHATSAPP_DB_PATH)
            cursor2 = conn2.cursor()
            cursor2.execute("""
                SELECT their_jid, push_name, business_name, full_name
                FROM whatsmeow_contacts
                WHERE
                    LOWER(push_name) LIKE LOWER(?)
                    OR LOWER(business_name) LIKE LOWER(?)
                    OR LOWER(full_name) LIKE LOWER(?)
                    OR their_jid LIKE ?
            """, (search_pattern, search_pattern, search_pattern, search_pattern))

            for row in cursor2.fetchall():
                jid = row[0]
                if jid in seen_jids:
                    continue
                name = row[1] or row[2] or row[3]
                phone = jid.split('@')[0]
                if '@lid' in jid:
                    resolved = resolve_lid_to_phone(jid)
                    if resolved:
                        phone = resolved
                if phone in local_contacts:
                    name = local_contacts[phone]
                seen_jids.add(jid)
                result.append(Contact(phone_number=phone, name=name, jid=jid))

            conn2.close()
        except sqlite3.Error:
            pass

        # 3. Search local contacts by name
        for phone, name in local_contacts.items():
            if query.lower() in name.lower() or query in phone:
                # Find matching JID
                lid = resolve_phone_to_lid(phone)
                jid = f"{lid}@lid" if lid else f"{phone}@s.whatsapp.net"
                if jid not in seen_jids:
                    seen_jids.add(jid)
                    result.append(Contact(phone_number=phone, name=name, jid=jid))

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        pass


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = _connect_db(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY datetime(c.last_message_time) DESC
            LIMIT ? OFFSET ?
        """, (jid, jid, limit, page * limit))
        
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = _connect_db(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY datetime(m.timestamp) DESC
            LIMIT 1
        """, (jid, jid))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = _connect_db(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        query += " WHERE c.jid = ?"
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = _connect_db(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        # Clean the phone number
        phone_clean = sender_phone_number.lstrip('+').replace(' ', '').replace('-', '')

        # First try direct phone number match in chats
        cursor.execute("""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{phone_clean}%",))

        chat_data = cursor.fetchone()

        # If no match, try resolving phone → LID and searching by LID JID
        if not chat_data:
            lid = resolve_phone_to_lid(phone_clean)
            if lid:
                lid_jid = f"{lid}@lid"
                cursor.execute("""
                    SELECT
                        c.jid,
                        c.name,
                        c.last_message_time,
                        m.content as last_message,
                        m.sender as last_sender,
                        m.is_from_me as last_is_from_me
                    FROM chats c
                    LEFT JOIN messages m ON c.jid = m.chat_jid
                        AND c.last_message_time = m.timestamp
                    WHERE c.jid = ? AND c.jid NOT LIKE '%@g.us'
                    LIMIT 1
                """, (lid_jid,))
                chat_data = cursor.fetchone()

        if not chat_data:
            return None

        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def save_contact(phone_number: str, name: str) -> Tuple[bool, str]:
    """Save a contact name to local contacts mapping.

    Args:
        phone_number: Phone number (with country code, no + or spaces)
        name: Display name for this contact

    Returns:
        Tuple of (success, status_message)
    """
    try:
        phone_clean = phone_number.lstrip('+').replace(' ', '').replace('-', '')
        contacts = _load_local_contacts()
        old_name = contacts.get(phone_clean)
        contacts[phone_clean] = name
        _save_local_contacts(contacts)

        if old_name:
            return True, f"Updated contact: {phone_clean} from '{old_name}' to '{name}'"
        return True, f"Saved contact: {phone_clean} as '{name}'"
    except Exception as e:
        return False, f"Error saving contact: {str(e)}"


def list_saved_contacts() -> dict:
    """List all locally saved contacts."""
    return _load_local_contacts()


def resolve_lid(lid_jid: str) -> dict:
    """Resolve a LID JID to phone number and contact info.

    Args:
        lid_jid: The LID JID (e.g. '44543760154755@lid' or just '44543760154755')

    Returns:
        Dict with phone_number, push_name, business_name, local_name
    """
    phone = resolve_lid_to_phone(lid_jid)
    result = {
        "lid": lid_jid,
        "phone_number": phone,
        "phone_jid": f"{phone}@s.whatsapp.net" if phone else None,
        "push_name": None,
        "business_name": None,
        "local_name": None,
    }

    if phone:
        # Check local contacts
        local_contacts = _load_local_contacts()
        result["local_name"] = local_contacts.get(phone)

    # Check whatsmeow_contacts for the LID JID
    try:
        conn = _connect_db(WHATSAPP_DB_PATH)
        cursor = conn.cursor()

        jid_to_check = lid_jid if '@' in lid_jid else f"{lid_jid}@lid"
        cursor.execute(
            "SELECT push_name, business_name FROM whatsmeow_contacts WHERE their_jid = ?",
            (jid_to_check,)
        )
        row = cursor.fetchone()
        if row:
            result["push_name"] = row[0] if row[0] else None
            result["business_name"] = row[1] if row[1] else None

        # Also check by phone JID
        if phone:
            cursor.execute(
                "SELECT push_name, business_name FROM whatsmeow_contacts WHERE their_jid = ?",
                (f"{phone}@s.whatsapp.net",)
            )
            row = cursor.fetchone()
            if row:
                if not result["push_name"] and row[0]:
                    result["push_name"] = row[0]
                if not result["business_name"] and row[1]:
                    result["business_name"] = row[1]

        conn.close()
    except sqlite3.Error:
        pass

    return result


def _log_outbound_message(recipient: str, content: str, media_type: str = None, filename: str = None) -> None:
    """Log an outbound message to messages.db after a successful send."""
    try:
        import uuid
        from datetime import timezone

        # Clean the recipient to get the phone number
        phone_clean = recipient.lstrip('+').replace(' ', '').replace('-', '').split('@')[0]

        # Determine the chat_jid — check if a chat already exists for this recipient
        conn = _connect_db(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        chat_jid = None

        # Try direct phone match
        cursor.execute("SELECT jid FROM chats WHERE jid LIKE ? AND jid NOT LIKE '%@g.us'", (f"%{phone_clean}%",))
        row = cursor.fetchone()
        if row:
            chat_jid = row[0]

        # Try LID match
        if not chat_jid:
            lid = resolve_phone_to_lid(phone_clean)
            if lid:
                lid_jid = f"{lid}@lid"
                cursor.execute("SELECT jid FROM chats WHERE jid = ?", (lid_jid,))
                row = cursor.fetchone()
                if row:
                    chat_jid = row[0]
                else:
                    chat_jid = lid_jid

        # If still no chat_jid, use the recipient as-is
        if not chat_jid:
            if '@' in recipient:
                chat_jid = recipient
            else:
                chat_jid = f"{phone_clean}@s.whatsapp.net"

        now = datetime.now(timezone.utc).astimezone()
        msg_id = f"OUT-{uuid.uuid4().hex[:16]}"

        # Ensure chat exists
        local_contacts = _load_local_contacts()
        chat_name = local_contacts.get(phone_clean, None)
        cursor.execute(
            "INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, COALESCE(?, (SELECT name FROM chats WHERE jid = ?)), ?)",
            (chat_jid, chat_name, chat_jid, now.isoformat())
        )

        # Insert the outbound message
        cursor.execute(
            """INSERT OR REPLACE INTO messages
            (id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL, NULL, NULL, NULL)""",
            (msg_id, chat_jid, "me", content, now.isoformat(), media_type, filename)
        )

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to log outbound message: {e}")


def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Check bridge health before attempting to send
        health = check_bridge_health()
        if health != "ok":
            return False, health

        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }

        response = requests.post(url, json=payload)

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            success = result.get("success", False)
            status_msg = result.get("message", "Unknown response")
            # Log outbound message on success
            if success:
                _log_outbound_message(recipient, message)
            return success, status_msg
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            success = result.get("success", False)
            status_msg = result.get("message", "Unknown response")
            if success:
                # Determine media type from extension
                ext = os.path.splitext(media_path)[1].lower()
                media_type = "document"
                if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
                    media_type = "image"
                elif ext in ('.mp4', '.mov', '.avi', '.mkv'):
                    media_type = "video"
                elif ext in ('.mp3', '.ogg', '.opus', '.wav', '.m4a'):
                    media_type = "audio"
                _log_outbound_message(recipient, f"[Sent file: {os.path.basename(media_path)}]", media_type=media_type, filename=os.path.basename(media_path))
            return success, status_msg
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None
