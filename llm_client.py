import ast
import json
import os
from typing import AsyncGenerator, List, Optional

import google.generativeai as genai


DEFAULT_GEMINI_MODEL = "models/gemini-2.5-flash"

_gemini_configured = False
_gemini_models_cached: Optional[List[str]] = None
_gemini_default_model: Optional[str] = None

SYSTEM_PROMPT = (
    "You are a helpful smart scheduling assistant. "
    "Have short, natural, conversational replies. "
    "Ask clarifying questions about meeting duration, date, and time preferences "
    "before proposing times. Do not invent calendar data yet; just reason about the "
    "request at a high level."
)

SCHEDULING_SYSTEM_PROMPT = (
    "You are a smart scheduling assistant. You help the user find a meeting time. "

    "Possible actions:\n"
    "- schedule → create a meeting\n"
    "- find → search meetings\n"
    "- delete → cancel a meeting\n"
    "- reschedule → move a meeting to a new time\n\n"

    "Use the 'Current scheduling state' below to remember what you already know.\n"

    "When scheduling collect:\n"
    "- duration_minutes\n"
    "- preferred_date\n"
    "- preferred_time_of_day\n"
    "- title\n"
    "- reminder_minutes\n\n"

    "When finding meetings set action='find'.\n"
    "If wanting a meeting some time before/after another meeting, set offset accordingly, in minutes (e.g. offset=-30 for 30 minutes before).\n"
    "When deleting meetings set action='delete'.\n"
    "When rescheduling meetings set action='reschedule'.\n\n"

    "For rescheduling extract:\n"
    "- title\n"
    "- preferred_date\n"
    "- preferred_time_of_day\n\n"

    "When a User wants to schedule a meeting before/after/or in between two events, update state with the before_event_title, after_event_title, or between_event_titles accordingly" 


    "Use urrent_datetime to resolve relative dates and times. Unless mentioned, always assume the year, month, and time of booking to be relative."
    "Ignore the timezone while interpreting relative dates and times. "
    "If key details are missing (e.g. duration, day, time of day, title, reminder), ask for them one at a time. Reminder defaults to 15 minutes if not specified."
    "When 'Proposed slots' appear in the state, offer those times to the user (e.g. 'I have 2:00 PM or 4:30 PM on Tuesday. Which works for you?'). "
    "When the user picks one (e.g. 'first one', '2 PM'), update confirmed_slot and confirm that the meeting is booked. "
    "If the state is missing details, try to infer them from the user message and include them in a STATE_UPDATE even if you still need to ask clarifying questions. "
    "If the state says 'None found in the requested window', suggest trying another day or time. "
    "Keep replies short and natural. Do not invent times; only use the proposed slots from the state when present. "
    "Do not set confirmed_slot until all required details (duration, date, time, title) are known. "
    "If the user provides a complete specific time (e.g., 'March 30th at 7 PM' and duration is known), infer the slot and set confirmed_slot to a dict with 'start' and 'end' ISO timestamps (e.g., {\"start\": \"2026-03-30T19:00:00Z\", \"end\": \"2026-03-30T20:00:00Z\"}). "
    "If you update the state (e.g., set confirmed_slot, duration_minutes, preferred_date, excluded_days, title, etc.), output at the end of your reply: STATE_UPDATE: {json_dict_with_all_updates}"
    "Example: STATE_UPDATE: {'duration_minutes': 60, 'preferred_date': '2026-03-30', 'preferred_time_of_day': 'evening', 'excluded_days': ['2026-03-31'], 'title': 'Team meeting', 'reminder_minutes': 15, 'confirmed_slot': {'start': '2026-03-30T19:00:00Z', 'end': '2026-03-30T20:00:00Z'}}"
    "Only include fields you are updating; omit if not applicable."
    "Remember to keep the conversation natural and helpful, and only include the STATE_UPDATE when you have new information to add to the scheduling state."
)


