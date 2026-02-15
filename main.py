import os
import re
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ============================================================
# CONFIGURATION
# ============================================================
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "xoxb-your-bot-token-here")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "xapp-your-app-token-here")
SOURCE_CHANNEL_NAME = "phone-checks"

# ============================================================
# APP SETUP
# ============================================================
app = App(token=SLACK_BOT_TOKEN)
channel_cache = {}
source_channel_id = None


# ============================================================
# HELPER FUNCTIONS
# ============================================================
def refresh_channel_cache():
    """Fetch all channels and cache them."""
    global channel_cache
    channel_cache = {}
    cursor = None
    while True:
        result = app.client.conversations_list(
            types="public_channel,private_channel",
            limit=1000,
            cursor=cursor
        )
        for channel in result["channels"]:
            channel_cache[channel["id"]] = channel
        cursor = result.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    print(f"Cached {len(channel_cache)} channels")


def get_source_channel_id():
    """Find the source channel ID from the cache."""
    global source_channel_id
    for ch_id, ch in channel_cache.items():
        if ch["name"] == SOURCE_CHANNEL_NAME:
            source_channel_id = ch_id
            print(f"Source channel: #{SOURCE_CHANNEL_NAME} ({ch_id})")
            return
    print(f"WARNING: Could not find source channel #{SOURCE_CHANNEL_NAME}")


def get_message_text(event):
    """
    Extract the text from a message event.
    Handles regular messages, bot messages, and messages with attachments.
    """
    # Try the normal text field first
    text = event.get("text", "")
    if text:
        return text

    # Some bots put content in attachments instead of text
    attachments = event.get("attachments", [])
    for att in attachments:
        if att.get("text"):
            return att["text"]
        if att.get("fallback"):
            return att["fallback"]
        if att.get("pretext"):
            return att["pretext"]

    # Some bots use blocks
    blocks = event.get("blocks", [])
    for block in blocks:
        if block.get("type") == "rich_text":
            for element in block.get("elements", []):
                for sub in element.get("elements", []):
                    if sub.get("type") == "text":
                        return sub.get("text", "")
        if block.get("type") == "section":
            block_text = block.get("text", {})
            if block_text.get("text"):
                return block_text["text"]

    return ""


def parse_quo_message(text):
    """
    Parse a Quo message to extract contact name, case numbers, and message body.

    Format: "Name CaseNum(s) (phone) → RJL line (phone) MessageText"

    Examples:
        "Phil Garret 1425 (512) 694-5181 → RJL Outbound (512) 500-5266 Hi Laura..."
        "Lourdes Galeas 940 & 1206 (504) 723-7482 → RJL Main Line (512) 537-3369 Esos me los dio..."
        "(512) 964-4192 → RJL Main Line (512) 537-3369 Missed call"  <-- no contact, skip

    Returns: (contact_name, [case_numbers], message_body) or (None, [], None) if no contact
    """
    # Check if message starts with a phone number (no saved contact) — skip
    if re.match(r"^\(\d{3}\)", text.strip()):
        return None, [], None

    # Extract everything before the first phone number as the contact + case info
    contact_match = re.match(r"^(.+?)\s*\(\d{3}\)\s*\d{3}-\d{4}", text)
    if not contact_match:
        return None, [], None

    contact_part = contact_match.group(1).strip()

    # Extract case numbers (3-5 digit numbers, possibly joined by "&")
    case_numbers = re.findall(r"\b(\d{3,5})\b", contact_part)
    if not case_numbers:
        return None, [], None

    # Extract the contact name (everything before the first number)
    name_match = re.match(r"^([A-Za-z\s]+)", contact_part)
    contact_name = name_match.group(1).strip() if name_match else "Unknown"

    # Extract the message body (everything after the → RJL... (phone) pattern)
    body_match = re.search(r"→\s*RJL[^)]+\)\s*(.*)", text, re.DOTALL)
    message_body = body_match.group(1).strip() if body_match else text

    return contact_name, case_numbers, message_body


