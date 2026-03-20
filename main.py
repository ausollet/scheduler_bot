import asyncio
import json as _json
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os

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
    derive_window_from_events,
)
from google_oauth import (
    create_authorization_url,
    credentials_to_dict,
    exchange_code_for_credentials,
)
from llm_client import (
    generate_reply_with_context,
    get_default_model,
    get_gemini_models,
    normalize_model_name,
    stream_reply_with_context,
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


def _reset_scheduling_state(session_id: str) -> None:
    sess = get_or_create_session(session_id)
    for key in (
        "duration_minutes", "confirmed_slot", "proposed_slots", "preferred_time",
        "preferred_time_of_day", "title", "action", "after_event_title",
        "before_event_title", "between_event_titles", "preferred_date",
    ):
        sess["state"][key] = None


async def _prepare_session_for_llm(
    session_id: str,
    message: str,
    model_name: str,
    user_timezone: str,
) -> tuple[str, list]:
    """
    Session init, dumbbot slot extraction, calendar slot search, state formatting.
    Appends the user message to history and returns (state_str, history).
    """
    get_or_create_session(session_id)
    append_message(session_id, "user", message)

    normalized_model = normalize_model_name(model_name)
    if normalized_model == "dumbbot":
        updates = extract_slots_from_message(message)
        if updates:
            update_state(session_id, updates)

    state = get_state(session_id)
    print(f"[DEBUG] State after initial extraction: {state}")

    proposed = state.get("proposed_slots") or []
    match_user_choice_to_slot(message, proposed)

    google_creds = get_google_credentials(session_id)

    if state.get("action") == "schedule" and not state.get("confirmed_slot") and state.get("duration_minutes"):
        if google_creds:
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
    history = history[:-1] if history and history[-1].get("role") == "user" else history
    return state_str, history


async def _post_llm_processing(
    session_id: str,
    model_name: str,
    user_timezone: str,
    initial_reply: str,
    updates: dict,
) -> str:
    """
    Applies LLM state updates, runs calendar operations (booking/find/delete/reschedule).
    Returns the final reply string (may differ from initial_reply).
    Does NOT call append_message — the caller is responsible.
    """
    reply = initial_reply
    google_creds = get_google_credentials(session_id)

    print(f"[DEBUG] LLM updates: {updates}")
    if updates:
        update_state(session_id, updates)
        print(f"[DEBUG] State after LLM updates: {get_state(session_id)}")

    state = get_state(session_id)
    print(state)

    if state.get("action") == "schedule" and state.get("confirmed_slot") and state.get("title"):
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
                time_zone=user_timezone,
                credentials=google_creds,
            )
            if event:
                print("[DEBUG] Confirmed slot booked successfully")
                _reset_scheduling_state(session_id)
            else:
                print("[DEBUG] Booking failed, finding next slot")
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
                    next_slots = [s for s in all_slots if s["start"] != taken_start][:3]
                    if next_slots:
                        update_state(session_id, {"proposed_slots": next_slots, "confirmed_slot": None})
                        print(f"[DEBUG] Proposed alternative slots: {next_slots}")
                        state = get_state(session_id)
                        state_str = format_state_for_prompt(state, user_timezone)
                        history = get_messages(session_id, last_n=CONVERSATION_HISTORY_LIMIT)
                        special_message = "The selected time slot is not available. Please suggest alternative times from the proposed slots."
                        llm_result = generate_reply_with_context(
                            model_name, special_message, state_str, history, user_timezone
                        )
                        reply = llm_result["reply"]
                        if llm_result["updates"]:
                            update_state(session_id, llm_result["updates"])
                        print(f"[DEBUG] Alternative suggestion reply: {reply}")
                    else:
                        reply += " That time is taken, and no other slots found in the window. Please suggest another day or time."
                        print("[DEBUG] No alternative slots found")
                else:
                    reply += " (Booking failed, please try again.)"

    state = get_state(session_id)

    if state.get("action") == "find" and google_creds:
        meetings = find_meetings(
            title=state.get("title"),
            date=datetime.strptime(state.get("preferred_date"), "%Y-%m-%d") if state.get("preferred_date") else None,
            start_time=datetime.combine(datetime.strptime(state.get("preferred_date"), "%Y-%m-%d") if state.get("preferred_date") else datetime.today(), datetime.strptime(state.get("preferred_time"), "%H:%M").time()) if state.get("preferred_time") else None,
            user_timezone=user_timezone,
            credentials=google_creds,
        )
        if meetings:
            formatted = "\n".join(f"{m['title']} at {m['start']}" for m in meetings)
            reply = f"I found these meetings:\n{formatted}"
        else:
            reply = "I couldn't find any matching meetings."
        _reset_scheduling_state(session_id)

    if state.get("action") == "delete" and google_creds:
        meetings = find_meetings(
            title=state.get("title"),
            date=datetime.strptime(state.get("preferred_date"), "%Y-%m-%d") if state.get("preferred_date") else None,
            credentials=google_creds,
        )
        if not meetings:
            reply = "I couldn't find a meeting matching that."
        else:
            event = meetings[0]
            success = delete_event(event["id"], credentials=google_creds)
            reply = f"I cancelled the meeting '{event['title']}'." if success else "I couldn't cancel the meeting."
        _reset_scheduling_state(session_id)

    if state.get("action") == "reschedule" and google_creds:
        meetings = find_meetings(
            title=state.get("title"),
            date=datetime.strptime(state.get("preferred_date"), "%Y-%m-%d") if state.get("preferred_date") else None,
            credentials=google_creds,
        )
        if not meetings:
            reply = "I couldn't find the meeting you want to move."
        else:
            event = meetings[0]
            if not state.get("duration_minutes"):
                start = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(event["end"].replace("Z", "+00:00"))
                duration_minutes = int((end - start).total_seconds() / 60)
                update_state(session_id, {"duration_minutes": duration_minutes})
                state = get_state(session_id)

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
                            time_zone=user_timezone,
                            credentials=google_creds,
                        )
                        reply = f"I moved '{event['title']}' to {slot['start']}." if updated else "I couldn't reschedule that meeting."
        _reset_scheduling_state(session_id)

    return reply