def normalize_model_name(name: Optional[str]) -> str:
    """
    Normalize arbitrary UI choices into internal model identifiers.

    For now we support:
    - gemini models
    """
    if not name:
        return DEFAULT_GEMINI_MODEL

    key = name.strip().lower()
    print(f"Normalizing model name: {key}")
    if key in {"gemini", "models/gemini-2.5-flash"}:
        print(f"Default Gemini model: {DEFAULT_GEMINI_MODEL}")
        return DEFAULT_GEMINI_MODEL
    if key == "dumbbot":
        return "dumbbot"

    # Fallback: assume the caller passed a concrete model name already.
    return name


def _ensure_gemini() -> bool:
    global _gemini_configured
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("No GEMINI_API_KEY found in environment; Gemini calls will fall back to stub.")
        return False
    if not _gemini_configured:
        genai.configure(api_key=api_key)
        _gemini_configured = True
    return True


def _list_gemini_models() -> List[str]:
    """
    Fetch and cache available Gemini models that support generateContent.
    This is mainly for debugging / developer visibility.
    """
    global _gemini_models_cached
    if _gemini_models_cached is not None:
        return _gemini_models_cached

    if not _ensure_gemini():
        return []

    try:
        models = genai.list_models()
        names: List[str] = []
        for m in models:
            # Some SDK versions expose supported methods via .supported_generation_methods
            methods = getattr(m, "supported_generation_methods", [])
            if "generateContent" in methods:
                names.append(getattr(m, "name", ""))
        _gemini_models_cached = names
        # Choose the first suitable model as our default if not already set.
        global _gemini_default_model
        if names and _gemini_default_model is None:
            _gemini_default_model = names[0]
        print("Available Gemini models supporting generateContent:")
        for n in names:
            print(f"  - {n}")
        return names
    except Exception as exc:
        print(f"Error while listing Gemini models: {exc}")
        return []


def get_gemini_models() -> List[str]:
    """
    Public accessor for Gemini models; triggers discovery once.
    """
    return _list_gemini_models()


def get_default_model() -> str:
    """
    Default model used when the client doesn't specify one.
    Prefer the first discovered Gemini model that supports generateContent,
    and fall back to a hard-coded reasonable default if discovery fails.
    """
    global _gemini_default_model
    # Ensure discovery has run at least once if we have an API key.
    if _gemini_default_model is None and os.getenv("GEMINI_API_KEY"):
        _list_gemini_models()
    return _gemini_default_model or DEFAULT_GEMINI_MODEL

def _call_gemini(model: str, user_message: str) -> Optional[str]:
    return _call_gemini_prompt(model, f"{SYSTEM_PROMPT}\n\nUser: {user_message}\nAssistant:")


def _call_gemini_prompt(model: str, full_prompt: str) -> Optional[str]:
    if not _ensure_gemini():
        return None
    try:
        m = genai.GenerativeModel(model)
        resp = m.generate_content(full_prompt)
        text = getattr(resp, "text", None)
        return text.strip() if text else None
    except Exception as exc:
        print(f"Gemini call failed for model '{model}': {exc}")
        available = _list_gemini_models()
        if available:
            print("Gemini models you can use (generateContent supported):")
            for name in available:
                print(f"  - {name}")
        return None
        return None

def generate_reply_stub(model: Optional[str], user_message: str, state_str: str = "", history: list[dict] = None) -> str:
    """
    Stub behavior when no API keys are configured or an error occurs.
    Asks for required details in order: duration, day, time of day.
    """
    normalized = normalize_model_name(model)
    prefix = f"[Stub / {normalized}] "

    # Check if it's a scheduling request
    if "schedule" in user_message.lower() or "meeting" in user_message.lower() or "Duration:" in state_str or "Preferred day/date:" in state_str or "Time of day:" in state_str or "Title:" in state_str or "Reminder:" in state_str:
        # Ask in order based on what's missing in state_str
        if "Duration:" not in state_str:
            return prefix + "I can help you schedule a meeting. To start, how long should the meeting be? (e.g., 30 minutes, 1 hour)"
        # elif "Title:" not in state_str:
        #     return prefix + "Got the duration. What should the meeting be called? (e.g., Team standup)"
        elif "Preferred day/date:" not in state_str:
            return prefix + "Got the title. What day would you like to schedule it? (e.g., tomorrow, next Tuesday)"
        elif "Time of day:" not in state_str:
            return prefix + "Got the day. What time of day works best? (e.g., morning, afternoon, evening)"
        # elif "Reminder:" not in state_str:
        #     return prefix + "Got the time. How many minutes before the meeting should I remind you? (e.g., 15, or 0 for no reminder, default is 15)"
        else:
            return prefix + "I have all the details: " + state_str + ". In a real setup, I'd check your calendar and propose times. For now, let's say the meeting is booked!"

    return prefix + (
        "You said: "
        + user_message
        + ". For scheduling, you can say things like 'I need to schedule a meeting.'"
    )


