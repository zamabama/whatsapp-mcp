import random
from typing import List, Dict, Any, Optional
from mcp.server.fastmcp import FastMCP
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    send_message as whatsapp_send_message,
    send_file as whatsapp_send_file,
    send_audio_message as whatsapp_audio_voice_message,
    download_media as whatsapp_download_media,
    save_contact as whatsapp_save_contact,
    list_saved_contacts as whatsapp_list_saved_contacts,
    resolve_lid as whatsapp_resolve_lid,
    get_version as whatsapp_get_version,
)

# Initialize FastMCP server
mcp = FastMCP("whatsapp")

@mcp.tool()
def search_contacts(query: str) -> List[Dict[str, Any]]:
    """Search WhatsApp contacts by name or phone number.
    
    Args:
        query: Search term to match against contact names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return contacts

@mcp.tool()
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
) -> List[Dict[str, Any]]:
    """Get WhatsApp messages matching specified criteria with optional context.
    
    Args:
        after: Optional ISO-8601 formatted string to only return messages after this date
        before: Optional ISO-8601 formatted string to only return messages before this date
        sender_phone_number: Optional phone number to filter messages by sender
        chat_jid: Optional chat JID to filter messages by chat
        query: Optional search term to filter messages by content
        limit: Maximum number of messages to return (default 20)
        page: Page number for pagination (default 0)
        include_context: Whether to include messages before and after matches (default True)
        context_before: Number of messages to include before each match (default 1)
        context_after: Number of messages to include after each match (default 1)
    """
    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after
    )
    return messages

@mcp.tool()
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.
    
    Args:
        query: Optional search term to filter chats by name or JID
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        include_last_message: Whether to include the last message in each chat (default True)
        sort_by: Field to sort results by, either "last_active" or "name" (default "last_active")
    """
    chats = whatsapp_list_chats(
        query=query,
        limit=limit,
        page=page,
        include_last_message=include_last_message,
        sort_by=sort_by
    )
    return chats

@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by JID.
    
    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return chat

@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by sender phone number.
    
    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return chat

@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    chats = whatsapp_get_contact_chats(jid, limit, page)
    return chats

@mcp.tool()
def get_last_interaction(jid: str) -> str:
    """Get most recent WhatsApp message involving the contact.
    
    Args:
        jid: The JID of the contact to search for
    """
    message = whatsapp_get_last_interaction(jid)
    return message

@mcp.tool()
def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> Dict[str, Any]:
    """Get context around a specific WhatsApp message.
    
    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
    """
    context = whatsapp_get_message_context(message_id, before, after)
    return context

# Pending send confirmations: maps confirmation_code -> (recipient, message, contact_name)
_pending_sends: Dict[str, tuple] = {}