def _sse(data: dict) -> str:
    return f"data: {_json.dumps(data)}\n\n"


async def _with_keepalive(gen, interval: float = 8.0):
    """Wraps an async generator, injecting SSE keepalive comments when the
    real generator is slow (e.g. during calendar API calls). This prevents
    Railway's nginx proxy from closing the connection mid-stream."""
    it = gen.__aiter__()
    pending = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if done:
                try:
                    yield pending.result()
                    pending = asyncio.ensure_future(it.__anext__())
                except StopAsyncIteration:
                    break
            else:
                yield ": keepalive\n\n"
    finally:
        pending.cancel()
        try:
            await pending
        except (asyncio.CancelledError, StopAsyncIteration):
            pass


@app.post("/api/converse", response_model=ConverseResponse)
async def converse(req: ConverseRequest) -> ConverseResponse:
    """
    Stateful conversation: session store holds message history and
    extracted scheduling state; LLM gets state + history for slot-filling.
    """
    session_id = req.session_id or "session-1"
    model_name = req.model or get_default_model()
    user_timezone = req.timezone or "UTC"

    state_str, history = await _prepare_session_for_llm(session_id, req.message, model_name, user_timezone)

    google_creds = get_google_credentials(session_id)
    state = get_state(session_id)
    if state.get("action") == "schedule" and not state.get("confirmed_slot") and state.get("duration_minutes") and not google_creds:
        reply = (
            "To look at your calendar and propose times, please connect your Google account "
            "using the \"Connect calendar\" button."
        )
        append_message(session_id, "assistant", reply)
        return ConverseResponse(reply=reply, session_id=session_id)

    llm_result = generate_reply_with_context(model_name, req.message, state_str, history, user_timezone)
    final_reply = await _post_llm_processing(
        session_id, model_name, user_timezone, llm_result["reply"], llm_result["updates"]
    )
    append_message(session_id, "assistant", final_reply)
    return ConverseResponse(reply=final_reply, session_id=session_id)


@app.post("/api/converse/stream")
async def converse_stream(req: ConverseRequest):
    """
    Streaming SSE endpoint. Sends LLM response chunks as they arrive, then
    runs calendar post-processing, and signals completion.

    Events:
      {"type": "chunk", "text": str, "session_id": str}
      {"type": "processing"}
      {"type": "supplement", "text": str}   -- only if calendar ops changed the reply
      {"type": "done", "session_id": str}
      {"type": "error", "message": str}
    """
    session_id = req.session_id or "session-1"
    model_name = req.model or get_default_model()
    user_timezone = req.timezone or "UTC"

    async def event_generator():
        # Flush nginx's internal buffer on Railway so chunks aren't held back
        yield ": " + " " * 4096 + "\n\n"
        try:
            state_str, history = await _prepare_session_for_llm(
                session_id, req.message, model_name, user_timezone
            )

            google_creds = get_google_credentials(session_id)
            state = get_state(session_id)
            if state.get("action") == "schedule" and not state.get("confirmed_slot") and state.get("duration_minutes") and not google_creds:
                no_creds_reply = (
                    "To look at your calendar and propose times, please connect your Google account "
                    "using the \"Connect calendar\" button."
                )
                yield _sse({"type": "chunk", "text": no_creds_reply, "session_id": session_id})
                append_message(session_id, "assistant", no_creds_reply)
                yield _sse({"type": "done", "session_id": session_id})
                return

            full_reply = ""
            llm_updates = {}

            async for event in stream_reply_with_context(
                model_name, req.message, state_str, history, user_timezone
            ):
                if event["type"] == "chunk":
                    full_reply += event["text"]
                    yield _sse({"type": "chunk", "text": event["text"], "session_id": session_id})
                elif event["type"] == "done":
                    llm_updates = event.get("updates", {})
                    if event.get("full_reply"):
                        full_reply = event["full_reply"]

            yield _sse({"type": "processing"})

            final_reply = await _post_llm_processing(
                session_id, model_name, user_timezone, full_reply, llm_updates
            )

            if final_reply != full_reply:
                yield _sse({"type": "supplement", "text": final_reply})

            append_message(session_id, "assistant", final_reply)
            yield _sse({"type": "done", "session_id": session_id})

        except Exception as exc:
            print(f"[ERROR] Stream endpoint error: {exc}")
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        _with_keepalive(event_generator()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


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

