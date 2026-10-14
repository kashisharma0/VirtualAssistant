"""
Virtual Assistant: real-time voice assistant built on the ElevenLabs
Conversational AI SDK.
"""

import json
import os
import re
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import (
    ClientTools,
    Conversation,
    ConversationInitiationData,
)
from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
load_dotenv()

AGENT_ID = os.getenv("AGENT_ID")
API_KEY = os.getenv("API_KEY")
USER_NAME = os.getenv("USER_NAME", "Kashish")
USER_ID = os.getenv("USER_ID", "kashish123")

SCHEDULE_FILE = Path(__file__).with_name("schedule.json")
_schedule_lock = threading.Lock()  # tools may run on worker threads

# "Morning" and "afternoon" in the original schedule had no exact time,
# so 09:00 and 15:00 are assumed here. Edit freely.
DEFAULT_SCHEDULE = [
    {"time": "09:00", "title": "Project work on Virtual Assistant"},
    {"time": "13:30", "title": "Lunch with family"},
    {"time": "15:00", "title": "Coding practice"},
    {"time": "18:00", "title": "Evening walk with friends"},
]


# --------------------------------------------------------------------------
# Schedule storage helpers
# --------------------------------------------------------------------------
def parse_time(text: str) -> str:
    """Convert '7 pm', '7:30 PM', '1.30pm' or '19:00' to 24-hour 'HH:MM'."""
    match = re.fullmatch(
        r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?m?\.?\s*", str(text).lower()
    )
    if not match:
        raise ValueError(f"I couldn't understand the time '{text}'.")
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), match[3]
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"'{text}' is not a valid 12-hour time.")
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"'{text}' is not a valid time.")
    return f"{hour:02d}:{minute:02d}"


def speak_time(hhmm: str) -> str:
    """'13:30' -> '1:30 PM'"""
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def load_schedule() -> list:
    with _schedule_lock:
        if not SCHEDULE_FILE.exists():
            _write_schedule(DEFAULT_SCHEDULE)
            return list(DEFAULT_SCHEDULE)
        try:
            return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("Warning: schedule.json was unreadable; using defaults.")
            return list(DEFAULT_SCHEDULE)


def _write_schedule(events: list) -> None:
    events.sort(key=lambda e: e["time"])
    SCHEDULE_FILE.write_text(json.dumps(events, indent=2), encoding="utf-8")


def schedule_as_text(events: list) -> str:
    if not events:
        return "The schedule is empty."
    return "; ".join(f"{speak_time(e['time'])}: {e['title']}" for e in events)


# --------------------------------------------------------------------------
# Client tools (called by the agent when the user asks to read/change plans)
# Each receives a dict of parameters and returns a string the agent will use.
# --------------------------------------------------------------------------
def get_schedule(params: dict) -> str:
    return schedule_as_text(load_schedule())


def add_event(params: dict) -> str:
    try:
        title = str(params.get("title", "")).strip()
        if not title:
            return "I need a title for the event."
        time_24 = parse_time(params.get("time", ""))
        with _schedule_lock:
            events = (
                json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
                if SCHEDULE_FILE.exists()
                else list(DEFAULT_SCHEDULE)
            )
            if any(e["time"] == time_24 and e["title"].lower() == title.lower() for e in events):
                return f"'{title}' at {speak_time(time_24)} is already scheduled."
            events.append({"time": time_24, "title": title})
            _write_schedule(events)
        return f"Added '{title}' at {speak_time(time_24)}."
    except ValueError as err:
        return str(err)
    except Exception as err:  # never crash the live session
        return f"Sorry, I couldn't add that: {err}"


def remove_event(params: dict) -> str:
    try:
        keyword = str(params.get("title", "")).strip().lower()
        if not keyword:
            return "Which event should I remove?"
        with _schedule_lock:
            events = (
                json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
                if SCHEDULE_FILE.exists()
                else list(DEFAULT_SCHEDULE)
            )
            kept = [e for e in events if keyword not in e["title"].lower()]
            removed = len(events) - len(kept)
            if removed == 0:
                return f"I couldn't find an event matching '{keyword}'."
            _write_schedule(kept)
        return f"Removed {removed} event(s) matching '{keyword}'."
    except Exception as err:
        return f"Sorry, I couldn't remove that: {err}"


# --------------------------------------------------------------------------
# Prompt building
# --------------------------------------------------------------------------
def build_prompt(events: list) -> str:
    now = datetime.now().strftime("%A, %d %B %Y, %I:%M %p")
    return (
        f"You are a friendly voice assistant for {USER_NAME}. "
        "Your replies are spoken aloud, so keep them short (one or two sentences) "
        "and conversational. "
        f"Current date and time: {now}. "
        "For schedule questions, answer only from the schedule data; if you don't "
        "know something, say so instead of guessing. "
        "To read the latest schedule use the get_schedule tool. To add or remove "
        "events use add_event (time, title) and remove_event (title). "
        "Never say a change was made unless the tool confirmed it. "
        f"Schedule at the start of this chat: {schedule_as_text(events)}"
    )


# --------------------------------------------------------------------------
# Event callbacks
# --------------------------------------------------------------------------
def on_agent_response(response: str) -> None:
    print(f"Agent: {response}")


def on_interrupted(original: str, corrected: str) -> None:
    print(f"Agent interrupted. What was actually said: {corrected}")


def on_user_transcript(transcript: str) -> None:
    print(f"You: {transcript}")


def on_latency(ms: int) -> None:
    print(f"[latency: {ms} ms]")


def on_end() -> None:
    print("Session ended.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> None:
    missing = [n for n, v in (("AGENT_ID", AGENT_ID), ("API_KEY", API_KEY)) if not v]
    if missing:
        sys.exit(f"Missing {', '.join(missing)}. Copy .env.example to .env and fill it in.")

    events = load_schedule()

    client = ElevenLabs(api_key=API_KEY)

    tools = ClientTools()
    tools.register("get_schedule", get_schedule)
    tools.register("add_event", add_event)
    tools.register("remove_event", remove_event)

    # Per-session overrides: must be enabled in the agent's Security settings.
    config = ConversationInitiationData(
        conversation_config_override={
            "agent": {
                "prompt": {"prompt": build_prompt(events)},
                "first_message": f"Hello {USER_NAME}, how can I help you today?",
            }
        },
        user_id=USER_ID,
    )

    conversation = Conversation(
        client,
        AGENT_ID,
        requires_auth=True,  # private agent: SDK fetches a signed URL with your key
        audio_interface=DefaultAudioInterface(),
        config=config,
        client_tools=tools,
        callback_agent_response=on_agent_response,
        callback_agent_response_correction=on_interrupted,
        callback_user_transcript=on_user_transcript,
        callback_latency_measurement=on_latency,
        callback_end_session=on_end,
    )

    # Ctrl+C ends the session cleanly instead of killing the audio stream.
    signal.signal(signal.SIGINT, lambda *_: conversation.end_session())

    print("Starting session. Speak into your microphone. Press Ctrl+C to stop.")
    try:
        conversation.start_session()
        conversation_id = conversation.wait_for_session_end()
        print(f"Conversation ID: {conversation_id}")
    except Exception as err:
        sys.exit(f"Could not run the session: {err}")


if __name__ == "__main__":
    main()
