"""
In-memory conversation state and slot-filling for the Smart Scheduler.
Sessions store message history and extracted scheduling state.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dateutil.tz import gettz
from calendar_service import find_meetings

# In-memory session store: session_id -> { "messages": [...], "state": {...} }
_sessions: dict[str, dict] = {}

DEFAULT_STATE = {
    "duration_minutes": None,
    "preferred_date": None,
    "preferred_time": None,
    "preferred_time_of_day": None,
    "excluded_days": [],
    "proposed_slots": None,
    "confirmed_slot": None,
    "title": None,
    "reminder_minutes": None,
    "action": "schedule", # "find", "schedule", "delete", "reschedule"
    "after_event_title": None,
    "before_event_title": None,
    "between_event_titles": None,
    "offset": None,
    "current_datetime": datetime.now(timezone.utc).isoformat(),
    # OAuth credentials are stored here when the user connects a Google account.
    # This is a plain dict suitable for reconstruction via google.oauth2.credentials.Credentials.
    "google_credentials": None,
    
}


def get_or_create_session(session_id: str) -> dict:
    if session_id not in _sessions:
        _sessions[session_id] = {
            "messages": [],
            "state": dict(DEFAULT_STATE),
        }
    return _sessions[session_id]


def append_message(session_id: str, role: str, content: str) -> None:
    sess = get_or_create_session(session_id)
    sess["messages"].append({"role": role, "content": content})


def get_messages(session_id: str, last_n: Optional[int] = None) -> list[dict]:
    sess = get_or_create_session(session_id)
    msgs = sess["messages"]
    if last_n is not None:
        return msgs[-last_n:]
    return list(msgs)


def get_state(session_id: str) -> dict:
    return dict(get_or_create_session(session_id)["state"])


def update_state(session_id: str, updates: dict) -> None:
    sess = get_or_create_session(session_id)
    for k, v in updates.items():
        if k in sess["state"] and v is not None:
            sess["state"][k] = v


def get_google_credentials(session_id: str) -> Optional[dict]:
    """Return stored Google OAuth credential dict for the session, or None."""
    sess = get_or_create_session(session_id)
    return sess["state"].get("google_credentials")


def set_google_credentials(session_id: str, creds: dict) -> None:
    """Store Google OAuth credential dict for the session."""
    sess = get_or_create_session(session_id)
    sess["state"]["google_credentials"] = creds


def _parse_duration_minutes(text: str) -> Optional[int]:
    text = text.lower()
    # "1 hour", "30 min", "45 minutes", "1h", "30m"
    m = re.search(r"(\d+)\s*(?:hour|hr|h)\b", text)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*(?:min(?:ute)?s?|m)\b", text)
    if m:
        return int(m.group(1))
    if "hour" in text or "hr" in text:
        m = re.search(r"(\d+)", text)
        if m:
            return int(m.group(1)) * 60
    return None


def _parse_time_of_day(text: str) -> Optional[str]:
    text = text.lower()

    # Prefer explicit times (e.g. "7 pm") to broader words like "afternoon".
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3) == "pm":
            hour += 12
        if 5 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        return "evening"

    if any(w in text for w in ["morning", "am", "before noon"]):
        return "morning"
    if any(w in text for w in ["afternoon", "midday"]):
        return "afternoon"
    if any(w in text for w in ["evening", "night", "late"]):
        return "evening"
    return None


def _parse_preferred_day(text: str) -> Optional[str]:
    text = text.lower()
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for d in days:
        if d in text:
            return d
    if "tomorrow" in text:
        d = datetime.now() + timedelta(days=1)
        return days[d.weekday()]
    if "next week" in text:
        return "next_week"

    # Try to parse an explicit date like "March 30" or "Mar 30th".
    # dateutil.parser.parse can recognize these patterns when given fuzzy text.
    try:
        from dateutil.parser import parse as parse_date

        # Only treat it as a date if the text contains a month name or a numeric day.
        # Note: regex must close the non-capturing group properly.
        if re.search(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", text, re.I) or re.search(r"\b\d{1,2}(st|nd|rd|th)?\b", text):
            parsed = parse_date(text, fuzzy=True, default=datetime.now())
            # Only accept as a date if it includes a month/day (not just time)
            if parsed and (parsed.year != datetime.now().year or parsed.month != datetime.now().month or parsed.day != datetime.now().day):
                return parsed.date().isoformat()
    except Exception:
        pass

    return None


def _parse_excluded_days(text: str) -> list[str]:
    text = text.lower()
    excluded = []
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for d in days:
        if f"not {d}" in text or f"not on {d}" in text or f"except {d}" in text:
            excluded.append(d)
    return excluded


def extract_slots_from_message(text: str) -> dict[str, Any]:
    """
    Rule-based extraction of scheduling intent from the user's message.
    Returns a dict of state updates to merge (only non-None keys).
    """
    updates = {}
    duration = _parse_duration_minutes(text)
    if duration is not None:
        updates["duration_minutes"] = duration
    time_of_day = _parse_time_of_day(text)
    if time_of_day is not None:
        updates["preferred_time_of_day"] = time_of_day
    day = _parse_preferred_day(text)
    if day is not None:
        updates["preferred_date"] = day
    excluded = _parse_excluded_days(text)
    if excluded:
        updates["excluded_days"] = excluded
    # Simple title extraction: look for "meeting" or quoted strings
    title_match = re.search(r'(?:meeting|call|appointment)\s+(?:called|named|titled)?\s*["\']?([^"\']+)["\']?', text, re.I)
    if title_match:
        updates["title"] = title_match.group(1).strip()
    # Reminder extraction: e.g., "remind me 15 minutes before"
    reminder_match = re.search(r'remind(?:\s+me)?\s+(\d+)\s*(?:min(?:ute)?s?|m)\s+before', text, re.I)
    if reminder_match:
        updates["reminder_minutes"] = int(reminder_match.group(1))
    return updates


def format_state_for_prompt(state: dict, tz_str: str = "UTC") -> str:
    parts = []
    if state.get("duration_minutes"):
        parts.append(f"Duration: {state['duration_minutes']} minutes")
    if state.get("preferred_date"):
        parts.append(f"Preferred day/date: {state['preferred_date']}")
    if state.get("preferred_time_of_day"):
        parts.append(f"Time of day: {state['preferred_time_of_day']}")
    if state.get("excluded_days"):
        parts.append(f"Excluded days: {', '.join(state['excluded_days'])}")
    if state.get("title"):
        parts.append(f"Title: {state['title']}")
    if state.get("reminder_minutes"):
        parts.append(f"Reminder: {state['reminder_minutes']} minutes before")
    proposed = state.get("proposed_slots")
    if proposed is not None:
        if not proposed:
            parts.append("Proposed slots: None found in the requested window. Suggest alternative days or times.")
        else:
            slot_strs = [_format_slot(s, tz_str) for s in proposed]
            parts.append("Proposed slots: " + "; ".join(slot_strs))
    if state.get("confirmed_slot"):
        parts.append(f"Confirmed: {_format_slot(state['confirmed_slot'], tz_str)} (meeting booked)")

    # Indicate whether the user has connected a Google calendar account.
    if state.get("google_credentials"):
        parts.append("Google calendar: connected")
    else:
        parts.append("Google calendar: not connected")

    if not parts:
        return "No scheduling details collected yet."
    return " | ".join(parts)


def _format_slot(slot: dict, tz_str: str = "UTC") -> str:
    start = slot.get("start", "")
    try:
        dt_utc = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        user_tz = gettz(tz_str) or timezone.utc
        dt_local = dt_utc.astimezone(user_tz)
        return dt_local.strftime("%a %b %d at %I:%M %p")
    except Exception:
        return start


def match_user_choice_to_slot(user_message: str, proposed_slots: list[dict]) -> Optional[dict]:
    """
    If the user is confirming one of the proposed slots (e.g. "first one", "2 PM"),
    return that slot; otherwise None.
    """
    if not proposed_slots or not user_message:
        return None
    text = user_message.lower().strip()
    # "first", "first one", "1st" -> index 0
    if any(w in text for w in ["first", "1st", "earliest", "first one"]):
        return proposed_slots[0]
    # "second", "2nd" -> index 1
    if any(w in text for w in ["second", "2nd", "second one"]) and len(proposed_slots) > 1:
        return proposed_slots[1]
    # "third", "3rd" -> index 2
    if any(w in text for w in ["third", "3rd"]) and len(proposed_slots) > 2:
        return proposed_slots[2]
    # Try to match by time "2 pm", "2:00"
    for slot in proposed_slots:
        start = slot.get("start", "")
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            hour12 = dt.hour % 12 or 12
            if str(hour12) in text or str(dt.hour) in text:
                return slot
        except Exception:
            continue
    # "yes" / "that works" when we have exactly one proposal
    if len(proposed_slots) == 1 and any(w in text for w in ["yes", "ok", "sure", "that works", "book it", "confirm"]):
        return proposed_slots[0]
    return None


def state_to_search_window(state: dict) -> Optional[tuple[datetime, datetime]]:
    """
    Convert conversation state into a (window_start, window_end) for calendar search.
    Returns None if we don't have enough (at least duration and a day or default to next 7 days).
    Uses UTC; caller can adjust for timezone.
    """
    duration = state.get("duration_minutes")
    if not duration:
        return None

    now = datetime.now(timezone.utc)
    # Default: search next 7 days, business hours 8–18 UTC
    window_start = now.replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    if window_start < now:
        window_start = window_start + timedelta(days=1)
    window_end = window_start + timedelta(days=7)
    window_end = window_end.replace(hour=18, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

    day_name = state.get("preferred_date")
    if day_name and day_name != "next_week":
        # Allow ISO dates (YYYY-MM-DD) to be used as the preferred date.
        if re.match(r"^\d{4}-\d{2}-\d{2}$", day_name):
            try:
                dt = datetime.fromisoformat(day_name).replace(tzinfo=timezone.utc)
                window_start = dt.replace(hour=8, minute=0, second=0, microsecond=0)
                window_end = window_start.replace(hour=18, minute=0, second=0, microsecond=0)
            except Exception:
                pass
        else:
            days_list = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            if day_name in days_list:
                target_weekday = days_list.index(day_name)
                current_weekday = now.weekday()
                days_ahead = (target_weekday - current_weekday + 7) % 7
                if days_ahead == 0 and now.hour >= 18:
                    days_ahead = 7
                elif days_ahead == 0 and now.hour < 8:
                    pass
                else:
                    days_ahead = days_ahead if days_ahead > 0 else 7
                window_start = (now + timedelta(days=days_ahead)).replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
                window_end = window_start.replace(hour=18, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
            elif day_name == "tomorrow":
                window_start = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
                window_end = window_start.replace(hour=18, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

    time_of_day = state.get("preferred_time_of_day")
    if time_of_day == "morning":
        window_start = window_start.replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        window_end = window_start.replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    elif time_of_day == "afternoon":
        window_start = window_start.replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        window_end = window_start.replace(hour=17, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    elif time_of_day == "evening":
        window_start = window_start.replace(hour=17, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        window_end = window_start.replace(hour=21, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

    if window_end <= window_start:
        window_end = window_start + timedelta(hours=10)
    return (window_start, window_end)

def derive_window_from_events(state):

    if state.get("after_event_title"):

        events = find_meetings(
            title=state["after_event_title"],
            date=state.get("preferred_date"),
            credentials=state.get("google_credentials")
        )

        if events:
            ref = events[0]
            start = datetime.fromisoformat(ref["end"])
            end = start + timedelta(hours=6)
            return start, end

    if state.get("before_event_title"):

        events = find_meetings(
            title=state["before_event_title"],
            date=state.get("preferred_date"),
            credentials=state.get("google_credentials")
        )

        if events:
            ref = events[0]
            end = datetime.fromisoformat(ref["start"])
            start = end - timedelta(hours=6)
            return start, end

    if state.get("between_event_titles"):

        t1, t2 = state["between_event_titles"]

        e1 = find_meetings(title=t1, credentials=state.get("google_credentials"))
        e2 = find_meetings(title=t2, credentials=state.get("google_credentials"))

        if e1 and e2:
            start = datetime.fromisoformat(e1[0]["end"])
            end = datetime.fromisoformat(e2[0]["start"])
            return start, end

    return None