@mcp.tool()
def send_message(
    recipient: str,
    message: str,
    contact_name: Optional[str] = None,
    confirmation_code: Optional[str] = None
) -> Dict[str, Any]:
    """Send a WhatsApp message to a person or group. For group chats use the JID.

    IMPORTANT: You MUST first show the draft message to the user and get explicit approval
    before calling this with confirmed=True. Messages will be BLOCKED unless confirmed=True.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        message: The message text to send
        contact_name: Optional display name to save for this contact (e.g. "MKM - Cassie").
                     If provided, automatically saves the phone->name mapping for future identification.
        confirmation_code: REQUIRED. Must be True to actually send. Default is False which blocks sending
                  and returns the message as a draft. Only set to True AFTER the user has reviewed
                  and explicitly approved the message.

    Returns:
        A dictionary containing success status and a status message
    """
    # SAFETY STEP 1: No code provided — generate one and block the send
    if not confirmation_code:
        code = str(random.randint(1000, 9999))
        _pending_sends[code] = (recipient, message, contact_name)
        return {
            "success": False,
            "message": f"BLOCKED: Message NOT sent. A confirmation code has been generated.\n\nDRAFT to {recipient}:\n{message}\n\nConfirmation code: {code}\n\nShow the draft to the user. After they approve and provide the code, call send_message again with confirmation_code=\"{code}\"."
        }

    # SAFETY STEP 2: Code provided — validate it matches a pending send
    if confirmation_code not in _pending_sends:
        return {
            "success": False,
            "message": f"INVALID CODE: \"{confirmation_code}\" does not match any pending message. You must call send_message WITHOUT a code first to generate a new one."
        }

    # Retrieve and remove the pending send (one-time use)
    pending_recipient, pending_message, pending_contact_name = _pending_sends.pop(confirmation_code)

    # Verify the message matches what was approved
    if pending_recipient != recipient or pending_message != message:
        return {
            "success": False,
            "message": "BLOCKED: The recipient or message does not match what was approved. Generate a new code."
        }

    # Validate input
    if not recipient:
        return {
            "success": False,
            "message": "Recipient must be provided"
        }

    # Auto-save contact name if provided (use pending_contact_name if not provided now)
    save_name = contact_name or pending_contact_name
    if save_name:
        phone_clean = recipient.lstrip('+').replace(' ', '').replace('-', '').split('@')[0]
        whatsapp_save_contact(phone_clean, save_name)

    # Call the whatsapp_send_message function with the unified recipient parameter
    success, status_message = whatsapp_send_message(recipient, message)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_file(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send a file such as a picture, raw audio, video or document via WhatsApp to the specified recipient. For group messages use the JID.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the media file to send (image, video, document)
    
    Returns:
        A dictionary containing success status and a status message
    """
    
    # Call the whatsapp_send_file function
    success, status_message = whatsapp_send_file(recipient, media_path)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_audio_message(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send any audio file as a WhatsApp audio message to the specified recipient. For group messages use the JID. If it errors due to ffmpeg not being installed, use send_file instead.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the audio file to send (will be converted to Opus .ogg if it's not a .ogg file)
    
    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_audio_voice_message(recipient, media_path)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> Dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    file_path = whatsapp_download_media(message_id, chat_jid)
    
    if file_path:
        return {
            "success": True,
            "message": "Media downloaded successfully",
            "file_path": file_path
        }
    else:
        return {
            "success": False,
            "message": "Failed to download media"
        }

@mcp.tool()
def save_contact(phone_number: str, name: str) -> Dict[str, Any]:
    """Save a contact with a display name. This stores the mapping locally so contacts
    can be identified by name in future messages and searches.

    Args:
        phone_number: Phone number with country code, no + or spaces (e.g. "447928545139")
        name: Display name for this contact (e.g. "APS - Andy")

    Returns:
        A dictionary containing success status and a status message
    """
    success, message = whatsapp_save_contact(phone_number, name)
    return {"success": success, "message": message}


@mcp.tool()
def list_saved_contacts() -> Dict[str, str]:
    """List all locally saved contacts (phone_number -> name mappings).

    Returns:
        A dictionary of phone_number -> name mappings
    """
    return whatsapp_list_saved_contacts()


@mcp.tool()
def resolve_lid(lid_jid: str) -> Dict[str, Any]:
    """Resolve a WhatsApp LID (Linked ID) to a phone number and contact info.
    LIDs are opaque identifiers (e.g. '44543760154755@lid') that WhatsApp uses
    instead of phone numbers in newer protocol versions.

    Args:
        lid_jid: The LID JID (e.g. '44543760154755@lid' or just '44543760154755')

    Returns:
        A dictionary with phone_number, push_name, business_name, and local_name
    """
    return whatsapp_resolve_lid(lid_jid)


@mcp.tool()
def whatsapp_version() -> Dict[str, Any]:
    """Report the version of this WhatsApp MCP server and the live bridge binary.

    Use this to confirm whether this session is running the updated build. A result
    with mcp_server_version "1.1.0-quoted-reply" (and "quoted-reply-context" in
    features) means quoted/reply context is supported here. If this tool does not
    exist at all, the session is on an older build and should be reloaded.

    Returns:
        A dictionary with mcp_server_version, bridge_version, and supported features
    """
    return whatsapp_get_version()


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport='stdio')