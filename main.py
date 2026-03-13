from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Smart Scheduler AI Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConverseRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


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

    # Very simple canned behavior to demonstrate flow.
    user_text = req.message.lower().strip()
    if "schedule" in user_text or "meeting" in user_text:
        reply = (
            "Sure! I can help you schedule your meetings."
            "To start, how long should the meeting be?"
        )
    else:
        reply = f"You said: {req.message}. For scheduling, you can say things like 'I need to schedule a meeting.'"

    return ConverseResponse(reply=reply, session_id=session_id)


# python main.py: http://localhost:8000/
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