def _build_scheduling_prompt(state_str: str, history: list[dict], user_message: str, user_timezone: str = "UTC") -> str:
    lines = [
        SCHEDULING_SYSTEM_PROMPT,
        "",
        f"User's timezone: {user_timezone}",
        "",
        "Current scheduling state:",
        state_str,
        "",
        "Conversation so far:",
    ]
    for m in history:
        role = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {m.get('content', '')}")
    lines.extend(["", f"User: {user_message}", "Assistant:"])
    return "\n".join(lines)


def _build_scheduling_messages(
    state_str: str, history: list[dict], user_message: str, user_timezone: str = "UTC"
) -> list[dict]:
    system = (
        SCHEDULING_SYSTEM_PROMPT
        + f"\n\nUser's timezone: {user_timezone}"
        + "\n\nCurrent scheduling state:\n"
        + state_str
    )
    messages = [{"role": "system", "content": system}]
    for m in history:
        role = m.get("role", "user")
        if role == "assistant":
            role = "assistant"
        else:
            role = "user"
        messages.append({"role": role, "content": m.get("content", "")})
    messages.append({"role": "user", "content": user_message})
    return messages


def _parse_llm_response(text: str) -> dict:
    """
    Parse LLM response for state updates.
    Looks for STATE_UPDATE: {json} at the end of the reply.
    Returns {"reply": str, "updates": dict}
    """
    print(f"[DEBUG] LLM raw response: {text}")
    if "STATE_UPDATE:" in text:
        parts = text.rsplit("STATE_UPDATE:", 1)
        reply = parts[0].strip()
        update_str = parts[1].strip()
        try:
            updates = json.loads(update_str)
            print(f"[DEBUG] Parsed STATE_UPDATE: {updates}")
            return {"reply": reply, "updates": updates}
        except json.JSONDecodeError:
            # Try parsing as Python literal (handles single quotes)
            try:
                updates = ast.literal_eval(update_str)
                print(f"[DEBUG] Parsed STATE_UPDATE with ast.literal_eval: {updates}")
                return {"reply": reply, "updates": updates}
            except (ValueError, SyntaxError) as e:
                print(f"[ERROR] Failed to parse STATE_UPDATE JSON or literal: {update_str} - {e}")
                return {"reply": text, "updates": {}}
    print("[DEBUG] No STATE_UPDATE found in response")
    return {"reply": text, "updates": {}}


class StreamingStateFilter:
    """
    Filters STATE_UPDATE: {json} out of a streaming LLM response so it never
    appears in the displayed text or TTS output.
    """
    MARKER = "STATE_UPDATE:"

    def __init__(self):
        self._display_buf = ""
        self._tail_buf = ""

    def feed(self, chunk: str) -> str:
        """Returns the display-safe portion of chunk; holds back STATE_UPDATE."""
        combined = self._tail_buf + chunk
        marker_pos = combined.find(self.MARKER)
        if marker_pos != -1:
            safe = combined[:marker_pos]
            self._display_buf += safe
            self._tail_buf = combined[marker_pos:]
            return safe
        hold = len(self.MARKER) - 1
        safe = combined[:-hold] if len(combined) > hold else ""
        self._tail_buf = combined[-hold:] if len(combined) > hold else combined
        self._display_buf += safe
        return safe

    def finalize(self) -> dict:
        """Parse STATE_UPDATE from buffered tail. Returns updates dict."""
        tail = self._tail_buf
        if self.MARKER in tail:
            update_str = tail.rsplit(self.MARKER, 1)[1].strip()
            try:
                return json.loads(update_str)
            except json.JSONDecodeError:
                try:
                    return ast.literal_eval(update_str)
                except (ValueError, SyntaxError):
                    pass
        return {}

    @property
    def full_display_text(self) -> str:
        return self._display_buf


