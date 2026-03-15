from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from calendar_service import (
    find_available_slots,
    create_event,
    find_meetings,
    delete_event,
    update_event_time,
)

from conversation import (
    _format_slot,
    append_message,
    extract_slots_from_message,
    format_state_for_prompt,
    get_google_credentials,
    get_messages,
    get_or_create_session,
    get_state,
    match_user_choice_to_slot,
    state_to_search_window,
    set_google_credentials,
    update_state,
)
from google_oauth import (
    create_authorization_url,
    credentials_to_dict,
    exchange_code_for_credentials,
)
from llm_client import (
    OPENAI_FALLBACK_MODEL,
    generate_reply_with_context,
    get_default_model,
    get_gemini_models,
    normalize_model_name,
)


BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Smart Scheduler AI Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class ConverseRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    timezone: Optional[str] = None


class ConverseResponse(BaseModel):
    reply: str
    session_id: str


@app.get("/")
async def serve_index() -> FileResponse:
    """
    Minimal web UI.
    """

    index_path = BASE_DIR / "static" / "index.html"
    return FileResponse(index_path)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """
    Serve a blank favicon to avoid 404 errors.
    """
    return Response(status_code=204)


# Keep last N turns for context (user + assistant pairs)
CONVERSATION_HISTORY_LIMIT = 20


@app.post("/api/converse", response_model=ConverseResponse)
async def converse(req: ConverseRequest) -> ConverseResponse:
    """
    Stateful conversation: session store holds message history and
    extracted scheduling state; LLM gets state + history for slot-filling.
    """
    session_id = req.session_id or "session-1"
    model_name = req.model or get_default_model()
    user_timezone = req.timezone or "UTC"

    get_or_create_session(session_id)
    append_message(session_id, "user", req.message)

    # If using the dumbbot proxy, still apply simple rule-based slot extraction.
    # Otherwise, let the LLM parse the message and return STATE_UPDATE JSON.
    normalized_model = normalize_model_name(model_name)
    if normalized_model == "dumbbot":
        updates = extract_slots_from_message(req.message)
        if updates:
            update_state(session_id, updates)

    state = get_state(session_id)
    print(f"[DEBUG] State after initial extraction: {state}")

    # If user might be confirming one of the proposed slots, book it
    proposed = state.get("proposed_slots") or []
    chosen = match_user_choice_to_slot(req.message, proposed)

    google_creds = get_google_credentials(session_id)

    if state.get("action") == "schedule" and chosen and not state.get("confirmed_slot") and google_creds and state.get("title"):
        title = state.get("title") or "Scheduled meeting"
        reminder_minutes = state.get("reminder_minutes") or 15
        event = create_event(
            chosen["start"],
            chosen["end"],
            title=title,
            reminder_minutes=reminder_minutes,
            credentials=google_creds,
        )
        if event:
            update_state(session_id, {"confirmed_slot": chosen, "proposed_slots": None})
        # else keep proposed_slots so the LLM can ask to retry

    # If we have enough to search and no confirmed booking yet, query calendar.
    if state.get("action") == "schedule" and not state.get("confirmed_slot") and state.get("duration_minutes"):
        if not google_creds:
            # Prompt the user to connect their Google account.
            reply = (
                "To look at your calendar and propose times, please connect your Google account "
                "using the \"Connect calendar\" button."
            )
            return ConverseResponse(reply=reply, session_id=session_id)

        window = state_to_search_window(state)
        if window:
            window_start, window_end = window
            print(f"[DEBUG] Searching calendar for slots: {window_start} to {window_end}")
            slots = find_available_slots(
                state["duration_minutes"],
                window_start,
                window_end,
                max_slots=5,
                credentials=google_creds,
            )
            print(f"[DEBUG] Found slots: {slots}")
            update_state(session_id, {"proposed_slots": slots})

    state = get_state(session_id)
    state_str = format_state_for_prompt(state, user_timezone)
    history = get_messages(session_id, last_n=CONVERSATION_HISTORY_LIMIT)
    # Exclude the message we just appended so the model sees history only
    history = history[:-1] if history and history[-1].get("role") == "user" else history

    llm_result = generate_reply_with_context(
        model_name, req.message, state_str, history, user_timezone
    )
    reply = llm_result["reply"]
    updates = llm_result["updates"]
    print(f"[DEBUG] LLM result: reply='{reply}', updates={updates}")
    if updates:
        update_state(session_id, updates)
        print(f"[DEBUG] State after LLM updates: {get_state(session_id)}")

    # After LLM updates, check if we need to book
    state = get_state(session_id)
    print(state)
    if state.get("action") == "schedule" and state.get("confirmed_slot") and not state.get("booked") and state.get("title"):
        google_creds = get_google_credentials(session_id)
        if google_creds:
            chosen = state["confirmed_slot"]
            print(f"[DEBUG] Attempting to book confirmed slot: {chosen}")
            title = state.get("title") or "Scheduled meeting"
            reminder_minutes = state.get("reminder_minutes") or 15
            event = create_event(
                chosen["start"],
                chosen["end"],
                title=title,
                reminder_minutes=reminder_minutes,
                credentials=google_creds,
            )
            if event:
                update_state(session_id, {"booked": True})
                print("[DEBUG] Confirmed slot booked successfully")
            else:
                print("[DEBUG] Booking failed, finding next slot")
                # Booking failed, find next available slot
                if state.get("after_event_title") or state.get("before_event_title") or state.get("between_event_titles"):
                    window = derive_window_from_events(state)
                else:
                    window = state_to_search_window(state)

                if window:
                    taken_start = chosen["start"]
                    all_slots = find_available_slots(
                        state["duration_minutes"],
                        window[0],
                        window[1],
                        max_slots=10,
                        credentials=google_creds,
                    )
                    next_slots = [s for s in all_slots if s["start"] != taken_start][:3]  # Get up to 3 alternatives
                    if next_slots:
                        update_state(session_id, {"proposed_slots": next_slots, "confirmed_slot": None})
                        print(f"[DEBUG] Proposed alternative slots: {next_slots}")
                        # Regenerate reply with LLM suggesting alternatives
                        state = get_state(session_id)
                        state_str = format_state_for_prompt(state, user_timezone)
                        history = get_messages(session_id, last_n=CONVERSATION_HISTORY_LIMIT)
                        # Add a system message indicating booking failure
                        special_message = "The selected time slot is not available. Please suggest alternative times from the proposed slots."
                        llm_result = generate_reply_with_context(
                            model_name, special_message, state_str, history, user_timezone
                        )
                        reply = llm_result["reply"]
                        updates = llm_result["updates"]
                        if updates:
                            update_state(session_id, updates)
                        print(f"[DEBUG] Alternative suggestion reply: {reply}")
                    else:
                        reply += " That time is taken, and no other slots found in the window. Please suggest another day or time."
                        print("[DEBUG] No alternative slots found")
                else:
                    reply += " (Booking failed, please try again.)"

    if state.get("action") == "find" and google_creds:
        meetings = find_meetings(
            title=state.get("title"),
            date=datetime.strptime(state.get("preferred_date"), "%Y-%m-%d").date() if state.get("preferred_date") else None,
            start_time=datetime.combine(datetime.today(), datetime.strptime(state.get("preferred_time"), "%H:%M").time()) if state.get("preferred_time") else None,
            user_timezone=user_timezone,
            credentials=google_creds,
        )

        if meetings:
            formatted = "\n".join(
                f"{m['title']} at {m['start']}" for m in meetings[:]
            )
            reply = f"I found these meetings:\n{formatted}"
        else:
            reply = "I couldn't find any matching meetings."

        update_state(session_id, {"search_results": meetings})

    if state.get("action") == "delete" and google_creds:
        meetings = find_meetings(
            title=state.get("title"),
            date=datetime.strptime(state.get("preferred_date"), "%Y-%m-%d").date() if state.get("preferred_date") else None,
            credentials=google_creds,
        )

        if not meetings:
            reply = "I couldn't find a meeting matching that."

        else:
            event = meetings[0]
            success = delete_event(event["id"], credentials=google_creds)

            if success:
                reply = f"I cancelled the meeting '{event['title']}'."
            else:
                reply = "I couldn't cancel the meeting."

    if state.get("action") == "reschedule" and google_creds:

        meetings = find_meetings(
            title=state.get("title"),
            date=None,
            credentials=google_creds,
        )

        if not meetings:
            reply = "I couldn't find the meeting you want to move."

        else:
            event = meetings[0]

            if state.get("after_event_title") or state.get("before_event_title") or state.get("between_event_titles"):
                window = derive_window_from_events(state)
            else:
                window = state_to_search_window(state)


            if not window:
                reply = "What time should I move it to?"

            else:
                slots = find_available_slots(
                    state["duration_minutes"],
                    window[0],
                    window[1],
                    credentials=google_creds,
                )

                if not slots:
                    reply = "I couldn't find a free slot to move it."

                else:
                    slot = slots[0]

                    updated = update_event_time(
                        event["id"],
                        slot["start"],
                        slot["end"],
                        credentials=google_creds,
                    )

                    if updated:
                        reply = f"I moved '{event['title']}' to {slot['start']}."
                    else:
                        reply = "I couldn't reschedule that meeting."

    append_message(session_id, "assistant", reply)
    return ConverseResponse(reply=reply, session_id=session_id)


