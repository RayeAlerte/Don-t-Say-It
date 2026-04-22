from pydantic import BaseModel, Field
from typing import Optional

class ActionPayload(BaseModel):
    action: str = Field(..., description="The event type: join, lock_trap, lock_word, bounty_guess")
    prompt: Optional[str] = None
    word: Optional[str] = None
    latency_ms: Optional[float] = None
    client_ts: Optional[float] = None

class ErrorPayload(BaseModel):
    status: str = "error"
    message: str

class SuccessPayload(BaseModel):
    status: str = "success"
    message: str