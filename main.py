import os
import re
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ============================================================
# CONFIGURATION - Fill these in
# ============================================================
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "xoxb-your-bot-token-here")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "xapp-your-app-token-here")

# The name or ID of the source channel where Quo posts messages
SOURCE_CHANNEL_NAME = "phone-checks"  # <-- Change this to your actual channel name

# ============================================================
# APP SETUP
# ============================================================
app = App(token=SLACK_BOT_TOKEN)

# Cache for channel list (refreshed periodically)
channel_cache = {}


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
    # Check if message starts with a phone number (no saved contact) — skip these
    if re.match(r"^\(\d{3}\)", text.strip()):
        return None, [], None

    # Extract everything before the first phone number as the contact + case info
    contact_match = re.match(r"^(.+?)\s*\(\d{3}\)\s*\d{3}-\d{4}", text)
    if not contact_match:
        return None, [], None

    contact_part = contact_match.group(1).strip()

    # Extract case numbers from the contact part
    # Case numbers are 3-5 digit numbers, possibly joined by "&"
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

    # If not found in cache, refresh and try again
    refresh_channel_cache()
    for ch_id, ch in channel_cache.items():
        if pattern.search(ch["name"]):
            return ch_id, ch["name"]

    return None, None


def join_channel(channel_id):
    """Auto-join a channel so the bot can post in it."""
    try:
        app.client.conversations_join(channel=channel_id)
        print(f"  Joined channel {channel_id}")
    except Exception as e:
        # Already in channel or other non-fatal error
        if "already_in_channel" not in str(e):
            print(f"  Warning joining channel {channel_id}: {e}")


def get_tagged_users_from_topic(channel_id):
    """Read the channel topic and extract user mentions."""
    try:
        result = app.client.conversations_info(channel=channel_id)
        topic = result["channel"]["topic"]["value"]
        # Slack stores user mentions in topics as <@U12345678>
        user_ids = re.findall(r"<@(U[A-Z0-9]+)>", topic)
        return user_ids
    except Exception as e:
        print(f"Error reading topic for {channel_id}: {e}")
        return []


# ============================================================
# Get source channel ID on startup
# ============================================================
source_channel_id = None


def get_source_channel_id():
    global source_channel_id
    for ch_id, ch in channel_cache.items():
        if ch["name"] == SOURCE_CHANNEL_NAME:
            source_channel_id = ch_id
            print(f"Source channel: #{SOURCE_CHANNEL_NAME} ({ch_id})")
            return
    print(f"WARNING: Could not find source channel #{SOURCE_CHANNEL_NAME}")


# ============================================================
# MESSAGE HANDLER
# ============================================================
@app.event("message")
def handle_message(event, say):
    # Only process messages from the source channel
    if event.get("channel") != source_channel_id:
        return

    # Ignore bot messages, edits, etc.
    if event.get("subtype"):
        return

    text = event.get("text", "")
    print(f"\n{'='*60}")
    print(f"New message: {text[:120]}...")

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

        # Auto-join the channel so the bot can read topic and post
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