@app.get("/api/models")
async def list_models() -> dict:
    """
    Return the available LLM model options so the frontend
    can populate its dropdown dynamically.
    """
    gemini_models = get_gemini_models()
    default_model = get_default_model()
    return {
        "default_model": default_model,
        "gemini": gemini_models,
        "openai": [OPENAI_FALLBACK_MODEL],
        "dumbbot": ["dumbbot"],
    }


@app.get("/api/auth_url")
async def get_auth_url(session_id: Optional[str] = None, request: Request = None) -> dict:
    """Return an authorization URL the client can redirect the user to."""
    session_id = session_id or "session-1"
	public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")

	if not public_domain:
		redirect_uri = request.url_for("oauth2callback", _scheme="https")
	else:
		redirect_uri = 'https://' + public_domain + '/api/oauth2callback'
    auth_url = create_authorization_url(session_id, redirect_uri)
    return {"auth_url": auth_url, "session_id": session_id}


@app.post("/api/logout")
async def logout(session_id: Optional[str] = None) -> dict:
    """Log out by clearing Google credentials for the session."""
    session_id = session_id or "session-1"
    set_google_credentials(session_id, None)
    return {"message": "Logged out successfully"}


@app.get("/api/oauth2callback")
async def oauth2callback(request: Request, state: str, code: Optional[str] = None, error: Optional[str] = None):
    """OAuth2 callback for Google sign-in."""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")

    session_id, creds = exchange_code_for_credentials(state, code)
    # Store credentials in session state for later calendar operations.
    set_google_credentials(session_id, credentials_to_dict(creds))

    # Redirect back to the UI, preserving the session_id.
    redirect_url = f"/?session_id={session_id}&connected=1"
    return RedirectResponse(redirect_url)


# If you want to run with: python main.py
if __name__ == "__main__":
    import os
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

