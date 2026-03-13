from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from llm_client import (
    OPENAI_FALLBACK_MODEL,
    generate_reply,
    get_default_model,
    get_gemini_models,
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


@app.post("/api/converse", response_model=ConverseResponse)
async def converse(req: ConverseRequest) -> ConverseResponse:
    """
    Placeholder conversation endpoint.
    For now this just echoes back and keeps a simple session ID.
    """

    session_id = req.session_id or "session-1"
    model_name = req.model or get_default_model()
    reply = generate_reply(model_name, req.message)
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
    }


# If you want to run with: python main.py
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

