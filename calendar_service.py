"""
Google Calendar API wrapper: free/busy lookup and event creation.
Uses service account auth via GOOGLE_APPLICATION_CREDENTIALS (path to JSON key).
Set CALENDAR_ID to the calendar to query (e.g. primary or the service account's email).
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build

# Scope for reading busy and creating events
SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/calendar.events"]

_service = None


def _get_service(credentials: Optional[dict] = None):
    """Build a Google Calendar service using either provided OAuth credentials or a service account.

    If `credentials` is provided, it's expected to be a dict that can be passed into
    google.oauth2.credentials.Credentials (see google_oauth.credentials_to_dict).
    """

    # OAuth credentials take priority; these are per-session and not shared.
    if credentials:
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request

            creds = Credentials(
                token=credentials.get("token"),
                refresh_token=credentials.get("refresh_token"),
                token_uri=credentials.get("token_uri"),
                client_id=credentials.get("client_id"),
                client_secret=credentials.get("client_secret"),
                scopes=credentials.get("scopes"),
            )
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            print(f"Google OAuth credential error: {e}")
            # Fall back to service account if OAuth is invalid

    global _service
    if _service is not None:
        return _service
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path or not os.path.isfile(creds_path):
        return None
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    _service = build("calendar", "v3", credentials=creds)
    return _service


def get_calendar_id() -> str:
    return os.getenv("CALENDAR_ID", "primary")


def find_available_slots(
    duration_minutes: int,
    window_start: datetime,
    window_end: datetime,
    calendar_id: Optional[str] = None,
    timezone: str = "UTC",
    max_slots: int = 5,
    credentials: Optional[dict] = None,
) -> list[dict]:
    """
    Return list of available slots in the given window, each at least duration_minutes long.
    Each slot is {"start": iso str, "end": iso str}.
    """
    print(f"[DEBUG] Finding available slots: duration={duration_minutes}min, window={window_start} to {window_end}, max_slots={max_slots}")
    service = _get_service(credentials)
    if service is None:
        print("[DEBUG] No calendar service available")
        return []

    calendar_id = calendar_id or get_calendar_id()
    time_min = window_start.isoformat()
    time_max = window_end.isoformat()

    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": calendar_id}],
    }
    try:
        result = service.freebusy().query(body=body).execute()
    except Exception as e:
        print(f"Calendar freebusy error: {e}")
        return []

    busy_list = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    # Sort busy intervals by start
    busy_sorted = sorted(busy_list, key=lambda x: x["start"])
    slots = []
    current = window_start
    duration_delta = timedelta(minutes=duration_minutes)

    for b in busy_sorted:
        b_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
        b_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
        # Free gap from current to b_start
        if b_start > current and (b_start - current) >= duration_delta:
            slot_end = current + duration_delta
            slots.append({
                "start": current.isoformat(),
                "end": slot_end.isoformat(),
            })
            if len(slots) >= max_slots:
                return slots
        current = max(current, b_end)

    # After last busy, to window_end
    if window_end > current and (window_end - current) >= duration_delta:
        slot_end = current + duration_delta
        slots.append({
            "start": current.isoformat(),
            "end": slot_end.isoformat(),
        })

    print(f"[DEBUG] Found {len(slots)} available slots")
    return slots


def create_event(
    start_iso: str,
    end_iso: str,
    title: str = "Scheduled meeting",
    calendar_id: Optional[str] = None,
    attendees: Optional[list[str]] = None,
    reminder_minutes: int = 15,
    credentials: Optional[dict] = None,
) -> Optional[dict]:
    """Create a calendar event. Returns the event dict or None on failure."""
    print(f"[DEBUG] Creating event: {title} from {start_iso} to {end_iso}")
    service = _get_service(credentials)
    if service is None:
        print("[DEBUG] No calendar service for event creation")
        return None

    calendar_id = calendar_id or get_calendar_id()
    body = {
        "summary": title,
        "start": {"dateTime": start_iso, "timeZone": "UTC"},
        "end": {"dateTime": end_iso, "timeZone": "UTC"},
    }
    if reminder_minutes > 0:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": reminder_minutes}],
        }
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]
    try:
        event = service.events().insert(calendarId=calendar_id, body=body).execute()
        print(f"[DEBUG] Event created successfully: {event.get('id')}")
        return event
    except Exception as e:
        print(f"[ERROR] Calendar create event error: {e}")
        return None