def find_case_channel(case_number):
    """Find a channel whose name ends with the case number."""
    pattern = re.compile(r"-" + re.escape(case_number) + r"$")
    for ch_id, ch in channel_cache.items():
        if pattern.search(ch["name"]):
            return ch_id, ch["name"]

    # Refresh cache and try again
    refresh_channel_cache()
    for ch_id, ch in channel_cache.items():
        if pattern.search(ch["name"]):
            return ch_id, ch["name"]

    return None, None


def join_channel(channel_id):
    """Auto-join a channel so the bot can post in it."""
    try:
        app.client.conversations_join(channel=channel_id)
    except Exception as e:
        if "already_in_channel" not in str(e):
            print(f"  Warning joining channel {channel_id}: {e}")


def get_tagged_users_from_topic(channel_id):
    """Read the channel topic and extract user mentions."""
    try:
        result = app.client.conversations_info(channel=channel_id)
        topic = result["channel"]["topic"]["value"]
        user_ids = re.findall(r"<@(U[A-Z0-9]+)>", topic)
        return user_ids
    except Exception as e:
        print(f"Error reading topic for {channel_id}: {e}")
        return []


# ============================================================
# MESSAGE HANDLER — catches ALL message events
# ============================================================
@app.event("message")
def handle_message(event, say):
    channel = event.get("channel")
    subtype = event.get("subtype")

    # DEBUG: Log every event from the source channel
    if channel == source_channel_id:
        print(f"\n{'='*60}")
        print(f"DEBUG EVENT from #phone-checks:")
        print(f"  subtype: {subtype}")
        print(f"  bot_id: {event.get('bot_id', 'none')}")
        print(f"  user: {event.get('user', 'none')}")
        print(f"  text: {event.get('text', '')[:100]}")
        print(f"  has attachments: {len(event.get('attachments', []))}")
        print(f"  has blocks: {len(event.get('blocks', []))}")
        print(f"  full event keys: {list(event.keys())}")

    # Only process messages from the source channel
    if channel != source_channel_id:
        return

    # Ignore message edits, deletions, joins, etc.
    # But allow: normal messages (no subtype), bot_message, and file_share
    allowed_subtypes = {None, "bot_message", "file_share"}
    if subtype not in allowed_subtypes:
        print(f"  Skipped — subtype '{subtype}' not in allowed list")
        return

    # Extract text from the message (handles regular text, attachments, blocks)
    text = get_message_text(event)
    if not text:
        print(f"  Skipped — no text content found")
        return

    print(f"  Extracted text: {text[:120]}")

    # Parse the Quo message
    contact_name, case_numbers, message_body = parse_quo_message(text)

    if not contact_name:
        print(f"  Skipped — no saved contact or case number")
        return

    print(f"  Contact: {contact_name}")
    print(f"  Case numbers: {case_numbers}")
    print(f"  Message: {message_body[:80]}")

    # Post to each case channel
    for case_number in case_numbers:
        case_channel_id, case_channel_name = find_case_channel(case_number)

        if not case_channel_id:
            print(f"  No channel found for case {case_number}")
            continue

        print(f"  Found channel: #{case_channel_name}")

        # Auto-join the channel
        join_channel(case_channel_id)

        # Get tagged users from the channel topic
        tagged_users = get_tagged_users_from_topic(case_channel_id)
        mentions = " ".join([f"<@{uid}>" for uid in tagged_users])

        # Build the message
        forwarded_message = f"📱 *{contact_name}* (Case {case_number}):\n\n{message_body}"
        if mentions:
            forwarded_message += f"\n\n{mentions}"

        # Post to the case channel
        try:
            app.client.chat_postMessage(
                channel=case_channel_id,
                text=forwarded_message
            )
            print(f"  ✅ Posted to #{case_channel_name}" + (f" — tagged {mentions}" if mentions else ""))
        except Exception as e:
            print(f"  ❌ Error posting to #{case_channel_name}: {e}")


# ============================================================
# START THE BOT
# ============================================================
if __name__ == "__main__":
    print("Starting Case Router Bot...")
    print("=" * 60)
    refresh_channel_cache()
    get_source_channel_id()
    print("=" * 60)
    print("Listening for messages...\n")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
