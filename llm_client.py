import ast
import json
import os
from typing import List, Optional

import google.generativeai as genai
from openai import OpenAI


DEFAULT_GEMINI_MODEL = "models/gemini-2.5-flash"
OPENAI_FALLBACK_MODEL = "gpt-4o-mini"

_openai_client: Optional[OpenAI] = None
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
    "Use current date and time to resolve relative dates and times. "
    "Use the user's timezone for interpreting relative dates and times. "
    "Use the 'Current scheduling state' below to remember what you already know. "
    "If key details are missing (e.g. duration, day, time of day, title, reminder), ask for them one at a time. Reminder defaults to 15 minutes if not specified."
    "When 'Proposed slots' appear in the state, offer those times to the user (e.g. 'I have 2:00 PM or 4:30 PM on Tuesday. Which works for you?'). "
    "When the user picks one (e.g. 'first one', '2 PM'), confirm that the meeting is booked. "
    "If the state is missing details, try to infer them from the user message and include them in a STATE_UPDATE even if you still need to ask clarifying questions. "
    "If the state says 'None found in the requested window', suggest trying another day or time. "
    "Keep replies short and natural. Do not invent times; only use the proposed slots from the state when present. "
    "Do not set confirmed_slot until all required details (duration, date, time, title) are known. "
    "If the user provides a complete specific time (e.g., 'March 30th at 7 PM' and duration is known), infer the slot and set confirmed_slot to a dict with 'start' and 'end' ISO timestamps (e.g., {\"start\": \"2026-03-30T19:00:00Z\", \"end\": \"2026-03-30T20:00:00Z\"}). "
    "If you update the state (e.g., set confirmed_slot, duration_minutes, preferred_date, excluded_days, title, etc.), output at the end of your reply: STATE_UPDATE: {json_dict_with_all_updates}"
    "Example: STATE_UPDATE: {'duration_minutes': 60, 'preferred_date': '2026-03-30', 'preferred_time_of_day': 'evening', 'excluded_days': ['2026-03-31'], 'title': 'Team meeting', 'reminder_minutes': 15, 'confirmed_slot': {'start': '2026-03-30T19:00:00Z', 'end': '2026-03-30T20:00:00Z'}}"
    "Only include fields you are updating; omit if not applicable.")


def normalize_model_name(name: Optional[str]) -> str:
    """
    Normalize arbitrary UI choices into internal model identifiers.

    For now we support:
    - \"gemini\" / \"models/gemini-2.5-flash\"
    - \"openai\" / \"gpt-4o-mini\"
    """
    if not name:
        return DEFAULT_GEMINI_MODEL

    key = name.strip().lower()
    print(f"Normalizing model name: {key}")
    if key in {"gemini", "models/gemini-2.5-flash"}:
        print(f"Default Gemini model: {DEFAULT_GEMINI_MODEL}")
        return DEFAULT_GEMINI_MODEL
    if key in {"openai", "gpt-4o", "gpt-4o-mini"}:
        return OPENAI_FALLBACK_MODEL
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


def _ensure_openai() -> Optional[OpenAI]:
    global _openai_client
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    if _openai_client is None:
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


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


def _call_openai(model: str, user_message: str) -> Optional[str]:
    return _call_openai_messages(
        model,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )


def _call_openai_messages(model: str, messages: list[dict]) -> Optional[str]:
    client = _ensure_openai()
    if client is None:
        return None
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.3,
    )
    choice = resp.choices[0].message
    return (choice.content or "").strip()


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
        if normalized.startswith("models/gemini"):
            prompt = _build_scheduling_prompt(state_str, history, user_message, user_timezone)
            print(f"[DEBUG] Gemini prompt: {prompt}")
            text = _call_gemini_prompt(normalized, prompt)
            if text:
                return _parse_llm_response(text)
        elif normalized.startswith("gpt"):
            messages = _build_scheduling_messages(state_str, history, user_message, user_timezone)
            print(f"[DEBUG] OpenAI messages: {messages}")
            text = _call_openai_messages(normalized, messages)
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
        elif normalized.startswith("gpt"):
            text = _call_openai(normalized, user_message)
            if text:
                return text
    except Exception as exc:
        print(f"LLM error for model {normalized}: {exc}")

    return generate_reply_stub(normalized, user_message, state_str, history)
