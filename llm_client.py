import os
from typing import Optional, List

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
    return _gemini_default_model or DEFAULT_GEMINI_MODEL_FALLBACK


def _ensure_openai() -> Optional[OpenAI]:
    global _openai_client
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    if _openai_client is None:
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def _call_gemini(model: str, user_message: str) -> Optional[str]:
    if not _ensure_gemini():
        return None
    # Attempt with requested model first
    try:
        m = genai.GenerativeModel(model)
        prompt = f"{SYSTEM_PROMPT}\n\nUser: {user_message}\nAssistant:"
        resp = m.generate_content(prompt)
        text = getattr(resp, "text", None)
        return text.strip() if text else None
    except Exception as exc:
        print(f"Gemini call failed for model '{model}': {exc}")
        # On failure (e.g. 404), list models to help the developer see options.
        available = _list_gemini_models()
        if available:
            print("Gemini models you can use (generateContent supported):")
            for name in available:
                print(f"  - {name}")
        return None


def _call_openai(model: str, user_message: str) -> Optional[str]:
    client = _ensure_openai()
    if client is None:
        return None
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
    )
    choice = resp.choices[0].message
    return (choice.content or "").strip()


def generate_reply_stub(model: Optional[str], user_message: str) -> str:
    """
    Stub behavior when no API keys are configured or an error occurs.
    """
    normalized = normalize_model_name(model)
    prefix = f"[Stub / {normalized}] "

    if "schedule" in user_message.lower() or "meeting" in user_message.lower():
        return (
            prefix
            + "I can help you schedule a meeting. "
            + "To start, how long should the meeting be?"
        )

    return prefix + (
        "You said: "
        + user_message
        + ". For scheduling, you can say things like 'I need to schedule a meeting.'"
    )


def generate_reply(model: Optional[str], user_message: str) -> str:
    """
    Main entry point used by the API.

    It will try the requested provider (Gemini vs OpenAI). If no API key is
    present or a call fails, it falls back to a simple stub so the UI stays
    functional.
    """
    normalized = normalize_model_name(model)

    try:
        if normalized.startswith("models/gemini"):
            text = _call_gemini(normalized, user_message)
            if text:
                return f"[Gemini] {text}"
        elif normalized.startswith("gpt"):
            text = _call_openai(normalized, user_message)
            if text:
                return f"[OpenAI] {text}"
    except Exception as exc:
        # In a real app we would log this more robustly.
        print(f"LLM error for model {normalized}: {exc}")

    # Fallback if provider missing or failed.
    return generate_reply_stub(normalized, user_message)