async def _call_gemini_prompt_stream(model: str, full_prompt: str):
    """Async generator yielding raw text chunks from Gemini streaming."""
    if not _ensure_gemini():
        return
    try:
        m = genai.GenerativeModel(model)
        response = await m.generate_content_async(full_prompt, stream=True)
        async for chunk in response:
            text = getattr(chunk, "text", None)
            if text:
                yield text
    except Exception as exc:
        print(f"Gemini streaming call failed for model '{model}': {exc}")
        return


async def stream_reply_with_context(
    model: Optional[str],
    user_message: str,
    state_str: str,
    history: list[dict],
    user_timezone: str = "UTC",
) -> AsyncGenerator[dict, None]:
    """
    Async generator. Yields dicts:
      {"type": "chunk", "text": str}             -- display-safe LLM text fragments
      {"type": "done", "updates": dict,
       "full_reply": str}                        -- final event with parsed STATE_UPDATE
    Falls back to generate_reply_with_context for non-Gemini models (single chunk + done).
    """
    normalized = normalize_model_name(model)
    if not normalized.startswith("models/"):
        result = generate_reply_with_context(model, user_message, state_str, history, user_timezone)
        yield {"type": "chunk", "text": result["reply"]}
        yield {"type": "done", "updates": result["updates"], "full_reply": result["reply"]}
        return

    prompt = _build_scheduling_prompt(state_str, history, user_message, user_timezone)
    state_filter = StreamingStateFilter()
    try:
        async for raw_chunk in _call_gemini_prompt_stream(normalized, prompt):
            display_text = state_filter.feed(raw_chunk)
            if display_text:
                yield {"type": "chunk", "text": display_text}
        updates = state_filter.finalize()
        yield {"type": "done", "updates": updates, "full_reply": state_filter.full_display_text}
    except Exception as exc:
        print(f"[ERROR] Streaming LLM error: {exc}")
        yield {"type": "chunk", "text": "Sorry, I encountered an error. Please try again."}
        yield {"type": "done", "updates": {}, "full_reply": ""}


def generate_reply_with_context(
    model: Optional[str],
    user_message: str,
    state_str: str,
    history: list[dict],
    user_timezone: str = "UTC",
) -> dict:
    """
    Generate a reply using current scheduling state and conversation history.
    Returns {"reply": str, "updates": dict} where updates is state changes from LLM.
    """
    normalized = normalize_model_name(model)
    print(f"[DEBUG] Generating reply with model: {normalized}, user_message: {user_message}")
    print(f"[DEBUG] Current state: {state_str}")
    print(f"[DEBUG] Conversation history: {history}")
    try:
        if normalized.startswith("models/"):
            prompt = _build_scheduling_prompt(state_str, history, user_message, user_timezone)
            print(f"[DEBUG] Gemini prompt: {prompt}")
            text = _call_gemini_prompt(normalized, prompt)
            if text:
                return _parse_llm_response(text)
        elif normalized == "dumbbot":
            reply = generate_reply_stub(normalized, user_message, state_str, history)
            print(f"[DEBUG] Dumbbot reply: {reply}")
            return {"reply": reply, "updates": {}}
    except Exception as exc:
        print(f"[ERROR] LLM error for model {normalized}: {exc}")
    reply = generate_reply_stub(normalized, user_message, state_str, history)
    print(f"[DEBUG] Fallback stub reply: {reply}")
    return {"reply": reply, "updates": {}}


def generate_reply(model: Optional[str], user_message: str) -> str:
    """
    Main entry point used by the API (single-turn, no context).
    For stateful scheduling, use generate_reply_with_context instead.
    """
    normalized = normalize_model_name(model)

    try:
        if normalized.startswith("models/gemini"):
            text = _call_gemini(normalized, user_message)
            if text:
                return text
    except Exception as exc:
        print(f"LLM error for model {normalized}: {exc}")

    return generate_reply_stub(normalized, user_message, state_str, history)
