import os
import re
import time
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ============================================================
# CONFIGURATION
# ============================================================
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "xoxb-your-bot-token-here")
SOURCE_CHANNEL_NAME = "phone-checks"
POLL_INTERVAL = 5  # seconds between checks

# ============================================================
# SETUP
# ============================================================
client = WebClient(token=SLACK_BOT_TOKEN)
channel_cache = {}
source_channel_id = None
last_timestamp = None  # Track the last message we've seen


# ============================================================
# HELPER FUNCTIONS
# ============================================================
def refresh_channel_cache():
    """Fetch all channels and cache them."""
    global channel_cache
    channel_cache = {}
    cursor = None
    while True:
        result = client.conversations_list(
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


def get_message_text(message):
    """
    Extract the text from a message.
    Handles regular text, attachments, and blocks.
    """
    # Try the normal text field first
    text = message.get("text", "")
    if text:
        return text

    # Some bots put content in attachments
    attachments = message.get("attachments", [])
    for att in attachments:
        if att.get("text"):
            return att["text"]
        if att.get("fallback"):
            return att["fallback"]
        if att.get("pretext"):
            return att["pretext"]

    # Some bots use blocks
    blocks = message.get("blocks", [])
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
        client.conversations_join(channel=channel_id)
    except SlackApiError as e:
        if "already_in_channel" not in str(e):
            print(f"  Warning joining channel {channel_id}: {e}")


def get_tagged_users_from_topic(channel_id):
    """Read the channel topic and extract user mentions."""
    try:
        result = client.conversations_info(channel=channel_id)
        topic = result["channel"]["topic"]["value"]
        user_ids = re.findall(r"<@(U[A-Z0-9]+)>", topic)
        return user_ids
    except SlackApiError as e:
        print(f"Error reading topic for {channel_id}: {e}")
        return []


def get_new_messages():
    """Fetch new messages from the source channel since last check."""
    global last_timestamp
    try:
        kwargs = {
            "channel": source_channel_id,
            "limit": 20,
        }
        if last_timestamp:
            kwargs["oldest"] = last_timestamp

        result = client.conversations_history(**kwargs)
        messages = result.get("messages", [])

        # Messages come newest-first, reverse to process oldest first
        messages.reverse()

        # Filter out messages we've already seen (oldest param is inclusive)
        if last_timestamp:
            messages = [m for m in messages if m["ts"] != last_timestamp]

        # Update the last timestamp
        if messages:
            last_timestamp = messages[-1]["ts"]

        return messages

    except SlackApiError as e:
        print(f"Error fetching messages: {e}")
        return []


def process_message(message):
    """Process a single message from the source channel."""
    # DEBUG: Log the raw message so we can see exactly what Quo sends
    print(f"\n{'='*60}")
    print(f"RAW MESSAGE: {message}")

    # Extract text from the message
    text = get_message_text(message)
    if not text:
        print(f"  Could not extract text")
        return

    print(f"New message:")
    print(f"  From: {message.get('username', message.get('user', message.get('bot_id', 'unknown')))}")
    print(f"  Text: {text[:120]}")

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
            client.chat_postMessage(
                channel=case_channel_id,
                text=forwarded_message
            )
            print(f"  ✅ Posted to #{case_channel_name}" + (f" — tagged {mentions}" if mentions else ""))
        except SlackApiError as e:
            print(f"  ❌ Error posting to #{case_channel_name}: {e}")


# ============================================================
# MAIN LOOP
# ============================================================
if __name__ == "__main__":
    print("Starting Case Router Bot (polling mode)...")
    print("=" * 60)
    refresh_channel_cache()
    get_source_channel_id()

    if not source_channel_id:
        print("FATAL: Could not find source channel. Exiting.")
        exit(1)

    # Set the starting timestamp to now so we don't process old messages
    last_timestamp = str(time.time())

    print("=" * 60)
    print(f"Polling #{SOURCE_CHANNEL_NAME} every {POLL_INTERVAL} seconds...\n")

    while True:
        try:
            messages = get_new_messages()
            for msg in messages:
                process_message(msg)
        except Exception as e:
            print(f"Error in main loop: {e}")

        time.sleep(POLL_INTERVAL)
